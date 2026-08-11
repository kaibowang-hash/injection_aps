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
	"sales_order",
	"sales_order_item",
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
	"demand_source_snapshot_json",
	"fulfillment_baseline_json",
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
NET_REQUIREMENT_SNAPSHOT_FIELDS = (
	"name",
	"company",
	"customer",
	"sales_order",
	"sales_order_item",
	"item_code",
	"demand_date",
	"demand_qty",
	"available_stock_qty",
	"open_work_order_qty",
	"existing_work_order_policy",
	"safety_stock_gap_qty",
	"minimum_batch_qty",
	"planning_qty",
	"net_requirement_qty",
	"demand_source_snapshot_json",
	"fulfillment_baseline_json",
	"is_system_generated",
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
	proposal["source_demand_delta"] = doc.source_demand_delta
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
		existing_log_rows = frappe.db.sql(
			"""
			select name
			from `tabAPS Change Application Log`
			where application_fingerprint = %s
			order by name
			limit 1
			for update
			""",
			(doc.application_fingerprint,),
		)
		existing_log = existing_log_rows[0][0] if existing_log_rows else None
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
	"""Return current rows locked Customer -> Run -> Result -> NR -> Segment -> source -> Request.

	The first Change Request read is deliberately only a routing hint.  Under
	MariaDB REPEATABLE READ it may establish an old consistent-read snapshot, so no
	business value from it is trusted.  Every value used by Apply is returned by a
	locking/current read below; the final request row must still match the hint's
	scope or the caller retries the whole transaction.
	"""
	scope = frappe.db.get_value(
		"APS Change Request",
		change_request,
		["planning_run", "target_result", "customer", "source_demand_delta", "change_type"],
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
	# Customer schedule import, delivery allocation, and Change Request Apply all
	# serialize on the same Customer row.  Acquire it before the Planning Run so a
	# schedule version cannot change between the final snapshot check and mutation.
	customer = scope.get("customer")
	if customer:
		locked_customer = frappe.db.sql(
			"select name from `tabCustomer` where name = %s for update",
			customer,
		)
		if not locked_customer:
			frappe.throw(
				_("Customer {0} was not found.", context="Injection APS").format(customer),
				frappe.DoesNotExistError,
			)
	locked_run = frappe.db.sql(
		"select * from `tabAPS Planning Run` where name = %s for update",
		(run_name,),
		as_dict=True,
	)
	if not locked_run:
		frappe.throw(_("Planning Run {0} was not found.").format(run_name), frappe.DoesNotExistError)
	run_row = frappe._dict(locked_run[0])
	run_plant_floor_rows = frappe.db.sql(
		"""
		select *
		from `tabAPS Planning Run Plant Floor`
		where parent = %s
		order by idx, name
		for update
		""",
		(run_name,),
		as_dict=True,
	)
	result_rows = frappe.db.sql(
		"""
		select *
		from `tabAPS Schedule Result`
		where planning_run = %s
		order by creation, name
		for update
		""",
		(run_name,),
		as_dict=True,
	)
	result_rows = [frappe._dict(row) for row in result_rows]
	result_names = [row.name for row in result_rows]
	net_names = sorted({row.net_requirement for row in result_rows if row.get("net_requirement")})
	net_rows = []
	if net_names:
		net_rows = frappe.db.sql(
			"""
			select *
			from `tabAPS Net Requirement`
			where name in %(net_names)s
			order by name
			for update
			""",
			{"net_names": tuple(net_names)},
			as_dict=True,
		)
	segment_rows = []
	if result_names:
		segment_rows = frappe.db.sql(
			"""
			select *
			from `tabAPS Schedule Segment`
			where parenttype = 'APS Schedule Result'
				and parent in %(result_names)s
			order by parent, start_time, sequence_no, name
			for update
			""",
			{"result_names": tuple(result_names)},
			as_dict=True,
		)

	# Demand Delta and customer schedule rows follow the plan locks and precede
	# the final Request lock.  Lock every schedule item in each referenced header:
	# appended rows may not yet occur in the Result's frozen target list.
	delta_row = None
	if scope.get("source_demand_delta"):
		delta_rows = frappe.db.sql(
			"select * from `tabAPS Demand Delta` where name = %s for update",
			(scope.get("source_demand_delta"),),
			as_dict=True,
		)
		if not delta_rows:
			frappe.throw(
				_("Source Demand Delta {0} was not found.").format(scope.get("source_demand_delta")),
				frappe.DoesNotExistError,
			)
		delta_row = frappe._dict(delta_rows[0])
	schedule_names = {
		target.get("customer_schedule")
		for result in result_rows
		for target in (
			_load_customer_schedule_baseline(result.get("fulfillment_baseline_json")).get("targets") or []
		)
		if isinstance(target, dict) and target.get("customer_schedule")
	}
	if delta_row and delta_row.get("schedule_reference"):
		schedule_names.add(delta_row.schedule_reference)
	schedule_names = sorted(schedule_names)
	schedule_rows = []
	schedule_item_rows = []
	schedule_conditions = []
	schedule_values = {}
	if schedule_names:
		schedule_conditions.append("name in %(schedule_names)s")
		schedule_values["schedule_names"] = tuple(schedule_names)
	if scope.get("source_demand_delta") and scope.get("customer") and run_row.get("company"):
		schedule_conditions.append(
			"(company = %(schedule_company)s and customer = %(schedule_customer)s and status = 'Active')"
		)
		schedule_values.update(
			{
				"schedule_company": run_row.get("company"),
				"schedule_customer": scope.get("customer"),
			}
		)
	if schedule_conditions:
		schedule_rows = frappe.db.sql(
			"""
			select *
			from `tabCustomer Delivery Schedule`
			where {conditions}
			order by name
			for update
			""".format(conditions=" or ".join(schedule_conditions)),
			schedule_values,
			as_dict=True,
		)
		locked_schedule_names = sorted(row.get("name") for row in schedule_rows if row.get("name"))
		if locked_schedule_names:
			schedule_item_rows = frappe.db.sql(
				"""
				select *
				from `tabCustomer Delivery Schedule Item`
				where parent in %(schedule_names)s
				order by parent, idx, name
				for update
				""",
				{"schedule_names": tuple(locked_schedule_names)},
				as_dict=True,
			)
	sales_orders = sorted(
		{
			row.get("sales_order")
			for row in [*result_rows, *([delta_row] if delta_row else [])]
			if row.get("sales_order")
		}
	)
	item_codes = sorted(
		{
			row.get("item_code")
			for row in [*result_rows, *([delta_row] if delta_row else [])]
			if row.get("item_code")
		}
	)
	sales_order_item_rows = []
	if sales_orders and item_codes:
		sales_order_item_rows = frappe.db.sql(
			"""
			select name, parent, item_code, idx
			from `tabSales Order Item`
			where parent in %(sales_orders)s and item_code in %(item_codes)s
			order by parent, idx, name
			for update
			""",
			{"sales_orders": tuple(sales_orders), "item_codes": tuple(item_codes)},
			as_dict=True,
		)
	fulfillment_state = _lock_application_fulfillment_state(
		run_name=run_name,
		company=run_row.get("company"),
		customer=scope.get("customer"),
		item_codes=item_codes,
		result_rows=result_rows,
		segment_rows=segment_rows,
		schedule_item_rows=schedule_item_rows,
		enabled=bool(scope.get("source_demand_delta")),
	)
	selected_plant_floors = planning._coerce_plant_floor_list(
		plant_floors=(run_row.get("selected_plant_floor_summary") or "").split(","),
		plant_floor=run_row.get("plant_floor"),
	)
	downtime_rows = []
	if scope.get("change_type") in ("Increase Qty", "Urgent Order", "Machine Exception"):
		downtime_conditions = [
			"status in %(statuses)s",
			"company = %(company)s",
			"end_time > %(horizon_start)s",
			"start_time < %(horizon_end)s",
			"(ifnull(planning_run, '') = '' or planning_run = %(run_name)s)",
		]
		downtime_values = {
			"statuses": tuple(planning.ACTIVE_DOWNTIME_STATUSES),
			"company": run_row.get("company"),
			"horizon_start": run_row.get("horizon_start"),
			"horizon_end": run_row.get("horizon_end"),
			"run_name": run_name,
		}
		if selected_plant_floors:
			downtime_conditions.append(
				"(ifnull(plant_floor, '') = '' or plant_floor in %(plant_floors)s)"
			)
			downtime_values["plant_floors"] = tuple(selected_plant_floors)
		downtime_rows = frappe.db.sql(
			"""
			select
				name, company, scope, plant_floor, workstation, start_time, end_time,
				available_capacity_percent, reason, status, planning_run
			from `tabAPS Downtime Window`
			where {conditions}
			order by start_time, end_time
			for update
			""".format(conditions=" and ".join(downtime_conditions)),
			downtime_values,
			as_dict=True,
		)
	locked_request = frappe.db.sql(
		"select * from `tabAPS Change Request` where name = %s for update",
		(change_request,),
		as_dict=True,
	)
	if not locked_request:
		frappe.throw(
			_("APS Change Request {0} was not found.").format(change_request),
			frappe.DoesNotExistError,
		)
	request_row = frappe._dict(locked_request[0])
	if (
		request_row.name != change_request
		or request_row.planning_run != run_name
		or request_row.target_result != scope.get("target_result")
		or (request_row.customer or "") != (scope.get("customer") or "")
		or (request_row.source_demand_delta or "") != (scope.get("source_demand_delta") or "")
		or request_row.change_type != scope.get("change_type")
	):
		frappe.throw(
			_(
				"Change Request scope changed while Apply was starting. Retry after refreshing the request.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	doc = frappe.get_doc({"doctype": "APS Change Request", **dict(request_row)})
	locked_state = frappe._dict(
		run=run_row,
		run_plant_floors=[frappe._dict(row) for row in run_plant_floor_rows],
		results=[frappe._dict(row) for row in result_rows],
		net_requirements=[frappe._dict(row) for row in net_rows],
		segments=[frappe._dict(row) for row in segment_rows],
		delta=delta_row,
		schedules=[frappe._dict(row) for row in schedule_rows],
		schedule_items=[frappe._dict(row) for row in schedule_item_rows],
		sales_order_items=[frappe._dict(row) for row in sales_order_item_rows],
		**fulfillment_state,
		capacity_windows=[frappe._dict(row) for row in downtime_rows],
	)
	doc.flags.aps_application_locked_state = locked_state
	return doc


def _lock_application_fulfillment_state(
	*,
	run_name: str,
	company: str | None,
	customer: str | None,
	item_codes: list[str],
	result_rows,
	segment_rows,
	schedule_item_rows,
	enabled: bool,
) -> dict[str, list]:
	"""Lock current fulfillment ledgers and their physical source documents.

	Demand Delta acceptance writes a new fulfillment epoch.  Child-row rollups and
	ordinary RR reads are not authoritative enough for its lower bound, so this
	locks both allocation ledgers and every discoverable live source before the
	final Change Request lock.  The corresponding validators fail closed when an
	eligible submitted source has not reached its ledger yet.
	"""
	empty = {
		"delivery_allocations": [],
		"delivery_notes": [],
		"delivery_note_items": [],
		"production_allocations": [],
		"work_orders": [],
		"stock_entries": [],
		"stock_entry_details": [],
	}
	if not enabled:
		return empty

	delivery_allocations = []
	delivery_notes = []
	delivery_note_items = []
	if company and customer and item_codes:
		delivery_allocations = frappe.db.sql(
			"""
			select *
			from `tabAPS Delivery Allocation`
			where company = %(company)s
				and customer = %(customer)s
				and item_code in %(item_codes)s
			order by source_posting_time, creation, name
			for update
			""",
			{"company": company, "customer": customer, "item_codes": tuple(item_codes)},
			as_dict=True,
		)
		delivery_notes = frappe.db.sql(
			"""
			select *
			from `tabDelivery Note` dn
			where dn.company = %(company)s
				and dn.customer = %(customer)s
				and exists (
					select 1 from `tabDelivery Note Item` dni
					where dni.parent = dn.name and dni.item_code in %(item_codes)s
				)
			order by dn.name
			for update
			""",
			{"company": company, "customer": customer, "item_codes": tuple(item_codes)},
			as_dict=True,
		)
		delivery_note_names = sorted(row.get("name") for row in delivery_notes if row.get("name"))
		if delivery_note_names:
			delivery_note_items = frappe.db.sql(
				"""
				select *
				from `tabDelivery Note Item`
				where parent in %(delivery_notes)s and item_code in %(item_codes)s
				order by parent, idx, name
				for update
				""",
				{"delivery_notes": tuple(delivery_note_names), "item_codes": tuple(item_codes)},
				as_dict=True,
			)

	production_allocations = frappe.db.sql(
		"""
		select *
		from `tabAPS Production Allocation`
		where planning_run = %s
		order by source_posting_time, creation, name
		for update
		""",
		(run_name,),
		as_dict=True,
	)
	linked_work_orders = {
		row.get("linked_work_order")
		for row in segment_rows
		if row.get("linked_work_order")
	} | {
		row.get("work_order")
		for row in production_allocations
		if row.get("work_order")
	}
	work_order_conditions = ["custom_aps_run = %(run_name)s"]
	work_order_values = {"run_name": run_name}
	if linked_work_orders:
		work_order_conditions.append("name in %(work_orders)s")
		work_order_values["work_orders"] = tuple(sorted(linked_work_orders))
	work_orders = frappe.db.sql(
		"""
		select *
		from `tabWork Order`
		where {conditions}
		order by name
		for update
		""".format(conditions=" or ".join(work_order_conditions)),
		work_order_values,
		as_dict=True,
	)
	work_order_names = sorted(row.get("name") for row in work_orders if row.get("name"))
	segment_names = sorted(row.get("name") for row in segment_rows if row.get("name"))
	scheduling_items = sorted(
		{
			value
			for row in [*segment_rows, *production_allocations]
			for value in (row.get("linked_scheduling_item"), row.get("scheduling_item"))
			if value
		}
	)
	wos_names = sorted(
		{
			value
			for row in [*segment_rows, *production_allocations]
			for value in (
				row.get("linked_work_order_scheduling"),
				row.get("work_order_scheduling"),
			)
			if value
		}
	)
	stock_conditions = []
	stock_values = {}
	for fieldname, values, parameter in (
		("work_order", work_order_names, "work_orders"),
		("custom_aps_segment_reference", segment_names, "segments"),
		("custom_aps_scheduling_item", scheduling_items, "scheduling_items"),
		("work_order_scheduling", wos_names, "wos_names"),
	):
		if values:
			stock_conditions.append("{0} in %({1})s".format(fieldname, parameter))
			stock_values[parameter] = tuple(values)
	stock_entries = []
	stock_entry_details = []
	if stock_conditions:
		stock_entries = frappe.db.sql(
			"""
			select *
			from `tabStock Entry`
			where purpose = 'Manufacture' and ({conditions})
			order by name
			for update
			""".format(conditions=" or ".join(stock_conditions)),
			stock_values,
			as_dict=True,
		)
		stock_entry_names = sorted(row.get("name") for row in stock_entries if row.get("name"))
		if stock_entry_names:
			stock_entry_details = frappe.db.sql(
				"""
				select *
				from `tabStock Entry Detail`
				where parent in %(stock_entries)s
				order by parent, idx, name
				for update
				""",
				{"stock_entries": tuple(stock_entry_names)},
				as_dict=True,
			)
	return {
		"delivery_allocations": [frappe._dict(row) for row in delivery_allocations],
		"delivery_notes": [frappe._dict(row) for row in delivery_notes],
		"delivery_note_items": [frappe._dict(row) for row in delivery_note_items],
		"production_allocations": [frappe._dict(row) for row in production_allocations],
		"work_orders": [frappe._dict(row) for row in work_orders],
		"stock_entries": [frappe._dict(row) for row in stock_entries],
		"stock_entry_details": [frappe._dict(row) for row in stock_entry_details],
	}


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


def _get_application_locked_state(doc):
	flags = getattr(doc, "flags", None)
	return flags.get("aps_application_locked_state") if flags else None


def _get_apply_document(doc, doctype: str, name: str):
	"""Hydrate an existing document from the current rows already locked by Apply."""
	state = _get_application_locked_state(doc)
	if not state:
		return frappe.get_doc(doctype, name)
	if doctype == "APS Planning Run":
		row = state.get("run") if state.run.get("name") == name else None
		child_field = "selected_plant_floors"
	elif doctype == "APS Schedule Result":
		row = next((item for item in state.results if item.get("name") == name), None)
		child_field = "segments"
	else:
		row = None
		child_field = None
	if not row:
		frappe.throw(
			_("{0} {1} is outside the locked Apply scope.", context="Injection APS").format(doctype, name),
			frappe.ValidationError,
		)
	payload = {"doctype": doctype, **dict(row)}
	if child_field:
		if doctype == "APS Planning Run":
			payload[child_field] = [dict(row) for row in state.run_plant_floors]
		else:
			payload[child_field] = [
				dict(segment)
				for segment in state.segments
				if segment.get("parent") == name
			]
	locked_doc = frappe.get_doc(payload)
	locked_doc.flags.aps_application_locked_state = state
	return locked_doc


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
	_validate_customer_schedule_change_source(doc)


def _validate_customer_schedule_change_source(doc) -> None:
	"""Do not let a plan-only date edit silently rewrite customer demand lineage.

	Quantity Change Requests may intentionally adjust the production response while
	the frozen customer demand stays unchanged.  A customer delivery date, however,
	is source data: it must first be versioned by Schedule Import & Diff, which emits
	the immutable Demand Delta accepted by this workflow.
	"""
	if doc.change_type not in DATE_CHANGE_TYPES or doc.source_demand_delta or not doc.target_result:
		return
	demand_source = frappe.db.get_value("APS Schedule Result", doc.target_result, "demand_source")
	if demand_source == "Customer Delivery Schedule":
		frappe.throw(
			_(
				"Customer delivery dates must be changed through Schedule Import & Diff first, then applied from its Demand Delta."
			),
			frappe.ValidationError,
		)


def _validate_source_demand_delta(doc):
	if not doc.source_demand_delta:
		return
	delta = frappe.db.get_value(
		"APS Demand Delta",
		doc.source_demand_delta,
		[
			"company",
			"customer",
			"item_code",
			"change_type",
			"schedule_reference",
			"previous_schedule_date",
			"current_schedule_date",
			"previous_qty",
			"current_qty",
			"delta_qty",
			"sales_order",
			"customer_part_no",
		],
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
		if delta_value and delta_value != request_value:
			frappe.throw(
				_("Source Demand Delta {0} {1} does not match this change request.").format(
					doc.source_demand_delta,
					label,
				),
				frappe.ValidationError,
			)
	schedule_reference = delta.get("schedule_reference")
	if schedule_reference:
		schedule = frappe.db.get_value(
			"Customer Delivery Schedule",
			schedule_reference,
			["status", "company", "customer"],
			as_dict=True,
		)
		if not schedule or schedule.get("status") != "Active":
			frappe.throw(
				_(
					"Source Demand Delta {0} no longer belongs to the active customer schedule. Re-import and analyze the latest delta."
				).format(doc.source_demand_delta),
				frappe.ValidationError,
			)
		if (schedule.get("company") or "") != (delta.get("company") or "") or (
			schedule.get("customer") or ""
		) != (delta.get("customer") or ""):
			frappe.throw(
				_("Source Demand Delta {0} has inconsistent customer schedule ownership.").format(
					doc.source_demand_delta
				),
					frappe.ValidationError,
				)
	if not doc.target_result:
		_resolve_exact_demand_sales_lineage(delta, delta_name=doc.source_demand_delta)
		return
	target = frappe.db.get_value(
		"APS Schedule Result",
		doc.target_result,
		["name", "company", "customer", "sales_order", "sales_order_item", "item_code"],
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
	_resolve_exact_demand_sales_lineage(
		delta,
		target=target,
		delta_name=doc.source_demand_delta,
	)


def _resolve_exact_demand_sales_lineage(
	delta,
	*,
	target=None,
	delta_name: str | None = None,
	resolved_sales_order_item: str | None = None,
) -> dict[str, str]:
	"""Resolve one immutable Demand Delta to one exact Sales Order detail.

	Customer schedule rows currently carry a Sales Order header, but not a detail
	name.  Reusing the first compatible detail would silently mix framework-order
	lines.  Therefore the header/item pair must resolve to exactly one detail, and
	an existing Result must already carry that same exact lineage.
	"""
	delta_name = delta_name or (delta.get("name") if delta else None) or "-"
	sales_order = (delta.get("sales_order") if delta else None) or ""
	item_code = (delta.get("item_code") if delta else None) or ""
	if not sales_order or not item_code:
		frappe.throw(
			_(
				"Source Demand Delta {0} must identify an exact Sales Order and Item before it can be applied."
			).format(delta_name),
			frappe.ValidationError,
		)
	sales_order_item = resolved_sales_order_item or planning._resolve_unique_sales_order_item(
		sales_order,
		item_code,
	)
	if not sales_order_item:
		frappe.throw(
			_(
				"Source Demand Delta {0} Sales Order {1} and Item {2} do not resolve to exactly one Sales Order Item."
			).format(delta_name, sales_order, item_code),
			frappe.ValidationError,
		)
	if target is not None:
		target_name = target.get("name") or "-"
		target_sales_order = target.get("sales_order") or ""
		target_sales_order_item = target.get("sales_order_item") or ""
		if target_sales_order != sales_order or target_sales_order_item != sales_order_item:
			frappe.throw(
				_(
					"Source Demand Delta {0} Sales Order lineage does not match target result {1}."
				).format(delta_name, target_name),
				frappe.ValidationError,
			)
	return {
		"sales_order": sales_order,
		"sales_order_item": sales_order_item,
		"item_code": item_code,
	}


def _get_source_demand_delta_sales_lineage(
	delta_name: str,
	*,
	target=None,
	locked_state=None,
) -> dict[str, str]:
	delta = locked_state.get("delta") if locked_state else None
	if delta and delta.get("name") != delta_name:
		delta = None
	if not locked_state:
		delta = frappe.db.get_value(
			"APS Demand Delta",
			delta_name,
			["name", "sales_order", "item_code"],
			as_dict=True,
		)
	if not delta:
		frappe.throw(_("Source Demand Delta {0} was not found.").format(delta_name))
	resolved_sales_order_item = None
	if locked_state:
		matches = [
			row.get("name")
			for row in locked_state.sales_order_items
			if row.get("parent") == delta.get("sales_order")
			and row.get("item_code") == delta.get("item_code")
		]
		if len(matches) != 1:
			frappe.throw(
				_("Source Demand Delta {0} Sales Order and Item do not resolve to exactly one locked Sales Order Item.", context="Injection APS").format(delta_name),
				frappe.ValidationError,
			)
		resolved_sales_order_item = matches[0]
	return _resolve_exact_demand_sales_lineage(
		delta,
		target=target,
		delta_name=delta_name,
		resolved_sales_order_item=resolved_sales_order_item,
	)


def _analyze_increase(doc, before_snapshot: dict[str, Any]) -> dict[str, Any]:
	result, segments = _target_result_context(doc.target_result)
	current_qty = flt(result.planned_qty)
	demand_change = _build_customer_schedule_demand_change(doc, result)
	target_qty = (
		flt(demand_change.get("target_planned_qty"))
		if demand_change
		else _resolve_target_qty(doc, current_qty)
	)
	if target_qty <= current_qty + QTY_TOLERANCE and not demand_change:
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
		"customer_demand_change": demand_change,
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
	demand_change = _build_customer_schedule_demand_change(doc, result)
	target_qty = (
		flt(demand_change.get("target_planned_qty"))
		if demand_change
		else (0 if doc.change_type == "Cancel" else _resolve_target_qty(doc, current_qty))
	)
	if (
		doc.change_type == "Decrease Qty"
		and (target_qty < 0 or target_qty >= current_qty - QTY_TOLERANCE)
		and not demand_change
	):
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
		"customer_demand_change": demand_change,
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
	demand_change = _build_customer_schedule_demand_change(doc, result)
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
		"customer_demand_change": demand_change,
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
	sales_lineage = (
		_get_source_demand_delta_sales_lineage(doc.source_demand_delta)
		if doc.source_demand_delta
		else {}
	)
	proposal = {
		"allowed": 1 if urgent.get("selected_option") else 0,
		"target_result": None,
		"target_net_requirement": None,
		"item_code": doc.item_code,
		"customer": doc.customer,
		"sales_order": sales_lineage.get("sales_order"),
		"sales_order_item": sales_lineage.get("sales_order_item"),
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
	result_doc = _get_apply_document(doc, "APS Schedule Result", proposal["target_result"])
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
	demand_update = _update_target_net_requirement(result_doc, proposal)
	_reset_run_approval(doc.planning_run)
	return {
		"target_result": result_doc.name,
		"target_planned_qty": flt(proposal["target_planned_qty"]),
		"created_segment_count": len(created_segment_names),
		"created_segments": created_segment_names,
		"demand_update": demand_update,
	}


def _apply_decrease_or_cancel(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	result_doc = _get_apply_document(doc, "APS Schedule Result", proposal["target_result"])
	changed_segments = _apply_segment_actions(doc, proposal.get("segment_actions") or [])
	result_doc.planned_qty = flt(proposal["target_planned_qty"])
	result_doc.is_manual = 1
	frappe.db.set_value(
		"APS Schedule Result",
		result_doc.name,
		{"planned_qty": result_doc.planned_qty, "is_manual": 1},
	)
	demand_update = _update_target_net_requirement(result_doc, proposal)
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
		"demand_update": demand_update,
	}


def _apply_date_change(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	result_doc = _get_apply_document(doc, "APS Schedule Result", proposal["target_result"])
	old_date = result_doc.requested_date
	result_doc.requested_date = getdate(proposal["new_required_date"])
	result_doc.is_manual = 1
	result_doc.save(ignore_permissions=True)
	demand_update = _update_target_net_requirement(result_doc, proposal)
	_reset_run_approval(doc.planning_run)
	return {
		"target_result": result_doc.name,
		"old_required_date": old_date,
		"new_required_date": result_doc.requested_date,
		"demand_update": demand_update,
	}


def _apply_urgent_order(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	if not proposal.get("new_segments"):
		frappe.throw(_("Urgent Order has no schedulable proposal and cannot be applied."), frappe.ValidationError)
	if doc.source_demand_delta:
		live_lineage = _get_source_demand_delta_sales_lineage(
			doc.source_demand_delta,
			locked_state=_get_application_locked_state(doc),
		)
		if (
			(proposal.get("sales_order") or "") != live_lineage["sales_order"]
			or (proposal.get("sales_order_item") or "") != live_lineage["sales_order_item"]
		):
			frappe.throw(
				_("Demand Delta Sales Order lineage changed after analysis. Analyze the request again."),
				frappe.ValidationError,
			)
	shifted_segments = _apply_segment_actions(doc, proposal.get("segment_actions") or [])
	demand_doc = frappe.get_doc(
		{
			"doctype": "APS Demand Pool",
			"company": doc.company,
			"customer": doc.customer,
			"sales_order": proposal.get("sales_order"),
			"sales_order_item": proposal.get("sales_order_item"),
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
	run_doc = _get_apply_document(doc, "APS Planning Run", doc.planning_run)
	net_doc = frappe.get_doc(
		{
			"doctype": "APS Net Requirement",
			"company": doc.company,
			"customer": doc.customer,
			"sales_order": proposal.get("sales_order"),
			"sales_order_item": proposal.get("sales_order_item"),
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
			"sales_order": proposal.get("sales_order"),
			"sales_order_item": proposal.get("sales_order_item"),
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


def _get_complete_v4_net_requirement_evidence(baseline: dict[str, Any]) -> dict[str, Any]:
	"""Fail closed when an older Result cannot prove safety/minimum-batch lineage."""
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
	missing = [
		fieldname
		for fieldname in required_fields
		if not isinstance(evidence, dict) or fieldname not in evidence
	]
	if cint(baseline.get("version")) != 4 or missing or cint((evidence or {}).get("formula_version")) != 1:
		frappe.throw(
			_(
				"APS Result has no complete version-4 Net Requirement evidence ({0}). Rebuild the complete Planning Run."
			).format(", ".join(missing) or "formula_version"),
			frappe.ValidationError,
		)
	return evidence


def _build_customer_schedule_demand_change(doc, result) -> dict[str, Any]:
	"""Translate one imported gross-demand Delta into its net production response.

	Customer schedule ``qty`` is gross customer demand.  ``Result.planned_qty`` is
	net production after finite stock/open-WO coverage and may also carry a minimum
	batch.  They must never be copied into each other.  The import is the only writer
	of the source schedule; this context merely freezes the active targets that the
	Change Request is accepting.
	"""
	delta_name = getattr(doc, "source_demand_delta", None)
	if not delta_name or (getattr(result, "demand_source", None) or "") != "Customer Delivery Schedule":
		return {}
	delta = frappe.db.get_value(
		"APS Demand Delta",
		delta_name,
		[
			"name",
			"schedule_reference",
			"change_type",
			"previous_qty",
			"current_qty",
			"delta_qty",
			"previous_schedule_date",
			"current_schedule_date",
			"sales_order",
			"customer_part_no",
		],
		as_dict=True,
	)
	if not delta:
		frappe.throw(_("Source Demand Delta {0} was not found.").format(delta_name), frappe.ValidationError)
	sales_lineage = _resolve_exact_demand_sales_lineage(
		delta,
		target=result,
		delta_name=delta_name,
	)
	target_schedule_date = _resolve_demand_delta_target_date(doc, result, delta)

	baseline = _load_customer_schedule_baseline(getattr(result, "fulfillment_baseline_json", None))
	targets = [
		row
		for row in baseline.get("targets") or []
		if isinstance(row, dict) and row.get("customer_schedule_item")
	]
	active_target_names = {
		row.get("customer_schedule_item")
		for row in targets
		if not cint(row.get("retired")) and row.get("customer_schedule_item")
	}
	# Append keeps the older active schedule and therefore has no replacement row
	# for baseline remapping.  Resolve the newly imported target by the complete
	# Delta identity; never fall back to item-only matching.
	if delta.get("change_type") == "Appended":
		for row in _get_delta_customer_schedule_target_rows(delta, result):
			if row.get("status") != "Cancelled" and flt(row.get("qty")) > QTY_TOLERANCE:
				active_target_names.add(row.get("name"))
	active_target_names = sorted(name for name in active_target_names if name)
	live_rows = _get_customer_schedule_target_rows(active_target_names)
	live_by_name = {row.get("name"): row for row in live_rows}
	missing = sorted(set(active_target_names) - set(live_by_name))
	if missing:
		frappe.throw(
			_("Customer schedule targets changed after import: {0}. Analyze the latest Demand Delta.").format(
				", ".join(missing)
			),
			frappe.ValidationError,
		)
	for row in live_rows:
		if row.get("schedule_status") != "Active":
			frappe.throw(
				_("Customer schedule target {0} is no longer active. Analyze the latest Demand Delta.").format(
					row.get("name")
				),
				frappe.ValidationError,
			)
		for fieldname in ("company", "customer", "item_code"):
			if (row.get(fieldname) or "") != (getattr(result, fieldname, None) or ""):
				frappe.throw(
					_("Customer schedule target {0} no longer matches APS result {1}.").format(
						row.get("name"), result.name
					),
					frappe.ValidationError,
				)
		if (row.get("sales_order") or "") != sales_lineage["sales_order"]:
			frappe.throw(
				_(
					"Customer schedule target {0} Sales Order does not match APS result {1}."
				).format(row.get("name"), result.name),
				frappe.ValidationError,
			)
		if not row.get("schedule_date") or getdate(row.get("schedule_date")) != target_schedule_date:
			frappe.throw(
				_(
					"Customer schedule target {0} date does not match the exact Demand Delta date {1}."
				).format(row.get("name"), target_schedule_date),
				frappe.ValidationError,
			)
		row["sales_order_item"] = sales_lineage["sales_order_item"]

	delivered_by_target = _get_authoritative_target_deliveries(result, active_target_names)
	produced_by_target = _get_authoritative_target_production(result, active_target_names)
	for row in live_rows:
		row["delivered_qty"] = max(flt(delivered_by_target.get(row.get("name"))), 0)
		# The child-row roll-up is only a display cache and may lag execution sync.
		# Gate a customer reduction on the effective production allocation ledger.
		row["produced_qty"] = max(flt(produced_by_target.get(row.get("name"))), 0)
		row["open_qty"] = max(flt(row.get("qty")) - flt(row.get("delivered_qty")), 0)
		row["fulfillment_lower_bound_qty"] = max(
			flt(row.get("delivered_qty")),
			flt(row.get("produced_qty")),
			flt(row.get("allocated_qty")),
		)
		_assert_customer_schedule_target_qty_floor(row)

	evidence = _get_complete_v4_net_requirement_evidence(baseline)
	net_state = _get_exact_customer_change_net_requirement_state(result, sales_lineage)
	for fieldname in (
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"safety_stock_gap_qty",
		"minimum_batch_qty",
	):
		if fieldname in evidence and abs(flt(evidence.get(fieldname)) - flt(net_state.get(fieldname))) > QTY_TOLERANCE:
			frappe.throw(
				_(
					"APS Result and Net Requirement formula evidence differ at {0}. Rebuild the complete Planning Run."
				).format(fieldname),
				frappe.ValidationError,
			)
	if evidence.get("existing_work_order_policy") and (
		evidence.get("existing_work_order_policy") != net_state.get("existing_work_order_policy")
	):
		frappe.throw(
			_("APS Result and Net Requirement Work Order policy differ. Rebuild the complete Planning Run."),
			frappe.ValidationError,
		)
	current_demand_qty = max(flt(net_state.get("demand_qty")), 0)
	target_demand_qty = sum(flt(row.get("open_qty")) for row in live_rows)
	available_stock_qty = max(flt(net_state.get("available_stock_qty")), 0)
	open_work_order_qty = max(flt(net_state.get("open_work_order_qty")), 0)
	existing_work_order_policy = net_state.get("existing_work_order_policy") or ""
	if existing_work_order_policy not in ("Include", "Exclude") or (
		existing_work_order_policy != "Include" and open_work_order_qty > QTY_TOLERANCE
	):
		frappe.throw(
			_("Net Requirement Work Order evidence is invalid. Rebuild the complete Planning Run."),
			frappe.ValidationError,
		)
	credited_work_order_qty = open_work_order_qty if existing_work_order_policy == "Include" else 0
	safety_stock_gap_qty = max(flt(net_state.get("safety_stock_gap_qty")), 0)
	minimum_batch_coverage_qty = max(flt(evidence.get("minimum_batch_coverage_qty")), 0)
	if minimum_batch_coverage_qty > QTY_TOLERANCE or cint(evidence.get("is_safety_stock_group")):
		frappe.throw(
			_(
				"Demand Delta {0} belongs to cross-target minimum-batch or safety-stock coverage. Rebuild the complete Planning Run instead of applying an incremental change."
			).format(delta_name),
			frappe.ValidationError,
		)
	target_base_residual_qty = max(
		target_demand_qty - available_stock_qty - credited_work_order_qty + safety_stock_gap_qty,
		0,
	)
	target_net_qty = target_base_residual_qty
	minimum_batch_qty = max(flt(net_state.get("minimum_batch_qty")), 0)
	residual_planning_qty = (
		max(target_net_qty, minimum_batch_qty) if target_net_qty > QTY_TOLERANCE else 0
	)
	new_batch_surplus_qty = max(residual_planning_qty - target_net_qty, 0)
	if doc.change_type in DATE_CHANGE_TYPES:
		target_planned_qty = flt(result.planned_qty)
	else:
		# A Result covers one total boundary: exact existing-WO coverage plus the
		# residual, or a minimum-batch-expanded residual, whichever is larger.
		target_planned_qty = max(
			residual_planning_qty,
			credited_work_order_qty + target_net_qty,
			0,
		)

	target_snapshot = [_customer_schedule_target_state(row) for row in live_rows]
	return {
		"mode": "Imported Demand Delta",
		"source_demand_delta": delta_name,
		"schedule_reference": delta.get("schedule_reference"),
		"delta_change_type": delta.get("change_type"),
		"sales_order": sales_lineage["sales_order"],
		"sales_order_item": sales_lineage["sales_order_item"],
		"previous_customer_qty": flt(delta.get("previous_qty")),
		"current_customer_qty": flt(delta.get("current_qty")),
		"customer_delta_qty": flt(delta.get("delta_qty")),
		"previous_schedule_date": delta.get("previous_schedule_date"),
		"current_schedule_date": delta.get("current_schedule_date"),
		"target_schedule_date": target_schedule_date,
		"current_demand_qty": current_demand_qty,
		"target_demand_qty": target_demand_qty,
		"available_stock_qty": available_stock_qty,
		"open_work_order_qty": open_work_order_qty,
		"credited_open_work_order_qty": credited_work_order_qty,
		"existing_work_order_policy": existing_work_order_policy,
		"safety_stock_gap_qty": safety_stock_gap_qty,
		"minimum_batch_coverage_qty": 0,
		"target_base_residual_qty": target_base_residual_qty,
		"target_net_requirement_qty": target_net_qty,
		"minimum_batch_qty": minimum_batch_qty,
		"target_residual_planning_qty": residual_planning_qty,
		"new_batch_surplus_qty": new_batch_surplus_qty,
		"is_safety_stock_group": 0,
		"target_planned_qty": target_planned_qty,
		"targets": target_snapshot,
		"target_state_token": _hash_payload(target_snapshot),
		"retired_target_count": sum(cint(row.get("retired")) for row in targets),
		"source_schedule_mutated_by_change_request": 0,
		"historical_offsets_preserved": 1,
	}


def _get_exact_customer_change_net_requirement_state(result, sales_lineage: dict[str, str]):
	net_requirement = getattr(result, "net_requirement", None)
	if not net_requirement:
		frappe.throw(
			_("Customer Demand Delta target has no exact APS Net Requirement. Rebuild the Planning Run."),
			frappe.ValidationError,
		)
	state = frappe.db.get_value(
		"APS Net Requirement",
		net_requirement,
		[
			"name",
			"company",
			"customer",
			"sales_order",
			"sales_order_item",
			"item_code",
			"demand_qty",
			"available_stock_qty",
			"open_work_order_qty",
			"existing_work_order_policy",
			"safety_stock_gap_qty",
			"minimum_batch_qty",
			"planning_qty",
			"net_requirement_qty",
		],
		as_dict=True,
	)
	if not state:
		frappe.throw(
			_("Customer Demand Delta target Net Requirement {0} was not found.").format(net_requirement),
			frappe.DoesNotExistError,
		)
	expected = {
		"company": getattr(result, "company", None) or "",
		"customer": getattr(result, "customer", None) or "",
		"sales_order": sales_lineage["sales_order"],
		"sales_order_item": sales_lineage["sales_order_item"],
		"item_code": getattr(result, "item_code", None) or "",
	}
	if any((state.get(fieldname) or "") != value for fieldname, value in expected.items()):
		frappe.throw(
			_(
				"Net Requirement {0} does not match the exact Company/Customer/Sales Order/Item lineage of APS result {1}."
			).format(net_requirement, result.name),
			frappe.ValidationError,
		)
	return state


def _resolve_demand_delta_target_date(doc, result, delta):
	delta_name = delta.get("name") or getattr(doc, "source_demand_delta", None) or "-"
	result_date_value = getattr(result, "requested_date", None)
	result_date = getdate(result_date_value) if result_date_value else None
	current_date_value = delta.get("current_schedule_date")
	previous_date_value = delta.get("previous_schedule_date")
	current_date = getdate(current_date_value) if current_date_value else None
	previous_date = getdate(previous_date_value) if previous_date_value else None
	if doc.change_type in DATE_CHANGE_TYPES:
		requested_value = getattr(doc, "required_date", None)
		requested_date = getdate(requested_value) if requested_value else None
		if not current_date or requested_date != current_date:
			frappe.throw(
				_(
					"Source Demand Delta {0} current date must exactly match the Change Request date."
				).format(delta_name),
				frappe.ValidationError,
			)
		if previous_date and result_date and previous_date != result_date:
			frappe.throw(
				_(
					"Source Demand Delta {0} previous date does not match target result {1}."
				).format(delta_name, result.name),
				frappe.ValidationError,
			)
		return current_date
	expected_delta_date = current_date or previous_date
	if not result_date or (expected_delta_date and expected_delta_date != result_date):
		frappe.throw(
			_(
				"Source Demand Delta {0} date does not match target result {1}. Rebuild the Planning Run instead of merging dates."
			).format(delta_name, result.name),
			frappe.ValidationError,
		)
	return result_date


def _get_customer_schedule_target_rows(target_names: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
	target_names = sorted({name for name in target_names or [] if name})
	if not target_names:
		return []
	return [
		dict(row)
		for row in frappe.db.sql(
			"""
			select
				i.name, i.parent, i.idx, i.sales_order, i.item_code, i.customer_part_no, i.schedule_date,
				i.qty, i.allocated_qty, i.produced_qty, i.delivered_qty,
				i.balance_qty, i.status, i.modified,
				s.company, ifnull(s.customer, '') as customer,
				s.schedule_scope, s.version_no, s.status as schedule_status,
				s.modified as schedule_modified
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where i.name in %(target_names)s
			order by i.parent asc, i.idx asc, i.name asc
			""",
			{"target_names": tuple(target_names)},
			as_dict=True,
		)
	]


def _get_delta_customer_schedule_target_rows(delta, result) -> list[dict[str, Any]]:
	"""Resolve the imported row with full business identity for Append support."""
	sales_lineage = _resolve_exact_demand_sales_lineage(
		delta,
		target=result,
		delta_name=delta.get("name"),
	)
	schedule_reference = delta.get("schedule_reference")
	current_date = delta.get("current_schedule_date")
	if not schedule_reference or not current_date:
		return []
	names = frappe.get_all(
		"Customer Delivery Schedule Item",
		filters={
			"parent": schedule_reference,
			"parenttype": "Customer Delivery Schedule",
			"item_code": getattr(result, "item_code", None),
			"schedule_date": getdate(current_date),
		},
		pluck="name",
		order_by="idx asc, name asc",
	)
	rows = _get_customer_schedule_target_rows(names)
	expected_sales_order = sales_lineage["sales_order"]
	expected_customer_part = delta.get("customer_part_no") or ""
	matches = [
		row
		for row in rows
		if (row.get("sales_order") or "") == expected_sales_order
		and (row.get("customer_part_no") or "") == expected_customer_part
	]
	if len(matches) > 1:
		frappe.throw(
			_(
				"Source Demand Delta {0} matches multiple customer schedule rows. Resolve the duplicate source identity before Apply."
			).format(delta.get("name") or "-"),
			frappe.ValidationError,
		)
	if not matches and delta.get("change_type") != "Cancelled":
		frappe.throw(
			_("Source Demand Delta {0} has no exact active schedule row.").format(delta.get("name") or "-"),
			frappe.ValidationError,
		)
	for row in matches:
		row["sales_order_item"] = sales_lineage["sales_order_item"]
	return matches


def _get_authoritative_target_deliveries(result, target_names: list[str]) -> dict[str, float]:
	if not target_names:
		return {}
	locked_state = _get_application_locked_state(result)
	if locked_state:
		return _get_locked_target_delivery_lower_bounds(result, target_names, locked_state)
	from injection_aps.services import delivery_sync

	return delivery_sync.get_schedule_delivery_lower_bounds(
		company=getattr(result, "company", None),
		customer=getattr(result, "customer", None),
		schedule_item_names=target_names,
	)


def _get_authoritative_target_production(result, target_names: list[str]) -> dict[str, float]:
	"""Read effective good output from the allocation ledger, not the child cache."""
	target_names = sorted({name for name in target_names or [] if name})
	if not target_names:
		return {}
	locked_state = _get_application_locked_state(result)
	if locked_state:
		return _get_locked_target_production_lower_bounds(result, target_names, locked_state)
	rows = frappe.db.sql(
		"""
		select
			a.customer_schedule_item,
			coalesce(sum(a.good_qty), 0) as produced_qty
		from `tabAPS Production Allocation` a
		inner join `tabAPS Schedule Result` r on r.name = a.schedule_result
		where a.is_effective = 1
			and a.customer_schedule_item in %(target_names)s
			and r.company = %(company)s
			and ifnull(r.customer, '') = ifnull(%(customer)s, '')
		group by a.customer_schedule_item
		""",
		{
			"target_names": tuple(target_names),
			"company": getattr(result, "company", None),
			"customer": getattr(result, "customer", None),
		},
		as_dict=True,
	)
	return {
		row.get("customer_schedule_item"): max(flt(row.get("produced_qty")), 0)
		for row in rows
		if row.get("customer_schedule_item")
	}


def _get_locked_target_delivery_lower_bounds(result, target_names, locked_state) -> dict[str, float]:
	"""Validate locked Delivery Note truth before trusting its locked allocation ledger."""
	from injection_aps.services import delivery_sync

	target_names = sorted(set(target_names))
	schedule_by_name = {row.get("name"): row for row in locked_state.schedules}
	active_targets = []
	for item in locked_state.schedule_items:
		header = schedule_by_name.get(item.get("parent")) or {}
		if (
			header.get("status") == "Active"
			and (header.get("company") or "") == (result.get("company") or "")
			and (header.get("customer") or "") == (result.get("customer") or "")
		):
			active_targets.append({**dict(item), "company": header.get("company"), "customer": header.get("customer")})
	active_by_name = {row.get("name"): row for row in active_targets if row.get("name")}
	missing = sorted(set(target_names) - set(active_by_name))
	if missing:
		frappe.throw(
			_("Customer schedule rows are no longer Active or do not belong to this scope: {0}.").format(
				", ".join(missing)
			),
			frappe.ValidationError,
		)
	allocations = [dict(row) for row in locked_state.delivery_allocations]
	note_by_name = {row.get("name"): row for row in locked_state.delivery_notes}
	item_by_name = {row.get("name"): row for row in locked_state.delivery_note_items}
	active_target_names = set(active_by_name)
	target_related_sources = {
		row.get("source_delivery_note_item")
		for row in allocations
		if row.get("customer_schedule_item") in active_target_names
		and row.get("source_delivery_note_item")
	}
	allocations_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for allocation in allocations:
		if allocation.get("source_delivery_note_item"):
			allocations_by_source[allocation["source_delivery_note_item"]].append(allocation)

	def build_source(note, item):
		qty = abs(flt(item.get("stock_qty") or item.get("qty")))
		posting_time = get_datetime(
			"{0} {1}".format(getdate(note.get("posting_date")), note.get("posting_time") or "00:00:00")
		)
		return {
			"source_delivery_note": note.get("name"),
			"source_delivery_note_item": item.get("name"),
			"source_docstatus": cint(note.get("docstatus")),
			"source_qty": qty,
			"source_posting_time": posting_time,
			"is_return": cint(note.get("is_return")),
			"return_against": note.get("return_against"),
			"original_delivery_note_item": item.get("dn_detail"),
			"direct_schedule_item": item.get("custom_aps_customer_schedule_item"),
		}

	for item in locked_state.delivery_note_items:
		note = note_by_name.get(item.get("parent")) or {}
		if cint(note.get("docstatus")) != 1:
			continue
		source_qty = abs(flt(item.get("stock_qty") or item.get("qty")))
		if source_qty <= QTY_TOLERANCE:
			continue
		matches_active_target = any(
			(target.get("item_code") or "") == (item.get("item_code") or "")
			and (target.get("sales_order") or "") == (item.get("against_sales_order") or "")
			and getdate(target.get("schedule_date")) == getdate(note.get("posting_date"))
			for target in active_targets
		)
		direct_target = item.get("custom_aps_customer_schedule_item")
		original_direct = item_by_name.get(item.get("dn_detail")) or {}
		is_relevant = (
			item.get("name") in target_related_sources
			or direct_target in active_target_names
			or original_direct.get("custom_aps_customer_schedule_item") in active_target_names
			or matches_active_target
		)
		if not is_relevant:
			continue
		effective = [
			row
			for row in allocations_by_source.get(item.get("name")) or []
			if cint(row.get("is_effective"))
		]
		if abs(sum(flt(row.get("allocated_qty")) for row in effective) - source_qty) > QTY_TOLERANCE:
			frappe.throw(
				_("Delivery source {0} is not fully represented by the locked APS delivery ledger. Run delivery synchronization and retry.", context="Injection APS").format(item.get("name") or "-"),
				frappe.ValidationError,
			)
		expected_fingerprint = delivery_sync._delivery_source_fingerprint(build_source(note, item))
		if any(
			cint(row.get("source_docstatus")) != 1
			or (row.get("source_fingerprint") or "") != expected_fingerprint
			for row in effective
		):
			frappe.throw(
				_("Delivery source {0} changed after allocation synchronization. Retry after synchronization.", context="Injection APS").format(
					item.get("name") or "-"
				),
				frappe.ValidationError,
			)

	for allocation in allocations:
		if not cint(allocation.get("is_effective")):
			continue
		note = note_by_name.get(allocation.get("source_delivery_note"))
		item = item_by_name.get(allocation.get("source_delivery_note_item"))
		if not note or not item or cint(note.get("docstatus")) != 1:
			frappe.throw(
				_("APS delivery allocation has no submitted locked Delivery Note source.", context="Injection APS"),
				frappe.ValidationError,
			)

	result_qty = {name: 0.0 for name in target_names}
	for allocation in allocations:
		target = allocation.get("customer_schedule_item")
		if target in result_qty and cint(allocation.get("is_effective")):
			result_qty[target] += flt(allocation.get("effective_qty"))
	return {name: max(flt(qty), 0) for name, qty in result_qty.items()}


def _get_locked_target_production_lower_bounds(result, target_names, locked_state) -> dict[str, float]:
	"""Fail closed on unsynchronized Manufacture sources, then sum locked good output."""
	from injection_aps.services import execution_sync

	allocations = [dict(row) for row in locked_state.production_allocations]
	entry_by_name = {row.get("name"): row for row in locked_state.stock_entries}
	detail_by_name = {row.get("name"): row for row in locked_state.stock_entry_details}
	work_order_by_name = {row.get("name"): row for row in locked_state.work_orders}
	allocations_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for allocation in allocations:
		if allocation.get("source_stock_entry_detail"):
			allocations_by_source[allocation["source_stock_entry_detail"]].append(allocation)

	for detail in locked_state.stock_entry_details:
		entry = entry_by_name.get(detail.get("parent")) or {}
		if cint(entry.get("docstatus")) != 1 or entry.get("purpose") != "Manufacture":
			continue
		work_order = work_order_by_name.get(entry.get("work_order")) or {}
		source = {
			"source_stock_entry": entry.get("name"),
			"source_stock_entry_detail": detail.get("name"),
			"source_docstatus": cint(entry.get("docstatus")),
			"work_order": entry.get("work_order"),
			"work_order_scheduling": entry.get("work_order_scheduling"),
			"direct_scheduling_item": entry.get("custom_aps_scheduling_item"),
			"direct_segment": entry.get("custom_aps_segment_reference"),
			"explicit_output_type": entry.get("custom_aps_output_type"),
			"item_code": detail.get("item_code"),
			"source_qty": flt(detail.get("transfer_qty")) or flt(detail.get("qty")),
			"is_finished_item": detail.get("is_finished_item"),
			"is_scrap_item": detail.get("is_scrap_item"),
			"t_warehouse": detail.get("t_warehouse"),
			"work_order_item": work_order.get("production_item"),
			"scrap_warehouse": work_order.get("scrap_warehouse"),
		}
		if source["source_qty"] <= QTY_TOLERANCE or not execution_sync._is_work_order_finished_output(source):
			continue
		execution_sync._assert_work_order_output_item(source)
		source["output_type"] = execution_sync._classify_manufacture_output(source)
		source["source_posting_time"] = get_datetime(
			"{0} {1}".format(getdate(entry.get("posting_date")), entry.get("posting_time") or "00:00:00")
		)
		effective = [
			row
			for row in allocations_by_source.get(detail.get("name")) or []
			if cint(row.get("is_effective"))
		]
		if abs(
			sum(flt(row.get("allocated_qty")) for row in effective) - flt(source["source_qty"])
		) > QTY_TOLERANCE:
			frappe.throw(
				_("Manufacture source {0} is not fully represented by the locked APS production ledger. Run production synchronization and retry.", context="Injection APS").format(detail.get("name") or "-"),
				frappe.ValidationError,
			)
		expected_fingerprint = execution_sync._source_fingerprint(source)
		if any(
			cint(row.get("source_docstatus")) != 1
			or (row.get("source_fingerprint") or "") != expected_fingerprint
			for row in effective
		):
			frappe.throw(
				_("Manufacture source {0} changed after production synchronization.", context="Injection APS").format(
					detail.get("name") or "-"
				),
				frappe.ValidationError,
			)

	for allocation in allocations:
		if not cint(allocation.get("is_effective")):
			continue
		entry = entry_by_name.get(allocation.get("source_stock_entry"))
		detail = detail_by_name.get(allocation.get("source_stock_entry_detail"))
		if not entry or not detail or cint(entry.get("docstatus")) != 1:
			frappe.throw(
				_("APS production allocation has no submitted locked Stock Entry source.", context="Injection APS"),
				frappe.ValidationError,
			)

	result_qty = {name: 0.0 for name in target_names}
	for allocation in allocations:
		target = allocation.get("customer_schedule_item")
		if target in result_qty and cint(allocation.get("is_effective")):
			result_qty[target] += flt(allocation.get("good_qty"))
	return {name: max(flt(qty), 0) for name, qty in result_qty.items()}


def _assert_customer_schedule_target_qty_floor(row: dict[str, Any]) -> None:
	qty = flt(row.get("qty"))
	lower_bound = max(flt(row.get("fulfillment_lower_bound_qty")), 0)
	if qty + QTY_TOLERANCE < lower_bound:
		frappe.throw(
			_(
				"Customer schedule target {0} quantity {1} is below the authoritative delivered/produced/allocated lower bound {2}."
			).format(row.get("name") or "-", qty, lower_bound),
			frappe.ValidationError,
		)


def _customer_schedule_target_state(row: dict[str, Any]) -> dict[str, Any]:
	return {
		"name": row.get("name") or "",
		"parent": row.get("parent") or "",
		"idx": cint(row.get("idx")),
		"sales_order": row.get("sales_order") or "",
		"sales_order_item": row.get("sales_order_item") or "",
		"customer_part_no": row.get("customer_part_no") or "",
		"item_code": row.get("item_code") or "",
		"schedule_date": str(row.get("schedule_date") or ""),
		"qty": round(flt(row.get("qty")), 6),
		"allocated_qty": round(flt(row.get("allocated_qty")), 6),
		"produced_qty": round(flt(row.get("produced_qty")), 6),
		"delivered_qty": round(flt(row.get("delivered_qty")), 6),
		"open_qty": round(flt(row.get("open_qty")), 6),
		"balance_qty": round(flt(row.get("balance_qty")), 6),
		"fulfillment_lower_bound_qty": round(flt(row.get("fulfillment_lower_bound_qty")), 6),
		"status": row.get("status") or "",
		"modified": str(row.get("modified") or ""),
		"company": row.get("company") or "",
		"customer": row.get("customer") or "",
		"schedule_scope": row.get("schedule_scope") or "",
		"version_no": row.get("version_no") or "",
		"schedule_status": row.get("schedule_status") or "",
		"schedule_modified": str(row.get("schedule_modified") or ""),
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
	locked_state = _get_application_locked_state(doc)
	for action in actions:
		if locked_state:
			segment = next(
				(row for row in locked_state.segments if row.get("name") == action.get("segment_name")),
				None,
			)
			if not segment:
				frappe.throw(
					_("Segment {0} is outside the locked Apply scope.", context="Injection APS").format(action.get("segment_name")),
					frappe.ValidationError,
				)
			result_doc = _get_apply_document(doc, "APS Schedule Result", segment.get("parent"))
			run_doc = _get_apply_document(doc, "APS Planning Run", doc.planning_run)
		else:
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
		_apply_family_segment_action(segment, action_name, values, action, locked_state=locked_state)
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
	*,
	locked_state=None,
):
	if not primary_segment.get("family_group"):
		return
	if locked_state:
		siblings = [
			row
			for row in locked_state.segments
			if row.get("parent") == primary_segment.get("parent")
			and row.get("family_group") == primary_segment.get("family_group")
			and row.get("segment_kind") == "Family Co-Product"
		]
	else:
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
	demand_change = proposal.get("customer_demand_change") or {}
	locked_state = _get_application_locked_state(result_doc)
	locked_net_state = next(
		(
			row
			for row in (locked_state.get("net_requirements") if locked_state else [])
			if row.get("name") == net_requirement
		),
		None,
	)
	net_exists = bool(locked_net_state) if locked_state else bool(
		net_requirement and frappe.db.exists("APS Net Requirement", net_requirement)
	)
	if demand_change and not net_exists:
		frappe.throw(
			_("Imported Demand Delta requires its exact APS Net Requirement. Rebuild the Planning Run."),
			frappe.ValidationError,
		)
	demand_acceptance = _accept_customer_schedule_delta_baseline(
		result_doc,
		proposal,
	)
	if not net_exists:
		return {
			"net_requirement": None,
			"customer_demand": demand_acceptance,
		}
	target_total_qty = flt(proposal.get("target_planned_qty"))
	if demand_change:
		planning_qty = flt(demand_change.get("target_residual_planning_qty"))
		net_requirement_qty = flt(demand_change.get("target_net_requirement_qty"))
	else:
		net_state = locked_net_state or frappe.db.get_value(
			"APS Net Requirement",
			net_requirement,
			["open_work_order_qty", "existing_work_order_policy"],
			as_dict=True,
		) or {}
		credited_work_order_qty = (
			max(flt(net_state.get("open_work_order_qty")), 0)
			if net_state.get("existing_work_order_policy") == "Include"
			else 0
		)
		planning_qty = max(target_total_qty - credited_work_order_qty, 0)
		net_requirement_qty = planning_qty
	values = {
		"planning_qty": planning_qty,
		"net_requirement_qty": net_requirement_qty,
		"reason_text": _("Current plan target applied by APS Change Request {0}.").format(
			proposal.get("change_request") or "-"
		),
	}
	# Gross/outstanding customer demand is independent from the net production
	# target.  It changes here only when an imported Demand Delta is explicitly
	# accepted; a plan-only quantity adjustment leaves demand_qty untouched.
	if demand_change:
		values["demand_qty"] = flt(demand_change.get("target_demand_qty"))
	if demand_acceptance.get("baseline_json"):
		values["fulfillment_baseline_json"] = demand_acceptance["baseline_json"]
	if demand_acceptance.get("demand_source_snapshot_json"):
		values["demand_source_snapshot_json"] = demand_acceptance["demand_source_snapshot_json"]
	if proposal.get("new_required_date"):
		values["demand_date"] = getdate(proposal.get("new_required_date"))
	frappe.db.set_value("APS Net Requirement", net_requirement, values)
	return {
		"net_requirement": net_requirement,
		"customer_demand": {
			key: value for key, value in demand_acceptance.items() if key != "baseline_json"
		},
	}


def _get_frozen_customer_schedule_target_offsets(target: dict[str, Any]) -> dict[str, float]:
	"""Validate and return the immutable fulfillment epoch for one target."""
	required_fields = (
		"opening_required_qty",
		"opening_allocated_qty",
		"opening_produced_qty",
		"opening_delivered_qty",
		"source_open_qty",
	)
	missing = [fieldname for fieldname in required_fields if target.get(fieldname) in (None, "")]
	target_name = target.get("customer_schedule_item") or "-"
	if missing:
		frappe.throw(
			_(
				"Customer schedule target {0} has no complete original fulfillment offsets ({1}). Rebuild the complete Planning Run."
			).format(target_name, ", ".join(missing)),
			frappe.ValidationError,
		)
	values = {fieldname: flt(target.get(fieldname)) for fieldname in required_fields}
	if any(value < -QTY_TOLERANCE for value in values.values()):
		frappe.throw(
			_(
				"Customer schedule target {0} has invalid negative original fulfillment offsets. Rebuild the complete Planning Run."
			).format(target_name),
			frappe.ValidationError,
		)
	values = {fieldname: max(value, 0) for fieldname, value in values.items()}
	expected_source_open_qty = max(
		values["opening_required_qty"] - values["opening_delivered_qty"],
		0,
	)
	if abs(values["source_open_qty"] - expected_source_open_qty) > QTY_TOLERANCE:
		frappe.throw(
			_(
				"Customer schedule target {0} original gross, delivered and source-open quantities are not conserved. Rebuild the complete Planning Run."
			).format(target_name),
			frappe.ValidationError,
		)
	return values


def _build_customer_schedule_accepted_epoch(
	target: dict[str, Any],
	current: dict[str, Any],
) -> dict[str, float]:
	"""Build a conserved accepted epoch while keeping the original epoch immutable."""
	frozen = _get_frozen_customer_schedule_target_offsets(target)
	target_name = target.get("customer_schedule_item") or current.get("name") or "-"
	accepted_required_qty = max(flt(current.get("qty")), 0)
	accepted_delivered_qty = max(flt(current.get("delivered_qty")), 0)
	if accepted_delivered_qty + QTY_TOLERANCE < frozen["opening_delivered_qty"]:
		frappe.throw(
			_(
				"Customer schedule target {0} has a return crossing its original delivery offset. Rebuild the complete Planning Run."
			).format(target_name),
			frappe.ValidationError,
		)
	# A gross reduction below the original delivered epoch cannot be reconciled by
	# merely clamping source-open to zero; that would lose part of returned demand.
	if accepted_required_qty + QTY_TOLERANCE < frozen["opening_delivered_qty"]:
		frappe.throw(
			_(
				"Customer schedule target {0} accepted quantity cannot be reconciled with its original delivery offset. Rebuild the complete Planning Run."
			).format(target_name),
			frappe.ValidationError,
		)
	accepted_source_open_qty = max(
		accepted_required_qty - frozen["opening_delivered_qty"],
		0,
	)
	accepted_current_open_qty = max(
		accepted_required_qty - accepted_delivered_qty,
		0,
	)
	if abs(accepted_current_open_qty - flt(current.get("open_qty"))) > QTY_TOLERANCE:
		frappe.throw(
			_(
				"Customer schedule target {0} accepted open quantity is not conserved. Rebuild the complete Planning Run."
			).format(target_name),
			frappe.ValidationError,
		)
	return {
		"accepted_required_qty": accepted_required_qty,
		"accepted_delivered_qty": accepted_delivered_qty,
		"accepted_source_open_qty": accepted_source_open_qty,
		"accepted_current_open_qty": accepted_current_open_qty,
	}


def _accept_customer_schedule_delta_baseline(result_doc, proposal: dict[str, Any]) -> dict[str, Any]:
	"""Accept imported source state without ever modifying its schedule rows.

	The Result/Net Requirement baseline is APS-owned audit data.  Updating it is the
	explicit acknowledgement of a reviewed Demand Delta.  The original opening epoch
	is immutable; a separate accepted epoch records the reviewed gross/current-open
	quantities so a sync replay cannot count pre-change execution a second time.
	"""
	demand_change = proposal.get("customer_demand_change") or {}
	if not demand_change:
		return {
			"mode": "Plan Only",
			"source_schedule_mutated": 0,
			"baseline_updated": 0,
		}
	if (result_doc.get("demand_source") or "") != "Customer Delivery Schedule":
		frappe.throw(
			_("Imported customer Demand Delta cannot be applied to a non-customer APS result."),
			frappe.ValidationError,
		)
	if (demand_change.get("source_demand_delta") or "") != (proposal.get("source_demand_delta") or ""):
		frappe.throw(_("Demand Delta proposal lineage is inconsistent. Analyze the request again."), frappe.ValidationError)
	locked_state = _get_application_locked_state(result_doc)
	live_lineage = _get_source_demand_delta_sales_lineage(
		demand_change.get("source_demand_delta"),
		target=result_doc,
		locked_state=locked_state,
	)
	if (
		(demand_change.get("sales_order") or "") != live_lineage["sales_order"]
		or (demand_change.get("sales_order_item") or "") != live_lineage["sales_order_item"]
	):
		frappe.throw(
			_("Demand Delta Sales Order lineage changed after analysis. Analyze the request again."),
			frappe.ValidationError,
		)

	baseline = _load_customer_schedule_baseline(result_doc.get("fulfillment_baseline_json"))
	_get_complete_v4_net_requirement_evidence(baseline)
	baseline_targets = baseline.setdefault("targets", [])
	targets = [
		row
		for row in baseline_targets
		if isinstance(row, dict) and row.get("customer_schedule_item")
	]
	active_target_names = sorted(
		{
			row.get("customer_schedule_item")
			for row in targets
			if not cint(row.get("retired")) and row.get("customer_schedule_item")
		}
		| {
			row.get("name")
			for row in demand_change.get("targets") or []
			if isinstance(row, dict) and row.get("name")
		}
	)
	if locked_state:
		schedule_by_name = {row.get("name"): row for row in locked_state.schedules}
		live_rows = []
		for source in locked_state.schedule_items:
			if source.get("name") not in active_target_names:
				continue
			header = schedule_by_name.get(source.get("parent")) or {}
			live_rows.append(
				{
					**dict(source),
					"company": header.get("company"),
					"customer": header.get("customer") or "",
					"schedule_scope": header.get("schedule_scope"),
					"version_no": header.get("version_no"),
					"schedule_status": header.get("status"),
					"schedule_modified": header.get("modified"),
				}
			)
		live_rows.sort(key=lambda row: (row.get("parent") or "", cint(row.get("idx")), row.get("name") or ""))
	else:
		live_rows = _get_customer_schedule_target_rows(active_target_names)
	delivered_by_target = _get_authoritative_target_deliveries(result_doc, active_target_names)
	produced_by_target = _get_authoritative_target_production(result_doc, active_target_names)
	target_schedule_date_value = demand_change.get("target_schedule_date")
	if not target_schedule_date_value:
		frappe.throw(
			_("Demand Delta proposal has no exact accepted schedule date. Analyze the request again."),
			frappe.ValidationError,
		)
	target_schedule_date = getdate(target_schedule_date_value)
	for row in live_rows:
		for fieldname in ("company", "customer", "item_code"):
			if (row.get(fieldname) or "") != (result_doc.get(fieldname) or ""):
				frappe.throw(
					_("Customer schedule target {0} no longer matches APS result {1}.").format(
						row.get("name"), result_doc.name
					),
					frappe.ValidationError,
				)
		if (row.get("sales_order") or "") != live_lineage["sales_order"]:
			frappe.throw(
				_(
					"Customer schedule target {0} Sales Order does not match APS result {1}."
				).format(row.get("name"), result_doc.name),
				frappe.ValidationError,
			)
		if not row.get("schedule_date") or getdate(row.get("schedule_date")) != target_schedule_date:
			frappe.throw(
				_(
					"Customer schedule target {0} date does not match the exact Demand Delta date {1}."
				).format(row.get("name"), target_schedule_date),
				frappe.ValidationError,
			)
		row["sales_order_item"] = live_lineage["sales_order_item"]
		row["delivered_qty"] = max(flt(delivered_by_target.get(row.get("name"))), 0)
		row["produced_qty"] = max(flt(produced_by_target.get(row.get("name"))), 0)
		row["open_qty"] = max(flt(row.get("qty")) - flt(row.get("delivered_qty")), 0)
		row["fulfillment_lower_bound_qty"] = max(
			flt(row.get("delivered_qty")),
			flt(row.get("produced_qty")),
			flt(row.get("allocated_qty")),
		)
		_assert_customer_schedule_target_qty_floor(row)
	current_state = [_customer_schedule_target_state(row) for row in live_rows]
	if _hash_payload(current_state) != (demand_change.get("target_state_token") or ""):
		frappe.throw(
			_("The active customer schedule changed after analysis. Analyze the latest Demand Delta before Apply."),
			frappe.ValidationError,
		)
	row_by_name = {row.get("name"): row for row in live_rows}
	existing_target_names = {row.get("customer_schedule_item") for row in targets}
	for current in live_rows:
		if current.get("name") in existing_target_names:
			continue
		# Append imports introduce a genuinely new exact demand target.  This is the
		# first epoch for that row, so freeze all original offsets exactly once.
		opening_required_qty = max(flt(current.get("qty")), 0)
		opening_delivered_qty = max(flt(current.get("delivered_qty")), 0)
		new_target = {
			"customer_schedule": current.get("parent"),
			"customer_schedule_item": current.get("name"),
			"sales_order": current.get("sales_order"),
			"sales_order_item": live_lineage["sales_order_item"],
			"item_code": current.get("item_code"),
			"schedule_date": str(current.get("schedule_date") or ""),
			"source_open_qty": max(opening_required_qty - opening_delivered_qty, 0),
			"opening_required_qty": opening_required_qty,
			"opening_allocated_qty": max(flt(current.get("allocated_qty")), 0),
			"opening_produced_qty": max(flt(current.get("produced_qty")), 0),
			"opening_delivered_qty": opening_delivered_qty,
		}
		baseline_targets.append(new_target)
		targets.append(new_target)
		existing_target_names.add(current.get("name"))
	accepted = []
	for target in targets:
		target_name = target.get("customer_schedule_item")
		if cint(target.get("retired")):
			continue
		current = row_by_name.get(target_name)
		if not current or current.get("schedule_status") != "Active":
			frappe.throw(
				_("Customer schedule target {0} is no longer active. Analyze the latest Demand Delta.").format(
					target_name
				),
				frappe.ValidationError,
			)
		for fieldname, expected in (
			("sales_order", live_lineage["sales_order"]),
			("sales_order_item", live_lineage["sales_order_item"]),
			("item_code", current.get("item_code") or ""),
		):
			if target.get(fieldname) and (target.get(fieldname) or "") != (expected or ""):
				frappe.throw(
					_(
						"Customer schedule target {0} frozen {1} lineage differs from the accepted Demand Delta. Rebuild the complete Planning Run."
					).format(target_name, fieldname),
					frappe.ValidationError,
				)
		accepted_epoch = _build_customer_schedule_accepted_epoch(target, current)
		accepted_required_qty = accepted_epoch["accepted_required_qty"]
		accepted_delivered_qty = accepted_epoch["accepted_delivered_qty"]
		accepted_source_open_qty = accepted_epoch["accepted_source_open_qty"]
		accepted_current_open_qty = accepted_epoch["accepted_current_open_qty"]
		# Never overwrite opening_* / source_open_qty / schedule_date.  Consumers use
		# this explicit accepted epoch after a reviewed Delta and retain the originals
		# for replaying production, delivery and returns idempotently.
		target["accepted_required_qty"] = accepted_required_qty
		target["accepted_delivered_qty"] = accepted_delivered_qty
		target["accepted_source_open_qty"] = accepted_source_open_qty
		target["accepted_current_open_qty"] = accepted_current_open_qty
		target["accepted_schedule_date"] = str(current.get("schedule_date") or "")
		target["attributed_qty"] = accepted_source_open_qty
		target["sales_order"] = current.get("sales_order")
		target["sales_order_item"] = live_lineage["sales_order_item"]
		target["item_code"] = current.get("item_code")
		target["accepted_source_demand_delta"] = demand_change.get("source_demand_delta")
		target["accepted_by_change_request"] = proposal.get("change_request")
		target.pop("current_required_qty", None)
		accepted.append(
			{
				"customer_schedule_item": target_name,
				"qty": accepted_required_qty,
				"open_qty": accepted_current_open_qty,
				"accepted_source_open_qty": accepted_source_open_qty,
				"schedule_date": str(current.get("schedule_date") or ""),
				"fulfillment_lower_bound_qty": max(flt(current.get("fulfillment_lower_bound_qty")), 0),
			}
		)
	if baseline.get("sales_order_items"):
		frappe.throw(
			_(
				"This Result combines customer schedule and Sales Order backlog sources. Rebuild the complete Planning Run instead of applying an incremental Demand Delta."
			),
			frappe.ValidationError,
		)
	existing_source_rows = _load_json_list(result_doc.get("demand_source_snapshot_json"))
	unsupported_sources = [
		row
		for row in existing_source_rows
		if (row.get("source_doctype") or "") != "Customer Delivery Schedule"
	]
	if unsupported_sources:
		frappe.throw(
			_(
				"This Result has mixed demand sources that cannot be updated incrementally. Rebuild the complete Planning Run."
			),
			frappe.ValidationError,
		)
	existing_sources_by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for row in existing_source_rows:
		if row.get("source_detail_name"):
			existing_sources_by_target[row["source_detail_name"]].append(row)
	duplicate_source_targets = sorted(
		target_name
		for target_name, rows in existing_sources_by_target.items()
		if len(rows) > 1
	)
	if duplicate_source_targets:
		frappe.throw(
			_(
				"Demand source snapshot has duplicate customer schedule targets: {0}. Rebuild the complete Planning Run."
			).format(", ".join(duplicate_source_targets)),
			frappe.ValidationError,
		)
	accepted_target_by_name = {
		row.get("customer_schedule_item"): row
		for row in targets
		if isinstance(row, dict) and not cint(row.get("retired"))
	}
	accepted_source_rows = []
	for current in live_rows:
		accepted_target = accepted_target_by_name.get(current.get("name"))
		if not accepted_target or "accepted_current_open_qty" not in accepted_target:
			frappe.throw(
				_(
					"Customer schedule target {0} has no complete accepted demand epoch. Rebuild the complete Planning Run."
				).format(current.get("name") or "-"),
				frappe.ValidationError,
			)
		previous_source = (existing_sources_by_target.get(current.get("name")) or [{}])[0]
		accepted_source_rows.append(
			{
				"demand_pool": previous_source.get("demand_pool"),
				"source_doctype": "Customer Delivery Schedule",
				"source_name": current.get("parent"),
				"source_detail_name": current.get("name"),
				"sales_order": live_lineage["sales_order"],
				"sales_order_item": live_lineage["sales_order_item"],
				"qty": max(flt(accepted_target.get("accepted_current_open_qty")), 0),
			}
		)
	accepted_source_rows.sort(
		key=lambda row: (
			row.get("sales_order") or "",
			row.get("source_detail_name") or "",
			row.get("demand_pool") or "",
		)
	)
	if abs(
		sum(flt(row.get("qty")) for row in accepted_source_rows)
		- flt(demand_change.get("target_demand_qty"))
	) > QTY_TOLERANCE:
		frappe.throw(
			_("Accepted customer schedule targets do not conserve the Demand Delta quantity."),
			frappe.ValidationError,
		)
	demand_source_snapshot_json = json.dumps(
		accepted_source_rows,
		ensure_ascii=True,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
	_, formula_baseline_json = planning._build_net_requirement_lineage_snapshot(
		[],
		demand_qty=demand_change.get("target_demand_qty"),
		available_stock_qty=demand_change.get("available_stock_qty"),
		open_work_order_qty=demand_change.get("credited_open_work_order_qty"),
		existing_work_order_policy=demand_change.get("existing_work_order_policy"),
		safety_stock_gap_qty=demand_change.get("safety_stock_gap_qty"),
		minimum_batch_qty=demand_change.get("minimum_batch_qty"),
		minimum_batch_coverage_qty=demand_change.get("minimum_batch_coverage_qty"),
		net_requirement_qty=demand_change.get("target_net_requirement_qty"),
		planning_qty=demand_change.get("target_residual_planning_qty"),
		new_batch_surplus_qty=demand_change.get("new_batch_surplus_qty"),
		is_safety_stock_group=demand_change.get("is_safety_stock_group"),
	)
	formula_baseline = _load_customer_schedule_baseline(formula_baseline_json)
	net_requirement_baseline = formula_baseline.get("net_requirement") or {}
	formula_quantities = {
		"demand_qty": demand_change.get("target_demand_qty"),
		"available_stock_qty": demand_change.get("available_stock_qty"),
		"open_work_order_qty": demand_change.get("credited_open_work_order_qty"),
		"safety_stock_gap_qty": demand_change.get("safety_stock_gap_qty"),
		"minimum_batch_qty": demand_change.get("minimum_batch_qty"),
		"minimum_batch_coverage_qty": demand_change.get("minimum_batch_coverage_qty"),
		"base_residual_qty": demand_change.get("target_base_residual_qty"),
		"net_requirement_qty": demand_change.get("target_net_requirement_qty"),
		"planning_qty": demand_change.get("target_residual_planning_qty"),
		"new_batch_surplus_qty": demand_change.get("new_batch_surplus_qty"),
	}
	formula_changed = any(
		abs(flt(net_requirement_baseline.get(fieldname)) - flt(expected)) > QTY_TOLERANCE
		for fieldname, expected in formula_quantities.items()
	) or (net_requirement_baseline.get("existing_work_order_policy") or "") != (
		demand_change.get("existing_work_order_policy") or ""
	) or cint(net_requirement_baseline.get("is_safety_stock_group")) != cint(
		demand_change.get("is_safety_stock_group")
	) or cint(net_requirement_baseline.get("formula_version")) != 1
	if formula_changed:
		frappe.throw(
			_("Demand Delta formula evidence changed after analysis. Analyze the request again."),
			frappe.ValidationError,
		)
	baseline["version"] = 4
	baseline["net_requirement"] = net_requirement_baseline
	baseline["sales_order_items"] = []
	baseline["accepted_source_demand_delta"] = demand_change.get("source_demand_delta")
	baseline["accepted_by_change_request"] = proposal.get("change_request")
	baseline_json = json.dumps(
		baseline,
		ensure_ascii=True,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
	frappe.db.set_value(
		"APS Schedule Result",
		result_doc.name,
		{
			"fulfillment_baseline_json": baseline_json,
			"demand_source_snapshot_json": demand_source_snapshot_json,
		},
		update_modified=False,
	)
	return {
		"mode": "Imported Demand Delta",
		"source_demand_delta": demand_change.get("source_demand_delta"),
		"source_schedule_mutated": 0,
		"baseline_updated": 1,
		"accepted_targets": accepted,
		"retired_target_count": sum(cint(row.get("retired")) for row in targets),
		"historical_offsets_preserved": 1,
		"baseline_json": baseline_json,
		"demand_source_snapshot_json": demand_source_snapshot_json,
	}


def _load_customer_schedule_baseline(value) -> dict[str, Any]:
	if isinstance(value, dict):
		return value
	try:
		baseline = json.loads(value or "{}")
	except (TypeError, ValueError):
		baseline = {}
	return baseline if isinstance(baseline, dict) else {}


def _load_json_list(value) -> list[dict[str, Any]]:
	if isinstance(value, list):
		rows = value
	else:
		try:
			rows = json.loads(value or "[]")
		except (TypeError, ValueError):
			rows = []
	return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


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


def _capture_plan_snapshot(doc, scope: str, *, locked_state=None) -> dict[str, Any]:
	if locked_state:
		run_row = frappe._dict(
			{fieldname: locked_state.run.get(fieldname) for fieldname in RUN_SNAPSHOT_FIELDS}
		)
	else:
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
	elif locked_state:
		result_names = [row.get("name") for row in locked_state.results]
	else:
		result_names = frappe.get_all(
			"APS Schedule Result",
			filters={"planning_run": doc.planning_run},
			pluck="name",
			order_by="creation asc, name asc",
		)
	if locked_state:
		result_name_set = set(result_names)
		results = [
			frappe._dict({fieldname: row.get(fieldname) for fieldname in RESULT_SNAPSHOT_FIELDS})
			for row in locked_state.results
			if row.get("name") in result_name_set
		]
		segments = [
			frappe._dict({fieldname: row.get(fieldname) for fieldname in SEGMENT_SNAPSHOT_FIELDS})
			for row in locked_state.segments
			if row.get("parent") in result_name_set
		]
		segments.sort(
			key=lambda row: (
				row.get("parent") or "",
				get_datetime(row.get("start_time")) if row.get("start_time") else get_datetime("1900-01-01"),
				cint(row.get("sequence_no")),
				row.get("name") or "",
			)
		)
	else:
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
	if locked_state:
		net_requirements = [
			frappe._dict(
				{fieldname: row.get(fieldname) for fieldname in NET_REQUIREMENT_SNAPSHOT_FIELDS}
			)
			for row in locked_state.net_requirements
			if row.get("name") in net_names
		]
		net_requirements.sort(key=lambda row: row.get("name") or "")
	else:
		net_requirements = frappe.get_all(
			"APS Net Requirement",
			filters={"name": ("in", net_names or [""])},
			fields=list(NET_REQUIREMENT_SNAPSHOT_FIELDS),
			order_by="name asc",
		)
	snapshot = {
		"scope": scope,
		"run": dict(run_row),
		"results": [dict(row) for row in results],
		"segments": [dict(row) for row in segments],
		"net_requirements": [dict(row) for row in net_requirements],
		"customer_schedule_targets": _capture_customer_schedule_target_snapshot(
			results,
			locked_state=locked_state,
		),
		"source_demand_delta": _capture_source_demand_delta_snapshot(
			doc.source_demand_delta,
			locked_state=locked_state,
		),
	}
	if scope == "run":
		if locked_state:
			snapshot["capacity_windows"] = [dict(row) for row in locked_state.capacity_windows]
		else:
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


def _capture_customer_schedule_target_snapshot(results, *, locked_state=None) -> dict[str, Any]:
	"""Include source demand identity in optimistic-concurrency snapshots."""
	lineage_rows = sorted(
		[
			{
				"result": result.get("name") or "",
				"customer_schedule_item": target.get("customer_schedule_item") or "",
				"sales_order": result.get("sales_order") or "",
				"sales_order_item": result.get("sales_order_item") or "",
			}
			for result in results or []
			for target in (
				_load_customer_schedule_baseline(result.get("fulfillment_baseline_json")).get("targets") or []
			)
			if isinstance(target, dict) and target.get("customer_schedule_item")
		],
		key=lambda row: (row["customer_schedule_item"], row["result"]),
	)
	target_names = sorted(
		{
			target.get("customer_schedule_item")
			for result in results or []
			for target in (
				_load_customer_schedule_baseline(result.get("fulfillment_baseline_json")).get("targets") or []
			)
			if isinstance(target, dict) and target.get("customer_schedule_item")
		}
	)
	if locked_state:
		schedule_by_name = {row.get("name"): row for row in locked_state.schedules}
		rows = []
		for source in locked_state.schedule_items:
			if source.get("name") not in target_names:
				continue
			header = schedule_by_name.get(source.get("parent")) or {}
			rows.append(
				{
					**dict(source),
					"company": header.get("company"),
					"customer": header.get("customer") or "",
					"schedule_scope": header.get("schedule_scope"),
					"version_no": header.get("version_no"),
					"schedule_status": header.get("status"),
					"schedule_modified": header.get("modified"),
				}
			)
		rows.sort(key=lambda row: (row.get("parent") or "", cint(row.get("idx")), row.get("name") or ""))
	else:
		rows = _get_customer_schedule_target_rows(target_names)
	lineages_by_target: dict[str, set[str]] = defaultdict(set)
	for lineage in lineage_rows:
		if lineage["sales_order_item"]:
			lineages_by_target[lineage["customer_schedule_item"]].add(lineage["sales_order_item"])
	for row in rows:
		lineages = lineages_by_target.get(row.get("name")) or set()
		if len(lineages) == 1:
			row["sales_order_item"] = next(iter(lineages))
	return {
		"target_names": target_names,
		"missing_target_names": sorted(set(target_names) - {row.get("name") for row in rows}),
		"result_lineage": lineage_rows,
		"rows": [_customer_schedule_target_state(row) for row in rows],
	}


def _capture_source_demand_delta_snapshot(
	delta_name: str | None,
	*,
	locked_state=None,
) -> dict[str, Any] | None:
	if not delta_name:
		return None
	fields = [
		"name", "import_batch", "schedule_reference", "company", "customer",
		"sales_order", "item_code", "customer_part_no", "previous_schedule_date",
		"current_schedule_date", "previous_qty", "current_qty", "delta_qty",
		"change_type", "modified",
	]
	if locked_state:
		locked_delta = locked_state.get("delta")
		row = (
			frappe._dict({fieldname: locked_delta.get(fieldname) for fieldname in fields})
			if locked_delta and locked_delta.get("name") == delta_name
			else None
		)
	else:
		row = frappe.db.get_value("APS Demand Delta", delta_name, fields, as_dict=True)
	if not row:
		return {"name": delta_name, "missing": 1}
	if locked_state:
		schedule = next(
			(
				schedule
				for schedule in locked_state.schedules
				if schedule.get("name") == row.get("schedule_reference")
			),
			None,
		)
	else:
		schedule = frappe.db.get_value(
			"Customer Delivery Schedule",
			row.get("schedule_reference"),
			["status", "modified"],
			as_dict=True,
		) if row.get("schedule_reference") else None
	if locked_state:
		matching_sales_order_items = [
			item.get("name")
			for item in locked_state.sales_order_items
			if item.get("parent") == row.get("sales_order")
			and item.get("item_code") == row.get("item_code")
		]
		resolved_sales_order_item = (
			matching_sales_order_items[0] if len(matching_sales_order_items) == 1 else None
		)
	else:
		resolved_sales_order_item = planning._resolve_unique_sales_order_item(
			row.get("sales_order"), row.get("item_code")
		)
	return {
		**dict(row),
		"resolved_sales_order_item": resolved_sales_order_item,
		"schedule_status": schedule.get("status") if schedule else None,
		"schedule_modified": schedule.get("modified") if schedule else None,
	}


def _assert_snapshot_current(doc, proposal: dict[str, Any]) -> dict[str, Any]:
	scope = proposal.get("snapshot_scope") or "target"
	locked_state = _get_application_locked_state(doc)
	current_snapshot = _capture_plan_snapshot(
		doc,
		scope,
		locked_state=locked_state,
	)
	current_hash = _hash_payload(current_snapshot)
	expected_hash = proposal.get("source_snapshot_hash") or doc.source_snapshot_hash
	if not expected_hash or current_hash != expected_hash:
		frappe.throw(
			_("The plan changed after analysis. Re-analyze this request before confirmation or Apply."),
			frappe.ValidationError,
		)
	if locked_state:
		# Later consistency code uses Frappe's normal reads.  A routing hint may have
		# opened an older RR snapshot before these current-read locks were acquired.
		# Never use that snapshot as truth: require it to be byte-for-byte equivalent
		# to the locked state before allowing any mutation.  This also closes the
		# narrow changed-then-reverted race between the hint and the Customer lock.
		repeatable_read_snapshot = _capture_plan_snapshot(doc, scope)
		if _hash_payload(repeatable_read_snapshot) != current_hash:
			frappe.throw(
				_("The transaction snapshot differs from the locked current plan. Retry Apply in a new request.", context="Injection APS"),
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
