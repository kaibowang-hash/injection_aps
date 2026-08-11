from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime

from injection_aps.injection_aps.doctype.aps_change_request.aps_change_request import (
	CHANGE_TYPES,
	normalize_change_type,
)
from injection_aps.services import consistency, planning


ENGINE_VERSION = 1
QTY_TOLERANCE = 0.000001
TARGET_CHANGE_TYPES = ("Increase Qty", "Decrease Qty", "Cancel", "Pull In", "Push Out")
QUANTITY_CHANGE_TYPES = ("Increase Qty", "Decrease Qty", "Cancel")
DATE_CHANGE_TYPES = ("Pull In", "Push Out")
PROTECTED_ACTUAL_STATUSES = ("Running", "Completed", "Delayed", "Slow Progress", "Overproduced")
FROZEN_SEGMENT_STATUSES = ("Approved", "Work Order Proposed", "Shift Proposed", "Applied", "Completed")
RETAINED_DISPOSITIONS = ("Inventory", "Obsolete Risk", "Pending Negotiation")
DEMAND_DELTA_CHANGE_TYPE_MAP = {
	"Added": "Urgent Order",
	"Appended": "Increase Qty",
	"Increased": "Increase Qty",
	"Reduced": "Decrease Qty",
	"Cancelled": "Cancel",
	"Advanced": "Pull In",
	"Delayed": "Push Out",
}
RESULT_SNAPSHOT_FIELDS = (
	"name",
	"planning_run",
	"company",
	"plant_floor",
	"net_requirement",
	"customer",
	"item_code",
	"requested_date",
	"demand_source",
	"planned_qty",
	"scheduled_qty",
	"machine_scheduled_qty",
	"demand_covered_qty",
	"overproduction_qty",
	"unscheduled_qty",
	"produced_qty",
	"delivered_qty",
	"status",
	"risk_status",
	"projected_completion_time",
	"schedule_delay_minutes",
	"is_urgent",
	"is_locked",
	"is_manual",
)
SEGMENT_SNAPSHOT_FIELDS = (
	"name",
	"parent",
	"workstation",
	"plant_floor",
	"start_time",
	"end_time",
	"planned_qty",
	"sequence_no",
	"lane_key",
	"campaign_key",
	"parallel_group",
	"family_group",
	"segment_kind",
	"primary_item_code",
	"co_product_item_code",
	"setup_minutes",
	"changeover_minutes",
	"mould_reference",
	"segment_status",
	"risk_status",
	"risk_flags",
	"linked_work_order",
	"linked_work_order_scheduling",
	"linked_scheduling_item",
	"actual_status",
	"actual_completed_qty",
	"actual_start_time",
	"actual_end_time",
	"anchor_strength",
	"execution_anchor_source",
	"is_locked",
	"is_manual",
)
RUN_SNAPSHOT_FIELDS = (
	"name",
	"company",
	"status",
	"approval_state",
	"horizon_start",
	"horizon_end",
	"horizon_days",
	"total_net_requirement_qty",
	"total_machine_scheduled_qty",
	"total_demand_covered_qty",
	"total_overproduction_qty",
	"total_unscheduled_qty",
	"total_produced_qty",
	"total_delivered_qty",
	"consistency_status",
)


def analyze_change_request(change_request: str) -> dict[str, Any]:
	doc = _get_locked_change_request(change_request)
	if doc.status not in ("Draft", "Analyzed", "PMC Confirmed", "Approved"):
		frappe.throw(
			_("Only unapplied active change requests can be analyzed. Current status: {0}.").format(
				doc.status
			),
			frappe.ValidationError,
		)
	doc.change_type = normalize_change_type(doc.change_type)
	_validate_request_inputs(doc)
	snapshot_scope = "run" if doc.change_type in ("Increase Qty", "Urgent Order", "Machine Exception") else "target"
	before_snapshot = _capture_plan_snapshot(doc, snapshot_scope)
	analysis = _dispatch_analysis(doc, before_snapshot)
	proposal = analysis["proposal"]
	impact = analysis["impact"]
	proposal["engine_version"] = ENGINE_VERSION
	proposal["snapshot_scope"] = snapshot_scope
	proposal["change_request"] = doc.name
	proposal["change_type"] = doc.change_type
	source_snapshot_hash = _hash_payload(before_snapshot)
	proposal["source_snapshot_hash"] = source_snapshot_hash
	analysis_fingerprint = _hash_payload(
		{
			"engine_version": ENGINE_VERSION,
			"request": _request_fingerprint_payload(doc),
			"source_snapshot_hash": source_snapshot_hash,
			"proposal": proposal,
		}
	)
	application_fingerprint = _hash_payload(
		{
			"change_request": doc.name,
			"analysis_fingerprint": analysis_fingerprint,
			"engine_version": ENGINE_VERSION,
		}
	)

	protection = proposal.get("quantity_protection") or {}
	doc.current_required_date = proposal.get("current_required_date")
	doc.current_planned_qty = flt(proposal.get("current_planned_qty"))
	doc.current_machine_scheduled_qty = flt(protection.get("machine_scheduled_qty"))
	doc.delivered_qty = flt(protection.get("delivered_qty"))
	doc.produced_qty = flt(protection.get("produced_qty"))
	doc.started_locked_qty = flt(protection.get("started_locked_qty"))
	doc.frozen_qty = flt(protection.get("frozen_qty"))
	doc.minimum_retained_qty = flt(protection.get("minimum_retained_qty"))
	doc.cancellable_qty = flt(protection.get("cancellable_qty"))
	doc.retained_excess_qty = flt(protection.get("retained_excess_qty"))
	doc.target_planned_qty = flt(proposal.get("target_planned_qty"))
	doc.item_code = proposal.get("item_code") or doc.item_code
	doc.customer = proposal.get("customer") or doc.customer
	doc.plant_floor = proposal.get("plant_floor") or doc.plant_floor
	doc.status = "Analyzed"
	doc.approval_state = "Pending"
	doc.analysis_revision = cint(doc.analysis_revision) + 1
	doc.analyzed_by = frappe.session.user
	doc.analyzed_on = now_datetime()
	doc.pmc_confirmed_by = None
	doc.pmc_confirmed_on = None
	doc.approved_by = None
	doc.approved_on = None
	doc.applied_by = None
	doc.applied_on = None
	doc.application_log = None
	doc.apply_count = 0
	doc.impact_summary = _build_impact_summary(doc.change_type, impact, proposal)
	doc.impact_json = _json_dumps(impact)
	doc.proposal_json = _json_dumps(proposal)
	doc.before_snapshot_json = _json_dumps(before_snapshot)
	doc.after_snapshot_json = None
	doc.application_result_json = None
	doc.analysis_fingerprint = analysis_fingerprint
	doc.source_snapshot_hash = source_snapshot_hash
	doc.application_fingerprint = application_fingerprint
	doc.flags.change_engine_transition = True
	doc.save(ignore_permissions=True)
	return _analysis_response(doc, impact, proposal)


def confirm_change_request(change_request: str) -> dict[str, Any]:
	doc = _get_locked_change_request(change_request)
	_assert_status(doc, "Analyzed", _("Only an analyzed change request can be confirmed by PMC."))
	proposal = _load_json_object(doc.proposal_json, _("Adjustment proposal", context="Injection APS"))
	if not cint(proposal.get("allowed", 1)):
		frappe.throw(_("This change has blocking impacts and cannot be confirmed."), frappe.ValidationError)
	_validate_retained_disposition(doc, proposal)
	_assert_snapshot_current(doc, proposal)
	doc.status = "PMC Confirmed"
	doc.approval_state = "PMC Confirmed"
	doc.pmc_confirmed_by = frappe.session.user
	doc.pmc_confirmed_on = now_datetime()
	doc.flags.change_engine_transition = True
	doc.save(ignore_permissions=True)
	return _workflow_response(doc)


def approve_change_request(change_request: str) -> dict[str, Any]:
	doc = _get_locked_change_request(change_request)
	_assert_status(doc, "PMC Confirmed", _("PMC confirmation is required before approval."))
	proposal = _load_json_object(doc.proposal_json, _("Adjustment proposal", context="Injection APS"))
	_assert_snapshot_current(doc, proposal)
	doc.status = "Approved"
	doc.approval_state = "Approved"
	doc.approved_by = frappe.session.user
	doc.approved_on = now_datetime()
	doc.flags.change_engine_transition = True
	doc.save(ignore_permissions=True)
	return _workflow_response(doc)


def reject_change_request(change_request: str, reason: str | None = None) -> dict[str, Any]:
	doc = _get_locked_change_request(change_request)
	if doc.status not in ("Analyzed", "PMC Confirmed", "Approved"):
		frappe.throw(_("Only an analyzed, PMC-confirmed, or approved request can be rejected."), frappe.ValidationError)
	doc.status = "Rejected"
	doc.approval_state = "Rejected"
	if (reason or "").strip():
		doc.notes = "\n".join(
			part
			for part in [
				doc.notes,
				_("Rejected: {0}", context="Injection APS").format(reason.strip()),
			]
			if part
		)
	doc.flags.change_engine_transition = True
	doc.save(ignore_permissions=True)
	return _workflow_response(doc)


def apply_change_request(change_request: str) -> dict[str, Any]:
	save_point = "aps_change_apply_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		doc = _get_application_scope_locked_change_request(change_request)
		if doc.status == "Applied":
			result = _load_json_object(
				doc.application_result_json,
				_("Application result", context="Injection APS"),
				allow_empty=True,
			)
			frappe.db.release_savepoint(save_point)
			return {
				**result,
				"change_request": doc.name,
				"status": doc.status,
				"application_log": doc.application_log,
				"idempotent_replay": 1,
			}
		_assert_status(doc, "Approved", _("The change request must be PMC-confirmed and approved before Apply."))
		if not doc.application_fingerprint or not doc.analysis_fingerprint:
			frappe.throw(_("Analyze the request again before Apply."), frappe.ValidationError)
		existing_log = frappe.db.get_value(
			"APS Change Application Log",
			{"application_fingerprint": doc.application_fingerprint},
			"name",
		)
		if existing_log:
			frappe.throw(_("Application fingerprint already belongs to audit log {0}.").format(existing_log))
		proposal = _load_json_object(doc.proposal_json, _("Adjustment proposal", context="Injection APS"))
		_validate_retained_disposition(doc, proposal)
		before_snapshot = _assert_snapshot_current(doc, proposal)
		mutation = _dispatch_apply(doc, proposal)
		_validate_changed_schedule(doc, proposal)
		consistency_summary = consistency.recalculate_plan_consistency(
			doc.planning_run,
			reason="change request {0} applied".format(doc.name),
		)
		if not consistency_summary.get("valid"):
			frappe.throw(
				_("Applied change failed plan consistency with {0} error(s).").format(
					len(consistency_summary.get("errors") or [])
				),
				frappe.ValidationError,
			)
		after_snapshot = _capture_plan_snapshot(doc, proposal.get("snapshot_scope") or "target")
		applied_on = now_datetime()
		application_result = {
			"change_request": doc.name,
			"planning_run": doc.planning_run,
			"change_type": doc.change_type,
			"mutation": mutation,
			"consistency": consistency_summary,
			"application_fingerprint": doc.application_fingerprint,
			"idempotent_replay": 0,
		}
		log_doc = frappe.get_doc(
			{
				"doctype": "APS Change Application Log",
				"change_request": doc.name,
				"planning_run": doc.planning_run,
				"change_type": doc.change_type,
				"application_fingerprint": doc.application_fingerprint,
				"analysis_fingerprint": doc.analysis_fingerprint,
				"analyzed_by": doc.analyzed_by,
				"analyzed_on": doc.analyzed_on,
				"pmc_confirmed_by": doc.pmc_confirmed_by,
				"pmc_confirmed_on": doc.pmc_confirmed_on,
				"approved_by": doc.approved_by,
				"approved_on": doc.approved_on,
				"applied_by": frappe.session.user,
				"applied_on": applied_on,
				"before_snapshot_hash": _hash_payload(before_snapshot),
				"after_snapshot_hash": _hash_payload(after_snapshot),
				"proposal_json": doc.proposal_json,
				"before_snapshot_json": _json_dumps(before_snapshot),
				"after_snapshot_json": _json_dumps(after_snapshot),
				"application_result_json": _json_dumps(application_result),
			}
		).insert(ignore_permissions=True)
		application_result["application_log"] = log_doc.name
		doc.status = "Applied"
		doc.approval_state = "Approved"
		doc.applied_by = frappe.session.user
		doc.applied_on = applied_on
		doc.application_log = log_doc.name
		doc.apply_count = 1
		doc.before_snapshot_json = _json_dumps(before_snapshot)
		doc.after_snapshot_json = _json_dumps(after_snapshot)
		doc.application_result_json = _json_dumps(application_result)
		doc.flags.change_engine_transition = True
		doc.save(ignore_permissions=True)
		frappe.db.release_savepoint(save_point)
		return application_result
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def _get_locked_change_request(change_request: str):
	rows = frappe.db.sql(
		"select name from `tabAPS Change Request` where name = %s for update",
		(change_request,),
	)
	if not rows:
		frappe.throw(
			_("APS Change Request {0} was not found.").format(change_request),
			frappe.DoesNotExistError,
		)
	return frappe.get_doc("APS Change Request", change_request)


def _get_application_scope_locked_change_request(change_request: str):
	"""Serialize plan mutations with one deterministic Run -> Result -> Segment lock order."""
	scope = frappe.db.get_value(
		"APS Change Request",
		change_request,
		["planning_run", "target_result"],
		as_dict=True,
	)
	if not scope:
		frappe.throw(
			_("APS Change Request {0} was not found.").format(change_request),
			frappe.DoesNotExistError,
		)
	run_name = scope.get("planning_run")
	if not run_name:
		frappe.throw(_("A valid Planning Run is required."), frappe.ValidationError)
	locked_run = frappe.db.sql(
		"select name from `tabAPS Planning Run` where name = %s for update",
		(run_name,),
	)
	if not locked_run:
		frappe.throw(_("Planning Run {0} was not found.").format(run_name), frappe.DoesNotExistError)
	result_rows = frappe.db.sql(
		"""
		select name
		from `tabAPS Schedule Result`
		where planning_run = %s
		order by name
		for update
		""",
		(run_name,),
	)
	result_names = [row[0] for row in result_rows]
	if result_names:
		frappe.db.sql(
			"""
			select name
			from `tabAPS Schedule Segment`
			where parenttype = 'APS Schedule Result'
				and parent in %(result_names)s
			order by parent, name
			for update
			""",
			{"result_names": tuple(result_names)},
		)
	locked_request = frappe.db.sql(
		"select name from `tabAPS Change Request` where name = %s for update",
		(change_request,),
	)
	if not locked_request:
		frappe.throw(
			_("APS Change Request {0} was not found.").format(change_request),
			frappe.DoesNotExistError,
		)
	doc = frappe.get_doc("APS Change Request", change_request)
	if doc.planning_run != run_name or doc.target_result != scope.get("target_result"):
		frappe.throw(
			_(
				"Change Request scope changed while Apply was starting. Retry after refreshing the request.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	return doc


def _dispatch_analysis(doc, before_snapshot: dict[str, Any]) -> dict[str, Any]:
	dispatch = {
		"Increase Qty": _analyze_increase,
		"Decrease Qty": _analyze_decrease_or_cancel,
		"Cancel": _analyze_decrease_or_cancel,
		"Pull In": _analyze_date_change,
		"Push Out": _analyze_date_change,
		"Urgent Order": _analyze_urgent_order,
		"Machine Exception": _analyze_machine_exception,
	}
	return dispatch[doc.change_type](doc, before_snapshot)


def _dispatch_apply(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	dispatch = {
		"Increase Qty": _apply_increase,
		"Decrease Qty": _apply_decrease_or_cancel,
		"Cancel": _apply_decrease_or_cancel,
		"Pull In": _apply_date_change,
		"Push Out": _apply_date_change,
		"Urgent Order": _apply_urgent_order,
		"Machine Exception": _apply_machine_exception,
	}
	return dispatch[doc.change_type](doc, proposal)


def _validate_request_inputs(doc):
	if doc.change_type not in CHANGE_TYPES:
		frappe.throw(_("Select a supported change type.", context="Injection APS"), frappe.ValidationError)
	if not doc.planning_run or not frappe.db.exists("APS Planning Run", doc.planning_run):
		frappe.throw(_("A valid Planning Run is required."), frappe.ValidationError)
	if doc.change_type in TARGET_CHANGE_TYPES:
		if not doc.target_result:
			frappe.throw(_("Target Schedule Result is required for {0}.").format(doc.change_type))
		if not frappe.db.exists("APS Schedule Result", doc.target_result):
			frappe.throw(_("Target Schedule Result {0} was not found.").format(doc.target_result))
	if doc.change_type in DATE_CHANGE_TYPES and not doc.required_date:
		frappe.throw(_("New Required Date is required for {0}.").format(doc.change_type))
	if doc.change_type == "Urgent Order":
		if not doc.item_code or not doc.required_date:
			frappe.throw(_("Item Code and New Required Date are required for an urgent order."))
		if _resolve_urgent_qty(doc) <= 0:
			frappe.throw(_("Urgent Order quantity must be greater than zero."))
	if doc.change_type == "Machine Exception":
		if not doc.workstation or not doc.exception_start_time or not doc.exception_end_time:
			frappe.throw(_("Workstation, Exception Start Time, and Exception End Time are required."))
		if get_datetime(doc.exception_end_time) <= get_datetime(doc.exception_start_time):
			frappe.throw(_("Exception End Time must be later than Exception Start Time."))
		if doc.machine_exception_mode == "Downtime" and flt(doc.available_capacity_percent) != 0:
			frappe.throw(_("Downtime must use 0 percent available capacity."), frappe.ValidationError)
		if doc.machine_exception_mode == "Reduced Capacity" and not (0 < flt(doc.available_capacity_percent) < 100):
			frappe.throw(_("Reduced Capacity must be between 1 and 99 percent."), frappe.ValidationError)
	_validate_source_demand_delta(doc)


def _validate_source_demand_delta(doc):
	if not doc.source_demand_delta:
		return
	delta = frappe.db.get_value(
		"APS Demand Delta",
		doc.source_demand_delta,
		["company", "customer", "item_code", "change_type"],
		as_dict=True,
	)
	if not delta:
		frappe.throw(_("Source Demand Delta {0} was not found.").format(doc.source_demand_delta))
	expected_change_type = DEMAND_DELTA_CHANGE_TYPE_MAP.get(delta.change_type)
	if not expected_change_type:
		frappe.throw(
			_("Source Demand Delta {0} has no actionable change.").format(doc.source_demand_delta),
			frappe.ValidationError,
		)
	if doc.change_type != expected_change_type:
		frappe.throw(
			_("Source Demand Delta {0} requires change type {1}, not {2}.").format(
				doc.source_demand_delta,
				expected_change_type,
				doc.change_type,
			),
			frappe.ValidationError,
		)
	for fieldname, label in (("company", _("Company")), ("customer", _("Customer")), ("item_code", _("Item"))):
		delta_value = delta.get(fieldname)
		request_value = doc.get(fieldname)
		if delta_value and request_value and delta_value != request_value:
			frappe.throw(
				_("Source Demand Delta {0} {1} does not match this change request.").format(
					doc.source_demand_delta,
					label,
				),
				frappe.ValidationError,
			)
	if not doc.target_result:
		return
	target = frappe.db.get_value(
		"APS Schedule Result",
		doc.target_result,
		["company", "customer", "item_code"],
		as_dict=True,
	)
	for fieldname, label in (("company", _("Company")), ("customer", _("Customer")), ("item_code", _("Item"))):
		delta_value = delta.get(fieldname)
		target_value = target.get(fieldname) if target else None
		if delta_value and delta_value != target_value:
			frappe.throw(
				_("Source Demand Delta {0} {1} does not match target result {2}.").format(
					doc.source_demand_delta,
					label,
					doc.target_result,
				),
				frappe.ValidationError,
			)


def _analyze_increase(doc, before_snapshot: dict[str, Any]) -> dict[str, Any]:
	result, segments = _target_result_context(doc.target_result)
	current_qty = flt(result.planned_qty)
	target_qty = _resolve_target_qty(doc, current_qty)
	if target_qty <= current_qty + QTY_TOLERANCE:
		frappe.throw(_("Increase Qty target must be greater than current planned quantity {0}.").format(current_qty))
	protection = _calculate_quantity_protection(result, segments, target_qty)
	additional_schedule_qty = max(target_qty - protection["machine_scheduled_qty"], 0)
	capacity = _build_append_capacity_proposal(
		doc=doc,
		item_code=result.item_code,
		customer=result.customer,
		required_date=result.requested_date,
		qty=additional_schedule_qty,
	)
	new_segments = capacity.get("segments") or []
	scheduled_addition_qty = sum(
		flt(row.get("planned_qty"))
		for row in new_segments
		if consistency.is_effective_primary_segment(row)
	)
	projected_machine_qty = protection["machine_scheduled_qty"] + scheduled_addition_qty
	impact_row = _build_target_impact_row(
		result,
		segments,
		new_required_date=result.requested_date,
		new_planned_qty=target_qty,
		new_machine_qty=projected_machine_qty,
		new_completion=_max_datetime([row.get("end_time") for row in [*segments, *new_segments]]),
	)
	proposal = {
		"allowed": 1,
		"target_result": result.name,
		"target_net_requirement": result.net_requirement,
		"item_code": result.item_code,
		"customer": result.customer,
		"plant_floor": result.plant_floor,
		"current_required_date": result.requested_date,
		"current_planned_qty": current_qty,
		"target_planned_qty": target_qty,
		"quantity_delta": target_qty - current_qty,
		"quantity_protection": protection,
		"new_segments": new_segments,
		"segment_actions": [],
		"capacity_exceptions": capacity.get("exceptions") or [],
		"projected_machine_scheduled_qty": projected_machine_qty,
		"projected_unscheduled_qty": max(target_qty - projected_machine_qty, 0),
		"affected_orders": [impact_row],
	}
	impact = {
		"affected_orders": [impact_row],
		"scheduled_addition_qty": scheduled_addition_qty,
		"unscheduled_addition_qty": max(additional_schedule_qty - scheduled_addition_qty, 0),
		"candidate_workstations": capacity.get("candidate_workstations") or [],
		"exceptions": capacity.get("exceptions") or [],
		"suggestions": _build_capacity_suggestions(capacity, impact_rows=[impact_row]),
	}
	return {"proposal": proposal, "impact": impact}


def _analyze_decrease_or_cancel(doc, before_snapshot: dict[str, Any]) -> dict[str, Any]:
	result, segments = _target_result_context(doc.target_result)
	current_qty = flt(result.planned_qty)
	target_qty = 0 if doc.change_type == "Cancel" else _resolve_target_qty(doc, current_qty)
	if doc.change_type == "Decrease Qty" and (target_qty < 0 or target_qty >= current_qty - QTY_TOLERANCE):
		frappe.throw(_("Decrease Qty target must be between zero and current planned quantity {0}.").format(current_qty))
	protection = _calculate_quantity_protection(result, segments, target_qty)
	protection_gap = max(protection["minimum_retained_qty"] - protection["machine_scheduled_qty"], 0)
	desired_machine_qty = max(
		protection["minimum_retained_qty"],
		min(protection["machine_scheduled_qty"], target_qty),
	)
	segment_actions = _build_segment_reduction_actions(
		segments,
		max(protection["machine_scheduled_qty"] - desired_machine_qty, 0),
	)
	projected_machine_qty = protection["machine_scheduled_qty"] - sum(
		flt(row.get("before_qty")) - flt(row.get("after_qty")) for row in segment_actions
	)
	protection["effective_retained_qty"] = projected_machine_qty
	protection["retained_excess_qty"] = max(projected_machine_qty - target_qty, 0)
	protection["scheduled_reduction_qty"] = max(protection["machine_scheduled_qty"] - projected_machine_qty, 0)
	protection["protection_reconciliation_gap"] = protection_gap
	new_completion = _completion_after_segment_actions(segments, segment_actions)
	impact_row = _build_target_impact_row(
		result,
		segments,
		new_required_date=result.requested_date,
		new_planned_qty=target_qty,
		new_machine_qty=projected_machine_qty,
		new_completion=new_completion,
	)
	proposal = {
		"allowed": 0 if protection_gap > QTY_TOLERANCE else 1,
		"target_result": result.name,
		"target_net_requirement": result.net_requirement,
		"item_code": result.item_code,
		"customer": result.customer,
		"plant_floor": result.plant_floor,
		"current_required_date": result.requested_date,
		"current_planned_qty": current_qty,
		"target_planned_qty": target_qty,
		"quantity_delta": target_qty - current_qty,
		"quantity_protection": protection,
		"new_segments": [],
		"segment_actions": segment_actions,
		"projected_machine_scheduled_qty": projected_machine_qty,
		"projected_unscheduled_qty": max(target_qty - projected_machine_qty, 0),
		"projected_overproduction_qty": max(projected_machine_qty - target_qty, 0),
		"affected_orders": [impact_row],
	}
	impact = {
		"affected_orders": [impact_row],
		"quantity_protection": protection,
		"segment_actions": segment_actions,
		"retained_disposition_required": 1 if protection["retained_excess_qty"] > QTY_TOLERANCE else 0,
		"blockers": (
			[
				_("Protected quantity exceeds effective machine scheduling by {0}; reconcile execution and segment quantities before Apply.").format(
					frappe.format(protection_gap, {"fieldtype": "Float"})
				)
			]
			if protection_gap > QTY_TOLERANCE
			else []
		),
		"suggestions": _build_reduction_suggestions(protection, target_qty),
	}
	return {"proposal": proposal, "impact": impact}


def _analyze_date_change(doc, before_snapshot: dict[str, Any]) -> dict[str, Any]:
	result, segments = _target_result_context(doc.target_result)
	current_date = getdate(result.requested_date)
	new_date = getdate(doc.required_date)
	if doc.change_type == "Pull In" and new_date >= current_date:
		frappe.throw(_("Pull In date must be earlier than the current required date {0}.").format(current_date))
	if doc.change_type == "Push Out" and new_date <= current_date:
		frappe.throw(_("Push Out date must be later than the current required date {0}.").format(current_date))
	protection = _calculate_quantity_protection(result, segments, flt(result.planned_qty))
	completion = _result_completion(segments)
	impact_row = _build_target_impact_row(
		result,
		segments,
		new_required_date=new_date,
		new_planned_qty=flt(result.planned_qty),
		new_machine_qty=protection["machine_scheduled_qty"],
		new_completion=completion,
	)
	late_after = bool(completion and completion > planning._get_due_datetime(new_date))
	proposal = {
		"allowed": 1,
		"target_result": result.name,
		"target_net_requirement": result.net_requirement,
		"item_code": result.item_code,
		"customer": result.customer,
		"plant_floor": result.plant_floor,
		"current_required_date": current_date,
		"new_required_date": new_date,
		"current_planned_qty": flt(result.planned_qty),
		"target_planned_qty": flt(result.planned_qty),
		"quantity_protection": protection,
		"segment_actions": [],
		"new_segments": [],
		"affected_orders": [impact_row],
		"late_after_change": 1 if late_after else 0,
	}
	impact = {
		"affected_orders": [impact_row],
		"late_after_change": 1 if late_after else 0,
		"suggestions": (
			[_suggest_overtime_or_subcontract(impact_row)] if late_after else [_("No segment movement is required for the new delivery date.")]
		),
	}
	return {"proposal": proposal, "impact": impact}


def _analyze_urgent_order(doc, before_snapshot: dict[str, Any]) -> dict[str, Any]:
	qty = _resolve_urgent_qty(doc)
	urgent = _build_urgent_insertion_proposal(doc, qty)
	proposal = {
		"allowed": 1 if urgent.get("selected_option") else 0,
		"target_result": None,
		"target_net_requirement": None,
		"item_code": doc.item_code,
		"customer": doc.customer,
		"plant_floor": (urgent.get("selected_option") or {}).get("plant_floor") or doc.plant_floor,
		"current_required_date": None,
		"new_required_date": getdate(doc.required_date),
		"current_planned_qty": 0,
		"target_planned_qty": qty,
		"quantity_delta": qty,
		"quantity_protection": _empty_quantity_protection(),
		"new_segments": (urgent.get("selected_option") or {}).get("new_segments") or [],
		"segment_actions": (urgent.get("selected_option") or {}).get("segment_actions") or [],
		"affected_orders": (urgent.get("selected_option") or {}).get("affected_orders") or [],
		"selected_machine_option": urgent.get("selected_option"),
		"machine_options": urgent.get("machine_options") or [],
		"unscheduled_qty": qty if not urgent.get("selected_option") else 0,
	}
	impact = {
		"affected_orders": proposal["affected_orders"],
		"cascading_delay_count": len(proposal["segment_actions"]),
		"additional_mold_changes": (urgent.get("selected_option") or {}).get("additional_mold_changes", 0),
		"freeze_conflicts": (urgent.get("selected_option") or {}).get("freeze_conflicts") or urgent.get("freeze_conflicts") or [],
		"affected_customers": sorted({row.get("customer") for row in proposal["affected_orders"] if row.get("customer")}),
		"alternate_machine_options": urgent.get("machine_options") or [],
		"overtime_suggestion": urgent.get("overtime_suggestion"),
		"subcontract_suggestion": urgent.get("subcontract_suggestion"),
		"exceptions": urgent.get("exceptions") or [],
	}
	return {"proposal": proposal, "impact": impact}


def _analyze_machine_exception(doc, before_snapshot: dict[str, Any]) -> dict[str, Any]:
	window = {
		"name": "PENDING::{0}".format(doc.name),
		"company": doc.company,
		"scope": "Workstation",
		"plant_floor": doc.plant_floor,
		"workstation": doc.workstation,
		"start_time": get_datetime(doc.exception_start_time),
		"end_time": get_datetime(doc.exception_end_time),
		"available_capacity_percent": flt(doc.available_capacity_percent),
		"reason": doc.notes or doc.machine_exception_mode,
		"status": "Draft",
		"planning_run": doc.planning_run,
	}
	preview = planning._build_schedule_impact_preview(
		run_name=doc.planning_run,
		windows_override=[window],
	)
	affected_orders = _build_machine_affected_orders(doc.planning_run, preview.get("proposed_updates") or [])
	proposal = {
		"allowed": preview.get("allowed", 0),
		"target_result": None,
		"target_net_requirement": None,
		"item_code": doc.item_code,
		"customer": doc.customer,
		"plant_floor": doc.plant_floor,
		"current_required_date": None,
		"current_planned_qty": 0,
		"target_planned_qty": 0,
		"quantity_protection": _empty_quantity_protection(),
		"capacity_window": window,
		"segment_actions": [
			{
				"action": "Move",
				"segment_name": row.get("segment_name"),
				"result_name": row.get("result_name"),
				"before_start_time": row.get("old_start_time"),
				"before_end_time": row.get("old_end_time"),
				"after_start_time": row.get("new_start_time"),
				"after_end_time": row.get("new_end_time"),
				"before_qty": row.get("planned_qty"),
				"after_qty": row.get("planned_qty"),
			}
			for row in preview.get("proposed_updates") or []
		],
		"new_segments": [],
		"affected_orders": affected_orders,
		"blockers": preview.get("blockers") or [],
	}
	impact = {
		"affected_orders": affected_orders,
		"cascading_delay_count": len(proposal["segment_actions"]),
		"freeze_conflicts": preview.get("blockers") or [],
		"affected_customers": sorted({row.get("customer") for row in affected_orders if row.get("customer")}),
		"delivery_risks": preview.get("delivery_risks") or [],
		"overtime_suggestion": _build_machine_recovery_suggestion(affected_orders),
		"subcontract_suggestion": _build_machine_subcontract_suggestion(preview),
	}
	return {"proposal": proposal, "impact": impact}


def _apply_increase(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	result_doc = frappe.get_doc("APS Schedule Result", proposal["target_result"])
	result_doc.planned_qty = flt(proposal["target_planned_qty"])
	max_sequence = max([cint(row.sequence_no) for row in result_doc.get("segments") or []] or [0])
	created_segments = []
	for index, row in enumerate(proposal.get("new_segments") or [], start=1):
		values = _prepare_new_change_segment(
			row,
			_("Added by APS Change Request {0}.").format(doc.name),
		)
		values["sequence_no"] = max_sequence + index
		child = result_doc.append("segments", values)
		created_segments.append(child)
	result_doc.is_manual = 1
	result_doc.save(ignore_permissions=True)
	created_segment_names = [row.name for row in created_segments]
	_update_target_net_requirement(result_doc, proposal)
	_reset_run_approval(doc.planning_run)
	return {
		"target_result": result_doc.name,
		"target_planned_qty": flt(proposal["target_planned_qty"]),
		"created_segment_count": len(created_segment_names),
		"created_segments": created_segment_names,
	}


def _apply_decrease_or_cancel(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	result_doc = frappe.get_doc("APS Schedule Result", proposal["target_result"])
	changed_segments = _apply_segment_actions(doc, proposal.get("segment_actions") or [])
	result_doc.reload()
	result_doc.planned_qty = flt(proposal["target_planned_qty"])
	result_doc.is_manual = 1
	result_doc.save(ignore_permissions=True)
	_update_target_net_requirement(result_doc, proposal)
	protection = proposal.get("quantity_protection") or {}
	retained_excess_qty = flt(protection.get("retained_excess_qty"))
	if retained_excess_qty > QTY_TOLERANCE:
		planning._ensure_open_exception(
			planning_run=doc.planning_run,
			severity="Warning",
			exception_type="Cancellation Retained Production",
			message=_("Change Request {0} retains {1} as {2} after the customer reduction.").format(
				doc.name,
				frappe.format(retained_excess_qty, {"fieldtype": "Float"}),
				doc.retained_disposition,
			),
			item_code=result_doc.item_code,
			customer=result_doc.customer,
			source_doctype="APS Change Request",
			source_name=doc.name,
			resolution_hint=_("Track the retained quantity as inventory, obsolete risk, or pending customer negotiation."),
			is_blocking=0,
		)
	_reset_run_approval(doc.planning_run)
	return {
		"target_result": result_doc.name,
		"target_planned_qty": flt(proposal["target_planned_qty"]),
		"changed_segments": changed_segments,
		"retained_excess_qty": retained_excess_qty,
		"retained_disposition": doc.retained_disposition if retained_excess_qty > QTY_TOLERANCE else None,
	}


def _apply_date_change(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	result_doc = frappe.get_doc("APS Schedule Result", proposal["target_result"])
	old_date = result_doc.requested_date
	result_doc.requested_date = getdate(proposal["new_required_date"])
	result_doc.is_manual = 1
	result_doc.save(ignore_permissions=True)
	_update_target_net_requirement(result_doc, proposal)
	_reset_run_approval(doc.planning_run)
	return {
		"target_result": result_doc.name,
		"old_required_date": old_date,
		"new_required_date": result_doc.requested_date,
	}


def _apply_urgent_order(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	if not proposal.get("new_segments"):
		frappe.throw(_("Urgent Order has no schedulable proposal and cannot be applied."), frappe.ValidationError)
	shifted_segments = _apply_segment_actions(doc, proposal.get("segment_actions") or [])
	demand_doc = frappe.get_doc(
		{
			"doctype": "APS Demand Pool",
			"company": doc.company,
			"customer": doc.customer,
			"item_code": doc.item_code,
			"demand_source": "Urgent Order",
			"demand_date": getdate(doc.required_date),
			"qty": flt(proposal["target_planned_qty"]),
			"status": "Planned",
			"priority_score": 1000,
			"is_urgent": 1,
			"source_doctype": "APS Change Request",
			"source_name": doc.name,
			"remark": _("Applied urgent order from APS Change Request {0}.").format(doc.name),
			"is_system_generated": 0,
		}
	).insert(ignore_permissions=True)
	run_doc = frappe.get_doc("APS Planning Run", doc.planning_run)
	net_doc = frappe.get_doc(
		{
			"doctype": "APS Net Requirement",
			"company": doc.company,
			"customer": doc.customer,
			"item_code": doc.item_code,
			"demand_date": getdate(doc.required_date),
			"demand_qty": flt(proposal["target_planned_qty"]),
			"available_stock_qty": 0,
			"open_work_order_qty": 0,
			"existing_work_order_policy": run_doc.existing_work_order_policy,
			"safety_stock_gap_qty": 0,
			"max_stock_qty": 0,
			"overstock_qty": 0,
			"minimum_batch_qty": 0,
			"planning_qty": flt(proposal["target_planned_qty"]),
			"net_requirement_qty": flt(proposal["target_planned_qty"]),
			"reason_text": _("Urgent demand from APS Change Request {0}.").format(doc.name),
			"is_system_generated": 1,
		}
	).insert(ignore_permissions=True)
	segments = []
	for row in proposal.get("new_segments") or []:
		values = _prepare_new_change_segment(
			row,
			_("Urgent insertion from APS Change Request {0}.").format(doc.name),
		)
		segments.append(values)
	completion = _max_datetime([row.get("end_time") for row in segments])
	late = bool(completion and completion > planning._get_due_datetime(doc.required_date))
	result_doc = frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": doc.planning_run,
			"company": doc.company,
			"plant_floor": proposal.get("plant_floor"),
			"net_requirement": net_doc.name,
			"customer": doc.customer,
			"item_code": doc.item_code,
			"requested_date": getdate(doc.required_date),
			"demand_source": "Urgent Order",
			"planned_qty": flt(proposal["target_planned_qty"]),
			"status": "Risk" if late else "Planned",
			"risk_status": "Attention" if late else "Normal",
			"flow_step": "Urgent Change Applied",
			"next_step_hint": "Confirm Run",
			"is_urgent": 1,
			"is_manual": 1,
			"notes": _("Created by APS Change Request {0}.").format(doc.name),
			"segments": segments,
		}
	).insert(ignore_permissions=True)
	_reset_run_approval(doc.planning_run)
	return {
		"demand_pool": demand_doc.name,
		"net_requirement": net_doc.name,
		"created_result": result_doc.name,
		"created_segments": [row.name for row in result_doc.get("segments") or []],
		"shifted_segments": shifted_segments,
	}


def _apply_machine_exception(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	window = proposal.get("capacity_window") or {}
	window_doc = frappe.get_doc(
		{
			"doctype": "APS Downtime Window",
			"company": doc.company,
			"scope": "Workstation",
			"plant_floor": doc.plant_floor,
			"workstation": doc.workstation,
			"start_time": window.get("start_time"),
			"end_time": window.get("end_time"),
			"available_capacity_percent": flt(window.get("available_capacity_percent")),
			"reason": window.get("reason") or doc.machine_exception_mode,
			"status": "Applied",
			"planning_run": doc.planning_run,
			"notes": _("Applied from APS Change Request {0}.").format(doc.name),
		}
	).insert(ignore_permissions=True)
	changed_segments = _apply_segment_actions(doc, proposal.get("segment_actions") or [], downtime_window=window_doc.name)
	doc.downtime_window = window_doc.name
	_reset_run_approval(doc.planning_run)
	return {
		"downtime_window": window_doc.name,
		"available_capacity_percent": flt(window_doc.available_capacity_percent),
		"changed_segments": changed_segments,
	}


def _target_result_context(result_name: str) -> tuple[Any, list[dict[str, Any]]]:
	result = frappe.get_doc("APS Schedule Result", result_name)
	segments = [row.as_dict() for row in result.get("segments") or []]
	segments.sort(key=lambda row: (get_datetime(row.get("start_time")), cint(row.get("sequence_no")), row.get("name") or ""))
	return result, segments


def _calculate_quantity_protection(result, segments: list[dict[str, Any]], target_qty: float) -> dict[str, float]:
	effective_segments = [row for row in segments if consistency.is_effective_primary_segment(row)]
	machine_scheduled_qty = sum(flt(row.get("planned_qty")) for row in effective_segments)
	actual_completed_qty = sum(flt(row.get("actual_completed_qty")) for row in effective_segments)
	produced_qty = max(flt(result.produced_qty), actual_completed_qty)
	delivered_qty = flt(result.delivered_qty)
	started_locked_qty = sum(
		flt(row.get("planned_qty"))
		for row in effective_segments
		if row.get("linked_work_order")
		or row.get("linked_work_order_scheduling")
		or row.get("linked_scheduling_item")
		or row.get("actual_start_time")
		or row.get("actual_end_time")
		or flt(row.get("actual_completed_qty")) > 0
		or row.get("actual_status") in PROTECTED_ACTUAL_STATUSES
	)
	frozen_qty = sum(
		flt(row.get("planned_qty"))
		for row in effective_segments
		if cint(row.get("is_locked")) or row.get("segment_status") in FROZEN_SEGMENT_STATUSES
	)
	minimum_retained_qty = max(delivered_qty, produced_qty, started_locked_qty, frozen_qty)
	return {
		"machine_scheduled_qty": machine_scheduled_qty,
		"delivered_qty": delivered_qty,
		"produced_qty": produced_qty,
		"started_locked_qty": started_locked_qty,
		"frozen_qty": frozen_qty,
		"minimum_retained_qty": minimum_retained_qty,
		"cancellable_qty": max(machine_scheduled_qty - minimum_retained_qty, 0),
		"retained_excess_qty": max(minimum_retained_qty - flt(target_qty), 0),
		"effective_retained_qty": minimum_retained_qty,
	}


def _empty_quantity_protection() -> dict[str, float]:
	return {
		"machine_scheduled_qty": 0,
		"delivered_qty": 0,
		"produced_qty": 0,
		"started_locked_qty": 0,
		"frozen_qty": 0,
		"minimum_retained_qty": 0,
		"cancellable_qty": 0,
		"retained_excess_qty": 0,
		"effective_retained_qty": 0,
	}


def _resolve_target_qty(doc, current_qty: float) -> float:
	target_value = doc.get("target_planned_qty")
	change_qty = flt(doc.qty)
	if doc.change_type == "Increase Qty":
		if target_value not in (None, "") and flt(target_value) > current_qty + QTY_TOLERANCE:
			return flt(target_value)
		if change_qty <= 0:
			frappe.throw(_("Increase Qty requires a positive Requested Change Qty or a larger Target Planned Qty."))
		return current_qty + change_qty
	if doc.change_type == "Decrease Qty":
		if target_value not in (None, "") and 0 <= flt(target_value) < current_qty - QTY_TOLERANCE:
			return flt(target_value)
		if change_qty <= 0:
			frappe.throw(_("Decrease Qty requires a positive Requested Change Qty or a smaller Target Planned Qty."))
		return max(current_qty - change_qty, 0)
	if doc.change_type == "Cancel":
		return 0
	return current_qty


def _resolve_urgent_qty(doc) -> float:
	return flt(doc.target_planned_qty) if flt(doc.target_planned_qty) > 0 else flt(doc.qty)


def _build_segment_reduction_actions(
	segments: list[dict[str, Any]],
	qty_to_remove: float,
) -> list[dict[str, Any]]:
	remaining = max(flt(qty_to_remove), 0)
	actions = []
	for row in sorted(
		[row for row in segments if consistency.is_effective_primary_segment(row)],
		key=lambda item: (get_datetime(item.get("end_time")), cint(item.get("sequence_no"))),
		reverse=True,
	):
		if remaining <= QTY_TOLERANCE:
			break
		if planning._is_segment_execution_protected(row):
			continue
		before_qty = flt(row.get("planned_qty"))
		removed_qty = min(before_qty, remaining)
		after_qty = max(before_qty - removed_qty, 0)
		action = "Cancel" if after_qty <= QTY_TOLERANCE else "Resize"
		after_end_time = row.get("end_time")
		if action == "Resize":
			duration = get_datetime(row.get("end_time")) - get_datetime(row.get("start_time"))
			after_end_time = get_datetime(row.get("start_time")) + duration * (after_qty / before_qty)
		actions.append(
			{
				"action": action,
				"segment_name": row.get("name"),
				"result_name": row.get("parent"),
				"before_start_time": row.get("start_time"),
				"before_end_time": row.get("end_time"),
				"after_start_time": row.get("start_time"),
				"after_end_time": after_end_time,
				"before_qty": before_qty,
				"after_qty": after_qty,
				"removed_qty": removed_qty,
				"workstation": row.get("workstation"),
				"mould_reference": row.get("mould_reference"),
			}
		)
		remaining -= removed_qty
	return actions


def _completion_after_segment_actions(
	segments: list[dict[str, Any]],
	actions: list[dict[str, Any]],
):
	action_map = {row.get("segment_name"): row for row in actions}
	end_times = []
	for segment in segments:
		if not consistency.is_effective_primary_segment(segment):
			continue
		action = action_map.get(segment.get("name"))
		if action and action.get("action") == "Cancel":
			continue
		end_times.append(action.get("after_end_time") if action else segment.get("end_time"))
	return _max_datetime(end_times)


def _result_completion(segments: list[dict[str, Any]]):
	return _max_datetime(
		[row.get("end_time") for row in segments if consistency.is_effective_primary_segment(row)]
	)


def _build_target_impact_row(
	result,
	segments: list[dict[str, Any]],
	new_required_date,
	new_planned_qty: float,
	new_machine_qty: float,
	new_completion,
) -> dict[str, Any]:
	old_completion = _result_completion(segments)
	due_datetime = planning._get_due_datetime(new_required_date)
	delayed_qty = min(flt(new_planned_qty), flt(new_machine_qty)) if new_completion and new_completion > due_datetime else 0
	delay_minutes = (
		max((get_datetime(new_completion) - due_datetime).total_seconds() / 60, 0)
		if new_completion
		else 0
	)
	return {
		"affected_order": result.name,
		"result_name": result.name,
		"item_code": result.item_code,
		"customer": result.customer,
		"old_required_date": result.requested_date,
		"new_required_date": new_required_date,
		"due_date": new_required_date,
		"old_planned_qty": flt(result.planned_qty),
		"new_planned_qty": flt(new_planned_qty),
		"old_machine_scheduled_qty": sum(
			flt(row.get("planned_qty")) for row in segments if consistency.is_effective_primary_segment(row)
		),
		"new_machine_scheduled_qty": flt(new_machine_qty),
		"old_completion_time": old_completion,
		"new_completion_time": new_completion,
		"delayed_qty": delayed_qty,
		"delay_minutes": delay_minutes,
	}


def _build_append_capacity_proposal(
	doc,
	item_code: str,
	customer: str | None,
	required_date,
	qty: float,
) -> dict[str, Any]:
	if flt(qty) <= QTY_TOLERANCE:
		return {"segments": [], "exceptions": [], "candidate_workstations": []}
	run_doc = frappe.get_doc("APS Planning Run", doc.planning_run)
	settings = planning.get_settings_dict()
	selected_plant_floors = planning._get_run_selected_plant_floors(run_doc)
	item_context = planning._get_item_context(item_code, settings)
	capability_rows = planning._get_machine_capability_rows(plant_floors=selected_plant_floors)
	candidates = planning._select_machine_candidates(
		item_code=item_code,
		item_context=item_context,
		capability_rows=capability_rows,
		plant_floors=selected_plant_floors,
	)
	workstation_state = planning._build_workstation_state_map(capability_rows)
	mold_state: dict[str, dict[str, Any]] = {}
	current_segments = [
		row
		for row in planning._get_run_impact_segments(doc.planning_run)
		if consistency.is_effective_primary_segment(row)
	]
	planning._apply_segments_to_planning_state(
		sorted(current_segments, key=lambda row: get_datetime(row.get("end_time"))),
		workstation_state,
		mold_state,
	)
	horizon_start = max(get_datetime(now_datetime()), get_datetime(run_doc.horizon_start or now_datetime()))
	horizon_end = max(
		get_datetime(run_doc.horizon_end or add_days(required_date, 7)),
		get_datetime(add_days(required_date, 7)),
		horizon_start + timedelta(days=1),
	)
	downtime_windows = planning._get_active_downtime_windows(
		company=doc.company,
		plant_floors=selected_plant_floors,
		horizon_start=horizon_start,
		horizon_end=horizon_end,
		run_name=doc.planning_run,
	)
	best = planning._choose_best_slot(
		company=doc.company,
		customer=customer,
		item_code=item_code,
		item_context=item_context,
		qty=qty,
		demand_date=required_date,
		horizon_start=horizon_start,
		horizon_end=horizon_end,
		workstation_state=workstation_state,
		mold_state=mold_state,
		candidates=candidates,
		settings=settings,
		selected_plant_floors=selected_plant_floors,
		downtime_windows=downtime_windows,
	)
	return {
		**best,
		"candidate_workstations": sorted({row.get("workstation") for row in candidates if row.get("workstation")}),
	}


def _build_capacity_suggestions(capacity: dict[str, Any], impact_rows: list[dict[str, Any]]) -> list[str]:
	suggestions = []
	if flt(capacity.get("unscheduled_qty")) > QTY_TOLERANCE:
		suggestions.append(_("Release another qualified machine or use approved subcontract capacity for the unscheduled quantity."))
	if any(flt(row.get("delayed_qty")) > QTY_TOLERANCE for row in impact_rows):
		suggestions.append(_("Evaluate overtime before the required date or negotiate a partial delivery split."))
	if not suggestions:
		suggestions.append(_("The additional quantity fits after the current machine schedule without moving protected work."))
	return suggestions


def _build_reduction_suggestions(protection: dict[str, Any], target_qty: float) -> list[str]:
	retained = flt(protection.get("retained_excess_qty"))
	if retained > QTY_TOLERANCE:
		return [
			_("Stop only the cancellable remainder and classify {0} retained quantity before PMC confirmation.").format(
				frappe.format(retained, {"fieldtype": "Float"})
			)
		]
	return [_("Cancel or resize only unprotected segments until machine scheduling matches the target quantity.")]


def _suggest_overtime_or_subcontract(impact_row: dict[str, Any]) -> str:
	return _("Recover {0} delayed minute(s) with overtime, an alternate machine, partial delivery, or approved subcontracting.").format(
		cint(impact_row.get("delay_minutes"))
	)


def _build_urgent_insertion_proposal(doc, qty: float) -> dict[str, Any]:
	run_doc = frappe.get_doc("APS Planning Run", doc.planning_run)
	settings = planning.get_settings_dict()
	selected_plant_floors = planning._get_run_selected_plant_floors(run_doc)
	item_context = planning._get_item_context(doc.item_code, settings)
	capability_rows = planning._get_machine_capability_rows(plant_floors=selected_plant_floors)
	candidates = planning._select_machine_candidates(
		item_code=doc.item_code,
		item_context=item_context,
		capability_rows=capability_rows,
		plant_floors=selected_plant_floors,
	)
	schedule_rows = _get_run_schedule_rows(doc.planning_run)
	horizon_start = max(get_datetime(now_datetime()), get_datetime(run_doc.horizon_start or now_datetime()))
	horizon_end = max(
		get_datetime(run_doc.horizon_end or add_days(doc.required_date, 14)),
		get_datetime(add_days(doc.required_date, 14)),
		horizon_start + timedelta(days=1),
	)
	downtime_windows = planning._get_active_downtime_windows(
		company=doc.company,
		plant_floors=selected_plant_floors,
		horizon_start=horizon_start,
		horizon_end=horizon_end,
		run_name=doc.planning_run,
	)
	options = []
	exceptions = []
	seen_lanes = set()
	for candidate in candidates:
		lane_key = (candidate.get("workstation"), candidate.get("mould_reference"))
		if lane_key in seen_lanes:
			continue
		seen_lanes.add(lane_key)
		option = _build_urgent_machine_option(
			doc=doc,
			qty=qty,
			candidate=candidate,
			item_context=item_context,
			settings=settings,
			schedule_rows=schedule_rows,
			downtime_windows=downtime_windows,
			horizon_start=horizon_start,
			horizon_end=horizon_end,
		)
		if option.get("allowed"):
			options.append(option)
		else:
			exceptions.extend(option.get("exceptions") or [])
	options.sort(key=lambda row: row.get("score"))
	selected = options[0] if options else None
	option_summaries = [_urgent_option_summary(row, selected=row is selected) for row in options]
	affected_rows = selected.get("affected_orders") if selected else []
	max_delay = max([flt(row.get("delay_minutes")) for row in affected_rows] or [0])
	return {
		"selected_option": selected,
		"machine_options": option_summaries,
		"freeze_conflicts": selected.get("freeze_conflicts") if selected else [],
		"exceptions": exceptions,
		"overtime_suggestion": (
			_("Recover up to {0} delayed minute(s) with an approved overtime window.").format(cint(max_delay))
			if max_delay > 0
			else _("No overtime is required by the selected machine option.")
		),
		"subcontract_suggestion": (
			_("Evaluate subcontracting because no qualified in-house insertion slot is available.")
			if not selected
			else _("Keep subcontracting as a fallback if PMC rejects the selected machine option.")
		),
	}


def _build_urgent_machine_option(
	doc,
	qty: float,
	candidate: dict[str, Any],
	item_context: dict[str, Any],
	settings: dict[str, Any],
	schedule_rows: list[dict[str, Any]],
	downtime_windows: list[dict[str, Any]],
	horizon_start,
	horizon_end,
) -> dict[str, Any]:
	workstation = candidate.get("workstation")
	mould_reference = candidate.get("mould_reference")
	lane_rows = sorted(
		[row for row in schedule_rows if row.get("workstation") == workstation],
		key=lambda row: get_datetime(row.get("start_time")),
	)
	predecessor = None
	for row in lane_rows:
		if get_datetime(row.get("end_time")) <= get_datetime(horizon_start):
			predecessor = row
		else:
			break
	state = {
		"next_available": get_datetime(horizon_start),
		"last_color_code": predecessor.get("color_code") if predecessor else "",
		"last_material_code": predecessor.get("material_code") if predecessor else "",
		"last_mould_reference": predecessor.get("mould_reference") if predecessor else "",
	}
	setup_minutes, setup_exceptions, blocked = planning._estimate_setup_penalty(
		candidate=candidate,
		state=state,
		item_context=item_context,
		settings=settings,
	)
	if blocked:
		return {"allowed": 0, "exceptions": setup_exceptions}
	capacity = planning._estimate_hourly_capacity(candidate=candidate, settings=settings)
	hourly_capacity = flt(capacity.get("hourly_capacity_qty"))
	if hourly_capacity <= 0:
		return {
			"allowed": 0,
			"exceptions": [
				{
					"exception_type": "Missing Capacity",
					"message": _("Workstation {0} has no usable hourly capacity.").format(workstation),
				}
			],
		}
	matching_windows = planning._get_matching_downtime_windows(
		downtime_windows,
		workstation=workstation,
		plant_floor=candidate.get("plant_floor"),
		company=doc.company,
	)
	constraints = []
	for row in schedule_rows:
		if planning._is_segment_execution_protected(row) and row.get("workstation") == workstation:
			constraints.append({**row, "constraint_type": "Frozen Task"})
		elif row.get("mould_reference") == mould_reference and row.get("workstation") != workstation:
			constraints.append({**row, "constraint_type": "Mold Occupied"})
	start_time = get_datetime(horizon_start) + timedelta(minutes=setup_minutes)
	start_time, end_time, conflicts = _fit_urgent_slot(
		start_time=start_time,
		qty=qty,
		hourly_capacity=hourly_capacity,
		downtime_windows=matching_windows,
		constraints=constraints,
		horizon_end=horizon_end,
	)
	if not start_time or not end_time:
		return {
			"allowed": 0,
			"exceptions": [
				{
					"exception_type": "No Insertion Slot",
					"message": _("No safe urgent-order slot is available on {0} inside the planning horizon.").format(workstation),
				}
			],
		}
	new_segment = {
		"workstation": workstation,
		"plant_floor": candidate.get("plant_floor"),
		"start_time": start_time,
		"end_time": end_time,
		"planned_qty": qty,
		"sequence_no": 1,
		"lane_key": candidate.get("lane_key"),
		"campaign_key": planning._build_campaign_key(doc.item_code, mould_reference, workstation),
		"parallel_group": "",
		"family_group": "",
		"segment_kind": "Primary",
		"primary_item_code": doc.item_code,
		"co_product_item_code": "",
		"setup_minutes": setup_minutes,
		"changeover_minutes": setup_minutes,
		"mould_reference": mould_reference,
		"schedule_explanation": _("Urgent insertion on {0}; hourly capacity {1}.").format(
			workstation,
			frappe.format(hourly_capacity, {"fieldtype": "Float"}),
		),
		"manual_change_note": "",
		"risk_flags": "Urgent Order",
		"segment_status": "Planned",
		"anchor_strength": planning.ANCHOR_STRENGTH_SOFT,
		"execution_anchor_source": "APS Change Request",
		"color_code": item_context.get("color_code"),
		"material_code": item_context.get("material_code"),
		"is_locked": 0,
		"is_manual": 1,
		"_output_qty": candidate.get("output_qty"),
		"_output_group": candidate.get("output_group") or "Default",
		"_is_family_mold": cint(candidate.get("is_family_mold")),
	}
	_side_outputs, side_segments, _summary = planning._build_family_side_outputs(doc.item_code, [new_segment])
	segment_actions = _build_cascade_actions(
		insert_end=end_time,
		insert_start=start_time,
		workstation=workstation,
		schedule_rows=schedule_rows,
		downtime_windows=matching_windows,
	)
	affected_orders = _build_affected_orders_from_actions(doc.planning_run, schedule_rows, segment_actions)
	new_order_completion = end_time
	new_order_delay_minutes = max(
		(new_order_completion - planning._get_due_datetime(doc.required_date)).total_seconds() / 60,
		0,
	)
	affected_orders.insert(
		0,
		{
			"affected_order": _("New Urgent Order", context="Injection APS"),
			"result_name": None,
			"item_code": doc.item_code,
			"customer": doc.customer,
			"old_required_date": None,
			"new_required_date": getdate(doc.required_date),
			"due_date": getdate(doc.required_date),
			"old_planned_qty": 0,
			"new_planned_qty": qty,
			"old_machine_scheduled_qty": 0,
			"new_machine_scheduled_qty": qty,
			"old_completion_time": None,
			"new_completion_time": new_order_completion,
			"delayed_qty": qty if new_order_delay_minutes > 0 else 0,
			"delay_minutes": new_order_delay_minutes,
		},
	)
	late_qty = sum(flt(row.get("delayed_qty")) for row in affected_orders)
	additional_mold_changes = 1 if not predecessor or predecessor.get("mould_reference") != mould_reference else 0
	return {
		"allowed": 1,
		"workstation": workstation,
		"plant_floor": candidate.get("plant_floor"),
		"mould_reference": mould_reference,
		"start_time": start_time,
		"completion_time": end_time,
		"scheduled_qty": qty,
		"new_segments": [new_segment, *side_segments],
		"segment_actions": segment_actions,
		"affected_orders": affected_orders,
		"freeze_conflicts": conflicts,
		"additional_mold_changes": additional_mold_changes,
		"setup_minutes": setup_minutes,
		"exceptions": setup_exceptions,
		"score": (
			1 if new_order_delay_minutes > 0 else 0,
			late_qty,
			len(conflicts),
			end_time,
			len(segment_actions),
			setup_minutes,
		),
	}


def _fit_urgent_slot(
	start_time,
	qty: float,
	hourly_capacity: float,
	downtime_windows: list[dict[str, Any]],
	constraints: list[dict[str, Any]],
	horizon_end,
):
	current = get_datetime(start_time)
	conflicts = []
	for _attempt in range(200):
		current = planning._shift_start_past_downtime(current, downtime_windows)
		end_time = planning._estimate_end_for_qty_around_downtime(
			start_time=current,
			qty=qty,
			hourly_capacity_qty=hourly_capacity,
			downtime_windows=downtime_windows,
			horizon_end=horizon_end,
		)
		conflict = next(
			(
				row
				for row in sorted(constraints, key=lambda item: get_datetime(item.get("start_time")))
				if planning._intervals_overlap(current, end_time, row.get("start_time"), row.get("end_time"))
			),
			None,
		)
		if not conflict:
			if end_time <= get_datetime(horizon_end):
				return current, end_time, conflicts
			return None, None, conflicts
		conflicts.append(
			{
				"segment_name": conflict.get("name"),
				"result_name": conflict.get("parent"),
				"workstation": conflict.get("workstation"),
				"start_time": conflict.get("start_time"),
				"end_time": conflict.get("end_time"),
				"reason": conflict.get("constraint_type") or "Protected Segment",
			}
		)
		current = max(current, get_datetime(conflict.get("end_time")))
	return None, None, conflicts


def _build_cascade_actions(
	insert_start,
	insert_end,
	workstation: str,
	schedule_rows: list[dict[str, Any]],
	downtime_windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	lane_rows = sorted(
		[
			row
			for row in schedule_rows
			if row.get("workstation") == workstation and get_datetime(row.get("end_time")) > get_datetime(insert_start)
		],
		key=lambda row: get_datetime(row.get("start_time")),
	)
	protected = [row for row in lane_rows if planning._is_segment_execution_protected(row)]
	cursor = get_datetime(insert_end)
	actions = []
	for row in lane_rows:
		old_start = get_datetime(row.get("start_time"))
		old_end = get_datetime(row.get("end_time"))
		if planning._is_segment_execution_protected(row):
			cursor = max(cursor, old_end)
			continue
		desired_start = max(old_start, cursor)
		if desired_start <= old_start:
			cursor = max(cursor, old_end)
			continue
		duration = old_end - old_start
		new_start, new_end = _fit_fixed_duration(
			start_time=desired_start,
			duration=duration,
			downtime_windows=downtime_windows,
			constraints=[candidate for candidate in protected if candidate.get("name") != row.get("name")],
		)
		actions.append(
			{
				"action": "Move",
				"segment_name": row.get("name"),
				"result_name": row.get("parent"),
				"before_start_time": old_start,
				"before_end_time": old_end,
				"after_start_time": new_start,
				"after_end_time": new_end,
				"before_qty": flt(row.get("planned_qty")),
				"after_qty": flt(row.get("planned_qty")),
				"workstation": row.get("workstation"),
				"mould_reference": row.get("mould_reference"),
			}
		)
		cursor = new_end
	return actions


def _fit_fixed_duration(start_time, duration, downtime_windows, constraints):
	current = get_datetime(start_time)
	for _attempt in range(200):
		current = planning._shift_start_past_downtime(current, downtime_windows)
		end_time = current + duration
		full_downtime = next(
			(
				row
				for row in downtime_windows or []
				if flt(row.get("available_capacity_percent")) <= 0
				and planning._intervals_overlap(current, end_time, row.get("start_time"), row.get("end_time"))
			),
			None,
		)
		constraint = next(
			(
				row
				for row in constraints or []
				if planning._intervals_overlap(current, end_time, row.get("start_time"), row.get("end_time"))
			),
			None,
		)
		conflict = full_downtime or constraint
		if not conflict:
			return current, end_time
		current = max(current, get_datetime(conflict.get("end_time")))
	frappe.throw(_("Unable to find a conflict-free cascade slot."), frappe.ValidationError)


def _urgent_option_summary(option: dict[str, Any], selected: bool = False) -> dict[str, Any]:
	return {
		"selected": 1 if selected else 0,
		"workstation": option.get("workstation"),
		"plant_floor": option.get("plant_floor"),
		"mould_reference": option.get("mould_reference"),
		"start_time": option.get("start_time"),
		"completion_time": option.get("completion_time"),
		"scheduled_qty": option.get("scheduled_qty"),
		"affected_order_count": len(option.get("affected_orders") or []),
		"cascading_delay_count": len(option.get("segment_actions") or []),
		"freeze_conflict_count": len(option.get("freeze_conflicts") or []),
		"additional_mold_changes": option.get("additional_mold_changes") or 0,
		"delayed_qty": sum(flt(row.get("delayed_qty")) for row in option.get("affected_orders") or []),
	}


def _get_run_schedule_rows(run_name: str) -> list[dict[str, Any]]:
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=["name", "item_code", "customer", "requested_date", "planned_qty", "machine_scheduled_qty"],
	)
	result_map = {row.name: row for row in results}
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": ("in", list(result_map) or [""]), "parenttype": "APS Schedule Result"},
		fields=list(SEGMENT_SNAPSHOT_FIELDS),
		order_by="start_time asc, end_time asc",
	)
	rows = []
	for row in segments:
		if not consistency.is_effective_primary_segment(row):
			continue
		parent = result_map.get(row.parent)
		values = dict(row)
		values.update(
			{
				"item_code": parent.item_code if parent else None,
				"customer": parent.customer if parent else None,
				"requested_date": parent.requested_date if parent else None,
				"result_planned_qty": parent.planned_qty if parent else 0,
			}
		)
		rows.append(values)
	return rows


def _build_affected_orders_from_actions(
	run_name: str,
	schedule_rows: list[dict[str, Any]],
	actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	if not actions:
		return []
	action_map = {row.get("segment_name"): row for row in actions}
	result_names = sorted({row.get("result_name") for row in actions if row.get("result_name")})
	result_rows = {
		row.name: row
		for row in frappe.get_all(
			"APS Schedule Result",
			filters={"name": ("in", result_names or [""]), "planning_run": run_name},
			fields=["name", "item_code", "customer", "requested_date", "planned_qty", "machine_scheduled_qty"],
		)
	}
	by_result = defaultdict(list)
	for row in schedule_rows:
		if row.get("parent") in result_names:
			by_result[row.get("parent")].append(row)
	impact_rows = []
	for result_name in result_names:
		result = result_rows.get(result_name)
		if not result:
			continue
		rows = by_result.get(result_name) or []
		old_completion = _max_datetime([row.get("end_time") for row in rows])
		new_completion = _max_datetime(
			[
				(action_map.get(row.get("name")) or {}).get("after_end_time") or row.get("end_time")
				for row in rows
				if (action_map.get(row.get("name")) or {}).get("action") != "Cancel"
			]
		)
		due_datetime = planning._get_due_datetime(result.requested_date)
		delayed_qty = sum(
			flt((action_map.get(row.get("name")) or {}).get("after_qty", row.get("planned_qty")))
			for row in rows
			if get_datetime((action_map.get(row.get("name")) or {}).get("after_end_time") or row.get("end_time")) > due_datetime
		)
		delay_minutes = max((get_datetime(new_completion) - due_datetime).total_seconds() / 60, 0) if new_completion else 0
		impact_rows.append(
			{
				"affected_order": result.name,
				"result_name": result.name,
				"item_code": result.item_code,
				"customer": result.customer,
				"old_required_date": result.requested_date,
				"new_required_date": result.requested_date,
				"due_date": result.requested_date,
				"old_planned_qty": flt(result.planned_qty),
				"new_planned_qty": flt(result.planned_qty),
				"old_machine_scheduled_qty": flt(result.machine_scheduled_qty),
				"new_machine_scheduled_qty": flt(result.machine_scheduled_qty),
				"old_completion_time": old_completion,
				"new_completion_time": new_completion,
				"delayed_qty": delayed_qty,
				"delay_minutes": delay_minutes,
			}
		)
	return impact_rows


def _build_machine_affected_orders(run_name: str, updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
	schedule_rows = _get_run_schedule_rows(run_name)
	actions = [
		{
			"action": "Move",
			"segment_name": row.get("segment_name"),
			"result_name": row.get("result_name"),
			"after_start_time": row.get("new_start_time"),
			"after_end_time": row.get("new_end_time"),
			"after_qty": row.get("planned_qty"),
		}
		for row in updates
	]
	return _build_affected_orders_from_actions(run_name, schedule_rows, actions)


def _build_machine_recovery_suggestion(affected_orders: list[dict[str, Any]]) -> str:
	max_delay = max([flt(row.get("delay_minutes")) for row in affected_orders] or [0])
	if max_delay <= 0:
		return _("No overtime is required by the current capacity-impact proposal.")
	return _("Recover up to {0} delayed minute(s) with overtime after the machine window.").format(cint(max_delay))


def _build_machine_subcontract_suggestion(preview: dict[str, Any]) -> str:
	if preview.get("blockers"):
		return _("Use an alternate machine or approved subcontracting because protected work conflicts with this capacity window.")
	if preview.get("delivery_risks"):
		return _("Compare approved subcontract capacity against the listed delivery-risk rows.")
	return _("Subcontracting is not required by the current proposal.")


def _apply_segment_actions(
	doc,
	actions: list[dict[str, Any]],
	downtime_window: str | None = None,
) -> list[dict[str, Any]]:
	changed = []
	for action in actions:
		segment, result_doc, run_doc = planning._get_segment_with_result(action.get("segment_name"))
		if run_doc.name != doc.planning_run:
			frappe.throw(_("Segment {0} no longer belongs to the selected Planning Run.").format(segment.get("name")))
		if planning._is_segment_execution_protected(segment):
			frappe.throw(
				_("Segment {0} became execution-protected after analysis.").format(segment.get("name")),
				frappe.ValidationError,
			)
		action_name = action.get("action")
		values = {
			"is_manual": 1,
			"manual_change_note": _("Applied by APS Change Request {0}.").format(doc.name),
			"anchor_strength": planning.ANCHOR_STRENGTH_SOFT,
			"execution_anchor_source": "APS Change Request",
		}
		if action_name == "Move":
			values.update(
				{
					"start_time": get_datetime(action.get("after_start_time")),
					"end_time": get_datetime(action.get("after_end_time")),
				}
			)
		elif action_name == "Resize":
			values.update(
				{
					"planned_qty": flt(action.get("after_qty")),
					"end_time": get_datetime(action.get("after_end_time")),
					"segment_kind": "Manual",
				}
			)
		elif action_name == "Cancel":
			values.update(
				{
					"planned_qty": 0,
					"segment_status": "Cancelled",
					"segment_kind": "Manual",
				}
			)
		else:
			frappe.throw(
				_("Unsupported segment action: {0}.", context="Injection APS").format(action_name)
			)
		frappe.db.set_value("APS Schedule Segment", segment.get("name"), values)
		_apply_family_segment_action(segment, action_name, values, action)
		frappe.db.set_value(
			"APS Schedule Result",
			result_doc.name,
			{
				"is_manual": 1,
				"flow_step": "Change Applied - Recalculation Pending",
				"next_step_hint": "Review Plan Consistency",
			},
			update_modified=False,
		)
		adjustment_type = "Downtime Impact" if downtime_window else ("Move" if action_name == "Move" else "Resize")
		planning._record_segment_adjustment(
			adjustment_type,
			run_doc,
			result_doc,
			segment,
			target_start_time=values.get("start_time") or segment.get("start_time"),
			target_end_time=values.get("end_time") or segment.get("end_time"),
			target_qty=values.get("planned_qty") if "planned_qty" in values else segment.get("planned_qty"),
			target_workstation=segment.get("workstation"),
			target_mould_reference=segment.get("mould_reference"),
			downtime_window=downtime_window,
			impact_summary=_("Applied from Change Request {0}.").format(doc.name),
			payload=action,
			status="Applied",
		)
		changed.append(
			{
				"segment_name": segment.get("name"),
				"result_name": result_doc.name,
				"action": action_name,
				"before_qty": flt(segment.get("planned_qty")),
				"after_qty": flt(action.get("after_qty")),
				"before_start_time": segment.get("start_time"),
				"after_start_time": values.get("start_time") or segment.get("start_time"),
				"before_end_time": segment.get("end_time"),
				"after_end_time": values.get("end_time") or segment.get("end_time"),
			}
		)
	return changed


def _apply_family_segment_action(
	primary_segment: dict[str, Any],
	action_name: str,
	primary_values: dict[str, Any],
	action: dict[str, Any],
):
	if not primary_segment.get("family_group"):
		return
	siblings = frappe.get_all(
		"APS Schedule Segment",
		filters={
			"parent": primary_segment.get("parent"),
			"family_group": primary_segment.get("family_group"),
			"segment_kind": "Family Co-Product",
		},
		fields=["name", "planned_qty"],
	)
	before_qty = flt(action.get("before_qty"))
	ratio = flt(action.get("after_qty")) / before_qty if before_qty > 0 else 0
	for sibling in siblings:
		values = {
			"is_manual": 1,
			"manual_change_note": primary_values.get("manual_change_note"),
		}
		if action_name == "Move":
			values.update(
				{
					"start_time": primary_values.get("start_time"),
					"end_time": primary_values.get("end_time"),
				}
			)
		elif action_name == "Resize":
			values.update(
				{
					"planned_qty": flt(sibling.planned_qty) * ratio,
					"end_time": primary_values.get("end_time"),
				}
			)
		elif action_name == "Cancel":
			values.update({"planned_qty": 0, "segment_status": "Cancelled"})
		frappe.db.set_value("APS Schedule Segment", sibling.name, values)


def _clean_segment_values(row: dict[str, Any]) -> dict[str, Any]:
	allowed_fields = {
		"workstation",
		"plant_floor",
		"start_time",
		"end_time",
		"planned_qty",
		"sequence_no",
		"lane_key",
		"campaign_key",
		"parallel_group",
		"family_group",
		"segment_kind",
		"primary_item_code",
		"co_product_item_code",
		"setup_minutes",
		"changeover_minutes",
		"mould_reference",
		"schedule_explanation",
		"manual_change_note",
		"segment_note",
		"original_segment",
		"split_group",
		"split_index",
		"split_reason",
		"risk_status",
		"risk_flags",
		"schedule_delay_minutes",
		"segment_status",
		"anchor_strength",
		"execution_anchor_source",
		"color_code",
		"material_code",
		"is_locked",
		"is_manual",
	}
	values = {fieldname: row.get(fieldname) for fieldname in allowed_fields if fieldname in row}
	values["segment_status"] = values.get("segment_status") or "Planned"
	values["risk_status"] = values.get("risk_status") or "Normal"
	values["segment_kind"] = values.get("segment_kind") or "Primary"
	return values


def _prepare_new_change_segment(row: dict[str, Any], note: str) -> dict[str, Any]:
	values = _clean_segment_values(row)
	if values.get("segment_kind") != "Family Co-Product":
		values["segment_kind"] = "Manual"
	values["is_manual"] = 1
	values["manual_change_note"] = note
	return values


def _update_target_net_requirement(result_doc, proposal: dict[str, Any]):
	net_requirement = proposal.get("target_net_requirement") or result_doc.net_requirement
	if not net_requirement or not frappe.db.exists("APS Net Requirement", net_requirement):
		return None
	values = {
		"demand_qty": flt(proposal.get("target_planned_qty")),
		"planning_qty": flt(proposal.get("target_planned_qty")),
		"net_requirement_qty": flt(proposal.get("target_planned_qty")),
		"reason_text": _("Current plan target applied by APS Change Request {0}.").format(
			proposal.get("change_request") or "-"
		),
	}
	if proposal.get("new_required_date"):
		values["demand_date"] = getdate(proposal.get("new_required_date"))
	frappe.db.set_value("APS Net Requirement", net_requirement, values)
	_sync_customer_schedule_targets_for_change(result_doc, proposal, net_requirement)
	return net_requirement


def _sync_customer_schedule_targets_for_change(result_doc, proposal: dict[str, Any], net_requirement: str) -> list[str]:
	"""Keep controlled Change Request mutations aligned with frozen customer demand."""
	if (result_doc.get("demand_source") or "") != "Customer Delivery Schedule":
		return []
	baseline = _load_customer_schedule_baseline(result_doc.get("fulfillment_baseline_json"))
	targets = [
		row
		for row in baseline.get("targets") or []
		if isinstance(row, dict) and row.get("customer_schedule_item") and not cint(row.get("retired"))
	]
	if not targets:
		return []
	target_qty_by_name = _allocate_changed_target_qty(targets, flt(proposal.get("target_planned_qty")))
	new_date = getdate(proposal.get("new_required_date") or result_doc.get("requested_date"))
	rows = frappe.db.sql(
		"""
		select
			i.name,
			i.delivered_qty,
			s.status as schedule_status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name in %(target_names)s
		for update
		""",
		{"target_names": tuple(sorted(target_qty_by_name))},
		as_dict=True,
	)
	row_by_name = {row.name: row for row in rows}
	updated = []
	for target in targets:
		target_name = target.get("customer_schedule_item")
		if target_name not in target_qty_by_name:
			continue
		current = row_by_name.get(target_name)
		if not current or current.schedule_status != "Active":
			frappe.throw(
				_("Customer schedule target {0} is no longer active. Rebuild the plan before applying this change.").format(
					target_name
				),
				frappe.ValidationError,
			)
		qty = flt(target_qty_by_name[target_name])
		delivered_qty = flt(current.delivered_qty)
		frappe.db.set_value(
			"Customer Delivery Schedule Item",
			target_name,
			{
				"schedule_date": new_date,
				"qty": qty,
				"balance_qty": max(qty - delivered_qty, 0),
				"status": "Cancelled" if qty <= QTY_TOLERANCE else ("Covered" if delivered_qty >= qty else "Open"),
			},
			update_modified=False,
		)
		target["opening_required_qty"] = qty
		target["source_open_qty"] = qty
		target["schedule_date"] = str(new_date)
		updated.append(target_name)
	net_requirement_baseline = baseline.setdefault("net_requirement", {})
	net_requirement_baseline["demand_qty"] = flt(proposal.get("target_planned_qty"))
	net_requirement_baseline["available_stock_qty"] = flt(net_requirement_baseline.get("available_stock_qty"))
	net_requirement_baseline["open_work_order_qty"] = flt(net_requirement_baseline.get("open_work_order_qty"))
	if not net_requirement_baseline.get("existing_work_order_policy"):
		net_requirement_baseline["existing_work_order_policy"] = frappe.db.get_value(
			"APS Planning Run",
			result_doc.planning_run,
			"existing_work_order_policy",
		)
	baseline_json = json.dumps(baseline, ensure_ascii=False, sort_keys=True)
	frappe.db.set_value(
		"APS Schedule Result",
		result_doc.name,
		"fulfillment_baseline_json",
		baseline_json,
		update_modified=False,
	)
	frappe.db.set_value(
		"APS Net Requirement",
		net_requirement,
		"fulfillment_baseline_json",
		baseline_json,
		update_modified=False,
	)
	return updated


def _load_customer_schedule_baseline(value) -> dict[str, Any]:
	if isinstance(value, dict):
		return value
	try:
		baseline = json.loads(value or "{}")
	except (TypeError, ValueError):
		baseline = {}
	return baseline if isinstance(baseline, dict) else {}


def _allocate_changed_target_qty(targets: list[dict[str, Any]], target_qty: float) -> dict[str, float]:
	names = [row.get("customer_schedule_item") for row in targets if row.get("customer_schedule_item")]
	if not names:
		return {}
	target_qty = max(flt(target_qty), 0)
	if len(names) == 1:
		return {names[0]: target_qty}
	basis = [
		max(flt(row.get("source_open_qty") or row.get("opening_required_qty")), 0)
		for row in targets
		if row.get("customer_schedule_item")
	]
	basis_total = sum(basis)
	remaining = target_qty
	allocated: dict[str, float] = {}
	for name, base_qty in zip(names[:-1], basis[:-1], strict=True):
		qty = round(target_qty * base_qty / basis_total, 6) if basis_total > QTY_TOLERANCE else 0
		allocated[name] = max(qty, 0)
		remaining -= allocated[name]
	allocated[names[-1]] = max(remaining, 0)
	return allocated


def _reset_run_approval(run_name: str):
	from injection_aps.services import capacity_balance

	values = {"status": "Planned", "approval_state": "Pending"}
	meta = frappe.get_meta("APS Planning Run")
	if meta.has_field("approved_by"):
		values["approved_by"] = None
	if meta.has_field("approved_on"):
		values["approved_on"] = None
	frappe.db.set_value("APS Planning Run", run_name, values)
	capacity_balance.invalidate_capacity_balance(run_name)


def _validate_changed_schedule(doc, proposal: dict[str, Any]):
	has_new_machine_segments = any(
		(row.get("segment_kind") or "Primary") != "Family Co-Product"
		for row in proposal.get("new_segments") or []
	)
	has_timing_change = any(row.get("action") == "Move" for row in proposal.get("segment_actions") or [])
	if not has_new_machine_segments and not has_timing_change:
		return
	overlap = planning._validate_run_segment_overlaps(doc.planning_run, persist_exceptions=False)
	mold_overlap = planning._validate_run_mold_overlaps(doc.planning_run, persist_exceptions=False)
	messages = list(overlap.get("messages") or []) + list(mold_overlap.get("messages") or [])
	if messages:
		frappe.throw(
			_("Change application would create a schedule conflict:<br>{0}").format("<br>".join(messages[:12])),
			frappe.ValidationError,
		)


def _capture_plan_snapshot(doc, scope: str) -> dict[str, Any]:
	run_row = frappe.db.get_value(
		"APS Planning Run",
		doc.planning_run,
		list(RUN_SNAPSHOT_FIELDS),
		as_dict=True,
	)
	if not run_row:
		frappe.throw(_("Planning Run {0} was not found.").format(doc.planning_run))
	if scope == "target":
		result_names = [doc.target_result]
	else:
		result_names = frappe.get_all(
			"APS Schedule Result",
			filters={"planning_run": doc.planning_run},
			pluck="name",
			order_by="creation asc, name asc",
		)
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"name": ("in", result_names or [""])},
		fields=list(RESULT_SNAPSHOT_FIELDS),
		order_by="creation asc, name asc",
	)
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={
			"parent": ("in", [row.name for row in results] or [""]),
			"parenttype": "APS Schedule Result",
		},
		fields=list(SEGMENT_SNAPSHOT_FIELDS),
		order_by="parent asc, start_time asc, sequence_no asc, name asc",
	)
	net_names = sorted({row.net_requirement for row in results if row.net_requirement})
	net_requirements = frappe.get_all(
		"APS Net Requirement",
		filters={"name": ("in", net_names or [""])},
		fields=[
			"name",
			"company",
			"customer",
			"item_code",
			"demand_date",
			"demand_qty",
			"available_stock_qty",
			"open_work_order_qty",
			"planning_qty",
			"net_requirement_qty",
			"is_system_generated",
		],
		order_by="name asc",
	)
	snapshot = {
		"scope": scope,
		"run": dict(run_row),
		"results": [dict(row) for row in results],
		"segments": [dict(row) for row in segments],
		"net_requirements": [dict(row) for row in net_requirements],
	}
	if scope == "run":
		snapshot["capacity_windows"] = [
			dict(row)
			for row in planning._get_active_downtime_windows(
				company=run_row.company,
				plant_floors=planning._get_run_selected_plant_floors(frappe.get_doc("APS Planning Run", doc.planning_run)),
				horizon_start=run_row.horizon_start,
				horizon_end=run_row.horizon_end,
				run_name=doc.planning_run,
			)
		]
	return _json_normalize(snapshot)


def _assert_snapshot_current(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	scope = proposal.get("snapshot_scope") or "target"
	current_snapshot = _capture_plan_snapshot(doc, scope)
	current_hash = _hash_payload(current_snapshot)
	expected_hash = proposal.get("source_snapshot_hash") or doc.source_snapshot_hash
	if not expected_hash or current_hash != expected_hash:
		frappe.throw(
			_("The plan changed after analysis. Re-analyze this request before confirmation or Apply."),
			frappe.ValidationError,
		)
	return current_snapshot


def _validate_retained_disposition(doc, proposal: dict[str, Any]):
	retained_qty = flt((proposal.get("quantity_protection") or {}).get("retained_excess_qty"))
	if retained_qty <= QTY_TOLERANCE:
		return
	if doc.retained_disposition not in RETAINED_DISPOSITIONS:
		frappe.throw(
			_("Select Inventory, Obsolete Risk, or Pending Negotiation for retained quantity {0}.").format(
				frappe.format(retained_qty, {"fieldtype": "Float"})
			),
			frappe.ValidationError,
		)


def _assert_status(doc, expected_status: str, message: str):
	if doc.status != expected_status:
		frappe.throw(
			_("{0} Current status: {1}.", context="Injection APS").format(message, doc.status),
			frappe.ValidationError,
		)


def _workflow_response(doc) -> dict[str, Any]:
	return {
		"change_request": doc.name,
		"status": doc.status,
		"approval_state": doc.approval_state,
		"pmc_confirmed_by": doc.pmc_confirmed_by,
		"approved_by": doc.approved_by,
		"application_fingerprint": doc.application_fingerprint,
	}


def _analysis_response(doc, impact: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
	return {
		**_workflow_response(doc),
		"analysis_revision": cint(doc.analysis_revision),
		"analysis_fingerprint": doc.analysis_fingerprint,
		"source_snapshot_hash": doc.source_snapshot_hash,
		"allowed": cint(proposal.get("allowed", 1)),
		"quantity_protection": proposal.get("quantity_protection") or {},
		"impact": _json_normalize(impact),
		"proposal": _json_normalize(proposal),
	}


def _build_impact_summary(change_type: str, impact: dict[str, Any], proposal: dict[str, Any]) -> str:
	return _(
		"{0}: {1} affected order(s), {2} segment action(s), target quantity {3}."
	).format(
		change_type,
		len(impact.get("affected_orders") or []),
		len(proposal.get("segment_actions") or []),
		frappe.format(proposal.get("target_planned_qty") or 0, {"fieldtype": "Float"}),
	)


def _request_fingerprint_payload(doc) -> dict[str, Any]:
	return {
		"name": doc.name,
		"planning_run": doc.planning_run,
		"company": doc.company,
		"plant_floor": doc.plant_floor,
		"source_demand_delta": doc.source_demand_delta,
		"change_type": doc.change_type,
		"target_result": doc.target_result,
		"item_code": doc.item_code,
		"customer": doc.customer,
		"required_date": doc.required_date,
		"qty": flt(doc.qty),
		"target_planned_qty": flt(doc.target_planned_qty),
		"machine_exception_mode": doc.machine_exception_mode,
		"workstation": doc.workstation,
		"exception_start_time": doc.exception_start_time,
		"exception_end_time": doc.exception_end_time,
		"available_capacity_percent": flt(doc.available_capacity_percent),
	}


def _load_json_object(value: str | None, label: str, allow_empty: bool = False) -> dict[str, Any]:
	if not value:
		if allow_empty:
			return {}
		frappe.throw(_("{0} is missing. Analyze the request again.").format(label), frappe.ValidationError)
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError) as exc:
		frappe.throw(
			_("{0} is invalid JSON: {1}", context="Injection APS").format(label, exc),
			frappe.ValidationError,
		)
	if not isinstance(parsed, dict):
		frappe.throw(
			_("{0} must be a JSON object.", context="Injection APS").format(label),
			frappe.ValidationError,
		)
	return parsed


def _json_dumps(value: Any) -> str:
	return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, indent=2)


def _json_normalize(value: Any) -> Any:
	return json.loads(json.dumps(value, default=str, ensure_ascii=False, sort_keys=True))


def _hash_payload(value: Any) -> str:
	payload = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _max_datetime(values: list[Any]):
	parsed = [get_datetime(value) for value in values if value]
	return max(parsed) if parsed else None
