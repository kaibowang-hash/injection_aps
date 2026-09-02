from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

from injection_aps.services import planning
from injection_aps.services.v2_flags import is_v2_enabled


QTY_TOLERANCE = 0.000001
REVISION_MODES = ("Full Replacement", "Partial Revision", "Incremental Demand")
MODE_TO_LEGACY_STRATEGY = {
	"Full Replacement": "Replace Scope",
	"Partial Revision": "Partial Update",
	"Incremental Demand": "Append",
}


def normalize_revision_mode(value: str | None) -> str:
	mode = str(value or "").strip()
	aliases = {
		"Replace Scope": "Full Replacement",
		"Partial Update": "Partial Revision",
		"Partial Item Update": "Partial Revision",
		"Append": "Incremental Demand",
		"Full": "Full Replacement",
		"Partial": "Partial Revision",
		"Incremental": "Incremental Demand",
	}
	mode = aliases.get(mode, mode)
	if mode not in REVISION_MODES:
		frappe.throw(
			_("Revision mode must be Full Replacement, Partial Revision, or Incremental Demand."),
			frappe.ValidationError,
		)
	return mode


def recommend_mode(
	previous_rows: list[dict[str, Any]],
	incoming_rows: list[dict[str, Any]],
	*,
	source_contract: str | None = None,
) -> dict[str, Any]:
	"""Recommend a mode without pretending the file can express business intent."""
	contract_mode = _source_contract_mode(source_contract)
	if contract_mode:
		return {
			"recommended_mode": contract_mode,
			"reason_code": "SOURCE_CONTRACT",
			"reason": _("The configured source contract explicitly declares this revision mode."),
			"confidence": "High",
			"requires_user_confirmation": 1,
		}
	if not previous_rows:
		return {
			"recommended_mode": "Full Replacement",
			"reason_code": "INITIAL_SCOPE",
			"reason": _("No active demand exists in this customer/company/scope, so the first version is a full baseline."),
			"confidence": "High",
			"requires_user_confirmation": 1,
		}

	previous_by_external = {
		(row.get("external_line_reference") or ""): row
		for row in previous_rows
		if row.get("external_line_reference")
	}
	previous_by_identity = {row.get("demand_identity"): row for row in previous_rows if row.get("demand_identity")}
	previous_by_business = defaultdict(list)
	for row in previous_rows:
		previous_by_business[_business_key(row)].append(row)
	matched_identities = set()
	matched_rows = 0
	for row in incoming_rows:
		matched = None
		if row.get("demand_identity"):
			matched = previous_by_identity.get(row.get("demand_identity"))
		if not matched and row.get("external_line_reference"):
			matched = previous_by_external.get(row.get("external_line_reference"))
		if not matched:
			candidates = previous_by_business.get(_business_key(row)) or []
			if len(candidates) == 1:
				matched = candidates[0]
		if matched:
			matched_rows += 1
			matched_identities.add(matched.get("demand_identity") or matched.get("name"))

	active_identity_count = len({row.get("demand_identity") or row.get("name") for row in previous_rows})
	if matched_rows == len(incoming_rows) and len(matched_identities) == active_identity_count:
		mode = "Full Replacement"
		code = "COMPLETE_IDENTITY_COVERAGE"
		reason = _("Every active demand identity is represented by the incoming rows; a full replacement is the most likely intent.")
		confidence = "Medium"
	elif matched_rows:
		mode = "Partial Revision"
		code = "PARTIAL_IDENTITY_OVERLAP"
		reason = _("The file overlaps only part of the active demand identities; omitted identities should normally remain active.")
		confidence = "Medium"
	else:
		mode = "Incremental Demand"
		code = "NO_STABLE_IDENTITY_OVERLAP"
		reason = _("No stable identity match was found. APS recommends independent incremental demand, but the source cannot prove this intent.")
		confidence = "Low"
	return {
		"recommended_mode": mode,
		"reason_code": code,
		"reason": reason,
		"confidence": confidence,
		"requires_user_confirmation": 1,
	}


def recommend_schedule_revision_mode(
	*,
	customer: str,
	company: str,
	schedule_scope: str | None,
	file_url: str | None = None,
	rows_json: str | list[dict] | None = None,
	mapping_json: str | dict[str, Any] | None = None,
	source_contract: str | None = None,
) -> dict[str, Any]:
	_require_v2_enabled()
	customer, company, schedule_scope = _normalize_scope(customer, company, schedule_scope)
	rows, parse_context = planning._normalize_schedule_rows(
		file_url=file_url,
		rows_json=rows_json,
		mapping_json=mapping_json,
	)
	incoming_rows = planning._prepare_schedule_rows_for_import(rows)
	previous_rows = _get_active_revision_rows(company, customer, schedule_scope)
	recommendation = recommend_mode(previous_rows, incoming_rows, source_contract=source_contract)
	return {
		"company": company,
		"customer": customer,
		"schedule_scope": schedule_scope,
		"active_row_count": len(previous_rows),
		"incoming_row_count": len(incoming_rows),
		"parse_context": parse_context,
		**recommendation,
	}


def preview_revision(
	*,
	customer: str,
	company: str,
	version_no: str,
	schedule_scope: str | None,
	revision_mode: str,
	duplicate_policy: str | None = None,
	file_url: str | None = None,
	rows_json: str | list[dict] | None = None,
	mapping_json: str | dict[str, Any] | None = None,
	source_type: str = "Customer Delivery Schedule",
	source_contract: str | None = None,
) -> dict[str, Any]:
	_require_v2_enabled()
	customer, company, schedule_scope = _normalize_scope(customer, company, schedule_scope)
	version_no = str(version_no or "").strip()
	if not version_no:
		frappe.throw(_("Version No is required."), frappe.ValidationError)
	revision_mode = normalize_revision_mode(revision_mode)
	duplicate_policy = planning._normalize_schedule_duplicate_policy(duplicate_policy)
	rows, parse_context = planning._normalize_schedule_rows(
		file_url=file_url,
		rows_json=rows_json,
		mapping_json=mapping_json,
	)
	prepared_rows = planning._prepare_schedule_rows_for_import(rows)
	row_issues = planning._validate_schedule_import_rows(prepared_rows, customer=customer, company=company)
	resolved_rows, duplicate_groups = planning._resolve_schedule_row_duplicates(
		prepared_rows,
		duplicate_policy=duplicate_policy,
	)
	previous_rows = _get_active_revision_rows(company, customer, schedule_scope)
	active_state_token = _build_active_state_token(company, customer, schedule_scope, previous_rows)
	recommendation = recommend_mode(previous_rows, resolved_rows, source_contract=source_contract)
	duplicate_blocked = bool(duplicate_groups and duplicate_policy == "Block")
	identity_resolutions = []
	identity_issues = []
	if not row_issues and not duplicate_blocked:
		identity_resolutions, identity_issues = _resolve_incoming_identities(
			company=company,
			customer=customer,
			schedule_scope=schedule_scope,
			previous_rows=previous_rows,
			incoming_rows=resolved_rows,
			revision_mode=revision_mode,
		)
	plan = _build_revision_plan(previous_rows, identity_resolutions, revision_mode) if not row_issues and not duplicate_blocked else {"rows": [], "effective_rows": []}
	checks = _build_revision_checks(
		revision_mode=revision_mode,
		recommendation=recommendation,
		row_issues=row_issues,
		duplicate_groups=duplicate_groups,
		duplicate_policy=duplicate_policy,
		identity_issues=identity_issues,
		rows=plan["rows"],
	)
	can_apply = not any(cint(row.get("blocking")) for row in checks)
	source_rows = planning._build_schedule_source_snapshot_rows(resolved_rows)
	revision_fingerprint = _build_revision_fingerprint(
		company=company,
		customer=customer,
		schedule_scope=schedule_scope,
		version_no=version_no,
		revision_mode=revision_mode,
		source_type=source_type,
		source_contract=source_contract,
		active_state_token=active_state_token,
		rows=source_rows,
	)
	existing = _get_revision_replay(revision_fingerprint)
	if existing:
		can_apply = False
		checks.append(_check("notice", _("Idempotent replay"), _("This confirmed revision has already been applied."), [existing.get("schedule")]))
	return {
		"company": company,
		"customer": customer,
		"schedule_scope": schedule_scope,
		"version_no": version_no,
		"revision_mode": revision_mode,
		"legacy_import_strategy": MODE_TO_LEGACY_STRATEGY[revision_mode],
		"duplicate_policy": duplicate_policy,
		"source_contract": source_contract or "",
		"recommended_revision_mode": recommendation["recommended_mode"],
		"recommendation_reason": recommendation["reason"],
		"recommendation_reason_code": recommendation["reason_code"],
		"recommendation_confidence": recommendation["confidence"],
		"requires_mode_confirmation": 1,
		"mode_differs_from_recommendation": cint(revision_mode != recommendation["recommended_mode"]),
		"row_count": len(plan["rows"]),
		"source_row_count": len(prepared_rows),
		"effective_row_count": len(plan["effective_rows"]),
		"previous_total_qty": sum(flt(row.get("effective_qty") if row.get("effective_qty") is not None else row.get("qty")) for row in previous_rows),
		"incoming_total_qty": sum(flt(row.get("qty")) for row in resolved_rows),
		"post_revision_total_qty": sum(flt(row.get("effective_qty")) for row in plan["effective_rows"]),
		"total_excess_qty": sum(flt(row.get("excess_qty")) for row in plan["rows"]),
		"summary": dict(Counter(row.get("revision_action") or "Unchanged" for row in plan["rows"])),
		"rows": plan["rows"],
		"source_rows": source_rows,
		"effective_schedule_rows": plan["effective_rows"],
		"identity_issues": identity_issues,
		"duplicate_groups": duplicate_groups,
		"checks": checks,
		"can_apply": can_apply,
		"is_idempotent_replay": cint(bool(existing)),
		"existing_revision": existing,
		"active_state_token": active_state_token,
		"revision_fingerprint": revision_fingerprint,
		"parse_context": parse_context,
	}


def apply_revision(
	*,
	customer: str,
	company: str,
	version_no: str,
	schedule_scope: str | None,
	confirmed_revision_mode: str,
	duplicate_policy: str | None = None,
	file_url: str | None = None,
	rows_json: str | list[dict] | None = None,
	mapping_json: str | dict[str, Any] | None = None,
	source_type: str = "Customer Delivery Schedule",
	source_contract: str | None = None,
	mode_confirmation_reason: str | None = None,
	expected_active_state_token: str | None = None,
	expected_revision_fingerprint: str | None = None,
	reference_access_validator=None,
) -> dict[str, Any]:
	_require_v2_enabled()
	customer, company, schedule_scope = _normalize_scope(customer, company, schedule_scope)
	confirmed_revision_mode = normalize_revision_mode(confirmed_revision_mode)
	mode_confirmation_reason = str(mode_confirmation_reason or "").strip()
	savepoint = f"aps_revision_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		_lock_revision_scope(company, customer)
		preview = preview_revision(
			customer=customer,
			company=company,
			version_no=version_no,
			schedule_scope=schedule_scope,
			revision_mode=confirmed_revision_mode,
			duplicate_policy=duplicate_policy,
			file_url=file_url,
			rows_json=rows_json,
			mapping_json=mapping_json,
			source_type=source_type,
			source_contract=source_contract,
		)
		if reference_access_validator:
			reference_access_validator(preview)
		if expected_active_state_token and expected_active_state_token != preview.get("active_state_token"):
			frappe.throw(_("The active schedule changed after preview. Refresh and confirm the latest revision."), frappe.ValidationError)
		if expected_revision_fingerprint and expected_revision_fingerprint != preview.get("revision_fingerprint"):
			frappe.throw(_("The revision input changed after preview. Refresh and confirm the latest rows."), frappe.ValidationError)
		if preview.get("is_idempotent_replay"):
			frappe.db.release_savepoint(savepoint)
			return {**(preview.get("existing_revision") or {}), "idempotent_replay": 1, "summary": preview.get("summary") or {}}
		if not preview.get("can_apply"):
			messages = [row.get("summary") for row in preview.get("checks") or [] if cint(row.get("blocking"))]
			frappe.throw(_("Revision checks failed:<br>{0}").format("<br>".join(messages[:12])), frappe.ValidationError)
		if confirmed_revision_mode != preview.get("recommended_revision_mode") and not mode_confirmation_reason:
			frappe.throw(_("Explain why the confirmed revision mode differs from the APS recommendation."), frappe.ValidationError)
		result = _persist_revision(
			preview=preview,
			file_url=file_url,
			source_type=source_type,
			mode_confirmation_reason=mode_confirmation_reason,
		)
		frappe.db.release_savepoint(savepoint)
		return result
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise


def resolve_identity_ambiguity(
	*,
	company: str,
	customer: str,
	schedule_scope: str,
	demand_identity: str,
	reason: str,
	schedule_item: str | None = None,
) -> dict[str, Any]:
	_require_v2_enabled()
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A resolution reason is required."), frappe.ValidationError)
	customer, company, schedule_scope = _normalize_scope(customer, company, schedule_scope)
	identity = _get_identity(demand_identity)
	_validate_identity_scope(identity, company, customer, schedule_scope)
	if not schedule_item:
		return {
			"demand_identity": identity.name,
			"identity_resolution_reason": reason,
			"identity_match_method": "Manual Resolution",
		}
	row = _get_schedule_item_with_scope(schedule_item)
	if not row:
		frappe.throw(_("Schedule item {0} does not exist.").format(schedule_item), frappe.ValidationError)
	if (row.company, row.customer, row.schedule_scope, row.item_code) != (
		identity.company,
		identity.customer,
		identity.schedule_scope,
		identity.item_code,
	):
		frappe.throw(_("The selected Demand Identity does not match the schedule-item scope."), frappe.ValidationError)
	_lock_revision_scope(company, customer)
	current_item = identity.current_schedule_item
	if current_item and current_item != schedule_item:
		frappe.throw(_("The selected Demand Identity already has another current schedule row."), frappe.ValidationError)
	now = now_datetime()
	frappe.db.set_value(
		"Customer Delivery Schedule Item",
		schedule_item,
		{
			"demand_identity": identity.name,
			"identity_match_method": "Manual Resolution",
			"identity_resolution_reason": reason,
			"identity_resolved_by": frappe.session.user,
			"identity_resolved_on": now,
		},
		update_modified=False,
	)
	if row.schedule_status == "Active":
		frappe.db.set_value(
			"APS Demand Identity",
			identity.name,
			{
				"current_schedule": row.parent,
				"current_schedule_item": schedule_item,
				"status": "Active" if flt(row.qty) > QTY_TOLERANCE else "Cancelled",
				"last_resolution_method": "Manual Resolution",
				"last_resolution_reason": reason,
				"last_resolved_by": frappe.session.user,
				"last_resolved_on": now,
				"last_revised_on": now,
			},
			update_modified=False,
		)
	return {"schedule_item": schedule_item, "demand_identity": identity.name, "resolved": 1}


def backfill_active_demand_identities() -> dict[str, int]:
	"""Idempotently give every active legacy schedule row its own stable identity."""
	if not frappe.db.exists("DocType", "APS Demand Identity"):
		return {"created": 0, "linked": 0, "delivery_linked": 0, "ambiguities": 0}
	rows = frappe.db.sql(
		"""
		select i.name, i.parent, i.item_code, i.customer_part_no, i.schedule_date,
			i.qty, i.delivered_qty, i.produced_qty, i.demand_identity, i.external_line_reference,
			s.company, s.customer, s.schedule_scope
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where s.status = 'Active'
		order by s.company, s.customer, s.schedule_scope, s.creation, i.idx, i.name
		""",
		as_dict=True,
	)
	ambiguity_groups = _find_backfill_ambiguities(rows)
	ambiguous_rows = {
		row_name
		for group in ambiguity_groups
		for row_name in group.get("schedule_items") or []
	}
	for group in ambiguity_groups:
		_record_backfill_ambiguity(group)
	created = linked = delivery_linked = 0
	for row in rows:
		if row.name in ambiguous_rows:
			continue
		identity_name = row.demand_identity
		created_identity = False
		if not identity_name or not frappe.db.exists("APS Demand Identity", identity_name):
			identity = frappe.get_doc(
				{
					"doctype": "APS Demand Identity",
					"company": row.company,
					"customer": row.customer,
					"schedule_scope": row.schedule_scope or "Default Scope",
					"item_code": row.item_code,
					"customer_part_no": row.customer_part_no,
					"external_line_reference": row.external_line_reference,
					"current_schedule": row.parent,
					"current_schedule_item": row.name,
					"status": "Active" if flt(row.qty) > QTY_TOLERANCE else "Cancelled",
					"first_seen_on": now_datetime(),
					"last_revised_on": now_datetime(),
					"last_resolution_method": "Legacy Backfill",
				}
			).insert(ignore_permissions=True)
			identity_name = identity.name
			created += 1
			created_identity = True
		if created_identity:
			frappe.db.set_value(
				"Customer Delivery Schedule Item",
				row.name,
				{
					"demand_identity": identity_name,
					"identity_match_method": "Legacy Backfill",
					"original_schedule_date": row.schedule_date,
					"effective_schedule_date": row.schedule_date,
					"effective_qty": flt(row.qty),
					"executed_floor_qty": max(flt(row.delivered_qty), flt(row.produced_qty)),
					"excess_qty": max(max(flt(row.delivered_qty), flt(row.produced_qty)) - flt(row.qty), 0),
					"open_revised_qty": max(flt(row.qty) - flt(row.delivered_qty), 0),
				},
				update_modified=False,
			)
			linked += 1
		if frappe.db.exists("DocType", "APS Delivery Allocation"):
			pending_links = frappe.db.count(
				"APS Delivery Allocation",
				{"customer_schedule_item": row.name, "demand_identity": ("is", "not set")},
			)
			frappe.db.sql(
				"update `tabAPS Delivery Allocation` set demand_identity = %s where customer_schedule_item = %s and ifnull(demand_identity, '') = ''",
				(identity_name, row.name),
			)
			delivery_linked += pending_links
	return {
		"created": created,
		"linked": linked,
		"delivery_linked": delivery_linked,
		"ambiguities": len(ambiguity_groups),
	}


def _find_backfill_ambiguities(rows) -> list[dict[str, Any]]:
	external_groups = defaultdict(list)
	identity_groups = defaultdict(list)
	for source in rows or []:
		row = dict(source)
		external_reference = str(row.get("external_line_reference") or "").strip()
		if external_reference:
			external_groups[
				(
					row.get("company"),
					row.get("customer"),
					row.get("schedule_scope") or "Default Scope",
					row.get("item_code"),
					external_reference,
				)
			].append(row)
		if row.get("demand_identity"):
			identity_groups[row.get("demand_identity")].append(row)
	result = []
	for key, candidates in external_groups.items():
		if len(candidates) > 1:
			result.append(_backfill_ambiguity_group("DUPLICATE_EXTERNAL_LINE", key, candidates))
	for key, candidates in identity_groups.items():
		if len(candidates) > 1:
			result.append(_backfill_ambiguity_group("DUPLICATE_DEMAND_IDENTITY", (key,), candidates))
	return result


def _backfill_ambiguity_group(reason_code, business_key, candidates):
	schedule_items = sorted({row.get("name") for row in candidates if row.get("name")})
	fingerprint = hashlib.sha256(
		json.dumps(
			{"reason_code": reason_code, "business_key": business_key, "schedule_items": schedule_items},
			ensure_ascii=True,
			sort_keys=True,
			default=str,
		).encode()
	).hexdigest()
	first = candidates[0]
	return {
		"reason_code": reason_code,
		"fingerprint": fingerprint,
		"company": first.get("company"),
		"customer": first.get("customer"),
		"item_code": first.get("item_code"),
		"schedule": first.get("parent"),
		"business_key": list(business_key),
		"schedule_items": schedule_items,
	}


def _record_backfill_ambiguity(group):
	if not frappe.db.exists("DocType", "APS Exception Log"):
		return
	exception_type = f"Phase1 Identity Backfill {group['fingerprint'][:16]}"
	if frappe.db.exists(
		"APS Exception Log",
		{"exception_type": exception_type, "source_doctype": "Customer Delivery Schedule", "source_name": group.get("schedule")},
	):
		return
	frappe.get_doc(
		{
			"doctype": "APS Exception Log",
			"severity": "Warning",
			"exception_type": exception_type,
			"status": "Open",
			"item_code": group.get("item_code"),
			"customer": group.get("customer"),
			"message": _("Phase 1 migration found ambiguous active demand lineage and did not guess or merge it."),
			"resolution_hint": _("Review the listed active schedule rows and resolve their Demand Identity explicitly."),
			"diagnostic_json": json.dumps(group, ensure_ascii=True, sort_keys=True, default=str),
			"source_doctype": "Customer Delivery Schedule",
			"source_name": group.get("schedule"),
			"is_blocking": 0,
		}
	).insert(ignore_permissions=True)


def _normalize_scope(customer: str, company: str, schedule_scope: str | None) -> tuple[str, str, str]:
	customer = str(customer or "").strip()
	company = str(company or "").strip()
	schedule_scope = planning._normalize_schedule_scope(schedule_scope)
	if not customer or not company:
		frappe.throw(_("Customer and Company are required."), frappe.ValidationError)
	return customer, company, schedule_scope


def _require_v2_enabled():
	if not is_v2_enabled():
		frappe.throw(_("APS V2 is disabled. The current Legacy import and delivery workflow remains active."), frappe.PermissionError)


def _source_contract_mode(source_contract: str | None) -> str | None:
	value = str(source_contract or "").strip()
	if not value:
		return None
	try:
		return normalize_revision_mode(value)
	except frappe.ValidationError:
		return None


def _business_key(row: dict[str, Any]) -> tuple[str, str, str]:
	return (
		str(row.get("item_code") or ""),
		str(row.get("sales_order") or ""),
		str(row.get("customer_part_no") or ""),
	)


def _exact_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
	return (*_business_key(row), str(row.get("schedule_date") or ""))


def _get_active_revision_rows(company: str, customer: str, schedule_scope: str) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "Customer Delivery Schedule"):
		return []
	fields = [
		"i.name", "i.parent", "i.idx", "i.sales_order", "i.item_code", "i.customer_part_no",
		"i.schedule_date", "i.qty", "i.allocated_qty", "i.produced_qty", "i.delivered_qty",
		"i.balance_qty", "i.status", "i.remark", "i.production_strategy", "i.demand_confidence",
		"i.cancellation_risk_percent", "i.prebuild_allowed", "i.max_prebuild_days",
		"i.source_origin", "i.source_excel_row", "i.source_excel_rows", "i.manual_override",
		"i.manual_change_reason", "s.name as schedule_name", "s.creation as schedule_creation",
		"s.import_strategy", "s.revision_mode",
	]
	meta = frappe.get_meta("Customer Delivery Schedule Item")
	for fieldname in (
		"external_line_reference", "demand_identity", "original_schedule_date", "effective_schedule_date",
		"effective_qty", "executed_floor_qty", "excess_qty", "open_revised_qty",
	):
		if meta.has_field(fieldname):
			fields.append(f"i.{fieldname}")
	rows = frappe.db.sql(
		f"""
		select {', '.join(fields)}
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where s.company = %s and s.customer = %s
			and ifnull(s.schedule_scope, '') = ifnull(%s, '')
			and s.status = 'Active'
		order by i.schedule_date, s.creation, i.idx, i.name
		""",
		(company, customer, schedule_scope),
		as_dict=True,
	)
	return [dict(row) for row in rows]


def _build_active_state_token(company: str, customer: str, schedule_scope: str, rows: list[dict[str, Any]]) -> str:
	payload = {
		"company": company,
		"customer": customer,
		"schedule_scope": schedule_scope,
		"rows": [
			{
				"name": row.get("name"),
				"parent": row.get("parent"),
				"demand_identity": row.get("demand_identity") or "",
				"item_code": row.get("item_code") or "",
				"schedule_date": str(row.get("schedule_date") or ""),
				"qty": round(flt(row.get("qty")), 6),
				"produced_qty": round(flt(row.get("produced_qty")), 6),
				"delivered_qty": round(flt(row.get("delivered_qty")), 6),
			}
			for row in rows
		],
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _resolve_incoming_identities(*, company, customer, schedule_scope, previous_rows, incoming_rows, revision_mode):
	by_identity = {row.get("demand_identity"): row for row in previous_rows if row.get("demand_identity")}
	by_external = defaultdict(list)
	by_previous_item = {row.get("name"): row for row in previous_rows if row.get("name")}
	by_exact = defaultdict(list)
	by_business = defaultdict(list)
	for row in previous_rows:
		if row.get("external_line_reference"):
			by_external[row.get("external_line_reference")].append(row)
		by_exact[_exact_key(row)].append(row)
		by_business[_business_key(row)].append(row)
	resolved = []
	issues = []
	consumed = set()
	for index, source in enumerate(incoming_rows, start=1):
		row = dict(source)
		identity = None
		previous = None
		method = ""
		candidates = []
		explicit_identity = row.get("demand_identity")
		if revision_mode == "Incremental Demand":
			if explicit_identity or (row.get("external_line_reference") and by_external.get(row.get("external_line_reference"))):
				candidates = by_external.get(row.get("external_line_reference")) or ([by_identity.get(explicit_identity)] if by_identity.get(explicit_identity) else [])
				issues.append(_identity_issue(index, row, "INCREMENTAL_IDENTITY_REUSE", _("Incremental demand must use a new stable identity and a distinct external line reference."), candidates))
			else:
				method = "Incremental Identity"
		else:
			if explicit_identity:
				previous = by_identity.get(explicit_identity)
				if not previous:
					issues.append(
						_identity_issue(
							index,
							row,
							"IDENTITY_NOT_ACTIVE_IN_SCOPE",
							_("The selected Demand Identity is not part of the current active revision scope."),
							[],
						)
					)
				else:
					identity = explicit_identity
					method = "Manual Resolution"
					if not str(row.get("identity_resolution_reason") or "").strip():
						issues.append(
							_identity_issue(
								index,
								row,
								"MANUAL_RESOLUTION_REASON_REQUIRED",
								_("A reason is required when a Demand Identity is selected manually."),
								[previous],
							)
						)
			elif row.get("external_line_reference"):
				candidates = by_external.get(row.get("external_line_reference")) or []
				method = "External Line"
			elif row.get("previous_schedule_item"):
				candidates = [by_previous_item.get(row.get("previous_schedule_item"))] if by_previous_item.get(row.get("previous_schedule_item")) else []
				method = "Previous Schedule Item"
			elif row.get("previous_schedule_date"):
				candidates = by_exact.get(_exact_key({**row, "schedule_date": row.get("previous_schedule_date")})) or []
				method = "Explicit Previous Date"
			else:
				candidates = by_exact.get(_exact_key(row)) or []
				method = "Previous Schedule Item"
				if not candidates:
					candidates = by_business.get(_business_key(row)) or []
					method = "Unique Business Candidate"
			if candidates:
				candidates = [candidate for candidate in candidates if candidate]
				if len(candidates) == 1:
					previous = candidates[0]
					identity = previous.get("demand_identity")
				elif len(candidates) > 1:
					issues.append(_identity_issue(index, row, "AMBIGUOUS_IDENTITY", _("More than one active demand identity matches this row; APS will not guess."), candidates))
			if previous and not identity:
				issues.append(_identity_issue(index, row, "MISSING_LEGACY_IDENTITY", _("The matched active row has not been assigned a Demand Identity."), [previous]))
		if identity:
			if identity in consumed:
				issues.append(_identity_issue(index, row, "IDENTITY_REUSED_IN_INPUT", _("Two incoming rows resolve to the same Demand Identity."), [previous] if previous else []))
			consumed.add(identity)
		row["demand_identity"] = identity
		row["previous_row"] = previous
		row["previous_schedule_item"] = previous.get("name") if previous else None
		row["identity_match_method"] = method or "New Identity"
		resolved.append(row)
	return resolved, issues


def _identity_issue(index, row, code, message, candidates):
	return {
		"row_index": index,
		"source_excel_rows": planning._schedule_source_row_numbers(row),
		"item_code": row.get("item_code"),
		"schedule_date": str(row.get("schedule_date") or ""),
		"reason_code": code,
		"message": message,
		"candidates": [
			{
				"demand_identity": candidate.get("demand_identity"),
				"schedule_item": candidate.get("name"),
				"schedule_date": str(candidate.get("schedule_date") or ""),
				"qty": flt(candidate.get("qty")),
			}
			for candidate in candidates or []
		],
	}


def _build_revision_plan(previous_rows, incoming_rows, revision_mode):
	previous_by_identity = {row.get("demand_identity"): row for row in previous_rows if row.get("demand_identity")}
	touched = set()
	result_rows = []
	for source in incoming_rows:
		previous = source.get("previous_row") or {}
		row = _build_revision_row(previous, source, revision_mode)
		result_rows.append(row)
		if row.get("demand_identity"):
			touched.add(row.get("demand_identity"))
	if revision_mode == "Partial Revision":
		for identity, previous in previous_by_identity.items():
			if identity not in touched:
				result_rows.append(_build_retained_row(previous))
	elif revision_mode == "Full Replacement":
		for identity, previous in previous_by_identity.items():
			if identity not in touched:
				result_rows.append(_build_cancelled_row(previous))
	result_rows.sort(key=lambda row: (str(row.get("schedule_date") or ""), row.get("item_code") or "", row.get("demand_identity") or ""))
	for index, row in enumerate(result_rows, start=1):
		row["line_idx"] = index
	return {"rows": result_rows, "effective_rows": [dict(row) for row in result_rows]}


def _build_revision_row(previous, source, revision_mode):
	row = {key: value for key, value in source.items() if key not in {"previous_row"}}
	previous_qty = flt(previous.get("effective_qty") if previous.get("effective_qty") is not None else previous.get("qty"))
	new_qty = flt(source.get("qty"))
	previous_date = previous.get("schedule_date")
	delivered_qty = flt(previous.get("delivered_qty"))
	produced_qty = flt(previous.get("produced_qty"))
	executed_floor = max(delivered_qty, produced_qty, flt(previous.get("executed_floor_qty")))
	if revision_mode == "Incremental Demand":
		action = "Incremental"
	elif not previous:
		action = "Added"
	elif new_qty <= QTY_TOLERANCE:
		action = "Cancelled"
	elif previous_date and getdate(previous_date) != getdate(source.get("schedule_date")):
		action = "Date Moved"
	elif abs(new_qty - previous_qty) <= QTY_TOLERANCE:
		action = "Unchanged"
	else:
		action = "Changed"
	row.update(
		{
			"previous_qty": previous_qty,
			"new_qty": new_qty,
			"delta_qty": new_qty if revision_mode == "Incremental Demand" else new_qty - previous_qty,
			"previous_schedule_date": previous_date,
			"original_schedule_date": previous.get("original_schedule_date") or previous_date or source.get("schedule_date"),
			"effective_schedule_date": source.get("schedule_date"),
			"effective_qty": new_qty,
			"allocated_qty": flt(previous.get("allocated_qty")),
			"produced_qty": produced_qty,
			"delivered_qty": delivered_qty,
			"executed_floor_qty": executed_floor,
			"excess_qty": max(executed_floor - new_qty, 0),
			"open_revised_qty": max(new_qty - delivered_qty, 0),
			"balance_qty": max(new_qty - delivered_qty, 0),
			"revision_action": action,
			"change_type": _legacy_change_type(action, previous_qty, new_qty, previous_date, source.get("schedule_date")),
			"status": "Cancelled" if new_qty <= QTY_TOLERANCE else ("Covered" if delivered_qty >= new_qty else "Open"),
			"delivery_match_status": previous.get("delivery_match_status") or "Not Delivered",
		}
	)
	return row


def _build_retained_row(previous):
	row = dict(previous)
	qty = flt(previous.get("effective_qty") if previous.get("effective_qty") is not None else previous.get("qty"))
	delivered_qty = flt(previous.get("delivered_qty"))
	produced_qty = flt(previous.get("produced_qty"))
	executed_floor = max(delivered_qty, produced_qty, flt(previous.get("executed_floor_qty")))
	row.update(
		{
			"previous_row": previous,
			"previous_schedule_item": previous.get("name"),
			"previous_qty": qty,
			"new_qty": qty,
			"delta_qty": 0,
			"previous_schedule_date": previous.get("schedule_date"),
			"original_schedule_date": previous.get("original_schedule_date") or previous.get("schedule_date"),
			"effective_schedule_date": previous.get("schedule_date"),
			"effective_qty": qty,
			"executed_floor_qty": executed_floor,
			"excess_qty": max(executed_floor - qty, 0),
			"open_revised_qty": max(qty - delivered_qty, 0),
			"balance_qty": max(qty - delivered_qty, 0),
			"revision_action": "Retained",
			"change_type": "Unchanged",
			"identity_match_method": "Previous Schedule Item",
		}
	)
	return row


def _build_cancelled_row(previous):
	row = _build_retained_row(previous)
	executed_floor = max(flt(previous.get("delivered_qty")), flt(previous.get("produced_qty")), flt(previous.get("executed_floor_qty")))
	row.update(
		{
			"qty": 0,
			"new_qty": 0,
			"delta_qty": -flt(row.get("previous_qty")),
			"effective_qty": 0,
			"executed_floor_qty": executed_floor,
			"excess_qty": executed_floor,
			"open_revised_qty": 0,
			"balance_qty": 0,
			"revision_action": "Cancelled",
			"change_type": "Cancelled",
			"status": "Cancelled",
			"source_origin": "cancelled_by_replace",
		}
	)
	return row


def _legacy_change_type(action, previous_qty, new_qty, previous_date, current_date):
	if action == "Incremental":
		return "Appended"
	if action == "Added":
		return "Added"
	if action == "Cancelled":
		return "Cancelled"
	if action == "Date Moved":
		return "Advanced" if getdate(current_date) < getdate(previous_date) else "Delayed"
	if new_qty > previous_qty + QTY_TOLERANCE:
		return "Increased"
	if new_qty + QTY_TOLERANCE < previous_qty:
		return "Reduced"
	return "Unchanged"


def _build_revision_checks(*, revision_mode, recommendation, row_issues, duplicate_groups, duplicate_policy, identity_issues, rows):
	checks = []
	if row_issues:
		checks.append(_check("failed", _("Source validation"), _("One or more source rows are invalid."), [issue.get("message") for issue in row_issues], 1))
	else:
		checks.append(_check("passed", _("Source validation"), _("Customer, company, item, date, quantity, and Sales Order references are valid.")))
	if duplicate_groups and duplicate_policy == "Block":
		checks.append(_check("failed", _("Duplicate rows"), _("Duplicate business/date rows are blocked until an explicit policy is selected."), [str(row.get("excel_rows")) for row in duplicate_groups], 1))
	elif duplicate_groups:
		checks.append(_check("notice", _("Duplicate rows"), _("Duplicate rows will be summed because the user explicitly selected Sum.")))
	if identity_issues:
		checks.append(_check("failed", _("Demand Identity"), _("One or more rows have ambiguous or invalid identity lineage; APS will not guess."), [issue.get("message") for issue in identity_issues], 1))
	else:
		checks.append(_check("passed", _("Demand Identity"), _("Every incoming row has a unique inherited identity or a controlled new-identity action.")))
	checks.append(_check("notice", _("Mode recommendation"), recommendation.get("reason"), [_("Recommended: {0}").format(recommendation.get("recommended_mode")), _("Selected: {0}").format(revision_mode)]))
	if revision_mode == "Full Replacement" and any(row.get("revision_action") == "Cancelled" for row in rows):
		checks.append(_check("notice", _("Full replacement cancellations"), _("Omitted active identities will be recorded as zero-quantity cancellations; executed quantities become Excess.")))
	if revision_mode == "Incremental Demand" and rows:
		checks.append(_check("notice", _("Incremental overlap"), _("Every row creates independent demand and adds to the active quantity, even when item and dates overlap.")))
	return checks


def _check(status, title, summary, details=None, blocking=0):
	return {"status": status, "title": title, "summary": summary, "details": details or [], "blocking": cint(blocking)}


def _build_revision_fingerprint(*, company, customer, schedule_scope, version_no, revision_mode, source_type, source_contract, active_state_token, rows):
	# The request fingerprint is intentionally independent of mutable active state.
	# Concurrency is guarded separately by active_state_token; including that token
	# here would make an exact retry unrecognizable immediately after its first apply.
	payload = {
		"company": company,
		"customer": customer,
		"schedule_scope": schedule_scope,
		"version_no": version_no,
		"revision_mode": revision_mode,
		"source_type": source_type or "Customer Delivery Schedule",
		"source_contract": source_contract or "",
		"rows": rows,
	}
	return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _get_revision_replay(fingerprint):
	if not fingerprint or not frappe.db.exists("DocType", "Customer Delivery Schedule"):
		return None
	meta = frappe.get_meta("Customer Delivery Schedule")
	if not meta.has_field("revision_fingerprint"):
		return None
	name = frappe.db.get_value("Customer Delivery Schedule", {"revision_fingerprint": fingerprint}, "name")
	if not name:
		return None
	return {"schedule": name, "import_batch": frappe.db.get_value("Customer Delivery Schedule", name, "import_batch"), "revision_fingerprint": fingerprint}


def _lock_revision_scope(company, customer):
	if not frappe.db.sql("select name from `tabCompany` where name = %s for update", company):
		frappe.throw(_("Company {0} does not exist.").format(company), frappe.ValidationError)
	if not frappe.db.sql("select name from `tabCustomer` where name = %s for update", customer):
		frappe.throw(_("Customer {0} does not exist.").format(customer), frappe.ValidationError)


def _persist_revision(*, preview, file_url, source_type, mode_confirmation_reason):
	mode = preview["revision_mode"]
	strategy = MODE_TO_LEGACY_STRATEGY[mode]
	active_headers = frappe.get_all(
		"Customer Delivery Schedule",
		filters={"company": preview["company"], "customer": preview["customer"], "schedule_scope": preview["schedule_scope"], "status": "Active"},
		fields=["name", "creation", "import_strategy"],
		order_by="creation desc, name desc",
	)
	batch = frappe.get_doc(
		{
			"doctype": "APS Schedule Import Batch",
			"customer": preview["customer"],
			"company": preview["company"],
			"schedule_scope": preview["schedule_scope"],
			"version_no": preview["version_no"],
			"import_strategy": strategy,
			"duplicate_policy": preview.get("duplicate_policy") or "Block",
			"import_fingerprint": preview["revision_fingerprint"],
			"status": "Imported",
			"imported_rows": cint(preview["source_row_count"]),
			"effective_rows": cint(preview["effective_row_count"]),
			"previous_total_qty": flt(preview["previous_total_qty"]),
			"post_import_total_qty": flt(preview["post_revision_total_qty"]),
			"change_summary": json.dumps(preview.get("summary") or {}, ensure_ascii=True, sort_keys=True),
			"duplicate_summary": json.dumps(preview.get("duplicate_groups") or [], ensure_ascii=True, sort_keys=True, default=str),
			"source_type": source_type,
			"uploaded_file": file_url,
			"parser_mode": (preview.get("parse_context") or {}).get("parser_mode"),
			"sheet_name": (preview.get("parse_context") or {}).get("sheet_name"),
			"mapping_json": json.dumps((preview.get("parse_context") or {}).get("mapping") or {}, ensure_ascii=True, sort_keys=True),
		}
	).insert(ignore_permissions=True)
	if mode in {"Full Replacement", "Partial Revision"}:
		for header in active_headers:
			frappe.db.set_value("Customer Delivery Schedule", header.name, "status", "Superseded", update_modified=False)
	identity_names = {}
	for row in preview.get("effective_schedule_rows") or []:
		identity_name = row.get("demand_identity")
		if not identity_name:
			identity_doc = frappe.get_doc(
				{
					"doctype": "APS Demand Identity",
					"company": preview["company"],
					"customer": preview["customer"],
					"schedule_scope": preview["schedule_scope"],
					"item_code": row.get("item_code"),
					"customer_part_no": row.get("customer_part_no"),
					"external_line_reference": row.get("external_line_reference"),
					"status": "Active" if flt(row.get("effective_qty")) > QTY_TOLERANCE else "Cancelled",
					"first_seen_on": now_datetime(),
					"last_revised_on": now_datetime(),
					"last_resolution_method": row.get("identity_match_method") or "New Identity",
				}
			).insert(ignore_permissions=True)
			identity_name = identity_doc.name
		identity_names[id(row)] = identity_name
	dates = [getdate(row.get("schedule_date")) for row in preview.get("effective_schedule_rows") or [] if row.get("schedule_date") and flt(row.get("effective_qty")) > QTY_TOLERANCE]
	items = []
	for row in preview.get("effective_schedule_rows") or []:
		identity_name = identity_names[id(row)]
		items.append(
			{
				"sales_order": row.get("sales_order"),
				"item_code": row.get("item_code"),
				"customer_part_no": row.get("customer_part_no"),
				"external_line_reference": row.get("external_line_reference"),
				"demand_identity": identity_name,
				"previous_schedule_item": row.get("previous_schedule_item"),
				"identity_match_method": row.get("identity_match_method") or "New Identity",
				"identity_resolution_reason": row.get("identity_resolution_reason"),
				"identity_resolved_by": frappe.session.user if row.get("identity_match_method") == "Manual Resolution" else None,
				"identity_resolved_on": now_datetime() if row.get("identity_match_method") == "Manual Resolution" else None,
				"schedule_date": row.get("schedule_date"),
				"original_schedule_date": row.get("original_schedule_date"),
				"effective_schedule_date": row.get("effective_schedule_date"),
				"qty": flt(row.get("effective_qty")),
				"effective_qty": flt(row.get("effective_qty")),
				"executed_floor_qty": flt(row.get("executed_floor_qty")),
				"excess_qty": flt(row.get("excess_qty")),
				"open_revised_qty": flt(row.get("open_revised_qty")),
				"allocated_qty": flt(row.get("allocated_qty")),
				"produced_qty": flt(row.get("produced_qty")),
				"delivered_qty": flt(row.get("delivered_qty")),
				"balance_qty": flt(row.get("open_revised_qty")),
				"change_type": row.get("change_type") or "Unchanged",
				"revision_action": row.get("revision_action") or "Unchanged",
				"delivery_match_status": row.get("delivery_match_status") or "Not Delivered",
				"status": row.get("status") or "Open",
				"remark": row.get("remark"),
				"source_origin": row.get("source_origin") or "imported",
				"source_excel_row": cint(row.get("source_excel_row")),
				"source_excel_rows": row.get("source_excel_rows"),
				"manual_override": cint(row.get("manual_override")),
				"manual_change_reason": row.get("manual_change_reason"),
				"production_strategy": row.get("production_strategy") or "Auto Balance",
				"demand_confidence": row.get("demand_confidence") or "Confirmed",
				"cancellation_risk_percent": flt(row.get("cancellation_risk_percent")),
				"prebuild_allowed": cint(row.get("prebuild_allowed")) if row.get("prebuild_allowed") is not None else 1,
				"max_prebuild_days": cint(row.get("max_prebuild_days")),
			}
		)
	schedule = frappe.get_doc(
		{
			"doctype": "Customer Delivery Schedule",
			"customer": preview["customer"],
			"company": preview["company"],
			"schedule_scope": preview["schedule_scope"],
			"version_no": preview["version_no"],
			"import_strategy": strategy,
			"revision_mode": mode,
			"recommended_revision_mode": preview.get("recommended_revision_mode"),
			"recommendation_reason": preview.get("recommendation_reason"),
			"supersedes_schedule": active_headers[0].name if active_headers and mode != "Incremental Demand" else None,
			"effective_from": min(dates) if dates else None,
			"effective_to": max(dates) if dates else None,
			"mode_confirmed_by": frappe.session.user,
			"mode_confirmed_on": now_datetime(),
			"mode_confirmation_reason": mode_confirmation_reason,
			"source_contract": preview.get("source_contract"),
			"revision_fingerprint": preview["revision_fingerprint"],
			"import_batch": batch.name,
			"source_type": source_type,
			"status": "Active",
			"schedule_total_qty": sum(flt(row.get("effective_qty")) for row in preview.get("effective_schedule_rows") or []),
			"change_summary": json.dumps(preview.get("summary") or {}, ensure_ascii=True, sort_keys=True),
			"items": items,
		}
	)
	schedule.flags.aps_schedule_import_transition = True
	schedule.insert(ignore_permissions=True)
	for item in schedule.items:
		values = {
			"current_schedule": schedule.name,
			"current_schedule_item": item.name,
			"status": "Active" if flt(item.effective_qty) > QTY_TOLERANCE else "Cancelled",
			"last_revised_on": now_datetime(),
			"last_resolution_method": item.identity_match_method,
		}
		if item.identity_match_method == "Manual Resolution":
			values.update({"last_resolution_reason": item.identity_resolution_reason, "last_resolved_by": frappe.session.user, "last_resolved_on": now_datetime()})
		frappe.db.set_value("APS Demand Identity", item.demand_identity, values, update_modified=False)
	from injection_aps.services import delivery_fulfillment

	delivery_fulfillment.sync_delivery_allocations(
		company=preview["company"],
		customer=preview["customer"],
		item_codes=sorted({item.item_code for item in schedule.items if item.item_code}),
	)
	frappe.db.set_value("APS Schedule Import Batch", batch.name, "schedule_reference", schedule.name, update_modified=False)
	planning._record_schedule_deltas(
		import_batch=batch.name,
		schedule_name=schedule.name,
		customer=preview["customer"],
		company=preview["company"],
		schedule_scope=preview["schedule_scope"],
		diff_rows=preview.get("rows") or [],
	)
	return {
		"import_batch": batch.name,
		"schedule": schedule.name,
		"revision_mode": mode,
		"revision_fingerprint": preview["revision_fingerprint"],
		"summary": preview.get("summary") or {},
		"total_excess_qty": flt(preview.get("total_excess_qty")),
		"idempotent_replay": 0,
	}


def _get_identity(name):
	if not name or not frappe.db.exists("APS Demand Identity", name):
		frappe.throw(_("Demand Identity {0} does not exist.").format(name or "-"), frappe.ValidationError)
	return frappe.get_doc("APS Demand Identity", name)


def _validate_identity_scope(identity, company, customer, schedule_scope, item_code=None):
	if (identity.company, identity.customer, identity.schedule_scope) != (company, customer, schedule_scope):
		frappe.throw(_("The selected Demand Identity belongs to another company, customer, or schedule scope."), frappe.ValidationError)
	if item_code and identity.item_code != item_code:
		frappe.throw(_("The selected Demand Identity belongs to another item."), frappe.ValidationError)


def _get_schedule_item_with_scope(name):
	rows = frappe.db.sql(
		"""
		select i.name, i.parent, i.item_code, i.qty, s.company, s.customer, s.schedule_scope,
			s.status as schedule_status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name = %s
		""",
		name,
		as_dict=True,
	)
	return rows[0] if rows else None
