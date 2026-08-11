from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


EFFECTIVE_SEGMENT_KINDS = ("Primary", "Manual")
INACTIVE_SEGMENT_STATUSES = ("Blocked", "Cancelled")
MANAGED_EXCEPTION_TYPES = (
	"Late Delivery",
	"Unscheduled Quantity",
	"Overproduction",
	"Plan Consistency Error",
)
MANAGED_RISK_FLAGS = (
	"Late Delivery",
	"Invalid Segment",
	"Frozen / Locked",
	"Execution: Delayed",
	"Execution: Slow Progress",
	"Execution: No Recent Update",
	"Execution: Overproduced",
)
RISK_RANK = {"Normal": 0, "Attention": 1, "Critical": 2, "Blocked": 3}
QTY_TOLERANCE = 0.000001


def calculate_quantity_fields(planned_qty: float, machine_scheduled_qty: float) -> dict[str, float]:
	planned_qty = flt(planned_qty)
	machine_scheduled_qty = flt(machine_scheduled_qty)
	return {
		"planned_qty": planned_qty,
		"machine_scheduled_qty": machine_scheduled_qty,
		"demand_covered_qty": min(planned_qty, machine_scheduled_qty),
		"overproduction_qty": max(machine_scheduled_qty - planned_qty, 0),
		"unscheduled_qty": max(planned_qty - machine_scheduled_qty, 0),
	}


def is_effective_primary_segment(segment: dict[str, Any] | Any) -> bool:
	kind = _value(segment, "segment_kind") or "Primary"
	status = _value(segment, "segment_status") or "Planned"
	return (
		kind in EFFECTIVE_SEGMENT_KINDS
		and status not in INACTIVE_SEGMENT_STATUSES
		and flt(_value(segment, "planned_qty")) > 0
		and _segment_structure_errors(segment) == []
	)


def recalculate_plan_consistency(run_name: str, reason: str | None = None) -> dict[str, Any]:
	"""Rebuild every canonical quantity and risk projection for one planning run."""
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	_resolve_managed_exceptions(run_name)
	open_exceptions = frappe.get_all(
		"APS Exception Log",
		filters={"planning_run": run_name, "status": "Open"},
		fields=["name", "severity", "exception_type", "source_name", "source_doctype", "is_blocking"],
	)
	exceptions_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for row in open_exceptions:
		if row.source_name:
			exceptions_by_source[row.source_name].append(row)

	result_summaries = []
	for result_name in frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		pluck="name",
		order_by="creation asc",
	):
		result_summaries.append(
			_recalculate_result(
				frappe.get_doc("APS Schedule Result", result_name),
				exceptions_by_source=exceptions_by_source,
			)
		)

	totals = _sum_run_totals(result_summaries)
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		{
			"total_net_requirement_qty": totals["planned_qty"],
			"total_machine_scheduled_qty": totals["machine_scheduled_qty"],
			"total_demand_covered_qty": totals["demand_covered_qty"],
			"total_overproduction_qty": totals["overproduction_qty"],
			"total_scheduled_qty": totals["machine_scheduled_qty"],
			"total_unscheduled_qty": totals["unscheduled_qty"],
			"total_produced_qty": totals["produced_qty"],
			"total_delivered_qty": totals["delivered_qty"],
			"result_count": len(result_summaries),
			"consistency_status": "Unchecked",
			"consistency_checked_on": now_datetime(),
			"consistency_details": _("Recalculation in progress: {0}").format(reason or _("plan mutation")),
		},
		update_modified=False,
	)

	validation = validate_plan_consistency(run_name, update_run=False)
	if validation["valid"]:
		_resolve_managed_exceptions(run_name, exception_types=("Plan Consistency Error",))
	else:
		_ensure_managed_exception(
			planning_run=run_name,
			severity="Blocking",
			exception_type="Plan Consistency Error",
			message=_("Planning Run {0} failed quantity/risk consistency validation with {1} error(s).").format(
				run_name, len(validation["errors"])
			),
			source_doctype="APS Planning Run",
			source_name=run_name,
			resolution_hint=_("Recalculate the run and resolve every reported inconsistency before release."),
			is_blocking=1,
			diagnostic={"errors": validation["errors"][:50]},
		)

	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		{
			"consistency_status": "Valid" if validation["valid"] else "Invalid",
			"consistency_checked_on": now_datetime(),
			"consistency_details": json.dumps(
				{
					"reason": reason or "plan mutation",
					"error_count": len(validation["errors"]),
					"errors": validation["errors"][:50],
				},
				ensure_ascii=False,
				sort_keys=True,
			),
			"exception_count": frappe.db.count(
				"APS Exception Log", {"planning_run": run_name, "status": "Open"}
			),
		},
		update_modified=False,
	)
	return {
		"run": run_name,
		"valid": validation["valid"],
		"errors": validation["errors"],
		"totals": totals,
		"results": result_summaries,
	}


def validate_plan_consistency(run_name: str, update_run: bool = True) -> dict[str, Any]:
	run_row = frappe.db.get_value(
		"APS Planning Run",
		run_name,
		[
			"total_net_requirement_qty",
			"total_machine_scheduled_qty",
			"total_demand_covered_qty",
			"total_overproduction_qty",
			"total_scheduled_qty",
			"total_unscheduled_qty",
			"total_produced_qty",
			"total_delivered_qty",
			"result_count",
		],
		as_dict=True,
	)
	if not run_row:
		frappe.throw(_("APS Planning Run {0} was not found.").format(run_name))

	errors: list[dict[str, Any]] = []
	result_totals = []
	for row in frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
			"name",
			"planned_qty",
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"scheduled_qty",
			"unscheduled_qty",
			"produced_qty",
			"delivered_qty",
			"risk_status",
			"net_requirement",
		],
		order_by="creation asc",
	):
		segments = frappe.get_all(
			"APS Schedule Segment",
			filters={"parent": row.name, "parenttype": "APS Schedule Result"},
			fields=[
				"name",
				"workstation",
				"start_time",
				"end_time",
				"planned_qty",
				"segment_kind",
				"segment_status",
				"risk_status",
			],
		)
		for segment in segments:
			if _is_active_machine_segment(segment):
				for structural_error in _segment_structure_errors(segment):
					errors.append(
						_error("invalid_segment", row.name, structural_error, segment=segment.name)
					)
		effective_segments = [segment for segment in segments if is_effective_primary_segment(segment)]
		machine_scheduled_qty = sum(flt(segment.planned_qty) for segment in effective_segments)
		expected = calculate_quantity_fields(row.planned_qty, machine_scheduled_qty)
		for fieldname in (
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"unscheduled_qty",
		):
			_compare_qty(errors, row.name, fieldname, row.get(fieldname), expected[fieldname])
		_compare_qty(errors, row.name, "scheduled_qty_legacy_mirror", row.scheduled_qty, machine_scheduled_qty)

		due_datetime = _due_datetime(row.name)
		late_segments = [
			segment
			for segment in effective_segments
			if segment.end_time and get_datetime(segment.end_time) > due_datetime
		]
		if late_segments and row.risk_status not in ("Critical", "Blocked"):
			errors.append(_error("result_lateness_risk", row.name, _("Late result is not Critical or Blocked.")))
		for segment in late_segments:
			if segment.risk_status not in ("Critical", "Blocked"):
				errors.append(
					_error("segment_lateness_risk", row.name, _("Late segment is not Critical or Blocked."), segment=segment.name)
				)
			if not frappe.db.exists(
				"APS Exception Log",
				{
					"planning_run": run_name,
					"exception_type": "Late Delivery",
					"source_doctype": "APS Schedule Segment",
					"source_name": segment.name,
					"status": "Open",
				},
			):
				errors.append(
					_error("missing_lateness_exception", row.name, _("Late segment has no open exception."), segment=segment.name)
				)
		result_totals.append(
			{
				**expected,
				"produced_qty": flt(row.produced_qty),
				"delivered_qty": flt(row.delivered_qty),
			}
		)

	expected_totals = _sum_run_totals(result_totals)
	run_field_map = {
		"total_net_requirement_qty": "planned_qty",
		"total_machine_scheduled_qty": "machine_scheduled_qty",
		"total_demand_covered_qty": "demand_covered_qty",
		"total_overproduction_qty": "overproduction_qty",
		"total_scheduled_qty": "machine_scheduled_qty",
		"total_unscheduled_qty": "unscheduled_qty",
		"total_produced_qty": "produced_qty",
		"total_delivered_qty": "delivered_qty",
	}
	for run_field, total_field in run_field_map.items():
		_compare_qty(errors, run_name, run_field, run_row.get(run_field), expected_totals[total_field])
	if cint(run_row.result_count) != len(result_totals):
		errors.append(
			_error(
				"run_result_count",
				run_name,
				_("Run result count {0} does not match {1} result rows.").format(run_row.result_count, len(result_totals)),
			)
		)

	valid = not errors
	if update_run:
		frappe.db.set_value(
			"APS Planning Run",
			run_name,
			{
				"consistency_status": "Valid" if valid else "Invalid",
				"consistency_checked_on": now_datetime(),
				"consistency_details": json.dumps(
					{"error_count": len(errors), "errors": errors[:50]},
					ensure_ascii=False,
					sort_keys=True,
				),
			},
			update_modified=False,
		)
	return {"run": run_name, "valid": valid, "errors": errors, "totals": expected_totals}


def assert_plan_consistent(run_name: str, recalculate: bool = True, reason: str | None = None) -> dict[str, Any]:
	result = (
		recalculate_plan_consistency(run_name, reason=reason or "pre-release consistency gate")
		if recalculate
		else validate_plan_consistency(run_name)
	)
	if not result["valid"]:
		messages = [row.get("message") or row.get("code") for row in result["errors"][:8]]
		frappe.throw(
			_("Plan consistency validation failed. Release is blocked until these errors are resolved:<br>{0}").format(
				"<br>".join(messages)
			),
			frappe.ValidationError,
		)
	return result


def get_run_quantity_summary(run_name: str) -> dict[str, Any]:
	row = frappe.db.get_value(
		"APS Planning Run",
		run_name,
		[
			"total_net_requirement_qty as planned_qty",
			"total_machine_scheduled_qty as machine_scheduled_qty",
			"total_demand_covered_qty as demand_covered_qty",
			"total_overproduction_qty as overproduction_qty",
			"total_unscheduled_qty as unscheduled_qty",
			"total_produced_qty as produced_qty",
			"total_delivered_qty as delivered_qty",
			"total_prebuild_qty as prebuild_qty",
			"total_jit_qty as jit_qty",
			"total_scrap_qty as scrap_qty",
			"total_current_deliverable_qty as current_deliverable_qty",
			"total_prebuild_inventory_qty as prebuild_inventory_qty",
			"total_cancellation_inventory_risk_qty as cancellation_inventory_risk_qty",
			"consistency_status",
			"consistency_checked_on",
		],
		as_dict=True,
	)
	return dict(row or {})


def get_worst_risk(*statuses: str | None) -> str:
	risk = "Normal"
	for status in statuses:
		risk = _worse_risk(risk, status)
	return risk


def get_exception_risk(rows: list[dict[str, Any]]) -> str:
	return _risk_from_exceptions(rows)


def _recalculate_result(result_doc, exceptions_by_source: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
	due_datetime = _due_datetime(result_doc.name, result_doc.requested_date)
	effective_segments = []
	result_risk = _risk_from_exceptions(
		(exceptions_by_source.get(result_doc.name) or [])
		+ (exceptions_by_source.get(result_doc.net_requirement) or [])
	)
	projected_completion_time = None
	schedule_delay_minutes = 0.0
	execution_by_work_order: dict[str, float] = defaultdict(float)
	unlinked_produced_qty = 0.0
	selected_moulds = []
	structural_errors = []

	for segment in result_doc.segments or []:
		segment_errors = _segment_structure_errors(segment) if _is_active_machine_segment(segment) else []
		segment_risk = _risk_from_exceptions(exceptions_by_source.get(segment.name) or [])
		flags = [
			flag
			for flag in (segment.risk_flags or "").splitlines()
			if flag and flag not in MANAGED_RISK_FLAGS
		]
		if segment_errors:
			segment_risk = _worse_risk(segment_risk, "Blocked")
			flags.append("Invalid Segment")
			structural_errors.extend((segment.name, message) for message in segment_errors)
		if segment.segment_status == "Blocked":
			segment_risk = _worse_risk(segment_risk, "Blocked")
		if segment.actual_status in ("Delayed", "Overproduced"):
			segment_risk = _worse_risk(segment_risk, "Critical")
			flags.append(f"Execution: {segment.actual_status}")
		elif segment.actual_status in ("Slow Progress", "No Recent Update"):
			segment_risk = _worse_risk(segment_risk, "Attention")
			flags.append(f"Execution: {segment.actual_status}")
		if cint(segment.is_locked) or cint(segment.anchor_strength) >= 50:
			flags.append("Frozen / Locked")

		segment_delay = 0.0
		if is_effective_primary_segment(segment):
			effective_segments.append(segment)
			end_time = get_datetime(segment.end_time)
			projected_completion_time = max(projected_completion_time or end_time, end_time)
			if end_time > due_datetime:
				segment_delay = (end_time - due_datetime).total_seconds() / 60
				schedule_delay_minutes = max(schedule_delay_minutes, segment_delay)
				segment_risk = _worse_risk(segment_risk, "Critical")
				flags.append("Late Delivery")
				_ensure_managed_exception(
					planning_run=result_doc.planning_run,
					severity="Critical",
					exception_type="Late Delivery",
					message=_("Segment {0} ends after requested delivery time by {1} minute(s).").format(
						segment.name, round(segment_delay, 2)
					),
					item_code=result_doc.item_code,
					customer=result_doc.customer,
					workstation=segment.workstation,
					source_doctype="APS Schedule Segment",
					source_name=segment.name,
					resolution_hint=_("Move or resize this segment, or confirm a revised customer delivery date."),
					is_blocking=0,
					diagnostic={
						"result": result_doc.name,
						"required_delivery_time": str(due_datetime),
						"segment_end_time": str(end_time),
						"schedule_delay_minutes": segment_delay,
					},
				)
			if segment.mould_reference:
				selected_moulds.append(segment.mould_reference)

		actual_qty = flt(segment.actual_completed_qty)
		if segment.linked_work_order:
			execution_by_work_order[segment.linked_work_order] = max(
				execution_by_work_order[segment.linked_work_order], actual_qty
			)
		else:
			unlinked_produced_qty += actual_qty
		segment.risk_status = segment_risk
		segment.schedule_delay_minutes = segment_delay
		segment.risk_flags = "\n".join(dict.fromkeys(flags))
		frappe.db.set_value(
			"APS Schedule Segment",
			segment.name,
			{
				"risk_status": segment.risk_status,
				"schedule_delay_minutes": segment.schedule_delay_minutes,
				"risk_flags": segment.risk_flags,
			},
			update_modified=False,
		)

	machine_scheduled_qty = sum(flt(segment.planned_qty) for segment in effective_segments)
	quantities = calculate_quantity_fields(result_doc.planned_qty, machine_scheduled_qty)
	source_progress = _get_source_progress(result_doc)
	execution_produced_qty = unlinked_produced_qty + sum(execution_by_work_order.values())
	production_ledger = _get_production_ledger_progress(result_doc.name)
	produced_qty = (
		production_ledger["good_qty"]
		if production_ledger["authoritative"]
		else max(execution_produced_qty, source_progress["produced_qty"])
	)
	delivered_qty = source_progress["delivered_qty"]

	if quantities["unscheduled_qty"] > QTY_TOLERANCE:
		result_risk = _worse_risk(result_risk, "Attention")
		_ensure_managed_exception(
			planning_run=result_doc.planning_run,
			severity="Warning",
			exception_type="Unscheduled Quantity",
			message=_("Result {0} still has {1} unscheduled.").format(
				result_doc.name, quantities["unscheduled_qty"]
			),
			item_code=result_doc.item_code,
			customer=result_doc.customer,
			source_doctype="APS Schedule Result",
			source_name=result_doc.name,
			resolution_hint=_("Add valid machine segments or reduce the planned demand quantity."),
			is_blocking=0,
		)
	if quantities["overproduction_qty"] > QTY_TOLERANCE:
		result_risk = _worse_risk(result_risk, "Attention")
		_ensure_managed_exception(
			planning_run=result_doc.planning_run,
			severity="Warning",
			exception_type="Overproduction",
			message=_("Result {0} schedules {1} above planned demand.").format(
				result_doc.name, quantities["overproduction_qty"]
			),
			item_code=result_doc.item_code,
			customer=result_doc.customer,
			source_doctype="APS Schedule Result",
			source_name=result_doc.name,
			resolution_hint=_("Confirm the quantity as stock build or resize the machine segments."),
			is_blocking=0,
		)
	if schedule_delay_minutes > 0:
		result_risk = _worse_risk(result_risk, "Critical")
	for segment in result_doc.segments or []:
		result_risk = _worse_risk(result_risk, segment.risk_status or "Normal")
	for segment_name, message in structural_errors:
		result_risk = _worse_risk(result_risk, "Blocked")
		_ensure_managed_exception(
			planning_run=result_doc.planning_run,
			severity="Blocking",
			exception_type="Plan Consistency Error",
			message=message,
			item_code=result_doc.item_code,
			customer=result_doc.customer,
			source_doctype="APS Schedule Segment",
			source_name=segment_name,
			resolution_hint=_("Repair or cancel the invalid segment before releasing the plan."),
			is_blocking=1,
		)

	result_status = result_doc.status
	if result_doc.status in ("Draft", "Planned", "Risk", "Blocked"):
		result_status = "Blocked" if result_risk == "Blocked" else "Risk" if result_risk != "Normal" else "Planned"
	blocking_reason = result_doc.blocking_reason or ""
	if result_risk == "Blocked" and structural_errors:
		blocking_reason = "Plan consistency: {0}".format(
			"; ".join(message for _segment_name, message in structural_errors[:5])
		)
	elif blocking_reason.startswith("Plan consistency: "):
		blocking_reason = ""
	frappe.db.set_value(
		"APS Schedule Result",
		result_doc.name,
		{
			"machine_scheduled_qty": quantities["machine_scheduled_qty"],
			"demand_covered_qty": quantities["demand_covered_qty"],
			"overproduction_qty": quantities["overproduction_qty"],
			"scheduled_qty": quantities["machine_scheduled_qty"],
			"unscheduled_qty": quantities["unscheduled_qty"],
			"produced_qty": produced_qty,
			"good_produced_qty": produced_qty,
			"scrap_qty": production_ledger["scrap_qty"] if production_ledger["authoritative"] else flt(result_doc.scrap_qty),
			"delivered_qty": delivered_qty,
			"actual_progress_qty": produced_qty,
			"projected_completion_time": projected_completion_time,
			"schedule_delay_minutes": schedule_delay_minutes,
			"risk_status": result_risk,
			"primary_mould_reference": selected_moulds[0] if selected_moulds else "",
			"selected_moulds": "\n".join(dict.fromkeys(selected_moulds)),
			"status": result_status,
			"blocking_reason": blocking_reason,
		},
		update_modified=False,
	)
	return {
		"name": result_doc.name,
		**quantities,
		"produced_qty": produced_qty,
		"delivered_qty": delivered_qty,
		"risk_status": result_risk,
		"schedule_delay_minutes": schedule_delay_minutes,
		"late_segment_count": sum(
			1 for segment in effective_segments if get_datetime(segment.end_time) > due_datetime
		),
	}


def _get_source_progress(result_doc) -> dict[str, float]:
	produced_qty = 0.0
	delivered_qty = 0.0
	if frappe.db.exists("DocType", "Customer Delivery Schedule"):
		row = frappe.db.sql(
			"""
			select
				coalesce(sum(i.produced_qty), 0) as produced_qty,
				coalesce(sum(i.delivered_qty), 0) as delivered_qty
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.company = %(company)s
				and s.status = 'Active'
				and ifnull(s.customer, '') = ifnull(%(customer)s, '')
				and i.item_code = %(item_code)s
				and i.schedule_date = %(requested_date)s
			""",
			{
				"company": result_doc.company,
				"customer": result_doc.customer,
				"item_code": result_doc.item_code,
				"requested_date": getdate(result_doc.requested_date),
			},
			as_dict=True,
		)[0]
		produced_qty = flt(row.produced_qty)
		delivered_qty = flt(row.delivered_qty)
	if result_doc.demand_source == "Sales Order Backlog" and frappe.db.exists("DocType", "Sales Order"):
		row = frappe.db.sql(
			"""
			select coalesce(sum(i.delivered_qty), 0) as delivered_qty
			from `tabSales Order Item` i
			inner join `tabSales Order` s on s.name = i.parent
			where s.company = %(company)s
				and s.docstatus = 1
				and ifnull(s.customer, '') = ifnull(%(customer)s, '')
				and i.item_code = %(item_code)s
				and i.delivery_date = %(requested_date)s
			""",
			{
				"company": result_doc.company,
				"customer": result_doc.customer,
				"item_code": result_doc.item_code,
				"requested_date": getdate(result_doc.requested_date),
			},
			as_dict=True,
		)[0]
		delivered_qty = flt(row.delivered_qty)
	return {"produced_qty": produced_qty, "delivered_qty": delivered_qty}


def _get_production_ledger_progress(result_name: str) -> dict[str, Any]:
	if not frappe.db.exists("DocType", "APS Production Allocation"):
		return {"authoritative": False, "good_qty": 0.0, "scrap_qty": 0.0}
	has_ledger = frappe.db.exists("APS Production Allocation", {"schedule_result": result_name})
	has_synced_scope = bool(
		frappe.db.get_value("APS Schedule Result", result_name, "last_execution_sync_on")
	)
	if not has_ledger and not has_synced_scope:
		return {"authoritative": False, "good_qty": 0.0, "scrap_qty": 0.0}
	row = frappe.db.sql(
		"""
		select
			coalesce(sum(case when is_effective = 1 then good_qty else 0 end), 0) as good_qty,
			coalesce(sum(case when is_effective = 1 then scrap_qty else 0 end), 0) as scrap_qty
		from `tabAPS Production Allocation`
		where schedule_result = %s
		""",
		result_name,
		as_dict=True,
	)[0]
	return {"authoritative": True, "good_qty": flt(row.good_qty), "scrap_qty": flt(row.scrap_qty)}


def _sum_run_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
	return {
		fieldname: sum(flt(row.get(fieldname)) for row in rows)
		for fieldname in (
			"planned_qty",
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"unscheduled_qty",
			"produced_qty",
			"delivered_qty",
		)
	}


def _segment_structure_errors(segment: dict[str, Any] | Any) -> list[str]:
	errors = []
	name = _value(segment, "name") or _("new segment")
	if not _value(segment, "workstation"):
		errors.append(_("Segment {0} has no workstation.").format(name))
	start_time = _value(segment, "start_time")
	end_time = _value(segment, "end_time")
	if not start_time or not end_time:
		errors.append(_("Segment {0} must have both start and end time.").format(name))
	elif get_datetime(end_time) <= get_datetime(start_time):
		errors.append(_("Segment {0} end time must be later than start time.").format(name))
	return errors


def _is_active_machine_segment(segment: dict[str, Any] | Any) -> bool:
	return (
		(_value(segment, "segment_kind") or "Primary") in EFFECTIVE_SEGMENT_KINDS
		and (_value(segment, "segment_status") or "Planned") not in INACTIVE_SEGMENT_STATUSES
		and flt(_value(segment, "planned_qty")) > 0
	)


def _due_datetime(result_name: str, requested_date=None):
	requested_date = requested_date or frappe.db.get_value("APS Schedule Result", result_name, "requested_date")
	date_value = getdate(requested_date)
	return get_datetime(f"{date_value} 23:59:59")


def _risk_from_exceptions(rows: list[dict[str, Any]]) -> str:
	risk = "Normal"
	for row in rows:
		if cint(row.get("is_blocking")) or row.get("severity") == "Blocking":
			risk = _worse_risk(risk, "Blocked")
		elif row.get("severity") == "Critical":
			risk = _worse_risk(risk, "Critical")
		elif row.get("severity") == "Warning":
			risk = _worse_risk(risk, "Attention")
	return risk


def _worse_risk(current: str | None, candidate: str | None) -> str:
	current = current if current in RISK_RANK else "Normal"
	candidate = candidate if candidate in RISK_RANK else "Normal"
	return candidate if RISK_RANK[candidate] > RISK_RANK[current] else current


def _ensure_managed_exception(
	planning_run: str,
	severity: str,
	exception_type: str,
	message: str,
	item_code: str | None = None,
	customer: str | None = None,
	workstation: str | None = None,
	source_doctype: str | None = None,
	source_name: str | None = None,
	resolution_hint: str | None = None,
	is_blocking: int = 0,
	diagnostic: dict[str, Any] | None = None,
):
	filters = {
		"planning_run": planning_run,
		"exception_type": exception_type,
		"source_doctype": source_doctype,
		"source_name": source_name,
	}
	existing = frappe.db.get_value("APS Exception Log", filters, "name", order_by="creation asc")
	values = {
		"severity": severity,
		"message": message,
		"item_code": item_code,
		"customer": customer,
		"workstation": workstation,
		"resolution_hint": resolution_hint,
		"is_blocking": is_blocking,
		"diagnostic_json": json.dumps(diagnostic or {}, ensure_ascii=False, sort_keys=True, default=str),
		"status": "Open",
	}
	if existing:
		frappe.db.set_value("APS Exception Log", existing, values, update_modified=False)
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "APS Exception Log",
			"planning_run": planning_run,
			"exception_type": exception_type,
			"source_doctype": source_doctype,
			"source_name": source_name,
			**values,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _resolve_managed_exceptions(run_name: str, exception_types: tuple[str, ...] = MANAGED_EXCEPTION_TYPES):
	for name in frappe.get_all(
		"APS Exception Log",
		filters={
			"planning_run": run_name,
			"exception_type": ("in", exception_types),
			"status": "Open",
		},
		pluck="name",
	):
		frappe.db.set_value("APS Exception Log", name, "status", "Resolved", update_modified=False)


def _compare_qty(errors: list[dict[str, Any]], source: str, fieldname: str, actual, expected):
	if abs(flt(actual) - flt(expected)) <= QTY_TOLERANCE:
		return
	errors.append(
		_error(
			"quantity_mismatch",
			source,
			_("{0} is {1}, expected {2}.").format(fieldname, flt(actual), flt(expected)),
			fieldname=fieldname,
			actual=flt(actual),
			expected=flt(expected),
		)
	)


def _error(code: str, source: str, message: str, **details) -> dict[str, Any]:
	return {"code": code, "source": source, "message": message, **details}


def _value(row: dict[str, Any] | Any, fieldname: str):
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)
