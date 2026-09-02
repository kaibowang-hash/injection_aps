from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, today

from injection_aps.services.v2_flags import is_v2_enabled


def create_trial_run_for_admission(
	*,
	company: str,
	plant_floor: str | None = None,
	plant_floors: list[str] | str | None = None,
	item_code: str | None = None,
	customer: str | None = None,
	horizon_days: int | None = None,
	existing_work_order_policy: str | None = None,
) -> dict[str, Any]:
	"""Create a V2 Draft Run and its demand baseline without scheduling it."""
	if not is_v2_enabled():
		frappe.throw(
			_("APS V2 is disabled; use the existing recalculation flow."),
			frappe.ValidationError,
		)

	from injection_aps.services import demand_ledger, horizon_status, planning

	settings = planning.get_settings_dict()
	policy = planning._normalize_existing_work_order_policy(existing_work_order_policy)
	resolved_item = planning._resolve_item_name(item_code) if item_code else None
	selected_floors = planning._normalize_selected_plant_floors(
		company=company,
		plant_floors=plant_floors,
		plant_floor=plant_floor,
		required=True,
	)
	planning._lock_company_for_aps_planning(company)
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": company,
			"plant_floor": selected_floors[0],
			"planning_date": today(),
			"horizon_days": cint(horizon_days or settings.get("planning_horizon_days") or 14),
			"horizon_start": now_datetime(),
			"run_type": "Trial",
			"existing_work_order_policy": policy,
			"planning_customer_filter": customer or None,
			"planning_item_filter": resolved_item or None,
			"status": "Draft",
			"approval_state": "Pending",
			"consistency_status": "Unchecked",
			"capacity_balance_status": "Not Analyzed",
		}
	)
	for fieldname, value in horizon_status.planning_run_window_fields(run, settings).items():
		setattr(run, fieldname, value)
	run.due_time_policy = settings.get("due_time_policy") or "Delivery Date End Of Day"
	planning._apply_selected_plant_floors_to_run(run, selected_floors)
	run.flags.aps_run_transition = True
	run.insert(ignore_permissions=True)
	baseline = demand_ledger.prepare_run_demand_baseline(run.name)
	admission = baseline.get("admission") or {}
	state = admission.get("admission_state") or {}
	has_optional = bool(cint(state.get("optional_row_count")))
	next_route = (
		f"aps-demand-admission-workbench?run_name={run.name}"
		if has_optional
		else f"aps-run-console?run_name={run.name}&from_admission=auto"
	)
	return {
		"run": run.name,
		"status": "Draft",
		"selected_plant_floors": selected_floors,
		"admission_summary": admission.get("summary") or {},
		"admission_state": state,
		"admission_required": int(has_optional),
		"auto_skipped": int(not has_optional),
		"next_route": next_route,
	}
