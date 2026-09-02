from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from injection_aps.services import horizon_status
from injection_aps.services.v2_flags import is_v2_enabled


OPEN_STATUSES = ("Open", "Requested", "Approved")
QTY_TOLERANCE = 0.000001
SOLVER_INPUT_BLOCKER_PREFIX = "solver_input|"


def sync_solver_input_blocker(run_doc, *, blocker_key: str | None, message: str | None) -> None:
	"""Persist BOM input failures as Never Override and retire them only after a clean rebuild."""
	if not is_v2_enabled() or not frappe.db.exists("DocType", "APS Constraint Resolution"):
		return
	open_rows = frappe.get_all(
		"APS Constraint Resolution",
		filters={"planning_run": run_doc.name, "blocker_key": ("in", ["bom_cycle", "bom_input"]), "status": ("in", ["Open", "Requested", "Approved"])},
		fields=["name", "blocker_key"],
		limit_page_length=0,
	)
	if not blocker_key:
		for row in open_rows:
			_set_values(row.name, {"status": "Superseded", "resolved_by": frappe.session.user, "resolved_on": now_datetime()})
		return
	input_hash = hashlib.sha256(str(message or "").encode()).hexdigest()
	key = _resolution_key(run_doc.name, input_hash, blocker_key, None, None)
	values = {
		"planning_run": run_doc.name, "company": run_doc.company, "plant_floor": run_doc.get("plant_floor"),
		"blocker_key": blocker_key, "blocker_policy": "Never Override", "severity": "Blocking", "status": "Open",
		"message": message or _("Invalid multilevel BOM input.", context="Injection APS"),
		"suggested_action": _("Correct the BOM master or reapprove the changed BOM selection, then analyze again.", context="Injection APS"),
		"input_fingerprint": input_hash, "idempotency_key": key,
	}
	_upsert_engine_record(values)
	for row in open_rows:
		if row.blocker_key != blocker_key or frappe.db.get_value("APS Constraint Resolution", row.name, "idempotency_key") != key:
			_set_values(row.name, {"status": "Superseded", "resolved_by": frappe.session.user, "resolved_on": now_datetime()})


def sync_solver_input_blockers(
	run_doc: Any,
	blockers: list[dict[str, Any]],
	*,
	input_fingerprint: str | None = None,
) -> str:
	"""Expose pre-job CP-SAT input failures in the Resolution Center."""
	if not is_v2_enabled() or not frappe.db.exists("DocType", "APS Constraint Resolution"):
		return ""
	rows = [dict(row) for row in blockers]
	input_hash = input_fingerprint or hashlib.sha256(
		json.dumps(rows, ensure_ascii=True, sort_keys=True, default=str).encode()
	).hexdigest()
	current_keys = set()
	for blocker in rows:
		result_name = blocker.get("schedule_result")
		result_row = _result_context(result_name)
		commitment = blocker.get("demand_commitment") or _resolve_commitment(
			run_doc.name, result_row
		)
		blocker_key = SOLVER_INPUT_BLOCKER_PREFIX + str(
			blocker.get("blocker_key") or "invalid_input"
		)
		scope_key = str(
			blocker.get("scope_key")
			or blocker.get("demand_key")
			or blocker.get("demand_commitment")
			or ""
		)
		if not result_name and not scope_key:
			scope_key = hashlib.sha256(
				json.dumps(blocker, ensure_ascii=True, sort_keys=True, default=str).encode()
			).hexdigest()
		key = _resolution_key(run_doc.name, input_hash, blocker_key, result_name, scope_key)
		current_keys.add(key)
		policy = blocker.get("blocker_policy") or "Never Override"
		values = {
			"planning_run": run_doc.name,
			"company": run_doc.company,
			"plant_floor": result_row.get("plant_floor") or run_doc.get("plant_floor"),
			"schedule_result": result_name,
			"demand_commitment": commitment,
			"blocker_key": blocker_key,
			"blocker_policy": policy,
			"severity": "Blocking",
			"status": "Open",
			"affected_customer": result_row.get("customer"),
			"affected_item": result_row.get("item_code"),
			"affected_qty": max(
				flt(blocker.get("affected_qty")),
				flt(result_row.get("critical_unplanned_qty")),
				flt(result_row.get("unscheduled_qty")),
				flt(result_row.get("planned_qty")),
				0,
			),
			"message": blocker.get("message")
			or _("Invalid CP-SAT solver input.", context="Injection APS"),
			"suggested_action": _suggested_action(policy),
			"input_fingerprint": input_hash,
			"idempotency_key": key,
		}
		_upsert_engine_record(values)
	for row in frappe.get_all(
		"APS Constraint Resolution",
		filters={
			"planning_run": run_doc.name,
			"blocker_key": ("like", f"{SOLVER_INPUT_BLOCKER_PREFIX}%"),
			"status": ("in", ["Open", "Requested", "Approved"]),
		},
		fields=["name", "idempotency_key"],
		limit_page_length=0,
	):
		if row.idempotency_key not in current_keys:
			_set_values(
				row.name,
				{
					"status": "Superseded",
					"resolved_by": frappe.session.user,
					"resolved_on": now_datetime(),
				},
			)
	return input_hash


def get_constraint_resolutions(planning_run: str) -> dict[str, Any]:
	_require_v2()
	run = frappe.get_doc("APS Planning Run", planning_run)
	rows = frappe.get_all(
		"APS Constraint Resolution",
		filters={"planning_run": run.name, "status": ("!=", "Superseded")},
		fields=[
			"name", "planning_run", "company", "plant_floor", "schedule_result", "schedule_segment",
			"demand_commitment", "blocker_key", "blocker_policy", "severity", "status",
			"affected_customer", "affected_item", "affected_qty", "message", "suggested_action",
			"resolution_type", "reason", "proposed_value_json", "expires_on", "requested_by",
			"requested_on", "approved_by", "approved_on", "resolved_by", "resolved_on",
			"input_fingerprint", "output_fingerprint",
		],
		order_by="status asc, blocker_policy asc, affected_customer asc, affected_item asc, name asc",
		limit_page_length=0,
	)
	groups = {"must_fix": [], "temporary_override": [], "exclude": [], "acknowledgment": []}
	visible_rows = []
	for source in rows:
		row = dict(source)
		if (
			row.get("status") == "Approved"
			and row.get("blocker_policy") == "Temporary Override"
			and (not row.get("expires_on") or get_datetime(row.get("expires_on")) <= get_datetime(now_datetime()))
		):
			# Reads stay side-effect free; an expired override is projected as expired
			# until the next controlled recompute persists the transition.
			row["status"] = "Expired"
		policy = row.get("blocker_policy")
		group = (
			"temporary_override" if policy == "Temporary Override"
			else "exclude" if policy == "Exclude Only"
			else "acknowledgment" if policy == "Acknowledgment"
			else "must_fix"
		)
		groups[group].append(row)
		visible_rows.append(row)
	rows = visible_rows
	temporary_override_supported, temporary_override_reason = _temporary_override_capability()
	return {
		"planning_run": run.name,
		"company": run.company,
		"readiness_status": run.get("capacity_balance_status") or "Not Analyzed",
		"input_fingerprint": run.get("capacity_balance_fingerprint"),
		"temporary_override_supported": temporary_override_supported,
		"temporary_override_reason": temporary_override_reason,
		"summary": {
			"total": len(rows),
			"open": sum(1 for row in rows if row.get("status") in OPEN_STATUSES),
			"must_fix": len(groups["must_fix"]),
			"temporary_override": len(groups["temporary_override"]),
			"exclude": len(groups["exclude"]),
			"excluded": sum(1 for row in rows if row.get("status") == "Excluded"),
		},
		"groups": groups,
		"rows": [dict(row) for row in rows],
	}


def sync_from_analysis(run_doc: Any, analysis: dict[str, Any]) -> None:
	if not is_v2_enabled() or not frappe.db.exists("DocType", "APS Constraint Resolution"):
		return
	current_keys = set()
	for blocker in analysis.get("hard_blockers") or []:
		result = blocker.get("result")
		result_row = _result_context(result)
		commitment = _resolve_commitment(run_doc.name, result_row)
		key = _resolution_key(
			run_doc.name,
			analysis.get("analysis_fingerprint"),
			blocker.get("key"),
			result,
			blocker.get("segment"),
		)
		current_keys.add(key)
		values = {
			"planning_run": run_doc.name,
			"company": run_doc.company,
			"plant_floor": result_row.get("plant_floor") or run_doc.get("plant_floor"),
			"schedule_result": result,
			"schedule_segment": blocker.get("segment"),
			"demand_commitment": commitment,
			"blocker_key": blocker.get("key") or "unknown",
			"blocker_policy": blocker.get("policy") or "Never Override",
			"severity": "Blocking",
			"status": "Open",
			"affected_customer": result_row.get("customer"),
			"affected_item": result_row.get("item_code"),
			"affected_qty": _affected_blocker_qty(blocker, result_row),
			"message": blocker.get("message") or _("Unresolved APS constraint.", context="Injection APS"),
			"suggested_action": _suggested_action(blocker.get("policy")),
			"input_fingerprint": analysis.get("analysis_fingerprint"),
			"idempotency_key": key,
		}
		_upsert_engine_record(values)

	used_overrides = {row.get("name") for row in analysis.get("approved_overrides") or []}
	for row in frappe.get_all(
		"APS Constraint Resolution",
		filters={"planning_run": run_doc.name, "status": ("in", ["Open", "Requested", "Approved"])},
		fields=["name", "status", "idempotency_key"],
		limit_page_length=0,
	):
		if row.status == "Approved" and row.name in used_overrides:
			continue
		if row.idempotency_key not in current_keys:
			_set_values(row.name, {"status": "Superseded", "resolved_by": frappe.session.user, "resolved_on": now_datetime()})


def request_temporary_override(
	resolution: str,
	*,
	resolution_type: str,
	proposed_value: dict[str, Any] | str | None,
	expires_on: Any,
	reason: str,
	expected_fingerprint: str,
) -> dict[str, Any]:
	_require_v2()
	_assert_temporary_override_supported()
	doc = _lock_resolution(resolution)
	_assert_current_fingerprint(doc, expected_fingerprint)
	if doc.blocker_policy != "Temporary Override":
		frappe.throw(_("This constraint cannot be temporarily overridden.", context="Injection APS"), frappe.ValidationError)
	effective_status = (
		"Expired"
		if doc.status == "Approved" and (not doc.expires_on or get_datetime(doc.expires_on) <= get_datetime(now_datetime()))
		else doc.status
	)
	if effective_status not in ("Open", "Rejected", "Expired"):
		frappe.throw(_("Only an open, rejected, or expired resolution can be requested.", context="Injection APS"), frappe.ValidationError)
	reason = _required_reason(reason)
	if not expires_on or get_datetime(expires_on) <= get_datetime(now_datetime()):
		frappe.throw(_("Temporary override expiry must be in the future.", context="Injection APS"), frappe.ValidationError)
	allowed = {"Temporary Cycle Override", "Temporary Capacity Override", "Temporary Compatibility Override"}
	if resolution_type not in allowed:
		frappe.throw(_("Select a supported temporary override type.", context="Injection APS"), frappe.ValidationError)
	_set_values(
		doc.name,
		{
			"status": "Requested", "resolution_type": resolution_type, "reason": reason,
			"proposed_value_json": _json_value(proposed_value), "expires_on": expires_on,
			"requested_by": frappe.session.user, "requested_on": now_datetime(),
			"approved_by": None, "approved_on": None,
		},
	)
	return _response(doc.name, "Requested", "Approve the temporary override, then recompute the run.")


def approve_temporary_override(
	resolution: str,
	*,
	reason: str,
	expected_fingerprint: str,
) -> dict[str, Any]:
	_require_v2()
	_assert_temporary_override_supported()
	doc = _lock_resolution(resolution)
	_assert_current_fingerprint(doc, expected_fingerprint)
	if doc.blocker_policy != "Temporary Override" or doc.status != "Requested":
		frappe.throw(_("Only a requested temporary override can be approved.", context="Injection APS"), frappe.ValidationError)
	if not doc.expires_on or get_datetime(doc.expires_on) <= get_datetime(now_datetime()):
		frappe.throw(_("The temporary override is already expired.", context="Injection APS"), frappe.ValidationError)
	if doc.resolution_type == "Temporary Compatibility Override" and doc.requested_by == frappe.session.user:
		frappe.throw(_("A high-risk compatibility override cannot be self-approved.", context="Injection APS"), frappe.PermissionError)
	_set_values(
		doc.name,
		{
			"status": "Approved", "reason": _required_reason(reason),
			"approved_by": frappe.session.user, "approved_on": now_datetime(),
		},
	)
	_invalidate_analysis(doc.planning_run)
	return _response(doc.name, "Approved", "Recompute the run to include the approved override.")


def exclude_commitment_from_release(
	resolution: str,
	*,
	reason: str,
	expected_fingerprint: str,
) -> dict[str, Any]:
	_require_v2()
	doc = _lock_resolution(resolution)
	_assert_current_fingerprint(doc, expected_fingerprint)
	if doc.blocker_policy != "Exclude Only":
		frappe.throw(_("This constraint cannot be excluded from release.", context="Injection APS"), frappe.ValidationError)
	if not doc.demand_commitment and not doc.schedule_result:
		frappe.throw(_("The affected demand cannot be identified for exclusion.", context="Injection APS"), frappe.ValidationError)
	reason = _required_reason(reason)
	when = now_datetime()
	if doc.demand_commitment:
		commitment = frappe.get_doc("APS Demand Commitment", doc.demand_commitment)
		commitment.exclude_from_release = 1
		commitment.exclusion_reason = reason
		commitment.excluded_by = frappe.session.user
		commitment.excluded_on = when
		commitment.flags.aps_phase2_transition = True
		commitment.save(ignore_permissions=True)
	if doc.schedule_result:
		frappe.db.set_value(
			"APS Schedule Result", doc.schedule_result,
			{"exclude_from_release": 1, "exclusion_reason": reason},
			update_modified=False,
		)
	_set_values(
		doc.name,
		{
			"status": "Excluded", "resolution_type": "Exclude From Release", "reason": reason,
			"approved_by": frappe.session.user, "approved_on": when,
			"resolved_by": frappe.session.user, "resolved_on": when,
		},
	)
	_invalidate_analysis(doc.planning_run)
	return _response(doc.name, "Excluded", "Recompute the run; the remaining feasible plan can then be applied.")


def _temporary_override_capability() -> tuple[bool, str]:
	from injection_aps.services import v2_flags

	settings = v2_flags.get_v2_settings()
	if settings["enable_aps_v2"] and settings["solver_engine"] == "CP-SAT":
		return False, _(
			"The current CP-SAT solver does not support temporary constraint overrides. "
			"Correct the master data or exclude the affected demand.",
			context="Injection APS",
		)
	return True, ""


def _assert_temporary_override_supported() -> None:
	supported, reason = _temporary_override_capability()
	if not supported:
		frappe.throw(reason, frappe.ValidationError)


def recompute_after_resolution(planning_run: str, *, expected_fingerprint: str | None = None) -> dict[str, Any]:
	_require_v2()
	_expire_overrides(planning_run)
	run = frappe.get_doc("APS Planning Run", planning_run)
	if expected_fingerprint and run.get("capacity_balance_fingerprint") not in (None, "", expected_fingerprint):
		frappe.throw(_("The run fingerprint changed. Refresh before recomputing.", context="Injection APS"), frappe.ValidationError)
	from injection_aps.services import capacity_balance, v2_flags

	settings = v2_flags.get_v2_settings()
	if settings["enable_aps_v2"] and settings["solver_engine"] == "CP-SAT":
		# Override/Exclude changes are Solver inputs. Re-enter the V2 orchestration
		# path so a new immutable input fingerprint and independently validated
		# scenario are produced instead of silently falling back to Legacy analysis.
		from injection_aps.services import solver_orchestration

		return solver_orchestration.analyze_v2_schedule(run.name, run_in_background=True)

	return capacity_balance.analyze_capacity_balance(run.name, persist=True)


def get_excluded_result_names(planning_run: str) -> set[str]:
	if not frappe.db.exists("DocType", "APS Schedule Result"):
		return set()
	return set(
		frappe.get_all(
			"APS Schedule Result",
			filters={"planning_run": planning_run, "exclude_from_release": 1},
			pluck="name",
			limit_page_length=0,
		)
	)


def approved_overrides(
	planning_run: str,
	*,
	at_time: Any | None = None,
	expected_input_fingerprint: str | None = None,
) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "APS Constraint Resolution"):
		return []
	at_time = get_datetime(at_time or now_datetime())
	rows = frappe.get_all(
		"APS Constraint Resolution",
		filters={"planning_run": planning_run, "status": "Approved", "blocker_policy": "Temporary Override"},
		fields=["name", "blocker_key", "resolution_type", "proposed_value_json", "expires_on", "approved_by", "approved_on", "input_fingerprint"],
		limit_page_length=0,
	)
	return [
		dict(row)
		for row in rows
		if row.expires_on
		and get_datetime(row.expires_on) > at_time
		and (
			not expected_input_fingerprint
			or row.input_fingerprint == expected_input_fingerprint
		)
	]


def apply_approved_overrides_to_analysis(
	planning_run: str,
	analysis: dict[str, Any],
	*,
	expected_input_fingerprint: str,
) -> list[dict[str, Any]]:
	"""Downgrade only the exact approved blocker scope into an acknowledged risk."""
	overrides = approved_overrides(
		planning_run,
		expected_input_fingerprint=expected_input_fingerprint,
	)
	used = []
	for override in overrides:
		scope = frappe.db.get_value(
			"APS Constraint Resolution", override["name"],
			["schedule_result", "schedule_segment"], as_dict=True,
		) or {}
		matched = False
		for demand in analysis.get("demands") or []:
			if scope.get("schedule_result") and demand.get("result") != scope.get("schedule_result"):
				continue
			if scope.get("schedule_segment") and demand.get("segment") != scope.get("schedule_segment"):
				continue
			for check in demand.get("checks") or []:
				if check.get("key") != override.get("blocker_key") or check.get("status") not in {"blocked", "failed"}:
					continue
				check["status"] = "warning"
				check["key"] = f"approved_override|{override['name']}|{override.get('blocker_key') or ''}"
				check["message"] = _("Approved temporary override {0}: {1}", context="Injection APS").format(override["name"], check.get("message") or "")
				matched = True
		if matched:
			used.append({
				"name": override["name"], "blocker_key": override.get("blocker_key"),
				"resolution_type": override.get("resolution_type"), "expires_on": str(override.get("expires_on")),
				"approved_by": override.get("approved_by"), "approved_on": str(override.get("approved_on")),
				"proposed_value_json": override.get("proposed_value_json") or "{}",
			})
	return sorted(used, key=lambda row: row["name"])


def assert_analysis_overrides_current(planning_run: str, analysis: dict[str, Any]) -> None:
	current = {row["name"]: row for row in approved_overrides(planning_run)}
	stale = [row.get("name") for row in analysis.get("approved_overrides") or [] if row.get("name") not in current]
	if stale:
		frappe.throw(
			_("Temporary override(s) expired or were withdrawn: {0}. Analyze again.", context="Injection APS").format(", ".join(stale)),
			frappe.ValidationError,
		)


def _expire_overrides(planning_run: str) -> None:
	for row in frappe.get_all(
		"APS Constraint Resolution",
		filters={"planning_run": planning_run, "status": "Approved", "blocker_policy": "Temporary Override"},
		fields=["name", "expires_on"],
		limit_page_length=0,
	):
		if not row.expires_on or get_datetime(row.expires_on) <= get_datetime(now_datetime()):
			_set_values(row.name, {"status": "Expired", "resolved_on": now_datetime()})


def _resolve_commitment(planning_run: str, result: dict[str, Any]) -> str | None:
	if result.get("demand_commitment"):
		return result["demand_commitment"]
	filters = {
		"planning_run": planning_run,
		"item_code": result.get("item_code"),
		"customer": result.get("customer") or ("is", "not set"),
		"status": ("in", ["Draft", "Proposed", "Approved", "Released", "In Progress"]),
	}
	if result.get("requested_date"):
		filters["original_due_date"] = result["requested_date"]
	rows = frappe.get_all("APS Demand Commitment", filters=filters, pluck="name", limit=2)
	return rows[0] if len(rows) == 1 else None


def _result_context(result: str | None) -> dict[str, Any]:
	if not result:
		return {}
	return dict(
		frappe.db.get_value(
			"APS Schedule Result", result,
			[
				"plant_floor", "customer", "item_code", "requested_date", "demand_commitment",
				"planned_qty", "unscheduled_qty", "critical_unplanned_qty",
			],
			as_dict=True,
		) or {}
	)


def _affected_blocker_qty(blocker: dict[str, Any], result: dict[str, Any]) -> float:
	qty = max(flt(blocker.get("planned_qty")), 0)
	if qty <= QTY_TOLERANCE:
		qty = max(flt(result.get("critical_unplanned_qty")), flt(result.get("unscheduled_qty")), 0)
	if qty <= QTY_TOLERANCE:
		qty = max(flt(result.get("planned_qty")), 0)
	return qty


def _upsert_engine_record(values: dict[str, Any]) -> None:
	name = frappe.db.get_value("APS Constraint Resolution", {"idempotency_key": values["idempotency_key"]})
	if name:
		doc = frappe.get_doc("APS Constraint Resolution", name)
		# Preserve a user's Requested/Approved/Excluded decision for the same input.
		preserved = doc.status in {"Requested", "Approved", "Excluded"}
		for fieldname, value in values.items():
			if fieldname != "status" or not preserved:
				doc.set(fieldname, value)
	else:
		doc = frappe.get_doc({"doctype": "APS Constraint Resolution", **values})
	doc.flags.aps_resolution_transition = True
	if name:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)


def _set_values(name: str, values: dict[str, Any]) -> None:
	doc = frappe.get_doc("APS Constraint Resolution", name)
	for fieldname, value in values.items():
		doc.set(fieldname, value)
	doc.flags.aps_resolution_transition = True
	doc.save(ignore_permissions=True)


def _lock_resolution(name: str):
	scope = frappe.db.get_value(
		"APS Constraint Resolution",
		name,
		["planning_run", "company"],
		as_dict=True,
	) or {}
	if not scope:
		frappe.throw(_("Constraint Resolution {0} was not found.", context="Injection APS").format(name), frappe.DoesNotExistError)
	company = scope.get("company") or frappe.db.get_value("APS Planning Run", scope.get("planning_run"), "company")
	if company:
		frappe.db.sql("select name from `tabCompany` where name = %s for update", company)
	run_rows = frappe.db.sql(
		"select name, capacity_balance_fingerprint from `tabAPS Planning Run` where name = %s for update",
		scope.get("planning_run"),
		as_dict=True,
	)
	if not run_rows:
		frappe.throw(_("Planning Run {0} was not found.", context="Injection APS").format(scope.get("planning_run")), frappe.DoesNotExistError)
	rows = frappe.db.sql(
		"select * from `tabAPS Constraint Resolution` where name = %s for update",
		name,
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Constraint Resolution {0} was not found.", context="Injection APS").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc("APS Constraint Resolution", name)
	doc.update(rows[0])
	doc.flags.aps_locked_run_fingerprint = run_rows[0].get("capacity_balance_fingerprint") or ""
	return doc


def _assert_current_fingerprint(doc: Any, expected: str) -> None:
	run_fingerprint = doc.flags.get("aps_locked_run_fingerprint")
	if run_fingerprint is None:
		run_fingerprint = frappe.db.get_value("APS Planning Run", doc.planning_run, "capacity_balance_fingerprint") or ""
	if not expected or expected != (doc.input_fingerprint or "") or expected != run_fingerprint:
		frappe.throw(_("The analysis fingerprint changed. Refresh and analyze again.", context="Injection APS"), frappe.ValidationError)


def _invalidate_analysis(planning_run: str) -> None:
	from injection_aps.services import capacity_balance

	capacity_balance.invalidate_capacity_balance(
		planning_run,
		preserve_constraint_resolutions=True,
	)


def _required_reason(reason: str) -> str:
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required for this controlled decision.", context="Injection APS"), frappe.ValidationError)
	return reason


def _json_value(value: dict[str, Any] | str | None) -> str:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			frappe.throw(_("Proposed override value must be valid JSON.", context="Injection APS"), frappe.ValidationError)
	return json.dumps(value or {}, ensure_ascii=True, sort_keys=True, default=str)


def _resolution_key(run: str, fingerprint: str | None, key: str | None, result: str | None, segment: str | None) -> str:
	return hashlib.sha256(f"{run}|{fingerprint or ''}|{key or ''}|{result or ''}|{segment or ''}".encode()).hexdigest()


def _suggested_action(policy: str | None) -> str:
	return {
		"Temporary Override": _("Correct master data or request a time-limited override.", context="Injection APS"),
		"Exclude Only": _("Correct the source or exclude this commitment from this release.", context="Injection APS"),
		"Acknowledgment": _("Review the delivery impact and acknowledge the risk.", context="Injection APS"),
	}.get(policy, _("Correct the source data; this constraint cannot be overridden.", context="Injection APS"))


def _response(name: str, status: str, next_action: str) -> dict[str, Any]:
	return {"resolution": name, "status": status, "next_action": next_action, "actor": frappe.session.user, "timestamp": now_datetime()}


def _require_v2() -> None:
	if not is_v2_enabled():
		frappe.throw(_("APS V2 is disabled."), frappe.PermissionError)
