from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from injection_aps.services.v2_flags import is_v2_enabled


QTY_TOLERANCE = 0.000001
ACTIVE_COMMITMENT_STATUSES = ("Draft", "Proposed", "Approved", "Released", "In Progress")
EDITABLE_RUN_STATUSES = ("Draft", "Planned", "Risk")
ADMISSION_STRATEGIES = ("standard", "conservative", "all_recommended", "custom")


def calculate_p1_candidate(open_so_qty: float, open_schedule_qty: float, active_p1_qty: float) -> float:
	return max(flt(open_so_qty) - flt(open_schedule_qty) - flt(active_p1_qty), 0)


def calculate_p2_candidate(safety_target: float, projected_unallocated_fg: float) -> float:
	return max(flt(safety_target) - flt(projected_unallocated_fg), 0)


def rebuild_admission_candidates(
	planning_run: str,
	*,
	p0_commitments: Iterable[dict[str, Any]],
	baseline_fingerprint: str,
	projected_unallocated_fg: dict[str, float] | None = None,
) -> dict[str, Any]:
	run = frappe.get_doc("APS Planning Run", planning_run)
	rows = []
	for commitment in p0_commitments:
		candidate = max(flt(commitment.get("requested_qty")), 0)
		if candidate <= QTY_TOLERANCE:
			continue
		rows.append(
			{
				"planning_run": run.name,
				"company": run.company,
				"customer": commitment.get("customer"),
				"item_code": commitment.get("item_code"),
				"demand_identity": commitment.get("demand_identity"),
				"source_doctype": "Customer Delivery Schedule Item",
				"source_name": commitment.get("schedule_item"),
				"admission_class": "P0",
				"candidate_qty": candidate,
				"recommended_qty": candidate,
				"selected_qty": candidate,
				"mandatory": 1,
				"status": "Selected",
				"recommendation_reason": _("Confirmed customer schedule demand is mandatory P0."),
				"source_key": commitment.get("demand_identity"),
			}
		)

	scope_customer = run.get("planning_customer_filter") or None
	scope_item = run.get("planning_item_filter") or None
	open_so = _get_open_sales_order_qty(
		run.company, customer=scope_customer, item_code=scope_item
	)
	open_schedule = _get_open_schedule_qty(
		run.company, customer=scope_customer, item_code=scope_item
	)
	active_p1 = _get_active_p1_commitment_qty(
		run.company,
		exclude_run=run.name,
		customer=scope_customer,
		item_code=scope_item,
	)
	for key in sorted(set(open_so) | set(open_schedule) | set(active_p1)):
		candidate = calculate_p1_candidate(open_so.get(key), open_schedule.get(key), active_p1.get(key))
		if candidate <= QTY_TOLERANCE:
			continue
		customer, item_code = key
		rows.append(
			{
				"planning_run": run.name,
				"company": run.company,
				"customer": customer,
				"item_code": item_code,
				"source_doctype": "Sales Order",
				"source_name": None,
				"admission_class": "P1",
				"candidate_qty": candidate,
				"recommended_qty": candidate,
				"selected_qty": 0,
				"mandatory": 0,
				"status": "Candidate",
				"recommendation_reason": _(
					"Framework SO remainder after open customer schedules and active P1 commitments."
				),
				"source_key": f"{customer}|{item_code}",
			}
		)

	projected = projected_unallocated_fg or {}
	for item_code, safety_target in sorted(
		_get_safety_targets(run.company, item_code=scope_item).items()
	):
		candidate = calculate_p2_candidate(safety_target, projected.get(item_code))
		if candidate <= QTY_TOLERANCE:
			continue
		rows.append(
			{
				"planning_run": run.name,
				"company": run.company,
				"customer": None,
				"item_code": item_code,
				"source_doctype": "Item",
				"source_name": item_code,
				"admission_class": "P2",
				"candidate_qty": candidate,
				"recommended_qty": candidate,
				"selected_qty": 0,
				"mandatory": 0,
				"status": "Candidate",
				"recommendation_reason": _(
					"Safety target gap after projected unallocated finished-goods stock."
				),
				"source_key": item_code,
			}
		)

	input_fingerprint = _fingerprint(
		{
			"baseline": baseline_fingerprint,
			"candidates": [_candidate_fingerprint_row(row) for row in rows],
		}
	)
	active_keys = set()
	persisted = []
	for values in rows:
		key = _admission_key(run.name, values["admission_class"], values.get("source_key"))
		active_keys.add(key)
		values.update({"input_fingerprint": input_fingerprint, "idempotency_key": key})
		doc = _upsert_admission(values)
		persisted.append(doc)

	for stale in frappe.get_all(
		"APS Demand Admission",
		filters={"planning_run": run.name, "status": ("!=", "Superseded")},
		fields=["name", "idempotency_key"],
		limit_page_length=0,
	):
		if stale.get("idempotency_key") not in active_keys:
			_set_admission_values(stale.name, {"status": "Superseded", "selected_qty": 0})

	decision_fingerprint = _decision_fingerprint(input_fingerprint, persisted)
	for doc in persisted:
		_set_admission_values(doc.name, {"decision_fingerprint": decision_fingerprint})
	optional_rows = [doc for doc in persisted if doc.admission_class in {"P1", "P2"}]
	auto_confirmed = not optional_rows
	frappe.db.set_value(
		"APS Planning Run",
		run.name,
		{
			"admission_fingerprint": decision_fingerprint,
			"admission_confirmed_fingerprint": decision_fingerprint if auto_confirmed else None,
			"admission_confirmed_by": frappe.session.user if auto_confirmed else None,
			"admission_confirmed_on": now_datetime() if auto_confirmed else None,
			"admission_decision_reason": (
				_("Automatically confirmed because the baseline contains only mandatory P0 demand.")
				if auto_confirmed else None
			),
			"admission_strategy": "p0_only" if auto_confirmed else None,
			"total_selected_p1_qty": 0,
			"total_selected_p2_qty": 0,
			"status": "Draft",
			"approval_state": "Pending",
			"consistency_status": "Unchecked",
			"consistency_checked_on": None,
			"capacity_balance_status": "Not Analyzed",
			"capacity_balance_analyzed_on": None,
			"capacity_balance_confirmed_by": None,
			"capacity_balance_confirmed_on": None,
			"capacity_balance_applied_on": None,
			"capacity_balance_fingerprint": None,
			"capacity_balance_analysis_json": None,
		},
		update_modified=False,
	)
	return get_demand_admission_candidates(run.name)


def get_demand_admission_candidates(planning_run: str) -> dict[str, Any]:
	run = frappe.get_doc("APS Planning Run", planning_run)
	rows = frappe.get_all(
		"APS Demand Admission",
		filters={"planning_run": run.name, "status": ("!=", "Superseded")},
		fields=[
			"name", "planning_run", "company", "customer", "item_code", "demand_identity",
			"source_doctype", "source_name", "admission_class", "candidate_qty", "recommended_qty",
			"selected_qty", "mandatory", "status", "recommendation_reason", "continuity_benefit",
			"changeover_reduction_minutes", "inventory_days", "utilization_benefit_percent",
			"input_fingerprint", "decision_fingerprint", "decision_reason", "decided_by", "decided_on",
		],
		order_by="admission_class asc, customer asc, item_code asc, name asc",
		limit_page_length=0,
	)
	result = {
		"planning_run": run.name,
		"company": run.company,
		"demand_baseline_fingerprint": run.get("demand_baseline_fingerprint"),
		"admission_fingerprint": run.get("admission_fingerprint"),
		"summary": _summarize(rows),
		"rows": [dict(row) for row in rows],
	}
	result["admission_state"] = get_admission_state(run.name, run=run, rows=result["rows"])
	return result


def preview_admission_impact(
	planning_run: str,
	decisions: list[dict[str, Any]],
	*,
	expected_fingerprint: str,
	strategy: str | None = None,
) -> dict[str, Any]:
	current = get_demand_admission_candidates(planning_run)
	_validate_expected_fingerprint(current, expected_fingerprint)
	by_name = {row["name"]: row for row in current["rows"]}
	preview_rows = _validate_decisions(by_name, decisions)
	before_summary = _summarize(current["rows"])
	after_summary = _summarize(preview_rows)
	changed_rows = [
		row for row in preview_rows
		if abs(flt(row["selected_qty"]) - flt(by_name[row["name"]]["selected_qty"])) > QTY_TOLERANCE
	]
	planned_fingerprint = (current.get("admission_state") or {}).get("planned_fingerprint") or ""
	return {
		"planning_run": planning_run,
		"current_fingerprint": current["admission_fingerprint"],
		"strategy": str(strategy or "").strip(),
		"before_summary": before_summary,
		"summary": after_summary,
		"changed_row_count": len(changed_rows),
		"excluded_row_count": sum(
			1 for row in preview_rows
			if row["admission_class"] in {"P1", "P2"} and flt(row["selected_qty"]) <= QTY_TOLERANCE
		),
		"partial_row_count": sum(
			1 for row in preview_rows
			if row["admission_class"] in {"P1", "P2"}
			and QTY_TOLERANCE < flt(row["selected_qty"]) < flt(row["candidate_qty"]) - QTY_TOLERANCE
		),
		"total_selected_delta": round(
			flt(after_summary.get("selected_total_qty")) - flt(before_summary.get("selected_total_qty")), 6
		),
		"invalidates_previous_analysis": bool(
			planned_fingerprint
			and (changed_rows or planned_fingerprint != current["admission_fingerprint"])
		),
		"rows": preview_rows,
	}


def save_demand_admission_decisions(
	planning_run: str,
	decisions: list[dict[str, Any]],
	*,
	expected_fingerprint: str,
	reason: str,
	strategy: str | None = None,
) -> dict[str, Any]:
	if not is_v2_enabled():
		frappe.throw(_("APS V2 is disabled; admission decisions cannot be changed."), frappe.ValidationError)
	reason = str(reason or "").strip()
	strategy = str(strategy or "custom").strip() or "custom"
	if not reason:
		frappe.throw(_("A decision reason is required when changing P1/P2 admission."), frappe.ValidationError)
	if strategy not in ADMISSION_STRATEGIES:
		frappe.throw(_("The selected demand admission strategy is not valid."), frappe.ValidationError)
	_lock_run_scope(planning_run)
	run = frappe.get_doc("APS Planning Run", planning_run)
	if run.status not in EDITABLE_RUN_STATUSES or run.approval_state == "Approved":
		frappe.throw(
			_("Demand admission can only be changed on an unapproved Draft, Planned, or Risk run."),
			frappe.ValidationError,
		)
	current = get_demand_admission_candidates(planning_run)
	_validate_expected_fingerprint(current, expected_fingerprint)
	by_name = {row["name"]: row for row in current["rows"]}
	preview_rows = _validate_decisions(by_name, decisions)
	now = now_datetime()
	changed = False
	for row in preview_rows:
		before = by_name[row["name"]]
		if row["admission_class"] == "P0":
			continue
		changed = changed or abs(flt(row["selected_qty"]) - flt(before["selected_qty"])) > QTY_TOLERANCE
		_set_admission_values(
			row["name"],
			{
				"selected_qty": row["selected_qty"],
				"status": "Selected" if flt(row["selected_qty"]) > QTY_TOLERANCE else "Excluded",
				"decision_reason": reason,
				"decided_by": frappe.session.user,
				"decided_on": now,
			},
		)

	updated = get_demand_admission_candidates(planning_run)
	decision_fingerprint = _decision_fingerprint(
		updated["rows"][0]["input_fingerprint"] if updated["rows"] else current["demand_baseline_fingerprint"],
		updated["rows"],
	)
	for row in updated["rows"]:
		_set_admission_values(row["name"], {"decision_fingerprint": decision_fingerprint})

	from injection_aps.services import demand_ledger

	demand_ledger.sync_optional_admission_commitments(planning_run, updated["rows"], decision_fingerprint)
	summary = _summarize(updated["rows"])
	frappe.db.set_value(
		"APS Planning Run",
		planning_run,
		{
			"admission_fingerprint": decision_fingerprint,
			"admission_confirmed_fingerprint": decision_fingerprint,
			"admission_confirmed_by": frappe.session.user,
			"admission_confirmed_on": now,
			"admission_decision_reason": reason,
			"admission_strategy": strategy,
			"total_selected_p1_qty": summary["selected_p1_qty"],
			"total_selected_p2_qty": summary["selected_p2_qty"],
			"status": "Draft",
			"approval_state": "Pending",
			"consistency_status": "Unchecked",
			"consistency_checked_on": None,
			"capacity_balance_status": "Not Analyzed",
			"capacity_balance_analyzed_on": None,
			"capacity_balance_confirmed_by": None,
			"capacity_balance_confirmed_on": None,
			"capacity_balance_applied_on": None,
			"capacity_balance_fingerprint": None,
			"capacity_balance_analysis_json": None,
		},
		update_modified=True,
	)
	result = get_demand_admission_candidates(planning_run)
	planned_fingerprint = run.get("planned_admission_fingerprint") or ""
	result["analysis_invalidated"] = int(
		bool(planned_fingerprint and (changed or planned_fingerprint != decision_fingerprint))
	)
	result["recalculation_required"] = 1
	result["next_route"] = f"aps-run-console?run_name={planning_run}&from_admission=1"
	return result


def get_admission_state(
	planning_run: str,
	*,
	run=None,
	rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	run = run or frappe.get_doc("APS Planning Run", planning_run)
	if rows is None:
		rows = [dict(row) for row in frappe.get_all(
			"APS Demand Admission",
			filters={"planning_run": planning_run, "status": ("!=", "Superseded")},
			fields=["name", "admission_class", "status", "selected_qty"],
			limit_page_length=0,
		)]
	optional_rows = [row for row in rows if row.get("admission_class") in {"P1", "P2"}]
	current_fingerprint = run.get("admission_fingerprint") or ""
	confirmed_fingerprint = run.get("admission_confirmed_fingerprint") or ""
	planned_fingerprint = run.get("planned_admission_fingerprint") or ""
	baseline_ready = bool(run.get("demand_baseline_fingerprint") and current_fingerprint)
	confirmed = bool(baseline_ready and confirmed_fingerprint == current_fingerprint)
	recalculation_required = bool(confirmed and planned_fingerprint != current_fingerprint)
	legacy_unchecked = not baseline_ready
	editable = bool(run.status in EDITABLE_RUN_STATUSES and run.approval_state != "Approved")
	return {
		"baseline_ready": int(baseline_ready),
		"confirmed": int(confirmed),
		"legacy_unchecked": int(legacy_unchecked),
		"editable": int(editable),
		"blocking_reason": (
			""
			if editable
			else _("Demand admission is locked because this Run has been approved or left the editable planning states.")
		),
		"optional_row_count": len(optional_rows),
		"p1_row_count": sum(1 for row in optional_rows if row.get("admission_class") == "P1"),
		"p2_row_count": sum(1 for row in optional_rows if row.get("admission_class") == "P2"),
		"recalculation_required": int(recalculation_required),
		"current_fingerprint": current_fingerprint,
		"confirmed_fingerprint": confirmed_fingerprint,
		"planned_fingerprint": planned_fingerprint,
		"confirmed_by": run.get("admission_confirmed_by") or "",
		"confirmed_on": run.get("admission_confirmed_on"),
		"decision_reason": run.get("admission_decision_reason") or "",
		"strategy": run.get("admission_strategy") or "",
	}


def require_admission_ready(planning_run: str, *, require_planned: bool = False) -> dict[str, Any]:
	state = get_admission_state(planning_run)
	# Historical Runs created before admission fingerprints existed remain readable
	# and are not rewritten. New prepared Runs always have a baseline fingerprint.
	if state["legacy_unchecked"]:
		return state
	if not state["confirmed"]:
		frappe.throw(
			_("Confirm this Run's demand admission before recalculation or capacity analysis."),
			frappe.ValidationError,
		)
	if require_planned and state["recalculation_required"]:
		frappe.throw(
			_("Demand admission changed after the last calculation. Recalculate this Run before continuing."),
			frappe.ValidationError,
		)
	return state


def _get_open_sales_order_qty(
	company: str,
	*,
	customer: str | None = None,
	item_code: str | None = None,
) -> dict[tuple[str, str], float]:
	conditions = []
	params = {"company": company}
	if customer:
		conditions.append("and so.customer = %(customer)s")
		params["customer"] = customer
	if item_code:
		conditions.append("and soi.item_code = %(item_code)s")
		params["item_code"] = item_code
	rows = frappe.db.sql(
		"""
		select so.customer, soi.item_code,
			sum(greatest(ifnull(soi.stock_qty, soi.qty) - ifnull(soi.delivered_qty, 0), 0)) as open_qty
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where so.company = %(company)s and so.docstatus = 1
			and ifnull(so.status, '') not in ('Closed', 'Cancelled')
			{scope_conditions}
		group by so.customer, soi.item_code
		""".format(scope_conditions="\n\t\t\t".join(conditions)),
		params,
		as_dict=True,
	)
	return {(row.customer, row.item_code): max(flt(row.open_qty), 0) for row in rows}


def _get_open_schedule_qty(
	company: str,
	*,
	customer: str | None = None,
	item_code: str | None = None,
) -> dict[tuple[str, str], float]:
	conditions = []
	params = {"company": company}
	if customer:
		conditions.append("and s.customer = %(customer)s")
		params["customer"] = customer
	if item_code:
		conditions.append("and i.item_code = %(item_code)s")
		params["item_code"] = item_code
	rows = frappe.db.sql(
		"""
		select s.customer, i.item_code,
			sum(greatest(ifnull(i.effective_qty, i.qty) - ifnull(i.delivered_qty, 0), 0)) as open_qty
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where s.company = %(company)s and s.status = 'Active'
			and ifnull(i.status, '') != 'Cancelled'
			{scope_conditions}
		group by s.customer, i.item_code
		""".format(scope_conditions="\n\t\t\t".join(conditions)),
		params,
		as_dict=True,
	)
	return {(row.customer, row.item_code): max(flt(row.open_qty), 0) for row in rows}


def _get_active_p1_commitment_qty(
	company: str,
	*,
	exclude_run: str | None = None,
	customer: str | None = None,
	item_code: str | None = None,
) -> dict[tuple[str, str], float]:
	filters: dict[str, Any] = {
		"company": company,
		"admission_class": "P1",
		"status": ("in", ACTIVE_COMMITMENT_STATUSES),
		# Trial selections and Referenced mirrors are not active P1 ownership.
		# Counting them would make one what-if Run suppress another and would count
		# the same Formal source twice.
		"owner_state": "Owned",
		"formal_owner": 1,
	}
	if exclude_run:
		filters["planning_run"] = ("!=", exclude_run)
	if customer:
		filters["customer"] = customer
	if item_code:
		filters["item_code"] = item_code
	rows = frappe.get_all(
		"APS Demand Commitment",
		filters=filters,
		fields=["customer", "item_code", "remaining_qty"],
		limit_page_length=0,
	)
	result: dict[tuple[str, str], float] = defaultdict(float)
	for row in rows:
		result[(row.get("customer"), row.get("item_code"))] += max(flt(row.get("remaining_qty")), 0)
	return dict(result)


def _get_safety_targets(company: str, *, item_code: str | None = None) -> dict[str, float]:
	settings = frappe.get_cached_doc("APS Settings")
	fieldname = str(settings.get("item_safety_stock_field") or "safety_stock").strip()
	meta = frappe.get_meta("Item")
	if not meta.has_field(fieldname):
		return {}
	filters = {"disabled": 0}
	if item_code:
		filters["name"] = item_code
	rows = frappe.get_all(
		"Item",
		filters=filters,
		fields=["name", fieldname],
		limit_page_length=0,
	)
	return {row.name: max(flt(row.get(fieldname)), 0) for row in rows if flt(row.get(fieldname)) > QTY_TOLERANCE}


def _upsert_admission(values: dict[str, Any]):
	name = frappe.db.get_value("APS Demand Admission", {"idempotency_key": values["idempotency_key"]})
	if name:
		doc = frappe.get_doc("APS Demand Admission", name)
		selected = flt(doc.selected_qty) if values["admission_class"] in {"P1", "P2"} else flt(values["selected_qty"])
		for fieldname, value in values.items():
			if fieldname == "source_key":
				continue
			doc.set(fieldname, value)
		doc.selected_qty = min(selected, flt(doc.candidate_qty))
		doc.status = "Selected" if flt(doc.selected_qty) > QTY_TOLERANCE else "Candidate"
		doc.flags.aps_phase2_transition = True
		doc.save(ignore_permissions=True)
		return doc
	doc = frappe.get_doc({"doctype": "APS Demand Admission", **{key: value for key, value in values.items() if key != "source_key"}})
	doc.flags.aps_phase2_transition = True
	doc.insert(ignore_permissions=True)
	return doc


def _set_admission_values(name: str, values: dict[str, Any]) -> None:
	doc = frappe.get_doc("APS Demand Admission", name)
	for fieldname, value in values.items():
		doc.set(fieldname, value)
	doc.flags.aps_phase2_transition = True
	doc.save(ignore_permissions=True)


def _validate_expected_fingerprint(current: dict[str, Any], expected: str) -> None:
	if not expected or expected != current.get("admission_fingerprint"):
		frappe.throw(_("Admission candidates changed. Refresh before saving decisions."), frappe.ValidationError)


def _validate_decisions(by_name: dict[str, dict[str, Any]], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
	proposed = {name: dict(row) for name, row in by_name.items()}
	seen = set()
	for decision in decisions or []:
		name = str(decision.get("name") or "").strip()
		if not name or name in seen or name not in proposed:
			frappe.throw(_("Admission decision contains an unknown or duplicate row."), frappe.ValidationError)
		seen.add(name)
		row = proposed[name]
		selected = flt(decision.get("selected_qty"))
		if selected < -QTY_TOLERANCE:
			frappe.throw(_("Selected admission quantity cannot be negative."), frappe.ValidationError)
		selected = max(selected, 0)
		if row["admission_class"] == "P0" and abs(selected - flt(row["candidate_qty"])) > QTY_TOLERANCE:
			frappe.throw(_("P0 demand is mandatory and cannot be deselected."), frappe.ValidationError)
		if selected > flt(row["candidate_qty"]) + QTY_TOLERANCE:
			frappe.throw(_("Selected admission quantity cannot exceed its candidate quantity."), frappe.ValidationError)
		row["selected_qty"] = selected
	return list(proposed.values())


def _summarize(rows: Iterable[dict[str, Any]]) -> dict[str, float]:
	result = {
		"p0_qty": 0.0, "p1_candidate_qty": 0.0, "p2_candidate_qty": 0.0,
		"selected_p1_qty": 0.0, "selected_p2_qty": 0.0,
		"p0_count": 0, "p1_count": 0, "p2_count": 0,
	}
	for row in rows:
		admission_class = row.get("admission_class")
		if admission_class == "P0":
			result["p0_count"] += 1
			result["p0_qty"] += flt(row.get("selected_qty"))
		elif admission_class == "P1":
			result["p1_count"] += 1
			result["p1_candidate_qty"] += flt(row.get("candidate_qty"))
			result["selected_p1_qty"] += flt(row.get("selected_qty"))
		elif admission_class == "P2":
			result["p2_count"] += 1
			result["p2_candidate_qty"] += flt(row.get("candidate_qty"))
			result["selected_p2_qty"] += flt(row.get("selected_qty"))
	result["optional_count"] = result["p1_count"] + result["p2_count"]
	result["selected_total_qty"] = result["p0_qty"] + result["selected_p1_qty"] + result["selected_p2_qty"]
	return {
		key: int(value) if key.endswith("_count") else round(value, 6)
		for key, value in result.items()
	}


def _candidate_fingerprint_row(row: dict[str, Any]) -> dict[str, Any]:
	return {
		"class": row.get("admission_class"), "source": row.get("source_key"),
		"customer": row.get("customer"), "item": row.get("item_code"),
		"candidate": round(flt(row.get("candidate_qty")), 6),
		"recommended": round(flt(row.get("recommended_qty")), 6),
		"mandatory": int(bool(row.get("mandatory"))),
	}


def _decision_fingerprint(input_fingerprint: str, rows: Iterable[Any]) -> str:
	return _fingerprint(
		{
			"input": input_fingerprint,
			"selected": [
				{
					"name": _get(row, "name"),
					"class": _get(row, "admission_class"),
					"qty": round(flt(_get(row, "selected_qty")), 6),
				}
				for row in sorted(rows, key=lambda value: _get(value, "name") or "")
			],
		}
	)


def _admission_key(run: str, admission_class: str, source_key: str | None) -> str:
	return hashlib.sha256(f"{run}|{admission_class}|{source_key or ''}".encode()).hexdigest()


def _fingerprint(value: Any) -> str:
	return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _lock_run_scope(planning_run: str) -> None:
	company = frappe.db.get_value("APS Planning Run", planning_run, "company")
	if not company:
		frappe.throw(_("Planning Run {0} was not found.").format(planning_run), frappe.DoesNotExistError)
	frappe.db.sql("select name from `tabCompany` where name = %s for update", company)
	frappe.db.sql("select name from `tabAPS Planning Run` where name = %s for update", planning_run)


def _get(row: Any, fieldname: str):
	return row.get(fieldname) if isinstance(row, dict) else getattr(row, fieldname, None)
