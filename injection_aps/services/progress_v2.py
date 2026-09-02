from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from time import perf_counter
from typing import Any, Callable, Iterable

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, nowdate


QTY_TOLERANCE = 1e-6
ACTIVE_OWNER_STATUSES = ("Draft", "Proposed", "Approved", "Released", "In Progress")
MAX_PAGE_LENGTH = 200
MAX_MATRIX_COLUMNS = 31
MAX_MATRIX_RANGE_DAYS = 366
MAX_STATUS_FILTER_ROWS = 1000
MAX_STATUS_FILTER_GROUPS = 500

LAYER_FIELDS = (
	"schedule_qty",
	"original_plan_qty",
	"current_plan_qty",
	"forecast_qty",
	"actual_good_qty",
	"actual_scrap_qty",
	"delivery_plan_qty",
	"delivered_qty",
	"stock_covered_qty",
	"shortage_qty",
	"recovery_qty",
)


def get_progress_detail(
	*,
	company: str | None = None,
	customer: str | None = None,
	item_code: str | None = None,
	schedule_scope: str | None = None,
	date_from=None,
	date_to=None,
	status: str | None = None,
	run_name: str | None = None,
	offset: int | None = None,
	page_length: int | None = None,
	schedule_item: str | None = None,
	commitment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	result_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	segment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
	started = perf_counter()
	company = _resolve_company(company)
	page_length = min(max(cint(page_length or 100), 1), MAX_PAGE_LENGTH)
	offset = max(cint(offset), 0)
	schedule_rows, total_rows, total_schedule_qty = _get_schedule_rows(
		company=company,
		customer=customer,
		item_code=item_code,
		schedule_scope=schedule_scope,
		date_from=date_from,
		date_to=date_to,
		offset=0 if status else offset,
		page_length=MAX_STATUS_FILTER_ROWS + 1 if status else page_length,
		schedule_item=schedule_item,
	)
	projection = _build_projection(
		schedule_rows,
		run_name=run_name,
		commitment_access_filter=commitment_access_filter,
		result_access_filter=result_access_filter,
		segment_access_filter=segment_access_filter,
	)
	rows = projection["rows"]
	if status:
		if total_rows > MAX_STATUS_FILTER_ROWS:
			frappe.throw(
				_("Status filtering would scan too many schedule rows. Narrow the customer, item, or date range.", context="Injection APS"),
				frappe.ValidationError,
			)
		filtered_rows = [row for row in rows if row.get("status") == status]
		total_rows = len(filtered_rows)
		total_schedule_qty = sum(flt(row.get("schedule_qty")) for row in filtered_rows)
		rows = filtered_rows[offset : offset + page_length]
	return {
		"enabled": 1,
		"mode": "V2",
		"projection": projection["projection"],
		"rows": rows,
		"summary": summarize_rows(rows),
		"pagination": {
			"offset": offset,
			"page_length": page_length,
			"returned_rows": len(rows),
			"total_rows": total_rows,
			"has_more": offset + len(rows) < total_rows,
		},
		"filters": {
			"company": company,
			"customer": customer,
			"item_code": item_code,
			"schedule_scope": schedule_scope,
			"date_from": date_from,
			"date_to": date_to,
			"status": status,
			"run_name": run_name,
		},
		"source_total_schedule_qty": total_schedule_qty,
		"performance": {
			"runtime_ms": round((perf_counter() - started) * 1000, 2),
			"projected_rows": len(rows),
			"source_rows": len(schedule_rows),
		},
	}


def get_progress_matrix_data(
	*,
	company: str | None = None,
	customer: str | None = None,
	item_code: str | None = None,
	schedule_scope: str | None = None,
	date_from=None,
	date_to=None,
	status: str | None = None,
	run_name: str | None = None,
	offset: int | None = None,
	page_length: int | None = None,
	column_offset: int | None = None,
	column_limit: int | None = None,
	commitment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	result_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	segment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
	started = perf_counter()
	company = _resolve_company(company)
	window_start, window_end = _matrix_window(date_from, date_to)
	all_dates = _date_strings(window_start, window_end)
	column_offset = max(cint(column_offset), 0)
	column_limit = min(max(cint(column_limit or 14), 1), MAX_MATRIX_COLUMNS)
	visible_dates = all_dates[column_offset : column_offset + column_limit]
	page_length = min(max(cint(page_length or 100), 1), MAX_PAGE_LENGTH)
	offset = max(cint(offset), 0)
	schedule_rows, total_groups, total_schedule_qty = _get_matrix_schedule_rows(
		company=company,
		customer=customer,
		item_code=item_code,
		schedule_scope=schedule_scope,
		date_from=window_start,
		date_to=window_end,
		offset=0 if status else offset,
		page_length=MAX_STATUS_FILTER_GROUPS + 1 if status else page_length,
	)
	projection = _build_projection(
		schedule_rows,
		run_name=run_name,
		commitment_access_filter=commitment_access_filter,
		result_access_filter=result_access_filter,
		segment_access_filter=segment_access_filter,
	)
	matrix_rows = aggregate_matrix_rows(projection["rows"], visible_dates)
	if status:
		if total_groups > MAX_STATUS_FILTER_GROUPS:
			frappe.throw(
				_("Status filtering would scan too many customer/item groups. Narrow the filters or date range.", context="Injection APS"),
				frappe.ValidationError,
			)
		filtered_rows = [row for row in matrix_rows if row.get("status") == status]
		total_groups = len(filtered_rows)
		total_schedule_qty = sum(flt(row.get("schedule_qty")) for row in filtered_rows)
		matrix_rows = filtered_rows[offset : offset + page_length]
	response = {
		"enabled": 1,
		"mode": "V2",
		"projection": projection["projection"],
		"rows": matrix_rows,
		"summary": summarize_rows(matrix_rows),
		"pagination": {
			"offset": offset,
			"page_length": page_length,
			"returned_rows": len(matrix_rows),
			"total_rows": total_groups,
			"has_more": offset + len(matrix_rows) < total_groups,
			"unit": "Customer / Item",
		},
		"filters": {
			"company": company,
			"customer": customer,
			"item_code": item_code,
			"schedule_scope": schedule_scope,
			"date_from": date_from,
			"date_to": date_to,
			"status": status,
			"run_name": run_name,
		},
		"source_total_schedule_qty": total_schedule_qty,
		"performance": {
			"runtime_ms": round((perf_counter() - started) * 1000, 2),
			"projected_rows": len(matrix_rows),
			"source_rows": len(schedule_rows),
		},
	}
	response["matrix"] = {
		"range_start": str(window_start),
		"range_end": str(window_end),
		"total_columns": len(all_dates),
		"column_offset": column_offset,
		"column_limit": column_limit,
		"dates": visible_dates,
		"has_previous_columns": column_offset > 0,
		"has_more_columns": column_offset + len(visible_dates) < len(all_dates),
		"layers": list(LAYER_FIELDS),
	}
	return response


def aggregate_matrix_rows(rows: Iterable[dict[str, Any]], visible_dates: Iterable[str]) -> list[dict[str, Any]]:
	"""Collapse schedule lines into the customer/item operating view before rendering."""
	grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
	for row in rows or []:
		grouped[_matrix_group_key(row)].append(row)
	result = []
	status_rank = {"Delivered": 0, "Stock Covered": 1, "On Track": 2, "No Formal Plan": 3, "Unknown": 3, "At Risk": 4, "Uncovered": 5, "Late": 6}
	for key, group_rows in grouped.items():
		worst = max(group_rows, key=lambda row: status_rank.get(row.get("status"), 3))
		events = [event for row in group_rows for event in row.get("events") or []]
		cells = build_sparse_matrix_cells(events, visible_dates, default_status=worst.get("status"))
		_add_matrix_progress_alerts(cells, events, visible_dates)
		customer_parts = sorted({row.get("customer_part_no") for row in group_rows if row.get("customer_part_no")})
		result.append(
			{
				"key": "|".join(key),
				"company": key[0],
				"customer": key[1],
				"item_code": key[2],
				"customer_part_no": " / ".join(customer_parts),
				"schedule_date": min((getdate(row.get("schedule_date")) for row in group_rows), default=None),
				"schedule_count": len(group_rows),
				"schedules": list(dict.fromkeys(row.get("schedule") for row in group_rows if row.get("schedule"))),
				"schedule_items": [row.get("schedule_item") for row in group_rows if row.get("schedule_item")],
				"demand_identities": [row.get("demand_identity") for row in group_rows if row.get("demand_identity")],
				**{
					fieldname: sum(flt(row.get(fieldname)) for row in group_rows)
					for fieldname in (*LAYER_FIELDS, "open_demand_qty", "unprojected_open_qty")
				},
				"projection_available": cint(all(cint(row.get("projection_available", 1)) for row in group_rows)),
				"unprojected_rows": sum(1 for row in group_rows if not cint(row.get("projection_available", 1))),
				"conservation_status": "Mismatch" if any(row.get("conservation_status") == "Mismatch" for row in group_rows) else "OK",
				"status": worst.get("status"),
				"status_tone": worst.get("status_tone"),
				"reason": worst.get("reason"),
				"source_documents": _dedupe_sources(source for row in group_rows for source in row.get("source_documents") or []),
				"commitment_names": list(dict.fromkeys(name for row in group_rows for name in row.get("commitment_names") or [])),
				"result_names": list(dict.fromkeys(name for row in group_rows for name in row.get("result_names") or [])),
				"run_names": sorted({name for row in group_rows for name in row.get("run_names") or []}),
				"events": events,
				"cells": cells,
			}
		)
	return result


def get_progress_cell(
	*,
	date_value,
	demand_identity: str | None = None,
	schedule_item: str | None = None,
	run_name: str | None = None,
	commitment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	result_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	segment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
	if not demand_identity and not schedule_item:
		frappe.throw(_("Select a Demand Identity or schedule row before opening progress details.", context="Injection APS"), frappe.ValidationError)
	if demand_identity and not schedule_item:
		schedule_item = frappe.db.get_value("APS Demand Identity", demand_identity, "current_schedule_item")
	company = None
	if schedule_item:
		company = frappe.db.sql(
			"""
			select s.company
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where i.name = %s
			""",
			schedule_item,
		)[0][0] if frappe.db.exists("Customer Delivery Schedule Item", schedule_item) else None
	response = get_progress_detail(
		company=company,
		run_name=run_name,
		page_length=1,
		schedule_item=schedule_item,
		commitment_access_filter=commitment_access_filter,
		result_access_filter=result_access_filter,
		segment_access_filter=segment_access_filter,
	)
	row = next(
		(
			candidate
			for candidate in response.get("rows") or []
			if (not demand_identity or candidate.get("demand_identity") == demand_identity)
			and (not schedule_item or candidate.get("schedule_item") == schedule_item)
		),
		None,
	)
	if not row:
		frappe.throw(_("The requested progress row was not found in the current projection.", context="Injection APS"), frappe.DoesNotExistError)
	date_key = str(getdate(date_value))
	cell = build_sparse_matrix_cells(row.get("events") or [], [date_key], default_status=row.get("status")).get(date_key) or _empty_cell(row.get("status"))
	cell_sources = _dedupe_sources(
		source
		for event in row.get("events") or []
		if event.get("date") == date_key
		for source in event.get("sources") or []
	)
	return {
		"projection": response.get("projection"),
		"date": date_key,
		"cell": cell,
		"row": {key: value for key, value in row.items() if key != "events"},
		"source_documents": cell_sources,
		"lineage": _lineage_groups(row.get("source_documents") or []),
	}


def summarize_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
	rows = list(rows or [])
	status_counts: dict[str, int] = defaultdict(int)
	for row in rows:
		status_counts[row.get("status") or "Unknown"] += 1
	return {
		"rows": len(rows),
		"schedule_qty": sum(flt(row.get("schedule_qty")) for row in rows),
		"open_demand_qty": sum(flt(row.get("open_demand_qty")) for row in rows),
		"original_plan_qty": sum(flt(row.get("original_plan_qty")) for row in rows),
		"current_plan_qty": sum(flt(row.get("current_plan_qty")) for row in rows),
		"forecast_qty": sum(flt(row.get("forecast_qty")) for row in rows),
		"actual_good_qty": sum(flt(row.get("actual_good_qty")) for row in rows),
		"actual_scrap_qty": sum(flt(row.get("actual_scrap_qty")) for row in rows),
		"delivery_plan_qty": sum(flt(row.get("delivery_plan_qty")) for row in rows),
		"delivered_qty": sum(flt(row.get("delivered_qty")) for row in rows),
		"stock_covered_qty": sum(flt(row.get("stock_covered_qty")) for row in rows),
		"shortage_qty": sum(flt(row.get("shortage_qty")) for row in rows),
		"recovery_qty": sum(flt(row.get("recovery_qty")) for row in rows),
		"unprojected_rows": sum(cint(row.get("unprojected_rows")) or cint(not row.get("projection_available", 1)) for row in rows),
		"unprojected_open_qty": sum(flt(row.get("unprojected_open_qty")) for row in rows),
		"conservation_issue_rows": sum(1 for row in rows if row.get("conservation_status") == "Mismatch"),
		"status_counts": dict(status_counts),
	}


def classify_progress_row(row: dict[str, Any], *, today: date | None = None) -> tuple[str, str, str]:
	today = getdate(today or nowdate())
	schedule_qty = max(flt(row.get("schedule_qty")), 0)
	delivered_qty = max(flt(row.get("delivered_qty")), 0)
	stock_qty = max(flt(row.get("stock_covered_qty")), 0)
	current_plan_qty = max(flt(row.get("current_plan_qty")), 0)
	shortage_qty = max(flt(row.get("shortage_qty")), 0)
	due_date = getdate(row.get("schedule_date"))
	forecast_completion = _as_datetime(row.get("forecast_completion_time"))
	due_time = _as_datetime(row.get("effective_due_time")) or datetime.combine(due_date, time.max)
	if schedule_qty <= QTY_TOLERANCE or delivered_qty + QTY_TOLERANCE >= schedule_qty:
		return "Delivered", "green", _("Actual Delivery Note allocations cover the effective schedule quantity.", context="Injection APS")
	if not cint(row.get("projection_available", 1)):
		return "No Formal Plan", "gray", _("No Formal APS owner exists for this demand. Select a Trial Run to inspect trial stock and planning quantities.", context="Injection APS")
	if delivered_qty + stock_qty + QTY_TOLERANCE >= schedule_qty:
		return "Stock Covered", "blue", _("Delivered quantity and active finished-goods allocation cover the demand.", context="Injection APS")
	if not row.get("demand_identity"):
		return "Unknown", "gray", _("This active schedule row has no Demand Identity and cannot be projected safely.", context="Injection APS")
	if row.get("owner_conflict"):
		return "Unknown", "gray", _("More than one commitment is present for this projection scope; resolve demand ownership.", context="Injection APS")
	if row.get("conservation_status") == "Mismatch":
		return "Unknown", "gray", _("Demand ledger quantities do not conserve; inspect the linked Commitment and allocations.", context="Injection APS")
	if forecast_completion and forecast_completion > due_time:
		return "Late", "red", _("Forecast completion is later than the effective customer due time.", context="Injection APS")
	if due_date < today and delivered_qty + stock_qty + max(flt(row.get("actual_good_qty")), 0) + QTY_TOLERANCE < schedule_qty:
		return "Late", "red", _("The customer due date has passed and actual delivery/coverage remains incomplete.", context="Injection APS")
	if shortage_qty > QTY_TOLERANCE:
		if row.get("recovery_completion_time") and due_date >= today:
			return "At Risk", "yellow", _("The plan has a shortage, but a recovery completion is projected.", context="Injection APS")
		return "Uncovered", "red", _("The current Formal projection still contains critical unplanned quantity.", context="Injection APS")
	if current_plan_qty + delivered_qty + stock_qty + QTY_TOLERANCE < schedule_qty:
		return "Uncovered", "red", _("Delivered, stock-covered, and current planned quantities do not cover demand.", context="Injection APS")
	if not forecast_completion:
		return "Unknown", "gray", _("Current production is planned, but no Forecast completion is available.", context="Injection APS")
	if (due_time - forecast_completion).total_seconds() <= 24 * 3600:
		return "At Risk", "yellow", _("Forecast completion is within 24 hours of the effective due time.", context="Injection APS")
	return "On Track", "green", _("The current Forecast covers demand before the effective due time.", context="Injection APS")


def build_sparse_matrix_cells(events: Iterable[dict[str, Any]], visible_dates: Iterable[str], *, default_status: str | None = None) -> dict[str, dict[str, Any]]:
	visible = set(visible_dates or [])
	cells: dict[str, dict[str, Any]] = {}
	for event in events or []:
		date_key = str(event.get("date") or "")
		layer = event.get("layer")
		if date_key not in visible or layer not in LAYER_FIELDS:
			continue
		cell = cells.setdefault(date_key, _empty_cell(default_status))
		cell[layer] = round(flt(cell.get(layer)) + flt(event.get("qty")), 6)
		cell["sources"] = _dedupe_sources([*(cell.get("sources") or []), *(event.get("sources") or [])])
		if event.get("reason") and event.get("reason") not in cell["reasons"]:
			cell["reasons"].append(event.get("reason"))
	return cells


def _add_matrix_progress_alerts(cells, events, visible_dates) -> None:
	"""Attach conservative cumulative warnings to the four user-facing layers."""
	ordered_events = sorted(
		(event for event in events or [] if event.get("date") and event.get("layer") in LAYER_FIELDS),
		key=lambda event: str(event.get("date")),
	)
	cumulative: dict[str, float] = defaultdict(float)
	index = 0
	today = date.today()
	for date_value in sorted(str(value) for value in visible_dates or []):
		while index < len(ordered_events) and str(ordered_events[index].get("date")) <= date_value:
			event = ordered_events[index]
			cumulative[event.get("layer")] += flt(event.get("qty"))
			index += 1
		schedule_qty = max(cumulative["schedule_qty"], 0)
		planned_coverage = max(cumulative["current_plan_qty"], 0)
		stock_coverage = max(cumulative["stock_covered_qty"], 0)
		delivered_qty = max(cumulative["delivered_qty"], 0)
		actual_good_qty = max(cumulative["actual_good_qty"], 0)
		alerts = []
		planning_gap = max(schedule_qty - delivered_qty - stock_coverage - planned_coverage, 0)
		if planning_gap > QTY_TOLERANCE:
			alerts.append(
				{
					"layer": "plan",
					"tone": "red",
					"reason": _(
						"Cumulative APS coverage is {0} short of the customer schedule by this date.",
						context="Injection APS",
					).format(round(planning_gap, 6)),
				}
			)
		production_gap = max(planned_coverage - actual_good_qty, 0)
		if getdate(date_value) < today and production_gap > QTY_TOLERANCE:
			alerts.append(
				{
					"layer": "actual",
					"tone": "yellow",
					"reason": _(
						"Actual inbound is {0} behind cumulative APS completions by this date.",
						context="Injection APS",
					).format(round(production_gap, 6)),
				}
			)
		delivery_gap = max(schedule_qty - delivered_qty, 0)
		if getdate(date_value) <= today and delivery_gap > QTY_TOLERANCE:
			alerts.append(
				{
					"layer": "delivery",
					"tone": "red",
					"reason": _(
						"Delivered quantity is {0} short of the cumulative customer schedule by this date.",
						context="Injection APS",
					).format(round(delivery_gap, 6)),
				}
			)
		if alerts:
			cell = cells.setdefault(date_value, _empty_cell(None))
			cell["alerts"] = alerts
			for alert in alerts:
				if alert["reason"] not in cell["reasons"]:
					cell["reasons"].append(alert["reason"])


def _build_projection(
	schedule_rows: list[dict[str, Any]],
	*,
	run_name: str | None,
	commitment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	result_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
	segment_access_filter: Callable[[list[dict[str, Any]]], Iterable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
	identities = sorted({row.get("demand_identity") for row in schedule_rows if row.get("demand_identity")})
	schedule_items = sorted({row.get("schedule_item") for row in schedule_rows if row.get("schedule_item")})
	commitments = _get_commitments(identities, run_name=run_name)
	if commitment_access_filter is not None:
		commitments = list(commitment_access_filter(commitments) or [])
	commitment_names = [row["name"] for row in commitments]
	results = _get_results(commitment_names)
	if result_access_filter is not None:
		results = list(result_access_filter(results) or [])
	result_names = [row["name"] for row in results]
	segments = _get_segments(result_names)
	if segment_access_filter is not None:
		segments = list(segment_access_filter(segments) or [])
	production_rows = _get_production_allocations(schedule_items, result_names, run_name=run_name)
	if segment_access_filter is not None:
		visible_segment_names = {row.get("name") for row in segments if row.get("name")}
		production_rows = [
			row for row in production_rows
			if not row.get("segment") or row.get("segment") in visible_segment_names
		]
	stock_rows = _get_stock_allocations(
		identities,
		run_name=run_name,
		commitment_names=commitment_names,
	)
	delivery_rows = _get_delivery_allocations(identities)
	delivery_plan_rows = _get_delivery_plan_rows(identities)
	pegging_rows = _get_bom_peggings(commitment_names)

	commitments_by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for row in commitments:
		commitments_by_identity[row.get("demand_identity")].append(row)
	results_by_commitment: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for row in results:
		results_by_commitment[row.get("demand_commitment")].append(row)
	segments_by_result: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for row in segments:
		segments_by_result[row.get("parent")].append(row)
	production_by_schedule = _group_rows(production_rows, "customer_schedule_item")
	stock_by_identity = _group_rows(stock_rows, "demand_identity")
	delivery_by_identity = _group_rows(delivery_rows, "demand_identity")
	dp_by_identity = _group_rows(delivery_plan_rows, "demand_identity")
	pegging_by_root = _group_rows(pegging_rows, "root_commitment")

	rows = []
	selected_runs = set()
	for schedule_row in schedule_rows:
		identity = schedule_row.get("demand_identity")
		identity_commitments = commitments_by_identity.get(identity) or []
		for commitment in identity_commitments:
			selected_runs.add(commitment.get("planning_run"))
		identity_results = [
			result
			for commitment in identity_commitments
			for result in results_by_commitment.get(commitment.get("name")) or []
		]
		identity_segments = [
			segment
			for result in identity_results
			for segment in segments_by_result.get(result.get("name")) or []
		]
		root_peggings = [
			pegging
			for commitment in identity_commitments
			for pegging in pegging_by_root.get(commitment.get("name")) or []
		]
		rows.append(
			_project_row(
				schedule_row,
				commitments=identity_commitments,
				results=identity_results,
				segments=identity_segments,
				production_rows=production_by_schedule.get(schedule_row.get("schedule_item")) or [],
				stock_rows=stock_by_identity.get(identity) or [],
				delivery_rows=delivery_by_identity.get(identity) or [],
				delivery_plan_rows=dp_by_identity.get(identity) or [],
				pegging_rows=root_peggings,
				projection_available=bool(run_name or identity_commitments),
			)
		)
	selected_runs.discard(None)
	projection_available = bool(run_name or selected_runs)
	projection_type = "Single Run" if run_name else "Effective Cross-Run"
	return {
		"projection": {
			"type": projection_type,
			"single_run_view": cint(bool(run_name)),
			"selected_run": run_name,
			"run_names": sorted(selected_runs | ({run_name} if run_name else set())),
			"available": cint(projection_available),
			"label": (
				_("Single Run view", context="Injection APS")
				if run_name
				else _("Current effective cross-Run projection", context="Injection APS")
				if projection_available
				else _("No Formal APS Projection", context="Injection APS")
			),
			"reason": (
				_("Only commitments from the explicitly selected APS Run are shown.", context="Injection APS")
				if run_name
				else _("Each Demand Identity is projected from its current Formal owner; no recent old Run is selected by status priority.", context="Injection APS")
				if projection_available
				else _("No Formal APS owner exists in this scope. Trial Run stock and plan quantities are excluded; production and delivery appear only after APS attributes their source documents to this demand.", context="Injection APS")
			),
		},
		"rows": rows,
	}


def _project_row(
	schedule_row: dict[str, Any],
	*,
	commitments: list[dict[str, Any]],
	results: list[dict[str, Any]],
	segments: list[dict[str, Any]],
	production_rows: list[dict[str, Any]],
	stock_rows: list[dict[str, Any]],
	delivery_rows: list[dict[str, Any]],
	delivery_plan_rows: list[dict[str, Any]],
	pegging_rows: list[dict[str, Any]],
	projection_available: bool = True,
) -> dict[str, Any]:
	row = dict(schedule_row)
	schedule_qty = max(flt(row.get("schedule_qty")), 0)
	delivered_qty = max(sum(flt(item.get("effective_qty")) for item in delivery_rows), 0)
	legacy_delivered_qty = max(flt(row.get("schedule_delivered_qty")), 0)
	stock_qty = sum(_stock_remaining(item) for item in stock_rows)
	commitment_stock = max((flt(item.get("stock_covered_qty")) for item in commitments), default=0)
	if not stock_rows:
		stock_qty = commitment_stock
	open_demand_qty = max(schedule_qty - delivered_qty, 0)
	on_time_qty = sum(flt(item.get("on_time_qty")) for item in commitments)
	recovery_qty = sum(flt(item.get("late_qty")) for item in commitments)
	shortage_qty = sum(flt(item.get("unscheduled_qty")) for item in commitments)
	if not commitments and projection_available:
		shortage_qty = open_demand_qty
	if commitments and shortage_qty <= QTY_TOLERANCE:
		shortage_qty = sum(flt(item.get("critical_unplanned_qty")) for item in results)
	carried_qty = sum(flt(item.get("carried_qty")) for item in commitments)
	new_plan_qty = sum(flt(item.get("newly_planned_qty")) for item in commitments)
	requested_open_qty = sum(flt(item.get("requested_qty")) for item in commitments)
	partition_qty = on_time_qty + recovery_qty + shortage_qty
	production_commitment_qty = carried_qty + new_plan_qty
	partition_status = (
		"Pending"
		if commitments
		and partition_qty <= QTY_TOLERANCE
		and production_commitment_qty > QTY_TOLERANCE
		else "Validated"
	)
	partition_delta = (
		0
		if partition_status == "Pending"
		else round(production_commitment_qty - partition_qty, 6)
	)
	demand_delta = round(requested_open_qty - stock_qty - carried_qty - new_plan_qty, 6) if commitments else 0
	conservation_status = "OK"
	if commitments and (abs(partition_delta) > QTY_TOLERANCE or abs(demand_delta) > QTY_TOLERANCE):
		conservation_status = "Mismatch"

	active_segments = [item for item in segments if item.get("segment_status") != "Cancelled" and flt(item.get("planned_qty")) > 0]
	original_plan_qty = sum(flt(item.get("planned_qty")) for item in active_segments)
	unrepresented_carried_qty = _unrepresented_carried_qty(
		results,
		active_segments,
		carried_qty=carried_qty,
	)
	current_plan_qty = original_plan_qty + unrepresented_carried_qty
	forecast_qty = original_plan_qty
	original_completion = _latest_datetime(_segment_time(item, "original", "end") for item in active_segments)
	current_completion = _latest_datetime(_segment_time(item, "current", "end") for item in active_segments)
	forecast_completion = _latest_datetime(_segment_time(item, "forecast", "end") for item in active_segments)
	if not forecast_completion:
		forecast_completion = _latest_datetime(item.get("projected_completion_time") for item in results)
	actual_good_qty = sum(max(flt(item.get("good_qty")), 0) for item in production_rows)
	actual_scrap_qty = sum(max(flt(item.get("scrap_qty")), 0) for item in production_rows)
	if not production_rows:
		actual_good_qty = sum(max(flt(item.get("actual_good_qty")), 0) for item in active_segments)
		actual_scrap_qty = sum(max(flt(item.get("actual_scrap_qty")), 0) for item in active_segments)
	if not production_rows and actual_good_qty <= QTY_TOLERANCE and actual_scrap_qty <= QTY_TOLERANCE:
		actual_good_qty = sum(max(flt(item.get("good_produced_qty")), 0) for item in results)
		actual_scrap_qty = sum(max(flt(item.get("scrap_qty")), 0) for item in results)
	delivery_plan_qty = sum(max(flt(item.get("planned_delivery_qty")), 0) for item in delivery_plan_rows)
	recovery_completion = _latest_datetime(item.get("recovery_completion_time") for item in results)
	if not recovery_completion and recovery_qty > QTY_TOLERANCE:
		recovery_completion = _latest_datetime(
			_segment_time(item, "current", "end")
			for item in active_segments
			if (item.get("horizon_zone") or "") == "Recovery" or (item.get("production_mode") or "") == "Late"
		)
	effective_due_time = _latest_datetime(item.get("effective_due_time") for item in commitments)

	sources = _build_row_sources(
		row,
		commitments,
		results,
		active_segments,
		production_rows,
		stock_rows,
		delivery_rows,
		delivery_plan_rows,
		pegging_rows,
	)
	events = _build_row_events(
		row,
		active_segments=active_segments,
		results=results,
		production_rows=production_rows,
		commitments=commitments,
		carried_qty=unrepresented_carried_qty,
		stock_qty=stock_qty,
		shortage_qty=shortage_qty,
		recovery_qty=recovery_qty,
		recovery_completion=recovery_completion,
		delivery_rows=delivery_rows,
		delivery_plan_rows=delivery_plan_rows,
	)
	row.update(
		{
			"schedule_qty": schedule_qty,
			"open_demand_qty": open_demand_qty,
			"projection_available": cint(projection_available),
			"unprojected_open_qty": open_demand_qty if not projection_available else 0,
			"requested_open_qty": requested_open_qty,
			"original_plan_qty": original_plan_qty,
			"current_plan_qty": current_plan_qty,
			"forecast_qty": forecast_qty,
			"original_completion_time": original_completion,
			"current_completion_time": current_completion,
			"forecast_completion_time": forecast_completion,
			"actual_good_qty": actual_good_qty,
			"actual_scrap_qty": actual_scrap_qty,
			"delivery_plan_qty": delivery_plan_qty,
			"delivered_qty": delivered_qty,
			"legacy_schedule_delivered_qty": legacy_delivered_qty,
			"unallocated_legacy_delivery_qty": max(legacy_delivered_qty - delivered_qty, 0),
			"stock_covered_qty": stock_qty,
			"carried_qty": carried_qty,
			"new_plan_qty": new_plan_qty,
			"on_time_qty": on_time_qty,
			"shortage_qty": shortage_qty,
			"recovery_qty": recovery_qty,
			"recovery_completion_time": recovery_completion,
			"effective_due_time": effective_due_time,
			"owner_conflict": cint(len(commitments) > 1),
			"commitment_names": [item.get("name") for item in commitments],
			"result_names": [item.get("name") for item in results],
			"run_names": sorted({item.get("planning_run") for item in commitments if item.get("planning_run")}),
			"conservation_status": conservation_status,
			"demand_conservation_delta": demand_delta,
			"solver_partition_status": partition_status,
			"solver_partition_delta": partition_delta,
			"source_documents": sources,
			"events": events,
		}
	)
	status, tone, reason = classify_progress_row(row)
	row["status"] = status
	row["status_tone"] = tone
	row["reason"] = reason
	return row


def _unrepresented_carried_qty(results, active_segments, *, carried_qty):
	"""Add carried Work Orders only when current APS segments do not already include them."""
	carried_qty = max(flt(carried_qty), 0)
	if carried_qty <= QTY_TOLERANCE:
		return 0
	segment_qty_by_result = defaultdict(float)
	for segment in active_segments:
		segment_qty_by_result[segment.get("parent")] += max(flt(segment.get("planned_qty")), 0)
	represented_qty = 0
	for result in results:
		try:
			baseline = json.loads(result.get("fulfillment_baseline_json") or "{}")
		except (TypeError, ValueError):
			continue
		evidence = baseline.get("net_requirement") if isinstance(baseline, dict) else None
		if not isinstance(evidence, dict):
			continue
		open_work_order_qty = max(flt(evidence.get("open_work_order_qty")), 0)
		net_requirement_qty = max(flt(evidence.get("net_requirement_qty")), 0)
		if (
			open_work_order_qty <= QTY_TOLERANCE
			or flt(result.get("planned_qty")) + QTY_TOLERANCE
				< open_work_order_qty + net_requirement_qty
		):
			continue
		represented_qty += min(
			open_work_order_qty,
			segment_qty_by_result.get(result.get("name"), 0),
		)
	return max(carried_qty - represented_qty, 0)


def _build_row_events(
	row,
	*,
	active_segments,
	results,
	production_rows,
	stock_qty,
	shortage_qty,
	recovery_qty,
	recovery_completion,
	delivery_rows,
	delivery_plan_rows,
	commitments=(),
	carried_qty=0,
):
	events = []
	due_date = str(getdate(row.get("schedule_date")))
	schedule_sources = [_doc_ref("Customer Delivery Schedule", row.get("schedule")), _doc_ref("Customer Delivery Schedule Item", row.get("schedule_item"))]
	if row.get("demand_identity"):
		schedule_sources.append(_doc_ref("APS Demand Identity", row.get("demand_identity")))
	_add_event(events, due_date, "schedule_qty", row.get("schedule_qty"), schedule_sources)
	_add_event(events, due_date, "stock_covered_qty", stock_qty, schedule_sources)
	_add_event(events, due_date, "shortage_qty", shortage_qty, schedule_sources)
	if carried_qty > QTY_TOLERANCE:
		carried_sources = _dedupe_sources(
			[
				*schedule_sources,
				*[
					_doc_ref("APS Demand Commitment", item.get("name"))
					for item in commitments
					if flt(item.get("carried_qty")) > QTY_TOLERANCE
				],
				*[
					_doc_ref("Work Order", name)
					for item in commitments
					for name in _json_source_names(item.get("source_work_orders_json"))
				],
			]
		)
		_add_event(
			events,
			due_date,
			"current_plan_qty",
			carried_qty,
			carried_sources,
			_("Existing Work Orders carry this quantity into the current demand plan.", context="Injection APS"),
		)
	for segment in active_segments:
		segment_sources = _segment_sources(segment, results)
		planned_qty = flt(segment.get("planned_qty"))
		_add_event(events, _date_of(_segment_time(segment, "original", "end")), "original_plan_qty", planned_qty, segment_sources)
		_add_event(
			events,
			_date_of(_segment_time(segment, "current", "end")),
			"current_plan_qty",
			planned_qty,
			segment_sources,
			_segment_plan_change_reason(segment),
		)
		_add_event(events, _date_of(_segment_time(segment, "forecast", "end")), "forecast_qty", planned_qty, segment_sources)
	if production_rows:
		for item in production_rows:
			production_sources = _production_sources(item)
			actual_date = _date_of(item.get("source_posting_time"))
			_add_event(events, actual_date, "actual_good_qty", item.get("good_qty"), production_sources)
			_add_event(events, actual_date, "actual_scrap_qty", item.get("scrap_qty"), production_sources)
	else:
		for segment in active_segments:
			segment_sources = _segment_sources(segment, results)
			actual_date = _date_of(segment.get("actual_end_time") or segment.get("last_actual_report_time") or segment.get("actual_start_time"))
			_add_event(events, actual_date, "actual_good_qty", segment.get("actual_good_qty"), segment_sources)
			_add_event(events, actual_date, "actual_scrap_qty", segment.get("actual_scrap_qty"), segment_sources)
	if not production_rows and not active_segments:
		for result in results:
			result_sources = [_doc_ref("APS Schedule Result", result.get("name"))]
			actual_date = _date_of(result.get("actual_end_time") or result.get("last_actual_report_time"))
			_add_event(events, actual_date, "actual_good_qty", result.get("good_produced_qty"), result_sources)
			_add_event(events, actual_date, "actual_scrap_qty", result.get("scrap_qty"), result_sources)
	for item in delivery_plan_rows:
		_add_event(
			events,
			str(getdate(item.get("required_delivery_date") or row.get("schedule_date"))),
			"delivery_plan_qty",
			item.get("planned_delivery_qty"),
			[_doc_ref("Delivery Plan", item.get("delivery_plan")), _doc_ref("Delivery Plan Item Qty", item.get("name"))],
		)
	for item in delivery_rows:
		_add_event(
			events,
			_date_of(item.get("source_posting_time")) or due_date,
			"delivered_qty",
			item.get("effective_qty"),
			[_doc_ref("Delivery Note", item.get("source_delivery_note")), _doc_ref("APS Delivery Allocation", item.get("name"))],
		)
	_add_event(events, _date_of(recovery_completion), "recovery_qty", recovery_qty, schedule_sources)
	return events


def _build_row_sources(row, commitments, results, segments, production_rows, stock_rows, delivery_rows, delivery_plan_rows, pegging_rows):
	sources = [
		_doc_ref("Customer Delivery Schedule", row.get("schedule")),
		_doc_ref("Customer Delivery Schedule Item", row.get("schedule_item")),
		_doc_ref("APS Demand Identity", row.get("demand_identity")),
	]
	for item in commitments:
		sources.extend([_doc_ref("APS Demand Commitment", item.get("name")), _doc_ref("APS Planning Run", item.get("planning_run"))])
		sources.extend(_doc_ref("Work Order", name) for name in _json_source_names(item.get("source_work_orders_json")))
	for item in results:
		sources.extend([_doc_ref("APS Schedule Result", item.get("name")), _doc_ref("APS Production Campaign", item.get("production_campaign"))])
	for item in segments:
		sources.extend(_segment_sources(item, results))
	for item in production_rows:
		sources.extend(_production_sources(item))
	for item in stock_rows:
		sources.append(_doc_ref("APS Stock Coverage Allocation", item.get("name")))
	for item in delivery_rows:
		sources.extend([_doc_ref("APS Delivery Allocation", item.get("name")), _doc_ref("Delivery Note", item.get("source_delivery_note"))])
	for item in delivery_plan_rows:
		sources.extend([_doc_ref("Delivery Plan", item.get("delivery_plan")), _doc_ref("Delivery Plan Item Qty", item.get("name"))])
	for item in pegging_rows:
		sources.append(_doc_ref("APS BOM Pegging", item.get("name")))
	return _dedupe_sources(sources)


def _production_sources(row):
	return _dedupe_sources(
		[
			_doc_ref("APS Production Allocation", row.get("name")),
			_doc_ref("Stock Entry", row.get("source_stock_entry")),
			_doc_ref("Work Order", row.get("work_order")),
			_doc_ref("Work Order Scheduling", row.get("work_order_scheduling")),
			_doc_ref("Scheduling Item", row.get("scheduling_item")),
			_doc_ref("APS Schedule Result", row.get("schedule_result")),
			_doc_ref("APS Schedule Segment", row.get("segment")),
		]
	)


def _segment_sources(segment, results):
	result = next((item for item in results if item.get("name") == segment.get("parent")), {})
	return _dedupe_sources(
		[
			_doc_ref("APS Schedule Result", segment.get("parent")),
			_doc_ref("APS Schedule Segment", segment.get("name")),
			_doc_ref("APS Production Campaign", segment.get("production_campaign") or result.get("production_campaign")),
			_doc_ref("Work Order", segment.get("linked_work_order")),
			_doc_ref("Work Order Scheduling", segment.get("linked_work_order_scheduling")),
			_doc_ref("Scheduling Item", segment.get("linked_scheduling_item")),
		]
	)


def _get_schedule_rows(*, company, customer, item_code, schedule_scope, date_from, date_to, offset, page_length, schedule_item=None):
	date_expression = "coalesce(i.effective_schedule_date, i.schedule_date)"
	qty_expression = "coalesce(i.effective_qty, i.qty, 0)"
	visible_schedules, visible_items, visible_identities = _get_visible_schedule_scope(
		company=company,
		customer=customer,
		item_code=item_code,
		schedule_scope=schedule_scope,
	)
	if not visible_schedules or not visible_items:
		return [], 0, 0.0
	conditions = [
		"s.status = 'Active'",
		"ifnull(i.status, '') != 'Cancelled'",
		f"{qty_expression} > 0",
		"s.name in %(visible_schedules)s",
		"i.item_code in %(visible_items)s",
		"(ifnull(i.demand_identity, '') = '' or i.demand_identity in %(visible_identities)s)",
	]
	params: dict[str, Any] = {
		"offset": offset,
		"page_length": page_length,
		"visible_schedules": tuple(visible_schedules),
		"visible_items": tuple(visible_items),
		"visible_identities": tuple(visible_identities or [""]),
	}
	for value, condition, key in (
		(company, "s.company = %(company)s", "company"),
		(customer, "s.customer = %(customer)s", "customer"),
		(item_code, "i.item_code = %(item_code)s", "item_code"),
		(schedule_scope, "s.schedule_scope = %(schedule_scope)s", "schedule_scope"),
		(schedule_item, "i.name = %(schedule_item)s", "schedule_item"),
	):
		if value:
			conditions.append(condition)
			params[key] = value
	if date_from:
		conditions.append(f"{date_expression} >= %(date_from)s")
		params["date_from"] = getdate(date_from)
	if date_to:
		conditions.append(f"{date_expression} <= %(date_to)s")
		params["date_to"] = getdate(date_to)
	where = " and ".join(conditions)
	base = f"""
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where {where}
	"""
	rows = frappe.db.sql(
		f"""
		select i.name as schedule_item, i.parent as schedule, i.demand_identity,
			s.company, s.customer, s.schedule_scope, s.version_no, s.source_type,
			i.item_code, i.customer_part_no, i.sales_order,
			{date_expression} as schedule_date,
			{qty_expression} as schedule_qty,
			ifnull(i.delivered_qty, 0) as schedule_delivered_qty,
			ifnull(i.executed_floor_qty, 0) as executed_floor_qty,
			ifnull(i.excess_qty, 0) as excess_qty,
			i.delivery_match_status, i.identity_match_method, i.remark
		{base}
		order by {date_expression}, s.customer, i.item_code, i.name
		limit %(page_length)s offset %(offset)s
		""",
		params,
		as_dict=True,
	)
	summary = frappe.db.sql(
		f"select count(*) as total_rows, coalesce(sum({qty_expression}), 0) as total_schedule_qty {base}",
		params,
		as_dict=True,
	)[0]
	return [dict(row) for row in rows], cint(summary.total_rows), flt(summary.total_schedule_qty)


def _get_matrix_schedule_rows(*, company, customer, item_code, schedule_scope, date_from, date_to, offset, page_length):
	"""Page customer/item groups, then load every schedule line in those groups."""
	date_expression = "coalesce(i.effective_schedule_date, i.schedule_date)"
	qty_expression = "coalesce(i.effective_qty, i.qty, 0)"
	visible_schedules, visible_items, visible_identities = _get_visible_schedule_scope(
		company=company,
		customer=customer,
		item_code=item_code,
		schedule_scope=schedule_scope,
	)
	if not visible_schedules or not visible_items:
		return [], 0, 0.0
	conditions = [
		"s.status = 'Active'",
		"ifnull(i.status, '') != 'Cancelled'",
		f"{qty_expression} > 0",
		"s.name in %(visible_schedules)s",
		"i.item_code in %(visible_items)s",
		"(ifnull(i.demand_identity, '') = '' or i.demand_identity in %(visible_identities)s)",
	]
	params: dict[str, Any] = {
		"offset": offset,
		"page_length": page_length,
		"visible_schedules": tuple(visible_schedules),
		"visible_items": tuple(visible_items),
		"visible_identities": tuple(visible_identities or [""]),
	}
	for value, condition, key in (
		(company, "s.company = %(company)s", "company"),
		(customer, "s.customer = %(customer)s", "customer"),
		(item_code, "i.item_code = %(item_code)s", "item_code"),
		(schedule_scope, "s.schedule_scope = %(schedule_scope)s", "schedule_scope"),
	):
		if value:
			conditions.append(condition)
			params[key] = value
	if date_from:
		conditions.append(f"{date_expression} >= %(date_from)s")
		params["date_from"] = getdate(date_from)
	if date_to:
		conditions.append(f"{date_expression} <= %(date_to)s")
		params["date_to"] = getdate(date_to)
	where = " and ".join(conditions)
	base = f"""
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where {where}
	"""
	groups = frappe.db.sql(
		f"""
		select s.company, s.customer, i.item_code
		{base}
		group by s.company, s.customer, i.item_code
		order by s.customer, i.item_code, s.company
		limit %(page_length)s offset %(offset)s
		""",
		params,
		as_dict=True,
	)
	summary = frappe.db.sql(
		f"""
		select count(*) as total_groups, coalesce(sum(group_schedule_qty), 0) as total_schedule_qty
		from (
			select s.company, s.customer, i.item_code, sum({qty_expression}) as group_schedule_qty
			{base}
			group by s.company, s.customer, i.item_code
		) grouped_schedule
		""",
		params,
		as_dict=True,
	)[0]
	if not groups:
		return [], cint(summary.total_groups), flt(summary.total_schedule_qty)
	group_conditions = []
	for index, group in enumerate(groups):
		for fieldname in ("company", "customer", "item_code"):
			params[f"group_{fieldname}_{index}"] = group.get(fieldname)
		group_conditions.append(
			f"(s.company <=> %(group_company_{index})s and s.customer <=> %(group_customer_{index})s "
			f"and i.item_code <=> %(group_item_code_{index})s)"
		)
	rows = frappe.db.sql(
		f"""
		select i.name as schedule_item, i.parent as schedule, i.demand_identity,
			s.company, s.customer, s.schedule_scope, s.version_no, s.source_type,
			i.item_code, i.customer_part_no, i.sales_order,
			{date_expression} as schedule_date,
			{qty_expression} as schedule_qty,
			ifnull(i.delivered_qty, 0) as schedule_delivered_qty,
			ifnull(i.executed_floor_qty, 0) as executed_floor_qty,
			ifnull(i.excess_qty, 0) as excess_qty,
			i.delivery_match_status, i.identity_match_method, i.remark
		{base}
			and ({' or '.join(group_conditions)})
		order by s.customer, i.item_code, {date_expression}, i.name
		""",
		params,
		as_dict=True,
	)
	return [dict(row) for row in rows], cint(summary.total_groups), flt(summary.total_schedule_qty)


def _get_visible_schedule_scope(*, company, customer, item_code, schedule_scope):
	"""Resolve the permission-safe schedule/item universe before SQL paging.

	Totals and offsets must be calculated inside the same visible universe as the
	returned rows; sanitizing only the current page would leak hidden row counts.
	"""
	filters: dict[str, Any] = {"status": "Active"}
	if company:
		filters["company"] = company
	if customer:
		filters["customer"] = customer
	if schedule_scope:
		filters["schedule_scope"] = schedule_scope
	parents = [
		dict(row)
		for row in frappe.get_list(
			"Customer Delivery Schedule",
			filters=filters,
			fields=["name", "customer"],
			limit_page_length=0,
		)
	]
	if not parents:
		return [], [], []
	customers = sorted({row.get("customer") for row in parents if row.get("customer")})
	visible_customers = set(
		frappe.get_list(
			"Customer",
			filters={"name": ("in", customers or [""])},
			pluck="name",
			limit_page_length=0,
		)
	)
	visible_schedules = sorted(
		row["name"] for row in parents if not row.get("customer") or row.get("customer") in visible_customers
	)
	if not visible_schedules:
		return [], [], []
	item_conditions = ["parent in %(schedules)s"]
	params: dict[str, Any] = {"schedules": tuple(visible_schedules)}
	if item_code:
		item_conditions.append("item_code = %(item_code)s")
		params["item_code"] = item_code
	candidate_scope = frappe.db.sql(
		f"select distinct item_code, demand_identity from `tabCustomer Delivery Schedule Item` where {' and '.join(item_conditions)}",
		params,
	)
	candidate_items = [
		row[0]
		for row in candidate_scope
		if row[0]
	]
	candidate_identities = [
		row[1]
		for row in candidate_scope
		if row[1]
	]
	visible_items = frappe.get_list(
		"Item",
		filters={"name": ("in", candidate_items or [""])},
		pluck="name",
		limit_page_length=0,
	)
	visible_identities = frappe.get_list(
		"APS Demand Identity",
		filters={"name": ("in", candidate_identities or [""])},
		pluck="name",
		limit_page_length=0,
	)
	return visible_schedules, visible_items, visible_identities


def _matrix_group_key(row):
	return tuple(str(row.get(fieldname) or "") for fieldname in ("company", "customer", "item_code"))


def _get_commitments(identities, *, run_name):
	if not identities:
		return []
	filters: dict[str, Any] = {
		"demand_identity": ("in", identities),
		"status": ("not in", ["Cancelled", "Excess"]),
	}
	if run_name:
		filters["planning_run"] = run_name
	else:
		filters.update(
			{
				"formal_owner": 1,
				"owner_state": "Owned",
				"status": ("in", ACTIVE_OWNER_STATUSES),
			}
		)
	return [
		dict(row)
		for row in frappe.get_list(
			"APS Demand Commitment",
			filters=filters,
			fields=[
				"name", "company", "customer", "planning_run", "source_run", "item_code",
				"demand_identity", "schedule_item", "status", "owner_state",
				"formal_owner", "requested_qty", "stock_covered_qty", "carried_qty",
				"newly_planned_qty", "on_time_qty", "late_qty", "unscheduled_qty", "produced_qty",
				"delivered_qty", "remaining_qty", "excess_qty", "effective_due_time",
				"input_fingerprint", "transitioned_on",
				"source_work_orders_json",
			],
			order_by="demand_identity asc, transitioned_on desc, name desc",
			limit_page_length=0,
		)
	]


def _get_results(commitment_names):
	if not commitment_names:
		return []
	fields = [
		"name", "company", "customer", "planning_run", "demand_commitment", "production_campaign", "item_code",
		"sales_order", "sales_order_item", "plant_floor", "net_requirement",
		"planned_qty", "machine_scheduled_qty", "on_time_qty", "recovery_qty", "critical_unplanned_qty",
		"good_produced_qty", "scrap_qty", "actual_start_time", "actual_end_time",
		"last_actual_report_time", "projected_completion_time", "recovery_completion_time",
		"risk_status", "status", "shortage_code", "solver_explanation", "fulfillment_baseline_json",
	]
	return [dict(row) for row in frappe.get_list("APS Schedule Result", filters={"demand_commitment": ("in", commitment_names)}, fields=fields, order_by="modified asc", limit_page_length=0)]


def _get_segments(result_names):
	if not result_names:
		return []
	fields = [
		"name", "parent", "planned_qty", "segment_status", "horizon_zone", "production_mode",
		"start_time", "end_time", "baseline_start_time", "baseline_end_time",
		"solver_start_time", "solver_end_time", "current_start_time", "current_end_time",
		"forecast_start_time", "forecast_end_time", "actual_start_time", "actual_end_time",
		"actual_good_qty", "actual_scrap_qty", "last_actual_report_time",
		"production_campaign", "capacity_owner", "linked_work_order",
		"linked_work_order_scheduling", "linked_scheduling_item", "workstation", "mould_reference",
	]
	return [dict(row) for row in frappe.get_all("APS Schedule Segment", filters={"parent": ("in", result_names)}, fields=fields, order_by="start_time asc, name asc", limit_page_length=0)]


def _get_production_allocations(schedule_items, result_names, *, run_name):
	if not schedule_items or not frappe.db.exists("DocType", "APS Production Allocation"):
		return []
	filters: dict[str, Any] = {
		"customer_schedule_item": ("in", schedule_items),
		"source_docstatus": 1,
		"is_effective": 1,
	}
	if run_name:
		if not result_names:
			return []
		filters["planning_run"] = run_name
		filters["schedule_result"] = ("in", result_names)
	return [
		dict(row)
		for row in frappe.get_list(
			"APS Production Allocation",
			filters=filters,
			fields=[
				"name", "planning_run", "schedule_result", "segment", "customer_schedule_item",
				"work_order", "work_order_scheduling", "scheduling_item", "source_stock_entry",
				"source_stock_entry_detail", "source_posting_time", "output_type", "good_qty", "scrap_qty",
			],
			order_by="source_posting_time asc, name asc",
			limit_page_length=0,
		)
	]


def _get_stock_allocations(identities, *, run_name, commitment_names=()):
	if not identities or not frappe.db.exists("DocType", "APS Stock Coverage Allocation"):
		return []
	commitment_names = sorted({name for name in commitment_names or [] if name})
	if not commitment_names:
		return []
	filters: dict[str, Any] = {"demand_identity": ("in", identities), "status": "Active"}
	if run_name:
		filters["owner_run"] = run_name
	filters["commitment"] = ("in", commitment_names)
	return [dict(row) for row in frappe.get_list("APS Stock Coverage Allocation", filters=filters, fields=["name", "demand_identity", "commitment", "owner_run", "warehouse", "allocated_qty", "consumed_qty", "released_qty", "remaining_qty", "status"], limit_page_length=0)]


def _get_delivery_allocations(identities):
	if not identities or not frappe.db.exists("DocType", "APS Delivery Allocation"):
		return []
	return [dict(row) for row in frappe.get_list("APS Delivery Allocation", filters={"demand_identity": ("in", identities), "is_effective": 1}, fields=["name", "demand_identity", "delivery_plan_detail", "source_delivery_note", "source_delivery_note_item", "source_posting_time", "effective_qty", "allocation_method", "match_status", "is_return"], order_by="source_posting_time asc, name asc", limit_page_length=0)]


def _get_delivery_plan_rows(identities):
	if not identities or not frappe.db.exists("DocType", "Delivery Plan Item Qty"):
		return []
	rows = [
		dict(row)
		for row in frappe.db.sql(
			"""
			select i.name, i.parent as delivery_plan, i.custom_aps_demand_identity as demand_identity,
				i.required_arrival_date as required_delivery_date, i.planned_delivery_qty,
				dp.docstatus, dp.modified
			from `tabDelivery Plan Item Qty` i
			inner join `tabDelivery Plan` dp on dp.name = i.parent
			where i.custom_aps_demand_identity in %(identities)s and dp.docstatus < 2
			order by i.required_arrival_date, i.parent, i.idx
			""",
			{"identities": tuple(identities)},
			as_dict=True,
		)
	]
	parents = sorted({row.get("delivery_plan") for row in rows if row.get("delivery_plan")})
	visible_parents = set(
		frappe.get_list(
			"Delivery Plan",
			filters={"name": ("in", parents or [""])},
			pluck="name",
			limit_page_length=0,
		)
	)
	return [row for row in rows if row.get("delivery_plan") in visible_parents]


def _get_bom_peggings(commitment_names):
	if not commitment_names or not frappe.db.exists("DocType", "APS BOM Pegging"):
		return []
	return [dict(row) for row in frappe.get_list("APS BOM Pegging", filters={"root_commitment": ("in", commitment_names), "status": ("!=", "Cancelled")}, fields=["name", "root_commitment", "parent_result", "child_result", "parent_item", "component_item", "status"], limit_page_length=0)]


def _segment_time(segment, layer: str, boundary: str):
	if layer == "original":
		return segment.get(f"baseline_{boundary}_time") or segment.get(f"solver_{boundary}_time") or segment.get(f"{boundary}_time")
	if layer == "current":
		return segment.get(f"current_{boundary}_time") or segment.get(f"{boundary}_time")
	if layer == "forecast":
		return segment.get(f"forecast_{boundary}_time") or segment.get(f"current_{boundary}_time") or segment.get(f"{boundary}_time")
	return segment.get(f"{layer}_{boundary}_time")


def _segment_plan_change_reason(segment):
	original_start = _as_datetime(_segment_time(segment, "original", "start"))
	original_end = _as_datetime(_segment_time(segment, "original", "end"))
	current_start = _as_datetime(_segment_time(segment, "current", "start"))
	current_end = _as_datetime(_segment_time(segment, "current", "end"))
	if not original_end or not current_end:
		return None
	reasons = []
	completion_delay = (current_end - original_end).total_seconds() / 3600
	if completion_delay > 0.01:
		reasons.append(_("Plan completion moved {0} hours later.", context="Injection APS").format(round(completion_delay, 1)))
	if original_start and current_start:
		original_duration = (original_end - original_start).total_seconds() / 3600
		current_duration = (current_end - current_start).total_seconds() / 3600
		if current_duration - original_duration > 0.01:
			reasons.append(_("Segment duration increased by {0} hours.", context="Injection APS").format(round(current_duration - original_duration, 1)))
	return " ".join(reasons) or None


def _stock_remaining(row):
	if row.get("remaining_qty") not in (None, ""):
		return max(flt(row.get("remaining_qty")), 0)
	return max(flt(row.get("allocated_qty")) - flt(row.get("consumed_qty")) - flt(row.get("released_qty")), 0)


def _json_source_names(value) -> list[str]:
	try:
		rows = value if isinstance(value, list) else json.loads(value or "[]")
	except (TypeError, ValueError):
		return []
	if not isinstance(rows, list):
		return []
	return [
		str(row.get("name") if isinstance(row, dict) else row)
		for row in rows
		if (row.get("name") if isinstance(row, dict) else row)
	]


def _matrix_window(date_from, date_to):
	start = getdate(date_from) if date_from else getdate(nowdate()) - timedelta(days=7)
	end = getdate(date_to) if date_to else getdate(nowdate()) + timedelta(days=21)
	if end < start:
		frappe.throw(_("Progress matrix end date cannot be earlier than its start date.", context="Injection APS"), frappe.ValidationError)
	if (end - start).days + 1 > MAX_MATRIX_RANGE_DAYS:
		frappe.throw(_("Progress matrix range cannot exceed {0} days.", context="Injection APS").format(MAX_MATRIX_RANGE_DAYS), frappe.ValidationError)
	return start, end


def _date_strings(start: date, end: date):
	return [str(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]


def _empty_cell(status=None):
	return {**{field: 0.0 for field in LAYER_FIELDS}, "status": status or "Unknown", "sources": [], "reasons": []}


def _add_event(events, date_value, layer, qty, sources, reason=None):
	if not date_value or layer not in LAYER_FIELDS or abs(flt(qty)) <= QTY_TOLERANCE:
		return
	events.append({"date": str(date_value), "layer": layer, "qty": flt(qty), "sources": _dedupe_sources(sources), "reason": reason})


def _doc_ref(doctype, name):
	if not doctype or not name:
		return None
	return {"doctype": doctype, "name": name, "label": name}


def _dedupe_sources(sources):
	result = []
	seen = set()
	for source in sources or []:
		if not source or not source.get("doctype") or not source.get("name"):
			continue
		key = (source.get("doctype"), source.get("name"))
		if key in seen:
			continue
		seen.add(key)
		result.append(dict(source))
	return result


def _lineage_groups(sources):
	groups = defaultdict(list)
	production = {"APS Schedule Result", "APS Schedule Segment", "APS Production Campaign", "APS Production Allocation", "Stock Entry", "Work Order", "Work Order Scheduling", "Scheduling Item", "APS BOM Pegging"}
	delivery = {"Delivery Plan", "Delivery Plan Item Qty", "Delivery Note", "APS Delivery Allocation"}
	for source in sources or []:
		group = "production" if source.get("doctype") in production else "delivery" if source.get("doctype") in delivery else "demand"
		groups[group].append(source)
	return dict(groups)


def _group_rows(rows, key):
	result = defaultdict(list)
	for row in rows or []:
		result[row.get(key)].append(row)
	return result


def _resolve_company(company):
	if company:
		return company
	return frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("APS Settings", "default_company")


def _date_of(value):
	if not value:
		return None
	return str(getdate(value))


def _as_datetime(value):
	if not value:
		return None
	return get_datetime(value)


def _latest_datetime(values):
	prepared = [_as_datetime(value) for value in values if value]
	return max(prepared) if prepared else None
