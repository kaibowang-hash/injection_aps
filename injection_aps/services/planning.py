from __future__ import annotations

import hashlib
import json
import math
import os
import zipfile
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

import frappe
from frappe import _
from openpyxl import load_workbook
from openpyxl.utils.cell import column_index_from_string, get_column_letter
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime, today

from injection_aps.services import consistency
from injection_aps.services.permissions import (
	APS_APPROVE_ROLES,
	APS_DEMAND_ROLES,
	APS_PLAN_ROLES,
	APS_RELEASE_ROLES,
)


DEMAND_SOURCE_PRIORITY = {
	"Urgent Order": 1000,
	"Customer Delivery Schedule": 800,
	"Forecast": 700,
	"Sales Order Backlog": 600,
	"Safety Stock": 400,
	"Trial Production": 300,
	"Complaint Replenishment": 300,
}

RUN_OPEN_STATUSES = ("Draft", "Planned", "Approved", "Work Order Proposed", "Shift Proposed")
LOCKED_SEGMENT_STATUSES = ("Approved", "Work Order Proposed", "Shift Proposed", "Applied")
MANUAL_ADJUSTMENT_BLOCKED_SEGMENT_STATUSES = ("Applied", "Completed")
BLOCKING_WORKSTATION_RISK = "Non FDA"
MAX_REBUILD_WARNINGS = 20
ITEM_NAME_PREFIX_FALLBACKS = ("临时物料:",)
SCHEDULABLE_ITEM_GROUPS = ("Plastic Part", "Sub-assemblies")
BLOCKING_MOLD_STATUSES = (
	"Under Maintenance",
	"Under External Maintenance",
	"Scrapped",
	"Outsourced",
	"Pending Asset Link",
)
APS_ALLOWED_MACHINE_STATUSES = ("Available", "Running", "Setup")
FROZEN_SCHEDULING_STATUSES = ("Material Transfer", "Job Card", "Manufacture")
ACTIVE_SCHEDULING_STATUSES = ("", "Schedule Confirmed", "Material Transfer", "Job Card", "Manufacture")
ACTIVE_DOWNTIME_STATUSES = ("Active", "Applied")
CONFIRMED_ADJUSTMENT_STATUSES = ("Confirmed", "Applied")
SCHEDULE_IMPORT_STRATEGIES = ("Replace Scope", "Partial Update", "Append")
SCHEDULE_DUPLICATE_POLICIES = ("Block", "Sum")
SCHEDULE_POLICY_FIELDS = (
	"production_strategy",
	"demand_confidence",
	"cancellation_risk_percent",
	"prebuild_allowed",
	"max_prebuild_days",
)
ANCHOR_STRENGTH_HARD = 100
ANCHOR_STRENGTH_RELEASED = 70
ANCHOR_STRENGTH_LOCKED = 50
ANCHOR_STRENGTH_SOFT = 20
CAPACITY_SOURCE_LABELS = {
	"mold_cycle": "Mold Cycle × Output Per Cycle",
	"machine_hourly_fallback": "APS Machine Hourly Capacity",
	"machine_daily_fallback": "APS Machine Daily Capacity",
	"fallback_cycle": "Fallback Cycle",
	"default_hourly_fallback": "Fallback Hourly Capacity",
}
SCHEDULE_PROGRESS_RUN_STATUS_PRIORITY = {
	"Applied": 1,
	"Shift Proposed": 2,
	"Work Order Proposed": 3,
	"Approved": 4,
	"Planned": 5,
}
SCHEDULE_PROGRESS_RISK_ACTUAL_STATUSES = ("Delayed", "Slow Progress", "No Recent Update", "Overproduced")
SCHEDULE_PROGRESS_RISK_RESULT_STATUSES = ("Attention", "Critical", "Blocked")
EXISTING_WORK_ORDER_POLICIES = ("Include", "Exclude")
INACTIVE_WORK_ORDER_STATUSES = ("Stopped", "Completed", "Closed", "Cancelled")
MAX_SCHEDULE_FILE_BYTES = 15 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 120 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 150
MAX_XLSX_ARCHIVE_MEMBERS = 5_000
MAX_XLSX_MEMBER_BYTES = 40 * 1024 * 1024
MAX_SCHEDULE_WORKSHEET_ROWS = 50_000
MAX_SCHEDULE_WORKSHEET_COLUMNS = 512
MAX_SCHEDULE_WORKSHEET_CELLS = 2_000_000
MAX_SCHEDULE_NORMALIZED_ROWS = 50_000
MAX_SCHEDULE_ROWS_JSON_BYTES = 10 * 1024 * 1024
MAX_SCHEDULE_MAPPING_JSON_BYTES = 256 * 1024
MAX_SCHEDULE_MAPPING_FIELDS = 24
MAX_SCHEDULE_MAPPING_NESTING_DEPTH = 2
MAX_SCHEDULE_MAPPING_VALUE_CHARACTERS = 4_096
MAX_SCHEDULE_ROW_FIELDS = 64
MAX_SCHEDULE_CELL_CHARACTERS = 32_767
SCHEDULE_VALIDATION_QUERY_CHUNK_SIZE = 1_000
QTY_TOLERANCE = 0.000001
WORK_ORDER_PROPOSAL_RESULT_FIELDS = (
	"name",
	"planning_run",
	"net_requirement",
	"customer",
	"sales_order",
	"sales_order_item",
	"item_code",
	"requested_date",
	"demand_source",
	"machine_scheduled_qty",
	"production_strategy",
	"demand_confidence",
	"cancellation_risk_percent",
	"prebuild_allowed",
	"max_prebuild_days",
	"demand_source_snapshot_json",
	"fulfillment_baseline_json",
	"is_urgent",
	"is_locked",
	"is_manual",
	"status",
	"flow_step",
	"blocking_reason",
	"modified",
)
SCHEDULE_MAPPING_FIELDS = frozenset(
	{
		"parser_mode",
		"sheet_name",
		"header_row_no",
		"data_start_row_no",
		"item_reference_column",
		"customer_part_no_column",
		"description_column",
		"sales_order_column",
		"row_type_column",
		"remark_column",
		"demand_row_type_value",
		"date_columns_mode",
		"date_start_column",
		"date_end_column",
	}
)

ACTION_REQUIRED_ROLES = {
	"promote_import": APS_PLAN_ROLES,
	"rebuild_demand_pool": APS_PLAN_ROLES,
	"run_trial": APS_PLAN_ROLES,
	"approve": APS_APPROVE_ROLES,
	"generate_work_order_proposals": APS_RELEASE_ROLES,
	"generate_shift_schedule_proposals": APS_RELEASE_ROLES,
	"apply_work_order_proposals": APS_RELEASE_ROLES,
	"apply_shift_schedule_proposals": APS_RELEASE_ROLES,
	"analyze_change_request": APS_DEMAND_ROLES,
	"confirm_change_request": APS_PLAN_ROLES,
	"approve_change_request": APS_APPROVE_ROLES,
	"reject_change_request": APS_APPROVE_ROLES,
	"apply_change_request": APS_APPROVE_ROLES,
	"analyze_capacity_balance": APS_PLAN_ROLES,
	"confirm_capacity_balance": APS_PLAN_ROLES,
	"apply_capacity_balance": APS_PLAN_ROLES,
}


class APSItemReferenceError(frappe.ValidationError):
	pass


def _normalize_existing_work_order_policy(value: str | None) -> str:
	policy = (value or "").strip() if isinstance(value, str) else ""
	if policy not in EXISTING_WORK_ORDER_POLICIES:
		frappe.throw(
			_("Please explicitly select whether to include or exclude existing work orders before calculating net requirements."),
			frappe.ValidationError,
		)
	return policy


def _normalize_item_code(value: str | None) -> str:
	return _resolve_item_name(value) or (value or "")


def _build_campaign_key(item_code: str | None, mould_reference: str | None, workstation: str | None) -> str:
	item_code = _normalize_item_code(item_code)
	return "::".join([item_code or "", mould_reference or "", workstation or ""])


def _intervals_overlap(left_start, left_end, right_start, right_end) -> bool:
	if not left_start or not left_end or not right_start or not right_end:
		return False
	return get_datetime(left_start) < get_datetime(right_end) and get_datetime(left_end) > get_datetime(right_start)


def _get_active_downtime_windows(
	company: str | None = None,
	plant_floors: list[str] | str | None = None,
	horizon_start=None,
	horizon_end=None,
	run_name: str | None = None,
) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "APS Downtime Window"):
		return []
	selected_plant_floors = _coerce_plant_floor_list(plant_floors=plant_floors)
	conditions = ["status in ({0})".format(", ".join(["%s"] * len(ACTIVE_DOWNTIME_STATUSES)))]
	values: list[Any] = list(ACTIVE_DOWNTIME_STATUSES)
	if company:
		conditions.append("company = %s")
		values.append(company)
	if horizon_start:
		conditions.append("end_time > %s")
		values.append(get_datetime(horizon_start))
	if horizon_end:
		conditions.append("start_time < %s")
		values.append(get_datetime(horizon_end))
	if selected_plant_floors:
		conditions.append("(ifnull(plant_floor, '') = '' or plant_floor in ({0}))".format(", ".join(["%s"] * len(selected_plant_floors))))
		values.extend(selected_plant_floors)
	if run_name:
		conditions.append("(ifnull(planning_run, '') = '' or planning_run = %s)")
		values.append(run_name)
	return frappe.db.sql(
		"""
		select
			name,
			company,
			scope,
			plant_floor,
			workstation,
			start_time,
			end_time,
			available_capacity_percent,
			reason,
			status,
			planning_run
		from `tabAPS Downtime Window`
		where {conditions}
		order by start_time asc, end_time asc
		""".format(conditions=" and ".join(conditions)),
		values,
		as_dict=True,
	)


def _downtime_applies_to_target(
	window: dict[str, Any],
	workstation: str | None = None,
	plant_floor: str | None = None,
	company: str | None = None,
) -> bool:
	if company and window.get("company") and window.get("company") != company:
		return False
	scope = window.get("scope") or "Plant Floor"
	if scope == "Company":
		return True
	if scope == "Workstation":
		return bool(workstation) and window.get("workstation") == workstation
	if window.get("plant_floor") and plant_floor and window.get("plant_floor") != plant_floor:
		return False
	return bool(plant_floor) or not window.get("plant_floor")


def _get_matching_downtime_windows(
	downtime_windows: list[dict[str, Any]] | None,
	workstation: str | None = None,
	plant_floor: str | None = None,
	company: str | None = None,
) -> list[dict[str, Any]]:
	return [
		row
		for row in sorted(downtime_windows or [], key=lambda item: (get_datetime(item.get("start_time")), get_datetime(item.get("end_time"))))
		if _downtime_applies_to_target(row, workstation=workstation, plant_floor=plant_floor, company=company)
	]


def _shift_start_past_downtime(start_time, downtime_windows: list[dict[str, Any]] | None):
	start_value = get_datetime(start_time)
	changed = True
	while changed:
		changed = False
		for row in downtime_windows or []:
			if _downtime_capacity_factor(row) > 0:
				continue
			window_start = get_datetime(row.get("start_time"))
			window_end = get_datetime(row.get("end_time"))
			if window_start <= start_value < window_end:
				start_value = window_end
				changed = True
				break
	return start_value


def _available_run_hours_between(start_time, end_time, downtime_windows: list[dict[str, Any]] | None) -> float:
	start_value = get_datetime(start_time)
	end_value = get_datetime(end_time)
	if end_value <= start_value:
		return 0
	available_hours = 0.0
	for interval_start, interval_end, factor, _active_windows in _iter_capacity_intervals(
		start_value,
		end_value,
		downtime_windows,
	):
		available_hours += max((interval_end - interval_start).total_seconds() / 3600, 0) * factor
	return max(available_hours, 0)


def _estimate_end_for_qty_around_downtime(
	start_time,
	qty: float,
	hourly_capacity_qty: float,
	downtime_windows: list[dict[str, Any]] | None,
	horizon_end=None,
):
	rate = max(flt(hourly_capacity_qty), 0)
	current = get_datetime(start_time)
	if rate <= 0 or flt(qty) <= 0:
		return current
	remaining_hours = flt(qty) / rate
	limit = get_datetime(horizon_end) if horizon_end else None
	window_end = max(
		[get_datetime(row.get("end_time")) for row in downtime_windows or [] if row.get("end_time")] or [current]
	)
	timeline_end = max(window_end, limit or window_end, current)
	for interval_start, interval_end, factor, _active_windows in _iter_capacity_intervals(
		current,
		timeline_end,
		downtime_windows,
	):
		if factor <= 0:
			current = interval_end
			continue
		wall_hours = max((interval_end - interval_start).total_seconds() / 3600, 0)
		effective_hours = wall_hours * factor
		if remaining_hours <= effective_hours + 1e-12:
			return interval_start + timedelta(hours=remaining_hours / factor)
		remaining_hours -= effective_hours
		current = interval_end
	return current + timedelta(hours=remaining_hours)


def _allocate_qty_around_downtime(
	start_time,
	qty: float,
	hourly_capacity_qty: float,
	horizon_end,
	downtime_windows: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], float]:
	rate = max(flt(hourly_capacity_qty), 0)
	remaining = flt(qty)
	current = get_datetime(start_time)
	limit = get_datetime(horizon_end)
	chunks: list[dict[str, Any]] = []
	if rate <= 0 or remaining <= 0 or current >= limit:
		return chunks, remaining
	for interval_start, interval_end, factor, active_windows in _iter_capacity_intervals(
		current,
		limit,
		downtime_windows,
	):
		if remaining <= 0:
			break
		if factor <= 0:
			continue
		effective_rate = rate * factor
		wall_hours = max((interval_end - interval_start).total_seconds() / 3600, 0)
		chunk_qty = min(remaining, wall_hours * effective_rate)
		if chunk_qty <= 0:
			continue
		chunk_end = interval_start + timedelta(hours=chunk_qty / effective_rate)
		chunks.append(
			{
				"start_time": interval_start,
				"end_time": chunk_end,
				"planned_qty": chunk_qty,
				"downtime_window": active_windows[0].get("name") if active_windows else None,
				"available_capacity_percent": factor * 100,
			}
		)
		remaining -= chunk_qty
	return chunks, max(remaining, 0)


def _downtime_capacity_factor(window: dict[str, Any]) -> float:
	value = window.get("available_capacity_percent")
	if value in (None, ""):
		return 0.0
	return min(max(flt(value) / 100, 0), 1)


def _iter_capacity_intervals(start_time, end_time, downtime_windows: list[dict[str, Any]] | None):
	start_value = get_datetime(start_time)
	end_value = get_datetime(end_time)
	boundaries = {start_value, end_value}
	for row in downtime_windows or []:
		window_start = max(start_value, get_datetime(row.get("start_time")))
		window_end = min(end_value, get_datetime(row.get("end_time")))
		if window_start < window_end:
			boundaries.add(window_start)
			boundaries.add(window_end)
	ordered = sorted(boundaries)
	for index in range(len(ordered) - 1):
		interval_start = ordered[index]
		interval_end = ordered[index + 1]
		if interval_end <= interval_start:
			continue
		midpoint = interval_start + (interval_end - interval_start) / 2
		active_windows = [
			row
			for row in downtime_windows or []
			if get_datetime(row.get("start_time")) <= midpoint < get_datetime(row.get("end_time"))
		]
		factor = min([_downtime_capacity_factor(row) for row in active_windows] or [1.0])
		yield interval_start, interval_end, factor, active_windows


def _coerce_plant_floor_list(plant_floors: Any = None, plant_floor: str | None = None) -> list[str]:
	values = []
	if plant_floors:
		parsed = plant_floors
		if isinstance(parsed, str):
			text = parsed.strip()
			if text.startswith("["):
				try:
					parsed = json.loads(text)
				except Exception:
					parsed = [chunk.strip() for chunk in text.replace("\n", ",").split(",") if chunk.strip()]
			else:
				parsed = [chunk.strip() for chunk in text.replace("\n", ",").split(",") if chunk.strip()]
		if not isinstance(parsed, (list, tuple, set)):
			parsed = [parsed]
		for row in parsed:
			value = row.get("plant_floor") if isinstance(row, dict) else row
			if value and str(value).strip() and str(value).strip() not in values:
				values.append(str(value).strip())
	if plant_floor and plant_floor not in values:
		values.append(plant_floor)
	return values


def _normalize_selected_plant_floors(
	company: str | None,
	plant_floors: Any = None,
	plant_floor: str | None = None,
	required: bool = False,
) -> list[str]:
	selected = _coerce_plant_floor_list(plant_floors=plant_floors, plant_floor=plant_floor)
	if not selected:
		if required:
			frappe.throw(_("Select at least one Plant Floor before APS planning."))
		return []
	if not frappe.db.exists("DocType", "Plant Floor"):
		frappe.throw(_("Plant Floor master data is required for APS planning."))
	rows = frappe.get_all(
		"Plant Floor",
		filters={"name": ("in", selected)},
		fields=["name", "company"],
	)
	row_map = {row.name: row for row in rows}
	missing = [row for row in selected if row not in row_map]
	if missing:
		frappe.throw(_("Plant Floor {0} was not found.").format(", ".join(missing)))
	if company:
		invalid = [row for row in selected if (row_map.get(row) or {}).get("company") not in ("", None, company)]
		if invalid:
			frappe.throw(
				_("Plant Floor {0} does not belong to company {1}.").format(", ".join(invalid), company)
			)
	return selected


def _apply_selected_plant_floors_to_run(run_doc, plant_floors: list[str]):
	plant_floors = _coerce_plant_floor_list(plant_floors=plant_floors)
	run_doc.set("selected_plant_floors", [])
	for plant_floor in plant_floors:
		run_doc.append("selected_plant_floors", {"plant_floor": plant_floor})
	run_doc.plant_floor = plant_floors[0] if len(plant_floors) == 1 else None
	run_doc.selected_plant_floor_summary = ", ".join(plant_floors)


def _get_run_selected_plant_floors(run_doc) -> list[str]:
	rows = [row.plant_floor for row in (run_doc.get("selected_plant_floors") or []) if row.plant_floor]
	if not rows and getattr(run_doc, "plant_floor", None):
		rows = [run_doc.plant_floor]
	return _coerce_plant_floor_list(rows)


def _get_primary_result_plant_floor(segments: list[dict[str, Any]], fallback: str | None = None) -> str | None:
	for row in segments or []:
		if row.get("segment_kind") == "Family Co-Product":
			continue
		if row.get("plant_floor"):
			return row.get("plant_floor")
	return fallback


def _serialize_diagnostic_json(diagnostic: Any = None, diagnostic_json: str | None = None) -> str:
	if diagnostic_json:
		return str(diagnostic_json)
	if diagnostic in (None, "", {}):
		return ""
	try:
		return json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
	except Exception:
		return ""


def _parse_diagnostic_json(value: Any) -> dict[str, Any]:
	if not value:
		return {}
	if isinstance(value, dict):
		return value
	try:
		parsed = json.loads(value)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _label_run_status(status: str | None) -> str:
	return {
		"Draft": "Draft",
		"Planned": "Recalculated",
		"Approved": "Approved",
		"Work Order Proposed": "Work Order Proposals Generated",
		"Shift Proposed": "Day/Night Shift Proposals Generated",
		"Applied": "Formally Applied",
	}.get(status or "", status or "")


def _label_approval_state(state: str | None) -> str:
	return {
		"Pending": "Pending Approval",
		"Approved": "Approved",
		"Rejected": "Rejected",
	}.get(state or "", state or "")


def _build_planning_run_context(doc) -> dict[str, Any]:
	has_applied_wo_batch = bool(
		frappe.db.exists(
			"APS Work Order Proposal Batch",
			{"planning_run": doc.name, "status": "Applied"},
		)
	)
	has_applied_shift_batch = bool(
		frappe.db.exists(
			"APS Shift Schedule Proposal Batch",
			{"planning_run": doc.name, "status": "Applied"},
		)
	)
	blocking_reason = ""
	current_step = _label_run_status(doc.status or "Draft")
	next_step = "Confirm Run"
	if doc.status == "Draft":
		next_step = "Recalculate"
	elif doc.approval_state != "Approved":
		next_step = "Confirm Run"
	elif has_applied_shift_batch or doc.status == "Applied":
		next_step = "Monitor Execution Drift"
	elif has_applied_wo_batch:
		next_step = "Shift Proposals"
	elif doc.status == "Approved":
		next_step = "Work Order Proposals"
	elif doc.status == "Work Order Proposed":
		next_step = "Review Work Order Proposals"
	elif doc.status == "Shift Proposed":
		next_step = "Review Day/Night Shift Proposals"

	if cint(doc.exception_count) and doc.status in ("Planned", "Approved", "Work Order Proposed", "Shift Proposed"):
		blocking_reason = "There are still {0} APS exceptions waiting for review.".format(doc.exception_count)
	if doc.get("consistency_status") != "Valid":
		blocking_reason = "Plan consistency is {0}. Recalculate and resolve consistency errors before release.".format(
			doc.get("consistency_status") or "Unchecked"
		)

	selected_plant_floors = _get_run_selected_plant_floors(doc)
	return {
		"doctype": "APS Planning Run",
		"docname": doc.name,
		"current_step": current_step,
		"next_step": next_step,
		"blocking_reason": blocking_reason,
		"company": doc.company,
		"selected_plant_floors": selected_plant_floors,
		"selected_plant_floor_summary": ", ".join(selected_plant_floors),
		"horizon_days": cint(doc.horizon_days or 0),
		"status": doc.status,
		"status_label": _label_run_status(doc.status),
		"approval_state": doc.approval_state,
		"approval_state_label": _label_approval_state(doc.approval_state),
		"existing_work_order_policy": doc.get("existing_work_order_policy"),
		"exception_count": cint(doc.exception_count or 0),
		"consistency_status": doc.get("consistency_status") or "Unchecked",
		"quantity_summary": consistency.get_run_quantity_summary(doc.name),
		"planning_date": doc.planning_date,
		"modified": doc.modified,
		"run_route": _build_form_route("APS Planning Run", doc.name),
	}


def get_recent_run_contexts(limit: int = 8) -> list[dict[str, Any]]:
	rows = frappe.get_all(
		"APS Planning Run",
		filters={"status": ("in", RUN_OPEN_STATUSES)},
		fields=[
			"name",
			"company",
			"plant_floor",
			"selected_plant_floor_summary",
			"planning_date",
			"horizon_days",
			"status",
			"approval_state",
			"consistency_status",
			"exception_count",
			"modified",
		],
		order_by="modified desc",
		limit=limit,
	)
	context_rows = []
	for row in rows:
		doc = frappe._dict(row)
		context = _build_planning_run_context(doc)
		context_rows.append(
			{
				"name": row.name,
				"company": row.company,
				"selected_plant_floors": context.get("selected_plant_floors") or _coerce_plant_floor_list(
					plant_floors=(row.selected_plant_floor_summary or "").split(","), plant_floor=row.plant_floor
				),
				"horizon_days": cint(row.horizon_days or 0),
				"status": row.status,
				"status_label": context.get("status_label"),
				"approval_state": row.approval_state,
				"approval_state_label": context.get("approval_state_label"),
				"exception_count": cint(row.exception_count or 0),
				"modified": row.modified,
				"route": f"aps-run-console?run_name={row.name}",
				"gantt_route": f"aps-schedule-gantt?run_name={row.name}",
				"execution_route": f"aps-release-center?run_name={row.name}",
			}
		)
	return context_rows


def preview_customer_delivery_schedule(
	customer: str,
	company: str,
	version_no: str,
	schedule_scope: str | None = None,
	import_strategy: str | None = None,
	duplicate_policy: str | None = None,
	file_url: str | None = None,
	rows_json: str | list[dict] | None = None,
	mapping_json: str | dict[str, Any] | None = None,
	source_type: str = "Customer Delivery Schedule",
) -> dict[str, Any]:
	customer = str(customer or "").strip()
	if not customer:
		frappe.throw(
			_("Customer is required for a customer schedule preview.", context="Injection APS"),
			frappe.ValidationError,
		)
	company = str(company or "").strip()
	if not company:
		frappe.throw(
			_("Company is required for a customer schedule preview.", context="Injection APS"),
			frappe.ValidationError,
		)
	version_no = _normalize_schedule_version(version_no)
	schedule_scope = _normalize_schedule_scope(schedule_scope or version_no)
	import_strategy = _normalize_schedule_import_strategy(import_strategy)
	duplicate_policy = _normalize_schedule_duplicate_policy(duplicate_policy)
	rows, parse_context = _normalize_schedule_rows(
		file_url=file_url,
		rows_json=rows_json,
		mapping_json=mapping_json,
	)
	prepared_rows = _prepare_schedule_rows_for_import(rows)
	active_snapshot = _get_active_schedule_snapshot(
		customer=customer,
		company=company,
		schedule_scope=schedule_scope,
	)
	previous_rows = active_snapshot["rows"]
	row_issues = _validate_schedule_import_rows(
		prepared_rows,
		customer=customer,
		company=company,
	)
	resolved_rows, duplicate_groups = _resolve_schedule_row_duplicates(
		prepared_rows,
		duplicate_policy=duplicate_policy,
	)
	# The Schedule Console imports this editable snapshot rather than reopening
	# the workbook under the Customer lock.  Validate the fully expanded shape
	# now, including all defaulted editor fields, so Preview can never approve a
	# payload that the formal Import endpoint must reject for the same 10 MB cap.
	source_snapshot_rows = _build_schedule_source_snapshot_rows(resolved_rows)
	_validate_schedule_rows_payload(source_snapshot_rows)
	partial_ambiguities = _find_partial_update_ambiguities(
		previous_rows,
		resolved_rows,
	) if import_strategy == "Partial Update" and not row_issues else []
	import_fingerprint = _build_schedule_import_fingerprint(
		customer=customer,
		company=company,
		version_no=version_no,
		schedule_scope=schedule_scope,
		import_strategy=import_strategy,
		source_type=source_type,
		rows=source_snapshot_rows,
	)
	replay = _get_schedule_import_replay(import_fingerprint)
	blocking_duplicate = bool(duplicate_groups and duplicate_policy == "Block")
	append_zero_rows = [row for row in resolved_rows if abs(flt(row.get("qty"))) < 0.000001] if import_strategy == "Append" else []
	can_plan = not row_issues and not blocking_duplicate and not append_zero_rows and not partial_ambiguities
	if can_plan:
		plan = _build_schedule_import_plan(
			previous_rows=previous_rows,
			incoming_rows=resolved_rows,
			import_strategy=import_strategy,
		)
		diff_rows = _attach_schedule_import_impacts(
			plan["diff_rows"],
			customer=customer,
			company=company,
			import_strategy=import_strategy,
		)
		effective_schedule_rows = plan["effective_schedule_rows"]
	else:
		diff_rows = _build_blocked_schedule_preview_rows(prepared_rows, duplicate_groups)
		effective_schedule_rows = []
	delivery_lower_bound_rows = [
		row
		for row in diff_rows
		if import_strategy != "Append"
		and flt(row.get("new_qty")) + 0.000001 < flt(row.get("delivered_qty"))
	]
	previous_total_qty = sum(flt(row.get("qty")) for row in previous_rows)
	post_import_total_qty = (
		previous_total_qty + sum(flt(row.get("qty")) for row in effective_schedule_rows)
		if import_strategy == "Append"
		else sum(flt(row.get("qty")) for row in effective_schedule_rows)
	)
	checks = _build_schedule_import_checks(
		import_strategy=import_strategy,
		duplicate_policy=duplicate_policy,
		duplicate_groups=duplicate_groups,
		row_issues=row_issues,
		append_zero_rows=append_zero_rows,
		partial_ambiguities=partial_ambiguities,
		replay=replay,
		delivery_lower_bound_rows=delivery_lower_bound_rows,
	)
	can_import = not replay and not any(cint(check.get("blocking")) for check in checks)
	return {
		"customer": customer,
		"company": company,
		"schedule_scope": schedule_scope,
		"import_strategy": import_strategy,
		"duplicate_policy": duplicate_policy,
		"version_no": version_no,
		"row_count": len(diff_rows),
		"source_row_count": len(prepared_rows),
		"effective_row_count": len(effective_schedule_rows),
		"previous_total_qty": previous_total_qty,
		"incoming_total_qty": sum(flt(row.get("qty")) for row in resolved_rows),
		"post_import_total_qty": post_import_total_qty,
		"total_delta_qty": post_import_total_qty - previous_total_qty,
		"summary": _summarize_change_types(diff_rows),
		"rows": diff_rows,
		"source_rows": source_snapshot_rows,
		"effective_schedule_rows": effective_schedule_rows,
		"duplicate_groups": duplicate_groups,
		"checks": checks,
		"can_import": can_import,
		"is_idempotent_replay": 1 if replay else 0,
		"existing_import": replay,
		"import_fingerprint": import_fingerprint,
		"active_state_token": active_snapshot["token"],
		"parse_context": parse_context,
	}


def import_customer_delivery_schedule(
	customer: str,
	company: str,
	version_no: str,
	schedule_scope: str | None = None,
	import_strategy: str | None = None,
	duplicate_policy: str | None = None,
	file_url: str | None = None,
	rows_json: str | list[dict] | None = None,
	mapping_json: str | dict[str, Any] | None = None,
	source_type: str = "Customer Delivery Schedule",
	rebuild: int = 0,
	existing_work_order_policy: str | None = None,
	active_state_token: str | None = None,
	expected_import_fingerprint: str | None = None,
	reference_access_validator=None,
) -> dict[str, Any]:
	customer = str(customer or "").strip()
	if not customer:
		frappe.throw(
			_("Customer is required for a customer schedule import.", context="Injection APS"),
			frappe.ValidationError,
		)
	company = str(company or "").strip()
	if not company:
		frappe.throw(
			_("Company is required for a customer schedule import.", context="Injection APS"),
			frappe.ValidationError,
		)
	initial_preview = None
	# The console supplies both confirmation tokens.  In that strict path, do
	# not parse the potentially large source outside the customer lock.  Older
	# callers without tokens retain one compatibility preview to establish the
	# expected state before waiting for the lock.
	if not active_state_token or not expected_import_fingerprint:
		initial_preview = preview_customer_delivery_schedule(
			customer=customer,
			company=company,
			version_no=version_no,
			schedule_scope=schedule_scope,
			import_strategy=import_strategy,
			duplicate_policy=duplicate_policy,
			file_url=file_url,
			rows_json=rows_json,
			mapping_json=mapping_json,
			source_type=source_type,
		)
		if reference_access_validator:
			reference_access_validator(initial_preview)
	expected_state_token = active_state_token or (initial_preview or {}).get("active_state_token")
	expected_fingerprint = expected_import_fingerprint or (initial_preview or {}).get("import_fingerprint")
	save_point = f"aps_schedule_import_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(save_point)
	try:
		locked_customer = frappe.db.sql(
			"select name from `tabCustomer` where name = %s for update",
			customer,
		)
		if not locked_customer:
			frappe.throw(_("Customer {0} does not exist.").format(customer or "-"), frappe.ValidationError)
		# Re-read the source and active schedule while holding the stable Customer
		# lock.  Every import for the customer uses this same lock, including the
		# first import where no active schedule header exists yet.
		preview = preview_customer_delivery_schedule(
			customer=customer,
			company=company,
			version_no=version_no,
			schedule_scope=schedule_scope,
			import_strategy=import_strategy,
			duplicate_policy=duplicate_policy,
			file_url=file_url,
			rows_json=rows_json,
			mapping_json=mapping_json,
			source_type=source_type,
		)
		# Permission checks must run against the source re-read while the stable
		# Customer lock is held, immediately before any schedule rows are written.
		if reference_access_validator:
			reference_access_validator(preview)
		if expected_fingerprint and expected_fingerprint != preview.get("import_fingerprint"):
			frappe.throw(
				_(
					"The schedule source changed after preview. Refresh Preview and confirm the latest rows before importing."
				),
				frappe.ValidationError,
			)
		if preview.get("is_idempotent_replay"):
			result = {
				**(preview.get("existing_import") or {}),
				"summary": preview.get("summary") or {},
				"idempotent_replay": 1,
				"promotion": None,
			}
			frappe.db.release_savepoint(save_point)
			return result
		if expected_state_token and expected_state_token != preview.get("active_state_token"):
			frappe.throw(
				_(
					"The active customer schedule changed after preview. Refresh Preview and confirm the latest impact before importing."
				),
				frappe.ValidationError,
			)
		if not preview.get("can_import"):
			blocking_messages = [
				check.get("summary") or check.get("title")
				for check in preview.get("checks") or []
				if cint(check.get("blocking"))
			]
			frappe.throw(
				_("Schedule import checks failed. Resolve every blocking check before import:<br>{0}").format(
					"<br>".join(blocking_messages[:12])
				),
				frappe.ValidationError,
			)
		schedule_scope = preview.get("schedule_scope")
		import_strategy = preview.get("import_strategy")
		version_no = preview.get("version_no")
		result = _apply_customer_delivery_schedule_import(
			preview=preview,
			customer=customer,
			company=company,
			version_no=version_no,
			schedule_scope=schedule_scope,
			import_strategy=import_strategy,
			duplicate_policy=preview.get("duplicate_policy"),
			file_url=file_url,
			source_type=source_type,
		)
		if cint(rebuild):
			existing_work_order_policy = _normalize_existing_work_order_policy(existing_work_order_policy)
			result["promotion"] = {
				"company": company,
				"existing_work_order_policy": existing_work_order_policy,
				"demand_pool": rebuild_demand_pool(company=company),
				"net_requirement": rebuild_net_requirements(
					company=company,
					existing_work_order_policy=existing_work_order_policy,
				),
				"next_route": "aps-net-requirement-workbench",
			}
		else:
			result["promotion"] = None
		frappe.db.release_savepoint(save_point)
		return result
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point=save_point)
		replay = _get_schedule_import_replay(preview.get("import_fingerprint"))
		if replay:
			return {**replay, "summary": preview.get("summary") or {}, "idempotent_replay": 1, "promotion": None}
		raise
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def compare_schedule_against_active(
	customer: str,
	company: str,
	schedule_scope: str | None,
	import_strategy: str | None,
	rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	schedule_scope = _normalize_schedule_scope(schedule_scope)
	import_strategy = _normalize_schedule_import_strategy(import_strategy)
	previous_rows = _get_active_schedule_rows(customer=customer, company=company, schedule_scope=schedule_scope)
	plan = _build_schedule_import_plan(
		previous_rows=previous_rows,
		incoming_rows=rows,
		import_strategy=import_strategy,
	)
	return _attach_schedule_import_impacts(
		plan["diff_rows"],
		customer=customer,
		company=company,
		import_strategy=import_strategy,
	)


def _normalize_schedule_scope(value: str | None) -> str:
	scope = str(value or "").strip()
	return scope or "Default Scope"


def _normalize_schedule_version(value: str | None) -> str:
	return str(value or "").strip()


def _normalize_schedule_import_strategy(value: str | None) -> str:
	strategy = str(value or "").strip()
	if strategy == "Partial Item Update":
		strategy = "Partial Update"
	return strategy if strategy in SCHEDULE_IMPORT_STRATEGIES else "Replace Scope"


def _normalize_schedule_duplicate_policy(value: str | None) -> str:
	policy = str(value or "").strip()
	return policy if policy in SCHEDULE_DUPLICATE_POLICIES else "Block"


def _build_effective_schedule_rows(
	previous_rows: list[dict[str, Any]],
	incoming_rows: list[dict[str, Any]],
	import_strategy: str,
) -> list[dict[str, Any]]:
	return _build_schedule_import_plan(
		previous_rows=previous_rows,
		incoming_rows=incoming_rows,
		import_strategy=_normalize_schedule_import_strategy(import_strategy),
	)["effective_schedule_rows"]


def _prepare_schedule_rows_for_import(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	_prime_item_resolution_cache(row.get("item_code") for row in rows or [])
	prepared = []
	for row in rows or []:
		item_code = str(row.get("item_code") or "").strip()
		schedule_date = row.get("schedule_date")
		previous_schedule_date = row.get("previous_schedule_date")
		source_excel_rows = _schedule_source_row_numbers(row)
		prepared.append(
			{
				"sales_order": str(row.get("sales_order") or "").strip() or None,
				"item_code": (_resolve_item_name(item_code) or item_code) if item_code else "",
				"customer_part_no": row.get("customer_part_no"),
				"schedule_date": getdate(schedule_date) if schedule_date else None,
				"previous_schedule_date": getdate(previous_schedule_date) if previous_schedule_date else None,
				"qty": flt(row.get("qty")),
				"remark": row.get("remark"),
				"source_origin": row.get("source_origin") or "imported",
				"source_excel_row": source_excel_rows[0] if source_excel_rows else 0,
				"source_excel_rows": ", ".join(str(value) for value in source_excel_rows),
				"manual_override": cint(row.get("manual_override")),
				"manual_change_reason": row.get("manual_change_reason"),
				"production_strategy": row.get("production_strategy"),
				"demand_confidence": row.get("demand_confidence"),
				"cancellation_risk_percent": (
					flt(row.get("cancellation_risk_percent"))
					if row.get("cancellation_risk_percent") is not None
					else None
				),
				"prebuild_allowed": (
					cint(row.get("prebuild_allowed")) if row.get("prebuild_allowed") is not None else None
				),
				"max_prebuild_days": (
					cint(row.get("max_prebuild_days")) if row.get("max_prebuild_days") is not None else None
				),
			}
		)
	return prepared


def _build_schedule_source_snapshot_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Return the exact row shape serialized by Schedule Console on formal import."""
	return [
		{
			"sales_order": row.get("sales_order") or "",
			"item_code": row.get("item_code") or "",
			"customer_part_no": row.get("customer_part_no") or "",
			"schedule_date": row.get("schedule_date") or "",
			"previous_schedule_date": row.get("previous_schedule_date") or "",
			"qty": flt(row.get("import_qty") if row.get("import_qty") is not None else row.get("qty")),
			"production_strategy": row.get("production_strategy") or "Auto Balance",
			"demand_confidence": row.get("demand_confidence") or "Confirmed",
			"cancellation_risk_percent": flt(row.get("cancellation_risk_percent")),
			"prebuild_allowed": cint(row.get("prebuild_allowed")) if row.get("prebuild_allowed") is not None else 1,
			"max_prebuild_days": cint(row.get("max_prebuild_days")),
			"remark": row.get("remark") or "",
			"source_origin": row.get("source_origin") or "imported",
			"source_excel_row": row.get("source_excel_row") or "",
			"source_excel_rows": row.get("source_excel_rows") or "",
			"manual_override": 1 if cint(row.get("manual_override")) else 0,
			"manual_change_reason": row.get("manual_change_reason") or "",
		}
		for row in rows or []
	]


def _schedule_source_row_numbers(row: dict[str, Any]) -> list[int]:
	values = row.get("source_excel_rows") or row.get("source_excel_row") or []
	if not isinstance(values, (list, tuple, set)):
		values = str(values).replace(";", ",").split(",")
	return sorted({cint(value) for value in values if cint(value) > 0})


def _validate_schedule_import_rows(
	rows: list[dict[str, Any]],
	customer: str | None = None,
	company: str | None = None,
) -> list[dict[str, Any]]:
	issues = []
	if not rows:
		return [{"excel_rows": [], "message": _("The import contains no schedule rows.")}]
	item_doctype_exists = frappe.db.exists("DocType", "Item")
	item_codes = sorted({row.get("item_code") for row in rows if row.get("item_code")})
	existing_item_codes = (
		_get_existing_names_in_chunks("Item", item_codes) if item_doctype_exists else set()
	)
	for index, row in enumerate(rows, start=1):
		excel_rows = _schedule_source_row_numbers(row) or [index]
		if not row.get("item_code"):
			issues.append(
				{
					"excel_rows": excel_rows,
					"message": _("Item Code is required.", context="Injection APS"),
				}
			)
		elif item_doctype_exists and row.get("item_code") not in existing_item_codes:
			issues.append(
				{
					"excel_rows": excel_rows,
					"message": _("Item {0} does not exist.", context="Injection APS").format(
						row.get("item_code")
					),
				}
			)
		if not row.get("schedule_date"):
			issues.append(
				{
					"excel_rows": excel_rows,
					"message": _("Schedule Date is required.", context="Injection APS"),
				}
			)
		if flt(row.get("qty")) < 0:
			issues.append({"excel_rows": excel_rows, "message": _("Quantity cannot be negative; use zero to cancel.")})
		if row.get("production_strategy") not in (None, "", "Auto Balance", "Force Prebuild", "Force JIT"):
			issues.append({"excel_rows": excel_rows, "message": _("Production Strategy is invalid.")})
		if row.get("demand_confidence") not in (None, "", "Confirmed", "Forecast"):
			issues.append({"excel_rows": excel_rows, "message": _("Demand Confidence is invalid.")})
		if not 0 <= flt(row.get("cancellation_risk_percent")) <= 100:
			issues.append({"excel_rows": excel_rows, "message": _("Cancellation Risk Percent must be between 0 and 100.")})
		if cint(row.get("max_prebuild_days")) < 0:
			issues.append({"excel_rows": excel_rows, "message": _("Max Prebuild Days cannot be negative.")})
	issues.extend(_validate_schedule_sales_order_ownership(rows, customer=customer, company=company))
	return issues


def _iter_query_chunks(values, chunk_size: int = SCHEDULE_VALIDATION_QUERY_CHUNK_SIZE):
	values = list(values or [])
	for start in range(0, len(values), max(cint(chunk_size), 1)):
		yield values[start : start + max(cint(chunk_size), 1)]


def _get_existing_names_in_chunks(doctype: str, names) -> set[str]:
	existing = set()
	for chunk in _iter_query_chunks(sorted({name for name in names or [] if name})):
		existing.update(
			frappe.get_all(
				doctype,
				filters={"name": ("in", chunk)},
				pluck="name",
			)
		)
	return existing


def _validate_schedule_sales_order_ownership(
	rows: list[dict[str, Any]],
	*,
	customer: str | None,
	company: str | None,
) -> list[dict[str, Any]]:
	issues = []
	rows_by_order = defaultdict(list)
	for index, row in enumerate(rows or [], start=1):
		sales_order = str(row.get("sales_order") or "").strip()
		if sales_order:
			rows_by_order[sales_order].append((index, row))
	if not rows_by_order:
		return issues
	if not frappe.db.exists("DocType", "Sales Order"):
		return [
			{
				"excel_rows": sorted(
					{source_row for entries in rows_by_order.values() for index, row in entries for source_row in (_schedule_source_row_numbers(row) or [index])}
				),
				"message": _("Sales Order validation is unavailable; import with Sales Order references is blocked."),
			}
		]
	order_names = sorted(rows_by_order)
	orders_by_name = {}
	items_by_order = defaultdict(set)
	for chunk in _iter_query_chunks(order_names):
		for order in frappe.get_all(
			"Sales Order",
			filters={"name": ("in", chunk)},
			fields=["name", "customer", "company", "docstatus"],
		):
			orders_by_name[order.get("name")] = order
		for item in frappe.get_all(
			"Sales Order Item",
			filters={"parent": ("in", chunk), "parenttype": "Sales Order"},
			fields=["parent", "item_code"],
		):
			if item.get("parent") and item.get("item_code"):
				items_by_order[item.get("parent")].add(item.get("item_code"))
	for sales_order, entries in rows_by_order.items():
		excel_rows = sorted(
			{source_row for index, row in entries for source_row in (_schedule_source_row_numbers(row) or [index])}
		)
		order = orders_by_name.get(sales_order)
		if not order:
			issues.append({"excel_rows": excel_rows, "message": _("Sales Order {0} does not exist.").format(sales_order)})
			continue
		if customer and order.get("customer") != customer:
			issues.append(
				{"excel_rows": excel_rows, "message": _("Sales Order {0} belongs to a different customer.").format(sales_order)}
			)
		if company and order.get("company") != company:
			issues.append(
				{"excel_rows": excel_rows, "message": _("Sales Order {0} belongs to a different company.").format(sales_order)}
			)
		if cint(order.get("docstatus")) != 1:
			issues.append(
				{"excel_rows": excel_rows, "message": _("Sales Order {0} must be submitted before it can be scheduled.").format(sales_order)}
			)
		order_items = items_by_order.get(sales_order) or set()
		for index, row in entries:
			if row.get("item_code") not in order_items:
				issues.append(
					{
						"excel_rows": _schedule_source_row_numbers(row) or [index],
						"message": _("Item {0} is not present on Sales Order {1}.").format(
							row.get("item_code") or "-", sales_order
						),
					}
				)
	return issues


def _resolve_schedule_row_duplicates(
	rows: list[dict[str, Any]],
	duplicate_policy: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	grouped = defaultdict(list)
	for row in rows or []:
		grouped[_schedule_row_key(row)].append(row)
	duplicate_groups = []
	resolved_rows = []
	for key, group in grouped.items():
		if len(group) == 1:
			resolved_rows.append(dict(group[0]))
			continue
		excel_rows = sorted({value for row in group for value in _schedule_source_row_numbers(row)})
		duplicate_groups.append(
			{
				"sales_order": key[0],
				"item_code": key[1],
				"schedule_date": key[2] or None,
				"customer_part_no": key[3],
				"excel_rows": excel_rows,
				"quantities": [flt(row.get("qty")) for row in group],
				"total_qty": sum(flt(row.get("qty")) for row in group),
			}
		)
		if duplicate_policy == "Sum":
			combined = dict(group[0])
			combined["qty"] = sum(flt(row.get("qty")) for row in group)
			combined["source_excel_row"] = excel_rows[0] if excel_rows else 0
			combined["source_excel_rows"] = ", ".join(str(value) for value in excel_rows)
			combined["source_origin"] = "summed_duplicate"
			resolved_rows.append(combined)
		else:
			resolved_rows.extend(dict(row) for row in group)
	return resolved_rows, duplicate_groups


def _find_partial_update_ambiguities(
	previous_rows: list[dict[str, Any]],
	incoming_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	previous_exact = {_schedule_row_key(row): row for row in previous_rows}
	previous_by_identity = defaultdict(list)
	incoming_by_identity = defaultdict(list)
	for row in previous_rows:
		previous_by_identity[_schedule_identity_key(row)].append(row)
	for row in incoming_rows:
		incoming_by_identity[_schedule_identity_key(row)].append(row)
	ambiguities = []
	invalid_explicit_rows = set()
	explicit_sources = defaultdict(list)
	for row in incoming_rows:
		previous_date = row.get("previous_schedule_date")
		if not previous_date:
			continue
		previous_key = _schedule_row_key(dict(row, schedule_date=previous_date))
		if previous_key not in previous_exact:
			invalid_explicit_rows.add(id(row))
			previous_group = previous_by_identity.get(_schedule_identity_key(row)) or []
			ambiguities.append(
				{
					"item_code": row.get("item_code"),
					"sales_order": row.get("sales_order"),
					"excel_rows": _schedule_source_row_numbers(row),
					"previous_dates": sorted(str(item.get("schedule_date")) for item in previous_group),
					"new_date": str(row.get("schedule_date") or ""),
				}
			)
			continue
		explicit_sources[previous_key].append(row)
	for previous_key, source_rows in explicit_sources.items():
		if len(source_rows) <= 1:
			continue
		invalid_explicit_rows.update(id(row) for row in source_rows)
		row = source_rows[0]
		ambiguities.append(
			{
				"item_code": row.get("item_code"),
				"sales_order": row.get("sales_order"),
				"excel_rows": sorted({value for item in source_rows for value in _schedule_source_row_numbers(item)}),
				"previous_dates": [previous_key[2]],
				"new_date": ", ".join(sorted(str(item.get("schedule_date") or "") for item in source_rows)),
			}
		)
	consumed_explicit_sources = set(explicit_sources)
	for previous_key, source_rows in explicit_sources.items():
		for row in source_rows:
			if id(row) in invalid_explicit_rows:
				continue
			destination_key = _schedule_row_key(row)
			if (
				destination_key != previous_key
				and destination_key in previous_exact
				and destination_key not in consumed_explicit_sources
			):
				invalid_explicit_rows.add(id(row))
				ambiguities.append(
					{
						"item_code": row.get("item_code"),
						"sales_order": row.get("sales_order"),
						"excel_rows": _schedule_source_row_numbers(row),
						"previous_dates": [previous_key[2], destination_key[2]],
						"new_date": str(row.get("schedule_date") or ""),
					}
				)
	for identity, incoming_group in incoming_by_identity.items():
		previous_group = previous_by_identity.get(identity) or []
		for row in incoming_group:
			if id(row) in invalid_explicit_rows:
				continue
			if row.get("previous_schedule_date"):
				continue
			if _schedule_row_key(row) in previous_exact or not previous_group:
				continue
			if len(previous_group) == 1 and len(incoming_group) == 1:
				continue
			ambiguities.append(
				{
					"item_code": row.get("item_code"),
					"sales_order": row.get("sales_order"),
					"excel_rows": _schedule_source_row_numbers(row),
					"previous_dates": sorted(str(item.get("schedule_date")) for item in previous_group),
					"new_date": str(row.get("schedule_date") or ""),
				}
			)
	return ambiguities


def _build_schedule_import_fingerprint(
	*,
	customer: str,
	company: str,
	version_no: str | None,
	schedule_scope: str,
	import_strategy: str,
	source_type: str,
	rows: list[dict[str, Any]],
) -> str:
	semantic_rows = []
	for row in rows or []:
		semantic_rows.append(
			{
				"sales_order": row.get("sales_order") or "",
				"item_code": row.get("item_code") or "",
				"customer_part_no": row.get("customer_part_no") or "",
				"schedule_date": str(row.get("schedule_date") or ""),
				"previous_schedule_date": str(row.get("previous_schedule_date") or ""),
				"qty": round(flt(row.get("qty")), 6),
				"remark": row.get("remark") or "",
				"source_origin": row.get("source_origin") or "imported",
				"manual_override": cint(row.get("manual_override")),
				"manual_change_reason": row.get("manual_change_reason") or "",
				"production_strategy": row.get("production_strategy") or "Auto Balance",
				"demand_confidence": row.get("demand_confidence") or "Confirmed",
				"cancellation_risk_percent": round(flt(row.get("cancellation_risk_percent")), 6),
				"prebuild_allowed": (
					cint(row.get("prebuild_allowed")) if row.get("prebuild_allowed") is not None else 1
				),
				"max_prebuild_days": cint(row.get("max_prebuild_days")),
			}
		)
	semantic_rows.sort(
		key=lambda row: (
			row["sales_order"],
			row["item_code"],
			row["schedule_date"],
			row["customer_part_no"],
			row["qty"],
			row["production_strategy"],
			row["demand_confidence"],
			row["cancellation_risk_percent"],
			row["prebuild_allowed"],
			row["max_prebuild_days"],
		)
	)
	payload = {
		"customer": customer or "",
		"company": company or "",
		# Append is an additive command.  Re-uploading the same semantic rows under
		# a renamed customer file/version must remain a replay, otherwise the same
		# demand is added twice.  Replacement versions retain their version boundary.
		"version_no": "" if import_strategy == "Append" else _normalize_schedule_version(version_no),
		"schedule_scope": schedule_scope or "",
		"import_strategy": import_strategy,
		"source_type": source_type or "Customer Delivery Schedule",
		"rows": semantic_rows,
	}
	return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _get_schedule_import_replay(import_fingerprint: str | None) -> dict[str, Any] | None:
	if not import_fingerprint or not frappe.db.exists("DocType", "APS Schedule Import Batch"):
		return None
	meta = frappe.get_meta("APS Schedule Import Batch")
	if not meta.has_field("import_fingerprint"):
		return None
	batch = frappe.db.get_value(
		"APS Schedule Import Batch",
		{"import_fingerprint": import_fingerprint, "status": "Imported"},
		["name", "schedule_reference"],
		as_dict=True,
	)
	if not batch:
		return None
	return {"import_batch": batch.name, "schedule": batch.schedule_reference, "import_fingerprint": import_fingerprint}


def _build_schedule_import_plan(
	*,
	previous_rows: list[dict[str, Any]],
	incoming_rows: list[dict[str, Any]],
	import_strategy: str,
) -> dict[str, list[dict[str, Any]]]:
	previous_rows = _aggregate_schedule_rows(previous_rows)
	incoming_rows = _prepare_schedule_rows_for_import(incoming_rows)
	if import_strategy == "Append":
		diff_rows = []
		previous_exact = {_schedule_row_key(row): row for row in previous_rows}
		for incoming in incoming_rows:
			previous = previous_exact.get(_schedule_row_key(incoming)) or {}
			row = _build_schedule_diff_row(previous, incoming)
			row["import_qty"] = flt(incoming.get("qty"))
			row["qty"] = flt(previous.get("qty")) + row["import_qty"]
			row["new_qty"] = row["qty"]
			row["delta_qty"] = row["import_qty"]
			row["change_type"] = "Appended"
			row["source_origin"] = incoming.get("source_origin") or "appended"
			diff_rows.append(row)
		return {
			"effective_schedule_rows": [dict(row, source_origin=row.get("source_origin") or "appended") for row in incoming_rows],
			"diff_rows": _sort_schedule_diff_rows(diff_rows),
		}

	if import_strategy == "Partial Update":
		previous_exact = {_schedule_row_key(row): row for row in previous_rows}
		incoming_by_identity = defaultdict(list)
		previous_by_identity = defaultdict(list)
		for row in incoming_rows:
			incoming_by_identity[_schedule_identity_key(row)].append(row)
		for row in previous_rows:
			previous_by_identity[_schedule_identity_key(row)].append(row)
		resolved_incoming = []
		consumed_previous_keys = set()
		for incoming in incoming_rows:
			incoming_key = _schedule_row_key(incoming)
			identity = _schedule_identity_key(incoming)
			previous_date = incoming.get("previous_schedule_date")
			candidate_key = None
			# An explicit old date is the user's transfer instruction.  Resolve all
			# such moves against the immutable previous snapshot so swaps/crossing
			# moves cannot overwrite each other while the plan is being assembled.
			if previous_date:
				explicit_key = _schedule_row_key(dict(incoming, schedule_date=previous_date))
				if explicit_key in previous_exact and explicit_key not in consumed_previous_keys:
					candidate_key = explicit_key
			if candidate_key is None and incoming_key in previous_exact and incoming_key not in consumed_previous_keys:
				candidate_key = incoming_key
			if candidate_key is None and len(previous_by_identity.get(identity) or []) == 1 and len(incoming_by_identity.get(identity) or []) == 1:
				fallback_key = _schedule_row_key(previous_by_identity[identity][0])
				if fallback_key not in consumed_previous_keys:
					candidate_key = fallback_key
			previous = previous_exact.get(candidate_key) if candidate_key else None
			if candidate_key:
				consumed_previous_keys.add(candidate_key)
			merged = dict(previous or {})
			merged.update(
				{
					fieldname: value
					for fieldname, value in incoming.items()
					if fieldname not in SCHEDULE_POLICY_FIELDS or value is not None
				}
			)
			merged["allocated_qty"] = flt((previous or {}).get("allocated_qty"))
			merged["produced_qty"] = flt((previous or {}).get("produced_qty"))
			merged["delivered_qty"] = flt((previous or {}).get("delivered_qty"))
			merged["balance_qty"] = max(flt(merged.get("qty")) - flt(merged.get("delivered_qty")), 0)
			resolved_incoming.append((incoming_key, merged))
		effective_by_key = {
			key: dict(row, source_origin=row.get("source_origin") or "retained_existing")
			for key, row in previous_exact.items()
			if key not in consumed_previous_keys
		}
		for incoming_key, merged in resolved_incoming:
			effective_by_key[incoming_key] = merged
		effective_rows = list(effective_by_key.values())
	else:
		effective_rows = incoming_rows

	return {
		"effective_schedule_rows": effective_rows,
		"diff_rows": _build_replacement_schedule_diff(previous_rows, effective_rows),
	}


def _build_replacement_schedule_diff(
	previous_rows: list[dict[str, Any]],
	current_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	previous_exact = {_schedule_row_key(row): row for row in previous_rows}
	current_exact = {_schedule_row_key(row): row for row in current_rows}
	diff_rows = []
	matched_previous = set()
	matched_current = set()
	# Preserve explicit line-to-line moves before exact-date or FIFO pairing.
	# This is essential for swaps/crossing moves where a new date is also the
	# date of another old line.
	for current_key, current in current_exact.items():
		previous_date = current.get("previous_schedule_date")
		if not previous_date:
			continue
		previous_key = _schedule_row_key(dict(current, schedule_date=previous_date))
		if previous_key not in previous_exact or previous_key in matched_previous:
			continue
		diff_rows.append(_build_schedule_diff_row(previous_exact[previous_key], current))
		matched_previous.add(previous_key)
		matched_current.add(current_key)
	for key in sorted(set(previous_exact) & set(current_exact)):
		if key in matched_previous or key in matched_current:
			continue
		diff_rows.append(_build_schedule_diff_row(previous_exact[key], current_exact[key]))
		matched_previous.add(key)
		matched_current.add(key)

	previous_grouped = defaultdict(list)
	current_grouped = defaultdict(list)
	for key, row in previous_exact.items():
		if key not in matched_previous:
			previous_grouped[_schedule_identity_key(row)].append(row)
	for key, row in current_exact.items():
		if key not in matched_current:
			current_grouped[_schedule_identity_key(row)].append(row)
	for identity in sorted(set(previous_grouped) | set(current_grouped)):
		previous_group = sorted(previous_grouped.get(identity) or [], key=lambda row: str(row.get("schedule_date") or ""))
		current_group = sorted(current_grouped.get(identity) or [], key=lambda row: str(row.get("schedule_date") or ""))
		pairs = min(len(previous_group), len(current_group))
		for index in range(pairs):
			diff_rows.append(_build_schedule_diff_row(previous_group[index], current_group[index]))
		for current in current_group[pairs:]:
			diff_rows.append(_build_schedule_diff_row({}, current))
		for previous in previous_group[pairs:]:
			cancelled = dict(previous, qty=0, source_origin="cancelled_by_replace")
			diff_rows.append(_build_schedule_diff_row(previous, cancelled))
	return _sort_schedule_diff_rows(diff_rows)


def _build_schedule_diff_row(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
	row = dict(current)
	previous_qty = flt(previous.get("qty"))
	current_qty = flt(current.get("qty"))
	row["previous_qty"] = previous_qty
	row["new_qty"] = current_qty
	row["qty"] = current_qty
	row["delta_qty"] = current_qty - previous_qty
	row["previous_schedule_date"] = previous.get("schedule_date")
	row["new_schedule_date"] = current.get("schedule_date")
	row["allocated_qty"] = flt(previous.get("allocated_qty"))
	row["produced_qty"] = flt(previous.get("produced_qty"))
	row["delivered_qty"] = flt(previous.get("delivered_qty"))
	row["balance_qty"] = max(current_qty - row["delivered_qty"], 0)
	row["change_type"] = _detect_change_type(previous, current)
	return row


def _sort_schedule_diff_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	rows = sorted(
		rows,
		key=lambda row: (
			str(row.get("schedule_date") or row.get("previous_schedule_date") or ""),
			row.get("sales_order") or "",
			row.get("item_code") or "",
			row.get("customer_part_no") or "",
		),
	)
	for index, row in enumerate(rows, start=1):
		row["line_idx"] = index
	return rows


def _attach_schedule_import_impacts(
	rows: list[dict[str, Any]],
	*,
	customer: str,
	company: str,
	import_strategy: str,
) -> list[dict[str, Any]]:
	frozen_qty = _get_schedule_frozen_qty(customer=customer, company=company)
	for row in rows or []:
		previous_date = row.get("previous_schedule_date") or row.get("schedule_date")
		row["frozen_qty"] = flt(frozen_qty.get((row.get("item_code") or "", str(previous_date or ""))))
		destructive = import_strategy != "Append" and row.get("change_type") in {
			"Cancelled", "Reduced", "Advanced", "Delayed"
		}
		row["affects_produced"] = cint(destructive and flt(row.get("produced_qty")) > 0)
		row["affects_delivered"] = cint(destructive and flt(row.get("delivered_qty")) > 0)
		row["affects_frozen"] = cint(destructive and flt(row.get("frozen_qty")) > 0)
		row["has_execution_impact"] = cint(
			row["affects_produced"] or row["affects_delivered"] or row["affects_frozen"]
		)
		impact_parts = []
		for label, fieldname in (
			(_("Produced", context="Injection APS"), "produced_qty"),
			(_("Delivered", context="Injection APS"), "delivered_qty"),
			(_("Frozen", context="Injection APS"), "frozen_qty"),
		):
			if flt(row.get(fieldname)) > 0:
				impact_parts.append(f"{label} {flt(row.get(fieldname)):g}")
		row["execution_impact"] = (
			" / ".join(impact_parts)
			if destructive and impact_parts
			else _("None", context="Injection APS")
		)
	return rows


def _get_schedule_frozen_qty(customer: str, company: str) -> dict[tuple[str, str], float]:
	qty_by_key = defaultdict(float)
	if not frappe.db.exists("DocType", "APS Planning Run") or not frappe.db.exists("DocType", "APS Schedule Result"):
		return qty_by_key
	runs = frappe.get_all(
		"APS Planning Run",
		filters={"company": company},
		fields=["name"],
		order_by="modified desc",
		limit=1,
	)
	if not runs:
		return qty_by_key
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": runs[0].name, "customer": customer},
		fields=["name", "item_code", "requested_date", "is_locked", "machine_scheduled_qty"],
	)
	result_by_name = {row.name: row for row in results}
	for row in results:
		if cint(row.is_locked):
			qty_by_key[(row.item_code or "", str(row.requested_date or ""))] += flt(row.machine_scheduled_qty)
	if result_by_name and frappe.db.exists("DocType", "APS Schedule Segment"):
		segments = frappe.get_all(
			"APS Schedule Segment",
			filters={"parent": ("in", list(result_by_name)), "is_locked": 1},
			fields=["parent", "planned_qty"],
		)
		for segment in segments:
			result = result_by_name.get(segment.parent)
			if result and not cint(result.is_locked):
				qty_by_key[(result.item_code or "", str(result.requested_date or ""))] += flt(segment.planned_qty)
	return qty_by_key


def _build_blocked_schedule_preview_rows(
	rows: list[dict[str, Any]],
	duplicate_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	duplicate_keys = {
		(group.get("sales_order") or "", group.get("item_code") or "", group.get("schedule_date") or "", group.get("customer_part_no") or "")
		for group in duplicate_groups
	}
	preview_rows = []
	for index, source in enumerate(rows, start=1):
		row = dict(source)
		row["line_idx"] = index
		row["previous_qty"] = None
		row["new_qty"] = flt(row.get("qty"))
		row["delta_qty"] = None
		row["new_schedule_date"] = row.get("schedule_date")
		row["change_type"] = (
			"Duplicate Blocked"
			if _schedule_row_key(row) in duplicate_keys
			else "Validation Blocked"
		)
		row["execution_impact"] = _("Not evaluated", context="Injection APS")
		preview_rows.append(row)
	return preview_rows


def _build_schedule_import_checks(
	*,
	import_strategy: str,
	duplicate_policy: str,
	duplicate_groups: list[dict[str, Any]],
	row_issues: list[dict[str, Any]],
	append_zero_rows: list[dict[str, Any]],
	partial_ambiguities: list[dict[str, Any]],
	replay: dict[str, Any] | None,
	delivery_lower_bound_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	checks = []
	if row_issues:
		details = [
			_("Excel rows {0}: {1}", context="Injection APS").format(", ".join(str(value) for value in issue.get("excel_rows") or []) or "-", issue.get("message"))
			for issue in row_issues
		]
		checks.append(
			_schedule_import_check(
				"failed",
				_("Row validation", context="Injection APS"),
				_("Invalid schedule rows were found.", context="Injection APS"),
				details,
				1,
			)
		)
	else:
		checks.append(
			_schedule_import_check(
				"passed",
				_("Row validation", context="Injection APS"),
				_("Every row has a valid item, date, and non-negative quantity."),
			)
		)
	if duplicate_groups and duplicate_policy == "Block":
		details = [
			_("Excel rows {0}: {1} / {2} / {3}, quantities {4}.").format(
				", ".join(str(value) for value in group.get("excel_rows") or []),
				group.get("sales_order") or _("No Sales Order", context="Injection APS"),
				group.get("item_code"),
				group.get("schedule_date"),
				" + ".join(f"{flt(value):g}" for value in group.get("quantities") or []),
			)
			for group in duplicate_groups
		]
		checks.append(_schedule_import_check("failed", _("Duplicate identities", context="Injection APS"), _("Duplicates are blocked by default.", context="Injection APS"), details, 1))
	elif duplicate_groups:
		details = [
			_("Excel rows {0} are explicitly summed to {1}.").format(
				", ".join(str(value) for value in group.get("excel_rows") or []), f"{flt(group.get('total_qty')):g}"
			)
			for group in duplicate_groups
		]
		checks.append(_schedule_import_check("warning", _("Duplicate identities", context="Injection APS"), _("Explicit Sum policy is active.", context="Injection APS"), details))
	else:
		checks.append(_schedule_import_check("passed", _("Duplicate identities", context="Injection APS"), _("No duplicate business keys were found.", context="Injection APS")))
	if append_zero_rows:
		details = [
			_("Excel rows {0} contain zero quantity.", context="Injection APS").format(", ".join(str(value) for value in _schedule_source_row_numbers(row)) or "-")
			for row in append_zero_rows
		]
		checks.append(_schedule_import_check("failed", _("Zero quantity semantics", context="Injection APS"), _("Append creates independent demand and cannot cancel an existing row. Use Partial Update or Replace Scope."), details, 1))
	else:
		zero_quantity_summary = (
			_("Zero quantities are preserved as explicit cancellations.", context="Injection APS")
			if import_strategy != "Append"
			else _("Append rows contain positive independent demand.", context="Injection APS")
		)
		checks.append(
			_schedule_import_check(
				"passed",
				_("Zero quantity semantics", context="Injection APS"),
				zero_quantity_summary,
			)
		)
	if delivery_lower_bound_rows:
		details = [
			_("Excel rows {0}: {1} / {2} has new quantity {3}, below delivered quantity {4}.").format(
				", ".join(str(value) for value in _schedule_source_row_numbers(row)) or "-",
				row.get("item_code") or "-",
				row.get("schedule_date") or row.get("previous_schedule_date") or "-",
				f"{flt(row.get('new_qty')):g}",
				f"{flt(row.get('delivered_qty')):g}",
			)
			for row in delivery_lower_bound_rows
		]
		checks.append(
			_schedule_import_check(
				"failed",
				_("Delivered quantity lower bound", context="Injection APS"),
				_("A schedule cannot be reduced below quantity already delivered. Cancel or return the Delivery Note first."),
				details,
				1,
			)
		)
	else:
		checks.append(
			_schedule_import_check(
				"passed",
				_("Delivered quantity lower bound", context="Injection APS"),
				_("Every new schedule quantity covers its already-delivered quantity."),
			)
		)
	if partial_ambiguities:
		details = [
			_("Excel rows {0}: {1} has multiple possible previous dates ({2}).").format(
				", ".join(str(value) for value in row.get("excel_rows") or []) or "-",
				row.get("item_code"),
				", ".join(row.get("previous_dates") or []),
			)
			for row in partial_ambiguities
		]
		checks.append(_schedule_import_check("failed", _("Partial update matching", context="Injection APS"), _("Date changes must identify one previous delivery row."), details, 1))
	elif import_strategy == "Partial Update":
		checks.append(_schedule_import_check("passed", _("Partial update matching", context="Injection APS"), _("Only file identities will change; omitted rows remain active.")))
	if import_strategy == "Replace Scope":
		checks.append(_schedule_import_check("passed", _("Replace scope", context="Injection APS"), _("Omitted active rows will be recorded as cancellations.")))
	elif import_strategy == "Append":
		checks.append(_schedule_import_check("passed", _("Append totals", context="Injection APS"), _("Preview quantities show active total plus the independent imported quantity.")))
	if replay:
		checks.append(_schedule_import_check("notice", _("Idempotency", context="Injection APS"), _("This semantic file was already imported; no demand will be added again."), [replay.get("import_batch")]))
	else:
		checks.append(_schedule_import_check("passed", _("Idempotency", context="Injection APS"), _("A semantic fingerprint will prevent duplicate application.", context="Injection APS")))
	checks.append(_schedule_import_check("passed", _("Atomic write", context="Injection APS"), _("Batch, schedule, deltas, and optional rebuild run in one transaction.")))
	return checks


def _schedule_import_check(
	status: str,
	title: str,
	summary: str,
	details: list[str] | None = None,
	blocking: int = 0,
) -> dict[str, Any]:
	return {
		"status": status,
		"title": title,
		"summary": summary,
		"details": details or [],
		"blocking": cint(blocking),
	}


def _apply_customer_delivery_schedule_import(
	*,
	preview: dict[str, Any],
	customer: str,
	company: str,
	version_no: str,
	schedule_scope: str,
	import_strategy: str,
	duplicate_policy: str,
	file_url: str | None,
	source_type: str,
) -> dict[str, Any]:
	parse_context = preview.get("parse_context") or {}
	previous_schedule_items = (
		_get_active_schedule_item_rows(customer=customer, company=company, schedule_scope=schedule_scope)
		if import_strategy in {"Replace Scope", "Partial Update"}
		else []
	)
	import_batch = frappe.get_doc(
		{
			"doctype": "APS Schedule Import Batch",
			"customer": customer,
			"company": company,
			"schedule_scope": schedule_scope,
			"version_no": version_no,
			"import_strategy": import_strategy,
			"duplicate_policy": duplicate_policy,
			"import_fingerprint": preview.get("import_fingerprint"),
			"status": "Imported",
			"imported_rows": cint(preview.get("source_row_count")),
			"effective_rows": cint(preview.get("effective_row_count")),
			"previous_total_qty": flt(preview.get("previous_total_qty")),
			"post_import_total_qty": flt(preview.get("post_import_total_qty")),
			"change_summary": json.dumps(preview.get("summary") or {}, ensure_ascii=True, sort_keys=True),
			"duplicate_summary": json.dumps(preview.get("duplicate_groups") or [], ensure_ascii=True, sort_keys=True, default=str),
			"source_type": source_type,
			"uploaded_file": file_url,
			"parser_mode": parse_context.get("parser_mode"),
			"sheet_name": parse_context.get("sheet_name"),
			"mapping_json": _serialize_diagnostic_json(parse_context.get("mapping")),
		}
	).insert(ignore_permissions=True)

	if import_strategy in {"Replace Scope", "Partial Update"}:
		for name in frappe.get_all(
			"Customer Delivery Schedule",
			filters={
				"customer": customer,
				"company": company,
				"schedule_scope": schedule_scope,
				"status": "Active",
			},
			pluck="name",
		):
			frappe.db.set_value("Customer Delivery Schedule", name, "status", "Superseded")

	diff_by_key = {_schedule_row_key(row): row for row in preview.get("rows") or []}
	items = []
	for source in preview.get("effective_schedule_rows") or []:
		row = dict(source)
		diff = diff_by_key.get(_schedule_row_key(row)) or {}
		qty = flt(row.get("qty"))
		delivered_qty = flt(row.get("delivered_qty"))
		items.append(
			{
				"sales_order": row.get("sales_order"),
				"item_code": row.get("item_code"),
				"customer_part_no": row.get("customer_part_no"),
				"schedule_date": row.get("schedule_date"),
				"qty": qty,
				"allocated_qty": flt(row.get("allocated_qty")),
				"produced_qty": flt(row.get("produced_qty")),
				"delivered_qty": delivered_qty,
				"balance_qty": max(qty - delivered_qty, 0),
				"change_type": diff.get("change_type") or ("Appended" if import_strategy == "Append" else "Unchanged"),
				"status": "Cancelled" if qty <= 0 else ("Open" if qty > delivered_qty else "Covered"),
				"remark": row.get("remark"),
				"source_origin": row.get("source_origin") or ("appended" if import_strategy == "Append" else "imported"),
				"source_excel_row": cint(row.get("source_excel_row")),
				"source_excel_rows": row.get("source_excel_rows"),
				"manual_override": cint(row.get("manual_override")),
				"manual_change_reason": row.get("manual_change_reason"),
				"production_strategy": row.get("production_strategy") or "Auto Balance",
				"demand_confidence": row.get("demand_confidence") or "Confirmed",
				"cancellation_risk_percent": flt(row.get("cancellation_risk_percent")),
				"prebuild_allowed": (
					cint(row.get("prebuild_allowed")) if row.get("prebuild_allowed") is not None else 1
				),
				"max_prebuild_days": cint(row.get("max_prebuild_days")),
			}
		)

	schedule = frappe.get_doc(
		{
			"doctype": "Customer Delivery Schedule",
			"customer": customer,
			"company": company,
			"schedule_scope": schedule_scope,
			"version_no": version_no,
			"import_strategy": import_strategy,
			"import_batch": import_batch.name,
			"source_type": source_type,
			"status": "Active",
			"schedule_total_qty": sum(flt(row.get("qty")) for row in items),
			"change_summary": json.dumps(preview.get("summary") or {}, ensure_ascii=True, sort_keys=True),
			"items": items,
		}
	)
	schedule.flags.aps_schedule_import_transition = True
	schedule.insert(ignore_permissions=True)
	if previous_schedule_items:
		_remap_schedule_execution_allocations(
			previous_item_rows=previous_schedule_items,
			new_item_rows=[row.as_dict() for row in schedule.items],
			diff_rows=preview.get("rows") or [],
		)
	frappe.db.set_value("APS Schedule Import Batch", import_batch.name, "schedule_reference", schedule.name)
	_record_schedule_deltas(
		import_batch=import_batch.name,
		schedule_name=schedule.name,
		customer=customer,
		company=company,
		schedule_scope=schedule_scope,
		diff_rows=preview.get("rows") or [],
	)
	return {
		"import_batch": import_batch.name,
		"schedule": schedule.name,
		"summary": preview.get("summary") or {},
		"import_fingerprint": preview.get("import_fingerprint"),
		"idempotent_replay": 0,
	}


def _get_active_schedule_item_rows(customer: str, company: str, schedule_scope: str | None) -> list[dict[str, Any]]:
	return frappe.db.sql(
		"""
			select
				i.name,
				i.parent,
				i.sales_order,
				i.item_code,
				i.customer_part_no,
				i.schedule_date
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.customer = %s
				and s.company = %s
				and ifnull(s.schedule_scope, '') = ifnull(%s, '')
				and s.status = 'Active'
			order by s.creation asc, i.idx asc
		""",
		(customer, company, schedule_scope),
		as_dict=True,
	)


def _build_schedule_item_remap(
	previous_item_rows: list[dict[str, Any]],
	new_item_rows: list[dict[str, Any]],
	diff_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
	"""Map superseded child rows to their active replacements by explicit business identity."""
	new_by_key = {_schedule_row_key(row): row for row in new_item_rows or [] if row.get("name")}
	transition_by_old_key = {}
	for row in diff_rows or []:
		new_target = new_by_key.get(_schedule_row_key(row))
		previous_date = row.get("previous_schedule_date")
		if not new_target or not previous_date:
			continue
		old_key = _schedule_row_key({**row, "schedule_date": previous_date})
		transition_by_old_key[old_key] = new_target
	remap = {}
	for old_row in previous_item_rows or []:
		old_name = old_row.get("name")
		old_key = _schedule_row_key(old_row)
		# A declared date move must win over an exact-date match.  Otherwise a
		# two-line date swap silently leaves both ledgers attached to the wrong
		# customer demand line.
		target = transition_by_old_key.get(old_key) or new_by_key.get(old_key)
		# Partial Update keeps a zero-quantity audit row in the new Active header,
		# but that cancelled child is not a valid execution or delivery target.
		# Treat it exactly like an omitted row so fully returned delivery lineage can
		# settle and explicit DNI links are cleared instead of pointing at zero demand.
		if target and (
			target.get("status") == "Cancelled"
			or (target.get("qty") is not None and flt(target.get("qty")) <= 0.000001)
		):
			target = None
		if not old_name:
			continue
		remap[old_name] = {
			"customer_schedule_item": target.get("name") if target else None,
			"customer_schedule": target.get("parent") if target else None,
			"schedule_date": target.get("schedule_date") if target else None,
		}
	return remap


def _remap_schedule_execution_allocations(
	*,
	previous_item_rows: list[dict[str, Any]],
	new_item_rows: list[dict[str, Any]],
	diff_rows: list[dict[str, Any]],
) -> dict[str, int]:
	return _run_atomic_batch_operation(
		"aps_remap_schedule_allocations",
		lambda: _rebuild_schedule_execution_allocations(
			previous_item_rows=previous_item_rows,
			new_item_rows=new_item_rows,
			diff_rows=diff_rows,
		),
	)


def _rebuild_schedule_execution_allocations(
	*,
	previous_item_rows: list[dict[str, Any]],
	new_item_rows: list[dict[str, Any]],
	diff_rows: list[dict[str, Any]],
) -> dict[str, int]:
	from injection_aps.services import capacity_balance, delivery_sync, execution_sync

	remap = _build_schedule_item_remap(previous_item_rows, new_item_rows, diff_rows)
	old_item_names = sorted(remap)
	counts = {
		"schedule_items": len(remap),
		"production_allocations": 0,
		"delivery_allocations": 0,
		"delivery_syncs": 0,
		"production_syncs": 0,
	}
	if not old_item_names:
		return counts

	production_runs = []
	if frappe.db.exists("DocType", "APS Production Allocation"):
		production_rows = frappe.get_all(
			"APS Production Allocation",
			filters={"customer_schedule_item": ("in", old_item_names)},
			fields=["name", "planning_run", "schedule_result"],
		)
		counts["production_allocations"] = len(production_rows)
		production_runs = sorted({row.get("planning_run") for row in production_rows if row.get("planning_run")})

	delivery_rows = []
	if frappe.db.exists("DocType", "APS Delivery Allocation"):
		delivery_rows = frappe.get_all(
			"APS Delivery Allocation",
			filters={"customer_schedule_item": ("in", old_item_names)},
			fields=["name", "source_delivery_note_item"],
		)
		counts["delivery_allocations"] = len(delivery_rows)

	direct_rows = []
	direct_link_updates = []
	if frappe.db.exists("DocType", "Delivery Note Item") and frappe.get_meta("Delivery Note Item").has_field(
		"custom_aps_customer_schedule_item"
	):
		direct_rows = frappe.get_all(
			"Delivery Note Item",
			filters={"custom_aps_customer_schedule_item": ("in", old_item_names)},
			fields=["name", "custom_aps_customer_schedule_item"],
		)
		for row in direct_rows:
			# These rows were explicitly linked by the user and therefore have one
			# unambiguous target.  FIFO-split rows have no direct field and are
			# remapped part-by-part through the delivery ledger below.  Keep the old
			# direct value until delivery synchronization succeeds: it is also the
			# discovery trace for a submitted DN whose async ledger has not run yet.
			target = remap.get(row.get("custom_aps_customer_schedule_item")) or {}
			direct_link_updates.append((row.get("name"), target.get("customer_schedule_item")))

	item_codes = sorted(
		{row.get("item_code") for row in [*(previous_item_rows or []), *(new_item_rows or [])] if row.get("item_code")}
	)
	company = next((row.get("company") for row in new_item_rows or [] if row.get("company")), None)
	customer = next((row.get("customer") for row in new_item_rows or [] if row.get("customer")), None)
	if not company or not customer:
		parent = next(
			(
				row.get("parent")
				for row in [*(new_item_rows or []), *(previous_item_rows or [])]
				if row.get("parent")
			),
			None,
		)
		if parent:
			schedule_scope = frappe.db.get_value(
				"Customer Delivery Schedule", parent, ["company", "customer"], as_dict=True
			) or {}
			company = company or schedule_scope.get("company")
			customer = customer or schedule_scope.get("customer")
	if not company or not customer or not item_codes:
		frappe.throw(
			_("APS execution lineage scope could not be resolved; the schedule replacement was not applied."),
			frappe.ValidationError,
		)
	baseline_runs = _remap_result_fulfillment_baselines(
		remap,
		new_item_rows=new_item_rows,
		company=company,
		customer=customer,
		item_codes=item_codes,
	)
	production_runs = sorted(set(production_runs) | set(baseline_runs))
	# Always rescan the replaced physical scope.  A submitted DN can commit before
	# its enqueue-after-commit allocation job and therefore has neither a direct
	# link nor a ledger row yet; target_remap is the only durable discovery trace.
	if company and customer and item_codes:
		delivery_sync.sync_delivery_allocations(
			company=company,
			customer=customer,
			item_codes=item_codes,
			target_remap={
				old_name: target if target.get("customer_schedule_item") else None
				for old_name, target in remap.items()
			},
		)
		counts["delivery_syncs"] = 1
		# Only after the physical DN history has either been remapped or proven a
		# fully returned zero-net chain may the explicit source link move/disappear.
		for delivery_note_item, replacement_item in direct_link_updates:
			frappe.db.set_value(
				"Delivery Note Item",
				delivery_note_item,
				"custom_aps_customer_schedule_item",
				replacement_item,
				update_modified=False,
			)
	for run_name in production_runs:
		capacity_balance.invalidate_capacity_balance(run_name)
		execution_sync.sync_production_for_run(run_name)
		counts["production_syncs"] += 1
	return counts


def _remap_result_fulfillment_baselines(
	remap: dict[str, dict[str, Any]],
	*,
	new_item_rows: list[dict[str, Any]],
	company: str | None,
	customer: str | None,
	item_codes: list[str],
) -> list[str]:
	"""Move persisted target lineage before production reconciliation is replayed."""
	if not remap or not company or not customer or not item_codes:
		return []
	rows = frappe.get_all(
		"APS Schedule Result",
		filters={
			"company": company,
			"customer": customer,
			"item_code": ("in", item_codes),
		},
		fields=["name", "planning_run", "fulfillment_baseline_json"],
	)
	affected_runs = set()
	current_qty_by_name = {
		row.get("name"): flt(row.get("qty"))
		for row in new_item_rows or []
		if row.get("name")
	}
	for row in rows:
		baseline = _parse_json_object(row.get("fulfillment_baseline_json"), {})
		targets = baseline.get("targets") if isinstance(baseline, dict) else None
		if not isinstance(targets, list):
			continue
		changed = False
		demand_changed = False
		for target in targets:
			old_name = target.get("customer_schedule_item")
			if old_name not in remap:
				continue
			mapping = remap.get(old_name) or {}
			if mapping.get("customer_schedule_item"):
				current_qty = flt(current_qty_by_name.get(mapping.get("customer_schedule_item")))
				if (
					getdate(mapping.get("schedule_date")) != getdate(target.get("schedule_date"))
					or abs(current_qty - flt(target.get("opening_required_qty"))) > 0.000001
				):
					demand_changed = True
				target["customer_schedule_item"] = mapping.get("customer_schedule_item")
				target["customer_schedule"] = mapping.get("customer_schedule")
				target["schedule_date"] = str(mapping.get("schedule_date") or "")
				target["current_required_qty"] = current_qty
				target.pop("retired", None)
			else:
				target["retired"] = 1
				demand_changed = True
			changed = True
		if not changed:
			continue
		frappe.db.set_value(
			"APS Schedule Result",
			row.name,
			"fulfillment_baseline_json",
			json.dumps(
				baseline,
				ensure_ascii=True,
				sort_keys=True,
				separators=(",", ":"),
				default=str,
			),
			update_modified=False,
		)
		if row.get("planning_run"):
			affected_runs.add(row.get("planning_run"))
		if demand_changed and row.get("planning_run"):
			frappe.db.set_value(
				"APS Schedule Result",
				row.name,
				{
					"status": "Blocked",
					"risk_status": "Critical",
					"flow_step": "Customer Schedule Changed",
					"next_step_hint": "Recalculate the planning run before release",
					"blocking_reason": _(
						"Customer schedule quantity/date changed after planning; the execution ledger was preserved, but the plan commitment must be recalculated."
					),
				},
				update_modified=False,
			)
			_ensure_open_exception(
				planning_run=row.get("planning_run"),
				severity="Blocking",
				exception_type="Customer Schedule Changed After Planning",
				message=_(
					"Customer schedule lineage for APS result {0} changed after planning. Recalculate before any further release."
				).format(row.name),
				source_doctype="APS Schedule Result",
				source_name=row.name,
				resolution_hint=_("Create or recalculate a planning run against the active customer schedule."),
				is_blocking=1,
			)
	return sorted(affected_runs)


def _get_active_schedule_snapshot(
	customer: str,
	company: str,
	schedule_scope: str | None,
) -> dict[str, Any]:
	schedule_scope = _normalize_schedule_scope(schedule_scope)
	schedule_headers = frappe.get_all(
		"Customer Delivery Schedule",
		filters={
			"customer": customer,
			"company": company,
			"schedule_scope": schedule_scope,
			"status": "Active",
		},
		fields=["name", "modified"],
		order_by="name asc",
	)
	schedule_names = [row.get("name") for row in schedule_headers if row.get("name")]
	if not schedule_names:
		payload = {
			"customer": customer or "",
			"company": company or "",
			"schedule_scope": schedule_scope or "",
			"headers": [],
			"items": [],
		}
		return {
			"rows": [],
			"token": hashlib.sha256(
				json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
			).hexdigest(),
		}
	rows = frappe.get_all(
		"Customer Delivery Schedule Item",
		filters={"parent": ("in", schedule_names), "parenttype": "Customer Delivery Schedule"},
		fields=[
			"name",
			"parent",
			"idx",
			"sales_order",
			"item_code",
			"customer_part_no",
			"schedule_date",
			"qty",
			"allocated_qty",
			"produced_qty",
			"delivered_qty",
			"balance_qty",
			"status",
			"remark",
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
			"source_origin",
			"source_excel_row",
			"source_excel_rows",
			"manual_override",
			"manual_change_reason",
		],
		order_by="parent asc, idx asc, name asc",
	)
	if rows:
		from injection_aps.services import delivery_sync

		live_delivered_qty = delivery_sync.get_schedule_delivery_lower_bounds(
			company=company,
			customer=customer,
			schedule_item_names=[row.get("name") for row in rows if row.get("name")],
		)
		for row in rows:
			# Submitted Delivery Notes are the authority.  Child/ledger rollups are
			# asynchronous caches and may be either low (new DN) or high (new return).
			delivered_qty = max(flt(live_delivered_qty.get(row.get("name"))), 0)
			qty = flt(row.get("qty"))
			row["delivered_qty"] = delivered_qty
			row["balance_qty"] = max(qty - delivered_qty, 0)
			row["status"] = "Cancelled" if qty <= 0.000001 else ("Covered" if delivered_qty >= qty else "Open")
	state_payload = {
		"customer": customer or "",
		"company": company or "",
		"schedule_scope": schedule_scope or "",
		"headers": [
			{"name": row.get("name") or "", "modified": str(row.get("modified") or "")}
			for row in schedule_headers
		],
		"items": [
			{
				"name": row.get("name") or "",
				"parent": row.get("parent") or "",
				"idx": cint(row.get("idx")),
				"sales_order": row.get("sales_order") or "",
				"item_code": row.get("item_code") or "",
				"customer_part_no": row.get("customer_part_no") or "",
				"schedule_date": str(row.get("schedule_date") or ""),
				"qty": round(flt(row.get("qty")), 6),
				"allocated_qty": round(flt(row.get("allocated_qty")), 6),
				"produced_qty": round(flt(row.get("produced_qty")), 6),
				"delivered_qty": round(flt(row.get("delivered_qty")), 6),
				"balance_qty": round(flt(row.get("balance_qty")), 6),
				"status": row.get("status") or "",
				"remark": str(row.get("remark") or ""),
				"production_strategy": row.get("production_strategy") or "",
				"demand_confidence": row.get("demand_confidence") or "",
				"cancellation_risk_percent": round(flt(row.get("cancellation_risk_percent")), 6),
				"prebuild_allowed": cint(row.get("prebuild_allowed")),
				"max_prebuild_days": cint(row.get("max_prebuild_days")),
				"source_origin": str(row.get("source_origin") or ""),
				"source_excel_row": cint(row.get("source_excel_row")),
				"source_excel_rows": str(row.get("source_excel_rows") or ""),
				"manual_override": cint(row.get("manual_override")),
				"manual_change_reason": str(row.get("manual_change_reason") or ""),
			}
			for row in rows
		],
	}
	return {
		"rows": _aggregate_schedule_rows(rows),
		"token": hashlib.sha256(
			json.dumps(state_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
		).hexdigest(),
	}


def _get_active_schedule_rows(customer: str, company: str, schedule_scope: str | None) -> list[dict[str, Any]]:
	return _get_active_schedule_snapshot(customer, company, schedule_scope)["rows"]


def _aggregate_schedule_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	aggregated = {}
	for row in rows or []:
		key = _schedule_row_key(row)
		existing = aggregated.get(key)
		if not existing:
			source_excel_rows = _schedule_source_row_numbers(row)
			aggregated[key] = {
				"sales_order": row.get("sales_order"),
				"item_code": row.get("item_code"),
				"customer_part_no": row.get("customer_part_no"),
				"schedule_date": getdate(row.get("schedule_date")) if row.get("schedule_date") else None,
				"qty": flt(row.get("qty")),
				"allocated_qty": flt(row.get("allocated_qty")),
				"produced_qty": flt(row.get("produced_qty")),
				"delivered_qty": flt(row.get("delivered_qty")),
				"balance_qty": flt(row.get("balance_qty")),
				"remark": row.get("remark"),
				"source_origin": row.get("source_origin") or "imported",
				"source_excel_row": source_excel_rows[0] if source_excel_rows else 0,
				"source_excel_rows": ", ".join(str(value) for value in source_excel_rows),
				"manual_override": cint(row.get("manual_override")),
				"manual_change_reason": row.get("manual_change_reason"),
				"production_strategy": row.get("production_strategy") or "Auto Balance",
				"demand_confidence": row.get("demand_confidence") or "Confirmed",
				"cancellation_risk_percent": flt(row.get("cancellation_risk_percent")),
				"prebuild_allowed": (
					cint(row.get("prebuild_allowed")) if row.get("prebuild_allowed") is not None else 1
				),
				"max_prebuild_days": cint(row.get("max_prebuild_days")),
			}
			continue
		existing["qty"] += flt(row.get("qty"))
		existing["allocated_qty"] += flt(row.get("allocated_qty"))
		existing["produced_qty"] += flt(row.get("produced_qty"))
		existing["delivered_qty"] += flt(row.get("delivered_qty"))
		existing["balance_qty"] += flt(row.get("balance_qty"))
		_merge_aggregated_schedule_policy(existing, row)
		combined_source_rows = sorted(
			set(_schedule_source_row_numbers(existing)) | set(_schedule_source_row_numbers(row))
		)
		existing["source_excel_row"] = combined_source_rows[0] if combined_source_rows else 0
		existing["source_excel_rows"] = ", ".join(str(value) for value in combined_source_rows)
	return list(aggregated.values())


def _merge_aggregated_schedule_policy(existing: dict[str, Any], row: dict[str, Any]):
	"""Keep conservative execution controls when Append versions share one business key."""
	row_strategy = row.get("production_strategy") or "Auto Balance"
	if existing.get("production_strategy") != row_strategy:
		existing["production_strategy"] = "Force JIT"
	if (row.get("demand_confidence") or "Confirmed") == "Forecast":
		existing["demand_confidence"] = "Forecast"
	existing["cancellation_risk_percent"] = max(
		flt(existing.get("cancellation_risk_percent")), flt(row.get("cancellation_risk_percent"))
	)
	existing["prebuild_allowed"] = min(
		cint(existing.get("prebuild_allowed")),
		cint(row.get("prebuild_allowed")) if row.get("prebuild_allowed") is not None else 1,
	)
	existing["max_prebuild_days"] = min(
		cint(existing.get("max_prebuild_days")), cint(row.get("max_prebuild_days"))
	)


def _record_schedule_deltas(
	import_batch: str,
	schedule_name: str,
	customer: str,
	company: str,
	schedule_scope: str | None,
	diff_rows: list[dict[str, Any]],
):
	if not frappe.db.exists("DocType", "APS Demand Delta"):
		return
	for row in diff_rows or []:
		previous_qty = flt(row.get("previous_qty"))
		current_qty = flt(row.get("new_qty") if row.get("new_qty") is not None else row.get("qty"))
		delta_qty = flt(row.get("delta_qty") if row.get("delta_qty") is not None else current_qty - previous_qty)
		change_type = row.get("change_type") or "Unchanged"
		if change_type == "Unchanged" and abs(delta_qty) < 0.0001:
			continue
		frappe.get_doc(
			{
				"doctype": "APS Demand Delta",
				"import_batch": import_batch,
				"schedule_reference": schedule_name,
				"customer": customer,
				"company": company,
				"schedule_scope": _normalize_schedule_scope(schedule_scope),
				"sales_order": row.get("sales_order"),
				"item_code": _normalize_item_code(row.get("item_code")),
				"customer_part_no": row.get("customer_part_no"),
				"previous_schedule_date": row.get("previous_schedule_date"),
				"current_schedule_date": row.get("schedule_date"),
				"previous_qty": previous_qty,
				"current_qty": current_qty,
				"delta_qty": delta_qty,
				"change_type": change_type,
				"source_excel_rows": row.get("source_excel_rows") or str(row.get("source_excel_row") or ""),
				"produced_qty": flt(row.get("produced_qty")),
				"delivered_qty": flt(row.get("delivered_qty")),
				"frozen_qty": flt(row.get("frozen_qty")),
				"affects_produced": cint(row.get("affects_produced")),
				"affects_delivered": cint(row.get("affects_delivered")),
				"affects_frozen": cint(row.get("affects_frozen")),
				"remark": row.get("remark"),
			}
		).insert(ignore_permissions=True)


def rebuild_demand_pool(company: str | None = None) -> dict[str, Any]:
	company = str(company or "").strip()
	if not company:
		frappe.throw(
			_("Company is required for an APS Demand Pool rebuild.", context="Injection APS"),
			frappe.ValidationError,
		)
	reference_repair = repair_item_references(company=company, include_standard=0, include_aps=1)
	_delete_system_generated_rows("APS Demand Pool", company=company)

	created_names = []
	warnings = []
	warning_keys = set()
	skipped_rows = 0
	active_schedules = frappe.get_all(
		"Customer Delivery Schedule",
		filters=_strip_none({"company": company, "status": "Active"}),
		fields=["name", "customer", "company", "version_no", "source_type"],
	)

	for schedule in active_schedules:
		for row in frappe.get_all(
			"Customer Delivery Schedule Item",
			filters={"parent": schedule.name, "parenttype": "Customer Delivery Schedule"},
			fields=[
				"name",
				"sales_order",
				"item_code",
				"schedule_date",
				"qty",
				"allocated_qty",
				"produced_qty",
				"delivered_qty",
				"balance_qty",
				"change_type",
				"customer_part_no",
				"production_strategy",
				"demand_confidence",
				"cancellation_risk_percent",
				"prebuild_allowed",
				"max_prebuild_days",
			],
		):
			resolved_item_code = _resolve_item_name(row.item_code)
			if not resolved_item_code:
				skipped_rows += 1
				_append_rebuild_warning(
					warnings,
					warning_keys,
					item_reference=row.item_code,
					source_doctype="Customer Delivery Schedule",
					source_name=schedule.name,
					row_name=row.name,
				)
				continue
			if not _is_schedulable_item(resolved_item_code):
				skipped_rows += 1
				_append_item_group_warning(
					warnings,
					warning_keys,
					item_code=resolved_item_code,
					source_doctype="Customer Delivery Schedule",
					source_name=schedule.name,
					row_name=row.name,
					item_group=_get_item_group(resolved_item_code),
				)
				continue
			if resolved_item_code != row.item_code:
				frappe.db.set_value(
					"Customer Delivery Schedule Item",
					row.name,
					"item_code",
					resolved_item_code,
					update_modified=False,
				)
			open_qty = _schedule_row_open_demand_qty(row)
			if open_qty <= 0:
				continue
			demand = _build_demand_row(
				company=schedule.company,
				customer=schedule.customer,
				item_code=resolved_item_code,
				demand_source=schedule.source_type or "Customer Delivery Schedule",
				demand_date=row.schedule_date,
				qty=open_qty,
				source_doctype="Customer Delivery Schedule",
				source_name=schedule.name,
				sales_order=row.sales_order,
				sales_order_item=_resolve_unique_sales_order_item(row.sales_order, resolved_item_code),
				source_detail_name=row.name,
				remark=row.change_type,
				customer_part_no=row.customer_part_no,
				production_strategy=row.production_strategy,
				demand_confidence=(
					"Forecast" if (schedule.source_type or "") == "Forecast" else row.demand_confidence
				),
				cancellation_risk_percent=row.cancellation_risk_percent,
				prebuild_allowed=row.prebuild_allowed,
				max_prebuild_days=row.max_prebuild_days,
			)
			created_names.append(demand.insert(ignore_permissions=True).name)

	backlog_result = _append_sales_order_backlog(company=company, warnings=warnings, warning_keys=warning_keys)
	created_names.extend(backlog_result["rows"])
	skipped_rows += cint(backlog_result.get("skipped_rows"))
	created_names.extend(_append_safety_stock_demands(company=company))

	return {
		"created_rows": len(created_names),
		"rows": created_names,
		"warning_count": len(warnings),
		"warnings": warnings[:MAX_REBUILD_WARNINGS],
		"skipped_rows": skipped_rows,
		"reference_repair": reference_repair,
	}


def _schedule_row_open_demand_qty(row: dict[str, Any] | Any) -> float:
	"""Return delivery demand without trusting the legacy allocation cache.

	``allocated_qty`` has never been an authoritative APS execution ledger.  It is
	carried across schedule versions for display, while exact submitted Work Orders
	are deducted later by :func:`rebuild_net_requirements`.  Subtracting both here
	and there under-plans the same demand and leaves a cancelled Work Order's stale
	cache suppressing demand indefinitely.
	"""
	balance_qty = row.get("balance_qty")
	if balance_qty in (None, ""):
		balance_qty = max(flt(row.get("qty")) - flt(row.get("delivered_qty")), 0)
	return max(flt(balance_qty), 0)


def rebuild_net_requirements(
	company: str | None = None,
	existing_work_order_policy: str | None = None,
) -> dict[str, Any]:
	company = str(company or "").strip()
	if not company:
		frappe.throw(
			_("Company is required for an APS Net Requirement rebuild.", context="Injection APS"),
			frappe.ValidationError,
		)
	existing_work_order_policy = _normalize_existing_work_order_policy(existing_work_order_policy)
	reference_repair = repair_item_references(company=company, include_standard=0, include_aps=1)
	_delete_system_generated_rows("APS Net Requirement", company=company)

	demand_rows = frappe.get_all(
		"APS Demand Pool",
		filters=_strip_none({"company": company, "status": ("!=", "Cancelled")}),
		fields=[
			"name",
			"company",
			"customer",
			"sales_order",
			"sales_order_item",
			"item_code",
			"demand_date",
			"qty",
			"demand_source",
			"is_urgent",
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
			"source_doctype",
			"source_name",
			"source_detail_name",
		],
		order_by="demand_date asc, priority_score desc, modified asc",
	)
	grouped = defaultdict(list)
	warnings = []
	warning_keys = set()
	skipped_rows = 0
	for row in demand_rows:
		resolved_item_code = _resolve_item_name(row.item_code)
		if not resolved_item_code:
			skipped_rows += 1
			_append_rebuild_warning(
				warnings,
				warning_keys,
				item_reference=row.item_code,
				source_doctype="APS Demand Pool",
				source_name=row.name,
			)
			continue
		if not _is_schedulable_item(resolved_item_code):
			skipped_rows += 1
			_append_item_group_warning(
				warnings,
				warning_keys,
				item_code=resolved_item_code,
				source_doctype="APS Demand Pool",
				source_name=row.name,
				item_group=_get_item_group(resolved_item_code),
			)
			continue
		if resolved_item_code != row.item_code:
			frappe.db.set_value("APS Demand Pool", row.name, "item_code", resolved_item_code, update_modified=False)
			row.item_code = resolved_item_code
		grouped[
			(
				row.company,
				row.customer,
				row.sales_order,
				row.sales_order_item,
				resolved_item_code,
				row.demand_date,
				row.production_strategy or "Auto Balance",
				row.demand_confidence or ("Forecast" if row.demand_source == "Forecast" else "Confirmed"),
				flt(row.cancellation_risk_percent),
				cint(row.prebuild_allowed),
				cint(row.max_prebuild_days),
			)
		].append(row)

	has_safety_demand_by_item = {
		_normalize_item_code(row.item_code)
		for row in demand_rows
		if (row.get("demand_source") or "") == "Safety Stock"
	}
	stock_map = _get_available_stock_map(company, demand_rows=demand_rows)
	open_work_order_map = _get_open_work_order_map(company) if existing_work_order_policy == "Include" else {}
	settings = get_settings_dict()
	item_codes = {_normalize_item_code(row.item_code) for row in demand_rows if row.get("item_code")}
	safety_stock_by_item = {
		item: flt(_get_item_mapping_value(item, settings["item_safety_stock_field"]))
		for item in item_codes
	}
	# Safety stock is a floor, not a second demand that can share the same pieces
	# with a customer order.  Only stock above that floor is available to demand.
	remaining_stock_map = defaultdict(
		float,
		{
			item: max(flt(stock_map.get(item)) - flt(safety_stock_by_item.get(item)), 0)
			for item in item_codes
		},
	)
	remaining_work_order_map = defaultdict(float, {key: flt(qty) for key, qty in open_work_order_map.items()})
	safety_gap_remaining_map: dict[str, float] = {}
	minimum_batch_surplus_by_item = defaultdict(float)
	minimum_batch_owners_by_identity = defaultdict(list)
	# Exact customer/SO demand remains independent, but its unavoidable minimum-
	# batch surplus is physical stock and may satisfy the same item's safety floor.
	# Process dedicated Safety Stock rows after customer demand so stock production
	# never silently replaces an exact Sales Order requirement.
	grouped_rows = sorted(
		grouped.items(),
		key=lambda entry: (
			all((row.get("demand_source") or "") == "Safety Stock" for row in entry[1]),
			str(entry[0][5] or ""),
			str(entry[0][1] or ""),
			str(entry[0][2] or ""),
			str(entry[0][3] or ""),
		),
	)

	created_names = []
	for group_key, rows in grouped_rows:
		(
		row_company,
		customer,
		sales_order,
		sales_order_item,
		item_code,
		demand_date,
		production_strategy,
		demand_confidence,
		cancellation_risk_percent,
		prebuild_allowed,
		max_prebuild_days,
		) = group_key
		demand_qty = sum(flt(row.qty) for row in rows)
		safety_stock_qty = flt(safety_stock_by_item.get(item_code))
		max_stock_qty = flt(_get_item_mapping_value(item_code, settings["item_max_stock_field"]))
		minimum_batch_qty = flt(_get_item_mapping_value(item_code, settings["item_min_batch_field"]))
		if item_code not in safety_gap_remaining_map:
			safety_gap_remaining_map[item_code] = (
				0
				if item_code in has_safety_demand_by_item
				else max(safety_stock_qty - flt(stock_map.get(item_code)), 0)
			)
		is_safety_stock_group = all((row.get("demand_source") or "") == "Safety Stock" for row in rows)
		allow_stock_work_order_pool = bool(
			is_safety_stock_group
			or (
				not customer
				and all(
					(row.get("source_doctype") or "") != "Customer Delivery Schedule"
					for row in rows
				)
			)
		)
		# Safety Stock rows are already the post-free-stock gap; do not spend the same stock twice.
		available_stock_qty = 0 if is_safety_stock_group else min(demand_qty, flt(remaining_stock_map[item_code]))
		if not is_safety_stock_group:
			remaining_stock_map[item_code] = max(flt(remaining_stock_map[item_code]) - available_stock_qty, 0)
		open_qty_after_stock = max(demand_qty - available_stock_qty, 0)
		work_order_identity = _get_net_requirement_work_order_identity(
			company=row_company,
			item_code=item_code,
			sales_order=sales_order,
			sales_order_item=sales_order_item,
			is_safety_stock=is_safety_stock_group,
			allow_stock_pool=allow_stock_work_order_pool,
		)
		open_work_order_qty = (
			min(open_qty_after_stock, flt(remaining_work_order_map[work_order_identity]))
			if work_order_identity
			else 0
		)
		if work_order_identity:
			remaining_work_order_map[work_order_identity] = max(
				flt(remaining_work_order_map[work_order_identity]) - open_work_order_qty,
				0,
			)
		safety_gap = flt(safety_gap_remaining_map.get(item_code))
		safety_gap_remaining_map[item_code] = 0
		overstock_qty = max(flt(remaining_stock_map[item_code]) - max_stock_qty, 0) if max_stock_qty else 0
		net_qty = max(demand_qty - available_stock_qty - open_work_order_qty + safety_gap, 0)
		minimum_batch_coverage_qty = 0.0
		coverage_owner = None
		batch_identity = (*group_key[:5], *group_key[6:])
		# A schedule target may only belong to one Result.  Reuse an earlier lot's
		# surplus only when that one lot can cover this whole, otherwise-uncovered
		# target.  We deliberately do not split one target across stock/WO/multiple
		# Results merely to make the minimum-batch arithmetic look smaller.
		if (
			not is_safety_stock_group
			and net_qty > QTY_TOLERANCE
			and available_stock_qty <= QTY_TOLERANCE
			and open_work_order_qty <= QTY_TOLERANCE
			and safety_gap <= QTY_TOLERANCE
			and abs(net_qty - demand_qty) <= QTY_TOLERANCE
		):
			coverage_owner = next(
				(
					owner
					for owner in minimum_batch_owners_by_identity[batch_identity]
					if flt(owner.get("surplus_qty")) + QTY_TOLERANCE >= net_qty
				),
				None,
			)
			if coverage_owner:
				minimum_batch_coverage_qty = net_qty
				net_qty = 0
				coverage_owner["surplus_qty"] = max(
					flt(coverage_owner.get("surplus_qty")) - minimum_batch_coverage_qty,
					0,
				)
				minimum_batch_surplus_by_item[item_code] = max(
					flt(minimum_batch_surplus_by_item[item_code]) - minimum_batch_coverage_qty,
					0,
				)
		elif is_safety_stock_group and net_qty > 0:
			minimum_batch_coverage_qty = min(
				net_qty,
				max(flt(minimum_batch_surplus_by_item[item_code]), 0),
			)
			net_qty = max(net_qty - minimum_batch_coverage_qty, 0)
			minimum_batch_surplus_by_item[item_code] = max(
				flt(minimum_batch_surplus_by_item[item_code]) - minimum_batch_coverage_qty,
				0,
			)
		planning_qty = max(net_qty, minimum_batch_qty) if net_qty > 0 and minimum_batch_qty > 0 else net_qty
		new_batch_surplus = 0.0
		if not is_safety_stock_group:
			new_batch_surplus = max(planning_qty - net_qty, 0)
			minimum_batch_surplus_by_item[item_code] += new_batch_surplus
		reason_text = _build_net_requirement_reason(
			demand_qty=demand_qty,
			available_stock_qty=available_stock_qty,
			open_work_order_qty=open_work_order_qty,
			existing_work_order_policy=existing_work_order_policy,
			safety_gap=safety_gap,
			overstock_qty=overstock_qty,
			minimum_batch_qty=minimum_batch_qty,
			planning_qty=planning_qty,
		)
		if minimum_batch_coverage_qty > 0 and is_safety_stock_group:
			reason_text = "{0} {1}".format(
				reason_text,
				_(
					"Existing customer minimum-batch surplus covers {0} of the safety-stock requirement.",
					context="Injection APS",
				).format(minimum_batch_coverage_qty),
			)
		elif minimum_batch_coverage_qty > 0:
			reason_text = "{0} {1}".format(
				reason_text,
				_(
					"One earlier compatible minimum-batch lot fully covers this requirement: {0}.",
					context="Injection APS",
				).format(minimum_batch_coverage_qty),
			)
		source_snapshot_json, fulfillment_baseline_json = _build_net_requirement_lineage_snapshot(
			rows,
			demand_qty=demand_qty,
			available_stock_qty=available_stock_qty,
			open_work_order_qty=open_work_order_qty,
			existing_work_order_policy=existing_work_order_policy,
			safety_stock_gap_qty=safety_gap,
			minimum_batch_qty=minimum_batch_qty,
			minimum_batch_coverage_qty=minimum_batch_coverage_qty,
			net_requirement_qty=net_qty,
			planning_qty=planning_qty,
			new_batch_surplus_qty=new_batch_surplus,
			is_safety_stock_group=is_safety_stock_group,
		)

		if coverage_owner:
			_extend_minimum_batch_owner_lineage(coverage_owner, rows)

		doc_values = {
				"doctype": "APS Net Requirement",
				"company": row_company,
				"customer": customer,
				"sales_order": sales_order,
				"sales_order_item": sales_order_item,
				"item_code": item_code,
				"demand_date": demand_date,
				"demand_qty": demand_qty,
				"available_stock_qty": available_stock_qty,
				"open_work_order_qty": open_work_order_qty,
				"existing_work_order_policy": existing_work_order_policy,
				"safety_stock_gap_qty": safety_gap,
				"max_stock_qty": max_stock_qty,
				"overstock_qty": overstock_qty,
				"minimum_batch_qty": minimum_batch_qty,
				"production_strategy": production_strategy,
				"demand_confidence": demand_confidence,
				"cancellation_risk_percent": cancellation_risk_percent,
				"prebuild_allowed": prebuild_allowed,
				"max_prebuild_days": max_prebuild_days,
				"planning_qty": planning_qty,
				"net_requirement_qty": net_qty,
				"reason_text": reason_text,
				"demand_source_snapshot_json": source_snapshot_json,
				"fulfillment_baseline_json": fulfillment_baseline_json,
				"is_system_generated": 1,
			}
		doc = frappe.get_doc(doc_values).insert(ignore_permissions=True)
		created_names.append(doc.name)
		if new_batch_surplus > QTY_TOLERANCE:
			minimum_batch_owners_by_identity[batch_identity].append(
				{
					"name": doc.name,
					"values": doc_values,
					"rows": list(rows),
					"surplus_qty": new_batch_surplus,
					"minimum_batch_coverage_qty": minimum_batch_coverage_qty,
					"new_batch_surplus_qty": new_batch_surplus,
					"is_safety_stock_group": is_safety_stock_group,
					"base_reason_text": reason_text,
				}
			)

	return {
		"existing_work_order_policy": existing_work_order_policy,
		"created_rows": len(created_names),
		"rows": created_names,
		"warning_count": len(warnings),
		"warnings": warnings[:MAX_REBUILD_WARNINGS],
		"skipped_rows": skipped_rows,
		"reference_repair": reference_repair,
	}


def _build_net_requirement_lineage_snapshot(
	rows: list[dict[str, Any]],
	*,
	demand_qty: float | None = None,
	available_stock_qty: float | None = None,
	open_work_order_qty: float | None = None,
	existing_work_order_policy: str | None = None,
	safety_stock_gap_qty: float | None = None,
	minimum_batch_qty: float | None = None,
	minimum_batch_coverage_qty: float | None = None,
	net_requirement_qty: float | None = None,
	planning_qty: float | None = None,
	new_batch_surplus_qty: float | None = None,
	is_safety_stock_group: bool | int = False,
) -> tuple[str, str]:
	"""Freeze demand sources and fulfillment offsets at net-requirement creation."""
	source_rows = []
	target_names = sorted(
		{
			row.get("source_detail_name")
			for row in rows or []
			if row.get("source_doctype") == "Customer Delivery Schedule" and row.get("source_detail_name")
		}
	)
	target_map = {}
	if target_names:
		target_map = {
			row.name: row
			for row in frappe.get_all(
				"Customer Delivery Schedule Item",
				filters={"name": ("in", target_names)},
				fields=[
					"name",
					"parent",
					"sales_order",
					"item_code",
					"schedule_date",
					"qty",
					"allocated_qty",
					"produced_qty",
					"delivered_qty",
				],
			)
		}
	sales_order_item_open_qty = defaultdict(float)
	for row in rows or []:
		if (row.get("demand_source") or "") != "Sales Order Backlog":
			continue
		sales_order_item = row.get("sales_order_item")
		if sales_order_item:
			sales_order_item_open_qty[sales_order_item] += max(flt(row.get("qty")), 0)
	sales_order_item_map = {}
	if sales_order_item_open_qty:
		sales_order_item_map = {
			row.name: row
			for row in frappe.get_all(
				"Sales Order Item",
				filters={"name": ("in", sorted(sales_order_item_open_qty))},
				fields=["name", "parent", "item_code", "qty", "delivered_qty"],
			)
		}
	fulfillment_targets = []
	for row in rows or []:
		source = {
			"demand_pool": row.get("name"),
			"source_doctype": row.get("source_doctype"),
			"source_name": row.get("source_name"),
			"source_detail_name": row.get("source_detail_name"),
			"sales_order": row.get("sales_order"),
			"sales_order_item": row.get("sales_order_item"),
			"qty": max(flt(row.get("qty")), 0),
		}
		source_rows.append(source)
		target = target_map.get(row.get("source_detail_name"))
		if not target:
			continue
		fulfillment_targets.append(
			{
				"customer_schedule": target.parent,
				"customer_schedule_item": target.name,
				"sales_order": target.sales_order,
				"sales_order_item": row.get("sales_order_item"),
				"item_code": target.item_code,
				"schedule_date": str(target.schedule_date or ""),
				"source_open_qty": max(flt(row.get("qty")), 0),
				"opening_required_qty": max(flt(target.qty), 0),
				"opening_allocated_qty": max(flt(target.allocated_qty), 0),
				"opening_produced_qty": max(flt(target.produced_qty), 0),
				"opening_delivered_qty": max(flt(target.delivered_qty), 0),
			}
		)
	fulfillment_sales_order_items = []
	for sales_order_item, source_open_qty in sales_order_item_open_qty.items():
		target = sales_order_item_map.get(sales_order_item)
		if not target:
			continue
		fulfillment_sales_order_items.append(
			{
				"sales_order": target.parent,
				"sales_order_item": target.name,
				"item_code": target.item_code,
				"source_open_qty": source_open_qty,
				"opening_ordered_qty": max(flt(target.qty), 0),
				"opening_delivered_qty": max(flt(target.delivered_qty), 0),
			}
		)
	source_rows.sort(
		key=lambda row: (
			row.get("sales_order") or "",
			row.get("source_detail_name") or "",
			row.get("demand_pool") or "",
		)
	)
	fulfillment_targets.sort(key=lambda row: row.get("customer_schedule_item") or "")
	fulfillment_sales_order_items.sort(key=lambda row: row.get("sales_order_item") or "")
	return (
		json.dumps(source_rows, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str),
		json.dumps(
			{
				"version": 4,
				# Net Requirements are rebuilt and old rows are intentionally deleted.
				# Persist the stock-coverage evidence with the Result lineage so an
				# already Applied run keeps its finite-stock claim after that rebuild.
				"net_requirement": {
					"formula_version": 1,
					"demand_qty": max(flt(demand_qty), 0),
					"available_stock_qty": max(flt(available_stock_qty), 0),
					"open_work_order_qty": max(flt(open_work_order_qty), 0),
					"existing_work_order_policy": existing_work_order_policy or "",
					"safety_stock_gap_qty": max(flt(safety_stock_gap_qty), 0),
					"minimum_batch_qty": max(flt(minimum_batch_qty), 0),
					"minimum_batch_coverage_qty": max(flt(minimum_batch_coverage_qty), 0),
					"base_residual_qty": max(
						flt(demand_qty)
						- flt(available_stock_qty)
						- flt(open_work_order_qty)
						+ flt(safety_stock_gap_qty),
						0,
					),
					"net_requirement_qty": max(flt(net_requirement_qty), 0),
					"planning_qty": max(flt(planning_qty), 0),
					"new_batch_surplus_qty": max(flt(new_batch_surplus_qty), 0),
					"is_safety_stock_group": cint(is_safety_stock_group),
				},
				"targets": fulfillment_targets,
				"sales_order_items": fulfillment_sales_order_items,
			},
			ensure_ascii=True,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		),
	)


def _extend_minimum_batch_owner_lineage(
	owner: dict[str, Any],
	covered_rows: list[dict[str, Any]],
) -> None:
	"""Attach whole later targets to the one earlier lot that covers them."""
	owner_rows = owner.setdefault("rows", [])
	owner_rows.extend(covered_rows)
	owner_values = owner["values"]
	covered_qty = sum(
		max(flt(row.get("qty")), 0) for row in covered_rows
	)
	owner_values["demand_qty"] = flt(owner_values.get("demand_qty")) + covered_qty
	owner["covered_later_qty"] = flt(owner.get("covered_later_qty")) + covered_qty
	owner["minimum_batch_coverage_qty"] = (
		flt(owner.get("minimum_batch_coverage_qty")) + covered_qty
	)
	owner_values["reason_text"] = "{0} {1}".format(
		owner.get("base_reason_text") or owner_values.get("reason_text") or "",
		_(
			"This minimum-batch lot also covers {0} of later compatible schedule demand.",
			context="Injection APS",
		).format(owner["covered_later_qty"]),
	).strip()
	source_snapshot_json, fulfillment_baseline_json = _build_net_requirement_lineage_snapshot(
		owner_rows,
		demand_qty=owner_values["demand_qty"],
		available_stock_qty=owner_values.get("available_stock_qty"),
		open_work_order_qty=owner_values.get("open_work_order_qty"),
		existing_work_order_policy=owner_values.get("existing_work_order_policy"),
		safety_stock_gap_qty=owner_values.get("safety_stock_gap_qty"),
		minimum_batch_qty=owner_values.get("minimum_batch_qty"),
		minimum_batch_coverage_qty=owner.get("minimum_batch_coverage_qty"),
		net_requirement_qty=owner_values.get("net_requirement_qty"),
		planning_qty=owner_values.get("planning_qty"),
		new_batch_surplus_qty=owner.get("new_batch_surplus_qty"),
		is_safety_stock_group=owner.get("is_safety_stock_group"),
	)
	owner_values["demand_source_snapshot_json"] = source_snapshot_json
	owner_values["fulfillment_baseline_json"] = fulfillment_baseline_json
	frappe.db.set_value(
		"APS Net Requirement",
		owner["name"],
		{
			"demand_qty": owner_values["demand_qty"],
			"reason_text": owner_values["reason_text"],
			"demand_source_snapshot_json": source_snapshot_json,
			"fulfillment_baseline_json": fulfillment_baseline_json,
		},
		update_modified=False,
	)


def run_planning_run(
	run_name: str | None = None,
	company: str | None = None,
	plant_floor: str | None = None,
	plant_floors: list[str] | str | None = None,
	horizon_days: int | None = None,
	item_code: str | None = None,
	customer: str | None = None,
	run_type: str | None = None,
	existing_work_order_policy: str | None = None,
) -> dict[str, Any]:
	if not run_name and not str(company or "").strip():
		frappe.throw(
			_("Company is required when creating an APS Planning Run.", context="Injection APS"),
			frappe.ValidationError,
		)
	existing_work_order_policy = _normalize_existing_work_order_policy(existing_work_order_policy)
	settings = get_settings_dict()
	company = company or settings["default_company"]
	horizon_days = cint(horizon_days or settings["planning_horizon_days"] or 14)
	horizon_start = get_datetime(now_datetime())
	horizon_end = get_datetime(add_days(horizon_start, horizon_days))
	item_code = _resolve_item_name(item_code) if item_code else None

	if run_name:
		run_doc = frappe.get_doc("APS Planning Run", run_name)
		run_doc.company = run_doc.company or company
	else:
		run_doc = frappe.get_doc({"doctype": "APS Planning Run"})
		run_doc.company = company
	selected_plant_floors = _normalize_selected_plant_floors(
		company=run_doc.company or company,
		plant_floors=plant_floors or _get_run_selected_plant_floors(run_doc),
		plant_floor=plant_floor or run_doc.plant_floor,
		required=True,
	)
	run_doc.company = run_doc.company or company
	run_doc.planning_date = run_doc.planning_date or today()
	run_doc.horizon_days = horizon_days
	run_doc.horizon_start = horizon_start
	run_doc.horizon_end = horizon_end
	run_doc.run_type = run_type or run_doc.run_type or "Trial"
	run_doc.existing_work_order_policy = existing_work_order_policy
	run_doc.status = "Draft"
	run_doc.approval_state = "Pending"
	for fieldname, value in {
		"capacity_balance_status": "Not Analyzed",
		"capacity_balance_analyzed_on": None,
		"capacity_balance_confirmed_by": None,
		"capacity_balance_confirmed_on": None,
		"capacity_balance_applied_on": None,
		"capacity_balance_fingerprint": None,
		"capacity_balance_analysis_json": None,
	}.items():
		setattr(run_doc, fieldname, value)
	_apply_selected_plant_floors_to_run(run_doc, selected_plant_floors)
	if run_doc.is_new():
		run_doc.insert(ignore_permissions=True)
	else:
		run_doc.save(ignore_permissions=True)

	demand_rebuild = rebuild_demand_pool(company=run_doc.company)
	net_rebuild = rebuild_net_requirements(
		company=run_doc.company,
		existing_work_order_policy=existing_work_order_policy,
	)

	for name in frappe.get_all("APS Schedule Result", filters={"planning_run": run_doc.name}, pluck="name"):
		frappe.delete_doc("APS Schedule Result", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("APS Exception Log", filters={"planning_run": run_doc.name}, pluck="name"):
		frappe.delete_doc("APS Exception Log", name, force=1, ignore_permissions=True)

	net_rows = frappe.get_all(
		"APS Net Requirement",
		filters=_strip_none(
			{
			"company": run_doc.company,
			"customer": customer,
			"item_code": item_code,
			"demand_date": ("between", [getdate(horizon_start), getdate(horizon_end)]),
			}
		),
		fields=[
			"name",
			"customer",
			"sales_order",
			"sales_order_item",
			"item_code",
			"demand_date",
			"demand_qty",
			"available_stock_qty",
			"open_work_order_qty",
			"planning_qty",
			"minimum_batch_qty",
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
			"net_requirement_qty",
			"reason_text",
			"demand_source_snapshot_json",
			"fulfillment_baseline_json",
		],
		order_by="demand_date asc, modified asc",
	)
	# Fully stock-covered demand still consumes a finite physical resource. Keep a
	# zero-production Result so capacity analysis can reserve that stock across Runs.
	net_rows = [row for row in net_rows if _net_requirement_requires_result(row)]

	capability_rows = _get_machine_capability_rows(plant_floors=selected_plant_floors)
	workstation_state = _build_workstation_state_map(capability_rows)
	downtime_windows = _get_active_downtime_windows(
		company=run_doc.company,
		plant_floors=selected_plant_floors,
		horizon_start=horizon_start,
		horizon_end=horizon_end,
		run_name=run_doc.name,
	)
	locked_segments = _get_locked_segments(selected_plant_floors)
	execution_anchor_rows = _get_execution_anchor_rows(selected_plant_floors)
	mold_state = _build_mold_state_map(locked_segments)
	_apply_locked_segments_to_state(workstation_state, locked_segments)
	_apply_anchor_rows_to_state(workstation_state, mold_state, execution_anchor_rows)

	result_names = []
	exception_names = []
	total_scheduled_qty = 0
	total_unscheduled_qty = 0
	family_credit_map: dict[tuple[str, str, str, str, str], float] = defaultdict(float)

	for row in net_rows:
		original_planning_qty = _net_requirement_production_target_qty(row)
		credit_key = (
			row.customer or "",
			row.sales_order or "",
			row.sales_order_item or "",
			str(getdate(row.demand_date)),
			row.item_code,
		)
		credit_applied = min(original_planning_qty, flt(family_credit_map.get(credit_key)))
		if credit_applied:
			family_credit_map[credit_key] = max(flt(family_credit_map.get(credit_key)) - credit_applied, 0)
		planning_qty = max(original_planning_qty - credit_applied, 0)
		item_context = _get_item_context(row.item_code, settings)
		demand_source = _get_primary_demand_source(
			row.item_code,
			row.customer,
			row.demand_date,
			sales_order=row.sales_order,
			production_strategy=row.production_strategy,
		)
		best = {
			"scheduled_qty": 0,
			"unscheduled_qty": 0,
			"result_status": "Planned",
			"risk_status": "Normal",
			"segments": [],
			"selected_moulds": [],
			"copy_mold_parallel": 0,
			"family_mold_result": 1 if credit_applied else 0,
			"primary_mould_reference": "",
			"schedule_explanation": "",
			"family_side_outputs": [],
			"family_output_summary": "",
			"exceptions": [],
		}
		if planning_qty > 0:
			candidates = _select_machine_candidates(
				item_code=row.item_code,
				item_context=item_context,
				capability_rows=capability_rows,
				plant_floors=selected_plant_floors,
			)
			adjustment_best = _build_confirmed_adjustment_best(
				run_doc=run_doc,
				net_row=row,
				item_context=item_context,
				qty=planning_qty,
				candidates=candidates,
				settings=settings,
				workstation_state=workstation_state,
				mold_state=mold_state,
				horizon_start=horizon_start,
				horizon_end=horizon_end,
				downtime_windows=downtime_windows,
			)
			if adjustment_best:
				best = adjustment_best
				residual_qty = max(planning_qty - flt(adjustment_best.get("scheduled_qty")), 0)
				if residual_qty > 0:
					residual_best = _choose_best_slot(
						company=run_doc.company,
						customer=row.customer,
						item_code=row.item_code,
						item_context=item_context,
						qty=residual_qty,
						demand_date=row.demand_date,
						horizon_start=horizon_start,
						horizon_end=horizon_end,
						workstation_state=workstation_state,
						mold_state=mold_state,
						candidates=candidates,
						settings=settings,
						selected_plant_floors=selected_plant_floors,
						downtime_windows=downtime_windows,
					)
					best["segments"] = (best.get("segments") or []) + (residual_best.get("segments") or [])
					best["scheduled_qty"] = flt(best.get("scheduled_qty")) + flt(residual_best.get("scheduled_qty"))
					best["unscheduled_qty"] = max(planning_qty - flt(best.get("scheduled_qty")), 0)
					best["selected_moulds"] = list(dict.fromkeys((best.get("selected_moulds") or []) + (residual_best.get("selected_moulds") or [])))
					best["exceptions"] = (best.get("exceptions") or []) + (residual_best.get("exceptions") or [])
					best["copy_mold_parallel"] = 1 if len(best["selected_moulds"]) > 1 else best.get("copy_mold_parallel")
					if residual_best.get("risk_status") in ("Attention", "Critical", "Blocked"):
						best["risk_status"] = residual_best.get("risk_status")
						best["result_status"] = residual_best.get("result_status")
			else:
				best = _choose_best_slot(
					company=run_doc.company,
					customer=row.customer,
					item_code=row.item_code,
					item_context=item_context,
					qty=planning_qty,
					demand_date=row.demand_date,
					horizon_start=horizon_start,
					horizon_end=horizon_end,
					workstation_state=workstation_state,
					mold_state=mold_state,
					candidates=candidates,
					settings=settings,
					selected_plant_floors=selected_plant_floors,
					downtime_windows=downtime_windows,
				)
		total_scheduled_for_row = credit_applied + flt(best["scheduled_qty"])
		total_unscheduled_for_row = max(original_planning_qty - total_scheduled_for_row, 0)
		family_messages = []
		if credit_applied:
			family_messages.append(
				_("Covered {0} by prior Family Mold co-production.").format(
					frappe.format(credit_applied, {"fieldtype": "Float"})
				)
			)
		if best.get("family_output_summary"):
			family_messages.append(best["family_output_summary"])
		for side_output in best.get("family_side_outputs") or []:
			side_key = (
				row.customer or "",
				row.sales_order or "",
				row.sales_order_item or "",
				str(getdate(row.demand_date)),
				side_output.get("item_code"),
			)
			family_credit_map[side_key] = flt(family_credit_map.get(side_key)) + flt(side_output.get("qty"))

		flow_step = "Recalculation Completed"
		next_step_hint = "Confirm Run"
		blocking_reason = ""
		if best["result_status"] == "Blocked":
			flow_step = "Blocked"
			next_step_hint = "Handle Exceptions"
			blocking_reason = "; ".join(
				row_error.get("message") for row_error in (best.get("exceptions") or []) if row_error.get("is_blocking")
			)
		elif total_unscheduled_for_row > 0:
			flow_step = "Risk Pending Review"
			next_step_hint = "Review Board and Exceptions"
			blocking_reason = "There is still unscheduled quantity: {0}.".format(total_unscheduled_for_row)

		result_plant_floor = _get_primary_result_plant_floor(best.get("segments") or [], run_doc.plant_floor)
		result_doc = frappe.get_doc(
			{
				"doctype": "APS Schedule Result",
				"planning_run": run_doc.name,
				"company": run_doc.company,
				"plant_floor": result_plant_floor,
				"net_requirement": row.name,
				"customer": row.customer,
				"sales_order": row.sales_order,
				"sales_order_item": row.sales_order_item,
				"item_code": row.item_code,
				"requested_date": row.demand_date,
				"demand_source": demand_source,
				"production_strategy": row.production_strategy or settings.get("default_production_strategy") or "Auto Balance",
				"demand_confidence": row.demand_confidence or ("Forecast" if demand_source == "Forecast" else "Confirmed"),
				"cancellation_risk_percent": flt(row.cancellation_risk_percent),
				"prebuild_allowed": cint(row.prebuild_allowed),
				"max_prebuild_days": cint(row.max_prebuild_days or settings.get("default_max_prebuild_days") or 7),
				"planned_qty": original_planning_qty,
				"scheduled_qty": total_scheduled_for_row,
				"unscheduled_qty": total_unscheduled_for_row,
				"status": best["result_status"],
				"risk_status": best["risk_status"],
				"flow_step": flow_step,
				"next_step_hint": next_step_hint,
				"blocking_reason": blocking_reason,
				"copy_mold_parallel": best.get("copy_mold_parallel") or 0,
				"family_mold_result": best.get("family_mold_result") or (1 if credit_applied else 0),
				"primary_mould_reference": best.get("primary_mould_reference"),
				"selected_moulds": "\n".join(best.get("selected_moulds") or []),
				"schedule_explanation": best.get("schedule_explanation"),
				"family_output_summary": "\n".join(family_messages),
				"is_urgent": 1 if item_context["is_urgent"] else 0,
				"is_locked": 0,
				"is_manual": 0,
				"demand_source_snapshot_json": row.demand_source_snapshot_json,
				"fulfillment_baseline_json": row.fulfillment_baseline_json,
				"notes": "\n".join(part for part in [row.reason_text, *family_messages] if part),
				"segments": best["segments"],
			}
		).insert(ignore_permissions=True)
		result_names.append(result_doc.name)
		total_scheduled_qty += flt(total_scheduled_for_row)
		total_unscheduled_qty += flt(total_unscheduled_for_row)

		for error in best["exceptions"]:
			exception_doc = _create_exception(
				planning_run=run_doc.name,
				severity=error["severity"],
				exception_type=error["exception_type"],
				message=error["message"],
				item_code=row.item_code,
				customer=row.customer,
				workstation=error.get("workstation"),
				source_doctype="APS Net Requirement",
				source_name=row.name,
				resolution_hint=error.get("resolution_hint"),
				is_blocking=error.get("is_blocking", 1),
				diagnostic=error.get("diagnostic"),
			)
			exception_names.append(exception_doc.name)

	run_doc.db_set(
		{
			"horizon_days": horizon_days,
			"horizon_start": horizon_start,
			"horizon_end": horizon_end,
			"run_type": run_type or run_doc.run_type or "Trial",
			"existing_work_order_policy": existing_work_order_policy,
			"status": "Planned",
			"approval_state": "Pending",
			"total_net_requirement_qty": sum(flt(row.planning_qty or row.net_requirement_qty) for row in net_rows),
			"total_scheduled_qty": total_scheduled_qty,
			"total_unscheduled_qty": total_unscheduled_qty,
			"exception_count": len(exception_names),
			"result_count": len(result_names),
		}
	)
	overlap_summary = _validate_run_segment_overlaps(run_doc.name, persist_exceptions=True)
	mold_overlap_summary = _validate_run_mold_overlaps(run_doc.name, persist_exceptions=True)
	if overlap_summary["exception_names"] or mold_overlap_summary["exception_names"]:
		run_doc.db_set(
			"exception_count",
			len(exception_names)
			+ len(overlap_summary["exception_names"])
			+ len(mold_overlap_summary["exception_names"]),
		)
	consistency_summary = consistency.recalculate_plan_consistency(
		run_doc.name,
		reason="global or local planning run",
	)
	capacity_analysis = None
	capacity_application = None
	if result_names:
		from injection_aps.services import capacity_balance

		capacity_analysis = capacity_balance.analyze_capacity_balance(run_doc.name, persist=True)
		if (
			not capacity_analysis.get("summary", {}).get("blocked_demands")
			and not capacity_analysis.get("summary", {}).get("unscheduled_qty")
			and not capacity_analysis.get("summary", {}).get("requires_confirmation")
		):
			capacity_application = capacity_balance.apply_capacity_balance(run_doc.name)
			consistency_summary = capacity_application.get("consistency") or consistency_summary

	return {
		"run": run_doc.name,
		"existing_work_order_policy": existing_work_order_policy,
		"results": result_names,
		"exceptions": exception_names + overlap_summary["exception_names"] + mold_overlap_summary["exception_names"],
		"selected_plant_floors": selected_plant_floors,
		"filters": _strip_none({"item_code": item_code, "customer": customer}),
		"preflight_warning_count": cint(demand_rebuild.get("warning_count")) + cint(net_rebuild.get("warning_count")),
		"preflight_warnings": (demand_rebuild.get("warnings") or []) + (net_rebuild.get("warnings") or []),
		"overlap_count": overlap_summary["count"],
		"mold_overlap_count": mold_overlap_summary["count"],
		"consistency": consistency_summary,
		"capacity_balance": capacity_analysis,
		"capacity_application": capacity_application,
	}


def _net_requirement_requires_result(row: dict[str, Any] | Any) -> bool:
	"""Keep every finite resource or lot-coverage claim auditable in a run."""
	baseline = _parse_json_object(row.get("fulfillment_baseline_json"), {})
	formula_evidence = baseline.get("net_requirement") if isinstance(baseline, dict) else {}
	return (
		flt(row.get("net_requirement_qty")) > QTY_TOLERANCE
		or flt(row.get("available_stock_qty")) > QTY_TOLERANCE
		or flt(row.get("open_work_order_qty")) > QTY_TOLERANCE
		or flt((formula_evidence or {}).get("minimum_batch_coverage_qty")) > QTY_TOLERANCE
	)


def _net_requirement_production_target_qty(row: dict[str, Any] | Any) -> float:
	"""Restore exact unstarted-WO coverage before scheduling the total boundary.

	``planning_qty`` may be a minimum-batch-expanded residual after an existing WO
	was deducted.  Adding the two blindly would double the expansion.  The larger
	of the expanded residual and the physical coverage-plus-residual is the one
	total Work Order boundary that capacity and proposal reconciliation must use.
	"""
	return max(
		flt(row.get("planning_qty") or row.get("net_requirement_qty")),
		flt(row.get("open_work_order_qty")) + flt(row.get("net_requirement_qty")),
		0,
	)


def approve_planning_run(run_name: str) -> dict[str, Any]:
	from injection_aps.services import capacity_balance

	run_doc = frappe.get_doc("APS Planning Run", run_name)
	consistency_gate = consistency.assert_plan_consistent(
		run_name,
		reason="planning run approval",
	)
	capacity_balance.assert_applied_capacity_current(run_name, lock_rows=True)
	mold_gate = validate_run_mold_readiness(run_name, persist_exceptions=True)
	overlap_summary = _validate_run_segment_overlaps(run_name, persist_exceptions=True)
	mold_overlap_summary = _validate_run_mold_overlaps(run_name, persist_exceptions=True)
	blockers = [row["message"] for row in mold_gate["rows"] if row.get("blocking")]
	if overlap_summary["messages"]:
		blockers.extend(overlap_summary["messages"])
	if mold_overlap_summary["messages"]:
		blockers.extend(mold_overlap_summary["messages"])
	if blockers:
		run_doc.db_set(
			{
				"status": "Planned",
				"approval_state": "Pending",
				"exception_count": frappe.db.count("APS Exception Log", {"planning_run": run_name, "status": "Open"}),
			}
		)
		frappe.throw("<br>".join(blockers[:12]))
	result_names = frappe.get_all("APS Schedule Result", filters={"planning_run": run_name}, pluck="name")
	run_doc.db_set(
		{
			"status": "Approved",
			"approval_state": "Approved",
			"approved_by": frappe.session.user,
			"approved_on": now_datetime(),
		}
	)
	for result_name in result_names:
		frappe.db.set_value("APS Schedule Result", result_name, {"status": "Approved", "flow_step": "Plan Approved", "next_step_hint": "Review Work Order Proposals"})
	if result_names:
		for segment_name in frappe.get_all(
			"APS Schedule Segment",
			filters={"parenttype": "APS Schedule Result", "parent": ("in", result_names)},
			pluck="name",
		):
			frappe.db.set_value(
				"APS Schedule Segment",
				segment_name,
				{
					"segment_status": "Approved",
					"is_locked": 1,
					"anchor_strength": ANCHOR_STRENGTH_LOCKED,
					"execution_anchor_source": "APS Approved Segment",
				},
			)
	return {
		"run": run_name,
		"status": "Approved",
		"mold_gate": mold_gate,
		"overlap_count": overlap_summary["count"],
		"mold_overlap_count": mold_overlap_summary["count"],
		"consistency": consistency_gate,
	}


def sync_planning_run_to_execution(run_name: str) -> dict[str, Any]:
	return generate_work_order_proposals(run_name)


def release_planning_run(run_name: str, release_horizon_days: int | None = None) -> dict[str, Any]:
	return generate_shift_schedule_proposals(run_name=run_name, release_horizon_days=release_horizon_days)


def _assert_release_capacity_current(run_name: str, *, lock_rows: bool) -> dict[str, Any]:
	"""Keep every formal release gate bound to the last Applied live-resource state."""
	from injection_aps.services import capacity_balance

	return capacity_balance.assert_applied_capacity_current(
		run_name,
		lock_rows=lock_rows,
	)


def _rebind_release_capacity_resources(run_name: str, *, reason: str) -> dict[str, Any]:
	from injection_aps.services import capacity_balance

	return capacity_balance.rebind_applied_capacity_resources_after_release(
		run_name,
		reason=reason,
		lock_rows=True,
	)


def generate_work_order_proposals(run_name: str) -> dict[str, Any]:
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	if run_doc.approval_state != "Approved":
		frappe.throw(_("Approve the APS Planning Run before generating work order proposals."))
	consistency.assert_plan_consistent(
		run_name,
		reason="work order proposal generation",
	)
	_assert_release_capacity_current(run_name, lock_rows=True)
	mold_gate = validate_run_mold_readiness(run_name, persist_exceptions=True)
	if mold_gate["blocking_count"]:
		frappe.throw(_("Fix mold master blockers before generating work order proposals."))

	items = []
	matched_work_orders = set()
	for result in frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name, "machine_scheduled_qty": (">", 0), "status": ("!=", "Blocked")},
		fields=list(WORK_ORDER_PROPOSAL_RESULT_FIELDS),
		order_by="requested_date asc, item_code asc",
	):
		lineage = _get_result_sales_order_lineage(result)
		if lineage.get("blocking_reason"):
			frappe.throw(
				_("APS result {0} cannot be released to a Work Order: {1}").format(
					result.name,
					lineage.get("blocking_reason"),
				),
				frappe.ValidationError,
			)
		_validate_exact_sales_order_lineage(
			lineage,
			item_code=result.item_code,
			company=run_doc.company,
			customer=result.customer,
		)
		primary_segments = _get_primary_segments_for_result(result.name)
		if not primary_segments:
			continue
		preferred_segment = primary_segments[0]
		preferred_campaign_key = preferred_segment.get("campaign_key") or _build_campaign_key(
			result.item_code,
			preferred_segment.get("mould_reference"),
			preferred_segment.get("workstation"),
		)
		stock_purpose = _get_aps_work_order_stock_pool(
			demand_source=result.get("demand_source"),
			sales_order=lineage.get("sales_order"),
			sales_order_item=lineage.get("sales_order_item"),
		)
		existing = _find_existing_work_order_for_result(
			result.name,
			result.item_code,
			company=run_doc.company,
			run_name=run_doc.name,
			sales_order=lineage.get("sales_order"),
			sales_order_item=lineage.get("sales_order_item"),
			stock_purpose=stock_purpose,
			target_result=result,
			preferred_workstation=preferred_segment.get("workstation"),
			preferred_campaign_key=preferred_campaign_key,
			excluded_work_orders=sorted(matched_work_orders),
			require_unique=True,
		)
		proposed_qty = flt(result.machine_scheduled_qty)
		covered_existing_qty = _get_result_open_work_order_coverage(result)
		if covered_existing_qty > QTY_TOLERANCE:
			if not existing:
				frappe.throw(
					_(
						"APS result {0} deducted {1} from an exact existing Work Order, but that Work Order is no longer safely reusable. Rebuild Net Requirements and the Planning Run.",
						context="Injection APS",
					).format(result.name, f"{covered_existing_qty:g}"),
					frappe.ValidationError,
				)
			existing_open_qty = max(
				flt(existing.get("qty")) - flt(existing.get("produced_qty")),
				0,
			)
			if existing_open_qty + QTY_TOLERANCE < covered_existing_qty:
				frappe.throw(
					_(
						"Work Order {0} now has only {1} open quantity, below the {2} frozen into APS result {3}. Rebuild Net Requirements and the Planning Run.",
						context="Injection APS",
					).format(
						existing.get("name"),
						f"{existing_open_qty:g}",
						f"{covered_existing_qty:g}",
						result.name,
					),
					frappe.ValidationError,
				)
		if existing and (existing.get("has_execution") or existing.get("scheduling_rows")) and (
			existing.get("custom_aps_result_reference") or ""
		) != result.name:
			frappe.throw(
				_(
					"Work Order {0} has started, issued material, or formal scheduling. Link and confirm its exact APS demand manually before regenerating this proposal.",
					context="Injection APS",
				).format(existing.get("name")),
				frappe.ValidationError,
			)
		prefer_update_existing = bool(
			existing
			and (
				preferred_campaign_key in (existing.get("campaign_keys") or [])
				or preferred_segment.get("workstation") in (existing.get("workstations") or [])
			)
		)
		action = _classify_work_order_action(
			existing,
			proposed_qty,
			prefer_update_existing=prefer_update_existing,
		)
		existing_qty = 0
		existing_name = None
		if existing:
			existing_qty = flt(existing.get("qty"))
			existing_name = existing.get("name")
			matched_work_orders.add(existing_name)
		review_note = "The system generated a reconciliation proposal against the current execution layer."
		if covered_existing_qty > QTY_TOLERANCE:
			review_note = _(
				"Exact Work Order {0} contributed {1} to this plan. Reconcile that same unstarted container to the total planned boundary {2}; do not shrink it to the residual alone.",
				context="Injection APS",
			).format(existing_name or "-", f"{covered_existing_qty:g}", f"{proposed_qty:g}")
			previous_owner = (existing or {}).get("custom_aps_result_reference") or ""
			if previous_owner and previous_owner != result.name:
				review_note = "{0} {1}".format(
					review_note,
					_(
						"This proposal transfers the unstarted Work Order from changed APS result {0}; approval is the explicit ownership-transfer confirmation.",
						context="Injection APS",
					).format(previous_owner),
				)
		elif action == "Update Existing":
			review_note = "Prefer reusing existing work order {0} and update it to the new quantity boundary.".format(existing_name or "-")
		elif action == "Create Delta":
			review_note = "Keep existing work order {0} unchanged and create a delta work order for the extra quantity.".format(existing_name or "-")
		elif action == "Close Residual":
			review_note = "Keep completed quantity and close the remaining unexecuted quantity."
		elif action == "Cancel Unstarted":
			review_note = "The unstarted work order will be cancelled."
		elif action == "Keep Existing":
			review_note = "The existing work order remains the stable execution container."
		items.append(
			{
				"result_reference": result.name,
				"item_code": result.item_code,
				"customer": result.customer,
				"sales_order": lineage.get("sales_order"),
				"sales_order_item": lineage.get("sales_order_item"),
				"required_delivery_date": result.requested_date,
				"action": action,
				"proposed_qty": proposed_qty,
				"result_state_token": _work_order_result_proposal_state_token(result, primary_segments),
				"existing_work_order": existing_name,
				"existing_qty": existing_qty,
				"covered_existing_qty": covered_existing_qty,
				"existing_state_token": _work_order_proposal_state_token(existing),
				"target_start_time": primary_segments[0].get("start_time"),
				"target_end_time": primary_segments[-1].get("end_time"),
				"review_status": "Pending",
				"review_note": review_note,
			}
		)

	for snapshot in _get_open_aps_managed_work_orders(run_doc.company):
		if snapshot.get("name") in matched_work_orders:
			continue
		if not _work_order_is_orphan_candidate_for_run(snapshot, run_name):
			continue
		action = _classify_work_order_action(snapshot, 0)
		if action not in ("Cancel Unstarted", "Close Residual"):
			continue
		# This is an explicit orphan/residual proposal.  Never attach it to a
		# current or historical result by item fallback; the reviewer sees the WO's
		# own SO/SOI lineage and Apply revalidates that exact snapshot.
		result_reference = None
		result_snapshot = {}
		result_segments = []
		customer = (
			frappe.db.get_value("Sales Order", snapshot.get("sales_order"), "customer")
			if snapshot.get("sales_order")
			else None
		)
		items.append(
			{
				"result_reference": result_reference,
				"item_code": snapshot.get("production_item"),
				"customer": customer,
				"sales_order": snapshot.get("sales_order"),
				"sales_order_item": snapshot.get("sales_order_item"),
				"required_delivery_date": snapshot.get("custom_aps_required_delivery_date"),
				"action": action,
				"proposed_qty": 0,
				"result_state_token": "",
				"existing_work_order": snapshot.get("name"),
				"existing_qty": flt(snapshot.get("qty")),
				"existing_state_token": _work_order_proposal_state_token(snapshot),
				"target_start_time": snapshot.get("planned_start_date"),
				"target_end_time": snapshot.get("planned_end_date"),
				"review_status": "Pending",
				"review_note": "No current APS result owns this exact Work Order. Review its explicit SO/SOI lineage before cancelling or closing the residual.",
			}
		)

	proposal_fingerprint = _work_order_proposal_fingerprint(run_name, items)
	existing_batch = frappe.db.get_value(
		"APS Work Order Proposal Batch",
		{"proposal_fingerprint": proposal_fingerprint, "status": ("!=", "Cancelled")},
		"name",
	)
	if existing_batch:
		return _format_work_order_proposal_batch(existing_batch, idempotent_replay=True)
	batch = frappe.get_doc(
		{
			"doctype": "APS Work Order Proposal Batch",
			"planning_run": run_name,
			"company": run_doc.company,
			"plant_floor": run_doc.plant_floor,
			"proposal_date": today(),
			"proposal_fingerprint": proposal_fingerprint,
			"status": "Ready For Review",
			"approval_state": "Pending",
			"proposal_count": len(items),
			"applied_count": 0,
			"notes": _("Generated from APS Planning Run {0}. Review against existing work orders before formal reconciliation.").format(run_name),
			"items": items,
		}
	)
	batch.flags.proposal_engine_transition = True
	try:
		batch.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing_batch = frappe.db.get_value(
			"APS Work Order Proposal Batch",
			{"proposal_fingerprint": proposal_fingerprint},
			"name",
		)
		if existing_batch:
			return _format_work_order_proposal_batch(existing_batch, idempotent_replay=True)
		raise
	_set_run_result_segment_status(
		run_name=run_name,
		run_status="Work Order Proposed",
		result_status="Work Order Proposed",
		segment_status="Work Order Proposed",
		flow_step="Work Order Proposal Review",
		next_step_hint="Review and apply work order proposals",
	)
	return {"run": run_name, "work_order_proposal_batch": batch.name, "proposal_count": len(items)}


def _work_order_proposal_fingerprint(run_name: str, items: list[dict[str, Any]]) -> str:
	payload = [
		{
			"result_reference": row.get("result_reference") or "",
			"item_code": row.get("item_code") or "",
			"customer": row.get("customer") or "",
			"sales_order": row.get("sales_order") or "",
			"sales_order_item": row.get("sales_order_item") or "",
			"required_delivery_date": str(row.get("required_delivery_date") or ""),
			"action": row.get("action") or "",
			"proposed_qty": round(flt(row.get("proposed_qty")), 6),
			"result_state_token": row.get("result_state_token") or "",
			"existing_work_order": row.get("existing_work_order") or "",
			"existing_qty": round(flt(row.get("existing_qty")), 6),
			"covered_existing_qty": round(flt(row.get("covered_existing_qty")), 6),
			"existing_state_token": row.get("existing_state_token") or "",
			"target_start_time": str(row.get("target_start_time") or ""),
			"target_end_time": str(row.get("target_end_time") or ""),
		}
		for row in items or []
	]
	payload.sort(key=lambda row: (row["result_reference"], row["existing_work_order"], row["action"]))
	return _proposal_state_token({"planning_run": run_name, "items": payload})


def _format_work_order_proposal_batch(batch_name: str, *, idempotent_replay: bool = False) -> dict[str, Any]:
	batch = frappe.get_doc("APS Work Order Proposal Batch", batch_name)
	items = _get_proposal_batch_items(batch)
	return {
		"run": batch.planning_run,
		"work_order_proposal_batch": batch.name,
		"proposal_count": cint(batch.proposal_count or len(items)),
		"applied_work_orders": sorted(
			{
				row.get("target_work_order")
				for row in items
				if row.get("target_work_order") and row.get("review_status") == "Applied"
			}
		),
		"idempotent_replay": 1 if idempotent_replay else 0,
	}


def _run_atomic_batch_operation(label: str, operation):
	"""Run one formal release action as a single rollback boundary."""
	save_point = f"{label}_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(save_point)
	try:
		result = operation()
		frappe.db.release_savepoint(save_point)
		return result
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def _lock_named_rows(doctype: str, names) -> None:
	names = sorted({name for name in names or [] if name})
	if not names:
		return
	frappe.db.sql(
		f"select name from `tab{doctype}` where name in %s order by name for update",
		[tuple(names)],
	)


def _get_open_work_orders_by_result(result_names) -> dict[str, list[str]]:
	result_names = sorted({name for name in result_names or [] if name})
	grouped = defaultdict(list)
	if not result_names:
		return grouped
	for row in frappe.get_all(
		"Work Order",
		filters={
			"custom_aps_result_reference": ("in", result_names),
			"docstatus": 1,
			"status": ("not in", list(INACTIVE_WORK_ORDER_STATUSES)),
		},
		fields=["name", "custom_aps_result_reference"],
		order_by="name asc",
	):
		if row.get("name") and row.get("custom_aps_result_reference"):
			grouped[row.get("custom_aps_result_reference")].append(row.get("name"))
	return grouped


def _prepare_work_order_apply_state(batch, approved_rows: list[Any]) -> dict[str, Any]:
	result_names = sorted({row.result_reference for row in approved_rows if row.result_reference})
	_lock_named_rows("APS Schedule Result", result_names)
	# Proposal rows contain the exact lineage resolved during review.  Lock both
	# levels before re-reading/validating them so an amended SO detail cannot race
	# the formal Work Order creation inside this transaction.
	_lock_named_rows(
		"Sales Order",
		{row.sales_order for row in approved_rows if row.sales_order},
	)
	_lock_named_rows(
		"Sales Order Item",
		{row.sales_order_item for row in approved_rows if row.sales_order_item},
	)
	open_by_result = _get_open_work_orders_by_result(result_names)
	explicit_work_orders = {row.existing_work_order for row in approved_rows if row.existing_work_order}
	work_order_names = sorted(
		explicit_work_orders
		| {name for names in open_by_result.values() for name in names}
	)
	_lock_named_rows("Work Order", work_order_names)

	# Re-read after locking.  APS writers for this run are serialized by the run
	# lock, and this second read detects a row that became visible while the
	# explicit Work Order locks were being acquired.
	locked_open_by_result = _get_open_work_orders_by_result(result_names)
	new_names = {
		name
		for names in locked_open_by_result.values()
		for name in names
		if name not in work_order_names
	}
	if new_names:
		_lock_named_rows("Work Order", new_names)
		work_order_names = sorted(set(work_order_names) | new_names)
		locked_open_by_result = _get_open_work_orders_by_result(result_names)

	initial_snapshots = {
		name: _get_work_order_reconciliation_snapshot(name)
		for name in work_order_names
	}
	wos_names = {
		row.get("work_order_scheduling")
		for snapshot in initial_snapshots.values()
		for row in (snapshot or {}).get("scheduling_rows") or []
		if row.get("work_order_scheduling")
	}
	scheduling_item_names = {
		row.get("name")
		for snapshot in initial_snapshots.values()
		for row in (snapshot or {}).get("scheduling_rows") or []
		if row.get("name")
	}
	_lock_named_rows("Work Order Scheduling", wos_names)
	_lock_named_rows("Scheduling Item", scheduling_item_names)
	work_order_snapshots = {
		name: _get_work_order_reconciliation_snapshot(name)
		for name in work_order_names
	}
	for row in approved_rows:
		_assert_destructive_work_order_action_permission(row)
	return {
		"open_by_result": locked_open_by_result,
		"work_order_snapshots": work_order_snapshots,
		"exact_sales_order_lineage_locked": True,
	}


def _assert_destructive_work_order_action_permission(row) -> None:
	action = row.get("action") or ""
	work_order = row.get("existing_work_order")
	if not work_order or action not in {"Cancel Unstarted", "Close Residual"}:
		return
	permission_type = "cancel" if action == "Cancel Unstarted" else "write"
	if frappe.has_permission("Work Order", permission_type, doc=work_order):
		return
	frappe.throw(
		_("You need {0} permission on Work Order {1} before applying APS action {2}.", context="Injection APS").format(
			permission_type, work_order, action
		),
		frappe.PermissionError,
	)


def _validate_work_order_proposal_row_current(
	*,
	row,
	batch,
	run_doc,
	result_doc,
	primary_segments: list[dict[str, Any]] | None,
	apply_state: dict[str, Any],
) -> dict[str, Any]:
	action = row.action or ""
	result_name = row.result_reference or ""
	if not result_name and not result_doc and action in {"Cancel Unstarted", "Close Residual"}:
		return _validate_orphan_work_order_proposal_row(
			row=row,
			run_doc=run_doc,
			apply_state=apply_state,
		)
	if not result_doc or result_doc.get("planning_run") != batch.planning_run:
		frappe.throw(
			_("APS result {0} no longer belongs to this planning run. Regenerate the proposal batch.").format(
				result_name or "-"
			),
			frappe.ValidationError,
		)
	if (result_doc.get("item_code") or "") != (row.item_code or ""):
		frappe.throw(
			_("APS result {0} item changed after proposal review. Regenerate the proposal batch.").format(
				result_name or "-"
			),
			frappe.ValidationError,
		)
	lineage = _get_result_sales_order_lineage(result_doc)
	if lineage.get("blocking_reason"):
		frappe.throw(
			_("APS result {0} no longer has one releasable Sales Order lineage: {1}").format(
				result_name or "-",
				lineage.get("blocking_reason"),
			),
			frappe.ValidationError,
		)
	if (row.get("customer") or "") != (result_doc.get("customer") or ""):
		frappe.throw(
			_("APS result {0} customer changed after proposal review. Regenerate the proposal batch.").format(
				result_name or "-"
			),
			frappe.ValidationError,
		)
	if (row.get("sales_order") or "") != (lineage.get("sales_order") or "") or (
		row.get("sales_order_item") or ""
	) != (lineage.get("sales_order_item") or ""):
		frappe.throw(
			_("APS result {0} Sales Order lineage changed after proposal review. Regenerate the proposal batch.").format(
				result_name or "-"
			),
			frappe.ValidationError,
		)
	if apply_state.get("exact_sales_order_lineage_locked"):
		lineage = _validate_exact_sales_order_lineage(
			lineage,
			item_code=result_doc.get("item_code"),
			company=run_doc.company,
			customer=result_doc.get("customer"),
		)
	current_covered_existing_qty = _get_result_open_work_order_coverage(result_doc)
	if abs(current_covered_existing_qty - flt(row.get("covered_existing_qty"))) > QTY_TOLERANCE:
		frappe.throw(
			_(
				"APS result {0} existing Work Order coverage changed after review. Rebuild the plan and regenerate the proposal batch.",
				context="Injection APS",
			).format(result_name or "-"),
			frappe.ValidationError,
		)
	expected_result_token = row.get("result_state_token")
	if not expected_result_token or expected_result_token != _work_order_result_proposal_state_token(
		result_doc,
		primary_segments,
	):
		frappe.throw(
			_(
				"APS result {0} or its machine schedule changed after proposal review. Regenerate the proposal batch."
			).format(result_name or "-"),
			frappe.ValidationError,
		)
	if action not in {"Cancel Unstarted", "Close Residual"} and abs(
		flt(result_doc.get("machine_scheduled_qty")) - flt(row.proposed_qty)
	) > 0.0001:
		frappe.throw(
			_("APS result {0} quantity changed after proposal review. Regenerate the proposal batch.").format(
				result_name or "-"
			),
			frappe.ValidationError,
		)

	open_by_result = apply_state.get("open_by_result") or {}
	snapshots = apply_state.get("work_order_snapshots") or {}
	linked_open_names = set(open_by_result.get(result_name) or [])
	if action == "New":
		if linked_open_names:
			frappe.throw(
				_("APS result {0} already has an open Work Order ({1}) from another proposal. Regenerate the batch.").format(
					result_name or "-", ", ".join(sorted(linked_open_names))
				),
				frappe.ValidationError,
			)
		return {
			"delta_qty": flt(row.proposed_qty),
			"sales_order": lineage.get("sales_order"),
			"sales_order_item": lineage.get("sales_order_item"),
		}
	stock_purpose = _get_aps_work_order_stock_pool(
		demand_source=result_doc.get("demand_source"),
		sales_order=lineage.get("sales_order"),
		sales_order_item=lineage.get("sales_order_item"),
	)
	if not lineage.get("can_reuse") and not stock_purpose:
		frappe.throw(
			_("APS result {0} has no exact Sales Order Item and cannot reuse an existing Work Order.").format(
				result_name or "-"
			),
			frappe.ValidationError,
		)

	work_order_name = row.existing_work_order
	snapshot = snapshots.get(work_order_name)
	if not work_order_name or not snapshot:
		frappe.throw(
			_("Work Order {0} is no longer available. Regenerate the proposal batch.").format(
				work_order_name or "-"
			),
			frappe.ValidationError,
		)
	if cint(snapshot.get("docstatus")) != 1 or (snapshot.get("status") or "") in INACTIVE_WORK_ORDER_STATUSES:
		frappe.throw(
			_("Work Order {0} is no longer open. Regenerate the proposal batch.").format(work_order_name),
			frappe.ValidationError,
		)
	if snapshot.get("company") != run_doc.company or snapshot.get("production_item") != row.item_code:
		frappe.throw(
			_("Work Order {0} no longer matches the proposal company or item. Regenerate the batch.").format(
				work_order_name
			),
			frappe.ValidationError,
		)
	if (snapshot.get("sales_order") or "") != (lineage.get("sales_order") or "") or (
		snapshot.get("sales_order_item") or ""
	) != (lineage.get("sales_order_item") or ""):
		frappe.throw(
			_("Work Order {0} no longer matches the proposal Sales Order lineage. Regenerate the batch.").format(
				work_order_name
			),
			frappe.ValidationError,
		)
	if stock_purpose and (snapshot.get("custom_aps_source") or "") != stock_purpose:
		frappe.throw(
			_(
				"Work Order {0} no longer belongs to the {1} stock-production pool. Regenerate the batch.",
				context="Injection APS",
			).format(work_order_name, stock_purpose),
			frappe.ValidationError,
		)
	if not _work_order_can_be_controlled_reused(
		snapshot,
		result_name=result_name,
		run_name=batch.planning_run,
		target_result=result_doc,
	):
		frappe.throw(
			_("Work Order {0} belongs to another active APS run or result and cannot be reassigned.").format(
				work_order_name
			),
			frappe.ValidationError,
		)
	expected_token = row.get("existing_state_token")
	if not expected_token or expected_token != _work_order_proposal_state_token(snapshot):
		frappe.throw(
			_("Work Order {0} changed after proposal review (quantity, status, material, production, or scheduling). Regenerate the batch.").format(
				work_order_name
			),
			frappe.ValidationError,
		)
	current_action = _classify_work_order_action(
		snapshot,
		flt(row.proposed_qty),
		prefer_update_existing=action == "Update Existing",
	)
	if current_action != action:
		frappe.throw(
			_("Work Order {0} now requires action {1}, not {2}. Regenerate the proposal batch.").format(
				work_order_name, current_action, action
			),
			frappe.ValidationError,
		)

	if action == "Create Delta":
		current_names = linked_open_names | {work_order_name}
		current_open_qty = sum(flt((snapshots.get(name) or {}).get("qty")) for name in current_names)
		approved_delta = max(flt(row.proposed_qty) - flt(row.existing_qty), 0)
		current_delta = max(flt(row.proposed_qty) - current_open_qty, 0)
		if current_delta <= 0 or abs(current_delta - approved_delta) > 0.0001:
			frappe.throw(
				_("Delta quantity for APS result {0} changed after review. Regenerate the proposal batch.").format(
					result_name or "-"
				),
				frappe.ValidationError,
			)
		return {
			"delta_qty": current_delta,
			"sales_order": lineage.get("sales_order"),
			"sales_order_item": lineage.get("sales_order_item"),
		}
	return {
		"delta_qty": 0,
		"sales_order": lineage.get("sales_order"),
		"sales_order_item": lineage.get("sales_order_item"),
	}


def _validate_orphan_work_order_proposal_row(*, row, run_doc, apply_state: dict[str, Any]) -> dict[str, float]:
	work_order_name = row.existing_work_order
	snapshot = (apply_state.get("work_order_snapshots") or {}).get(work_order_name)
	if not work_order_name or not snapshot:
		frappe.throw(
			_("Residual Work Order {0} is no longer available. Regenerate the proposal batch.").format(
				work_order_name or "-"
			),
			frappe.ValidationError,
		)
	if cint(snapshot.get("docstatus")) != 1 or (snapshot.get("status") or "") in INACTIVE_WORK_ORDER_STATUSES:
		frappe.throw(
			_("Residual Work Order {0} is no longer open. Regenerate the proposal batch.").format(
				work_order_name
			),
			frappe.ValidationError,
		)
	if snapshot.get("company") != run_doc.company or snapshot.get("production_item") != row.item_code:
		frappe.throw(
			_("Residual Work Order {0} company or item changed after review.").format(work_order_name),
			frappe.ValidationError,
		)
	if (snapshot.get("sales_order") or "") != (row.get("sales_order") or "") or (
		snapshot.get("sales_order_item") or ""
	) != (row.get("sales_order_item") or ""):
		frappe.throw(
			_("Residual Work Order {0} Sales Order lineage changed after review.").format(work_order_name),
			frappe.ValidationError,
		)
	if row.get("sales_order"):
		customer = frappe.db.get_value("Sales Order", row.get("sales_order"), "customer")
		if (customer or "") != (row.get("customer") or ""):
			frappe.throw(
				_("Residual Work Order {0} customer lineage changed after review.").format(work_order_name),
				frappe.ValidationError,
			)
	expected_token = row.get("existing_state_token")
	if not expected_token or expected_token != _work_order_proposal_state_token(snapshot):
		frappe.throw(
			_("Residual Work Order {0} changed after review. Regenerate the proposal batch.").format(
				work_order_name
			),
			frappe.ValidationError,
		)
	current_action = _classify_work_order_action(snapshot, 0)
	if current_action != row.action:
		frappe.throw(
			_("Residual Work Order {0} now requires action {1}, not {2}.").format(
				work_order_name,
				current_action,
				row.action,
			),
			frappe.ValidationError,
		)
	return {"delta_qty": 0}


def _validate_proposal_review_is_complete(items, *, proposal_label: str) -> None:
	"""Require an explicit decision on every row before any formal write starts."""
	unresolved = [
		cint(row.get("idx")) or index
		for index, row in enumerate(items or [], start=1)
		if (row.get("review_status") or "Pending") not in {"Approved", "Rejected", "Applied"}
	]
	if unresolved:
		frappe.throw(
			_(
				"{0} proposal rows {1} still require an explicit Approved or Rejected decision. Complete the batch review before Apply."
			).format(proposal_label, ", ".join(str(value) for value in unresolved[:20])),
			frappe.ValidationError,
		)


def _get_proposal_batch_items(batch) -> list[Any]:
	items = getattr(batch, "items", None)
	if items is None or callable(items):
		getter = getattr(batch, "get", None)
		items = getter("items") if callable(getter) else None
	return list(items or [])


def _get_proposal_batch_field(batch, fieldname: str):
	getter = getattr(batch, "get", None)
	if callable(getter):
		return getter(fieldname)
	return getattr(batch, fieldname, None)


def _assert_work_order_proposal_batch_fingerprint_current(batch) -> None:
	expected = str(_get_proposal_batch_field(batch, "proposal_fingerprint") or "")
	current = _work_order_proposal_fingerprint(
		_get_proposal_batch_field(batch, "planning_run"),
		_get_proposal_batch_items(batch),
	)
	if not expected or current != expected:
		frappe.throw(
			_(
				"Work Order proposal batch {0} changed after generation. Regenerate and review the batch before Apply.",
				context="Injection APS",
			).format(_get_proposal_batch_field(batch, "name") or "-"),
			frappe.ValidationError,
		)


def apply_work_order_proposals(batch_name: str) -> dict[str, Any]:
	return _run_atomic_batch_operation(
		"aps_apply_work_order_proposals",
		lambda: _apply_work_order_proposals(batch_name),
	)


def _apply_work_order_proposals(batch_name: str) -> dict[str, Any]:
	frappe.db.sql(
		"select name from `tabAPS Work Order Proposal Batch` where name = %s for update",
		batch_name,
	)
	batch = frappe.get_doc("APS Work Order Proposal Batch", batch_name)
	if getattr(batch, "status", None) == "Applied":
		return _format_work_order_proposal_batch(batch.name, idempotent_replay=True)
	_validate_proposal_review_is_complete(
		_get_proposal_batch_items(batch),
		proposal_label="Work Order",
	)
	consistency.assert_plan_consistent(
		batch.planning_run,
		reason="work order proposal apply",
	)
	_assert_work_order_proposal_batch_fingerprint_current(batch)
	# Capacity Apply uses Company -> Planning Run ordering.  Acquire the same
	# company-scoped live-resource guard before the run row to avoid an inverse
	# lock order between capacity Apply and formal release.
	_assert_release_capacity_current(batch.planning_run, lock_rows=True)
	frappe.db.sql(
		"select name from `tabAPS Planning Run` where name = %s for update",
		batch.planning_run,
	)
	run_doc = frappe.get_doc("APS Planning Run", batch.planning_run)
	mold_gate = validate_run_mold_readiness(batch.planning_run, persist_exceptions=True)
	if mold_gate["blocking_count"]:
		frappe.throw(_("Fix mold master blockers before applying work order proposals."))
	approved_rows = [row for row in batch.items if row.review_status == "Approved"]
	if not approved_rows:
		frappe.throw(_("No work order proposal rows are marked Approved. Review the batch before formal creation."))
	approved_result_names = [row.result_reference for row in approved_rows if row.result_reference]
	approved_work_orders = [row.existing_work_order for row in approved_rows if row.existing_work_order]
	if len(approved_result_names) != len(set(approved_result_names)) or len(approved_work_orders) != len(
		set(approved_work_orders)
	):
		frappe.throw(
			_("Approved proposal rows contain duplicate APS results or Work Orders. Regenerate one unambiguous batch."),
			frappe.ValidationError,
		)
	apply_state = _prepare_work_order_apply_state(batch, approved_rows)

	settings = get_settings_dict()
	applied_work_orders = []
	applied_result_names = set()
	skipped_rows = []
	for row in approved_rows:
		result_doc = (
			frappe.get_doc("APS Schedule Result", row.result_reference)
			if row.result_reference and frappe.db.exists("APS Schedule Result", row.result_reference)
			else None
		)
		primary_segments = _get_primary_segments_for_result(result_doc.name) if result_doc else []
		start_time = row.target_start_time or (primary_segments[0].get("start_time") if primary_segments else None)
		end_time = row.target_end_time or (primary_segments[-1].get("end_time") if primary_segments else start_time)
		try:
			current_state = _validate_work_order_proposal_row_current(
				row=row,
				batch=batch,
				run_doc=run_doc,
				result_doc=result_doc,
				primary_segments=primary_segments,
				apply_state=apply_state,
			)
			if row.action == "New":
				if not result_doc:
					raise frappe.ValidationError(_("New work orders require a current APS result reference."))
				work_order_name = _create_formal_work_order(
					run_doc=run_doc,
					result=result_doc,
					qty=flt(row.proposed_qty),
					start_time=start_time,
					end_time=end_time,
					settings=settings,
					proposal_batch=batch.name,
					sales_order=current_state.get("sales_order"),
					sales_order_item=current_state.get("sales_order_item"),
				)
				row.target_work_order = work_order_name
				row.review_status = "Applied"
				row.review_note = _("Formal Work Order {0} created by APS reconciliation.").format(work_order_name)
				applied_work_orders.append(work_order_name)
				applied_result_names.add(result_doc.name)
			elif row.action == "Create Delta":
				if not result_doc:
					raise frappe.ValidationError(_("Delta work orders require a current APS result reference."))
				create_qty = flt(current_state.get("delta_qty"))
				if create_qty <= 0:
					raise frappe.ValidationError(_("No additional delta quantity is required for result {0}.").format(row.result_reference or "-"))
				work_order_name = _create_formal_work_order(
					run_doc=run_doc,
					result=result_doc,
					qty=create_qty,
					start_time=start_time,
					end_time=end_time,
					settings=settings,
					proposal_batch=batch.name,
					sales_order=current_state.get("sales_order"),
					sales_order_item=current_state.get("sales_order_item"),
				)
				row.target_work_order = work_order_name
				row.review_status = "Applied"
				row.review_note = _("Delta Work Order {0} created while preserving existing container {1}.").format(
					work_order_name,
					row.existing_work_order or "-",
				)
				applied_work_orders.append(work_order_name)
				applied_result_names.add(result_doc.name)
			elif row.action == "Keep Existing" and row.existing_work_order:
				_link_existing_work_order_to_result(
					work_order_name=row.existing_work_order,
					run_name=batch.planning_run,
					result_name=result_doc.name if result_doc else (row.result_reference or ""),
					proposal_batch=batch.name,
					required_delivery_date=row.required_delivery_date,
				)
				row.target_work_order = row.existing_work_order
				row.review_status = "Applied"
				row.review_note = _("Existing Work Order retained as the stable execution container.")
				applied_work_orders.append(row.existing_work_order)
				if result_doc:
					applied_result_names.add(result_doc.name)
			elif row.action == "Update Existing" and row.existing_work_order:
				work_order_name = _update_existing_work_order(
					work_order_name=row.existing_work_order,
					run_name=batch.planning_run,
					result_name=result_doc.name if result_doc else (row.result_reference or ""),
					proposal_batch=batch.name,
					qty=flt(row.proposed_qty),
					start_time=start_time,
					end_time=end_time,
					required_delivery_date=row.required_delivery_date,
				)
				row.target_work_order = work_order_name
				row.review_status = "Applied"
				row.review_note = _("Existing Work Order {0} updated in place.").format(work_order_name)
				applied_work_orders.append(work_order_name)
				if result_doc:
					applied_result_names.add(result_doc.name)
			elif row.action == "Cancel Unstarted" and row.existing_work_order:
				_drop_unfrozen_scheduling_rows_for_work_order(
					work_order_name=row.existing_work_order,
					planning_run=batch.planning_run,
					source_doctype="APS Work Order Proposal Batch",
					source_name=batch.name,
				)
				work_order_name = _cancel_unstarted_work_order(row.existing_work_order)
				row.target_work_order = work_order_name
				row.review_status = "Applied"
				row.review_note = _("Unstarted Work Order {0} cancelled and unreleased scheduling rows removed.").format(work_order_name)
				applied_work_orders.append(work_order_name)
			elif row.action == "Close Residual" and row.existing_work_order:
				_drop_unfrozen_scheduling_rows_for_work_order(
					work_order_name=row.existing_work_order,
					planning_run=batch.planning_run,
					source_doctype="APS Work Order Proposal Batch",
					source_name=batch.name,
				)
				work_order_name = _close_residual_work_order(
					work_order_name=row.existing_work_order,
					run_name=batch.planning_run,
					result_name=result_doc.name if result_doc else row.result_reference,
					proposal_batch=batch.name,
					required_delivery_date=row.required_delivery_date,
				)
				row.target_work_order = work_order_name
				row.review_status = "Applied"
				row.review_note = _("Residual quantity on Work Order {0} was closed.").format(work_order_name)
				applied_work_orders.append(work_order_name)
			else:
				raise frappe.ValidationError(
					_("Approved action {0} for result {1} cannot be reconciled automatically.").format(
						row.action or "-", row.result_reference or "-"
					)
				)
		except Exception as exc:
			raise frappe.ValidationError(
				_("Approved work-order proposal for result {0} failed; the complete batch was rolled back: {1}").format(
					row.result_reference or "-", str(exc)
				)
			) from exc

	batch.approved_by = frappe.session.user
	batch.approved_on = now_datetime()
	batch.flags.proposal_engine_transition = True
	batch.save(ignore_permissions=True)

	if applied_work_orders and applied_result_names:
		_set_run_result_segment_status(
			run_name=batch.planning_run,
			run_status="Work Order Proposed",
			result_status="Work Order Proposed",
			segment_status="Work Order Proposed",
			flow_step="Formal Work Orders Ready",
			next_step_hint="Generate shift schedule proposals",
			result_names=sorted(applied_result_names),
		)
	elif applied_work_orders:
		frappe.db.set_value("APS Planning Run", batch.planning_run, "status", "Work Order Proposed")
	_rebind_release_capacity_resources(
		batch.planning_run,
		reason=f"Work Order proposal batch {batch.name} applied",
	)
	return {
		"run": batch.planning_run,
		"work_order_proposal_batch": batch.name,
		"applied_work_orders": sorted(set(applied_work_orders)),
		"skipped_rows": skipped_rows,
	}


def _append_review_note(existing_notes: str | None, line: str) -> str:
	notes = (existing_notes or "").strip()
	entry = (line or "").strip()
	if not entry:
		return notes
	if not notes:
		return entry
	return f"{notes}\n{entry}"


def reject_work_order_proposals(batch_name: str, reason: str) -> dict[str, Any]:
	reason_text = (reason or "").strip()
	if not reason_text:
		frappe.throw(_("Please enter a rejection reason."))
	return _run_atomic_batch_operation(
		"aps_reject_work_order_proposals",
		lambda: _reject_work_order_proposals(batch_name, reason_text),
	)


def _reject_work_order_proposals(batch_name: str, reason_text: str) -> dict[str, Any]:
	frappe.db.sql(
		"select name from `tabAPS Work Order Proposal Batch` where name = %s for update",
		batch_name,
	)
	batch = frappe.get_doc("APS Work Order Proposal Batch", batch_name)
	frappe.db.sql(
		"select name from `tabAPS Planning Run` where name = %s for update",
		batch.planning_run,
	)
	if batch.status == "Applied" or any(row.review_status == "Applied" for row in batch.items):
		frappe.throw(
			_("Applied Work Order proposal batches or rows cannot be rejected."),
			frappe.ValidationError,
		)

	rejected_rows = 0
	for row in batch.items:
		if row.review_status in ("Applied", "Skipped", "Rejected"):
			continue
		row.review_status = "Rejected"
		row.review_note = reason_text
		rejected_rows += 1

	if not rejected_rows:
		frappe.throw(_("No pending or approved work-order proposal rows are available to reject."))

	timestamp = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	batch.notes = _append_review_note(
		batch.notes,
		_("[{0}] {1} rejected remaining work-order proposal rows: {2}").format(
			timestamp,
			frappe.session.user,
			reason_text,
		),
	)
	batch.approved_by = frappe.session.user
	batch.approved_on = now_datetime()
	batch.flags.proposal_engine_transition = True
	batch.save(ignore_permissions=True)
	return {
		"run": batch.planning_run,
		"work_order_proposal_batch": batch.name,
		"rejected_rows": rejected_rows,
	}


def generate_shift_schedule_proposals(
	run_name: str | None = None,
	work_order_proposal_batch: str | None = None,
	release_horizon_days: int | None = None,
	release_from_date=None,
	shift_type: str | None = None,
) -> dict[str, Any]:
	context = _build_shift_schedule_release_context(
		run_name=run_name,
		work_order_proposal_batch=work_order_proposal_batch,
		release_horizon_days=release_horizon_days,
		release_from_date=release_from_date,
		shift_type=shift_type,
	)
	run_doc = context["run_doc"]
	consistency.assert_plan_consistent(
		run_doc.name,
		reason="shift schedule proposal generation",
	)
	_assert_release_capacity_current(run_doc.name, lock_rows=True)
	wo_batch = context["work_order_proposal_batch_doc"]
	release_from = context["release_from"]
	release_to = context["release_to"]
	release_shift_type = context["shift_type"]
	items = context["items"]
	summary = _summarize_shift_schedule_items(items)
	proposal_fingerprint = _shift_schedule_proposal_fingerprint(
		run_name=run_doc.name,
		work_order_proposal_batch=wo_batch.name,
		release_from=release_from,
		release_to=release_to,
		shift_type=release_shift_type,
		items=items,
	)
	existing_batch = frappe.db.get_value(
		"APS Shift Schedule Proposal Batch",
		{"proposal_fingerprint": proposal_fingerprint, "status": ("!=", "Cancelled")},
		"name",
	)
	if existing_batch:
		return _format_shift_schedule_proposal_batch(existing_batch, idempotent_replay=True)

	batch = frappe.get_doc(
		{
			"doctype": "APS Shift Schedule Proposal Batch",
			"planning_run": run_doc.name,
			"company": run_doc.company,
			"plant_floor": run_doc.plant_floor,
			"work_order_proposal_batch": wo_batch.name,
			"proposal_date": today(),
			"proposal_fingerprint": proposal_fingerprint,
			"status": "Ready For Review",
			"approval_state": "Pending",
			"proposal_count": len(items),
			"notes": _(
				"Generated from APS work order proposal batch {0} for {1} to {2} ({3}). Review against existing day/night shift scheduling before formal reconciliation."
			).format(
				wo_batch.name,
				frappe.format(release_from, {"fieldtype": "Date"}),
				frappe.format(release_to, {"fieldtype": "Date"}),
				release_shift_type or _("All Shifts", context="Injection APS"),
			),
			"items": items,
		}
	)
	batch.flags.proposal_engine_transition = True
	try:
		batch.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing_batch = frappe.db.get_value(
			"APS Shift Schedule Proposal Batch",
			{"proposal_fingerprint": proposal_fingerprint},
			"name",
		)
		if existing_batch:
			return _format_shift_schedule_proposal_batch(existing_batch, idempotent_replay=True)
		raise
	proposed_result_names = sorted({row.get("result_reference") for row in items if row.get("result_reference")})
	proposed_segment_names = sorted({row.get("segment_reference") for row in items if row.get("segment_reference")})
	if proposed_result_names or proposed_segment_names:
		_set_run_result_segment_status(
			run_name=run_doc.name,
			run_status="Shift Proposed",
			result_status="Shift Proposed",
			segment_status="Shift Proposed",
			flow_step="Shift Schedule Proposal Review",
			next_step_hint="Review and apply day/night shift proposals",
			result_names=proposed_result_names,
			segment_names=proposed_segment_names,
		)
	else:
		frappe.db.set_value("APS Planning Run", run_doc.name, "status", "Shift Proposed")
	return {
		"run": run_doc.name,
		"shift_schedule_proposal_batch": batch.name,
		"proposal_count": len(items),
		"release_from": release_from,
		"release_to": release_to,
		"shift_type": release_shift_type or "All",
		"action_counts": summary["action_counts"],
		"total_planned_qty": summary["total_planned_qty"],
	}


def _shift_schedule_proposal_fingerprint(
	*,
	run_name: str,
	work_order_proposal_batch: str,
	release_from,
	release_to,
	shift_type: str | None,
	items: list[dict[str, Any]],
) -> str:
	# Date/shift filters are generation-query inputs and are not persisted on the
	# proposal batch.  The selected row dates and shifts are already fingerprinted
	# below, so excluding the transient filters makes the evidence independently
	# recomputable after the batch row has been locked for Apply.
	payload_items = [
		{
			"result_reference": row.get("result_reference") or "",
			"segment_reference": row.get("segment_reference") or "",
			"item_code": row.get("item_code") or "",
			"action": row.get("action") or "",
			"posting_date": str(row.get("posting_date") or ""),
			"shift_type": row.get("shift_type") or "",
			"plant_floor": row.get("plant_floor") or "",
			"workstation": row.get("workstation") or "",
			"work_order": row.get("work_order") or "",
			"planned_start_time": str(row.get("planned_start_time") or ""),
			"planned_end_time": str(row.get("planned_end_time") or ""),
			"planned_qty": round(flt(row.get("planned_qty")), 6),
			"existing_scheduling": row.get("existing_scheduling") or "",
			"existing_scheduling_item": row.get("existing_scheduling_item") or "",
			"work_order_state_token": row.get("work_order_state_token") or "",
			"segment_state_token": row.get("segment_state_token") or "",
			"scheduling_state_token": row.get("scheduling_state_token") or "",
		}
		for row in items or []
	]
	payload_items.sort(
		key=lambda row: (
			row["segment_reference"],
			row["existing_scheduling_item"],
			row["action"],
		)
	)
	return _proposal_state_token(
		{
			"planning_run": run_name,
			"work_order_proposal_batch": work_order_proposal_batch,
			"items": payload_items,
		}
	)


def _assert_shift_schedule_proposal_batch_fingerprint_current(batch) -> None:
	expected = str(_get_proposal_batch_field(batch, "proposal_fingerprint") or "")
	current = _shift_schedule_proposal_fingerprint(
		run_name=_get_proposal_batch_field(batch, "planning_run"),
		work_order_proposal_batch=_get_proposal_batch_field(batch, "work_order_proposal_batch"),
		release_from=None,
		release_to=None,
		shift_type=None,
		items=_get_proposal_batch_items(batch),
	)
	if not expected or current != expected:
		frappe.throw(
			_(
				"Shift schedule proposal batch {0} changed after generation. Regenerate and review the batch before Apply.",
				context="Injection APS",
			).format(_get_proposal_batch_field(batch, "name") or "-"),
			frappe.ValidationError,
		)


def _format_shift_schedule_proposal_batch(
	batch_name: str,
	*,
	idempotent_replay: bool = False,
) -> dict[str, Any]:
	batch = frappe.get_doc("APS Shift Schedule Proposal Batch", batch_name)
	items = _get_proposal_batch_items(batch)
	summary = _summarize_shift_schedule_items(items)
	dates = [getdate(row.get("posting_date")) for row in items if row.get("posting_date")]
	shift_types = {row.get("shift_type") for row in items if row.get("shift_type")}
	return {
		"run": batch.planning_run,
		"shift_schedule_proposal_batch": batch.name,
		"proposal_count": cint(batch.proposal_count or len(items)),
		"release_from": min(dates) if dates else None,
		"release_to": max(dates) if dates else None,
		"shift_type": next(iter(shift_types)) if len(shift_types) == 1 else "All",
		"action_counts": summary["action_counts"],
		"total_planned_qty": summary["total_planned_qty"],
		"release_batch": batch.get("release_batch"),
		"applied_rows": sum(1 for row in items if row.get("review_status") == "Applied"),
		"idempotent_replay": 1 if idempotent_replay else 0,
	}


def _normalize_release_shift_type(shift_type: str | None) -> str | None:
	value = (shift_type or "").strip()
	if not value or value in ("All", "Both", "All Shifts", "两班", "全部", "全部班次"):
		return None
	if value in ("白班", "Day", "Day Shift"):
		return "白班"
	if value in ("晚班", "Night", "Night Shift"):
		return "晚班"
	return value


def _build_shift_schedule_release_context(
	run_name: str | None = None,
	work_order_proposal_batch: str | None = None,
	release_horizon_days: int | None = None,
	release_from_date=None,
	shift_type: str | None = None,
) -> dict[str, Any]:
	if not work_order_proposal_batch:
		work_order_proposal_batch = frappe.db.get_value(
			"APS Work Order Proposal Batch",
			{"planning_run": run_name, "status": "Applied"},
			"name",
			order_by="modified desc",
		)
	if not work_order_proposal_batch:
		frappe.throw(_("Apply a work order proposal batch before generating shift schedule proposals."))
	wo_batch = frappe.get_doc("APS Work Order Proposal Batch", work_order_proposal_batch)
	run_doc = frappe.get_doc("APS Planning Run", wo_batch.planning_run)
	release_horizon_days = (
		cint(release_horizon_days)
		if release_horizon_days not in (None, "")
		else cint(get_settings_dict()["release_horizon_days"] or 1)
	)
	release_from = getdate(release_from_date or today())
	release_to = getdate(add_days(release_from, release_horizon_days))
	release_shift_type = _normalize_release_shift_type(shift_type)
	items = _build_shift_schedule_proposal_items(
		wo_batch=wo_batch,
		release_from=release_from,
		release_to=release_to,
		shift_type=release_shift_type,
	)
	return {
		"run_doc": run_doc,
		"work_order_proposal_batch_doc": wo_batch,
		"release_from": release_from,
		"release_to": release_to,
		"release_horizon_days": release_horizon_days,
		"shift_type": release_shift_type,
		"items": items,
	}


def _get_shift_release_work_orders(proposal_row) -> list[str]:
	"""Return every execution container that owns this Result quantity.

	A Create Delta row has two containers by design: the reviewed existing WO and
	the newly created delta WO.  Collapsing that row to ``target_work_order`` made
	the later WOS proposal put the complete Result on the smaller delta container.
	"""
	if proposal_row.action == "Create Delta":
		names = [proposal_row.existing_work_order, proposal_row.target_work_order]
	else:
		names = [proposal_row.target_work_order or proposal_row.existing_work_order]
	result = list(dict.fromkeys(name for name in names if name))
	if proposal_row.action == "Create Delta" and len(result) != 2:
		frappe.throw(
			_("Create Delta result {0} must retain both the existing and delta Work Orders.").format(
				proposal_row.result_reference or "-"
			),
			frappe.ValidationError,
		)
	return result


def _scheduling_row_is_in_release_scope(
	row: dict[str, Any],
	*,
	release_from,
	release_to,
	shift_type: str | None,
) -> bool:
	posting_date = row.get("posting_date")
	if not posting_date and (row.get("planned_start_date") or row.get("from_time")):
		posting_date = getdate(row.get("from_time") or row.get("planned_start_date"))
	if not posting_date:
		return False
	posting_date = getdate(posting_date)
	if release_from and posting_date < getdate(release_from):
		return False
	if release_to and posting_date > getdate(release_to):
		return False
	return not shift_type or (row.get("shift_type") or "") == shift_type


def _shift_slice_is_in_release_scope(
	row: dict[str, Any],
	*,
	release_from,
	release_to,
	shift_type: str | None,
) -> bool:
	posting_date = getdate(row.get("posting_date") or row.get("start_time"))
	if release_from and posting_date < getdate(release_from):
		return False
	if release_to and posting_date > getdate(release_to):
		return False
	row_shift = row.get("shift_type") or _determine_shift_type(row.get("start_time"))
	return not shift_type or row_shift == shift_type


def _consume_committed_shift_slice_qty(
	shift_slice: dict[str, Any],
	committed_qty: float,
) -> tuple[dict[str, Any] | None, float]:
	"""Remove already produced/frozen quantity once, in deterministic time order."""
	planned_qty = max(flt(shift_slice.get("planned_qty")), 0)
	committed_qty = max(flt(committed_qty), 0)
	consumed_qty = min(planned_qty, committed_qty)
	remaining_committed = max(committed_qty - consumed_qty, 0)
	if consumed_qty >= planned_qty - QTY_TOLERANCE:
		return None, remaining_committed
	if consumed_qty <= QTY_TOLERANCE:
		return dict(shift_slice), remaining_committed
	result = dict(shift_slice)
	start_time = get_datetime(result.get("start_time"))
	end_time = get_datetime(result.get("end_time"))
	if start_time and end_time and end_time > start_time:
		fraction = consumed_qty / planned_qty
		result["start_time"] = start_time + (end_time - start_time) * fraction
	result["planned_qty"] = planned_qty - consumed_qty
	return result, remaining_committed


def _consume_segment_committed_quantities(
	shift_slices: list[dict[str, Any]],
	fixed_rows: list[dict[str, Any]],
	actual_completed_qty: float,
) -> list[dict[str, Any]]:
	"""Apply formal commitments to their own shift before consuming FIFO.

	This matters when one APS segment crosses a release boundary.  A future frozen
	WOS row must not accidentally consume today's slice merely because both rows
	share the same segment reference.
	"""
	if not shift_slices:
		return []
	consumed = [0.0 for _slice in shift_slices]

	def consume(quantity: float, preferred_indexes: list[int]) -> None:
		remaining = max(flt(quantity), 0)
		indexes = preferred_indexes + [
			index for index in range(len(shift_slices)) if index not in preferred_indexes
		]
		for index in indexes:
			available = max(flt(shift_slices[index].get("planned_qty")) - consumed[index], 0)
			allocated = min(remaining, available)
			consumed[index] += allocated
			remaining = max(remaining - allocated, 0)
			if remaining <= QTY_TOLERANCE:
				break

	proven_fixed_actual_qty = 0.0
	for existing in sorted(
		fixed_rows,
		key=lambda value: (
			str(value.get("posting_date") or value.get("planned_start_date") or ""),
			str(value.get("planned_start_date") or ""),
			value.get("name") or "",
		),
	):
		quantity = max(flt(existing.get("scheduling_qty")), 0)
		# A segment-level actual can overlap a fixed WOS row only when that exact
		# Scheduling Item carries completed/defect evidence.  Merely having some
		# fixed quantity on the same segment is not proof: WO-level direct reporting
		# can belong to a different (current) slice.
		proven_fixed_actual_qty += min(
			quantity,
			max(flt(existing.get("completed_qty")), 0)
			+ max(flt(existing.get("defect_qty")), 0),
		)
		posting_date = getdate(existing.get("posting_date") or existing.get("planned_start_date"))
		row_shift = existing.get("shift_type") or (
			_determine_shift_type(existing.get("planned_start_date"))
			if existing.get("planned_start_date")
			else ""
		)
		preferred = [
			index
			for index, shift_slice in enumerate(shift_slices)
			if getdate(shift_slice.get("posting_date") or shift_slice.get("start_time")) == posting_date
			and (shift_slice.get("shift_type") or _determine_shift_type(shift_slice.get("start_time")))
			== row_shift
		]
		consume(quantity, preferred)
	# Deduplicate only the actual quantity proven on those exact fixed rows. Any
	# WO-level/direct actual without that evidence is an additional commitment.
	proven_fixed_actual_qty = min(
		max(flt(actual_completed_qty), 0),
		proven_fixed_actual_qty,
	)
	consume(
		max(flt(actual_completed_qty) - proven_fixed_actual_qty, 0),
		list(range(len(shift_slices))),
	)

	result = []
	for shift_slice, consumed_qty in zip(shift_slices, consumed):
		remaining_slice, _unused = _consume_committed_shift_slice_qty(shift_slice, consumed_qty)
		if remaining_slice:
			result.append(remaining_slice)
	return result


def _preferred_work_orders_for_shift_slice(
	segment: dict[str, Any],
	work_order_names: list[str],
	scope_rows: dict[str, list[dict[str, Any]]],
	matched_existing_rows: dict[str, set[str]],
) -> list[str]:
	target_date = getdate(segment.get("posting_date") or segment.get("start_time"))
	target_shift = segment.get("shift_type") or _determine_shift_type(segment.get("start_time"))
	preferred = []
	for work_order_name in work_order_names:
		if any(
			not existing.get("is_frozen")
			and existing.get("name") not in matched_existing_rows[work_order_name]
			and existing.get("custom_aps_segment_reference") == segment.get("name")
			and getdate(existing.get("posting_date") or existing.get("planned_start_date")) == target_date
			and (existing.get("shift_type") or "") == target_shift
			for existing in scope_rows.get(work_order_name) or []
		):
			preferred.append(work_order_name)
	return preferred + [name for name in work_order_names if name not in preferred]


def _allocate_shift_slice_to_work_orders(
	segment: dict[str, Any],
	work_order_names: list[str],
	remaining_capacity: dict[str, float],
) -> list[tuple[str, dict[str, Any]]]:
	"""Split one physical slice across explicit WOs without losing quantity."""
	planned_qty = max(flt(segment.get("planned_qty")), 0)
	remaining_qty = planned_qty
	quantities: list[tuple[str, float]] = []
	for work_order_name in work_order_names:
		available_qty = max(flt(remaining_capacity.get(work_order_name)), 0)
		allocated_qty = min(remaining_qty, available_qty)
		if allocated_qty <= QTY_TOLERANCE:
			continue
		quantities.append((work_order_name, allocated_qty))
		remaining_capacity[work_order_name] = max(available_qty - allocated_qty, 0)
		remaining_qty = max(remaining_qty - allocated_qty, 0)
		if remaining_qty <= QTY_TOLERANCE:
			break
	if remaining_qty > QTY_TOLERANCE:
		frappe.throw(
			_("APS segment {0} has {1} unscheduled quantity after applying the exact Work Order boundaries. Regenerate the Work Order proposal.", context="Injection APS").format(
				segment.get("name") or "-",
				frappe.format(remaining_qty, {"fieldtype": "Float"}),
			),
			frappe.ValidationError,
		)
	if not quantities:
		return []
	start_time = get_datetime(segment.get("start_time")) if segment.get("start_time") else None
	end_time = get_datetime(segment.get("end_time")) if segment.get("end_time") else None
	allocated = []
	cumulative_qty = 0.0
	for index, (work_order_name, allocated_qty) in enumerate(quantities, start=1):
		piece = dict(segment)
		piece["planned_qty"] = allocated_qty
		if start_time and end_time and end_time > start_time and planned_qty > QTY_TOLERANCE:
			piece["start_time"] = start_time + (end_time - start_time) * (cumulative_qty / planned_qty)
			cumulative_qty += allocated_qty
			piece["end_time"] = (
				end_time
				if index == len(quantities)
				else start_time + (end_time - start_time) * (cumulative_qty / planned_qty)
			)
		piece["work_order_split_index"] = index
		piece["work_order_split_count"] = len(quantities)
		allocated.append((work_order_name, piece))
	return allocated


def _build_shift_schedule_proposal_items(
	wo_batch,
	release_from,
	release_to,
	shift_type: str | None = None,
) -> list[dict[str, Any]]:
	items = []
	for row in wo_batch.items:
		if row.review_status != "Applied":
			continue
		work_order_names = _get_shift_release_work_orders(row)
		if not work_order_names:
			continue
		snapshots = {
			name: _get_work_order_reconciliation_snapshot(name)
			for name in work_order_names
		}
		state_tokens = {
			name: _work_order_proposal_state_token(snapshots.get(name))
			for name in work_order_names
		}
		scope_rows: dict[str, list[dict[str, Any]]] = {}
		matched_existing_rows: dict[str, set[str]] = defaultdict(set)
		remaining_capacity: dict[str, float] = {}
		fixed_rows_by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
		for work_order_name in work_order_names:
			snapshot = snapshots.get(work_order_name)
			if not snapshot:
				frappe.throw(
					_("Work Order {0} is no longer available for shift proposal generation.").format(
						work_order_name
					),
					frappe.ValidationError,
				)
			all_rows = [dict(existing) for existing in snapshot.get("scheduling_rows") or []]
			current_scope_rows = [
				existing
				for existing in all_rows
				if _scheduling_row_is_in_release_scope(
					existing,
					release_from=release_from,
					release_to=release_to,
					shift_type=shift_type,
				)
			]
			for existing in current_scope_rows:
				existing["is_frozen"] = 1 if _is_frozen_scheduling_row(existing) else 0
			scope_rows[work_order_name] = current_scope_rows
			fixed_rows = [
				existing
				for existing in all_rows
				if _is_frozen_scheduling_row(existing)
				or not _scheduling_row_is_in_release_scope(
					existing,
					release_from=release_from,
					release_to=release_to,
					shift_type=shift_type,
				)
			]
			fixed_scheduling_qty = sum(max(flt(existing.get("scheduling_qty")), 0) for existing in fixed_rows)
			# ERPNext Work Order.produced_qty is good finished output.  Only the exact
			# Scheduling Item completed_qty can prove overlap; defect_qty consumes a
			# segment but must never hide separate good production at WO level.
			proven_fixed_actual_qty = min(
				max(flt(snapshot.get("produced_qty")), 0),
				sum(
					min(
						max(flt(existing.get("scheduling_qty")), 0),
						max(flt(existing.get("completed_qty")), 0),
					)
					for existing in fixed_rows
				),
			)
			direct_produced_qty = max(
				max(flt(snapshot.get("produced_qty")), 0) - proven_fixed_actual_qty,
				0,
			)
			remaining_capacity[work_order_name] = max(
				flt(snapshot.get("qty"))
				- fixed_scheduling_qty
				- direct_produced_qty,
				0,
			)
			for existing in fixed_rows:
				segment_name = existing.get("custom_aps_segment_reference")
				if segment_name:
					fixed_rows_by_segment[segment_name].append(existing)

		current_segments = []
		if (
			row.action not in ("Cancel Unstarted", "Close Residual")
			and row.result_reference
			and frappe.db.exists("APS Schedule Result", row.result_reference)
		):
			for source_segment in _get_primary_segments_for_result(row.result_reference):
				segment = dict(source_segment)
				segment["item_code"] = row.item_code
				segment["proposal_state_token"] = _segment_proposal_state_token(segment)
				shift_slices = _consume_segment_committed_quantities(
					_split_segment_into_shift_slices(segment),
					fixed_rows_by_segment.get(segment.get("name")) or [],
					max(flt(segment.get("actual_completed_qty")), 0),
				)
				for shift_slice in shift_slices:
					if not _shift_slice_is_in_release_scope(
						shift_slice,
						release_from=release_from,
						release_to=release_to,
						shift_type=shift_type,
					):
						continue
					current_segments.append(shift_slice)

		visible_qty = sum(flt(segment.get("planned_qty")) for segment in current_segments)
		allocated_qty = 0.0
		for segment in current_segments:
			preferred_work_orders = _preferred_work_orders_for_shift_slice(
				segment,
				work_order_names,
				scope_rows,
				matched_existing_rows,
			)
			allocations = _allocate_shift_slice_to_work_orders(
				segment,
				preferred_work_orders,
				remaining_capacity,
			)
			for work_order_name, allocated_segment in allocations:
				existing_row = _find_matching_scheduling_row(
					work_order_name=work_order_name,
					segment=allocated_segment,
					matched_row_names=matched_existing_rows[work_order_name],
					release_from=release_from,
					release_to=release_to,
				)
				if existing_row:
					matched_existing_rows[work_order_name].add(existing_row.get("name"))
				action = _classify_shift_schedule_action(existing_row, allocated_segment)
				allocated_qty += flt(allocated_segment.get("planned_qty"))
				items.append(
					{
						"result_reference": row.result_reference,
						"segment_reference": allocated_segment.get("name"),
						"action": action,
						"item_code": row.item_code,
						"work_order": work_order_name,
						"plant_floor": allocated_segment.get("plant_floor"),
						"posting_date": allocated_segment.get("posting_date") or getdate(allocated_segment.get("start_time")),
						"shift_type": allocated_segment.get("shift_type") or _determine_shift_type(allocated_segment.get("start_time")),
						"workstation": allocated_segment.get("workstation"),
						"planned_start_time": allocated_segment.get("start_time"),
						"planned_end_time": allocated_segment.get("end_time"),
						"planned_qty": allocated_segment.get("planned_qty"),
						"existing_scheduling": existing_row.get("work_order_scheduling") if existing_row else None,
						"existing_scheduling_item": existing_row.get("name") if existing_row else None,
						"work_order_state_token": state_tokens.get(work_order_name) or "",
						"segment_state_token": allocated_segment.get("proposal_state_token") or "",
						"scheduling_state_token": _scheduling_row_proposal_state_token(existing_row),
						"review_status": "Pending",
						"review_note": _("APS allocated this segment quantity to Work Order {0}; split Work Orders remain quantity-conserving and separately auditable.", context="Injection APS").format(work_order_name),
					}
				)
		if abs(allocated_qty - visible_qty) > QTY_TOLERANCE:
			frappe.throw(
				_("Shift proposal quantities for APS result {0} do not conserve the visible segment total.").format(
					row.result_reference or "-"
				),
				frappe.ValidationError,
			)

		for work_order_name in work_order_names:
			for existing_row in scope_rows.get(work_order_name) or []:
				if existing_row.get("name") in matched_existing_rows[work_order_name] or existing_row.get("is_frozen"):
					continue
				if row.action in ("Cancel Unstarted", "Close Residual") or existing_row.get("custom_aps_segment_reference"):
					segment_snapshot = _get_segment_proposal_snapshot(existing_row.get("custom_aps_segment_reference"))
					items.append(
						{
							"result_reference": row.result_reference or existing_row.get("custom_aps_result_reference"),
							"segment_reference": existing_row.get("custom_aps_segment_reference") or f"cancel::{existing_row.get('name')}",
							"action": "Cancel Existing",
							"item_code": row.item_code,
							"work_order": work_order_name,
							"plant_floor": existing_row.get("plant_floor"),
							"posting_date": existing_row.get("posting_date"),
							"shift_type": existing_row.get("shift_type"),
							"workstation": existing_row.get("workstation"),
							"planned_start_time": existing_row.get("planned_start_date"),
							"planned_end_time": existing_row.get("planned_end_date"),
							"planned_qty": existing_row.get("scheduling_qty"),
							"existing_scheduling": existing_row.get("work_order_scheduling"),
							"existing_scheduling_item": existing_row.get("name"),
							"work_order_state_token": state_tokens.get(work_order_name) or "",
							"segment_state_token": _segment_proposal_state_token(segment_snapshot),
							"scheduling_state_token": _scheduling_row_proposal_state_token(existing_row),
							"review_status": "Pending",
							"review_note": _("Unexecuted formal scheduling row will be cancelled or removed."),
						}
					)
	return items


def _summarize_shift_schedule_items(items: list[dict[str, Any]]) -> dict[str, Any]:
	action_counts = defaultdict(int)
	total_planned_qty = 0.0
	for row in items or []:
		action_counts[row.get("action") or ""] += 1
		if row.get("action") != "Cancel Existing":
			total_planned_qty += flt(row.get("planned_qty"))
	return {"action_counts": dict(action_counts), "total_planned_qty": total_planned_qty}


def preview_shift_schedule_release(
	run_name: str | None = None,
	work_order_proposal_batch: str | None = None,
	release_horizon_days: int | None = None,
	release_from_date=None,
	shift_type: str | None = None,
) -> dict[str, Any]:
	context = _build_shift_schedule_release_context(
		run_name=run_name,
		work_order_proposal_batch=work_order_proposal_batch,
		release_horizon_days=release_horizon_days,
		release_from_date=release_from_date,
		shift_type=shift_type,
	)
	items = context["items"]
	summary = _summarize_shift_schedule_items(items)
	pending_batches = _get_pending_shift_batches_for_release(
		context["run_doc"].name,
		context["release_from"],
		context["release_to"],
		context["shift_type"],
	)
	return {
		"run": context["run_doc"].name,
		"work_order_proposal_batch": context["work_order_proposal_batch_doc"].name,
		"release_from": context["release_from"],
		"release_to": context["release_to"],
		"shift_type": context["shift_type"] or "All",
		"proposal_count": len(items),
		"action_counts": summary["action_counts"],
		"total_planned_qty": summary["total_planned_qty"],
		"pending_batches": pending_batches,
		"preview_rows": items[:80],
		"truncated": len(items) > 80,
	}


def _get_pending_shift_batches_for_release(run_name: str, release_from, release_to, shift_type: str | None = None) -> list[dict[str, Any]]:
	candidate_batches = frappe.get_all(
		"APS Shift Schedule Proposal Batch",
		filters={
			"planning_run": run_name,
			"status": ("in", ["Ready For Review", "Partially Reviewed", "Reviewed"]),
		},
		fields=["name", "status", "proposal_count", "modified"],
		order_by="modified desc",
		limit=20,
	)
	if not candidate_batches:
		return []
	batch_names = [row.name for row in candidate_batches]
	item_filters = {
		"parent": ("in", batch_names),
		"posting_date": ("between", [release_from, release_to]),
	}
	if shift_type:
		item_filters["shift_type"] = shift_type
	child_rows = frappe.get_all(
		"APS Shift Schedule Proposal Item",
		filters=item_filters,
		fields=["parent"],
	)
	count_by_parent = defaultdict(int)
	for row in child_rows:
		count_by_parent[row.parent] += 1
	return [
		{**row, "matching_count": count_by_parent.get(row.name, 0)}
		for row in candidate_batches
		if count_by_parent.get(row.name, 0)
	]


def _validate_shift_proposal_work_order_totals(batch, approved_rows: list[Any]):
	by_work_order: dict[str, list[Any]] = defaultdict(list)
	for row in approved_rows:
		if row.work_order:
			by_work_order[row.work_order].append(row)
	for work_order_name, rows in by_work_order.items():
		work_order = frappe.db.get_value(
			"Work Order",
			work_order_name,
			["qty", "produced_qty"],
			as_dict=True,
		) or {}
		if not hasattr(work_order, "get"):
			# Keep the guard compatible with lightweight/custom database adapters that
			# return the first requested value even when ``as_dict`` is supplied.
			work_order = {"qty": work_order, "produced_qty": 0}
		work_order_qty = flt(work_order.get("qty"))
		if work_order_qty <= 0:
			continue
		touched_items = {
			row.existing_scheduling_item
			for row in rows
			if row.existing_scheduling_item
		}
		remaining_existing_rows = []
		for existing in _get_formal_scheduling_reconciliation_rows(work_order_name):
			if existing.get("name") in touched_items and not _is_frozen_scheduling_row(existing):
				continue
			remaining_existing_rows.append(existing)
		remaining_existing_qty = sum(
			max(flt(existing.get("scheduling_qty")), 0)
			for existing in remaining_existing_rows
		)
		# Same semantic boundary as proposal generation: produced_qty can overlap
		# completed_qty on the same retained WOS item, never its defect quantity.
		proven_existing_actual_qty = min(
			max(flt(work_order.get("produced_qty")), 0),
			sum(
				min(
					max(flt(existing.get("scheduling_qty")), 0),
					max(flt(existing.get("completed_qty")), 0),
				)
				for existing in remaining_existing_rows
			),
		)
		direct_produced_qty = max(
			max(flt(work_order.get("produced_qty")), 0) - proven_existing_actual_qty,
			0,
		)
		approved_qty = sum(flt(row.planned_qty) for row in rows if row.action != "Cancel Existing")
		total_qty = remaining_existing_qty + approved_qty + direct_produced_qty
		if total_qty > work_order_qty + 0.0001:
			frappe.throw(
				_("WOS proposals and non-overlapping production for Work Order {0} would commit {1}, exceeding Work Order qty {2}.", context="Injection APS").format(
					work_order_name,
					frappe.format(total_qty, {"fieldtype": "Float"}),
					frappe.format(work_order_qty, {"fieldtype": "Float"}),
				)
			)


def _get_shift_target_wos_names(batch, approved_rows: list[Any]) -> set[str]:
	target_keys = {
		(
			batch.company,
			row.plant_floor or batch.plant_floor or "",
			str(getdate(row.posting_date)),
			row.shift_type or "",
		)
		for row in approved_rows
		if row.posting_date
	}
	if not target_keys:
		return set()
	dates = sorted({key[2] for key in target_keys})
	rows = frappe.get_all(
		"Work Order Scheduling",
		filters={"company": batch.company, "posting_date": ("in", dates)},
		fields=["name", "company", "plant_floor", "posting_date", "shift_type"],
	)
	return {
		row.get("name")
		for row in rows
		if row.get("name")
		and (
			row.get("company"),
			row.get("plant_floor") or "",
			str(getdate(row.get("posting_date"))),
			row.get("shift_type") or "",
		) in target_keys
	}


def _prepare_shift_apply_state(batch, approved_rows: list[Any]) -> dict[str, Any]:
	result_names = sorted({row.result_reference for row in approved_rows if row.result_reference})
	segment_names = sorted(
		{
			row.segment_reference
			for row in approved_rows
			if row.segment_reference and not str(row.segment_reference).startswith("cancel::")
		}
	)
	work_order_names = sorted({row.work_order for row in approved_rows if row.work_order})
	_lock_named_rows("APS Schedule Result", result_names)
	_lock_named_rows("APS Schedule Segment", segment_names)
	_lock_named_rows("Work Order", work_order_names)

	initial_snapshots = {
		name: _get_work_order_reconciliation_snapshot(name)
		for name in work_order_names
	}
	wos_names = {
		row.existing_scheduling
		for row in approved_rows
		if row.existing_scheduling
	} | _get_shift_target_wos_names(batch, approved_rows)
	wos_names.update(
		row.get("work_order_scheduling")
		for snapshot in initial_snapshots.values()
		for row in (snapshot or {}).get("scheduling_rows") or []
		if row.get("work_order_scheduling")
	)
	_lock_named_rows("Work Order Scheduling", wos_names)
	locked_target_names = _get_shift_target_wos_names(batch, approved_rows)
	new_target_names = locked_target_names - wos_names
	if new_target_names:
		_lock_named_rows("Work Order Scheduling", new_target_names)
		wos_names.update(new_target_names)
	scheduling_item_names = {
		row.existing_scheduling_item
		for row in approved_rows
		if row.existing_scheduling_item
	} | {
		row.get("name")
		for snapshot in initial_snapshots.values()
		for row in (snapshot or {}).get("scheduling_rows") or []
		if row.get("name")
	}
	_lock_named_rows("Scheduling Item", scheduling_item_names)

	work_order_snapshots = {
		name: _get_work_order_reconciliation_snapshot(name)
		for name in work_order_names
	}
	scheduling_rows = {
		row.get("name"): row
		for snapshot in work_order_snapshots.values()
		for row in (snapshot or {}).get("scheduling_rows") or []
		if row.get("name")
	}
	segment_snapshots = {
		name: _get_segment_proposal_snapshot(name)
		for name in segment_names
	}
	return {
		"work_order_snapshots": work_order_snapshots,
		"scheduling_rows": scheduling_rows,
		"segment_snapshots": segment_snapshots,
		"work_order_schedulings": sorted(wos_names),
	}


def _validate_shift_proposal_row_current(*, row, batch, apply_state: dict[str, Any]) -> None:
	work_order_snapshot = (apply_state.get("work_order_snapshots") or {}).get(row.work_order)
	if not work_order_snapshot:
		frappe.throw(
			_("Work Order {0} is no longer available for shift scheduling. Regenerate the batch.").format(
				row.work_order or "-"
			),
			frappe.ValidationError,
		)
	if (
		cint(work_order_snapshot.get("docstatus")) != 1
		or (work_order_snapshot.get("status") or "") in INACTIVE_WORK_ORDER_STATUSES
	):
		frappe.throw(
			_("Work Order {0} is no longer open for shift scheduling. Regenerate the batch.").format(
				row.work_order or "-"
			),
			frappe.ValidationError,
		)
	if work_order_snapshot.get("company") != batch.company or work_order_snapshot.get("production_item") != row.item_code:
		frappe.throw(
			_("Work Order {0} no longer matches the shift proposal company or item. Regenerate the batch.").format(
				row.work_order or "-"
			),
			frappe.ValidationError,
		)
	expected_work_order_token = row.get("work_order_state_token")
	if not expected_work_order_token or expected_work_order_token != _work_order_proposal_state_token(
		work_order_snapshot
	):
		frappe.throw(
			_("Work Order {0} changed after shift proposal review. Regenerate the batch.").format(
				row.work_order or "-"
			),
			frappe.ValidationError,
		)

	segment_name = row.segment_reference
	segment_snapshot = (apply_state.get("segment_snapshots") or {}).get(segment_name)
	expected_segment_token = row.get("segment_state_token") or ""
	if row.action != "Cancel Existing":
		if not segment_snapshot or not expected_segment_token or expected_segment_token != _segment_proposal_state_token(
			segment_snapshot
		):
			frappe.throw(
				_("APS segment {0} changed after shift proposal review. Regenerate the batch.").format(
					segment_name or "-"
				),
				frappe.ValidationError,
			)
		if segment_snapshot.get("parent") != row.result_reference or (
			segment_snapshot.get("segment_status") or ""
		) in consistency.INACTIVE_SEGMENT_STATUSES:
			frappe.throw(
				_("APS segment {0} is no longer active for this result. Regenerate the batch.").format(
					segment_name or "-"
				),
				frappe.ValidationError,
			)
	elif expected_segment_token and (
		not segment_snapshot or expected_segment_token != _segment_proposal_state_token(segment_snapshot)
	):
		frappe.throw(
			_("APS segment {0} changed after cancellation review. Regenerate the batch.").format(
				segment_name or "-"
			),
			frappe.ValidationError,
		)

	scheduling_rows = apply_state.get("scheduling_rows") or {}
	current_scheduling_row = scheduling_rows.get(row.existing_scheduling_item)
	expected_scheduling_token = row.get("scheduling_state_token") or ""
	if row.existing_scheduling_item:
		if not current_scheduling_row or not expected_scheduling_token or expected_scheduling_token != _scheduling_row_proposal_state_token(
			current_scheduling_row
		):
			frappe.throw(
				_("Scheduling Item {0} changed after review. Regenerate the shift proposal batch.").format(
					row.existing_scheduling_item
				),
				frappe.ValidationError,
			)
		if _is_frozen_scheduling_row(current_scheduling_row):
			frappe.throw(
				_("Scheduling Item {0} entered execution after review. Regenerate the shift proposal batch.").format(
					row.existing_scheduling_item
				),
				frappe.ValidationError,
			)
	elif row.action != "New":
		frappe.throw(
			_("Shift proposal action {0} no longer has an existing scheduling row. Regenerate the batch.").format(
				row.action or "-"
			),
			frappe.ValidationError,
		)

	if row.action not in {"New", "Cancel Existing"}:
		current_action = _classify_shift_schedule_action(
			current_scheduling_row,
			{
				"name": row.segment_reference,
				"posting_date": row.posting_date,
				"shift_type": row.shift_type,
				"plant_floor": row.plant_floor or batch.plant_floor,
				"workstation": row.workstation,
				"start_time": row.planned_start_time,
				"end_time": row.planned_end_time,
				"planned_qty": row.planned_qty,
			},
		)
		if current_action != row.action:
			frappe.throw(
				_("Shift proposal for segment {0} now requires action {1}, not {2}. Regenerate the batch.").format(
					row.segment_reference or "-", current_action, row.action or "-"
				),
				frappe.ValidationError,
			)
	if row.action == "New":
		for current in (work_order_snapshot.get("scheduling_rows") or []):
			if current.get("custom_aps_segment_reference") != row.segment_reference:
				continue
			if get_datetime(current.get("planned_start_date") or row.planned_start_time) == get_datetime(
				row.planned_start_time
			) and get_datetime(current.get("planned_end_date") or row.planned_end_time) == get_datetime(
				row.planned_end_time
			):
				frappe.throw(
					_("Segment {0} already has a formal scheduling row from another proposal. Regenerate the batch.").format(
						row.segment_reference or "-"
					),
					frappe.ValidationError,
				)


def apply_shift_schedule_proposals(batch_name: str) -> dict[str, Any]:
	return _run_atomic_batch_operation(
		"aps_apply_shift_schedule_proposals",
		lambda: _apply_shift_schedule_proposals(batch_name),
	)


def _apply_shift_schedule_proposals(batch_name: str) -> dict[str, Any]:
	frappe.db.sql(
		"select name from `tabAPS Shift Schedule Proposal Batch` where name = %s for update",
		batch_name,
	)
	batch = frappe.get_doc("APS Shift Schedule Proposal Batch", batch_name)
	if getattr(batch, "status", None) == "Applied":
		return _format_shift_schedule_proposal_batch(batch.name, idempotent_replay=True)
	_validate_proposal_review_is_complete(
		_get_proposal_batch_items(batch),
		proposal_label="Shift Schedule",
	)
	consistency.assert_plan_consistent(
		batch.planning_run,
		reason="formal shift schedule release",
	)
	_assert_shift_schedule_proposal_batch_fingerprint_current(batch)
	_assert_release_capacity_current(batch.planning_run, lock_rows=True)
	frappe.db.sql(
		"select name from `tabAPS Planning Run` where name = %s for update",
		batch.planning_run,
	)
	overlap_summary = _validate_run_segment_overlaps(batch.planning_run, persist_exceptions=True)
	mold_overlap_summary = _validate_run_mold_overlaps(batch.planning_run, persist_exceptions=True)
	mold_gate = validate_run_mold_readiness(batch.planning_run, persist_exceptions=True)
	if overlap_summary["count"] or mold_overlap_summary["count"] or mold_gate["blocking_count"]:
		frappe.throw(_("Resolve overlap and mold blockers before applying shift schedule proposals."))
	approved_rows = [row for row in batch.items if row.review_status == "Approved"]
	if not approved_rows:
		frappe.throw(_("No shift proposal rows are marked Approved. Review the batch before formal scheduling."))
	existing_scheduling_items = [
		row.existing_scheduling_item for row in approved_rows if row.existing_scheduling_item
	]
	if len(existing_scheduling_items) != len(set(existing_scheduling_items)):
		frappe.throw(
			_("Approved shift proposals contain the same Scheduling Item more than once. Regenerate one unambiguous batch."),
			frappe.ValidationError,
		)
	apply_state = _prepare_shift_apply_state(batch, approved_rows)
	for row in approved_rows:
		_validate_shift_proposal_row_current(row=row, batch=batch, apply_state=apply_state)
	_validate_shift_proposal_work_order_totals(batch, approved_rows)

	grouped = defaultdict(list)
	for row in approved_rows:
		grouped[(str(row.posting_date), row.shift_type or "", row.workstation or "", row.work_order or "")].append(row)

	scheduling_docs = set()
	scheduling_item_names = set()
	applied_rows = 0
	applied_result_names = set()
	applied_segment_names = set()
	for row in approved_rows:
		try:
			scheduling = _upsert_formal_shift_scheduling(batch, row)
			row.target_scheduling = scheduling.get("docname")
			row.review_status = "Applied"
			row.review_note = scheduling.get("message") or _("Applied to formal Work Order Scheduling {0}.").format(scheduling.get("docname"))
			applied_rows += 1
			if row.result_reference:
				applied_result_names.add(row.result_reference)
			if row.segment_reference:
				applied_segment_names.add(row.segment_reference)
			if scheduling.get("docname"):
				scheduling_docs.add(scheduling["docname"])
			if scheduling.get("scheduling_item"):
				scheduling_item_names.add(scheduling["scheduling_item"])
		except Exception as exc:
			raise frappe.ValidationError(
				_("Approved shift proposal for segment {0} failed; the complete batch was rolled back: {1}").format(
					row.segment_reference or "-", str(exc)
				)
			) from exc

	batch.approved_by = frappe.session.user
	batch.approved_on = now_datetime()
	batch.flags.proposal_engine_transition = True
	batch.save(ignore_permissions=True)

	released_wos_rows = _build_release_batch_wos_rows(scheduling_docs)
	release_batch = frappe.get_doc(
		{
			"doctype": "APS Release Batch",
			"planning_run": batch.planning_run,
			"company": batch.company,
			"release_from_date": min((getdate(row.posting_date) for row in batch.items), default=getdate(today())),
			"release_to_date": max((getdate(row.posting_date) for row in batch.items), default=getdate(today())),
			"status": "Released" if applied_rows else "Draft",
			"generated_work_orders": len(
				{
					row.work_order
					for row in batch.items
					if row.review_status == "Applied" and row.work_order
				}
			),
			"work_order_scheduling": sorted(scheduling_docs)[0] if len(scheduling_docs) == 1 else None,
			"released_work_order_schedulings": released_wos_rows,
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value(
		"APS Shift Schedule Proposal Batch",
		batch.name,
		"release_batch",
		release_batch.name,
		update_modified=False,
	)
	_backlink_release_batch_to_wos(
		release_batch.name,
		scheduling_docs=scheduling_docs,
		scheduling_item_names=scheduling_item_names,
	)

	if applied_rows and (applied_result_names or applied_segment_names):
		_set_run_result_segment_status(
			run_name=batch.planning_run,
			run_status="Applied",
			result_status="Applied",
			segment_status="Applied",
			flow_step="Formal Schedule Applied",
			next_step_hint="Monitor execution drift and exceptions",
			result_names=sorted(applied_result_names),
			segment_names=sorted(applied_segment_names),
		)
	elif applied_rows:
		frappe.db.set_value("APS Planning Run", batch.planning_run, "status", "Applied")
	_rebind_release_capacity_resources(
		batch.planning_run,
		reason=f"Shift schedule proposal batch {batch.name} applied",
	)
	return {
		"run": batch.planning_run,
		"shift_schedule_proposal_batch": batch.name,
		"release_batch": release_batch.name,
		"work_order_schedulings": sorted(scheduling_docs),
		"applied_rows": applied_rows,
	}


def _build_release_batch_wos_rows(scheduling_docs) -> list[dict[str, Any]]:
	rows = []
	for docname in sorted(set(scheduling_docs or [])):
		if not docname or not frappe.db.exists("Work Order Scheduling", docname):
			continue
		doc = frappe.get_doc("Work Order Scheduling", docname)
		scheduling_items = doc.get("scheduling_items") or []
		rows.append(
			{
				"work_order_scheduling": doc.name,
				"posting_date": doc.get("posting_date"),
				"shift_type": doc.get("shift_type"),
				"total_qty": flt(doc.get("total_qty")) or sum(flt(row.get("scheduling_qty")) for row in scheduling_items),
				"scheduling_item_count": len(scheduling_items),
				"status": doc.get("status"),
			}
		)
	return sorted(rows, key=lambda row: (str(row.get("posting_date") or ""), row.get("shift_type") or "", row.get("work_order_scheduling") or ""))


def _backlink_release_batch_to_wos(
	release_batch_name: str,
	scheduling_docs=None,
	scheduling_item_names=None,
) -> None:
	if not release_batch_name:
		return
	scheduling_docs = sorted(set(scheduling_docs or []))
	scheduling_item_names = sorted(set(scheduling_item_names or []))
	if scheduling_docs and frappe.db.has_column("Work Order Scheduling", "custom_aps_release_batch"):
		for docname in scheduling_docs:
			if frappe.db.exists("Work Order Scheduling", docname):
				frappe.db.set_value(
					"Work Order Scheduling",
					docname,
					"custom_aps_release_batch",
					release_batch_name,
					update_modified=False,
				)
	if scheduling_item_names and frappe.db.has_column("Scheduling Item", "custom_aps_release_batch"):
		frappe.db.sql(
			"""
			update `tabScheduling Item`
			set custom_aps_release_batch = %(release_batch)s
			where name in %(scheduling_item_names)s
			""",
			{
				"release_batch": release_batch_name,
				"scheduling_item_names": tuple(scheduling_item_names),
			},
		)


def backfill_release_batch_wos_links(run_name: str | None = None) -> dict[str, Any]:
	filters = _strip_none({"planning_run": run_name})
	release_batches = frappe.get_all(
		"APS Release Batch",
		filters=filters,
		fields=["name", "planning_run", "release_from_date", "release_to_date", "work_order_scheduling"],
		order_by="creation asc",
	)
	updated_batches = 0
	linked_wos = 0
	for row in release_batches:
		doc = frappe.get_doc("APS Release Batch", row.name)
		if doc.get("released_work_order_schedulings"):
			continue
		scheduling_docs = []
		if row.work_order_scheduling and frappe.db.exists("Work Order Scheduling", row.work_order_scheduling):
			scheduling_docs = [row.work_order_scheduling]
		elif row.planning_run and row.release_from_date and row.release_to_date:
			scheduling_docs = frappe.get_all(
				"Work Order Scheduling",
				filters={
					"custom_aps_run": row.planning_run,
					"posting_date": ("between", [row.release_from_date, row.release_to_date]),
				},
				pluck="name",
				order_by="posting_date asc, shift_type asc, name asc",
			)
		if not scheduling_docs:
			continue
		for wos_row in _build_release_batch_wos_rows(scheduling_docs):
			doc.append("released_work_order_schedulings", wos_row)
		if len(scheduling_docs) == 1 and not doc.work_order_scheduling:
			doc.work_order_scheduling = scheduling_docs[0]
		doc.save(ignore_permissions=True)
		scheduling_item_names = []
		if frappe.db.exists("DocType", "Scheduling Item"):
			scheduling_item_names = frappe.get_all(
				"Scheduling Item",
				filters={"parent": ("in", scheduling_docs)},
				pluck="name",
			)
		_backlink_release_batch_to_wos(
			doc.name,
			scheduling_docs=scheduling_docs,
			scheduling_item_names=scheduling_item_names,
		)
		updated_batches += 1
		linked_wos += len(set(scheduling_docs))
	frappe.db.commit()
	return {"updated_batches": updated_batches, "linked_wos": linked_wos}


def reject_shift_schedule_proposals(batch_name: str, reason: str) -> dict[str, Any]:
	reason_text = (reason or "").strip()
	if not reason_text:
		frappe.throw(_("Please enter a rejection reason."))
	return _run_atomic_batch_operation(
		"aps_reject_shift_schedule_proposals",
		lambda: _reject_shift_schedule_proposals(batch_name, reason_text),
	)


def _reject_shift_schedule_proposals(batch_name: str, reason_text: str) -> dict[str, Any]:
	frappe.db.sql(
		"select name from `tabAPS Shift Schedule Proposal Batch` where name = %s for update",
		batch_name,
	)
	batch = frappe.get_doc("APS Shift Schedule Proposal Batch", batch_name)
	frappe.db.sql(
		"select name from `tabAPS Planning Run` where name = %s for update",
		batch.planning_run,
	)
	if batch.status == "Applied" or any(row.review_status == "Applied" for row in batch.items):
		frappe.throw(
			_("Applied Shift Schedule proposal batches or rows cannot be rejected."),
			frappe.ValidationError,
		)

	rejected_rows = 0
	for row in batch.items:
		if row.review_status in ("Applied", "Skipped", "Rejected"):
			continue
		row.review_status = "Rejected"
		row.review_note = reason_text
		rejected_rows += 1

	if not rejected_rows:
		frappe.throw(_("No pending or approved day/night shift proposal rows are available to reject."))

	timestamp = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	batch.notes = _append_review_note(
		batch.notes,
		_("[{0}] {1} rejected remaining day/night shift proposal rows: {2}").format(
			timestamp,
			frappe.session.user,
			reason_text,
		),
	)
	batch.approved_by = frappe.session.user
	batch.approved_on = now_datetime()
	batch.flags.proposal_engine_transition = True
	batch.save(ignore_permissions=True)
	return {
		"run": batch.planning_run,
		"shift_schedule_proposal_batch": batch.name,
		"rejected_rows": rejected_rows,
	}


def update_schedule_notes(
	result_name: str | None = None,
	segment_name: str | None = None,
	result_note: str | None = None,
	segment_note: str | None = None,
) -> dict[str, Any]:
	if not result_name and not segment_name:
		frappe.throw(_("Provide result_name or segment_name to update notes."))
	if segment_name and not result_name:
		result_name = frappe.db.get_value("APS Schedule Segment", segment_name, "parent")
	result_doc = frappe.get_doc("APS Schedule Result", result_name)
	if result_note is not None:
		result_doc.notes = result_note
	updated_segment_note = None
	if segment_name and segment_note is not None:
		for row in result_doc.segments:
			if row.name == segment_name:
				row.segment_note = segment_note
				updated_segment_note = row.segment_note
				break
	result_doc.save(ignore_permissions=True)
	return {
		"result_name": result_doc.name,
		"result_note": result_doc.notes,
		"segment_name": segment_name,
		"segment_note": updated_segment_note,
		"modified_by": result_doc.modified_by,
		"modified": result_doc.modified,
	}


def sync_execution_feedback_to_aps(run_name: str) -> dict[str, Any]:
	from injection_aps.services import execution_sync

	return execution_sync.sync_production_for_run(run_name)


def get_execution_health_for_run(run_name: str, sync: bool = False) -> dict[str, Any]:
	if sync:
		sync_execution_feedback_to_aps(run_name)
	rows = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=["actual_status", "actual_progress_qty"],
	)
	status_counts = defaultdict(int)
	for row in rows:
		status_counts[row.actual_status or "Not Started"] += 1
	today_entries = _count_today_manufacture_entries(run_name)
	return {
		"run": run_name,
		"status_counts": dict(status_counts),
		"running_segments": status_counts.get("Running", 0),
		"delayed_segments": status_counts.get("Delayed", 0) + status_counts.get("Slow Progress", 0),
		"no_recent_update_segments": status_counts.get("No Recent Update", 0),
		"today_completed_entries": today_entries,
	}


def get_customer_schedule_progress_data(
	company: str | None = None,
	customer: str | None = None,
	item_code: str | None = None,
	schedule_scope: str | None = None,
	date_from=None,
	date_to=None,
	status: str | None = None,
	run_name: str | None = None,
	limit: int | None = None,
) -> dict[str, Any]:
	settings = get_settings_dict()
	company = company or frappe.defaults.get_user_default("Company") or settings.get("default_company")
	item_code = (_resolve_item_name(item_code) or item_code) if item_code else None
	selected_run = _get_customer_schedule_progress_run(run_name=run_name, company=company)
	all_schedule_rows = _get_customer_schedule_progress_schedule_rows(company=company, item_code=item_code)
	progress_rows = _build_customer_schedule_progress_rows(
		schedule_rows=all_schedule_rows,
		company=company,
		run_doc=selected_run,
	)
	visible_rows = [
		row
		for row in progress_rows
		if _matches_customer_schedule_progress_filters(
			row,
			customer=customer,
			item_code=item_code,
			schedule_scope=schedule_scope,
			date_from=date_from,
			date_to=date_to,
			status=status,
		)
	]
	row_limit = min(max(cint(limit or 500), 1), 2000)
	return {
		"selected_run": _format_customer_schedule_progress_run(selected_run),
		"summary": _summarize_customer_schedule_progress_rows(visible_rows),
		"rows": visible_rows[:row_limit],
		"truncated": len(visible_rows) > row_limit,
		"filters": {
			"company": company,
			"customer": customer,
			"item_code": item_code,
			"schedule_scope": schedule_scope,
			"date_from": date_from,
			"date_to": date_to,
			"status": status,
			"run_name": selected_run.get("name") if selected_run else None,
			"limit": row_limit,
		},
	}


def _get_customer_schedule_progress_run(run_name: str | None = None, company: str | None = None):
	if run_name:
		if not frappe.db.exists("APS Planning Run", run_name):
			frappe.throw(_("APS Planning Run {0} was not found.").format(run_name))
		return frappe.get_doc("APS Planning Run", run_name).as_dict()

	statuses = tuple(SCHEDULE_PROGRESS_RUN_STATUS_PRIORITY)
	conditions = ["pr.status in ({0})".format(", ".join(["%s"] * len(statuses)))]
	params: list[Any] = list(statuses)
	if company:
		conditions.append("pr.company = %s")
		params.append(company)
	status_rank_sql = " ".join(
		"when %s then {0}".format(rank)
		for _status, rank in SCHEDULE_PROGRESS_RUN_STATUS_PRIORITY.items()
	)
	rank_params = list(SCHEDULE_PROGRESS_RUN_STATUS_PRIORITY)
	rows = frappe.db.sql(
		"""
		select
			pr.name,
			pr.company,
			pr.plant_floor,
			pr.selected_plant_floor_summary,
			pr.planning_date,
			pr.horizon_start,
			pr.horizon_end,
			pr.status,
			pr.approval_state,
			pr.modified
		from `tabAPS Planning Run` pr
		where {conditions}
			and exists (
				select 1
				from `tabAPS Schedule Result` res
				where res.planning_run = pr.name
			)
		order by case pr.status {status_rank_sql} else 99 end asc, pr.modified desc
		limit 1
		""".format(
			conditions=" and ".join(conditions),
			status_rank_sql=status_rank_sql,
		),
		[*params, *rank_params],
		as_dict=True,
	)
	return rows[0] if rows else None


def _format_customer_schedule_progress_run(run_doc) -> dict[str, Any] | None:
	if not run_doc:
		return None
	return {
		"name": run_doc.get("name"),
		"company": run_doc.get("company"),
		"status": run_doc.get("status"),
		"approval_state": run_doc.get("approval_state"),
		"planning_date": run_doc.get("planning_date"),
		"horizon_start": run_doc.get("horizon_start"),
		"horizon_end": run_doc.get("horizon_end"),
		"modified": run_doc.get("modified"),
		"route": _build_form_route("APS Planning Run", run_doc.get("name")),
		"gantt_route": f"aps-schedule-gantt?run_name={run_doc.get('name')}" if run_doc.get("name") else "",
	}


def _get_customer_schedule_progress_schedule_rows(
	company: str | None = None,
	item_code: str | None = None,
) -> list[dict[str, Any]]:
	schedule_filters = _strip_none({"company": company, "status": "Active"})
	schedules = frappe.get_all(
		"Customer Delivery Schedule",
		filters=schedule_filters,
		fields=["name", "customer", "company", "schedule_scope", "version_no", "source_type", "modified"],
		order_by="modified desc",
	)
	if not schedules:
		return []
	schedule_map = {row.name: row for row in schedules}
	child_filters = {
		"parent": ("in", [row.name for row in schedules]),
		"parenttype": "Customer Delivery Schedule",
	}
	if item_code:
		child_filters["item_code"] = item_code
	rows = frappe.get_all(
		"Customer Delivery Schedule Item",
		filters=child_filters,
		fields=[
			"name",
			"parent",
			"idx",
			"sales_order",
			"item_code",
			"customer_part_no",
			"schedule_date",
			"qty",
			"allocated_qty",
			"produced_qty",
			"delivered_qty",
			"balance_qty",
			"status",
			"remark",
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
		],
		order_by="schedule_date asc, parent asc, idx asc",
	)
	prepared = []
	for row in rows:
		if (row.get("status") or "") == "Cancelled" or flt(row.get("qty")) <= 0:
			continue
		schedule = schedule_map.get(row.parent)
		if not schedule:
			continue
		resolved_item = _resolve_item_name(row.item_code) or row.item_code
		prepared.append(
			{
				"schedule": row.parent,
				"schedule_item": row.name,
				"idx": cint(row.idx),
				"customer": schedule.customer,
				"company": schedule.company,
				"schedule_scope": schedule.schedule_scope,
				"version_no": schedule.version_no,
				"source_type": schedule.source_type,
				"sales_order": row.sales_order,
				"item_code": resolved_item,
				"customer_part_no": row.customer_part_no,
				"schedule_date": getdate(row.schedule_date),
				"required_qty": flt(row.qty),
				"allocated_qty": flt(row.allocated_qty),
				"actual_produced_qty": flt(row.produced_qty),
				"delivered_qty": flt(row.delivered_qty),
				"production_strategy": row.production_strategy or "Auto Balance",
				"demand_confidence": row.demand_confidence or "Confirmed",
				"cancellation_risk_percent": flt(row.cancellation_risk_percent),
				"prebuild_allowed": cint(row.prebuild_allowed),
				"max_prebuild_days": cint(row.max_prebuild_days),
				"remark": row.remark,
				"schedule_modified": schedule.modified,
			}
		)
	return sorted(
		prepared,
		key=lambda row: (
			row.get("company") or "",
			row.get("item_code") or "",
			getdate(row.get("schedule_date")),
			row.get("customer") or "",
			row.get("schedule") or "",
			cint(row.get("idx")),
		),
	)


def _build_customer_schedule_progress_rows(
	schedule_rows: list[dict[str, Any]],
	company: str | None,
	run_doc,
) -> list[dict[str, Any]]:
	if not schedule_rows:
		return []

	from injection_aps.services import availability

	fulfillment_results = (
		availability.get_run_fulfillment_projection(run_doc.get("name"), persist=False).get("results") or []
		if run_doc
		else []
	)
	stock_map = _get_customer_claimable_stock_map(
		company,
		demand_rows=[
			{
				"company": row.get("company"),
				"customer": row.get("customer"),
				"sales_order": row.get("sales_order"),
				"item_code": row.get("item_code"),
				"qty": max(flt(row.get("required_qty")) - flt(row.get("delivered_qty")), 0),
				"demand_source": "Customer Delivery Schedule",
			}
			for row in schedule_rows
		],
	)
	if run_doc:
		stock_map = _adjust_progress_stock_for_selected_run(stock_map, fulfillment_results)
	remaining_stock = defaultdict(float, {item: flt(qty) for item, qty in stock_map.items()})
	supply_map = _get_customer_schedule_progress_production_supply_map(
		run_doc.get("name") if run_doc else None,
		company=company,
		target_item_codes={row.get("item_code") for row in schedule_rows if row.get("item_code")},
	)
	progress_rows = []

	for source_row in schedule_rows:
		row = dict(source_row)
		required_qty = flt(row.get("required_qty"))
		delivered_qty = flt(row.get("delivered_qty"))
		open_qty = max(required_qty - delivered_qty, 0)
		stock_covered_qty = min(open_qty, flt(remaining_stock[row.get("item_code")]))
		remaining_stock[row.get("item_code")] = max(flt(remaining_stock[row.get("item_code")]) - stock_covered_qty, 0)
		row.update(
			{
				"delivered_qty": delivered_qty,
				"stock_covered_qty": stock_covered_qty,
				"production_covered_qty": 0.0,
				"uncovered_qty": max(open_qty - stock_covered_qty, 0),
				"projected_completion_time": None,
				"variance_hours": None,
				"selected_run": run_doc.get("name") if run_doc else None,
				"result_names": [],
				"_supply_risk_reasons": [],
			}
		)
		_allocate_customer_schedule_progress_supply(row, supply_map)
		_attach_customer_schedule_fulfillment(row, fulfillment_results)
		_set_customer_schedule_progress_status(row)
		row["routes"] = _get_customer_schedule_progress_routes(row)
		row.pop("_supply_risk_reasons", None)
		progress_rows.append(row)

	return progress_rows


def _adjust_progress_stock_for_selected_run(
	stock_map: dict[str, float],
	fulfillment_results: list[dict[str, Any]],
) -> dict[str, float]:
	"""Rewind selected-run net output from current FG Bin before replaying its supply."""
	produced_by_item = defaultdict(float)
	delivered_by_item = defaultdict(float)
	for row in fulfillment_results or []:
		item_code = row.get("item_code")
		if not item_code:
			continue
		produced_by_item[item_code] += max(flt(row.get("actual_good_qty")), 0)
		delivered_by_item[item_code] += max(flt(row.get("delivered_qty")), 0)
	items = set(stock_map) | set(produced_by_item) | set(delivered_by_item)
	return {
		item: max(
			flt(stock_map.get(item))
			- flt(produced_by_item.get(item))
			+ flt(delivered_by_item.get(item)),
			0,
		)
		for item in items
	}


def _attach_customer_schedule_fulfillment(row: dict[str, Any], fulfillment_results: list[dict[str, Any]]):
	result_allocations = row.get("_result_allocations") or {}
	if not result_allocations:
		# Keep direct callers and legacy progress rows deterministic: only exact
		# customer/SO/item/date results may be attached, and their coverage is
		# claimed once up to this schedule row's demand.
		remaining = max(flt(row.get("required_qty")), 0)
		result_allocations = {}
		for projection in fulfillment_results or []:
			if remaining <= QTY_TOLERANCE or not _progress_fulfillment_identity_matches(row, projection):
				continue
			planned_qty = max(
				flt(projection.get("planned_qty")),
				flt(projection.get("prebuild_qty")) + flt(projection.get("jit_qty")),
				flt(projection.get("actual_good_qty")),
			)
			allocated_qty = min(remaining, planned_qty)
			if allocated_qty <= QTY_TOLERANCE:
				continue
			result_allocations[projection.get("result")] = allocated_qty
			remaining -= allocated_qty
	matches = [
		(projection, min(flt(result_allocations.get(projection.get("result"))) / max(flt(projection.get("planned_qty")), 0.000001), 1))
		for projection in fulfillment_results
		if projection.get("result") in result_allocations
	]

	def apportioned(fieldname: str) -> float:
		return sum(flt(item.get(fieldname)) * fraction for item, fraction in matches)

	row.update(
		{
			"prebuild_qty": apportioned("prebuild_qty"),
			"jit_qty": apportioned("jit_qty"),
			"early_days": max((flt(item.get("early_days")) for item, _fraction in matches), default=0),
			"actual_good_qty": apportioned("actual_good_qty"),
			"scrap_qty": apportioned("scrap_qty"),
			"current_deliverable_qty": min(
				max(flt(row.get("required_qty")) - flt(row.get("delivered_qty")), 0),
				apportioned("current_deliverable_qty"),
			),
			"projected_peak_inventory_qty": max(
				(flt(item.get("projected_peak_inventory_qty")) * fraction for item, fraction in matches), default=0
			),
			"late_qty_before_balance": apportioned("late_qty_before_balance"),
			"late_qty_after_balance": apportioned("late_qty_after_balance"),
			"prebuild_inventory_qty": apportioned("prebuild_inventory_qty"),
			"cancellation_inventory_risk_qty": apportioned("cancellation_inventory_risk_qty"),
			"last_actual_report_time": max(
				(
					get_datetime(item.get("last_actual_report_time"))
					for item, _fraction in matches
					if item.get("last_actual_report_time")
				),
				default=None,
			),
			"production_source_documents": list(
				dict.fromkeys(
					 doc
					for item, _fraction in matches
					for doc in item.get("production_source_documents") or []
				)
			),
			"delivery_source_documents": list(
				dict.fromkeys(
					doc
					for item, _fraction in matches
					for doc in item.get("delivery_source_documents") or []
				)
			),
		}
	)
	row.pop("_result_allocations", None)


def _progress_fulfillment_identity_matches(row, projection) -> bool:
	for fieldname in ("company", "customer", "sales_order", "item_code"):
		if (row.get(fieldname) or "") != (projection.get(fieldname) or ""):
			return False
	row_date = row.get("schedule_date")
	projection_date = projection.get("requested_date")
	return bool(row_date and projection_date and getdate(row_date) == getdate(projection_date))


def _get_customer_schedule_progress_production_supply_map(
	run_name: str | None,
	company: str | None = None,
	target_item_codes: set[str] | None = None,
):
	if not run_name:
		return {}
	result_filters = {"planning_run": run_name}
	if company:
		result_filters["company"] = company
	results = frappe.get_all(
		"APS Schedule Result",
		filters=result_filters,
		fields=[
			"name",
			"planning_run",
			"company",
			"item_code",
			"customer",
			"sales_order",
			"sales_order_item",
			"requested_date",
			"machine_scheduled_qty",
			"fulfillment_baseline_json",
			"risk_status",
			"status",
			"blocking_reason",
		],
		order_by="requested_date asc, modified asc",
	)
	if not results:
		return {}
	result_map = {row.name: row for row in results}
	target_item_codes = set(target_item_codes or [])
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={"parenttype": "APS Schedule Result", "parent": ("in", list(result_map))},
		fields=[
			"name",
			"parent",
			"workstation",
			"start_time",
			"end_time",
			"planned_qty",
			"segment_kind",
			"primary_item_code",
			"co_product_item_code",
			"segment_status",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"actual_status",
			"actual_completed_qty",
			"actual_good_qty",
			"actual_scrap_qty",
			"actual_start_time",
			"actual_end_time",
			"last_actual_report_time",
			"last_execution_sync_on",
			"production_mode",
		],
		order_by="end_time asc, start_time asc, idx asc",
	)
	candidate_segments = []
	for segment in segments:
		result = result_map.get(segment.parent)
		if not result:
			continue
		item_code = _get_customer_schedule_progress_segment_item(segment, result)
		if not item_code:
			continue
		if not _is_customer_schedule_progress_supply_segment(segment, result):
			continue
		if target_item_codes and item_code not in target_item_codes:
			continue
		candidate_segments.append((segment, result, item_code))
	now_value = now_datetime()
	execution_map = _get_customer_schedule_progress_execution_snapshots([segment for segment, _result, _item_code in candidate_segments])
	supply_map = defaultdict(list)
	prepared = []
	for segment, result, item_code in candidate_segments:
		planned_qty = flt(segment.planned_qty)
		if planned_qty <= 0:
			continue
		if segment.get("last_execution_sync_on"):
			execution = {
				"actual_status": segment.get("actual_status"),
				"actual_completed_qty": segment.get("actual_good_qty"),
				"actual_start_time": segment.get("actual_start_time"),
				"actual_end_time": segment.get("actual_end_time"),
			}
		else:
			execution = execution_map.get(segment.name) or _get_segment_execution_snapshot(segment)
		actual_qty = min(max(flt(execution.get("actual_completed_qty")), 0), planned_qty)
		prepared.append(
			{
				"segment": segment,
				"result": result,
				"item_code": item_code,
				"execution": execution,
				"actual_qty": actual_qty,
				"planned_remaining_qty": max(planned_qty - actual_qty, 0),
				"actual_completion_time": (
					execution.get("actual_end_time")
					if actual_qty >= planned_qty and execution.get("actual_end_time")
					else now_value
				),
			}
		)
	target_state = {}
	for entry in prepared:
		state_key = (entry["result"].name, entry["item_code"])
		target_state.setdefault(
			state_key,
			_get_customer_schedule_progress_result_targets(
				entry["result"], entry["item_code"]
			),
		)
	# Attribute actual first, then remaining plan, so one Result's physical output
	# follows its frozen schedule targets FIFO without letting an earlier segment's
	# future plan consume the target needed by a later segment's actual report.
	for quantity_field, source, completion_field in (
		("actual_qty", "Actual", "actual_completion_time"),
		("planned_remaining_qty", "Planned", None),
	):
		for entry in prepared:
			qty = max(flt(entry.get(quantity_field)), 0)
			if qty <= QTY_TOLERANCE:
				continue
			completion_time = (
				entry.get(completion_field)
				if completion_field
				else entry["segment"].end_time
			)
			_emit_customer_schedule_progress_supply(
				supply_map,
				target_state[(entry["result"].name, entry["item_code"])],
				result=entry["result"],
				segment=entry["segment"],
				item_code=entry["item_code"],
				qty=qty,
				completion_time=completion_time,
				source=source,
				execution=entry["execution"],
			)
	for key, rows in supply_map.items():
		supply_map[key] = sorted(
			rows,
			key=lambda row: (
				get_datetime(row.get("completion_time") or now_value),
				row.get("result_name") or "",
				row.get("segment_name") or "",
			),
		)
	return supply_map


def _get_customer_schedule_progress_result_targets(result, item_code: str) -> list[dict[str, Any]]:
	baseline = _parse_json_object(result.get("fulfillment_baseline_json"), {})
	targets = [
		{
			"company": result.get("company"),
			"customer": result.get("customer") or "",
			"sales_order": row.get("sales_order") or result.get("sales_order") or "",
			"item_code": item_code,
			"schedule_date": getdate(row.get("accepted_schedule_date") or row.get("schedule_date")),
			"remaining_qty": _get_frozen_target_fulfillment_qty(row),
			"schedule_item": row.get("customer_schedule_item") or "",
		}
		for row in baseline.get("targets") or []
		if isinstance(row, dict)
		and not cint(row.get("retired"))
		and (row.get("item_code") or result.get("item_code") or "") == item_code
		and (row.get("accepted_schedule_date") or row.get("schedule_date"))
		and _get_frozen_target_fulfillment_qty(row) > QTY_TOLERANCE
	]
	if not targets:
		targets = [
			{
				"company": result.get("company"),
				"customer": result.get("customer") or "",
				"sales_order": result.get("sales_order") or "",
				"item_code": item_code,
				"schedule_date": getdate(result.get("requested_date")),
				"remaining_qty": max(flt(result.get("machine_scheduled_qty")), 0),
				"schedule_item": "",
			}
		]
	return sorted(
		targets,
		key=lambda row: (
			getdate(row.get("schedule_date")),
			row.get("sales_order") or "",
			row.get("schedule_item") or "",
		),
	)


def _get_frozen_target_fulfillment_qty(row) -> float:
	value = (
		row.get("accepted_source_open_qty")
		if row.get("accepted_source_open_qty") not in (None, "")
		else row.get("source_open_qty")
	)
	return max(flt(value), 0)


def _emit_customer_schedule_progress_supply(
	supply_map,
	targets: list[dict[str, Any]],
	*,
	result,
	segment,
	item_code: str,
	qty: float,
	completion_time,
	source: str,
	execution,
) -> None:
	remaining = max(flt(qty), 0)
	for target in targets:
		if remaining <= QTY_TOLERANCE:
			break
		allocated = min(remaining, max(flt(target.get("remaining_qty")), 0))
		if allocated <= QTY_TOLERANCE:
			continue
		target["remaining_qty"] = max(flt(target.get("remaining_qty")) - allocated, 0)
		remaining -= allocated
		key = (
			target.get("company"),
			target.get("customer") or "",
			target.get("sales_order") or "",
			item_code,
			getdate(target.get("schedule_date")),
		)
		supply_map[key].append(
			_build_customer_schedule_progress_supply(
				result=result,
				segment=segment,
				qty=allocated,
				completion_time=completion_time,
				source=source,
				execution=execution,
			)
		)


def _get_customer_schedule_progress_execution_snapshots(segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
	if not segments:
		return {}

	segment_names = list(dict.fromkeys(segment.get("name") for segment in segments if segment.get("name")))
	linked_scheduling_names = list(
		dict.fromkeys(segment.get("linked_scheduling_item") for segment in segments if segment.get("linked_scheduling_item"))
	)
	scheduling_by_name = {}
	scheduling_by_segment = defaultdict(list)
	if frappe.db.exists("DocType", "Scheduling Item"):
		fields = ["name", "parent", "work_order", "completed_qty", "from_time", "to_time", "modified"]
		if linked_scheduling_names:
			scheduling_by_name = {
				row.name: row
				for row in frappe.get_all(
					"Scheduling Item",
					filters={"name": ("in", linked_scheduling_names)},
					fields=fields,
				)
			}
		if segment_names and frappe.db.has_column("Scheduling Item", "custom_aps_segment_reference"):
			for row in frappe.get_all(
				"Scheduling Item",
				filters={"custom_aps_segment_reference": ("in", segment_names)},
				fields=[*fields, "custom_aps_segment_reference"],
				order_by="modified desc",
			):
				segment_name = row.get("custom_aps_segment_reference")
				if segment_name:
					scheduling_by_segment[segment_name].append(row)

	work_order_names = []
	for segment in segments:
		if segment.get("linked_work_order"):
			work_order_names.append(segment.get("linked_work_order"))
	for scheduling_item in [*scheduling_by_name.values(), *(item for rows in scheduling_by_segment.values() for item in rows)]:
		if scheduling_item.get("work_order"):
			work_order_names.append(scheduling_item.get("work_order"))
	work_order_names = list(dict.fromkeys(work_order_names))
	work_orders = {}
	if work_order_names and frappe.db.exists("DocType", "Work Order"):
		work_orders = {
			row.name: row
			for row in frappe.get_all(
				"Work Order",
				filters={"name": ("in", work_order_names)},
				fields=["name", "produced_qty"],
			)
		}

	snapshots = {}
	remaining_work_order_qty = {
		name: max(flt(row.get("produced_qty")), 0)
		for name, row in work_orders.items()
	}
	for segment in segments:
		scheduling_items = []
		linked_item = scheduling_by_name.get(segment.get("linked_scheduling_item"))
		if linked_item:
			scheduling_items.append(linked_item)
		for scheduling_item in scheduling_by_segment.get(segment.get("name")) or []:
			if scheduling_item.get("name") not in {row.get("name") for row in scheduling_items}:
				scheduling_items.append(scheduling_item)
		linked_work_order = (
			next((row.get("work_order") for row in scheduling_items if row.get("work_order")), None)
			or segment.get("linked_work_order")
		)
		work_order = work_orders.get(linked_work_order)
		if not scheduling_items and work_order:
			# A WO-level produced_qty is one total, not one value per APS segment.
			# Allocate it once in stable segment order until execution_sync creates
			# detail-level ledgers; never copy the whole WO into every segment.
			attributed_qty = min(
				max(flt(segment.get("planned_qty")), 0),
				max(flt(remaining_work_order_qty.get(linked_work_order)), 0),
			)
			remaining_work_order_qty[linked_work_order] = max(
				flt(remaining_work_order_qty.get(linked_work_order)) - attributed_qty,
				0,
			)
			work_order = frappe._dict({**dict(work_order), "produced_qty": attributed_qty})
		snapshots[segment.name] = _build_segment_execution_snapshot(
			segment,
			scheduling_items,
			work_order,
		)
	return snapshots


def _get_customer_schedule_progress_segment_item(segment, result) -> str | None:
	if (segment.get("segment_kind") or "") == "Family Co-Product":
		return _resolve_item_name(segment.get("co_product_item_code")) or segment.get("co_product_item_code")
	return _resolve_item_name(result.get("item_code")) or result.get("item_code")


def _is_customer_schedule_progress_supply_segment(segment, result) -> bool:
	"""Only effective Primary/Manual work may become customer-facing supply."""
	return bool(
		result
		and (result.get("status") or "") != "Blocked"
		and consistency.is_effective_primary_segment(segment)
	)


def _build_customer_schedule_progress_supply(result, segment, qty, completion_time, source, execution):
	risk_reasons = []
	actual_status = execution.get("actual_status") or segment.get("actual_status")
	if actual_status in SCHEDULE_PROGRESS_RISK_ACTUAL_STATUSES:
		risk_reasons.append(_("Actual production status is {0}.").format(actual_status))
	if result.get("risk_status") in SCHEDULE_PROGRESS_RISK_RESULT_STATUSES:
		risk_reasons.append(_("APS result risk status is {0}.").format(result.get("risk_status")))
	if (segment.get("segment_status") or "") in ("Risk", "Blocked"):
		risk_reasons.append(_("Schedule segment status is {0}.").format(segment.get("segment_status")))
	if result.get("blocking_reason"):
		risk_reasons.append(result.get("blocking_reason"))
	return {
		"company": result.get("company"),
		"customer": result.get("customer") or "",
		"sales_order": result.get("sales_order") or "",
		"requested_date": result.get("requested_date"),
		"qty": flt(qty),
		"remaining_qty": flt(qty),
		"completion_time": completion_time,
		"source": source,
		"result_name": result.get("name"),
		"segment_name": segment.get("name"),
		"actual_status": actual_status,
		"risk_reasons": list(dict.fromkeys([reason for reason in risk_reasons if reason])),
	}


def _allocate_customer_schedule_progress_supply(row: dict[str, Any], supply_map):
	need_qty = flt(row.get("uncovered_qty"))
	if need_qty <= 0:
		return
	supplies = supply_map.get(
		(
			row.get("company"),
			row.get("customer") or "",
			row.get("sales_order") or "",
			row.get("item_code"),
			getdate(row.get("schedule_date")),
		)
	) or []
	covered_qty = 0.0
	result_names = []
	result_allocations = defaultdict(float)
	last_completion_time = None
	risk_reasons = []

	for supply in supplies:
		if need_qty <= 0:
			break
		available_qty = flt(supply.get("remaining_qty"))
		if available_qty <= 0:
			continue
		take_qty = min(need_qty, available_qty)
		supply["remaining_qty"] = max(available_qty - take_qty, 0)
		need_qty = max(need_qty - take_qty, 0)
		covered_qty += take_qty
		last_completion_time = supply.get("completion_time") or last_completion_time
		if supply.get("result_name") and supply.get("result_name") not in result_names:
			result_names.append(supply.get("result_name"))
		if supply.get("result_name"):
			result_allocations[supply.get("result_name")] += take_qty
		risk_reasons.extend(supply.get("risk_reasons") or [])

	row["production_covered_qty"] = covered_qty
	row["uncovered_qty"] = need_qty
	row["projected_completion_time"] = last_completion_time
	row["result_names"] = result_names
	row["_result_allocations"] = dict(result_allocations)
	row["_supply_risk_reasons"] = list(dict.fromkeys([reason for reason in risk_reasons if reason]))


def _set_customer_schedule_progress_status(row: dict[str, Any]):
	required_qty = flt(row.get("required_qty"))
	delivered_qty = flt(row.get("delivered_qty"))
	stock_covered_qty = flt(row.get("stock_covered_qty"))
	production_covered_qty = flt(row.get("production_covered_qty"))
	uncovered_qty = max(required_qty - delivered_qty - stock_covered_qty - production_covered_qty, 0)
	row["uncovered_qty"] = uncovered_qty
	now_value = now_datetime()
	due_datetime = _get_due_datetime(row.get("schedule_date"))
	projected_completion_time = row.get("projected_completion_time")
	if projected_completion_time:
		projected_completion_time = get_datetime(projected_completion_time)
		row["projected_completion_time"] = projected_completion_time
		row["variance_hours"] = round((due_datetime - projected_completion_time).total_seconds() / 3600, 2)

	if required_qty <= 0 or delivered_qty >= required_qty:
		row["status"] = "Delivered"
		row["risk_reason"] = _("Delivered quantity covers this customer schedule row.")
		return
	if delivered_qty + stock_covered_qty >= required_qty:
		row["status"] = "Stock Covered"
		row["risk_reason"] = _("Available stock covers the remaining schedule quantity.")
		return
	if uncovered_qty > 0:
		if now_value > due_datetime:
			row["status"] = "Late"
			row["risk_reason"] = _("Delivery date has passed and {0} is still uncovered.").format(
				frappe.format(uncovered_qty, {"fieldtype": "Float"})
			)
		else:
			row["status"] = "Uncovered"
			row["risk_reason"] = _("Stock and the selected APS run still leave {0} uncovered.").format(
				frappe.format(uncovered_qty, {"fieldtype": "Float"})
			)
		return
	if projected_completion_time and projected_completion_time > due_datetime:
		row["status"] = "Late"
		row["risk_reason"] = _("Projected completion is later than the delivery date.")
		return
	if row.get("_supply_risk_reasons"):
		row["status"] = "At Risk"
		row["risk_reason"] = "; ".join(row.get("_supply_risk_reasons")[:3])
		return
	if projected_completion_time and row.get("variance_hours") is not None and 0 <= flt(row.get("variance_hours")) <= 24:
		row["status"] = "At Risk"
		row["risk_reason"] = _("Projected completion is within 24 hours of the delivery deadline.")
		return
	row["status"] = "On Track"
	row["risk_reason"] = _("Projected production covers this schedule before the delivery date.")


def _matches_customer_schedule_progress_filters(
	row: dict[str, Any],
	customer: str | None = None,
	item_code: str | None = None,
	schedule_scope: str | None = None,
	date_from=None,
	date_to=None,
	status: str | None = None,
) -> bool:
	if customer and row.get("customer") != customer:
		return False
	if item_code and row.get("item_code") != item_code:
		return False
	if schedule_scope and row.get("schedule_scope") != schedule_scope:
		return False
	if date_from and getdate(row.get("schedule_date")) < getdate(date_from):
		return False
	if date_to and getdate(row.get("schedule_date")) > getdate(date_to):
		return False
	if status and row.get("status") != status:
		return False
	return True


def _summarize_customer_schedule_progress_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
	status_counts = defaultdict(int)
	for row in rows or []:
		status_counts[row.get("status") or "Unknown"] += 1
	return {
		"rows": len(rows or []),
		"required_qty": sum(flt(row.get("required_qty")) for row in rows or []),
		"delivered_qty": sum(flt(row.get("delivered_qty")) for row in rows or []),
		"actual_good_qty": sum(flt(row.get("actual_good_qty")) for row in rows or []),
		"scrap_qty": sum(flt(row.get("scrap_qty")) for row in rows or []),
		"current_deliverable_qty": sum(flt(row.get("current_deliverable_qty")) for row in rows or []),
		"prebuild_qty": sum(flt(row.get("prebuild_qty")) for row in rows or []),
		"jit_qty": sum(flt(row.get("jit_qty")) for row in rows or []),
		"prebuild_inventory_qty": sum(flt(row.get("prebuild_inventory_qty")) for row in rows or []),
		"cancellation_inventory_risk_qty": sum(
			flt(row.get("cancellation_inventory_risk_qty")) for row in rows or []
		),
		"stock_covered_qty": sum(flt(row.get("stock_covered_qty")) for row in rows or []),
		"production_covered_qty": sum(flt(row.get("production_covered_qty")) for row in rows or []),
		"uncovered_qty": sum(flt(row.get("uncovered_qty")) for row in rows or []),
		"risk_rows": status_counts.get("At Risk", 0) + status_counts.get("Uncovered", 0),
		"late_rows": status_counts.get("Late", 0),
		"status_counts": dict(status_counts),
	}


def _get_customer_schedule_progress_routes(row: dict[str, Any]) -> dict[str, Any]:
	selected_run = row.get("selected_run")
	result_routes = [
		_build_form_route("APS Schedule Result", result_name)
		for result_name in (row.get("result_names") or [])
		if result_name
	]
	return {
		"schedule": _build_form_route("Customer Delivery Schedule", row.get("schedule")),
		"selected_run": _build_form_route("APS Planning Run", selected_run),
		"gantt": f"aps-schedule-gantt?run_name={selected_run}" if selected_run else "",
		"results": result_routes,
	}


def _with_role_filtered_actions(context: dict[str, Any]) -> dict[str, Any]:
	actions = []
	user_roles = set(frappe.get_roles())
	is_admin = frappe.session.user == "Administrator"
	for action in context.get("actions") or []:
		required_roles = ACTION_REQUIRED_ROLES.get(action.get("action_key"))
		if required_roles and not is_admin and not user_roles.intersection(required_roles):
			action = dict(action)
			action["enabled"] = 0
			action["disabled_reason"] = _("Your role can view this APS step but cannot run this action.")
		actions.append(action)
	context["actions"] = actions
	return context


def analyze_change_request_impact(change_request: str) -> dict[str, Any]:
	from injection_aps.services import change_engine

	return change_engine.analyze_change_request(change_request)


def confirm_change_request(change_request: str) -> dict[str, Any]:
	from injection_aps.services import change_engine

	return change_engine.confirm_change_request(change_request)


def approve_change_request(change_request: str) -> dict[str, Any]:
	from injection_aps.services import change_engine

	return change_engine.approve_change_request(change_request)


def reject_change_request(change_request: str, reason: str | None = None) -> dict[str, Any]:
	from injection_aps.services import change_engine

	return change_engine.reject_change_request(change_request, reason=reason)


def apply_change_request(change_request: str) -> dict[str, Any]:
	from injection_aps.services import change_engine

	return change_engine.apply_change_request(change_request)


def analyze_insert_order_impact(
	company: str,
	plant_floor: str | None = None,
	plant_floors: list[str] | str | None = None,
	item_code: str | None = None,
	qty: float | None = None,
	required_date: str | None = None,
	customer: str | None = None,
) -> dict[str, Any]:
	settings = get_settings_dict()
	item_code = _require_item_name(item_code)
	if not required_date:
		frappe.throw(_("Required Date is required for insert-order impact analysis."))
	qty = flt(qty)
	selected_plant_floors = _normalize_selected_plant_floors(
		company=company,
		plant_floors=plant_floors,
		plant_floor=plant_floor,
		required=True,
	)
	item_context = _get_item_context(item_code, settings)
	available_molds = _get_available_mold_rows(item_code)
	capability_rows = _get_machine_capability_rows(plant_floors=selected_plant_floors)
	workstation_state = _build_workstation_state_map(capability_rows)
	downtime_windows = _get_active_downtime_windows(
		company=company,
		plant_floors=selected_plant_floors,
		horizon_start=get_datetime(now_datetime()),
		horizon_end=get_datetime(add_days(required_date, 7)),
	)
	locked_segments = _get_locked_segments(selected_plant_floors)
	mold_state = _build_mold_state_map(locked_segments)
	_apply_locked_segments_to_state(workstation_state, locked_segments)
	candidates = _select_machine_candidates(
		item_code=item_code,
		item_context=item_context,
		capability_rows=capability_rows,
		plant_floors=selected_plant_floors,
	)
	best = _choose_best_slot(
		company=company,
		customer=customer,
		item_code=item_code,
		item_context=item_context,
		qty=qty,
		demand_date=required_date,
		horizon_start=get_datetime(now_datetime()),
		horizon_end=get_datetime(add_days(required_date, 7)),
		workstation_state=workstation_state,
		mold_state=mold_state,
		candidates=candidates,
		settings=settings,
		selected_plant_floors=selected_plant_floors,
		downtime_windows=downtime_windows,
	)
	impacted = []
	impacted_customers = set()
	changeover_minutes = sum(flt(segment.get("changeover_minutes")) for segment in best.get("segments") or [])
	if best["segments"]:
		first_segment = best["segments"][0]
		overlap_segments = frappe.get_all(
			"APS Schedule Segment",
			filters={
				"workstation": first_segment.get("workstation"),
				"start_time": ("<", first_segment.get("end_time")),
				"end_time": (">", first_segment.get("start_time")),
			},
			fields=["name", "parent", "workstation", "start_time", "end_time", "planned_qty"],
			order_by="start_time asc",
		)
		result_meta = {
			row.name: row
			for row in frappe.get_all(
				"APS Schedule Result",
				filters={"name": ("in", [row.parent for row in overlap_segments])} if overlap_segments else {"name": "__missing__"},
				fields=["name", "customer", "item_code", "requested_date"],
			)
		}
		for row in overlap_segments:
			parent = result_meta.get(row.parent)
			if parent and parent.customer:
				impacted_customers.add(parent.customer)
			impacted.append(
				{
					"workstation": row.get("workstation"),
					"segment_name": row.get("name"),
					"result_name": row.get("parent"),
					"item_code": parent.item_code if parent else None,
					"customer": parent.customer if parent else None,
					"requested_date": parent.requested_date if parent else None,
					"start_time": row.get("start_time"),
					"end_time": row.get("end_time"),
					"planned_qty": row.get("planned_qty"),
				}
			)

	return {
		"item_code": item_code,
		"customer": customer,
		"required_date": required_date,
		"selected_plant_floors": selected_plant_floors,
		"scheduled_qty": best["scheduled_qty"],
		"unscheduled_qty": best["unscheduled_qty"],
		"candidate_workstations": [row.get("workstation") for row in candidates],
		"candidate_molds": [
			{
				"mould_reference": row.get("mold"),
				"mold_name": row.get("mold_name"),
				"is_family_mold": cint(row.get("is_family_mold")),
				"machine_tonnage": row.get("machine_tonnage"),
				"cavity_count": row.get("cavity_count"),
				"cycle_time_seconds": row.get("cycle_time_seconds"),
				"output_qty": row.get("output_qty"),
				"cavity_output_qty": row.get("cavity_output_qty"),
				"effective_output_qty": row.get("effective_output_qty"),
			}
			for row in available_molds
		],
		"parallelization_plan": [
			{
				"workstation": row.get("workstation"),
				"mould_reference": row.get("mould_reference"),
				"planned_qty": row.get("planned_qty"),
				"start_time": row.get("start_time"),
				"end_time": row.get("end_time"),
				"lane_key": row.get("lane_key"),
			}
			for row in (best.get("segments") or [])
			if row.get("segment_kind") != "Family Co-Product"
		],
		"family_side_outputs": best.get("family_side_outputs") or [],
		"impacted_segments": impacted,
		"displaced_segments": impacted,
		"impacted_customers": sorted(impacted_customers),
		"changeover_minutes": changeover_minutes,
		"future_batch_hint": _get_future_demand_hint(
			company=company,
			customer=customer,
			item_code=item_code,
			demand_date=required_date,
		),
		"missing_machine": any(error.get("exception_type") == "Machine Unavailable" for error in best["exceptions"]),
		"missing_mould": 0 if available_molds else 1,
		"schedule_explanation": best.get("schedule_explanation"),
		"family_output_summary": best.get("family_output_summary"),
		"exceptions": best["exceptions"],
	}


def rebuild_exceptions(run_name: str) -> dict[str, Any]:
	for name in frappe.get_all("APS Exception Log", filters={"planning_run": run_name}, pluck="name"):
		frappe.delete_doc("APS Exception Log", name, force=1, ignore_permissions=True)
	validate_run_mold_readiness(run_name, persist_exceptions=True)
	_validate_run_segment_overlaps(run_name, persist_exceptions=True)
	_validate_run_mold_overlaps(run_name, persist_exceptions=True)
	consistency_summary = consistency.recalculate_plan_consistency(
		run_name,
		reason="exception rebuild",
	)
	recreated = frappe.get_all(
		"APS Exception Log",
		filters={"planning_run": run_name, "status": "Open"},
		pluck="name",
	)
	return {"run": run_name, "exceptions": recreated, "consistency": consistency_summary}


def get_next_actions_for_context(doctype: str, docname: str) -> dict[str, Any]:
	if doctype == "APS Schedule Import Batch":
		doc = frappe.get_doc(doctype, docname)
		schedule_name = frappe.db.get_value("Customer Delivery Schedule", {"import_batch": doc.name}, "name")
		current_step = "Imported" if doc.status == "Imported" else doc.status or "Draft"
		next_step = "Rebuild Demand Pool / Net Requirement" if doc.status == "Imported" else "Complete Import"
		return _with_role_filtered_actions({
			"doctype": doctype,
			"docname": docname,
			"current_step": current_step,
			"next_step": next_step,
			"blocking_reason": "" if doc.status == "Imported" else "Import has not been completed yet.",
			"actions": [
				{
					"label": "Import and Rebuild",
					"action_key": "promote_import",
					"method": "injection_aps.api.app.promote_schedule_import_to_net_requirement",
					"kwargs": {"import_batch": doc.name},
					"requires_existing_work_order_policy": 1,
					"enabled": 1 if doc.status == "Imported" else 0,
				},
				{
					"label": "Open Schedule",
					"action_key": "open_schedule",
					"route": f"Form/Customer Delivery Schedule/{schedule_name}" if schedule_name else "",
					"enabled": 1 if schedule_name else 0,
				},
				{
					"label": "Net Requirements",
					"action_key": "open_net_requirement",
					"route": "aps-net-requirement-workbench",
					"enabled": 1,
				},
			],
		})

	if doctype == "Customer Delivery Schedule":
		doc = frappe.get_doc(doctype, docname)
		return _with_role_filtered_actions({
			"doctype": doctype,
			"docname": docname,
			"current_step": doc.status or "Draft",
			"next_step": "Rebuild Demand and Recalculate" if doc.status == "Active" else "Activate the Current Version First",
			"blocking_reason": "" if doc.status == "Active" else "Only active schedules can drive APS.",
			"actions": [
				{
					"label": "Rebuild Demand",
					"action_key": "rebuild_demand_pool",
					"method": "injection_aps.api.app.rebuild_demand_pool",
					"kwargs": {"company": doc.company},
					"enabled": 1 if doc.status == "Active" else 0,
				},
				{
					"label": "Net Workbench",
					"action_key": "open_net_requirement",
					"route": "aps-net-requirement-workbench",
					"enabled": 1,
				},
				{
					"label": "Version Diff",
					"action_key": "open_schedule",
					"route": f"Form/Customer Delivery Schedule/{doc.name}",
					"enabled": 1,
				},
			],
		})

	if doctype == "APS Planning Run":
		doc = frappe.get_doc(doctype, docname)
		has_applied_wo_batch = bool(
			frappe.db.exists(
				"APS Work Order Proposal Batch",
				{"planning_run": doc.name, "status": "Applied"},
			)
		)
		context = _build_planning_run_context(doc)
		is_consistent = doc.get("consistency_status") == "Valid"
		return _with_role_filtered_actions({
			**context,
			"actions": [
				{
					"label": "Recalculate",
					"action_key": "run_trial",
					"method": "injection_aps.api.app.run_planning_run",
					"kwargs": {"run_name": doc.name},
					"requires_existing_work_order_policy": 1,
					"enabled": 1,
					"confirm_required": 1,
					"confirm_title": "Confirm Recalculate",
					"confirm_summary": [
						"APS Run: {0}".format(doc.name),
						"Company: {0}".format(doc.company or "-"),
						"Plant Floors: {0}".format(context.get("selected_plant_floor_summary") or "-"),
						"Horizon: {0} days".format(cint(doc.horizon_days or 0)),
						"This action will recalculate APS results from the current demand.",
					],
				},
				{
					"label": "Confirm Run",
					"action_key": "approve",
					"method": "injection_aps.api.app.approve_planning_run",
					"kwargs": {"run_name": doc.name},
					"enabled": 1 if is_consistent and doc.approval_state != "Approved" and doc.status in ("Planned", "Risk", "Draft") else 0,
					"disabled_reason": "Plan consistency must be Valid before confirmation." if not is_consistent else "",
					"confirm_required": 1,
					"confirm_title": "Confirm APS Run",
					"confirm_summary": [
						"APS Run: {0}".format(doc.name),
						"Current Status: {0}".format(_label_run_status(doc.status)),
						"Exceptions: {0}".format(cint(doc.exception_count or 0)),
						"After confirmation, the run will move into the downstream proposal review flow.",
					],
				},
				{
					"label": "WO Proposals",
					"action_key": "generate_work_order_proposals",
					"method": "injection_aps.api.app.generate_work_order_proposals",
					"kwargs": {"run_name": doc.name},
					"enabled": 1 if is_consistent and doc.approval_state == "Approved" and doc.status in ("Approved",) else 0,
					"disabled_reason": "Plan consistency must be Valid before release." if not is_consistent else "",
					"confirm_required": 1,
					"confirm_title": "Confirm Generate Work Order Proposals",
					"confirm_summary": [
						"APS Run: {0}".format(doc.name),
						"This action will generate a work-order proposal batch for review.",
					],
				},
				{
					"label": "Shift Proposals",
					"action_key": "generate_shift_schedule_proposals",
					"method": "injection_aps.api.app.generate_shift_schedule_proposals",
					"kwargs": {"run_name": doc.name},
					"enabled": 1 if is_consistent and (has_applied_wo_batch or doc.status == "Shift Proposed") else 0,
					"disabled_reason": "Plan consistency must be Valid before release." if not is_consistent else "",
					"confirm_required": 1,
					"confirm_title": "Confirm Generate Day/Night Shift Proposals",
					"confirm_summary": [
						"APS Run: {0}".format(doc.name),
						"This action will generate a day/night shift proposal batch for review.",
					],
				},
				{
					"label": "Board",
					"action_key": "open_gantt",
					"route": f"aps-schedule-gantt?run_name={doc.name}",
					"enabled": 1,
				},
				{
					"label": "Execution",
					"action_key": "open_release_center",
					"route": f"aps-release-center?run_name={doc.name}",
					"enabled": 1,
				},
			],
		})

	if doctype == "APS Work Order Proposal Batch":
		doc = frappe.get_doc(doctype, docname)
		run_is_consistent = frappe.db.get_value("APS Planning Run", doc.planning_run, "consistency_status") == "Valid"
		return _with_role_filtered_actions({
			"doctype": doctype,
			"docname": docname,
			"current_step": doc.status or "Draft",
			"next_step": "Review proposal rows and apply formal work orders",
			"blocking_reason": "" if doc.status in ("Ready For Review", "Reviewed", "Applied") else "Generate proposal rows first.",
			"actions": [
				{
					"label": "Run",
					"action_key": "open_run",
					"route": f"Form/APS Planning Run/{doc.planning_run}" if doc.planning_run else "",
					"enabled": 1 if doc.planning_run else 0,
				},
				{
					"label": "Apply Results",
					"action_key": "apply_work_order_proposals",
					"method": "injection_aps.api.app.apply_work_order_proposals",
					"kwargs": {"batch_name": doc.name},
					"enabled": 1 if run_is_consistent and doc.status in ("Ready For Review", "Partially Reviewed", "Reviewed") else 0,
					"disabled_reason": "Plan consistency must be Valid before formal apply." if not run_is_consistent else "",
					"confirm_required": 1,
					"confirm_title": "Confirm Apply Work Order Results",
					"confirm_summary": [
						"Work Order Proposal Batch: {0}".format(doc.name),
						"Approved rows will formally create or bind work orders.",
					],
				},
				{
					"label": "Execution",
					"action_key": "open_release_center",
					"route": f"aps-release-center?run_name={doc.planning_run}" if doc.planning_run else "aps-release-center",
					"enabled": 1,
				},
			],
		})

	if doctype == "APS Shift Schedule Proposal Batch":
		doc = frappe.get_doc(doctype, docname)
		run_is_consistent = frappe.db.get_value("APS Planning Run", doc.planning_run, "consistency_status") == "Valid"
		return _with_role_filtered_actions({
			"doctype": doctype,
			"docname": docname,
			"current_step": doc.status or "Draft",
			"next_step": "Review day/night shift rows and apply formal scheduling",
			"blocking_reason": "" if doc.status in ("Ready For Review", "Reviewed", "Applied") else "Generate shift proposal rows first.",
			"actions": [
				{
					"label": "Run",
					"action_key": "open_run",
					"route": f"Form/APS Planning Run/{doc.planning_run}" if doc.planning_run else "",
					"enabled": 1 if doc.planning_run else 0,
				},
				{
					"label": "Apply Results",
					"action_key": "apply_shift_schedule_proposals",
					"method": "injection_aps.api.app.apply_shift_schedule_proposals",
					"kwargs": {"batch_name": doc.name},
					"enabled": 1 if run_is_consistent and doc.status in ("Ready For Review", "Partially Reviewed", "Reviewed") else 0,
					"disabled_reason": "Plan consistency must be Valid before formal apply." if not run_is_consistent else "",
					"confirm_required": 1,
					"confirm_title": "Confirm Apply Day/Night Shift Results",
					"confirm_summary": [
						"Shift Proposal Batch: {0}".format(doc.name),
						"Approved rows will formally write day/night scheduling rows.",
					],
				},
				{
					"label": "Execution",
					"action_key": "open_release_center",
					"route": f"aps-release-center?run_name={doc.planning_run}" if doc.planning_run else "aps-release-center",
					"enabled": 1,
				},
			],
		})

	if doctype == "APS Release Batch":
		doc = frappe.get_doc(doctype, docname)
		return _with_role_filtered_actions({
			"doctype": doctype,
			"docname": docname,
			"current_step": doc.status or "Draft",
			"next_step": "Monitor execution feedback" if doc.status == "Released" else "Apply formal documents",
			"blocking_reason": "" if doc.status == "Released" else "Formal work orders / shift schedulings have not been applied yet.",
			"actions": [
				{
					"label": "Run",
					"action_key": "open_run",
					"route": f"Form/APS Planning Run/{doc.planning_run}" if doc.planning_run else "",
					"enabled": 1 if doc.planning_run else 0,
				},
				{
					"label": "Execution",
					"action_key": "open_release_center",
					"route": f"aps-release-center?run_name={doc.planning_run}" if doc.planning_run else "aps-release-center",
					"enabled": 1,
				},
				{
					"label": "Open Execution Scheduling",
					"action_key": "open_scheduling",
					"route": f"Form/Work Order Scheduling/{doc.work_order_scheduling}" if doc.work_order_scheduling else "",
					"enabled": 1 if doc.work_order_scheduling else 0,
				},
			],
		})

	frappe.throw(_("Next-action context is not supported for {0}.").format(doctype))


def promote_schedule_import_to_net_requirement(
	import_batch: str | None = None,
	schedule: str | None = None,
	company: str | None = None,
	existing_work_order_policy: str | None = None,
) -> dict[str, Any]:
	existing_work_order_policy = _normalize_existing_work_order_policy(existing_work_order_policy)
	if import_batch:
		doc = frappe.get_doc("APS Schedule Import Batch", import_batch)
		company = company or doc.company
	if schedule:
		doc = frappe.get_doc("Customer Delivery Schedule", schedule)
		company = company or doc.company
	demand = rebuild_demand_pool(company=company)
	net = rebuild_net_requirements(
		company=company,
		existing_work_order_policy=existing_work_order_policy,
	)
	return {
		"company": company,
		"existing_work_order_policy": existing_work_order_policy,
		"demand_pool": demand,
		"net_requirement": net,
		"next_route": "aps-net-requirement-workbench",
	}


def create_trial_run_from_net_requirement_context(
	company: str | None = None,
	plant_floor: str | None = None,
	plant_floors: list[str] | str | None = None,
	item_code: str | None = None,
	customer: str | None = None,
	horizon_days: int | None = None,
	existing_work_order_policy: str | None = None,
) -> dict[str, Any]:
	existing_work_order_policy = _normalize_existing_work_order_policy(existing_work_order_policy)
	return run_planning_run(
		company=company,
		plant_floor=plant_floor,
		plant_floors=plant_floors,
		horizon_days=horizon_days,
		item_code=item_code,
		customer=customer,
		run_type="Trial",
		existing_work_order_policy=existing_work_order_policy,
	)


def _item_quantity_requires_integer(item_code: str | None) -> bool:
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") if item_code else None
	return bool(stock_uom and cint(frappe.db.get_value("UOM", stock_uom, "must_be_whole_number")))


def _normalize_manual_target_qty(item_code: str | None, target_qty) -> float:
	precision = frappe.get_precision("APS Schedule Segment", "planned_qty") or 6
	qty = flt(target_qty, precision)
	if qty <= 0:
		frappe.throw(_("Target quantity must be greater than zero."), frappe.ValidationError)
	requires_integer = _item_quantity_requires_integer(item_code)
	if requires_integer and abs(qty - round(qty)) > 1e-9:
		frappe.throw(
			_("Target quantity must be a whole number because the stock UOM does not allow fractions."),
			frappe.ValidationError,
		)
	return float(round(qty)) if requires_integer else qty


def _build_manual_quantity_totals(
	result_planned_qty: float,
	current_result_scheduled_qty: float,
	current_segment_qty: float,
	target_qty: float,
) -> dict[str, float]:
	projected_result_qty = max(
		flt(current_result_scheduled_qty) - flt(current_segment_qty) + flt(target_qty),
		0,
	)
	return {
		"projected_result_qty": projected_result_qty,
		"unscheduled_qty": max(flt(result_planned_qty) - projected_result_qty, 0),
		"overproduction_qty": max(projected_result_qty - flt(result_planned_qty), 0),
	}


def _validate_manual_overproduction_confirmation(
	preview: dict[str, Any],
	allow_overproduction: int = 0,
	manual_note: str | None = None,
) -> None:
	if not cint(preview.get("quantity_mode")) or flt(preview.get("overproduction_qty")) <= 0:
		return
	if not cint(allow_overproduction):
		frappe.throw(
			_("This adjustment exceeds the result planned quantity. Confirm manual overproduction before applying."),
			frappe.ValidationError,
		)
	if not (manual_note or "").strip():
		frappe.throw(_("A reason is required when confirming manual overproduction."), frappe.ValidationError)


def _max_conflict_free_qty(
	start_time,
	hourly_capacity: float,
	conflict_start_times: list[Any],
	item_code: str | None,
) -> float | None:
	starts = [get_datetime(value) for value in conflict_start_times if value and get_datetime(value) >= get_datetime(start_time)]
	if not starts:
		return None
	available_hours = max((min(starts) - get_datetime(start_time)).total_seconds() / 3600, 0)
	if available_hours < 0.25:
		return 0
	precision = frappe.get_precision("APS Schedule Segment", "planned_qty") or 6
	raw_qty = max(available_hours * max(flt(hourly_capacity), 0), 0)
	if _item_quantity_requires_integer(item_code):
		return float(math.floor(raw_qty + 1e-9))
	factor = 10**precision
	return math.floor(raw_qty * factor + 1e-9) / factor


def preview_manual_schedule_adjustment(
	segment_name: str,
	target_workstation: str | None = None,
	before_segment_name: str | None = None,
	target_start_time=None,
	target_end_time=None,
	target_qty: float | None = None,
	allow_locked: int = 0,
	allow_risk_override: int = 0,
	allow_overproduction: int = 0,
) -> dict[str, Any]:
	quantity_mode = target_qty not in (None, "")
	if quantity_mode and target_end_time not in (None, ""):
		frappe.throw(_("Target quantity and target end time cannot be supplied together."), frappe.ValidationError)
	segment_rows = frappe.get_all(
		"APS Schedule Segment",
		filters={"name": segment_name},
		fields=[
			"name",
			"parent",
			"parenttype",
			"workstation",
			"plant_floor",
			"start_time",
			"end_time",
			"planned_qty",
			"segment_kind",
			"segment_status",
			"is_locked",
			"is_manual",
			"mould_reference",
			"lane_key",
			"parallel_group",
			"family_group",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"actual_status",
			"actual_completed_qty",
			"actual_start_time",
			"actual_end_time",
		],
		limit=1,
	)
	if not segment_rows:
		frappe.throw(_("APS Schedule Segment {0} was not found.").format(segment_name))
	segment = segment_rows[0]
	if segment.segment_kind == "Family Co-Product":
		frappe.throw(_("Family Co-Product segment cannot be adjusted directly. Move the primary segment instead."))
	if (
		segment.get("linked_work_order")
		or segment.get("linked_work_order_scheduling")
		or segment.get("linked_scheduling_item")
		or segment.get("actual_start_time")
		or segment.get("actual_end_time")
		or flt(segment.get("actual_completed_qty")) > 0
		or segment.get("actual_status") in ("Running", "Completed", "Delayed", "Slow Progress", "Overproduced")
	):
		return {
			"allowed": 0,
			"blocking_reasons": [_("Segment {0} is released or already has execution feedback.").format(segment_name)],
		}
	if (
		cint(segment.is_locked)
		or segment.segment_status in MANUAL_ADJUSTMENT_BLOCKED_SEGMENT_STATUSES
	) and not cint(allow_locked):
		return {
			"allowed": 0,
			"blocking_reasons": [_("Segment {0} is locked or already applied to formal execution.").format(segment_name)],
		}

	result = frappe.get_doc("APS Schedule Result", segment.parent)
	if quantity_mode:
		target_qty = _normalize_manual_target_qty(result.item_code, target_qty)
	run_doc = frappe.get_doc("APS Planning Run", result.planning_run)
	selected_plant_floors = _get_run_selected_plant_floors(run_doc)
	settings = get_settings_dict()
	item_context = _get_item_context(result.item_code, settings)
	target_workstation = target_workstation or segment.workstation
	candidates = [
		row
		for row in _select_machine_candidates(
			item_code=result.item_code,
			item_context=item_context,
			capability_rows=_get_machine_capability_rows(selected_plant_floors),
			plant_floors=selected_plant_floors,
		)
		if row.get("workstation") == target_workstation
	]
	if not candidates:
		return {
			"allowed": 0,
			"blocking_reasons": _diagnose_target_workstation_failure(
				item_code=result.item_code,
				target_workstation=target_workstation,
				plant_floors=selected_plant_floors,
				item_context=item_context,
			),
		}

	candidate = next((row for row in candidates if row.get("mould_reference") == segment.mould_reference), candidates[0])
	previous_rows = frappe.get_all(
		"APS Schedule Segment",
		filters={
			"workstation": target_workstation,
			"name": ("!=", segment_name),
			"parenttype": "APS Schedule Result",
			"parent": ("in", frappe.get_all("APS Schedule Result", filters={"planning_run": run_doc.name}, pluck="name") or [""]),
			"segment_kind": ("!=", "Family Co-Product"),
		},
		fields=["name", "workstation", "start_time", "end_time", "color_code", "material_code", "mould_reference"],
		order_by="end_time asc",
	)
	before_segment = None
	if before_segment_name:
		before_rows = frappe.get_all(
			"APS Schedule Segment",
			filters={"name": before_segment_name},
			fields=["name", "start_time", "workstation"],
			limit=1,
		)
		before_segment = before_rows[0] if before_rows else None
	effective_target_start_time = target_start_time
	if quantity_mode and not effective_target_start_time:
		effective_target_start_time = segment.start_time
	requested_start_time = get_datetime(effective_target_start_time) if effective_target_start_time else None

	base_floor_time = get_datetime("2000-01-01 00:00:00") if effective_target_start_time else get_datetime(now_datetime())
	state = {
		"next_available": base_floor_time,
		"last_color_code": "",
		"last_material_code": "",
		"last_mould_reference": "",
	}
	for row in previous_rows:
		row_end_time = get_datetime(row.end_time)
		if before_segment and row_end_time > get_datetime(before_segment.get("start_time")):
			continue
		if requested_start_time and row_end_time > requested_start_time:
			continue
		if row_end_time >= state["next_available"]:
			state["next_available"] = row_end_time
			state["last_color_code"] = row.get("color_code") or ""
			state["last_material_code"] = row.get("material_code") or ""
			state["last_mould_reference"] = row.get("mould_reference") or ""

	setup_minutes, setup_exceptions, blocked = _estimate_setup_penalty(
		candidate=candidate,
		state=state,
		item_context=item_context,
		settings=settings,
	)
	mold_rows = frappe.db.sql(
		"""
		select
			seg.name,
			seg.workstation,
			seg.start_time,
			seg.end_time,
			res.item_code
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` res on res.name = seg.parent
		where res.planning_run = %s
			and seg.parenttype = 'APS Schedule Result'
			and ifnull(seg.segment_kind, '') != 'Family Co-Product'
			and ifnull(seg.mould_reference, '') = %s
			and seg.name != %s
		order by seg.start_time asc
		""",
		[run_doc.name, candidate.get("mould_reference"), segment_name],
		as_dict=True,
	)
	mold_next_available = base_floor_time
	for row in mold_rows:
		row_end_time = get_datetime(row.get("end_time"))
		if before_segment and row_end_time > get_datetime(before_segment.get("start_time")):
			continue
		if requested_start_time and row_end_time > requested_start_time:
			continue
		mold_next_available = max(mold_next_available, row_end_time)

	earliest_start_time = max(state["next_available"], mold_next_available) + timedelta(minutes=setup_minutes)
	start_time = earliest_start_time
	if effective_target_start_time:
		start_time = get_datetime(effective_target_start_time)
	hourly_capacity = _estimate_hourly_capacity(candidate=candidate, settings=settings)["hourly_capacity_qty"]
	if quantity_mode:
		planned_qty = flt(target_qty)
		end_time = start_time + timedelta(hours=_estimate_run_hours(planned_qty, candidate, settings))
	elif target_end_time:
		end_time = get_datetime(target_end_time)
		if end_time <= start_time:
			blocked = True
			setup_exceptions.append(
				{
					"severity": "Critical",
					"exception_type": "Invalid End Time",
					"message": _("Target end time must be later than the computed start time."),
					"workstation": target_workstation,
					"is_blocking": 1,
				}
			)
		duration_hours = max((end_time - start_time).total_seconds() / 3600, 0)
		planned_qty = max(int(duration_hours * max(hourly_capacity, 0)), 0)
	else:
		planned_qty = flt(segment.planned_qty)
		end_time = start_time + timedelta(hours=_estimate_run_hours(planned_qty, candidate, settings))

	blocking_reasons = []
	workstation_overlap_rows = []
	mold_overlap_rows = []
	override_available = 0
	override_reason = ""
	if effective_target_start_time and start_time < earliest_start_time:
		blocked = True
		blocking_reasons.append(
			_("Target start time {0} is earlier than the earliest feasible start {1}.").format(
				frappe.format(start_time, {"fieldtype": "Datetime"}),
				frappe.format(earliest_start_time, {"fieldtype": "Datetime"}),
			)
		)
	if _has_fda_conflict(item_context, candidate):
		override_reason = _("Target workstation {0} violates FDA restriction.").format(target_workstation)
		if cint(allow_risk_override):
			setup_exceptions.append(
				{
					"severity": "Warning",
					"exception_type": "FDA Override",
					"message": _("Manual override accepted FDA risk on workstation {0}.").format(target_workstation),
					"workstation": target_workstation,
					"resolution_hint": _("Confirm contamination controls and approval before execution."),
					"is_blocking": 0,
				}
			)
		else:
			blocked = True
			override_available = 1
			blocking_reasons.append(override_reason)
	if before_segment and end_time > get_datetime(before_segment.get("start_time")):
		blocked = True
		blocking_reasons.append(_("Moved segment would overlap the target sequence anchor {0}.").format(before_segment_name))
	for row in previous_rows:
		if before_segment and row.get("name") == before_segment_name:
			continue
		if get_datetime(row.get("start_time")) < end_time and get_datetime(row.get("end_time")) > start_time:
			blocked = True
			workstation_overlap_rows.append(row)
			blocking_reasons.append(_("Target timing would overlap workstation segment {0}.").format(row.get("name")))
	for row in mold_rows:
		if get_datetime(row.get("start_time")) < end_time and get_datetime(row.get("end_time")) > start_time:
			blocked = True
			mold_overlap_rows.append(row)
			blocking_reasons.append(
				_("Mold {0} would still overlap segment {1} on workstation {2}.").format(
					candidate.get("mould_reference"),
					row.get("name"),
					row.get("workstation") or "-",
				)
			)
	if planned_qty <= 0:
		blocked = True
		blocking_reasons.append(_("Target timing produces zero quantity. Extend the segment window before saving."))
	if blocked:
		for row in setup_exceptions:
			if row.get("is_blocking"):
				blocking_reasons.append(row.get("message"))
	override_available = 1 if override_available and len(blocking_reasons) == 1 else 0
	blocking_summary = ""
	blocking_context_rows = []
	resolution_suggestions = []
	latest_safe_start_time = None
	if mold_overlap_rows:
		first_conflict = sorted(mold_overlap_rows, key=lambda row: get_datetime(row.get("start_time")))[0]
		duration = get_datetime(end_time) - get_datetime(start_time)
		latest_safe_start_time = get_datetime(first_conflict.get("start_time")) - duration
		blocking_summary = (
			"Mold {0} already has continuous scheduling after the selected time. This segment cannot be moved later by itself."
		).format(candidate.get("mould_reference") or "-")
		blocking_context_rows = [
			{"label": "Current Segment", "value": segment_name},
			{"label": "Target Workstation", "value": target_workstation or "-"},
			{"label": "Target Start", "value": _format_manual_adjustment_datetime(start_time)},
			{"label": "Recalculated End", "value": _format_manual_adjustment_datetime(end_time)},
		]
		if latest_safe_start_time and latest_safe_start_time >= get_datetime(earliest_start_time):
			blocking_context_rows.append(
				{
					"label": "Latest Conflict-Free Start",
					"value": _format_manual_adjustment_datetime(latest_safe_start_time),
				}
			)
		blocking_context_rows.extend(
			[
				{
					"label": "Conflicting Segment",
					"value": "{name} / {item_code} / {workstation} / {start} - {end}".format(
						name=row.get("name") or "-",
						item_code=row.get("item_code") or "-",
						workstation=row.get("workstation") or "-",
						start=_format_manual_adjustment_datetime(row.get("start_time")),
						end=_format_manual_adjustment_datetime(row.get("end_time")),
					),
				}
				for row in mold_overlap_rows
			]
		)
		resolution_suggestions = [
			"Keep the current start time, or choose an earlier time window.",
			"If the whole chain must move later, shift the downstream segments that use the same mold together.",
			"If only part of the quantity should move later, split the current segment first and then move the remaining quantity.",
		]
	elif workstation_overlap_rows:
		blocking_summary = "The target workstation already has scheduling in this time window. The current segment cannot be inserted directly."
		blocking_context_rows = [
			{"label": "Current Segment", "value": segment_name},
			{"label": "Target Workstation", "value": target_workstation or "-"},
			{"label": "Target Start", "value": _format_manual_adjustment_datetime(start_time)},
			{"label": "Recalculated End", "value": _format_manual_adjustment_datetime(end_time)},
		]
		blocking_context_rows.extend(
			[
				{
					"label": "Workstation Conflict",
					"value": "{name} / {start} - {end}".format(
						name=row.get("name") or "-",
						start=_format_manual_adjustment_datetime(row.get("start_time")),
						end=_format_manual_adjustment_datetime(row.get("end_time")),
					),
				}
				for row in workstation_overlap_rows
			]
		)
		resolution_suggestions = [
			"Try a free window on the target workstation.",
			"If you only want to change sequence, place the current segment before the target segment in an available slot.",
		]

	primary_segments = _get_primary_segments_for_result(result.name)
	current_result_scheduled_qty = sum(flt(row.get("planned_qty")) for row in primary_segments)
	quantity_totals = _build_manual_quantity_totals(
		result_planned_qty=flt(result.planned_qty),
		current_result_scheduled_qty=current_result_scheduled_qty,
		current_segment_qty=flt(segment.planned_qty),
		target_qty=planned_qty,
	)
	overproduction_qty = flt(quantity_totals.get("overproduction_qty"))
	requires_overproduction_confirmation = bool(
		quantity_mode and overproduction_qty > 0 and not cint(allow_overproduction)
	)
	if quantity_mode and overproduction_qty > 0 and cint(allow_overproduction):
		setup_exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Manual Overproduction",
				"message": _("Manual overproduction of {0} is being confirmed.").format(
					frappe.format(overproduction_qty, {"fieldtype": "Float"})
				),
				"workstation": target_workstation,
				"is_blocking": 0,
			}
		)

	conflict_start_times = []
	if before_segment:
		before_start = get_datetime(before_segment.get("start_time"))
		conflict_start_times.append(start_time if before_start < start_time else before_start)
	for row in [*previous_rows, *mold_rows]:
		row_start = get_datetime(row.get("start_time"))
		row_end = get_datetime(row.get("end_time"))
		if row_end > start_time:
			conflict_start_times.append(start_time if row_start < start_time else row_start)
	max_conflict_free_qty = _max_conflict_free_qty(
		start_time=start_time,
		hourly_capacity=hourly_capacity,
		conflict_start_times=conflict_start_times,
		item_code=result.item_code,
	)
	conflict_segments = [
		{
			"segment_name": row.get("name"),
			"resource_type": "Workstation",
			"workstation": row.get("workstation") or target_workstation,
			"start_time": row.get("start_time"),
			"end_time": row.get("end_time"),
		}
		for row in workstation_overlap_rows
	]
	conflict_segments.extend(
		[
			{
				"segment_name": row.get("name"),
				"resource_type": "Mold",
				"workstation": row.get("workstation"),
				"mould_reference": candidate.get("mould_reference"),
				"start_time": row.get("start_time"),
				"end_time": row.get("end_time"),
			}
			for row in mold_overlap_rows
		]
	)
	if quantity_mode:
		schedule_explanation = _(
			"Manual quantity adjustment to {0} on {1} with mold {2}."
		).format(
			frappe.format(planned_qty, {"fieldtype": "Float"}),
			target_workstation,
			candidate.get("mould_reference"),
		)
	else:
		schedule_explanation = _("Manual move to {0} with mold {1}.").format(
			target_workstation,
			candidate.get("mould_reference"),
		)

	return {
		"allowed": 0 if blocked else 1,
		"quantity_mode": 1 if quantity_mode else 0,
		"segment_name": segment_name,
		"result_name": result.name,
		"planning_run": run_doc.name,
		"current_workstation": segment.workstation,
		"current_mould_reference": segment.mould_reference,
		"current_start_time": segment.start_time,
		"current_end_time": segment.end_time,
		"target_workstation": target_workstation,
		"target_mould_reference": candidate.get("mould_reference"),
		"target_plant_floor": candidate.get("plant_floor"),
		"lane_key": candidate.get("lane_key"),
		"requested_start_time": requested_start_time,
		"earliest_start_time": earliest_start_time,
		"start_time": start_time,
		"end_time": end_time,
		"duration_hours": max((get_datetime(end_time) - get_datetime(start_time)).total_seconds() / 3600, 0),
		"setup_minutes": setup_minutes,
		"planned_qty": planned_qty,
		"current_qty": flt(segment.planned_qty),
		"target_qty": planned_qty,
		"current_result_scheduled_qty": current_result_scheduled_qty,
		"result_planned_qty": flt(result.planned_qty),
		"projected_result_qty": quantity_totals.get("projected_result_qty"),
		"unscheduled_qty": quantity_totals.get("unscheduled_qty"),
		"overproduction_qty": overproduction_qty,
		"requires_overproduction_confirmation": 1 if requires_overproduction_confirmation else 0,
		"max_conflict_free_qty": max_conflict_free_qty,
		"conflict_segments": conflict_segments,
		"hourly_capacity_qty": hourly_capacity,
		"blocking_reasons": list(dict.fromkeys(blocking_reasons)),
		"blocking_title": "Quantity Adjustment Blocked" if quantity_mode else "Manual Move Blocked",
		"blocking_summary": blocking_summary,
		"blocking_context_rows": blocking_context_rows,
		"resolution_suggestions": resolution_suggestions,
		"latest_safe_start_time": latest_safe_start_time,
		"preview_exceptions": setup_exceptions,
		"override_available": override_available,
		"override_reason": override_reason,
		"schedule_explanation": schedule_explanation,
	}


def _diagnose_target_workstation_failure(
	item_code: str,
	target_workstation: str,
	plant_floors: list[str] | str | None,
	item_context: dict[str, Any] | None = None,
) -> list[str]:
	item_context = item_context or {}
	capability_rows = [
		row
		for row in _get_machine_capability_rows(plant_floors)
		if row.get("workstation") == target_workstation
	]
	if not capability_rows:
		return [
			_("Workstation {0} is not enabled in APS Machine Capability for the selected plant floor scope {1}.").format(
				target_workstation,
				", ".join(_coerce_plant_floor_list(plant_floors=plant_floors))
				or _("Unknown", context="Injection APS"),
			)
		]

	capability = capability_rows[0]
	reasons = []
	if capability.get("machine_status") not in APS_ALLOWED_MACHINE_STATUSES:
		reasons.append(
			_("Workstation {0} is currently {1}.").format(
				target_workstation,
				capability.get("machine_status"),
			)
		)

	mold_rows = _get_available_mold_rows(item_code)
	if not mold_rows:
		reasons.append(_("No active mold is available for {0}.").format(item_code))
		return reasons

	tonnage_candidates = []
	required_tonnages = []
	for mold_row in mold_rows:
		required_tonnage = flt(mold_row.get("machine_tonnage"))
		if required_tonnage > 0:
			required_tonnages.append(required_tonnage)
		if not capability.get("machine_tonnage") or required_tonnage <= 0:
			tonnage_candidates.append(mold_row)
			continue
		if flt(capability.get("machine_tonnage")) >= required_tonnage:
			tonnage_candidates.append(mold_row)

	if not tonnage_candidates:
		minimum_required_tonnage = min(required_tonnages) if required_tonnages else 0
		reasons.append(
			_("Workstation {0} tonnage {1}T does not meet the minimum mold tonnage {2}T required for {3}.").format(
				target_workstation,
				frappe.format(capability.get("machine_tonnage") or 0, {"fieldtype": "Float"}),
				frappe.format(minimum_required_tonnage or 0, {"fieldtype": "Float"}),
				item_code,
			)
		)
		return reasons

	workstation_rules = frappe.get_all(
		"APS Mould-Machine Rule",
		filters=_strip_none({"item_code": item_code, "workstation": target_workstation, "is_active": 1}),
		fields=["workstation", "priority", "preferred", "mould_reference", "min_tonnage", "max_tonnage"],
		order_by="preferred desc, priority asc",
	)
	if workstation_rules:
		matched_rules = [
			_match_rule_for_candidate(workstation_rules, capability, mold_row)
			for mold_row in tonnage_candidates
		]
		if not any(matched_rules):
			reasons.append(
				_("APS Mould-Machine Rule does not allow item {0} on workstation {1}.").format(
					item_code,
					target_workstation,
				)
			)
			return reasons

	if item_context and any(_has_fda_conflict(item_context, {**capability, "risk_category": capability.get("risk_category")}) for _ in tonnage_candidates):
		reasons.append(
			_("Workstation {0} violates FDA restriction for {1}. Use manual risk override only if approved.").format(
				target_workstation,
				item_code,
			)
		)

	return reasons or [
		_("Workstation {0} is not a valid lane for {1}.").format(target_workstation, item_code)
	]


def _format_manual_adjustment_datetime(value) -> str:
	if not value:
		return "-"
	return frappe.format(get_datetime(value), {"fieldtype": "Datetime"})


def _segment_duration_hours(segment: dict[str, Any]) -> float:
	if not segment.get("start_time") or not segment.get("end_time"):
		return 0
	return max((get_datetime(segment.get("end_time")) - get_datetime(segment.get("start_time"))).total_seconds() / 3600, 0)


def _segment_effective_hourly_rate(segment: dict[str, Any]) -> float:
	duration_hours = _segment_duration_hours(segment)
	if duration_hours <= 0:
		return 0
	return flt(segment.get("planned_qty")) / duration_hours


def _is_segment_execution_protected(segment: dict[str, Any]) -> bool:
	if cint(segment.get("is_locked")):
		return True
	if segment.get("segment_status") in MANUAL_ADJUSTMENT_BLOCKED_SEGMENT_STATUSES:
		return True
	if segment.get("linked_work_order") or segment.get("linked_work_order_scheduling") or segment.get("linked_scheduling_item"):
		return True
	if segment.get("actual_start_time") or segment.get("actual_end_time") or flt(segment.get("actual_completed_qty")) > 0:
		return True
	if segment.get("actual_status") in ("Running", "Completed", "Delayed", "Slow Progress", "Overproduced"):
		return True
	return False


def _get_segment_with_result(segment_name: str) -> tuple[dict[str, Any], Any, Any]:
	rows = frappe.get_all(
		"APS Schedule Segment",
		filters={"name": segment_name},
		fields=[
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
			"schedule_explanation",
			"manual_change_note",
			"segment_note",
			"risk_flags",
			"segment_status",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"actual_status",
			"actual_completed_qty",
			"actual_good_qty",
			"actual_scrap_qty",
			"actual_start_time",
			"actual_end_time",
			"delay_minutes",
			"last_actual_report_time",
			"execution_source_documents",
			"last_execution_sync_on",
			"production_mode",
			"capacity_bucket_start",
			"capacity_bucket_end",
			"available_capacity_qty",
			"occupied_capacity_qty",
			"remaining_capacity_qty",
			"load_percent",
			"projected_late_qty",
			"prebuildable_qty",
			"anchor_strength",
			"execution_anchor_source",
			"color_code",
			"material_code",
			"is_locked",
			"is_manual",
		],
		limit=1,
	)
	if not rows:
		frappe.throw(_("APS Schedule Segment {0} was not found.").format(segment_name))
	segment = rows[0]
	result_doc = frappe.get_doc("APS Schedule Result", segment.parent)
	run_doc = frappe.get_doc("APS Planning Run", result_doc.planning_run)
	return segment, result_doc, run_doc


def _record_segment_adjustment(
	adjustment_type: str,
	run_doc,
	result_doc,
	segment: dict[str, Any],
	target_start_time=None,
	target_end_time=None,
	target_qty: float | None = None,
	target_workstation: str | None = None,
	target_mould_reference: str | None = None,
	split_group: str | None = None,
	split_index: int | None = None,
	split_reason: str | None = None,
	downtime_window: str | None = None,
	impact_summary: str | None = None,
	payload: dict[str, Any] | None = None,
	status: str = "Confirmed",
):
	if not frappe.db.exists("DocType", "APS Segment Adjustment"):
		return None
	doc = frappe.get_doc(
		{
			"doctype": "APS Segment Adjustment",
			"planning_run": run_doc.name,
			"company": run_doc.company,
			"plant_floor": segment.get("plant_floor") or result_doc.plant_floor,
			"net_requirement": result_doc.net_requirement,
			"result_reference": result_doc.name,
			"segment_reference": segment.get("name"),
			"adjustment_type": adjustment_type,
			"status": status,
			"item_code": result_doc.item_code,
			"customer": result_doc.customer,
			"workstation": segment.get("workstation"),
			"target_workstation": target_workstation or segment.get("workstation"),
			"target_mould_reference": target_mould_reference or segment.get("mould_reference"),
			"target_start_time": target_start_time,
			"target_end_time": target_end_time,
			"target_qty": flt(target_qty),
			"split_group": split_group,
			"split_index": split_index or 0,
			"split_reason": split_reason,
			"downtime_window": downtime_window,
			"impact_summary": impact_summary,
			"payload_json": json.dumps(payload or {}, default=str, ensure_ascii=False, indent=2),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _apply_segments_to_planning_state(
	segments: list[dict[str, Any]],
	workstation_state: dict[str, dict[str, Any]],
	mold_state: dict[str, dict[str, Any]],
):
	for segment in segments:
		state = workstation_state.get(segment.get("workstation"))
		end_time = get_datetime(segment.get("end_time"))
		if state is not None:
			state["next_available"] = end_time
			state["last_color_code"] = segment.get("color_code") or ""
			state["last_material_code"] = segment.get("material_code") or ""
			state["last_mould_reference"] = segment.get("mould_reference") or ""
			state["last_end_time"] = end_time
		mold_name = segment.get("mould_reference")
		if mold_name:
			mold_state[mold_name] = {
				"next_available": end_time,
				"last_workstation": segment.get("workstation") or "",
				"last_end_time": end_time,
				"anchor_item_code": _normalize_item_code(segment.get("primary_item_code")),
				"anchor_strength": segment.get("anchor_strength") or ANCHOR_STRENGTH_SOFT,
				"anchor_source": segment.get("execution_anchor_source") or "APS Segment Adjustment",
				"anchor_campaign_key": segment.get("campaign_key") or "",
			}


def _get_confirmed_adjustment_rows(run_name: str, net_requirement: str | None) -> list[dict[str, Any]]:
	if not net_requirement or not frappe.db.exists("DocType", "APS Segment Adjustment"):
		return []
	return frappe.get_all(
		"APS Segment Adjustment",
		filters={
			"planning_run": run_name,
			"net_requirement": net_requirement,
			"status": ("in", CONFIRMED_ADJUSTMENT_STATUSES),
			"adjustment_type": ("in", ["Split", "Move", "Resize"]),
		},
		fields=[
			"name",
			"adjustment_type",
			"target_workstation",
			"target_mould_reference",
			"target_start_time",
			"target_end_time",
			"target_qty",
			"split_group",
			"split_reason",
			"payload_json",
			"modified",
		],
		order_by="target_start_time asc, modified asc",
	)


def _get_adjustment_pieces(adjustment: dict[str, Any]) -> list[dict[str, Any]]:
	payload = {}
	if adjustment.get("payload_json"):
		try:
			payload = json.loads(adjustment.get("payload_json") or "{}")
		except Exception:
			payload = {}
	if payload.get("proposed_segments"):
		return [
			{
				"start_time": row.get("start_time"),
				"end_time": row.get("end_time"),
				"planned_qty": row.get("planned_qty"),
				"split_index": row.get("split_index"),
			}
			for row in payload.get("proposed_segments") or []
		]
	if payload.get("start_time") or payload.get("end_time"):
		return [
			{
				"start_time": payload.get("start_time"),
				"end_time": payload.get("end_time"),
				"planned_qty": payload.get("planned_qty"),
				"split_index": 0,
			}
		]
	return [
		{
			"start_time": adjustment.get("target_start_time"),
			"end_time": adjustment.get("target_end_time"),
			"planned_qty": adjustment.get("target_qty"),
			"split_index": 0,
		}
	]


def _build_confirmed_adjustment_best(
	run_doc,
	net_row: dict[str, Any],
	item_context: dict[str, Any],
	qty: float,
	candidates: list[dict[str, Any]],
	settings: dict[str, Any],
	workstation_state: dict[str, dict[str, Any]],
	mold_state: dict[str, dict[str, Any]],
	horizon_start,
	horizon_end,
	downtime_windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
	adjustments = _get_confirmed_adjustment_rows(run_doc.name, net_row.get("name"))
	if not adjustments:
		return None
	segments = []
	exceptions = []
	remaining_qty = flt(qty)
	sequence_no = 1
	for adjustment in adjustments:
		if remaining_qty <= 0:
			break
		candidate = next(
			(
				row
				for row in candidates
				if row.get("workstation") == adjustment.get("target_workstation")
				and (
					not adjustment.get("target_mould_reference")
					or row.get("mould_reference") == adjustment.get("target_mould_reference")
				)
			),
			None,
		)
		if not candidate:
			exceptions.append(
				{
					"severity": "Warning",
					"exception_type": "Adjustment Replay Skipped",
					"message": _("Confirmed adjustment {0} could not find its target machine/mold lane.").format(adjustment.get("name")),
					"workstation": adjustment.get("target_workstation"),
					"resolution_hint": _("Review APS Segment Adjustment or current machine capability."),
					"is_blocking": 0,
				}
			)
			continue
		matching_windows = _get_matching_downtime_windows(
			downtime_windows,
			workstation=candidate.get("workstation"),
			plant_floor=candidate.get("plant_floor"),
		)
		for piece in _get_adjustment_pieces(adjustment):
			if remaining_qty <= 0:
				break
			if not piece.get("start_time") or not piece.get("end_time"):
				continue
			start_time = get_datetime(piece.get("start_time"))
			end_time = get_datetime(piece.get("end_time"))
			if end_time <= start_time:
				continue
			duration = end_time - start_time
			state = workstation_state.get(candidate.get("workstation")) or {}
			mold_row = mold_state.get(candidate.get("mould_reference")) or {}
			start_time = max(
				get_datetime(horizon_start),
				start_time,
				get_datetime(state.get("next_available") or horizon_start),
				get_datetime(mold_row.get("next_available") or horizon_start),
			)
			start_time = _shift_start_past_downtime(start_time, matching_windows)
			end_time = start_time + duration
			while True:
				overlap = next((row for row in matching_windows if _intervals_overlap(start_time, end_time, row.get("start_time"), row.get("end_time"))), None)
				if not overlap:
					break
				start_time = get_datetime(overlap.get("end_time"))
				end_time = start_time + duration
			if start_time >= get_datetime(horizon_end):
				continue
			planned_qty = min(remaining_qty, flt(piece.get("planned_qty") or adjustment.get("target_qty")))
			if planned_qty <= 0:
				continue
			segment = {
				"workstation": candidate.get("workstation"),
				"plant_floor": candidate.get("plant_floor"),
				"start_time": start_time,
				"end_time": end_time,
				"planned_qty": planned_qty,
				"sequence_no": sequence_no,
				"lane_key": candidate.get("lane_key"),
				"campaign_key": _build_campaign_key(net_row.get("item_code"), candidate.get("mould_reference"), candidate.get("workstation")),
				"parallel_group": "",
				"family_group": "",
				"segment_kind": "Manual",
				"primary_item_code": net_row.get("item_code"),
				"co_product_item_code": "",
				"setup_minutes": 0,
				"changeover_minutes": 0,
				"mould_reference": candidate.get("mould_reference"),
				"schedule_explanation": _("Replayed confirmed APS segment adjustment {0}.").format(adjustment.get("name")),
				"manual_change_note": adjustment.get("name"),
				"original_segment": "",
				"split_group": adjustment.get("split_group") or "",
				"split_index": cint(piece.get("split_index") or 0),
				"split_reason": adjustment.get("split_reason") or "",
				"risk_flags": "",
				"segment_status": "Planned",
				"anchor_strength": ANCHOR_STRENGTH_SOFT,
				"execution_anchor_source": "APS Segment Adjustment",
				"color_code": item_context.get("color_code"),
				"material_code": item_context.get("material_code"),
				"is_locked": 0,
				"is_manual": 1,
			}
			segments.append(segment)
			_apply_segments_to_planning_state([segment], workstation_state, mold_state)
			remaining_qty -= planned_qty
			sequence_no += 1
	if not segments:
		return None
	scheduled_qty = sum(flt(segment.get("planned_qty")) for segment in segments)
	due_datetime = _get_due_datetime(net_row.get("demand_date"))
	risk_status = "Attention" if any(get_datetime(segment.get("end_time")) > due_datetime for segment in segments) else "Normal"
	return {
		"scheduled_qty": scheduled_qty,
		"unscheduled_qty": max(flt(qty) - scheduled_qty, 0),
		"result_status": "Risk" if risk_status != "Normal" else "Planned",
		"risk_status": risk_status,
		"segments": segments,
		"selected_moulds": list(dict.fromkeys(segment.get("mould_reference") for segment in segments if segment.get("mould_reference"))),
		"copy_mold_parallel": 0,
		"family_mold_result": 0,
		"primary_mould_reference": segments[0].get("mould_reference") if segments else "",
		"schedule_explanation": _("Replayed confirmed manual APS adjustments before scheduling residual quantity."),
		"family_side_outputs": [],
		"family_output_summary": "",
		"exceptions": exceptions,
	}


def _build_segment_split_preview(
	segment_name: str,
	split_time=None,
	split_qty: float | None = None,
	downtime_window: str | None = None,
	split_reason: str | None = None,
) -> dict[str, Any]:
	segment, result_doc, run_doc = _get_segment_with_result(segment_name)
	if segment.segment_kind == "Family Co-Product":
		frappe.throw(_("Family Co-Product segment cannot be adjusted directly. Move the primary segment instead."))
	if _is_segment_execution_protected(segment):
		return {
			"allowed": 0,
			"blocking_reasons": [_("Segment {0} is locked, released, or already has execution feedback.").format(segment_name)],
		}
	rate = _segment_effective_hourly_rate(segment)
	if rate <= 0:
		return {"allowed": 0, "blocking_reasons": [_("Segment has no usable capacity rate for splitting.")]}
	start_time = get_datetime(segment.start_time)
	end_time = get_datetime(segment.end_time)
	total_qty = flt(segment.planned_qty)
	reason = split_reason or _("Manual Split", context="Injection APS")
	after_start_time = None
	if downtime_window:
		if not frappe.db.exists("APS Downtime Window", downtime_window):
			frappe.throw(_("APS Downtime Window {0} was not found.").format(downtime_window))
		window = frappe.get_doc("APS Downtime Window", downtime_window)
		if not _downtime_applies_to_target(
			window.as_dict(),
			workstation=segment.workstation,
			plant_floor=segment.plant_floor,
			company=run_doc.company,
		):
			return {"allowed": 0, "blocking_reasons": [_("Downtime window does not apply to this segment lane.")]}
		if not _intervals_overlap(start_time, end_time, window.start_time, window.end_time):
			return {"allowed": 0, "blocking_reasons": [_("Downtime window does not overlap this segment.")]}
		split_dt = max(start_time, min(end_time, get_datetime(window.start_time)))
		after_start_time = max(get_datetime(window.end_time), split_dt)
		reason = window.reason or _("Downtime Window", context="Injection APS")
	elif split_time:
		split_dt = get_datetime(split_time)
	elif split_qty is not None:
		before_qty = flt(split_qty)
		if before_qty <= 0 or before_qty >= total_qty:
			return {"allowed": 0, "blocking_reasons": [_("Split quantity must be between 0 and the segment planned quantity.")]}
		split_dt = start_time + timedelta(hours=before_qty / rate)
	else:
		return {"allowed": 0, "blocking_reasons": [_("Provide split_time, split_qty, or downtime_window.")]}
	if split_dt <= start_time or split_dt >= end_time:
		return {"allowed": 0, "blocking_reasons": [_("Split point must be inside the segment time window.")]}
	before_qty = min(max((split_dt - start_time).total_seconds() / 3600 * rate, 0), total_qty)
	after_qty = max(total_qty - before_qty, 0)
	if before_qty <= 0 or after_qty <= 0:
		return {"allowed": 0, "blocking_reasons": [_("Split would create an empty segment.")]}
	after_start_time = after_start_time or split_dt
	after_end_time = after_start_time + timedelta(hours=after_qty / rate)
	split_group = f"SPL-{frappe.generate_hash(length=8)}"
	proposed_segments = [
		{
			"segment_name": segment.name,
			"start_time": start_time,
			"end_time": split_dt,
			"planned_qty": before_qty,
			"split_index": 1,
			"is_existing": 1,
		},
		{
			"segment_name": None,
			"start_time": after_start_time,
			"end_time": after_end_time,
			"planned_qty": after_qty,
			"split_index": 2,
			"is_existing": 0,
		},
	]
	return {
		"allowed": 1,
		"planning_run": run_doc.name,
		"result_name": result_doc.name,
		"segment_name": segment.name,
		"split_group": split_group,
		"split_reason": reason,
		"downtime_window": downtime_window,
		"hourly_capacity_qty": rate,
		"total_qty": total_qty,
		"proposed_segments": proposed_segments,
		"impact_summary": _("Split {0} into {1} + {2}.").format(
			segment.name,
			frappe.format(before_qty, {"fieldtype": "Float"}),
			frappe.format(after_qty, {"fieldtype": "Float"}),
		),
	}


def preview_segment_split(
	segment_name: str,
	split_time=None,
	split_qty: float | None = None,
	downtime_window: str | None = None,
	split_reason: str | None = None,
) -> dict[str, Any]:
	return _build_segment_split_preview(
		segment_name=segment_name,
		split_time=split_time,
		split_qty=split_qty,
		downtime_window=downtime_window,
		split_reason=split_reason,
	)


def apply_segment_split(
	segment_name: str,
	split_time=None,
	split_qty: float | None = None,
	downtime_window: str | None = None,
	split_reason: str | None = None,
) -> dict[str, Any]:
	preview = preview_segment_split(
		segment_name=segment_name,
		split_time=split_time,
		split_qty=split_qty,
		downtime_window=downtime_window,
		split_reason=split_reason,
	)
	if not preview.get("allowed"):
		frappe.throw(
			"\n".join(
				preview.get("blocking_reasons")
				or [_("Segment split is blocked.", context="Injection APS")]
			)
		)
	segment, result_doc, run_doc = _get_segment_with_result(segment_name)
	result_doc = frappe.get_doc("APS Schedule Result", result_doc.name)
	original_child = next((row for row in result_doc.segments if row.name == segment_name), None)
	if not original_child:
		frappe.throw(_("APS Schedule Segment {0} was not found on its result.").format(segment_name))
	proposed = preview.get("proposed_segments") or []
	first = proposed[0]
	second = proposed[1]
	original_child.end_time = first["end_time"]
	original_child.planned_qty = first["planned_qty"]
	original_child.segment_kind = "Manual"
	original_child.is_manual = 1
	original_child.manual_change_note = preview.get("impact_summary")
	original_child.original_segment = original_child.original_segment or segment_name
	original_child.split_group = preview.get("split_group")
	original_child.split_index = 1
	original_child.split_reason = preview.get("split_reason")
	new_child = result_doc.append(
		"segments",
		{
			"workstation": segment.workstation,
			"plant_floor": segment.plant_floor,
			"start_time": second["start_time"],
			"end_time": second["end_time"],
			"planned_qty": second["planned_qty"],
			"sequence_no": cint(segment.sequence_no) + 1,
			"lane_key": segment.lane_key,
			"campaign_key": segment.campaign_key,
			"parallel_group": segment.parallel_group,
			"family_group": segment.family_group,
			"segment_kind": "Manual",
			"primary_item_code": segment.primary_item_code,
			"co_product_item_code": segment.co_product_item_code,
			"setup_minutes": 0,
			"changeover_minutes": 0,
			"mould_reference": segment.mould_reference,
			"schedule_explanation": segment.schedule_explanation,
			"manual_change_note": preview.get("impact_summary"),
			"segment_note": segment.segment_note,
			"original_segment": segment_name,
			"split_group": preview.get("split_group"),
			"split_index": 2,
			"split_reason": preview.get("split_reason"),
			"risk_flags": segment.risk_flags,
			"segment_status": "Planned",
			"anchor_strength": ANCHOR_STRENGTH_SOFT,
			"execution_anchor_source": "Manual Split",
			"color_code": segment.color_code,
			"material_code": segment.material_code,
			"is_locked": 0,
			"is_manual": 1,
		},
	)
	if segment.family_group:
		_apply_family_split_for_primary(result_doc, segment, first, second, preview)
	result_doc.save(ignore_permissions=True)
	from injection_aps.services import capacity_balance

	capacity_balance.invalidate_capacity_balance(run_doc.name)
	consistency_summary = _refresh_result_after_manual_adjustment(result_doc.name)
	if run_doc.status in ("Approved", "Work Order Proposed", "Shift Proposed", "Applied"):
		run_doc.db_set({"status": "Planned", "approval_state": "Pending"})
	_record_segment_adjustment(
		"Split",
		run_doc,
		result_doc,
		segment,
		target_start_time=first["start_time"],
		target_end_time=second["end_time"],
		target_qty=preview.get("total_qty"),
		split_group=preview.get("split_group"),
		split_reason=preview.get("split_reason"),
		downtime_window=preview.get("downtime_window"),
		impact_summary=preview.get("impact_summary"),
		payload=preview,
	)
	return {
		"planning_run": run_doc.name,
		"result_name": result_doc.name,
		"segment_name": segment_name,
		"new_segment_name": new_child.name,
		"split_group": preview.get("split_group"),
		"consistency": consistency_summary,
		"next_actions": get_next_actions_for_context("APS Planning Run", run_doc.name),
	}


def _apply_family_split_for_primary(result_doc, primary_segment: dict[str, Any], first: dict[str, Any], second: dict[str, Any], preview: dict[str, Any]):
	base_qty = flt(primary_segment.get("planned_qty"))
	if base_qty <= 0:
		return
	family_rows = [
		row
		for row in result_doc.segments
		if row.name != primary_segment.get("name")
		and row.family_group == primary_segment.get("family_group")
		and row.segment_kind == "Family Co-Product"
	]
	for row in family_rows:
		ratio = flt(row.planned_qty) / base_qty
		first_qty = flt(first.get("planned_qty")) * ratio
		second_qty = flt(second.get("planned_qty")) * ratio
		row.end_time = first.get("end_time")
		row.planned_qty = first_qty
		row.manual_change_note = preview.get("impact_summary")
		row.original_segment = row.original_segment or row.name
		row.split_group = preview.get("split_group")
		row.split_index = 1
		row.split_reason = preview.get("split_reason")
		result_doc.append(
			"segments",
			{
				"workstation": row.workstation,
				"plant_floor": row.plant_floor,
				"start_time": second.get("start_time"),
				"end_time": second.get("end_time"),
				"planned_qty": second_qty,
				"sequence_no": cint(row.sequence_no) + 1,
				"lane_key": row.lane_key,
				"campaign_key": row.campaign_key,
				"parallel_group": row.parallel_group,
				"family_group": row.family_group,
				"segment_kind": "Family Co-Product",
				"primary_item_code": row.primary_item_code,
				"co_product_item_code": row.co_product_item_code,
				"setup_minutes": 0,
				"changeover_minutes": 0,
				"mould_reference": row.mould_reference,
				"schedule_explanation": row.schedule_explanation,
				"manual_change_note": preview.get("impact_summary"),
				"segment_note": row.segment_note,
				"original_segment": row.name,
				"split_group": preview.get("split_group"),
				"split_index": 2,
				"split_reason": preview.get("split_reason"),
				"risk_flags": row.risk_flags,
				"segment_status": "Planned",
				"anchor_strength": ANCHOR_STRENGTH_SOFT,
				"execution_anchor_source": "Manual Split",
				"color_code": row.color_code,
				"material_code": row.material_code,
				"is_locked": 0,
				"is_manual": 1,
			},
		)


def create_or_update_downtime_window(
	name: str | None = None,
	company: str | None = None,
	scope: str | None = None,
	plant_floor: str | None = None,
	workstation: str | None = None,
	start_time=None,
	end_time=None,
	available_capacity_percent: float | None = None,
	reason: str | None = None,
	status: str | None = "Active",
	planning_run: str | None = None,
	notes: str | None = None,
) -> dict[str, Any]:
	if not frappe.db.exists("DocType", "APS Downtime Window"):
		frappe.throw(_("APS Downtime Window is not installed. Run migrate first."))
	if name:
		doc = frappe.get_doc("APS Downtime Window", name)
	else:
		doc = frappe.get_doc({"doctype": "APS Downtime Window"})
	previous_scope = {
		"company": doc.get("company"),
		"start_time": doc.get("start_time"),
		"end_time": doc.get("end_time"),
	} if name else None
	if planning_run and not company:
		company = frappe.db.get_value("APS Planning Run", planning_run, "company")
	doc.company = company or doc.company or get_settings_dict().get("default_company")
	doc.scope = scope or doc.scope or ("Workstation" if workstation else "Plant Floor")
	doc.plant_floor = plant_floor or doc.plant_floor
	doc.workstation = workstation or doc.workstation
	doc.start_time = get_datetime(start_time or doc.start_time)
	doc.end_time = get_datetime(end_time or doc.end_time)
	if available_capacity_percent is not None:
		doc.available_capacity_percent = flt(available_capacity_percent)
	doc.reason = reason if reason is not None else doc.reason
	doc.status = status or doc.status or "Active"
	doc.planning_run = planning_run or doc.planning_run
	doc.notes = notes if notes is not None else doc.notes
	doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)
	from injection_aps.services import capacity_balance

	affected_runs = set()
	for window_scope in (
		previous_scope,
		{
			"company": doc.company,
			"start_time": doc.start_time,
			"end_time": doc.end_time,
		},
	):
		if not window_scope or not all(
			window_scope.get(fieldname) for fieldname in ("company", "start_time", "end_time")
		):
			continue
		affected_runs.update(
			frappe.get_all(
				"APS Planning Run",
				filters={
					"company": window_scope["company"],
					"status": ("!=", "Closed"),
					"horizon_start": ("<", window_scope["end_time"]),
					"horizon_end": (">", window_scope["start_time"]),
				},
				pluck="name",
				limit_page_length=0,
			)
		)
	for affected_run in sorted(affected_runs):
		capacity_balance.invalidate_capacity_balance(affected_run)
	impact = None
	if planning_run:
		impact = preview_schedule_impact(run_name=planning_run, downtime_window=doc.name)
	return {
		"downtime_window": doc.name,
		"status": doc.status,
		"impact_preview": impact,
	}


def _get_run_impact_segments(run_name: str) -> list[dict[str, Any]]:
	result_rows = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=["name", "item_code", "customer", "requested_date", "net_requirement"],
	)
	result_map = {row.name: row for row in result_rows}
	segment_rows = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": ("in", list(result_map) or [""]), "segment_kind": ("!=", "Family Co-Product")},
		fields=[
			"name",
			"parent",
			"workstation",
			"plant_floor",
			"start_time",
			"end_time",
			"planned_qty",
			"segment_status",
			"is_locked",
			"is_manual",
			"mould_reference",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"actual_status",
			"actual_completed_qty",
			"actual_start_time",
			"actual_end_time",
		],
		order_by="start_time asc, end_time asc",
	)
	for row in segment_rows:
		parent = result_map.get(row.parent)
		row["item_code"] = parent.item_code if parent else None
		row["customer"] = parent.customer if parent else None
		row["requested_date"] = parent.requested_date if parent else None
		row["net_requirement"] = parent.net_requirement if parent else None
	return segment_rows


def _build_schedule_impact_preview(
	run_name: str,
	downtime_window: str | None = None,
	windows_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	windows = []
	if windows_override is not None:
		windows = [dict(row) for row in windows_override]
	elif downtime_window:
		if not frappe.db.exists("APS Downtime Window", downtime_window):
			frappe.throw(_("APS Downtime Window {0} was not found.").format(downtime_window))
		windows = [frappe.get_doc("APS Downtime Window", downtime_window).as_dict()]
	else:
		windows = _get_active_downtime_windows(
			company=run_doc.company,
			plant_floors=_get_run_selected_plant_floors(run_doc),
			horizon_start=run_doc.horizon_start or now_datetime(),
			horizon_end=run_doc.horizon_end or add_days(today(), run_doc.horizon_days or 14),
			run_name=run_name,
		)
	segments = _get_run_impact_segments(run_name)
	availability_by_workstation: dict[str, Any] = {}
	availability_by_mold: dict[str, Any] = {}
	proposed_updates = []
	blockers = []
	for segment in segments:
		segment_start = get_datetime(segment.start_time)
		segment_end = get_datetime(segment.end_time)
		duration = segment_end - segment_start
		duration_hours = max(duration.total_seconds() / 3600, 0)
		hourly_capacity = flt(segment.planned_qty) / duration_hours if duration_hours > 0 else 0
		matching_windows = _get_matching_downtime_windows(windows, workstation=segment.workstation, plant_floor=segment.plant_floor, company=run_doc.company)
		is_relevant = any(_intervals_overlap(segment_start, segment_end, row.get("start_time"), row.get("end_time")) or segment_start >= get_datetime(row.get("start_time")) for row in matching_windows)
		current_workstation_available = availability_by_workstation.get(segment.workstation)
		current_mold_available = availability_by_mold.get(segment.mould_reference)
		if not is_relevant:
			availability_by_workstation[segment.workstation] = max(current_workstation_available or segment_end, segment_end)
			if segment.mould_reference:
				availability_by_mold[segment.mould_reference] = max(current_mold_available or segment_end, segment_end)
			continue
		if _is_segment_execution_protected(segment):
			if any(_intervals_overlap(segment_start, segment_end, row.get("start_time"), row.get("end_time")) for row in matching_windows):
				blockers.append(
					{
						"segment_name": segment.name,
						"item_code": segment.item_code,
						"reason": _("Segment is locked or already has execution feedback."),
					}
				)
			availability_by_workstation[segment.workstation] = max(current_workstation_available or segment_end, segment_end)
			if segment.mould_reference:
				availability_by_mold[segment.mould_reference] = max(current_mold_available or segment_end, segment_end)
			continue
		new_start = max(segment_start, current_workstation_available or segment_start, current_mold_available or segment_start)
		new_start = _shift_start_past_downtime(new_start, matching_windows)
		new_end = (
			_estimate_end_for_qty_around_downtime(
				start_time=new_start,
				qty=segment.planned_qty,
				hourly_capacity_qty=hourly_capacity,
				downtime_windows=matching_windows,
				horizon_end=run_doc.horizon_end,
			)
			if hourly_capacity > 0
			else new_start + duration
		)
		if new_start != segment_start or new_end != segment_end:
			risk = ""
			if segment.requested_date and new_end > _get_due_datetime(segment.requested_date):
				risk = _("Late Delivery Risk")
			proposed_updates.append(
				{
					"segment_name": segment.name,
					"result_name": segment.parent,
					"item_code": segment.item_code,
					"customer": segment.customer,
					"workstation": segment.workstation,
					"mould_reference": segment.mould_reference,
					"old_start_time": segment_start,
					"old_end_time": segment_end,
					"new_start_time": new_start,
					"new_end_time": new_end,
					"planned_qty": segment.planned_qty,
					"linked_work_order": segment.linked_work_order,
					"linked_work_order_scheduling": segment.linked_work_order_scheduling,
					"wos_action": "Move Existing" if segment.linked_work_order_scheduling else "New",
					"delivery_risk": risk,
					"available_capacity_percent": min(
						[_downtime_capacity_factor(row) * 100 for row in matching_windows] or [100]
					),
				}
			)
		availability_by_workstation[segment.workstation] = new_end
		if segment.mould_reference:
			availability_by_mold[segment.mould_reference] = new_end
	delivery_risks = [row for row in proposed_updates if row.get("delivery_risk")]
	return {
		"allowed": 0 if blockers else 1,
		"planning_run": run_name,
		"downtime_window": downtime_window,
		"capacity_windows": windows,
		"proposed_updates": proposed_updates,
		"affected_count": len(proposed_updates),
		"blockers": blockers,
		"delivery_risks": delivery_risks,
		"wos_changes": [
			{
				"segment_name": row.get("segment_name"),
				"work_order": row.get("linked_work_order"),
				"existing_scheduling": row.get("linked_work_order_scheduling"),
				"action": row.get("wos_action"),
				"planned_qty": row.get("planned_qty"),
				"new_start_time": row.get("new_start_time"),
				"new_end_time": row.get("new_end_time"),
			}
			for row in proposed_updates
		],
	}


def preview_schedule_impact(run_name: str, downtime_window: str | None = None, segment_name: str | None = None) -> dict[str, Any]:
	if not run_name and segment_name:
		run_name = frappe.db.get_value("APS Schedule Result", frappe.db.get_value("APS Schedule Segment", segment_name, "parent"), "planning_run")
	if not run_name:
		frappe.throw(_("Planning Run is required for schedule impact preview."))
	return _build_schedule_impact_preview(run_name=run_name, downtime_window=downtime_window)


def apply_schedule_impact(run_name: str, downtime_window: str | None = None) -> dict[str, Any]:
	preview = preview_schedule_impact(run_name=run_name, downtime_window=downtime_window)
	if not preview.get("allowed"):
		frappe.throw("\n".join(row.get("reason") or row.get("segment_name") for row in preview.get("blockers") or []))
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	applied = 0
	for row in preview.get("proposed_updates") or []:
		segment, result_doc, _run_doc = _get_segment_with_result(row.get("segment_name"))
		frappe.db.set_value(
			"APS Schedule Segment",
			row.get("segment_name"),
			{
				"start_time": row.get("new_start_time"),
				"end_time": row.get("new_end_time"),
				"is_manual": 1,
				"manual_change_note": _("Schedule impact applied from downtime window {0}.").format(downtime_window or "-"),
				"anchor_strength": ANCHOR_STRENGTH_SOFT,
				"execution_anchor_source": "Downtime Impact",
			},
		)
		_record_segment_adjustment(
			"Downtime Impact",
			run_doc,
			result_doc,
			segment,
			target_start_time=row.get("new_start_time"),
			target_end_time=row.get("new_end_time"),
			target_qty=row.get("planned_qty"),
			downtime_window=downtime_window,
			impact_summary=_("Moved by downtime impact preview."),
			payload=row,
			status="Applied",
		)
		applied += 1
	if applied and run_doc.status in ("Approved", "Work Order Proposed", "Shift Proposed", "Applied"):
		run_doc.db_set({"status": "Planned", "approval_state": "Pending"})
	if applied:
		from injection_aps.services import capacity_balance

		capacity_balance.invalidate_capacity_balance(run_name)
	for risk in preview.get("delivery_risks") or []:
		_ensure_open_exception(
			planning_run=run_name,
			severity="Warning",
			exception_type="Late Delivery Risk",
			message=_("Downtime impact moves segment {0} past requested date.").format(risk.get("segment_name")),
			item_code=risk.get("item_code"),
			customer=risk.get("customer"),
			workstation=risk.get("workstation"),
			source_doctype="APS Schedule Segment",
			source_name=risk.get("segment_name"),
			resolution_hint=_("Review customer delivery and release revised WOS proposals."),
			is_blocking=0,
		)
	overlap_summary = _validate_run_segment_overlaps(run_name, persist_exceptions=True)
	mold_overlap_summary = _validate_run_mold_overlaps(run_name, persist_exceptions=True)
	consistency_summary = consistency.recalculate_plan_consistency(
		run_name,
		reason="downtime schedule impact",
	)
	return {
		"planning_run": run_name,
		"applied_count": applied,
		"overlap_count": overlap_summary.get("count"),
		"mold_overlap_count": mold_overlap_summary.get("count"),
		"consistency": consistency_summary,
		"next_actions": get_next_actions_for_context("APS Planning Run", run_name),
	}


def apply_manual_schedule_adjustment(
	segment_name: str,
	target_workstation: str | None = None,
	before_segment_name: str | None = None,
	target_start_time=None,
	target_end_time=None,
	target_qty: float | None = None,
	manual_note: str | None = None,
	allow_locked: int = 0,
	allow_risk_override: int = 0,
	allow_overproduction: int = 0,
) -> dict[str, Any]:
	preview = preview_manual_schedule_adjustment(
		segment_name=segment_name,
		target_workstation=target_workstation,
		before_segment_name=before_segment_name,
		target_start_time=target_start_time,
		target_end_time=target_end_time,
		target_qty=target_qty,
		allow_locked=allow_locked,
		allow_risk_override=allow_risk_override,
		allow_overproduction=allow_overproduction,
	)
	if not preview.get("allowed"):
		frappe.throw("\n".join(preview.get("blocking_reasons") or [_("Manual adjustment is blocked.")]))
	_validate_manual_overproduction_confirmation(
		preview,
		allow_overproduction=allow_overproduction,
		manual_note=manual_note,
	)
	is_overproduction = bool(cint(preview.get("quantity_mode")) and flt(preview.get("overproduction_qty")) > 0)

	rows = frappe.get_all(
		"APS Schedule Segment",
		filters={"name": segment_name},
		fields=["name", "parent", "segment_kind", "family_group"],
		limit=1,
	)
	if not rows:
		frappe.throw(_("APS Schedule Segment {0} was not found.").format(segment_name))
	segment = rows[0]
	filters = {"parent": segment.parent}
	if segment.get("family_group"):
		filters["family_group"] = segment.get("family_group")
	else:
		filters["name"] = segment.name
	child_segments = frappe.get_all(
		"APS Schedule Segment",
		filters=filters,
		fields=["name", "segment_kind", "planned_qty", "risk_flags"],
	)
	result_doc = frappe.get_doc("APS Schedule Result", segment.parent)
	run_doc = frappe.get_doc("APS Planning Run", result_doc.planning_run)
	primary_base_qty = 0.0
	for row in child_segments:
		if row.segment_kind != "Family Co-Product":
			primary_base_qty = flt(row.planned_qty)
			break
	for row in child_segments:
		risk_flags = [value for value in (row.get("risk_flags") or "").splitlines() if value]
		if cint(allow_risk_override):
			risk_flags.append("FDA Override")
		if is_overproduction:
			risk_flags.append("Manual Overproduction")
		planned_qty = preview.get("planned_qty")
		if row.segment_kind == "Family Co-Product":
			ratio = 0
			if primary_base_qty > 0:
				ratio = flt(row.planned_qty) / primary_base_qty
			planned_qty = flt(preview.get("planned_qty")) * ratio if ratio else flt(row.planned_qty)
		values = {
			"workstation": preview["target_workstation"],
			"plant_floor": preview.get("target_plant_floor"),
			"start_time": preview["start_time"],
			"end_time": preview["end_time"],
			"planned_qty": planned_qty,
			"mould_reference": preview["target_mould_reference"],
			"lane_key": preview["lane_key"],
			"campaign_key": _build_campaign_key(result_doc.item_code, preview["target_mould_reference"], preview["target_workstation"]),
			"anchor_strength": ANCHOR_STRENGTH_SOFT,
			"execution_anchor_source": "Manual Adjustment",
			"is_manual": 1,
			"manual_change_note": manual_note or preview.get("schedule_explanation"),
			"risk_flags": "\n".join(dict.fromkeys(risk_flags)),
		}
		if row.segment_kind != "Family Co-Product":
			values["segment_kind"] = "Manual"
		frappe.db.set_value("APS Schedule Segment", row.name, values)

	result_doc.db_set(
		{
			"is_manual": 1,
			"plant_floor": preview.get("target_plant_floor"),
			"status": "Risk" if is_overproduction else result_doc.status,
			"risk_status": "Attention" if cint(allow_risk_override) or is_overproduction else result_doc.risk_status,
			"flow_step": "Manual Adjustment Pending Confirmation",
			"next_step_hint": "Confirm Run",
			"blocking_reason": (
				_("Manual overproduction of {0} was confirmed. Reason: {1}").format(
					frappe.format(preview.get("overproduction_qty"), {"fieldtype": "Float"}),
					(manual_note or "").strip(),
				)
				if is_overproduction
				else _("Manual FDA override was applied.") if cint(allow_risk_override) else ""
			),
			"primary_mould_reference": preview["target_mould_reference"],
			"selected_moulds": preview["target_mould_reference"],
			"schedule_explanation": preview.get("schedule_explanation"),
		}
	)
	from injection_aps.services import capacity_balance

	capacity_balance.invalidate_capacity_balance(run_doc.name)
	if is_overproduction or run_doc.status in ("Approved", "Work Order Proposed", "Shift Proposed", "Applied"):
		run_doc.db_set({"status": "Planned", "approval_state": "Pending"})
	if cint(allow_risk_override):
		_create_exception(
			planning_run=run_doc.name,
			severity="Warning",
			exception_type="FDA Override",
			message=_("Manual adjustment placed {0} on {1} with FDA override.").format(result_doc.item_code, preview["target_workstation"]),
			item_code=result_doc.item_code,
			customer=result_doc.customer,
			workstation=preview["target_workstation"],
			source_doctype="APS Schedule Result",
			source_name=result_doc.name,
			resolution_hint=_("Review override approval before syncing or releasing."),
			is_blocking=0,
		)
	if is_overproduction:
		_create_exception(
			planning_run=run_doc.name,
			severity="Warning",
			exception_type="Manual Overproduction",
			message=_(
				"Manual quantity adjustment for {0} schedules {1} against planned quantity {2}, exceeding it by {3}."
			).format(
				result_doc.item_code,
				frappe.format(preview.get("projected_result_qty"), {"fieldtype": "Float"}),
				frappe.format(preview.get("result_planned_qty"), {"fieldtype": "Float"}),
				frappe.format(preview.get("overproduction_qty"), {"fieldtype": "Float"}),
			),
			item_code=result_doc.item_code,
			customer=result_doc.customer,
			workstation=preview["target_workstation"],
			source_doctype="APS Schedule Segment",
			source_name=segment_name,
			resolution_hint=_("Reason: {0}. Reapprove the APS run before releasing work order proposals.").format(
				(manual_note or "").strip()
			),
			is_blocking=0,
		)
	updated_segment, _updated_result, _updated_run = _get_segment_with_result(segment_name)
	adjustment_payload = dict(preview)
	adjustment_payload.update(
		{
			"allow_overproduction": 1 if cint(allow_overproduction) else 0,
			"manual_note": (manual_note or "").strip(),
		}
	)
	_record_segment_adjustment(
		"Resize" if target_end_time or target_qty not in (None, "") else "Move",
		run_doc,
		result_doc,
		updated_segment,
		target_start_time=preview.get("start_time"),
		target_end_time=preview.get("end_time"),
		target_qty=preview.get("planned_qty"),
		target_workstation=preview.get("target_workstation"),
		target_mould_reference=preview.get("target_mould_reference"),
		impact_summary=manual_note or preview.get("schedule_explanation"),
		payload=adjustment_payload,
	)
	consistency_summary = consistency.recalculate_plan_consistency(
		run_doc.name,
		reason="manual schedule adjustment applied",
	)

	return {
		"segment_name": segment_name,
		"result_name": result_doc.name,
		"planning_run": run_doc.name,
		"current_qty": preview.get("current_qty"),
		"target_qty": preview.get("target_qty"),
		"projected_result_qty": preview.get("projected_result_qty"),
		"unscheduled_qty": preview.get("unscheduled_qty"),
		"overproduction_qty": preview.get("overproduction_qty"),
		"requires_overproduction_confirmation": 0,
		"max_conflict_free_qty": preview.get("max_conflict_free_qty"),
		"consistency": consistency_summary,
		"next_actions": get_next_actions_for_context("APS Planning Run", run_doc.name),
	}


def _refresh_result_after_manual_adjustment(result_name: str):
	run_name = frappe.db.get_value("APS Schedule Result", result_name, "planning_run")
	if not run_name:
		return None
	return consistency.recalculate_plan_consistency(
		run_name,
		reason="manual segment adjustment",
	)


def _build_segment_capacity_snapshot(
	segment_row: dict[str, Any],
	mold_rows: list[dict[str, Any]],
	capability_map: dict[str, dict[str, Any]],
	settings: dict[str, Any],
) -> dict[str, Any]:
	mold_row = next((row for row in mold_rows if row.get("mold") == segment_row.get("mould_reference")), {}) or {}
	capability = capability_map.get(segment_row.get("workstation")) or {}
	candidate = dict(capability)
	candidate["cycle_time_seconds"] = flt(mold_row.get("cycle_time_seconds"))
	candidate["effective_output_qty"] = flt(mold_row.get("effective_output_qty"))
	candidate["output_qty"] = flt(mold_row.get("output_qty"))
	candidate["cavity_output_qty"] = flt(mold_row.get("cavity_output_qty"))
	return _build_capacity_display(candidate, settings)


def get_schedule_result_detail(result_name: str) -> dict[str, Any]:
	result = frappe.get_doc("APS Schedule Result", result_name)
	settings = get_settings_dict()
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": result_name, "parenttype": "APS Schedule Result"},
		fields=[
			"name",
			"workstation",
			"start_time",
			"end_time",
			"planned_qty",
			"sequence_no",
			"lane_key",
			"parallel_group",
			"family_group",
			"segment_kind",
			"primary_item_code",
			"co_product_item_code",
			"mould_reference",
			"segment_status",
			"risk_status",
			"is_locked",
			"is_manual",
			"schedule_explanation",
			"risk_flags",
			"schedule_delay_minutes",
			"segment_note",
			"manual_change_note",
			"original_segment",
			"split_group",
			"split_index",
			"split_reason",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"actual_status",
			"actual_completed_qty",
			"actual_good_qty",
			"actual_scrap_qty",
			"actual_start_time",
			"actual_end_time",
			"delay_minutes",
			"last_actual_report_time",
			"execution_source_documents",
			"last_execution_sync_on",
			"production_mode",
			"capacity_bucket_start",
			"capacity_bucket_end",
			"available_capacity_qty",
			"occupied_capacity_qty",
			"remaining_capacity_qty",
			"load_percent",
			"projected_late_qty",
			"prebuildable_qty",
		],
		order_by="sequence_no asc, idx asc",
	)
	stock_entry_map = _get_latest_stock_entry_by_work_order(
		[row.get("linked_work_order") for row in segments if row.get("linked_work_order")]
	)
	for row in segments:
		row["work_order_route"] = _build_form_route("Work Order", row.get("linked_work_order"))
		row["work_order_scheduling_route"] = _build_form_route("Work Order Scheduling", row.get("linked_work_order_scheduling"))
		row["scheduling_item_route"] = _build_form_route("Scheduling Item", row.get("linked_scheduling_item"))
		stock_entry = stock_entry_map.get(row.get("linked_work_order"))
		row["latest_stock_entry"] = stock_entry.get("name") if stock_entry else None
		row["latest_stock_entry_route"] = _build_form_route("Stock Entry", stock_entry.get("name") if stock_entry else None)
	item_detail = _get_item_detail_snapshot(result.item_code, result.customer, settings)
	source_rows = _get_result_source_rows(result)
	exception_rows = _get_result_exception_rows(result)
	mold_rows = _get_result_mold_rows(result, segments)
	workstations = list(dict.fromkeys(row.get("workstation") for row in segments if row.get("workstation")))
	capability_map = (
		{
			row.workstation: row
			for row in frappe.get_all(
				"APS Machine Capability",
				filters={"workstation": ("in", workstations)},
				fields=["workstation", "hourly_capacity_qty", "daily_capacity_qty", "machine_status"],
			)
		}
		if workstations
		else {}
	)
	for row in segments:
		row.update(_build_segment_capacity_snapshot(row, mold_rows, capability_map, settings))
	from injection_aps.services import availability

	fulfillment_projection = availability.get_result_fulfillment_projection(result_name)
	production_allocations = frappe.get_all(
		"APS Production Allocation",
		filters={"schedule_result": result_name},
		fields=[
			"segment",
			"source_stock_entry",
			"source_stock_entry_detail",
			"scheduling_item",
			"output_type",
			"allocation_method",
			"good_qty",
			"scrap_qty",
			"effective_qty",
			"is_effective",
			"source_posting_time",
			"reversal_reason",
		],
		order_by="source_posting_time desc, creation desc",
	)
	for row in production_allocations:
		row["source_route"] = _build_form_route("Stock Entry", row.source_stock_entry)
		row["scheduling_item_route"] = _build_form_route("Scheduling Item", row.scheduling_item)
	latest_entry_by_segment = {}
	for allocation in production_allocations:
		if allocation.is_effective and allocation.segment and allocation.source_stock_entry:
			latest_entry_by_segment.setdefault(allocation.segment, allocation.source_stock_entry)
	for segment in segments:
		ledger_entry = latest_entry_by_segment.get(segment.name)
		if ledger_entry or segment.get("last_execution_sync_on"):
			segment["latest_stock_entry"] = ledger_entry
			segment["latest_stock_entry_route"] = _build_form_route("Stock Entry", ledger_entry)
	delivery_allocations = []
	schedule_item_names = {
		row.get("customer_schedule_item")
		for row in frappe.get_all(
			"APS Production Allocation",
			filters={"schedule_result": result_name, "customer_schedule_item": ("is", "set")},
			fields=["customer_schedule_item"],
		)
		if row.get("customer_schedule_item")
	}
	schedule_item_names.update(
		frappe.db.sql_list(
			"""
			select i.name
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.company = %s
				and ifnull(s.customer, '') = ifnull(%s, '')
				and i.item_code = %s
				and i.schedule_date = %s
			""",
			(result.company, result.customer, result.item_code, getdate(result.requested_date)),
		)
	)
	for schedule_item in schedule_item_names:
		delivery_allocations.extend(
			frappe.get_all(
				"APS Delivery Allocation",
				filters={"customer_schedule_item": schedule_item},
				fields=[
					"customer_schedule_item",
					"source_delivery_note",
					"source_delivery_note_item",
					"allocation_method",
					"effective_qty",
					"is_return",
					"is_effective",
					"source_posting_time",
					"reversal_reason",
				],
				order_by="source_posting_time desc, creation desc",
			)
		)
	for row in delivery_allocations:
		row["source_route"] = _build_form_route("Delivery Note", row.source_delivery_note)
	return {
		"result": result.as_dict(),
		"segments": segments,
		"item_detail": item_detail,
		"source_rows": source_rows,
		"exception_rows": exception_rows,
		"mold_rows": mold_rows,
		"fulfillment_projection": fulfillment_projection,
		"production_allocations": production_allocations,
		"delivery_allocations": delivery_allocations,
		"routes": {
			"planning_run": _build_form_route("APS Planning Run", result.planning_run),
			"net_requirement": _build_form_route("APS Net Requirement", result.net_requirement),
			"result": _build_form_route("APS Schedule Result", result.name),
		},
		"next_actions": get_next_actions_for_context("APS Planning Run", result.planning_run),
	}


def get_exception_resolution_context(exception_name: str) -> dict[str, Any]:
	doc = frappe.get_doc("APS Exception Log", exception_name)
	return _build_exception_resolution_context(doc)


def detach_standard_references(company: str | None, dry_run: bool = True) -> dict[str, Any]:
	company = str(company or "").strip()
	if not company:
		frappe.throw(
			_("Company is required for the APS standard reference cleanup.", context="Injection APS"),
			frappe.ValidationError,
		)
	rows = []
	for doctype, target in {
		"Work Order": {
			"fieldnames": [
			"custom_aps_run",
			"custom_aps_source",
			"custom_aps_required_delivery_date",
			"custom_aps_is_urgent",
			"custom_aps_release_status",
			"custom_aps_locked_for_reschedule",
			"custom_aps_schedule_reference",
			"custom_aps_result_reference",
			"custom_aps_proposal_batch",
			],
		},
		"Work Order Scheduling": {
			"fieldnames": [
			"custom_aps_run",
			"custom_aps_freeze_state",
			"custom_aps_approval_state",
			],
		},
		"Scheduling Item": {
			"fieldnames": [
			"custom_aps_run",
			"custom_aps_result_reference",
			"custom_aps_segment_reference",
			"custom_aps_shift_proposal",
			],
			"company_parent_doctype": "Work Order Scheduling",
		},
		"Delivery Plan": {
			"fieldnames": [
			"custom_aps_version",
			"custom_aps_source",
			],
		},
	}.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		fieldnames = target["fieldnames"]
		names = _get_records_with_any_field_set(
			doctype,
			fieldnames,
			company=company,
			company_parent_doctype=target.get("company_parent_doctype"),
		)
		rows.append({"doctype": doctype, "count": len(names), "names": names[:20]})
		if not dry_run and names:
			for name in names:
				values = {fieldname: None for fieldname in fieldnames if frappe.get_meta(doctype).has_field(fieldname)}
				frappe.db.set_value(doctype, name, values)
	return {"company": company, "dry_run": cint(dry_run), "rows": rows}


def get_settings_dict() -> dict[str, Any]:
	settings = frappe.get_cached_doc("APS Settings", "APS Settings")
	return {
		"default_company": settings.default_company,
		"default_plant_floor": settings.default_plant_floor,
		"planning_horizon_days": cint(settings.planning_horizon_days or 14),
		"release_horizon_days": cint(settings.release_horizon_days or 1),
		"freeze_days": cint(settings.freeze_days or 2),
		"default_production_strategy": settings.default_production_strategy or "Auto Balance",
		"capacity_bucket_mode": settings.capacity_bucket_mode or "Shift",
		"default_max_prebuild_days": cint(settings.default_max_prebuild_days or 7),
		"high_cancellation_risk_percent": flt(settings.high_cancellation_risk_percent or 60),
		"require_pmc_confirmation_for_risk": cint(settings.require_pmc_confirmation_for_risk),
		"minimum_parallel_split_qty": flt(settings.minimum_parallel_split_qty or 500),
		"minimum_run_window_hours": flt(settings.minimum_run_window_hours or 2),
		"default_setup_minutes": flt(settings.default_setup_minutes or 30),
		"default_first_article_minutes": flt(settings.default_first_article_minutes or 45),
		"mold_change_penalty_minutes": flt(settings.mold_change_penalty_minutes or 30),
		"missing_cycle_fallback_seconds": flt(settings.missing_cycle_fallback_seconds or 60),
		"default_hourly_capacity_qty": flt(settings.default_hourly_capacity_qty or 120),
		"item_food_grade_field": (
			"custom_aps_food_grade"
			if not settings.item_food_grade_field
			or settings.item_food_grade_field == "custom_food_grade"
			else settings.item_food_grade_field
		),
		"item_first_article_field": settings.item_first_article_field or "custom_is_first_article",
		"item_color_field": settings.item_color_field or "color",
		"item_material_field": settings.item_material_field or "material",
		"item_safety_stock_field": settings.item_safety_stock_field or "safety_stock",
		"item_max_stock_field": settings.item_max_stock_field or "custom_aps_max_stock_qty",
		"item_min_batch_field": settings.item_min_batch_field or "min_order_qty",
		"customer_short_name_field": settings.customer_short_name_field or "custom_customer_short_name",
		"workstation_risk_field": settings.workstation_risk_field or "custom_production_risk_category",
		"scheduling_item_risk_field": settings.scheduling_item_risk_field or "custom_workstation_risk_category_",
		"plant_floor_source_warehouse_field": settings.plant_floor_source_warehouse_field or "custom_default_source_warehouse",
		"plant_floor_wip_warehouse_field": settings.plant_floor_wip_warehouse_field or "warehouse",
		"plant_floor_fg_warehouse_field": settings.plant_floor_fg_warehouse_field or "custom_default_finished_goods_warehouse",
		"plant_floor_scrap_warehouse_field": settings.plant_floor_scrap_warehouse_field or "custom_default_scrap_warehouse",
	}


def inspect_customer_delivery_schedule_file(
	file_url: str,
	sheet_name: str | None = None,
	header_row_no: int | None = None,
	max_rows: int = 16,
) -> dict[str, Any]:
	sheet_rows, workbook_context = _read_schedule_workbook_rows(
		file_url=file_url,
		sheet_name=sheet_name,
		max_rows=max_rows,
	)
	guess = _guess_schedule_mapping(sheet_rows, forced_header_row_no=header_row_no)
	return {
		"sheet_names": workbook_context.get("sheet_names") or [],
		"selected_sheet": workbook_context.get("sheet_name"),
		"sample_rows": sheet_rows[:max_rows],
		"column_options": _build_schedule_column_options(sheet_rows, guess.get("header_row_no") or 1),
		"detected_mapping": guess,
	}


def _validate_json_container_depth(payload: str, *, maximum_depth: int, message: str):
	"""Reject deeply nested JSON before the decoder allocates nested containers."""
	depth = 0
	in_string = False
	escaped = False
	for character in payload:
		if in_string:
			if escaped:
				escaped = False
			elif character == "\\":
				escaped = True
			elif character == '"':
				in_string = False
			continue
		if character == '"':
			in_string = True
		elif character in "[{":
			depth += 1
			if depth > maximum_depth:
				frappe.throw(_(message), frappe.ValidationError)
		elif character in "]}":
			depth = max(depth - 1, 0)


def _json_string_payload_size(value: str) -> int:
	"""Return UTF-8 bytes for a JSON string without allocating the encoded JSON."""
	extra_escape_bytes = sum(
		5 if ord(character) < 0x20 else 1 if character in {'"', "\\"} else 0
		for character in value
	)
	return len(value.encode("utf-8")) + extra_escape_bytes + 2


def _json_scalar_payload_size(value: Any) -> int:
	if value is None:
		return 4
	if isinstance(value, bool):
		return 4 if value else 5
	if isinstance(value, str):
		return _json_string_payload_size(value)
	if isinstance(value, int):
		return len(str(value).encode("ascii"))
	if isinstance(value, float):
		if not math.isfinite(value):
			raise ValueError
		return len(repr(value).encode("ascii"))
	if isinstance(value, (datetime, date_cls, time_cls)):
		return _json_string_payload_size(value.isoformat())
	raise TypeError


def _normalize_schedule_rows(
	file_url: str | None = None,
	rows_json: str | list[dict] | None = None,
	mapping_json: str | dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
	mapping = _normalize_schedule_mapping(mapping_json)
	parser_mode = mapping.get("parser_mode") or "rows"
	parse_context = {
		"parser_mode": parser_mode,
		"sheet_name": mapping.get("sheet_name"),
		"mapping": mapping,
	}
	if rows_json:
		if isinstance(rows_json, str):
			if len(rows_json.encode("utf-8")) > MAX_SCHEDULE_ROWS_JSON_BYTES:
				frappe.throw(_("Schedule row payload exceeds the 10 MB safety limit."), frappe.ValidationError)
			_validate_json_container_depth(
				rows_json,
				maximum_depth=2,
				message="Schedule row payload contains nested values that are not allowed.",
			)
			try:
				data_rows = json.loads(rows_json or "[]")
			except (TypeError, ValueError, RecursionError) as exc:
				raise frappe.ValidationError(_("Schedule row payload is not valid JSON.")) from exc
		else:
			data_rows = rows_json or []
		_validate_schedule_rows_payload(data_rows)
		parse_context["parser_mode"] = "rows"
		parse_context["source_mode"] = "rows_json"
		normalized = _normalize_schedule_rows_from_long_rows(data_rows)
		return normalized, parse_context
	if file_url and parser_mode == "matrix":
		rows, matrix_context = _normalize_schedule_rows_from_matrix(file_url=file_url, mapping=mapping)
		parse_context.update(matrix_context)
		return rows, parse_context
	if file_url:
		raw_rows, workbook_context = _read_schedule_workbook_rows(
			file_url=file_url,
			sheet_name=mapping.get("sheet_name"),
		)
		if not raw_rows:
			return [], parse_context
		header_row_no = cint(mapping.get("header_row_no") or 1)
		data_start_row_no = cint(mapping.get("data_start_row_no") or (header_row_no + 1))
		header_idx = max(header_row_no - 1, 0)
		headers = [_normalize_header(cell) for cell in (raw_rows[header_idx] if len(raw_rows) > header_idx else [])]
		data_rows = []
		start_index = max(data_start_row_no - 1, header_idx + 1)
		for row_index, row in enumerate(raw_rows[start_index:], start=start_index + 1):
			if not any(cell not in (None, "") for cell in row):
				continue
			normalized_row = {
				headers[idx]: row[idx]
				for idx in range(min(len(headers), len(row)))
				if headers[idx]
			}
			normalized_row["source_excel_row"] = row_index
			data_rows.append(normalized_row)
		parse_context.update(
			{
				"sheet_name": workbook_context.get("sheet_name"),
				"header_row_no": header_row_no,
				"data_start_row_no": data_start_row_no,
				"source_mode": "file_rows",
			}
		)
		_validate_schedule_rows_payload(data_rows)
	else:
		data_rows = []

	normalized = _normalize_schedule_rows_from_long_rows(data_rows)
	return normalized, parse_context


def _validate_schedule_rows_payload(data_rows: Any):
	if not isinstance(data_rows, list):
		frappe.throw(_("Schedule rows must be an array."), frappe.ValidationError)
	if len(data_rows) > MAX_SCHEDULE_WORKSHEET_ROWS:
		frappe.throw(
			_("Schedule row payload exceeds the {0} row safety limit.").format(MAX_SCHEDULE_WORKSHEET_ROWS),
			frappe.ValidationError,
		)
	payload_size = 2
	for index, row in enumerate(data_rows, start=1):
		if not isinstance(row, dict):
			frappe.throw(_("Schedule row {0} must be an object.").format(index), frappe.ValidationError)
		if len(row) > MAX_SCHEDULE_ROW_FIELDS:
			frappe.throw(
				_("Schedule row {0} exceeds the {1} field safety limit.").format(index, MAX_SCHEDULE_ROW_FIELDS),
				frappe.ValidationError,
			)
		payload_size += _schedule_row_payload_size(row, index=index) + (1 if index > 1 else 0)
		if payload_size > MAX_SCHEDULE_ROWS_JSON_BYTES:
			frappe.throw(_("Schedule row payload exceeds the 10 MB safety limit."), frappe.ValidationError)


def _schedule_row_payload_size(row: dict[str, Any], *, index: int) -> int:
	row_size = 2
	for field_index, (fieldname, value) in enumerate(row.items()):
		if not isinstance(fieldname, str):
			frappe.throw(_("Schedule row {0} contains an invalid field name.").format(index), frappe.ValidationError)
		if len(fieldname) > MAX_SCHEDULE_CELL_CHARACTERS:
			frappe.throw(
				_("Schedule row {0} contains a value longer than the Excel cell limit.").format(index),
				frappe.ValidationError,
			)
		if isinstance(value, str) and len(value) > MAX_SCHEDULE_CELL_CHARACTERS:
			frappe.throw(
				_("Schedule row {0} contains a value longer than the Excel cell limit.").format(index),
				frappe.ValidationError,
			)
		try:
			value_size = _json_scalar_payload_size(value)
		except (TypeError, ValueError):
			frappe.throw(
				_("Schedule row {0} contains a nested or unsupported value.").format(index),
				frappe.ValidationError,
			)
		row_size += (
			(1 if field_index else 0)
			+ _json_string_payload_size(fieldname)
			+ 1
			+ value_size
		)
	return row_size


def _normalize_schedule_rows_from_long_rows(data_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	normalized = []
	for idx, row in enumerate(data_rows or [], start=1):
		item_code = row.get("item_code") or row.get("item") or row.get("item code")
		item_code = str(item_code).strip() if item_code else ""
		schedule_date = row.get("schedule_date") or row.get("schedule date") or row.get("delivery_date") or row.get("delivery date")
		previous_schedule_date = (
			row.get("previous_schedule_date")
			or row.get("previous schedule date")
			or row.get("old_delivery_date")
			or row.get("old delivery date")
		)
		qty_value = row.get("qty") if row.get("qty") is not None else row.get("quantity")
		normalized.append(
			{
				"sales_order": row.get("sales_order") or row.get("sales order"),
				"item_code": item_code,
				"customer_part_no": row.get("customer_part_no") or row.get("customer part no"),
				"schedule_date": getdate(schedule_date) if schedule_date else None,
				"previous_schedule_date": getdate(previous_schedule_date) if previous_schedule_date else None,
				"qty": flt(qty_value),
				"remark": row.get("remark") or row.get("remarks"),
				"source_origin": row.get("source_origin") or "imported",
				"source_excel_row": cint(row.get("source_excel_row") or row.get("excel_row") or row.get("row_no") or 0) or idx,
				"source_excel_rows": row.get("source_excel_rows"),
				"manual_override": cint(row.get("manual_override")),
				"manual_change_reason": row.get("manual_change_reason"),
				"production_strategy": row.get("production_strategy") or row.get("production strategy"),
				"demand_confidence": row.get("demand_confidence") or row.get("demand confidence"),
				"cancellation_risk_percent": (
					row.get("cancellation_risk_percent")
					if row.get("cancellation_risk_percent") is not None
					else row.get("cancellation risk percent")
				),
				"prebuild_allowed": (
					row.get("prebuild_allowed") if row.get("prebuild_allowed") is not None else row.get("prebuild allowed")
				),
				"max_prebuild_days": (
					row.get("max_prebuild_days")
					if row.get("max_prebuild_days") is not None
					else row.get("max prebuild days")
				),
			}
		)
	return normalized


def _normalize_schedule_rows_from_matrix(
	file_url: str,
	mapping: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
	raw_rows, workbook_context = _read_schedule_workbook_rows(
		file_url=file_url,
		sheet_name=mapping.get("sheet_name"),
	)
	if not raw_rows:
		return [], workbook_context

	header_row_no = cint(mapping.get("header_row_no") or 1)
	data_start_row_no = cint(mapping.get("data_start_row_no") or (header_row_no + 1))
	header_idx = max(header_row_no - 1, 0)
	headers = raw_rows[header_idx] if len(raw_rows) > header_idx else []
	item_idx = _coerce_excel_column_index(mapping.get("item_reference_column"))
	customer_part_idx = _coerce_excel_column_index(mapping.get("customer_part_no_column"))
	description_idx = _coerce_excel_column_index(mapping.get("description_column"))
	sales_order_idx = _coerce_excel_column_index(mapping.get("sales_order_column"))
	row_type_idx = _coerce_excel_column_index(mapping.get("row_type_column"))
	remark_idx = _coerce_excel_column_index(mapping.get("remark_column"))
	demand_row_type_value = str(mapping.get("demand_row_type_value") or "").strip()
	date_columns = _resolve_matrix_date_columns(headers, mapping)
	if item_idx is None:
		frappe.throw(_("Please select Item Reference Column before previewing matrix schedule import."))
	if not date_columns:
		frappe.throw(_("No date columns were detected for the selected matrix mapping."))
	normalized = []
	normalized_payload_size = 2

	for row_number, row in enumerate(raw_rows[max(data_start_row_no - 1, header_idx + 1) :], start=max(data_start_row_no, header_row_no + 1)):
		if not any(cell not in (None, "") for cell in row):
			continue
		item_reference = _get_row_value(row, item_idx)
		if not item_reference:
			continue
		row_type_value = str(_get_row_value(row, row_type_idx) or "").strip()
		if demand_row_type_value and row_type_value and row_type_value != demand_row_type_value:
			continue
		if demand_row_type_value and row_type_idx is not None and not row_type_value:
			continue
		item_name = str(item_reference).strip()
		description = _get_row_value(row, description_idx)
		customer_part_no = _get_row_value(row, customer_part_idx) or item_reference
		sales_order = _get_row_value(row, sales_order_idx)
		remark = _get_row_value(row, remark_idx)
		for date_column in date_columns:
			if len(normalized) >= MAX_SCHEDULE_NORMALIZED_ROWS:
				frappe.throw(
					_("Matrix schedule expansion exceeds the {0} row safety limit.").format(
						MAX_SCHEDULE_NORMALIZED_ROWS
					),
					frappe.ValidationError,
				)
			column_idx = date_column["index"]
			qty = flt(_get_row_value(row, column_idx))
			normalized_row = {
				"sales_order": sales_order,
				"item_code": item_name,
				"customer_part_no": customer_part_no,
				"schedule_date": date_column["schedule_date"],
				"qty": qty,
				"remark": _build_matrix_row_remark(
					base_remark=remark,
					description=description,
					row_type=row_type_value,
					cell_ref=f"{get_column_letter(column_idx + 1)}{row_number}",
				),
				"source_excel_row": row_number,
			}
			normalized_payload_size += _schedule_row_payload_size(
				normalized_row,
				index=len(normalized) + 1,
			) + (1 if normalized else 0)
			if normalized_payload_size > MAX_SCHEDULE_ROWS_JSON_BYTES:
				frappe.throw(_("Schedule row payload exceeds the 10 MB safety limit."), frappe.ValidationError)
			normalized.append(normalized_row)

	workbook_context.update(
		{
			"parser_mode": "matrix",
			"header_row_no": header_row_no,
			"data_start_row_no": data_start_row_no,
			"date_column_count": len(date_columns),
			"mapping": mapping,
		}
	)
	return normalized, workbook_context


def _build_matrix_row_remark(
	base_remark: Any = None,
	description: Any = None,
	row_type: Any = None,
	cell_ref: str | None = None,
) -> str:
	parts = []
	if base_remark:
		parts.append(str(base_remark).strip())
	if description:
		parts.append(_("Description: {0}").format(str(description).strip()))
	if row_type:
		parts.append(_("Row Type: {0}").format(str(row_type).strip()))
	if cell_ref:
		parts.append(_("Source Cell: {0}").format(cell_ref))
	return " | ".join(part for part in parts if part)[:MAX_SCHEDULE_CELL_CHARACTERS]


def _normalize_schedule_mapping(mapping_json: str | dict[str, Any] | None) -> dict[str, Any]:
	if not mapping_json:
		return {"parser_mode": "rows"}
	if isinstance(mapping_json, str):
		if len(mapping_json.encode("utf-8")) > MAX_SCHEDULE_MAPPING_JSON_BYTES:
			frappe.throw(_("Schedule mapping exceeds the 256 KB safety limit."), frappe.ValidationError)
		_validate_json_container_depth(
			mapping_json,
			maximum_depth=MAX_SCHEDULE_MAPPING_NESTING_DEPTH,
			message="Schedule mapping nesting exceeds the safety limit.",
		)
		try:
			mapping = json.loads(mapping_json)
		except (TypeError, ValueError, RecursionError) as exc:
			raise frappe.ValidationError(_("Schedule mapping is not valid JSON.")) from exc
	else:
		mapping = mapping_json
	if not isinstance(mapping, dict):
		frappe.throw(_("Schedule mapping must be an object."), frappe.ValidationError)
	if len(mapping) > MAX_SCHEDULE_MAPPING_FIELDS:
		frappe.throw(_("Schedule mapping contains too many fields."), frappe.ValidationError)
	payload_size = 2
	for index, (fieldname, value) in enumerate(mapping.items()):
		if not isinstance(fieldname, str):
			frappe.throw(_("Schedule mapping contains an invalid field name."), frappe.ValidationError)
		if isinstance(value, str) and len(value) > MAX_SCHEDULE_MAPPING_VALUE_CHARACTERS:
			frappe.throw(_("Schedule mapping contains a value that is too long."), frappe.ValidationError)
		try:
			value_size = _json_scalar_payload_size(value)
		except (TypeError, ValueError):
			frappe.throw(_("Schedule mapping must contain only scalar values."), frappe.ValidationError)
		payload_size += (
			(1 if index else 0)
			+ _json_string_payload_size(fieldname)
			+ 1
			+ value_size
		)
		if payload_size > MAX_SCHEDULE_MAPPING_JSON_BYTES:
			frappe.throw(_("Schedule mapping exceeds the 256 KB safety limit."), frappe.ValidationError)
	normalized = {fieldname: value for fieldname, value in mapping.items() if fieldname in SCHEDULE_MAPPING_FIELDS}
	normalized["parser_mode"] = "matrix" if normalized.get("parser_mode") == "matrix" else "rows"
	return normalized


def _read_schedule_workbook_rows(
	file_url: str,
	sheet_name: str | None = None,
	max_rows: int | None = None,
) -> tuple[list[list[Any]], dict[str, Any]]:
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	# Role access to the APS endpoint must not bypass the File document's own
	# private-file ACL.  check_permission raises before the filesystem is read.
	file_doc.check_permission("read")
	file_path = file_doc.get_full_path()
	_validate_schedule_workbook_archive(file_path)
	workbook = load_workbook(filename=file_path, data_only=True, read_only=True)
	selected_sheet_name = ""
	try:
		sheet_names = list(workbook.sheetnames)
		worksheet = workbook[sheet_name] if sheet_name and sheet_name in workbook.sheetnames else workbook.active
		selected_sheet_name = worksheet.title
		if worksheet.max_row > MAX_SCHEDULE_WORKSHEET_ROWS:
			frappe.throw(
				_("Schedule worksheet has {0} rows; the maximum is {1}.").format(
					worksheet.max_row, MAX_SCHEDULE_WORKSHEET_ROWS
				),
				frappe.ValidationError,
			)
		if worksheet.max_column > MAX_SCHEDULE_WORKSHEET_COLUMNS:
			frappe.throw(
				_("Schedule worksheet has {0} columns; the maximum is {1}.").format(
					worksheet.max_column, MAX_SCHEDULE_WORKSHEET_COLUMNS
				),
				frappe.ValidationError,
			)
		if worksheet.max_row * worksheet.max_column > MAX_SCHEDULE_WORKSHEET_CELLS:
			frappe.throw(
				_("Schedule worksheet exceeds the {0} cell safety limit.").format(
					MAX_SCHEDULE_WORKSHEET_CELLS
				),
				frappe.ValidationError,
			)
		row_limit = min(cint(max_rows), MAX_SCHEDULE_WORKSHEET_ROWS) if cint(max_rows) > 0 else None
		rows = []
		for row in worksheet.iter_rows(max_row=row_limit, max_col=worksheet.max_column):
			values = [cell.value for cell in row]
			if any(
				isinstance(value, str) and len(value) > MAX_SCHEDULE_CELL_CHARACTERS
				for value in values
			):
				frappe.throw(
					_("The schedule worksheet contains a value longer than the Excel cell limit."),
					frappe.ValidationError,
				)
			rows.append(values)
	finally:
		workbook.close()
	return rows, {"sheet_name": selected_sheet_name, "sheet_names": sheet_names}


def _validate_schedule_workbook_archive(file_path: str) -> dict[str, int]:
	"""Reject oversized or highly-compressed XLSX archives before OpenPyXL parses them."""
	try:
		file_size = os.path.getsize(file_path)
	except OSError as exc:
		raise frappe.ValidationError(_("The uploaded schedule file cannot be read.")) from exc
	if file_size <= 0 or file_size > MAX_SCHEDULE_FILE_BYTES:
		frappe.throw(
			_("Schedule file size must be between 1 byte and {0} MB.").format(
				MAX_SCHEDULE_FILE_BYTES // (1024 * 1024)
			),
			frappe.ValidationError,
		)
	try:
		with zipfile.ZipFile(file_path) as archive:
			members = archive.infolist()
			if len(members) > MAX_XLSX_ARCHIVE_MEMBERS:
				frappe.throw(
					_("The XLSX workbook contains too many archive members."),
					frappe.ValidationError,
				)
			if any(member.file_size > MAX_XLSX_MEMBER_BYTES for member in members):
				frappe.throw(
					_("An XLSX workbook member exceeds the safety limit."),
					frappe.ValidationError,
				)
			if any(
				member.file_size / max(member.compress_size, 1) > MAX_XLSX_COMPRESSION_RATIO
				for member in members
			):
				frappe.throw(
					_("An XLSX workbook member exceeds the compression-ratio safety limit."),
					frappe.ValidationError,
				)
			uncompressed_size = sum(max(member.file_size, 0) for member in members)
			compressed_size = sum(max(member.compress_size, 0) for member in members)
	except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
		raise frappe.ValidationError(_("The uploaded file is not a valid XLSX workbook.")) from exc
	if uncompressed_size > MAX_XLSX_UNCOMPRESSED_BYTES:
		frappe.throw(
			_("The expanded XLSX workbook exceeds the {0} MB safety limit.").format(
				MAX_XLSX_UNCOMPRESSED_BYTES // (1024 * 1024)
			),
			frappe.ValidationError,
		)
	compression_ratio = uncompressed_size / max(compressed_size, 1)
	if compression_ratio > MAX_XLSX_COMPRESSION_RATIO:
		frappe.throw(
			_("The XLSX compression ratio exceeds the safety limit."),
			frappe.ValidationError,
		)
	return {
		"file_size": file_size,
		"uncompressed_size": uncompressed_size,
		"compression_ratio": int(compression_ratio),
	}


def _guess_schedule_mapping(rows: list[list[Any]], forced_header_row_no: int | None = None) -> dict[str, Any]:
	header_row_no = cint(forced_header_row_no or 0) or _guess_schedule_header_row_no(rows)
	column_options = _build_schedule_column_options(rows, header_row_no)
	header_idx = max(header_row_no - 1, 0)
	headers = rows[header_idx] if len(rows) > header_idx else []
	item_reference_column = _guess_column_by_labels(headers, ("mtlpartnum", "item code", "item", "material", "part no", "料号", "物料"))
	customer_part_no_column = item_reference_column
	description_column = _guess_column_by_labels(headers, ("description", "描述", "desc"))
	row_type_column = _guess_column_by_labels(headers, ("类别", "category", "type", "row type"))
	plan_qty_column = _guess_column_by_labels(headers, ("plan qty", "总数量", "交货日期/总数量", "plan quantity"))
	po_qty_column = _guess_column_by_labels(headers, ("po qty", "订单数量", "order qty"))
	date_columns = _resolve_matrix_date_columns(headers, {"date_columns_mode": "auto"})
	data_start_row_no = header_row_no + 1
	row_type_values = []
	if row_type_column:
		row_type_idx = _coerce_excel_column_index(row_type_column)
		for row in rows[data_start_row_no - 1 : min(len(rows), data_start_row_no + 20)]:
			value = str(_get_row_value(row, row_type_idx) or "").strip()
			if value and value not in row_type_values:
				row_type_values.append(value)
	demand_row_type_value = next((value for value in row_type_values if "交货" in value or "delivery" in value.lower()), row_type_values[0] if row_type_values else "")
	return {
		"parser_mode": "matrix" if len(date_columns) >= 2 else "rows",
		"header_row_no": header_row_no,
		"data_start_row_no": data_start_row_no,
		"item_reference_column": item_reference_column or "",
		"customer_part_no_column": customer_part_no_column or "",
		"description_column": description_column or "",
		"row_type_column": row_type_column or "",
		"demand_row_type_value": demand_row_type_value,
		"plan_qty_column": plan_qty_column or "",
		"po_qty_column": po_qty_column or "",
		"date_columns_mode": "auto",
		"date_start_column": date_columns[0]["column"] if date_columns else "",
		"date_end_column": date_columns[-1]["column"] if date_columns else "",
		"date_column_letters": [row["column"] for row in date_columns],
		"column_options": column_options,
		"row_type_values": row_type_values,
	}


def _guess_schedule_header_row_no(rows: list[list[Any]]) -> int:
	best_row_no = 1
	best_score = -1
	for idx, row in enumerate(rows[:10], start=1):
		headers = [_normalize_header(cell) for cell in row]
		date_like = len(_resolve_matrix_date_columns(row, {"date_columns_mode": "auto"}))
		score = sum(
			1
			for value in headers
			if value in {"mtlpartnum", "description", "po qty", "plan qty", "类别", "category", "type"}
		) * 10
		score += date_like * 3
		score += sum(1 for value in headers if value) * 0.2
		if score > best_score:
			best_score = score
			best_row_no = idx
	return best_row_no


def _build_schedule_column_options(rows: list[list[Any]], header_row_no: int) -> list[dict[str, Any]]:
	header_idx = max(header_row_no - 1, 0)
	headers = rows[header_idx] if len(rows) > header_idx else []
	options = []
	for idx, header in enumerate(headers):
		column_letter = get_column_letter(idx + 1)
		label = f"{column_letter} · {str(header or '').strip() or _('Blank Header')}"
		options.append({"value": column_letter, "label": label})
	return options


def _guess_column_by_labels(headers: list[Any], candidates: tuple[str, ...]) -> str | None:
	normalized_headers = [_normalize_header(cell) for cell in headers]
	for candidate in candidates:
		for idx, header in enumerate(normalized_headers):
			if header == candidate:
				return get_column_letter(idx + 1)
	for candidate in candidates:
		for idx, header in enumerate(normalized_headers):
			if candidate and candidate in header:
				return get_column_letter(idx + 1)
	return None


def _resolve_matrix_date_columns(headers: list[Any], mapping: dict[str, Any]) -> list[dict[str, Any]]:
	mode = mapping.get("date_columns_mode") or "auto"
	start_idx = _coerce_excel_column_index(mapping.get("date_start_column"))
	end_idx = _coerce_excel_column_index(mapping.get("date_end_column"))
	candidate_indices = []
	for idx, header in enumerate(headers or []):
		if mode == "range":
			if start_idx is None:
				continue
			if idx < start_idx:
				continue
			if end_idx is not None and idx > end_idx:
				continue
		parsed = _parse_schedule_header_date(header)
		if parsed:
			candidate_indices.append((idx, parsed))
	results = []
	previous_date = None
	for idx, parsed in candidate_indices:
		schedule_date = parsed["date"]
		if previous_date and not parsed["has_year"]:
			while schedule_date <= previous_date:
				schedule_date = schedule_date.replace(year=schedule_date.year + 1)
		results.append(
			{
				"index": idx,
				"column": get_column_letter(idx + 1),
				"header": headers[idx],
				"schedule_date": schedule_date,
			}
		)
		previous_date = schedule_date
	return results


def _parse_schedule_header_date(value: Any) -> dict[str, Any] | None:
	if value in (None, ""):
		return None
	if isinstance(value, datetime):
		return {"date": value.date(), "has_year": True}
	if isinstance(value, date_cls):
		return {"date": value, "has_year": True}
	text = str(value).strip()
	if not text:
		return None
	patterns = [
		("%d-%b-%Y", True),
		("%d-%b-%y", True),
		("%Y-%m-%d", True),
		("%Y/%m/%d", True),
		("%d/%m/%Y", True),
		("%m/%d/%Y", True),
		("%d.%m.%Y", True),
		("%d-%b", False),
		("%d/%m", False),
		("%m/%d", False),
	]
	current_year = getdate(today()).year
	for pattern, has_year in patterns:
		try:
			parsed = datetime.strptime(text, pattern)
		except Exception:
			continue
		if not has_year:
			parsed = parsed.replace(year=current_year)
		return {"date": parsed.date(), "has_year": has_year}
	try:
		parsed_date = getdate(text)
	except Exception:
		return None
	return {"date": parsed_date, "has_year": any(char.isdigit() for char in text if char in "-/.")}


def _coerce_excel_column_index(value: Any) -> int | None:
	if value in (None, ""):
		return None
	if isinstance(value, int):
		return max(value - 1, 0)
	text = str(value).strip()
	if not text:
		return None
	if "·" in text:
		text = text.split("·", 1)[0].strip()
	if " " in text and not text.isdigit():
		text = text.split(" ", 1)[0].strip()
	if text.isdigit():
		return max(int(text) - 1, 0)
	return column_index_from_string(text.upper()) - 1


def _get_row_value(row: list[Any], index: int | None) -> Any:
	if index is None or index < 0 or index >= len(row or []):
		return None
	return row[index]


def _normalize_header(value: Any) -> str:
	return str(value or "").strip().lower().replace("_", " ")


def _get_unique_item_matches(fieldname: str, values) -> dict[str, str]:
	matches = defaultdict(list)
	for chunk in _iter_query_chunks(sorted({value for value in values or [] if value})):
		for row in frappe.get_all(
			"Item",
			filters={fieldname: ("in", chunk)},
			fields=["name", fieldname],
		):
			value = row.get(fieldname)
			if value and row.get("name"):
				matches[str(value)].append(row.get("name"))
	return {
		value: names[0]
		for value, names in matches.items()
		if len(set(names)) == 1
	}


def _prime_item_resolution_cache(item_references):
	cache = _get_request_cache("injection_aps_item_resolution_cache")
	references = sorted(
		{
			str(reference).strip()
			for reference in item_references or []
			if reference is not None and str(reference).strip()
		}
	)
	pending = [reference for reference in references if reference not in cache]
	if not pending:
		return
	if not frappe.db.exists("DocType", "Item"):
		cache.update({reference: "" for reference in pending})
		return

	for name in _get_existing_names_in_chunks("Item", pending):
		cache[name] = name
	pending = [reference for reference in pending if reference not in cache]
	for fieldname in ("item_code", "item_name"):
		if not pending:
			break
		matches = _get_unique_item_matches(fieldname, pending)
		for reference in pending:
			if matches.get(reference):
				cache[reference] = matches[reference]
		pending = [reference for reference in pending if reference not in cache]

	for prefix in ITEM_NAME_PREFIX_FALLBACKS:
		if not pending:
			break
		candidate_by_reference = {reference: f"{prefix}{reference}" for reference in pending}
		matches = _get_unique_item_matches("item_name", candidate_by_reference.values())
		for reference, candidate in candidate_by_reference.items():
			if matches.get(candidate):
				cache[reference] = matches[candidate]
		pending = [reference for reference in pending if reference not in cache]
	cache.update({reference: "" for reference in pending})


def _resolve_item_name(item_reference: str | None) -> str | None:
	if not item_reference:
		return None

	reference = str(item_reference).strip()
	if not reference:
		return None

	cache = _get_request_cache("injection_aps_item_resolution_cache")
	if reference in cache:
		return cache[reference] or None
	if not frappe.db.exists("DocType", "Item"):
		cache[reference] = ""
		return None

	item_name = None
	if frappe.db.exists("Item", reference):
		item_name = reference
	else:
		item_name = _get_unique_item_name_by_field("item_code", reference)
		if not item_name:
			item_name = _get_unique_item_name_by_field("item_name", reference)
		if not item_name:
			for prefix in ITEM_NAME_PREFIX_FALLBACKS:
				item_name = _get_unique_item_name_by_field("item_name", f"{prefix}{reference}")
				if item_name:
					break

	cache[reference] = item_name or ""
	return item_name or None


def _require_item_name(item_reference: str | None) -> str:
	item_name = _resolve_item_name(item_reference)
	if item_name:
		return item_name
	raise APSItemReferenceError(_("Item reference {0} could not be resolved to an Item record.").format(item_reference or ""))


def _get_request_cache(cache_key: str) -> dict[str, Any]:
	cache = getattr(frappe.local, cache_key, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, cache_key, cache)
	return cache


def _get_unique_item_name_by_field(fieldname: str, value: str) -> str | None:
	if not value:
		return None
	names = frappe.get_all("Item", filters={fieldname: value}, pluck="name", limit=2)
	return names[0] if len(names) == 1 else None


def repair_item_references(
	company: str | None = None,
	include_standard: int = 1,
	include_aps: int = 1,
	commit: bool = False,
) -> dict[str, Any]:
	company = str(company or "").strip()
	if not company:
		frappe.throw(
			_("Company is required for an APS Item reference repair.", context="Injection APS"),
			frappe.ValidationError,
		)
	repaired_rows = []
	unresolved_rows = []
	target_summaries = []

	for target in _get_item_reference_repair_targets(
		company=company,
		include_standard=include_standard,
		include_aps=include_aps,
	):
		rows = frappe.db.sql(target["query"], target.get("params") or [], as_dict=True)
		repaired_count, unresolved_count = _repair_item_reference_rows(
			target=target,
			rows=rows,
			repaired_rows=repaired_rows,
			unresolved_rows=unresolved_rows,
		)
		target_summaries.append(
			{
				"label": target["label"],
				"doctype": target["doctype"],
				"scanned_rows": len(rows),
				"repaired_rows": repaired_count,
				"unresolved_rows": unresolved_count,
			}
		)

	if commit and (repaired_rows or unresolved_rows):
		frappe.db.commit()

	return {
		"repaired_count": sum(row["repaired_rows"] for row in target_summaries),
		"unresolved_count": sum(row["unresolved_rows"] for row in target_summaries),
		"repaired_rows": repaired_rows[:MAX_REBUILD_WARNINGS],
		"unresolved_rows": unresolved_rows[:MAX_REBUILD_WARNINGS],
		"targets": target_summaries,
	}


def _get_item_reference_repair_targets(
	company: str | None = None,
	include_standard: int = 1,
	include_aps: int = 1,
) -> list[dict[str, Any]]:
	targets = []
	company_filter_sql = ""
	company_params: list[Any] = []
	if company:
		company_filter_sql = " and {table_alias}.company = %s"
		company_params = [company]

	if cint(include_aps):
		targets.extend(
			[
				{
					"label": "Customer Delivery Schedule Item",
					"doctype": "Customer Delivery Schedule Item",
					"query": """
						select
							cdsi.name,
							cdsi.item_code,
							cdsi.parent as source_name,
							cds.company
						from `tabCustomer Delivery Schedule Item` cdsi
						inner join `tabCustomer Delivery Schedule` cds on cds.name = cdsi.parent
						left join `tabItem` item on item.name = cdsi.item_code
						where ifnull(cdsi.item_code, '') != ''
							and item.name is null
					"""
					+ company_filter_sql.format(table_alias="cds"),
					"params": list(company_params),
				},
				{
					"label": "APS Demand Pool",
					"doctype": "APS Demand Pool",
					"query": """
						select
							dp.name,
							dp.item_code,
							dp.name as source_name,
							dp.company
						from `tabAPS Demand Pool` dp
						left join `tabItem` item on item.name = dp.item_code
						where ifnull(dp.item_code, '') != ''
							and item.name is null
					"""
					+ company_filter_sql.format(table_alias="dp"),
					"params": list(company_params),
				},
				{
					"label": "APS Net Requirement",
					"doctype": "APS Net Requirement",
					"query": """
						select
							nr.name,
							nr.item_code,
							nr.name as source_name,
							nr.company
						from `tabAPS Net Requirement` nr
						left join `tabItem` item on item.name = nr.item_code
						where ifnull(nr.item_code, '') != ''
							and item.name is null
					"""
					+ company_filter_sql.format(table_alias="nr"),
					"params": list(company_params),
				},
				{
					"label": "APS Schedule Result",
					"doctype": "APS Schedule Result",
					"query": """
						select
							sr.name,
							sr.item_code,
							sr.name as source_name,
							sr.company
						from `tabAPS Schedule Result` sr
						left join `tabItem` item on item.name = sr.item_code
						where ifnull(sr.item_code, '') != ''
							and item.name is null
					"""
					+ company_filter_sql.format(table_alias="sr"),
					"params": list(company_params),
				},
				{
					"label": "APS Exception Log",
					"doctype": "APS Exception Log",
					"query": """
						select
							ex.name,
							ex.item_code,
							ex.name as source_name,
							run.company
						from `tabAPS Exception Log` ex
						inner join `tabAPS Planning Run` run on run.name = ex.planning_run
						left join `tabItem` item on item.name = ex.item_code
						where ifnull(ex.item_code, '') != ''
							and item.name is null
							and run.company = %s
					""",
					"params": [company],
				},
			]
		)

	if cint(include_standard):
		query = """
			select
				soi.name,
				soi.item_code,
				soi.parent as source_name,
				so.company
			from `tabSales Order Item` soi
			inner join `tabSales Order` so on so.name = soi.parent
			left join `tabItem` item on item.name = soi.item_code
			where ifnull(soi.item_code, '') != ''
				and item.name is null
		"""
		params = []
		if company:
			query += " and so.company = %s"
			params.append(company)
		targets.append(
			{
				"label": "Sales Order Item",
				"doctype": "Sales Order Item",
				"query": query,
				"params": params,
				"read_only": True,
			}
		)

	return targets


def _repair_item_reference_rows(
	target: dict[str, Any],
	rows: list[dict[str, Any]],
	repaired_rows: list[dict[str, Any]],
	unresolved_rows: list[dict[str, Any]],
) -> tuple[int, int]:
	repaired_count = 0
	unresolved_count = 0

	for row in rows:
		current_reference = row.get("item_code")
		resolved_item_code = _resolve_item_name(current_reference)
		if not resolved_item_code:
			unresolved_count += 1
			if len(unresolved_rows) < MAX_REBUILD_WARNINGS:
				unresolved_rows.append(
					{
						"doctype": target["doctype"],
						"docname": row.get("name"),
						"source_name": row.get("source_name"),
						"item_reference": current_reference,
					}
			)
			continue
		if resolved_item_code == current_reference:
			continue
		if target.get("read_only"):
			unresolved_count += 1
			if len(unresolved_rows) < MAX_REBUILD_WARNINGS:
				unresolved_rows.append(
					{
						"doctype": target["doctype"],
						"docname": row.get("name"),
						"source_name": row.get("source_name"),
						"item_reference": current_reference,
						"resolved_item_code": resolved_item_code,
						"message": _("Standard ERPNext rows are not changed automatically by APS."),
					}
				)
			continue
		frappe.db.set_value(
			target["doctype"],
			row.get("name"),
			"item_code",
			resolved_item_code,
			update_modified=False,
		)
		repaired_count += 1
		if len(repaired_rows) < MAX_REBUILD_WARNINGS:
			repaired_rows.append(
				{
					"doctype": target["doctype"],
					"docname": row.get("name"),
					"source_name": row.get("source_name"),
					"old_item_reference": current_reference,
					"new_item_code": resolved_item_code,
				}
			)

	return repaired_count, unresolved_count


def _append_rebuild_warning(
	warnings: list[dict[str, Any]],
	warning_keys: set[tuple[str, str, str, str]],
	*,
	item_reference: str | None,
	source_doctype: str,
	source_name: str | None = None,
	row_name: str | None = None,
):
	key = (
		source_doctype or "",
		source_name or "",
		row_name or "",
		str(item_reference or ""),
	)
	if key in warning_keys:
		return
	warning_keys.add(key)
	message = _("Skipped {0} {1} because item reference {2} could not be resolved to an Item record.").format(
		source_doctype,
		source_name or row_name or "",
		item_reference or _("(blank)", context="Injection APS"),
	)
	warnings.append(
		{
			"source_doctype": source_doctype,
			"source_name": source_name,
			"row_name": row_name,
			"item_reference": item_reference,
			"message": message,
		}
	)


def _append_item_group_warning(
	warnings: list[dict[str, Any]],
	warning_keys: set[tuple[str, str, str, str]],
	*,
	item_code: str | None,
	source_doctype: str,
	source_name: str | None = None,
	row_name: str | None = None,
	item_group: str | None = None,
):
	key = (
		source_doctype or "",
		source_name or "",
		row_name or "",
		f"item-group::{item_code or ''}",
	)
	if key in warning_keys:
		return
	warning_keys.add(key)
	warnings.append(
		{
			"source_doctype": source_doctype,
			"source_name": source_name,
			"row_name": row_name,
			"item_reference": item_code,
			"message": _(
				"Skipped {0} {1} because item {2} belongs to item group {3}. APS only schedules {4}."
			).format(
				source_doctype,
				source_name or row_name or "",
				item_code or _("(blank)", context="Injection APS"),
				item_group or _("(blank)", context="Injection APS"),
				", ".join(SCHEDULABLE_ITEM_GROUPS),
			),
		}
	)


def _get_item_group(item_code: str | None) -> str:
	item_name = _resolve_item_name(item_code)
	if not item_name or not frappe.db.exists("DocType", "Item"):
		return ""
	cache = _get_request_cache("injection_aps_item_group_cache")
	if item_name not in cache:
		cache[item_name] = frappe.db.get_value("Item", item_name, "item_group") or ""
	return cache[item_name]


def _is_schedulable_item(item_code: str | None) -> bool:
	return _get_item_group(item_code) in SCHEDULABLE_ITEM_GROUPS


def _schedule_row_key(row: dict[str, Any]) -> tuple:
	schedule_date = row.get("schedule_date")
	return (
		row.get("sales_order") or "",
		row.get("item_code") or "",
		str(getdate(schedule_date)) if schedule_date else "",
		row.get("customer_part_no") or "",
	)


def _schedule_identity_key(row: dict[str, Any]) -> tuple:
	return (
		row.get("sales_order") or "",
		row.get("item_code") or "",
		row.get("customer_part_no") or "",
	)


def _detect_change_type(previous: dict[str, Any], current: dict[str, Any]) -> str:
	if not previous and flt(current.get("qty")) > 0:
		return "Added"
	if previous and flt(current.get("qty")) <= 0:
		return "Cancelled"
	previous_date = previous.get("schedule_date")
	current_date = current.get("schedule_date")
	if previous and previous_date and current_date and getdate(current_date) < getdate(previous_date):
		return "Advanced"
	if previous and previous_date and current_date and getdate(current_date) > getdate(previous_date):
		return "Delayed"
	if flt(current.get("qty")) > flt(previous.get("qty")):
		return "Increased"
	if flt(current.get("qty")) < flt(previous.get("qty")):
		return "Reduced"
	return "Unchanged"


def _summarize_change_types(rows: list[dict[str, Any]]) -> dict[str, int]:
	summary = defaultdict(int)
	for row in rows:
		summary[row.get("change_type") or "Unknown"] += 1
	return dict(summary)


def _build_demand_row(
	company: str,
	customer: str | None,
	item_code: str,
	demand_source: str,
	demand_date,
	qty: float,
	source_doctype: str,
	source_name: str,
	sales_order: str | None = None,
	sales_order_item: str | None = None,
	source_detail_name: str | None = None,
	remark: str | None = None,
	customer_part_no: str | None = None,
	is_urgent: int = 0,
	production_strategy: str | None = None,
	demand_confidence: str | None = None,
	cancellation_risk_percent: float | None = None,
	prebuild_allowed: int | None = None,
	max_prebuild_days: int | None = None,
) -> frappe.model.document.Document:
	from injection_aps.services.capacity_balance import normalize_production_strategy

	settings = get_settings_dict()
	item_code = _require_item_name(item_code)
	if not _is_schedulable_item(item_code):
		raise frappe.ValidationError(
			_("Item {0} belongs to item group {1}. Injection APS only schedules {2}.").format(
				item_code,
				_get_item_group(item_code) or _("(blank)", context="Injection APS"),
				", ".join(SCHEDULABLE_ITEM_GROUPS),
			)
		)
	item_context = _get_item_context(item_code, settings)
	item_meta = frappe.get_meta("Item")
	item_prebuild_allowed = (
		cint(frappe.db.get_value("Item", item_code, "custom_aps_prebuild_allowed"))
		if item_meta.has_field("custom_aps_prebuild_allowed")
		else 1
	)
	item_max_prebuild_days = (
		cint(frappe.db.get_value("Item", item_code, "custom_aps_max_prebuild_days"))
		if item_meta.has_field("custom_aps_max_prebuild_days")
		else 0
	)
	item_cancellation_risk = (
		flt(frappe.db.get_value("Item", item_code, "custom_aps_cancellation_risk_percent"))
		if item_meta.has_field("custom_aps_cancellation_risk_percent")
		else 0
	)
	return frappe.get_doc(
		{
			"doctype": "APS Demand Pool",
			"company": company,
			"customer": customer,
			"sales_order": sales_order,
			"sales_order_item": sales_order_item,
			"item_code": item_code,
			"customer_part_no": customer_part_no,
			"demand_source": demand_source,
			"demand_date": demand_date,
			"qty": qty,
			"production_strategy": normalize_production_strategy(
				production_strategy,
				default=settings.get("default_production_strategy") or "Auto Balance",
			),
			"demand_confidence": demand_confidence or ("Forecast" if demand_source == "Forecast" else "Confirmed"),
			"cancellation_risk_percent": (
				flt(cancellation_risk_percent)
				if cancellation_risk_percent not in (None, "")
				else item_cancellation_risk
			),
			"prebuild_allowed": item_prebuild_allowed if prebuild_allowed in (None, "") else cint(prebuild_allowed),
			"max_prebuild_days": cint(
				max_prebuild_days or item_max_prebuild_days or settings.get("default_max_prebuild_days") or 7
			),
			"status": "Open",
			"priority_score": _score_demand(
				demand_source=demand_source,
				demand_date=demand_date,
				is_urgent=is_urgent,
			),
			"is_urgent": is_urgent,
			"food_grade": item_context["food_grade"],
			"color_code": item_context["color_code"],
			"material_code": item_context["material_code"],
			"is_first_article": 1 if item_context["is_first_article"] else 0,
			"source_doctype": source_doctype,
			"source_name": source_name,
			"source_detail_name": source_detail_name,
			"remark": remark,
			"is_system_generated": 1,
		}
	)


def _resolve_unique_sales_order_item(sales_order: str | None, item_code: str | None) -> str | None:
	"""Return an SO detail only when the header/item identifies exactly one row.

	Framework orders can contain the same finished item on more than one detail.
	Choosing the first row would create a false Work Order lineage, so ambiguity is
	persisted as a blank detail and is blocked again before formal WO release.
	"""
	if not sales_order or not item_code or not frappe.db.exists("DocType", "Sales Order Item"):
		return None
	rows = frappe.get_all(
		"Sales Order Item",
		filters={
			"parent": sales_order,
			"parenttype": "Sales Order",
			"item_code": item_code,
		},
		pluck="name",
		order_by="idx asc, name asc",
		limit=2,
	)
	return rows[0] if len(rows) == 1 else None


def _append_sales_order_backlog(
	company: str | None = None,
	warnings: list[dict[str, Any]] | None = None,
	warning_keys: set[tuple[str, str, str, str]] | None = None,
) -> dict[str, Any]:
	if not frappe.db.exists("DocType", "Sales Order Item"):
		return {"rows": [], "skipped_rows": 0}

	query = """
		select
			soi.name as sales_order_item_name,
			so.company,
			so.customer,
			soi.parent as sales_order,
			soi.item_code,
			soi.delivery_date,
			greatest(ifnull(soi.qty, 0) - ifnull(soi.delivered_qty, 0), 0) as open_qty
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1
			and ifnull(so.status, '') not in ('Closed', 'Completed', 'Cancelled')
			and greatest(ifnull(soi.qty, 0) - ifnull(soi.delivered_qty, 0), 0) > 0
	"""
	params = []
	if company:
		query += " and so.company = %s"
		params.append(company)

	rows = frappe.db.sql(query, params, as_dict=True)
	active_schedule_pairs = set()
	for schedule_row in frappe.db.sql(
		"""
		select cdsi.name, cdsi.parent, cdsi.sales_order, cdsi.item_code, cds.customer, cds.company
		from `tabCustomer Delivery Schedule Item` cdsi
		inner join `tabCustomer Delivery Schedule` cds on cds.name = cdsi.parent
		where cds.status = 'Active'
		""",
		as_dict=True,
	):
		resolved_item_code = _resolve_item_name(schedule_row.item_code)
		if not resolved_item_code:
			if warnings is not None and warning_keys is not None:
				_append_rebuild_warning(
					warnings,
					warning_keys,
					item_reference=schedule_row.item_code,
					source_doctype="Customer Delivery Schedule",
					source_name=schedule_row.parent,
					row_name=schedule_row.name,
				)
			continue
		if resolved_item_code != schedule_row.item_code:
			frappe.db.set_value(
				"Customer Delivery Schedule Item",
				schedule_row.name,
				"item_code",
				resolved_item_code,
				update_modified=False,
			)
		active_schedule_pairs.add(
			(
				schedule_row.company,
				schedule_row.customer,
				resolved_item_code,
				schedule_row.sales_order or "",
			)
		)
	created = []
	skipped_rows = 0
	for row in rows:
		resolved_item_code = _resolve_item_name(row.item_code)
		if not resolved_item_code:
			skipped_rows += 1
			if warnings is not None and warning_keys is not None:
				_append_rebuild_warning(
					warnings,
					warning_keys,
					item_reference=row.item_code,
					source_doctype="Sales Order",
					source_name=row.sales_order,
				)
			continue
		if not _is_schedulable_item(resolved_item_code):
			skipped_rows += 1
			if warnings is not None and warning_keys is not None:
				_append_item_group_warning(
					warnings,
					warning_keys,
					item_code=resolved_item_code,
					source_doctype="Sales Order",
					source_name=row.sales_order,
					item_group=_get_item_group(resolved_item_code),
				)
			continue
		if (row.company, row.customer, resolved_item_code, row.sales_order or "") in active_schedule_pairs:
			continue
		demand = _build_demand_row(
			company=row.company,
			customer=row.customer,
			item_code=resolved_item_code,
			demand_source="Sales Order Backlog",
			demand_date=row.delivery_date or today(),
			qty=row.open_qty,
			source_doctype="Sales Order",
			source_name=row.sales_order,
			sales_order=row.sales_order,
			sales_order_item=row.sales_order_item_name,
			source_detail_name=row.sales_order_item_name,
		)
		created.append(demand.insert(ignore_permissions=True).name)
	return {"rows": created, "skipped_rows": skipped_rows}


def _append_safety_stock_demands(company: str | None = None) -> list[str]:
	settings = get_settings_dict()
	fieldname = settings["item_safety_stock_field"]
	if not fieldname or not frappe.db.exists("DocType", "Item"):
		return []
	item_meta = frappe.get_meta("Item")
	if not item_meta.has_field(fieldname):
		return []

	created = []
	stock_map = _get_available_stock_map(company)
	item_rows = frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["name", fieldname],
	)
	for item in item_rows:
		safety_stock = flt(item.get(fieldname))
		if not safety_stock:
			continue
		if not _is_schedulable_item(item.name):
			continue
		shortage = max(safety_stock - flt(stock_map.get(item.name)), 0)
		if shortage <= 0:
			continue
		demand = _build_demand_row(
			company=company or frappe.defaults.get_user_default("Company"),
			customer=None,
			item_code=item.name,
			demand_source="Safety Stock",
			demand_date=today(),
			qty=shortage,
			source_doctype="Item",
			source_name=item.name,
		)
		created.append(demand.insert(ignore_permissions=True).name)
	return created


def _score_demand(demand_source: str, demand_date, is_urgent: int = 0) -> int:
	days_to_due = (getdate(demand_date) - getdate(today())).days
	urgency_bonus = 250 if cint(is_urgent) else 0
	date_bonus = max(60 - max(days_to_due, -30), 0)
	return cint(DEMAND_SOURCE_PRIORITY.get(demand_source, 100) + urgency_bonus + date_bonus)


def _get_available_stock_map(company: str | None, demand_rows: list[dict[str, Any]] | None = None) -> dict[str, float]:
	if not frappe.db.exists("DocType", "Bin"):
		return {}
	company = company or frappe.defaults.get_user_default("Company")
	if not company:
		return {}
	from injection_aps.services import availability

	warehouses = availability._get_finished_goods_warehouses(company)
	if not warehouses:
		return {}
	query = """
		select
			bin.item_code,
			sum(ifnull(bin.actual_qty, 0)) as actual_qty,
			sum(ifnull(bin.reserved_qty, 0)) as reserved_qty,
			sum(ifnull(bin.reserved_stock, 0)) as reserved_stock,
			sum(ifnull(bin.reserved_qty_for_production, 0)) as reserved_qty_for_production,
			sum(ifnull(bin.reserved_qty_for_sub_contract, 0)) as reserved_qty_for_sub_contract,
			sum(ifnull(bin.reserved_qty_for_production_plan, 0)) as reserved_qty_for_production_plan
		from `tabBin` bin
		inner join `tabWarehouse` wh on wh.name = bin.warehouse
		where wh.company = %(company)s
			and wh.is_group = 0
			and wh.disabled = 0
			and bin.warehouse in %(warehouses)s
		group by bin.item_code
	"""
	params = {"company": company, "warehouses": warehouses}
	reservation_credit_map = _get_aps_sales_order_reservation_credit_map(company, demand_rows)
	stock_map = {}
	for row in frappe.db.sql(query, params, as_dict=True):
		# ``reserved_stock`` is the explicit Stock Reservation Entry view and can
		# overlap SO ``reserved_qty``.  Use the larger sales/stock reservation once,
		# then add independent production, subcontract and production-plan claims.
		stock_reservation_qty = max(flt(row.reserved_qty), flt(row.reserved_stock))
		credited_reserved_qty = min(
			flt(reservation_credit_map.get(row.item_code)),
			stock_reservation_qty,
		)
		external_reserved_qty = max(stock_reservation_qty - credited_reserved_qty, 0)
		production_reserved_qty = (
			flt(row.reserved_qty_for_production)
			+ flt(row.reserved_qty_for_sub_contract)
			+ flt(row.reserved_qty_for_production_plan)
		)
		stock_map[row.item_code] = max(
			flt(row.actual_qty) - external_reserved_qty - production_reserved_qty,
			0,
		)
	return stock_map


def _get_customer_claimable_stock_map(
	company: str | None,
	demand_rows: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
	"""Return the single APS ATP stock pool after the Item safety floor.

	Callers must use this for customer promises and cross-run FG claims. Safety
	Stock planning itself intentionally uses the raw reservation-aware map to
	calculate the floor gap and must not call this helper.
	"""
	stock_map = _get_available_stock_map(company, demand_rows=demand_rows)
	if not stock_map:
		return {}
	settings = get_settings_dict()
	safety_field = settings.get("item_safety_stock_field")
	return {
		item_code: max(
			flt(qty) - max(flt(_get_item_mapping_value(item_code, safety_field)), 0),
			0,
		)
		for item_code, qty in stock_map.items()
	}


def _get_aps_sales_order_reservation_credit_map(
	company: str | None,
	demand_rows: list[dict[str, Any]] | None,
) -> dict[str, float]:
	if not demand_rows or not frappe.db.exists("DocType", "Sales Order Item"):
		return {}

	direct_demand_map = defaultdict(float)
	item_codes = set()
	for row in demand_rows:
		if (row.get("demand_source") or "") == "Safety Stock":
			continue
		item_code = _normalize_item_code(row.get("item_code"))
		if not item_code:
			continue
		demand_qty = max(flt(row.get("qty")), 0)
		if demand_qty <= 0:
			continue
		item_codes.add(item_code)
		sales_order = row.get("sales_order")
		if sales_order:
			direct_demand_map[(sales_order, item_code)] += demand_qty

	if not item_codes or not direct_demand_map:
		return {}

	sales_order_filters = set(sales_order for sales_order, _item_code in direct_demand_map)
	reservation_rows = _get_sales_order_reservation_rows(
		company=company,
		item_codes=sorted(item_codes),
		sales_orders=sorted(sales_order_filters),
		customers=None,
	)

	credit_map = defaultdict(float)
	remaining_direct_demand = defaultdict(float, direct_demand_map)
	for row in reservation_rows:
		item_code = _normalize_item_code(row.get("item_code"))
		reserved_qty = flt(row.get("reserved_qty"))
		if not item_code or reserved_qty <= 0:
			continue

		direct_key = (row.get("sales_order"), item_code)
		if remaining_direct_demand[direct_key] > 0:
			credit_qty = min(reserved_qty, remaining_direct_demand[direct_key])
			credit_map[item_code] += credit_qty
			remaining_direct_demand[direct_key] -= credit_qty
			reserved_qty -= credit_qty

	return dict(credit_map)


def _get_sales_order_reservation_rows(
	company: str | None,
	item_codes: list[str],
	sales_orders: list[str] | None = None,
	customers: list[str] | None = None,
) -> list[dict[str, Any]]:
	if not item_codes or (not sales_orders and not customers):
		return []

	dont_reserve_on_return = cint(
		frappe.get_cached_value(
			"Selling Settings",
			"Selling Settings",
			"dont_reserve_sales_order_qty_on_sales_return",
		)
	)
	conditions = [
		"so.docstatus = 1",
		"ifnull(so.status, '') not in ('On Hold', 'Closed')",
		"ifnull(soi.warehouse, '') != ''",
		"soi.item_code in ({0})".format(", ".join(["%s"] * len(item_codes))),
	]
	params: list[Any] = [dont_reserve_on_return, *item_codes]
	if company:
		conditions.append("so.company = %s")
		params.append(company)
	scope_conditions = []
	if sales_orders:
		scope_conditions.append("so.name in ({0})".format(", ".join(["%s"] * len(sales_orders))))
		params.extend(sales_orders)
	if customers:
		scope_conditions.append("so.customer in ({0})".format(", ".join(["%s"] * len(customers))))
		params.extend(customers)
	conditions.append("({0})".format(" or ".join(scope_conditions)))

	query = """
		select
			so.name as sales_order,
			so.company,
			so.customer,
			so.transaction_date,
			soi.item_code,
			soi.delivery_date,
			sum(
				case
					when ifnull(soi.qty, 0) > 0 then
						ifnull(soi.stock_qty, 0)
						* greatest(
							ifnull(soi.qty, 0)
							- ifnull(soi.delivered_qty, 0)
							- if(%s, ifnull(soi.returned_qty, 0), 0),
							0
						)
						/ ifnull(soi.qty, 0)
					else 0
				end
			) as reserved_qty
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where {conditions}
		group by so.name, so.company, so.customer, so.transaction_date, soi.item_code, soi.delivery_date
		having reserved_qty > 0
		order by soi.delivery_date asc, so.transaction_date asc, so.name asc
	""".format(conditions=" and ".join(conditions))
	return frappe.db.sql(query, params, as_dict=True)


def _get_net_requirement_work_order_identity(
	*,
	company: str | None,
	item_code: str,
	sales_order: str | None,
	sales_order_item: str | None,
	is_safety_stock: bool,
	allow_stock_pool: bool = False,
) -> tuple[str, str, str, str, str] | None:
	"""Return the only Work Order pool that may cover one demand group.

	Customer demand is exact to one submitted SO/SOI tuple. Stock production is
	exact to an APS-owned logical pool. An incomplete SO tuple is deliberately
	left unmatched instead of falling back to an item-only Work Order.
	"""
	if sales_order and sales_order_item:
		return ("Sales Order", company or "", sales_order, sales_order_item, item_code)
	if sales_order or sales_order_item:
		return None
	if not (is_safety_stock or allow_stock_pool):
		return None
	stock_pool = "Safety Stock" if is_safety_stock else "Stock Production"
	return ("Stock Pool", company or "", stock_pool, "", item_code)


def _get_open_work_order_map(
	company: str | None,
) -> dict[tuple[str, str, str, str, str], float]:
	if not frappe.db.exists("DocType", "Work Order"):
		return {}
	scheduling_guard = ""
	if frappe.db.exists("DocType", "Scheduling Item") and frappe.db.exists(
		"DocType", "Work Order Scheduling"
	):
		scheduling_guard = """
			and not exists (
				select 1
				from `tabScheduling Item` si
				inner join `tabWork Order Scheduling` wos on wos.name = si.parent
				where si.work_order = wo.name
					and ifnull(wos.status, '') in ('Draft', 'Schedule Confirmed', 'Material Transfer', 'Job Card', 'Manufacture')
			)
		"""
	query = """
		select
			wo.company,
			wo.sales_order,
			wo.sales_order_item,
			wo.production_item as item_code,
			wo.custom_aps_source,
			count(distinct wo.name) as work_order_count,
			sum(greatest(ifnull(wo.qty, 0) - ifnull(wo.produced_qty, 0), 0)) as open_qty
		from `tabWork Order` wo
		left join `tabAPS Schedule Result` aps_result
			on aps_result.name = wo.custom_aps_result_reference
		left join `tabAPS Planning Run` aps_run
			on aps_run.name = wo.custom_aps_run
		where wo.docstatus = 1
			and ifnull(wo.status, '') in ('Submitted', 'Not Started')
			and ifnull(wo.produced_qty, 0) = 0
			and ifnull(wo.material_transferred_for_manufacturing, 0) = 0
			and (
				(
					ifnull(wo.sales_order, '') != ''
					and ifnull(wo.sales_order_item, '') != ''
				)
				or (
					ifnull(wo.sales_order, '') = ''
					and ifnull(wo.sales_order_item, '') = ''
					and wo.custom_aps_source in ('Stock Production', 'Safety Stock')
					and aps_result.name is not null
					and aps_run.name is not null
					and aps_result.planning_run = wo.custom_aps_run
					and aps_result.company = wo.company
					and aps_run.company = wo.company
					and aps_result.item_code = wo.production_item
					and ifnull(aps_result.sales_order, '') = ''
					and ifnull(aps_result.sales_order_item, '') = ''
					and (
						(
							wo.custom_aps_source = 'Safety Stock'
							and aps_result.demand_source = 'Safety Stock'
						)
						or (
							wo.custom_aps_source = 'Stock Production'
							and ifnull(aps_result.demand_source, '') != 'Safety Stock'
						)
					)
				)
			)
			{scheduling_guard}
	""".format(scheduling_guard=scheduling_guard)
	params = []
	if company:
		query += " and wo.company = %s"
		params.append(company)
	query += " group by wo.company, wo.sales_order, wo.sales_order_item, wo.production_item, wo.custom_aps_source"
	result = defaultdict(float)
	for row in frappe.db.sql(query, params, as_dict=True):
		# One Result/WO controlled-reuse boundary cannot silently collapse several
		# execution containers.  Leave an ambiguous pool out of automatic deduction;
		# the proposal generator independently blocks multiple exact candidates.
		if cint(row.get("work_order_count") or 1) != 1:
			continue
		if row.sales_order and row.sales_order_item:
			identity = (
				"Sales Order",
				row.company or "",
				row.sales_order,
				row.sales_order_item,
				row.item_code,
			)
		elif not row.sales_order and not row.sales_order_item and row.custom_aps_source in {
			"Stock Production",
			"Safety Stock",
		}:
			identity = (
				"Stock Pool",
				row.company or "",
				row.custom_aps_source,
				"",
				row.item_code,
			)
		else:
			continue
		result[identity] += flt(row.open_qty)
	return dict(result)


def _build_net_requirement_reason(
	demand_qty: float,
	available_stock_qty: float,
	open_work_order_qty: float,
	existing_work_order_policy: str,
	safety_gap: float,
	overstock_qty: float,
	minimum_batch_qty: float,
	planning_qty: float,
) -> str:
	if existing_work_order_policy == "Include":
		return _(
			"Demand {0} - APS-usable stock {1} - included existing open work orders {2} + one-time safety gap {3}; remaining overstock {4}; minimum batch {5}; planning qty {6}."
		).format(
			demand_qty,
			available_stock_qty,
			open_work_order_qty,
			safety_gap,
			overstock_qty,
			minimum_batch_qty,
			planning_qty,
		)
	return _(
		"Demand {0} - APS-usable stock {1}; existing open work orders were explicitly excluded + one-time safety gap {2}; remaining overstock {3}; minimum batch {4}; planning qty {5}."
	).format(
		demand_qty,
		available_stock_qty,
		safety_gap,
		overstock_qty,
		minimum_batch_qty,
		planning_qty,
	)


def _get_item_mapping_value(item_code: str, fieldname: str | None):
	if not fieldname or not frappe.db.exists("DocType", "Item") or not frappe.get_meta("Item").has_field(fieldname):
		return None
	item_code = _resolve_item_name(item_code)
	if not item_code:
		return None
	return frappe.db.get_value("Item", item_code, fieldname)


def _get_item_context(item_code: str, settings: dict[str, Any]) -> dict[str, Any]:
	item_code = _require_item_name(item_code)
	meta = frappe.get_meta("Item")
	item_doc = frappe.get_cached_doc("Item", item_code)
	food_grade = item_doc.get(settings["item_food_grade_field"]) if meta.has_field(settings["item_food_grade_field"]) else ""
	color_code = item_doc.get(settings["item_color_field"]) if meta.has_field(settings["item_color_field"]) else ""
	material_code = item_doc.get(settings["item_material_field"]) if meta.has_field(settings["item_material_field"]) else ""
	first_article = item_doc.get(settings["item_first_article_field"]) if meta.has_field(settings["item_first_article_field"]) else 0

	if (not color_code or not material_code) and frappe.db.exists("DocType", "Mold"):
		mold_row = _get_primary_mold_row(item_code)
		if mold_row and (not color_code or not material_code):
			material_row = frappe.db.sql(
				"""
				select material_item, color_spec
				from `tabMold Default Material`
				where parent = %s and parenttype = 'Mold'
				order by idx asc
				limit 1
				""",
				(mold_row.get("mold"),),
				as_dict=True,
			)
			if material_row:
				color_code = color_code or material_row[0].get("color_spec")
				material_code = material_code or material_row[0].get("material_item")

	return {
		"item_name": item_doc.item_name or "",
		"item_group": item_doc.item_group or "",
		"food_grade": food_grade or "",
		"color_code": color_code or "",
		"material_code": material_code or "",
		"is_first_article": cint(first_article),
		"is_urgent": 0,
	}


def _get_available_mold_rows(item_code: str) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "Mold"):
		return []
	item_code = _resolve_item_name(item_code)
	if not item_code:
		return []
	query = """
		select
			m.name as mold,
			m.mold_name,
			m.machine_tonnage,
			m.cavity_count,
			m.standard_cycle_seconds,
			m.status as mold_status,
			m.is_family_mold,
			mp.priority,
			mp.is_default_product,
			mp.item_code,
			mp.output_group,
			mp.configuration_label,
			mp.color_spec,
			mp.output_qty,
			mp.cavity_output_qty
		from `tabMold` m
		inner join `tabMold Product` mp on mp.parent = m.name and mp.parenttype = 'Mold'
		where m.docstatus = 1
			and mp.item_code = %s
			and ifnull(m.status, '') not in ({0})
		order by mp.is_default_product desc, mp.priority asc, m.modified desc
	""".format(", ".join(["%s"] * len(BLOCKING_MOLD_STATUSES)))
	params = [item_code, *BLOCKING_MOLD_STATUSES]
	rows = []
	seen_molds = set()
	for row in frappe.db.sql(query, params, as_dict=True):
		if row.get("mold") in seen_molds:
			continue
		seen_molds.add(row.get("mold"))
		row["cycle_time_seconds"] = flt(row.get("standard_cycle_seconds"))
		row["effective_output_qty"] = _get_effective_mold_output_qty(row)
		rows.append(row)
	return rows


def _get_effective_mold_output_qty(mold_row: dict[str, Any]) -> float:
	if flt(mold_row.get("cavity_output_qty")) > 0:
		return flt(mold_row.get("cavity_output_qty"))
	if flt(mold_row.get("output_qty")) > 0:
		return flt(mold_row.get("output_qty"))
	if 0 < flt(mold_row.get("cavity_count")) <= 128:
		return flt(mold_row.get("cavity_count"))
	return 1


def _get_primary_mold_row(item_code: str) -> dict[str, Any] | None:
	rows = _get_available_mold_rows(item_code)
	return rows[0] if rows else None


def _get_preferred_mold_row(item_code: str) -> dict[str, Any] | None:
	return _get_primary_mold_row(item_code)


def _get_family_output_rows(
	mold_name: str,
	primary_item_code: str,
	output_group: str | None = None,
) -> list[dict[str, Any]]:
	if not mold_name or not frappe.db.exists("DocType", "Mold Product"):
		return []
	primary_item_code = _resolve_item_name(primary_item_code)
	primary_output_group = output_group
	if not primary_output_group:
		primary_output_group = frappe.db.get_value(
			"Mold Product",
			{"parent": mold_name, "parenttype": "Mold", "item_code": primary_item_code},
			"output_group",
		)
	primary_output_group = primary_output_group or "Default"
	query = """
		select
			mp.item_code,
			mp.output_group,
			mp.configuration_label,
			mp.color_spec,
			mp.output_qty,
			mp.cavity_output_qty
		from `tabMold Product` mp
		inner join `tabItem` item on item.name = mp.item_code
		where mp.parent = %s
			and mp.parenttype = 'Mold'
			and mp.item_code != %s
			and ifnull(mp.output_group, 'Default') = %s
			and item.item_group in ({0})
		order by mp.priority asc, mp.idx asc
	""".format(", ".join(["%s"] * len(SCHEDULABLE_ITEM_GROUPS)))
	params = [mold_name, primary_item_code, primary_output_group, *SCHEDULABLE_ITEM_GROUPS]
	return frappe.db.sql(query, params, as_dict=True)


def _get_primary_demand_source(
	item_code: str,
	customer: str | None,
	demand_date,
	sales_order: str | None = None,
	production_strategy: str | None = None,
) -> str:
	item_code = _resolve_item_name(item_code) or item_code
	filters = {
		"item_code": item_code,
		"customer": customer,
		"demand_date": demand_date,
		"sales_order": sales_order or ("is", "not set"),
		"status": ("!=", "Cancelled"),
	}
	if production_strategy:
		filters["production_strategy"] = production_strategy
	row = frappe.get_all(
		"APS Demand Pool",
		filters=filters,
		fields=["demand_source", "priority_score"],
		order_by="priority_score desc, modified asc",
		limit=1,
	)
	return row[0].get("demand_source") if row else ""


def _build_form_route(doctype: str | None, docname: str | None) -> str:
	if not doctype or not docname:
		return ""
	return f"Form/{doctype}/{docname}"


def _get_item_detail_snapshot(item_code: str, customer: str | None, settings: dict[str, Any]) -> dict[str, Any]:
	item_code = _require_item_name(item_code)
	item_doc = frappe.get_cached_doc("Item", item_code)
	meta = frappe.get_meta("Item")
	customer_reference = ""
	customer_reference_field = ""
	for fieldname in ("customer_code", "default_manufacturer_part_no", "custom_part_information"):
		if meta.has_field(fieldname) and item_doc.get(fieldname):
			customer_reference = item_doc.get(fieldname)
			customer_reference_field = fieldname
			break
	drawing_file = ""
	for fieldname in ("drawing_file", "sec_drawing_file"):
		if meta.has_field(fieldname) and item_doc.get(fieldname):
			drawing_file = item_doc.get(fieldname)
			break
	context = _get_item_context(item_code, settings)
	return {
		"item_code": item_code,
		"item_name": item_doc.item_name or "",
		"item_group": item_doc.item_group or "",
		"stock_uom": item_doc.stock_uom or "",
		"customer": customer or item_doc.get("customer") or "",
		"customer_reference": customer_reference or "",
		"customer_reference_field": customer_reference_field or "",
		"drawing_file": drawing_file or "",
		"food_grade": context.get("food_grade") or "",
		"color_code": context.get("color_code") or "",
		"material_code": context.get("material_code") or "",
		"is_first_article": cint(context.get("is_first_article")),
		"item_route": _build_form_route("Item", item_code),
	}


def _get_result_source_rows(result) -> list[dict[str, Any]]:
	if not result.net_requirement or not frappe.db.exists("DocType", "APS Demand Pool"):
		return []
	rows = frappe.get_all(
		"APS Demand Pool",
		filters={
			"company": result.company,
			"customer": result.customer,
			"sales_order": result.get("sales_order") or ("is", "not set"),
			"item_code": result.item_code,
			"demand_date": result.requested_date,
			"status": ("!=", "Cancelled"),
		},
		fields=[
			"name",
			"demand_source",
			"demand_date",
			"qty",
			"customer_part_no",
			"sales_order",
			"source_doctype",
			"source_name",
			"remark",
		],
		order_by="priority_score desc, modified asc",
	)
	for row in rows:
		row["source_route"] = _build_form_route(row.get("source_doctype"), row.get("source_name"))
		row["sales_order_route"] = _build_form_route("Sales Order", row.get("sales_order"))
		row["demand_pool_route"] = _build_form_route("APS Demand Pool", row.get("name"))
	return rows


def _get_result_exception_rows(result) -> list[dict[str, Any]]:
	rows = frappe.get_all(
		"APS Exception Log",
		filters={"planning_run": result.planning_run, "status": "Open"},
		fields=[
			"name",
			"severity",
			"exception_type",
			"message",
			"is_blocking",
			"source_doctype",
			"source_name",
			"resolution_hint",
			"workstation",
			"diagnostic_json",
		],
		order_by="modified desc",
	)
	relevant = []
	segment_names = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": result.name, "parenttype": "APS Schedule Result"},
		pluck="name",
	)
	relevant_names = {result.name, result.net_requirement, *segment_names}
	for row in rows:
		if row.get("source_name") in relevant_names:
			diagnostic = _parse_diagnostic_json(row.get("diagnostic_json"))
			row["diagnostic"] = diagnostic
			row["root_cause_text"] = diagnostic.get("root_cause_text") or ""
			row["suggested_actions"] = diagnostic.get("suggested_actions") or []
			row["source_route"] = _build_form_route(row.get("source_doctype"), row.get("source_name"))
			relevant.append(row)
	return relevant


def _build_exception_resolution_context(doc) -> dict[str, Any]:
	diagnostic = _parse_diagnostic_json(doc.get("diagnostic_json"))
	root_cause_text = diagnostic.get("root_cause_text") or doc.get("resolution_hint") or doc.get("message") or ""
	suggested_actions = diagnostic.get("suggested_actions") or ([doc.get("resolution_hint")] if doc.get("resolution_hint") else [])
	related_routes = {
		"source": _build_form_route(doc.get("source_doctype"), doc.get("source_name")),
		"item": _build_form_route("Item", doc.get("item_code")),
		"workstation": _build_form_route("Workstation", doc.get("workstation")),
		"gantt": f"aps-schedule-gantt?run_name={doc.get('planning_run')}" if doc.get("planning_run") else "",
		"execution": f"aps-release-center?run_name={doc.get('planning_run')}" if doc.get("planning_run") else "",
	}
	return {
		"name": doc.name,
		"planning_run": doc.get("planning_run"),
		"severity": doc.get("severity"),
		"exception_type": doc.get("exception_type"),
		"item_code": doc.get("item_code"),
		"customer": doc.get("customer"),
		"workstation": doc.get("workstation"),
		"message": doc.get("message"),
		"resolution_hint": doc.get("resolution_hint"),
		"is_blocking": cint(doc.get("is_blocking")),
		"source_doctype": doc.get("source_doctype"),
		"source_name": doc.get("source_name"),
		"diagnostic": diagnostic,
		"root_cause_codes": diagnostic.get("root_cause_codes") or [],
		"root_cause_text": root_cause_text,
		"suggested_actions": [row for row in suggested_actions if row],
		"related_routes": related_routes,
		"gantt_focus": {
			"run_name": doc.get("planning_run"),
			"item_code": doc.get("item_code"),
			"workstation": doc.get("workstation"),
			"exception_name": doc.name,
		},
	}


def _get_result_mold_rows(result, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
	mold_names = list(dict.fromkeys(row.get("mould_reference") for row in segments if row.get("mould_reference")))
	if not mold_names or not frappe.db.exists("DocType", "Mold"):
		return []
	rows = frappe.db.sql(
		"""
		select
			m.name as mold,
			m.mold_name,
			m.status as mold_status,
			m.machine_tonnage,
			m.cavity_count,
			m.standard_cycle_seconds,
			m.is_family_mold,
			mp.item_code,
			mp.output_group,
			mp.configuration_label,
			mp.color_spec,
			mp.output_qty,
			mp.cavity_output_qty,
			mp.priority
		from `tabMold` m
		left join `tabMold Product` mp
			on mp.parent = m.name
			and mp.parenttype = 'Mold'
			and mp.item_code = %s
		where m.name in ({0})
		order by m.name asc
		""".format(", ".join(["%s"] * len(mold_names))),
		[result.item_code, *mold_names],
		as_dict=True,
	)
	for row in rows:
		row["cycle_time_seconds"] = flt(row.get("standard_cycle_seconds"))
		row["cavity_count"] = flt(row.get("cavity_output_qty") or row.get("output_qty") or row.get("cavity_count"))
		row["effective_output_qty"] = _get_effective_mold_output_qty(row)
		row["mold_route"] = _build_form_route("Mold", row.get("mold"))
	return rows


def _get_mold_master_rows_for_item(item_code: str) -> list[dict[str, Any]]:
	if not item_code or not frappe.db.exists("DocType", "Mold"):
		return []
	rows = frappe.db.sql(
		"""
		select
			m.name as mold,
			m.mold_name,
			m.status as mold_status,
			m.machine_tonnage,
			m.standard_cycle_seconds,
			m.is_family_mold,
			mp.item_code,
			mp.output_group,
			mp.configuration_label,
			mp.color_spec,
			mp.output_qty,
			mp.cavity_output_qty,
			mp.priority
		from `tabMold` m
		inner join `tabMold Product` mp
			on mp.parent = m.name
			and mp.parenttype = 'Mold'
			and mp.item_code = %s
		order by m.name asc, ifnull(mp.priority, 999) asc
		""",
		[item_code],
		as_dict=True,
	)
	for row in rows:
		row["cycle_time_seconds"] = flt(row.get("standard_cycle_seconds"))
		row["effective_output_qty"] = _get_effective_mold_output_qty(row)
		row["mold_route"] = _build_form_route("Mold", row.get("mold"))
	return rows


def _format_root_cause_text(lines: list[str]) -> str:
	return "\n".join(str(line).strip() for line in lines if str(line or "").strip())


def _build_mold_unavailable_diagnostic(item_code: str, selected_plant_floors: list[str] | None = None) -> dict[str, Any]:
	mold_rows = _get_mold_master_rows_for_item(item_code)
	root_cause_codes = []
	root_cause_lines = []
	suggested_actions = []
	if not mold_rows:
		root_cause_codes.append("MOLD_PRODUCT_MAPPING_MISSING")
		root_cause_lines.append("No Mold Product mapping was found for this item, so APS cannot determine an available mold.")
		suggested_actions.extend(
			[
				"Complete the Mold Product mapping for this item in Mold master data.",
				"Make sure the corresponding mold master is submitted and available.",
			]
		)
	else:
		root_cause_codes.append("NO_ACTIVE_MOLD")
		for row in mold_rows:
			root_cause_lines.append(
				"{0} is currently in status {1}.".format(row.get("mold"), row.get("mold_status") or "Status Not Maintained")
			)
		suggested_actions.extend(
			[
				"Check whether the mold is under maintenance, pending asset link, or scrapped.",
				"Make sure the Mold Product mapping, cycle, and output data are complete.",
			]
		)
	return {
		"selected_plant_floors": selected_plant_floors or [],
		"candidate_molds": [row.get("mold") for row in mold_rows if row.get("mold")],
		"candidate_workstations": [],
		"root_cause_codes": root_cause_codes,
		"root_cause_text": _format_root_cause_text(root_cause_lines),
		"suggested_actions": suggested_actions,
		"mold_rows": mold_rows,
	}


def _build_machine_unavailable_diagnostic(
	item_code: str,
	selected_plant_floors: list[str] | None = None,
	candidate_molds: list[str] | None = None,
) -> dict[str, Any]:
	root_cause_lines = [
		"No workstation in the selected plant floors satisfies tonnage, FDA, status, and mold-mapping constraints.",
	]
	suggested_actions = [
		"Check whether APS Machine Capability has been synchronized and enabled.",
		"Check workstation status, risk category, tonnage, and APS Mould-Machine Rule constraints.",
	]
	return {
		"selected_plant_floors": selected_plant_floors or [],
		"candidate_molds": candidate_molds or [],
		"candidate_workstations": [],
		"root_cause_codes": ["NO_ELIGIBLE_MACHINE_LANE"],
		"root_cause_text": _format_root_cause_text(root_cause_lines),
		"suggested_actions": suggested_actions,
	}


def _build_late_delivery_diagnostic(
	item_code: str,
	qty: float,
	scheduled_qty: float,
	unscheduled_qty: float,
	selected_plant_floors: list[str] | None,
	candidates: list[dict[str, Any]],
	selected_options: list[dict[str, Any]],
	blocking_exceptions: list[dict[str, Any]],
	horizon_end,
) -> dict[str, Any]:
	root_cause_codes = ["HORIZON_LIMIT"]
	root_cause_lines = [
		"The current planning horizon ends at {0}, and {1} is still unscheduled.".format(
			frappe.format(get_datetime(horizon_end), {"fieldtype": "Datetime"}),
			frappe.format(unscheduled_qty, {"fieldtype": "Float"}),
		)
	]
	candidate_molds = list(dict.fromkeys(row.get("mould_reference") for row in candidates if row.get("mould_reference")))
	candidate_workstations = list(dict.fromkeys(row.get("workstation") for row in candidates if row.get("workstation")))
	total_available_qty = sum(max(flt(row.get("available_qty")), 0) for row in selected_options or [])
	if total_available_qty < flt(qty):
		root_cause_codes.append("HORIZON_CAPACITY_INSUFFICIENT")
		root_cause_lines.append(
			"Available capacity inside the current horizon is about {0}, which is lower than the demand {1}.".format(
				frappe.format(total_available_qty, {"fieldtype": "Float"}),
				frappe.format(qty, {"fieldtype": "Float"}),
			)
		)
	if len(candidate_molds) <= 1:
		root_cause_codes.append("COPY_MOLD_LIMITED")
		root_cause_lines.append("The number of molds that can participate in scheduling is limited, so copy molds cannot further increase parallelization.")
	blocking_types = [row.get("exception_type") for row in blocking_exceptions if row.get("exception_type")]
	if blocking_types:
		root_cause_codes.append("CONSTRAINT_BLOCKED")
		root_cause_lines.append("Additional constraints blocked some candidate resources: {0}.".format(" / ".join(sorted(set(blocking_types))[:4])))
	if "FDA Conflict" in blocking_types:
		root_cause_codes.append("FDA_CONFLICT")
	if "Color Transition Blocked" in blocking_types:
		root_cause_codes.append("COLOR_BLOCKED")
	suggested_actions = [
		"Extend the APS horizon or split the demand into the next planning window.",
		"Check whether more copy molds, eligible workstations, or acceptable manual changeover plans are available.",
	]
	return {
		"requested_qty": flt(qty),
		"scheduled_qty": flt(scheduled_qty),
		"unscheduled_qty": flt(unscheduled_qty),
		"selected_plant_floors": selected_plant_floors or [],
		"candidate_molds": candidate_molds,
		"candidate_workstations": candidate_workstations,
		"root_cause_codes": list(dict.fromkeys(root_cause_codes)),
		"root_cause_text": _format_root_cause_text(root_cause_lines),
		"suggested_actions": suggested_actions,
	}


def _get_machine_capability_rows(plant_floors: list[str] | str | None) -> list[dict[str, Any]]:
	selected_plant_floors = _coerce_plant_floor_list(plant_floors=plant_floors)
	if not selected_plant_floors:
		return []
	rows = frappe.get_all(
		"APS Machine Capability",
		filters={"plant_floor": ("in", selected_plant_floors), "is_active": 1},
		fields=[
			"name",
			"workstation",
			"plant_floor",
			"machine_tonnage",
			"risk_category",
			"hourly_capacity_qty",
			"daily_capacity_qty",
			"queue_sequence",
			"machine_status",
			"max_run_hours",
		],
		order_by="queue_sequence asc, workstation asc",
	)
	return rows


def _build_workstation_state_map(capability_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
	state = {}
	baseline_now = get_datetime(now_datetime())
	for row in capability_rows:
		state[row["workstation"]] = {
			"next_available": baseline_now,
			"last_color_code": "",
			"last_material_code": "",
			"last_mould_reference": "",
			"last_end_time": None,
			"anchor_item_code": "",
			"anchor_strength": 0,
			"anchor_source": "",
			"anchor_campaign_key": "",
			"capability": row,
		}
	return state


def _build_mold_state_map(locked_segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
	state = {}
	baseline_now = get_datetime(now_datetime())
	for row in locked_segments:
		mold_name = row.get("mould_reference")
		if not mold_name:
			continue
		current = state.setdefault(
			mold_name,
			{
				"next_available": baseline_now,
				"last_workstation": "",
				"last_end_time": None,
				"anchor_item_code": "",
				"anchor_strength": 0,
				"anchor_source": "",
				"anchor_campaign_key": "",
			},
		)
		end_time = get_datetime(row.get("end_time"))
		if end_time > current["next_available"]:
			current["next_available"] = end_time
			current["last_workstation"] = row.get("workstation") or ""
			current["last_end_time"] = end_time
			current["anchor_item_code"] = _normalize_item_code(row.get("primary_item_code"))
			current["anchor_strength"] = cint(row.get("anchor_strength") or ANCHOR_STRENGTH_LOCKED)
			current["anchor_source"] = row.get("execution_anchor_source") or "APS Locked Segment"
			current["anchor_campaign_key"] = row.get("campaign_key") or _build_campaign_key(
				row.get("primary_item_code"),
				row.get("mould_reference"),
				row.get("workstation"),
			)
	return state


def _get_execution_anchor_strength(status: str | None, row: dict[str, Any]) -> int:
	status = (status or "").strip()
	if status in FROZEN_SCHEDULING_STATUSES or row.get("from_time") or flt(row.get("completed_qty")) > 0:
		return ANCHOR_STRENGTH_HARD
	if status in ("Schedule Confirmed", ""):
		return ANCHOR_STRENGTH_RELEASED
	return ANCHOR_STRENGTH_SOFT


def _get_execution_anchor_rows(plant_floors: list[str] | str | None) -> list[dict[str, Any]]:
	selected_plant_floors = _coerce_plant_floor_list(plant_floors=plant_floors)
	if not selected_plant_floors or not frappe.db.exists("DocType", "Work Order Scheduling"):
		return []
	rows = frappe.db.sql(
		"""
		select
			si.name as scheduling_item,
			si.parent as work_order_scheduling,
			si.work_order,
			si.workstation,
			si.planned_start_date as start_time,
			si.planned_end_date as end_time,
			si.from_time,
			si.to_time,
			si.completed_qty,
			si.custom_aps_segment_reference,
			si.custom_aps_result_reference,
			si.custom_aps_run,
			wos.status as scheduling_status,
			wos.plant_floor,
			wo.production_item as item_code,
			seg.mould_reference,
			seg.color_code,
			seg.material_code,
			seg.campaign_key,
			seg.primary_item_code,
			seg.execution_anchor_source
		from `tabScheduling Item` si
		inner join `tabWork Order Scheduling` wos on wos.name = si.parent
		left join `tabWork Order` wo on wo.name = si.work_order
		left join `tabAPS Schedule Segment` seg on seg.name = si.custom_aps_segment_reference
		where wos.plant_floor in ({0})
			and ifnull(si.workstation, '') != ''
			and ifnull(wos.status, '') in ({1})
		order by
			case when ifnull(wos.status, '') in ('Material Transfer', 'Job Card', 'Manufacture') then 0 else 1 end,
			ifnull(si.from_time, si.planned_start_date) asc,
			ifnull(si.to_time, si.planned_end_date) asc
		""".format(", ".join(["%s"] * len(selected_plant_floors)), ", ".join(["%s"] * len(ACTIVE_SCHEDULING_STATUSES))),
		[*selected_plant_floors, *ACTIVE_SCHEDULING_STATUSES],
		as_dict=True,
	)
	anchor_rows = []
	for row in rows:
		start_time = row.get("from_time") or row.get("start_time")
		end_time = row.get("to_time") or row.get("end_time") or start_time
		if not start_time or not end_time:
			continue
		item_code = _normalize_item_code(row.get("primary_item_code") or row.get("item_code"))
		campaign_key = row.get("campaign_key") or _build_campaign_key(
			item_code,
			row.get("mould_reference"),
			row.get("workstation"),
		)
		anchor_rows.append(
			{
				"name": row.get("scheduling_item"),
				"work_order_scheduling": row.get("work_order_scheduling"),
				"work_order": row.get("work_order"),
				"workstation": row.get("workstation"),
				"plant_floor": row.get("plant_floor"),
				"start_time": start_time,
				"end_time": end_time,
				"planned_qty": flt(row.get("completed_qty") or 0),
				"color_code": row.get("color_code") or "",
				"material_code": row.get("material_code") or "",
				"mould_reference": row.get("mould_reference") or "",
				"primary_item_code": item_code,
				"campaign_key": campaign_key,
				"anchor_strength": _get_execution_anchor_strength(row.get("scheduling_status"), row),
				"execution_anchor_source": row.get("execution_anchor_source")
				or (row.get("scheduling_status") or "Scheduling Item"),
			}
		)
	return anchor_rows


def _apply_anchor_rows_to_state(
	workstation_state: dict[str, dict[str, Any]],
	mold_state: dict[str, dict[str, Any]],
	anchor_rows: list[dict[str, Any]],
):
	for row in anchor_rows:
		workstation = row.get("workstation")
		end_time = get_datetime(row.get("end_time"))
		if workstation in workstation_state:
			state = workstation_state[workstation]
			if end_time >= state["next_available"]:
				state["next_available"] = end_time
				state["last_color_code"] = row.get("color_code") or ""
				state["last_material_code"] = row.get("material_code") or ""
				state["last_mould_reference"] = row.get("mould_reference") or ""
				state["last_end_time"] = end_time
				state["anchor_item_code"] = _normalize_item_code(row.get("primary_item_code"))
				state["anchor_strength"] = cint(row.get("anchor_strength") or 0)
				state["anchor_source"] = row.get("execution_anchor_source") or ""
				state["anchor_campaign_key"] = row.get("campaign_key") or ""
		mold_name = row.get("mould_reference")
		if not mold_name:
			continue
		current = mold_state.setdefault(
			mold_name,
			{
				"next_available": get_datetime(now_datetime()),
				"last_workstation": "",
				"last_end_time": None,
				"anchor_item_code": "",
				"anchor_strength": 0,
				"anchor_source": "",
				"anchor_campaign_key": "",
			},
		)
		if end_time >= current["next_available"]:
			current["next_available"] = end_time
			current["last_workstation"] = workstation or ""
			current["last_end_time"] = end_time
			current["anchor_item_code"] = _normalize_item_code(row.get("primary_item_code"))
			current["anchor_strength"] = cint(row.get("anchor_strength") or 0)
			current["anchor_source"] = row.get("execution_anchor_source") or ""
			current["anchor_campaign_key"] = row.get("campaign_key") or ""


def _get_locked_segments(plant_floors: list[str] | str | None) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "APS Schedule Segment"):
		return []
	selected_plant_floors = _coerce_plant_floor_list(plant_floors=plant_floors)
	if not selected_plant_floors:
		return []
	return frappe.get_all(
		"APS Schedule Segment",
		filters={
			"segment_status": ("in", LOCKED_SEGMENT_STATUSES),
			"is_locked": 1,
			"plant_floor": ("in", selected_plant_floors),
		},
		fields=[
			"name",
			"workstation",
			"start_time",
			"end_time",
			"planned_qty",
			"color_code",
			"material_code",
			"mould_reference",
			"primary_item_code",
			"campaign_key",
			"anchor_strength",
			"execution_anchor_source",
		],
	)


def _apply_locked_segments_to_state(
	workstation_state: dict[str, dict[str, Any]],
	locked_segments: list[dict[str, Any]],
):
	for row in locked_segments:
		state = workstation_state.get(row.get("workstation"))
		if not state:
			continue
		end_time = get_datetime(row.get("end_time"))
		if end_time > state["next_available"]:
			state["next_available"] = end_time
			state["last_color_code"] = row.get("color_code") or ""
			state["last_material_code"] = row.get("material_code") or ""
			state["last_mould_reference"] = row.get("mould_reference") or ""
			state["last_end_time"] = end_time
			state["anchor_item_code"] = _normalize_item_code(row.get("primary_item_code"))
			state["anchor_strength"] = cint(row.get("anchor_strength") or ANCHOR_STRENGTH_LOCKED)
			state["anchor_source"] = row.get("execution_anchor_source") or "APS Locked Segment"
			state["anchor_campaign_key"] = row.get("campaign_key") or _build_campaign_key(
				row.get("primary_item_code"),
				row.get("mould_reference"),
				row.get("workstation"),
			)


def _select_machine_candidates(
	item_code: str,
	item_context: dict[str, Any],
	capability_rows: list[dict[str, Any]],
	plant_floors: list[str] | str | None,
) -> list[dict[str, Any]]:
	mold_rows = _get_available_mold_rows(item_code)
	if not mold_rows:
		return []
	selected_plant_floors = _coerce_plant_floor_list(plant_floors=plant_floors)

	rules = frappe.get_all(
		"APS Mould-Machine Rule",
		filters=_strip_none({"item_code": item_code, "is_active": 1}),
		fields=["workstation", "priority", "preferred", "mould_reference", "min_tonnage", "max_tonnage"],
		order_by="preferred desc, priority asc",
	)
	rule_map = defaultdict(list)
	for row in rules:
		rule_map[row.workstation].append(row)

	candidates = []
	for capability in capability_rows:
		if selected_plant_floors and capability.get("plant_floor") not in selected_plant_floors:
			continue
		if capability.get("machine_status") not in APS_ALLOWED_MACHINE_STATUSES:
			continue
		workstation_rules = rule_map.get(capability.get("workstation")) or []
		for mold_row in mold_rows:
			if capability.get("machine_tonnage") and mold_row.get("machine_tonnage"):
				if flt(capability.get("machine_tonnage")) < flt(mold_row.get("machine_tonnage")):
					continue

			rule = _match_rule_for_candidate(workstation_rules, capability, mold_row)
			if workstation_rules and not rule:
				continue

			candidate = dict(capability)
			candidate["preferred"] = cint(rule.get("preferred")) if rule else 0
			candidate["priority"] = cint(rule.get("priority")) if rule else cint(capability.get("queue_sequence") or 999)
			candidate["mould_reference"] = mold_row.get("mold")
			candidate["output_group"] = mold_row.get("output_group") or "Default"
			candidate["configuration_label"] = mold_row.get("configuration_label")
			candidate["color_spec"] = mold_row.get("color_spec")
			candidate["mold_name"] = mold_row.get("mold_name")
			candidate["cavity_count"] = flt(
				mold_row.get("cavity_output_qty") or mold_row.get("output_qty") or mold_row.get("cavity_count")
			)
			candidate["cycle_time_seconds"] = flt(mold_row.get("cycle_time_seconds"))
			candidate["output_qty"] = flt(mold_row.get("output_qty"))
			candidate["cavity_output_qty"] = flt(mold_row.get("cavity_output_qty"))
			candidate["effective_output_qty"] = flt(mold_row.get("effective_output_qty"))
			candidate["is_family_mold"] = cint(mold_row.get("is_family_mold"))
			candidate["lane_key"] = f"{mold_row.get('mold')}::{capability.get('workstation')}"
			candidate["mold_priority"] = cint(mold_row.get("priority") or 999)
			candidate["default_product"] = cint(mold_row.get("is_default_product"))
			required_tonnage = flt(mold_row.get("machine_tonnage"))
			candidate_tonnage = flt(capability.get("machine_tonnage"))
			candidate["required_tonnage"] = required_tonnage
			candidate["tonnage_gap"] = max(candidate_tonnage - required_tonnage, 0) if candidate_tonnage and required_tonnage else 999999
			candidates.append(candidate)

	return sorted(
		candidates,
		key=lambda row: (
			-cint(row.get("preferred")),
			flt(row.get("tonnage_gap")) if row.get("tonnage_gap") is not None else 999999,
			cint(row.get("priority") or 999),
			cint(row.get("mold_priority") or 999),
			-cint(row.get("default_product")),
			row.get("mould_reference") or "",
			row.get("workstation") or "",
		),
	)


def _match_rule_for_candidate(
	workstation_rules: list[dict[str, Any]],
	capability: dict[str, Any],
	mold_row: dict[str, Any],
) -> dict[str, Any] | None:
	if not workstation_rules:
		return None

	matches = []
	for rule in workstation_rules:
		if rule.get("mould_reference") and rule.get("mould_reference") != mold_row.get("mold"):
			continue
		if rule.get("min_tonnage") and capability.get("machine_tonnage"):
			if flt(capability.get("machine_tonnage")) < flt(rule.get("min_tonnage")):
				continue
		if rule.get("max_tonnage") and capability.get("machine_tonnage"):
			if flt(capability.get("machine_tonnage")) > flt(rule.get("max_tonnage")):
				continue
		matches.append(rule)

	if not matches:
		return None

	matches.sort(key=lambda row: (-cint(row.get("preferred")), cint(row.get("priority") or 999)))
	return matches[0]


def _choose_best_slot(
	company: str,
	customer: str | None,
	item_code: str,
	item_context: dict[str, Any],
	qty: float,
	demand_date,
	horizon_start,
	horizon_end,
	workstation_state: dict[str, dict[str, Any]],
	mold_state: dict[str, dict[str, Any]],
	candidates: list[dict[str, Any]],
	settings: dict[str, Any],
	selected_plant_floors: list[str] | None = None,
	downtime_windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	if not _get_available_mold_rows(item_code):
		diagnostic = _build_mold_unavailable_diagnostic(item_code, selected_plant_floors=selected_plant_floors)
		return {
			"scheduled_qty": 0,
			"unscheduled_qty": qty,
			"result_status": "Blocked",
			"risk_status": "Blocked",
			"segments": [],
			"selected_moulds": [],
			"schedule_explanation": _("No active mold is available for {0}.").format(item_code),
			"family_side_outputs": [],
			"exceptions": [
				{
					"severity": "Critical",
					"exception_type": "Mold Unavailable",
					"message": _("No active mold is available for {0}.").format(item_code),
					"resolution_hint": _("Check Mold status, Mold Product mapping and submitted mold master data."),
					"is_blocking": 1,
					"diagnostic": diagnostic,
				}
			],
		}

	if not candidates:
		diagnostic = _build_machine_unavailable_diagnostic(
			item_code,
			selected_plant_floors=selected_plant_floors,
			candidate_molds=[row.get("mold") for row in _get_mold_master_rows_for_item(item_code)],
		)
		return {
			"scheduled_qty": 0,
			"unscheduled_qty": qty,
			"result_status": "Blocked",
			"risk_status": "Blocked",
			"segments": [],
			"selected_moulds": [],
			"schedule_explanation": _("No eligible machine lane is available for {0}.").format(item_code),
			"family_side_outputs": [],
			"exceptions": [
				{
					"severity": "Critical",
					"exception_type": "Machine Unavailable",
					"message": _("No eligible APS machine capability rows were found for {0}.").format(item_code),
					"resolution_hint": _("Maintain APS Machine Capability or relax mould-machine constraints."),
					"is_blocking": 1,
					"diagnostic": diagnostic,
				}
			],
		}

	proposals = []
	blocking_exceptions = []
	for candidate in candidates:
		proposal = _build_candidate_proposal(
			item_code=item_code,
			item_context=item_context,
			qty=qty,
			candidate=candidate,
			horizon_start=horizon_start,
			horizon_end=horizon_end,
			workstation_state=workstation_state,
			mold_state=mold_state,
			settings=settings,
			downtime_windows=downtime_windows,
		)
		if proposal.get("is_blocked"):
			blocking_exceptions.extend(proposal.get("exceptions") or [])
			continue
		proposals.append(proposal)

	if not proposals:
		return {
			"scheduled_qty": 0,
			"unscheduled_qty": qty,
			"result_status": "Blocked",
			"risk_status": "Blocked",
			"segments": [],
			"selected_moulds": [],
			"schedule_explanation": _("All candidate lanes for {0} were blocked by mold, FDA or setup constraints.").format(item_code),
			"family_side_outputs": [],
			"exceptions": blocking_exceptions,
		}

	unique_options = []
	used_workstations = set()
	used_moulds = set()
	for proposal in sorted(proposals, key=lambda row: row["score"]):
		if proposal["workstation"] in used_workstations:
			continue
		if proposal["mould_reference"] in used_moulds:
			continue
		used_workstations.add(proposal["workstation"])
		used_moulds.add(proposal["mould_reference"])
		unique_options.append(proposal)

	if not unique_options:
		return {
			"scheduled_qty": 0,
			"unscheduled_qty": qty,
			"result_status": "Blocked",
			"risk_status": "Blocked",
			"segments": [],
			"selected_moulds": [],
			"schedule_explanation": _("No unique mold-machine lanes remain for {0}.").format(item_code),
			"family_side_outputs": [],
			"exceptions": blocking_exceptions,
		}

	due_datetime = _get_due_datetime(demand_date)
	primary = unique_options[0]
	use_parallel = (
		len(unique_options) > 1
		and flt(qty) >= flt(settings.get("minimum_parallel_split_qty") or 0)
		and (primary["available_qty"] < flt(qty) or primary["end_time_full_qty"] > due_datetime)
	)
	selected_options = unique_options if use_parallel else [primary]
	if not use_parallel and primary["available_qty"] < flt(qty) and len(unique_options) > 1:
		selected_options = unique_options
		use_parallel = True

	parallel_group = f"PAR-{frappe.generate_hash(length=8)}" if use_parallel else ""
	selected_segments = []
	exceptions = list(blocking_exceptions)
	remaining = flt(qty)
	total_changeover = 0

	sequence_no = 1
	for proposal in selected_options:
		if remaining <= 0:
			break
		allocatable_qty = min(remaining, proposal["available_qty"] if use_parallel else max(proposal["available_qty"], remaining))
		if not use_parallel and proposal["available_qty"] < remaining:
			allocatable_qty = proposal["available_qty"]
		if allocatable_qty <= 0:
			continue

		chunks, unscheduled_from_downtime = _allocate_qty_around_downtime(
			start_time=proposal["start_time"],
			qty=allocatable_qty,
			hourly_capacity_qty=proposal["hourly_capacity_qty"],
			horizon_end=horizon_end,
			downtime_windows=proposal.get("downtime_windows") or [],
		)
		if unscheduled_from_downtime > 0:
			exceptions.append(
				{
					"severity": "Warning",
					"exception_type": "Downtime Capacity Loss",
					"message": _("Downtime windows prevented {0} of {1} from being scheduled inside the horizon.").format(
						frappe.format(unscheduled_from_downtime, {"fieldtype": "Float"}),
						item_code,
					),
					"workstation": proposal.get("workstation"),
					"resolution_hint": _("Extend the APS horizon or release more qualified capacity."),
					"is_blocking": 0,
				}
			)
		if len(chunks) > 1:
			exceptions.append(
				{
					"severity": "Warning",
					"exception_type": "Downtime Split",
					"message": _("APS split {0} around downtime on {1}.").format(item_code, proposal.get("workstation")),
					"workstation": proposal.get("workstation"),
					"resolution_hint": _("Review the split sequence before approving WOS proposals."),
					"is_blocking": 0,
				}
			)
		split_group = f"SPL-{frappe.generate_hash(length=8)}" if len(chunks) > 1 else ""
		for chunk_index, chunk in enumerate(chunks, start=1):
			segment_status = "Planned"
			segment = {
				"workstation": proposal["workstation"],
				"plant_floor": proposal.get("plant_floor"),
				"start_time": chunk["start_time"],
				"end_time": chunk["end_time"],
				"planned_qty": chunk["planned_qty"],
				"sequence_no": sequence_no,
				"lane_key": proposal["lane_key"],
				"campaign_key": proposal.get("campaign_key") or _build_campaign_key(item_code, proposal.get("mould_reference"), proposal.get("workstation")),
				"parallel_group": parallel_group,
				"family_group": "",
				"segment_kind": "Primary",
				"primary_item_code": item_code,
				"co_product_item_code": "",
				"setup_minutes": proposal["setup_minutes"] if chunk_index == 1 else 0,
				"changeover_minutes": proposal["setup_minutes"] if chunk_index == 1 else 0,
				"mould_reference": proposal["mould_reference"],
				"schedule_explanation": proposal["schedule_explanation"],
				"manual_change_note": "",
				"original_segment": "",
				"split_group": split_group,
				"split_index": chunk_index if split_group else 0,
				"split_reason": _("Downtime Window", context="Injection APS") if split_group else "",
				"risk_flags": "\n".join(sorted({row.get("exception_type") for row in proposal["exceptions"] if row.get("exception_type")})),
				"segment_status": segment_status,
				"anchor_strength": proposal.get("anchor_strength") or 0,
				"execution_anchor_source": proposal.get("execution_anchor_source") or "",
				"color_code": item_context.get("color_code"),
				"material_code": item_context.get("material_code"),
				"is_locked": 0,
				"is_manual": 0,
				"_output_qty": proposal["output_qty"],
				"_output_group": proposal.get("output_group") or "Default",
				"_is_family_mold": proposal["is_family_mold"],
			}
			selected_segments.append(segment)
			sequence_no += 1
		exceptions.extend(proposal["exceptions"])
		total_changeover += flt(proposal["setup_minutes"])
		remaining -= sum(flt(chunk.get("planned_qty")) for chunk in chunks)

	if not selected_segments:
		return {
			"scheduled_qty": 0,
			"unscheduled_qty": qty,
			"result_status": "Blocked",
			"risk_status": "Blocked",
			"segments": [],
			"selected_moulds": [],
			"schedule_explanation": _("No segment could be placed inside the current planning horizon for {0}.").format(item_code),
			"family_side_outputs": [],
			"exceptions": exceptions,
		}

	scheduled_qty = sum(flt(segment["planned_qty"]) for segment in selected_segments)
	unscheduled_qty = max(flt(qty) - scheduled_qty, 0)
	risk_status = "Normal"
	result_status = "Planned"
	if any(get_datetime(segment["end_time"]) > due_datetime for segment in selected_segments):
		risk_status = "Attention"
		result_status = "Risk"
	if unscheduled_qty > 0:
		late_delivery_diagnostic = _build_late_delivery_diagnostic(
			item_code=item_code,
			qty=qty,
			scheduled_qty=scheduled_qty,
			unscheduled_qty=unscheduled_qty,
			selected_plant_floors=selected_plant_floors,
			candidates=candidates,
			selected_options=selected_options,
			blocking_exceptions=blocking_exceptions,
			horizon_end=horizon_end,
		)
		risk_status = "Critical"
		result_status = "Risk"
		exceptions.append(
			{
				"severity": "Critical",
				"exception_type": "Late Delivery Risk",
				"message": _("Only {0} of {1} can be scheduled inside the current horizon for {2}.").format(
					scheduled_qty,
					qty,
					item_code,
				),
				"resolution_hint": _("Extend the horizon, release additional copy molds or split the requirement."),
				"is_blocking": 0,
				"diagnostic": late_delivery_diagnostic,
			}
		)

	if use_parallel and len(selected_segments) > 1:
		exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Copy Mold Parallelized",
				"message": _("APS split {0} across {1} mold-machine lanes to protect delivery.").format(item_code, len(selected_segments)),
				"resolution_hint": _("Review the copy-mold split and lock the sequence if the shop agrees."),
				"is_blocking": 0,
			}
		)

	base_hourly_capacity = max(selected_options[0]["hourly_capacity_qty"], 1)
	minimum_window_qty = base_hourly_capacity * flt(settings.get("minimum_run_window_hours") or 0)
	if (
		scheduled_qty > 0
		and minimum_window_qty
		and scheduled_qty < minimum_window_qty
		and total_changeover >= flt(settings.get("mold_change_penalty_minutes") or 0)
	):
		future_hint = _get_future_demand_hint(
			company=company,
			customer=customer,
			item_code=item_code,
			demand_date=demand_date,
		)
		exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Low Qty High Changeover Risk",
				"message": _(
					"{0} has a small run quantity {1} against changeover {2} minutes. {3}"
				).format(item_code, scheduled_qty, total_changeover, future_hint or ""),
				"resolution_hint": _("Consider batching this item with the next FC window if delivery promise allows."),
				"is_blocking": 0,
			}
		)

	for segment in selected_segments:
		state = workstation_state.get(segment["workstation"])
		if not state:
			pass
		else:
			state["next_available"] = get_datetime(segment["end_time"])
			state["last_color_code"] = segment.get("color_code") or ""
			state["last_material_code"] = segment.get("material_code") or ""
			state["last_mould_reference"] = segment.get("mould_reference") or ""
			state["last_end_time"] = get_datetime(segment["end_time"])
		mold_name = segment.get("mould_reference")
		if mold_name:
			mold_state[mold_name] = {
				"next_available": get_datetime(segment["end_time"]),
				"last_workstation": segment.get("workstation") or "",
				"last_end_time": get_datetime(segment["end_time"]),
			}

	detected_family_outputs, _detected_family_segments, detected_family_summary = _build_family_side_outputs(
		item_code=item_code,
		primary_segments=selected_segments,
	)
	# A side-output quantity is not a customer-demand allocation by itself. The
	# former shortcut omitted SO Item identity and had no execution ledger, so it
	# could silently satisfy another order and then disappear during consistency
	# recalculation. Until an explicit producer→consumer allocation is persisted,
	# schedule every co-product demand independently and expose the potential only
	# as a non-blocking planning hint.
	family_side_outputs = []
	family_segments = []
	family_summary = ""
	if detected_family_outputs:
		exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Family Co-Product Requires Explicit Allocation",
				"message": _(
					"Potential family-mold output was not credited automatically because no exact Sales Order Item and execution allocation ledger exists: {0}",
					context="Injection APS",
				).format(detected_family_summary),
				"resolution_hint": _(
					"Plan the co-product demand independently or create a reviewed exact allocation before release.",
					context="Injection APS",
				),
				"is_blocking": 0,
			}
		)
	clean_segments = []
	for segment in selected_segments:
		clean_segment = dict(segment)
		clean_segment.pop("_output_qty", None)
		clean_segment.pop("_output_group", None)
		clean_segment.pop("_is_family_mold", None)
		clean_segments.append(clean_segment)
	clean_segments.extend(family_segments)
	selected_moulds = list(dict.fromkeys(segment.get("mould_reference") for segment in selected_segments if segment.get("mould_reference")))
	schedule_explanation = _(
		"Scheduled {0} on {1} lane(s); molds {2}; due {3}."
	).format(
		scheduled_qty,
		len(selected_segments),
		", ".join(selected_moulds) or _("(none)", context="Injection APS"),
		getdate(demand_date),
	)
	return {
		"scheduled_qty": scheduled_qty,
		"unscheduled_qty": unscheduled_qty,
		"result_status": result_status,
		"risk_status": risk_status,
		"segments": clean_segments,
		"selected_moulds": selected_moulds,
		"copy_mold_parallel": 1 if len(selected_moulds) > 1 else 0,
		"family_mold_result": 1 if family_side_outputs else 0,
		"primary_mould_reference": selected_moulds[0] if selected_moulds else "",
		"schedule_explanation": schedule_explanation,
		"family_side_outputs": family_side_outputs,
		"family_output_summary": family_summary,
		"exceptions": exceptions,
	}


def _build_candidate_proposal(
	item_code: str,
	item_context: dict[str, Any],
	qty: float,
	candidate: dict[str, Any],
	horizon_start,
	horizon_end,
	workstation_state: dict[str, dict[str, Any]],
	mold_state: dict[str, dict[str, Any]],
	settings: dict[str, Any],
	downtime_windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	state = workstation_state.get(candidate.get("workstation")) or {}
	mold_row = mold_state.get(candidate.get("mould_reference")) or {}
	normalized_item_code = _normalize_item_code(item_code)
	base_start = max(
		get_datetime(horizon_start),
		get_datetime(state.get("next_available") or horizon_start),
		get_datetime(mold_row.get("next_available") or horizon_start),
	)
	setup_minutes, candidate_exceptions, blocked = _estimate_setup_penalty(
		candidate=candidate,
		state=state,
		item_context=item_context,
		settings=settings,
	)
	if _has_fda_conflict(item_context, candidate):
		candidate_exceptions.append(
			{
				"severity": "Critical",
				"exception_type": "FDA Conflict",
				"message": _("Workstation {0} risk category {1} cannot run FDA requirement for {2}.").format(
					candidate.get("workstation"),
					candidate.get("risk_category") or "",
					item_code,
				),
				"workstation": candidate.get("workstation"),
				"resolution_hint": _("Select an FDA-capable workstation or change the risk mapping."),
				"is_blocking": 1,
			}
		)
		blocked = True

	start_time = base_start + timedelta(minutes=setup_minutes)
	candidate_downtime_windows = _get_matching_downtime_windows(
		downtime_windows,
		workstation=candidate.get("workstation"),
		plant_floor=candidate.get("plant_floor"),
	)
	shifted_start_time = _shift_start_past_downtime(start_time, candidate_downtime_windows)
	if shifted_start_time != start_time:
		candidate_exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Downtime Start Shift",
				"message": _("Downtime shifted {0} start on {1} from {2} to {3}.").format(
					item_code,
					candidate.get("workstation"),
					frappe.format(start_time, {"fieldtype": "Datetime"}),
					frappe.format(shifted_start_time, {"fieldtype": "Datetime"}),
				),
				"workstation": candidate.get("workstation"),
				"resolution_hint": _("Review active APS Downtime Window records."),
				"is_blocking": 0,
			}
		)
	start_time = shifted_start_time
	capacity = _estimate_hourly_capacity(candidate=candidate, settings=settings)
	hourly_capacity_qty = capacity["hourly_capacity_qty"]
	if capacity.get("capacity_source") == "fallback_cycle":
		candidate_exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Missing Mold Cycle",
				"message": _("Mold {0} is missing a valid cycle time or output qty; APS used fallback cycle seconds.").format(
					candidate.get("mould_reference")
				),
				"workstation": candidate.get("workstation"),
				"resolution_hint": _("Maintain Mold.standard_cycle_seconds and Mold Product cavity / output values for more accurate scheduling."),
				"is_blocking": 0,
			}
		)

	available_hours = _available_run_hours_between(start_time, horizon_end, candidate_downtime_windows)
	if flt(candidate.get("max_run_hours")) > 0:
		available_hours = min(available_hours, flt(candidate.get("max_run_hours")))
	available_qty = max(available_hours * hourly_capacity_qty, 0)
	end_time_full_qty = _estimate_end_for_qty_around_downtime(
		start_time=start_time,
		qty=qty,
		hourly_capacity_qty=hourly_capacity_qty,
		downtime_windows=candidate_downtime_windows,
		horizon_end=horizon_end,
	)
	anchor_strength = 0
	execution_anchor_source = ""
	continuity_rank = 3
	if state.get("anchor_item_code") == normalized_item_code and state.get("last_mould_reference") == candidate.get("mould_reference"):
		anchor_strength = max(anchor_strength, cint(state.get("anchor_strength") or 0))
		execution_anchor_source = state.get("anchor_source") or execution_anchor_source
		continuity_rank = 0 if state.get("anchor_campaign_key") else 1
	elif mold_row.get("anchor_item_code") == normalized_item_code and mold_row.get("last_workstation") == candidate.get("workstation"):
		anchor_strength = max(anchor_strength, cint(mold_row.get("anchor_strength") or 0))
		execution_anchor_source = mold_row.get("anchor_source") or execution_anchor_source
		continuity_rank = 1
	elif state.get("last_mould_reference") == candidate.get("mould_reference"):
		continuity_rank = 2
	campaign_key = (
		state.get("anchor_campaign_key")
		if state.get("anchor_item_code") == normalized_item_code
		and state.get("last_mould_reference") == candidate.get("mould_reference")
		and state.get("anchor_campaign_key")
		else _build_campaign_key(item_code, candidate.get("mould_reference"), candidate.get("workstation"))
	)
	schedule_explanation = _(
		"Mold {0} on {1}; setup {2} min; hourly capacity {3}."
	).format(
		candidate.get("mould_reference"),
		candidate.get("workstation"),
		setup_minutes,
		frappe.format(hourly_capacity_qty, {"fieldtype": "Float"}),
	)
	schedule_explanation += " " + _(
		"Cycle {0}s; cavities/output {1}/{2}."
	).format(
		frappe.format(candidate.get("cycle_time_seconds") or 0, {"fieldtype": "Float"}),
		frappe.format(candidate.get("cavity_count") or 0, {"fieldtype": "Float"}),
		frappe.format(candidate.get("effective_output_qty") or 0, {"fieldtype": "Float"}),
	)
	if anchor_strength:
		schedule_explanation += " " + _(
			"Continuation anchor {0} keeps the mold on the current machine."
		).format(execution_anchor_source or _("Execution", context="Injection APS"))
	immediately_available = 1 if base_start <= (get_datetime(horizon_start) + timedelta(minutes=1)) else 0
	return {
		"is_blocked": blocked,
		"workstation": candidate.get("workstation"),
		"plant_floor": candidate.get("plant_floor"),
		"mould_reference": candidate.get("mould_reference"),
		"output_group": candidate.get("output_group") or "Default",
		"configuration_label": candidate.get("configuration_label"),
		"color_spec": candidate.get("color_spec"),
		"lane_key": candidate.get("lane_key"),
		"is_family_mold": cint(candidate.get("is_family_mold")),
		"output_qty": flt(candidate.get("output_qty")),
		"effective_output_qty": flt(candidate.get("effective_output_qty")),
		"start_time": start_time,
		"setup_minutes": setup_minutes,
		"hourly_capacity_qty": hourly_capacity_qty,
		"available_qty": available_qty,
		"end_time_full_qty": end_time_full_qty,
		"schedule_explanation": schedule_explanation,
		"campaign_key": campaign_key,
		"anchor_strength": anchor_strength,
		"execution_anchor_source": execution_anchor_source,
		"exceptions": candidate_exceptions,
		"downtime_windows": candidate_downtime_windows,
		"score": (
			continuity_rank,
			-anchor_strength,
			0 if immediately_available else 1,
			flt(candidate.get("tonnage_gap")) if candidate.get("tonnage_gap") is not None else 999999,
			end_time_full_qty,
			setup_minutes,
			-cint(candidate.get("preferred")),
			cint(candidate.get("priority") or 999),
			cint(candidate.get("mold_priority") or 999),
		),
	}


def _estimate_setup_penalty(candidate, state, item_context, settings):
	setup_minutes = flt(settings["default_setup_minutes"])
	exceptions = []
	is_blocked = False
	transition_rule = _get_color_transition_rule(state.get("last_color_code"), item_context.get("color_code"))
	if transition_rule:
		setup_minutes = max(setup_minutes, flt(transition_rule.get("setup_minutes") or setup_minutes))
		if cint(transition_rule.get("is_blocking")) or (transition_rule.get("change_level") or "") == "Blocked":
			exceptions.append(
				{
					"severity": "Critical",
					"exception_type": "Color Transition Blocked",
					"message": _("Color transition {0} -> {1} is configured as blocking.").format(
						state.get("last_color_code") or "-",
						item_context.get("color_code") or "-",
					),
					"workstation": candidate.get("workstation"),
					"resolution_hint": _("Choose another workstation or maintain a non-blocking color transition."),
					"is_blocking": 1,
				}
			)
			is_blocked = True
		elif transition_rule.get("penalty_score"):
			exceptions.append(
				{
					"severity": "Warning",
					"exception_type": "Color Transition",
					"message": _("Color transition {0} -> {1} has penalty {2}.").format(
						state.get("last_color_code") or "-",
						item_context.get("color_code") or "-",
						transition_rule.get("penalty_score"),
					),
					"workstation": candidate.get("workstation"),
					"resolution_hint": _("Group similar colors to reduce changeover cost."),
					"is_blocking": 0,
				}
			)

	if state.get("last_material_code") and state.get("last_material_code") != item_context.get("material_code"):
		setup_minutes += 15
		exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Material Changeover",
				"message": _("Material changeover is required on workstation {0}.").format(candidate.get("workstation")),
				"workstation": candidate.get("workstation"),
				"resolution_hint": _("Group the same material family where possible."),
				"is_blocking": 0,
			}
		)

	if cint(item_context.get("is_first_article")):
		setup_minutes += flt(settings["default_first_article_minutes"])
		exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "First Article Confirmation",
				"message": _("First article confirmation time was added for {0}.").format(candidate.get("workstation")),
				"workstation": candidate.get("workstation"),
				"resolution_hint": _("Keep QA review slots visible in the short horizon."),
				"is_blocking": 0,
			}
		)

	if state.get("last_mould_reference") and candidate.get("mould_reference") and state.get("last_mould_reference") != candidate.get("mould_reference"):
		setup_minutes += flt(settings.get("mold_change_penalty_minutes") or 30)
		exceptions.append(
			{
				"severity": "Warning",
				"exception_type": "Mould Changeover",
				"message": _("Mold changeover is required on workstation {0}.").format(candidate.get("workstation")),
				"workstation": candidate.get("workstation"),
				"resolution_hint": _("Avoid short runs immediately after a mould change when future FC can be batched."),
				"is_blocking": 0,
			}
		)

	return setup_minutes, exceptions, is_blocked


def _estimate_hourly_capacity(candidate: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
	effective_output_qty = max(flt(candidate.get("effective_output_qty")), flt(candidate.get("output_qty")))
	if flt(candidate.get("cycle_time_seconds")) > 0 and effective_output_qty > 0:
		return {
			"hourly_capacity_qty": (3600 / flt(candidate.get("cycle_time_seconds"))) * effective_output_qty,
			"capacity_source": "mold_cycle",
		}

	if flt(candidate.get("hourly_capacity_qty")) > 0:
		return {
			"hourly_capacity_qty": flt(candidate.get("hourly_capacity_qty")),
			"capacity_source": "machine_hourly_fallback",
		}

	if flt(candidate.get("daily_capacity_qty")) > 0:
		return {
			"hourly_capacity_qty": flt(candidate.get("daily_capacity_qty")) / 24,
			"capacity_source": "machine_daily_fallback",
		}

	fallback_cycle_seconds = flt(settings.get("missing_cycle_fallback_seconds") or 0)
	if fallback_cycle_seconds > 0 and effective_output_qty > 0:
		return {
			"hourly_capacity_qty": (3600 / fallback_cycle_seconds) * effective_output_qty,
			"capacity_source": "fallback_cycle",
		}

	return {
		"hourly_capacity_qty": flt(settings["default_hourly_capacity_qty"]),
		"capacity_source": "default_hourly_fallback",
	}


def _build_capacity_display(candidate: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
	capacity = _estimate_hourly_capacity(candidate=candidate, settings=settings)
	hourly_capacity_qty = flt(capacity.get("hourly_capacity_qty"))
	explicit_daily_capacity_qty = flt(candidate.get("daily_capacity_qty"))
	daily_capacity_qty = (
		explicit_daily_capacity_qty
		if capacity.get("capacity_source") == "machine_daily_fallback" and explicit_daily_capacity_qty > 0
		else hourly_capacity_qty * 24
	)
	return {
		"hourly_capacity_qty": hourly_capacity_qty,
		"daily_capacity_qty": flt(daily_capacity_qty),
		"capacity_source": capacity.get("capacity_source") or "",
		"capacity_source_label": CAPACITY_SOURCE_LABELS.get(capacity.get("capacity_source") or "", "Unknown Capacity Basis"),
	}


def _estimate_run_hours(qty: float, candidate: dict[str, Any], settings: dict[str, Any]) -> float:
	hourly_capacity_qty = _estimate_hourly_capacity(candidate=candidate, settings=settings)["hourly_capacity_qty"]
	return max(flt(qty) / max(hourly_capacity_qty, 1), 0.25)


def _has_fda_conflict(item_context: dict[str, Any], candidate: dict[str, Any]) -> bool:
	food_grade_value = item_context.get("food_grade")
	food_grade = str(food_grade_value or "").upper()
	risk_category = (candidate.get("risk_category") or "").strip()
	requires_fda = cint(food_grade_value) or food_grade in ("YES", "TRUE", "1") or "FDA" in food_grade
	return bool(requires_fda) and risk_category == BLOCKING_WORKSTATION_RISK


def _get_due_datetime(demand_date) -> datetime:
	date_value = getdate(demand_date or today())
	# A date promise is the half-open natural-day boundary.  Treat a segment that
	# ends exactly at next-day 00:00 as on time, matching capacity JIT slicing.
	return get_datetime(add_days(date_value, 1))


def _get_future_demand_hint(company: str, item_code: str, demand_date, customer: str | None = None) -> str:
	next_rows = frappe.get_list(
		"APS Demand Pool",
		filters=_strip_none(
			{
				"company": company,
				"customer": customer,
				"item_code": item_code,
				"demand_date": (">", getdate(demand_date)),
				"status": ("!=", "Cancelled"),
			}
		),
		fields=["demand_date", "qty", "demand_source"],
		order_by="demand_date asc",
		limit=1,
	)
	if not next_rows:
		return ""
	row = next_rows[0]
	return _("Next open demand is {0} qty on {1} ({2}).").format(
		row.get("qty"),
		row.get("demand_date"),
		row.get("demand_source") or _("Unknown", context="Injection APS"),
	)


def _build_family_side_outputs(
	item_code: str,
	primary_segments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
	side_outputs = []
	side_segments = []
	for segment in primary_segments:
		if not cint(segment.get("_is_family_mold")) or not segment.get("mould_reference"):
			continue
		primary_output_qty = max(flt(segment.get("_output_qty")), 1)
		cycles = flt(segment.get("planned_qty")) / primary_output_qty
		family_group = f"FAM-{frappe.generate_hash(length=8)}"
		segment["family_group"] = family_group
		for sibling in _get_family_output_rows(
			segment.get("mould_reference"),
			item_code,
			segment.get("_output_group"),
		):
			sibling_output_qty = flt(sibling.get("cavity_output_qty") or sibling.get("output_qty"))
			side_qty = flt(cycles * sibling_output_qty)
			if side_qty <= 0:
				continue
			side_outputs.append(
				{
					"item_code": sibling.get("item_code"),
					"qty": side_qty,
					"source_item_code": item_code,
					"mould_reference": segment.get("mould_reference"),
					"workstation": segment.get("workstation"),
				}
			)
			side_segments.append(
				{
					"workstation": segment.get("workstation"),
					"plant_floor": segment.get("plant_floor"),
					"start_time": segment.get("start_time"),
					"end_time": segment.get("end_time"),
					"planned_qty": side_qty,
					"sequence_no": segment.get("sequence_no"),
					"lane_key": segment.get("lane_key"),
					"campaign_key": segment.get("campaign_key"),
					"parallel_group": segment.get("parallel_group"),
					"family_group": family_group,
					"segment_kind": "Family Co-Product",
					"primary_item_code": item_code,
					"co_product_item_code": sibling.get("item_code"),
					"setup_minutes": 0,
					"changeover_minutes": 0,
					"mould_reference": segment.get("mould_reference"),
					"schedule_explanation": _("Family Mold co-produces {0} together with {1}.").format(
						sibling.get("item_code"),
						item_code,
					),
					"manual_change_note": "",
					"risk_flags": "Family Co-Production",
					"segment_status": segment.get("segment_status"),
					"anchor_strength": segment.get("anchor_strength") or 0,
					"execution_anchor_source": segment.get("execution_anchor_source") or "",
					"color_code": segment.get("color_code"),
					"material_code": segment.get("material_code"),
					"is_locked": 0,
					"is_manual": 0,
				}
			)

	summary = ""
	if side_outputs:
		summary = "; ".join(
			f"{row['item_code']}={frappe.format(row['qty'], {'fieldtype': 'Float'})}@{row['mould_reference']}"
			for row in side_outputs
		)
	return side_outputs, side_segments, summary


def _get_color_transition_rule(from_color: str | None, to_color: str | None) -> dict[str, Any] | None:
	if not from_color or not to_color:
		return None
	rows = frappe.get_all(
		"APS Color Transition Rule",
		filters={"from_color": from_color, "to_color": to_color, "is_active": 1},
		fields=["change_level", "penalty_score", "setup_minutes", "is_blocking"],
		limit=1,
	)
	return rows[0] if rows else None


def _create_exception(
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
	diagnostic_json: str | None = None,
):
	return frappe.get_doc(
		{
			"doctype": "APS Exception Log",
			"planning_run": planning_run,
			"severity": severity,
			"exception_type": exception_type,
			"message": message,
			"item_code": item_code,
			"customer": customer,
			"workstation": workstation,
			"source_doctype": source_doctype,
			"source_name": source_name,
			"resolution_hint": resolution_hint,
			"is_blocking": is_blocking,
			"diagnostic_json": _serialize_diagnostic_json(diagnostic=diagnostic, diagnostic_json=diagnostic_json),
			"status": "Open",
		}
	).insert(ignore_permissions=True)


def _ensure_open_exception(
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
	diagnostic_json: str | None = None,
):
	existing = frappe.db.exists(
		"APS Exception Log",
		{
			"planning_run": planning_run,
			"exception_type": exception_type,
			"source_doctype": source_doctype,
			"source_name": source_name,
			"status": "Open",
		},
	)
	if existing:
		frappe.db.set_value(
			"APS Exception Log",
			existing,
			{
				"severity": severity,
				"message": message,
				"item_code": item_code,
				"customer": customer,
				"workstation": workstation,
				"resolution_hint": resolution_hint,
				"is_blocking": is_blocking,
				"diagnostic_json": _serialize_diagnostic_json(diagnostic=diagnostic, diagnostic_json=diagnostic_json),
			},
		)
		return frappe.get_doc("APS Exception Log", existing)
	return _create_exception(
		planning_run=planning_run,
		severity=severity,
		exception_type=exception_type,
		message=message,
		item_code=item_code,
		customer=customer,
		workstation=workstation,
		source_doctype=source_doctype,
		source_name=source_name,
		resolution_hint=resolution_hint,
		is_blocking=is_blocking,
		diagnostic=diagnostic,
		diagnostic_json=diagnostic_json,
	)


def validate_run_mold_readiness(run_name: str, persist_exceptions: bool = False) -> dict[str, Any]:
	rows = []
	exception_names = []
	for result in frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name, "machine_scheduled_qty": (">", 0)},
		fields=["name", "item_code", "customer", "primary_mould_reference"],
	):
		primary_segments = _get_primary_segments_for_result(result.name)
		selected_molds = list(
			dict.fromkeys(
				segment.get("mould_reference")
				for segment in primary_segments
				if segment.get("mould_reference")
			)
		)
		available_rows = {row.get("mold"): row for row in _get_available_mold_rows(result.item_code)}
		blockers = []
		if not available_rows:
			blockers.append(("Mold Master Missing", _("Item {0} has no approved Mold / Mold Product master data for APS scheduling.").format(result.item_code)))
		if not primary_segments:
			blockers.append(("Primary Segment Missing", _("Result {0} has no primary APS schedule segment.").format(result.name)))
		if not result.primary_mould_reference or not selected_molds:
			blockers.append(("Mold Reference Empty", _("Result {0} is missing a selected mold reference.").format(result.name)))
		for mold_name in selected_molds or ([result.primary_mould_reference] if result.primary_mould_reference else []):
			mold_row = available_rows.get(mold_name)
			if not mold_row:
				blockers.append(("Mold Product Missing", _("Mold {0} is not available from Mold Product master data for {1}.").format(mold_name, result.item_code)))
				continue
			if (mold_row.get("mold_status") or "") in BLOCKING_MOLD_STATUSES:
				blockers.append(("Mold Status Blocked", _("Mold {0} is currently {1}.").format(mold_name, mold_row.get("mold_status"))))
			if flt(mold_row.get("standard_cycle_seconds")) <= 0 or flt(mold_row.get("effective_output_qty")) <= 0:
				blockers.append(("Mold Cycle Missing", _("Mold {0} is missing cycle time or effective output qty.").format(mold_name)))

		for exception_type, message in blockers:
			rows.append(
				{
					"result_name": result.name,
					"item_code": result.item_code,
					"exception_type": exception_type,
					"message": message,
					"blocking": 1,
				}
			)
			if persist_exceptions:
				exception_doc = _ensure_open_exception(
					planning_run=run_name,
					severity="Critical",
					exception_type=exception_type,
					message=message,
					item_code=result.item_code,
					customer=result.customer,
					source_doctype="APS Schedule Result",
					source_name=result.name,
					resolution_hint=_("Maintain Mold / Mold Product master data before formal APS approval."),
					is_blocking=1,
					diagnostic={
						"root_cause_codes": [exception_type.replace(" ", "_").upper()],
						"root_cause_text": message,
						"suggested_actions": [
							"Complete mold master data before recalculating or approving APS.",
						],
						"candidate_molds": selected_molds or ([result.primary_mould_reference] if result.primary_mould_reference else []),
					},
				)
				exception_names.append(exception_doc.name)
	return {
		"run": run_name,
		"rows": rows,
		"blocking_count": len(rows),
		"exception_names": exception_names,
	}


def _validate_run_segment_overlaps(run_name: str, persist_exceptions: bool = False) -> dict[str, Any]:
	rows = frappe.db.sql(
		"""
		select
			seg.name,
			seg.parent,
			seg.workstation,
			seg.start_time,
			seg.end_time,
			seg.segment_kind,
			res.item_code,
			res.customer
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` res on res.name = seg.parent
		where res.planning_run = %s
			and seg.parenttype = 'APS Schedule Result'
			and ifnull(seg.segment_kind, '') != 'Family Co-Product'
			and ifnull(seg.workstation, '') != ''
		order by seg.workstation asc, seg.start_time asc, seg.end_time asc
		""",
		[run_name],
		as_dict=True,
	)
	by_workstation = defaultdict(list)
	for row in rows:
		if row.get("start_time") and row.get("end_time"):
			by_workstation[row.get("workstation")].append(row)

	messages = []
	exception_names = []
	for workstation, segments in by_workstation.items():
		segments = sorted(segments, key=lambda row: (get_datetime(row.get("start_time")), get_datetime(row.get("end_time"))))
		for previous, current in zip(segments, segments[1:]):
			if get_datetime(current.get("start_time")) < get_datetime(previous.get("end_time")):
				message = _(
					"Workstation {0} has overlapping primary segments {1} and {2}."
				).format(workstation, previous.get("name"), current.get("name"))
				messages.append(message)
				if persist_exceptions:
					exception_doc = _ensure_open_exception(
						planning_run=run_name,
						severity="Critical",
						exception_type="Primary Segment Overlap",
						message=message,
						item_code=current.get("item_code"),
						customer=current.get("customer"),
						workstation=workstation,
						source_doctype="APS Schedule Segment",
						source_name=current.get("name"),
						resolution_hint=_("Adjust sequence or machine assignment before formal approval."),
						is_blocking=1,
						diagnostic={
							"root_cause_codes": ["WORKSTATION_PRIMARY_OVERLAP"],
							"root_cause_text": "There are two primary schedule segments on the same workstation at the same time: {0} / {1}.".format(previous.get("name"), current.get("name")),
							"suggested_actions": [
								"Adjust sequence or change workstation in the board to ensure only one primary segment exists in the same time window.",
							],
							"candidate_workstations": [workstation],
						},
					)
					exception_names.append(exception_doc.name)
	return {"run": run_name, "count": len(messages), "messages": messages, "exception_names": exception_names}


def _validate_run_mold_overlaps(run_name: str, persist_exceptions: bool = False) -> dict[str, Any]:
	rows = frappe.db.sql(
		"""
		select
			seg.name,
			seg.parent,
			seg.workstation,
			seg.mould_reference,
			seg.start_time,
			seg.end_time,
			seg.segment_kind,
			res.item_code,
			res.customer
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` res on res.name = seg.parent
		where res.planning_run = %s
			and seg.parenttype = 'APS Schedule Result'
			and ifnull(seg.segment_kind, '') != 'Family Co-Product'
			and ifnull(seg.mould_reference, '') != ''
		order by seg.mould_reference asc, seg.start_time asc, seg.end_time asc
		""",
		[run_name],
		as_dict=True,
	)
	by_mold = defaultdict(list)
	for row in rows:
		if row.get("start_time") and row.get("end_time"):
			by_mold[row.get("mould_reference")].append(row)

	messages = []
	exception_names = []
	for mold_name, segments in by_mold.items():
		segments = sorted(segments, key=lambda row: (get_datetime(row.get("start_time")), get_datetime(row.get("end_time"))))
		for previous, current in zip(segments, segments[1:]):
			if get_datetime(current.get("start_time")) < get_datetime(previous.get("end_time")):
				message = _(
					"Mold {0} overlaps across workstations {1} and {2} for segments {3} and {4}."
				).format(
					mold_name,
					previous.get("workstation") or "-",
					current.get("workstation") or "-",
					previous.get("name"),
					current.get("name"),
				)
				messages.append(message)
				if persist_exceptions:
					exception_doc = _ensure_open_exception(
						planning_run=run_name,
						severity="Critical",
						exception_type="Mold Occupancy Overlap",
						message=message,
						item_code=current.get("item_code"),
						customer=current.get("customer"),
						workstation=current.get("workstation"),
						source_doctype="APS Schedule Segment",
						source_name=current.get("name"),
						resolution_hint=_("Keep one mold on one machine at a time or split with another Mold master."),
						is_blocking=1,
						diagnostic={
							"root_cause_codes": ["MOLD_OCCUPANCY_OVERLAP"],
							"root_cause_text": "The same mold {0} is scheduled on both {1} and {2} at the same time.".format(
								mold_name,
								previous.get("workstation") or "-",
								current.get("workstation") or "-",
							),
							"suggested_actions": [
								"Keep only one primary schedule, or use another independent Mold master as the copy mold.",
								"Open the board, locate the conflicting segments, and reschedule them.",
							],
							"candidate_molds": [mold_name],
							"candidate_workstations": [previous.get("workstation"), current.get("workstation")],
						},
					)
					exception_names.append(exception_doc.name)
	return {"run": run_name, "count": len(messages), "messages": messages, "exception_names": exception_names}


def _get_primary_segments_for_result(result_name: str) -> list[dict[str, Any]]:
	return frappe.get_all(
		"APS Schedule Segment",
		filters={
			"parent": result_name,
			"parenttype": "APS Schedule Result",
			"segment_kind": ("!=", "Family Co-Product"),
			"segment_status": ("not in", list(consistency.INACTIVE_SEGMENT_STATUSES)),
			"planned_qty": (">", 0),
		},
		fields=[
			"name",
			"workstation",
			"plant_floor",
			"start_time",
			"end_time",
			"planned_qty",
			"parent",
			"mould_reference",
			"campaign_key",
			"anchor_strength",
			"execution_anchor_source",
			"segment_status",
			"risk_status",
			"schedule_delay_minutes",
			"actual_completed_qty",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"modified",
		],
		order_by="sequence_no asc, idx asc",
	)


def _get_segment_proposal_snapshot(segment_name: str | None) -> dict[str, Any] | None:
	if not segment_name or str(segment_name).startswith("cancel::"):
		return None
	rows = frappe.get_all(
		"APS Schedule Segment",
		filters={"name": segment_name},
		fields=[
			"name",
			"parent",
			"workstation",
			"plant_floor",
			"start_time",
			"end_time",
			"planned_qty",
			"mould_reference",
			"campaign_key",
			"segment_status",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"modified",
		],
		limit=1,
	)
	return dict(rows[0]) if rows else None


def _get_work_order_reconciliation_snapshot(work_order_name: str) -> dict[str, Any] | None:
	if not work_order_name or not frappe.db.exists("Work Order", work_order_name):
		return None
	wo = frappe.get_all(
		"Work Order",
		filters={"name": work_order_name},
		fields=[
			"name",
			"company",
			"production_item",
			"sales_order",
			"sales_order_item",
			"custom_aps_source",
			"qty",
			"produced_qty",
			"material_transferred_for_manufacturing",
			"docstatus",
			"planned_start_date",
			"planned_end_date",
			"status",
			"custom_aps_result_reference",
			"custom_aps_run",
			"custom_aps_schedule_reference",
			"custom_aps_proposal_batch",
			"custom_aps_required_delivery_date",
			"custom_aps_locked_for_reschedule",
			"modified",
		],
		limit=1,
	)
	if not wo:
		return None
	wo = dict(wo[0])
	scheduling_rows = []
	if frappe.db.exists("DocType", "Scheduling Item"):
		scheduling_rows = frappe.db.sql(
			"""
			select
				si.name,
				si.parent as work_order_scheduling,
				si.workstation,
				si.scheduling_qty,
				si.planned_start_date,
				si.planned_end_date,
				si.from_time,
				si.to_time,
				si.completed_qty,
				si.defect_qty,
				si.modified,
				si.custom_aps_segment_reference,
				si.custom_aps_result_reference,
				wos.status as scheduling_status,
				wos.plant_floor,
				wos.posting_date,
				wos.shift_type,
				wos.modified as scheduling_modified,
				seg.campaign_key,
				seg.mould_reference
			from `tabScheduling Item` si
			inner join `tabWork Order Scheduling` wos on wos.name = si.parent
			left join `tabAPS Schedule Segment` seg on seg.name = si.custom_aps_segment_reference
			where si.work_order = %s
			order by ifnull(si.from_time, si.planned_start_date) asc, ifnull(si.to_time, si.planned_end_date) asc
			""",
			[work_order_name],
			as_dict=True,
		)
	in_execution = any(
		(row.get("scheduling_status") or "") in FROZEN_SCHEDULING_STATUSES
		or row.get("from_time")
		or flt(row.get("completed_qty")) > 0
		or flt(row.get("defect_qty")) > 0
		for row in scheduling_rows
	)
	has_execution = (
		in_execution
		or flt(wo.get("produced_qty")) > 0
		or flt(wo.get("material_transferred_for_manufacturing")) > 0
	)
	wo["scheduling_rows"] = scheduling_rows
	wo["campaign_keys"] = [row.get("campaign_key") for row in scheduling_rows if row.get("campaign_key")]
	wo["workstations"] = [row.get("workstation") for row in scheduling_rows if row.get("workstation")]
	wo["has_execution"] = has_execution
	wo["in_execution"] = in_execution
	wo["can_cancel_unstarted"] = not has_execution and (wo.get("status") or "") in ("Submitted", "Not Started")
	wo["can_update_existing"] = (wo.get("status") or "") in ("Submitted", "Not Started")
	return wo


def _proposal_state_token(payload: dict[str, Any]) -> str:
	return hashlib.sha256(
		json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


def _work_order_result_proposal_state_token(
	result: dict[str, Any] | None,
	primary_segments: list[dict[str, Any]] | None,
) -> str:
	if not result:
		return ""
	segments = [
		{
			"name": segment.get("name") or "",
			"state": _segment_proposal_state_token(segment),
		}
		for segment in primary_segments or []
	]
	segments.sort(key=lambda row: row["name"])
	return _proposal_state_token(
		{
			"name": result.get("name") or "",
			"planning_run": result.get("planning_run") or "",
			"net_requirement": result.get("net_requirement") or "",
			"customer": result.get("customer") or "",
			"sales_order": result.get("sales_order") or "",
			"sales_order_item": result.get("sales_order_item") or "",
			"item_code": result.get("item_code") or "",
			"demand_source_snapshot_json": result.get("demand_source_snapshot_json") or "",
			"fulfillment_baseline_json": result.get("fulfillment_baseline_json") or "",
			"requested_date": str(result.get("requested_date") or ""),
			"machine_scheduled_qty": round(flt(result.get("machine_scheduled_qty")), 6),
			"production_strategy": result.get("production_strategy") or "",
			"demand_confidence": result.get("demand_confidence") or "",
			"cancellation_risk_percent": round(flt(result.get("cancellation_risk_percent")), 6),
			"prebuild_allowed": cint(result.get("prebuild_allowed")),
			"max_prebuild_days": cint(result.get("max_prebuild_days")),
			"is_urgent": cint(result.get("is_urgent")),
			"is_locked": cint(result.get("is_locked")),
			"is_manual": cint(result.get("is_manual")),
			"modified": str(result.get("modified") or ""),
			"primary_segments": segments,
		}
	)


def _work_order_proposal_state_token(snapshot: dict[str, Any] | None) -> str:
	if not snapshot:
		return ""
	scheduling_rows = [
		{
			"name": row.get("name") or "",
			"work_order_scheduling": row.get("work_order_scheduling") or "",
			"workstation": row.get("workstation") or "",
			"scheduling_qty": round(flt(row.get("scheduling_qty")), 6),
			"planned_start_date": str(row.get("planned_start_date") or ""),
			"planned_end_date": str(row.get("planned_end_date") or ""),
			"from_time": str(row.get("from_time") or ""),
			"to_time": str(row.get("to_time") or ""),
			"completed_qty": round(flt(row.get("completed_qty")), 6),
			"defect_qty": round(flt(row.get("defect_qty")), 6),
			"segment_reference": row.get("custom_aps_segment_reference") or "",
			"result_reference": row.get("custom_aps_result_reference") or "",
			"scheduling_status": row.get("scheduling_status") or "",
			"modified": str(row.get("modified") or ""),
			"scheduling_modified": str(row.get("scheduling_modified") or ""),
		}
		for row in snapshot.get("scheduling_rows") or []
	]
	scheduling_rows.sort(key=lambda row: (row["work_order_scheduling"], row["name"]))
	return _proposal_state_token(
		{
			"name": snapshot.get("name") or "",
			"company": snapshot.get("company") or "",
			"production_item": snapshot.get("production_item") or "",
			"sales_order": snapshot.get("sales_order") or "",
			"sales_order_item": snapshot.get("sales_order_item") or "",
			"custom_aps_source": snapshot.get("custom_aps_source") or "",
			"qty": round(flt(snapshot.get("qty")), 6),
			"produced_qty": round(flt(snapshot.get("produced_qty")), 6),
			"material_transferred_for_manufacturing": round(
				flt(snapshot.get("material_transferred_for_manufacturing")), 6
			),
			"docstatus": cint(snapshot.get("docstatus")),
			"status": snapshot.get("status") or "",
			"result_reference": snapshot.get("custom_aps_result_reference") or "",
			"run": snapshot.get("custom_aps_run") or "",
			"proposal_batch": snapshot.get("custom_aps_proposal_batch") or "",
			"modified": str(snapshot.get("modified") or ""),
			"scheduling_rows": scheduling_rows,
		}
	)


def _segment_proposal_state_token(segment: dict[str, Any] | None) -> str:
	if not segment:
		return ""
	return _proposal_state_token(
		{
			"name": segment.get("name") or "",
			"parent": segment.get("parent") or "",
			"workstation": segment.get("workstation") or "",
			"plant_floor": segment.get("plant_floor") or "",
			"start_time": str(segment.get("start_time") or ""),
			"end_time": str(segment.get("end_time") or ""),
			"planned_qty": round(flt(segment.get("planned_qty")), 6),
			"mould_reference": segment.get("mould_reference") or "",
			"campaign_key": segment.get("campaign_key") or "",
			"linked_work_order": segment.get("linked_work_order") or "",
			"linked_work_order_scheduling": segment.get("linked_work_order_scheduling") or "",
			"linked_scheduling_item": segment.get("linked_scheduling_item") or "",
		}
	)


def _scheduling_row_proposal_state_token(row: dict[str, Any] | None) -> str:
	if not row:
		return ""
	return _proposal_state_token(
		{
			"name": row.get("name") or "",
			"work_order_scheduling": row.get("work_order_scheduling") or "",
			"workstation": row.get("workstation") or "",
			"scheduling_qty": round(flt(row.get("scheduling_qty")), 6),
			"planned_start_date": str(row.get("planned_start_date") or ""),
			"planned_end_date": str(row.get("planned_end_date") or ""),
			"from_time": str(row.get("from_time") or ""),
			"to_time": str(row.get("to_time") or ""),
			"completed_qty": round(flt(row.get("completed_qty")), 6),
			"segment_reference": row.get("custom_aps_segment_reference") or "",
			"result_reference": row.get("custom_aps_result_reference") or "",
			"scheduling_status": row.get("scheduling_status") or "",
			"modified": str(row.get("modified") or ""),
			"scheduling_modified": str(row.get("scheduling_modified") or ""),
		}
	)


def _parse_json_object(value, default):
	if isinstance(value, (dict, list)):
		return value
	if not value:
		return default
	try:
		parsed = json.loads(value)
	except (TypeError, ValueError):
		return default
	return parsed if isinstance(parsed, type(default)) else default


def _get_result_sales_order_lineage(result: dict[str, Any] | None) -> dict[str, Any]:
	"""Resolve one explicit SO/SOI lineage; never infer across orders or details."""
	result = result or {}
	source_rows = _parse_json_object(result.get("demand_source_snapshot_json"), [])
	sales_orders = {
		value
		for value in [result.get("sales_order"), *(row.get("sales_order") for row in source_rows)]
		if value
	}
	if len(sales_orders) > 1:
		return {
			"sales_order": None,
			"sales_order_item": None,
			"can_reuse": False,
			"blocking_reason": _("The result aggregates more than one Sales Order."),
		}
	sales_order = next(iter(sales_orders), None)
	if not sales_order:
		demand_source = result.get("demand_source") or ""
		if demand_source not in {"Safety Stock", "Stock Production"}:
			return {
				"sales_order": None,
				"sales_order_item": None,
				"can_reuse": False,
				"blocking_reason": _(
					"Customer or forecast demand has no exact Sales Order and Sales Order Item. "
					"Link the demand before Work Order release; APS will not convert it silently to stock production.",
					context="Injection APS",
				),
			}
		return {
			"sales_order": None,
			"sales_order_item": None,
			"can_reuse": False,
			"blocking_reason": None,
		}
	source_items = {
		value
		for value in [result.get("sales_order_item"), *(row.get("sales_order_item") for row in source_rows)]
		if value
	}
	if len(source_items) > 1:
		return {
			"sales_order": sales_order,
			"sales_order_item": None,
			"can_reuse": False,
			"blocking_reason": _("The result aggregates more than one Sales Order Item."),
		}
	sales_order_item = next(iter(source_items), None)
	if not sales_order_item:
		sales_order_item = _resolve_unique_sales_order_item(sales_order, result.get("item_code"))
	if not sales_order_item:
		return {
			"sales_order": sales_order,
			"sales_order_item": None,
			"can_reuse": False,
			"blocking_reason": _(
				"The Sales Order does not identify exactly one compatible item row; select an explicit Sales Order Item before release."
			),
		}
	return {
		"sales_order": sales_order,
		"sales_order_item": sales_order_item,
		"can_reuse": True,
		"blocking_reason": None,
	}


def _get_result_open_work_order_coverage(result: dict[str, Any] | Any | None) -> float:
	"""Read the exact WO quantity already deducted before this Result was planned."""
	result = result or {}
	baseline = _parse_json_object(result.get("fulfillment_baseline_json"), {})
	evidence = baseline.get("net_requirement") or {}
	if not isinstance(evidence, dict) or evidence.get("existing_work_order_policy") != "Include":
		return 0
	return max(flt(evidence.get("open_work_order_qty")), 0)


def _validate_exact_sales_order_lineage(
	lineage: dict[str, Any] | None,
	*,
	item_code: str | None,
	company: str | None = None,
	customer: str | None = None,
) -> dict[str, Any]:
	"""Validate the exact submitted SO/SOI tuple used by a formal Work Order.

	ERPNext validates that the Sales Order contains a compatible item, but it does
	not guarantee that a supplied ``sales_order_item`` is that compatible row.
	APS therefore owns this stronger validation and repeats it after locking the
	SO/SOI rows during proposal Apply.
	"""
	lineage = dict(lineage or {})
	sales_order = lineage.get("sales_order")
	sales_order_item = lineage.get("sales_order_item")
	if not sales_order and not sales_order_item:
		return lineage
	if not sales_order or not sales_order_item:
		frappe.throw(
			_("A formal Work Order requires both Sales Order and Sales Order Item, or neither."),
			frappe.ValidationError,
		)

	order = frappe.db.get_value(
		"Sales Order",
		sales_order,
		["name", "docstatus", "status", "company", "customer"],
		as_dict=True,
	)
	if not order or cint(order.get("docstatus")) != 1 or (order.get("status") or "") in {
		"Cancelled",
		"Closed",
	}:
		frappe.throw(
			_("Sales Order {0} is no longer submitted and active. Regenerate the proposal batch.").format(
				sales_order
			),
			frappe.ValidationError,
		)
	if company and (order.get("company") or "") != company:
		frappe.throw(
			_("Sales Order {0} belongs to a different company. Regenerate the proposal batch.").format(
				sales_order
			),
			frappe.ValidationError,
		)
	if customer and (order.get("customer") or "") != customer:
		frappe.throw(
			_("Sales Order {0} belongs to a different customer. Regenerate the proposal batch.").format(
				sales_order
			),
			frappe.ValidationError,
		)

	order_item = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		["name", "parent", "parenttype", "item_code"],
		as_dict=True,
	)
	resolved_item = _normalize_item_code(item_code) or (item_code or "")
	if (
		not order_item
		or (order_item.get("parent") or "") != sales_order
		or (order_item.get("parenttype") or "Sales Order") != "Sales Order"
		or (_normalize_item_code(order_item.get("item_code")) or order_item.get("item_code") or "")
		!= resolved_item
	):
		frappe.throw(
			_("Sales Order Item {0} does not belong to Sales Order {1} and item {2}.").format(
				sales_order_item,
				sales_order,
				resolved_item or "-",
			),
			frappe.ValidationError,
		)
	return lineage


def _result_lineage_is_explicitly_retired(result_name: str | None) -> bool:
	"""Return True only for immutable APS evidence that every target retired."""
	if not result_name:
		return False
	rows = frappe.get_all(
		"APS Schedule Result",
		filters={"name": result_name},
		fields=["name", "fulfillment_baseline_json"],
		limit=1,
	)
	if not rows:
		return False
	baseline = _parse_json_object(rows[0].get("fulfillment_baseline_json"), {})
	targets = [row for row in baseline.get("targets") or [] if isinstance(row, dict)]
	return bool(targets) and all(cint(row.get("retired")) for row in targets)


def _result_lineage_can_transfer_to(
	owner_result_name: str | None,
	target_result: dict[str, Any] | Any | None,
) -> bool:
	"""Allow an explicit, reviewable transfer after a schedule replacement.

	The import remap preserves the new target identity on the old Result and marks
	that Result Blocked.  An unstarted WO may therefore move only when the old and
	new Results have the same company/customer/item/SO/SOI and at least one exact
	active target in common.  The proposal row exposes the old owner and Apply
	revalidates the same evidence under locks; execution-bearing WOs remain barred.
	"""
	if not owner_result_name or not target_result:
		return False
	rows = frappe.get_all(
		"APS Schedule Result",
		filters={"name": owner_result_name},
		fields=[
			"name",
			"company",
			"customer",
			"sales_order",
			"sales_order_item",
			"item_code",
			"status",
			"flow_step",
			"fulfillment_baseline_json",
		],
		limit=1,
	)
	if not rows:
		return False
	owner = rows[0]
	if (owner.get("status") or "") != "Blocked" or (
		owner.get("flow_step") or ""
	) != "Customer Schedule Changed":
		return False
	for fieldname in (
		"company",
		"customer",
		"sales_order",
		"sales_order_item",
		"item_code",
	):
		if (owner.get(fieldname) or "") != (target_result.get(fieldname) or ""):
			return False
	owner_baseline = _parse_json_object(owner.get("fulfillment_baseline_json"), {})
	target_baseline = _parse_json_object(target_result.get("fulfillment_baseline_json"), {})
	owner_targets = {
		row.get("customer_schedule_item")
		for row in owner_baseline.get("targets") or []
		if isinstance(row, dict) and not cint(row.get("retired")) and row.get("customer_schedule_item")
	}
	target_targets = {
		row.get("customer_schedule_item")
		for row in target_baseline.get("targets") or []
		if isinstance(row, dict) and not cint(row.get("retired")) and row.get("customer_schedule_item")
	}
	return bool(owner_targets & target_targets)


def _work_order_can_be_controlled_reused(
	snapshot: dict[str, Any] | None,
	*,
	result_name: str,
	run_name: str | None,
	target_result: dict[str, Any] | Any | None = None,
) -> bool:
	"""Disallow silently stealing a Work Order from another active APS lineage."""
	snapshot = snapshot or {}
	owner_result = snapshot.get("custom_aps_result_reference") or ""
	owner_run = snapshot.get("custom_aps_run") or ""
	if owner_result == (result_name or "") and (not owner_run or owner_run == (run_name or "")):
		return True
	if not owner_result and not owner_run:
		return True
	if owner_result and _result_lineage_is_explicitly_retired(owner_result):
		return True
	return bool(
		owner_result
		and not snapshot.get("has_execution")
		and _result_lineage_can_transfer_to(owner_result, target_result)
	)


def _find_existing_work_order_for_result(
	result_name: str,
	item_code: str,
	company: str | None = None,
	run_name: str | None = None,
	sales_order: str | None = None,
	sales_order_item: str | None = None,
	stock_purpose: str | None = None,
	target_result: dict[str, Any] | Any | None = None,
	preferred_workstation: str | None = None,
	preferred_campaign_key: str | None = None,
	excluded_work_orders: list[str] | None = None,
	require_unique: bool = False,
) -> dict[str, Any] | None:
	# Reuse is either one exact SO/SOI tuple or one explicit APS stock-purpose
	# pool. It never falls back to company+item alone.
	exact_sales_lineage = bool(sales_order and sales_order_item)
	exact_stock_pool = stock_purpose in {"Stock Production", "Safety Stock"}
	if not exact_sales_lineage and not exact_stock_pool:
		return None
	item_name = _normalize_item_code(item_code) or item_code
	excluded_work_orders = [name for name in (excluded_work_orders or []) if name]
	rows = frappe.get_all(
		"Work Order",
		filters=_strip_none(
			{
				"company": company,
				"production_item": item_name,
				"sales_order": sales_order if exact_sales_lineage else None,
				"sales_order_item": sales_order_item if exact_sales_lineage else None,
				"docstatus": 1,
				"status": ("not in", list(INACTIVE_WORK_ORDER_STATUSES)),
			}
		),
		fields=[
			"name",
			"sales_order",
			"sales_order_item",
			"custom_aps_result_reference",
			"planned_start_date",
			"creation",
		],
		order_by="planned_start_date asc, creation asc",
	)
	if not rows:
		return None
	eligible = []
	for row in rows:
		if row.get("name") in excluded_work_orders:
			continue
		snapshot = _get_work_order_reconciliation_snapshot(row.get("name"))
		if not snapshot:
			continue
		if exact_stock_pool and (
			(snapshot.get("sales_order") or "")
			or (snapshot.get("sales_order_item") or "")
			or (snapshot.get("custom_aps_source") or "") != stock_purpose
		):
			continue
		if not _work_order_can_be_controlled_reused(
			snapshot,
			result_name=result_name,
			run_name=run_name,
			target_result=target_result,
		):
			continue
		score = (
			0 if snapshot.get("custom_aps_result_reference") == result_name else 1,
			0 if preferred_campaign_key and preferred_campaign_key in (snapshot.get("campaign_keys") or []) else 1,
			0 if preferred_workstation and preferred_workstation in (snapshot.get("workstations") or []) else 1,
			0 if snapshot.get("in_execution") else 1,
			0 if snapshot.get("has_execution") else 1,
			get_datetime(snapshot.get("planned_start_date") or now_datetime()),
			snapshot.get("name"),
		)
		eligible.append((score, snapshot))
	if require_unique and len(eligible) > 1:
		frappe.throw(
			_(
				"Multiple open Work Orders match the same Sales Order Item for APS result {0}. Review their impact and consolidate or close them before controlled reuse.",
				context="Injection APS",
			).format(result_name),
			frappe.ValidationError,
		)
	return min(eligible, key=lambda row: row[0])[1] if eligible else None


def _classify_work_order_action(
	existing: dict[str, Any] | None,
	proposed_qty: float,
	prefer_update_existing: bool = False,
) -> str:
	if not existing:
		return "New"
	if (existing.get("status") or "") in INACTIVE_WORK_ORDER_STATUSES:
		frappe.throw(
			_("Work Order {0} is {1} and cannot be reused by APS.", context="Injection APS").format(
				existing.get("name") or "-", existing.get("status")
			),
			frappe.ValidationError,
		)
	existing_qty = flt(existing.get("qty"))
	produced_qty = flt(existing.get("produced_qty"))
	if proposed_qty <= 0:
		return "Close Residual" if existing.get("has_execution") else "Cancel Unstarted"
	if abs(existing_qty - proposed_qty) < 0.0001:
		return "Keep Existing"
	can_update_existing = bool(existing.get("can_update_existing"))
	if not can_update_existing:
		if proposed_qty > existing_qty + 0.0001:
			return "Create Delta"
		frappe.throw(
			_("Work Order {0} has execution, transferred material, or fixed scheduling and cannot be reduced automatically. Resolve its residual quantity before regenerating APS proposals.", context="Injection APS").format(
				existing.get("name") or "-"
			),
			frappe.ValidationError,
		)
	if existing.get("has_execution"):
		if proposed_qty > existing_qty + 0.0001:
			return "Update Existing" if prefer_update_existing else "Create Delta"
		if proposed_qty > produced_qty + 0.0001:
			return "Update Existing"
		return "Close Residual"
	return "Update Existing"


def _get_open_aps_managed_work_orders(company: str | None = None) -> list[dict[str, Any]]:
	rows = frappe.get_all(
		"Work Order",
		filters=_strip_none(
			{
				"company": company,
				"docstatus": 1,
				"status": ("not in", list(INACTIVE_WORK_ORDER_STATUSES)),
			}
		),
		fields=["name", "custom_aps_run", "custom_aps_result_reference", "custom_aps_locked_for_reschedule"],
		order_by="planned_start_date asc, creation asc",
	)
	result = []
	for row in rows:
		if not (row.get("custom_aps_run") or row.get("custom_aps_result_reference") or cint(row.get("custom_aps_locked_for_reschedule"))):
			continue
		snapshot = _get_work_order_reconciliation_snapshot(row.get("name"))
		if snapshot:
			result.append(snapshot)
	return result


def _work_order_is_orphan_candidate_for_run(snapshot: dict[str, Any], run_name: str) -> bool:
	"""Never cancel another active run's WO merely because this run did not match it."""
	if (snapshot.get("custom_aps_run") or "") == (run_name or ""):
		return True
	result_name = snapshot.get("custom_aps_result_reference")
	if not result_name:
		return False
	rows = frappe.get_all(
		"APS Schedule Result",
		filters={"name": result_name},
		fields=["name", "planning_run", "demand_source", "fulfillment_baseline_json"],
		limit=1,
	)
	if not rows:
		# Missing historical lineage is ambiguous, not proof that demand was cancelled.
		return False
	result = rows[0]
	if (result.get("planning_run") or "") == (run_name or ""):
		return True
	baseline = _parse_json_object(result.get("fulfillment_baseline_json"), {})
	targets = [row for row in baseline.get("targets") or [] if isinstance(row, dict)]
	if not targets:
		return False
	active_target_names = [
		row.get("customer_schedule_item")
		for row in targets
		if row.get("customer_schedule_item") and not cint(row.get("retired"))
	]
	if not active_target_names:
		return True
	active_count = frappe.db.sql(
		"""
		select count(*)
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name in %(target_names)s
			and s.status = 'Active'
			and ifnull(i.status, '') != 'Cancelled'
		""",
		{"target_names": tuple(sorted(set(active_target_names)))},
	)[0][0]
	return cint(active_count) == 0


def _set_run_result_segment_status(
	run_name: str,
	run_status: str,
	result_status: str,
	segment_status: str,
	flow_step: str | None = None,
	next_step_hint: str | None = None,
	result_names: list[str] | tuple[str, ...] | None = None,
	segment_names: list[str] | tuple[str, ...] | None = None,
):
	frappe.db.set_value("APS Planning Run", run_name, "status", run_status)
	result_names = [name for name in (result_names or []) if name]
	segment_names = [name for name in (segment_names or []) if name]
	result_set_clause = ["status = %s"]
	result_params: list[Any] = [result_status]
	if flow_step is not None:
		result_set_clause.append("flow_step = %s")
		result_params.append(flow_step)
	if next_step_hint is not None:
		result_set_clause.append("next_step_hint = %s")
		result_params.append(next_step_hint)
	result_sql = f"""
		update `tabAPS Schedule Result`
		set {", ".join(result_set_clause)}
		where planning_run = %s
	"""
	result_params.append(run_name)
	if result_names:
		result_sql += " and name in %s"
		result_params.append(tuple(result_names))
	if result_status != "Blocked":
		result_sql += " and ifnull(status, '') != 'Blocked'"
	frappe.db.sql(result_sql, result_params)

	segment_sql = """
		update `tabAPS Schedule Segment`
		set segment_status = %s
		where parenttype = 'APS Schedule Result'
			and parent in (
				select name
				from `tabAPS Schedule Result`
				where planning_run = %s
			)
	"""
	segment_params: list[Any] = [segment_status, run_name]
	if result_names:
		segment_sql += " and parent in %s"
		segment_params.append(tuple(result_names))
	if segment_names:
		segment_sql += " and name in %s"
		segment_params.append(tuple(segment_names))
	if segment_status != "Blocked":
		segment_sql += " and ifnull(segment_status, '') != 'Blocked'"
	frappe.db.sql(segment_sql, segment_params)


def _create_formal_work_order(
	run_doc,
	result,
	qty: float,
	start_time,
	end_time,
	settings: dict[str, Any],
	proposal_batch: str,
	sales_order: str | None,
	sales_order_item: str | None,
) -> str:
	item_code = _resolve_item_name(result.item_code) or result.item_code
	bom_no = frappe.db.get_value("Item", item_code, "default_bom")
	if not bom_no:
		bom_no = frappe.db.get_value("BOM", {"item": item_code, "is_default": 1, "is_active": 1}, "name")
	if not bom_no:
		frappe.throw(_("No BOM was found for {0}.").format(item_code))
	plant_floor_doc = None
	primary_segments = _get_primary_segments_for_result(result.name)
	work_order_floor = _get_primary_result_plant_floor(primary_segments, run_doc.plant_floor)
	if work_order_floor and frappe.db.exists("DocType", "Plant Floor"):
		plant_floor_doc = frappe.get_doc("Plant Floor", work_order_floor)
	warehouse_values = _get_work_order_warehouse_values(
		plant_floor_doc=plant_floor_doc,
		settings=settings,
		item_code=item_code,
		company=run_doc.company,
	)
	stock_pool = _get_aps_work_order_stock_pool(
		demand_source=result.demand_source,
		sales_order=sales_order,
		sales_order_item=sales_order_item,
	)
	work_order_values = {
		"doctype": "Work Order",
		"production_item": item_code,
		"bom_no": bom_no,
		"qty": qty,
		"company": run_doc.company,
		# Always use the exact lineage resolved during proposal generation and
		# revalidated under lock during Apply.  The result fields may be blank when
		# a unique SO detail was resolved from the frozen demand snapshot.
		"sales_order": sales_order or None,
		"sales_order_item": sales_order_item or None,
		"planned_start_date": start_time,
		"planned_end_date": end_time,
		"wip_warehouse": warehouse_values.get("wip_warehouse"),
		"source_warehouse": warehouse_values.get("source_warehouse"),
		"fg_warehouse": warehouse_values.get("fg_warehouse"),
		"scrap_warehouse": warehouse_values.get("scrap_warehouse"),
		"custom_aps_run": run_doc.name,
		# No-SO production must carry an explicit APS stock purpose. Rebuilds may
		# consume only that purpose-specific pool; the Result link retains the
		# original demand-source audit trail.
		"custom_aps_source": stock_pool or result.demand_source or "APS Planning Run",
		"custom_aps_required_delivery_date": result.requested_date,
		"custom_aps_is_urgent": result.is_urgent,
		"custom_aps_release_status": "Planned",
		"custom_aps_locked_for_reschedule": 1,
		"custom_aps_schedule_reference": result.name,
		"custom_aps_result_reference": result.name,
		"custom_aps_proposal_batch": proposal_batch,
	}
	if frappe.get_meta("Work Order").has_field("custom_purpose"):
		work_order_values["custom_purpose"] = "Stock"
	work_order = frappe.get_doc(work_order_values)
	work_order.insert(ignore_permissions=True)
	work_order.submit()
	return work_order.name


def _get_aps_work_order_stock_pool(
	*,
	demand_source: str | None,
	sales_order: str | None,
	sales_order_item: str | None,
) -> str | None:
	if sales_order and sales_order_item:
		return None
	if sales_order or sales_order_item:
		return None
	if demand_source == "Safety Stock":
		return "Safety Stock"
	if demand_source == "Stock Production":
		return "Stock Production"
	return None


def _save_work_order_with_controller(work_order, ignore_submitted_validation: bool = True):
	if ignore_submitted_validation:
		work_order.flags.ignore_validate_update_after_submit = True
	work_order.save(ignore_permissions=True)
	if cint(work_order.docstatus) != 1:
		return
	# ERPNext only refreshes these derivatives from on_submit/on_cancel. APS is
	# allowed to rewrite an entirely unstarted submitted WO, so the controller
	# side effects must run in the same transaction or Bin production reservation
	# and Sales Order work_order_qty remain at the old quantity.
	if work_order.production_plan and frappe.db.exists(
		"Production Plan Item Reference", {"parent": work_order.production_plan}
	):
		work_order.update_work_order_qty_in_combined_so()
	else:
		work_order.update_work_order_qty_in_so()
	work_order.update_ordered_qty()
	work_order.update_planned_qty()
	work_order.update_reserved_qty_for_production()


def _link_existing_work_order_to_result(
	work_order_name: str,
	run_name: str,
	result_name: str,
	proposal_batch: str,
	required_delivery_date=None,
):
	values = {
		"custom_aps_run": run_name,
		"custom_aps_release_status": "Planned",
		"custom_aps_locked_for_reschedule": 1,
		"custom_aps_schedule_reference": result_name,
		"custom_aps_result_reference": result_name,
		"custom_aps_proposal_batch": proposal_batch,
	}
	if required_delivery_date:
		values["custom_aps_required_delivery_date"] = required_delivery_date
	work_order = frappe.get_doc("Work Order", work_order_name)
	for fieldname, value in values.items():
		work_order.set(fieldname, value)
	_save_work_order_with_controller(work_order)


def _update_existing_work_order(
	work_order_name: str,
	run_name: str,
	result_name: str,
	proposal_batch: str,
	qty: float,
	start_time,
	end_time,
	required_delivery_date=None,
) -> str:
	snapshot = _get_work_order_reconciliation_snapshot(work_order_name)
	if not snapshot or not snapshot.get("can_update_existing"):
		frappe.throw(_("Work Order {0} can no longer be rewritten by APS.").format(work_order_name))
	old_qty = max(flt(snapshot.get("qty")), 1)
	new_qty = max(flt(qty), 0)
	if new_qty <= 0:
		frappe.throw(_("Work Order {0} cannot be updated to zero qty.").format(work_order_name))
	if new_qty + 0.0001 < flt(snapshot.get("produced_qty")):
		frappe.throw(
			_("Work Order {0} cannot be reduced below already produced qty {1}.").format(
				work_order_name,
				frappe.format(flt(snapshot.get("produced_qty")), {"fieldtype": "Float"}),
			)
		)
	ratio = new_qty / old_qty if old_qty else 1
	values = {
		"qty": new_qty,
		"planned_start_date": start_time,
		"planned_end_date": end_time,
		"custom_aps_run": run_name,
		"custom_aps_release_status": "Planned",
		"custom_aps_locked_for_reschedule": 1,
		"custom_aps_schedule_reference": result_name,
		"custom_aps_result_reference": result_name,
		"custom_aps_proposal_batch": proposal_batch,
	}
	if required_delivery_date:
		values["custom_aps_required_delivery_date"] = required_delivery_date
	work_order = frappe.get_doc("Work Order", work_order_name)
	for fieldname, value in values.items():
		work_order.set(fieldname, value)
	for row in work_order.get("required_items") or []:
		if cint(row.get("is_additional_item")):
			continue
		required_qty = flt(row.get("required_qty")) * ratio
		amount = flt(row.get("rate")) * required_qty if flt(row.get("rate")) else 0
		row.required_qty = required_qty
		row.amount = amount
	for row in work_order.get("operations") or []:
		if flt(row.get("completed_qty")) > 0 or (row.get("status") or "") == "Completed":
			continue
		time_in_mins = flt(row.get("time_in_mins")) * ratio if flt(row.get("time_in_mins")) else 0
		planned_operating_cost = flt(row.get("planned_operating_cost")) * ratio if flt(row.get("planned_operating_cost")) else 0
		row.time_in_mins = time_in_mins
		row.planned_operating_cost = planned_operating_cost
	_save_work_order_with_controller(work_order)
	return work_order_name


def _cancel_unstarted_work_order(work_order_name: str) -> str:
	snapshot = _get_work_order_reconciliation_snapshot(work_order_name)
	if not snapshot or not snapshot.get("can_cancel_unstarted"):
		frappe.throw(_("Work Order {0} can no longer be cancelled as unstarted.").format(work_order_name))
	work_order = frappe.get_doc("Work Order", work_order_name)
	work_order.cancel()
	return work_order.name


def _close_residual_work_order(
	work_order_name: str,
	run_name: str,
	result_name: str | None = None,
	proposal_batch: str | None = None,
	required_delivery_date=None,
) -> str:
	from erpnext.manufacturing.doctype.work_order.work_order import close_work_order

	close_work_order(work_order_name, "Closed")
	values = {
		"custom_aps_run": run_name,
		"custom_aps_release_status": "Locked",
		"custom_aps_locked_for_reschedule": 1,
	}
	if result_name:
		values["custom_aps_schedule_reference"] = result_name
		values["custom_aps_result_reference"] = result_name
	if proposal_batch:
		values["custom_aps_proposal_batch"] = proposal_batch
	if required_delivery_date:
		values["custom_aps_required_delivery_date"] = required_delivery_date
	work_order = frappe.get_doc("Work Order", work_order_name)
	for fieldname, value in values.items():
		work_order.set(fieldname, value)
	_save_work_order_with_controller(work_order)
	return work_order_name


def _is_frozen_scheduling_row(row: dict[str, Any] | None) -> bool:
	row = row or {}
	return (
		(row.get("scheduling_status") or "") in FROZEN_SCHEDULING_STATUSES
		or bool(row.get("from_time"))
		or flt(row.get("completed_qty")) > 0
		or flt(row.get("defect_qty")) > 0
	)


def _get_formal_scheduling_reconciliation_rows(
	work_order_name: str,
	release_from=None,
	release_to=None,
) -> list[dict[str, Any]]:
	snapshot = _get_work_order_reconciliation_snapshot(work_order_name)
	rows = [dict(row) for row in (snapshot or {}).get("scheduling_rows") or []]
	start_date = getdate(release_from) if release_from else None
	limit_date = getdate(release_to) if release_to else None
	result = []
	for row in rows:
		posting_date = getdate(row.get("posting_date")) if row.get("posting_date") else None
		if start_date and posting_date and posting_date < start_date:
			continue
		if limit_date and posting_date and posting_date > limit_date:
			continue
		row["is_frozen"] = 1 if _is_frozen_scheduling_row(row) else 0
		row["posting_date"] = posting_date
		result.append(row)
	return result


def _remove_scheduling_item_from_doc(doc, scheduling_item_name: str):
	child = next((row for row in doc.get("scheduling_items") if row.name == scheduling_item_name), None)
	if child:
		doc.remove(child)


def _cleanup_empty_formal_scheduling_doc(docname: str):
	if not docname or not frappe.db.exists("Work Order Scheduling", docname):
		return
	doc = frappe.get_doc("Work Order Scheduling", docname)
	if doc.get("scheduling_items"):
		doc.save(ignore_permissions=True)
		return
	if (doc.status or "") in FROZEN_SCHEDULING_STATUSES:
		doc.save(ignore_permissions=True)
		return
	frappe.delete_doc("Work Order Scheduling", doc.name, force=1, ignore_permissions=True)


def _drop_unfrozen_scheduling_rows_for_work_order(
	work_order_name: str,
	planning_run: str,
	source_doctype: str,
	source_name: str,
) -> dict[str, Any]:
	removed_rows = []
	frozen_rows = []
	for row in _get_formal_scheduling_reconciliation_rows(work_order_name):
		if _is_frozen_scheduling_row(row):
			frozen_rows.append(row)
			continue
		doc = frappe.get_doc("Work Order Scheduling", row.get("work_order_scheduling"))
		_remove_scheduling_item_from_doc(doc, row.get("name"))
		doc.custom_aps_run = planning_run
		doc.custom_aps_freeze_state = "Open"
		doc.custom_aps_approval_state = "Approved"
		doc.save(ignore_permissions=True)
		_cleanup_empty_formal_scheduling_doc(doc.name)
		removed_rows.append(row)
	if frozen_rows:
		_ensure_open_exception(
			planning_run=planning_run,
			severity="Warning",
			exception_type="Residual Scheduling Frozen",
			message=_("Work Order {0} still has frozen formal scheduling rows.").format(work_order_name),
			item_code=frappe.db.get_value("Work Order", work_order_name, "production_item"),
			source_doctype=source_doctype,
			source_name=source_name,
			resolution_hint=_("Frozen scheduling rows were preserved and require manual completion or cancellation handling."),
			is_blocking=0,
		)
	return {"removed_rows": removed_rows, "frozen_rows": frozen_rows}


def _get_or_create_formal_shift_scheduling_doc(
	company: str,
	plant_floor: str,
	posting_date,
	shift_type: str,
	planning_run: str,
	batch_name: str,
):
	existing_name = frappe.db.get_value(
		"Work Order Scheduling",
		{
			"posting_date": getdate(posting_date),
			"company": company,
			"plant_floor": plant_floor,
			"shift_type": shift_type,
			"custom_aps_run": planning_run,
		},
		"name",
		order_by="modified desc",
	)
	if existing_name:
		doc = frappe.get_doc("Work Order Scheduling", existing_name)
		if (doc.status or "") in FROZEN_SCHEDULING_STATUSES:
			frappe.throw(_("Work Order Scheduling {0} is already frozen by execution.").format(doc.name))
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Work Order Scheduling",
				"posting_date": getdate(posting_date),
				"company": company,
				"plant_floor": plant_floor,
				"shift_type": shift_type,
				"purpose": "Manufacture",
				"status": "",
			}
		)
	doc.custom_aps_run = planning_run
	doc.custom_aps_freeze_state = "Open"
	doc.custom_aps_approval_state = "Approved"
	doc.remarks = "\n".join(part for part in [doc.remarks, _("APS Shift Proposal {0}").format(batch_name)] if part)
	return doc


def _find_matching_scheduling_row(
	work_order_name: str,
	segment: dict[str, Any],
	matched_row_names: set[str] | None = None,
	release_from=None,
	release_to=None,
) -> dict[str, Any] | None:
	matched_row_names = matched_row_names or set()
	rows = [
		row
		for row in _get_formal_scheduling_reconciliation_rows(work_order_name, release_from=release_from, release_to=release_to)
		if row.get("name") not in matched_row_names and not row.get("is_frozen")
	]
	if not rows:
		return None
	target_posting_date = getdate(segment.get("posting_date") or segment.get("start_time")) if segment.get("start_time") else None
	target_shift_type = segment.get("shift_type") or (_determine_shift_type(segment.get("start_time")) if segment.get("start_time") else "")
	target_campaign_key = segment.get("campaign_key") or _build_campaign_key(
		segment.get("primary_item_code"),
		segment.get("mould_reference"),
		segment.get("workstation"),
	)
	target_start = get_datetime(segment.get("start_time")) if segment.get("start_time") else now_datetime()
	same_window_rows = [
		row
		for row in rows
		if row.get("posting_date") == target_posting_date and (row.get("shift_type") or "") == target_shift_type
	]
	if same_window_rows:
		rows = same_window_rows
	elif cint(segment.get("slice_count")) > 1:
		return None
	best_row = None
	best_score = None
	for row in rows:
		row_start = get_datetime(row.get("from_time") or row.get("planned_start_date") or target_start)
		score = (
			0 if row.get("custom_aps_segment_reference") == segment.get("name") else 1,
			0 if target_campaign_key and row.get("campaign_key") == target_campaign_key else 1,
			0 if row.get("workstation") == segment.get("workstation") else 1,
			0 if row.get("posting_date") == target_posting_date and (row.get("shift_type") or "") == target_shift_type else 1,
			abs((row_start - target_start).total_seconds()),
			row.get("name"),
		)
		if best_score is None or score < best_score:
			best_score = score
			best_row = row
	return best_row


def _classify_shift_schedule_action(existing_row: dict[str, Any] | None, segment: dict[str, Any]) -> str:
	if not existing_row:
		return "New"
	target_posting_date = getdate(segment.get("posting_date") or segment.get("start_time")) if segment.get("start_time") else None
	target_shift_type = segment.get("shift_type") or (_determine_shift_type(segment.get("start_time")) if segment.get("start_time") else "")
	target_plant_floor = segment.get("plant_floor")
	target_workstation = segment.get("workstation")
	target_start = get_datetime(segment.get("start_time")) if segment.get("start_time") else None
	target_end = get_datetime(segment.get("end_time")) if segment.get("end_time") else None
	same_target_doc = (
		existing_row.get("posting_date") == target_posting_date
		and (existing_row.get("shift_type") or "") == target_shift_type
		and (existing_row.get("plant_floor") or "") == (target_plant_floor or "")
	)
	same_values = (
		same_target_doc
		and (existing_row.get("workstation") or "") == (target_workstation or "")
		and abs(flt(existing_row.get("scheduling_qty")) - flt(segment.get("planned_qty"))) < 0.0001
		and get_datetime(existing_row.get("planned_start_date") or target_start) == target_start
		and get_datetime(existing_row.get("planned_end_date") or target_end) == target_end
	)
	if same_values:
		return "Keep Existing"
	if same_target_doc:
		return "Update Existing"
	return "Move Existing"


def _determine_shift_type(start_time) -> str:
	start_dt = get_datetime(start_time)
	return "白班" if 8 <= start_dt.hour < 20 else "晚班"


def _get_shift_window_for_time(value) -> tuple[datetime, datetime, str]:
	start_dt = get_datetime(value)
	work_date = getdate(start_dt)
	if 8 <= start_dt.hour < 20:
		shift_start = get_datetime(f"{work_date} 08:00:00")
		shift_end = get_datetime(f"{work_date} 20:00:00")
		return shift_start, shift_end, "白班"
	if start_dt.hour < 8:
		previous_date = getdate(add_days(work_date, -1))
		shift_start = get_datetime(f"{previous_date} 20:00:00")
		shift_end = get_datetime(f"{work_date} 08:00:00")
		return shift_start, shift_end, "晚班"
	next_date = getdate(add_days(work_date, 1))
	shift_start = get_datetime(f"{work_date} 20:00:00")
	shift_end = get_datetime(f"{next_date} 08:00:00")
	return shift_start, shift_end, "晚班"


def _get_shift_scheduling_qty_precision() -> int:
	"""Use the persisted target field precision, preserving an explicit zero."""
	precision = frappe.get_precision("Scheduling Item", "scheduling_qty")
	if precision is None:
		precision = frappe.get_precision("APS Shift Schedule Proposal Item", "planned_qty")
	return max(cint(6 if precision is None else precision), 0)


def _decimal_quantity(value) -> Decimal:
	try:
		return Decimal(str(value if value not in (None, "") else 0))
	except (InvalidOperation, TypeError, ValueError):
		frappe.throw(_("Shift scheduling quantity is not a valid number."), frappe.ValidationError)


def _allocate_shift_slice_quantities(
	segment: dict[str, Any],
	windows: list[tuple[datetime, datetime, datetime, str]],
	*,
	start_time,
	end_time,
) -> list[float]:
	"""Allocate representable quantities and let the last slice absorb drift."""
	precision = _get_shift_scheduling_qty_precision()
	item_code = segment.get("primary_item_code") or segment.get("item_code")
	if _item_quantity_requires_integer(item_code):
		precision = 0
	quantum = Decimal(1).scaleb(-precision)
	planned_qty = _decimal_quantity(segment.get("planned_qty"))
	normalized_qty = planned_qty.quantize(quantum, rounding=ROUND_HALF_UP)
	# Permit only sub-storage-noise differences.  A quantity that the formal
	# target cannot represent must be corrected upstream, never rounded silently.
	tolerance = quantum * Decimal("0.000001")
	if normalized_qty <= 0 or abs(planned_qty - normalized_qty) > tolerance:
		frappe.throw(
			_(
				"Segment {0} quantity {1} cannot be represented by formal shift scheduling at precision {2}."
			).format(segment.get("name") or "-", planned_qty, precision),
			frappe.ValidationError,
		)

	total_seconds = _decimal_quantity(max((end_time - start_time).total_seconds(), 1))
	remaining = normalized_qty
	allocated: list[Decimal] = []
	for index, (slice_start, slice_end, _shift_start, _shift_type) in enumerate(windows):
		if index == len(windows) - 1:
			quantity = remaining
		else:
			slice_seconds = _decimal_quantity(max((slice_end - slice_start).total_seconds(), 0))
			raw_quantity = normalized_qty * slice_seconds / total_seconds
			# Truncate persisted precision on preceding slices; the final slice
			# intentionally absorbs the complete representable remainder.
			quantity = raw_quantity.quantize(quantum, rounding=ROUND_DOWN)
			quantity = min(max(quantity, Decimal(0)), remaining)
		remaining -= quantity
		allocated.append(quantity)
	if remaining != 0:
		# Defensive guard: by construction the last slice consumes the exact
		# remainder, so reaching this branch indicates an arithmetic regression.
		frappe.throw(_("Shift slice quantities do not conserve the segment total."), frappe.ValidationError)
	return [float(quantity) for quantity in allocated]


def _split_segment_into_shift_slices(segment: dict[str, Any], release_from=None, release_to=None) -> list[dict[str, Any]]:
	start_time = get_datetime(segment.get("start_time")) if segment.get("start_time") else None
	end_time = get_datetime(segment.get("end_time")) if segment.get("end_time") else None
	planned_qty = flt(segment.get("planned_qty"))
	if not start_time or not end_time or end_time <= start_time or planned_qty <= 0:
		return [segment]

	windows: list[tuple[datetime, datetime, datetime, str]] = []
	current = start_time
	while current < end_time:
		shift_start, shift_end, shift_type = _get_shift_window_for_time(current)
		slice_end = min(end_time, shift_end)
		if slice_end <= current:
			current = min(end_time, current + timedelta(hours=1))
			continue
		windows.append((current, slice_end, shift_start, shift_type))
		current = slice_end

	if not windows:
		return []

	release_from_date = getdate(release_from) if release_from else None
	release_to_date = getdate(release_to) if release_to else None
	allocated_quantities = _allocate_shift_slice_quantities(
		segment,
		windows,
		start_time=start_time,
		end_time=end_time,
	)
	slices = []
	for index, (slice_start, slice_end, shift_start, shift_type) in enumerate(windows, start=1):
		posting_date = getdate(shift_start)
		slice_qty = allocated_quantities[index - 1] if index - 1 < len(allocated_quantities) else 0
		if slice_qty <= 0:
			continue
		if release_from_date and posting_date < release_from_date:
			continue
		if release_to_date and posting_date > release_to_date:
			continue
		slice_row = dict(segment)
		slice_row.update(
			{
				"start_time": slice_start,
				"end_time": slice_end,
				"planned_qty": slice_qty,
				"posting_date": posting_date,
				"shift_type": shift_type,
				"slice_index": index,
				"slice_count": len(windows),
			}
		)
		slices.append(slice_row)
	return slices


def _upsert_formal_shift_scheduling(batch, row) -> dict[str, Any]:
	if not frappe.db.exists("DocType", "Work Order Scheduling"):
		frappe.throw(_("Work Order Scheduling is not available in this site."))
	plant_floor = row.plant_floor or batch.plant_floor
	if row.action == "Cancel Existing":
		if not row.existing_scheduling or not row.existing_scheduling_item:
			return {"docname": None, "scheduling_item": None, "message": _("No existing formal scheduling row was found to cancel.")}
		doc = frappe.get_doc("Work Order Scheduling", row.existing_scheduling)
		if (doc.status or "") in FROZEN_SCHEDULING_STATUSES:
			_ensure_open_exception(
				planning_run=batch.planning_run,
				severity="Critical",
				exception_type="Shift Scheduling Frozen",
				message=_("Work Order Scheduling {0} is already in execution status {1}.").format(doc.name, doc.status),
				item_code=row.item_code,
				workstation=row.workstation,
				source_doctype="APS Shift Schedule Proposal Batch",
				source_name=batch.name,
				resolution_hint=_("Create residual replan instead of overwriting frozen shift scheduling."),
				is_blocking=1,
			)
			frappe.throw(_("Work Order Scheduling {0} is already frozen by execution.").format(doc.name))
		_remove_scheduling_item_from_doc(doc, row.existing_scheduling_item)
		doc.custom_aps_run = batch.planning_run
		doc.custom_aps_freeze_state = "Open"
		doc.custom_aps_approval_state = "Approved"
		doc.save(ignore_permissions=True)
		_cleanup_empty_formal_scheduling_doc(doc.name)
		if row.segment_reference and frappe.db.exists("APS Schedule Segment", row.segment_reference):
			frappe.db.set_value(
				"APS Schedule Segment",
				row.segment_reference,
				{
					"linked_work_order_scheduling": None,
					"linked_scheduling_item": None,
				},
			)
		return {
			"docname": doc.name if frappe.db.exists("Work Order Scheduling", doc.name) else None,
			"scheduling_item": None,
			"message": _("Existing formal scheduling row was cancelled."),
		}

	target_doc = _get_or_create_formal_shift_scheduling_doc(
		company=batch.company,
		plant_floor=plant_floor,
		posting_date=row.posting_date,
		shift_type=row.shift_type,
		planning_run=batch.planning_run,
		batch_name=batch.name,
	)
	child_row = None
	source_doc = None
	if row.existing_scheduling and frappe.db.exists("Work Order Scheduling", row.existing_scheduling):
		source_doc = frappe.get_doc("Work Order Scheduling", row.existing_scheduling)
		if (source_doc.status or "") in FROZEN_SCHEDULING_STATUSES:
			_ensure_open_exception(
				planning_run=batch.planning_run,
				severity="Critical",
				exception_type="Shift Scheduling Frozen",
				message=_("Work Order Scheduling {0} is already in execution status {1}.").format(source_doc.name, source_doc.status),
				item_code=row.item_code,
				workstation=row.workstation,
				source_doctype="APS Shift Schedule Proposal Batch",
				source_name=batch.name,
				resolution_hint=_("Create residual replan instead of overwriting frozen shift scheduling."),
				is_blocking=1,
			)
			frappe.throw(_("Work Order Scheduling {0} is already frozen by execution.").format(source_doc.name))
		child_row = next((child for child in source_doc.get("scheduling_items") if child.name == row.existing_scheduling_item), None)

	if child_row and source_doc and source_doc.name != target_doc.name:
		_remove_scheduling_item_from_doc(source_doc, child_row.name)
		source_doc.custom_aps_run = batch.planning_run
		source_doc.custom_aps_freeze_state = "Open"
		source_doc.custom_aps_approval_state = "Approved"
		source_doc.save(ignore_permissions=True)
		_cleanup_empty_formal_scheduling_doc(source_doc.name)
		child_row = None

	if not child_row and row.existing_scheduling_item:
		child_row = next((child for child in target_doc.get("scheduling_items") if child.name == row.existing_scheduling_item), None)
	if not child_row:
		target_start = get_datetime(row.planned_start_time) if row.planned_start_time else None
		target_end = get_datetime(row.planned_end_time) if row.planned_end_time else None
		child_row = next(
			(
				child
				for child in target_doc.get("scheduling_items")
				if child.get("work_order") == row.work_order
				and child.get("custom_aps_segment_reference") == row.segment_reference
				and (
					not target_start
					or get_datetime(child.get("planned_start_date") or target_start) == target_start
				)
				and (
					not target_end
					or get_datetime(child.get("planned_end_date") or target_end) == target_end
				)
			),
			None,
		)

	if not child_row:
		child_row = target_doc.append(
			"scheduling_items",
			{
				"work_order": row.work_order,
				"scheduling_qty": row.planned_qty,
				"workstation": row.workstation,
				"planned_start_date": row.planned_start_time,
				"planned_end_date": row.planned_end_time,
				"remarks": row.result_reference,
				"custom_aps_run": batch.planning_run,
				"custom_aps_result_reference": row.result_reference,
				"custom_aps_segment_reference": row.segment_reference,
				"custom_aps_shift_proposal": batch.name,
			},
		)
	else:
		child_row.work_order = row.work_order
		child_row.scheduling_qty = row.planned_qty
		child_row.workstation = row.workstation
		child_row.planned_start_date = row.planned_start_time
		child_row.planned_end_date = row.planned_end_time
		child_row.remarks = row.result_reference
		child_row.custom_aps_run = batch.planning_run
		child_row.custom_aps_result_reference = row.result_reference
		child_row.custom_aps_segment_reference = row.segment_reference
		child_row.custom_aps_shift_proposal = batch.name

	target_doc.save(ignore_permissions=True)
	if row.segment_reference and frappe.db.exists("APS Schedule Segment", row.segment_reference):
		frappe.db.set_value(
			"APS Schedule Segment",
			row.segment_reference,
			{
				"linked_work_order": row.work_order,
				"linked_work_order_scheduling": target_doc.name,
				"linked_scheduling_item": child_row.name,
			},
		)
	message = _("Formal scheduling kept in place.") if row.action == "Keep Existing" else _("Formal scheduling reconciled to {0}.").format(target_doc.name)
	return {"docname": target_doc.name, "scheduling_item": child_row.name, "message": message}


def _build_segment_execution_snapshot(segment, scheduling_item=None, work_order=None) -> dict[str, Any]:
	scheduling_items = []
	if isinstance(scheduling_item, (list, tuple)):
		scheduling_items = [row for row in scheduling_item if row]
	elif scheduling_item:
		scheduling_items = [scheduling_item]
	snapshot = {
		"linked_work_order": segment.get("linked_work_order") or None,
		"linked_work_order_scheduling": segment.get("linked_work_order_scheduling") or None,
		"linked_scheduling_item": segment.get("linked_scheduling_item") or None,
		"actual_completed_qty": 0.0,
		"actual_start_time": None,
		"actual_end_time": None,
		"delay_minutes": 0.0,
		"actual_status": "Not Started",
	}
	if scheduling_items:
		ordered_items = sorted(
			scheduling_items,
			key=lambda row: (
				get_datetime(row.get("from_time") or row.get("planned_start_date") or row.get("modified") or now_datetime()),
				row.get("name") or "",
			),
		)
		latest_item = ordered_items[-1]
		snapshot["linked_scheduling_item"] = latest_item.get("name")
		snapshot["linked_work_order"] = latest_item.get("work_order")
		snapshot["linked_work_order_scheduling"] = latest_item.get("parent")
		snapshot["actual_completed_qty"] = sum(flt(row.get("completed_qty")) for row in ordered_items)
		start_times = [row.get("from_time") for row in ordered_items if row.get("from_time")]
		end_times = [row.get("to_time") for row in ordered_items if row.get("to_time")]
		snapshot["actual_start_time"] = min(start_times) if start_times else None
		snapshot["actual_end_time"] = max(end_times) if end_times else None

	if not scheduling_items and work_order:
		snapshot["actual_completed_qty"] = flt(work_order.get("produced_qty"))

	now_value = now_datetime()
	planned_qty = flt(segment.get("planned_qty"))
	start_time = get_datetime(segment.get("start_time")) if segment.get("start_time") else now_value
	end_time = get_datetime(segment.get("end_time")) if segment.get("end_time") else start_time
	actual_qty = flt(snapshot["actual_completed_qty"])
	if actual_qty > planned_qty * 1.02:
		snapshot["actual_status"] = "Overproduced"
	elif actual_qty >= planned_qty and planned_qty > 0:
		snapshot["actual_status"] = "Completed"
	elif snapshot["actual_start_time"] or actual_qty > 0:
		snapshot["actual_status"] = "Running"
		elapsed_minutes = max((now_value - start_time).total_seconds() / 60, 0)
		total_minutes = max((end_time - start_time).total_seconds() / 60, 1)
		elapsed_ratio = elapsed_minutes / total_minutes
		completed_ratio = actual_qty / planned_qty if planned_qty else 0
		if now_value > end_time and actual_qty < planned_qty:
			snapshot["actual_status"] = "Delayed"
		elif elapsed_ratio > 0.4 and completed_ratio + 0.15 < elapsed_ratio:
			snapshot["actual_status"] = "Slow Progress"
	elif now_value > end_time:
		snapshot["actual_status"] = "No Recent Update"
	elif work_order and flt(work_order.get("produced_qty")) > 0:
		snapshot["actual_status"] = "Running"

	if snapshot["actual_status"] in ("Delayed", "No Recent Update", "Slow Progress"):
		snapshot["delay_minutes"] = max((now_value - end_time).total_seconds() / 60, 0)
	return snapshot


def _get_segment_execution_snapshot(segment) -> dict[str, Any]:
	scheduling_items = []
	has_scheduling_item = frappe.db.exists("DocType", "Scheduling Item")
	if has_scheduling_item and segment.get("linked_scheduling_item") and frappe.db.exists(
		"Scheduling Item", segment.get("linked_scheduling_item")
	):
		scheduling_items.append(frappe.get_doc("Scheduling Item", segment.get("linked_scheduling_item")))
	if has_scheduling_item and frappe.db.has_column("Scheduling Item", "custom_aps_segment_reference"):
		rows = frappe.get_all(
			"Scheduling Item",
			filters={"custom_aps_segment_reference": segment.get("name")},
			fields=["name"],
			order_by="planned_start_date asc, modified asc",
		)
		known_names = {row.name for row in scheduling_items}
		for row in rows:
			if row.name not in known_names:
				scheduling_items.append(frappe.get_doc("Scheduling Item", row.name))

	work_order = None
	linked_work_order = (
		next((row.get("work_order") for row in scheduling_items if row.get("work_order")), None)
		or segment.get("linked_work_order")
	)
	if linked_work_order and frappe.db.exists("Work Order", linked_work_order):
		work_order = frappe.get_doc("Work Order", linked_work_order)
	return _build_segment_execution_snapshot(segment, scheduling_items, work_order)


def _rollup_result_actual_status(segment_statuses: list[str]) -> str:
	if not segment_statuses:
		return "Not Started"
	priority = [
		"Overproduced",
		"Delayed",
		"Slow Progress",
		"No Recent Update",
		"Running",
		"Completed",
		"Not Started",
	]
	for status in priority:
		if status in segment_statuses:
			return status
	return segment_statuses[0]


def _rollup_delay_minutes(segments) -> float:
	return max((flt(segment.delay_minutes) for segment in segments), default=0.0)


def _sync_execution_exceptions(run_name: str, result_doc):
	execution_types = (
		"Slow Progress",
		"Delayed Execution",
		"No Recent Update",
		"Actual Output Mismatch",
	)
	for name in frappe.get_all(
		"APS Exception Log",
		filters={
			"planning_run": run_name,
			"source_doctype": "APS Schedule Result",
			"source_name": result_doc.name,
			"exception_type": ("in", execution_types),
			"status": "Open",
		},
		pluck="name",
	):
		frappe.db.set_value("APS Exception Log", name, "status", "Closed")

	status_map = {
		"Slow Progress": "Slow Progress",
		"Delayed": "Delayed Execution",
		"No Recent Update": "No Recent Update",
		"Overproduced": "Actual Output Mismatch",
	}
	exception_type = status_map.get(result_doc.actual_status)
	if not exception_type:
		return
	_ensure_open_exception(
		planning_run=run_name,
		severity="Critical" if result_doc.actual_status in ("Delayed", "Overproduced") else "Warning",
		exception_type=exception_type,
		message=_("Execution status for result {0} is {1}.").format(result_doc.name, result_doc.actual_status),
		item_code=result_doc.item_code,
		customer=result_doc.customer,
		source_doctype="APS Schedule Result",
		source_name=result_doc.name,
		resolution_hint=_("Generate a manual replan suggestion if the formal shift schedule needs to change."),
		is_blocking=1 if result_doc.actual_status == "Delayed" else 0,
	)


def _count_today_manufacture_entries(run_name: str) -> int:
	if not frappe.db.exists("DocType", "Stock Entry"):
		return 0
	rows = frappe.db.sql(
		"""
		select count(distinct se.name) as entry_count
		from `tabStock Entry` se
		inner join `tabWork Order` wo on wo.name = se.work_order
		where se.docstatus = 1
			and (
				ifnull(se.stock_entry_type, '') = 'Manufacture'
				or ifnull(se.purpose, '') = 'Manufacture'
			)
			and se.posting_date = %s
			and ifnull(wo.custom_aps_run, '') = %s
		""",
		[today(), run_name],
		as_dict=True,
	)
	return cint(rows[0].entry_count) if rows else 0


def _get_latest_stock_entry_by_work_order(work_order_names: list[str]) -> dict[str, dict[str, Any]]:
	work_order_names = [name for name in work_order_names if name]
	if not work_order_names or not frappe.db.exists("DocType", "Stock Entry"):
		return {}
	rows = frappe.db.sql(
		"""
		select
			se.name,
			se.work_order,
			se.posting_date,
			se.posting_time
		from `tabStock Entry` se
		where se.docstatus = 1
			and se.work_order in ({0})
			and (
				ifnull(se.stock_entry_type, '') = 'Manufacture'
				or ifnull(se.purpose, '') = 'Manufacture'
			)
		order by se.posting_date desc, se.posting_time desc, se.modified desc
		""".format(", ".join(["%s"] * len(work_order_names))),
		work_order_names,
		as_dict=True,
	)
	latest = {}
	for row in rows:
		latest.setdefault(row.work_order, row)
	return latest


def _sync_delivery_plan(run_doc) -> str | None:
	if not frappe.db.exists("DocType", "Delivery Plan"):
		return None

	result_rows = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_doc.name, "machine_scheduled_qty": (">", 0)},
		fields=["customer", "item_code", "requested_date", "machine_scheduled_qty"],
		order_by="requested_date asc, item_code asc",
	)
	if not result_rows:
		return None

	customer = next((row.customer for row in result_rows if row.customer), None)
	if not customer:
		return None

	dp = frappe.get_doc(
		{
			"doctype": "Delivery Plan",
			"customer": customer,
			"company": run_doc.company,
			"delivery_date": getdate(run_doc.horizon_start),
			"arrival_date": getdate(run_doc.horizon_start),
			"remark": _("Generated by Injection APS run {0}").format(run_doc.name),
			"custom_aps_version": run_doc.name,
			"custom_aps_source": "APS Planning Run",
			"item_qties": [
				{
					"item_code": row.item_code,
					"planned_delivery_qty": row.machine_scheduled_qty,
					"staging_qty": row.machine_scheduled_qty,
					"required_arrival_date": row.requested_date,
				}
				for row in result_rows
			],
		}
	).insert(ignore_permissions=True)
	return dp.name


def _create_release_work_order_scheduling(run_doc, release_batch: str, scheduling_items: list[dict[str, Any]]) -> str | None:
	if not scheduling_items or not frappe.db.exists("DocType", "Work Order Scheduling"):
		return None
	doc = frappe.get_doc(
		{
			"doctype": "Work Order Scheduling",
			"posting_date": today(),
			"company": run_doc.company,
			"plant_floor": run_doc.plant_floor,
			"purpose": "Manufacture",
			"status": "",
			"remarks": _("APS Release Batch {0}").format(release_batch),
			"custom_aps_run": run_doc.name,
			"custom_aps_freeze_state": "Locked",
			"custom_aps_approval_state": "Approved",
			"scheduling_items": scheduling_items,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _extract_tonnage_from_name(workstation_name: str | None) -> float:
	if not workstation_name:
		return 0

	name = str(workstation_name)
	patterns = [
		r"(\d+(?:\.\d+)?)\s*[Tt]\b",
		r"(\d+(?:\.\d+)?)\s*吨",
	]
	for pattern in patterns:
		matches = re.findall(pattern, name)
		if matches:
			return flt(matches[-1])
	digits = []
	for token in name.replace("/", " ").replace("_", " ").split():
		filtered = "".join(ch for ch in token if ch.isdigit())
		if filtered:
			digits.append(filtered)
	if not digits:
		return 0
	return flt(max(digits, key=len))


def _get_records_with_any_field_set(
	doctype: str,
	fieldnames: list[str],
	*,
	company: str | None = None,
	company_parent_doctype: str | None = None,
) -> list[str]:
	meta = frappe.get_meta(doctype)
	available = [fieldname for fieldname in fieldnames if meta.has_field(fieldname)]
	if not available:
		return []

	conditions = []
	for fieldname in available:
		field = meta.get_field(fieldname)
		if field.fieldtype in ("Check", "Int", "Float", "Currency", "Percent"):
			conditions.append(f"ifnull(source.`{fieldname}`, 0) != 0")
		else:
			conditions.append(f"ifnull(source.`{fieldname}`, '') != ''")

	params = []
	join = ""
	company_condition = ""
	if company:
		params.append(company)
		if company_parent_doctype:
			join = (
				f" inner join `tab{company_parent_doctype}` company_parent"
				" on company_parent.name = source.parent"
			)
			company_condition = " and company_parent.company = %s"
		elif meta.has_field("company"):
			company_condition = " and source.company = %s"
		else:
			frappe.throw(
				_(
					"Company scoping is unavailable for {0}; cleanup is blocked.",
					context="Injection APS",
				).format(doctype),
				frappe.ValidationError,
			)

	query = (
		f"select source.name from `tab{doctype}` source{join}"
		f" where ({' or '.join(conditions)}){company_condition}"
	)
	return [row.name for row in frappe.db.sql(query, tuple(params), as_dict=True)]


def _get_doc_field_value(doc, fieldname: str | None):
	if not doc or not fieldname:
		return None
	return doc.get(fieldname) if doc.meta.has_field(fieldname) else None


def _first_valid_warehouse(candidates: list[str | None], company: str | None = None) -> str | None:
	if not frappe.db.exists("DocType", "Warehouse"):
		return None
	for warehouse in candidates:
		if not warehouse or not frappe.db.exists("Warehouse", warehouse):
			continue
		warehouse_company = frappe.db.get_value("Warehouse", warehouse, "company")
		if not company or warehouse_company in (None, "", company):
			return warehouse
	return None


def _get_work_order_warehouse_values(
	plant_floor_doc,
	settings: dict[str, Any],
	item_code: str | None,
	company: str | None,
) -> dict[str, str | None]:
	item_default_warehouse = None
	if item_code and frappe.db.exists("DocType", "Item"):
		item_meta = frappe.get_meta("Item")
		if item_meta.has_field("default_warehouse"):
			item_default_warehouse = frappe.db.get_value("Item", item_code, "default_warehouse")
	wip_warehouse = _first_valid_warehouse(
		[
			_get_doc_field_value(plant_floor_doc, settings.get("plant_floor_wip_warehouse_field")),
			item_default_warehouse,
		],
		company=company,
	)
	source_warehouse = _first_valid_warehouse(
		[
			_get_doc_field_value(plant_floor_doc, settings.get("plant_floor_source_warehouse_field")),
			wip_warehouse,
			item_default_warehouse,
		],
		company=company,
	)
	fg_warehouse = _first_valid_warehouse(
		[
			_get_doc_field_value(plant_floor_doc, settings.get("plant_floor_fg_warehouse_field")),
			item_default_warehouse,
			wip_warehouse,
		],
		company=company,
	)
	scrap_warehouse = _first_valid_warehouse(
		[
			_get_doc_field_value(plant_floor_doc, settings.get("plant_floor_scrap_warehouse_field")),
		],
		company=company,
	)
	values = {
		"wip_warehouse": wip_warehouse,
		"source_warehouse": source_warehouse,
		"fg_warehouse": fg_warehouse,
		"scrap_warehouse": scrap_warehouse,
	}
	work_order_meta = frappe.get_meta("Work Order")
	missing = [
		(work_order_meta.get_field(fieldname).label or fieldname)
		for fieldname, value in values.items()
		if work_order_meta.has_field(fieldname)
		and cint(work_order_meta.get_field(fieldname).reqd)
		and not value
	]
	if missing:
		frappe.throw(
			_("Maintain {0} on Plant Floor or Item before creating Work Orders from APS.").format(
				", ".join(missing)
			)
		)
	return values


def _delete_system_generated_rows(doctype: str, company: str | None = None):
	if not frappe.db.exists("DocType", doctype):
		return
	filters = {"is_system_generated": 1}
	if company and frappe.get_meta(doctype).has_field("company"):
		filters["company"] = company
	names = frappe.get_all(doctype, filters=filters, pluck="name")
	if doctype == "APS Net Requirement" and names and frappe.db.exists("DocType", "APS Schedule Result"):
		frappe.db.sql(
			"""
			update `tabAPS Schedule Result`
			set net_requirement = ''
			where net_requirement in %s
			""",
			[tuple(names)],
		)
	for name in names:
		frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)


def _strip_none(values: dict[str, Any]) -> dict[str, Any]:
	return {key: value for key, value in values.items() if value not in (None, "")}
import re
