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
	workstation_plant_floors: dict[str, str | None] | None = None,
	company: str | None = None,
) -> list[dict[str, Any]]:
	blocked_intervals = blocked_intervals or {}
	downtime_windows = downtime_windows or []
	workstation_plant_floors = workstation_plant_floors or {}
	buckets = []
	for workstation in sorted({row for row in workstations if row}):
		for window in iter_shift_windows(horizon_start, horizon_end):
			start = window["start"]
			end = window["end"]
			free_intervals = [(start, end)]
			for blocked_start, blocked_end in blocked_intervals.get(workstation) or []:
				free_intervals = _subtract_from_intervals(free_intervals, blocked_start, blocked_end)
			matching_downtime = [
				row
				for row in downtime_windows
				if _downtime_applies_to_bucket(
					row,
					workstation=workstation,
					plant_floor=workstation_plant_floors.get(workstation),
					company=company,
				)
			]
			# Keep the time-varying factor, not only its aggregate effective minutes.
			# Otherwise a 50% pre-midnight slowdown can lend its unused wall time to
			# the post-midnight JIT portion of the same shift (and the persisted segment
			# is then written at the full rate).  Zero-capacity pieces are absent from
			# the physical free intervals; positive pieces retain their exact factor.
			capacity_factor_intervals = _build_capacity_factor_intervals(
				free_intervals, matching_downtime
			)
			free_intervals = [
				(row["start"], row["end"])
				for row in capacity_factor_intervals
				if flt(row.get("factor")) > CAPACITY_TOLERANCE
			]
			available_minutes = sum(
				_minutes_between(row["start"], row["end"]) * flt(row.get("factor"))
				for row in capacity_factor_intervals
			)
			free_minutes = _interval_minutes(free_intervals)
			available_minutes = max(available_minutes, 0)
			buckets.append(
				{
					"key": f"{workstation}|{start.isoformat()}",
					"workstation": workstation,
					**window,
					"free_intervals": free_intervals,
					"capacity_factor_intervals": capacity_factor_intervals,
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
	reserved_demands: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	reserved_demands = reserved_demands or []
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
	resource_balances = _build_shared_resource_balances([*reserved_demands, *demands])
	results = []
	for demand in sorted(
		reserved_demands,
		key=lambda row: (
			get_datetime(row.get("start_time") or row.get("due_time")),
			str(row.get("key") or ""),
		),
	):
		resource_limited_demand = _apply_shared_resource_limits(demand, resource_balances)
		reservation = _build_fixed_commitment_result(resource_limited_demand)
		_consume_shared_resources(resource_limited_demand, reservation, resource_balances)
	fixed_demands = sorted(
		(demand for demand in demands if cint(demand.get("fixed_commitment"))),
		key=lambda row: (
			get_datetime(row.get("start_time") or row.get("due_time")),
			str(row.get("key") or ""),
		),
	)
	for demand in fixed_demands:
		resource_limited_demand = _apply_shared_resource_limits(demand, resource_balances)
		result = _build_fixed_commitment_result(resource_limited_demand)
		results.append(result)
		_consume_shared_resources(resource_limited_demand, result, resource_balances)

	ordered_demands = sorted(
		(
			demand
			for demand in demands
			if not cint(demand.get("fixed_commitment"))
			and flt(demand.get("qty")) > CAPACITY_TOLERANCE
		),
		key=lambda row: (
			get_datetime(row.get("due_time")),
			-cint(row.get("priority") or 0),
			str(row.get("key") or ""),
		),
	)
	for demand in ordered_demands:
		resource_limited_demand = _apply_shared_resource_limits(demand, resource_balances)
		result = _balance_one_demand(
			resource_limited_demand,
			buckets_by_workstation.get(demand.get("workstation")) or [],
			mold_free,
			high_cancellation_risk_percent=high_cancellation_risk_percent,
		)
		results.append(result)
		_consume_shared_resources(resource_limited_demand, result, resource_balances)

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
	material_limit = demand.get("material_schedulable_qty")
	schedulable_qty = (
		qty
		if material_limit is None
		else min(qty, max(flt(material_limit), 0))
	)
	material_shortage_qty = max(qty - schedulable_qty, 0)
	storage_shortage_qty = 0.0
	if cint(demand.get("stock_retained")):
		storage_limits = [
			max(flt(demand.get(key)), 0)
			for key in ("inventory_room_qty", "warehouse_room_qty")
			if demand.get(key) is not None
		]
		if storage_limits:
			storage_schedulable_qty = min([schedulable_qty, *storage_limits])
			storage_shortage_qty = max(schedulable_qty - storage_schedulable_qty, 0)
			schedulable_qty = storage_schedulable_qty
	rate = max(flt(demand.get("hourly_rate")), 0)
	strategy = normalize_production_strategy(demand.get("strategy"))
	due_time = get_datetime(demand.get("due_time"))
	checks = []
	if not buckets or rate <= 0:
		return _blocked_demand_result(
			demand,
			qty,
			strategy,
			_("No usable workstation capacity or production rate."),
			key="missing_machine_capability" if not buckets else "missing_cycle_time",
		)

	prebuild_indices, jit_indices, late_indices = _classify_capacity_bucket_indices(
		buckets,
		due_time,
		due_granularity=demand.get("due_granularity"),
	)
	if not jit_indices and (demand.get("due_granularity") or "Datetime") != "Date":
		return _blocked_demand_result(
			demand,
			qty,
			strategy,
			_("Delivery time is outside the available capacity horizon."),
			key="invalid_horizon_input",
		)
	max_early_days = max(cint(demand.get("max_prebuild_days") or 0), 0)
	shelf_life_days = max(cint(demand.get("shelf_life_days") or 0), 0)
	if shelf_life_days and max_early_days:
		max_early_days = min(max_early_days, shelf_life_days)
	if (demand.get("due_granularity") or "Datetime") == "Date":
		earliest_prebuild_time = get_datetime(
			f"{getdate(due_time) - timedelta(days=max_early_days)} 00:00:00"
		)
		prebuild_indices = (
			[
				idx
				for idx in prebuild_indices
				if get_datetime(buckets[idx].get("end")) > earliest_prebuild_time
			]
			if max_early_days
			else []
		)
	else:
		earliest_prebuild_time = due_time - timedelta(days=max_early_days) if max_early_days else due_time
		prebuild_indices = [
			idx
			for idx in prebuild_indices
			if get_datetime(buckets[idx].get("end")) > earliest_prebuild_time
		]
	# Keep a cross-midnight bucket when only part of it is inside the permitted
	# prebuild window.  The interval allocator clips that bucket to this exact
	# datetime; filtering by the shift posting date would discard valid hours.
	demand["prebuild_earliest_time"] = earliest_prebuild_time

	prebuild_allowed = bool(cint(demand.get("prebuild_allowed", 1)))
	prebuild_cap, cap_checks = _get_prebuild_cap(demand, schedulable_qty)
	checks.extend(cap_checks)
	if cint(demand.get("warehouse_stock_uom_conflict")):
		checks.append(
			_check(
				"blocked",
				"warehouse_stock_uom",
				_(
					"FG warehouse capacity cannot be shared across different stock UOMs; Prebuild is blocked."
				),
			)
		)
	if material_shortage_qty > CAPACITY_TOLERANCE:
		checks.append(
				_check(
					"blocked",
					"material_readiness",
					_("Material only supports {0}; {1} cannot be scheduled.").format(
						f"{schedulable_qty:g}", f"{material_shortage_qty:g}"
					),
				)
			)
	if not prebuild_allowed:
		prebuild_cap = 0
		checks.append(_check("blocked", "prebuild_allowed", _("Item or demand policy does not allow Prebuild.")))
	if not prebuild_indices:
		prebuild_cap = 0
		checks.append(
			_check(
				"blocked",
				"max_prebuild_days",
				_("No earlier capacity bucket is inside the allowed Prebuild window."),
			)
		)
	if storage_shortage_qty > CAPACITY_TOLERANCE:
		checks.append(
			_check(
				"blocked",
				"stock_retained_capacity",
				_(
					"Stock production only has storage capacity for {0}; {1} cannot be scheduled.",
					context="Injection APS",
				).format(f"{schedulable_qty:g}", f"{storage_shortage_qty:g}"),
			)
		)

	setup_minutes = max(flt(demand.get("setup_minutes")), 0)
	jit_capacity = _estimate_qty_capacity(
		buckets,
		jit_indices,
		demand,
		mold_free,
		setup_minutes=setup_minutes,
		mode="JIT",
	)
	minimum_batch_qty = max(flt(demand.get("minimum_batch_qty")), 0)
	target_prebuild = 0.0
	if strategy == "Auto Balance":
		target_prebuild = max(schedulable_qty - jit_capacity, 0)
		target_prebuild = min(target_prebuild, prebuild_cap)
	elif strategy == "Force Prebuild":
		target_prebuild = min(schedulable_qty, prebuild_cap)

	allocations = []
	remaining_qty = schedulable_qty
	setup_state = {
		"remaining": setup_minutes,
		"setup_minutes": setup_minutes,
		"campaign_started": 0,
		"occupied_intervals": [],
	}
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
			list(reversed(jit_indices)),
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
	scheduled_qty = prebuild_qty + jit_qty + late_allocated_qty
	unscheduled_qty = max(remaining_qty + material_shortage_qty + storage_shortage_qty, 0)
	late_qty = max(late_allocated_qty + unscheduled_qty, 0)
	if scheduled_qty > 0 and minimum_batch_qty and scheduled_qty + CAPACITY_TOLERANCE < minimum_batch_qty:
		checks.append(
				_check(
					"blocked",
					"minimum_batch_qty",
					_("Scheduled production quantity {0} is below minimum batch {1}.").format(
						f"{scheduled_qty:g}", f"{minimum_batch_qty:g}"
					),
				)
			)
	if prebuild_qty <= CAPACITY_TOLERANCE and schedulable_qty <= jit_capacity + CAPACITY_TOLERANCE:
		checks.append(
			_check(
				"passed",
				"necessary_prebuild_only",
				_("JIT capacity is sufficient; no Prebuild was created."),
			)
		)
	elif prebuild_qty > 0:
		checks.append(
			_check(
				"passed",
				"necessary_prebuild_only",
				_("Only the quantity needed outside the due bucket was assigned to Prebuild."),
			)
		)

	requires_confirmation_reasons = []
	if prebuild_qty > 0 and (demand.get("demand_confidence") or "Confirmed") == "Forecast":
		requires_confirmation_reasons.append(_("Forecast demand", context="Injection APS"))
	if prebuild_qty > 0 and flt(demand.get("cancellation_risk_percent")) >= flt(high_cancellation_risk_percent):
		requires_confirmation_reasons.append(_("High cancellation risk", context="Injection APS"))
	if prebuild_qty > 0 and cint(demand.get("overstock_risk")):
		requires_confirmation_reasons.append(_("Overstock risk", context="Injection APS"))
	if prebuild_qty > 0 and demand.get("inventory_room_qty") is None:
		requires_confirmation_reasons.append(
			_("Item inventory limit not configured", context="Injection APS")
		)
	if prebuild_qty > 0 and demand.get("warehouse_room_qty") is None:
		requires_confirmation_reasons.append(
			_("FG warehouse capacity not configured", context="Injection APS")
		)
	if prebuild_qty > 0 and not cint(demand.get("material_advisory_only")) and demand.get("material_ready_qty") is None:
		requires_confirmation_reasons.append(_("Material readiness not proven", context="Injection APS"))
	requires_confirmation = bool(requires_confirmation_reasons)
	if requires_confirmation:
		checks.append(_check("warning", "pmc_confirmation", ", ".join(requires_confirmation_reasons)))

	earliest_prebuild = min(
		(get_datetime(row.get("end")) for row in allocations if row.get("mode") == "Prebuild"),
		default=None,
	)
	early_days = max((due_time - earliest_prebuild).total_seconds() / 86400, 0) if earliest_prebuild else 0
	status = "Balanced"
	hard_policy_block = any(
		row.get("status") == "blocked" and row.get("key") == "minimum_batch_qty"
		for row in checks
	)
	if unscheduled_qty > CAPACITY_TOLERANCE or hard_policy_block:
		status = "Blocked"
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
		"unscheduled_qty": unscheduled_qty,
		"early_days": round(early_days, 4),
		"projected_peak_inventory_qty": (
			flt(demand.get("current_inventory_qty"))
			+ max(flt(demand.get("prior_shared_prebuild_qty")), 0)
			+ (scheduled_qty if cint(demand.get("stock_retained")) else prebuild_qty)
		),
		"late_qty_before_balance": max(flt(demand.get("late_qty_before_balance")), 0),
		"late_qty_after_balance": late_qty,
		"requires_confirmation": cint(requires_confirmation),
		"confirmation_reasons": requires_confirmation_reasons,
		"status": status,
		"checks": checks,
		"allocations": sorted(allocations, key=lambda row: (get_datetime(row.get("start")), row.get("bucket_key") or "")),
		"fixed_commitment": 0,
	}


def _build_fixed_commitment_result(demand: dict[str, Any]) -> dict[str, Any]:
	qty = max(flt(demand.get("qty")), 0)
	prebuild_qty, jit_qty, late_qty = _fixed_mode_quantities(demand)
	remaining_ratio = min(
		max(flt(demand.get("resource_consumption_qty")) / qty, 0), 1
	) if qty > CAPACITY_TOLERANCE else 0
	resource_qty = max(flt(demand.get("resource_consumption_qty")), 0)
	demand["resource_prebuild_qty"] = (
		resource_qty
		if cint(demand.get("stock_retained"))
		else prebuild_qty * remaining_ratio
	)
	material_resource_qty = max(
		flt(
			demand.get("resource_material_consumption_qty")
			if demand.get("resource_material_consumption_qty") is not None
			else resource_qty
		),
		0,
	)
	checks = [
		_check(
			"passed",
			"fixed_commitment",
			_("Fixed or started segment is preserved.", context="Injection APS"),
		)
	]
	shortages = []
	material_limit = demand.get("material_ready_qty")
	if material_limit is not None and material_resource_qty > flt(material_limit) + CAPACITY_TOLERANCE:
		shortages.append(_("Material", context="Injection APS"))
	for key, label in (
		("inventory_room_qty", _("Item Inventory", context="Injection APS")),
		("warehouse_room_qty", _("Warehouse Capacity", context="Injection APS")),
	):
		limit = demand.get(key)
		if limit is not None and demand["resource_prebuild_qty"] > flt(limit) + CAPACITY_TOLERANCE:
			shortages.append(label)
	if cint(demand.get("warehouse_stock_uom_conflict")):
		shortages.append(_("Warehouse Stock UOM", context="Injection APS"))
	if cint(demand.get("linked_work_order_stopped")):
		shortages.append(_("Stopped Work Order", context="Injection APS"))
	if shortages:
		checks.append(
			_check(
				"blocked",
				"fixed_resource_overcommit",
				_(
					"Fixed segment exceeds available shared resources: {0}.",
					context="Injection APS",
				).format(", ".join(shortages)),
			)
		)
	allocations = []
	for mode, mode_qty in (("Prebuild", prebuild_qty), ("JIT", jit_qty), ("Late", late_qty)):
		if mode_qty <= CAPACITY_TOLERANCE:
			continue
		allocations.append(
			{
				"bucket_key": f"FIXED|{demand.get('segment')}|{mode}",
				"mode": mode,
				"start": demand.get("start_time"),
				"end": demand.get("end_time"),
				"qty": mode_qty,
				"setup_minutes": flt(demand.get("setup_minutes")) if not allocations else 0,
			}
		)
	return {
		"key": demand.get("key"),
		"result": demand.get("result"),
		"segment": demand.get("segment"),
		"workstation": demand.get("workstation"),
		"mould_reference": demand.get("mould_reference"),
		"strategy": normalize_production_strategy(demand.get("strategy")),
		"planned_qty": qty,
		"prebuild_qty": prebuild_qty,
		"jit_qty": jit_qty,
		"late_qty": late_qty,
		"unscheduled_qty": 0,
		"early_days": 0,
		"projected_peak_inventory_qty": (
			flt(demand.get("current_inventory_qty"))
			+ max(flt(demand.get("prior_shared_prebuild_qty")), 0)
			+ max(flt(demand.get("resource_prebuild_qty")), 0)
		),
		"late_qty_before_balance": late_qty,
		"late_qty_after_balance": late_qty,
		"requires_confirmation": 0,
		"confirmation_reasons": [],
		"status": "Blocked" if shortages else "Fixed",
		"checks": checks,
		"allocations": allocations,
		"fixed_commitment": 1,
	}


def _fixed_mode_quantities(demand: dict[str, Any]) -> tuple[float, float, float]:
	qty = max(flt(demand.get("qty")), 0)
	start = get_datetime(demand.get("start_time"))
	end = get_datetime(demand.get("end_time"))
	if end <= start or qty <= CAPACITY_TOLERANCE:
		return 0, 0, qty
	if (demand.get("due_granularity") or "Datetime") != "Date":
		existing_mode = demand.get("production_mode")
		if existing_mode in ("Prebuild", "JIT", "Late"):
			return (
				qty if existing_mode == "Prebuild" else 0,
				qty if existing_mode == "JIT" else 0,
				qty if existing_mode == "Late" else 0,
			)
		return (0, qty, 0) if end <= get_datetime(demand.get("due_time")) else (0, 0, qty)
	due_start = get_datetime(f"{getdate(demand.get('due_time'))} 00:00:00")
	due_end = due_start + timedelta(days=1)
	# A fixed segment created around partial downtime produces in proportion to
	# effective capacity, not wall-clock duration.  Without this weighting a
	# cross-midnight segment could assign too much quantity to the reduced-speed
	# side of the delivery-day boundary.
	factor_intervals = demand.get("capacity_factor_intervals") or [
		{"start": start, "end": end, "factor": 1.0}
	]
	total_minutes = _effective_overlap_minutes(factor_intervals, start, end)
	if total_minutes <= CAPACITY_TOLERANCE:
		return 0, 0, qty
	prebuild_minutes = _effective_overlap_minutes(factor_intervals, start, due_start)
	jit_minutes = _effective_overlap_minutes(factor_intervals, due_start, due_end)
	late_minutes = _effective_overlap_minutes(factor_intervals, due_end, end)
	prebuild_qty = qty * prebuild_minutes / total_minutes
	jit_qty = qty * jit_minutes / total_minutes
	late_qty = max(qty - prebuild_qty - jit_qty, 0)
	return prebuild_qty, jit_qty, late_qty


def _effective_overlap_minutes(
	factor_intervals: list[dict[str, Any]],
	window_start,
	window_end,
) -> float:
	if not window_start or not window_end or get_datetime(window_end) <= get_datetime(window_start):
		return 0
	return sum(
		_overlap_minutes(
			row.get("start"),
			row.get("end"),
			window_start,
			window_end,
		)
		* max(min(flt(row.get("factor")), 1), 0)
		for row in factor_intervals
		if row.get("start") and row.get("end")
	)


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
	if latest:
		return _allocate_qty_latest(
			qty,
			hourly_rate,
			buckets,
			indices,
			demand,
			mold_free,
			mode=mode,
			setup_state=setup_state,
		)
	remaining_qty = max(flt(qty), 0)
	allocations = []
	for idx in indices:
		if remaining_qty <= CAPACITY_TOLERANCE:
			break
		bucket = buckets[idx]
		while remaining_qty > CAPACITY_TOLERANCE and flt(bucket.get("remaining_budget_minutes")) > CAPACITY_TOLERANCE:
			common = _get_effective_common_free_intervals(
				bucket, demand, mold_free, mode=mode
			)
			if not common:
				break
			interval = common[-1] if latest else common[0]
			budget = flt(bucket.get("remaining_budget_minutes"))
			interval_start = get_datetime(interval[0])
			interval_end = get_datetime(interval[1])
			capacity_factor = max(min(flt(interval[2]), 1), 0)
			interval_minutes = _minutes_between(interval_start, interval_end)
			if capacity_factor <= CAPACITY_TOLERANCE or interval_minutes <= CAPACITY_TOLERANCE:
				break

			# One setup is valid only for one physically contiguous campaign. A fixed
			# job, full downtime, or an unused gap breaks continuity. The old single
			# setup_state let production on both sides of such a gap silently share one
			# changeover and therefore overstated schedulable quantity.
			if cint(setup_state.get("campaign_started")) and not _interval_touches_campaign(
				(interval_start, interval_end), setup_state.get("occupied_intervals") or []
			):
				setup_state["remaining"] = max(flt(setup_state.get("setup_minutes")), 0)
			setup_minutes = min(
				flt(setup_state.get("remaining")),
				interval_minutes,
				budget / capacity_factor,
			)
			setup_effective_minutes = setup_minutes * capacity_factor
			production_wall_minutes_available = max(interval_minutes - setup_minutes, 0)
			production_effective_minutes_available = min(
				production_wall_minutes_available * capacity_factor,
				max(budget - setup_effective_minutes, 0),
			)
			required_effective_minutes = remaining_qty / hourly_rate * 60
			production_effective_minutes = min(
				required_effective_minutes, production_effective_minutes_available
			)
			production_minutes = production_effective_minutes / capacity_factor
			occupied_minutes = setup_minutes + production_minutes
			if occupied_minutes <= CAPACITY_TOLERANCE:
				break
			if latest:
				occupied_end = interval_end
				occupied_start = occupied_end - timedelta(minutes=occupied_minutes)
				production_start = occupied_start + timedelta(minutes=setup_minutes)
				production_end = occupied_end
			else:
				occupied_start = interval_start
				production_start = occupied_start + timedelta(minutes=setup_minutes)
				production_end = production_start + timedelta(minutes=production_minutes)
				occupied_end = production_end
			_remove_occupied_interval(bucket, demand, mold_free, occupied_start, occupied_end)
			bucket["remaining_budget_minutes"] = max(
				budget - setup_effective_minutes - production_effective_minutes, 0
			)
			setup_state["remaining"] = max(flt(setup_state.get("remaining")) - setup_minutes, 0)
			setup_state["campaign_started"] = 1
			setup_state.setdefault("occupied_intervals", []).append((occupied_start, occupied_end))
			allocated_qty = min(remaining_qty, production_effective_minutes * hourly_rate / 60)
			if allocated_qty <= CAPACITY_TOLERANCE:
				# Setup can consume a short factor slice by itself. Keep its physical
				# occupation and continue into an adjacent slice; a real gap will reset
				# setup on the next iteration.
				continue
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


def _allocate_qty_latest(
	qty: float,
	hourly_rate: float,
	buckets: list[dict[str, Any]],
	indices: list[int],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	*,
	mode: str,
	setup_state: dict[str, Any],
) -> list[dict[str, Any]]:
	"""Allocate a latest-feasible suffix while keeping setup before production.

	The former reverse loop charged setup in the latest bucket and then placed
	additional production in earlier buckets.  That produced an impossible time
	line (production -> setup -> production).  Here each physically contiguous
	component is treated as one campaign: select a production suffix, reserve the
	immediately preceding setup wall time, then split the production by its exact
	capacity-factor pieces.
	"""
	remaining_qty = max(flt(qty), 0)
	if remaining_qty <= CAPACITY_TOLERANCE or hourly_rate <= CAPACITY_TOLERANCE:
		return []
	pieces = _collect_latest_effective_pieces(
		buckets,
		indices,
		demand,
		mold_free,
		mode=mode,
	)
	components = _group_effective_pieces(pieces)
	allocations: list[dict[str, Any]] = []
	for component in reversed(components):
		if remaining_qty <= CAPACITY_TOLERANCE:
			break
		component_start = get_datetime(component[0]["start"])
		component_end = get_datetime(component[-1]["end"])
		requested_effective_minutes = remaining_qty / hourly_rate * 60
		no_setup_start = _find_effective_tail_start(component, requested_effective_minutes)
		continues_campaign = bool(
			cint(setup_state.get("campaign_started"))
			and no_setup_start
			and _interval_touches_campaign(
				(no_setup_start, component_end),
				setup_state.get("occupied_intervals") or [],
			)
		)
		if continues_campaign:
			setup_minutes = max(flt(setup_state.get("remaining")), 0)
		else:
			setup_minutes = max(flt(setup_state.get("setup_minutes")), 0)
		if _minutes_between(component_start, component_end) <= setup_minutes + CAPACITY_TOLERANCE:
			continue
		production_floor = component_start + timedelta(minutes=setup_minutes)
		max_effective_minutes = _effective_piece_minutes(
			component,
			production_floor,
			component_end,
		)
		production_effective_minutes = min(
			requested_effective_minutes,
			max_effective_minutes,
		)
		if production_effective_minutes <= CAPACITY_TOLERANCE:
			continue
		production_start = _find_effective_tail_start(component, production_effective_minutes)
		if not production_start:
			continue
		occupied_start = production_start - timedelta(minutes=setup_minutes)
		if occupied_start < component_start:
			# Numerical guard: max_effective_minutes above guarantees feasibility.
			occupied_start = component_start
			production_start = occupied_start + timedelta(minutes=setup_minutes)
		production_rows = []
		for piece in component:
			piece_start = get_datetime(piece["start"])
			piece_end = get_datetime(piece["end"])
			occupied_piece_start = max(piece_start, occupied_start)
			occupied_piece_end = min(piece_end, component_end)
			if occupied_piece_end > occupied_piece_start:
				_remove_occupied_interval(
					piece["bucket"],
					demand,
					mold_free,
					occupied_piece_start,
					occupied_piece_end,
				)
				consumed_effective = (
					_minutes_between(occupied_piece_start, occupied_piece_end)
					* flt(piece["factor"])
				)
				piece["bucket"]["remaining_budget_minutes"] = max(
					flt(piece["bucket"].get("remaining_budget_minutes"))
					- consumed_effective,
					0,
				)
			production_piece_start = max(piece_start, production_start)
			production_piece_end = min(piece_end, component_end)
			if production_piece_end <= production_piece_start:
				continue
			production_piece_effective = (
				_minutes_between(production_piece_start, production_piece_end)
				* flt(piece["factor"])
			)
			allocated_qty = production_piece_effective * hourly_rate / 60
			if allocated_qty <= CAPACITY_TOLERANCE:
				continue
			production_rows.append(
				{
					"piece": piece,
					"start": production_piece_start,
					"end": production_piece_end,
					"qty": allocated_qty,
				}
			)
		if not production_rows:
			continue
		actual_allocated_qty = sum(flt(row["qty"]) for row in production_rows)
		remaining_qty = max(remaining_qty - actual_allocated_qty, 0)
		setup_state["remaining"] = 0
		setup_state["campaign_started"] = 1
		setup_state.setdefault("occupied_intervals", []).append(
			(occupied_start, component_end)
		)
		for row_index, row in enumerate(production_rows):
			piece = row["piece"]
			bucket = piece["bucket"]
			available_qty = flt(bucket.get("initial_available_minutes")) * hourly_rate / 60
			remaining_capacity_qty = (
				flt(bucket.get("remaining_budget_minutes")) * hourly_rate / 60
			)
			occupied_capacity_qty = max(available_qty - remaining_capacity_qty, 0)
			allocations.append(
				{
					"bucket_key": bucket.get("key"),
					"bucket_start": bucket.get("start"),
					"bucket_end": bucket.get("end"),
					"posting_date": bucket.get("posting_date"),
					"shift_type": bucket.get("shift_type"),
					"mode": mode,
					"start": row["start"],
					"end": row["end"],
					"qty": row["qty"],
					"setup_minutes": setup_minutes if row_index == 0 else 0,
					"available_capacity_qty": available_qty,
					"occupied_capacity_qty": occupied_capacity_qty,
					"remaining_capacity_qty": remaining_capacity_qty,
					"load_percent": round(
						(occupied_capacity_qty / available_qty * 100)
						if available_qty
						else 0,
						4,
					),
				}
			)
	return allocations


def _collect_latest_effective_pieces(
	buckets: list[dict[str, Any]],
	indices: list[int],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	*,
	mode: str | None,
) -> list[dict[str, Any]]:
	pieces = []
	for idx in sorted(set(indices)):
		bucket = buckets[idx]
		budget = max(flt(bucket.get("remaining_budget_minutes")), 0)
		bucket_pieces = _get_effective_common_free_intervals(
			bucket,
			demand,
			mold_free,
			mode=mode,
		)
		for start, end, factor in reversed(bucket_pieces):
			if budget <= CAPACITY_TOLERANCE:
				break
			factor = max(min(flt(factor), 1), 0)
			if factor <= CAPACITY_TOLERANCE:
				continue
			effective_minutes = min(_minutes_between(start, end) * factor, budget)
			if effective_minutes <= CAPACITY_TOLERANCE:
				continue
			usable_start = get_datetime(end) - timedelta(
				minutes=effective_minutes / factor
			)
			pieces.append(
				{
					"bucket": bucket,
					"start": usable_start,
					"end": get_datetime(end),
					"factor": factor,
				}
			)
			budget -= effective_minutes
	return sorted(pieces, key=lambda row: (row["start"], row["end"], row["bucket"].get("key") or ""))


def _group_effective_pieces(pieces: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
	components: list[list[dict[str, Any]]] = []
	for piece in pieces:
		if components and get_datetime(components[-1][-1]["end"]) == get_datetime(piece["start"]):
			components[-1].append(piece)
		else:
			components.append([piece])
	return components


def _effective_piece_minutes(
	pieces: list[dict[str, Any]],
	window_start,
	window_end,
) -> float:
	return sum(
		_overlap_minutes(
			piece["start"],
			piece["end"],
			window_start,
			window_end,
		)
		* flt(piece["factor"])
		for piece in pieces
	)


def _find_effective_tail_start(
	pieces: list[dict[str, Any]], required_effective_minutes: float
) -> datetime | None:
	remaining = max(flt(required_effective_minutes), 0)
	if not pieces:
		return None
	if remaining <= CAPACITY_TOLERANCE:
		return get_datetime(pieces[-1]["end"])
	for piece in reversed(pieces):
		factor = max(min(flt(piece["factor"]), 1), 0)
		piece_minutes = _minutes_between(piece["start"], piece["end"])
		piece_effective = piece_minutes * factor
		if remaining <= piece_effective + CAPACITY_TOLERANCE:
			return get_datetime(piece["end"]) - timedelta(minutes=remaining / factor)
		remaining -= piece_effective
	return get_datetime(pieces[0]["start"])


def _estimate_qty_capacity(
	buckets: list[dict[str, Any]],
	indices: list[int],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	*,
	setup_minutes: float,
	mode: str | None = None,
) -> float:
	pieces = _collect_latest_effective_pieces(
		buckets,
		indices,
		demand,
		mold_free,
		mode=mode,
	)
	components = _group_effective_pieces(pieces)
	# Setup consumes wall time at the factor that is active while setup runs; it
	# must be paid once for every physically disconnected campaign.
	minutes = sum(
		_effective_piece_minutes(
			component,
			get_datetime(component[0]["start"]) + timedelta(minutes=max(setup_minutes, 0)),
			component[-1]["end"],
		)
		for component in components
	)
	return minutes * max(flt(demand.get("hourly_rate")), 0) / 60


def _get_prebuild_cap(demand: dict[str, Any], qty: float) -> tuple[float, list[dict[str, str]]]:
	cap = qty
	checks = []
	limits = [
		("inventory_room_qty", "item_inventory_limit", _("Item inventory limit", context="Injection APS")),
		("warehouse_room_qty", "warehouse_capacity", _("Warehouse capacity", context="Injection APS")),
	]
	if not cint(demand.get("material_advisory_only")):
		limits.append(("material_ready_qty", "material_readiness", _("Material readiness", context="Injection APS")))
	for key, check_key, label in limits:
		value = demand.get(key)
		if value is None:
			checks.append(
				_check(
					"warning",
					check_key,
					_("{0} is not configured or proven.").format(label),
				)
			)
			continue
		value = max(flt(value), 0)
		cap = min(cap, value)
		checks.append(
			_check(
				"passed" if value > 0 else "blocked",
				check_key,
				_("{0} Prebuild limit is {1}.").format(label, f"{value:g}"),
			)
		)
	return max(cap, 0), checks


def _classify_capacity_bucket_indices(
	buckets: list[dict[str, Any]],
	due_time: datetime,
	*,
	due_granularity: str | None = None,
) -> tuple[list[int], list[int], list[int]]:
	"""Classify bucket portions against the same natural-day delivery cutoff.

	A 20:00-08:00 night shift can overlap two promise dates.  Its pre-midnight and
	post-midnight portions therefore appear in different mode lists and are clipped
	by :func:`_get_common_free_intervals` during allocation.  This avoids calling
	the whole shift JIT (or Prebuild) merely from its posting date.
	"""
	due_time = get_datetime(due_time)
	if (due_granularity or "Datetime") == "Date":
		due_date = getdate(due_time)
		due_start = get_datetime(f"{due_date} 00:00:00")
		due_end = due_start + timedelta(days=1)
		prebuild = []
		jit = []
		late = []
		for idx, bucket in enumerate(buckets):
			bucket_start = get_datetime(bucket.get("start"))
			bucket_end = get_datetime(bucket.get("end"))
			if bucket_start < due_start:
				prebuild.append(idx)
			if bucket_end > due_start and bucket_start < due_end:
				jit.append(idx)
			if bucket_end > due_end:
				late.append(idx)
		return prebuild, jit, late

	due_index = _find_due_bucket_index(buckets, due_time)
	if due_index is None:
		return [], [], []
	return list(range(due_index)), [due_index], list(range(due_index + 1, len(buckets)))


def _build_shared_resource_balances(demands: list[dict[str, Any]]) -> dict[str, Any]:
	"""Build one conservative balance for every shared finite resource.

	The same database availability is copied into every segment demand. Taking the
	minimum reported opening balance prevents either duplicate rows or a stale larger
	observation from increasing the resource pool.
	"""
	balances: dict[str, dict[str, float]] = {
		"inventory": {},
		"inventory_prebuild_consumed": {},
		"warehouse": {},
		"warehouse_stock_uoms": defaultdict(set),
		"warehouse_uom_conflicts": set(),
		"material": {},
	}
	for demand in demands:
		inventory_key = demand.get("inventory_resource_key")
		if inventory_key:
			balances["inventory_prebuild_consumed"].setdefault(inventory_key, 0.0)
		_register_resource_balance(
			balances["inventory"], inventory_key, demand.get("inventory_room_qty")
		)
		_register_resource_balance(
			balances["warehouse"], demand.get("warehouse_resource_key"), demand.get("warehouse_room_qty")
		)
		warehouse_key = demand.get("warehouse_resource_key")
		target_stock_uom = demand.get("target_stock_uom")
		if warehouse_key and target_stock_uom:
			balances["warehouse_stock_uoms"][warehouse_key].add(target_stock_uom)
		for requirement in demand.get("material_requirements") or []:
			_register_resource_balance(
				balances["material"], requirement.get("resource_key"), requirement.get("available_qty")
			)
	for warehouse_key, stock_uoms in balances["warehouse_stock_uoms"].items():
		if len(stock_uoms) > 1:
			# custom_aps_capacity_qty has no unit/conversion metadata. Two empty-warehouse
			# demand lookups can each report the same raw room in a different stock UOM,
			# so block shared Prebuild before either demand consumes that ambiguous pool.
			balances["warehouse"][warehouse_key] = 0.0
			balances["warehouse_uom_conflicts"].add(warehouse_key)
	return balances


def _register_resource_balance(target: dict[str, float], key: str | None, value: Any):
	if not key or value is None:
		return
	value = max(flt(value), 0)
	target[key] = min(target[key], value) if key in target else value


def _apply_shared_resource_limits(
	demand: dict[str, Any], resource_balances: dict[str, dict[str, float]]
) -> dict[str, Any]:
	limited = dict(demand)
	inventory_key = demand.get("inventory_resource_key")
	if inventory_key:
		limited["prior_shared_prebuild_qty"] = resource_balances["inventory_prebuild_consumed"].get(
			inventory_key, 0
		)
	warehouse_key = demand.get("warehouse_resource_key")
	if warehouse_key in resource_balances.get("warehouse_uom_conflicts", set()):
		limited["warehouse_stock_uom_conflict"] = 1
	for value_key, resource_type, resource_key_name in (
		("inventory_room_qty", "inventory", "inventory_resource_key"),
		("warehouse_room_qty", "warehouse", "warehouse_resource_key"),
	):
		resource_key = demand.get(resource_key_name)
		if resource_key in resource_balances[resource_type]:
			local_limit = demand.get(value_key)
			remaining_limit = resource_balances[resource_type][resource_key]
			limited[value_key] = (
				remaining_limit
				if local_limit is None
				else min(max(flt(local_limit), 0), remaining_limit)
			)

	material_caps = []
	for resource_key, per_unit in _material_requirements_by_resource(demand).items():
		available = resource_balances["material"].get(resource_key)
		if available is not None:
			material_caps.append(available / per_unit)
	if material_caps:
		shared_material_cap = min(material_caps)
		if demand.get("material_ready_qty") is None:
			limited["material_ready_qty"] = shared_material_cap
		else:
			limited["material_ready_qty"] = min(
				max(flt(demand.get("material_ready_qty")), 0), shared_material_cap
			)
	if limited.get("material_ready_qty") is not None:
		limited["material_schedulable_qty"] = max(flt(limited.get("material_ready_qty")), 0)
	return limited


def _consume_shared_resources(
	demand: dict[str, Any],
	result: dict[str, Any],
	resource_balances: dict[str, dict[str, float]],
):
	prebuild_qty = max(
		flt(
			demand.get("resource_prebuild_qty")
			if demand.get("resource_prebuild_qty") is not None
			else result.get("prebuild_qty")
		),
		0,
	)
	scheduled_qty = max(
		flt(
			demand.get("resource_material_consumption_qty")
			if demand.get("resource_material_consumption_qty") is not None
			else demand.get("resource_consumption_qty")
			if demand.get("resource_consumption_qty") is not None
			else sum(max(flt(row.get("qty")), 0) for row in result.get("allocations") or [])
		),
		0,
	)
	storage_qty = scheduled_qty if cint(demand.get("stock_retained")) else prebuild_qty
	inventory_key = demand.get("inventory_resource_key")
	if storage_qty > CAPACITY_TOLERANCE and inventory_key:
		resource_balances["inventory_prebuild_consumed"][inventory_key] = (
			resource_balances["inventory_prebuild_consumed"].get(inventory_key, 0) + storage_qty
		)
	for resource_type, resource_key_name in (
		("inventory", "inventory_resource_key"),
		("warehouse", "warehouse_resource_key"),
	):
		resource_key = demand.get(resource_key_name)
		if storage_qty > CAPACITY_TOLERANCE and resource_key in resource_balances[resource_type]:
			resource_balances[resource_type][resource_key] = max(
				resource_balances[resource_type][resource_key] - storage_qty, 0
			)
	for resource_key, per_unit in _material_requirements_by_resource(demand).items():
		if resource_key not in resource_balances["material"]:
			continue
		consumed = scheduled_qty * per_unit
		resource_balances["material"][resource_key] = max(
			resource_balances["material"][resource_key] - consumed, 0
		)


def _material_requirements_by_resource(demand: dict[str, Any]) -> dict[str, float]:
	requirements = defaultdict(float)
	for requirement in demand.get("material_requirements") or []:
		resource_key = requirement.get("resource_key")
		per_unit = max(flt(requirement.get("qty_per_unit")), 0)
		if resource_key and per_unit > CAPACITY_TOLERANCE:
			requirements[resource_key] += per_unit
	return dict(requirements)


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
	*,
	mode: str | None = None,
) -> list[tuple[datetime, datetime]]:
	intervals = list(bucket.get("free_intervals") or [])
	mold = demand.get("mould_reference")
	if mold:
		intervals = _intersect_interval_lists(
			intervals, mold_free.get((mold, bucket.get("key"))) or []
		)
	if mode and (demand.get("due_granularity") or "Datetime") == "Date":
		due_start = get_datetime(f"{getdate(demand.get('due_time'))} 00:00:00")
		due_end = due_start + timedelta(days=1)
		if mode == "Prebuild":
			intervals = [
				(start, min(get_datetime(end), due_start))
				for start, end in intervals
				if get_datetime(start) < due_start
			]
		elif mode == "JIT":
			intervals = [
				(max(get_datetime(start), due_start), min(get_datetime(end), due_end))
				for start, end in intervals
				if get_datetime(end) > due_start and get_datetime(start) < due_end
			]
		elif mode == "Late":
			intervals = [
				(max(get_datetime(start), due_end), get_datetime(end))
				for start, end in intervals
				if get_datetime(end) > due_end
			]
		intervals = [(start, end) for start, end in intervals if end > start]
	if mode == "Prebuild" and demand.get("prebuild_earliest_time"):
		earliest = get_datetime(demand.get("prebuild_earliest_time"))
		intervals = [
			(max(get_datetime(start), earliest), get_datetime(end))
			for start, end in intervals
			if get_datetime(end) > earliest
		]
		intervals = [(start, end) for start, end in intervals if end > start]
	return intervals


def _get_effective_common_free_intervals(
	bucket: dict[str, Any],
	demand: dict[str, Any],
	mold_free: dict[tuple[str, str], list[tuple[datetime, datetime]]],
	*,
	mode: str | None = None,
) -> list[tuple[datetime, datetime, float]]:
	"""Return allocatable wall-clock pieces together with their throughput factor."""
	common = _get_common_free_intervals(bucket, demand, mold_free, mode=mode)
	factor_rows = bucket.get("capacity_factor_intervals") or [
		{"start": start, "end": end, "factor": 1.0} for start, end in common
	]
	result = []
	for common_start, common_end in common:
		for factor_row in factor_rows:
			factor = max(min(flt(factor_row.get("factor")), 1), 0)
			if factor <= CAPACITY_TOLERANCE:
				continue
			start = max(get_datetime(common_start), get_datetime(factor_row.get("start")))
			end = min(get_datetime(common_end), get_datetime(factor_row.get("end")))
			if end > start:
				result.append((start, end, factor))
	return sorted(result, key=lambda row: (row[0], row[1], row[2]))


def _interval_touches_campaign(
	interval: tuple[Any, Any], occupied_intervals: list[tuple[Any, Any]]
) -> bool:
	start, end = (get_datetime(interval[0]), get_datetime(interval[1]))
	for occupied_start, occupied_end in occupied_intervals:
		occupied_start = get_datetime(occupied_start)
		occupied_end = get_datetime(occupied_end)
		if abs((start - occupied_end).total_seconds()) <= 0.000001:
			return True
		if abs((end - occupied_start).total_seconds()) <= 0.000001:
			return True
		if start < occupied_end and end > occupied_start:
			return True
	return False


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
	copy["capacity_factor_intervals"] = [
		{
			"start": get_datetime(factor_row.get("start")),
			"end": get_datetime(factor_row.get("end")),
			"factor": flt(factor_row.get("factor")),
		}
		for factor_row in (row.get("capacity_factor_intervals") or [])
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


def _downtime_capacity_factor(window: dict[str, Any]) -> float:
	return max(min(flt(window.get("available_capacity_percent")) / 100, 1), 0)


def _build_capacity_factor_intervals(
	free_intervals: list[tuple[Any, Any]],
	downtime_windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	"""Split free wall time at every downtime boundary and retain the exact factor."""
	result: list[dict[str, Any]] = []
	for free_start, free_end in free_intervals:
		free_start = get_datetime(free_start)
		free_end = get_datetime(free_end)
		boundaries = {free_start, free_end}
		applicable = []
		for window in downtime_windows:
			if not window.get("start_time") or not window.get("end_time"):
				continue
			window_start = max(free_start, get_datetime(window.get("start_time")))
			window_end = min(free_end, get_datetime(window.get("end_time")))
			if window_end <= window_start:
				continue
			applicable.append((window_start, window_end, _downtime_capacity_factor(window)))
			boundaries.update((window_start, window_end))
		points = sorted(boundaries)
		for start, end in zip(points, points[1:], strict=False):
			factor = min(
				(
					window_factor
					for window_start, window_end, window_factor in applicable
					if window_start < end and window_end > start
				),
				default=1.0,
			)
			if result and result[-1]["end"] == start and abs(flt(result[-1]["factor"]) - factor) <= CAPACITY_TOLERANCE:
				result[-1]["end"] = end
			else:
				result.append({"start": start, "end": end, "factor": factor})
	return result


def _downtime_applies_to_bucket(
	window: dict[str, Any],
	*,
	workstation: str | None,
	plant_floor: str | None,
	company: str | None,
) -> bool:
	"""Apply a downtime window only to its declared organizational scope."""
	if company and window.get("company") and window.get("company") != company:
		return False
	scope = window.get("scope") or ("Workstation" if window.get("workstation") else "Plant Floor")
	if scope == "Company":
		return True
	if scope == "Workstation":
		return bool(workstation) and window.get("workstation") == workstation
	window_floor = window.get("plant_floor")
	if window_floor:
		return bool(plant_floor) and window_floor == plant_floor
	return True


def _effective_capacity_minutes(
	free_intervals: list[tuple[Any, Any]],
	downtime_windows: list[dict[str, Any]],
) -> float:
	"""Integrate effective minutes without double-counting overlapping downtime."""
	total = 0.0
	for free_start, free_end in free_intervals:
		free_start = get_datetime(free_start)
		free_end = get_datetime(free_end)
		boundaries = {free_start, free_end}
		applicable = []
		for window in downtime_windows:
			if not window.get("start_time") or not window.get("end_time"):
				continue
			window_start = max(free_start, get_datetime(window.get("start_time")))
			window_end = min(free_end, get_datetime(window.get("end_time")))
			if window_end <= window_start:
				continue
			applicable.append((window_start, window_end, _downtime_capacity_factor(window)))
			boundaries.update((window_start, window_end))
		points = sorted(boundaries)
		for start, end in zip(points, points[1:], strict=False):
			factor = min(
				(
					window_factor
					for window_start, window_end, window_factor in applicable
					if window_start < end and window_end > start
				),
				default=1.0,
			)
			total += _minutes_between(start, end) * factor
	return max(total, 0)


def _check(status: str, key: str, message: str) -> dict[str, str]:
	return {"status": status, "key": key, "message": message}


def _blocked_demand_result(
	demand: dict[str, Any], qty: float, strategy: str, message: str, *, key: str = "capacity"
) -> dict[str, Any]:
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
		"checks": [_check("blocked", key, message)],
		"allocations": [],
		"fixed_commitment": 0,
	}


def canonical_json(value: Any) -> str:
	return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
	return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _lock_capacity_reservation_scope(run_doc, *, lock: bool) -> None:
	"""Serialize Apply decisions for all runs sharing company resources."""
	if lock and run_doc.company:
		frappe.db.sql("select name from `tabCompany` where name = %s for update", run_doc.company)


def _lock_and_refresh_capacity_run(run_doc, *, lock_company: bool = True):
	"""Lock the Run and overlay values returned by the locking/current read.

	Under MariaDB REPEATABLE READ, calling ``get_doc`` after ``FOR UPDATE`` can
	still return the transaction's older consistent-read snapshot (or a cached
	document).  Every capacity mutation therefore consumes the values returned by
	the locking statement itself.  The Company row is locked first so independent
	APS runs cannot commit competing shared-resource decisions.
	"""
	locked_company = run_doc.company
	if lock_company:
		_lock_capacity_reservation_scope(run_doc, lock=True)
	rows = frappe.db.sql(
		"""
		select
			name,
			company,
			plant_floor,
			horizon_start,
			horizon_end,
			status,
			approval_state,
			modified,
			capacity_balance_status,
			capacity_balance_fingerprint,
			capacity_balance_analysis_json,
			capacity_balance_confirmed_by,
			capacity_balance_confirmed_on,
			capacity_balance_applied_on
		from `tabAPS Planning Run`
		where name = %s
		for update
		""",
		run_doc.name,
		as_dict=True,
	)
	if not rows:
		frappe.throw(
			_("APS Planning Run {0} no longer exists.", context="Injection APS").format(
				run_doc.name
			),
			frappe.ValidationError,
		)
	current = rows[0]
	if current.get("company") != locked_company:
		frappe.throw(
			_(
				"APS run company changed while acquiring the capacity lock; retry the operation.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	for fieldname, value in current.items():
		setattr(run_doc, fieldname, value)
	selected_floors = frappe.db.sql(
		"""
		select plant_floor
		from `tabAPS Planning Run Plant Floor`
		where parent = %s
			and parenttype = 'APS Planning Run'
		order by idx, name
		for update
		""",
		run_doc.name,
		as_dict=True,
	)
	run_doc.set(
		"selected_plant_floors",
		[{"plant_floor": row.get("plant_floor")} for row in selected_floors if row.get("plant_floor")],
	)
	return run_doc


def _get_capacity_settings(*, current_read: bool = False) -> dict[str, Any]:
	"""Return APS capacity settings, optionally from a locking current read."""
	from injection_aps.services import planning

	settings = planning.get_settings_dict()
	if not current_read:
		return settings
	rows = frappe.db.sql(
		"""
		select field, value
		from `tabSingles`
		where doctype = 'APS Settings'
		order by field
		for update
		""",
		as_dict=True,
	)
	values = {row.get("field"): row.get("value") for row in rows if row.get("field")}
	string_defaults = {
		"default_production_strategy": "Auto Balance",
		"plant_floor_source_warehouse_field": "custom_default_source_warehouse",
		"plant_floor_fg_warehouse_field": "custom_default_finished_goods_warehouse",
		"item_safety_stock_field": "safety_stock",
	}
	for fieldname, default in string_defaults.items():
		if fieldname in values:
			settings[fieldname] = values.get(fieldname) or default
	if "default_max_prebuild_days" in values:
		settings["default_max_prebuild_days"] = cint(
			values.get("default_max_prebuild_days") or 7
		)
	if "high_cancellation_risk_percent" in values:
		settings["high_cancellation_risk_percent"] = flt(
			values.get("high_cancellation_risk_percent") or 60
		)
	return settings


def _lock_capacity_resource_bins(
	company: str | None,
	result_rows: list[dict[str, Any]] | dict[str, dict[str, Any]],
	demands: list[dict[str, Any]],
) -> None:
	"""Lock every Bin that can change a live material/FG/warehouse fingerprint."""
	if not company:
		return
	results = result_rows.values() if isinstance(result_rows, dict) else result_rows
	item_codes = {row.get("item_code") for row in results if row.get("item_code")}
	warehouses = set()
	for demand in demands or []:
		inventory_key = str(demand.get("inventory_resource_key") or "")
		if "|" in inventory_key:
			item_codes.add(inventory_key.split("|", 1)[1])
		if demand.get("warehouse_resource_key"):
			warehouses.add(demand.get("warehouse_resource_key"))
		for material in demand.get("material_requirements") or []:
			if material.get("item_code"):
				item_codes.add(material.get("item_code"))
			if material.get("warehouse"):
				warehouses.add(material.get("warehouse"))
	if not item_codes and not warehouses:
		return
	conditions = []
	params: dict[str, Any] = {"company": company}
	if item_codes:
		conditions.append("bin.item_code in %(item_codes)s")
		params["item_codes"] = tuple(sorted(item_codes))
	if warehouses:
		conditions.append("bin.warehouse in %(warehouses)s")
		params["warehouses"] = tuple(sorted(warehouses))
	frappe.db.sql(
		f"""
		select bin.name
		from `tabBin` bin
		inner join `tabWarehouse` wh on wh.name = bin.warehouse
		where wh.company = %(company)s
			and ({' or '.join(conditions)})
		order by bin.name
		for update
		""",
		params,
	)


def _lock_downtime_windows(windows: list[dict[str, Any]]) -> None:
	names = sorted({row.get("name") for row in windows if row.get("name")})
	if names:
		frappe.db.sql(
			"select name from `tabAPS Downtime Window` where name in %s order by name for update",
			(tuple(names),),
		)


def _get_current_active_downtime_windows(run_doc) -> list[dict[str, Any]]:
	"""Load relevant downtime with one locking/current read, including phantoms."""
	from injection_aps.services import planning

	if not frappe.db.exists("DocType", "APS Downtime Window"):
		return []
	statuses = tuple(planning.ACTIVE_DOWNTIME_STATUSES)
	plant_floors = planning._get_run_selected_plant_floors(run_doc)
	conditions = [
		"status in %(statuses)s",
		"company = %(company)s",
		"end_time > %(horizon_start)s",
		"start_time < %(horizon_end)s",
		"(ifnull(planning_run, '') = '' or planning_run = %(run)s)",
	]
	params: dict[str, Any] = {
		"statuses": statuses,
		"company": run_doc.company,
		"horizon_start": run_doc.horizon_start,
		"horizon_end": run_doc.horizon_end,
		"run": run_doc.name,
	}
	if plant_floors:
		conditions.append("(ifnull(plant_floor, '') = '' or plant_floor in %(plant_floors)s)")
		params["plant_floors"] = tuple(plant_floors)
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
		order by start_time, end_time, name
		for update
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)


def analyze_capacity_balance(run_name: str, persist: bool = True) -> dict[str, Any]:
	"""Build and optionally persist a shift-level balance proposal for an APS run."""
	from injection_aps.services import planning

	run_doc = frappe.get_doc("APS Planning Run", run_name)
	from injection_aps.services.v2_flags import is_v2_enabled

	v2_enabled = is_v2_enabled()
	result_rows, segment_rows = _get_run_balance_rows(run_name)
	if not result_rows:
		frappe.throw(_("APS run {0} has no schedule results to balance.").format(run_name))
	workstations = sorted({row.get("workstation") for row in segment_rows if row.get("workstation")})
	cross_result_rows, cross_segment_rows = _get_cross_run_applied_commitments(run_doc)
	reserved_demands = _build_cross_run_reservation_demands(
		run_doc, cross_result_rows, cross_segment_rows
	)
	fixed_intervals, mold_fixed_intervals = _get_fixed_execution_intervals(
		run_doc, segment_rows, cross_run_segments=cross_segment_rows
	)
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
		workstation_plant_floors=_get_workstation_plant_floor_map(segment_rows),
		company=run_doc.company,
	)
	demands = _build_segment_balance_demands(
		run_doc,
		result_rows,
		segment_rows,
		downtime_windows=downtime_windows,
	)
	material_advisory = []
	excluded_demands = []
	if v2_enabled:
		from injection_aps.services import constraint_resolution, horizon_status

		material_advisory = horizon_status.build_material_advisory([*reserved_demands, *demands])
		excluded_results = constraint_resolution.get_excluded_result_names(run_name)
		excluded_demands = [row for row in demands if row.get("result") in excluded_results]
		demands = [row for row in demands if row.get("result") not in excluded_results]
		demands = horizon_status.remove_material_constraints(demands)
		reserved_demands = horizon_status.remove_material_constraints(reserved_demands)
	finished_goods_stock_by_item = _get_physical_finished_goods_stock_map(
		run_doc.company,
		[*result_rows.values(), *cross_result_rows.values()],
	)
	stock_claim_conflicts = _find_finished_goods_stock_claim_conflicts(
		result_rows,
		cross_result_rows,
		demands,
		physical_stock_by_item=finished_goods_stock_by_item,
	)
	settings = planning.get_settings_dict()
	high_cancellation_risk_percent = flt(
		settings.get("high_cancellation_risk_percent") or 60
	)
	analysis = balance_capacity_nodes(
		demands,
		buckets,
		high_cancellation_risk_percent=high_cancellation_risk_percent,
		mold_blocked_intervals=mold_fixed_intervals,
		reserved_demands=reserved_demands,
	)
	_append_finished_goods_stock_claim_evidence(analysis, result_rows)
	_apply_finished_goods_stock_claim_blocks(analysis, stock_claim_conflicts)
	_apply_missing_net_requirement_evidence_blocks(
		analysis,
		result_rows,
		cross_result_rows,
	)
	if v2_enabled:
		analysis["excluded_demands"] = [
			{
				"result": row.get("result"), "segment": row.get("segment"),
				"planned_qty": max(flt(row.get("qty")), 0), "status": "Excluded",
			}
			for row in excluded_demands
		]
		analysis["approved_overrides"] = constraint_resolution.apply_approved_overrides_to_analysis(
			run_name, analysis
		)
		horizon_status.classify_v2_analysis(
			analysis,
			optional_admission_qty=flt(run_doc.get("total_selected_p1_qty")) + flt(run_doc.get("total_selected_p2_qty")),
			excluded_qty=sum(max(flt(row.get("qty")), 0) for row in excluded_demands),
		)
	source_snapshot = _build_source_snapshot(
		run_doc,
		result_rows,
		segment_rows,
		cross_result_rows=cross_result_rows,
		resource_demands=[*reserved_demands, *demands],
		downtime_windows=downtime_windows,
		fixed_intervals=fixed_intervals,
		mold_fixed_intervals=mold_fixed_intervals,
		high_cancellation_risk_percent=high_cancellation_risk_percent,
		finished_goods_stock_by_item=finished_goods_stock_by_item,
	)
	analysis["run"] = run_name
	analysis["source_fingerprint"] = fingerprint(source_snapshot)
	analysis["analysis_fingerprint"] = fingerprint(
		{"run": run_name, "source": source_snapshot, "demands": analysis.get("demands"), "buckets": analysis.get("buckets")}
	)
	analysis["source_snapshot"] = source_snapshot
	if v2_enabled:
		# Advisory evidence is deliberately attached after both fingerprints are
		# calculated. Raw-material changes therefore update only this display data.
		analysis["material_advisory"] = material_advisory
	if persist:
		_persist_capacity_analysis(run_doc, analysis)
		if v2_enabled:
			constraint_resolution.sync_from_analysis(run_doc, analysis)
	return analysis


def _remaining_finished_goods_stock_claim(result: dict[str, Any] | None) -> float:
	result = result or {}
	opening_claim = max(flt(result.get("available_stock_qty")), 0)
	remaining_demand = max(
		flt(result.get("demand_qty")) - max(flt(result.get("delivered_qty")), 0),
		0,
	)
	return min(opening_claim, remaining_demand)


def _persisted_net_requirement_evidence(result: dict[str, Any] | None) -> dict[str, Any]:
	"""Read durable stock coverage after the transient Net Requirement is rebuilt."""
	result = result or {}
	value = result.get("fulfillment_baseline_json")
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			value = {}
	if not isinstance(value, dict):
		value = {}
	evidence = value.get("net_requirement") or {}
	if not isinstance(evidence, dict):
		evidence = {}
	demand_qty = flt(evidence.get("demand_qty"))
	available_stock_qty = flt(evidence.get("available_stock_qty"))
	open_work_order_qty = flt(evidence.get("open_work_order_qty"))
	existing_work_order_policy = evidence.get("existing_work_order_policy") or ""
	complete = bool(
		cint(value.get("version")) >= 3
		and {
			"demand_qty",
			"available_stock_qty",
			"open_work_order_qty",
			"existing_work_order_policy",
		}.issubset(evidence)
		and _net_requirement_evidence_fields_are_valid(
			demand_qty,
			available_stock_qty,
			open_work_order_qty,
			existing_work_order_policy,
		)
	)
	return {
		"version": cint(value.get("version")),
		"complete": cint(complete),
		"demand_qty": max(demand_qty, 0),
		"available_stock_qty": max(available_stock_qty, 0),
		"open_work_order_qty": max(open_work_order_qty, 0),
		"existing_work_order_policy": existing_work_order_policy,
	}


def _net_requirement_quantities_are_valid(demand_qty: Any, available_stock_qty: Any) -> bool:
	demand_qty = flt(demand_qty)
	available_stock_qty = flt(available_stock_qty)
	return bool(
		demand_qty >= -CAPACITY_TOLERANCE
		and available_stock_qty >= -CAPACITY_TOLERANCE
		and available_stock_qty <= demand_qty + CAPACITY_TOLERANCE
	)


def _net_requirement_evidence_fields_are_valid(
	demand_qty: Any,
	available_stock_qty: Any,
	open_work_order_qty: Any,
	existing_work_order_policy: Any,
) -> bool:
	demand_qty = flt(demand_qty)
	available_stock_qty = flt(available_stock_qty)
	open_work_order_qty = flt(open_work_order_qty)
	return bool(
		_net_requirement_quantities_are_valid(demand_qty, available_stock_qty)
		and open_work_order_qty >= -CAPACITY_TOLERANCE
		and available_stock_qty + open_work_order_qty <= demand_qty + CAPACITY_TOLERANCE
		and existing_work_order_policy in {"Include", "Exclude"}
	)


def _missing_net_requirement_evidence_results(
	result_rows: dict[str, dict[str, Any]],
) -> list[str]:
	return sorted(
		row.get("name")
		for row in result_rows.values()
		if row.get("name") and not cint(row.get("net_requirement_evidence_complete"))
	)


def _assert_net_requirement_evidence_complete(
	result_rows: dict[str, dict[str, Any]],
	*,
	operation: str,
) -> None:
	missing = _missing_net_requirement_evidence_results(result_rows)
	if not missing:
		return
	frappe.throw(
		_(
			"APS Result(s) {0} no longer have their original Net Requirement and their historical stock evidence is incomplete. Rebuild net requirements and recalculate the affected run before {1}.",
			context="Injection APS",
		).format(", ".join(missing), operation),
		frappe.ValidationError,
	)


def _get_physical_finished_goods_stock_map(
	company: str | None,
	result_rows: list[dict[str, Any]] | dict[str, dict[str, Any]],
	*,
	current_read: bool = False,
) -> dict[str, float]:
	"""Use the same FG/reservation scope as net requirements for stock claims."""
	from injection_aps.services import planning

	results = list(result_rows.values()) if isinstance(result_rows, dict) else list(result_rows or [])
	demand_rows = []
	for result in results:
		evidence = _persisted_net_requirement_evidence(result)
		qty = max(flt(result.get("demand_qty")), evidence["demand_qty"])
		if not result.get("item_code") or qty <= CAPACITY_TOLERANCE:
			continue
		demand_rows.append(
			{
				"company": company,
				"customer": result.get("customer"),
				"sales_order": result.get("sales_order"),
				"sales_order_item": result.get("sales_order_item"),
				"item_code": result.get("item_code"),
				"demand_source": result.get("demand_source"),
				"qty": qty,
			}
		)
	stock_map = (
		_get_current_customer_claimable_stock_map(company, demand_rows)
		if current_read
		else planning._get_customer_claimable_stock_map(company, demand_rows=demand_rows)
	)
	item_codes = {row["item_code"] for row in demand_rows}
	return {item_code: max(flt(stock_map.get(item_code)), 0) for item_code in sorted(item_codes)}


def _get_current_customer_claimable_stock_map(
	company: str | None,
	demand_rows: list[dict[str, Any]],
) -> dict[str, float]:
	"""Reproduce APS ATP using current reads after locking all contributing rows."""
	if not company or not demand_rows:
		return {}
	item_codes = sorted({row.get("item_code") for row in demand_rows if row.get("item_code")})
	warehouses = _get_finished_goods_warehouses_for_update(company)
	if not item_codes or not warehouses:
		return {}
	bin_rows = frappe.db.sql(
		"""
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
			and ifnull(wh.disabled, 0) = 0
			and bin.warehouse in %(warehouses)s
			and bin.item_code in %(item_codes)s
		group by bin.item_code
		order by bin.item_code
		for update
		""",
		{
			"company": company,
			"warehouses": tuple(warehouses),
			"item_codes": tuple(item_codes),
		},
		as_dict=True,
	)
	reservation_credit = _get_current_sales_order_reservation_credit_map(
		company,
		demand_rows,
	)
	stock_map = {}
	for row in bin_rows:
		stock_reservation_qty = max(
			flt(row.get("reserved_qty")),
			flt(row.get("reserved_stock")),
		)
		credited_qty = min(
			max(flt(reservation_credit.get(row.get("item_code"))), 0),
			stock_reservation_qty,
		)
		external_reserved_qty = max(stock_reservation_qty - credited_qty, 0)
		production_reserved_qty = (
			flt(row.get("reserved_qty_for_production"))
			+ flt(row.get("reserved_qty_for_sub_contract"))
			+ flt(row.get("reserved_qty_for_production_plan"))
		)
		stock_map[row.get("item_code")] = max(
			flt(row.get("actual_qty"))
			- external_reserved_qty
			- production_reserved_qty,
			0,
		)

	settings = _get_capacity_settings(current_read=True)
	safety_field = settings.get("item_safety_stock_field")
	if not safety_field or not frappe.get_meta("Item").has_field(safety_field):
		return stock_map
	safety_rows = frappe.db.sql(
		f"""
		select name, `{safety_field}` as safety_stock_qty
		from `tabItem`
		where name in %s
		order by name
		for update
		""",
		(tuple(item_codes),),
		as_dict=True,
	)
	safety_by_item = {
		row.get("name"): max(flt(row.get("safety_stock_qty")), 0)
		for row in safety_rows
	}
	return {
		item_code: max(flt(stock_map.get(item_code)) - flt(safety_by_item.get(item_code)), 0)
		for item_code in item_codes
	}


def _get_current_sales_order_reservation_credit_map(
	company: str,
	demand_rows: list[dict[str, Any]],
) -> dict[str, float]:
	"""Return the exact APS-demand credit from locking Sales Order reads."""
	direct_demand = defaultdict(float)
	for row in demand_rows:
		if (row.get("demand_source") or "") == "Safety Stock":
			continue
		if row.get("sales_order") and row.get("item_code"):
			direct_demand[(row.get("sales_order"), row.get("item_code"))] += max(
				flt(row.get("qty")), 0
			)
	if not direct_demand:
		return {}
	sales_orders = sorted({key[0] for key in direct_demand})
	item_codes = sorted({key[1] for key in direct_demand})
	setting_rows = frappe.db.sql(
		"""
		select value
		from `tabSingles`
		where doctype = 'Selling Settings'
			and field = 'dont_reserve_sales_order_qty_on_sales_return'
		for update
		""",
		as_dict=True,
	)
	dont_reserve_on_return = cint(setting_rows[0].get("value")) if setting_rows else 0
	placeholders_so = ", ".join(["%s"] * len(sales_orders))
	placeholders_item = ", ".join(["%s"] * len(item_codes))
	reservation_rows = frappe.db.sql(
		f"""
		select
			so.name as sales_order,
			soi.item_code,
			so.transaction_date,
			soi.delivery_date,
			sum(
				case when ifnull(soi.qty, 0) > 0 then
					ifnull(soi.stock_qty, 0)
					* greatest(
						ifnull(soi.qty, 0)
						- ifnull(soi.delivered_qty, 0)
						- if(%s, ifnull(soi.returned_qty, 0), 0),
						0
					)
					/ ifnull(soi.qty, 0)
				else 0 end
			) as reserved_qty
		from `tabSales Order Item` soi
		inner join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1
			and ifnull(so.status, '') not in ('On Hold', 'Closed')
			and ifnull(soi.warehouse, '') != ''
			and so.company = %s
			and so.name in ({placeholders_so})
			and soi.item_code in ({placeholders_item})
		group by so.name, soi.item_code, so.transaction_date, soi.delivery_date
		having reserved_qty > 0
		order by soi.delivery_date, so.transaction_date, so.name
		for update
		""",
		[dont_reserve_on_return, company, *sales_orders, *item_codes],
		as_dict=True,
	)
	remaining = defaultdict(float, direct_demand)
	credit = defaultdict(float)
	for row in reservation_rows:
		key = (row.get("sales_order"), row.get("item_code"))
		allocated = min(max(flt(row.get("reserved_qty")), 0), max(remaining[key], 0))
		if allocated <= CAPACITY_TOLERANCE:
			continue
		credit[row.get("item_code")] += allocated
		remaining[key] -= allocated
	return dict(credit)


def _find_finished_goods_stock_claim_conflicts(
	result_rows: dict[str, dict[str, Any]],
	cross_result_rows: dict[str, dict[str, Any]],
	demands: list[dict[str, Any]],
	*,
	physical_stock_by_item: dict[str, float] | None = None,
) -> dict[str, float]:
	"""Detect physical FG stock promised by more than one active APS run.

	Net requirements consume stock before creating production segments.  That
	coverage therefore cannot be represented as a machine demand.  We keep it as
	a run-level claim and block a newer plan if the same physical balance has
	already been promised by another Applied run.
	"""
	physical_by_item: dict[str, float] = {
		item_code: max(flt(qty), 0)
		for item_code, qty in (physical_stock_by_item or {}).items()
	}
	if physical_stock_by_item is None:
		for demand in demands:
			result = result_rows.get(demand.get("result")) or {}
			item_code = result.get("item_code")
			if not item_code:
				continue
			observed = max(flt(demand.get("current_inventory_qty")), 0)
			physical_by_item[item_code] = max(physical_by_item.get(item_code, 0), observed)
	reserved_by_item: dict[str, float] = defaultdict(float)
	for result in cross_result_rows.values():
		if result.get("item_code"):
			reserved_by_item[result.get("item_code")] += _remaining_finished_goods_stock_claim(result)
	remaining_by_item = {
		item_code: max(qty - reserved_by_item.get(item_code, 0), 0)
		for item_code, qty in physical_by_item.items()
	}
	conflicts: dict[str, float] = {}
	for result in sorted(
		result_rows.values(),
		key=lambda row: (
			str(row.get("requested_date") or ""),
			str(row.get("name") or ""),
		),
	):
		claim = _remaining_finished_goods_stock_claim(result)
		if claim <= CAPACITY_TOLERANCE:
			continue
		item_code = result.get("item_code")
		available = max(remaining_by_item.get(item_code, 0), 0)
		consumed = min(claim, available)
		remaining_by_item[item_code] = max(available - consumed, 0)
		shortage = max(claim - consumed, 0)
		if shortage > CAPACITY_TOLERANCE:
			conflicts[result.get("name")] = shortage
	return conflicts


def _apply_finished_goods_stock_claim_blocks(
	analysis: dict[str, Any], conflicts: dict[str, float]
) -> None:
	analysis["resource_blocks"] = []
	analysis["summary"]["stock_claim_blocked_results"] = len(conflicts)
	if not conflicts:
		return
	blocked_with_demands = set()
	for demand in analysis.get("demands") or []:
		shortage = flt(conflicts.get(demand.get("result")))
		if shortage <= CAPACITY_TOLERANCE:
			continue
		demand["status"] = "Blocked"
		blocked_with_demands.add(demand.get("result"))
		demand.setdefault("checks", []).append(
			_check(
				"blocked",
				"cross_run_finished_goods_stock",
				_(
					"Another active APS run already claims the finished-goods stock used by this result; rebuild net requirements. Uncovered stock claim: {0}.",
					context="Injection APS",
				).format(f"{shortage:g}"),
			)
		)
	for result_name, shortage in sorted(conflicts.items()):
		if result_name in blocked_with_demands:
			continue
		resource_block = {
				"result": result_name,
				"key": "cross_run_finished_goods_stock",
				"shortage_qty": flt(shortage),
				"message": _(
					"Another active APS run already claims the finished-goods stock used by this result; rebuild net requirements. Uncovered stock claim: {0}.",
					context="Injection APS",
				).format(f"{flt(shortage):g}"),
			}
		analysis["resource_blocks"].append(resource_block)
		analysis.setdefault("demands", []).append(
			{
				"key": f"STOCK-CLAIM|{result_name}",
				"result": result_name,
				"segment": None,
				"strategy": "Stock Claim",
				"planned_qty": 0,
				"prebuild_qty": 0,
				"jit_qty": 0,
				"late_qty": 0,
				"unscheduled_qty": 0,
				"early_days": 0,
				"projected_peak_inventory_qty": 0,
				"late_qty_before_balance": 0,
				"late_qty_after_balance": 0,
				"requires_confirmation": 0,
				"confirmation_reasons": [],
				"status": "Blocked",
				"checks": [
					_check("blocked", resource_block["key"], resource_block["message"])
				],
				"allocations": [],
				"fixed_commitment": 1,
				"stock_claim_only": 1,
			}
		)
	analysis["summary"]["demand_count"] = len(analysis.get("demands") or [])
	analysis["summary"]["blocked_demands"] = sum(
		1 for row in analysis.get("demands") or [] if row.get("status") == "Blocked"
	)


def _append_finished_goods_stock_claim_evidence(
	analysis: dict[str, Any],
	result_rows: dict[str, dict[str, Any]],
) -> None:
	"""Persist a Result-level finite-stock node even when no machine segment exists."""
	existing_results = {
		row.get("result") for row in analysis.get("demands") or [] if row.get("result")
	}
	for result in sorted(
		result_rows.values(),
		key=lambda row: (str(row.get("requested_date") or ""), str(row.get("name") or "")),
	):
		result_name = result.get("name")
		claim_qty = _remaining_finished_goods_stock_claim(result)
		if not result_name or result_name in existing_results or claim_qty <= CAPACITY_TOLERANCE:
			continue
		analysis.setdefault("demands", []).append(
			{
				"key": f"STOCK-CLAIM|{result_name}",
				"result": result_name,
				"segment": None,
				"strategy": "Stock Claim",
				"planned_qty": 0,
				"prebuild_qty": 0,
				"jit_qty": 0,
				"late_qty": 0,
				"unscheduled_qty": 0,
				"early_days": 0,
				"projected_peak_inventory_qty": claim_qty,
				"late_qty_before_balance": 0,
				"late_qty_after_balance": 0,
				"requires_confirmation": 0,
				"confirmation_reasons": [],
				"status": "Balanced",
				"checks": [
					_check(
						"passed",
						"finished_goods_stock_claim",
						_(
							"This result reserves {0} of finite finished-goods stock across APS runs.",
							context="Injection APS",
						).format(f"{claim_qty:g}"),
					)
				],
				"allocations": [],
				"fixed_commitment": 1,
				"stock_claim_only": 1,
			}
		)
		existing_results.add(result_name)
	analysis["summary"]["demand_count"] = len(analysis.get("demands") or [])


def _apply_missing_net_requirement_evidence_blocks(
	analysis: dict[str, Any],
	result_rows: dict[str, dict[str, Any]],
	cross_result_rows: dict[str, dict[str, Any]],
) -> None:
	"""Fail closed when a deleted Net Requirement has no durable v3 quantities."""
	current_missing = _missing_net_requirement_evidence_results(result_rows)
	cross_missing = _missing_net_requirement_evidence_results(cross_result_rows)
	missing = [("current", name) for name in current_missing] + [
		("cross-run", name) for name in cross_missing
	]
	analysis.setdefault("summary", {})["missing_net_requirement_evidence_results"] = len(missing)
	if not missing:
		return

	current_fallback = next(iter(sorted(result_rows)), None)
	for scope, missing_result in missing:
		target_result = missing_result if scope == "current" else current_fallback
		message = _(
			"APS Result {0} has no live Net Requirement and no complete v3 stock-coverage evidence. Rebuild net requirements and recalculate the affected run before capacity can be applied or released.",
			context="Injection APS",
		).format(missing_result)
		key = f"missing_net_requirement_evidence|{scope}|{missing_result}"
		matched = False
		for demand in analysis.get("demands") or []:
			if demand.get("result") != target_result:
				continue
			demand["status"] = "Blocked"
			demand.setdefault("checks", []).append(_check("blocked", key, message))
			matched = True
		if not matched and target_result:
			analysis.setdefault("demands", []).append(
				{
					"key": f"NET-EVIDENCE|{scope}|{missing_result}",
					"result": target_result,
					"segment": None,
					"strategy": "Historical Stock Evidence",
					"planned_qty": 0,
					"prebuild_qty": 0,
					"jit_qty": 0,
					"late_qty": 0,
					"unscheduled_qty": 0,
					"early_days": 0,
					"projected_peak_inventory_qty": 0,
					"late_qty_before_balance": 0,
					"late_qty_after_balance": 0,
					"requires_confirmation": 0,
					"confirmation_reasons": [],
					"status": "Blocked",
					"checks": [_check("blocked", key, message)],
					"allocations": [],
					"fixed_commitment": 1,
					"historical_stock_evidence_only": 1,
				}
			)
		analysis.setdefault("resource_blocks", []).append(
			{
				"result": missing_result,
				"scope": scope,
				"key": key,
				"message": message,
			}
		)
	analysis["summary"]["demand_count"] = len(analysis.get("demands") or [])
	analysis["summary"]["blocked_demands"] = sum(
		1 for row in analysis.get("demands") or [] if row.get("status") == "Blocked"
	)


def confirm_capacity_balance(run_name: str) -> dict[str, Any]:
	from injection_aps.services.v2_flags import is_v2_enabled

	v2_enabled = is_v2_enabled()
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	run_doc = _lock_and_refresh_capacity_run(run_doc)
	if run_doc.capacity_balance_status in ("Applied", "Applied with Exceptions"):
		return {
			"run": run_name,
			"status": run_doc.capacity_balance_status,
			"confirmed_by": run_doc.capacity_balance_confirmed_by,
			"confirmed_on": run_doc.capacity_balance_confirmed_on,
			"idempotent_replay": 1,
		}
	allowed_statuses = (
		("Ready", "Acknowledgment Required")
		if v2_enabled else ("Suggestion Ready", "Confirmation Required")
	)
	if run_doc.capacity_balance_status not in allowed_statuses:
		frappe.throw(_("Analyze capacity balance before PMC confirmation."), frappe.ValidationError)
	if not run_doc.capacity_balance_fingerprint or not run_doc.capacity_balance_analysis_json:
		frappe.throw(_("Capacity analysis evidence is missing; analyze the run again."), frappe.ValidationError)
	result_rows, _segment_rows = _get_run_balance_rows(
		run_name,
		lock_rows=True,
		lock_linked_work_orders=True,
	)
	cross_result_rows, _cross_segment_rows = _get_cross_run_applied_commitments(
		run_doc,
		lock_rows=True,
	)
	_assert_net_requirement_evidence_complete(
		result_rows,
		operation="PMC capacity confirmation",
	)
	_assert_net_requirement_evidence_complete(
		cross_result_rows,
		operation="PMC capacity confirmation",
	)
	analysis = _load_capacity_analysis(run_doc)
	confirmed_on = now_datetime()
	analysis["confirmation_fingerprint"] = run_doc.capacity_balance_fingerprint
	analysis["confirmed_by"] = frappe.session.user
	analysis["confirmed_on"] = confirmed_on
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		{
			"capacity_balance_confirmed_by": frappe.session.user,
			"capacity_balance_confirmed_on": confirmed_on,
			"capacity_balance_analysis_json": json.dumps(
				analysis, default=str, ensure_ascii=False, indent=2
			),
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
	from injection_aps.services.v2_flags import is_v2_enabled

	v2_enabled = is_v2_enabled()

	save_point = "aps_capacity_apply_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		run_doc = frappe.get_doc("APS Planning Run", run_name)
		run_doc = _lock_and_refresh_capacity_run(run_doc)
		if run_doc.capacity_balance_status in ("Applied", "Applied with Exceptions"):
			analysis = _load_capacity_analysis(run_doc)
			_assert_applied_plan_snapshot_current(run_doc, analysis)
			frappe.db.release_savepoint(save_point)
			return {
				"run": run_name,
				"status": run_doc.capacity_balance_status,
				"analysis_fingerprint": run_doc.capacity_balance_fingerprint,
				"summary": analysis.get("summary") or {},
				"idempotent_replay": 1,
			}
		allowed_statuses = (
			("Ready", "Acknowledgment Required")
			if v2_enabled else ("Suggestion Ready", "Confirmation Required")
		)
		if run_doc.capacity_balance_status not in allowed_statuses:
			frappe.throw(_("Analyze a non-blocked capacity proposal before Apply."), frappe.ValidationError)
		analysis = _load_capacity_analysis(run_doc)
		if v2_enabled:
			from injection_aps.services import constraint_resolution

			constraint_resolution.assert_analysis_overrides_current(run_name, analysis)
		if analysis.get("summary", {}).get("blocked_demands") or (
			not v2_enabled and analysis.get("summary", {}).get("unscheduled_qty")
		):
			frappe.throw(_("Blocked or unscheduled capacity proposals cannot be applied."), frappe.ValidationError)
		requires_confirmation = bool(analysis.get("summary", {}).get("requires_confirmation"))
		confirmation_matches = bool(
			run_doc.capacity_balance_confirmed_by
			and analysis.get("confirmation_fingerprint") == run_doc.capacity_balance_fingerprint
		)
		if requires_confirmation and not confirmation_matches:
			if not pmc_confirmed:
				frappe.throw(_("PMC confirmation is required before applying this capacity proposal."), frappe.ValidationError)
			confirmed_on = now_datetime()
			analysis["confirmation_fingerprint"] = run_doc.capacity_balance_fingerprint
			analysis["confirmed_by"] = frappe.session.user
			analysis["confirmed_on"] = confirmed_on
			frappe.db.set_value(
				"APS Planning Run",
				run_name,
				{
					"capacity_balance_confirmed_by": frappe.session.user,
					"capacity_balance_confirmed_on": confirmed_on,
					"capacity_balance_analysis_json": json.dumps(
						analysis, default=str, ensure_ascii=False, indent=2
					),
				},
				update_modified=False,
			)
		result_rows, segment_rows = _get_run_balance_rows(
			run_name, lock_rows=True, lock_linked_work_orders=True
		)
		cross_result_rows, cross_segment_rows = _get_cross_run_applied_commitments(
			run_doc, lock_rows=True
		)
		_assert_net_requirement_evidence_complete(
			result_rows,
			operation="capacity Apply",
		)
		_assert_net_requirement_evidence_complete(
			cross_result_rows,
			operation="capacity Apply",
		)
		current_reserved_demands = _build_cross_run_reservation_demands(
			run_doc,
			cross_result_rows,
			cross_segment_rows,
			lock_existing_work_orders=True,
			current_read_resources=True,
			include_material_resources=not v2_enabled,
		)
		current_fixed_intervals, current_mold_fixed_intervals = _get_fixed_execution_intervals(
			run_doc,
			segment_rows,
			cross_run_segments=cross_segment_rows,
			lock_external_rows=True,
		)
		current_downtime_windows = _get_current_active_downtime_windows(run_doc)
		current_resource_demands = _build_segment_balance_demands(
			run_doc,
			result_rows,
			segment_rows,
			downtime_windows=current_downtime_windows,
			lock_existing_work_orders=True,
			current_read_resources=True,
			include_material_resources=not v2_enabled,
		)
		if v2_enabled:
			from injection_aps.services import constraint_resolution, horizon_status

			excluded_results = constraint_resolution.get_excluded_result_names(run_name)
			current_resource_demands = horizon_status.remove_material_constraints(
				[row for row in current_resource_demands if row.get("result") not in excluded_results]
			)
			current_reserved_demands = horizon_status.remove_material_constraints(current_reserved_demands)
		_lock_capacity_resource_bins(
			run_doc.company,
			[*result_rows.values(), *cross_result_rows.values()],
			[*current_reserved_demands, *current_resource_demands],
		)
		# Refresh every quantity after acquiring Bin locks. Standard Stock Entry
		# transactions do not participate in the APS Company-row lock.
		current_reserved_demands = _build_cross_run_reservation_demands(
			run_doc,
			cross_result_rows,
			cross_segment_rows,
			lock_existing_work_orders=True,
			current_read_resources=True,
			include_material_resources=not v2_enabled,
		)
		current_resource_demands = _build_segment_balance_demands(
			run_doc,
			result_rows,
			segment_rows,
			downtime_windows=current_downtime_windows,
			lock_existing_work_orders=True,
			current_read_resources=True,
			include_material_resources=not v2_enabled,
		)
		if v2_enabled:
			current_resource_demands = horizon_status.remove_material_constraints(
				[row for row in current_resource_demands if row.get("result") not in excluded_results]
			)
			current_reserved_demands = horizon_status.remove_material_constraints(current_reserved_demands)
		current_finished_goods_stock_by_item = _get_physical_finished_goods_stock_map(
			run_doc.company,
			[*result_rows.values(), *cross_result_rows.values()],
			current_read=True,
		)
		current_snapshot = _build_source_snapshot(
			run_doc,
			result_rows,
			segment_rows,
			cross_result_rows=cross_result_rows,
			resource_demands=[*current_reserved_demands, *current_resource_demands],
			downtime_windows=current_downtime_windows,
			fixed_intervals=current_fixed_intervals,
			mold_fixed_intervals=current_mold_fixed_intervals,
			high_cancellation_risk_percent=flt(
				_get_capacity_settings(current_read=True).get(
					"high_cancellation_risk_percent"
				)
				or 60
			),
			finished_goods_stock_by_item=current_finished_goods_stock_by_item,
		)
		if fingerprint(current_snapshot) != analysis.get("source_fingerprint"):
			frappe.throw(
				_(
					"The plan or its shared material, inventory, or warehouse capacity changed "
					"after capacity analysis. Analyze again before Apply."
				),
				frappe.ValidationError,
			)
		mutation = _apply_capacity_allocations(analysis, allow_partial=v2_enabled)
		if v2_enabled:
			mutation["excluded_results"] = _apply_excluded_results(run_name, analysis)
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
			details = "; ".join(
				str(row.get("message") or row)
				for row in (consistency_summary.get("errors") or [])[:5]
			)
			frappe.throw(
				_("Capacity balance failed plan consistency with {0} error(s): {1}").format(
					len(consistency_summary.get("errors") or []),
					details or _("No error detail was returned."),
				),
				frappe.ValidationError,
			)
		fulfillment = availability.recalculate_run_fulfillment(run_name)
		# Bind the Applied decision to the resulting physical plan.  This snapshot
		# excludes execution progress and live stock, so network retries remain
		# idempotent while later quantity/date/machine/lineage edits are rejected.
		post_result_rows, post_segment_rows = _get_run_balance_rows(
			run_name,
			lock_rows=True,
			lock_linked_work_orders=True,
		)
		analysis["applied_plan_fingerprint"] = _build_applied_plan_fingerprint(
			run_doc,
			post_result_rows,
			post_segment_rows,
			current_read_resources=True,
		)
		analysis["applied_resource_fingerprint"] = _build_live_capacity_resource_fingerprint(
			run_doc,
			post_result_rows,
			post_segment_rows,
			lock_external_rows=True,
		)
		applied_on = now_datetime()
		applied_status = (
			"Applied with Exceptions"
			if v2_enabled and (analysis.get("excluded_demands") or []) else "Applied"
		)
		frappe.db.set_value(
			"APS Planning Run",
			run_name,
			{
				"capacity_balance_status": applied_status,
				"capacity_balance_applied_on": applied_on,
				"capacity_balance_analysis_json": json.dumps(
					analysis, default=str, ensure_ascii=False, indent=2
				),
			},
			update_modified=False,
		)
		frappe.db.release_savepoint(save_point)
		return {
			"run": run_name,
			"status": applied_status,
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


def finalize_v2_solver_apply_evidence(
	run_name: str,
	*,
	applied_status: str,
) -> dict[str, Any]:
	"""Bind an already-written V2 solver schedule to formal release evidence.

	The solver owns the schedule mutation, while this service owns the shared
	capacity release contract.  Keeping the evidence construction here guarantees
	the V2 and Legacy Apply paths use the same consistency, overlap, fulfillment,
	plan-fingerprint and live-resource guards.
	"""
	from injection_aps.services import availability, consistency, planning

	run_doc = frappe.get_doc("APS Planning Run", run_name)
	analysis = _load_capacity_analysis(run_doc)
	result_rows, segment_rows = _get_run_balance_rows(
		run_name,
		lock_rows=True,
		lock_linked_work_orders=True,
	)
	_assert_net_requirement_evidence_complete(result_rows, operation="V2 solver Apply")
	overlap_summary = planning._validate_run_segment_overlaps(
		run_name,
		persist_exceptions=True,
	)
	mold_overlap_summary = planning._validate_run_mold_overlaps(
		run_name,
		persist_exceptions=True,
	)
	if overlap_summary.get("count") or mold_overlap_summary.get("count"):
		frappe.throw(
			_("V2 solver Apply created {0} workstation overlap(s) and {1} mold overlap(s).").format(
				overlap_summary.get("count") or 0,
				mold_overlap_summary.get("count") or 0,
			),
			frappe.ValidationError,
		)
	consistency_summary = consistency.recalculate_plan_consistency(
		run_name,
		reason="V2 solver schedule applied",
	)
	if not consistency_summary.get("valid"):
		details = "; ".join(
			str(row.get("message") or row)
			for row in (consistency_summary.get("errors") or [])[:5]
		)
		frappe.throw(
			_("V2 solver Apply failed plan consistency with {0} error(s): {1}").format(
				len(consistency_summary.get("errors") or []),
				details or _("No error detail was returned."),
			),
			frappe.ValidationError,
		)
	fulfillment = availability.recalculate_run_fulfillment(run_name)
	post_result_rows, post_segment_rows = _get_run_balance_rows(
		run_name,
		lock_rows=True,
		lock_linked_work_orders=True,
	)
	_assert_net_requirement_evidence_complete(
		post_result_rows,
		operation="V2 solver applied-plan validation",
	)
	analysis["applied_plan_fingerprint"] = _build_applied_plan_fingerprint(
		run_doc,
		post_result_rows,
		post_segment_rows,
		current_read_resources=True,
	)
	analysis["applied_resource_fingerprint"] = _build_live_capacity_resource_fingerprint(
		run_doc,
		post_result_rows,
		post_segment_rows,
		lock_external_rows=True,
	)
	applied_on = now_datetime()
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		{
			"capacity_balance_status": applied_status,
			"capacity_balance_applied_on": applied_on,
			"capacity_balance_analysis_json": json.dumps(
				analysis,
				default=str,
				ensure_ascii=False,
				indent=2,
			),
		},
		update_modified=False,
	)
	return {
		"analysis": analysis,
		"overlap_count": overlap_summary.get("count") or 0,
		"mold_overlap_count": mold_overlap_summary.get("count") or 0,
		"consistency": consistency_summary,
		"fulfillment": fulfillment,
		"applied_on": applied_on,
	}


def _load_capacity_analysis(run_doc) -> dict[str, Any]:
	try:
		analysis = json.loads(run_doc.capacity_balance_analysis_json or "{}")
	except (TypeError, ValueError):
		analysis = {}
	if not analysis or analysis.get("analysis_fingerprint") != run_doc.capacity_balance_fingerprint:
		frappe.throw(_("Capacity analysis is missing or its fingerprint does not match."), frappe.ValidationError)
	return analysis


def invalidate_capacity_balance(run_name: str) -> None:
	"""Invalidate every approval artefact that belongs to an older plan state."""
	if not run_name:
		return
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		{
			"capacity_balance_status": "Not Analyzed",
			"capacity_balance_analyzed_on": None,
			"capacity_balance_confirmed_by": None,
			"capacity_balance_confirmed_on": None,
			"capacity_balance_applied_on": None,
			"capacity_balance_fingerprint": None,
			"capacity_balance_analysis_json": None,
			"total_prebuild_qty": 0,
			"total_jit_qty": 0,
			"constraint_resolution_count": 0,
		},
		update_modified=False,
	)
	frappe.db.sql(
		"""
		update `tabAPS Schedule Result`
		set capacity_balance_status = 'Not Analyzed',
			capacity_balance_requires_confirmation = 0,
			capacity_balance_details = '',
			prebuild_qty = 0,
			jit_qty = 0,
			early_days = 0,
			projected_peak_inventory_qty = 0,
			late_qty_before_balance = 0,
			late_qty_after_balance = 0
		where planning_run = %s
		""",
		(run_name,),
	)


def assert_applied_capacity_current(run_name: str, *, lock_rows: bool = False) -> dict[str, Any]:
	"""Reject approval/replay when the physical plan changed after Apply."""
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	if lock_rows:
		# Formal release gates deliberately enter here before taking the Run row,
		# preserving the same Company -> Run order used by capacity Apply.
		run_doc = _lock_and_refresh_capacity_run(run_doc)
	if run_doc.capacity_balance_status not in ("Applied", "Applied with Exceptions"):
		frappe.throw(
			_("Apply the analyzed capacity balance before confirming this planning run."),
			frappe.ValidationError,
		)
	analysis = _load_capacity_analysis(run_doc)
	from injection_aps.services.v2_flags import is_v2_enabled

	if is_v2_enabled():
		from injection_aps.services import constraint_resolution

		constraint_resolution.assert_analysis_overrides_current(run_name, analysis)
	_assert_applied_plan_snapshot_current(run_doc, analysis, lock_rows=lock_rows)
	expected_resource_fingerprint = analysis.get("applied_resource_fingerprint")
	if not expected_resource_fingerprint:
		frappe.throw(
			_(
				"Applied capacity evidence predates the live resource guard. Analyze and Apply again.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	result_rows, segment_rows = _get_run_balance_rows(
		run_name,
		lock_rows=lock_rows,
		lock_linked_work_orders=lock_rows,
	)
	_assert_net_requirement_evidence_complete(
		result_rows,
		operation="formal release",
	)
	current_resource_fingerprint = _build_live_capacity_resource_fingerprint(
		run_doc,
		result_rows,
		segment_rows,
		lock_external_rows=lock_rows,
	)
	if current_resource_fingerprint != expected_resource_fingerprint:
		frappe.throw(
			_(
				"Shared material, finished-goods stock, warehouse capacity, downtime, or Work Order state changed after capacity Apply. Analyze and Apply again.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	return analysis


def rebind_applied_capacity_after_run_approval(run_name: str) -> dict[str, Any]:
	"""Rebind Applied evidence after the trusted Pending -> Approved transition.

	Approval deterministically locks the already-Applied APS segments. That changes
	the plan fingerprint and makes those same segments enter the fixed-interval
	resource snapshot, even though no quantity, machine, mold or time changed. The
	approval gate has already locked and validated the Applied evidence and its live
	resource rows in the current transaction, so it is safe to bind that one internal
	state transition here. Release mutations continue to invalidate the evidence.
	"""
	run_doc = _lock_and_refresh_capacity_run(
		frappe.get_doc("APS Planning Run", run_name),
		lock_company=False,
	)
	if run_doc.approval_state != "Approved":
		frappe.throw(
			_("Capacity evidence can only be rebound after the planning run is approved."),
			frappe.ValidationError,
		)
	if run_doc.capacity_balance_status not in ("Applied", "Applied with Exceptions"):
		frappe.throw(
			_("Apply the analyzed capacity balance before approving this planning run."),
			frappe.ValidationError,
		)
	analysis = _load_capacity_analysis(run_doc)
	result_rows, segment_rows = _get_run_balance_rows(
		run_name,
		lock_rows=True,
		lock_linked_work_orders=True,
	)
	_assert_net_requirement_evidence_complete(
		result_rows,
		operation="planning run approval",
	)
	analysis["applied_plan_fingerprint"] = _build_applied_plan_fingerprint(
		run_doc,
		result_rows,
		segment_rows,
		current_read_resources=True,
	)
	analysis["applied_resource_fingerprint"] = _build_live_capacity_resource_fingerprint(
		run_doc,
		result_rows,
		segment_rows,
		lock_external_rows=True,
	)
	rebound_on = now_datetime()
	analysis["run_approval_rebound_on"] = rebound_on
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		"capacity_balance_analysis_json",
		json.dumps(analysis, default=str, ensure_ascii=False, indent=2),
		update_modified=False,
	)
	return {
		"run": run_name,
		"status": run_doc.capacity_balance_status,
		"rebound_on": rebound_on,
		"applied_plan_fingerprint": analysis["applied_plan_fingerprint"],
		"applied_resource_fingerprint": analysis["applied_resource_fingerprint"],
	}


def rebind_applied_capacity_resources_after_release(
	run_name: str,
	*,
	reason: str,
	lock_rows: bool = True,
) -> dict[str, Any]:
	"""Invalidate capacity after release instead of accepting an unproven rebind.

	A release can legitimately change Work Order reservations and segment lineage,
	but it can race with unrelated stock, BOM, warehouse, downtime or settings
	changes.  A single post-release fingerprint cannot prove which changes belong to
	the release, so treating the whole fingerprint as trusted would silently absorb
	those unrelated changes.  The safe contract is to retain the released documents
	and require a fresh Analyze/Apply before any subsequent formal release.
	"""
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	# This function mutates approval state; keep the parameter for caller
	# compatibility but never permit an unlocked invalidation path.
	run_doc = _lock_and_refresh_capacity_run(run_doc, lock_company=False)
	if run_doc.capacity_balance_status not in ("Applied", "Applied with Exceptions"):
		frappe.throw(
			_(
				"Apply the analyzed capacity balance before rebinding release resources.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	analysis = _load_capacity_analysis(run_doc)
	_assert_applied_plan_snapshot_current(run_doc, analysis, lock_rows=True)
	timestamp = now_datetime()
	previous = analysis.get("applied_resource_fingerprint")
	invalidate_capacity_balance(run_name)
	return {
		"run": run_name,
		"status": "Not Analyzed",
		"previous_fingerprint": previous,
		"resource_fingerprint": None,
		"reason": reason,
		"invalidated_on": timestamp,
		"requires_reanalysis": 1,
	}


def _assert_applied_plan_snapshot_current(
	run_doc,
	analysis: dict[str, Any],
	*,
	lock_rows: bool = True,
) -> None:
	expected = analysis.get("applied_plan_fingerprint")
	if not expected:
		frappe.throw(
			_(
				"Applied capacity evidence predates the current plan-state guard. Analyze and Apply again.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	result_rows, segment_rows = _get_run_balance_rows(
		run_doc.name,
		lock_rows=lock_rows,
		lock_linked_work_orders=lock_rows,
	)
	_assert_net_requirement_evidence_complete(
		result_rows,
		operation="Applied-plan validation",
	)
	current = _build_applied_plan_fingerprint(
		run_doc,
		result_rows,
		segment_rows,
		current_read_resources=lock_rows,
	)
	if current != expected:
		frappe.throw(
			_(
				"The APS plan changed after capacity Apply. Analyze, confirm when required, and Apply capacity again.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)


def _build_applied_plan_fingerprint(
	run_doc,
	result_rows,
	segment_rows,
	*,
	current_read_resources: bool = False,
) -> str:
	from injection_aps.services import planning

	settings = _get_capacity_settings(current_read=current_read_resources)
	policy_rows = []
	for row in sorted(result_rows.values(), key=lambda value: value.get("name") or ""):
		item_policy = _get_item_prebuild_policy(
			row.get("item_code"),
			row,
			settings,
			current_read=current_read_resources,
		)
		policy_rows.append(
			{
				"result": row.get("name"),
				"production_strategy": row.get("production_strategy"),
				"demand_confidence": row.get("demand_confidence"),
				"cancellation_risk_percent": flt(row.get("cancellation_risk_percent")),
				"prebuild_allowed": cint(row.get("prebuild_allowed")),
				"max_prebuild_days": cint(row.get("max_prebuild_days")),
				"item_policy": {
					key: item_policy.get(key)
					for key in (
						"production_strategy",
						"prebuild_allowed",
						"max_prebuild_days",
						"shelf_life_days",
						"minimum_batch_qty",
						"max_stock_qty",
						"cancellation_risk_percent",
					)
				},
			}
		)
	payload = {
		"run": run_doc.name,
		"company": run_doc.company,
		"horizon_start": str(run_doc.horizon_start),
		"horizon_end": str(run_doc.horizon_end),
		"high_cancellation_risk_percent": flt(
			settings.get("high_cancellation_risk_percent") or 60
		),
		"results": [
			{
				"name": row.get("name"),
				"customer": row.get("customer"),
				"sales_order": row.get("sales_order"),
				"sales_order_item": row.get("sales_order_item"),
				"item_code": row.get("item_code"),
				"requested_date": str(row.get("requested_date") or ""),
				"planned_qty": flt(row.get("planned_qty")),
				"demand_source_snapshot_json": row.get("demand_source_snapshot_json") or "",
				"fulfillment_baseline_json": row.get("fulfillment_baseline_json") or "",
			}
			for row in sorted(result_rows.values(), key=lambda value: value.get("name") or "")
		],
		"segments": [
			{
				"name": row.get("name"),
				"parent": row.get("parent"),
				"workstation": row.get("workstation"),
				"plant_floor": row.get("plant_floor"),
				"start_time": str(row.get("start_time") or ""),
				"end_time": str(row.get("end_time") or ""),
				"planned_qty": flt(row.get("planned_qty")),
				"setup_minutes": flt(row.get("setup_minutes")),
				"changeover_minutes": flt(row.get("changeover_minutes")),
				"production_mode": row.get("production_mode"),
				"mould_reference": row.get("mould_reference"),
				"is_locked": cint(row.get("is_locked")),
				"anchor_strength": cint(row.get("anchor_strength")),
			}
			for row in sorted(segment_rows, key=lambda value: value.get("name") or "")
		],
		"policies": policy_rows,
	}
	return fingerprint(payload)


def _build_live_capacity_resource_fingerprint(
	run_doc,
	result_rows: dict[str, dict[str, Any]],
	segment_rows: list[dict[str, Any]],
	*,
	lock_external_rows: bool,
) -> str:
	"""Bind approval to live finite resources without changing Apply replay semantics."""
	from injection_aps.services import planning
	from injection_aps.services.v2_flags import is_v2_enabled

	v2_enabled = is_v2_enabled()

	cross_result_rows, cross_segment_rows = _get_cross_run_applied_commitments(
		run_doc, lock_rows=lock_external_rows
	)
	_assert_net_requirement_evidence_complete(
		result_rows,
		operation="live capacity validation",
	)
	_assert_net_requirement_evidence_complete(
		cross_result_rows,
		operation="live capacity validation",
	)
	reserved_demands = _build_cross_run_reservation_demands(
		run_doc,
		cross_result_rows,
		cross_segment_rows,
		lock_existing_work_orders=lock_external_rows,
		current_read_resources=lock_external_rows,
		include_material_resources=not v2_enabled,
	)
	fixed_intervals, mold_fixed_intervals = _get_fixed_execution_intervals(
		run_doc,
		segment_rows,
		cross_run_segments=cross_segment_rows,
		lock_external_rows=lock_external_rows,
	)
	if lock_external_rows:
		downtime_windows = _get_current_active_downtime_windows(run_doc)
	else:
		downtime_windows = planning._get_active_downtime_windows(
			company=run_doc.company,
			plant_floors=planning._get_run_selected_plant_floors(run_doc),
			horizon_start=run_doc.horizon_start,
			horizon_end=run_doc.horizon_end,
			run_name=run_doc.name,
		)
	current_demands = _build_segment_balance_demands(
		run_doc,
		result_rows,
		segment_rows,
		downtime_windows=downtime_windows,
		lock_existing_work_orders=lock_external_rows,
		current_read_resources=lock_external_rows,
		include_material_resources=not v2_enabled,
	)
	if v2_enabled:
		from injection_aps.services import constraint_resolution, horizon_status

		excluded_results = constraint_resolution.get_excluded_result_names(run_doc.name)
		reserved_demands = horizon_status.remove_material_constraints(reserved_demands)
		current_demands = horizon_status.remove_material_constraints(
			[row for row in current_demands if row.get("result") not in excluded_results]
		)
	if lock_external_rows:
		# The Company row serializes APS decisions; the relevant Bin locks also
		# serialize them against standard ERPNext stock postings, which do not lock
		# Company. Rebuild quantities after the locks to close the read/lock race.
		_lock_capacity_resource_bins(
			run_doc.company,
			[*result_rows.values(), *cross_result_rows.values()],
			[*reserved_demands, *current_demands],
		)
		reserved_demands = _build_cross_run_reservation_demands(
			run_doc,
			cross_result_rows,
			cross_segment_rows,
			lock_existing_work_orders=True,
			current_read_resources=True,
			include_material_resources=not v2_enabled,
		)
		current_demands = _build_segment_balance_demands(
			run_doc,
			result_rows,
			segment_rows,
			downtime_windows=downtime_windows,
			lock_existing_work_orders=True,
			current_read_resources=True,
			include_material_resources=not v2_enabled,
		)
		if v2_enabled:
			reserved_demands = horizon_status.remove_material_constraints(reserved_demands)
			current_demands = horizon_status.remove_material_constraints(
				[row for row in current_demands if row.get("result") not in excluded_results]
			)
	finished_goods_stock_by_item = _get_physical_finished_goods_stock_map(
		run_doc.company,
		[*result_rows.values(), *cross_result_rows.values()],
		current_read=lock_external_rows,
	)
	settings = _get_capacity_settings(current_read=lock_external_rows)
	return fingerprint(
		{
			"shared_resource_inputs": _build_shared_resource_snapshot(
				[*reserved_demands, *current_demands]
			),
			"cross_run_finished_goods_claims": _build_finished_goods_claim_snapshot(
				cross_result_rows
			),
			"finished_goods_stock_by_item": finished_goods_stock_by_item,
			"downtime": _build_downtime_snapshot(downtime_windows),
			"workstation_fixed_intervals": _build_interval_snapshot(fixed_intervals),
			"mold_fixed_intervals": _build_interval_snapshot(mold_fixed_intervals),
			"high_cancellation_risk_percent": flt(
				settings.get("high_cancellation_risk_percent") or 60
			),
		}
	)


def _apply_capacity_allocations(analysis: dict[str, Any], *, allow_partial: bool = False) -> dict[str, Any]:
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
			if cint(demand.get("fixed_commitment")):
				continue
			segment = segment_by_name.get(demand.get("segment"))
			if not segment:
				frappe.throw(_("Capacity source segment {0} no longer exists.").format(demand.get("segment")))
			if _is_fixed_segment(segment.as_dict()):
				frappe.throw(_("Fixed or started segment {0} cannot be capacity-balanced.").format(segment.name))
			allocations = demand.get("allocations") or []
			allocation_qty = sum(flt(row.get("qty")) for row in allocations)
			expected_qty = (
				max(flt(segment.planned_qty) - flt(demand.get("unscheduled_qty")), 0)
				if allow_partial else flt(segment.planned_qty)
			)
			if abs(allocation_qty - expected_qty) > CAPACITY_TOLERANCE:
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
			if not allocations:
				result_doc.remove(segment)
				updated_segments += 1
				continue
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


def _apply_excluded_results(run_name: str, analysis: dict[str, Any]) -> int:
	result_names = sorted({row.get("result") for row in analysis.get("excluded_demands") or [] if row.get("result")})
	for result_name in result_names:
		doc = frappe.get_doc("APS Schedule Result", result_name)
		for segment in doc.get("segments") or []:
			if not _is_fixed_segment(segment.as_dict()):
				segment.segment_status = "Cancelled"
		doc.status = "Blocked"
		doc.risk_status = "Critical"
		doc.flow_step = "Excluded From Release"
		doc.next_step_hint = "Carry as P0 into the next Planning Run"
		doc.blocking_reason = doc.exclusion_reason or _("Excluded from this release by an approved APS resolution.", context="Injection APS")
		doc.save(ignore_permissions=True)
	return len(result_names)


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


def _get_run_balance_rows(
	run_name: str,
	*,
	lock_rows: bool = False,
	lock_linked_work_orders: bool = False,
):
	if lock_rows:
		return _get_locked_run_balance_rows(
			run_name,
			lock_linked_work_orders=lock_linked_work_orders,
		)
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
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
			"demand_source_snapshot_json",
			"fulfillment_baseline_json",
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
			"is_urgent",
			"planned_qty",
			"delivered_qty",
			"late_qty_before_balance",
			"status",
			"risk_status",
			"blocking_reason",
			"modified",
		],
	)
	result_map = {row.name: dict(row) for row in results}
	net_names = sorted(
		{row.get("net_requirement") for row in result_map.values() if row.get("net_requirement")}
	)
	if net_names:
		net_map = {
			row.name: row
			for row in frappe.get_all(
				"APS Net Requirement",
				filters={"name": ("in", net_names)},
				fields=[
					"name",
					"demand_qty",
					"available_stock_qty",
					"open_work_order_qty",
					"existing_work_order_policy",
				],
			)
		}
	else:
		net_map = {}
	for result in result_map.values():
		net_row = net_map.get(result.get("net_requirement"))
		evidence = _persisted_net_requirement_evidence(result)
		result["net_requirement_evidence_complete"] = cint(
			_net_requirement_evidence_fields_are_valid(
				net_row.get("demand_qty"),
				net_row.get("available_stock_qty"),
				net_row.get("open_work_order_qty"),
				net_row.get("existing_work_order_policy"),
			)
			if net_row
			else evidence["complete"]
		)
		result["demand_qty"] = (
			flt(net_row.get("demand_qty")) if net_row else evidence["demand_qty"]
		)
		result["available_stock_qty"] = (
			flt(net_row.get("available_stock_qty"))
			if net_row
			else evidence["available_stock_qty"]
		)
		result["open_work_order_qty"] = (
			flt(net_row.get("open_work_order_qty"))
			if net_row
			else evidence["open_work_order_qty"]
		)
		result["existing_work_order_policy"] = (
			net_row.get("existing_work_order_policy")
			if net_row
			else evidence["existing_work_order_policy"]
		)
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
			"changeover_minutes",
			"production_mode",
			"mould_reference",
			"segment_status",
			"actual_status",
			"actual_completed_qty",
			"is_locked",
			"anchor_strength",
			"linked_work_order",
			"linked_scheduling_item",
			"modified",
		],
		order_by="parent asc, sequence_no asc, idx asc",
	)
	segment_rows = [dict(row) for row in segments]
	_annotate_active_linked_work_orders(segment_rows, lock_rows=lock_linked_work_orders)
	return result_map, segment_rows


def _get_locked_run_balance_rows(
	run_name: str,
	*,
	lock_linked_work_orders: bool,
):
	"""Return Result/NR/Segment values from stable locking current reads.

	Do not replace these queries with a name-only lock followed by ``get_all``:
	the latter is a consistent read and may keep seeing rows from before a
	concurrent transaction committed while this transaction waited for the lock.
	"""
	result_query = """
		select
			res.name,
			res.planning_run,
			res.company,
			res.plant_floor,
			res.net_requirement,
			res.customer,
			res.sales_order,
			res.sales_order_item,
			res.item_code,
			res.requested_date,
			res.demand_source,
			res.demand_source_snapshot_json,
			res.fulfillment_baseline_json,
			res.production_strategy,
			res.demand_confidence,
			res.cancellation_risk_percent,
			res.prebuild_allowed,
			res.max_prebuild_days,
			res.is_urgent,
			res.planned_qty,
			res.delivered_qty,
			res.late_qty_before_balance,
			res.status,
			res.risk_status,
			res.blocking_reason,
			res.modified,
			nr.name as live_net_requirement,
			nr.demand_qty,
			nr.available_stock_qty,
			nr.open_work_order_qty,
			nr.existing_work_order_policy
		from `tabAPS Schedule Result` res
		left join `tabAPS Net Requirement` nr on nr.name = res.net_requirement
		where res.planning_run = %s
		order by res.name
		for update
	"""
	result_map = {}
	for source in frappe.db.sql(result_query, (run_name,), as_dict=True):
		row = dict(source)
		if row.get("live_net_requirement"):
			row["net_requirement_evidence_complete"] = cint(
				_net_requirement_evidence_fields_are_valid(
					row.get("demand_qty"),
					row.get("available_stock_qty"),
					row.get("open_work_order_qty"),
					row.get("existing_work_order_policy"),
				)
			)
		else:
			evidence = _persisted_net_requirement_evidence(row)
			row["demand_qty"] = evidence["demand_qty"]
			row["available_stock_qty"] = evidence["available_stock_qty"]
			row["open_work_order_qty"] = evidence["open_work_order_qty"]
			row["existing_work_order_policy"] = evidence["existing_work_order_policy"]
			row["net_requirement_evidence_complete"] = evidence["complete"]
		result_map[row["name"]] = row

	segment_query = """
		select
			seg.name,
			seg.parent,
			seg.idx,
			seg.workstation,
			seg.plant_floor,
			seg.start_time,
			seg.end_time,
			seg.planned_qty,
			seg.setup_minutes,
			seg.changeover_minutes,
			seg.production_mode,
			seg.mould_reference,
			seg.segment_status,
			seg.actual_status,
			seg.actual_completed_qty,
			seg.is_locked,
			seg.anchor_strength,
			seg.linked_work_order,
			seg.linked_scheduling_item,
			seg.modified
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` res on res.name = seg.parent
		where res.planning_run = %(run)s
			and seg.parenttype = 'APS Schedule Result'
			and seg.segment_kind in ('Primary', 'Manual')
			and ifnull(seg.segment_status, '') not in ('Blocked', 'Cancelled')
			and ifnull(seg.planned_qty, 0) > 0
		order by seg.parent, seg.sequence_no, seg.idx, seg.name
		for update
	"""
	segment_rows = [
		dict(row)
		for row in frappe.db.sql(segment_query, {"run": run_name}, as_dict=True)
	]
	_annotate_active_linked_work_orders(
		segment_rows,
		lock_rows=lock_linked_work_orders,
	)
	return result_map, segment_rows


def _annotate_active_linked_work_orders(
	segment_rows: list[dict[str, Any]], *, lock_rows: bool = False
) -> None:
	work_orders = sorted(
		{row.get("linked_work_order") for row in segment_rows if row.get("linked_work_order")}
	)
	if not work_orders:
		return
	if lock_rows:
		rows = frappe.db.sql(
			"""
			select name, docstatus, status
			from `tabWork Order`
			where name in %s
			order by name
			for update
			""",
			(tuple(work_orders),),
			as_dict=True,
		)
	else:
		rows = frappe.get_all(
			"Work Order",
			filters={"name": ("in", work_orders)},
			fields=["name", "docstatus", "status"],
		)
	active = {
		row.name
		for row in rows
		if cint(row.docstatus) == 1
		and (row.status or "") not in ("Completed", "Closed", "Cancelled", "Stopped")
	}
	stopped = {row.name for row in rows if (row.status or "") == "Stopped"}
	for segment in segment_rows:
		segment["linked_work_order_active"] = cint(segment.get("linked_work_order") in active)
		segment["linked_work_order_stopped"] = cint(segment.get("linked_work_order") in stopped)


def _get_cross_run_applied_commitments(run_doc, *, lock_rows: bool = False):
	"""Return only unfinished physical segments already Applied by another run."""
	if not run_doc.company:
		return {}, []
	# Finished-goods stock can cover an APS result without creating any production
	# segment. Load every active Applied result first so such a claim cannot vanish
	# merely because the inner segment query has no row (or its work lies outside the
	# new run's horizon).
	result_query = """
		select
			res.name,
			res.company,
			res.plant_floor,
			res.net_requirement,
			res.customer,
			res.sales_order,
			res.sales_order_item,
			res.item_code,
			res.requested_date,
			res.demand_source,
			res.fulfillment_baseline_json,
			res.production_strategy,
			res.demand_confidence,
			res.cancellation_risk_percent,
			res.prebuild_allowed,
			res.max_prebuild_days,
			res.is_urgent,
			res.planned_qty,
			res.late_qty_before_balance,
			res.delivered_qty,
			ifnull(nr.demand_qty, 0) as demand_qty,
			ifnull(nr.available_stock_qty, 0) as available_stock_qty,
			ifnull(nr.open_work_order_qty, 0) as open_work_order_qty,
			ifnull(nr.existing_work_order_policy, '') as existing_work_order_policy,
			nr.name as live_net_requirement,
			res.planning_run as reservation_run
		from `tabAPS Schedule Result` res
		inner join `tabAPS Planning Run` run on run.name = res.planning_run
		left join `tabAPS Net Requirement` nr on nr.name = res.net_requirement
		where run.company = %(company)s
			and run.name != %(run)s
			and (run.capacity_balance_status = 'Applied' or run.capacity_balance_status = 'Applied with Exceptions')
			and ifnull(run.status, '') != 'Closed'
		order by run.name asc, res.name asc
	"""
	if lock_rows:
		result_query += " for update"
	result_map = {
		row.name: dict(row)
		for row in frappe.db.sql(
			result_query,
			{"company": run_doc.company, "run": run_doc.name},
			as_dict=True,
		)
	}
	for result in result_map.values():
		if result.get("live_net_requirement"):
			result["net_requirement_evidence_complete"] = cint(
				_net_requirement_evidence_fields_are_valid(
					result.get("demand_qty"),
					result.get("available_stock_qty"),
					result.get("open_work_order_qty"),
					result.get("existing_work_order_policy"),
				)
			)
			continue
		evidence = _persisted_net_requirement_evidence(result)
		result["demand_qty"] = evidence["demand_qty"]
		result["available_stock_qty"] = evidence["available_stock_qty"]
		result["open_work_order_qty"] = evidence["open_work_order_qty"]
		result["existing_work_order_policy"] = evidence["existing_work_order_policy"]
		result["net_requirement_evidence_complete"] = evidence["complete"]
	query = """
		select
			seg.name,
			seg.parent,
			seg.idx,
			seg.workstation,
			seg.plant_floor,
			seg.start_time,
			seg.end_time,
			seg.planned_qty,
			seg.setup_minutes,
			seg.changeover_minutes,
			seg.production_mode,
			seg.mould_reference,
			seg.segment_status,
			seg.actual_status,
			seg.actual_completed_qty,
			seg.is_locked,
			seg.anchor_strength,
			seg.linked_work_order,
			seg.linked_scheduling_item,
			case when ifnull(wo.status, '') = 'Stopped' then 1 else 0 end as linked_work_order_stopped,
			case when wo.docstatus = 1 and ifnull(wo.status, '') not in ('Completed', 'Closed', 'Cancelled', 'Stopped') then 1 else 0 end as linked_work_order_active,
			res.planning_run as reservation_run,
			res.company as result_company,
			res.plant_floor as result_plant_floor,
			res.net_requirement,
			res.customer,
			res.item_code,
			res.requested_date,
			res.demand_source,
			res.production_strategy,
			res.demand_confidence,
			res.cancellation_risk_percent,
			res.prebuild_allowed,
			res.max_prebuild_days,
			res.is_urgent,
			res.planned_qty as result_planned_qty,
			res.late_qty_before_balance as result_late_qty_before_balance,
			res.delivered_qty as result_delivered_qty,
			ifnull(nr.demand_qty, 0) as result_demand_qty,
			ifnull(nr.available_stock_qty, 0) as result_available_stock_qty
		from `tabAPS Schedule Segment` seg
		inner join `tabAPS Schedule Result` res on res.name = seg.parent
		inner join `tabAPS Planning Run` run on run.name = res.planning_run
		left join `tabWork Order` wo on wo.name = seg.linked_work_order
		left join `tabAPS Net Requirement` nr on nr.name = res.net_requirement
		where run.company = %(company)s
			and run.name != %(run)s
			and (run.capacity_balance_status = 'Applied' or run.capacity_balance_status = 'Applied with Exceptions')
			and ifnull(run.status, '') != 'Closed'
			and seg.parenttype = 'APS Schedule Result'
			and seg.segment_kind in ('Primary', 'Manual')
			and ifnull(seg.segment_status, '') not in ('Blocked', 'Cancelled', 'Completed')
			and ifnull(seg.actual_status, '') != 'Completed'
			and ifnull(seg.planned_qty, 0) - ifnull(seg.actual_completed_qty, 0) > %(tolerance)s
			and (
				ifnull(seg.linked_work_order, '') = ''
				or wo.name is null
				or ifnull(wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
			)
		order by seg.start_time asc, run.name asc, seg.name asc
		"""
	if lock_rows:
		query += " for update"
	rows = frappe.db.sql(
		query,
		{
			"company": run_doc.company,
			"run": run_doc.name,
			"tolerance": CAPACITY_TOLERANCE,
		},
		as_dict=True,
	)
	segments = []
	for source in rows:
		row = dict(source)
		result_map.setdefault(
			row["parent"],
			{
				"name": row["parent"],
				"company": row.get("result_company"),
				"plant_floor": row.get("result_plant_floor"),
				"net_requirement": row.get("net_requirement"),
				"customer": row.get("customer"),
				"item_code": row.get("item_code"),
				"requested_date": row.get("requested_date"),
				"demand_source": row.get("demand_source"),
				"production_strategy": row.get("production_strategy"),
				"demand_confidence": row.get("demand_confidence"),
				"cancellation_risk_percent": row.get("cancellation_risk_percent"),
				"prebuild_allowed": row.get("prebuild_allowed"),
				"max_prebuild_days": row.get("max_prebuild_days"),
				"is_urgent": row.get("is_urgent"),
				"planned_qty": row.get("result_planned_qty"),
				"late_qty_before_balance": row.get("result_late_qty_before_balance"),
				"delivered_qty": row.get("result_delivered_qty"),
				"demand_qty": row.get("result_demand_qty"),
				"available_stock_qty": row.get("result_available_stock_qty"),
				"net_requirement_evidence_complete": cint(bool(row.get("net_requirement"))),
				"reservation_run": row.get("reservation_run"),
			},
		)
		segments.append(row)
	return result_map, segments


def _build_segment_balance_demands(
	run_doc,
	result_rows: dict[str, dict[str, Any]],
	segment_rows: list[dict[str, Any]],
	*,
	downtime_windows: list[dict[str, Any]] | None = None,
	lock_existing_work_orders: bool = False,
	current_read_resources: bool = False,
	include_material_resources: bool = True,
) -> list[dict[str, Any]]:
	"""Build demands while consuming one existing-WO material credit exactly once.

	An included Work Order is part of the machine quantity boundary, but its raw
	material may already be excluded from free Bin stock by ERPNext's production
	reservation.  The old implementation either required that material twice or
	zeroed all material merely because a WO link existed.  We grant a credit only
	when the exact SO/SOI (or explicit stock-purpose) WO has a complete, current
	reservation for every BOM component, then allocate that output credit across
	the Result's segments in deterministic order.
	"""
	demands: list[dict[str, Any]] = []
	credit_by_result: dict[str, float] = {}
	for segment in sorted(
		segment_rows,
		key=lambda row: (
			str(row.get("parent") or ""),
			cint(row.get("idx")),
			str(row.get("start_time") or ""),
			str(row.get("name") or ""),
		),
	):
		result = result_rows.get(segment.get("parent"))
		if not result:
			continue
		demand = _build_segment_balance_demand(
			run_doc,
			result,
			segment,
			downtime_windows=downtime_windows,
			current_read_resources=current_read_resources,
			include_material_resources=include_material_resources,
		)
		result_name = result.get("name") or segment.get("parent")
		if not include_material_resources:
			demands.append(demand)
			continue
		if result_name not in credit_by_result:
			credit_by_result[result_name] = _get_proven_existing_work_order_material_credit(
				result,
				demand.get("material_requirements") or [],
				lock_rows=lock_existing_work_orders,
			)
		base_material_qty = (
			max(flt(demand.get("resource_consumption_qty")), 0)
			if demand.get("resource_consumption_qty") is not None
			else max(flt(demand.get("qty")), 0)
		)
		credit_qty = min(base_material_qty, max(flt(credit_by_result[result_name]), 0))
		credit_by_result[result_name] = max(flt(credit_by_result[result_name]) - credit_qty, 0)
		demand["existing_work_order_material_credit_qty"] = credit_qty
		demand["resource_material_consumption_qty"] = max(base_material_qty - credit_qty, 0)
		demands.append(demand)
	return demands


def _get_proven_existing_work_order_material_credit(
	result: dict[str, Any],
	material_requirements: list[dict[str, Any]],
	*,
	lock_rows: bool = False,
) -> float:
	"""Return output qty whose component reservation is provably current.

	A missing source warehouse or a stale aggregate Bin reservation returns zero;
	that is deliberately conservative and forces the free-material check to cover
	the whole plan instead of silently reusing another Work Order's reservation.
	"""
	baseline_coverage_qty = max(flt(result.get("open_work_order_qty")), 0)
	result_name = result.get("name")
	result_run = result.get("planning_run") or result.get("reservation_run")
	# Before release, the immutable Net Requirement boundary is the only safe
	# credit.  After release, APS-created WOs carry the exact Result/Run lineage;
	# their submitted reservation is equally authoritative even when the original
	# Net Requirement had ``open_work_order_qty = 0``.
	if not material_requirements or (baseline_coverage_qty <= CAPACITY_TOLERANCE and not result_name):
		return 0.0
	company = result.get("company")
	item_code = result.get("item_code")
	sales_order = result.get("sales_order")
	sales_order_item = result.get("sales_order_item")
	if not company or not item_code:
		return 0.0
	params: dict[str, Any] = {"company": company, "item_code": item_code}
	if sales_order and sales_order_item:
		lineage_condition = "wo.sales_order = %(sales_order)s and wo.sales_order_item = %(sales_order_item)s"
		params.update({"sales_order": sales_order, "sales_order_item": sales_order_item})
	elif not sales_order and not sales_order_item:
		stock_purpose = (
			"Safety Stock" if (result.get("demand_source") or "") == "Safety Stock" else "Stock Production"
		)
		lineage_condition = "ifnull(wo.sales_order, '') = '' and ifnull(wo.sales_order_item, '') = '' and wo.custom_aps_source = %(stock_purpose)s"
		params["stock_purpose"] = stock_purpose
	else:
		return 0.0
	query = f"""
		select
			wo.name,
			wo.qty,
			wo.produced_qty,
			wo.skip_transfer,
			wo.wip_warehouse,
			wo.custom_aps_result_reference,
			wo.custom_aps_run
		from `tabWork Order` wo
		where wo.company = %(company)s
			and wo.production_item = %(item_code)s
			and wo.docstatus = 1
			and ifnull(wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
			and ifnull(wo.qty, 0) > ifnull(wo.produced_qty, 0)
			and {lineage_condition}
		order by wo.name
	"""
	if lock_rows:
		query += " for update"
	candidates = frappe.db.sql(query, params, as_dict=True)
	linked_candidates = [
		row
		for row in candidates
		if result_name
		and (row.get("custom_aps_result_reference") or "") == result_name
		and bool(result_run)
		and (row.get("custom_aps_run") or "") == result_run
	]
	if linked_candidates:
		candidates = linked_candidates
		coverage_qty = max(flt(result.get("planned_qty")), baseline_coverage_qty, 0)
	else:
		# Legacy/external WOs have no APS owner until the reviewed reconciliation is
		# applied.  Their aggregate credit is capped by the quantity frozen into this
		# Result, so several exact WOs can be consumed without the old ``len == 1``
		# ambiguity or silently increasing the plan boundary.
		candidates = [
			row
			for row in candidates
			if not row.get("custom_aps_result_reference") and not row.get("custom_aps_run")
		]
		coverage_qty = baseline_coverage_qty
	if not candidates or coverage_qty <= CAPACITY_TOLERANCE:
		return 0.0
	work_order_names = tuple(row.get("name") for row in candidates if row.get("name"))
	if not work_order_names:
		return 0.0
	item_query = """
		select parent, item_code, source_warehouse, required_qty, transferred_qty, consumed_qty
		from `tabWork Order Item`
		where parent in %s
		order by parent, item_code, source_warehouse, idx
	"""
	if lock_rows:
		item_query += " for update"
	required_items = [
		dict(row)
		for row in frappe.db.sql(item_query, (work_order_names,), as_dict=True)
	]
	if not required_items:
		return 0.0
	by_work_order_item_warehouse: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
		lambda: {
			"raw_reserved": 0.0,
			"wip_unconsumed": 0.0,
			"wip_warehouse": "",
		}
	)
	work_order_by_name = {row.get("name"): row for row in candidates}
	for row in required_items:
		warehouse = row.get("source_warehouse") or ""
		if not warehouse:
			return 0.0
		work_order = work_order_by_name.get(row.get("parent")) or {}
		key = (row.get("parent") or "", row.get("item_code") or "", warehouse)
		required_qty = max(flt(row.get("required_qty")), 0)
		transferred_qty = max(flt(row.get("transferred_qty")), 0)
		consumed_qty = max(flt(row.get("consumed_qty")), 0)
		if cint(work_order.get("skip_transfer")):
			raw_reserved = max(required_qty - consumed_qty, 0)
			wip_unconsumed = 0.0
		else:
			# Consumption and transfer are separate ERPNext movements.  Count source
			# reservation and unconsumed WIP independently; this prevents both the old
			# all-or-nothing loss after a transfer and double credit after consumption.
			raw_reserved = max(required_qty - max(transferred_qty, consumed_qty), 0)
			wip_unconsumed = max(min(transferred_qty, required_qty) - consumed_qty, 0)
		values = by_work_order_item_warehouse[key]
		values["raw_reserved"] += raw_reserved
		values["wip_unconsumed"] += wip_unconsumed
		values["wip_warehouse"] = work_order.get("wip_warehouse") or ""

	qty_per_unit_by_component: dict[tuple[str, str], float] = defaultdict(float)
	for requirement in material_requirements:
		component = requirement.get("item_code") or ""
		warehouse = requirement.get("warehouse") or ""
		qty_per_unit = max(flt(requirement.get("qty_per_unit")), 0)
		if component and warehouse and qty_per_unit > CAPACITY_TOLERANCE:
			qty_per_unit_by_component[(component, warehouse)] += qty_per_unit
	if not qty_per_unit_by_component:
		return 0.0

	# Verify ERPNext's aggregate reservation is not stale.  The comparison uses
	# every active WO for the same Bin so another WO's reservation cannot be
	# misattributed to this result.
	reservation_by_bin: dict[tuple[str, str], float] = defaultdict(float)
	for (_work_order_name, component, warehouse), values in by_work_order_item_warehouse.items():
		reservation_by_bin[(component, warehouse)] += values["raw_reserved"]
	verified_raw_bins: set[tuple[str, str]] = set()
	for (component, warehouse), reserved_qty in reservation_by_bin.items():
		if reserved_qty <= CAPACITY_TOLERANCE:
			continue
		proof = frappe.db.sql(
			"""
			select
				coalesce(max(greatest(ifnull(bin.reserved_qty_for_production, 0), 0)), 0) as bin_reserved,
				coalesce(sum(greatest(
					case when ifnull(active_wo.skip_transfer, 0) = 0
						then ifnull(active_item.required_qty, 0) - greatest(
							ifnull(active_item.transferred_qty, 0),
							ifnull(active_item.consumed_qty, 0)
						)
						else ifnull(active_item.required_qty, 0) - ifnull(active_item.consumed_qty, 0)
					end,
					0
				)), 0) as expected_reserved
			from `tabWork Order Item` active_item
			inner join `tabWork Order` active_wo on active_wo.name = active_item.parent
			left join `tabBin` bin
				on bin.item_code = active_item.item_code
				and bin.warehouse = active_item.source_warehouse
			where active_item.item_code = %(item_code)s
				and active_item.source_warehouse = %(warehouse)s
				and active_wo.docstatus = 1
				and ifnull(active_wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
			""" + (" for update" if lock_rows else ""),
			{"item_code": component, "warehouse": warehouse},
			as_dict=True,
		)
		if proof and flt(proof[0].get("bin_reserved")) + CAPACITY_TOLERANCE >= flt(
			proof[0].get("expected_reserved")
		):
			verified_raw_bins.add((component, warehouse))

	wip_by_bin: dict[tuple[str, str], float] = defaultdict(float)
	for (_work_order_name, component, _warehouse), values in by_work_order_item_warehouse.items():
		if values["wip_unconsumed"] > CAPACITY_TOLERANCE and values["wip_warehouse"]:
			wip_by_bin[(component, values["wip_warehouse"])] += values["wip_unconsumed"]
	verified_wip_bins: set[tuple[str, str]] = set()
	for (component, wip_warehouse), wip_qty in wip_by_bin.items():
		if wip_qty <= CAPACITY_TOLERANCE:
			continue
		proof = frappe.db.sql(
			"""
			select
				coalesce(max(greatest(ifnull(bin.actual_qty, 0), 0)), 0) as bin_actual,
				coalesce(sum(greatest(
					least(
						ifnull(active_item.transferred_qty, 0),
						ifnull(active_item.required_qty, 0)
					) - ifnull(active_item.consumed_qty, 0),
					0
				)), 0) as expected_wip
			from `tabWork Order Item` active_item
			inner join `tabWork Order` active_wo on active_wo.name = active_item.parent
			left join `tabBin` bin
				on bin.item_code = active_item.item_code
				and bin.warehouse = active_wo.wip_warehouse
			where active_item.item_code = %(item_code)s
				and active_wo.wip_warehouse = %(warehouse)s
				and ifnull(active_wo.skip_transfer, 0) = 0
				and active_wo.docstatus = 1
				and ifnull(active_wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
			""" + (" for update" if lock_rows else ""),
			{"item_code": component, "warehouse": wip_warehouse},
			as_dict=True,
		)
		if proof and flt(proof[0].get("bin_actual")) + CAPACITY_TOLERANCE >= flt(
			proof[0].get("expected_wip")
		):
			verified_wip_bins.add((component, wip_warehouse))

	proven_output_qty = 0.0
	for work_order in candidates:
		work_order_name = work_order.get("name")
		candidate_output_qty = max(
			flt(work_order.get("qty")) - flt(work_order.get("produced_qty")),
			0,
		)
		for (component, warehouse), qty_per_unit in qty_per_unit_by_component.items():
			values = by_work_order_item_warehouse.get(
				(work_order_name, component, warehouse), {}
			)
			protected_qty = (
				flt(values.get("raw_reserved"))
				if (component, warehouse) in verified_raw_bins
				else 0.0
			)
			wip_warehouse = values.get("wip_warehouse") or ""
			if (component, wip_warehouse) in verified_wip_bins:
				protected_qty += flt(values.get("wip_unconsumed"))
			candidate_output_qty = min(candidate_output_qty, protected_qty / qty_per_unit)
		if candidate_output_qty > CAPACITY_TOLERANCE:
			proven_output_qty += candidate_output_qty
	return min(coverage_qty, proven_output_qty)


def _build_cross_run_reservation_demand(
	run_doc,
	result: dict[str, Any],
	segment: dict[str, Any],
	*,
	material_credit_qty: float = 0,
):
	demand = _build_segment_balance_demand(run_doc, result, segment)
	remaining_qty = max(
		flt(segment.get("planned_qty")) - max(flt(segment.get("actual_completed_qty")), 0),
		0,
	)
	demand.update(
		{
			"key": f"RESERVED|{result.get('reservation_run')}|{segment.get('name')}",
			"fixed_commitment": 1,
			"reservation_run": result.get("reservation_run"),
			"resource_consumption_qty": remaining_qty,
			"existing_work_order_material_credit_qty": min(
				remaining_qty, max(flt(material_credit_qty), 0)
			),
			"resource_material_consumption_qty": max(
				remaining_qty - max(flt(material_credit_qty), 0), 0
			),
			"finished_goods_stock_claim_qty": _remaining_finished_goods_stock_claim(result),
		}
	)
	return demand


def _build_cross_run_reservation_demands(
	run_doc,
	result_rows: dict[str, dict[str, Any]],
	segment_rows: list[dict[str, Any]],
	*,
	lock_existing_work_orders: bool = False,
	current_read_resources: bool = False,
	include_material_resources: bool = True,
) -> list[dict[str, Any]]:
	base_demands = _build_segment_balance_demands(
		run_doc,
		result_rows,
		segment_rows,
		lock_existing_work_orders=lock_existing_work_orders,
		current_read_resources=current_read_resources,
		include_material_resources=include_material_resources,
	)
	by_segment = {row.get("segment"): row for row in base_demands}
	reserved = []
	for segment in segment_rows:
		result = result_rows.get(segment.get("parent"))
		base = by_segment.get(segment.get("name"))
		if not result or not base:
			continue
		reserved.append(
			_build_cross_run_reservation_demand(
				run_doc,
				result,
				segment,
				material_credit_qty=base.get("existing_work_order_material_credit_qty"),
			)
		)
	return reserved


def _is_fixed_segment(segment: dict[str, Any]) -> bool:
	return bool(
		cint(segment.get("is_locked"))
		or cint(segment.get("linked_work_order_active"))
		or cint(segment.get("linked_work_order_stopped"))
		or cint(segment.get("anchor_strength")) >= 50
		or (segment.get("segment_status") or "") in ("Applied", "Completed")
		or (segment.get("actual_status") or "") not in ("", "Not Started")
		or flt(segment.get("actual_completed_qty")) > 0
	)


def _get_workstation_plant_floor_map(segment_rows: list[dict[str, Any]]) -> dict[str, str | None]:
	workstations = sorted({row.get("workstation") for row in segment_rows if row.get("workstation")})
	result = {
		row.get("workstation"): row.get("plant_floor")
		for row in segment_rows
		if row.get("workstation") and row.get("plant_floor")
	}
	missing = [workstation for workstation in workstations if workstation not in result]
	if missing:
		for row in frappe.get_all(
			"Workstation",
			filters={"name": ("in", missing)},
			fields=["name", "plant_floor"],
		):
			result[row.name] = row.plant_floor
	return result


def _get_fixed_execution_intervals(
	run_doc,
	run_segments,
	*,
	cross_run_segments=None,
	lock_external_rows: bool = False,
):
	fixed_by_workstation = defaultdict(list)
	fixed_by_mold = defaultdict(list)
	for segment in run_segments:
		if not _is_fixed_segment(segment):
			continue
		if not segment.get("workstation") or not segment.get("start_time") or not segment.get("end_time"):
			continue
		start_time = get_datetime(segment.get("start_time")) - timedelta(
			minutes=max(
				flt(segment.get("setup_minutes")), flt(segment.get("changeover_minutes")), 0
			)
		)
		interval = (start_time, segment.get("end_time"))
		fixed_by_workstation[segment.get("workstation")].append(interval)
		if segment.get("mould_reference"):
			fixed_by_mold[segment.get("mould_reference")].append(interval)
	for segment in cross_run_segments or []:
		if not segment.get("workstation") or not segment.get("start_time") or not segment.get("end_time"):
			continue
		start_time = get_datetime(segment.get("start_time")) - timedelta(
			minutes=max(
				flt(segment.get("setup_minutes")), flt(segment.get("changeover_minutes")), 0
			)
		)
		interval = (start_time, segment.get("end_time"))
		fixed_by_workstation[segment.get("workstation")].append(interval)
		if segment.get("mould_reference"):
			fixed_by_mold[segment.get("mould_reference")].append(interval)
	if frappe.db.exists("DocType", "Scheduling Item"):
		query = """
			select
				si.workstation,
				coalesce(si.from_time, si.planned_start_date) as start_time,
				coalesce(si.to_time, si.planned_end_date) as end_time,
				seg.mould_reference,
				greatest(ifnull(seg.setup_minutes, 0), ifnull(seg.changeover_minutes, 0)) as setup_minutes
			from `tabScheduling Item` si
			inner join `tabWork Order Scheduling` wos on wos.name = si.parent
			left join `tabAPS Schedule Segment` seg on seg.name = si.custom_aps_segment_reference
			where ifnull(wos.status, '') in ('Material Transfer', 'Job Card', 'Manufacture')
				and (
					si.custom_aps_campaign is null
					or si.custom_aps_capacity_owner = si.custom_aps_segment_reference
				)
				and coalesce(si.from_time, si.planned_start_date) < %(horizon_end)s
				and coalesce(si.to_time, si.planned_end_date) > %(horizon_start)s
			order by si.name asc
			"""
		if lock_external_rows:
			query += " for update"
		rows = frappe.db.sql(
			query,
			{"horizon_start": run_doc.horizon_start, "horizon_end": run_doc.horizon_end},
			as_dict=True,
		)
		for row in rows:
			if not row.workstation or not row.start_time or not row.end_time:
				continue
			interval = (
				get_datetime(row.start_time) - timedelta(minutes=max(flt(row.setup_minutes), 0)),
				row.end_time,
			)
			fixed_by_workstation[row.workstation].append(interval)
			if row.mould_reference:
				fixed_by_mold[row.mould_reference].append(interval)
	return dict(fixed_by_workstation), dict(fixed_by_mold)


def _build_segment_balance_demand(
	run_doc,
	result: dict[str, Any],
	segment: dict[str, Any],
	*,
	downtime_windows: list[dict[str, Any]] | None = None,
	current_read_resources: bool = False,
	include_material_resources: bool = True,
) -> dict[str, Any]:
	from injection_aps.services import planning

	duration_hours = max(_minutes_between(segment.get("start_time"), segment.get("end_time")) / 60, 0)
	effective_hours = duration_hours
	matching_downtime: list[dict[str, Any]] = []
	if downtime_windows is not None and duration_hours > 0:
		matching_downtime = planning._get_matching_downtime_windows(
			downtime_windows,
			workstation=segment.get("workstation"),
			plant_floor=segment.get("plant_floor") or result.get("plant_floor"),
			company=result.get("company"),
		)
		effective_hours = planning._available_run_hours_between(
			segment.get("start_time"), segment.get("end_time"), matching_downtime
		)
	hourly_rate = flt(segment.get("planned_qty")) / effective_hours if effective_hours else 0
	settings = _get_capacity_settings(current_read=current_read_resources)
	item_policy = _get_item_prebuild_policy(
		result.get("item_code"),
		result,
		settings,
		current_read=current_read_resources,
	)
	current_inventory_qty, inventory_room_qty = _get_item_inventory_room(
		result.get("company"),
		result.get("item_code"),
		item_policy.get("max_stock_qty"),
		current_read=current_read_resources,
	)
	warehouse, warehouse_room_qty = _get_warehouse_capacity_room(
		result.get("company"),
		result.get("plant_floor"),
		result.get("item_code"),
		settings,
		current_read=current_read_resources,
	)
	material_resource_details: dict[str, Any] = {}
	material_ready_qty = None
	if include_material_resources:
		source_warehouse = _get_valid_plant_floor_warehouse(
			result.get("company"),
			result.get("plant_floor"),
			settings.get("plant_floor_source_warehouse_field"),
			current_read=current_read_resources,
		)
		material_ready_qty = _get_material_ready_qty(
			result.get("company"),
			result.get("item_code"),
			warehouse=source_warehouse,
			resource_details=material_resource_details,
			current_read=current_read_resources,
		)
	due_time = get_datetime(f"{getdate(result.get('requested_date'))} 23:59:59")
	is_fixed = _is_fixed_segment(segment)
	planned_qty = max(flt(segment.get("planned_qty")), 0)
	stock_retained = bool(
		(result.get("demand_source") or "") == "Safety Stock"
		or (
			not result.get("customer")
			and not result.get("sales_order")
			and not result.get("sales_order_item")
		)
	)
	remaining_commitment_qty = (
		max(planned_qty - max(flt(segment.get("actual_completed_qty")), 0), 0)
		if is_fixed
		else None
	)
	return {
		"key": segment.get("name"),
		"result": result.get("name"),
		"segment": segment.get("name"),
		"workstation": segment.get("workstation"),
		"mould_reference": segment.get("mould_reference"),
		"qty": planned_qty,
		"hourly_rate": hourly_rate,
		"setup_minutes": flt(segment.get("setup_minutes")),
		"start_time": segment.get("start_time"),
		"end_time": segment.get("end_time"),
		"capacity_factor_intervals": _build_capacity_factor_intervals(
			[(segment.get("start_time"), segment.get("end_time"))],
			matching_downtime,
		),
		"production_mode": segment.get("production_mode"),
		"linked_work_order": segment.get("linked_work_order"),
		"linked_work_order_active": cint(segment.get("linked_work_order_active")),
		"linked_work_order_stopped": cint(segment.get("linked_work_order_stopped")),
		"fixed_commitment": cint(is_fixed),
		"stock_retained": cint(stock_retained),
		"resource_consumption_qty": (
			remaining_commitment_qty
		),
		# A later batch builder applies only a reservation credit that is proven
		# against the exact WO and current Bin aggregate.  A link alone is not proof:
		# WOs with a blank source warehouse create no ERPNext Bin reservation.
		"resource_material_consumption_qty": remaining_commitment_qty if include_material_resources else None,
		"due_time": due_time,
		"due_granularity": "Date",
		"strategy": result.get("production_strategy") or item_policy.get("production_strategy"),
		"demand_confidence": result.get("demand_confidence") or ("Forecast" if result.get("demand_source") == "Forecast" else "Confirmed"),
		"cancellation_risk_percent": max(
			flt(result.get("cancellation_risk_percent")),
			flt(item_policy.get("cancellation_risk_percent")),
		),
		"priority": 100 if cint(result.get("is_urgent")) else 0,
		"prebuild_allowed": cint(result.get("prebuild_allowed")) and cint(item_policy.get("prebuild_allowed")),
		"max_prebuild_days": cint(result.get("max_prebuild_days") or item_policy.get("max_prebuild_days")),
		"shelf_life_days": cint(item_policy.get("shelf_life_days")),
		"minimum_batch_qty": flt(item_policy.get("minimum_batch_qty")),
		"target_stock_uom": item_policy.get("stock_uom"),
		"current_inventory_qty": current_inventory_qty,
		"inventory_room_qty": inventory_room_qty,
		"inventory_resource_key": f"{result.get('company')}|{result.get('item_code')}",
		"warehouse_room_qty": warehouse_room_qty,
		"warehouse_resource_key": warehouse,
		"material_ready_qty": material_ready_qty,
		"material_requirements": material_resource_details.get("requirements") or [] if include_material_resources else [],
		"overstock_risk": cint(inventory_room_qty is not None and inventory_room_qty <= CAPACITY_TOLERANCE),
		"late_qty_before_balance": _segment_late_qty_before_balance(
			segment.get("start_time"),
			segment.get("end_time"),
			planned_qty,
			due_time,
		),
		"finished_goods_stock_claim_qty": _remaining_finished_goods_stock_claim(result),
	}


def _segment_late_qty_before_balance(start_time, end_time, qty: float, due_time) -> float:
	"""Return this segment's own late share; never reuse a persisted result total."""
	start = get_datetime(start_time)
	end = get_datetime(end_time)
	due = get_datetime(due_time)
	if (due.hour, due.minute, due.second, due.microsecond) == (23, 59, 59, 0):
		due += timedelta(seconds=1)
	qty = max(flt(qty), 0)
	if qty <= CAPACITY_TOLERANCE or end <= due:
		return 0.0
	if start >= due or end <= start:
		return qty
	return qty * _minutes_between(due, end) / max(_minutes_between(start, end), CAPACITY_TOLERANCE)


def _get_item_prebuild_policy(
	item_code: str,
	result: dict[str, Any],
	settings: dict[str, Any],
	*,
	current_read: bool = False,
) -> dict[str, Any]:
	meta = frappe.get_meta("Item")
	fields = ["shelf_life_in_days", "min_order_qty", "stock_uom"]
	for fieldname in (
		"custom_aps_prebuild_allowed",
		"custom_aps_max_prebuild_days",
		"custom_aps_cancellation_risk_percent",
		"custom_aps_max_stock_qty",
	):
		if meta.has_field(fieldname):
			fields.append(fieldname)
	if current_read:
		field_sql = ", ".join(f"`{fieldname}`" for fieldname in fields)
		rows = frappe.db.sql(
			f"select {field_sql} from `tabItem` where name = %s for update",
			item_code,
			as_dict=True,
		)
		row = rows[0] if rows else {}
	else:
		row = frappe.db.get_value("Item", item_code, fields, as_dict=True) or {}
	return {
		"production_strategy": result.get("production_strategy") or settings.get("default_production_strategy") or "Auto Balance",
		"prebuild_allowed": cint(row.get("custom_aps_prebuild_allowed", 1)),
		"max_prebuild_days": cint(row.get("custom_aps_max_prebuild_days") or settings.get("default_max_prebuild_days") or 7),
		"cancellation_risk_percent": flt(row.get("custom_aps_cancellation_risk_percent")),
		"max_stock_qty": flt(row.get("custom_aps_max_stock_qty")),
		"shelf_life_days": cint(row.get("shelf_life_in_days")),
		"minimum_batch_qty": flt(row.get("min_order_qty")),
		"stock_uom": row.get("stock_uom"),
	}


def _get_named_fields_for_update(
	doctype: str,
	name: str,
	fields: list[str],
) -> dict[str, Any] | None:
	"""Read one named master row with locking/current-read semantics."""
	if not name or not fields:
		return None
	meta = frappe.get_meta(doctype)
	valid_fields = [fieldname for fieldname in fields if meta.has_field(fieldname)]
	if not valid_fields:
		return None
	field_sql = ", ".join(f"`{fieldname}`" for fieldname in valid_fields)
	rows = frappe.db.sql(
		f"select {field_sql} from `tab{doctype}` where name = %s for update",
		name,
		as_dict=True,
	)
	return frappe._dict(rows[0]) if rows else None


def _get_finished_goods_warehouses_for_update(company: str) -> list[str]:
	"""Return and lock the current explicit Finished Goods warehouse scope."""
	rows = frappe.db.sql(
		"""
		select name
		from `tabWarehouse`
		where company = %s
			and is_group = 0
			and ifnull(disabled, 0) = 0
			and warehouse_type = 'Finished Goods'
		order by name
		for update
		""",
		company,
		as_dict=True,
	)
	warehouses = {row.get("name") for row in rows if row.get("name")}
	setting_rows = frappe.db.sql(
		"""
		select value
		from `tabSingles`
		where doctype = 'APS Settings'
			and field = 'plant_floor_fg_warehouse_field'
		for update
		""",
		as_dict=True,
	)
	fieldname = setting_rows[0].get("value") if setting_rows else None
	if not fieldname or not frappe.db.exists("DocType", "Plant Floor"):
		return sorted(warehouses)
	plant_floor_meta = frappe.get_meta("Plant Floor")
	if not plant_floor_meta.has_field(fieldname):
		return sorted(warehouses)
	configured = frappe.db.sql(
		f"""
		select `{fieldname}` as warehouse
		from `tabPlant Floor`
		where ifnull(`{fieldname}`, '') != ''
		order by name
		for update
		""",
		as_dict=True,
	)
	configured_names = sorted(
		{row.get("warehouse") for row in configured if row.get("warehouse")}
	)
	if not configured_names:
		return sorted(warehouses)
	valid = frappe.db.sql(
		"""
		select name
		from `tabWarehouse`
		where name in %s
			and company = %s
			and is_group = 0
			and ifnull(disabled, 0) = 0
		order by name
		for update
		""",
		(tuple(configured_names), company),
		as_dict=True,
	)
	warehouses.update(row.get("name") for row in valid if row.get("name"))
	return sorted(warehouses)


def _get_item_inventory_room(
	company: str,
	item_code: str,
	max_stock_qty: float,
	*,
	current_read: bool = False,
):
	from injection_aps.services import availability

	warehouses = (
		_get_finished_goods_warehouses_for_update(company)
		if current_read
		else availability._get_finished_goods_warehouses(company)
	)
	if not warehouses:
		# Without an explicit FG scope, counting every company Bin would mix RM/WIP/
		# scrap into finished inventory. Block Prebuild conservatively instead.
		return 0.0, 0.0
	query = """
		select coalesce(sum(greatest(ifnull(bin.actual_qty, 0), 0)), 0) as qty
		from `tabBin` bin
		inner join `tabWarehouse` wh on wh.name = bin.warehouse
		where wh.company = %(company)s
			and wh.is_group = 0
			and ifnull(wh.disabled, 0) = 0
			and bin.warehouse in %(warehouses)s
			and bin.item_code = %(item_code)s
		"""
	if current_read:
		query += " for update"
	rows = frappe.db.sql(
		query,
		{"company": company, "warehouses": warehouses, "item_code": item_code},
		as_dict=True,
	)
	current = flt(rows[0].qty) if rows else 0
	room = max(flt(max_stock_qty) - current, 0) if flt(max_stock_qty) > 0 else None
	return current, room


def _get_warehouse_capacity_room(
	company: str,
	plant_floor: str | None,
	item_code: str,
	settings: dict[str, Any],
	*,
	current_read: bool = False,
):
	warehouse = None
	warehouse_row = None
	fieldname = settings.get("plant_floor_fg_warehouse_field")
	if plant_floor and fieldname and frappe.get_meta("Plant Floor").has_field(fieldname):
		if current_read:
			plant_floor_row = _get_named_fields_for_update(
				"Plant Floor", plant_floor, [fieldname]
			)
			warehouse = plant_floor_row.get(fieldname) if plant_floor_row else None
		else:
			warehouse = frappe.db.get_value("Plant Floor", plant_floor, fieldname)
	if warehouse:
		warehouse_row = (
			_get_named_fields_for_update(
				"Warehouse",
				warehouse,
				["company", "is_group", "disabled", "custom_aps_capacity_qty"],
			)
			if current_read
			else frappe.db.get_value(
				"Warehouse",
				warehouse,
				["company", "is_group", "disabled", "custom_aps_capacity_qty"],
				as_dict=True,
			)
		)
		if (
			not warehouse_row
			or warehouse_row.company != company
			or cint(warehouse_row.is_group)
			or cint(warehouse_row.disabled)
		):
			warehouse = None
			warehouse_row = None
	if not warehouse:
		if current_read:
			warehouse_rows = frappe.db.sql(
				"""
				select name
				from `tabWarehouse`
				where company = %s
					and is_group = 0
					and ifnull(disabled, 0) = 0
					and warehouse_type = 'Finished Goods'
				order by name
				limit 1
				for update
				""",
				company,
				as_dict=True,
			)
			warehouse = warehouse_rows[0].get("name") if warehouse_rows else None
		else:
			warehouse = frappe.db.get_value(
				"Warehouse",
				{"company": company, "is_group": 0, "disabled": 0, "warehouse_type": "Finished Goods"},
				"name",
			)
	if not warehouse:
		return None, None
	if not warehouse_row:
		warehouse_row = (
			_get_named_fields_for_update(
				"Warehouse",
				warehouse,
				["company", "is_group", "disabled", "custom_aps_capacity_qty"],
			)
			if current_read
			else frappe.db.get_value(
				"Warehouse",
				warehouse,
				["company", "is_group", "disabled", "custom_aps_capacity_qty"],
				as_dict=True,
			)
		)
	if (
		not warehouse_row
		or warehouse_row.company != company
		or cint(warehouse_row.is_group)
		or cint(warehouse_row.disabled)
	):
		return None, None
	capacity = flt(warehouse_row.custom_aps_capacity_qty)
	if capacity <= 0:
		return warehouse, None
	# Warehouse capacity is shared by every item in the warehouse. Looking up only
	# the current finished item lets each item reuse the same physical free space.
	used_query = """
		select item.stock_uom, coalesce(sum(greatest(bin.actual_qty, 0)), 0) as qty
		from `tabBin` bin
		inner join `tabItem` item on item.name = bin.item_code
		where bin.warehouse = %s
			and greatest(ifnull(bin.actual_qty, 0), 0) > 0
		group by item.stock_uom
		"""
	if current_read:
		used_query += " for update"
	used_rows = frappe.db.sql(
		used_query,
		(warehouse,),
		as_dict=True,
	)
	stock_uoms = {row.stock_uom for row in used_rows if row.stock_uom}
	if current_read:
		item_row = _get_named_fields_for_update("Item", item_code, ["stock_uom"])
		target_stock_uom = item_row.get("stock_uom") if item_row else None
	else:
		target_stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
	if target_stock_uom:
		stock_uoms.add(target_stock_uom)
	if len(stock_uoms) > 1:
		# custom_aps_capacity_qty has no conversion/unit metadata. Adding raw quantities
		# across unlike stock UOMs is unsafe, so do not permit Prebuild in this scope.
		return warehouse, 0.0
	used = sum(max(flt(row.qty), 0) for row in used_rows)
	return warehouse, max(capacity - used, 0)


def _get_valid_plant_floor_warehouse(
	company: str,
	plant_floor: str | None,
	fieldname: str | None,
	*,
	current_read: bool = False,
) -> str | None:
	if not plant_floor or not fieldname or not frappe.get_meta("Plant Floor").has_field(fieldname):
		return None
	if current_read:
		plant_floor_row = _get_named_fields_for_update(
			"Plant Floor", plant_floor, [fieldname]
		)
		warehouse = plant_floor_row.get(fieldname) if plant_floor_row else None
	else:
		warehouse = frappe.db.get_value("Plant Floor", plant_floor, fieldname)
	if not warehouse:
		return None
	row = (
		_get_named_fields_for_update(
			"Warehouse", warehouse, ["company", "is_group", "disabled"]
		)
		if current_read
		else frappe.db.get_value(
			"Warehouse",
			warehouse,
			["company", "is_group", "disabled"],
			as_dict=True,
		)
	)
	if not row or row.company != company or cint(row.is_group) or cint(row.disabled):
		return None
	return warehouse


def _get_material_ready_qty(
	company: str,
	item_code: str,
	warehouse: str | None = None,
	resource_details: dict[str, Any] | None = None,
	current_read: bool = False,
) -> float | None:
	if current_read:
		bom_rows = frappe.db.sql(
			"""
			select name, quantity
			from `tabBOM`
			where company = %s
				and item = %s
				and docstatus = 1
				and is_active = 1
				and is_default = 1
			order by name
			limit 1
			for update
			""",
			(company, item_code),
			as_dict=True,
		)
		bom = bom_rows[0] if bom_rows else None
	else:
		bom = frappe.db.get_value(
			"BOM",
			{"company": company, "item": item_code, "docstatus": 1, "is_active": 1, "is_default": 1},
			["name", "quantity"],
			as_dict=True,
		)
	if not bom:
		return None
	if current_read:
		components = frappe.db.sql(
			"""
			select item_code, qty, source_warehouse
			from `tabBOM Item`
			where parent = %s
			order by idx
			for update
			""",
			bom.get("name"),
			as_dict=True,
		)
	else:
		components = frappe.get_all(
			"BOM Item",
			filters={"parent": bom.name},
			fields=["item_code", "qty", "source_warehouse"],
		)
	if not components:
		return None
	component_warehouses = sorted(
		{
			(component.get("source_warehouse") if hasattr(component, "get") else component.source_warehouse)
			or warehouse
			for component in components
			if (
				(component.get("source_warehouse") if hasattr(component, "get") else component.source_warehouse)
				or warehouse
			)
		}
	)
	raw_material_warehouses = _get_raw_material_warehouse_set(
		company,
		component_warehouses,
		current_read=current_read,
	)
	requirements = _aggregate_bom_component_requirements(
		company,
		flt(bom.get("quantity")),
		components,
		default_warehouse=warehouse,
		raw_material_warehouses=raw_material_warehouses,
	)
	for requirement in requirements:
		requirement["available_qty"] = _get_component_available_qty(
			company,
			requirement["item_code"],
			requirement["warehouse"],
			current_read=current_read,
		)
	ready_qty = min(
		(
			flt(requirement.get("available_qty")) / flt(requirement.get("qty_per_unit"))
			for requirement in requirements
			if flt(requirement.get("qty_per_unit")) > 0
		),
		default=None,
	)
	if resource_details is not None:
		resource_details["requirements"] = requirements
	return max(flt(ready_qty), 0) if ready_qty is not None else None


def _aggregate_bom_component_requirements(
	company: str,
	bom_qty: float,
	components: list[Any],
	*,
	default_warehouse: str | None = None,
	raw_material_warehouses: set[str] | None = None,
) -> list[dict[str, Any]]:
	bom_qty = flt(bom_qty)
	if bom_qty <= CAPACITY_TOLERANCE:
		return []
	resources: dict[str, dict[str, Any]] = {}
	for component in components:
		component_warehouse = component.source_warehouse or default_warehouse
		per_unit = flt(component.qty) / bom_qty
		if per_unit <= 0:
			continue
		# Every component consumes the company-wide item pool. A source-warehouse
		# component additionally consumes its warehouse sub-pool. This overlapping
		# hierarchy prevents a wildcard BOM from reusing stock already committed by
		# a warehouse-specific BOM while still enforcing the source warehouse limit.
		resource_scopes = []
		if not component_warehouse or (
			raw_material_warehouses is None
			or component_warehouse in raw_material_warehouses
		):
			resource_scopes.append((f"{company}|*|{component.item_code}", None))
		if component_warehouse:
			resource_scopes.append(
				(f"{company}|{component_warehouse}|{component.item_code}", component_warehouse)
			)
		for resource_key, resource_warehouse in resource_scopes:
			if resource_key not in resources:
				resources[resource_key] = {
					"resource_key": resource_key,
					"item_code": component.item_code,
					"warehouse": resource_warehouse,
					"qty_per_unit": 0.0,
				}
			resources[resource_key]["qty_per_unit"] += per_unit
	return list(resources.values())


def _get_raw_material_warehouse_set(
	company: str,
	warehouses: list[str],
	*,
	current_read: bool,
) -> set[str]:
	if not warehouses:
		return set()
	if current_read:
		rows = frappe.db.sql(
			"""
			select name
			from `tabWarehouse`
			where name in %s
				and company = %s
				and is_group = 0
				and ifnull(disabled, 0) = 0
				and warehouse_type = 'Raw Material'
			order by name
			for update
			""",
			(tuple(warehouses), company),
			as_dict=True,
		)
	else:
		rows = frappe.get_all(
			"Warehouse",
			filters={
				"name": ("in", warehouses),
				"company": company,
				"is_group": 0,
				"disabled": 0,
				"warehouse_type": "Raw Material",
			},
			fields=["name"],
		)
	return {row.get("name") for row in rows if row.get("name")}


def _get_component_available_qty(
	company: str,
	item_code: str,
	warehouse: str | None,
	*,
	current_read: bool = False,
) -> float:
	# A BOM without a source warehouse is not permission to consume every Bin in
	# the company.  Only explicitly typed Raw Material warehouses form the
	# wildcard pool; WIP, Finished Goods, Scrap and unclassified warehouses remain
	# unavailable until a source warehouse is configured.
	warehouse_condition = "and wh.warehouse_type = 'Raw Material'"
	params: list[Any] = [item_code, company]
	if warehouse:
		warehouse_condition = "and bin.warehouse = %s"
		params.append(warehouse)
	query = f"""
		select coalesce(sum(greatest(
			ifnull(bin.actual_qty, 0)
			- greatest(
				greatest(ifnull(bin.reserved_qty, 0), 0),
				greatest(ifnull(bin.reserved_stock, 0), 0)
			)
			- greatest(ifnull(bin.reserved_qty_for_production, 0), 0)
			- greatest(ifnull(bin.reserved_qty_for_sub_contract, 0), 0)
			- greatest(ifnull(bin.reserved_qty_for_production_plan, 0), 0),
			0
		)), 0) as qty
		from `tabBin` bin
		inner join `tabWarehouse` wh on wh.name = bin.warehouse
		where bin.item_code = %s
			and wh.company = %s
			and wh.is_group = 0
			and ifnull(wh.disabled, 0) = 0
			{warehouse_condition}
		"""
	if current_read:
		query += " for update"
	rows = frappe.db.sql(
		query,
		tuple(params),
		as_dict=True,
	)
	return max(flt(rows[0].qty), 0) if rows else 0


def _build_source_snapshot(
	run_doc,
	result_rows,
	segment_rows,
	*,
	cross_result_rows: dict[str, dict[str, Any]] | None = None,
	resource_demands: list[dict[str, Any]] | None = None,
	downtime_windows: list[dict[str, Any]] | None = None,
	fixed_intervals: dict[str, list[tuple[Any, Any]]] | None = None,
	mold_fixed_intervals: dict[str, list[tuple[Any, Any]]] | None = None,
	high_cancellation_risk_percent: float | None = None,
	finished_goods_stock_by_item: dict[str, float] | None = None,
):
	return {
		"run": run_doc.name,
		"modified": str(run_doc.modified),
		"horizon_start": str(run_doc.horizon_start),
		"horizon_end": str(run_doc.horizon_end),
		"results": [
			{
				"name": row.get("name"),
				"modified": str(row.get("modified") or ""),
				"status": row.get("status"),
				"risk_status": row.get("risk_status"),
				"blocking_reason": row.get("blocking_reason"),
				"customer": row.get("customer"),
				"sales_order": row.get("sales_order"),
				"sales_order_item": row.get("sales_order_item"),
				"demand_source_snapshot_json": row.get("demand_source_snapshot_json") or "",
				"fulfillment_baseline_json": row.get("fulfillment_baseline_json") or "",
				"strategy": row.get("production_strategy"),
				"demand_source": row.get("demand_source"),
				"demand_confidence": row.get("demand_confidence"),
				"cancellation_risk_percent": flt(row.get("cancellation_risk_percent")),
				"prebuild_allowed": cint(row.get("prebuild_allowed")),
				"max_prebuild_days": cint(row.get("max_prebuild_days")),
				"is_urgent": cint(row.get("is_urgent")),
				"planned_qty": flt(row.get("planned_qty")),
				"requested_date": str(row.get("requested_date")),
				"net_requirement_evidence_complete": cint(
					row.get("net_requirement_evidence_complete")
				),
			}
			for row in sorted(result_rows.values(), key=lambda item: item.get("name") or "")
		],
		"segments": [
			{
				"name": row.get("name"),
				"modified": str(row.get("modified") or ""),
				"parent": row.get("parent"),
				"workstation": row.get("workstation"),
				"mould_reference": row.get("mould_reference"),
				"start": str(row.get("start_time")),
				"end": str(row.get("end_time")),
				"qty": flt(row.get("planned_qty")),
				"setup_minutes": flt(row.get("setup_minutes")),
				"changeover_minutes": flt(row.get("changeover_minutes")),
				"production_mode": row.get("production_mode"),
				"status": row.get("segment_status"),
				"actual_status": row.get("actual_status"),
				"actual_qty": flt(row.get("actual_completed_qty")),
				"locked": cint(row.get("is_locked")),
				"anchor_strength": cint(row.get("anchor_strength")),
				"plant_floor": row.get("plant_floor"),
				"linked_work_order": row.get("linked_work_order"),
				"linked_work_order_active": cint(row.get("linked_work_order_active")),
				"linked_work_order_stopped": cint(row.get("linked_work_order_stopped")),
				"linked_scheduling_item": row.get("linked_scheduling_item"),
			}
			for row in sorted(segment_rows, key=lambda item: item.get("name") or "")
		],
		"cross_run_finished_goods_claims": _build_finished_goods_claim_snapshot(
			cross_result_rows or {}
		),
		"finished_goods_stock_by_item": {
			item_code: flt(qty)
			for item_code, qty in sorted((finished_goods_stock_by_item or {}).items())
		},
		"shared_resource_inputs": _build_shared_resource_snapshot(resource_demands or []),
		"capacity_constraints": {
			"high_cancellation_risk_percent": flt(high_cancellation_risk_percent),
			"downtime": _build_downtime_snapshot(downtime_windows or []),
			"workstation_fixed_intervals": _build_interval_snapshot(fixed_intervals or {}),
			"mold_fixed_intervals": _build_interval_snapshot(mold_fixed_intervals or {}),
		},
	}


def _build_finished_goods_claim_snapshot(
	result_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
	rows = []
	for result in result_rows.values():
		claim_qty = _remaining_finished_goods_stock_claim(result)
		if claim_qty <= CAPACITY_TOLERANCE:
			continue
		rows.append(
			{
				"result": result.get("name"),
				"reservation_run": result.get("reservation_run"),
				"item_code": result.get("item_code"),
				"demand_qty": flt(result.get("demand_qty")),
				"available_stock_qty": flt(result.get("available_stock_qty")),
				"delivered_qty": flt(result.get("delivered_qty")),
				"remaining_claim_qty": claim_qty,
				"net_requirement_evidence_complete": cint(
					result.get("net_requirement_evidence_complete")
				),
			}
		)
	return sorted(
		rows,
		key=lambda row: (
			str(row.get("reservation_run") or ""),
			str(row.get("result") or ""),
			str(row.get("item_code") or ""),
		),
	)


def _build_downtime_snapshot(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	rows = [
		{
			"name": row.get("name"),
			"company": row.get("company"),
			"scope": row.get("scope"),
			"plant_floor": row.get("plant_floor"),
			"workstation": row.get("workstation"),
			"start": str(row.get("start_time")),
			"end": str(row.get("end_time")),
			"available_capacity_percent": flt(row.get("available_capacity_percent")),
			"status": row.get("status"),
			"planning_run": row.get("planning_run"),
		}
		for row in windows
	]
	return sorted(
		rows,
		key=lambda row: (
			str(row.get("start") or ""),
			str(row.get("end") or ""),
			str(row.get("scope") or ""),
			str(row.get("plant_floor") or ""),
			str(row.get("workstation") or ""),
			str(row.get("name") or ""),
		),
	)


def _build_interval_snapshot(intervals_by_resource: dict[str, list[tuple[Any, Any]]]) -> list[dict[str, str]]:
	return sorted(
		[
			{"resource": str(resource or ""), "start": str(start), "end": str(end)}
			for resource, intervals in intervals_by_resource.items()
			for start, end in intervals
		],
		key=lambda row: (row["resource"], row["start"], row["end"]),
	)


def _build_shared_resource_snapshot(demands: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Return deterministic inputs used to cap shared resources during analysis."""
	rows = []
	for demand in demands:
		material_requirements = [
			{
				"resource_key": requirement.get("resource_key"),
				"qty_per_unit": flt(requirement.get("qty_per_unit")),
				"available_qty": _optional_snapshot_qty(requirement.get("available_qty")),
			}
			for requirement in demand.get("material_requirements") or []
		]
		material_requirements.sort(
			key=lambda row: (
				str(row.get("resource_key") or ""),
				flt(row.get("qty_per_unit")),
				row.get("available_qty") is None,
				flt(row.get("available_qty")),
			)
		)
		rows.append(
			{
				"key": demand.get("key"),
				"result": demand.get("result"),
				"segment": demand.get("segment"),
				"priority": cint(demand.get("priority")),
				"strategy": demand.get("strategy"),
				"due_time": str(demand.get("due_time")),
				"due_granularity": demand.get("due_granularity"),
				"setup_minutes": flt(demand.get("setup_minutes")),
				"prebuild_allowed": cint(demand.get("prebuild_allowed")),
				"max_prebuild_days": cint(demand.get("max_prebuild_days")),
				"shelf_life_days": cint(demand.get("shelf_life_days")),
				"minimum_batch_qty": flt(demand.get("minimum_batch_qty")),
				"demand_confidence": demand.get("demand_confidence"),
				"cancellation_risk_percent": flt(demand.get("cancellation_risk_percent")),
				"fixed_commitment": cint(demand.get("fixed_commitment")),
				"stock_retained": cint(demand.get("stock_retained")),
					"production_mode": demand.get("production_mode"),
					"linked_work_order": demand.get("linked_work_order"),
					"linked_work_order_active": cint(demand.get("linked_work_order_active")),
					"linked_work_order_stopped": cint(demand.get("linked_work_order_stopped")),
				"start_time": str(demand.get("start_time") or ""),
				"end_time": str(demand.get("end_time") or ""),
				"reservation_run": demand.get("reservation_run"),
				"resource_consumption_qty": _optional_snapshot_qty(
					demand.get("resource_consumption_qty")
				),
				"resource_material_consumption_qty": _optional_snapshot_qty(
					demand.get("resource_material_consumption_qty")
				),
				"existing_work_order_material_credit_qty": _optional_snapshot_qty(
					demand.get("existing_work_order_material_credit_qty")
				),
				"resource_prebuild_qty": _optional_snapshot_qty(
					_snapshot_resource_prebuild_qty(demand)
				),
				"finished_goods_stock_claim_qty": _optional_snapshot_qty(
					demand.get("finished_goods_stock_claim_qty")
				),
				"current_inventory_qty": _optional_snapshot_qty(demand.get("current_inventory_qty")),
				"inventory_room_qty": _optional_snapshot_qty(demand.get("inventory_room_qty")),
				"inventory_resource_key": demand.get("inventory_resource_key"),
				"warehouse_room_qty": _optional_snapshot_qty(demand.get("warehouse_room_qty")),
				"warehouse_resource_key": demand.get("warehouse_resource_key"),
				"target_stock_uom": demand.get("target_stock_uom"),
				"material_ready_qty": _optional_snapshot_qty(demand.get("material_ready_qty")),
				"material_requirements": material_requirements,
			}
		)
	return sorted(
		rows,
		key=lambda row: (
			str(row.get("key") or ""),
			str(row.get("result") or ""),
			str(row.get("segment") or ""),
		),
	)


def _snapshot_resource_prebuild_qty(demand: dict[str, Any]) -> float | None:
	if not cint(demand.get("fixed_commitment")):
		return demand.get("resource_prebuild_qty")
	qty = max(flt(demand.get("qty")), 0)
	if qty <= CAPACITY_TOLERANCE:
		return 0
	remaining = demand.get("resource_consumption_qty")
	remaining_ratio = 1 if remaining is None else min(max(flt(remaining) / qty, 0), 1)
	if cint(demand.get("stock_retained")):
		return qty * remaining_ratio
	prebuild_qty, _jit_qty, _late_qty = _fixed_mode_quantities(demand)
	return prebuild_qty * remaining_ratio


def _optional_snapshot_qty(value: Any) -> float | None:
	return None if value is None else flt(value)


def _persist_capacity_analysis(run_doc, analysis: dict[str, Any]):
	from injection_aps.services.v2_flags import is_v2_enabled

	v2_enabled = is_v2_enabled()
	for key in (
		"confirmation_fingerprint",
		"confirmed_by",
		"confirmed_on",
		"applied_plan_fingerprint",
		"applied_resource_fingerprint",
	):
		analysis.pop(key, None)
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
		if v2_enabled:
			status = "Hard Blocked" if any(row.get("status") == "Hard Blocked" for row in rows) else (
				"Acknowledgment Required" if requires_confirmation else "Ready"
			)
		else:
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
	status = (
		analysis.get("readiness_status")
		if v2_enabled else (
			"Blocked" if analysis["summary"].get("blocked_demands") else (
				"Confirmation Required" if analysis["summary"].get("requires_confirmation") else "Suggestion Ready"
			)
		)
	)
	resolution_count = len(analysis.get("hard_blockers") or []) if v2_enabled else 0
	excluded_count = len({row.get("result") for row in analysis.get("excluded_demands") or [] if row.get("result")}) if v2_enabled else 0
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
			# Confirmation is evidence for exactly one fingerprint.  A new Analyze,
			# including a repeated Analyze of changed inputs, must never inherit it.
			"capacity_balance_confirmed_by": None,
			"capacity_balance_confirmed_on": None,
			"capacity_balance_applied_on": None,
			"constraint_resolution_count": resolution_count,
			"excluded_commitment_count": excluded_count,
		},
		update_modified=False,
	)
