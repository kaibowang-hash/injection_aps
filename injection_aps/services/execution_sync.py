from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


QTY_TOLERANCE = 0.000001
ACTIVE_WOS_STATUSES = ("Manufacture",)
PHYSICAL_SEGMENT_KINDS = ("Primary", "Manual")


def sync_production_for_run(run_name: str) -> dict[str, Any]:
	"""Reconcile formal Manufacture rows to APS segments through an idempotent detail ledger."""
	from injection_aps.services import availability, consistency

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
		frappe.db.release_savepoint(save_point)
		return {
			"run": run_name,
			"source_detail_count": len(sources),
			"desired_allocation_count": len(desired),
			"ledger": ledger_summary,
			"rollup": rollup,
			"consistency": consistency_summary,
			"fulfillment": fulfillment,
		}
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def queue_production_sync(doc, method: str | None = None):
	"""Queue affected run reconciliation after a Manufacture submit/cancel transaction commits."""
	if (doc.get("purpose") or "") != "Manufacture":
		return
	for run_name in get_affected_production_runs(doc):
		frappe.enqueue(
			"injection_aps.services.execution_sync.sync_production_for_run",
			queue="short",
			enqueue_after_commit=True,
			job_id="aps-production-sync-{0}".format(run_name),
			deduplicate=True,
			run_name=run_name,
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
			seg.linked_work_order as work_order,
			seg.linked_work_order_scheduling as work_order_scheduling,
			seg.linked_scheduling_item as scheduling_item,
			r.company,
			r.customer,
			r.item_code,
			r.requested_date
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` r on r.name = seg.parent
		where seg.parenttype = 'APS Schedule Result'
			and r.planning_run = %(run_name)s
			and seg.segment_kind in ('Primary', 'Manual')
			and ifnull(seg.segment_status, '') != 'Blocked'
		order by seg.start_time asc, seg.idx asc, seg.name asc
		""",
		{"run_name": run_name},
		as_dict=True,
	)
	contexts = [dict(row) for row in rows]
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


def _get_formal_manufacture_sources(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
	work_orders = sorted({row.get("work_order") for row in contexts if row.get("work_order")})
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
	if not work_orders and not wos_names:
		return []
	conditions = []
	params: dict[str, Any] = {}
	if work_orders:
		conditions.append(
			"(ifnull(se.work_order_scheduling, '') = '' and se.work_order in %(work_orders)s)"
		)
		params["work_orders"] = work_orders
	if wos_names:
		conditions.append("se.work_order_scheduling in %(wos_names)s")
		params["wos_names"] = wos_names
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
			detail.qty as source_qty,
			detail.t_warehouse,
			wo.scrap_warehouse
		from `tabStock Entry` se
		inner join `tabStock Entry Detail` detail on detail.parent = se.name
		left join `tabWork Order` wo on wo.name = se.work_order
		where se.purpose = 'Manufacture'
			and se.docstatus = 1
			and detail.is_finished_item = 1
			and detail.qty > 0
			and ({conditions})
		order by se.posting_date asc, se.posting_time asc, se.creation asc, detail.idx asc, detail.name asc
		""".format(conditions=" or ".join(conditions)),
		params,
		as_dict=True,
	)
	result = []
	for row in rows:
		output_type = row.explicit_output_type or (
			"Scrap" if row.scrap_warehouse and row.t_warehouse == row.scrap_warehouse else "Good"
		)
		posting_time = get_datetime(f"{getdate(row.posting_date)} {row.posting_time or '00:00:00'}")
		result.append({**dict(row), "output_type": output_type, "source_posting_time": posting_time})
	return result


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
			key = (context["segment"], source["output_type"])
			quota = _context_output_quota(context, source["output_type"])
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
			used_by_context_output[(context["segment"], source["output_type"])] += remaining
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
	if source.get("direct_segment") in context_by_segment:
		return [context_by_segment[source["direct_segment"]]], "Direct"
	if source.get("direct_scheduling_item") in context_by_item:
		return [context_by_item[source["direct_scheduling_item"]]], "Direct"
	wos = source.get("work_order_scheduling")
	if wos:
		wos_row = frappe.db.get_value(
			"Work Order Scheduling",
			wos,
			["status", "custom_aps_run", "custom_aps_approval_state"],
			as_dict=True,
		)
		if not wos_row or wos_row.status not in ACTIVE_WOS_STATUSES:
			return [], "Execution Detail FIFO"
		if wos_row.custom_aps_run and wos_row.custom_aps_run != run_name:
			return [], "Execution Detail FIFO"
		if (wos_row.custom_aps_approval_state or "") in ("Rejected", "Cancelled"):
			return [], "Execution Detail FIFO"
		matching = [
			row
			for row in contexts
			if source.get("work_order") in ({row.get("work_order")} | {item.get("work_order") for item in row.get("scheduling_items") or []})
			and wos in ({row.get("work_order_scheduling")} | {item.get("parent") for item in row.get("scheduling_items") or []})
		]
		return sorted(matching, key=_context_sort_key), "Execution Detail FIFO"
	matching = [row for row in contexts if row.get("work_order") == source.get("work_order")]
	eligible_runs = set(_get_eligible_work_order_runs(source.get("work_order")))
	if eligible_runs != {run_name}:
		return [], "Execution Detail FIFO"
	return sorted(matching, key=_context_sort_key), "Execution Detail FIFO"


def _get_eligible_work_order_runs(work_order: str | None) -> list[str]:
	if not work_order:
		return []
	return frappe.db.sql_list(
		"""
		select distinct r.planning_run
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` r on r.name = seg.parent
		inner join `tabAPS Planning Run` run on run.name = r.planning_run
		where seg.parenttype = 'APS Schedule Result'
			and seg.linked_work_order = %s
			and seg.segment_kind in ('Primary', 'Manual')
			and ifnull(seg.segment_status, 'Planned') not in ('Blocked', 'Cancelled')
			and ifnull(seg.planned_qty, 0) > 0
			and ifnull(run.status, 'Draft') != 'Closed'
			and ifnull(run.approval_state, 'Pending') != 'Rejected'
		order by r.planning_run asc
		""",
		work_order,
	)


def _context_output_quota(context: dict[str, Any], output_type: str) -> float:
	fieldname = "defect_qty" if output_type == "Scrap" else "completed_qty"
	qty = sum(flt(row.get(fieldname)) for row in context.get("scheduling_items") or [])
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
	result = {}
	for context in contexts:
		result_name = context["schedule_result"]
		if result_name in result:
			continue
		rows = frappe.db.sql(
			"""
			select i.name, i.parent, i.qty, i.sales_order, i.schedule_date, i.idx
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.status = 'Active'
				and s.company = %(company)s
				and ifnull(s.customer, '') = ifnull(%(customer)s, '')
				and i.item_code = %(item_code)s
				and i.schedule_date = %(requested_date)s
			order by i.schedule_date asc, ifnull(i.sales_order, '') asc, s.creation asc, i.idx asc, i.name asc
			""",
			{
				"company": context.get("company"),
				"customer": context.get("customer"),
				"item_code": context.get("item_code"),
				"requested_date": getdate(context.get("requested_date")),
			},
			as_dict=True,
		)
		result[result_name] = [dict(row) for row in rows]
	return result


def _split_production_to_schedule_targets(context, qty, output_type, target_map, used_schedule_good):
	targets = target_map.get(context["schedule_result"]) or []
	if not targets:
		return [(None, qty)]
	if output_type == "Scrap":
		return [(targets[0], qty)]
	remaining = qty
	rows = []
	for target in targets:
		available = max(flt(target.get("qty")) - used_schedule_good[target["name"]], 0)
		allocated = min(remaining, available)
		if allocated > QTY_TOLERANCE:
			rows.append((target, allocated))
			used_schedule_good[target["name"]] += allocated
			remaining -= allocated
		if remaining <= QTY_TOLERANCE:
			break
	if remaining > QTY_TOLERANCE:
		if rows and rows[-1][0]["name"] == targets[-1]["name"]:
			rows[-1] = (targets[-1], rows[-1][1] + remaining)
		else:
			rows.append((targets[-1], remaining))
		used_schedule_good[targets[-1]["name"]] += remaining
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
			doc.update(values)
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
		doc.source_docstatus = docstatus
		doc.effective_qty = 0
		doc.reversed_qty = max(flt(doc.reversed_qty), previous_effective)
		doc.is_effective = 0
		doc.reversal_reason = "Source Stock Entry cancelled" if docstatus == 2 else "Source is no longer eligible"
		doc.last_synced_on = now_datetime()
		doc.save(ignore_permissions=True)
		reversed_count += cint(previous_effective > QTY_TOLERANCE)
	return {"created": created, "updated": updated, "reversed": reversed_count}


def _rollup_production_allocations(run_name: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
	from injection_aps.services import planning

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
	by_segment = defaultdict(list)
	by_result = defaultdict(list)
	for row in rows:
		by_segment[row.segment].append(row)
		by_result[row.schedule_result].append(row)
	now_value = now_datetime()
	status_counts = defaultdict(int)
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
	for result_name in {row["schedule_result"] for row in contexts}:
		result_rows = by_result.get(result_name) or []
		result_segments = frappe.get_all(
			"APS Schedule Segment",
			filters={"parent": result_name, "parenttype": "APS Schedule Result"},
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
