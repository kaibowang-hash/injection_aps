from __future__ import annotations

import json
import math
import time
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime

from injection_aps.services import bom_planning, campaign_planning, v2_flags
from injection_aps.services.solver.serialization import canonical_json, fingerprint


QTY_TOLERANCE = 0.000001
MIN_STABLE_SAMPLES = 3
EMERGENCY_ROLES = {"Manufacturing Manager"}
APPROVAL_ROLES = {"GMC"}


def forecast_segment(
	segment: dict[str, Any],
	*,
	execution_cutoff,
	rate_samples: list[float] | None = None,
	standard_rate_per_minute: float | None = None,
) -> dict[str, Any]:
	"""Forecast one segment without changing its current plan.

	Only actual good output satisfies the planned demand; scrap stays visible but does
	not reduce the remaining good quantity. A stable actual rate needs at least three positive samples. Otherwise the
	frozen standard rate is used and the row explicitly requires acknowledgment.
	"""
	cutoff = get_datetime(execution_cutoff)
	start = get_datetime(segment.get("current_start_time") or segment.get("start_time"))
	end = get_datetime(segment.get("current_end_time") or segment.get("end_time"))
	planned = max(flt(segment.get("planned_qty")), 0)
	good = max(flt(segment.get("actual_good_qty") or segment.get("actual_completed_qty")), 0)
	scrap = max(flt(segment.get("actual_scrap_qty")), 0)
	remaining = max(planned - good, 0)
	samples = [float(value) for value in rate_samples or [] if float(value or 0) > 0]
	stable = len(samples) >= MIN_STABLE_SAMPLES
	standard = float(standard_rate_per_minute or 0)
	if standard <= 0 and end > start:
		standard = planned / max((end - start).total_seconds() / 60, 1)
	effective_rate = median(samples[-5:]) if stable else standard
	if remaining > QTY_TOLERANCE and effective_rate <= 0:
		raise ValueError(f"Segment {segment.get('name') or '-'} has no usable actual or standard production rate.")
	controllable_start = max(cutoff, start)
	unfinished_setup = max(flt(segment.get("unfinished_setup_minutes")), 0)
	minutes = unfinished_setup + (remaining / effective_rate if remaining > QTY_TOLERANCE else 0)
	forecast_end = controllable_start + timedelta(minutes=minutes)
	if remaining <= QTY_TOLERANCE:
		forecast_end = min(cutoff, end) if cutoff >= start else start
	execution_state = str(segment.get("execution_state") or "")
	started = execution_state in {"In Progress", "Material Transfer"} or good > QTY_TOLERANCE or scrap > QTY_TOLERANCE
	return {
		"segment": segment.get("segment") or segment.get("name"),
		"forecast_start_time": controllable_start,
		"forecast_end_time": forecast_end,
		"planned_qty": planned,
		"actual_good_qty": good,
		"actual_scrap_qty": scrap,
		"remaining_qty": remaining,
		"effective_rate_per_minute": effective_rate,
		"rate_source": "Recent Stable Actual" if stable else "Standard Fallback",
		"sample_count": len(samples),
		"fallback_rate_used": bool(not stable and started and remaining > QTY_TOLERANCE),
	}


def solve_local_replan(
	segments: list[dict[str, Any]],
	*,
	execution_cutoff,
	next_shift_start,
	blocked_intervals: list[dict[str, Any]] | None = None,
	emergency: bool = False,
	time_limit_seconds: int = 30,
) -> dict[str, Any]:
	"""Solve a fixed-resource local replan with exact machine/mold NoOverlap.

	The current resource order is preserved as a stability constraint. Frozen and
	executing intervals are fixed exactly; only unstarted flexible intervals may move.
	"""
	from ortools.sat.python import cp_model

	started_on = time.monotonic()
	cutoff = get_datetime(execution_cutoff)
	minimum_move_time = cutoff if emergency else get_datetime(next_shift_start)
	rows = [dict(row) for row in segments]
	blocked_intervals = [dict(row) for row in blocked_intervals or []]
	if not rows:
		return {"rows": [], "status": "Optimal", "runtime_seconds": 0.0, "fallback_used": False}
	origin = min(
		[cutoff, minimum_move_time]
		+ [get_datetime(row["current_start_time"]) for row in rows]
		+ [get_datetime(row["start_time"]) for row in blocked_intervals]
	)
	horizon_end = max(
		[minimum_move_time + timedelta(days=14)]
		+ [get_datetime(row.get("forecast_end_time") or row["current_end_time"]) + timedelta(days=7) for row in rows]
		+ [get_datetime(row["end_time"]) for row in blocked_intervals]
	)
	horizon_minutes = max(int(math.ceil((horizon_end - origin).total_seconds() / 60)), 1)
	model = cp_model.CpModel()
	variables: dict[str, dict[str, Any]] = {}
	machine_intervals: dict[str, list[Any]] = defaultdict(list)
	mold_intervals: dict[str, list[Any]] = defaultdict(list)
	by_resource: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

	for row in rows:
		name = str(row["segment"])
		current_start = get_datetime(row["current_start_time"])
		current_end = get_datetime(row["current_end_time"])
		forecast_end = get_datetime(row.get("forecast_end_time") or current_end)
		frozen = bool(cint(row.get("is_frozen")) or row.get("execution_state") in {"In Progress", "Material Transfer", "Completed"})
		if frozen:
			duration = max(int(math.ceil((max(forecast_end, current_start) - current_start).total_seconds() / 60)), 0)
			start_minute = int(math.floor((current_start - origin).total_seconds() / 60))
			start_var = model.NewConstant(start_minute)
		else:
			duration = max(int(math.ceil((current_end - current_start).total_seconds() / 60)), 1)
			lower = max(int(math.ceil((minimum_move_time - origin).total_seconds() / 60)), 0)
			start_var = model.NewIntVar(lower, max(horizon_minutes - duration, lower), f"start|{name}")
		end_var = model.NewIntVar(-horizon_minutes, horizon_minutes * 2, f"end|{name}")
		model.Add(end_var == start_var + duration)
		interval = model.NewIntervalVar(start_var, duration, end_var, f"interval|{name}")
		variables[name] = {
			"row": row,
			"start": start_var,
			"end": end_var,
			"duration": duration,
			"frozen": frozen,
			"current_start_minute": int(round((current_start - origin).total_seconds() / 60)),
		}
		if row.get("workstation"):
			machine_intervals[str(row["workstation"])].append(interval)
			by_resource[("machine", str(row["workstation"]))].append(row)
		if row.get("mould_reference"):
			mold_intervals[str(row["mould_reference"])].append(interval)
			by_resource[("mold", str(row["mould_reference"]))].append(row)
	for index, blocked in enumerate(blocked_intervals):
		resource_type = str(blocked.get("resource_type") or "machine")
		resource = str(blocked.get("resource") or "")
		if not resource:
			continue
		start_minute = int(math.floor((get_datetime(blocked["start_time"]) - origin).total_seconds() / 60))
		end_minute = int(math.ceil((get_datetime(blocked["end_time"]) - origin).total_seconds() / 60))
		duration = max(end_minute - start_minute, 0)
		if duration <= 0:
			continue
		interval = model.NewIntervalVar(
			model.NewConstant(start_minute),
			duration,
			model.NewConstant(end_minute),
			f"blocked|{resource_type}|{resource}|{index}",
		)
		target_intervals = mold_intervals if resource_type == "mold" else machine_intervals
		target_intervals[resource].append(interval)
	for intervals in machine_intervals.values():
		model.AddNoOverlap(intervals)
	for intervals in mold_intervals.values():
		model.AddNoOverlap(intervals)
	for resource_rows in by_resource.values():
		ordered = sorted(resource_rows, key=lambda row: (get_datetime(row["current_start_time"]), str(row["segment"])))
		for previous, current in zip(ordered, ordered[1:]):
			model.Add(variables[str(current["segment"])]["start"] >= variables[str(previous["segment"])]["end"])

	deviations = []
	for name, values in variables.items():
		if values["frozen"]:
			continue
		deviation = model.NewIntVar(0, horizon_minutes * 2, f"deviation|{name}")
		model.AddAbsEquality(deviation, values["start"] - values["current_start_minute"])
		deviations.append(deviation)
	model.Minimize(sum(deviations))
	solver = cp_model.CpSolver()
	solver.parameters.max_time_in_seconds = max(int(time_limit_seconds), 1)
	solver.parameters.num_search_workers = 1
	solver.parameters.random_seed = 20260814
	status = solver.Solve(model)
	if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
		fallback_rows = propagate_resource_forecast(
			rows,
			execution_cutoff=cutoff,
			next_shift_start=minimum_move_time,
			emergency=emergency,
			blocked_intervals=blocked_intervals,
		)
		return {
			"rows": fallback_rows,
			"status": "Fallback",
			"runtime_seconds": time.monotonic() - started_on,
			"fallback_used": True,
		}

	result = []
	for row in rows:
		values = variables[str(row["segment"])]
		proposed_start = origin + timedelta(minutes=int(solver.Value(values["start"])))
		proposed_end = origin + timedelta(minutes=int(solver.Value(values["end"])))
		current_start = get_datetime(row["current_start_time"])
		current_end = get_datetime(row["current_end_time"])
		if values["frozen"]:
			diff_type = "Delayed by Execution" if proposed_end > current_end else "Frozen"
		elif proposed_start > current_start:
			diff_type = "Moved Later"
		elif proposed_start < current_start:
			diff_type = "Moved Earlier"
		else:
			diff_type = "Unchanged"
		result.append({
			**row,
			"proposed_start_time": proposed_start,
			"proposed_end_time": proposed_end,
			"diff_type": diff_type,
			"is_frozen": cint(values["frozen"]),
			"reason": (
				"Execution forecast is later than the current plan."
				if diff_type == "Delayed by Execution"
				else "Protected by execution/freeze ownership."
				if diff_type == "Frozen"
				else "Locally re-sequenced after exact machine/mold predecessors."
				if diff_type != "Unchanged"
				else "No material schedule difference."
			),
		})
	return {
		"rows": result,
		"status": "Optimal" if status == cp_model.OPTIMAL else "Feasible",
		"runtime_seconds": time.monotonic() - started_on,
		"fallback_used": False,
	}


def propagate_resource_forecast(
	segments: list[dict[str, Any]],
	*,
	execution_cutoff,
	next_shift_start,
	emergency: bool = False,
	blocked_intervals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
	"""Propagate delays independently along shared machine and mold resources."""
	cutoff = get_datetime(execution_cutoff)
	minimum_move_time = cutoff if emergency else get_datetime(next_shift_start)
	rows = [dict(row) for row in segments]
	blocked_intervals = [dict(row) for row in blocked_intervals or []]
	by_name = {str(row["segment"]): row for row in rows}
	predecessors: dict[str, set[str]] = defaultdict(set)
	for resource_field in ("workstation", "mould_reference"):
		by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
		for row in rows:
			if row.get(resource_field):
				by_resource[str(row[resource_field])].append(row)
		for resource_rows in by_resource.values():
			ordered = sorted(resource_rows, key=lambda row: (get_datetime(row["current_start_time"]), str(row["segment"])))
			for previous, current in zip(ordered, ordered[1:]):
				predecessors[str(current["segment"])].add(str(previous["segment"]))

	for row in sorted(rows, key=lambda value: (get_datetime(value["current_start_time"]), str(value["segment"]))):
		current_start = get_datetime(row["current_start_time"])
		current_end = get_datetime(row["current_end_time"])
		duration = max(current_end - current_start, timedelta())
		frozen = bool(cint(row.get("is_frozen")) or row.get("execution_state") in {"In Progress", "Material Transfer", "Completed"})
		if frozen:
			proposed_start = current_start
			proposed_end = max(get_datetime(row.get("forecast_end_time") or current_end), current_end if row.get("execution_state") == "Completed" else current_start)
			diff_type = "Delayed by Execution" if proposed_end > current_end else "Frozen"
		else:
			block_end = max(
				(by_name[name].get("proposed_end_time") or by_name[name].get("forecast_end_time") or by_name[name]["current_end_time"] for name in predecessors.get(str(row["segment"]), set())),
				default=current_start,
			)
			proposed_start = max(current_start, get_datetime(block_end), minimum_move_time)
			resource_keys = set()
			if row.get("workstation"):
				resource_keys.add(("machine", str(row["workstation"])))
			if row.get("mould_reference"):
				resource_keys.add(("mold", str(row["mould_reference"])))
			while True:
				proposed_end = proposed_start + duration
				conflicts = [
					blocked
					for blocked in blocked_intervals
					if (str(blocked.get("resource_type") or "machine"), str(blocked.get("resource") or "")) in resource_keys
					and get_datetime(blocked["start_time"]) < proposed_end
					and get_datetime(blocked["end_time"]) > proposed_start
				]
				if not conflicts:
					break
				proposed_start = max(get_datetime(blocked["end_time"]) for blocked in conflicts)
			proposed_end = proposed_start + duration
			diff_type = "Moved Later" if proposed_start > current_start else "Unchanged"
		row["proposed_start_time"] = proposed_start
		row["proposed_end_time"] = proposed_end
		row["diff_type"] = diff_type
		row["is_frozen"] = cint(frozen)
		row["reason"] = (
			"Execution forecast is later than the current plan."
			if diff_type == "Delayed by Execution"
			else "Protected by execution/freeze ownership."
			if diff_type == "Frozen"
			else "Shifted after the latest machine/mold predecessor forecast."
			if diff_type == "Moved Later"
			else "No material schedule difference."
		)
	return rows


def create_replan_cycle(
	baseline_run: str,
	*,
	shift_date,
	shift_type: str,
	execution_cutoff=None,
	cycle_type: str = "Scheduled Shift",
	reason: str | None = None,
) -> dict[str, Any]:
	_require_shift_replan()
	cycle_type = "Emergency Manual" if str(cycle_type or "").strip() in {"Emergency Manual", "紧急手工"} else "Scheduled Shift"
	if cycle_type == "Emergency Manual":
		_require_roles(EMERGENCY_ROLES)
		if not str(reason or "").strip():
			frappe.throw(_("An emergency replan requires a reason.", context="Injection APS"), frappe.ValidationError)
	run = frappe.get_doc("APS Planning Run", baseline_run)
	cutoff = get_datetime(execution_cutoff or now_datetime())
	shift_date = getdate(shift_date)
	shift_type = str(shift_type or "").strip()
	if not shift_type:
		frappe.throw(_("Shift Type is required.", context="Injection APS"), frappe.ValidationError)
	idempotency_key = fingerprint({
		"company": run.company,
		"plant_floor": run.get("plant_floor") or "",
		"shift_date": str(shift_date),
		"shift_type": shift_type,
		"execution_cutoff": cutoff.isoformat(),
		"baseline_run": run.name,
	})
	existing = frappe.db.get_value("APS Replan Cycle", {"idempotency_key": idempotency_key}, "name")
	if existing:
		return get_replan_cycle(existing)

	baseline = _load_baseline_segments(run.name)
	next_shift_start = _next_shift_start(shift_date, shift_type, cutoff)
	from injection_aps.services import planning

	downtime = planning._get_active_downtime_windows(
		company=run.company,
		plant_floors=[run.get("plant_floor")] if run.get("plant_floor") else None,
		horizon_start=cutoff,
		horizon_end=get_datetime(run.get("recovery_horizon_end_date") or run.horizon_end) + timedelta(days=1),
		run_name=run.name,
	)
	blocked_intervals = _local_replan_blocked_intervals(run, baseline, downtime)
	forecast_rows = []
	for row in baseline:
		samples = _recent_rate_samples(row, cutoff)
		forecast = forecast_segment(
			row,
			execution_cutoff=cutoff,
			rate_samples=samples,
			standard_rate_per_minute=_standard_rate(row),
		)
		forecast_rows.append({**row, **forecast})
	solver_result = solve_local_replan(
		forecast_rows,
		execution_cutoff=cutoff,
		next_shift_start=next_shift_start,
		blocked_intervals=blocked_intervals,
		emergency=cycle_type == "Emergency Manual",
		time_limit_seconds=max(v2_flags.get_v2_settings().get("shift_solver_time_limit_seconds") or 30, 1),
	)
	diffs = solver_result["rows"]
	bom_impacts = bom_planning.propagate_forecast_to_roots(run.name, forecast_rows) if v2_flags.get_v2_settings()["enable_multilevel_bom_planning"] else []
	fallback_used = any(cint(row.get("fallback_rate_used")) for row in forecast_rows if flt(row.get("remaining_qty")) > QTY_TOLERANCE)
	freshness_minutes = _freshness_minutes(baseline, cutoff)
	max_staleness = max(cint(v2_flags.get_v2_settings().get("max_execution_staleness_minutes")), 0)
	stale = bool(_has_active_execution(baseline) and freshness_minutes > max_staleness)
	fallback_required = bool(fallback_used or solver_result["fallback_used"] or stale)
	input_hash = fingerprint({
		"baseline": baseline,
		"cutoff": cutoff,
		"shift_date": shift_date,
		"shift_type": shift_type,
		"blocked_intervals": blocked_intervals,
		"source_snapshot": _shift_source_snapshot(run, cutoff),
	})
	solution_hash = fingerprint(diffs)
	counts = Counter(str(row.get("diff_type") or "Unchanged") for row in diffs)
	cycle = frappe.get_doc({
		"doctype": "APS Replan Cycle",
		"baseline_run": run.name,
		"company": run.company,
		"plant_floor": run.get("plant_floor"),
		"shift_date": shift_date,
		"shift_type": shift_type,
		"cycle_type": cycle_type,
		"status": "Acknowledgment Required" if fallback_required else "Proposal Ready",
		"execution_cutoff": cutoff,
		"freshness_status": "Stale" if stale else "Fallback Required" if fallback_required else "Fresh",
		"freshness_minutes": freshness_minutes,
		"fallback_rate_used": cint(fallback_used or solver_result["fallback_used"]),
		"input_fingerprint": input_hash,
		"solution_fingerprint": solution_hash,
		"idempotency_key": idempotency_key,
		"baseline_json": canonical_json(baseline),
		"forecast_json": canonical_json(forecast_rows),
		"diff_json": canonical_json(diffs),
		"solver_metrics_json": canonical_json({"mode": "Local CP-SAT", "status": solver_result["status"], "runtime_seconds": solver_result["runtime_seconds"], "next_shift_start": next_shift_start, "change_count": sum(value for key, value in counts.items() if key != "Unchanged"), "downtime_count": len(blocked_intervals), "bom_root_impacts": bom_impacts}),
		"unchanged_count": counts["Unchanged"],
		"moved_earlier_count": counts["Moved Earlier"],
		"moved_later_count": counts["Moved Later"],
		"resource_changed_count": counts["Machine/Mold Changed"],
		"quantity_changed_count": counts["Qty Changed"],
		"added_count": counts["Added"],
		"cancelled_count": counts["Cancelled Before Start"],
		"frozen_count": counts["Frozen"] + counts["Delayed by Execution"],
		"created_by_user": frappe.session.user,
		"audit_json": canonical_json({"created_by": frappe.session.user, "created_on": now_datetime(), "reason": str(reason or "").strip(), "automatic_apply": False}),
	})
	cycle.flags.aps_replan_transition = True
	cycle.insert(ignore_permissions=True)
	for row in forecast_rows:
		frappe.db.set_value("APS Schedule Segment", row["segment"], {
			"forecast_start_time": row["forecast_start_time"],
			"forecast_end_time": row["forecast_end_time"],
			"replan_cycle": cycle.name,
		}, update_modified=False)
	frappe.db.set_value("APS Planning Run", run.name, "latest_replan_cycle", cycle.name, update_modified=False)
	return get_replan_cycle(cycle.name)


def acknowledge_fallback_rate(cycle_name: str, *, reason: str, expected_fingerprint: str) -> dict[str, Any]:
	_require_roles(APPROVAL_ROLES)
	cycle = _cycle(cycle_name)
	_assert_cycle_fingerprint(cycle, expected_fingerprint)
	if cycle.status != "Acknowledgment Required":
		frappe.throw(_("This replan cycle does not use a fallback rate.", context="Injection APS"), frappe.ValidationError)
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required to acknowledge a fallback rate.", context="Injection APS"), frappe.ValidationError)
	_update_cycle(cycle, {
		"status": "Proposal Ready",
		"freshness_status": "Acknowledged",
		"fallback_acknowledged_by": frappe.session.user,
		"fallback_acknowledged_on": now_datetime(),
		"fallback_reason": reason,
	}, action="Fallback or stale execution evidence acknowledged")
	return get_replan_cycle(cycle.name)


def approve_replan_cycle(cycle_name: str, *, reason: str, expected_fingerprint: str) -> dict[str, Any]:
	_require_roles(APPROVAL_ROLES)
	cycle = _cycle(cycle_name)
	_assert_cycle_fingerprint(cycle, expected_fingerprint)
	if cycle.status != "Proposal Ready":
		frappe.throw(_("Only a ready replan proposal can be approved.", context="Injection APS"), frappe.ValidationError)
	if not cycle.shift_schedule_proposal_batch:
		frappe.throw(_("Generate the Shift Schedule proposal before approval.", context="Injection APS"), frappe.ValidationError)
	shift_batch = frappe.db.get_value(
		"APS Shift Schedule Proposal Batch",
		cycle.shift_schedule_proposal_batch,
		["status", "proposal_count"],
		as_dict=True,
	) or {}
	if cint(shift_batch.get("proposal_count")) and shift_batch.get("status") not in {"Reviewed", "Applied"}:
		frappe.throw(_("Review every Shift Schedule proposal row before approving the replan.", context="Injection APS"), frappe.ValidationError)
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required to approve a replan proposal.", context="Injection APS"), frappe.ValidationError)
	_update_cycle(cycle, {"status": "Approved", "approved_by": frappe.session.user, "approved_on": now_datetime(), "approval_reason": reason}, action="Replan approved")
	return get_replan_cycle(cycle.name)


def refresh_shift_actuals(baseline_run: str) -> dict[str, Any]:
	"""Refresh formal production and delivery facts before the cutoff snapshot."""
	_require_shift_replan()
	from injection_aps.services import delivery_sync, execution_sync

	run = frappe.get_doc("APS Planning Run", baseline_run)
	production = execution_sync.sync_production_for_run(baseline_run)
	scope_rows = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": baseline_run},
		fields=["customer", "item_code"],
		limit_page_length=0,
	)
	items_by_customer: dict[str | None, set[str]] = defaultdict(set)
	for row in scope_rows:
		if row.item_code:
			items_by_customer[row.customer or None].add(row.item_code)
	delivery = []
	for customer, item_codes in sorted(items_by_customer.items(), key=lambda row: str(row[0] or "")):
		delivery.append(
			delivery_sync.sync_delivery_allocations(
				run.company,
				customer=customer,
				item_codes=sorted(item_codes),
			)
		)
	return {
		"run": baseline_run,
		"refreshed_on": now_datetime(),
		"production": production,
		"delivery": delivery,
		"delivery_scope_count": len(delivery),
	}


def generate_replan_proposals(cycle_name: str, *, expected_fingerprint: str) -> dict[str, Any]:
	"""Generate a separately reviewable proposal for the next shift.

	The forecast does not change required good quantity, so the existing Applied Work
	Order proposal stays as the quantity boundary. Replan timing is emitted only into
	the Shift Schedule proposal; Current Plan remains untouched until Apply.
	"""
	_require_roles({"PMC", "GMC"})
	cycle = _cycle(cycle_name)
	_assert_cycle_fingerprint(cycle, expected_fingerprint)
	if cycle.status not in {"Proposal Ready", "Approved"}:
		frappe.throw(_("Acknowledge execution freshness and fallback risks before generating proposals.", context="Injection APS"), frappe.ValidationError)
	if cycle.shift_schedule_proposal_batch:
		return get_replan_cycle(cycle.name)
	from injection_aps.services import planning

	wo_batch = frappe.db.get_value(
		"APS Work Order Proposal Batch",
		{"planning_run": cycle.baseline_run, "status": "Applied"},
		"name",
		order_by="modified desc",
	)
	if not wo_batch:
		frappe.throw(_("Apply the Work Order proposal batch before generating the replan Shift Proposal.", context="Injection APS"), frappe.ValidationError)
	diffs = json.loads(cycle.diff_json or "[]")
	overrides = {
		str(row["segment"]): {
			"proposed_start_time": row.get("proposed_start_time"),
			"proposed_end_time": row.get("proposed_end_time"),
		}
		for row in diffs
		if row.get("segment") and not cint(row.get("is_frozen"))
	}
	proposal = planning.generate_shift_schedule_proposals(
		run_name=cycle.baseline_run,
		work_order_proposal_batch=wo_batch,
		release_horizon_days=0,
		release_from_date=cycle.shift_date,
		shift_type=cycle.shift_type,
		segment_overrides=overrides,
		replan_cycle=cycle.name,
	)
	_update_cycle(cycle, {
		"work_order_proposal_batch": wo_batch,
		"shift_schedule_proposal_batch": proposal["shift_schedule_proposal_batch"],
	}, action="Separate Work Order and Shift Schedule proposal references generated")
	return get_replan_cycle(cycle.name)


def apply_replan_cycle(cycle_name: str, *, expected_fingerprint: str) -> dict[str, Any]:
	_require_roles(APPROVAL_ROLES)
	cycle = _cycle(cycle_name)
	_assert_cycle_fingerprint(cycle, expected_fingerprint)
	if cycle.status == "Applied":
		return get_replan_cycle(cycle.name)
	if cycle.status != "Approved":
		frappe.throw(_("Approve the replan proposal before Apply.", context="Injection APS"), frappe.ValidationError)
	if not cycle.shift_schedule_proposal_batch:
		frappe.throw(_("Generate and review the Shift Schedule proposal before Apply.", context="Injection APS"), frappe.ValidationError)
	from injection_aps.services import planning

	shift_batch = frappe.db.get_value("APS Shift Schedule Proposal Batch", cycle.shift_schedule_proposal_batch, ["status", "proposal_count"], as_dict=True) or {}
	if cint(shift_batch.get("proposal_count")) and shift_batch.get("status") != "Applied":
		planning.apply_shift_schedule_proposals(cycle.shift_schedule_proposal_batch)
	diffs = json.loads(cycle.diff_json or "[]")
	segment_names = sorted({row.get("segment") for row in diffs if row.get("segment")})
	frappe.db.sql("select name from `tabAPS Replan Cycle` where name=%s for update", cycle.name)
	if segment_names:
		frappe.db.sql("select name from `tabAPS Schedule Segment` where name in %s order by name for update", (tuple(segment_names),))
	for row in diffs:
		if not row.get("segment") or cint(row.get("is_frozen")):
			continue
		segment = frappe.get_doc("APS Schedule Segment", row["segment"])
		if not segment.get("baseline_start_time"):
			segment.baseline_start_time = segment.start_time
		if not segment.get("baseline_end_time"):
			segment.baseline_end_time = segment.end_time
		segment.current_start_time = row["proposed_start_time"]
		segment.current_end_time = row["proposed_end_time"]
		segment.start_time = row["proposed_start_time"]
		segment.end_time = row["proposed_end_time"]
		segment.replan_cycle = cycle.name
		segment.assignment_reason = row.get("reason")
		segment.save(ignore_permissions=True)
		if segment.get("production_campaign") and segment.get("capacity_owner") == segment.name:
			campaign_planning.sync_campaign_derived_segments(segment.production_campaign, segment)
	_update_cycle(cycle, {"status": "Applied", "applied_by": frappe.session.user, "applied_on": now_datetime()}, action="Reviewed Shift Proposal and Current Plan applied atomically")
	return get_replan_cycle(cycle.name)


def get_replan_cycle(cycle_name: str) -> dict[str, Any]:
	cycle = _cycle(cycle_name)
	return {
		"name": cycle.name,
		"baseline_run": cycle.baseline_run,
		"company": cycle.company,
		"plant_floor": cycle.plant_floor,
		"shift_date": cycle.shift_date,
		"shift_type": cycle.shift_type,
		"cycle_type": cycle.cycle_type,
		"status": cycle.status,
		"execution_cutoff": cycle.execution_cutoff,
		"freshness_status": cycle.freshness_status,
		"freshness_minutes": cint(cycle.freshness_minutes),
		"fallback_rate_used": cint(cycle.fallback_rate_used),
		"input_fingerprint": cycle.input_fingerprint,
		"solution_fingerprint": cycle.solution_fingerprint,
		"work_order_proposal_batch": cycle.work_order_proposal_batch,
		"shift_schedule_proposal_batch": cycle.shift_schedule_proposal_batch,
		"counts": {
			"unchanged": cint(cycle.unchanged_count),
			"moved_earlier": cint(cycle.moved_earlier_count),
			"moved_later": cint(cycle.moved_later_count),
			"resource_changed": cint(cycle.resource_changed_count),
			"quantity_changed": cint(cycle.quantity_changed_count),
			"added": cint(cycle.added_count),
			"cancelled": cint(cycle.cancelled_count),
			"frozen": cint(cycle.frozen_count),
		},
		"diffs": json.loads(cycle.diff_json or "[]"),
	}


def scheduled_shift_replan() -> dict[str, int]:
	"""Scheduler entry point. It may create proposals but can never approve/apply."""
	settings = v2_flags.get_v2_settings()
	if not (settings["enable_aps_v2"] and settings["enable_shift_replan"]):
		return {"created": 0, "applied": 0}
	now = get_datetime(now_datetime()).replace(minute=0, second=0, microsecond=0)
	# The hourly hook acts only at the explicit one-hour-before-shift cutoffs.
	if now.hour not in {7, 19}:
		return {"created": 0, "applied": 0}
	shift_type = "Day Shift" if now.hour == 7 else "Night Shift"
	candidates = frappe.get_all(
		"APS Planning Run",
		filters={"run_type": "Formal", "status": "Applied", "approval_state": "Approved"},
		fields=["name", "company", "plant_floor", "modified"],
		order_by="modified desc",
		limit_page_length=0,
	)
	latest_by_scope = {}
	for row in candidates:
		latest_by_scope.setdefault((row.company, row.plant_floor or ""), row.name)
	created = 0
	failed = 0
	for run_name in latest_by_scope.values():
		try:
			refresh_shift_actuals(run_name)
			before = frappe.db.count(
				"APS Replan Cycle",
				{"baseline_run": run_name, "shift_date": getdate(now), "shift_type": shift_type, "execution_cutoff": now},
			)
			create_replan_cycle(
				run_name,
				shift_date=getdate(now),
				shift_type=shift_type,
				execution_cutoff=now,
				cycle_type="Scheduled Shift",
				reason="System-created next-shift proposal; automatic Apply is prohibited.",
			)
			created += cint(not before)
		except Exception:
			failed += 1
			frappe.log_error(frappe.get_traceback(), f"APS scheduled Shift Replan failed for {run_name}")
	return {"created": created, "applied": 0, "failed": failed}


def _local_replan_blocked_intervals(run, baseline, downtime) -> list[dict[str, Any]]:
	"""Expand zero-capacity downtime to exact machine intervals for local CP-SAT."""
	from injection_aps.services import planning

	resources = {
		(str(row.get("workstation") or ""), str(row.get("plant_floor") or run.get("plant_floor") or ""))
		for row in baseline
		if row.get("workstation")
	}
	result = []
	for window in downtime or []:
		if flt(window.get("available_capacity_percent")) > 0:
			continue
		for workstation, plant_floor in sorted(resources):
			if not planning._downtime_applies_to_target(
				window,
				workstation=workstation,
				plant_floor=plant_floor or None,
				company=run.company,
			):
				continue
			result.append({
				"resource_type": "machine",
				"resource": workstation,
				"start_time": get_datetime(window.get("start_time")),
				"end_time": get_datetime(window.get("end_time")),
				"source": window.get("name"),
				"reason": window.get("reason") or "Downtime",
			})
	return sorted(result, key=lambda row: (row["resource"], row["start_time"], row["end_time"], str(row.get("source") or "")))


def _shift_source_snapshot(run, cutoff) -> dict[str, Any]:
	"""Fingerprint live facts whose changes require a new immutable Cycle."""
	return {
		"run_modified": str(run.modified or ""),
		"execution_cutoff": str(get_datetime(cutoff)),
		"latest_schedule_revision": str(
			frappe.db.get_value(
				"Customer Delivery Schedule",
				{"company": run.company, "status": "Active"},
				"modified",
				order_by="modified desc",
			) or ""
		),
		"latest_production_allocation": str(
			frappe.db.get_value(
				"APS Production Allocation",
				{"planning_run": run.name, "is_effective": 1},
				"modified",
				order_by="modified desc",
			) or ""
		),
		"latest_delivery_allocation": str(
			frappe.db.get_value(
				"APS Delivery Allocation",
				{"company": run.company, "is_effective": 1},
				"modified",
				order_by="modified desc",
			) or ""
		),
	}


def _load_baseline_segments(run_name: str) -> list[dict[str, Any]]:
	rows = frappe.db.sql(
		"""
		select s.name as segment, s.parent as result, s.workstation, s.mould_reference,
			s.start_time, s.end_time, coalesce(s.current_start_time, s.start_time) as current_start_time,
			coalesce(s.current_end_time, s.end_time) as current_end_time, s.planned_qty,
			s.actual_completed_qty, s.actual_good_qty, s.actual_scrap_qty, s.actual_status,
			s.segment_status, s.is_locked, s.anchor_strength, s.linked_work_order,
			wo.status as work_order_status,
			coalesce((
				select max(pa.source_posting_time)
				from `tabAPS Production Allocation` pa
				where pa.segment=s.name and pa.is_effective=1
			), wo.modified) as execution_modified
		from `tabAPS Schedule Segment` s
		inner join `tabAPS Schedule Result` r on r.name=s.parent
		left join `tabWork Order` wo on wo.name=s.linked_work_order
		where r.planning_run=%s and s.parenttype='APS Schedule Result'
			and ifnull(s.segment_kind, 'Primary') in ('Primary', 'Manual')
			and ifnull(s.segment_status, '') not in ('Blocked', 'Cancelled')
		order by s.start_time, s.name
		""",
		run_name,
		as_dict=True,
	)
	result = []
	for row in rows:
		execution_state = _execution_state(row)
		result.append({
			**dict(row),
			"execution_state": execution_state,
			"is_frozen": cint(execution_state in {"In Progress", "Material Transfer", "Completed"} or cint(row.is_locked) or flt(row.anchor_strength) >= 2),
		})
	return result


def _execution_state(row) -> str:
	status = str(row.get("work_order_status") or row.get("actual_status") or "")
	if status in {"Completed", "Stopped"}:
		return "Completed"
	if status in {"In Process", "In Progress", "Work In Progress"}:
		return "In Progress"
	if status in {"Material Transferred for Manufacture", "Material Transfer"}:
		return "Material Transfer"
	return "Not Started"


def _recent_rate_samples(row: dict[str, Any], cutoff) -> list[float]:
	segment = row.get("segment")
	if not segment or not frappe.db.exists("DocType", "APS Production Allocation"):
		return []
	allocations = frappe.get_all(
		"APS Production Allocation",
		filters={"segment": segment, "is_effective": 1, "source_posting_time": ("<=", cutoff)},
		fields=["source_posting_time", "good_qty", "scrap_qty"],
		order_by="source_posting_time desc",
		limit=6,
	)
	ordered = sorted(allocations, key=lambda value: get_datetime(value.source_posting_time))
	samples = []
	for previous, current in zip(ordered, ordered[1:]):
		minutes = (get_datetime(current.source_posting_time) - get_datetime(previous.source_posting_time)).total_seconds() / 60
		qty = flt(current.good_qty) + flt(current.scrap_qty)
		if minutes > 0 and qty > 0:
			samples.append(qty / minutes)
	return samples


def _standard_rate(row: dict[str, Any]) -> float:
	start = get_datetime(row.get("current_start_time"))
	end = get_datetime(row.get("current_end_time"))
	return max(flt(row.get("planned_qty")), 0) / max((end - start).total_seconds() / 60, 1)


def _freshness_minutes(rows, cutoff) -> int:
	active = [row for row in rows if row.get("execution_state") in {"In Progress", "Material Transfer"} or flt(row.get("actual_good_qty")) > QTY_TOLERANCE or flt(row.get("actual_scrap_qty")) > QTY_TOLERANCE]
	values = [get_datetime(row.get("execution_modified")) for row in active if row.get("execution_modified")]
	if active and not values:
		return 10**9
	if not values:
		return 0
	return max(int((get_datetime(cutoff) - max(values)).total_seconds() / 60), 0)


def _has_active_execution(rows) -> bool:
	return any(
		row.get("execution_state") in {"In Progress", "Material Transfer"}
		or flt(row.get("actual_good_qty")) > QTY_TOLERANCE
		or flt(row.get("actual_scrap_qty")) > QTY_TOLERANCE
		for row in rows
	)


def _next_shift_start(shift_date, shift_type, cutoff):
	start_hour = 20 if str(shift_type).lower() in {"night", "night shift", "夜班"} else 8
	value = get_datetime(f"{shift_date} {start_hour:02d}:00:00")
	if value <= get_datetime(cutoff):
		value += timedelta(hours=12)
	return value


def _cycle(name):
	if not frappe.db.exists("APS Replan Cycle", name):
		frappe.throw(_("Replan Cycle {0} was not found.", context="Injection APS").format(name), frappe.DoesNotExistError)
	return frappe.get_doc("APS Replan Cycle", name)


def _assert_cycle_fingerprint(cycle, expected):
	if not expected or expected != cycle.solution_fingerprint:
		frappe.throw(_("The replan proposal changed. Refresh before continuing.", context="Injection APS"), frappe.ValidationError)


def _update_cycle(cycle, values, *, action: str | None = None):
	for key, value in values.items():
		cycle.set(key, value)
	if action:
		audit = json.loads(cycle.audit_json or "{}")
		events = list(audit.get("events") or [])
		events.append({"action": action, "user": frappe.session.user, "on": now_datetime()})
		audit["events"] = events
		cycle.audit_json = canonical_json(audit)
	cycle.flags.aps_replan_transition = True
	cycle.save(ignore_permissions=True)


def _require_shift_replan():
	settings = v2_flags.get_v2_settings()
	if not (settings["enable_aps_v2"] and settings["solver_engine"] == "CP-SAT" and settings["enable_shift_replan"]):
		frappe.throw(_("Enable APS V2, CP-SAT, and Shift Replan before creating a replan cycle.", context="Injection APS"), frappe.PermissionError)


def _require_roles(allowed):
	if not set(frappe.get_roles()).intersection(allowed):
		frappe.throw(_("You do not have permission for this replan action.", context="Injection APS"), frappe.PermissionError)
