from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

import frappe
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


QTY_TOLERANCE = 0.000001


def project_segment_output(
	segment: dict[str, Any],
	at_time,
	actual_events: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
	"""Return cumulative actual and remaining forecast; actual reports reset the future curve."""
	actual_events = sorted(
		(actual_events or []),
		key=lambda row: (get_datetime(row.get("time")), row.get("source") or ""),
	)
	at_time = get_datetime(at_time)
	start = get_datetime(segment.get("start_time"))
	end = get_datetime(segment.get("end_time"))
	planned_qty = max(flt(segment.get("planned_qty")), 0)
	actual_through = sum(
		flt(row.get("qty")) for row in actual_events if get_datetime(row.get("time")) <= at_time
	)
	if at_time <= start:
		projected = actual_through
	elif actual_events and any(get_datetime(row.get("time")) <= at_time for row in actual_events):
		last_report = max(
			get_datetime(row.get("time"))
			for row in actual_events
			if get_datetime(row.get("time")) <= at_time
		)
		anchor_time = max(last_report, start)
		if at_time <= anchor_time or end <= anchor_time:
			projected = actual_through
		else:
			fraction = min(max((at_time - anchor_time).total_seconds() / (end - anchor_time).total_seconds(), 0), 1)
			projected = actual_through + max(planned_qty - actual_through, 0) * fraction
	else:
		fraction = min(max((at_time - start).total_seconds() / max((end - start).total_seconds(), 1), 0), 1)
		projected = planned_qty * fraction
	if at_time >= end:
		projected = max(projected, planned_qty if actual_through < planned_qty else actual_through)
	return {
		"actual_qty": actual_through,
		"projected_cumulative_qty": projected,
		"projected_remaining_qty": max(projected - actual_through, 0),
	}


def get_run_fulfillment_projection(run_name: str, persist: bool = False, as_of=None) -> dict[str, Any]:
	as_of = get_datetime(as_of or now_datetime())
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
			"name",
			"company",
			"customer",
			"item_code",
			"requested_date",
			"demand_source",
			"production_strategy",
			"planned_qty",
			"prebuild_qty",
			"jit_qty",
			"early_days",
			"late_qty_before_balance",
			"late_qty_after_balance",
			"produced_qty",
			"delivered_qty",
		],
		order_by="requested_date asc, creation asc",
	)
	if not results:
		return {"run": run_name, "as_of": as_of, "summary": _empty_summary(), "results": []}
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={
			"parent": ("in", [row.name for row in results]),
			"parenttype": "APS Schedule Result",
			"segment_kind": ("in", ["Primary", "Manual"]),
			"segment_status": ("not in", ["Blocked", "Cancelled"]),
		},
		fields=[
			"name",
			"parent",
			"start_time",
			"end_time",
			"planned_qty",
			"production_mode",
			"load_percent",
			"capacity_bucket_start",
			"capacity_bucket_end",
		],
		order_by="start_time asc, idx asc",
	)
	segments_by_result = defaultdict(list)
	for row in segments:
		segments_by_result[row.parent].append(dict(row))
	production_rows = _get_production_rows(run_name)
	production_by_segment = defaultdict(list)
	production_by_result = defaultdict(list)
	for row in production_rows:
		production_by_segment[row["segment"]].append(row)
		production_by_result[row["schedule_result"]].append(row)
	schedule_targets = _get_result_schedule_targets(results)
	delivery_rows = _get_delivery_rows(schedule_targets)
	delivery_by_target = defaultdict(list)
	for row in delivery_rows:
		delivery_by_target[row["customer_schedule_item"]].append(row)
	stock_by_item = _get_company_finished_goods_stock(run_doc.company)
	opening_stock_by_item = _derive_opening_stock_by_item(
		run_doc.company,
		stock_by_item,
		as_of,
	)
	remaining_opening_stock = defaultdict(float, opening_stock_by_item)
	projections = []
	for result in results:
		open_demand = max(flt(result.planned_qty) - flt(result.delivered_qty), 0)
		opening_qty = min(open_demand, remaining_opening_stock[result.item_code])
		remaining_opening_stock[result.item_code] = max(
			remaining_opening_stock[result.item_code] - opening_qty,
			0,
		)
		projection = _build_result_projection(
			result,
			segments_by_result.get(result.name) or [],
			production_by_segment,
			production_by_result.get(result.name) or [],
			schedule_targets.get(result.name) or [],
			delivery_by_target,
			opening_qty=opening_qty,
			as_of=as_of,
		)
		projections.append(projection)
		if persist:
			_persist_result_projection(result.name, projection)
	summary = {
		"result_count": len(projections),
		"planned_qty": sum(flt(row.get("planned_qty")) for row in projections),
		"prebuild_qty": sum(flt(row.get("prebuild_qty")) for row in projections),
		"jit_qty": sum(flt(row.get("jit_qty")) for row in projections),
		"actual_good_qty": sum(flt(row.get("actual_good_qty")) for row in projections),
		"scrap_qty": sum(flt(row.get("scrap_qty")) for row in projections),
		"current_deliverable_qty": sum(flt(row.get("current_deliverable_qty")) for row in projections),
		"delivered_qty": sum(flt(row.get("delivered_qty")) for row in projections),
		"prebuild_inventory_qty": sum(flt(row.get("prebuild_inventory_qty")) for row in projections),
		"cancellation_inventory_risk_qty": sum(
			flt(row.get("cancellation_inventory_risk_qty")) for row in projections
		),
	}
	if persist:
		frappe.db.set_value(
			"APS Planning Run",
			run_name,
			{
				"total_scrap_qty": summary["scrap_qty"],
				"total_current_deliverable_qty": summary["current_deliverable_qty"],
				"total_prebuild_inventory_qty": summary["prebuild_inventory_qty"],
				"total_cancellation_inventory_risk_qty": summary["cancellation_inventory_risk_qty"],
			},
			update_modified=False,
		)
	return {"run": run_name, "as_of": as_of, "summary": summary, "results": projections}


def get_result_fulfillment_projection(result_name: str, as_of=None) -> dict[str, Any]:
	run_name = frappe.db.get_value("APS Schedule Result", result_name, "planning_run")
	projection = get_run_fulfillment_projection(run_name, persist=False, as_of=as_of)
	return next((row for row in projection["results"] if row["result"] == result_name), {})


def recalculate_run_fulfillment(run_name: str) -> dict[str, Any]:
	return get_run_fulfillment_projection(run_name, persist=True)


def _build_result_projection(
	result,
	segments,
	production_by_segment,
	production_rows,
	targets,
	delivery_by_target,
	*,
	opening_qty,
	as_of,
):
	production_events = []
	production_documents = []
	actual_good_qty = 0.0
	scrap_qty = 0.0
	prebuild_good_qty = 0.0
	jit_good_qty = 0.0
	segment_by_name = {row["name"]: row for row in segments}
	for row in production_rows:
		actual_good_qty += flt(row.get("good_qty"))
		scrap_qty += flt(row.get("scrap_qty"))
		production_documents.append(row.get("source_stock_entry"))
		mode = (segment_by_name.get(row.get("segment")) or {}).get("production_mode") or "JIT"
		if mode == "Prebuild":
			prebuild_good_qty += flt(row.get("good_qty"))
		else:
			jit_good_qty += flt(row.get("good_qty"))
		if flt(row.get("good_qty")) > 0 and row.get("source_posting_time"):
			production_events.append(
				{
					"segment": row.get("segment"),
					"time": get_datetime(row.get("source_posting_time")),
					"qty": flt(row.get("good_qty")),
					"source": row.get("source_stock_entry"),
				}
			)
	delivery_events = []
	delivery_documents = []
	for target in targets:
		for row in delivery_by_target.get(target["name"]) or []:
			delivery_events.append(
				{
					"time": get_datetime(row["source_posting_time"]),
					"qty": flt(row["effective_qty"]),
					"source": row["source_delivery_note"],
				}
			)
			delivery_documents.append(row["source_delivery_note"])
	delivered_qty = sum(flt(row["qty"]) for row in delivery_events)
	if not delivery_events:
		delivered_qty = flt(result.delivered_qty)
	allocated_qty = sum(flt(row.get("allocated_qty")) for row in targets)
	points = _build_projection_points(segments, production_events, delivery_events, as_of)
	timeline = []
	for point in points:
		actual_through = 0.0
		projected_remaining = 0.0
		for segment in segments:
			events = [row for row in production_events if row["segment"] == segment["name"]]
			segment_projection = project_segment_output(segment, point, events)
			actual_through += segment_projection["actual_qty"]
			projected_remaining += segment_projection["projected_remaining_qty"]
		delivered_through = sum(flt(row["qty"]) for row in delivery_events if row["time"] <= point)
		current_available = max(opening_qty + actual_through - allocated_qty - delivered_through, 0)
		projected_available = max(current_available + projected_remaining, 0)
		timeline.append(
			{
				"time": point,
				"opening_finished_goods_qty": opening_qty,
				"cumulative_actual_good_qty": actual_through,
				"projected_remaining_qty": projected_remaining,
				"allocated_qty": allocated_qty,
				"cumulative_delivered_qty": delivered_through,
				"current_available_to_promise_qty": current_available,
				"projected_available_to_promise_qty": projected_available,
			}
		)
	current_row = max((row for row in timeline if row["time"] <= as_of), key=lambda row: row["time"], default=None)
	if not current_row:
		current_row = timeline[0] if timeline else {
			"current_available_to_promise_qty": 0,
			"projected_available_to_promise_qty": 0,
		}
	open_qty = max(flt(result.planned_qty) - delivered_qty, 0)
	current_deliverable = min(open_qty, flt(current_row["current_available_to_promise_qty"]))
	prebuild_inventory_qty = max(prebuild_good_qty - max(delivered_qty, 0), 0)
	cancellation_risk = max(actual_good_qty - delivered_qty - open_qty, 0)
	last_report = max((row["time"] for row in production_events), default=None)
	return {
		"result": result.name,
		"customer": result.customer,
		"item_code": result.item_code,
		"requested_date": result.requested_date,
		"production_strategy": result.production_strategy or "Auto Balance",
		"planned_qty": flt(result.planned_qty),
		"prebuild_qty": flt(result.prebuild_qty),
		"jit_qty": flt(result.jit_qty),
		"early_days": flt(result.early_days),
		"late_qty_before_balance": flt(result.late_qty_before_balance),
		"late_qty_after_balance": flt(result.late_qty_after_balance),
		"actual_good_qty": actual_good_qty,
		"scrap_qty": scrap_qty,
		"prebuild_actual_good_qty": prebuild_good_qty,
		"jit_actual_good_qty": jit_good_qty,
		"opening_finished_goods_qty": opening_qty,
		"allocated_qty": allocated_qty,
		"delivered_qty": delivered_qty,
		"current_deliverable_qty": current_deliverable,
		"current_available_to_promise_qty": flt(current_row["current_available_to_promise_qty"]),
		"projected_available_to_promise_qty": flt(current_row["projected_available_to_promise_qty"]),
		"projected_peak_inventory_qty": max(
			(flt(row["projected_available_to_promise_qty"]) for row in timeline),
			default=0,
		),
		"prebuild_inventory_qty": prebuild_inventory_qty,
		"cancellation_inventory_risk_qty": cancellation_risk,
		"last_actual_report_time": last_report,
		"production_source_documents": list(dict.fromkeys(row for row in production_documents if row)),
		"delivery_source_documents": list(dict.fromkeys(row for row in delivery_documents if row)),
		"load_buckets": _build_load_buckets(segments),
		"timeline": timeline,
	}


def _get_production_rows(run_name):
	return [
		dict(row)
		for row in frappe.get_all(
			"APS Production Allocation",
			filters={"planning_run": run_name, "is_effective": 1},
			fields=[
				"schedule_result",
				"segment",
				"source_stock_entry",
				"source_posting_time",
				"good_qty",
				"scrap_qty",
			],
			order_by="source_posting_time asc, creation asc",
		)
	]


def _get_result_schedule_targets(results):
	result = {}
	for row in results:
		targets = frappe.db.sql(
			"""
			select i.name, i.parent, i.qty, i.allocated_qty, i.schedule_date
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.company = %(company)s
				and ifnull(s.customer, '') = ifnull(%(customer)s, '')
				and i.item_code = %(item_code)s
				and i.schedule_date = %(requested_date)s
			order by s.status = 'Active' desc, s.creation desc, i.idx asc
			""",
			{
				"company": row.company,
				"customer": row.customer,
				"item_code": row.item_code,
				"requested_date": getdate(row.requested_date),
			},
			as_dict=True,
		)
		result[row.name] = [dict(target) for target in targets]
	return result


def _get_delivery_rows(schedule_targets):
	target_names = {
		row["name"]
		for targets in schedule_targets.values()
		for row in targets
	}
	if not target_names:
		return []
	return [
		dict(row)
		for row in frappe.get_all(
			"APS Delivery Allocation",
			filters={"customer_schedule_item": ("in", list(target_names)), "is_effective": 1},
			fields=[
				"customer_schedule_item",
				"source_delivery_note",
				"source_posting_time",
				"effective_qty",
			],
			order_by="source_posting_time asc, creation asc",
		)
	]


def _get_company_finished_goods_stock(company):
	rows = frappe.db.sql(
		"""
		select bin.item_code, coalesce(sum(bin.actual_qty), 0) as qty
		from `tabBin` bin
		inner join `tabWarehouse` warehouse on warehouse.name = bin.warehouse
		where warehouse.company = %s and warehouse.is_group = 0
		group by bin.item_code
		""",
		company,
		as_dict=True,
	)
	return {row.item_code: max(flt(row.qty), 0) for row in rows}


def _derive_opening_stock_by_item(company, current_stock, as_of):
	produced = {
		row.item_code: flt(row.qty)
		for row in frappe.db.sql(
			"""
			select r.item_code, coalesce(sum(a.good_qty), 0) as qty
			from `tabAPS Production Allocation` a
			inner join `tabAPS Schedule Result` r on r.name = a.schedule_result
			where r.company = %s and a.is_effective = 1 and a.source_posting_time <= %s
			group by r.item_code
			""",
			(company, as_of),
			as_dict=True,
		)
	}
	delivered = {
		row.item_code: flt(row.qty)
		for row in frappe.db.sql(
			"""
			select item_code, coalesce(sum(effective_qty), 0) as qty
			from `tabAPS Delivery Allocation`
			where company = %s and is_effective = 1 and source_posting_time <= %s
			group by item_code
			""",
			(company, as_of),
			as_dict=True,
		)
	}
	items = set(current_stock) | set(produced) | set(delivered)
	return {
		item: max(flt(current_stock.get(item)) - flt(produced.get(item)) + flt(delivered.get(item)), 0)
		for item in items
	}


def _build_projection_points(segments, production_events, delivery_events, as_of):
	points = {get_datetime(as_of)}
	for segment in segments:
		start = get_datetime(segment["start_time"])
		end = get_datetime(segment["end_time"])
		points.update((start, end))
		cursor = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
		while cursor < end:
			points.add(cursor)
			cursor += timedelta(hours=1)
	for event in [*production_events, *delivery_events]:
		points.add(get_datetime(event["time"]))
	return sorted(points)


def _build_load_buckets(segments):
	rows = {}
	for segment in segments:
		key = str(segment.get("capacity_bucket_start") or segment.get("start_time"))
		current = rows.setdefault(
			key,
			{
				"start": segment.get("capacity_bucket_start") or segment.get("start_time"),
				"end": segment.get("capacity_bucket_end") or segment.get("end_time"),
				"load_percent": 0,
				"planned_qty": 0,
			},
		)
		current["load_percent"] = max(flt(current["load_percent"]), flt(segment.get("load_percent")))
		current["planned_qty"] += flt(segment.get("planned_qty"))
	return sorted(rows.values(), key=lambda row: get_datetime(row["start"]))


def _persist_result_projection(result_name, projection):
	frappe.db.set_value(
		"APS Schedule Result",
		result_name,
		{
			"good_produced_qty": projection["actual_good_qty"],
			"scrap_qty": projection["scrap_qty"],
			"current_deliverable_qty": projection["current_deliverable_qty"],
			"prebuild_inventory_qty": projection["prebuild_inventory_qty"],
			"cancellation_inventory_risk_qty": projection["cancellation_inventory_risk_qty"],
			"projected_peak_inventory_qty": projection["projected_peak_inventory_qty"],
			"last_actual_report_time": projection["last_actual_report_time"],
			"execution_source_documents": "\n".join(
				[
					*("Stock Entry: {0}".format(name) for name in projection["production_source_documents"]),
					*("Delivery Note: {0}".format(name) for name in projection["delivery_source_documents"]),
				]
			),
		},
		update_modified=False,
	)


def _empty_summary():
	return {
		"result_count": 0,
		"planned_qty": 0,
		"prebuild_qty": 0,
		"jit_qty": 0,
		"actual_good_qty": 0,
		"scrap_qty": 0,
		"current_deliverable_qty": 0,
		"delivered_qty": 0,
		"prebuild_inventory_qty": 0,
		"cancellation_inventory_risk_qty": 0,
	}
