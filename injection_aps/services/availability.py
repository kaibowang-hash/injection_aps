from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


QTY_TOLERANCE = 0.000001


def project_segment_output(
	segment: dict[str, Any],
	at_time,
	actual_events: list[dict[str, Any]] | None = None,
	*,
	as_of=None,
) -> dict[str, float]:
	"""Return cumulative actual and remaining forecast; actual reports reset the future curve."""
	actual_events = sorted(
		(actual_events or []),
		key=lambda row: (get_datetime(row.get("time")), row.get("source") or ""),
	)
	at_time = get_datetime(at_time)
	as_of = get_datetime(as_of or at_time)
	start = get_datetime(segment.get("start_time"))
	end = get_datetime(segment.get("end_time"))
	planned_qty = max(flt(segment.get("planned_qty")), 0)
	actual_through = sum(
		flt(row.get("qty")) for row in actual_events if get_datetime(row.get("time")) <= at_time
	)
	consumed_through = sum(
		max(flt(row.get("qty")), 0) + max(flt(row.get("scrap_qty")), 0)
		for row in actual_events
		if get_datetime(row.get("time")) <= at_time
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
			projected = actual_through + max(planned_qty - consumed_through, 0) * fraction
	else:
		fraction = min(max((at_time - start).total_seconds() / max((end - start).total_seconds(), 1), 0), 1)
		projected = planned_qty * fraction
	if at_time >= end and end <= as_of:
		# An expired segment is no longer a valid source of future supply.  Keep the
		# quantity that was actually reported, but never release its unreported
		# balance merely because the planned end time passed.
		projected = actual_through
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
			"sales_order",
			"sales_order_item",
			"net_requirement",
			"item_code",
			"requested_date",
			"demand_source",
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
			"planned_qty",
			"prebuild_qty",
			"jit_qty",
			"early_days",
			"late_qty_before_balance",
			"late_qty_after_balance",
			"produced_qty",
			"delivered_qty",
			"fulfillment_baseline_json",
		],
		order_by="requested_date asc, creation asc",
	)
	if not results:
		return {
			"run": run_name,
			"as_of": as_of,
			"summary": _empty_summary(),
			"warning_count": 0,
			"warnings": [],
			"results": [],
		}
	_annotate_result_fulfillment_demands(results)
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
	warnings = []
	_append_unresolved_fulfillment_warnings(schedule_targets, warnings)
	backlog_delivery_events_by_result = _get_backlog_incremental_delivery_events_map(
		results,
		warnings=warnings,
	)
	backlog_delivery_by_result = {
		result_name: sum(
			flt(event.get("qty"))
			for event in events
			if event.get("time") and get_datetime(event["time"]) <= as_of
		)
		for result_name, events in backlog_delivery_events_by_result.items()
	}
	delivery_rows = _get_delivery_rows(schedule_targets)
	delivery_by_target = defaultdict(list)
	for row in delivery_rows:
		delivery_by_target[row["customer_schedule_item"]].append(row)
	result_item_by_name = {row.name: row.item_code for row in results}
	run_produced_through = defaultdict(float)
	for row in production_rows:
		item_code = result_item_by_name.get(row.get("schedule_result"))
		if item_code and row.get("source_posting_time") and get_datetime(row["source_posting_time"]) <= as_of:
			run_produced_through[item_code] += flt(row.get("good_qty"))
	target_by_name = {
		target["name"]: target
		for targets in schedule_targets.values()
		for target in targets
	}
	target_item_by_name = {
		target["name"]: result_item_by_name.get(result_name)
		for result_name, targets in schedule_targets.items()
		for target in targets
	}
	run_delivered_through = defaultdict(float)
	for target_name, rows in delivery_by_target.items():
		item_code = target_item_by_name.get(target_name)
		if not item_code:
			continue
		target = target_by_name.get(target_name)
		for row in _get_attributed_delivery_rows(target or {}, rows):
			if row.get("source_posting_time") and get_datetime(row["source_posting_time"]) <= as_of:
				run_delivered_through[item_code] += flt(row.get("effective_qty"))
	backlog_delivered_after = defaultdict(float)
	for result_name, events in backlog_delivery_events_by_result.items():
		item_code = result_item_by_name.get(result_name)
		if not item_code:
			continue
		for event in events:
			if not event.get("time"):
				continue
			if get_datetime(event["time"]) <= as_of:
				run_delivered_through[item_code] += flt(event.get("qty"))
			elif not cint(event.get("tracked_by_aps_allocation")):
				backlog_delivered_after[item_code] += flt(event.get("qty"))
	stock_by_item = _get_company_fulfillment_finished_goods_stock(
		run_doc.company,
		results,
		warnings=warnings,
	)
	opening_stock_by_item = _derive_opening_stock_by_item(
		run_doc.company,
		stock_by_item,
		as_of,
		run_produced_through=run_produced_through,
		run_delivered_through=run_delivered_through,
		untracked_delivered_after=backlog_delivered_after,
		warnings=warnings,
	)
	remaining_opening_stock = defaultdict(float, opening_stock_by_item)
	remaining_current_stock = defaultdict(float, stock_by_item)
	projections = []
	for result in results:
		delivered_for_opening = _estimate_result_delivered_qty(
			result,
			schedule_targets.get(result.name) or [],
			delivery_by_target,
			as_of=as_of,
			backlog_delivered_qty=backlog_delivery_by_result.get(result.name),
		)
		fulfillment_demand_qty = _get_result_fulfillment_demand_qty(result)
		open_demand = max(fulfillment_demand_qty - delivered_for_opening, 0)
		opening_qty = min(open_demand, remaining_opening_stock[result.item_code])
		remaining_opening_stock[result.item_code] = max(
			remaining_opening_stock[result.item_code] - opening_qty,
			0,
		)
		current_stock_limit = min(
			open_demand,
			max(flt(remaining_current_stock[result.item_code]), 0),
		)
		remaining_current_stock[result.item_code] = max(
			flt(remaining_current_stock[result.item_code]) - current_stock_limit,
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
			current_physical_stock_limit=current_stock_limit,
			as_of=as_of,
			backlog_delivered_qty=backlog_delivery_by_result.get(result.name),
			backlog_delivery_events=backlog_delivery_events_by_result.get(result.name, []),
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
		"warning_count": len(warnings),
		"warnings": warnings,
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
	return {
		"run": run_name,
		"as_of": as_of,
		"summary": summary,
		"warning_count": len(warnings),
		"warnings": warnings,
		"results": projections,
	}


def get_result_fulfillment_projection(result_name: str, as_of=None) -> dict[str, Any]:
	run_name = frappe.db.get_value("APS Schedule Result", result_name, "planning_run")
	projection = get_run_fulfillment_projection(run_name, persist=False, as_of=as_of)
	return next((row for row in projection["results"] if row["result"] == result_name), {})


def recalculate_run_fulfillment(run_name: str) -> dict[str, Any]:
	return get_run_fulfillment_projection(run_name, persist=True)


def _annotate_result_fulfillment_demands(results) -> None:
	"""Attach the live NR fallback once without treating production plan as demand."""
	net_requirement_names = sorted(
		{
			row.get("net_requirement")
			for row in results or []
			if row.get("net_requirement")
		}
	)
	live_demand_by_name = (
		{
			row.name: max(flt(row.demand_qty), 0)
			for row in frappe.get_all(
				"APS Net Requirement",
				filters={"name": ("in", net_requirement_names)},
				fields=["name", "demand_qty"],
			)
		}
		if net_requirement_names
		else {}
	)
	for result in results or []:
		result["net_requirement_demand_qty"] = live_demand_by_name.get(
			result.get("net_requirement")
		)
		result["fulfillment_demand_qty"] = _get_result_fulfillment_demand_qty(result)


def _get_result_fulfillment_demand_qty(result, *, targets=None) -> float:
	"""Return frozen customer demand, never the possibly rounded production lot.

	Minimum-batch and stock-only Results make ``planned_qty`` unsuitable as a
	fulfillment boundary.  Version-3 lineage is authoritative: exact customer
	schedule/SO-item source quantities win, followed by the frozen/live Net
	Requirement demand.  ``targets`` is a legacy read-only fallback for old
	customer-schedule Results that still have an unambiguous attributed target.
	"""
	if result.get("fulfillment_demand_qty") not in (None, ""):
		return max(flt(result.get("fulfillment_demand_qty")), 0)

	baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
	if isinstance(baseline, dict) and cint(baseline.get("version")) >= 3:
		target_demand = _sum_frozen_source_open_qty(
			baseline.get("targets"),
			result=result,
			row_kind="Customer Schedule",
		)
		if target_demand is not None:
			return target_demand
		sales_order_demand = _sum_frozen_source_open_qty(
			baseline.get("sales_order_items"),
			result=result,
			row_kind="Sales Order Item",
		)
		if sales_order_demand is not None:
			return sales_order_demand
		net_requirement = baseline.get("net_requirement")
		if isinstance(net_requirement, dict) and "demand_qty" in net_requirement:
			return max(flt(net_requirement.get("demand_qty")), 0)

	if result.get("net_requirement_demand_qty") not in (None, ""):
		return max(flt(result.get("net_requirement_demand_qty")), 0)

	legacy_targets = [row for row in targets or [] if isinstance(row, dict)]
	if legacy_targets:
		return sum(
			max(flt(row.get("attributed_qty") or row.get("qty")), 0)
			for row in legacy_targets
		)
	return 0.0


def _sum_frozen_source_open_qty(rows, *, result, row_kind: str) -> float | None:
	if not isinstance(rows, list) or not rows:
		return None
	candidates = [row for row in rows if isinstance(row, dict) and not cint(row.get("retired"))]
	if not candidates:
		# A v3 target set that was fully retired is an explicit cancellation.
		return 0.0

	result_sales_order = result.get("sales_order") or ""
	result_sales_order_item = result.get("sales_order_item") or ""
	result_item = result.get("item_code") or ""
	matching = []
	for row in candidates:
		if result_item and (row.get("item_code") or "") != result_item:
			continue
		if (row.get("sales_order") or result_sales_order) and (
			row.get("sales_order") or ""
		) != result_sales_order:
			continue
		if row_kind == "Sales Order Item" and (
			row.get("sales_order_item") or result_sales_order_item
		) and (row.get("sales_order_item") or "") != result_sales_order_item:
			continue
		matching.append(row)
	if not matching or any("source_open_qty" not in row for row in matching):
		return None
	return sum(_get_frozen_target_fulfillment_qty(row) for row in matching)


def _get_frozen_target_fulfillment_qty(row) -> float:
	"""Return the accepted fulfillment cap without erasing the original epoch."""
	value = (
		row.get("accepted_source_open_qty")
		if row.get("accepted_source_open_qty") not in (None, "")
		else row.get("source_open_qty")
	)
	return max(flt(value), 0)


def _build_result_projection(
	result,
	segments,
	production_by_segment,
	production_rows,
	targets,
	delivery_by_target,
	*,
	opening_qty,
	current_physical_stock_limit=None,
	as_of,
	backlog_delivered_qty=None,
	backlog_delivery_events=None,
):
	as_of = get_datetime(as_of)
	fulfillment_demand_qty = _get_result_fulfillment_demand_qty(result, targets=targets)
	production_events = []
	production_documents = []
	actual_good_qty = 0.0
	scrap_qty = 0.0
	prebuild_good_qty = 0.0
	jit_good_qty = 0.0
	late_good_qty = 0.0
	segment_by_name = {row["name"]: row for row in segments}
	for row in production_rows:
		posting_time = row.get("source_posting_time")
		if not posting_time or get_datetime(posting_time) > as_of:
			continue
		good_qty = flt(row.get("good_qty"))
		row_scrap_qty = flt(row.get("scrap_qty"))
		actual_good_qty += good_qty
		scrap_qty += row_scrap_qty
		production_documents.append(row.get("source_stock_entry"))
		mode = _classify_actual_production_mode(
			result.get("requested_date"),
			posting_time,
			segment=segment_by_name.get(row.get("segment")) or {},
		)
		if mode == "Prebuild":
			prebuild_good_qty += good_qty
		elif mode == "JIT":
			jit_good_qty += good_qty
		elif mode == "Late":
			late_good_qty += good_qty
		if abs(good_qty) > QTY_TOLERANCE or abs(row_scrap_qty) > QTY_TOLERANCE:
			production_events.append(
				{
					"segment": row.get("segment"),
					"time": get_datetime(posting_time),
					"qty": good_qty,
					"scrap_qty": row_scrap_qty,
					"source": row.get("source_stock_entry"),
				}
			)
	delivery_events = []
	delivery_documents = []
	delivery_qty_without_ledger_time = 0.0
	for target in targets:
		raw_delivery_rows = delivery_by_target.get(target["name"]) or []
		if not raw_delivery_rows:
			delivery_qty_without_ledger_time += min(
				max(
					flt(target.get("delivered_qty")) - flt(target.get("opening_delivered_qty")),
					0,
				),
				max(flt(target.get("attributed_qty") or target.get("qty")), 0),
			)
			continue
		for row in _get_attributed_delivery_rows(
			target,
			raw_delivery_rows,
		):
			if not row.get("source_posting_time") or get_datetime(row["source_posting_time"]) > as_of:
				continue
			delivery_events.append(
				{
					"time": get_datetime(row["source_posting_time"]),
					"qty": flt(row["effective_qty"]),
					"source": row["source_delivery_note"],
				}
			)
			delivery_documents.append(row.get("source_delivery_note"))
	if not targets and (result.get("demand_source") or "") == "Sales Order Backlog":
		for event in backlog_delivery_events or []:
			if not event.get("time") or get_datetime(event["time"]) > as_of:
				continue
			delivery_events.append(
				{
					"time": get_datetime(event["time"]),
					"qty": flt(event.get("qty")),
					"source": event.get("source"),
				}
			)
			delivery_documents.append(event.get("source"))
	delivered_qty = delivery_qty_without_ledger_time + sum(flt(row["qty"]) for row in delivery_events)
	if not targets and (result.get("demand_source") or "") == "Sales Order Backlog":
		if backlog_delivery_events is None:
			delivered_qty = (
				flt(backlog_delivered_qty)
				if backlog_delivered_qty is not None
				else _get_persisted_backlog_incremental_delivered_qty(result)
			)
			delivery_qty_without_ledger_time = delivered_qty
	allocated_qty = sum(
		max(flt(row.get("allocated_qty")) - flt(row.get("opening_allocated_qty")), 0)
		for row in targets
	)
	points = _build_projection_points(segments, production_events, delivery_events, as_of)
	timeline = []
	for point in points:
		actual_through = 0.0
		projected_remaining = 0.0
		for segment in segments:
			events = [row for row in production_events if row["segment"] == segment["name"]]
			segment_projection = project_segment_output(
				segment,
				point,
				events,
				as_of=as_of,
			)
			actual_through += segment_projection["actual_qty"]
			projected_remaining += segment_projection["projected_remaining_qty"]
		# Child-row fallback quantities are already reflected in current Bin stock; only
		# timestamped ledger events are added back into opening stock and replayed here.
		delivered_through = sum(flt(row["qty"]) for row in delivery_events if row["time"] <= point)
		# ``allocated_qty`` belongs to these same customer-schedule targets and has
		# already reduced the demand used to create the APS result.  It is coverage,
		# not a second physical stock movement.  Subtracting it here would consume the
		# same quantity again after opening stock / actual output have been assigned
		# to this result (for example: produced 60, delivered 30, allocated 60 would
		# incorrectly show zero instead of 30 available pieces).
		current_available = max(opening_qty + actual_through - delivered_through, 0)
		if current_physical_stock_limit is not None and point >= as_of:
			# Current Bin stock is already net of physical movements.  Cap the
			# replayed APS supply with the exact, reservation-aware stock credit so
			# production attributed to this Result cannot borrow stock reserved for
			# another Sales Order or consume the configured safety-stock floor.
			current_available = min(
				current_available,
				max(flt(current_physical_stock_limit), 0),
			)
		remaining_customer_demand = max(
			fulfillment_demand_qty
			- max(delivery_qty_without_ledger_time + delivered_through, 0),
			0,
		)
		current_available = min(current_available, remaining_customer_demand)
		projected_available = min(
			max(current_available + projected_remaining, 0),
			remaining_customer_demand,
		)
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
	open_qty = max(fulfillment_demand_qty - delivered_qty, 0)
	current_deliverable = min(open_qty, flt(current_row["current_available_to_promise_qty"]))
	prebuild_inventory_qty = max(prebuild_good_qty - max(delivered_qty, 0), 0)
	cancellation_risk = max(actual_good_qty - delivered_qty - open_qty, 0)
	last_report = max((row["time"] for row in production_events), default=None)
	return {
		"result": result.name,
		"customer": result.customer,
		"sales_order": result.get("sales_order"),
		"sales_order_item": result.get("sales_order_item"),
		"item_code": result.item_code,
		"requested_date": result.requested_date,
		"production_strategy": result.production_strategy or "Auto Balance",
		"planned_qty": flt(result.planned_qty),
		"fulfillment_demand_qty": fulfillment_demand_qty,
		"prebuild_qty": flt(result.prebuild_qty),
		"jit_qty": flt(result.jit_qty),
		"early_days": flt(result.early_days),
		"late_qty_before_balance": flt(result.late_qty_before_balance),
		"late_qty_after_balance": flt(result.late_qty_after_balance),
		"actual_good_qty": actual_good_qty,
		"scrap_qty": scrap_qty,
		"prebuild_actual_good_qty": prebuild_good_qty,
		"jit_actual_good_qty": jit_good_qty,
		"late_actual_good_qty": late_good_qty,
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


def _classify_actual_production_mode(requested_date, posting_time, *, segment=None) -> str:
	"""Classify execution by the same natural delivery day used by capacity balance.

	A protected/fixed night segment cannot be physically split after a Work Order is
	active. Its stored whole-segment mode can therefore be stale or represent only
	one side of midnight. Event time is the unambiguous basis for actual progress.
	"""
	segment = segment or {}
	fallback = segment.get("production_mode") or "JIT"
	if not requested_date or not posting_time:
		return fallback
	due_start = get_datetime(f"{getdate(requested_date)} 00:00:00")
	due_end = due_start + timedelta(days=1)
	posting_time = get_datetime(posting_time)
	if posting_time < due_start:
		return "Prebuild"
	if posting_time < due_end:
		return "JIT"
	return "Late"


def _get_attributed_delivery_rows(target, rows) -> list[dict[str, Any]]:
	"""Cap a target's delivery history to the quantity attributed to this APS result."""
	limit = max(flt(target.get("attributed_qty") or target.get("qty")), 0)
	if limit <= QTY_TOLERANCE:
		return []
	physical_balance = 0.0
	opening_delivered_qty = max(flt(target.get("opening_delivered_qty")), 0)
	attributed_balance = 0.0
	result = []
	for source in rows:
		physical_balance = max(physical_balance + flt(source.get("effective_qty")), 0)
		new_attributed_balance = min(max(physical_balance - opening_delivered_qty, 0), limit)
		attributed_delta = new_attributed_balance - attributed_balance
		attributed_balance = new_attributed_balance
		if abs(attributed_delta) <= QTY_TOLERANCE:
			continue
		result.append({**dict(source), "effective_qty": attributed_delta})
	return result


def _estimate_result_delivered_qty(
	result,
	targets,
	delivery_by_target,
	*,
	as_of=None,
	backlog_delivered_qty=None,
) -> float:
	if not targets:
		if (result.get("demand_source") or "") != "Sales Order Backlog":
			return 0
		return (
			flt(backlog_delivered_qty)
			if backlog_delivered_qty is not None
			else _get_persisted_backlog_incremental_delivered_qty(result)
		)
	total = 0.0
	for target in targets:
		rows = delivery_by_target.get(target["name"]) or []
		if rows:
			total += sum(
				flt(row.get("effective_qty"))
				for row in _get_attributed_delivery_rows(target, rows)
				if not as_of
				or (
					row.get("source_posting_time")
					and get_datetime(row["source_posting_time"]) <= get_datetime(as_of)
				)
			)
		else:
			total += min(
				max(
					flt(target.get("delivered_qty")) - flt(target.get("opening_delivered_qty")),
					0,
				),
				max(flt(target.get("attributed_qty") or target.get("qty")), 0),
			)
	return max(total, 0)


def _get_persisted_backlog_incremental_delivered_qty(result) -> float:
	"""Use the consistency rollup only when its exact SO-item baseline is persisted."""
	baseline_row = _get_result_sales_order_item_baseline(result)
	if baseline_row is None:
		return 0.0
	return min(
		max(flt(result.get("delivered_qty")), 0),
		_get_result_fulfillment_demand_qty(result),
	)


def _get_backlog_incremental_delivery_map(results, warnings=None) -> dict[str, float]:
	"""Return post-baseline delivery by exact Sales Order Item without cross-SO borrowing."""
	warnings = warnings if warnings is not None else []
	groups = defaultdict(list)
	for result in results or []:
		if (result.get("demand_source") or "") != "Sales Order Backlog":
			continue
		baseline_row = _get_result_sales_order_item_baseline(result)
		if baseline_row is None:
			warnings.append(
				{
					"code": "BACKLOG_FULFILLMENT_BASELINE_MISSING",
					"result": result.get("name"),
					"message": _(
						"APS result {0} has no exact Sales Order Item delivery baseline; backlog delivery is not credited."
					).format(result.get("name")),
				}
			)
			continue
		key = (
			result.get("company") or "",
			result.get("customer") or "",
			result.get("item_code") or "",
			result.get("sales_order") or "",
			result.get("sales_order_item") or "",
		)
		groups[key].append((result, baseline_row))
	if not groups:
		return {}
	rows = frappe.db.sql(
		"""
		select
			i.name as sales_order_item, i.parent as sales_order, i.item_code,
			coalesce(i.delivered_qty, 0) as delivered_qty,
			s.company, ifnull(s.customer, '') as customer, s.docstatus
		from `tabSales Order Item` i
		inner join `tabSales Order` s on s.name = i.parent
		where i.name in %(sales_order_items)s
		""",
		{"sales_order_items": tuple(sorted({key[4] for key in groups}))},
		as_dict=True,
	)
	physical_by_key = {
		(
			row.get("company") or "",
			row.get("customer") or "",
			row.get("item_code") or "",
			row.get("sales_order") or "",
			row.get("sales_order_item") or "",
		): max(flt(row.get("delivered_qty")), 0)
		for row in rows
		if cint(row.get("docstatus")) == 1
	}
	result = {}
	for key, grouped_results in groups.items():
		physical_delivered = physical_by_key.get(key)
		if physical_delivered is None:
			continue
		claimed_through = 0.0
		for result_row, baseline_row in grouped_results:
			claim_start = max(
				claimed_through,
				max(flt(baseline_row.get("opening_delivered_qty")), 0),
			)
			claimed = min(
				max(physical_delivered - claim_start, 0),
				_get_result_fulfillment_demand_qty(result_row),
			)
			result[result_row.get("name")] = claimed
			claimed_through = claim_start + claimed
	return result


def _get_backlog_incremental_delivery_events_map(
	results,
	warnings=None,
) -> dict[str, list[dict[str, Any]]]:
	"""Build timestamped post-baseline delivery events for exact Sales Order Items.

	The Sales Order Item roll-up is sufficient for a quantity audit but cannot
	answer an ``as_of`` question.  Delivery Note history supplies the event time;
	the frozen SO-item opening quantity determines which part belongs to this run.
	No customer/item-only fallback is allowed here.
	"""
	warnings = warnings if warnings is not None else []
	groups = defaultdict(list)
	for result in results or []:
		if (result.get("demand_source") or "") != "Sales Order Backlog":
			continue
		baseline_row = _get_result_sales_order_item_baseline(result)
		if baseline_row is None:
			warnings.append(
				{
					"code": "BACKLOG_FULFILLMENT_BASELINE_MISSING",
					"result": result.get("name"),
					"message": _(
						"APS result {0} has no exact Sales Order Item delivery baseline; backlog delivery is not credited."
					).format(result.get("name")),
				}
			)
			continue
		key = (
			result.get("company") or "",
			result.get("customer") or "",
			result.get("item_code") or "",
			result.get("sales_order") or "",
			result.get("sales_order_item") or "",
		)
		groups[key].append((result, baseline_row))
	if not groups:
		return {}

	rows = frappe.db.sql(
		"""
		select
			dn.company, ifnull(dn.customer, '') as customer,
			dni.item_code, dni.against_sales_order as sales_order,
			dni.so_detail as sales_order_item,
			dn.name as source_delivery_note,
			concat(dn.posting_date, ' ', ifnull(dn.posting_time, '00:00:00')) as source_posting_time,
			case when dn.is_return = 1 then -1 else 1 end
				* abs(coalesce(nullif(dni.stock_qty, 0), dni.qty, 0)) as effective_qty,
			dn.creation, dni.idx, dni.name as source_delivery_note_item,
			exists(
				select 1
				from `tabAPS Delivery Allocation` allocation
				where allocation.source_delivery_note_item = dni.name
					and allocation.is_effective = 1
			) as tracked_by_aps_allocation
		from `tabDelivery Note` dn
		inner join `tabDelivery Note Item` dni on dni.parent = dn.name
		where dn.docstatus = 1
			and dni.so_detail in %(sales_order_items)s
		order by dn.posting_date asc, dn.posting_time asc, dn.creation asc, dni.idx asc, dni.name asc
		""",
		{"sales_order_items": tuple(sorted({key[4] for key in groups}))},
		as_dict=True,
	)
	rows_by_key = defaultdict(list)
	for row in rows:
		key = (
			row.get("company") or "",
			row.get("customer") or "",
			row.get("item_code") or "",
			row.get("sales_order") or "",
			row.get("sales_order_item") or "",
		)
		if key in groups:
			rows_by_key[key].append(row)

	events_by_result = defaultdict(list)
	for grouped_results in groups.values():
		for result_row, _baseline in grouped_results:
			events_by_result[result_row.get("name")]
	for key, grouped_results in groups.items():
		physical_balance = 0.0
		previous_claims = {result.get("name"): 0.0 for result, _baseline in grouped_results}
		for source in rows_by_key.get(key) or []:
			physical_balance = max(physical_balance + flt(source.get("effective_qty")), 0)
			claims = _claim_backlog_delivery_quantities(grouped_results, physical_balance)
			for result_name, claimed_qty in claims.items():
				delta = claimed_qty - previous_claims.get(result_name, 0.0)
				previous_claims[result_name] = claimed_qty
				if abs(delta) <= QTY_TOLERANCE:
					continue
				events_by_result[result_name].append(
					{
						"time": get_datetime(source.get("source_posting_time")),
						"qty": delta,
						"source": source.get("source_delivery_note"),
						"source_detail": source.get("source_delivery_note_item"),
						"tracked_by_aps_allocation": cint(
							source.get("tracked_by_aps_allocation")
						),
					}
				)
	return dict(events_by_result)


def _claim_backlog_delivery_quantities(grouped_results, physical_delivered) -> dict[str, float]:
	"""Claim one exact SO-item balance once, in deterministic result order."""
	claimed_through = 0.0
	claims = {}
	for result_row, baseline_row in grouped_results:
		claim_start = max(
			claimed_through,
			max(flt(baseline_row.get("opening_delivered_qty")), 0),
		)
		claimed = min(
			max(flt(physical_delivered) - claim_start, 0),
			_get_result_fulfillment_demand_qty(result_row),
		)
		claims[result_row.get("name")] = claimed
		claimed_through = claim_start + claimed
	return claims


def _get_result_sales_order_item_baseline(result) -> dict[str, Any] | None:
	baseline = _parse_fulfillment_baseline(result.get("fulfillment_baseline_json"))
	if baseline is None:
		return None
	sales_order = result.get("sales_order") or ""
	sales_order_item = result.get("sales_order_item") or ""
	item_code = result.get("item_code") or ""
	if not sales_order or not sales_order_item or not item_code:
		return None
	matches = [
		row
		for row in baseline.get("sales_order_items") or []
		if isinstance(row, dict)
		and (row.get("sales_order") or "") == sales_order
		and (row.get("sales_order_item") or "") == sales_order_item
		and (row.get("item_code") or "") == item_code
	]
	return matches[0] if len(matches) == 1 else None


def _append_unresolved_fulfillment_warnings(schedule_targets, warnings) -> None:
	"""Expose partial-target history whose pre-result coverage offset is not persisted."""
	for result_name, targets in (schedule_targets or {}).items():
		for target in targets or []:
			qty = max(flt(target.get("qty")), 0)
			attributed_qty = max(flt(target.get("attributed_qty") or qty), 0)
			if attributed_qty >= qty - QTY_TOLERANCE:
				continue
			if (
				max(flt(target.get("delivered_qty")), 0) <= QTY_TOLERANCE
				and max(flt(target.get("allocated_qty")), 0) <= QTY_TOLERANCE
			):
				continue
			warnings.append(
				{
					"code": "FULFILLMENT_BASELINE_UNRESOLVED",
					"result": result_name,
					"customer_schedule_item": target.get("name"),
					"message": _(
						"APS result {0} covers only part of schedule item {1}, which already has delivery or allocation history. The historical coverage offset is not persisted, so fulfillment quantities for this result cannot be attributed precisely."
					).format(result_name, target.get("name")),
				}
			)


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
	claimed_target_names = set()
	for row in results:
		baseline = _parse_fulfillment_baseline(row.get("fulfillment_baseline_json"))
		if baseline is not None:
			result[row.name] = _get_persisted_result_schedule_targets(
				row,
				baseline,
				claimed_target_names,
			)
			continue
		targets = frappe.db.sql(
			"""
			select
				i.name, i.parent, i.qty, i.allocated_qty, i.produced_qty, i.delivered_qty,
				i.schedule_date, i.sales_order,
				i.production_strategy, i.demand_confidence, i.cancellation_risk_percent,
				i.prebuild_allowed, i.max_prebuild_days
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.company = %(company)s
				and s.status = 'Active'
				and ifnull(s.customer, '') = ifnull(%(customer)s, '')
				and i.item_code = %(item_code)s
				and i.schedule_date = %(requested_date)s
				and ifnull(i.sales_order, '') = ifnull(%(sales_order)s, '')
			order by s.creation asc, i.idx asc, i.name asc
			""",
			{
				"company": row.company,
				"customer": row.customer,
				"item_code": row.item_code,
				"requested_date": getdate(row.requested_date),
				"sales_order": row.get("sales_order"),
			},
			as_dict=True,
		)
		matched_targets = [target for target in targets if _schedule_policy_matches(target, row)]
		if any(
			flt(target.get("allocated_qty")) > QTY_TOLERANCE
			or flt(target.get("produced_qty")) > QTY_TOLERANCE
			or flt(target.get("delivered_qty")) > QTY_TOLERANCE
			for target in matched_targets
		):
			# Legacy result with historical coverage has no trustworthy starting
			# offset.  Do not silently replay its delivery/production from zero.
			matched_targets = []
		result[row.name] = _claim_schedule_targets(
			matched_targets,
			claimed_target_names,
			max_qty=_get_result_fulfillment_demand_qty(row, targets=matched_targets),
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
		return {"targets": []}
	return parsed if isinstance(parsed, dict) else {"targets": []}


def _get_persisted_result_schedule_targets(result_row, baseline, claimed_target_names):
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
			i.name, i.parent, i.qty, i.allocated_qty, i.produced_qty, i.delivered_qty,
			i.schedule_date, i.sales_order,
			i.production_strategy, i.demand_confidence, i.cancellation_risk_percent,
			i.prebuild_allowed, i.max_prebuild_days,
			s.company, s.customer, s.status as schedule_status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name in %(target_names)s and s.status = 'Active'
		order by i.schedule_date asc, i.idx asc, i.name asc
		""",
		{"target_names": sorted(baseline_by_name)},
		as_dict=True,
	)
	remaining = _get_result_fulfillment_demand_qty(result_row)
	claimed = []
	for source in rows:
		name = source.get("name")
		if not name or name in claimed_target_names or remaining <= QTY_TOLERANCE:
			continue
		if (source.get("company") or "") != (result_row.get("company") or "") or (
			source.get("customer") or ""
		) != (result_row.get("customer") or "") or (source.get("sales_order") or "") != (
			result_row.get("sales_order") or ""
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
		target["allocated_qty"] = flt(source.get("allocated_qty"))
		target["produced_qty"] = flt(source.get("produced_qty"))
		target["delivered_qty"] = flt(source.get("delivered_qty"))
		target["attributed_qty"] = attributed_qty
		claimed.append(target)
		claimed_target_names.add(name)
		remaining -= attributed_qty
	return claimed


def _schedule_policy_matches(target, result) -> bool:
	"""Blank child policy values inherit the normalized policy carried by the APS result."""
	text_fields = (
		("production_strategy", "Auto Balance"),
		("demand_confidence", "Confirmed"),
	)
	for fieldname, default in text_fields:
		target_value = target.get(fieldname)
		if target_value in (None, ""):
			continue
		if target_value != (result.get(fieldname) or default):
			return False
	for fieldname in ("cancellation_risk_percent", "max_prebuild_days"):
		target_value = flt(target.get(fieldname))
		if target_value <= QTY_TOLERANCE:
			continue
		if abs(target_value - flt(result.get(fieldname))) > QTY_TOLERANCE:
			return False
	prebuild_allowed = target.get("prebuild_allowed")
	result_prebuild_allowed = 1 if result.get("prebuild_allowed") is None else cint(result.get("prebuild_allowed"))
	if prebuild_allowed not in (None, "") and cint(prebuild_allowed) != result_prebuild_allowed:
		return False
	return True


def _claim_schedule_targets(
	targets,
	claimed_target_names: set[str],
	*,
	max_qty: float | None = None,
) -> list[dict[str, Any]]:
	"""Assign demand-policy-matched schedule lines once, bounded by the result quantity."""
	claimed = []
	claimed_qty = 0.0
	for row in targets:
		if max_qty is not None and max_qty > QTY_TOLERANCE and claimed_qty >= max_qty - QTY_TOLERANCE:
			break
		target = dict(row)
		name = target.get("name")
		if not name or name in claimed_target_names:
			continue
		remaining_qty = max(flt(max_qty) - claimed_qty, 0) if max_qty is not None else None
		attributed_qty = max(flt(target.get("qty")), 0)
		if remaining_qty is not None:
			attributed_qty = min(attributed_qty, remaining_qty)
		if attributed_qty <= QTY_TOLERANCE:
			continue
		target["attributed_qty"] = attributed_qty
		target["allocated_qty"] = min(max(flt(target.get("allocated_qty")), 0), attributed_qty)
		claimed_target_names.add(name)
		claimed.append(target)
		claimed_qty += attributed_qty
	return claimed


def _get_delivery_rows(schedule_targets):
	target_names = {
		row["name"]
		for targets in schedule_targets.values()
		for row in targets
	}
	if not target_names:
		return []
	return [
		{
			**dict(row),
			"effective_qty": flt(row.effective_qty) if cint(row.is_effective) else 0,
		}
		for row in frappe.get_all(
			"APS Delivery Allocation",
			filters={"customer_schedule_item": ("in", list(target_names))},
			fields=[
				"customer_schedule_item",
				"source_delivery_note",
				"source_posting_time",
				"effective_qty",
				"is_effective",
			],
			order_by="source_posting_time asc, creation asc",
		)
	]


def _get_company_fulfillment_finished_goods_stock(company, results, *, warnings=None):
	"""Return FG stock usable by these exact Results after all stock claims.

	The raw Bin balance is not an ATP balance.  Reuse the net-requirement stock
	policy so sales/stock reservations belonging to other orders and independent
	production reservations are deducted once.  A reservation is credited back
	only when the Result carries a complete SO/SOI identity; partial identities
	remain external.  The configured safety stock is then kept outside customer
	fulfillment.
	"""
	if not _get_finished_goods_warehouses(company):
		# Keep the established warning contract in one place.
		return _get_company_finished_goods_stock(company, warnings=warnings)

	from injection_aps.services import planning

	demand_rows = []
	item_codes = set()
	for result in results or []:
		item_code = result.get("item_code")
		if not item_code:
			continue
		item_codes.add(item_code)
		demand_qty = _get_result_fulfillment_demand_qty(result)
		if demand_qty <= QTY_TOLERANCE:
			continue
		sales_order = result.get("sales_order")
		sales_order_item = result.get("sales_order_item")
		has_exact_order_lineage = bool(sales_order and sales_order_item)
		demand_rows.append(
			frappe._dict(
				{
					"demand_source": result.get("demand_source"),
					"item_code": item_code,
					"qty": demand_qty,
					"sales_order": sales_order if has_exact_order_lineage else None,
					"sales_order_item": sales_order_item if has_exact_order_lineage else None,
				}
			)
		)
	available_stock = planning._get_customer_claimable_stock_map(
		company,
		demand_rows=demand_rows,
	)
	return {
		item_code: max(flt(available_stock.get(item_code)), 0)
		for item_code in item_codes
	}


def _get_company_finished_goods_stock(company, *, warnings=None):
	warehouses = _get_finished_goods_warehouses(company)
	if not warehouses:
		if warnings is not None:
			warnings.append(
				{
					"code": "FG_WAREHOUSE_SCOPE_MISSING",
					"message": _(
						"No Finished Goods warehouse is configured or classified for company {0}; available-to-promise stock is conservatively treated as zero."
					).format(company),
				}
			)
		return {}
	rows = frappe.db.sql(
		"""
		select bin.item_code, coalesce(sum(bin.actual_qty), 0) as qty
		from `tabBin` bin
		inner join `tabWarehouse` warehouse on warehouse.name = bin.warehouse
		where warehouse.company = %(company)s
			and warehouse.is_group = 0
			and warehouse.disabled = 0
			and bin.warehouse in %(warehouses)s
		group by bin.item_code
		""",
		{"company": company, "warehouses": warehouses},
		as_dict=True,
	)
	return {row.item_code: max(flt(row.qty), 0) for row in rows}


def _get_finished_goods_warehouses(company: str) -> list[str]:
	"""Return explicitly configured/type-classified FG warehouses; never include raw/WIP by default."""
	warehouses = set(
		frappe.get_all(
			"Warehouse",
			filters={
				"company": company,
				"is_group": 0,
				"disabled": 0,
				"warehouse_type": "Finished Goods",
			},
			pluck="name",
		)
	)
	fieldname = frappe.db.get_single_value("APS Settings", "plant_floor_fg_warehouse_field")
	if fieldname and frappe.db.exists("DocType", "Plant Floor"):
		meta = frappe.get_meta("Plant Floor")
		if meta.has_field(fieldname):
			configured = frappe.get_all(
				"Plant Floor",
				filters={fieldname: ("is", "set")},
				pluck=fieldname,
			)
			if configured:
				valid = frappe.get_all(
					"Warehouse",
					filters={
						"name": ("in", list(set(configured))),
						"company": company,
						"is_group": 0,
						"disabled": 0,
					},
					pluck="name",
				)
				warehouses.update(valid)
	return sorted(warehouses)


def _derive_opening_stock_by_item(
	company,
	current_stock,
	as_of,
	*,
	run_produced_through=None,
	run_delivered_through=None,
	untracked_delivered_after=None,
	warnings=None,
):
	"""Rewind current FG Bin only for post-cutoff APS events, then remove this run's replayed events."""
	produced_after = {
		row.item_code: flt(row.qty)
		for row in frappe.db.sql(
			"""
			select r.item_code, coalesce(sum(a.good_qty), 0) as qty
			from `tabAPS Production Allocation` a
			inner join `tabAPS Schedule Result` r on r.name = a.schedule_result
			where r.company = %s and a.is_effective = 1 and a.source_posting_time > %s
			group by r.item_code
			""",
			(company, as_of),
			as_dict=True,
		)
	}
	delivered_after = {
		row.item_code: flt(row.qty)
		for row in frappe.db.sql(
			"""
			select item_code, coalesce(sum(effective_qty), 0) as qty
			from `tabAPS Delivery Allocation`
			where company = %s and is_effective = 1 and source_posting_time > %s
			group by item_code
			""",
			(company, as_of),
			as_dict=True,
		)
	}
	run_produced_through = run_produced_through or {}
	run_delivered_through = run_delivered_through or {}
	untracked_delivered_after = untracked_delivered_after or {}
	items = (
		set(current_stock)
		| set(produced_after)
		| set(delivered_after)
		| set(run_produced_through)
		| set(run_delivered_through)
		| set(untracked_delivered_after)
	)
	if warnings is not None and get_datetime(as_of) < now_datetime() - timedelta(minutes=5):
		warnings.append(
			{
				"code": "HISTORICAL_STOCK_REWIND_PARTIAL",
				"message": _(
					"Historical finished-goods stock is rewound from APS production and delivery events only; unrelated Stock Ledger movements after the cutoff are not reconstructable here."
				),
			}
		)
	return {
		item: max(
			flt(current_stock.get(item))
			- flt(produced_after.get(item))
			+ flt(delivered_after.get(item))
			+ flt(untracked_delivered_after.get(item))
			- flt(run_produced_through.get(item))
			+ flt(run_delivered_through.get(item)),
			0,
		)
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
		"warning_count": 0,
		"warnings": [],
	}
