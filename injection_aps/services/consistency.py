from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime


EFFECTIVE_SEGMENT_KINDS = ("Primary", "Manual")
INACTIVE_SEGMENT_STATUSES = ("Blocked", "Cancelled")
MANAGED_EXCEPTION_TYPES = (
	"Late Delivery",
	"Unscheduled Quantity",
	"Overproduction",
	"Plan Consistency Error",
	"Demand Lineage Changed",
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
AUDIT_DIFFERENCE_LIMIT = 200
AUDIT_EXISTING_WORK_ORDER_POLICIES = ("Include", "Exclude")


class _AuditDifferenceCollector(list):
	"""Keep the response bounded without hiding the true discrepancy count."""

	def __init__(self, limit: int = AUDIT_DIFFERENCE_LIMIT):
		super().__init__()
		self.limit = max(cint(limit), 1)
		self.total_count = 0

	def append(self, value):
		self.total_count += 1
		if len(self) < self.limit:
			super().append(value)

	def merge(self, values):
		if isinstance(values, _AuditDifferenceCollector):
			for value in values:
				self.append(value)
			self.total_count += max(values.total_count - len(values), 0)
			return
		for value in values or []:
			self.append(value)


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


def is_effective_quantity_segment(segment: dict[str, Any] | Any) -> bool:
	"""Return physical output rows that contribute quantity to their own Result.

	A V2 co-product row carries quantity and execution lineage, but its shared
	machine/mold interval is owned by the campaign Primary row. It therefore
	participates in Result quantity math without becoming a capacity interval.
	"""
	if is_effective_primary_segment(segment):
		return True
	return (
		(_value(segment, "segment_kind") or "") == "Family Co-Product"
		and (_value(segment, "segment_status") or "Planned") not in INACTIVE_SEGMENT_STATUSES
		and flt(_value(segment, "planned_qty")) > 0
		and _segment_structure_errors(segment) == []
		and _family_co_product_allocation_error(segment) is None
	)


def recalculate_plan_consistency(run_name: str, reason: str | None = None) -> dict[str, Any]:
	"""Rebuild every canonical quantity and risk projection for one planning run."""
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	_resolve_managed_exceptions(run_name)
	_sync_demand_lineage_exceptions(run_name)
	open_exceptions = frappe.get_all(
		"APS Exception Log",
		filters={"planning_run": run_name, "status": "Open"},
		fields=["name", "severity", "exception_type", "source_name", "source_doctype", "is_blocking"],
	)
	exceptions_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for row in open_exceptions:
		if row.source_name:
			exceptions_by_source[row.source_name].append(row)

	result_names = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		pluck="name",
		order_by="creation asc",
	)
	result_docs = [frappe.get_doc("APS Schedule Result", result_name) for result_name in result_names]
	source_progress_by_result = _get_run_source_progress(result_docs)
	result_summaries = []
	for result_doc in result_docs:
		result_summaries.append(
			_recalculate_result(
				result_doc,
				exceptions_by_source=exceptions_by_source,
				source_progress=source_progress_by_result.get(result_doc.name),
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
			"consistency_details": _(
				"Recalculation in progress: {0}", context="Injection APS"
			).format(
				reason or _("plan mutation", context="Injection APS")
			),
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
	result_rows = frappe.get_all(
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
			"company",
			"customer",
			"sales_order",
			"sales_order_item",
			"item_code",
			"requested_date",
			"demand_source",
			"demand_source_snapshot_json",
			"fulfillment_baseline_json",
		],
		order_by="creation asc",
	)
	lineage_errors_by_result = _get_customer_schedule_lineage_errors(result_rows)
	for row in result_rows:
		errors.extend(lineage_errors_by_result.get(row.name) or [])
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
				"parent",
				"production_campaign",
				"capacity_owner",
				"co_product_item_code",
			],
		)
		for segment in segments:
			family_error = _family_co_product_allocation_error(segment)
			if family_error:
				errors.append(_error("family_co_product_unallocated", row.name, family_error, segment=segment.name))
			if _is_active_machine_segment(segment):
				for structural_error in _segment_structure_errors(segment):
					errors.append(
						_error("invalid_segment", row.name, structural_error, segment=segment.name)
					)
		effective_segments = [segment for segment in segments if is_effective_quantity_segment(segment)]
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


def audit_run_quantity_consistency(run_name: str) -> dict[str, Any]:
	"""Read-only Phase 6 quantity audit for one APS Planning Run.

	The normal consistency gate may recalculate and persist canonical fields.
	This audit deliberately avoids writes so it can be used as an independent
	confirmation after import, scheduling, release, execution and delivery sync.
	"""
	run_row = frappe.db.get_value(
		"APS Planning Run",
		run_name,
		[
			"status",
			"existing_work_order_policy",
			"total_net_requirement_qty",
			"total_machine_scheduled_qty",
			"total_demand_covered_qty",
			"total_overproduction_qty",
			"total_scheduled_qty",
			"total_unscheduled_qty",
			"total_produced_qty",
			"total_scrap_qty",
			"total_delivered_qty",
			"result_count",
		],
		as_dict=True,
	)
	if not run_row:
		frappe.throw(_("APS Planning Run {0} was not found.").format(run_name))

	differences = _AuditDifferenceCollector()
	result_rows = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
			"name",
			"planning_run",
			"net_requirement",
			"company",
			"customer",
			"sales_order",
			"sales_order_item",
			"item_code",
			"planned_qty",
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"scheduled_qty",
			"unscheduled_qty",
			"produced_qty",
			"good_produced_qty",
			"scrap_qty",
			"delivered_qty",
			"risk_status",
			"schedule_delay_minutes",
			"requested_date",
			"demand_source",
			"demand_source_snapshot_json",
			"fulfillment_baseline_json",
		],
		order_by="creation asc",
	)
	result_names = [row.name for row in result_rows]
	segments_by_result = _get_audit_segments(result_names)
	authoritative_plan = _get_audit_authoritative_plan(
		result_rows,
		run_existing_work_order_policy=run_row.get("existing_work_order_policy"),
		differences=differences,
	)
	production = _get_audit_production_totals(run_name, result_names=result_names)
	delivery = _get_audit_delivery_totals(result_rows)
	expected_delivery_by_result = _get_audit_expected_delivery_by_result(
		result_rows,
		delivery=delivery,
		differences=differences,
		audited_run=run_name,
	)
	result_summaries = []

	for result in result_rows:
		segments = segments_by_result.get(result.name) or []
		effective_segments = [segment for segment in segments if is_effective_quantity_segment(segment)]
		machine_scheduled_qty = sum(flt(segment.planned_qty) for segment in effective_segments)
		authoritative_planned_qty = flt(authoritative_plan.get(result.name, result.planned_qty))
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result.name,
			fieldname="planned_qty",
			expected=authoritative_planned_qty,
			actual=result.planned_qty,
			source=result.get("net_requirement") or "missing_net_requirement",
		)
		expected = calculate_quantity_fields(authoritative_planned_qty, machine_scheduled_qty)
		for fieldname in (
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"unscheduled_qty",
		):
			_compare_audit_qty(
				differences,
				doctype="APS Schedule Result",
				name=result.name,
				fieldname=fieldname,
				expected=expected[fieldname],
				actual=result.get(fieldname),
			)
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result.name,
			fieldname="scheduled_qty",
			expected=machine_scheduled_qty,
			actual=result.scheduled_qty,
			source="effective_schedule_segments",
		)

		produced = production["by_result"].get(result.name) or {}
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result.name,
			fieldname="produced_qty",
			expected=produced.get("good_qty", 0),
			actual=result.produced_qty,
			source="effective_production_allocations",
		)
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result.name,
			fieldname="good_produced_qty",
			expected=produced.get("good_qty", 0),
			actual=result.good_produced_qty,
			source="effective_production_allocations",
		)
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result.name,
			fieldname="scrap_qty",
			expected=produced.get("scrap_qty", 0),
			actual=result.scrap_qty,
			source="effective_production_allocations",
		)
		expected_delivered = flt(expected_delivery_by_result.get(result.name))
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result.name,
			fieldname="delivered_qty",
			expected=expected_delivered,
			actual=result.delivered_qty,
			source="effective_delivery_allocations",
		)

		due_datetime = _due_datetime(result.name, result.requested_date)
		expected_delay = 0.0
		for segment in effective_segments:
			segment_delay = 0.0
			if segment.end_time and get_datetime(segment.end_time) > due_datetime:
				segment_delay = (get_datetime(segment.end_time) - due_datetime).total_seconds() / 60
			expected_delay = max(expected_delay, segment_delay)
			_compare_audit_qty(
				differences,
				doctype="APS Schedule Segment",
				name=segment.name,
				fieldname="schedule_delay_minutes",
				expected=segment_delay,
				actual=segment.schedule_delay_minutes,
				source="segment_end_vs_due_datetime",
			)
			if segment_delay > QTY_TOLERANCE and segment.risk_status not in ("Critical", "Blocked"):
				differences.append(
					_audit_difference(
						doctype="APS Schedule Segment",
						name=segment.name,
						fieldname="risk_status",
						expected="Critical or Blocked",
						actual=segment.risk_status or "",
						source="segment_end_vs_due_datetime",
					)
				)
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result.name,
			fieldname="schedule_delay_minutes",
			expected=expected_delay,
			actual=result.schedule_delay_minutes,
			source="latest_effective_segment_end_vs_due_datetime",
		)
		if expected_delay > QTY_TOLERANCE and result.risk_status not in ("Critical", "Blocked"):
			differences.append(
				_audit_difference(
					doctype="APS Schedule Result",
					name=result.name,
					fieldname="risk_status",
					expected="Critical or Blocked",
					actual=result.risk_status or "",
					source="latest_effective_segment_end_vs_due_datetime",
				)
			)

		for segment in segments:
			segment_production = production["by_segment"].get(segment.name) or {}
			_compare_audit_qty(
				differences,
				doctype="APS Schedule Segment",
				name=segment.name,
				fieldname="actual_good_qty",
				expected=segment_production.get("good_qty", 0),
				actual=segment.actual_good_qty,
				source="effective_production_allocations",
			)
			_compare_audit_qty(
				differences,
				doctype="APS Schedule Segment",
				name=segment.name,
				fieldname="actual_scrap_qty",
				expected=segment_production.get("scrap_qty", 0),
				actual=segment.actual_scrap_qty,
				source="effective_production_allocations",
			)
			_compare_audit_qty(
				differences,
				doctype="APS Schedule Segment",
				name=segment.name,
				fieldname="actual_completed_qty",
				expected=flt(segment.actual_good_qty) + flt(segment.actual_scrap_qty),
				actual=segment.actual_completed_qty,
				source="segment_actual_good_plus_scrap",
			)

		result_summaries.append(
			{
				**expected,
				"produced_qty": produced.get("good_qty", 0),
				"scrap_qty": produced.get("scrap_qty", 0),
				"delivered_qty": expected_delivered,
			}
		)

	differences.merge(production["invalid_sources"])
	differences.merge(delivery["invalid_sources"])

	expected_totals = _sum_run_totals(result_summaries)
	expected_totals["scrap_qty"] = sum(flt(row.get("scrap_qty")) for row in result_summaries)
	run_field_map = {
		"total_net_requirement_qty": "planned_qty",
		"total_machine_scheduled_qty": "machine_scheduled_qty",
		"total_demand_covered_qty": "demand_covered_qty",
		"total_overproduction_qty": "overproduction_qty",
		"total_scheduled_qty": "machine_scheduled_qty",
		"total_unscheduled_qty": "unscheduled_qty",
		"total_produced_qty": "produced_qty",
		"total_scrap_qty": "scrap_qty",
		"total_delivered_qty": "delivered_qty",
	}
	for run_field, total_field in run_field_map.items():
		_compare_audit_qty(
			differences,
			doctype="APS Planning Run",
			name=run_name,
			fieldname=run_field,
			expected=expected_totals[total_field],
			actual=run_row.get(run_field),
			source="schedule_result_summary",
		)
	if cint(run_row.result_count) != len(result_summaries):
		differences.append(
			_audit_difference(
				doctype="APS Planning Run",
				name=run_name,
				fieldname="result_count",
				expected=len(result_summaries),
				actual=cint(run_row.result_count),
				source="schedule_result_summary",
			)
		)

	return {
		"run": run_name,
		"valid": differences.total_count == 0,
		"difference_count": differences.total_count,
		"differences": list(differences),
		"differences_truncated": differences.total_count > len(differences),
		"difference_limit": differences.limit,
		"totals": expected_totals,
		"result_count": len(result_summaries),
		"production_source_count": production["source_count"],
		"delivery_source_count": delivery["source_count"],
	}


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


def _recalculate_result(
	result_doc,
	exceptions_by_source: dict[str, list[dict[str, Any]]],
	source_progress: dict[str, float] | None = None,
) -> dict[str, Any]:
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
		family_error = _family_co_product_allocation_error(segment)
		if family_error:
			segment_errors.append(family_error)
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
		if is_effective_quantity_segment(segment):
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
	source_progress = source_progress or _get_source_progress(result_doc)
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


def _get_run_source_progress(result_docs) -> dict[str, dict[str, float]]:
	progress = {
		row.name: {"produced_qty": 0.0, "delivered_qty": 0.0}
		for row in result_docs
	}
	targets_by_result = {}
	if result_docs and frappe.db.exists("DocType", "Customer Delivery Schedule"):
		from injection_aps.services.availability import _get_result_schedule_targets

		targets_by_result = _get_result_schedule_targets(result_docs)
		progress.update(_progress_from_claimed_schedule_targets(result_docs, targets_by_result))

	# Backlog fulfillment is tied to one exact Sales Order Item.  Only deliveries
	# posted after the net-requirement baseline may satisfy this result; otherwise
	# old framework-order history would be deducted from newly planned demand.
	backlog_groups = defaultdict(list)
	opening_delivered_by_result = {}
	for result_doc in result_docs:
		if (result_doc.get("demand_source") or "") != "Sales Order Backlog":
			continue
		if targets_by_result.get(result_doc.name):
			continue
		sales_order = result_doc.get("sales_order")
		sales_order_item = result_doc.get("sales_order_item")
		opening_delivered_qty = _get_result_sales_order_item_opening_delivered_qty(result_doc)
		# Legacy/ambiguous backlog results have no trustworthy historical offset.
		# Remain conservative instead of borrowing another Sales Order's delivery.
		if not sales_order or not sales_order_item or opening_delivered_qty is None:
			continue
		key = (
			result_doc.get("company"),
			result_doc.get("customer") or "",
			result_doc.get("item_code"),
			sales_order,
			sales_order_item,
		)
		backlog_groups[key].append(result_doc)
		opening_delivered_by_result[result_doc.name] = opening_delivered_qty
	if backlog_groups and frappe.db.exists("DocType", "Sales Order"):
		for key, rows in backlog_groups.items():
			physical_delivered = _get_sales_order_delivered_total(*key)
			for result_doc, claimed in _claim_backlog_delivered_qty(
				rows,
				physical_delivered,
				opening_delivered_by_result=opening_delivered_by_result,
			):
				progress[result_doc.name]["delivered_qty"] = claimed
	return progress


def _sync_demand_lineage_exceptions(run_name: str) -> None:
	result_rows = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
			"name",
			"planning_run",
			"company",
			"customer",
			"sales_order",
			"sales_order_item",
			"item_code",
			"requested_date",
			"demand_source",
			"demand_source_snapshot_json",
			"fulfillment_baseline_json",
		],
	)
	lineage_errors_by_result = _get_customer_schedule_lineage_errors(result_rows)
	for row in result_rows:
		lineage_errors = lineage_errors_by_result.get(row.name) or []
		if not lineage_errors:
			continue
		_ensure_managed_exception(
			planning_run=run_name,
			severity="Blocking",
			exception_type="Demand Lineage Changed",
			message=_(
				"Customer demand lineage for APS result {0} changed after planning; release is blocked."
			).format(row.name),
			item_code=row.get("item_code"),
			customer=row.get("customer"),
			source_doctype="APS Schedule Result",
			source_name=row.name,
			resolution_hint=_(
				"Recalculate the planning run from the active customer schedule or process the change through Change Impact."
			),
			is_blocking=1,
			diagnostic={"errors": lineage_errors},
		)


def _get_customer_schedule_lineage_errors(
	result_rows,
	*,
	target_rows=None,
) -> dict[str, list[dict[str, Any]]]:
	"""Validate frozen schedule targets against the current active demand identity."""
	baselines = {}
	target_names = set()
	for result in result_rows or []:
		if not _result_has_customer_schedule_source(result):
			continue
		baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
		baselines[result.name] = baseline
		if baseline:
			target_names.update(
				row.get("customer_schedule_item")
				for row in baseline.get("targets") or []
				if isinstance(row, dict) and row.get("customer_schedule_item") and not cint(row.get("retired"))
			)
	if not baselines:
		return {}
	if target_rows is None:
		target_rows = (
			frappe.db.sql(
				"""
				select
					i.name, i.parent, i.item_code, i.sales_order, i.schedule_date,
					i.qty, i.delivered_qty, i.status as item_status,
					s.company, ifnull(s.customer, '') as customer,
					s.status as schedule_status
				from `tabCustomer Delivery Schedule Item` i
				inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
				where i.name in %(target_names)s
				""",
				{"target_names": tuple(sorted(target_names))},
				as_dict=True,
			)
			if target_names
			else []
		)
	current_by_name = {row.get("name"): row for row in target_rows or [] if row.get("name")}
	errors_by_result = defaultdict(list)
	for result in result_rows or []:
		if result.name not in baselines:
			continue
		if result.get("planned_qty") is not None and flt(result.get("planned_qty")) <= QTY_TOLERANCE:
			continue
		baseline = baselines.get(result.name)
		baseline_targets = baseline.get("targets") if isinstance(baseline, dict) else None
		reasons = []
		if not isinstance(baseline_targets, list) or not baseline_targets:
			reasons.append(_("No frozen customer schedule target is available."))
		else:
			for frozen in baseline_targets:
				if not isinstance(frozen, dict):
					reasons.append(_("The frozen customer schedule target is invalid."))
					continue
				target_name = frozen.get("customer_schedule_item")
				if cint(frozen.get("retired")):
					reasons.append(_("Customer schedule target {0} was cancelled or retired.").format(target_name or "-"))
					continue
				current = current_by_name.get(target_name)
				if not current:
					reasons.append(_("Customer schedule target {0} is no longer available.").format(target_name or "-"))
					continue
				if (current.get("schedule_status") or "") != "Active" or (current.get("item_status") or "") == "Cancelled":
					reasons.append(_("Customer schedule target {0} is no longer active.").format(target_name))
					continue
				identity_mismatches = []
				for fieldname in ("company", "customer", "sales_order", "item_code"):
					if (current.get(fieldname) or "") != (result.get(fieldname) or ""):
						identity_mismatches.append(fieldname)
				effective_schedule_date = _get_audit_target_effective_schedule_date(frozen)
				if (
					not effective_schedule_date
					or getdate(current.get("schedule_date")) != getdate(effective_schedule_date)
					or getdate(result.get("requested_date")) != getdate(effective_schedule_date)
				):
					identity_mismatches.append("schedule_date")
				if identity_mismatches:
					reasons.append(
						_("Customer schedule target {0} no longer matches result fields: {1}.").format(
							target_name,
							", ".join(identity_mismatches),
						)
					)
				required_qty = (
					frozen.get("accepted_required_qty")
					if frozen.get("accepted_required_qty") not in (None, "")
					else frozen.get("opening_required_qty")
				)
				if abs(flt(current.get("qty")) - flt(required_qty)) > QTY_TOLERANCE:
					reasons.append(_("Customer schedule target {0} quantity changed after planning.").format(target_name))
				if (
					current.get("delivered_qty") not in (None, "")
					and flt(current.get("delivered_qty")) + QTY_TOLERANCE
					< flt(frozen.get("opening_delivered_qty"))
				):
					reasons.append(
						_(
							"Customer schedule target {0} delivery was returned below its frozen opening quantity; rebuild the complete Planning Run."
						).format(target_name)
					)
		for reason in dict.fromkeys(reasons):
			errors_by_result[result.name].append(
				_error("demand_lineage_changed", result.name, reason)
			)
	return dict(errors_by_result)


def _result_has_customer_schedule_source(result) -> bool:
	if (result.get("demand_source") or "") == "Customer Delivery Schedule":
		return True
	value = result.get("demand_source_snapshot_json")
	if not value:
		return False
	try:
		source_rows = value if isinstance(value, list) else json.loads(value)
	except (TypeError, ValueError):
		return False
	return any(
		isinstance(row, dict) and row.get("source_doctype") == "Customer Delivery Schedule"
		for row in source_rows or []
	)


def _progress_from_claimed_schedule_targets(result_docs, targets_by_result):
	progress = {}
	for result_doc in result_docs:
		produced_qty = 0.0
		delivered_qty = 0.0
		for target in targets_by_result.get(result_doc.name) or []:
			if target.get("accepted_source_open_qty") not in (None, ""):
				attributed_qty = max(flt(target.get("accepted_source_open_qty")), 0)
			elif target.get("attributed_qty") not in (None, ""):
				attributed_qty = max(flt(target.get("attributed_qty")), 0)
			else:
				attributed_qty = max(flt(target.get("qty")), 0)
			produced_qty += min(
				max(flt(target.get("produced_qty")) - flt(target.get("opening_produced_qty")), 0),
				attributed_qty,
			)
			delivered_qty += min(
				max(flt(target.get("delivered_qty")) - flt(target.get("opening_delivered_qty")), 0),
				attributed_qty,
			)
		progress[result_doc.name] = {
			"produced_qty": produced_qty,
			"delivered_qty": delivered_qty,
		}
	return progress


def _get_sales_order_delivered_total(
	company,
	customer,
	item_code,
	sales_order,
	sales_order_item,
) -> float:
	row = frappe.db.sql(
		"""
		select coalesce(sum(i.delivered_qty), 0) as delivered_qty
		from `tabSales Order Item` i
		inner join `tabSales Order` s on s.name = i.parent
		where s.company = %(company)s
			and s.docstatus = 1
			and ifnull(s.customer, '') = %(customer)s
			and s.name = %(sales_order)s
			and i.name = %(sales_order_item)s
			and i.item_code = %(item_code)s
		""",
		{
			"company": company,
			"customer": customer,
			"item_code": item_code,
			"sales_order": sales_order,
			"sales_order_item": sales_order_item,
		},
		as_dict=True,
	)[0]
	return flt(row.delivered_qty)


def _claim_backlog_delivered_qty(
	result_docs,
	physical_delivered,
	*,
	opening_delivered_by_result: dict[str, float] | None = None,
):
	physical_delivered = max(flt(physical_delivered), 0)
	opening_delivered_by_result = opening_delivered_by_result or {}
	claimed_through = 0.0
	claims = []
	for result_doc in result_docs:
		claim_start = max(
			claimed_through,
			max(flt(opening_delivered_by_result.get(result_doc.name)), 0),
		)
		claimed = min(
			max(physical_delivered - claim_start, 0),
			max(flt(result_doc.get("planned_qty")), 0),
		)
		claims.append((result_doc, claimed))
		claimed_through = claim_start + claimed
	return claims


def _get_result_sales_order_item_opening_delivered_qty(result_doc) -> float | None:
	baseline = _parse_fulfillment_baseline(result_doc.get("fulfillment_baseline_json"))
	if baseline is None:
		return None
	sales_order = result_doc.get("sales_order") or ""
	sales_order_item = result_doc.get("sales_order_item") or ""
	item_code = result_doc.get("item_code") or ""
	matches = [
		row
		for row in baseline.get("sales_order_items") or []
		if isinstance(row, dict)
		and (row.get("sales_order") or "") == sales_order
		and (row.get("sales_order_item") or "") == sales_order_item
		and (row.get("item_code") or "") == item_code
	]
	if len(matches) != 1:
		return None
	return max(flt(matches[0].get("opening_delivered_qty")), 0)


def _parse_fulfillment_baseline(value) -> dict[str, Any] | None:
	if value in (None, ""):
		return None
	if isinstance(value, dict):
		return value
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError):
		return None
	return parsed if isinstance(parsed, dict) else None


def _get_source_progress(result_doc) -> dict[str, float]:
	return _get_run_source_progress([result_doc]).get(
		result_doc.name,
		{"produced_qty": 0.0, "delivered_qty": 0.0},
	)


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
	name = _value(segment, "name") or _("new segment", context="Injection APS")
	if not _value(segment, "workstation"):
		errors.append(_("Segment {0} has no workstation.").format(name))
	start_time = _value(segment, "start_time")
	end_time = _value(segment, "end_time")
	if not start_time or not end_time:
		errors.append(_("Segment {0} must have both start and end time.").format(name))
	elif get_datetime(end_time) <= get_datetime(start_time):
		errors.append(_("Segment {0} end time must be later than start time.").format(name))
	return errors


def _family_co_product_allocation_error(segment: dict[str, Any] | Any) -> str | None:
	"""Allow only an exact V2 Campaign output ledger; legacy family credit fails closed."""
	if (
		(_value(segment, "segment_kind") or "Primary") == "Family Co-Product"
		and (_value(segment, "segment_status") or "Planned") not in INACTIVE_SEGMENT_STATUSES
		and flt(_value(segment, "planned_qty")) > 0
	):
		campaign = _value(segment, "production_campaign")
		owner = _value(segment, "capacity_owner")
		result = _value(segment, "parent")
		item_code = _value(segment, "co_product_item_code")
		if campaign and owner and result and item_code:
			campaign_owner = frappe.db.get_value("APS Production Campaign", campaign, "capacity_owner_segment")
			output = frappe.db.get_value(
				"APS Campaign Output",
				{"parent": campaign, "parenttype": "APS Production Campaign", "schedule_result": result, "item_code": item_code},
				["name", "planned_qty"],
				as_dict=True,
			)
			owner_campaign = frappe.db.get_value("APS Schedule Segment", owner, "production_campaign")
			if (
				campaign_owner == owner
				and owner_campaign == campaign
				and output
				and abs(flt(output.planned_qty) - flt(_value(segment, "planned_qty"))) <= QTY_TOLERANCE
			):
				return None
		return _(
			"Family co-product Segment {0} has no exact V2 Campaign output and capacity-owner lineage. "
			"Recalculate the plan; legacy automatic family credit cannot be released.",
			context="Injection APS",
		).format(_value(segment, "name") or _("new segment", context="Injection APS"))
	return None


def _is_active_machine_segment(segment: dict[str, Any] | Any) -> bool:
	return (
		(_value(segment, "segment_kind") or "Primary") in EFFECTIVE_SEGMENT_KINDS
		and (_value(segment, "segment_status") or "Planned") not in INACTIVE_SEGMENT_STATUSES
		and flt(_value(segment, "planned_qty")) > 0
	)


def _due_datetime(result_name: str, requested_date=None):
	requested_date = requested_date or frappe.db.get_value("APS Schedule Result", result_name, "requested_date")
	date_value = getdate(requested_date)
	return get_datetime(add_days(date_value, 1))


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


def _get_audit_segments(result_names: list[str]) -> dict[str, list[Any]]:
	if not result_names:
		return {}
	rows = frappe.get_all(
		"APS Schedule Segment",
		filters={
			"parent": ("in", tuple(result_names)),
			"parenttype": "APS Schedule Result",
		},
		fields=[
			"name",
			"parent",
			"workstation",
			"start_time",
			"end_time",
			"planned_qty",
			"segment_kind",
			"segment_status",
			"production_campaign",
			"capacity_owner",
			"co_product_item_code",
			"risk_status",
			"schedule_delay_minutes",
			"actual_completed_qty",
			"actual_good_qty",
			"actual_scrap_qty",
			"idx",
		],
		order_by="parent asc, idx asc",
	)
	by_result: dict[str, list[Any]] = defaultdict(list)
	for row in rows:
		by_result[row.parent].append(row)
	return dict(by_result)


def _get_audit_authoritative_plan(
	result_rows,
	*,
	run_existing_work_order_policy: str | None,
	differences: _AuditDifferenceCollector,
) -> dict[str, float]:
	"""Return the live Net Requirement production boundary for every Result."""
	names = sorted({row.get("net_requirement") for row in result_rows if row.get("net_requirement")})
	net_rows = (
		frappe.get_all(
			"APS Net Requirement",
			filters={"name": ("in", tuple(names))},
			fields=[
				"name",
				"company",
				"customer",
				"sales_order",
				"sales_order_item",
				"item_code",
				"demand_date",
				"demand_qty",
				"available_stock_qty",
				"planning_qty",
				"net_requirement_qty",
				"open_work_order_qty",
				"safety_stock_gap_qty",
				"minimum_batch_qty",
				"existing_work_order_policy",
				"demand_source_snapshot_json",
				"fulfillment_baseline_json",
				"is_system_generated",
			],
		)
		if names
		else []
	)
	by_name = {row.name: row for row in net_rows}
	results_by_net: dict[str, list[Any]] = defaultdict(list)
	for result in result_rows:
		if result.get("net_requirement"):
			results_by_net[result.net_requirement].append(result)

	result = {}
	formula_evidence_by_result: dict[str, tuple[Any, Any]] = {}
	for row in result_rows:
		net_name = row.get("net_requirement")
		net = by_name.get(net_name)
		frozen_net = _get_audit_frozen_net_requirement(row, differences=differences)
		if not net:
			# A normal Net Requirement rebuild intentionally clears old Result links.
			# A dangling non-empty link is corruption; a cleared link is acceptable only
			# when the Result carries complete v4 frozen formula evidence.
			if net_name:
				differences.append(
					_audit_difference(
						doctype="APS Schedule Result",
						name=row.name,
						fieldname="net_requirement",
						expected="existing APS Net Requirement or an intentionally cleared link",
						actual=net_name,
						source="missing_live_net_requirement",
					)
				)
			if frozen_net:
				_validate_audit_net_requirement(
					frozen_net,
					run_existing_work_order_policy=run_existing_work_order_policy,
					differences=differences,
					doctype="APS Schedule Result",
					name=row.name,
					require_system_generated=False,
					allow_run_level_safety_coverage=True,
				)
				formula_evidence_by_result[row.name] = (row, frozen_net)
				result[row.name] = _get_audit_net_requirement_boundary(frozen_net)
			else:
				result[row.name] = flt(row.get("planned_qty"))
			continue

		if len(results_by_net[net_name]) != 1:
			differences.append(
				_audit_difference(
					doctype="APS Net Requirement",
					name=net_name,
					fieldname="schedule_result_claim_count",
					expected=1,
					actual=len(results_by_net[net_name]),
					source=", ".join(item.name for item in results_by_net[net_name]),
				)
			)

		for result_field, net_field in (
			("company", "company"),
			("customer", "customer"),
			("sales_order", "sales_order"),
			("sales_order_item", "sales_order_item"),
			("item_code", "item_code"),
		):
			if (row.get(result_field) or "") == (net.get(net_field) or ""):
				continue
			differences.append(
				_audit_difference(
					doctype="APS Schedule Result",
					name=row.name,
					fieldname=result_field,
					expected=net.get(net_field) or "",
					actual=row.get(result_field) or "",
					source=net_name,
				)
			)
		if getdate(row.get("requested_date")) != getdate(net.get("demand_date")):
			differences.append(
				_audit_difference(
					doctype="APS Schedule Result",
					name=row.name,
					fieldname="requested_date",
					expected=str(getdate(net.get("demand_date"))),
					actual=str(getdate(row.get("requested_date"))),
					source=net_name,
				)
			)
		if run_existing_work_order_policy and net.get("existing_work_order_policy") != run_existing_work_order_policy:
			differences.append(
				_audit_difference(
					doctype="APS Net Requirement",
					name=net_name,
					fieldname="existing_work_order_policy",
					expected=run_existing_work_order_policy,
					actual=net.get("existing_work_order_policy"),
					source=row.name,
				)
			)
		_validate_audit_net_requirement(
			net,
			run_existing_work_order_policy=run_existing_work_order_policy,
			differences=differences,
			allow_run_level_safety_coverage=True,
		)
		if frozen_net:
			_validate_audit_net_requirement(
				frozen_net,
				run_existing_work_order_policy=run_existing_work_order_policy,
				differences=differences,
				doctype="APS Schedule Result",
				name=row.name,
				require_system_generated=False,
				allow_run_level_safety_coverage=True,
			)
			_compare_audit_net_requirement_evidence(
				result_row=row,
				live_net=net,
				frozen_net=frozen_net,
				differences=differences,
			)
		# The run schedules one total boundary: expanded residual production or
		# exact existing-WO coverage plus the unfulfilled residual, whichever is larger.
		authoritative_evidence = frozen_net or net
		formula_evidence_by_result[row.name] = (row, authoritative_evidence)
		result[row.name] = _get_audit_net_requirement_boundary(authoritative_evidence)
	_validate_audit_minimum_batch_surplus_conservation(
		formula_evidence_by_result,
		differences=differences,
	)
	return result


def _get_audit_net_requirement_boundary(net) -> float:
	return max(
		flt(net.get("planning_qty") or net.get("net_requirement_qty")),
		flt(net.get("open_work_order_qty")) + flt(net.get("net_requirement_qty")),
		0,
	)


def _get_audit_frozen_net_requirement(
	result_row,
	*,
	differences: _AuditDifferenceCollector,
):
	"""Load complete v4 formula evidence from a Result without guessing live state."""
	baseline = _parse_fulfillment_baseline(result_row.get("fulfillment_baseline_json"))
	evidence = baseline.get("net_requirement") if isinstance(baseline, dict) else None
	required_fields = (
		"formula_version",
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"existing_work_order_policy",
		"safety_stock_gap_qty",
		"minimum_batch_qty",
		"minimum_batch_coverage_qty",
		"base_residual_qty",
		"net_requirement_qty",
		"planning_qty",
		"new_batch_surplus_qty",
		"is_safety_stock_group",
	)
	missing = (
		[fieldname for fieldname in required_fields if not isinstance(evidence, dict) or fieldname not in evidence]
	)
	if not isinstance(baseline, dict) or cint(baseline.get("version")) < 4 or missing:
		differences.append(
			_audit_difference(
				doctype="APS Schedule Result",
				name=result_row.name,
				fieldname="fulfillment_baseline_json",
				expected="v4 frozen net-requirement formula evidence: {0}".format(", ".join(required_fields)),
				actual=(
					"Missing or invalid"
					if not isinstance(baseline, dict)
					else "version {0}; missing {1}".format(
						baseline.get("version") or 0,
						", ".join(missing) or "none",
					)
				),
				source="frozen_net_requirement_evidence",
			)
		)
		return None
	return frappe._dict(
		{
			"name": result_row.name,
			**evidence,
			"demand_source_snapshot_json": result_row.get("demand_source_snapshot_json"),
			"fulfillment_baseline_json": baseline,
			"is_system_generated": 1,
		}
	)


def _compare_audit_net_requirement_evidence(
	*,
	result_row,
	live_net,
	frozen_net,
	differences: _AuditDifferenceCollector,
) -> None:
	quantity_fields = (
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"safety_stock_gap_qty",
		"minimum_batch_qty",
		"net_requirement_qty",
		"planning_qty",
	)
	for fieldname in quantity_fields:
		_compare_audit_qty(
			differences,
			doctype="APS Schedule Result",
			name=result_row.name,
			fieldname=f"frozen_{fieldname}",
			expected=frozen_net.get(fieldname),
			actual=live_net.get(fieldname),
			source=live_net.name,
		)
	if (frozen_net.get("existing_work_order_policy") or "") != (
		live_net.get("existing_work_order_policy") or ""
	):
		differences.append(
			_audit_difference(
				doctype="APS Schedule Result",
				name=result_row.name,
				fieldname="frozen_existing_work_order_policy",
				expected=frozen_net.get("existing_work_order_policy") or "",
				actual=live_net.get("existing_work_order_policy") or "",
				source=live_net.name,
			)
		)
	live_sources = _parse_audit_demand_source_snapshot(live_net.get("demand_source_snapshot_json"))
	frozen_sources = _parse_audit_demand_source_snapshot(result_row.get("demand_source_snapshot_json"))
	if live_sources != frozen_sources:
		differences.append(
			_audit_difference(
				doctype="APS Schedule Result",
				name=result_row.name,
				fieldname="demand_source_snapshot_json",
				expected="exact frozen source snapshot",
				actual="live Net Requirement snapshot differs",
				source=live_net.name,
			)
		)


def _validate_audit_net_requirement(
	net,
	*,
	run_existing_work_order_policy: str | None,
	differences: _AuditDifferenceCollector,
	doctype: str = "APS Net Requirement",
	name: str | None = None,
	require_system_generated: bool = True,
	allow_run_level_safety_coverage: bool = False,
) -> None:
	"""Fail closed unless live quantities and v4 frozen evidence independently agree."""
	name = name or net.name

	def issue(fieldname, expected, actual, *, source="net_requirement_formula"):
		differences.append(
			_audit_difference(
				doctype=doctype,
				name=name,
				fieldname=fieldname,
				expected=expected,
				actual=actual,
				source=source,
			)
		)

	if require_system_generated and not cint(net.get("is_system_generated")):
		issue("is_system_generated", 1, net.get("is_system_generated"), source="frozen_net_requirement_evidence")

	qty_fields = (
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"safety_stock_gap_qty",
		"minimum_batch_qty",
		"net_requirement_qty",
		"planning_qty",
	)
	for fieldname in qty_fields:
		if net.get(fieldname) in (None, ""):
			issue(fieldname, "persisted non-negative quantity", "Missing", source="frozen_net_requirement_evidence")
		elif flt(net.get(fieldname)) < -QTY_TOLERANCE:
			issue(fieldname, ">= 0", net.get(fieldname))

	demand_qty = max(flt(net.get("demand_qty")), 0)
	available_stock_qty = max(flt(net.get("available_stock_qty")), 0)
	open_work_order_qty = max(flt(net.get("open_work_order_qty")), 0)
	safety_gap_qty = max(flt(net.get("safety_stock_gap_qty")), 0)
	minimum_batch_qty = max(flt(net.get("minimum_batch_qty")), 0)
	net_requirement_qty = max(flt(net.get("net_requirement_qty")), 0)
	planning_qty = max(flt(net.get("planning_qty")), 0)

	if available_stock_qty > demand_qty + QTY_TOLERANCE:
		issue("available_stock_qty", f"<= demand_qty ({demand_qty:g})", available_stock_qty)
	if available_stock_qty + open_work_order_qty > demand_qty + QTY_TOLERANCE:
		issue(
			"open_work_order_qty",
			f"<= uncovered demand ({max(demand_qty - available_stock_qty, 0):g})",
			open_work_order_qty,
		)
	policy = net.get("existing_work_order_policy") or ""
	if policy not in AUDIT_EXISTING_WORK_ORDER_POLICIES:
		issue(
			"existing_work_order_policy",
			"Include or Exclude",
			policy or "Missing",
			source="frozen_net_requirement_evidence",
		)
	if run_existing_work_order_policy and policy != run_existing_work_order_policy:
		# The caller also reports the Result/Run lineage mismatch.  Keep this
		# formula-level difference so a standalone helper invocation fails closed.
		issue("existing_work_order_policy", run_existing_work_order_policy, policy)
	if policy == "Exclude" and open_work_order_qty > QTY_TOLERANCE:
		issue("open_work_order_qty", 0, open_work_order_qty, source="existing_work_order_policy")

	base_residual = max(
		demand_qty - available_stock_qty - open_work_order_qty + safety_gap_qty,
		0,
	)
	if net_requirement_qty > base_residual + QTY_TOLERANCE:
		issue("net_requirement_qty", f"<= base residual ({base_residual:g})", net_requirement_qty)
	minimum_batch_coverage = max(base_residual - net_requirement_qty, 0)
	expected_planning_qty = (
		max(net_requirement_qty, minimum_batch_qty)
		if net_requirement_qty > QTY_TOLERANCE and minimum_batch_qty > QTY_TOLERANCE
		else net_requirement_qty
	)
	if abs(planning_qty - expected_planning_qty) > QTY_TOLERANCE:
		issue("planning_qty", expected_planning_qty, planning_qty)

	source_rows = _parse_audit_demand_source_snapshot(net.get("demand_source_snapshot_json"))
	if source_rows is None or not source_rows:
		issue(
			"demand_source_snapshot_json",
			"non-empty frozen demand source rows",
			"Missing or invalid",
			source="frozen_net_requirement_evidence",
		)
	else:
		valid_source_qty = 0.0
		seen_source_keys = set()
		source_doctypes = set()
		for index, row in enumerate(source_rows, start=1):
			if not isinstance(row, dict) or not row.get("source_doctype") or row.get("qty") in (None, ""):
				issue(
					"demand_source_snapshot_json",
					"rows with source_doctype and non-negative qty",
					f"invalid row {index}",
					source="frozen_net_requirement_evidence",
				)
				continue
			required_source_fields = ["demand_pool", "source_name"]
			if row.get("source_doctype") in ("Customer Delivery Schedule", "Sales Order"):
				required_source_fields.append("source_detail_name")
			if row.get("source_doctype") == "Sales Order":
				required_source_fields.extend(("sales_order", "sales_order_item"))
			missing_source_fields = [
				fieldname for fieldname in required_source_fields if row.get(fieldname) in (None, "")
			]
			if missing_source_fields:
				issue(
					"demand_source_snapshot_json",
					"source row with {0}".format(", ".join(required_source_fields)),
					"row {0} missing {1}".format(index, ", ".join(missing_source_fields)),
					source="frozen_net_requirement_evidence",
				)
			qty = flt(row.get("qty"))
			if qty < -QTY_TOLERANCE:
				issue("demand_source_snapshot_json", ">= 0 source qty", qty, source=f"source row {index}")
				continue
			valid_source_qty += max(qty, 0)
			key = (
				row.get("demand_pool") or "",
				row.get("source_doctype") or "",
				row.get("source_name") or "",
				row.get("source_detail_name") or "",
			)
			if key in seen_source_keys:
				issue(
					"demand_source_snapshot_json",
					"unique frozen demand source rows",
					f"duplicate row {index}",
					source="frozen_net_requirement_evidence",
				)
			seen_source_keys.add(key)
			source_doctypes.add(row.get("source_doctype"))
		if abs(valid_source_qty - demand_qty) > QTY_TOLERANCE:
			issue("demand_qty", valid_source_qty, demand_qty, source="frozen_demand_source_sum")

	baseline = _parse_fulfillment_baseline(net.get("fulfillment_baseline_json"))
	evidence = baseline.get("net_requirement") if isinstance(baseline, dict) else None
	required_evidence = (
		"formula_version",
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"existing_work_order_policy",
		"safety_stock_gap_qty",
		"minimum_batch_qty",
		"minimum_batch_coverage_qty",
		"base_residual_qty",
		"net_requirement_qty",
		"planning_qty",
		"new_batch_surplus_qty",
		"is_safety_stock_group",
	)
	missing_evidence = [
		fieldname for fieldname in required_evidence if not isinstance(evidence, dict) or fieldname not in evidence
	]
	if (
		not isinstance(baseline, dict)
		or cint(baseline.get("version")) < 4
		or not isinstance(evidence, dict)
		or missing_evidence
	):
		issue(
			"fulfillment_baseline_json",
			"complete v4 frozen net-requirement formula evidence",
			"Missing or incomplete: {0}".format(", ".join(missing_evidence) or "version"),
			source="frozen_net_requirement_evidence",
		)
		return
	if cint(evidence.get("formula_version")) != 1:
		issue("formula_version", 1, evidence.get("formula_version"), source="frozen_net_requirement_evidence")
	for fieldname, actual in (
		("demand_qty", demand_qty),
		("available_stock_qty", available_stock_qty),
		("open_work_order_qty", open_work_order_qty),
		("safety_stock_gap_qty", safety_gap_qty),
		("minimum_batch_qty", minimum_batch_qty),
		("net_requirement_qty", net_requirement_qty),
		("planning_qty", planning_qty),
	):
		if abs(flt(evidence.get(fieldname)) - actual) > QTY_TOLERANCE:
			issue(fieldname, evidence.get(fieldname), actual, source="frozen_net_requirement_evidence")
	if (evidence.get("existing_work_order_policy") or "") != policy:
		issue(
			"existing_work_order_policy",
			evidence.get("existing_work_order_policy") or "",
			policy,
			source="frozen_net_requirement_evidence",
		)
	if abs(flt(evidence.get("base_residual_qty")) - base_residual) > QTY_TOLERANCE:
		issue(
			"base_residual_qty",
			base_residual,
			evidence.get("base_residual_qty"),
			source="frozen_net_requirement_evidence",
		)
	if abs(flt(evidence.get("minimum_batch_coverage_qty")) - minimum_batch_coverage) > QTY_TOLERANCE:
		issue(
			"minimum_batch_coverage_qty",
			minimum_batch_coverage,
			evidence.get("minimum_batch_coverage_qty"),
			source="frozen_net_requirement_evidence",
		)
	is_safety_stock_group = cint(evidence.get("is_safety_stock_group"))
	if source_rows:
		source_is_safety_stock = bool(source_doctypes) and source_doctypes == {"Item"}
		if is_safety_stock_group != cint(source_is_safety_stock):
			issue(
				"is_safety_stock_group",
				cint(source_is_safety_stock),
				is_safety_stock_group,
				source="frozen_demand_source_type",
			)
	expected_new_batch_surplus = 0 if is_safety_stock_group else max(planning_qty - net_requirement_qty, 0)
	if abs(flt(evidence.get("new_batch_surplus_qty")) - expected_new_batch_surplus) > QTY_TOLERANCE:
		issue(
			"new_batch_surplus_qty",
			expected_new_batch_surplus,
			evidence.get("new_batch_surplus_qty"),
			source="frozen_net_requirement_evidence",
		)
	if (
		not is_safety_stock_group
		and minimum_batch_coverage > expected_new_batch_surplus + QTY_TOLERANCE
	):
		issue(
			"minimum_batch_coverage_qty",
			f"<= generated minimum-batch surplus ({expected_new_batch_surplus:g})",
			minimum_batch_coverage,
			source="minimum_batch_surplus_conservation",
		)
	if (
		is_safety_stock_group
		and minimum_batch_coverage > QTY_TOLERANCE
		and not allow_run_level_safety_coverage
	):
		issue(
			"minimum_batch_coverage_qty",
			"run-level donor surplus evidence",
			minimum_batch_coverage,
			source="minimum_batch_surplus_conservation",
		)


def _validate_audit_minimum_batch_surplus_conservation(
	formula_evidence_by_result: dict[str, tuple[Any, Any]],
	*,
	differences: _AuditDifferenceCollector,
) -> None:
	"""Conserve cross-demand minimum-batch surplus once per run/company/item."""
	by_key: dict[tuple[str, str], dict[str, Any]] = defaultdict(
		lambda: {
			"generated_surplus_qty": 0.0,
			"non_safety_coverage_qty": 0.0,
			"safety_coverage_qty": 0.0,
			"results": [],
		}
	)
	for result_name, (result_row, evidence) in formula_evidence_by_result.items():
		is_safety = cint(evidence.get("is_safety_stock_group"))
		coverage_qty = max(flt(evidence.get("minimum_batch_coverage_qty")), 0)
		new_surplus_qty = max(flt(evidence.get("new_batch_surplus_qty")), 0)
		if new_surplus_qty <= QTY_TOLERANCE and coverage_qty <= QTY_TOLERANCE:
			continue
		company = result_row.get("company") or evidence.get("company") or ""
		item_code = result_row.get("item_code") or evidence.get("item_code") or ""
		if not company or not item_code:
			differences.append(
				_audit_difference(
					doctype="APS Schedule Result",
					name=result_name,
					fieldname="minimum_batch_coverage_qty",
					expected="company and item_code for run-level surplus conservation",
					actual={"company": company, "item_code": item_code},
					source="minimum_batch_surplus_conservation",
				)
			)
			continue
		bucket = by_key[(company, item_code)]
		bucket["results"].append(result_name)
		if is_safety:
			bucket["safety_coverage_qty"] += coverage_qty
		else:
			# Surplus and its later consumers normally live on different Results.
			# Aggregate both sides before calculating what remains for Safety Stock;
			# subtracting coverage only from the same row would silently ignore a
			# consumer row whose own new surplus is zero.
			bucket["generated_surplus_qty"] += new_surplus_qty
			bucket["non_safety_coverage_qty"] += coverage_qty

	for (company, item_code), bucket in by_key.items():
		remaining_donor_qty = max(
			bucket["generated_surplus_qty"] - bucket["non_safety_coverage_qty"],
			0,
		)
		if bucket["safety_coverage_qty"] <= remaining_donor_qty + QTY_TOLERANCE:
			continue
		differences.append(
			_audit_difference(
				doctype="APS Planning Run",
				name=company,
				fieldname="minimum_batch_coverage_qty",
				expected="<= conserved donor surplus ({0:g})".format(remaining_donor_qty),
				actual=bucket["safety_coverage_qty"],
				source="minimum_batch_surplus_conservation:{0}:{1}".format(
					company,
					item_code,
				),
			)
		)


def _parse_audit_demand_source_snapshot(value) -> list[dict[str, Any]] | None:
	if isinstance(value, list):
		return value
	if value in (None, ""):
		return None
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError):
		return None
	return parsed if isinstance(parsed, list) else None


def _get_audit_schema_issues(
	requirements: dict[str, tuple[str, ...]],
	*,
	source: str,
) -> _AuditDifferenceCollector:
	issues = _AuditDifferenceCollector()
	for doctype, fieldnames in requirements.items():
		if not frappe.db.exists("DocType", doctype):
			issues.append(
				_audit_difference(
					doctype="DocType",
					name=doctype,
					fieldname="name",
					expected="installed audit source schema",
					actual="Missing",
					source=source,
				)
			)
			continue
		for fieldname in fieldnames:
			if frappe.db.has_column(doctype, fieldname):
				continue
			issues.append(
				_audit_difference(
					doctype="DocType",
					name=doctype,
					fieldname=fieldname,
					expected="installed database column",
					actual="Missing",
					source=source,
				)
			)
	return issues


def _get_audit_production_totals(run_name: str, *, result_names: list[str]) -> dict[str, Any]:
	issues = _get_audit_schema_issues(
		{
			"APS Production Allocation": (
				"planning_run",
				"schedule_result",
				"segment",
				"work_order",
				"work_order_scheduling",
				"scheduling_item",
				"allocation_method",
				"source_stock_entry",
				"source_stock_entry_detail",
				"source_docstatus",
				"output_type",
				"source_qty",
				"allocated_qty",
				"good_qty",
				"scrap_qty",
				"effective_qty",
				"is_effective",
			),
			"Stock Entry": (
				"docstatus",
				"purpose",
				"work_order",
				"work_order_scheduling",
				"custom_aps_scheduling_item",
				"custom_aps_segment_reference",
				"custom_aps_output_type",
			),
			"Stock Entry Detail": (
				"parent",
				"item_code",
				"qty",
				"transfer_qty",
				"is_finished_item",
				"t_warehouse",
			),
			"Work Order": (
				"production_item",
				"scrap_warehouse",
				"sales_order",
				"sales_order_item",
				"custom_aps_run",
				"custom_aps_result_reference",
			),
			"Scheduling Item": (
				"parent",
				"work_order",
				"custom_aps_run",
				"custom_aps_result_reference",
				"custom_aps_segment_reference",
			),
			"Work Order Scheduling": ("custom_aps_run", "custom_aps_approval_state"),
			"APS Schedule Result": (
				"planning_run",
				"item_code",
				"sales_order",
				"sales_order_item",
			),
			"APS Schedule Segment": (
				"parent",
				"linked_work_order",
				"linked_work_order_scheduling",
				"linked_scheduling_item",
			),
		},
		source="phase6_schema",
	)
	if issues.total_count:
		return {"by_result": {}, "by_segment": {}, "invalid_sources": issues, "source_count": 0}
	scrap_select = (
		"coalesce(sed.is_scrap_item, 0)"
		if frappe.db.has_column("Stock Entry Detail", "is_scrap_item")
		else "0"
	)
	rows = frappe.db.sql(
		"""
		select
			pa.name, pa.planning_run, pa.schedule_result, pa.segment, pa.work_order,
			pa.work_order_scheduling, pa.scheduling_item, pa.allocation_method,
			pa.source_stock_entry, pa.source_stock_entry_detail,
			pa.source_docstatus, pa.output_type, pa.source_qty,
			pa.allocated_qty, pa.good_qty, pa.scrap_qty, pa.effective_qty,
			se.name as live_stock_entry, se.docstatus as live_docstatus,
			se.purpose as live_purpose, se.work_order as live_work_order,
			se.work_order_scheduling as live_work_order_scheduling,
			se.custom_aps_scheduling_item as live_direct_scheduling_item,
			se.custom_aps_segment_reference as live_direct_segment,
			se.custom_aps_output_type as live_explicit_output_type,
			sed.name as live_detail, sed.parent as live_detail_parent,
			sed.item_code as live_item_code, sed.qty as live_document_qty,
			sed.transfer_qty as live_stock_qty, sed.is_finished_item as live_is_finished_item,
			{scrap_select} as live_is_scrap_item, sed.t_warehouse as live_target_warehouse,
			wo.name as live_work_order_name, wo.production_item as live_production_item,
			wo.scrap_warehouse as live_scrap_warehouse,
			wo.sales_order as live_work_order_sales_order,
			wo.sales_order_item as live_work_order_sales_order_item,
			wo.custom_aps_run as live_work_order_run,
			wo.custom_aps_result_reference as live_work_order_result,
			sr.name as live_result, sr.planning_run as live_result_run,
			sr.item_code as result_item_code, sr.sales_order as result_sales_order,
			sr.sales_order_item as result_sales_order_item,
			seg.name as live_segment, seg.parent as live_segment_result,
			seg.linked_work_order as segment_linked_work_order,
			seg.linked_work_order_scheduling as segment_linked_work_order_scheduling,
			seg.linked_scheduling_item as segment_linked_scheduling_item,
			si.name as live_scheduling_item, si.parent as scheduling_item_parent,
			si.work_order as scheduling_item_work_order,
			si.custom_aps_run as scheduling_item_run,
			si.custom_aps_result_reference as scheduling_item_result,
			si.custom_aps_segment_reference as scheduling_item_segment,
			wos.name as live_allocation_wos, wos.custom_aps_run as allocation_wos_run,
			wos.custom_aps_approval_state as allocation_wos_approval_state
		from `tabAPS Production Allocation` pa
		left join `tabStock Entry` se on se.name = pa.source_stock_entry
		left join `tabStock Entry Detail` sed on sed.name = pa.source_stock_entry_detail
		left join `tabWork Order` wo on wo.name = pa.work_order
		left join `tabAPS Schedule Result` sr on sr.name = pa.schedule_result
		left join `tabAPS Schedule Segment` seg on seg.name = pa.segment
		left join `tabScheduling Item` si on si.name = pa.scheduling_item
		left join `tabWork Order Scheduling` wos on wos.name = pa.work_order_scheduling
		where pa.is_effective = 1
			and (
				pa.planning_run = %(run_name)s
				or pa.source_stock_entry_detail in (
				select scoped.source_stock_entry_detail
				from `tabAPS Production Allocation` scoped
				where scoped.planning_run = %(run_name)s
					and scoped.is_effective = 1
					and scoped.source_stock_entry_detail is not null
				)
			)
		order by pa.source_stock_entry asc, pa.source_stock_entry_detail asc, pa.name asc
		""".format(scrap_select=scrap_select),
		{"run_name": run_name},
		as_dict=True,
	)
	live_sources = _get_audit_live_production_sources(run_name, scrap_select=scrap_select)
	valid_result_names = set(result_names)
	invalid_allocations = set()
	invalid_source_names = set()
	groups: dict[tuple[str, str], list[Any]] = defaultdict(list)

	def invalidate(row, fieldname, expected, actual, *, source=None):
		invalid_allocations.add(row.name)
		if row.get("source_stock_entry"):
			invalid_source_names.add(row.source_stock_entry)
		issues.append(
			_audit_difference(
				doctype="APS Production Allocation",
				name=row.name,
				fieldname=fieldname,
				expected=expected,
				actual=actual,
				source=source or row.get("source_stock_entry"),
			)
		)

	for row in rows:
		group_key = (
			row.get("source_stock_entry") or row.name,
			row.get("source_stock_entry_detail") or row.name,
		)
		groups[group_key].append(row)
		if row.get("planning_run") != run_name:
			invalidate(row, "planning_run", run_name, row.get("planning_run"), source="cross_run_source_claim")
		if not row.get("live_stock_entry"):
			invalidate(row, "source_stock_entry", row.get("source_stock_entry"), "Missing")
		elif cint(row.get("live_docstatus")) != 1:
			invalidate(row, "source_docstatus", 1, row.get("live_docstatus"))
		if cint(row.get("source_docstatus")) != 1:
			invalidate(row, "recorded_source_docstatus", 1, row.get("source_docstatus"))
		if row.get("live_purpose") != "Manufacture":
			invalidate(row, "source_purpose", "Manufacture", row.get("live_purpose") or "Missing")
		if not row.get("live_detail"):
			invalidate(row, "source_stock_entry_detail", row.get("source_stock_entry_detail"), "Missing")
		elif row.get("live_detail_parent") != row.get("source_stock_entry"):
			invalidate(
				row,
				"source_detail_parent",
				row.get("source_stock_entry"),
				row.get("live_detail_parent"),
			)
		if (
			row.get("schedule_result") not in valid_result_names
			or not row.get("live_result")
			or row.get("live_result_run") != run_name
		):
			invalidate(row, "schedule_result", "Result in audited run", row.get("schedule_result"))
		if not row.get("live_segment") or row.get("live_segment_result") != row.get("schedule_result"):
			invalidate(row, "segment", row.get("schedule_result"), row.get("live_segment_result") or "Missing")
		if not row.get("live_work_order_name"):
			invalidate(row, "work_order", row.get("work_order"), "Missing")
		elif row.get("live_work_order") != row.get("work_order"):
			invalidate(row, "source_work_order", row.get("work_order"), row.get("live_work_order"))
		if (
			not row.get("live_item_code")
			or row.get("live_item_code") != row.get("result_item_code")
			or row.get("live_item_code") != row.get("live_production_item")
		):
			invalidate(
				row,
				"source_item_code",
				row.get("result_item_code") or row.get("live_production_item") or "",
				row.get("live_item_code") or "Missing",
			)
		_validate_audit_production_lineage(row, run_name=run_name, invalidate=invalidate)
		live_output_type = _get_audit_live_production_output_type(row)
		if live_output_type != row.get("output_type"):
			invalidate(row, "output_type", live_output_type or "eligible Good/Scrap output", row.get("output_type"))
		live_qty = _get_audit_live_detail_qty(row)
		if live_qty <= QTY_TOLERANCE or abs(flt(row.get("source_qty")) - live_qty) > QTY_TOLERANCE:
			invalidate(row, "source_qty", live_qty, row.get("source_qty"))
		allocated_qty = flt(row.get("allocated_qty"))
		expected_good = allocated_qty if row.get("output_type") == "Good" else 0
		expected_scrap = allocated_qty if row.get("output_type") == "Scrap" else 0
		for fieldname, expected in (
			("effective_qty", allocated_qty),
			("good_qty", expected_good),
			("scrap_qty", expected_scrap),
		):
			if allocated_qty <= QTY_TOLERANCE or abs(flt(row.get(fieldname)) - expected) > QTY_TOLERANCE:
				invalidate(row, fieldname, expected, row.get(fieldname))

	for group_rows in groups.values():
		first = group_rows[0]
		live_qty = _get_audit_live_detail_qty(first)
		allocated_qty = sum(flt(row.get("allocated_qty")) for row in group_rows)
		if live_qty > QTY_TOLERANCE and abs(allocated_qty - live_qty) <= QTY_TOLERANCE:
			continue
		for row in group_rows:
			invalid_allocations.add(row.name)
			if row.get("source_stock_entry"):
				invalid_source_names.add(row.source_stock_entry)
		issues.append(
			_audit_difference(
				doctype="Stock Entry Detail",
				name=first.get("source_stock_entry_detail") or first.name,
				fieldname="effective_allocation_qty",
				expected=live_qty,
				actual=allocated_qty,
				source=first.get("source_stock_entry"),
			)
		)

	claimed_detail_names = {
		row.get("source_stock_entry_detail") for row in rows if row.get("source_stock_entry_detail")
	}
	for source in live_sources:
		if source.get("source_stock_entry_detail") in claimed_detail_names:
			continue
		if source.get("source_stock_entry"):
			invalid_source_names.add(source.source_stock_entry)
		issues.append(
			_audit_difference(
				doctype="Stock Entry Detail",
				name=source.get("source_stock_entry_detail") or source.get("source_stock_entry"),
				fieldname="effective_allocation_qty",
				expected=_get_audit_live_detail_qty(source),
				actual=0,
				source="submitted_manufacture_output_missing_from_aps_ledger",
			)
		)

	by_result: dict[str, dict[str, float]] = defaultdict(lambda: {"good_qty": 0.0, "scrap_qty": 0.0})
	by_segment: dict[str, dict[str, float]] = defaultdict(lambda: {"good_qty": 0.0, "scrap_qty": 0.0})
	valid_sources = set()
	for row in rows:
		if row.name in invalid_allocations or row.get("source_stock_entry") in invalid_source_names:
			continue
		by_result[row.schedule_result]["good_qty"] += flt(row.good_qty)
		by_result[row.schedule_result]["scrap_qty"] += flt(row.scrap_qty)
		by_segment[row.segment]["good_qty"] += flt(row.good_qty)
		by_segment[row.segment]["scrap_qty"] += flt(row.scrap_qty)
		valid_sources.add(row.source_stock_entry)
	return {
		"by_result": dict(by_result),
		"by_segment": dict(by_segment),
		"invalid_sources": issues,
		"source_count": len(valid_sources),
	}


def _validate_audit_production_lineage(row, *, run_name: str, invalidate) -> None:
	"""Verify the exact execution-detail path, not merely a matching item code."""
	direct_segment = row.get("live_direct_segment")
	direct_item = row.get("live_direct_scheduling_item")
	live_wos = row.get("live_work_order_scheduling")
	allocation_item = row.get("scheduling_item")
	allocation_wos = row.get("work_order_scheduling")

	if direct_segment and direct_segment != row.get("segment"):
		invalidate(row, "direct_segment", row.get("segment"), direct_segment)
	if direct_item and direct_item != allocation_item:
		invalidate(row, "direct_scheduling_item", allocation_item or "exact allocation Scheduling Item", direct_item)
	if live_wos and live_wos != allocation_wos:
		invalidate(row, "source_work_order_scheduling", allocation_wos or "exact allocation WOS", live_wos)
	if row.get("allocation_method") == "Direct" and not (direct_segment or direct_item):
		invalidate(row, "allocation_method", "Direct source segment or Scheduling Item", "Unlinked Direct")

	bound_by_execution_detail = False
	if allocation_item:
		if not row.get("live_scheduling_item"):
			invalidate(row, "scheduling_item", allocation_item, "Missing")
		else:
			bound_by_execution_detail = True
			for fieldname, expected, actual in (
				("scheduling_item_work_order", row.get("work_order"), row.get("scheduling_item_work_order")),
				("scheduling_item_run", run_name, row.get("scheduling_item_run")),
				("scheduling_item_result", row.get("schedule_result"), row.get("scheduling_item_result")),
				("scheduling_item_segment", row.get("segment"), row.get("scheduling_item_segment")),
			):
				if (actual or "") != (expected or ""):
					invalidate(row, fieldname, expected or "", actual or "")
			if allocation_wos and row.get("scheduling_item_parent") != allocation_wos:
				invalidate(row, "scheduling_item_parent", allocation_wos, row.get("scheduling_item_parent"))
	if allocation_wos:
		if not row.get("live_allocation_wos"):
			invalidate(row, "work_order_scheduling", allocation_wos, "Missing")
		else:
			if row.get("allocation_wos_run") != run_name:
				invalidate(row, "work_order_scheduling_run", run_name, row.get("allocation_wos_run"))
			if row.get("allocation_wos_approval_state") != "Approved":
				invalidate(
					row,
					"work_order_scheduling_approval_state",
					"Approved",
					row.get("allocation_wos_approval_state") or "",
				)

	if not bound_by_execution_detail:
		if row.get("segment_linked_work_order") != row.get("work_order"):
			invalidate(
				row,
				"segment_work_order_lineage",
				row.get("work_order"),
				row.get("segment_linked_work_order") or "Missing",
			)
		if allocation_wos and row.get("segment_linked_work_order_scheduling") != allocation_wos:
			invalidate(
				row,
				"segment_work_order_scheduling_lineage",
				allocation_wos,
				row.get("segment_linked_work_order_scheduling") or "Missing",
			)

	work_order_run = row.get("live_work_order_run") or ""
	work_order_result = row.get("live_work_order_result") or ""
	if bool(work_order_run) != bool(work_order_result):
		invalidate(
			row,
			"work_order_aps_owner",
			"both APS Run and APS Result, or neither",
			{"planning_run": work_order_run, "schedule_result": work_order_result},
		)
	elif work_order_run and (
		work_order_run != run_name or work_order_result != (row.get("schedule_result") or "")
	):
		invalidate(
			row,
			"work_order_aps_owner",
			{"planning_run": run_name, "schedule_result": row.get("schedule_result") or ""},
			{"planning_run": work_order_run, "schedule_result": work_order_result},
		)
	for fieldname, expected, actual in (
		("work_order_sales_order", row.get("result_sales_order"), row.get("live_work_order_sales_order")),
		(
			"work_order_sales_order_item",
			row.get("result_sales_order_item"),
			row.get("live_work_order_sales_order_item"),
		),
	):
		if (expected or "") != (actual or ""):
			invalidate(row, fieldname, expected or "", actual or "")


def _get_audit_live_production_sources(run_name: str, *, scrap_select: str) -> list[Any]:
	"""Load all submitted output details associated with the run in one query."""
	output_condition = """(
		(
			sed.is_finished_item = 1
			or {scrap_select} = 1
			or (
				wo.production_item is not null
				and sed.item_code = wo.production_item
				and se.custom_aps_output_type in ('Good', 'Scrap')
			)
			or (
				wo.production_item is not null
				and sed.item_code = wo.production_item
				and wo.scrap_warehouse is not null
				and sed.t_warehouse = wo.scrap_warehouse
			)
		)
		and (wo.production_item is null or sed.item_code = wo.production_item)
	)""".format(scrap_select=scrap_select)
	return frappe.db.sql(
		"""
		select distinct
			se.name as source_stock_entry,
			sed.name as source_stock_entry_detail,
			sed.qty as live_document_qty,
			sed.transfer_qty as live_stock_qty
		from `tabStock Entry` se
		inner join `tabStock Entry Detail` sed on sed.parent = se.name
		left join `tabWork Order` wo on wo.name = se.work_order
		where se.docstatus = 1
			and se.purpose = 'Manufacture'
			and {output_condition}
			and coalesce(nullif(sed.transfer_qty, 0), sed.qty) > 0
			and (
				wo.custom_aps_run = %(run_name)s
				or
				exists (
					select 1
					from `tabAPS Schedule Segment` run_segment
					inner join `tabAPS Schedule Result` run_result
						on run_result.name = run_segment.parent
					where run_result.planning_run = %(run_name)s
						and (
							run_segment.name = se.custom_aps_segment_reference
							or run_segment.linked_work_order = se.work_order
						)
				)
				or exists (
					select 1
					from `tabScheduling Item` run_item
					where (
							run_item.name = se.custom_aps_scheduling_item
							or run_item.work_order = se.work_order
						)
						and run_item.custom_aps_run = %(run_name)s
				)
				or exists (
					select 1
					from `tabWork Order Scheduling` run_schedule
					where run_schedule.name = se.work_order_scheduling
						and run_schedule.custom_aps_run = %(run_name)s
				)
			)
		order by se.name asc, sed.name asc
		""".format(output_condition=output_condition),
		{"run_name": run_name},
		as_dict=True,
	)


def _get_audit_live_detail_qty(row) -> float:
	stock_qty = flt(row.get("live_stock_qty"))
	return abs(stock_qty) if abs(stock_qty) > QTY_TOLERANCE else abs(flt(row.get("live_document_qty")))


def _get_audit_live_production_output_type(row) -> str | None:
	item_code = row.get("live_item_code")
	production_item = row.get("live_production_item")
	if not item_code or not production_item or item_code != production_item:
		return None
	if cint(row.get("live_is_scrap_item")):
		return "Scrap"
	if row.get("live_scrap_warehouse") and row.get("live_target_warehouse") == row.get("live_scrap_warehouse"):
		return "Scrap"
	if row.get("live_explicit_output_type") in ("Good", "Scrap"):
		return row.get("live_explicit_output_type")
	if cint(row.get("live_is_finished_item")):
		return "Good"
	return None


def _get_audit_backlog_delivery_totals(result_rows) -> dict[str, Any]:
	"""Rebuild exact Sales Order backlog delivery from submitted DN item rows."""
	issues = _AuditDifferenceCollector()
	keys_by_item: dict[str, set[tuple[str, str, str, str, str]]] = defaultdict(set)
	baselines_by_item: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
	for result in result_rows or []:
		if result.get("demand_source") != "Sales Order Backlog":
			continue
		baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
		for row in ((baseline or {}).get("sales_order_items") or []):
			if not isinstance(row, dict):
				continue
			if (
				(row.get("sales_order") or "") != (result.get("sales_order") or "")
				or (row.get("sales_order_item") or "") != (result.get("sales_order_item") or "")
				or (row.get("item_code") or "") != (result.get("item_code") or "")
			):
				continue
			key = (
				result.get("company") or "",
				result.get("customer") or "",
				result.get("item_code") or "",
				result.get("sales_order") or "",
				result.get("sales_order_item") or "",
			)
			if key[4]:
				keys_by_item[key[4]].add(key)
				baselines_by_item[key[4]].append((result.get("name") or "", row))
	if not keys_by_item:
		return {"by_key": {}, "invalid_sources": issues, "source_names": set()}

	sales_order_items = tuple(sorted(keys_by_item))
	live_items = frappe.db.sql(
		"""
		select
			i.name as sales_order_item, i.parent as sales_order, i.item_code, i.qty as ordered_qty,
			coalesce(i.delivered_qty, 0) as delivered_qty,
			s.name as live_sales_order, s.company,
			ifnull(s.customer, '') as customer, s.docstatus
		from `tabSales Order Item` i
		left join `tabSales Order` s on s.name = i.parent
		where i.name in %(sales_order_items)s
		""",
		{"sales_order_items": sales_order_items},
		as_dict=True,
	)
	events = frappe.db.sql(
		"""
		select
			dn.name as source_delivery_note, dn.company,
			ifnull(dn.customer, '') as customer, dn.is_return,
			dn.return_against, dn.posting_date, dn.posting_time,
			dn.creation, dni.idx, dni.name as source_delivery_note_item,
			dni.item_code, dni.against_sales_order as sales_order,
			dni.so_detail as sales_order_item, dni.dn_detail as original_delivery_note_item,
			original_dni.parent as original_delivery_note,
			original_dni.item_code as original_item_code,
			original_dni.against_sales_order as original_sales_order,
			original_dni.so_detail as original_sales_order_item,
			dni.qty as live_document_qty, dni.stock_qty as live_stock_qty
		from `tabDelivery Note` dn
		inner join `tabDelivery Note Item` dni on dni.parent = dn.name
		left join `tabDelivery Note Item` original_dni on original_dni.name = dni.dn_detail
		where dn.docstatus = 1
			and (
				dni.so_detail in %(sales_order_items)s
				or original_dni.so_detail in %(sales_order_items)s
				or (
					dn.is_return = 1
					and exists (
						select 1
						from `tabDelivery Note Item` return_scope
						where return_scope.parent = dn.return_against
							and return_scope.so_detail in %(sales_order_items)s
							and return_scope.item_code = dni.item_code
					)
				)
			)
			and abs(coalesce(nullif(dni.stock_qty, 0), dni.qty, 0)) > 0
		order by dn.posting_date asc, dn.posting_time asc, dn.creation asc, dni.idx asc, dni.name asc
		""",
		{"sales_order_items": sales_order_items},
		as_dict=True,
	)

	def issue(doctype, name, fieldname, expected, actual, *, source):
		issues.append(
			_audit_difference(
				doctype=doctype,
				name=name,
				fieldname=fieldname,
				expected=expected,
				actual=actual,
				source=source,
			)
		)

	live_by_item = {row.get("sales_order_item"): row for row in live_items}
	valid_keys = set()
	for sales_order_item, keys in keys_by_item.items():
		if len(keys) != 1:
			issue(
				"Sales Order Item",
				sales_order_item,
				"aps_backlog_identity_count",
				1,
				len(keys),
				source="conflicting_backlog_result_identity",
			)
			continue
		key = next(iter(keys))
		live = live_by_item.get(sales_order_item)
		live_key = (
			(live or {}).get("company") or "",
			(live or {}).get("customer") or "",
			(live or {}).get("item_code") or "",
			(live or {}).get("sales_order") or "",
			(live or {}).get("sales_order_item") or "",
		)
		if not live or not live.get("live_sales_order") or cint(live.get("docstatus")) != 1 or live_key != key:
			issue(
				"Sales Order Item",
				sales_order_item,
				"exact_submitted_source",
				"submitted Sales Order Item matching company/customer/item/order",
				"Missing or mismatched",
				source="backlog_delivery_source",
			)
			continue
		for result_name, baseline_row in baselines_by_item.get(sales_order_item) or []:
			if baseline_row.get("opening_ordered_qty") in (None, ""):
				continue
			if abs(flt(baseline_row.get("opening_ordered_qty")) - flt(live.get("ordered_qty"))) <= QTY_TOLERANCE:
				continue
			issue(
				"Sales Order Item",
				sales_order_item,
				"qty",
				baseline_row.get("opening_ordered_qty"),
				live.get("ordered_qty"),
				source=result_name or "backlog_frozen_baseline",
			)
		valid_keys.add(key)

	normal_by_detail: dict[str, tuple[Any, tuple[str, str, str, str, str]]] = {}
	normal_by_document_name: dict[str, list[tuple[Any, tuple[str, str, str, str, str]]]] = defaultdict(list)
	event_keys: dict[str, tuple[str, str, str, str, str]] = {}
	return_trace_details: dict[str, list[str]] = {}
	invalid_detail_names = set()

	def event_key(event):
		return (
			event.get("company") or "",
			event.get("customer") or "",
			event.get("item_code") or "",
			event.get("sales_order") or "",
			event.get("sales_order_item") or "",
		)

	for event in events:
		if cint(event.get("is_return")):
			continue
		key = event_key(event)
		if key not in valid_keys:
			invalid_detail_names.add(event.get("source_delivery_note_item"))
			issue(
				"Delivery Note Item",
				event.get("source_delivery_note_item") or event.get("source_delivery_note"),
				"sales_order_item_lineage",
				"exact audited Sales Order Item identity",
				key,
				source="backlog_delivery_source",
			)
			continue
		detail_name = event.get("source_delivery_note_item")
		event_keys[detail_name] = key
		normal_by_detail[detail_name] = (event, key)
		normal_by_document_name[event.get("source_delivery_note")].append((event, key))

	for event in events:
		if not cint(event.get("is_return")):
			continue
		detail_name = event.get("source_delivery_note_item")
		recorded_key = event_key(event)
		original_detail = event.get("original_delivery_note_item")
		traced_pair = normal_by_detail.get(original_detail) if original_detail else None
		trace_candidates: list[tuple[Any, tuple[str, str, str, str, str]]] = []
		if traced_pair:
			trace_candidates = [traced_pair]
		elif not original_detail and event.get("return_against"):
			for original, original_key in normal_by_document_name.get(event.get("return_against")) or []:
				if recorded_key[0] and recorded_key[0] != original_key[0]:
					continue
				if recorded_key[1] and recorded_key[1] != original_key[1]:
					continue
				if recorded_key[2] and recorded_key[2] != original_key[2]:
					continue
				if recorded_key[3] and recorded_key[3] != original_key[3]:
					continue
				if recorded_key[4] and recorded_key[4] != original_key[4]:
					continue
				trace_candidates.append((original, original_key))

		candidate_keys = {key for _original, key in trace_candidates}
		if len(candidate_keys) != 1:
			invalid_detail_names.add(detail_name)
			issue(
				"Delivery Note Item",
				detail_name or event.get("source_delivery_note"),
				"return_trace",
				"one submitted original Delivery Note lineage for the exact audited Sales Order Item",
				original_detail or event.get("return_against") or "Missing",
				source="backlog_delivery_return",
			)
			continue

		resolved_key = next(iter(candidate_keys))
		if any(actual and actual != expected for actual, expected in zip(recorded_key, resolved_key)):
			invalid_detail_names.add(detail_name)
			issue(
				"Delivery Note Item",
				detail_name or event.get("source_delivery_note"),
				"return_sales_order_item_lineage",
				resolved_key,
				recorded_key,
				source="backlog_delivery_return",
			)
			continue
		if original_detail:
			original_event = trace_candidates[0][0]
			if event.get("return_against") and event.get("return_against") != original_event.get(
				"source_delivery_note"
			):
				invalid_detail_names.add(detail_name)
				issue(
					"Delivery Note Item",
					detail_name or event.get("source_delivery_note"),
					"return_against",
					original_event.get("source_delivery_note"),
					event.get("return_against"),
					source="backlog_delivery_return",
				)
				continue
		event_keys[detail_name] = resolved_key
		return_trace_details[detail_name] = [
			original.get("source_delivery_note_item") for original, _key in trace_candidates
		]

	# Reconcile every return against still-unreturned physical quantity on its
	# exact original detail(s).  This catches a return that stays below the SOI
	# total only because another Delivery Note row masks an over-return.
	remaining_by_original = {
		detail_name: _get_audit_live_detail_qty(original)
		for detail_name, (original, _key) in normal_by_detail.items()
	}
	for event in events:
		if not cint(event.get("is_return")):
			continue
		detail_name = event.get("source_delivery_note_item")
		if detail_name in invalid_detail_names or detail_name not in event_keys:
			continue
		remaining = _get_audit_live_detail_qty(event)
		for original_detail in return_trace_details.get(detail_name) or []:
			available = max(flt(remaining_by_original.get(original_detail)), 0)
			used = min(remaining, available)
			remaining_by_original[original_detail] = available - used
			remaining -= used
			if remaining <= QTY_TOLERANCE:
				break
		if remaining <= QTY_TOLERANCE:
			continue
		invalid_detail_names.add(detail_name)
		issue(
			"Delivery Note Item",
			detail_name or event.get("source_delivery_note"),
			"return_qty",
			"<= unreturned quantity on traced original Delivery Note Item(s)",
			_get_audit_live_detail_qty(event),
			source="backlog_delivery_return",
		)

	events_by_key: dict[tuple[str, str, str, str, str], list[Any]] = defaultdict(list)
	for event in events:
		key = event_keys.get(event.get("source_delivery_note_item"))
		if key:
			events_by_key[key].append(event)

	by_key = {}
	valid_source_names = set()
	for key in valid_keys:
		physical = 0.0
		for event in events_by_key.get(key) or []:
			if event.get("source_delivery_note_item") in invalid_detail_names:
				continue
			qty = _get_audit_live_detail_qty(event)
			signed_qty = -qty if cint(event.get("is_return")) else qty
			physical += signed_qty
			valid_source_names.add(event.get("source_delivery_note"))
		physical = max(physical, 0)
		by_key[key] = physical
		live = live_by_item.get(key[4])
		if live and abs(flt(live.get("delivered_qty")) - physical) > QTY_TOLERANCE:
			issue(
				"Sales Order Item",
				key[4],
				"delivered_qty",
				physical,
				live.get("delivered_qty"),
				source="submitted_delivery_note_details",
			)
	return {"by_key": by_key, "invalid_sources": issues, "source_names": valid_source_names}


def _validate_audit_delivery_lineage(row, *, target_rows: dict[str, Any], invalidate) -> None:
	"""Verify Direct, FIFO and return attribution against the live source detail."""
	target = target_rows.get(row.get("customer_schedule_item")) or {}
	for fieldname in ("company", "customer", "item_code", "sales_order", "sales_order_item"):
		owner_field = f"result_{fieldname}"
		if owner_field not in target:
			continue
		if (row.get(fieldname) or "") != (target.get(owner_field) or ""):
			invalidate(
				row,
				f"fulfillment_owner_{fieldname}",
				target.get(owner_field) or "",
				row.get(fieldname) or "",
			)
	effective_schedule_date = _get_audit_target_effective_schedule_date(target)
	if effective_schedule_date and getdate(effective_schedule_date) != getdate(row.get("schedule_date")):
		invalidate(row, "schedule_date", effective_schedule_date, row.get("schedule_date"))

	method = row.get("allocation_method") or ""
	is_return = cint(row.get("live_is_return"))
	direct_target = row.get("live_direct_schedule_item")
	if is_return:
		if method != "Return Trace":
			invalidate(row, "allocation_method", "Return Trace", method)
		if (row.get("return_against") or "") != (row.get("live_return_against") or ""):
			invalidate(row, "return_against", row.get("live_return_against") or "", row.get("return_against") or "")
		original_detail = row.get("original_delivery_note_item")
		if not original_detail or not row.get("live_original_detail"):
			invalidate(row, "original_delivery_note_item", "submitted original delivery detail", original_detail or "Missing")
		else:
			if cint(row.get("live_original_docstatus")) != 1:
				invalidate(row, "original_delivery_note_docstatus", 1, row.get("live_original_docstatus"))
			if row.get("live_return_against") and row.get("live_return_against") != row.get(
				"live_original_delivery_note"
			):
				invalidate(
					row,
					"original_delivery_note",
					row.get("live_return_against"),
					row.get("live_original_delivery_note") or "",
				)
			if row.get("live_original_delivery_note_item") and row.get("live_original_delivery_note_item") != original_detail:
				invalidate(
					row,
					"original_delivery_note_item",
					row.get("live_original_delivery_note_item"),
					original_detail,
				)
			elif not row.get("live_original_delivery_note_item") and row.get("live_return_against") != row.get(
				"live_original_delivery_note"
			):
				invalidate(
					row,
					"original_delivery_note",
					row.get("live_return_against") or "",
					row.get("live_original_delivery_note") or "",
				)
			for fieldname, expected, actual in (
				("original_item_code", row.get("live_item_code"), row.get("live_original_item_code")),
				("original_sales_order", row.get("live_sales_order"), row.get("live_original_sales_order")),
				(
					"original_sales_order_item",
					row.get("live_sales_order_item"),
					row.get("live_original_sales_order_item"),
				),
			):
				if (expected or "") != (actual or ""):
					invalidate(row, fieldname, expected or "", actual or "")
			if flt(row.get("live_original_allocated_qty")) <= QTY_TOLERANCE:
				invalidate(
					row,
					"return_trace_qty",
					"positive original APS allocation to the same target",
					row.get("live_original_allocated_qty"),
				)
		return

	if direct_target:
		if method != "Direct":
			invalidate(row, "allocation_method", "Direct", method)
		return
	if method not in ("Controlled FIFO", "Replacement FIFO"):
		invalidate(row, "allocation_method", "Controlled FIFO or Replacement FIFO", method)
		return
	if method == "Controlled FIFO" and getdate(row.get("live_posting_date")) != getdate(row.get("schedule_date")):
		invalidate(row, "fifo_schedule_date", row.get("live_posting_date"), row.get("schedule_date"))


def _get_audit_delivery_totals(result_rows) -> dict[str, Any]:
	issues = _get_audit_schema_issues(
		{
			"APS Delivery Allocation": (
				"company",
				"customer",
				"item_code",
				"sales_order",
				"sales_order_item",
				"schedule_date",
				"customer_schedule_item",
				"allocation_method",
				"source_delivery_note",
				"source_delivery_note_item",
				"source_docstatus",
				"is_return",
				"return_against",
				"original_delivery_note_item",
				"source_qty",
				"allocated_qty",
				"effective_qty",
				"is_effective",
			),
			"Delivery Note": (
				"docstatus",
				"company",
				"customer",
				"posting_date",
				"posting_time",
				"is_return",
				"return_against",
			),
			"Delivery Note Item": (
				"parent",
				"item_code",
				"qty",
				"against_sales_order",
				"so_detail",
				"dn_detail",
				"stock_qty",
				"custom_aps_customer_schedule_item",
			),
			"Customer Delivery Schedule Item": (
				"parent",
				"item_code",
				"sales_order",
				"schedule_date",
				"qty",
				"delivered_qty",
				"status",
			),
			"Customer Delivery Schedule": ("company", "customer", "status"),
			"Sales Order": ("docstatus", "company", "customer"),
			"Sales Order Item": ("parent", "item_code", "delivered_qty"),
		},
		source="phase6_schema",
	)
	if issues.total_count:
		return {
			"by_target": {},
			"backlog_by_key": {},
			"invalid_sources": issues,
			"source_count": 0,
		}
	backlog = _get_audit_backlog_delivery_totals(result_rows)
	issues.merge(backlog["invalid_sources"])
	target_rows = {}
	for result in result_rows:
		for target in _get_audit_customer_schedule_target_rows(result):
			target_name = target.get("customer_schedule_item")
			if not target_name:
				continue
			target_rows[target_name] = {
				**target,
				"result_name": result.get("name"),
				"result_company": result.get("company"),
				"result_customer": result.get("customer"),
				"result_item_code": result.get("item_code"),
				"result_sales_order": result.get("sales_order"),
				"result_sales_order_item": result.get("sales_order_item"),
			}
	target_names = sorted(
		target_rows
	)
	if not target_names:
		return {
			"by_target": {},
			"backlog_by_key": backlog["by_key"],
			"invalid_sources": issues,
			"source_count": len(backlog["source_names"]),
		}
	rows = frappe.db.sql(
		"""
		select
			da.name, da.company, da.customer, da.item_code,
			da.sales_order, da.sales_order_item, da.schedule_date,
			da.customer_schedule_item, da.allocation_method,
			da.source_delivery_note, da.source_delivery_note_item,
			da.source_docstatus, da.is_return, da.return_against,
			da.original_delivery_note_item, da.source_qty,
			da.allocated_qty, da.effective_qty,
			dn.name as live_delivery_note, dn.docstatus as live_docstatus,
			dn.company as live_company, dn.customer as live_customer,
			dn.is_return as live_is_return, dn.posting_date as live_posting_date,
			dn.return_against as live_return_against,
			dni.name as live_detail, dni.parent as live_detail_parent,
			dni.item_code as live_item_code, dni.stock_qty as live_stock_qty,
			dni.qty as live_document_qty,
			dni.against_sales_order as live_sales_order,
			dni.so_detail as live_sales_order_item,
			dni.dn_detail as live_original_delivery_note_item,
			dni.custom_aps_customer_schedule_item as live_direct_schedule_item,
			original_dni.name as live_original_detail,
			original_dni.parent as live_original_delivery_note,
			original_dni.item_code as live_original_item_code,
			original_dni.against_sales_order as live_original_sales_order,
			original_dni.so_detail as live_original_sales_order_item,
			original_dn.docstatus as live_original_docstatus,
			(
				select coalesce(sum(original_allocation.allocated_qty), 0)
				from `tabAPS Delivery Allocation` original_allocation
				where original_allocation.source_delivery_note_item = da.original_delivery_note_item
					and original_allocation.customer_schedule_item = da.customer_schedule_item
					and original_allocation.is_return = 0
					and original_allocation.is_effective = 1
			) as live_original_allocated_qty
		from `tabAPS Delivery Allocation` da
		left join `tabDelivery Note` dn on dn.name = da.source_delivery_note
		left join `tabDelivery Note Item` dni on dni.name = da.source_delivery_note_item
		left join `tabDelivery Note Item` original_dni on original_dni.name = da.original_delivery_note_item
		left join `tabDelivery Note` original_dn on original_dn.name = original_dni.parent
		where da.is_effective = 1
			and (
				da.customer_schedule_item in %(target_names)s
				or da.source_delivery_note_item in (
					select scoped.source_delivery_note_item
					from `tabAPS Delivery Allocation` scoped
					where scoped.is_effective = 1
						and scoped.customer_schedule_item in %(target_names)s
						and scoped.source_delivery_note_item is not null
				)
			)
		order by da.source_delivery_note asc, da.source_delivery_note_item asc, da.name asc
		""",
		{"target_names": tuple(target_names)},
		as_dict=True,
	)
	live_sources = _get_audit_live_delivery_sources(target_names)
	invalid_allocations = set()
	invalid_source_names = set()
	groups: dict[tuple[str, str], list[Any]] = defaultdict(list)

	def invalidate(row, fieldname, expected, actual, *, source=None):
		invalid_allocations.add(row.name)
		if row.get("source_delivery_note"):
			invalid_source_names.add(row.source_delivery_note)
		issues.append(
			_audit_difference(
				doctype="APS Delivery Allocation",
				name=row.name,
				fieldname=fieldname,
				expected=expected,
				actual=actual,
				source=source or row.get("source_delivery_note"),
			)
		)

	for row in rows:
		group_key = (
			row.get("source_delivery_note") or row.name,
			row.get("source_delivery_note_item") or row.name,
		)
		groups[group_key].append(row)
		if not row.get("live_delivery_note"):
			invalidate(row, "source_delivery_note", row.get("source_delivery_note"), "Missing")
		elif cint(row.get("live_docstatus")) != 1:
			invalidate(row, "source_docstatus", 1, row.get("live_docstatus"))
		if cint(row.get("source_docstatus")) != 1:
			invalidate(row, "recorded_source_docstatus", 1, row.get("source_docstatus"))
		if not row.get("live_detail"):
			invalidate(row, "source_delivery_note_item", row.get("source_delivery_note_item"), "Missing")
		elif row.get("live_detail_parent") != row.get("source_delivery_note"):
			invalidate(
				row,
				"source_detail_parent",
				row.get("source_delivery_note"),
				row.get("live_detail_parent"),
			)
		for fieldname, live_fieldname in (
			("company", "live_company"),
			("customer", "live_customer"),
			("item_code", "live_item_code"),
			("sales_order", "live_sales_order"),
			("sales_order_item", "live_sales_order_item"),
		):
			if (row.get(fieldname) or "") == (row.get(live_fieldname) or ""):
				continue
			invalidate(row, fieldname, row.get(live_fieldname) or "", row.get(fieldname) or "")
		if cint(row.get("is_return")) != cint(row.get("live_is_return")):
			invalidate(row, "is_return", cint(row.get("live_is_return")), cint(row.get("is_return")))
		if row.get("live_direct_schedule_item") and row.get("live_direct_schedule_item") != row.get(
			"customer_schedule_item"
		):
			invalidate(
				row,
				"customer_schedule_item",
				row.get("live_direct_schedule_item"),
				row.get("customer_schedule_item"),
			)
		_validate_audit_delivery_lineage(row, target_rows=target_rows, invalidate=invalidate)
		live_qty = _get_audit_live_detail_qty(row)
		if live_qty <= QTY_TOLERANCE or abs(flt(row.get("source_qty")) - live_qty) > QTY_TOLERANCE:
			invalidate(row, "source_qty", live_qty, row.get("source_qty"))
		allocated_qty = flt(row.get("allocated_qty"))
		expected_effective = -allocated_qty if cint(row.get("live_is_return")) else allocated_qty
		if allocated_qty <= QTY_TOLERANCE or abs(flt(row.get("effective_qty")) - expected_effective) > QTY_TOLERANCE:
			invalidate(row, "effective_qty", expected_effective, row.get("effective_qty"))

	for group_rows in groups.values():
		first = group_rows[0]
		live_qty = _get_audit_live_detail_qty(first)
		allocated_qty = sum(flt(row.get("allocated_qty")) for row in group_rows)
		if live_qty > QTY_TOLERANCE and abs(allocated_qty - live_qty) <= QTY_TOLERANCE:
			continue
		for row in group_rows:
			invalid_allocations.add(row.name)
			if row.get("source_delivery_note"):
				invalid_source_names.add(row.source_delivery_note)
		issues.append(
			_audit_difference(
				doctype="Delivery Note Item",
				name=first.get("source_delivery_note_item") or first.name,
				fieldname="effective_allocation_qty",
				expected=live_qty,
				actual=allocated_qty,
					source=first.get("source_delivery_note"),
				)
			)

	return_groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
	for row in rows:
		if cint(row.get("live_is_return")) and row.get("original_delivery_note_item"):
			return_groups[(row.original_delivery_note_item, row.get("customer_schedule_item") or "")].append(row)
	for group_rows in return_groups.values():
		original_qty = max(flt(row.get("live_original_allocated_qty")) for row in group_rows)
		returned_qty = sum(flt(row.get("allocated_qty")) for row in group_rows)
		if returned_qty <= original_qty + QTY_TOLERANCE:
			continue
		for row in group_rows:
			invalid_allocations.add(row.name)
			if row.get("source_delivery_note"):
				invalid_source_names.add(row.source_delivery_note)
		issues.append(
			_audit_difference(
				doctype="Delivery Note Item",
				name=group_rows[0].get("original_delivery_note_item"),
				fieldname="cumulative_return_qty",
				expected=original_qty,
				actual=returned_qty,
				source="return_trace_capacity",
			)
		)

	claimed_detail_names = {
		row.get("source_delivery_note_item") for row in rows if row.get("source_delivery_note_item")
	}
	for source in live_sources:
		if source.get("source_delivery_note_item") in claimed_detail_names:
			continue
		if source.get("source_delivery_note"):
			invalid_source_names.add(source.source_delivery_note)
		issues.append(
			_audit_difference(
				doctype="Delivery Note Item",
				name=source.get("source_delivery_note_item") or source.get("source_delivery_note"),
				fieldname="effective_allocation_qty",
				expected=_get_audit_live_detail_qty(source),
				actual=0,
				source="submitted_delivery_detail_missing_from_aps_ledger",
			)
		)

	by_target: dict[str, float] = defaultdict(float)
	valid_sources = set(backlog["source_names"])
	for row in rows:
		if row.name in invalid_allocations or row.get("source_delivery_note") in invalid_source_names:
			continue
		if row.get("customer_schedule_item") in target_names:
			by_target[row.customer_schedule_item] += flt(row.effective_qty)
		valid_sources.add(row.source_delivery_note)
	return {
		"by_target": dict(by_target),
		"backlog_by_key": backlog["by_key"],
		"invalid_sources": issues,
		"source_count": len(valid_sources),
	}


def _get_audit_live_delivery_sources(target_names: list[str]) -> list[Any]:
	if not target_names:
		return []
	return frappe.db.sql(
		"""
		select distinct
			dn.name as source_delivery_note,
			dni.name as source_delivery_note_item,
			dni.qty as live_document_qty,
			dni.stock_qty as live_stock_qty
		from `tabDelivery Note` dn
		inner join `tabDelivery Note Item` dni on dni.parent = dn.name
		where dn.docstatus = 1
			and (
				dni.custom_aps_customer_schedule_item in %(target_names)s
				or exists (
					select 1
					from `tabAPS Delivery Allocation` linked
					where linked.source_delivery_note_item = dni.name
						and linked.customer_schedule_item in %(target_names)s
				)
				or exists (
					select 1
					from `tabAPS Delivery Allocation` traced
					where traced.is_return = 0
						and traced.customer_schedule_item in %(target_names)s
						and (
							traced.source_delivery_note_item = dni.dn_detail
							or (
								traced.source_delivery_note = dn.return_against
								and traced.item_code = dni.item_code
								and ifnull(traced.sales_order, '') = ifnull(dni.against_sales_order, '')
							)
						)
				)
				or exists (
					select 1
					from `tabDelivery Note Item` original_direct
					where original_direct.custom_aps_customer_schedule_item in %(target_names)s
						and (
							original_direct.name = dni.dn_detail
							or (
								original_direct.parent = dn.return_against
								and original_direct.item_code = dni.item_code
								and ifnull(original_direct.against_sales_order, '') = ifnull(dni.against_sales_order, '')
							)
						)
				)
				or exists (
					select 1
					from `tabCustomer Delivery Schedule Item` target
					inner join `tabCustomer Delivery Schedule` schedule
						on schedule.name = target.parent
					where target.name in %(target_names)s
						and schedule.company = dn.company
						and schedule.customer = dn.customer
						and target.item_code = dni.item_code
						and target.schedule_date = dn.posting_date
						and ifnull(target.sales_order, '') = ifnull(dni.against_sales_order, '')
				)
			)
			and abs(coalesce(nullif(dni.stock_qty, 0), dni.qty)) > 0
		order by dn.name asc, dni.name asc
		""",
		{"target_names": tuple(target_names)},
		as_dict=True,
	)


def _get_audit_validated_schedule_targets(
	result,
	*,
	differences: _AuditDifferenceCollector,
) -> list[dict[str, Any]]:
	baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
	has_schedule_source = _result_has_customer_schedule_source(result)
	if not has_schedule_source:
		return _get_audit_customer_schedule_target_rows(result)

	def issue(fieldname, expected, actual, *, source="fulfillment_baseline"):
		differences.append(
			_audit_difference(
				doctype="APS Schedule Result",
				name=result.name,
				fieldname=fieldname,
				expected=expected,
				actual=actual,
				source=source,
			)
		)

	rows = baseline.get("targets") if isinstance(baseline, dict) else None
	if not isinstance(rows, list) or not rows:
		issue("fulfillment_baseline_json", "non-empty frozen customer schedule targets", "Missing or invalid")
		return []

	source_snapshot = _parse_audit_demand_source_snapshot(result.get("demand_source_snapshot_json"))
	source_qty_by_target: dict[str, float] = defaultdict(float)
	sources_by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
	if source_snapshot is None:
		issue("demand_source_snapshot_json", "valid frozen demand source rows", "Missing or invalid")
	else:
		for source in source_snapshot:
			if isinstance(source, dict) and source.get("source_doctype") == "Customer Delivery Schedule":
				target_name = source.get("source_detail_name") or ""
				source_qty_by_target[target_name] += max(flt(source.get("qty")), 0)
				sources_by_target[target_name].append(source)

	required_nonempty_fields = (
		"customer_schedule_item",
		"customer_schedule",
		"item_code",
		"schedule_date",
		"source_open_qty",
		"opening_required_qty",
		"opening_delivered_qty",
	)
	# A customer schedule is a valid demand source even when it has not been
	# allocated to a framework Sales Order.  Keep the lineage keys mandatory so
	# absence is distinguishable from an explicit unallocated value, but allow
	# their values to be blank.
	required_identity_fields = ("sales_order", "sales_order_item")
	required_fields = (*required_nonempty_fields, *required_identity_fields)
	valid = []
	seen = set()
	for index, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			issue("fulfillment_baseline_json", "object target rows", f"invalid row {index}")
			continue
		if cint(row.get("retired")):
			continue
		missing = [
			fieldname
			for fieldname in required_nonempty_fields
			if row.get(fieldname) in (None, "")
		] + [fieldname for fieldname in required_identity_fields if fieldname not in row]
		if missing:
			issue(
				"fulfillment_baseline_json",
				"target fields: {0}".format(", ".join(required_fields)),
				"row {0} missing {1}".format(index, ", ".join(missing)),
			)
			continue
		target_name = row.get("customer_schedule_item")
		if target_name in seen:
			issue("fulfillment_baseline_json", "one claim per target", f"duplicate {target_name}")
			continue
		seen.add(target_name)
		if (row.get("item_code") or "") != (result.get("item_code") or ""):
			issue("fulfillment_target_item_code", result.get("item_code") or "", row.get("item_code") or "")
			continue
		if (row.get("sales_order") or "") != (result.get("sales_order") or ""):
			issue("fulfillment_target_sales_order", result.get("sales_order") or "", row.get("sales_order") or "")
			continue
		if (row.get("sales_order_item") or "") != (result.get("sales_order_item") or ""):
			issue(
				"fulfillment_target_sales_order_item",
				result.get("sales_order_item") or "",
				row.get("sales_order_item") or "",
			)
			continue
		opening_required = flt(row.get("opening_required_qty"))
		opening_delivered = flt(row.get("opening_delivered_qty"))
		source_open = flt(row.get("source_open_qty"))
		if min(opening_required, opening_delivered, source_open) < -QTY_TOLERANCE:
			issue("fulfillment_target_quantities", ">= 0", [opening_required, opening_delivered, source_open])
			continue
		if opening_delivered > opening_required + QTY_TOLERANCE:
			issue("opening_delivered_qty", f"<= opening_required_qty ({opening_required:g})", opening_delivered)
		opening_open = max(opening_required - opening_delivered, 0)
		if abs(source_open - opening_open) > QTY_TOLERANCE:
			issue("source_open_qty", opening_open, source_open)

		accepted_quantity_fields = (
			"accepted_required_qty",
			"accepted_delivered_qty",
			"accepted_source_open_qty",
			"accepted_current_open_qty",
		)
		accepted_provenance_fields = (
			"accepted_source_demand_delta",
			"accepted_by_change_request",
		)
		accepted_fields = (
			*accepted_quantity_fields,
			"accepted_schedule_date",
			*accepted_provenance_fields,
		)
		has_accepted_epoch = any(row.get(fieldname) not in (None, "") for fieldname in accepted_fields)
		accepted_values = {}
		if has_accepted_epoch:
			missing_accepted = [
				fieldname for fieldname in accepted_fields if row.get(fieldname) in (None, "")
			]
			if missing_accepted:
				issue(
					"fulfillment_baseline_json",
					"complete accepted Demand Delta epoch: {0}".format(", ".join(accepted_fields)),
					"row {0} missing {1}".format(index, ", ".join(missing_accepted)),
				)
				continue
			accepted_values = {
				fieldname: flt(row.get(fieldname)) for fieldname in accepted_quantity_fields
			}
			for fieldname in accepted_provenance_fields:
				if (baseline.get(fieldname) or "") != (row.get(fieldname) or ""):
					issue(
						fieldname,
						baseline.get(fieldname) or "complete accepted epoch top-level provenance",
						row.get(fieldname) or "Missing",
						source="accepted_epoch_provenance",
					)
			if min(accepted_values.values()) < -QTY_TOLERANCE:
				issue(
					"accepted_epoch_quantities",
					">= 0",
					accepted_values,
				)
				continue
			if accepted_values["accepted_required_qty"] + QTY_TOLERANCE < opening_delivered:
				issue(
					"accepted_required_qty",
					f">= opening_delivered_qty ({opening_delivered:g})",
					accepted_values["accepted_required_qty"],
				)
			if accepted_values["accepted_delivered_qty"] + QTY_TOLERANCE < opening_delivered:
				issue(
					"accepted_delivered_qty",
					f">= opening_delivered_qty ({opening_delivered:g})",
					accepted_values["accepted_delivered_qty"],
				)
			if (
				accepted_values["accepted_delivered_qty"]
				> accepted_values["accepted_required_qty"] + QTY_TOLERANCE
			):
				issue(
					"accepted_delivered_qty",
					f"<= accepted_required_qty ({accepted_values['accepted_required_qty']:g})",
					accepted_values["accepted_delivered_qty"],
				)
			expected_accepted_source_open = max(
				accepted_values["accepted_required_qty"] - opening_delivered,
				0,
			)
			expected_accepted_current_open = max(
				accepted_values["accepted_required_qty"]
				- accepted_values["accepted_delivered_qty"],
				0,
			)
			if abs(
				accepted_values["accepted_source_open_qty"] - expected_accepted_source_open
			) > QTY_TOLERANCE:
				issue(
					"accepted_source_open_qty",
					expected_accepted_source_open,
					accepted_values["accepted_source_open_qty"],
				)
			if abs(
				accepted_values["accepted_current_open_qty"] - expected_accepted_current_open
			) > QTY_TOLERANCE:
				issue(
					"accepted_current_open_qty",
					expected_accepted_current_open,
					accepted_values["accepted_current_open_qty"],
				)

		fulfillment_cap = (
			accepted_values.get("accepted_source_open_qty")
			if has_accepted_epoch
			else source_open
		)
		if (
			row.get("attributed_qty") not in (None, "")
			and flt(row.get("attributed_qty")) > flt(fulfillment_cap) + QTY_TOLERANCE
		):
			issue(
				"attributed_qty",
				f"<= fulfillment cap ({flt(fulfillment_cap):g})",
				row.get("attributed_qty"),
			)
		expected_snapshot_open = (
			accepted_values.get("accepted_current_open_qty")
			if has_accepted_epoch
			else source_open
		)
		if target_name not in source_qty_by_target:
			issue(
				"demand_source_snapshot_json",
				"matching frozen demand source row",
				expected_snapshot_open,
				source=target_name,
			)
		elif abs(source_qty_by_target[target_name] - flt(expected_snapshot_open)) > QTY_TOLERANCE:
			issue(
				"demand_source_snapshot_json",
				flt(expected_snapshot_open),
				source_qty_by_target[target_name],
				source=target_name,
			)
		for source in sources_by_target.get(target_name) or []:
			for source_field, target_field in (
				("source_name", "customer_schedule"),
				("sales_order", "sales_order"),
				("sales_order_item", "sales_order_item"),
			):
				if (source.get(source_field) or "") != (row.get(target_field) or ""):
					issue(
						f"source_{source_field}",
						row.get(target_field) or "",
						source.get(source_field) or "",
						source=target_name,
					)
		valid.append(row)
	expected_target_names = {
		row.get("customer_schedule_item")
		for row in rows
		if isinstance(row, dict) and row.get("customer_schedule_item") and not cint(row.get("retired"))
	}
	actual_source_targets = {name for name in source_qty_by_target if name}
	if actual_source_targets != expected_target_names:
		issue(
			"demand_source_snapshot_json",
			sorted(expected_target_names),
			sorted(actual_source_targets),
			source="customer_schedule_source_target_set",
		)
	if not valid:
		issue("fulfillment_baseline_json", "at least one active complete customer schedule target", "None")
	return valid


def _get_audit_cross_run_fulfillment_claims(audited_run: str) -> dict[str, dict[str, list[str]]]:
	"""Return targets/SO items already owned by another execution-active run."""
	rows = frappe.db.sql(
		"""
		select r.name, r.planning_run, r.fulfillment_baseline_json
		from `tabAPS Schedule Result` r
		inner join `tabAPS Planning Run` planning_run on planning_run.name = r.planning_run
		where r.planning_run != %(audited_run)s
			and planning_run.status in ('Approved', 'Work Order Proposed', 'Shift Proposed', 'Applied', 'Closed')
		""",
		{"audited_run": audited_run},
		as_dict=True,
	)
	claims = {"targets": defaultdict(list), "sales_order_items": defaultdict(list)}
	for row in rows:
		baseline = _parse_fulfillment_baseline(row.get("fulfillment_baseline_json"))
		if not isinstance(baseline, dict):
			continue
		for target in baseline.get("targets") or []:
			if isinstance(target, dict) and target.get("customer_schedule_item") and not cint(target.get("retired")):
				claims["targets"][target["customer_schedule_item"]].append(row.name)
		for source in baseline.get("sales_order_items") or []:
			if isinstance(source, dict) and source.get("sales_order_item"):
				claims["sales_order_items"][source["sales_order_item"]].append(row.name)
	return {
		"targets": dict(claims["targets"]),
		"sales_order_items": dict(claims["sales_order_items"]),
	}


def _get_audit_expected_delivery_by_result(
	result_rows,
	*,
	delivery: dict[str, Any],
	differences: _AuditDifferenceCollector,
	audited_run: str | None = None,
) -> dict[str, float]:
	"""Attribute live delivery once using frozen openings and exact demand caps."""
	expected = {row.name: 0.0 for row in result_rows}
	baseline_targets_by_result = {
		row.name: _get_audit_validated_schedule_targets(row, differences=differences) for row in result_rows
	}
	_validate_audit_accepted_epoch_provenance(
		result_rows,
		audited_run=audited_run,
		differences=differences,
	)
	target_names = sorted(
		{
			target.get("customer_schedule_item")
			for targets in baseline_targets_by_result.values()
			for target in targets
			if target.get("customer_schedule_item")
		}
	)
	live_targets = _get_audit_live_schedule_targets(target_names)
	cross_run_claims = (
		_get_audit_cross_run_fulfillment_claims(audited_run)
		if audited_run and result_rows
		else {"targets": {}, "sales_order_items": {}}
	)
	claimed_targets: dict[str, str] = {}
	for result in result_rows:
		for target in baseline_targets_by_result[result.name]:
			target_name = target.get("customer_schedule_item")
			if not target_name:
				continue
			if target_name in claimed_targets:
				differences.append(
					_audit_difference(
						doctype="Customer Delivery Schedule Item",
						name=target_name,
						fieldname="schedule_result_claim_count",
						expected=claimed_targets[target_name],
						actual=result.name,
						source="duplicate_fulfillment_target",
					)
				)
				continue
			claimed_targets[target_name] = result.name
			if cross_run_claims["targets"].get(target_name):
				differences.append(
					_audit_difference(
						doctype="Customer Delivery Schedule Item",
						name=target_name,
						fieldname="active_run_owner",
						expected=audited_run,
						actual=", ".join(cross_run_claims["targets"][target_name]),
						source="cross_run_fulfillment_claim",
					)
				)
			live = live_targets.get(target_name)
			if not live:
				differences.append(
					_audit_difference(
						doctype="Customer Delivery Schedule Item",
						name=target_name,
						fieldname="name",
						expected="existing active schedule target",
						actual="Missing",
						source=result.name,
					)
				)
				continue
			for result_field, live_field in (
				("company", "company"),
				("customer", "customer"),
				("sales_order", "sales_order"),
				("item_code", "item_code"),
			):
				if (result.get(result_field) or "") == (live.get(live_field) or ""):
					continue
				differences.append(
					_audit_difference(
						doctype="Customer Delivery Schedule Item",
						name=target_name,
						fieldname=result_field,
						expected=result.get(result_field) or "",
						actual=live.get(live_field) or "",
						source=result.name,
					)
				)
			if live.get("schedule_status") != "Active" or live.get("item_status") == "Cancelled":
				differences.append(
					_audit_difference(
						doctype="Customer Delivery Schedule Item",
						name=target_name,
						fieldname="status",
						expected="Active",
						actual=live.get("item_status") or live.get("schedule_status") or "",
						source=result.name,
					)
				)
			effective_schedule_date = _get_audit_target_effective_schedule_date(target)
			if effective_schedule_date and getdate(effective_schedule_date) != getdate(
				live.get("schedule_date")
			):
				differences.append(
					_audit_difference(
						doctype="Customer Delivery Schedule Item",
						name=target_name,
						fieldname="schedule_date",
						expected=str(getdate(effective_schedule_date)),
						actual=str(getdate(live.get("schedule_date"))),
						source=result.name,
					)
				)
			required_qty_field = (
				"accepted_required_qty"
				if target.get("accepted_required_qty") not in (None, "")
				else "opening_required_qty"
			)
			if required_qty_field in target:
				_compare_audit_qty(
					differences,
					doctype="Customer Delivery Schedule Item",
					name=target_name,
					fieldname="qty",
					expected=target.get(required_qty_field),
					actual=live.get("qty"),
					source=f"{result.name}:{required_qty_field}",
				)
			physical_delivered = max(flt(delivery["by_target"].get(target_name)), 0)
			_compare_audit_qty(
				differences,
				doctype="Customer Delivery Schedule Item",
				name=target_name,
				fieldname="delivered_qty",
				expected=physical_delivered,
				actual=live.get("delivered_qty"),
				source="effective_delivery_allocations",
			)
			opening_delivered = max(flt(target.get("opening_delivered_qty")), 0)
			if physical_delivered + QTY_TOLERANCE < opening_delivered:
				differences.append(
					_audit_difference(
						doctype="Customer Delivery Schedule Item",
						name=target_name,
						fieldname="opening_delivered_qty",
						expected=opening_delivered,
						actual=physical_delivered,
						source="return_below_frozen_opening_requires_run_rebuild",
					)
				)
			attributed_cap = _get_audit_target_attributed_cap(target)
			expected[result.name] += min(
				max(physical_delivered - opening_delivered, 0),
				attributed_cap,
			)

	backlog_expected = _get_audit_backlog_delivery_by_result(
		[
			row
			for row in result_rows
			if row.get("demand_source") == "Sales Order Backlog"
			and not baseline_targets_by_result[row.name]
		],
		delivery=delivery,
		differences=differences,
		cross_run_claims=cross_run_claims.get("sales_order_items") or {},
	)
	expected.update(backlog_expected)
	return expected


def _validate_audit_accepted_epoch_provenance(
	result_rows,
	*,
	audited_run: str | None,
	differences: _AuditDifferenceCollector,
) -> None:
	"""Anchor every accepted demand epoch in immutable external Apply evidence."""
	claims: dict[str, dict[str, Any]] = {}
	accepted_fields = (
		"accepted_required_qty",
		"accepted_delivered_qty",
		"accepted_source_open_qty",
		"accepted_current_open_qty",
		"accepted_schedule_date",
		"accepted_source_demand_delta",
		"accepted_by_change_request",
	)

	def issue(result_name, fieldname, expected, actual, *, source="accepted_epoch_provenance"):
		differences.append(
			_audit_difference(
				doctype="APS Schedule Result",
				name=result_name,
				fieldname=fieldname,
				expected=expected,
				actual=actual,
				source=source,
			)
		)

	for result in result_rows:
		baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
		if not isinstance(baseline, dict):
			continue
		active_targets = [
			target
			for target in baseline.get("targets") or []
			if isinstance(target, dict) and not cint(target.get("retired"))
		]
		accepted_targets = [
			target
			for target in active_targets
			if any(target.get(fieldname) not in (None, "") for fieldname in accepted_fields)
		]
		top_has_accepted_epoch = any(
			baseline.get(fieldname) not in (None, "")
			for fieldname in ("accepted_source_demand_delta", "accepted_by_change_request")
		)
		if not accepted_targets and not top_has_accepted_epoch:
			continue
		if top_has_accepted_epoch and not active_targets:
			issue(
				result.name,
				"fulfillment_baseline_json",
				"active accepted targets for top-level accepted provenance",
				"None",
			)
			continue
		if len(accepted_targets) != len(active_targets):
			issue(
				result.name,
				"fulfillment_baseline_json",
				"one complete accepted epoch for every active target",
				"{0} of {1} targets".format(len(accepted_targets), len(active_targets)),
			)
		complete_targets = []
		for target in accepted_targets:
			missing = [fieldname for fieldname in accepted_fields if target.get(fieldname) in (None, "")]
			if missing:
				issue(
					result.name,
					"fulfillment_baseline_json",
					"complete externally anchored accepted epoch",
					"target {0} missing {1}".format(
						target.get("customer_schedule_item") or "-",
						", ".join(missing),
					),
				)
				continue
			complete_targets.append(target)
		if not complete_targets or len(complete_targets) != len(active_targets):
			continue

		change_requests = {target.get("accepted_by_change_request") for target in complete_targets}
		demand_deltas = {target.get("accepted_source_demand_delta") for target in complete_targets}
		if len(change_requests) != 1 or len(demand_deltas) != 1:
			issue(
				result.name,
				"accepted_epoch_provenance",
				"one Change Request and Demand Delta for all active targets",
				{
					"change_requests": sorted(change_requests),
					"demand_deltas": sorted(demand_deltas),
				},
			)
			continue
		change_request = next(iter(change_requests))
		demand_delta = next(iter(demand_deltas))
		for fieldname, expected in (
			("accepted_by_change_request", change_request),
			("accepted_source_demand_delta", demand_delta),
		):
			if (baseline.get(fieldname) or "") != (expected or ""):
				issue(
					result.name,
					fieldname,
					expected,
					baseline.get(fieldname) or "Missing",
				)
				continue
		planning_run = audited_run or result.get("planning_run") or ""
		if not planning_run:
			issue(result.name, "planning_run", "accepted epoch owning run", "Missing")
			continue
		claims[result.name] = {
			"result": result,
			"baseline": baseline,
			"planning_run": planning_run,
			"change_request": change_request,
			"demand_delta": demand_delta,
		}

	if not claims:
		return

	change_request_names = sorted({claim["change_request"] for claim in claims.values()})
	change_request_rows = frappe.get_all(
		"APS Change Request",
		filters={"name": ("in", tuple(change_request_names))},
		fields=[
			"name",
			"status",
			"planning_run",
			"target_result",
			"source_demand_delta",
			"application_log",
			"application_fingerprint",
			"analysis_fingerprint",
			"apply_count",
			"proposal_json",
			"after_snapshot_json",
		],
	)
	requests_by_name: dict[str, list[Any]] = defaultdict(list)
	for row in change_request_rows:
		requests_by_name[row.name].append(row)
	application_log_rows_by_request = frappe.get_all(
		"APS Change Application Log",
		filters={"change_request": ("in", tuple(change_request_names))},
		fields=[
			"name",
			"change_request",
			"planning_run",
			"application_fingerprint",
			"analysis_fingerprint",
			"proposal_json",
			"after_snapshot_hash",
			"after_snapshot_json",
		],
	)
	claimed_fingerprints = sorted(
		{
			row.get("application_fingerprint")
			for row in change_request_rows
			if row.get("application_fingerprint")
		}
	)
	application_log_rows_by_fingerprint = (
		frappe.get_all(
			"APS Change Application Log",
			filters={"application_fingerprint": ("in", tuple(claimed_fingerprints))},
			fields=[
				"name",
				"change_request",
				"planning_run",
				"application_fingerprint",
				"analysis_fingerprint",
				"proposal_json",
				"after_snapshot_hash",
				"after_snapshot_json",
			],
		)
		if claimed_fingerprints
		else []
	)
	application_log_rows_by_name = {
		row.name: row
		for row in (*application_log_rows_by_request, *application_log_rows_by_fingerprint)
	}
	logs_by_request: dict[str, list[Any]] = defaultdict(list)
	fingerprint_owners: dict[str, list[str]] = defaultdict(list)
	for row in application_log_rows_by_name.values():
		logs_by_request[row.change_request].append(row)
		if row.get("application_fingerprint"):
			fingerprint_owners[row.application_fingerprint].append(row.name)

	for result_name, claim in claims.items():
		request_rows = requests_by_name.get(claim["change_request"]) or []
		if len(request_rows) != 1:
			issue(
				result_name,
				"accepted_by_change_request",
				"exactly one persisted Applied APS Change Request",
				len(request_rows),
			)
			continue
		request = request_rows[0]
		for fieldname, expected in (
			("status", "Applied"),
			("planning_run", claim["planning_run"]),
			("target_result", result_name),
			("source_demand_delta", claim["demand_delta"]),
			("apply_count", 1),
		):
			actual = request.get(fieldname)
			if (cint(actual) if fieldname == "apply_count" else (actual or "")) == expected:
				continue
			issue(result_name, fieldname, expected, actual, source=request.name)
		if not request.get("application_log") or not request.get("application_fingerprint"):
			issue(
				result_name,
				"application_fingerprint",
				"non-empty Application Log and fingerprint",
				{
					"application_log": request.get("application_log"),
					"application_fingerprint": request.get("application_fingerprint"),
				},
				source=request.name,
			)
			continue
		proposal = _parse_audit_json_object(request.get("proposal_json"))
		analysis_fingerprint = request.get("analysis_fingerprint") or ""
		engine_version = proposal.get("engine_version") if isinstance(proposal, dict) else None
		if not analysis_fingerprint or not engine_version:
			issue(
				result_name,
				"application_fingerprint",
				"analysis fingerprint and proposal engine version",
				{
					"analysis_fingerprint": analysis_fingerprint or "Missing",
					"engine_version": engine_version or "Missing",
				},
				source=request.name,
			)
		else:
			expected_application_fingerprint = _hash_audit_payload(
				{
					"change_request": request.name,
					"analysis_fingerprint": analysis_fingerprint,
					"engine_version": engine_version,
				}
			)
			if request.get("application_fingerprint") != expected_application_fingerprint:
				issue(
					result_name,
					"application_fingerprint",
					expected_application_fingerprint,
					request.get("application_fingerprint"),
					source=request.name,
				)
		if isinstance(proposal, dict):
			for fieldname, expected in (
				("change_request", request.name),
				("source_demand_delta", claim["demand_delta"]),
			):
				if (proposal.get(fieldname) or "") != (expected or ""):
					issue(
						result_name,
						"proposal_{0}".format(fieldname),
						expected,
						proposal.get(fieldname) or "Missing",
						source=request.name,
					)
		logs = logs_by_request.get(request.name) or []
		if len(logs) != 1:
			issue(
				result_name,
				"application_log",
				"exactly one APS Change Application Log",
				len(logs),
				source=request.name,
			)
			continue
		log = logs[0]
		for fieldname, expected, actual in (
			("application_log", request.get("application_log"), log.name),
			("planning_run", claim["planning_run"], log.get("planning_run")),
			(
				"application_fingerprint",
				request.get("application_fingerprint"),
				log.get("application_fingerprint"),
			),
			("analysis_fingerprint", analysis_fingerprint, log.get("analysis_fingerprint")),
		):
			if (actual or "") != (expected or ""):
				issue(result_name, fieldname, expected, actual, source=log.name)
		fingerprint = request.get("application_fingerprint")
		if len(fingerprint_owners.get(fingerprint) or []) != 1:
			issue(
				result_name,
				"application_fingerprint",
				"one unique Application Log owner",
				fingerprint_owners.get(fingerprint) or [],
				source=log.name,
			)
		log_proposal = _parse_audit_json_object(log.get("proposal_json"))
		if proposal != log_proposal:
			issue(
				result_name,
				"proposal_json",
				"request proposal identical to immutable Application Log proposal",
				"Mismatch or missing",
				source=log.name,
			)

		request_snapshot = _parse_audit_json_object(request.get("after_snapshot_json"))
		log_snapshot = _parse_audit_json_object(log.get("after_snapshot_json"))
		if request_snapshot is None or log_snapshot is None:
			issue(
				result_name,
				"after_snapshot_json",
				"valid matching JSON objects on request and log",
				"Missing or invalid",
				source=log.name,
			)
			continue
		if request_snapshot != log_snapshot:
			issue(
				result_name,
				"after_snapshot_json",
				"request snapshot identical to immutable Application Log snapshot",
				"Mismatch",
				source=log.name,
			)
		if _hash_audit_payload(log_snapshot) != (log.get("after_snapshot_hash") or ""):
			issue(
				result_name,
				"after_snapshot_hash",
				_hash_audit_payload(log_snapshot),
				log.get("after_snapshot_hash") or "Missing",
				source=log.name,
			)
		_validate_audit_accepted_epoch_snapshot(
			result_name=result_name,
			claim=claim,
			snapshot=log_snapshot,
			differences=differences,
			source=log.name,
		)


def _validate_audit_accepted_epoch_snapshot(
	*,
	result_name: str,
	claim: dict[str, Any],
	snapshot: dict[str, Any],
	differences: _AuditDifferenceCollector,
	source: str,
) -> None:
	run = snapshot.get("run")
	if not isinstance(run, dict) or (run.get("name") or "") != claim["planning_run"]:
		differences.append(
			_audit_difference(
				doctype="APS Change Application Log",
				name=source,
				fieldname="after_snapshot_run",
				expected=claim["planning_run"],
				actual=(run or {}).get("name") if isinstance(run, dict) else "Missing",
				source="accepted_epoch_provenance",
			)
		)
	result_snapshots = [
		row for row in snapshot.get("results") or [] if isinstance(row, dict) and row.get("name") == result_name
	]
	if len(result_snapshots) != 1:
		differences.append(
			_audit_difference(
				doctype="APS Change Application Log",
				name=source,
				fieldname="after_snapshot_result",
				expected="exactly one {0}".format(result_name),
				actual=len(result_snapshots),
				source="accepted_epoch_provenance",
			)
		)
		return
	snapshot_result = result_snapshots[0]
	snapshot_baseline = _parse_fulfillment_baseline(snapshot_result.get("fulfillment_baseline_json"))
	if snapshot_baseline != claim["baseline"]:
		differences.append(
			_audit_difference(
				doctype="APS Change Application Log",
				name=source,
				fieldname="after_snapshot_fulfillment_baseline",
				expected="exact accepted Result baseline",
				actual="Mismatch or missing",
				source="accepted_epoch_provenance",
			)
		)
	current_sources = _parse_audit_demand_source_snapshot(
		claim["result"].get("demand_source_snapshot_json")
	)
	snapshot_sources = _parse_audit_demand_source_snapshot(
		snapshot_result.get("demand_source_snapshot_json")
	)
	if snapshot_sources != current_sources:
		differences.append(
			_audit_difference(
				doctype="APS Change Application Log",
				name=source,
				fieldname="after_snapshot_demand_source_snapshot",
				expected="exact accepted Result demand source snapshot",
				actual="Mismatch or missing",
				source="accepted_epoch_provenance",
			)
		)


def _parse_audit_json_object(value) -> dict[str, Any] | None:
	if isinstance(value, dict):
		return value
	if value in (None, ""):
		return None
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError):
		return None
	return parsed if isinstance(parsed, dict) else None


def _hash_audit_payload(value: Any) -> str:
	payload = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_audit_live_schedule_targets(target_names: list[str]) -> dict[str, Any]:
	if not target_names:
		return {}
	rows = frappe.db.sql(
		"""
		select
			i.name, i.parent, i.item_code, i.sales_order, i.schedule_date,
			i.qty, i.delivered_qty, i.status as item_status,
			s.company, ifnull(s.customer, '') as customer,
			s.status as schedule_status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name in %(target_names)s
		""",
		{"target_names": tuple(target_names)},
		as_dict=True,
	)
	return {row.name: row for row in rows}


def _get_audit_target_attributed_cap(target) -> float:
	for fieldname in (
		"accepted_source_open_qty",
		"attributed_qty",
		"source_open_qty",
		"opening_required_qty",
	):
		if target.get(fieldname) not in (None, ""):
			return max(flt(target.get(fieldname)), 0)
	return 0.0


def _get_audit_target_effective_schedule_date(target):
	return target.get("accepted_schedule_date") or target.get("schedule_date")


def _get_audit_backlog_delivery_by_result(
	result_rows,
	*,
	delivery: dict[str, Any],
	differences: _AuditDifferenceCollector,
	cross_run_claims: dict[str, list[str]] | None = None,
) -> dict[str, float]:
	if not result_rows:
		return {}
	grouped: dict[tuple[str, str, str, str, str], list[tuple[Any, dict[str, Any]]]] = defaultdict(list)
	for result in result_rows:
		baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
		matches = [
			row
			for row in ((baseline or {}).get("sales_order_items") or [])
			if isinstance(row, dict)
			and (row.get("sales_order") or "") == (result.get("sales_order") or "")
			and (row.get("sales_order_item") or "") == (result.get("sales_order_item") or "")
			and (row.get("item_code") or "") == (result.get("item_code") or "")
		]
		required_fields = ("source_open_qty", "opening_ordered_qty", "opening_delivered_qty")
		if (
			len(matches) != 1
			or any(matches[0].get(fieldname) in (None, "") for fieldname in required_fields)
		):
			differences.append(
				_audit_difference(
					doctype="APS Schedule Result",
					name=result.name,
					fieldname="sales_order_item_baseline",
						expected=(
							"one exact Sales Order Item baseline with source_open_qty, "
							"opening_ordered_qty and opening_delivered_qty"
						),
					actual=len(matches),
					source=result.get("sales_order_item") or "missing_sales_order_item",
				)
			)
			continue
		baseline_row = matches[0]
		source_open_qty = flt(baseline_row.get("source_open_qty"))
		opening_ordered_qty = flt(baseline_row.get("opening_ordered_qty"))
		opening_delivered_qty = flt(baseline_row.get("opening_delivered_qty"))
		if min(source_open_qty, opening_ordered_qty, opening_delivered_qty) < -QTY_TOLERANCE:
			differences.append(
				_audit_difference(
					doctype="APS Schedule Result",
					name=result.name,
					fieldname="sales_order_item_baseline_qty",
					expected=">= 0",
						actual=[
							baseline_row.get("source_open_qty"),
							baseline_row.get("opening_ordered_qty"),
							baseline_row.get("opening_delivered_qty"),
						],
					source=result.get("sales_order_item") or "missing_sales_order_item",
				)
			)
			continue
		expected_open_qty = max(opening_ordered_qty - opening_delivered_qty, 0)
		if (
			opening_delivered_qty > opening_ordered_qty + QTY_TOLERANCE
			or abs(source_open_qty - expected_open_qty) > QTY_TOLERANCE
		):
			differences.append(
				_audit_difference(
					doctype="APS Schedule Result",
					name=result.name,
					fieldname="sales_order_item_baseline_qty",
					expected=expected_open_qty,
					actual=source_open_qty,
					source=result.get("sales_order_item") or "missing_sales_order_item",
				)
			)
			continue
		key = (
			result.get("company") or "",
			result.get("customer") or "",
			result.get("item_code") or "",
			result.get("sales_order") or "",
			result.get("sales_order_item") or "",
		)
		grouped[key].append((result, baseline_row))
		owners = (cross_run_claims or {}).get(key[4]) or []
		if owners:
			differences.append(
				_audit_difference(
					doctype="Sales Order Item",
					name=key[4],
					fieldname="active_run_owner",
					expected=result.get("planning_run") or "audited run",
					actual=", ".join(owners),
					source="cross_run_fulfillment_claim",
				)
			)
	if not grouped:
		return {}
	result = {}
	for key, claims in grouped.items():
		physical_delivered = max(flt((delivery.get("backlog_by_key") or {}).get(key)), 0)
		claimed_through = 0.0
		for result_row, baseline in claims:
			opening = max(flt(baseline.get("opening_delivered_qty")), 0)
			if physical_delivered + QTY_TOLERANCE < opening:
				differences.append(
					_audit_difference(
						doctype="Sales Order Item",
						name=key[4],
						fieldname="opening_delivered_qty",
						expected=opening,
						actual=physical_delivered,
						source="return_below_frozen_opening_requires_run_rebuild",
					)
				)
				result[result_row.name] = 0.0
				continue
			claim_start = max(claimed_through, opening)
			claimed = min(
				max(physical_delivered - claim_start, 0),
				max(flt(baseline.get("source_open_qty")), 0),
			)
			result[result_row.name] = claimed
			claimed_through = claim_start + claimed
	return result


def _get_audit_customer_schedule_targets(result) -> list[str]:
	return [
		row.get("customer_schedule_item") for row in _get_audit_customer_schedule_target_rows(result)
	]


def _get_audit_customer_schedule_target_rows(result) -> list[dict[str, Any]]:
	baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
	if not isinstance(baseline, dict):
		return []
	return [
		row
		for row in baseline.get("targets") or []
		if isinstance(row, dict) and row.get("customer_schedule_item") and not cint(row.get("retired"))
	]


def _compare_audit_qty(
	differences: list[dict[str, Any]],
	*,
	doctype: str,
	name: str,
	fieldname: str,
	expected,
	actual,
	source: str | None = None,
):
	if abs(flt(actual) - flt(expected)) <= QTY_TOLERANCE:
		return
	differences.append(
		_audit_difference(
			doctype=doctype,
			name=name,
			fieldname=fieldname,
			expected=flt(expected),
			actual=flt(actual),
			source=source,
		)
	)


def _audit_difference(
	*,
	doctype: str,
	name: str,
	fieldname: str,
	expected,
	actual,
	source: str | None = None,
) -> dict[str, Any]:
	difference = flt(actual) - flt(expected) if isinstance(expected, (int, float)) else None
	return {
		"doctype": doctype,
		"name": name,
		"fieldname": fieldname,
		"expected_qty": expected,
		"actual_qty": actual,
		"difference_qty": difference,
		"source": source,
	}


def _compare_qty(errors: list[dict[str, Any]], source: str, fieldname: str, actual, expected):
	if abs(flt(actual) - flt(expected)) <= QTY_TOLERANCE:
		return
	errors.append(
		_error(
			"quantity_mismatch",
			source,
			_("{0} is {1}, expected {2}.", context="Injection APS").format(
				fieldname, flt(actual), flt(expected)
			),
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
