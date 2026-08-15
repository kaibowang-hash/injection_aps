from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from time import perf_counter
from typing import Any, Iterable

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, nowdate


QTY_TOLERANCE = 1e-6
ACTIVE_OWNER_STATUSES = ("Draft", "Proposed", "Approved", "Released", "In Progress")
MAX_PAGE_LENGTH = 200
MAX_MATRIX_COLUMNS = 31
MAX_MATRIX_RANGE_DAYS = 366

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
		offset=offset,
		page_length=page_length,
		schedule_item=schedule_item,
	)
	projection = _build_projection(schedule_rows, run_name=run_name)
	rows = projection["rows"]
	if status:
		rows = [row for row in rows if row.get("status") == status]
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
			"has_more": offset + len(schedule_rows) < total_rows,
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
) -> dict[str, Any]:
	window_start, window_end = _matrix_window(date_from, date_to)
	all_dates = _date_strings(window_start, window_end)
	column_offset = max(cint(column_offset), 0)
	column_limit = min(max(cint(column_limit or 14), 1), MAX_MATRIX_COLUMNS)
	visible_dates = all_dates[column_offset : column_offset + column_limit]
	response = get_progress_detail(
		company=company,
		customer=customer,
		item_code=item_code,
		schedule_scope=schedule_scope,
		date_from=date_from,
		date_to=date_to,
		status=status,
		run_name=run_name,
		offset=offset,
		page_length=page_length,
	)
	matrix_rows = []
	for row in response["rows"]:
		cells = build_sparse_matrix_cells(row.get("events") or [], visible_dates, default_status=row.get("status"))
		matrix_rows.append(
			{
				"key": row.get("schedule_item") or row.get("demand_identity"),
				"company": row.get("company"),
				"schedule": row.get("schedule"),
				"demand_identity": row.get("demand_identity"),
				"schedule_item": row.get("schedule_item"),
				"customer": row.get("customer"),
				"item_code": row.get("item_code"),
				"customer_part_no": row.get("customer_part_no"),
				"schedule_date": row.get("schedule_date"),
				"schedule_qty": row.get("schedule_qty"),
				"open_demand_qty": row.get("open_demand_qty"),
				"original_plan_qty": row.get("original_plan_qty"),
				"current_plan_qty": row.get("current_plan_qty"),
				"forecast_qty": row.get("forecast_qty"),
				"actual_good_qty": row.get("actual_good_qty"),
				"actual_scrap_qty": row.get("actual_scrap_qty"),
				"delivery_plan_qty": row.get("delivery_plan_qty"),
				"delivered_qty": row.get("delivered_qty"),
				"stock_covered_qty": row.get("stock_covered_qty"),
				"shortage_qty": row.get("shortage_qty"),
				"recovery_qty": row.get("recovery_qty"),
				"conservation_status": row.get("conservation_status"),
				"status": row.get("status"),
				"status_tone": row.get("status_tone"),
				"reason": row.get("reason"),
				"source_documents": row.get("source_documents") or [],
				"commitment_names": row.get("commitment_names") or [],
				"result_names": row.get("result_names") or [],
				"run_names": row.get("run_names") or [],
				"cells": cells,
			}
		)
	response["rows"] = matrix_rows
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


def get_progress_cell(
	*,
	date_value,
	demand_identity: str | None = None,
	schedule_item: str | None = None,
	run_name: str | None = None,
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


def _build_projection(schedule_rows: list[dict[str, Any]], *, run_name: str | None) -> dict[str, Any]:
	identities = sorted({row.get("demand_identity") for row in schedule_rows if row.get("demand_identity")})
	commitments = _get_commitments(identities, run_name=run_name)
	commitment_names = [row["name"] for row in commitments]
	results = _get_results(commitment_names)
	result_names = [row["name"] for row in results]
	segments = _get_segments(result_names)
	stock_rows = _get_stock_allocations(identities, run_name=run_name)
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
				stock_rows=stock_by_identity.get(identity) or [],
				delivery_rows=delivery_by_identity.get(identity) or [],
				delivery_plan_rows=dp_by_identity.get(identity) or [],
				pegging_rows=root_peggings,
			)
		)
	selected_runs.discard(None)
	projection_type = "Single Run" if run_name else "Effective Cross-Run"
	return {
		"projection": {
			"type": projection_type,
			"single_run_view": cint(bool(run_name)),
			"selected_run": run_name,
			"run_names": sorted(selected_runs | ({run_name} if run_name else set())),
			"label": _("Single Run view", context="Injection APS") if run_name else _("Current effective cross-Run projection", context="Injection APS"),
			"reason": (
				_("Only commitments from the explicitly selected APS Run are shown.", context="Injection APS")
				if run_name
				else _("Each Demand Identity is projected from its current Formal owner; no recent old Run is selected by status priority.", context="Injection APS")
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
	stock_rows: list[dict[str, Any]],
	delivery_rows: list[dict[str, Any]],
	delivery_plan_rows: list[dict[str, Any]],
	pegging_rows: list[dict[str, Any]],
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
	if not commitments:
		shortage_qty = open_demand_qty
	if commitments and shortage_qty <= QTY_TOLERANCE:
		shortage_qty = sum(flt(item.get("critical_unplanned_qty")) for item in results)
	carried_qty = sum(flt(item.get("carried_qty")) for item in commitments)
	new_plan_qty = sum(flt(item.get("newly_planned_qty")) for item in commitments)
	requested_open_qty = sum(flt(item.get("requested_qty")) for item in commitments)
	partition_qty = on_time_qty + recovery_qty + shortage_qty
	partition_status = "Pending" if commitments and partition_qty <= QTY_TOLERANCE and new_plan_qty > QTY_TOLERANCE else "Validated"
	partition_delta = 0 if partition_status == "Pending" else round(new_plan_qty - partition_qty, 6)
	demand_delta = round(requested_open_qty - stock_qty - carried_qty - new_plan_qty, 6) if commitments else 0
	conservation_status = "OK"
	if commitments and (abs(partition_delta) > QTY_TOLERANCE or abs(demand_delta) > QTY_TOLERANCE):
		conservation_status = "Mismatch"

	active_segments = [item for item in segments if item.get("segment_status") != "Cancelled" and flt(item.get("planned_qty")) > 0]
	original_plan_qty = sum(flt(item.get("planned_qty")) for item in active_segments)
	current_plan_qty = original_plan_qty
	forecast_qty = original_plan_qty
	original_completion = _latest_datetime(_segment_time(item, "original", "end") for item in active_segments)
	current_completion = _latest_datetime(_segment_time(item, "current", "end") for item in active_segments)
	forecast_completion = _latest_datetime(_segment_time(item, "forecast", "end") for item in active_segments)
	if not forecast_completion:
		forecast_completion = _latest_datetime(item.get("projected_completion_time") for item in results)
	actual_good_qty = sum(max(flt(item.get("actual_good_qty")), 0) for item in active_segments)
	actual_scrap_qty = sum(max(flt(item.get("actual_scrap_qty")), 0) for item in active_segments)
	if actual_good_qty <= QTY_TOLERANCE and actual_scrap_qty <= QTY_TOLERANCE:
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

	sources = _build_row_sources(row, commitments, results, active_segments, stock_rows, delivery_rows, delivery_plan_rows, pegging_rows)
	events = _build_row_events(
		row,
		active_segments=active_segments,
		results=results,
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


def _build_row_events(
	row,
	*,
	active_segments,
	results,
	stock_qty,
	shortage_qty,
	recovery_qty,
	recovery_completion,
	delivery_rows,
	delivery_plan_rows,
):
	events = []
	due_date = str(getdate(row.get("schedule_date")))
	schedule_sources = [_doc_ref("Customer Delivery Schedule", row.get("schedule")), _doc_ref("Customer Delivery Schedule Item", row.get("schedule_item"))]
	if row.get("demand_identity"):
		schedule_sources.append(_doc_ref("APS Demand Identity", row.get("demand_identity")))
	_add_event(events, due_date, "schedule_qty", row.get("schedule_qty"), schedule_sources)
	_add_event(events, due_date, "stock_covered_qty", stock_qty, schedule_sources)
	_add_event(events, due_date, "shortage_qty", shortage_qty, schedule_sources)
	for segment in active_segments:
		segment_sources = _segment_sources(segment, results)
		planned_qty = flt(segment.get("planned_qty"))
		_add_event(events, _date_of(_segment_time(segment, "original", "end")), "original_plan_qty", planned_qty, segment_sources)
		_add_event(events, _date_of(_segment_time(segment, "current", "end")), "current_plan_qty", planned_qty, segment_sources)
		_add_event(events, _date_of(_segment_time(segment, "forecast", "end")), "forecast_qty", planned_qty, segment_sources)
		actual_date = _date_of(segment.get("actual_end_time") or segment.get("last_actual_report_time") or segment.get("actual_start_time"))
		_add_event(events, actual_date, "actual_good_qty", segment.get("actual_good_qty"), segment_sources)
		_add_event(events, actual_date, "actual_scrap_qty", segment.get("actual_scrap_qty"), segment_sources)
	if not active_segments:
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


def _build_row_sources(row, commitments, results, segments, stock_rows, delivery_rows, delivery_plan_rows, pegging_rows):
	sources = [
		_doc_ref("Customer Delivery Schedule", row.get("schedule")),
		_doc_ref("Customer Delivery Schedule Item", row.get("schedule_item")),
		_doc_ref("APS Demand Identity", row.get("demand_identity")),
	]
	for item in commitments:
		sources.extend([_doc_ref("APS Demand Commitment", item.get("name")), _doc_ref("APS Planning Run", item.get("planning_run"))])
	for item in results:
		sources.extend([_doc_ref("APS Schedule Result", item.get("name")), _doc_ref("APS Production Campaign", item.get("production_campaign"))])
	for item in segments:
		sources.extend(_segment_sources(item, results))
	for item in stock_rows:
		sources.append(_doc_ref("APS Stock Coverage Allocation", item.get("name")))
	for item in delivery_rows:
		sources.extend([_doc_ref("APS Delivery Allocation", item.get("name")), _doc_ref("Delivery Note", item.get("source_delivery_note"))])
	for item in delivery_plan_rows:
		sources.extend([_doc_ref("Delivery Plan", item.get("delivery_plan")), _doc_ref("Delivery Plan Item Qty", item.get("name"))])
	for item in pegging_rows:
		sources.append(_doc_ref("APS BOM Pegging", item.get("name")))
	return _dedupe_sources(sources)


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
	qty_expression = "case when ifnull(i.effective_qty, 0) > 0 then i.effective_qty else i.qty end"
	conditions = ["s.status = 'Active'", "ifnull(i.status, '') != 'Cancelled'", f"{qty_expression} > 0"]
	params: dict[str, Any] = {"offset": offset, "page_length": page_length}
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


def _get_commitments(identities, *, run_name):
	if not identities:
		return []
	conditions = ["demand_identity in %(identities)s", "status not in ('Cancelled', 'Excess')"]
	params: dict[str, Any] = {"identities": tuple(identities)}
	if run_name:
		conditions.append("planning_run = %(run_name)s")
		params["run_name"] = run_name
	else:
		conditions.extend(["formal_owner = 1", "owner_state = 'Owned'", "status in %(statuses)s"])
		params["statuses"] = ACTIVE_OWNER_STATUSES
	return [
		dict(row)
		for row in frappe.db.sql(
			f"""
			select name, planning_run, demand_identity, schedule_item, status, owner_state, formal_owner,
				requested_qty, stock_covered_qty, carried_qty, newly_planned_qty,
				on_time_qty, late_qty, unscheduled_qty, produced_qty, delivered_qty,
				remaining_qty, excess_qty, effective_due_time, input_fingerprint, transitioned_on
			from `tabAPS Demand Commitment`
			where {' and '.join(conditions)}
			order by demand_identity, transitioned_on desc, name desc
			""",
			params,
			as_dict=True,
		)
	]


def _get_results(commitment_names):
	if not commitment_names:
		return []
	fields = [
		"name", "planning_run", "demand_commitment", "production_campaign", "item_code",
		"machine_scheduled_qty", "on_time_qty", "recovery_qty", "critical_unplanned_qty",
		"good_produced_qty", "scrap_qty", "actual_start_time", "actual_end_time",
		"last_actual_report_time", "projected_completion_time", "recovery_completion_time",
		"risk_status", "status", "shortage_code", "solver_explanation",
	]
	return [dict(row) for row in frappe.get_all("APS Schedule Result", filters={"demand_commitment": ("in", commitment_names)}, fields=fields, order_by="modified asc", limit_page_length=0)]


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


def _get_stock_allocations(identities, *, run_name):
	if not identities or not frappe.db.exists("DocType", "APS Stock Coverage Allocation"):
		return []
	filters: dict[str, Any] = {"demand_identity": ("in", identities), "status": "Active"}
	if run_name:
		filters["owner_run"] = run_name
	return [dict(row) for row in frappe.get_all("APS Stock Coverage Allocation", filters=filters, fields=["name", "demand_identity", "commitment", "owner_run", "warehouse", "allocated_qty", "consumed_qty", "released_qty", "remaining_qty", "status"], limit_page_length=0)]


def _get_delivery_allocations(identities):
	if not identities or not frappe.db.exists("DocType", "APS Delivery Allocation"):
		return []
	return [dict(row) for row in frappe.get_all("APS Delivery Allocation", filters={"demand_identity": ("in", identities), "is_effective": 1}, fields=["name", "demand_identity", "delivery_plan_detail", "source_delivery_note", "source_delivery_note_item", "source_posting_time", "effective_qty", "allocation_method", "match_status", "is_return"], order_by="source_posting_time asc, name asc", limit_page_length=0)]


def _get_delivery_plan_rows(identities):
	if not identities or not frappe.db.exists("DocType", "Delivery Plan Item Qty"):
		return []
	return [
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


def _get_bom_peggings(commitment_names):
	if not commitment_names or not frappe.db.exists("DocType", "APS BOM Pegging"):
		return []
	return [dict(row) for row in frappe.get_all("APS BOM Pegging", filters={"root_commitment": ("in", commitment_names), "status": ("!=", "Cancelled")}, fields=["name", "root_commitment", "parent_result", "child_result", "parent_item", "component_item", "status"], limit_page_length=0)]


def _segment_time(segment, layer: str, boundary: str):
	if layer == "original":
		return segment.get(f"baseline_{boundary}_time") or segment.get(f"solver_{boundary}_time") or segment.get(f"{boundary}_time")
	if layer == "current":
		return segment.get(f"current_{boundary}_time") or segment.get(f"{boundary}_time")
	if layer == "forecast":
		return segment.get(f"forecast_{boundary}_time") or segment.get(f"current_{boundary}_time") or segment.get(f"{boundary}_time")
	return segment.get(f"{layer}_{boundary}_time")


def _stock_remaining(row):
	if row.get("remaining_qty") not in (None, ""):
		return max(flt(row.get("remaining_qty")), 0)
	return max(flt(row.get("allocated_qty")) - flt(row.get("consumed_qty")) - flt(row.get("released_qty")), 0)


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
	production = {"APS Schedule Result", "APS Schedule Segment", "APS Production Campaign", "Work Order", "Work Order Scheduling", "Scheduling Item", "APS BOM Pegging"}
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
