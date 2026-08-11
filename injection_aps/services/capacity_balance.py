from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


PRODUCTION_STRATEGIES = ("Auto Balance", "Force Prebuild", "Force JIT")
CAPACITY_TOLERANCE = 0.0001
DEFAULT_SHIFT_HOURS = 12


def normalize_production_strategy(value: str | None, default: str = "Auto Balance") -> str:
	strategy = (value or default or "Auto Balance").strip()
	if strategy not in PRODUCTION_STRATEGIES:
		raise frappe.ValidationError(
			_("Production strategy must be one of: {0}.").format(", ".join(PRODUCTION_STRATEGIES))
		)
	return strategy


def iter_shift_windows(horizon_start, horizon_end) -> list[dict[str, Any]]:
	start = get_datetime(horizon_start)
	end = get_datetime(horizon_end)
	if end <= start:
		return []
	if 8 <= start.hour < 20:
		cursor = get_datetime(f"{getdate(start)} 08:00:00")
	elif start.hour < 8:
		cursor = get_datetime(f"{getdate(start - timedelta(days=1))} 20:00:00")
	else:
		cursor = get_datetime(f"{getdate(start)} 20:00:00")
	windows = []
	while cursor < end:
		window_end = cursor + timedelta(hours=DEFAULT_SHIFT_HOURS)
		visible_start = max(cursor, start)
		visible_end = min(window_end, end)
		if visible_end > visible_start:
			windows.append(
				{
					"start": visible_start,
					"end": visible_end,
					"posting_date": getdate(cursor),
					"shift_type": "白班" if cursor.hour == 8 else "晚班",
				}
			)
		cursor = window_end
	return windows


def build_capacity_buckets(
	workstations: list[str],
	horizon_start,
	horizon_end,
	blocked_intervals: dict[str, list[tuple[Any, Any]]] | None = None,
	downtime_windows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
	blocked_intervals = blocked_intervals or {}
	downtime_windows = downtime_windows or []
	buckets = []
	for workstation in sorted({row for row in workstations if row}):
		for window in iter_shift_windows(horizon_start, horizon_end):
			start = window["start"]
			end = window["end"]
			free_intervals = [(start, end)]
			for blocked_start, blocked_end in blocked_intervals.get(workstation) or []:
				free_intervals = _subtract_from_intervals(free_intervals, blocked_start, blocked_end)
			capacity_budget = _minutes_between(start, end)
			for downtime in downtime_windows:
				if downtime.get("workstation") and downtime.get("workstation") != workstation:
					continue
				overlap = _overlap_minutes(start, end, downtime.get("start_time"), downtime.get("end_time"))
				if overlap <= 0:
					continue
				factor = max(min(flt(downtime.get("available_capacity_percent")) / 100, 1), 0)
				capacity_budget -= overlap * (1 - factor)
				if factor <= CAPACITY_TOLERANCE:
					free_intervals = _subtract_from_intervals(
						free_intervals,
						downtime.get("start_time"),
						downtime.get("end_time"),
					)
			free_minutes = _interval_minutes(free_intervals)
			available_minutes = max(min(capacity_budget, free_minutes), 0)
			buckets.append(
				{
					"key": f"{workstation}|{start.isoformat()}",
					"workstation": workstation,
					**window,
					"free_intervals": free_intervals,
					"initial_available_minutes": available_minutes,
					"remaining_budget_minutes": available_minutes,
					"initial_occupied_minutes": max(_minutes_between(start, end) - free_minutes, 0),
				}
			)
	return buckets


def balance_capacity_nodes(
	demands: list[dict[str, Any]],
	buckets: list[dict[str, Any]],
	*,
	high_cancellation_risk_percent: float = 60,
	mold_blocked_intervals: dict[str, list[tuple[Any, Any]]] | None = None,
) -> dict[str, Any]:
	working_buckets = [_copy_bucket(row) for row in buckets]
	buckets_by_workstation = defaultdict(list)
	for bucket in working_buckets:
		buckets_by_workstation[bucket.get("workstation")].append(bucket)
	for rows in buckets_by_workstation.values():
		rows.sort(key=lambda row: get_datetime(row.get("start")))

	mold_free = _build_mold_free_intervals(
		demands,
		working_buckets,
		mold_blocked_intervals=mold_blocked_intervals or {},
	)
	results = []
	ordered_demands = sorted(
		(demand for demand in demands if flt(demand.get("qty")) > CAPACITY_TOLERANCE),
		key=lambda row: (
			get_datetime(row.get("due_time")),
			-cint(row.get("priority") or 0),
			str(row.get("key") or ""),
		),
	)
	for demand in ordered_demands:
		results.append(
			_balance_one_demand(
				demand,
				buckets_by_workstation.get(demand.get("workstation")) or [],
				mold_free,
				high_cancellation_risk_percent=high_cancellation_risk_percent,
			)
		)

	bucket_rows = []
	for bucket in sorted(working_buckets, key=lambda row: (row.get("workstation") or "", row.get("start"))):
		available = flt(bucket.get("initial_available_minutes"))
		remaining = max(flt(bucket.get("remaining_budget_minutes")), 0)
		used = max(available - remaining, 0)
		bucket_rows.append(
			{
				"key": bucket.get("key"),
				"workstation": bucket.get("workstation"),
				"start": bucket.get("start"),
				"end": bucket.get("end"),
				"posting_date": bucket.get("posting_date"),
				"shift_type": bucket.get("shift_type"),
				"available_minutes": available,
				"occupied_minutes": used + flt(bucket.get("initial_occupied_minutes")),
				"remaining_minutes": remaining,
				"load_percent": round((used / available * 100) if available else (100 if used else 0), 4),
			}
		)

	summary = {
		"demand_count": len(results),
		"planned_qty": sum(flt(row.get("planned_qty")) for row in results),
		"prebuild_qty": sum(flt(row.get("prebuild_qty")) for row in results),
		"jit_qty": sum(flt(row.get("jit_qty")) for row in results),
		"late_qty": sum(flt(row.get("late_qty")) for row in results),
		"unscheduled_qty": sum(flt(row.get("unscheduled_qty")) for row in results),
		"requires_confirmation": sum(cint(row.get("requires_confirmation")) for row in results),
		"blocked_demands": sum(1 for row in results if row.get("status") == "Blocked"),
		"peak_load_percent": max((flt(row.get("load_percent")) for row in bucket_rows), default=0),
	}
	return {"summary": summary, "demands": results, "buckets": bucket_rows}


def _balance_one_demand(
	demand: dict[str, Any],
	buckets: list[dict[str, Any]],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	*,
	high_cancellation_risk_percent: float,
) -> dict[str, Any]:
	qty = max(flt(demand.get("qty")), 0)
	rate = max(flt(demand.get("hourly_rate")), 0)
	strategy = normalize_production_strategy(demand.get("strategy"))
	due_time = get_datetime(demand.get("due_time"))
	checks = []
	if not buckets or rate <= 0:
		return _blocked_demand_result(demand, qty, strategy, "No usable workstation capacity or production rate.")

	due_index = _find_due_bucket_index(buckets, due_time)
	if due_index is None:
		return _blocked_demand_result(demand, qty, strategy, "Delivery time is outside the available capacity horizon.")
	max_early_days = max(cint(demand.get("max_prebuild_days") or 0), 0)
	shelf_life_days = max(cint(demand.get("shelf_life_days") or 0), 0)
	if shelf_life_days and max_early_days:
		max_early_days = min(max_early_days, shelf_life_days)
	early_cutoff = due_time - timedelta(days=max_early_days) if max_early_days else due_time
	prebuild_indices = [
		idx
		for idx, bucket in enumerate(buckets)
		if idx < due_index and get_datetime(bucket.get("end")) > early_cutoff
	]
	jit_indices = [due_index]
	late_indices = list(range(due_index + 1, len(buckets)))

	prebuild_allowed = bool(cint(demand.get("prebuild_allowed", 1)))
	prebuild_cap, cap_checks = _get_prebuild_cap(demand, qty)
	checks.extend(cap_checks)
	if not prebuild_allowed:
		prebuild_cap = 0
		checks.append(_check("blocked", "prebuild_allowed", "Item or demand policy does not allow Prebuild."))
	if not prebuild_indices:
		prebuild_cap = 0
		checks.append(_check("blocked", "max_prebuild_days", "No earlier capacity bucket is inside the allowed Prebuild window."))

	setup_minutes = max(flt(demand.get("setup_minutes")), 0)
	jit_capacity = _estimate_qty_capacity(
		buckets,
		jit_indices,
		demand,
		mold_free,
		setup_minutes=setup_minutes,
	)
	minimum_batch_qty = max(flt(demand.get("minimum_batch_qty")), 0)
	target_prebuild = 0.0
	if strategy == "Auto Balance":
		target_prebuild = max(qty - jit_capacity, 0)
		if 0 < target_prebuild < minimum_batch_qty:
			target_prebuild = min(minimum_batch_qty, qty)
		target_prebuild = min(target_prebuild, prebuild_cap)
	elif strategy == "Force Prebuild":
		target_prebuild = min(qty, prebuild_cap)

	allocations = []
	remaining_qty = qty
	setup_state = {"remaining": setup_minutes}
	if target_prebuild > CAPACITY_TOLERANCE:
		prebuild_order = prebuild_indices if strategy == "Force Prebuild" else list(reversed(prebuild_indices))
		allocated = _allocate_qty(
			target_prebuild,
			rate,
			buckets,
			prebuild_order,
			demand,
			mold_free,
			mode="Prebuild",
			latest=strategy != "Force Prebuild",
			setup_state=setup_state,
		)
		allocations.extend(allocated)
		remaining_qty -= sum(flt(row.get("qty")) for row in allocated)

	if remaining_qty > CAPACITY_TOLERANCE:
		allocated = _allocate_qty(
			remaining_qty,
			rate,
			buckets,
			jit_indices,
			demand,
			mold_free,
			mode="JIT",
			latest=True,
			setup_state=setup_state,
		)
		allocations.extend(allocated)
		remaining_qty -= sum(flt(row.get("qty")) for row in allocated)

	if strategy == "Auto Balance" and remaining_qty > CAPACITY_TOLERANCE and prebuild_cap > target_prebuild:
		additional_prebuild = min(remaining_qty, prebuild_cap - target_prebuild)
		allocated = _allocate_qty(
			additional_prebuild,
			rate,
			buckets,
			list(reversed(prebuild_indices)),
			demand,
			mold_free,
			mode="Prebuild",
			latest=True,
			setup_state=setup_state,
		)
		allocations.extend(allocated)
		remaining_qty -= sum(flt(row.get("qty")) for row in allocated)

	if remaining_qty > CAPACITY_TOLERANCE:
		allocated = _allocate_qty(
			remaining_qty,
			rate,
			buckets,
			late_indices,
			demand,
			mold_free,
			mode="Late",
			latest=False,
			setup_state=setup_state,
		)
		allocations.extend(allocated)
		remaining_qty -= sum(flt(row.get("qty")) for row in allocated)

	prebuild_qty = sum(flt(row.get("qty")) for row in allocations if row.get("mode") == "Prebuild")
	jit_qty = sum(flt(row.get("qty")) for row in allocations if row.get("mode") == "JIT")
	late_allocated_qty = sum(flt(row.get("qty")) for row in allocations if row.get("mode") == "Late")
	late_qty = max(late_allocated_qty + remaining_qty, 0)
	if prebuild_qty > 0 and minimum_batch_qty and prebuild_qty + CAPACITY_TOLERANCE < minimum_batch_qty:
		checks.append(
			_check(
				"blocked",
				"minimum_batch_qty",
				f"Prebuild quantity {prebuild_qty} is below minimum batch {minimum_batch_qty}.",
			)
		)
	if prebuild_qty <= CAPACITY_TOLERANCE and qty <= jit_capacity + CAPACITY_TOLERANCE:
		checks.append(_check("passed", "necessary_prebuild_only", "JIT capacity is sufficient; no Prebuild was created."))
	elif prebuild_qty > 0:
		checks.append(_check("passed", "necessary_prebuild_only", "Only the quantity needed outside the due bucket was assigned to Prebuild."))

	requires_confirmation_reasons = []
	if prebuild_qty > 0 and (demand.get("demand_confidence") or "Confirmed") == "Forecast":
		requires_confirmation_reasons.append("Forecast demand")
	if prebuild_qty > 0 and flt(demand.get("cancellation_risk_percent")) >= flt(high_cancellation_risk_percent):
		requires_confirmation_reasons.append("High cancellation risk")
	if prebuild_qty > 0 and cint(demand.get("overstock_risk")):
		requires_confirmation_reasons.append("Overstock risk")
	if prebuild_qty > 0 and demand.get("material_ready_qty") is None:
		requires_confirmation_reasons.append("Material readiness not proven")
	requires_confirmation = bool(requires_confirmation_reasons)
	if requires_confirmation:
		checks.append(_check("warning", "pmc_confirmation", ", ".join(requires_confirmation_reasons)))

	earliest_prebuild = min(
		(get_datetime(row.get("end")) for row in allocations if row.get("mode") == "Prebuild"),
		default=None,
	)
	early_days = max((due_time - earliest_prebuild).total_seconds() / 86400, 0) if earliest_prebuild else 0
	status = "Balanced"
	if remaining_qty > CAPACITY_TOLERANCE or any(row.get("status") == "blocked" for row in checks if row.get("key") != "prebuild_allowed"):
		status = "Blocked" if remaining_qty > CAPACITY_TOLERANCE else "Balanced"
	elif requires_confirmation:
		status = "Confirmation Required"
	return {
		"key": demand.get("key"),
		"result": demand.get("result"),
		"segment": demand.get("segment"),
		"workstation": demand.get("workstation"),
		"mould_reference": demand.get("mould_reference"),
		"strategy": strategy,
		"planned_qty": qty,
		"prebuild_qty": prebuild_qty,
		"jit_qty": jit_qty,
		"late_qty": late_qty,
		"unscheduled_qty": max(remaining_qty, 0),
		"early_days": round(early_days, 4),
		"projected_peak_inventory_qty": flt(demand.get("current_inventory_qty")) + prebuild_qty,
		"late_qty_before_balance": max(flt(demand.get("late_qty_before_balance")), 0),
		"late_qty_after_balance": late_qty,
		"requires_confirmation": cint(requires_confirmation),
		"confirmation_reasons": requires_confirmation_reasons,
		"status": status,
		"checks": checks,
		"allocations": sorted(allocations, key=lambda row: (get_datetime(row.get("start")), row.get("bucket_key") or "")),
	}


def _allocate_qty(
	qty: float,
	hourly_rate: float,
	buckets: list[dict[str, Any]],
	indices: list[int],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	*,
	mode: str,
	latest: bool,
	setup_state: dict[str, float],
) -> list[dict[str, Any]]:
	remaining_qty = max(flt(qty), 0)
	allocations = []
	for idx in indices:
		if remaining_qty <= CAPACITY_TOLERANCE:
			break
		bucket = buckets[idx]
		while remaining_qty > CAPACITY_TOLERANCE and flt(bucket.get("remaining_budget_minutes")) > CAPACITY_TOLERANCE:
			common = _get_common_free_intervals(bucket, demand, mold_free)
			if not common:
				break
			interval = common[-1] if latest else common[0]
			budget = flt(bucket.get("remaining_budget_minutes"))
			interval_minutes = _minutes_between(*interval)
			setup_minutes = min(flt(setup_state.get("remaining")), interval_minutes, budget)
			production_minutes_available = min(interval_minutes, budget) - setup_minutes
			if production_minutes_available <= CAPACITY_TOLERANCE:
				break
			required_minutes = remaining_qty / hourly_rate * 60
			production_minutes = min(required_minutes, production_minutes_available)
			occupied_minutes = setup_minutes + production_minutes
			if latest:
				occupied_end = interval[1]
				occupied_start = occupied_end - timedelta(minutes=occupied_minutes)
				production_start = occupied_start + timedelta(minutes=setup_minutes)
				production_end = occupied_end
			else:
				occupied_start = interval[0]
				production_start = occupied_start + timedelta(minutes=setup_minutes)
				production_end = production_start + timedelta(minutes=production_minutes)
				occupied_end = production_end
			allocated_qty = min(remaining_qty, production_minutes * hourly_rate / 60)
			_remove_occupied_interval(bucket, demand, mold_free, occupied_start, occupied_end)
			bucket["remaining_budget_minutes"] = max(budget - occupied_minutes, 0)
			setup_state["remaining"] = max(flt(setup_state.get("remaining")) - setup_minutes, 0)
			remaining_qty -= allocated_qty
			available_qty = flt(bucket.get("initial_available_minutes")) * hourly_rate / 60
			remaining_capacity_qty = flt(bucket.get("remaining_budget_minutes")) * hourly_rate / 60
			occupied_capacity_qty = max(available_qty - remaining_capacity_qty, 0)
			allocations.append(
				{
					"bucket_key": bucket.get("key"),
					"bucket_start": bucket.get("start"),
					"bucket_end": bucket.get("end"),
					"posting_date": bucket.get("posting_date"),
					"shift_type": bucket.get("shift_type"),
					"mode": mode,
					"start": production_start,
					"end": production_end,
					"qty": allocated_qty,
					"setup_minutes": setup_minutes,
					"available_capacity_qty": available_qty,
					"occupied_capacity_qty": occupied_capacity_qty,
					"remaining_capacity_qty": remaining_capacity_qty,
					"load_percent": round((occupied_capacity_qty / available_qty * 100) if available_qty else 0, 4),
				}
			)
	return allocations


def _estimate_qty_capacity(
	buckets: list[dict[str, Any]],
	indices: list[int],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	*,
	setup_minutes: float,
) -> float:
	minutes = 0.0
	for idx in indices:
		bucket = buckets[idx]
		common_minutes = _interval_minutes(_get_common_free_intervals(bucket, demand, mold_free))
		minutes += min(common_minutes, flt(bucket.get("remaining_budget_minutes")))
	minutes = max(minutes - max(setup_minutes, 0), 0)
	return minutes * max(flt(demand.get("hourly_rate")), 0) / 60


def _get_prebuild_cap(demand: dict[str, Any], qty: float) -> tuple[float, list[dict[str, str]]]:
	cap = qty
	checks = []
	for key, label in (
		("inventory_room_qty", "item_inventory_limit"),
		("warehouse_room_qty", "warehouse_capacity"),
		("material_ready_qty", "material_readiness"),
	):
		value = demand.get(key)
		if value is None:
			checks.append(_check("warning", label, f"{label.replace('_', ' ').title()} is not configured or proven."))
			continue
		value = max(flt(value), 0)
		cap = min(cap, value)
		checks.append(_check("passed" if value > 0 else "blocked", label, f"Prebuild limit is {value}."))
	return max(cap, 0), checks


def _find_due_bucket_index(buckets: list[dict[str, Any]], due_time: datetime) -> int | None:
	for idx, bucket in enumerate(buckets):
		if get_datetime(bucket.get("start")) < due_time <= get_datetime(bucket.get("end")):
			return idx
	eligible = [idx for idx, bucket in enumerate(buckets) if get_datetime(bucket.get("end")) <= due_time]
	return eligible[-1] if eligible else None


def _build_mold_free_intervals(
	demands: list[dict[str, Any]],
	buckets: list[dict[str, Any]],
	*,
	mold_blocked_intervals: dict[str, list[tuple[Any, Any]]],
) -> dict[tuple[str, str], list[tuple[datetime, datetime]]]:
	molds = {row.get("mould_reference") for row in demands if row.get("mould_reference")}
	result = {}
	for mold in molds:
		for bucket in buckets:
			intervals = [(get_datetime(bucket.get("start")), get_datetime(bucket.get("end")))]
			for blocked_start, blocked_end in mold_blocked_intervals.get(mold) or []:
				intervals = _subtract_from_intervals(intervals, blocked_start, blocked_end)
			result[(mold, bucket.get("key"))] = intervals
	return result


def _get_common_free_intervals(
	bucket: dict[str, Any],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
) -> list[tuple[datetime, datetime]]:
	intervals = list(bucket.get("free_intervals") or [])
	mold = demand.get("mould_reference")
	if not mold:
		return intervals
	return _intersect_interval_lists(intervals, mold_free.get((mold, bucket.get("key"))) or [])


def _remove_occupied_interval(
	bucket: dict[str, Any],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	start,
	end,
):
	bucket["free_intervals"] = _subtract_from_intervals(bucket.get("free_intervals") or [], start, end)
	mold = demand.get("mould_reference")
	if mold:
		key = (mold, bucket.get("key"))
		mold_free[key] = _subtract_from_intervals(mold_free.get(key) or [], start, end)


def _copy_bucket(row: dict[str, Any]) -> dict[str, Any]:
	copy = dict(row)
	copy["start"] = get_datetime(row.get("start"))
	copy["end"] = get_datetime(row.get("end"))
	copy["free_intervals"] = [
		(get_datetime(start), get_datetime(end)) for start, end in (row.get("free_intervals") or [])
	]
	copy["initial_available_minutes"] = flt(row.get("initial_available_minutes"))
	copy["remaining_budget_minutes"] = flt(
		row.get("remaining_budget_minutes")
		if row.get("remaining_budget_minutes") is not None
		else row.get("initial_available_minutes")
	)
	return copy


def _subtract_from_intervals(
	intervals: list[tuple[Any, Any]],
	blocked_start,
	blocked_end,
) -> list[tuple[datetime, datetime]]:
	if not blocked_start or not blocked_end:
		return [(get_datetime(start), get_datetime(end)) for start, end in intervals]
	blocked_start = get_datetime(blocked_start)
	blocked_end = get_datetime(blocked_end)
	if blocked_end <= blocked_start:
		return [(get_datetime(start), get_datetime(end)) for start, end in intervals]
	result = []
	for start, end in intervals:
		start = get_datetime(start)
		end = get_datetime(end)
		if blocked_end <= start or blocked_start >= end:
			result.append((start, end))
			continue
		if blocked_start > start:
			result.append((start, min(blocked_start, end)))
		if blocked_end < end:
			result.append((max(blocked_end, start), end))
	return [(start, end) for start, end in result if end > start]


def _intersect_interval_lists(
	left: list[tuple[Any, Any]],
	right: list[tuple[Any, Any]],
) -> list[tuple[datetime, datetime]]:
	result = []
	for left_start, left_end in left:
		for right_start, right_end in right:
			start = max(get_datetime(left_start), get_datetime(right_start))
			end = min(get_datetime(left_end), get_datetime(right_end))
			if end > start:
				result.append((start, end))
	return sorted(result, key=lambda row: row[0])


def _interval_minutes(intervals: list[tuple[Any, Any]]) -> float:
	return sum(_minutes_between(start, end) for start, end in intervals)


def _minutes_between(start, end) -> float:
	if not start or not end:
		return 0.0
	return max((get_datetime(end) - get_datetime(start)).total_seconds() / 60, 0)


def _overlap_minutes(left_start, left_end, right_start, right_end) -> float:
	if not right_start or not right_end:
		return 0.0
	start = max(get_datetime(left_start), get_datetime(right_start))
	end = min(get_datetime(left_end), get_datetime(right_end))
	return _minutes_between(start, end)


def _check(status: str, key: str, message: str) -> dict[str, str]:
	return {"status": status, "key": key, "message": message}


def _blocked_demand_result(demand: dict[str, Any], qty: float, strategy: str, message: str) -> dict[str, Any]:
	return {
		"key": demand.get("key"),
		"result": demand.get("result"),
		"segment": demand.get("segment"),
		"workstation": demand.get("workstation"),
		"mould_reference": demand.get("mould_reference"),
		"strategy": strategy,
		"planned_qty": qty,
		"prebuild_qty": 0,
		"jit_qty": 0,
		"late_qty": qty,
		"unscheduled_qty": qty,
		"early_days": 0,
		"projected_peak_inventory_qty": flt(demand.get("current_inventory_qty")),
		"late_qty_before_balance": max(flt(demand.get("late_qty_before_balance")), 0),
		"late_qty_after_balance": qty,
		"requires_confirmation": 0,
		"confirmation_reasons": [],
		"status": "Blocked",
		"checks": [_check("blocked", "capacity", message)],
		"allocations": [],
	}


def canonical_json(value: Any) -> str:
	return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
	return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def analyze_capacity_balance(run_name: str, persist: bool = True) -> dict[str, Any]:
	"""Build and optionally persist a shift-level balance proposal for an APS run."""
	from injection_aps.services import planning

	run_doc = frappe.get_doc("APS Planning Run", run_name)
	result_rows, segment_rows = _get_run_balance_rows(run_name)
	if not segment_rows:
		frappe.throw(_("APS run {0} has no active physical schedule segments to balance.").format(run_name))
	workstations = sorted({row.get("workstation") for row in segment_rows if row.get("workstation")})
	fixed_intervals, mold_fixed_intervals = _get_fixed_execution_intervals(run_doc, segment_rows)
	downtime_windows = planning._get_active_downtime_windows(
		company=run_doc.company,
		plant_floors=planning._get_run_selected_plant_floors(run_doc),
		horizon_start=run_doc.horizon_start,
		horizon_end=run_doc.horizon_end,
		run_name=run_name,
	)
	buckets = build_capacity_buckets(
		workstations,
		run_doc.horizon_start,
		run_doc.horizon_end,
		blocked_intervals=fixed_intervals,
		downtime_windows=downtime_windows,
	)
	demands = [
		_build_segment_balance_demand(run_doc, result_rows[row.get("parent")], row)
		for row in segment_rows
		if row.get("parent") in result_rows and not _is_fixed_segment(row)
	]
	settings = planning.get_settings_dict()
	analysis = balance_capacity_nodes(
		demands,
		buckets,
		high_cancellation_risk_percent=flt(settings.get("high_cancellation_risk_percent") or 60),
		mold_blocked_intervals=mold_fixed_intervals,
	)
	source_snapshot = _build_source_snapshot(run_doc, result_rows, segment_rows)
	analysis["run"] = run_name
	analysis["source_fingerprint"] = fingerprint(source_snapshot)
	analysis["analysis_fingerprint"] = fingerprint(
		{"run": run_name, "source": source_snapshot, "demands": analysis.get("demands"), "buckets": analysis.get("buckets")}
	)
	analysis["source_snapshot"] = source_snapshot
	if persist:
		_persist_capacity_analysis(run_doc, analysis)
	return analysis


def confirm_capacity_balance(run_name: str) -> dict[str, Any]:
	frappe.db.sql("select name from `tabAPS Planning Run` where name = %s for update", run_name)
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	if run_doc.capacity_balance_status == "Applied":
		return {
			"run": run_name,
			"status": "Applied",
			"confirmed_by": run_doc.capacity_balance_confirmed_by,
			"confirmed_on": run_doc.capacity_balance_confirmed_on,
			"idempotent_replay": 1,
		}
	if run_doc.capacity_balance_status not in ("Suggestion Ready", "Confirmation Required"):
		frappe.throw(_("Analyze capacity balance before PMC confirmation."), frappe.ValidationError)
	if not run_doc.capacity_balance_fingerprint or not run_doc.capacity_balance_analysis_json:
		frappe.throw(_("Capacity analysis evidence is missing; analyze the run again."), frappe.ValidationError)
	confirmed_on = now_datetime()
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		{
			"capacity_balance_confirmed_by": frappe.session.user,
			"capacity_balance_confirmed_on": confirmed_on,
		},
		update_modified=False,
	)
	return {
		"run": run_name,
		"status": run_doc.capacity_balance_status,
		"confirmed_by": frappe.session.user,
		"confirmed_on": confirmed_on,
		"idempotent_replay": 0,
	}


def apply_capacity_balance(run_name: str, pmc_confirmed: bool = False) -> dict[str, Any]:
	from injection_aps.services import availability, consistency, planning

	save_point = "aps_capacity_apply_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		frappe.db.sql("select name from `tabAPS Planning Run` where name = %s for update", run_name)
		run_doc = frappe.get_doc("APS Planning Run", run_name)
		if run_doc.capacity_balance_status == "Applied":
			analysis = _load_capacity_analysis(run_doc)
			frappe.db.release_savepoint(save_point)
			return {
				"run": run_name,
				"status": "Applied",
				"analysis_fingerprint": run_doc.capacity_balance_fingerprint,
				"summary": analysis.get("summary") or {},
				"idempotent_replay": 1,
			}
		if run_doc.capacity_balance_status not in ("Suggestion Ready", "Confirmation Required"):
			frappe.throw(_("Analyze a non-blocked capacity proposal before Apply."), frappe.ValidationError)
		analysis = _load_capacity_analysis(run_doc)
		if analysis.get("summary", {}).get("blocked_demands") or analysis.get("summary", {}).get("unscheduled_qty"):
			frappe.throw(_("Blocked or unscheduled capacity proposals cannot be applied."), frappe.ValidationError)
		requires_confirmation = bool(analysis.get("summary", {}).get("requires_confirmation"))
		if requires_confirmation and not run_doc.capacity_balance_confirmed_by:
			if not pmc_confirmed:
				frappe.throw(_("PMC confirmation is required before applying this capacity proposal."), frappe.ValidationError)
			confirmed_on = now_datetime()
			frappe.db.set_value(
				"APS Planning Run",
				run_name,
				{
					"capacity_balance_confirmed_by": frappe.session.user,
					"capacity_balance_confirmed_on": confirmed_on,
				},
				update_modified=False,
			)
		result_rows, segment_rows = _get_run_balance_rows(run_name)
		current_snapshot = _build_source_snapshot(run_doc, result_rows, segment_rows)
		if fingerprint(current_snapshot) != analysis.get("source_fingerprint"):
			frappe.throw(
				_("The plan changed after capacity analysis. Analyze again before Apply."),
				frappe.ValidationError,
			)
		mutation = _apply_capacity_allocations(analysis)
		overlap_summary = planning._validate_run_segment_overlaps(run_name, persist_exceptions=True)
		mold_overlap_summary = planning._validate_run_mold_overlaps(run_name, persist_exceptions=True)
		if overlap_summary.get("count") or mold_overlap_summary.get("count"):
			frappe.throw(
				_("Capacity balance created {0} workstation overlap(s) and {1} mold overlap(s).").format(
					overlap_summary.get("count") or 0,
					mold_overlap_summary.get("count") or 0,
				),
				frappe.ValidationError,
			)
		consistency_summary = consistency.recalculate_plan_consistency(
			run_name,
			reason="capacity balance applied",
		)
		if not consistency_summary.get("valid"):
			frappe.throw(
				_("Capacity balance failed plan consistency with {0} error(s).").format(
					len(consistency_summary.get("errors") or [])
				),
				frappe.ValidationError,
			)
		fulfillment = availability.recalculate_run_fulfillment(run_name)
		applied_on = now_datetime()
		frappe.db.set_value(
			"APS Planning Run",
			run_name,
			{
				"capacity_balance_status": "Applied",
				"capacity_balance_applied_on": applied_on,
			},
			update_modified=False,
		)
		frappe.db.release_savepoint(save_point)
		return {
			"run": run_name,
			"status": "Applied",
			"analysis_fingerprint": analysis.get("analysis_fingerprint"),
			"summary": analysis.get("summary") or {},
			"mutation": mutation,
			"overlap_count": overlap_summary.get("count") or 0,
			"mold_overlap_count": mold_overlap_summary.get("count") or 0,
			"consistency": consistency_summary,
			"fulfillment": fulfillment,
			"applied_on": applied_on,
			"idempotent_replay": 0,
		}
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def _load_capacity_analysis(run_doc) -> dict[str, Any]:
	try:
		analysis = json.loads(run_doc.capacity_balance_analysis_json or "{}")
	except (TypeError, ValueError):
		analysis = {}
	if not analysis or analysis.get("analysis_fingerprint") != run_doc.capacity_balance_fingerprint:
		frappe.throw(_("Capacity analysis is missing or its fingerprint does not match."), frappe.ValidationError)
	return analysis


def _apply_capacity_allocations(analysis: dict[str, Any]) -> dict[str, Any]:
	rows_by_result = defaultdict(list)
	for row in analysis.get("demands") or []:
		rows_by_result[row.get("result")].append(row)
	updated_segments = 0
	created_segments = 0
	updated_results = 0
	for result_name, demand_rows in rows_by_result.items():
		result_doc = frappe.get_doc("APS Schedule Result", result_name)
		segment_by_name = {row.name: row for row in result_doc.segments}
		for demand in demand_rows:
			segment = segment_by_name.get(demand.get("segment"))
			if not segment:
				frappe.throw(_("Capacity source segment {0} no longer exists.").format(demand.get("segment")))
			if _is_fixed_segment(segment.as_dict()):
				frappe.throw(_("Fixed or started segment {0} cannot be capacity-balanced.").format(segment.name))
			allocations = demand.get("allocations") or []
			if not allocations or abs(sum(flt(row.get("qty")) for row in allocations) - flt(segment.planned_qty)) > CAPACITY_TOLERANCE:
				frappe.throw(_("Capacity allocation for segment {0} does not preserve its planned quantity.").format(segment.name))
			family_rows = [
				row
				for row in result_doc.segments
				if row.segment_kind == "Family Co-Product"
				and segment.family_group
				and row.family_group == segment.family_group
			]
			family_templates = [
				(_child_payload(row), flt(row.planned_qty) / max(flt(segment.planned_qty), CAPACITY_TOLERANCE))
				for row in family_rows
			]
			for row in family_rows:
				result_doc.remove(row)
			base_payload = _child_payload(segment)
			split_group = "BAL-{0}".format(fingerprint({"segment": segment.name, "analysis": analysis.get("analysis_fingerprint")})[:10])
			for allocation_index, allocation in enumerate(allocations, start=1):
				if allocation_index == 1:
					target = segment
				else:
					payload = dict(base_payload)
					for fieldname in (
						"linked_work_order",
						"linked_work_order_scheduling",
						"linked_scheduling_item",
						"actual_status",
						"actual_start_time",
						"actual_end_time",
						"last_execution_sync_on",
					):
						payload[fieldname] = None
					payload["actual_completed_qty"] = 0
					payload["actual_good_qty"] = 0
					payload["actual_scrap_qty"] = 0
					target = result_doc.append("segments", payload)
					created_segments += 1
				_apply_allocation_to_segment(
					target,
					allocation,
					source_segment=segment.name,
					split_group=split_group if len(allocations) > 1 else "",
					split_index=allocation_index if len(allocations) > 1 else 0,
					late_qty_after=flt(demand.get("late_qty_after_balance")),
				)
				updated_segments += 1
				for family_payload, ratio in family_templates:
					family_payload = dict(family_payload)
					family_payload["planned_qty"] = flt(allocation.get("qty")) * ratio
					family_target = result_doc.append("segments", family_payload)
					_apply_allocation_to_segment(
						family_target,
						allocation,
						source_segment=segment.name,
						split_group=split_group if len(allocations) > 1 else "",
						split_index=allocation_index if len(allocations) > 1 else 0,
						late_qty_after=flt(demand.get("late_qty_after_balance")),
						preserve_qty=True,
					)
					created_segments += 1
		result_doc.segments.sort(key=lambda row: (get_datetime(row.start_time), row.workstation or "", row.idx or 0))
		for sequence_no, row in enumerate(result_doc.segments, start=1):
			row.sequence_no = sequence_no
		result_doc.save(ignore_permissions=True)
		updated_results += 1
	return {
		"updated_results": updated_results,
		"updated_segments": updated_segments,
		"created_segments": created_segments,
	}


def _child_payload(row) -> dict[str, Any]:
	payload = {}
	for field in frappe.get_meta("APS Schedule Segment").fields:
		if not field.fieldname or field.fieldtype in ("Section Break", "Column Break", "Tab Break", "HTML", "Button"):
			continue
		payload[field.fieldname] = row.get(field.fieldname)
	return payload


def _apply_allocation_to_segment(
	segment,
	allocation: dict[str, Any],
	*,
	source_segment: str,
	split_group: str,
	split_index: int,
	late_qty_after: float,
	preserve_qty: bool = False,
):
	if not preserve_qty:
		segment.planned_qty = flt(allocation.get("qty"))
	segment.start_time = get_datetime(allocation.get("start"))
	segment.end_time = get_datetime(allocation.get("end"))
	segment.production_mode = allocation.get("mode")
	segment.capacity_bucket_start = get_datetime(allocation.get("bucket_start"))
	segment.capacity_bucket_end = get_datetime(allocation.get("bucket_end"))
	segment.available_capacity_qty = flt(allocation.get("available_capacity_qty"))
	segment.occupied_capacity_qty = flt(allocation.get("occupied_capacity_qty"))
	segment.remaining_capacity_qty = flt(allocation.get("remaining_capacity_qty"))
	segment.load_percent = flt(allocation.get("load_percent"))
	segment.projected_late_qty = flt(allocation.get("qty")) if allocation.get("mode") == "Late" else 0
	segment.prebuildable_qty = flt(allocation.get("qty")) if allocation.get("mode") == "Prebuild" else 0
	segment.setup_minutes = flt(allocation.get("setup_minutes"))
	segment.changeover_minutes = flt(allocation.get("setup_minutes"))
	segment.original_segment = source_segment if split_group else segment.original_segment
	segment.split_group = split_group
	segment.split_index = split_index
	segment.split_reason = "Capacity Balance" if split_group else segment.split_reason
	if allocation.get("mode") == "Late" and late_qty_after > 0:
		segment.risk_flags = "\n".join(
			dict.fromkeys([*(segment.risk_flags or "").splitlines(), "Capacity Late Quantity"])
		)


def _get_run_balance_rows(run_name: str):
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
			"name",
			"company",
			"plant_floor",
			"net_requirement",
			"customer",
			"item_code",
			"requested_date",
			"demand_source",
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
			"planned_qty",
			"late_qty_before_balance",
		],
	)
	result_map = {row.name: dict(row) for row in results}
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={
			"parenttype": "APS Schedule Result",
			"parent": ("in", list(result_map)),
			"segment_kind": ("in", ["Primary", "Manual"]),
			"segment_status": ("not in", ["Blocked", "Cancelled"]),
			"planned_qty": (">", 0),
		},
		fields=[
			"name",
			"parent",
			"idx",
			"workstation",
			"plant_floor",
			"start_time",
			"end_time",
			"planned_qty",
			"setup_minutes",
			"mould_reference",
			"segment_status",
			"actual_status",
			"actual_completed_qty",
			"is_locked",
			"anchor_strength",
			"linked_scheduling_item",
		],
		order_by="parent asc, sequence_no asc, idx asc",
	)
	return result_map, [dict(row) for row in segments]


def _is_fixed_segment(segment: dict[str, Any]) -> bool:
	return bool(
		cint(segment.get("is_locked"))
		or cint(segment.get("anchor_strength")) >= 50
		or (segment.get("segment_status") or "") in ("Applied", "Completed")
		or (segment.get("actual_status") or "") not in ("", "Not Started")
		or flt(segment.get("actual_completed_qty")) > 0
	)


def _get_fixed_execution_intervals(run_doc, run_segments):
	fixed_by_workstation = defaultdict(list)
	fixed_by_mold = defaultdict(list)
	for segment in run_segments:
		if not _is_fixed_segment(segment):
			continue
		interval = (segment.get("start_time"), segment.get("end_time"))
		fixed_by_workstation[segment.get("workstation")].append(interval)
		if segment.get("mould_reference"):
			fixed_by_mold[segment.get("mould_reference")].append(interval)
	if frappe.db.exists("DocType", "Scheduling Item"):
		rows = frappe.db.sql(
			"""
			select
				si.workstation,
				coalesce(si.from_time, si.planned_start_date) as start_time,
				coalesce(si.to_time, si.planned_end_date) as end_time,
				seg.mould_reference
			from `tabScheduling Item` si
			inner join `tabWork Order Scheduling` wos on wos.name = si.parent
			left join `tabAPS Schedule Segment` seg on seg.name = si.custom_aps_segment_reference
			where ifnull(wos.status, '') in ('Material Transfer', 'Job Card', 'Manufacture')
				and coalesce(si.from_time, si.planned_start_date) < %(horizon_end)s
				and coalesce(si.to_time, si.planned_end_date) > %(horizon_start)s
			""",
			{"horizon_start": run_doc.horizon_start, "horizon_end": run_doc.horizon_end},
			as_dict=True,
		)
		for row in rows:
			if not row.workstation or not row.start_time or not row.end_time:
				continue
			interval = (row.start_time, row.end_time)
			fixed_by_workstation[row.workstation].append(interval)
			if row.mould_reference:
				fixed_by_mold[row.mould_reference].append(interval)
	return dict(fixed_by_workstation), dict(fixed_by_mold)


def _build_segment_balance_demand(run_doc, result: dict[str, Any], segment: dict[str, Any]) -> dict[str, Any]:
	from injection_aps.services import planning

	duration_hours = max(_minutes_between(segment.get("start_time"), segment.get("end_time")) / 60, 0)
	hourly_rate = flt(segment.get("planned_qty")) / duration_hours if duration_hours else 0
	item_policy = _get_item_prebuild_policy(result.get("item_code"), result, planning.get_settings_dict())
	current_inventory_qty, inventory_room_qty = _get_item_inventory_room(
		result.get("company"),
		result.get("item_code"),
		item_policy.get("max_stock_qty"),
	)
	warehouse, warehouse_room_qty = _get_warehouse_capacity_room(
		result.get("company"),
		result.get("plant_floor"),
		result.get("item_code"),
		planning.get_settings_dict(),
	)
	material_ready_qty = _get_material_ready_qty(
		result.get("company"),
		result.get("item_code"),
		warehouse=warehouse,
	)
	due_time = get_datetime(f"{getdate(result.get('requested_date'))} 23:59:59")
	return {
		"key": segment.get("name"),
		"result": result.get("name"),
		"segment": segment.get("name"),
		"workstation": segment.get("workstation"),
		"mould_reference": segment.get("mould_reference"),
		"qty": flt(segment.get("planned_qty")),
		"hourly_rate": hourly_rate,
		"setup_minutes": flt(segment.get("setup_minutes")),
		"due_time": due_time,
		"strategy": result.get("production_strategy") or item_policy.get("production_strategy"),
		"demand_confidence": result.get("demand_confidence") or ("Forecast" if result.get("demand_source") == "Forecast" else "Confirmed"),
		"cancellation_risk_percent": max(
			flt(result.get("cancellation_risk_percent")),
			flt(item_policy.get("cancellation_risk_percent")),
		),
		"prebuild_allowed": cint(result.get("prebuild_allowed")) and cint(item_policy.get("prebuild_allowed")),
		"max_prebuild_days": cint(result.get("max_prebuild_days") or item_policy.get("max_prebuild_days")),
		"shelf_life_days": cint(item_policy.get("shelf_life_days")),
		"minimum_batch_qty": flt(item_policy.get("minimum_batch_qty")),
		"current_inventory_qty": current_inventory_qty,
		"inventory_room_qty": inventory_room_qty,
		"warehouse_room_qty": warehouse_room_qty,
		"material_ready_qty": material_ready_qty,
		"overstock_risk": cint(inventory_room_qty is not None and inventory_room_qty <= CAPACITY_TOLERANCE),
		"late_qty_before_balance": flt(result.get("late_qty_before_balance")) or (
			flt(segment.get("planned_qty")) if get_datetime(segment.get("end_time")) > due_time else 0
		),
	}


def _get_item_prebuild_policy(item_code: str, result: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
	meta = frappe.get_meta("Item")
	fields = ["shelf_life_in_days", "min_order_qty"]
	for fieldname in (
		"custom_aps_prebuild_allowed",
		"custom_aps_max_prebuild_days",
		"custom_aps_cancellation_risk_percent",
		"custom_aps_max_stock_qty",
	):
		if meta.has_field(fieldname):
			fields.append(fieldname)
	row = frappe.db.get_value("Item", item_code, fields, as_dict=True) or {}
	return {
		"production_strategy": result.get("production_strategy") or settings.get("default_production_strategy") or "Auto Balance",
		"prebuild_allowed": cint(row.get("custom_aps_prebuild_allowed", 1)),
		"max_prebuild_days": cint(row.get("custom_aps_max_prebuild_days") or settings.get("default_max_prebuild_days") or 7),
		"cancellation_risk_percent": flt(row.get("custom_aps_cancellation_risk_percent")),
		"max_stock_qty": flt(row.get("custom_aps_max_stock_qty")),
		"shelf_life_days": cint(row.get("shelf_life_in_days")),
		"minimum_batch_qty": flt(row.get("min_order_qty")),
	}


def _get_item_inventory_room(company: str, item_code: str, max_stock_qty: float):
	rows = frappe.db.sql(
		"""
		select coalesce(sum(bin.actual_qty), 0) as qty
		from `tabBin` bin
		inner join `tabWarehouse` wh on wh.name = bin.warehouse
		where wh.company = %s and wh.is_group = 0 and bin.item_code = %s
		""",
		(company, item_code),
		as_dict=True,
	)
	current = flt(rows[0].qty) if rows else 0
	room = max(flt(max_stock_qty) - current, 0) if flt(max_stock_qty) > 0 else None
	return current, room


def _get_warehouse_capacity_room(company: str, plant_floor: str | None, item_code: str, settings: dict[str, Any]):
	warehouse = None
	fieldname = settings.get("plant_floor_fg_warehouse_field")
	if plant_floor and fieldname and frappe.get_meta("Plant Floor").has_field(fieldname):
		warehouse = frappe.db.get_value("Plant Floor", plant_floor, fieldname)
	if not warehouse:
		warehouse = frappe.db.get_value(
			"Warehouse",
			{"company": company, "is_group": 0, "warehouse_type": "Finished Goods"},
			"name",
		)
	if not warehouse:
		return None, None
	capacity = flt(frappe.db.get_value("Warehouse", warehouse, "custom_aps_capacity_qty"))
	if capacity <= 0:
		return warehouse, None
	used = flt(frappe.db.get_value("Bin", {"warehouse": warehouse, "item_code": item_code}, "actual_qty"))
	return warehouse, max(capacity - used, 0)


def _get_material_ready_qty(company: str, item_code: str, warehouse: str | None = None) -> float | None:
	bom = frappe.db.get_value(
		"BOM",
		{"item": item_code, "docstatus": 1, "is_active": 1, "is_default": 1},
		["name", "quantity"],
		as_dict=True,
	)
	if not bom:
		return None
	components = frappe.get_all(
		"BOM Item",
		filters={"parent": bom.name},
		fields=["item_code", "qty", "source_warehouse"],
	)
	if not components:
		return None
	ready_qty = None
	for component in components:
		component_warehouse = component.source_warehouse or warehouse
		filters = {"item_code": component.item_code}
		if component_warehouse:
			filters["warehouse"] = component_warehouse
		available = sum(
			max(flt(row.actual_qty) - flt(row.reserved_qty), 0)
			for row in frappe.get_all("Bin", filters=filters, fields=["actual_qty", "reserved_qty"])
		)
		per_unit = flt(component.qty) / max(flt(bom.quantity), 1)
		component_ready = available / per_unit if per_unit > 0 else 0
		ready_qty = component_ready if ready_qty is None else min(ready_qty, component_ready)
	return max(flt(ready_qty), 0)


def _build_source_snapshot(run_doc, result_rows, segment_rows):
	return {
		"run": run_doc.name,
		"modified": str(run_doc.modified),
		"horizon_start": str(run_doc.horizon_start),
		"horizon_end": str(run_doc.horizon_end),
		"results": [
			{
				"name": row.get("name"),
				"strategy": row.get("production_strategy"),
				"planned_qty": flt(row.get("planned_qty")),
				"requested_date": str(row.get("requested_date")),
			}
			for row in sorted(result_rows.values(), key=lambda item: item.get("name") or "")
		],
		"segments": [
			{
				"name": row.get("name"),
				"parent": row.get("parent"),
				"workstation": row.get("workstation"),
				"start": str(row.get("start_time")),
				"end": str(row.get("end_time")),
				"qty": flt(row.get("planned_qty")),
				"status": row.get("segment_status"),
				"actual_status": row.get("actual_status"),
				"actual_qty": flt(row.get("actual_completed_qty")),
				"locked": cint(row.get("is_locked")),
			}
			for row in sorted(segment_rows, key=lambda item: item.get("name") or "")
		],
	}


def _persist_capacity_analysis(run_doc, analysis: dict[str, Any]):
	now_value = now_datetime()
	demand_by_result = defaultdict(list)
	for row in analysis.get("demands") or []:
		demand_by_result[row.get("result")].append(row)
	for result_name, rows in demand_by_result.items():
		prebuild_qty = sum(flt(row.get("prebuild_qty")) for row in rows)
		jit_qty = sum(flt(row.get("jit_qty")) for row in rows)
		late_before = sum(flt(row.get("late_qty_before_balance")) for row in rows)
		late_after = sum(flt(row.get("late_qty_after_balance")) for row in rows)
		requires_confirmation = any(cint(row.get("requires_confirmation")) for row in rows)
		status = "Blocked" if any(row.get("status") == "Blocked" for row in rows) else (
			"Confirmation Required" if requires_confirmation else "Balanced"
		)
		frappe.db.set_value(
			"APS Schedule Result",
			result_name,
			{
				"prebuild_qty": prebuild_qty,
				"jit_qty": jit_qty,
				"early_days": max((flt(row.get("early_days")) for row in rows), default=0),
				"projected_peak_inventory_qty": max(
					(flt(row.get("projected_peak_inventory_qty")) for row in rows), default=0
				),
				"late_qty_before_balance": late_before,
				"late_qty_after_balance": late_after,
				"capacity_balance_status": status,
				"capacity_balance_requires_confirmation": cint(requires_confirmation),
				"capacity_balance_details": json.dumps(rows, default=str, ensure_ascii=False, indent=2),
			},
			update_modified=False,
		)
	status = "Blocked" if analysis["summary"].get("blocked_demands") else (
		"Confirmation Required" if analysis["summary"].get("requires_confirmation") else "Suggestion Ready"
	)
	frappe.db.set_value(
		"APS Planning Run",
		run_doc.name,
		{
			"total_prebuild_qty": flt(analysis["summary"].get("prebuild_qty")),
			"total_jit_qty": flt(analysis["summary"].get("jit_qty")),
			"capacity_balance_status": status,
			"capacity_balance_analyzed_on": now_value,
			"capacity_balance_fingerprint": analysis.get("analysis_fingerprint"),
			"capacity_balance_analysis_json": json.dumps(analysis, default=str, ensure_ascii=False, indent=2),
		},
		update_modified=False,
	)
