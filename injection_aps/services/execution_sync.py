from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


QTY_TOLERANCE = 0.000001
ACTIVE_WOS_STATUSES = ("Manufacture",)
INACTIVE_WORK_ORDER_STATUSES = ("Stopped", "Completed", "Closed", "Cancelled")
PHYSICAL_SEGMENT_KINDS = ("Primary", "Manual")


def sync_production_for_run(run_name: str) -> dict[str, Any]:
	"""Reconcile formal Manufacture rows to APS segments through an idempotent detail ledger."""
	from injection_aps.services import availability, campaign_planning, consistency

	save_point = "aps_production_sync_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		frappe.db.sql("select name from `tabAPS Planning Run` where name = %s for update", run_name)
		frappe.get_doc("APS Planning Run", run_name)
		contexts = _get_run_segment_contexts(run_name)
		sources = _get_formal_manufacture_sources(contexts)
		desired = _build_desired_production_allocations(run_name, contexts, sources)
		ledger_summary = _reconcile_production_ledger(run_name, desired)
		rollup = _rollup_production_allocations(run_name, contexts)
		consistency_summary = consistency.recalculate_plan_consistency(
			run_name,
			reason="formal production allocation synchronization",
		)
		fulfillment = availability.recalculate_run_fulfillment(run_name)
		campaigns = campaign_planning.sync_campaign_actuals(run_name)
		frappe.db.release_savepoint(save_point)
		return {
			"run": run_name,
			"source_detail_count": len(sources),
			"desired_allocation_count": len(desired),
			"ledger": ledger_summary,
			"rollup": rollup,
			"consistency": consistency_summary,
			"fulfillment": fulfillment,
			"campaigns": campaigns,
		}
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def queue_production_sync(doc, method: str | None = None):
	"""Queue affected run reconciliation after a Manufacture submit/cancel transaction commits."""
	if (doc.get("purpose") or "") != "Manufacture":
		return
	for run_name in get_affected_production_runs(doc):
		event_identity = "|".join(
			str(value or "")
			for value in (
				run_name,
				doc.name,
				doc.get("docstatus"),
				doc.get("modified"),
				method,
			)
		)
		frappe.enqueue(
			"injection_aps.services.execution_sync.sync_production_for_run",
			queue="short",
			enqueue_after_commit=True,
			job_id="aps-production-sync-{0}-{1}".format(
				run_name,
				hashlib.sha256(event_identity.encode("utf-8")).hexdigest()[:20],
			),
			deduplicate=True,
			run_name=run_name,
		)


def validate_manufacture_before_submit(doc, method: str | None = None):
	"""Validate APS execution references without running the full historical reconciliation."""
	if (doc.get("purpose") or "") != "Manufacture":
		return
	direct_segment = doc.get("custom_aps_segment_reference")
	direct_scheduling_item = doc.get("custom_aps_scheduling_item")
	wos = doc.get("work_order_scheduling")
	work_order = doc.get("work_order")
	work_order_values = frappe.db.get_value(
		"Work Order",
		work_order,
		[
			"production_item",
			"scrap_warehouse",
			"sales_order",
			"sales_order_item",
			"custom_aps_run",
			"custom_aps_result_reference",
			"custom_aps_campaign",
		],
		as_dict=True,
	) if work_order else None
	work_order_aps_run = (work_order_values or {}).get("custom_aps_run")
	work_order_aps_result = (work_order_values or {}).get("custom_aps_result_reference")
	work_order_aps_campaign = (work_order_values or {}).get("custom_aps_campaign")
	runs = set()
	if direct_segment:
		run_name = _get_segment_run(direct_segment)
		if not run_name:
			frappe.throw(
				_("APS segment {0} does not exist or is not linked to a planning run.").format(direct_segment),
				frappe.ValidationError,
			)
		runs.add(run_name)
	if direct_scheduling_item:
		item_link = frappe.db.get_value(
			"Scheduling Item",
			direct_scheduling_item,
			["custom_aps_run", "custom_aps_segment_reference"],
			as_dict=True,
		)
		if not item_link:
			frappe.throw(
				_("APS Scheduling Item {0} does not exist.").format(direct_scheduling_item),
				frappe.ValidationError,
			)
		if item_link.custom_aps_run:
			runs.add(item_link.custom_aps_run)
		elif item_link.custom_aps_segment_reference:
			if run_name := _get_segment_run(item_link.custom_aps_segment_reference):
				runs.add(run_name)
	if wos:
		wos_run = frappe.db.get_value("Work Order Scheduling", wos, "custom_aps_run")
		if wos_run:
			runs.add(wos_run)
	if work_order_aps_run:
		runs.add(work_order_aps_run)
	eligible_wo_runs = _get_eligible_work_order_runs(work_order) if work_order else []
	if len(eligible_wo_runs) == 1:
		runs.add(eligible_wo_runs[0])
	has_aps_signal = bool(
		direct_segment
		or direct_scheduling_item
		or runs
		or eligible_wo_runs
		or work_order_aps_result
		or work_order_aps_campaign
	)
	if not has_aps_signal:
		return
	if len(runs) != 1:
		frappe.throw(
			_("Manufacture entry APS references do not identify one planning run; correct the APS links before submit."),
			frappe.ValidationError,
		)
	run_name = next(iter(runs))
	frappe.db.sql("select name from `tabAPS Planning Run` where name = %s for update", run_name)
	contexts = _get_run_segment_contexts(run_name)
	context_by_segment = {row["segment"]: row for row in contexts}
	context_by_item = {
		item.get("name"): context
		for context in contexts
		for item in context.get("scheduling_items") or []
		if item.get("name")
	}
	for detail in doc.get("items") or []:
		qty = flt(detail.get("transfer_qty")) or flt(detail.get("qty"))
		if qty <= QTY_TOLERANCE:
			continue
		source = {
			"source_stock_entry": doc.get("name") or "New Stock Entry",
			"source_stock_entry_detail": detail.get("name") or str(detail.get("idx") or "New Row"),
			"work_order": work_order,
			"work_order_scheduling": wos,
			"direct_scheduling_item": direct_scheduling_item,
			"direct_segment": direct_segment,
			"explicit_output_type": doc.get("custom_aps_output_type"),
			"item_code": detail.get("item_code"),
			"source_qty": qty,
			"is_finished_item": detail.get("is_finished_item"),
			"is_scrap_item": detail.get("is_scrap_item"),
			"t_warehouse": detail.get("t_warehouse"),
			"work_order_item": (work_order_values or {}).get("production_item"),
			"work_order_sales_order": (work_order_values or {}).get("sales_order"),
			"work_order_sales_order_item": (work_order_values or {}).get("sales_order_item"),
			"work_order_aps_run": work_order_aps_run,
			"work_order_aps_result": work_order_aps_result,
			"work_order_aps_campaign": work_order_aps_campaign,
			"scrap_warehouse": (work_order_values or {}).get("scrap_warehouse"),
		}
		# Use the exact same output predicate as reconciliation.  Some ERPNext
		# schemas do not expose ``is_scrap_item`` and defect-FG integrations instead
		# identify scrap by the header hint or the Work Order scrap warehouse.  A
		# flag-only prefilter here used to let those rows bypass submit validation and
		# fail later in the asynchronous reconciliation job.
		#
		# ERPNext ``is_scrap_item`` also marks BOM scrap/by-products, whose
		# quantity and Stock UOM are unrelated to completed finished units.
		# APS execution progress only accepts the Work Order production item;
		# zelin_pp defect output still qualifies because it routes that same item
		# to the Work Order scrap warehouse.
		_assert_work_order_output_item(source)
		if not _is_work_order_finished_output(source):
			continue
		source["output_type"] = _classify_manufacture_output(source)
		_get_source_candidates(
			run_name,
			source,
			contexts,
			context_by_segment,
			context_by_item,
		)


def get_affected_production_runs(stock_entry) -> list[str]:
	runs = set(
		frappe.get_all(
			"APS Production Allocation",
			filters={"source_stock_entry": stock_entry.name},
			pluck="planning_run",
		)
	)
	segment_reference = stock_entry.get("custom_aps_segment_reference")
	if segment_reference:
		run_name = _get_segment_run(segment_reference)
		if run_name:
			runs.add(run_name)
	scheduling_item = stock_entry.get("custom_aps_scheduling_item")
	if scheduling_item and frappe.db.exists("Scheduling Item", scheduling_item):
		run_name = frappe.db.get_value("Scheduling Item", scheduling_item, "custom_aps_run")
		segment_reference = segment_reference or frappe.db.get_value(
			"Scheduling Item", scheduling_item, "custom_aps_segment_reference"
		)
		if run_name:
			runs.add(run_name)
		if segment_reference and (run_name := _get_segment_run(segment_reference)):
			runs.add(run_name)
	wos = stock_entry.get("work_order_scheduling")
	if wos and frappe.db.exists("Work Order Scheduling", wos):
		run_name = frappe.db.get_value("Work Order Scheduling", wos, "custom_aps_run")
		if run_name:
			runs.add(run_name)
	work_order = stock_entry.get("work_order")
	if work_order:
		runs.update(_get_eligible_work_order_runs(work_order))
	return sorted(run for run in runs if run and frappe.db.exists("APS Planning Run", run))


def _get_segment_run(segment_name: str | None) -> str | None:
	if not segment_name:
		return None
	return frappe.db.sql(
		"""
		select r.planning_run
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` r on r.name = seg.parent
		where seg.name = %s and seg.parenttype = 'APS Schedule Result'
		limit 1
		""",
		segment_name,
	)[0][0] if frappe.db.exists("APS Schedule Segment", segment_name) else None


def _get_run_segment_contexts(run_name: str) -> list[dict[str, Any]]:
	rows = frappe.db.sql(
		"""
		select
			seg.name as segment,
			seg.parent as schedule_result,
			seg.workstation,
			seg.start_time,
			seg.end_time,
			seg.planned_qty,
			seg.segment_kind,
			seg.segment_status,
			seg.production_campaign,
			seg.capacity_owner,
			seg.linked_work_order as work_order,
			seg.linked_work_order_scheduling as work_order_scheduling,
			seg.linked_scheduling_item as scheduling_item,
			r.company,
			r.planning_run,
			r.customer,
			r.sales_order,
			r.sales_order_item,
			r.item_code,
			r.requested_date,
			r.planned_qty as result_planned_qty,
			r.production_strategy,
			r.demand_confidence,
			r.cancellation_risk_percent,
			r.prebuild_allowed,
			r.max_prebuild_days,
			r.fulfillment_baseline_json
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` r on r.name = seg.parent
		where seg.parenttype = 'APS Schedule Result'
			and r.planning_run = %(run_name)s
			and (
				seg.segment_kind in ('Primary', 'Manual')
				or (seg.segment_kind = 'Family Co-Product' and seg.production_campaign is not null)
			)
			and ifnull(seg.segment_status, 'Planned') not in ('Blocked', 'Cancelled')
			and ifnull(seg.planned_qty, 0) > 0
		order by seg.start_time asc, seg.idx asc, seg.name asc
		""",
		{"run_name": run_name},
		as_dict=True,
	)
	contexts = [dict(row) for row in rows]
	_attach_aps_owned_work_orders(contexts, run_name)
	if not contexts or not frappe.db.exists("DocType", "Scheduling Item"):
		return contexts
	segment_names = [row["segment"] for row in contexts]
	linked_items = [row.get("scheduling_item") for row in contexts if row.get("scheduling_item")]
	conditions = ["si.custom_aps_segment_reference in %(segment_names)s"]
	params = {"segment_names": segment_names, "linked_items": linked_items or ["__missing__"]}
	conditions.append("si.name in %(linked_items)s")
	items = frappe.db.sql(
		"""
		select
			si.name,
			si.parent,
			si.work_order,
			si.scheduling_qty,
			si.completed_qty,
			si.defect_qty,
			si.from_time,
			si.to_time,
			si.planned_start_date,
			si.planned_end_date,
			si.custom_aps_run,
			si.custom_aps_result_reference,
			si.custom_aps_segment_reference,
			si.custom_aps_campaign,
			si.custom_aps_output_role,
			si.custom_aps_capacity_owner,
			ifnull(wos.status, '') as wos_status,
			ifnull(wos.custom_aps_approval_state, '') as approval_state
		from `tabScheduling Item` si
		inner join `tabWork Order Scheduling` wos on wos.name = si.parent
		where ({conditions})
		order by coalesce(si.from_time, si.planned_start_date) asc, si.idx asc, si.name asc
		""".format(conditions=" or ".join(conditions)),
		params,
		as_dict=True,
	)
	by_segment = defaultdict(list)
	by_name = {row.name: row for row in items}
	for item in items:
		if item.custom_aps_segment_reference:
			by_segment[item.custom_aps_segment_reference].append(dict(item))
	for context in contexts:
		associated = list(by_segment.get(context["segment"]) or [])
		if context.get("scheduling_item") and context["scheduling_item"] in by_name:
			item = dict(by_name[context["scheduling_item"]])
			if not any(row.get("name") == item["name"] for row in associated):
				associated.append(item)
		context["scheduling_items"] = associated
	return contexts


def _attach_aps_owned_work_orders(contexts: list[dict[str, Any]], run_name: str) -> None:
	"""Attach submitted Work Orders whose APS owner fields point at each Result.

	The owner fields are written when a WO proposal is applied, before a Work
	Order Scheduling document necessarily exists.  Loading them in one query keeps
	that pre-WOS execution state visible to submit validation and reconciliation.
	A stopped/completed Work Order remains visible only so already-submitted output
	can be replayed; the new-entry validation path still rejects it through the
	active-run eligibility check.
	"""
	result_names = sorted(
		{context.get("schedule_result") for context in contexts if context.get("schedule_result")}
	)
	if not result_names:
		return
	rows = frappe.db.sql(
		"""
		select wo.name, wo.custom_aps_result_reference
		from `tabWork Order` wo
		where wo.docstatus = 1
			and wo.custom_aps_run = %(run_name)s
			and wo.custom_aps_result_reference in %(result_names)s
		order by wo.custom_aps_result_reference, wo.name
		""",
		{"run_name": run_name, "result_names": result_names},
		as_dict=True,
	)
	by_result: dict[str, list[str]] = defaultdict(list)
	for row in rows:
		row = dict(row)
		if row.get("name") and row.get("custom_aps_result_reference"):
			by_result[row["custom_aps_result_reference"]].append(row["name"])
	for context in contexts:
		context["aps_owned_work_orders"] = list(
			by_result.get(context.get("schedule_result")) or []
		)


def _context_work_orders(context: dict[str, Any]) -> set[str]:
	return {
		value
		for value in (
			context.get("work_order"),
			*(item.get("work_order") for item in context.get("scheduling_items") or []),
			*(context.get("aps_owned_work_orders") or []),
		)
		if value
	}


def _get_formal_manufacture_sources(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
	# A Result segment can be quantity-split across an existing WO and a delta WO.
	# The segment's convenience link stores only one value, while Scheduling Items
	# are the auditable one-to-many execution lineage.  Include both sources so a
	# valid Manufacture entry is never hidden merely because another split WO was
	# written to the segment link last.
	work_orders = sorted(
		{work_order for context in contexts for work_order in _context_work_orders(context)}
	)
	run_names = {row.get("planning_run") for row in contexts if row.get("planning_run")}
	run_name = next(iter(run_names)) if len(run_names) == 1 else None
	unique_work_orders = [
		work_order
		for work_order in work_orders
		if run_name and set(_get_eligible_work_order_runs(work_order)) == {run_name}
	]
	historical_owned_work_orders = sorted(
		{
			work_order
			for context in contexts
			for work_order in context.get("aps_owned_work_orders") or []
			if work_order
		}
	)
	source_work_orders = sorted(set(unique_work_orders) | set(historical_owned_work_orders))
	segment_names = sorted({row.get("segment") for row in contexts if row.get("segment")})
	scheduling_item_names = sorted(
		{
			value
			for row in contexts
			for value in (
				row.get("scheduling_item"),
				*(item.get("name") for item in row.get("scheduling_items") or []),
			)
			if value
		}
	)
	wos_names = sorted(
		{
			value
			for row in contexts
			for value in (
				row.get("work_order_scheduling"),
				*(item.get("parent") for item in row.get("scheduling_items") or []),
			)
			if value
		}
	)
	if not source_work_orders and not wos_names and not segment_names and not scheduling_item_names:
		return []
	conditions = []
	params: dict[str, Any] = {}
	if source_work_orders:
		# Include every Manufacture entry for a uniquely APS-owned WO. A populated but
		# invalid WOS must reach validation instead of disappearing from the source set.
		conditions.append("se.work_order in %(work_orders)s")
		params["work_orders"] = source_work_orders
	if wos_names:
		conditions.append("se.work_order_scheduling in %(wos_names)s")
		params["wos_names"] = wos_names
	if segment_names:
		conditions.append("se.custom_aps_segment_reference in %(segment_names)s")
		params["segment_names"] = segment_names
	if scheduling_item_names:
		conditions.append("se.custom_aps_scheduling_item in %(scheduling_item_names)s")
		params["scheduling_item_names"] = scheduling_item_names
	scrap_select, output_condition = _get_stock_entry_detail_output_sql()
	rows = frappe.db.sql(
		"""
		select
			se.name as source_stock_entry,
			se.docstatus as source_docstatus,
			se.work_order,
			se.work_order_scheduling,
			se.custom_aps_scheduling_item as direct_scheduling_item,
			se.custom_aps_segment_reference as direct_segment,
			se.custom_aps_output_type as explicit_output_type,
			se.posting_date,
			se.posting_time,
			se.modified,
			se.amended_from,
			detail.name as source_stock_entry_detail,
			detail.item_code,
			coalesce(nullif(detail.transfer_qty, 0), detail.qty) as source_qty,
			detail.qty as source_document_qty,
			detail.transfer_qty as source_stock_qty,
			detail.is_finished_item,
			{scrap_select},
			detail.t_warehouse,
			wo.production_item as work_order_item,
			wo.sales_order as work_order_sales_order,
			wo.sales_order_item as work_order_sales_order_item,
			wo.custom_aps_run as work_order_aps_run,
			wo.custom_aps_result_reference as work_order_aps_result,
			wo.custom_aps_campaign as work_order_aps_campaign,
			wo.docstatus as work_order_docstatus,
			wo.status as work_order_status,
			wo.scrap_warehouse
		from `tabStock Entry` se
		inner join `tabStock Entry Detail` detail on detail.parent = se.name
		left join `tabWork Order` wo on wo.name = se.work_order
		where se.purpose = 'Manufacture'
			and se.docstatus = 1
			and {output_condition}
			and coalesce(nullif(detail.transfer_qty, 0), detail.qty) > 0
			and ({conditions})
		order by se.posting_date asc, se.posting_time asc, se.creation asc, detail.idx asc, detail.name asc
		""".format(
			conditions=" or ".join(conditions),
			output_condition=output_condition,
			scrap_select=scrap_select,
		),
		params,
		as_dict=True,
	)
	result = []
	for row in rows:
		_assert_work_order_output_item(row)
		if not _is_work_order_finished_output(row):
			continue
		output_type = _classify_manufacture_output(row)
		posting_time = get_datetime(f"{getdate(row.posting_date)} {row.posting_time or '00:00:00'}")
		result.append({**dict(row), "output_type": output_type, "source_posting_time": posting_time})
	return result


def _get_stock_entry_detail_output_sql() -> tuple[str, str]:
	if frappe.db.has_column("Stock Entry Detail", "is_scrap_item"):
		return (
			"detail.is_scrap_item",
			"""(
				detail.is_finished_item = 1
				or detail.is_scrap_item = 1
				or (
					wo.production_item is not null
					and detail.item_code = wo.production_item
					and se.custom_aps_output_type in ('Good', 'Scrap')
				)
			)""",
		)
	return (
		"0 as is_scrap_item",
		"""(
				detail.is_finished_item = 1
				or (
					wo.production_item is not null
					and detail.item_code = wo.production_item
					and se.custom_aps_output_type in ('Good', 'Scrap')
				)
				or (
					wo.production_item is not null
					and detail.item_code = wo.production_item
					and wo.scrap_warehouse is not null
					and detail.t_warehouse = wo.scrap_warehouse
				)
			)""",
	)


def _is_work_order_finished_output(row: dict[str, Any]) -> bool:
	"""Exclude BOM scrap/by-products that are not measured in finished-item units."""
	item_code = row.get("item_code")
	work_order_item = row.get("work_order_item")
	output_signal = (
		cint(row.get("is_finished_item")) or cint(row.get("is_scrap_item"))
		or row.get("explicit_output_type") in ("Good", "Scrap")
		or (row.get("scrap_warehouse") and row.get("t_warehouse") == row.get("scrap_warehouse"))
	)
	return bool(item_code) and bool(output_signal) and (not work_order_item or item_code == work_order_item)


def _assert_work_order_output_item(row: dict[str, Any]) -> None:
	"""Reject a flagged output row that is not measured in the WO finished item."""
	item_code = row.get("item_code")
	work_order_item = row.get("work_order_item")
	# A different `is_scrap_item` is ordinary BOM scrap/by-product and is simply
	# excluded. A different row explicitly marked as finished item is malformed
	# execution data and must fail instead of being silently ignored.
	if (
		not cint(row.get("is_finished_item"))
		or not work_order_item
		or item_code == work_order_item
	):
		return
	frappe.throw(
		_(
			"Finished item {0} does not match Work Order production item {1}; BOM scrap or by-products cannot be counted as APS finished quantity.",
			context="Injection APS",
		).format(item_code or "-", work_order_item),
		frappe.ValidationError,
	)


def _classify_manufacture_output(row: dict[str, Any]) -> str:
	"""Classify standard ERPNext scrap rows and zelin_pp defect-FG entries consistently."""
	if cint(row.get("is_scrap_item")):
		return "Scrap"
	if row.get("scrap_warehouse") and row.get("t_warehouse") == row.get("scrap_warehouse"):
		return "Scrap"
	if row.get("explicit_output_type") in ("Good", "Scrap"):
		return row["explicit_output_type"]
	if cint(row.get("is_finished_item")):
		return "Good"
	return "Good"


def _build_desired_production_allocations(
	run_name: str,
	contexts: list[dict[str, Any]],
	sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	context_by_segment = {row["segment"]: row for row in contexts}
	context_by_item = {}
	for context in contexts:
		for item in context.get("scheduling_items") or []:
			context_by_item[item.get("name")] = context
	used_by_context_output = defaultdict(float)
	schedule_targets = _get_customer_schedule_targets(contexts)
	used_schedule_good = defaultdict(float)
	desired = []
	for source in sources:
		candidates, allocation_method = _get_source_candidates(
			run_name,
			source,
			contexts,
			context_by_segment,
			context_by_item,
		)
		if not candidates:
			continue
		remaining = flt(source.get("source_qty"))
		segment_allocations = []
		for context in candidates:
			if remaining <= QTY_TOLERANCE:
				break
			if allocation_method == "Direct" or context.get("scheduling_items"):
				key = (context["segment"], source["output_type"])
				quota = _context_output_quota(context, source["output_type"])
			else:
				# Before a formal Scheduling Item exists, Good and Scrap details are
				# two outcomes of the same physical segment quantity.  They must share
				# one FIFO quota or each output type can consume the full segment.
				key = (context["segment"], "Total")
				quota = _context_total_output_quota(context)
			available = max(quota - used_by_context_output[key], 0)
			qty = remaining if allocation_method == "Direct" else min(remaining, available)
			if qty <= QTY_TOLERANCE:
				continue
			segment_allocations.append((context, qty))
			used_by_context_output[key] += qty
			remaining -= qty
		if remaining > QTY_TOLERANCE:
			context = candidates[-1]
			segment_allocations.append((context, remaining))
			overflow_key = (
				(context["segment"], source["output_type"])
				if allocation_method == "Direct" or context.get("scheduling_items")
				else (context["segment"], "Total")
			)
			used_by_context_output[overflow_key] += remaining
			remaining = 0
		for context, qty in segment_allocations:
			for target, target_qty in _split_production_to_schedule_targets(
				context,
				qty,
				source["output_type"],
				schedule_targets,
				used_schedule_good,
			):
				allocation_key = _production_allocation_key(source, context, target)
				desired.append(
					{
						"allocation_key": allocation_key,
						"planning_run": run_name,
						"schedule_result": context["schedule_result"],
						"segment": context["segment"],
						"customer_schedule": target.get("parent") if target else None,
						"customer_schedule_item": target.get("name") if target else None,
						"work_order": source.get("work_order"),
						"work_order_scheduling": source.get("work_order_scheduling") or context.get("work_order_scheduling"),
						"scheduling_item": source.get("direct_scheduling_item") or _context_scheduling_item(context, source),
						"source_stock_entry": source["source_stock_entry"],
						"source_stock_entry_detail": source["source_stock_entry_detail"],
						"source_docstatus": 1,
						"source_posting_time": source["source_posting_time"],
						"output_type": source["output_type"],
						"allocation_method": allocation_method,
						"source_qty": flt(source["source_qty"]),
						"allocated_qty": target_qty,
						"good_qty": target_qty if source["output_type"] == "Good" else 0,
						"scrap_qty": target_qty if source["output_type"] == "Scrap" else 0,
						"effective_qty": target_qty,
						"reversed_qty": 0,
						"is_effective": 1,
						"reversal_reason": "",
						"source_fingerprint": _source_fingerprint(source),
						"last_synced_on": now_datetime(),
					}
				)
	return desired


def _get_source_candidates(
	run_name,
	source,
	contexts,
	context_by_segment,
	context_by_item,
):
	direct_segment = source.get("direct_segment")
	direct_scheduling_item = source.get("direct_scheduling_item")
	if direct_segment:
		context = context_by_segment.get(direct_segment)
		if not context:
			frappe.throw(
				_("Direct APS segment {0} is not an active segment of planning run {1}.").format(
					direct_segment, run_name
				),
				frappe.ValidationError,
			)
		_validate_direct_production_context(
			run_name,
			source,
			context,
			direct_segment=direct_segment,
			direct_scheduling_item=direct_scheduling_item,
		)
		return [context], "Direct"
	if direct_scheduling_item:
		context = context_by_item.get(direct_scheduling_item)
		if not context:
			frappe.throw(
				_("Direct Scheduling Item {0} is not linked to planning run {1}.").format(
					direct_scheduling_item, run_name
				),
				frappe.ValidationError,
			)
		_validate_direct_production_context(
			run_name,
			source,
			context,
			direct_scheduling_item=direct_scheduling_item,
		)
		return [context], "Direct"
	wos = source.get("work_order_scheduling")
	if wos:
		wos_row = frappe.db.get_value(
			"Work Order Scheduling",
			wos,
			["status", "custom_aps_run", "custom_aps_approval_state"],
			as_dict=True,
		)
		if not wos_row:
			frappe.throw(
				_("Stock Entry references missing Work Order Scheduling {0}.").format(wos),
				frappe.ValidationError,
			)
		if wos_row.status not in ACTIVE_WOS_STATUSES:
			frappe.throw(
				_("Work Order Scheduling {0} is not in Manufacture status.").format(wos),
				frappe.ValidationError,
			)
		if wos_row.custom_aps_run and wos_row.custom_aps_run != run_name:
			frappe.throw(
				_("Work Order Scheduling {0} belongs to another APS planning run.").format(wos),
				frappe.ValidationError,
			)
		approval_state = wos_row.custom_aps_approval_state or ""
		if approval_state in ("Rejected", "Cancelled") or (
			wos_row.custom_aps_run and approval_state != "Approved"
		):
			frappe.throw(
				_("Work Order Scheduling {0} does not have valid APS approval.").format(wos),
				frappe.ValidationError,
			)
		matching = [
			row
			for row in contexts
			if source.get("work_order") in _context_work_orders(row)
			and wos in ({row.get("work_order_scheduling")} | {item.get("parent") for item in row.get("scheduling_items") or []})
			and _work_order_lineage_matches_context(source, row)
			and _work_order_owner_matches_context(source, row)
		]
		if not matching:
			frappe.throw(
				_("Work Order Scheduling {0} is not linked to an active segment of planning run {1}.").format(
					wos, run_name
				),
				frappe.ValidationError,
			)
		for context in matching:
			_validate_production_output_context(source, context)
		return sorted(matching, key=_context_sort_key), "Execution Detail FIFO"
	matching = [
		row
		for row in contexts
		if source.get("work_order") in _context_work_orders(row)
		and _work_order_lineage_matches_context(source, row)
		and _work_order_owner_matches_context(source, row)
	]
	eligible_runs = set(_get_eligible_work_order_runs(source.get("work_order")))
	historical_exact_owner = _is_submitted_historical_exact_owner(
		run_name,
		source,
		matching,
	)
	if eligible_runs != {run_name} and not historical_exact_owner:
		frappe.throw(
			_("Work Order {0} is not uniquely linked to planning run {1}; select the APS segment explicitly.").format(
				source.get("work_order") or "-", run_name
			),
			frappe.ValidationError,
		)
	if not matching:
		frappe.throw(
			_("Work Order {0} has no active APS segment in planning run {1}.").format(
				source.get("work_order") or "-", run_name
			),
			frappe.ValidationError,
		)
	for context in matching:
		_validate_production_output_context(source, context)
	return sorted(matching, key=_context_sort_key), "Execution Detail FIFO"


def _is_submitted_historical_exact_owner(
	run_name: str,
	source: dict[str, Any],
	matching: list[dict[str, Any]],
) -> bool:
	"""Allow replay, never new submission, for an inactive exact APS-owned WO."""
	if cint(source.get("source_docstatus")) != 1 or cint(source.get("work_order_docstatus")) != 1:
		return False
	if (source.get("work_order_status") or "") not in INACTIVE_WORK_ORDER_STATUSES:
		return False
	owner_run = source.get("work_order_aps_run") or ""
	owner_result = source.get("work_order_aps_result") or ""
	if owner_run != run_name or not owner_result:
		return False
	return any(
		(context.get("planning_run") or "") == run_name
		and (context.get("schedule_result") or "") == owner_result
		and _work_order_lineage_matches_context(source, context)
		and _work_order_owner_matches_context(source, context)
		for context in matching
	)


def _validate_direct_production_context(
	run_name: str,
	source: dict[str, Any],
	context: dict[str, Any],
	*,
	direct_segment: str | None = None,
	direct_scheduling_item: str | None = None,
) -> None:
	"""Reject an explicit execution link unless WO, item, run and result all agree."""
	if context.get("planning_run") and context.get("planning_run") != run_name:
		frappe.throw(
			_("Direct production target belongs to planning run {0}, not {1}.").format(
				context.get("planning_run"), run_name
			),
			frappe.ValidationError,
		)
	if direct_segment and context.get("segment") != direct_segment:
		frappe.throw(_("Direct APS segment does not match the selected APS result."), frappe.ValidationError)
	_validate_work_order_owner_context(source, context)

	items = context.get("scheduling_items") or []
	direct_item = next((row for row in items if row.get("name") == direct_scheduling_item), None)
	if direct_scheduling_item and not direct_item:
		frappe.throw(
			_("Direct Scheduling Item {0} is not linked to APS segment {1}.").format(
				direct_scheduling_item, context.get("segment")
			),
			frappe.ValidationError,
		)
	execution_item = direct_item
	if not execution_item and source.get("work_order_scheduling"):
		execution_item = next(
			(
				row
				for row in items
				if row.get("parent") == source.get("work_order_scheduling")
				and (not source.get("work_order") or row.get("work_order") == source.get("work_order"))
			),
			None,
		)
		if not execution_item:
			frappe.throw(
				_("Stock Entry Work Order Scheduling is not linked to APS segment {0}.").format(
					context.get("segment")
				),
				frappe.ValidationError,
			)
	elif not execution_item and items:
		if len(items) > 1:
			frappe.throw(
				_("APS segment {0} has multiple execution items; select APS Scheduling Item explicitly.").format(
					context.get("segment")
				),
				frappe.ValidationError,
			)
		execution_item = items[0]
	if execution_item:
		_validate_direct_execution_item_state(execution_item)
	elif context.get("work_order_scheduling"):
		wos_state = frappe.db.get_value(
			"Work Order Scheduling",
			context.get("work_order_scheduling"),
			["status", "custom_aps_run", "custom_aps_approval_state"],
			as_dict=True,
		)
		if not wos_state or wos_state.status not in ACTIVE_WOS_STATUSES:
			frappe.throw(
				_("Linked Work Order Scheduling is not in Manufacture status."),
				frappe.ValidationError,
			)
		if wos_state.custom_aps_run and (
			wos_state.custom_aps_run != run_name or wos_state.custom_aps_approval_state != "Approved"
		):
			frappe.throw(
				_("Linked Work Order Scheduling does not have valid APS approval."),
				frappe.ValidationError,
			)
	if direct_item:
		if direct_item.get("custom_aps_run") and direct_item.get("custom_aps_run") != run_name:
			frappe.throw(
				_("Scheduling Item {0} belongs to a different APS planning run.").format(
					direct_scheduling_item
				),
				frappe.ValidationError,
			)
		if direct_item.get("custom_aps_result_reference") and direct_item.get(
			"custom_aps_result_reference"
		) != context.get("schedule_result"):
			frappe.throw(
				_("Scheduling Item {0} belongs to a different APS result.").format(direct_scheduling_item),
				frappe.ValidationError,
			)
		if direct_item.get("custom_aps_segment_reference") and direct_item.get(
			"custom_aps_segment_reference"
		) != context.get("segment"):
			frappe.throw(
				_("Scheduling Item {0} belongs to a different APS segment.").format(direct_scheduling_item),
				frappe.ValidationError,
			)
		if direct_item.get("custom_aps_campaign") and direct_item.get("custom_aps_campaign") != context.get("production_campaign"):
			frappe.throw(
				_("Scheduling Item {0} belongs to a different APS Production Campaign.").format(direct_scheduling_item),
				frappe.ValidationError,
			)

	expected_work_orders = _context_work_orders(context)
	if expected_work_orders and source.get("work_order") not in expected_work_orders:
		frappe.throw(
			_("Stock Entry Work Order {0} does not match APS segment {1}.").format(
				source.get("work_order"), context.get("segment")
			),
			frappe.ValidationError,
		)
	if direct_item and source.get("work_order") and direct_item.get("work_order") != source.get("work_order"):
		frappe.throw(
			_("Scheduling Item {0} belongs to a different Work Order.").format(direct_scheduling_item),
			frappe.ValidationError,
		)
	if not _work_order_lineage_matches_context(source, context):
		frappe.throw(
			_("Stock Entry Work Order Sales Order lineage does not match APS result {0}.").format(
				context.get("schedule_result") or "-"
			),
			frappe.ValidationError,
		)

	expected_schedules = {
		value
		for value in (
			context.get("work_order_scheduling"),
			*(row.get("parent") for row in items),
		)
		if value
	}
	if source.get("work_order_scheduling") and source.get("work_order_scheduling") not in expected_schedules:
		frappe.throw(
			_("Stock Entry Work Order Scheduling does not match APS segment {0}.").format(
				context.get("segment")
			),
			frappe.ValidationError,
		)

	_validate_production_output_context(source, context)


def _validate_production_output_context(
	source: dict[str, Any], context: dict[str, Any]
) -> None:
	"""Use one item-identity gate for Direct, WOS and WO FIFO allocation."""
	result_item = context.get("item_code")
	work_order_item = source.get("work_order_item")
	if work_order_item and result_item and work_order_item != result_item:
		frappe.throw(
			_("Work Order production item {0} does not match APS result item {1}.").format(
				work_order_item, result_item
			),
			frappe.ValidationError,
		)
	if result_item and source.get("item_code") != result_item:
		frappe.throw(
			_("Finished item {0} does not match APS result item {1}.").format(
				source.get("item_code"), result_item
			),
			frappe.ValidationError,
		)


def _work_order_lineage_matches_context(source: dict[str, Any], context: dict[str, Any]) -> bool:
	if source.get("work_order_aps_campaign"):
		return source.get("work_order_aps_campaign") == context.get("production_campaign")
	return (
		(source.get("work_order_sales_order") or "") == (context.get("sales_order") or "")
		and (source.get("work_order_sales_order_item") or "") == (context.get("sales_order_item") or "")
	)


def _work_order_owner_matches_context(source: dict[str, Any], context: dict[str, Any]) -> bool:
	owner_run = source.get("work_order_aps_run") or ""
	owner_result = source.get("work_order_aps_result") or ""
	if not owner_run and not owner_result:
		return True
	return bool(
		owner_run
		and owner_result
		and owner_run == (context.get("planning_run") or "")
		and owner_result == (context.get("schedule_result") or "")
	)


def _validate_work_order_owner_context(
	source: dict[str, Any], context: dict[str, Any]
) -> None:
	if _work_order_owner_matches_context(source, context):
		return
	frappe.throw(
		_(
			"Work Order {0} APS owner Run/Result does not match planning run {1} and result {2}."
		).format(
			source.get("work_order") or "-",
			context.get("planning_run") or "-",
			context.get("schedule_result") or "-",
		),
		frappe.ValidationError,
	)


def _validate_direct_execution_item_state(item: dict[str, Any]) -> None:
	if (item.get("wos_status") or "") not in ACTIVE_WOS_STATUSES:
		frappe.throw(
			_("Scheduling Item {0} is not in Manufacture status.").format(item.get("name")),
			frappe.ValidationError,
		)
	approval_state = item.get("approval_state") or ""
	if approval_state in ("Rejected", "Cancelled") or (
		item.get("custom_aps_run") and approval_state != "Approved"
	):
		frappe.throw(
			_("Scheduling Item {0} does not have valid APS approval.").format(item.get("name")),
			frappe.ValidationError,
		)


def _get_eligible_work_order_runs(work_order: str | None) -> list[str]:
	if not work_order:
		return []
	return frappe.db.sql_list(
		"""
		select distinct lineage.planning_run
		from (
			select r.planning_run
			from `tabAPS Schedule Segment` seg
			inner join `tabAPS Schedule Result` r on r.name = seg.parent
			inner join `tabAPS Planning Run` run on run.name = r.planning_run
			inner join `tabWork Order` wo on wo.name = seg.linked_work_order
			where seg.parenttype = 'APS Schedule Result'
				and seg.linked_work_order = %s
				and wo.docstatus = 1
				and ifnull(wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
				and (seg.segment_kind in ('Primary', 'Manual') or (seg.segment_kind = 'Family Co-Product' and seg.production_campaign is not null))
				and ifnull(seg.segment_status, 'Planned') not in ('Blocked', 'Cancelled')
				and ifnull(seg.planned_qty, 0) > 0
				and ifnull(run.status, 'Draft') != 'Closed'
				and ifnull(run.approval_state, 'Pending') != 'Rejected'
			union all
			select r.planning_run
			from `tabScheduling Item` si
			inner join `tabAPS Schedule Segment` seg
				on seg.name = si.custom_aps_segment_reference
			inner join `tabAPS Schedule Result` r on r.name = seg.parent
			inner join `tabAPS Planning Run` run on run.name = r.planning_run
			inner join `tabWork Order` wo on wo.name = si.work_order
			where si.work_order = %s
				and wo.docstatus = 1
				and ifnull(wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
				and si.custom_aps_run = r.planning_run
				and si.custom_aps_result_reference = r.name
				and seg.parenttype = 'APS Schedule Result'
				and (seg.segment_kind in ('Primary', 'Manual') or (seg.segment_kind = 'Family Co-Product' and seg.production_campaign is not null))
				and ifnull(seg.segment_status, 'Planned') not in ('Blocked', 'Cancelled')
				and ifnull(seg.planned_qty, 0) > 0
				and ifnull(run.status, 'Draft') != 'Closed'
				and ifnull(run.approval_state, 'Pending') != 'Rejected'
			union all
			select r.planning_run
			from `tabWork Order` wo
			inner join `tabAPS Schedule Result` r
				on r.name = wo.custom_aps_result_reference
			inner join `tabAPS Planning Run` run
				on run.name = r.planning_run
			inner join `tabAPS Schedule Segment` seg
				on seg.parent = r.name
				and seg.parenttype = 'APS Schedule Result'
			where wo.name = %s
				and wo.docstatus = 1
				and ifnull(wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
				and wo.custom_aps_run = r.planning_run
				and wo.production_item = r.item_code
				and (
					(wo.custom_aps_campaign is not null and wo.custom_aps_campaign = seg.production_campaign)
					or (
						ifnull(wo.sales_order, '') = ifnull(r.sales_order, '')
						and ifnull(wo.sales_order_item, '') = ifnull(r.sales_order_item, '')
					)
				)
				and (seg.segment_kind in ('Primary', 'Manual') or (seg.segment_kind = 'Family Co-Product' and seg.production_campaign is not null))
				and ifnull(seg.segment_status, 'Planned') not in ('Blocked', 'Cancelled')
				and ifnull(seg.planned_qty, 0) > 0
				and ifnull(run.status, 'Draft') != 'Closed'
				and ifnull(run.approval_state, 'Pending') != 'Rejected'
		) lineage
		order by lineage.planning_run asc
		""",
		(work_order, work_order, work_order),
	)


def _context_output_quota(context: dict[str, Any], output_type: str) -> float:
	fieldname = "defect_qty" if output_type == "Scrap" else "completed_qty"
	qty = sum(flt(row.get(fieldname)) for row in context.get("scheduling_items") or [])
	return qty if qty > QTY_TOLERANCE else max(flt(context.get("planned_qty")), 0)


def _context_total_output_quota(context: dict[str, Any]) -> float:
	qty = sum(
		flt(row.get("completed_qty")) + flt(row.get("defect_qty"))
		for row in context.get("scheduling_items") or []
	)
	return qty if qty > QTY_TOLERANCE else max(flt(context.get("planned_qty")), 0)


def _context_sort_key(context: dict[str, Any]):
	items = context.get("scheduling_items") or []
	first = min(
		(
			get_datetime(row.get("from_time") or row.get("planned_start_date"))
			for row in items
			if row.get("from_time") or row.get("planned_start_date")
		),
		default=get_datetime(context.get("start_time")),
	)
	return first, context.get("segment") or ""


def _context_scheduling_item(context: dict[str, Any], source: dict[str, Any]) -> str | None:
	items = [
		row
		for row in context.get("scheduling_items") or []
		if (not source.get("work_order") or row.get("work_order") == source.get("work_order"))
		and (not source.get("work_order_scheduling") or row.get("parent") == source.get("work_order_scheduling"))
	]
	return items[0].get("name") if items else context.get("scheduling_item")


def _get_customer_schedule_targets(contexts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
	from injection_aps.services.availability import _claim_schedule_targets, _schedule_policy_matches

	result = {}
	claimed_target_names = set()
	for context in contexts:
		result_name = context["schedule_result"]
		if result_name in result:
			continue
		baseline = _parse_fulfillment_baseline(context.get("fulfillment_baseline_json"))
		if baseline is not None:
			result[result_name] = _get_persisted_schedule_targets(
				context,
				baseline,
				claimed_target_names,
			)
			continue
		rows = frappe.db.sql(
			"""
			select
				i.name, i.parent, i.qty, i.sales_order, i.schedule_date, i.idx,
				i.allocated_qty, i.produced_qty, i.delivered_qty,
				i.production_strategy, i.demand_confidence, i.cancellation_risk_percent,
				i.prebuild_allowed, i.max_prebuild_days
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.status = 'Active'
				and s.company = %(company)s
				and ifnull(s.customer, '') = ifnull(%(customer)s, '')
				and i.item_code = %(item_code)s
				and i.schedule_date = %(requested_date)s
				and ifnull(i.sales_order, '') = ifnull(%(sales_order)s, '')
			order by i.schedule_date asc, ifnull(i.sales_order, '') asc, s.creation asc, i.idx asc, i.name asc
			""",
			{
				"company": context.get("company"),
				"customer": context.get("customer"),
				"item_code": context.get("item_code"),
				"requested_date": getdate(context.get("requested_date")),
				"sales_order": context.get("sales_order"),
			},
			as_dict=True,
		)
		matched_targets = [row for row in rows if _schedule_policy_matches(row, context)]
		# Legacy results have no persisted offset.  They may only use a pristine,
		# unambiguous target set; historical coverage is never replayed from zero.
		if any(
			flt(row.get("allocated_qty")) > QTY_TOLERANCE
			or flt(row.get("produced_qty")) > QTY_TOLERANCE
			or flt(row.get("delivered_qty")) > QTY_TOLERANCE
			for row in matched_targets
		):
			matched_targets = []
		result[result_name] = _claim_schedule_targets(
			matched_targets,
			claimed_target_names,
			max_qty=flt(context.get("result_planned_qty")),
		)
	return result


def _parse_fulfillment_baseline(value) -> dict[str, Any] | None:
	if value in (None, ""):
		return None
	if isinstance(value, dict):
		return value
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError):
		# A corrupt persisted baseline is authoritative evidence of an unsafe
		# lineage, not permission to fall back to item/date matching.
		return {"targets": []}
	return parsed if isinstance(parsed, dict) else {"targets": []}


def _get_persisted_schedule_targets(
	context: dict[str, Any],
	baseline: dict[str, Any],
	claimed_target_names: set[str],
) -> list[dict[str, Any]]:
	baseline_rows = [
		row
		for row in baseline.get("targets") or []
		if isinstance(row, dict) and row.get("customer_schedule_item") and not cint(row.get("retired"))
	]
	if not baseline_rows:
		return []
	baseline_by_name = {row["customer_schedule_item"]: row for row in baseline_rows}
	rows = frappe.db.sql(
		"""
		select
			i.name, i.parent, i.qty, i.sales_order, i.schedule_date, i.idx,
			s.company, s.customer, s.status as schedule_status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name in %(target_names)s and s.status = 'Active'
		order by i.schedule_date asc, i.idx asc, i.name asc
		""",
		{"target_names": sorted(baseline_by_name)},
		as_dict=True,
	)
	remaining = max(flt(context.get("result_planned_qty")), 0)
	claimed = []
	for source in rows:
		name = source.get("name")
		if not name or name in claimed_target_names or remaining <= QTY_TOLERANCE:
			continue
		if (source.get("company") or "") != (context.get("company") or "") or (
			source.get("customer") or ""
		) != (context.get("customer") or "") or (source.get("sales_order") or "") != (
			context.get("sales_order") or ""
		):
			continue
		frozen = baseline_by_name[name]
		attributed_qty = min(
			remaining,
			_get_frozen_target_fulfillment_qty(frozen),
			max(flt(source.get("qty")), 0),
		)
		if attributed_qty <= QTY_TOLERANCE:
			continue
		target = dict(source)
		target.update(frozen)
		target["name"] = name
		target["parent"] = source.get("parent")
		target["qty"] = flt(source.get("qty"))
		target["attributed_qty"] = attributed_qty
		claimed.append(target)
		claimed_target_names.add(name)
		remaining -= attributed_qty
	return claimed


def _get_frozen_target_fulfillment_qty(row) -> float:
	value = (
		row.get("accepted_source_open_qty")
		if row.get("accepted_source_open_qty") not in (None, "")
		else row.get("source_open_qty")
	)
	return max(flt(value), 0)


def _split_production_to_schedule_targets(context, qty, output_type, target_map, used_schedule_good):
	targets = target_map.get(context["schedule_result"]) or []
	if not targets:
		return [(None, qty)]
	if output_type == "Scrap":
		return [(targets[0], qty)]
	remaining = qty
	rows = []
	for target in targets:
		target_qty = flt(target.get("attributed_qty") or target.get("qty"))
		available = max(target_qty - used_schedule_good[target["name"]], 0)
		allocated = min(remaining, available)
		if allocated > QTY_TOLERANCE:
			rows.append((target, allocated))
			used_schedule_good[target["name"]] += allocated
			remaining -= allocated
		if remaining <= QTY_TOLERANCE:
			break
	if remaining > QTY_TOLERANCE:
		# Preserve overproduction on the APS segment without overstating a customer demand line.
		rows.append((None, remaining))
	return rows


def _production_allocation_key(source, context, target) -> str:
	return hashlib.sha256(
		"|".join(
			[
				source["source_stock_entry_detail"],
				context["segment"],
				(target or {}).get("name") or "",
				source["output_type"],
			]
		).encode("utf-8")
	).hexdigest()


def _source_fingerprint(source: dict[str, Any]) -> str:
	return hashlib.sha256(
		"|".join(
			str(source.get(fieldname) or "")
			for fieldname in (
				"source_stock_entry",
				"source_stock_entry_detail",
				"source_docstatus",
				"source_qty",
				"source_posting_time",
				"direct_scheduling_item",
				"direct_segment",
				"output_type",
			)
		).encode("utf-8")
	).hexdigest()


def _ledger_values_changed(doc, values: dict[str, Any]) -> bool:
	for fieldname, expected in values.items():
		if fieldname == "last_synced_on":
			continue
		actual = doc.get(fieldname)
		if actual in (None, "") and expected in (None, ""):
			continue
		field = doc.meta.get_field(fieldname)
		fieldtype = field.fieldtype if field else ""
		if fieldtype in ("Float", "Currency", "Percent"):
			if abs(flt(actual) - flt(expected)) > QTY_TOLERANCE:
				return True
		elif fieldtype in ("Check", "Int"):
			if cint(actual) != cint(expected):
				return True
		elif fieldtype == "Date":
			if getdate(actual) != getdate(expected):
				return True
		elif fieldtype == "Datetime":
			if get_datetime(actual) != get_datetime(expected):
				return True
		elif str(actual or "") != str(expected or ""):
			return True
	return False


def _reconcile_production_ledger(run_name: str, desired: list[dict[str, Any]]) -> dict[str, int]:
	desired_by_key = {row["allocation_key"]: row for row in desired}
	existing = {
		row.allocation_key: row.name
		for row in frappe.get_all(
			"APS Production Allocation",
			filters={"planning_run": run_name},
			fields=["name", "allocation_key"],
		)
	}
	created = 0
	updated = 0
	reversed_count = 0
	for key, values in desired_by_key.items():
		if key in existing:
			doc = frappe.get_doc("APS Production Allocation", existing[key])
			if _ledger_values_changed(doc, values):
				doc.update(values)
				doc.last_synced_on = now_datetime()
				doc.save(ignore_permissions=True)
				updated += 1
		else:
			frappe.get_doc({"doctype": "APS Production Allocation", **values}).insert(ignore_permissions=True)
			created += 1
	for key, name in existing.items():
		if key in desired_by_key:
			continue
		doc = frappe.get_doc("APS Production Allocation", name)
		docstatus = cint(frappe.db.get_value("Stock Entry", doc.source_stock_entry, "docstatus"))
		previous_effective = flt(doc.effective_qty)
		reversal_values = {
			"source_docstatus": docstatus,
			"effective_qty": 0,
			"reversed_qty": max(flt(doc.reversed_qty), previous_effective),
			"is_effective": 0,
			"reversal_reason": "Source Stock Entry cancelled" if docstatus == 2 else "Source is no longer eligible",
		}
		if _ledger_values_changed(doc, reversal_values):
			doc.update(reversal_values)
			doc.last_synced_on = now_datetime()
			doc.save(ignore_permissions=True)
		reversed_count += cint(previous_effective > QTY_TOLERANCE)
	return {"created": created, "updated": updated, "reversed": reversed_count}


def _rollup_production_allocations(run_name: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
	from injection_aps.services import planning

	active_segment_names = {row["segment"] for row in contexts}
	rows = frappe.get_all(
		"APS Production Allocation",
		filters={"planning_run": run_name, "is_effective": 1},
		fields=[
			"segment",
			"schedule_result",
			"customer_schedule_item",
			"work_order",
			"work_order_scheduling",
			"scheduling_item",
			"source_stock_entry",
			"source_posting_time",
			"good_qty",
			"scrap_qty",
		],
	)
	# Reconciliation should already reverse excluded targets; this guard prevents any
	# legacy effective row from leaking a cancelled/blocked segment into its result.
	rows = [row for row in rows if row.segment in active_segment_names]
	by_segment = defaultdict(list)
	by_result = defaultdict(list)
	for row in rows:
		by_segment[row.segment].append(row)
		by_result[row.schedule_result].append(row)
	now_value = now_datetime()
	status_counts = defaultdict(int)
	all_result_names = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		pluck="name",
	)
	_reset_inactive_segment_actuals(all_result_names, now_value, active_segment_names=active_segment_names)
	for context in contexts:
		segment_rows = by_segment.get(context["segment"]) or []
		good_qty = sum(flt(row.good_qty) for row in segment_rows)
		scrap_qty = sum(flt(row.scrap_qty) for row in segment_rows)
		total_qty = good_qty + scrap_qty
		posting_times = [get_datetime(row.source_posting_time) for row in segment_rows if row.source_posting_time]
		items = context.get("scheduling_items") or []
		actual_start_times = [row.get("from_time") for row in items if row.get("from_time")]
		actual_end_times = [row.get("to_time") for row in items if row.get("to_time")]
		actual_start = min(actual_start_times) if actual_start_times else (min(posting_times) if posting_times else None)
		last_report = max(posting_times) if posting_times else None
		actual_end = max(actual_end_times) if actual_end_times else (
			last_report if total_qty >= flt(context.get("planned_qty")) and total_qty > 0 else None
		)
		status, delay_minutes = _derive_segment_execution_status(context, total_qty, actual_start, actual_end, now_value)
		status_counts[status] += 1
		frappe.db.set_value(
			"APS Schedule Segment",
			context["segment"],
			{
				"actual_status": status,
				"actual_completed_qty": total_qty,
				"actual_good_qty": good_qty,
				"actual_scrap_qty": scrap_qty,
				"actual_start_time": actual_start,
				"actual_end_time": actual_end,
				"delay_minutes": delay_minutes,
				"last_execution_sync_on": now_value,
				"last_actual_report_time": last_report,
				"execution_source_documents": "\n".join(
					dict.fromkeys(row.source_stock_entry for row in segment_rows if row.source_stock_entry)
				),
			},
			update_modified=False,
		)
	for result_name in all_result_names:
		result_rows = by_result.get(result_name) or []
		result_segments = frappe.get_all(
			"APS Schedule Segment",
			filters={
				"parent": result_name,
				"parenttype": "APS Schedule Result",
				"segment_kind": ("in", PHYSICAL_SEGMENT_KINDS),
				"segment_status": ("not in", ["Blocked", "Cancelled"]),
				"planned_qty": (">", 0),
			},
			fields=["actual_status", "actual_start_time", "actual_end_time", "delay_minutes"],
		)
		last_report = max(
			(get_datetime(row.source_posting_time) for row in result_rows if row.source_posting_time),
			default=None,
		)
		frappe.db.set_value(
			"APS Schedule Result",
			result_name,
			{
				"actual_status": planning._rollup_result_actual_status(
					[row.actual_status for row in result_segments]
				),
				"actual_progress_qty": sum(flt(row.good_qty) for row in result_rows),
				"good_produced_qty": sum(flt(row.good_qty) for row in result_rows),
				"scrap_qty": sum(flt(row.scrap_qty) for row in result_rows),
				"actual_start_time": min(
					(row.actual_start_time for row in result_segments if row.actual_start_time),
					default=None,
				),
				"actual_end_time": max(
					(row.actual_end_time for row in result_segments if row.actual_end_time),
					default=None,
				),
				"delay_minutes": max((flt(row.delay_minutes) for row in result_segments), default=0),
				"last_execution_sync_on": now_value,
				"last_actual_report_time": last_report,
				"execution_source_documents": "\n".join(
					dict.fromkeys(row.source_stock_entry for row in result_rows if row.source_stock_entry)
				),
			},
			update_modified=False,
		)
		planning._sync_execution_exceptions(run_name, frappe.get_doc("APS Schedule Result", result_name))
	_touched_schedule_items = set(
		frappe.get_all(
			"APS Production Allocation",
			filters={"planning_run": run_name, "customer_schedule_item": ("is", "set")},
			pluck="customer_schedule_item",
		)
	)
	for schedule_item in _touched_schedule_items:
		produced_qty = flt(
			frappe.db.sql(
				"""
				select coalesce(sum(good_qty), 0)
				from `tabAPS Production Allocation`
				where customer_schedule_item = %s and is_effective = 1
				""",
				schedule_item,
			)[0][0]
		)
		frappe.db.set_value(
			"Customer Delivery Schedule Item",
			schedule_item,
			"produced_qty",
			produced_qty,
			update_modified=False,
		)
	return {
		"effective_allocation_count": len(rows),
		"good_qty": sum(flt(row.good_qty) for row in rows),
		"scrap_qty": sum(flt(row.scrap_qty) for row in rows),
		"status_counts": dict(status_counts),
	}


def _reset_inactive_segment_actuals(result_names, now_value, *, active_segment_names=None) -> None:
	active_segment_names = set(active_segment_names or [])
	all_segments = frappe.get_all(
		"APS Schedule Segment",
		filters={
			"parent": ("in", result_names or ["__missing__"]),
			"parenttype": "APS Schedule Result",
		},
		fields=["name"],
	)
	for row in all_segments:
		segment_name = row if isinstance(row, str) else row.get("name")
		if segment_name in active_segment_names:
			continue
		frappe.db.set_value(
			"APS Schedule Segment",
			segment_name,
			{
				"actual_status": "Not Started",
				"actual_completed_qty": 0,
				"actual_good_qty": 0,
				"actual_scrap_qty": 0,
				"actual_start_time": None,
				"actual_end_time": None,
				"delay_minutes": 0,
				"last_execution_sync_on": now_value,
				"last_actual_report_time": None,
				"execution_source_documents": "",
			},
			update_modified=False,
		)


def _derive_segment_execution_status(context, actual_qty, actual_start, actual_end, now_value):
	planned_qty = flt(context.get("planned_qty"))
	end_time = get_datetime(context.get("end_time"))
	if actual_qty > planned_qty * 1.02 and planned_qty > 0:
		return "Overproduced", 0
	if actual_qty >= planned_qty and planned_qty > 0:
		return "Completed", 0
	if actual_start or actual_qty > 0:
		if now_value > end_time:
			return "Delayed", max((now_value - end_time).total_seconds() / 60, 0)
		start_time = get_datetime(context.get("start_time"))
		elapsed = max((now_value - start_time).total_seconds() / 60, 0)
		total = max((end_time - start_time).total_seconds() / 60, 1)
		if elapsed / total > 0.4 and (actual_qty / planned_qty if planned_qty else 0) + 0.15 < elapsed / total:
			return "Slow Progress", 0
		return "Running", 0
	if now_value > end_time:
		return "No Recent Update", max((now_value - end_time).total_seconds() / 60, 0)
	return "Not Started", 0
