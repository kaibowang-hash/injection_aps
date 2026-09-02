from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import replace

from .capacity_solver import SolverRuntimeUnavailable, solve_capacity
from .models import (
	CapacityAllocation,
	DemandOutcome,
	ScheduledTask,
	SolverInput,
	SolverMetrics,
	SolverSolution,
)
from .objectives import DELIVERY_OBJECTIVES, SCENARIO_LABELS, SCENARIO_PROFILES
from .sequence_solver import SequenceInfeasible, solve_sequence
from .serialization import input_fingerprint, solution_fingerprint
from .validator import validate_solution


def solve_scenarios(snapshot: SolverInput) -> tuple[SolverSolution, ...]:
	input_hash = input_fingerprint(snapshot)
	results = []
	recommended_capacity_targets = None
	recommended_p0_floor = None
	total_limit = max(float(snapshot.time_limit_seconds), 1.0)
	per_scenario_limit = max(total_limit / 3, 1.0)
	deadline = time.monotonic() + total_limit
	for scenario_key in ("recommended", "delivery_priority", "minimum_changeover"):
		required = (
			{name: recommended_capacity_targets[name] for name in DELIVERY_OBJECTIVES if name in recommended_capacity_targets}
			if scenario_key == "minimum_changeover" and recommended_capacity_targets
			else None
		)
		remaining = deadline - time.monotonic()
		if remaining <= 0.01:
			solution = _greedy_fallback(
				snapshot,
				scenario_key,
				input_hash,
				("Global solver time limit reached before this scenario; explicit fallback used.",),
			)
		else:
			try:
				capacity = solve_capacity(
					snapshot,
					SCENARIO_PROFILES[scenario_key],
					required_objectives=required,
					time_limit_seconds=min(per_scenario_limit, remaining),
				)
				if capacity.status == "No Feasible":
					solution = _greedy_fallback(snapshot, scenario_key, input_hash, capacity.warnings)
				else:
					remaining = deadline - time.monotonic()
					if remaining <= 0.01:
						solution = _greedy_fallback(
							snapshot,
							scenario_key,
							input_hash,
							capacity.warnings + ("Global solver time limit reached before sequence optimization; explicit fallback used.",),
						)
					else:
						tasks, sequence_metrics, sequence_runtime, sequence_status = solve_sequence(
							snapshot,
							capacity.allocations,
							time_limit_seconds=min(per_scenario_limit, remaining),
						)
						outcomes = _outcomes_from_tasks(snapshot, tasks)
						metrics = calculate_metrics(snapshot, outcomes, capacity.allocations, tasks, sequence_metrics)
						status = "Optimal" if capacity.status == "Optimal" and sequence_status == "Optimal" else "Feasible"
						solution = SolverSolution(
							scenario_key=scenario_key,
							scenario_label=SCENARIO_LABELS[scenario_key],
							status=status,
							engine="CP-SAT",
							input_fingerprint=input_hash,
							solution_fingerprint="",
							runtime_seconds=capacity.runtime_seconds + sequence_runtime,
							objective_values=capacity.objective_values,
							allocations=capacity.allocations,
							tasks=tasks,
							outcomes=tuple(outcomes),
							metrics=metrics,
							frozen_intervals=snapshot.frozen_intervals,
							best_bound=capacity.best_bound,
							objective_value=capacity.objective_value,
							gap_percent=_gap(capacity.objective_value, capacity.best_bound, status),
							warnings=capacity.warnings,
							explanation=_explanation(scenario_key, metrics, status),
						)
			except (SolverRuntimeUnavailable, SequenceInfeasible) as exc:
				solution = _greedy_fallback(snapshot, scenario_key, input_hash, (str(exc),))
		validation = validate_solution(snapshot, solution, required_p0_floor=recommended_p0_floor, raise_on_error=False)
		solution = replace(solution, validation=tuple((key, value) for key, value in validation.items()), solution_fingerprint=solution_fingerprint(solution))
		if not validation["valid"]:
			solution = replace(solution, status="Failed", warnings=solution.warnings + tuple(row["message"] for row in validation["errors"]))
		results.append(solution)
		if scenario_key == "recommended" and solution.status not in {"Failed", "No Feasible"}:
			recommended_capacity_targets = dict(solution.objective_values)
			recommended_p0_floor = _p0_floor_from_metrics(solution.metrics)
	return tuple(results)


def calculate_metrics(snapshot, outcomes, allocations, tasks, sequence_metrics):
	demands = {row.key: row for row in snapshot.demands}
	alternatives = {(demand.key, alternative.key): alternative for demand in snapshot.demands for alternative in demand.alternatives}
	campaign_owners = {row.capacity_owner_demand for row in snapshot.multi_output_groups}
	p0_outcomes = [row for row in outcomes if row.demand_key not in campaign_owners and demands[row.demand_key].admission_class == "P0"]
	optional = [row for row in outcomes if row.demand_key not in campaign_owners and demands[row.demand_key].admission_class != "P0"]
	campaign_outcomes = calculate_campaign_member_outcomes(snapshot, allocations, tasks)
	campaign_p0 = [row for row in campaign_outcomes if row["admission_class"] == "P0"]
	campaign_optional = [row for row in campaign_outcomes if row["admission_class"] != "P0"]
	weighted_tardiness = 0
	max_lateness = 0
	horizon_end = max((row.end_minute for row in snapshot.buckets), default=0)
	for outcome in p0_outcomes:
		demand = demands[outcome.demand_key]
		weight = max(min(demand.service_priority, 1000), 0) + 1
		if outcome.recovery_completion_minute is not None:
			delay = max(outcome.recovery_completion_minute - demand.due_minute, 0)
			weighted_tardiness += outcome.late_units * delay * weight
			max_lateness = max(max_lateness, delay)
		weighted_tardiness += outcome.unscheduled_units * max(horizon_end - demand.due_minute + 1, 1) * weight
	for outcome in campaign_p0:
		weight = max(min(outcome["service_priority"], 1000), 0) + 1
		if outcome["recovery_completion_minute"] is not None:
			delay = max(outcome["recovery_completion_minute"] - outcome["due_minute"], 0)
			weighted_tardiness += outcome["late_units"] * delay * weight
			max_lateness = max(max_lateness, delay)
		weighted_tardiness += outcome["unscheduled_units"] * max(horizon_end - outcome["due_minute"] + 1, 1) * weight
	loads = defaultdict(int)
	continuity = 0
	tonnage = 0
	for allocation in allocations:
		alternative = alternatives[(allocation.demand_key, allocation.alternative_key)]
		loads[allocation.machine] += allocation.production_minutes + allocation.base_setup_minutes
		continuity += allocation.quantity_units * max(100 - alternative.continuity_rank, 0)
		tonnage += allocation.quantity_units * alternative.tonnage_gap
	return SolverMetrics(
		p0_on_time_units=sum(row.on_time_units for row in p0_outcomes) + sum(row["on_time_units"] for row in campaign_p0),
		p0_weighted_tardiness=weighted_tardiness,
		p0_critical_unplanned_units=sum(row.unscheduled_units for row in p0_outcomes) + sum(row["unscheduled_units"] for row in campaign_p0),
		change_count=int(sequence_metrics.get("change_count") or 0),
		setup_minutes=sum(row.base_setup_minutes for row in tasks) + int(sequence_metrics.get("changeover_minutes") or 0),
		continuity_units=continuity,
		tonnage_gap_units=tonnage,
		utilization_spread_minutes=(max(loads.values()) - min(loads.values())) if loads else 0,
		p1_p2_completed_units=sum(row.on_time_units + row.late_units for row in optional) + sum(row["on_time_units"] + row["late_units"] for row in campaign_optional),
		total_scheduled_units=(
			sum(row.quantity_units for row in allocations if row.demand_key not in campaign_owners)
			+ sum(row["on_time_units"] + row["late_units"] - row["fixed_on_time_units"] - row["fixed_late_units"] for row in campaign_outcomes)
		),
		total_late_units=sum(row.late_units for row in outcomes if row.demand_key not in campaign_owners) + sum(row["late_units"] for row in campaign_outcomes),
		total_unscheduled_units=sum(row.unscheduled_units for row in outcomes if row.demand_key not in campaign_owners) + sum(row["unscheduled_units"] for row in campaign_outcomes),
		max_lateness_minutes=max_lateness,
	)


def calculate_campaign_member_outcomes(snapshot, allocations, tasks=None):
	"""Project shared owner cycles to demanded outputs without adding capacity."""
	buckets = {row.key: row for row in snapshot.buckets}
	by_owner = defaultdict(list)
	for row in tasks or allocations:
		by_owner[row.demand_key].append(row)

	def end_minute(row):
		return row.end_minute if hasattr(row, "end_minute") else buckets[row.bucket_key].end_minute

	rows = []
	for group in snapshot.multi_output_groups:
		owner_allocations = sorted(
			by_owner.get(group.capacity_owner_demand) or (),
			key=lambda row: (end_minute(row), row.bucket_key, row.alternative_key),
		)
		for member in group.members:
			remaining = member.required_units
			on_time = member.fixed_on_time_units
			late = member.fixed_late_units
			completion = None
			recovery = None
			for allocation in owner_allocations:
				covered = min(remaining, allocation.cycles * member.output_units_per_cycle)
				if covered <= 0:
					continue
				allocation_end = end_minute(allocation)
				if allocation_end <= member.due_minute:
					on_time += covered
				else:
					late += covered
					recovery = max(recovery or allocation_end, allocation_end)
				completion = max(completion or allocation_end, allocation_end)
				remaining -= covered
			rows.append({
				"group_key": group.key,
				"demand_key": member.demand_key,
				"result": member.result,
				"commitment": member.commitment,
				"item_code": member.item_code,
				"admission_class": member.admission_class,
				"service_priority": member.service_priority,
				"due_minute": member.due_minute,
				"fixed_on_time_units": member.fixed_on_time_units,
				"fixed_late_units": member.fixed_late_units,
				"on_time_units": on_time,
				"late_units": late,
				"unscheduled_units": remaining,
				"completion_minute": completion,
				"recovery_completion_minute": recovery,
			})
	return rows


def _greedy_fallback(snapshot: SolverInput, scenario_key: str, input_hash: str, warnings: tuple[str, ...]) -> SolverSolution:
	started = time.monotonic()
	machine_cursor = defaultdict(int)
	mold_cursor = defaultdict(int)
	allocations = []
	tasks = []
	remaining_by_bucket = {row.key: row.available_minutes for row in snapshot.buckets}
	buckets = {row.key: row for row in snapshot.buckets}
	remaining_by_demand = {row.key: row.quantity_units for row in snapshot.demands}
	predecessors = defaultdict(list)
	for edge in snapshot.precedences:
		if edge.predecessor_demand and edge.successor_demand:
			predecessors[edge.successor_demand].append((edge.predecessor_demand, edge.lag_minutes))
	for demand in _precedence_ordered_demands(snapshot):
		remaining = demand.quantity_units
		allocation_start = len(allocations)
		task_start = len(tasks)
		machine_cursor_before = dict(machine_cursor)
		mold_cursor_before = dict(mold_cursor)
		remaining_by_bucket_before = dict(remaining_by_bucket)
		dependency_rows = predecessors.get(demand.key) or ()
		if any(remaining_by_demand.get(key, 0) > 0 for key, _lag in dependency_rows):
			continue
		precedence_ready = max(
			(
				max((row.end_minute for row in tasks if row.demand_key == key), default=demand.earliest_minute) + lag
				for key, lag in dependency_rows
			),
			default=demand.earliest_minute,
		)
		for alternative in sorted(demand.alternatives, key=lambda row: (row.preference_rank, row.tonnage_gap, row.key)):
			setup_paid = False
			cycle_minutes_ppm = max(int(round(float(alternative.cycle_minutes) * 1_000_000)), 1)
			for bucket in sorted((row for row in snapshot.buckets if row.machine == alternative.machine and row.end_minute > max(demand.earliest_minute, precedence_ready)), key=lambda row: (row.start_minute, row.key)):
				if remaining <= 0:
					break
				start = max(bucket.start_minute, demand.earliest_minute, precedence_ready, machine_cursor[alternative.machine], mold_cursor[alternative.mold] if alternative.mold else bucket.start_minute)
				available_wall = min(bucket.end_minute - start, remaining_by_bucket[bucket.key])
				base_setup = 0 if setup_paid else alternative.base_setup_minutes
				production_wall = max(available_wall - base_setup, 0)
				cycles = max((production_wall * bucket.capacity_factor_ppm) // cycle_minutes_ppm, 0)
				if cycles <= 0:
					continue
				cycles = min(cycles, int(math.ceil(remaining / alternative.output_units_per_cycle)))
				qty = min(remaining, cycles * alternative.output_units_per_cycle)
				cycles = int(math.ceil(qty / alternative.output_units_per_cycle))
				production = int(math.ceil(cycles * cycle_minutes_ppm / bucket.capacity_factor_ppm))
				end = start + base_setup + production
				allocation = CapacityAllocation(demand.key, demand.result, demand.commitment, alternative.key, bucket.key, alternative.machine, alternative.mold, qty, cycles, production, base_setup, bucket.horizon_zone)
				allocations.append(allocation)
				tasks.append(ScheduledTask(f"{demand.key}|{alternative.key}|{bucket.key}", demand.key, demand.result, demand.commitment, alternative.key, bucket.key, alternative.machine, alternative.mold, qty, cycles, start, start + base_setup, end, base_setup, 0, bucket.horizon_zone, len([row for row in tasks if row.machine == alternative.machine]) + 1))
				setup_paid = True
				remaining -= qty
				remaining_by_bucket[bucket.key] -= end - start
				machine_cursor[alternative.machine] = end
				if alternative.mold:
					mold_cursor[alternative.mold] = end
		if 0 < demand.quantity_units - remaining < demand.minimum_batch_units:
			del allocations[allocation_start:]
			del tasks[task_start:]
			machine_cursor.clear()
			machine_cursor.update(machine_cursor_before)
			mold_cursor.clear()
			mold_cursor.update(mold_cursor_before)
			remaining_by_bucket.clear()
			remaining_by_bucket.update(remaining_by_bucket_before)
			remaining = demand.quantity_units
		remaining_by_demand[demand.key] = remaining
	outcomes = _outcomes_from_tasks(snapshot, tasks)
	metrics = calculate_metrics(snapshot, outcomes, allocations, tasks, {"change_count": 0, "changeover_minutes": 0})
	solution = SolverSolution(
		scenario_key=scenario_key,
		scenario_label=SCENARIO_LABELS[scenario_key],
		status="Fallback",
		engine="Legacy Fallback",
		input_fingerprint=input_hash,
		solution_fingerprint="",
		runtime_seconds=time.monotonic() - started,
		objective_values=(("p0_on_time", metrics.p0_on_time_units), ("p0_unplanned", metrics.p0_critical_unplanned_units)),
		allocations=tuple(allocations),
		tasks=tuple(tasks),
		outcomes=tuple(outcomes),
		metrics=metrics,
		frozen_intervals=snapshot.frozen_intervals,
		warnings=tuple(warnings) + ("Legacy heuristic fallback is clearly marked and requires acknowledgment before apply.",),
		explanation=_explanation(scenario_key, metrics, "Fallback"),
	)
	return replace(solution, solution_fingerprint=solution_fingerprint(solution))


def _precedence_ordered_demands(snapshot: SolverInput):
	"""Stable topological order used only by the explicitly marked fallback."""
	demands = {row.key: row for row in snapshot.demands}
	children = defaultdict(set)
	indegree = defaultdict(int)
	for key in demands:
		indegree[key] = 0
	for edge in snapshot.precedences:
		if not edge.predecessor_demand or edge.predecessor_demand not in demands or edge.successor_demand not in demands:
			continue
		if edge.successor_demand not in children[edge.predecessor_demand]:
			children[edge.predecessor_demand].add(edge.successor_demand)
			indegree[edge.successor_demand] += 1

	def priority(key):
		row = demands[key]
		return (row.admission_class != "P0", row.due_minute, -row.service_priority, row.key)

	ready = sorted((key for key in demands if indegree[key] == 0), key=priority)
	ordered = []
	while ready:
		key = ready.pop(0)
		ordered.append(demands[key])
		for child in sorted(children.get(key) or (), key=priority):
			indegree[child] -= 1
			if indegree[child] == 0:
				ready.append(child)
		ready.sort(key=priority)
	if len(ordered) != len(demands):
		# The BOM expander rejects cycles; retaining all rows here lets the
		# independent validator fail closed if a malformed external snapshot arrives.
		ordered.extend(demands[key] for key in sorted(set(demands) - {row.key for row in ordered}, key=priority))
	return ordered


def _outcomes_from_tasks(snapshot, tasks):
	by_demand = defaultdict(list)
	for row in tasks:
		by_demand[row.demand_key].append(row)
	result = []
	for demand in snapshot.demands:
		rows = by_demand[demand.key]
		on_time = demand.fixed_on_time_units + sum(row.quantity_units for row in rows if row.end_minute <= demand.due_minute)
		late = demand.fixed_late_units + sum(row.quantity_units for row in rows if row.end_minute > demand.due_minute)
		allocated = sum(row.quantity_units for row in rows)
		completion = max((row.end_minute for row in rows), default=None)
		recovery = max((row.end_minute for row in rows if row.end_minute > demand.due_minute), default=None)
		result.append(DemandOutcome(demand.key, demand.result, demand.commitment, on_time, late, demand.quantity_units - allocated, completion, recovery))
	return result


def _p0_floor_from_metrics(metrics):
	return {
		"p0_on_time": int(metrics.p0_on_time_units),
		"p0_weighted_tardiness": int(metrics.p0_weighted_tardiness),
		"p0_unplanned": int(metrics.p0_critical_unplanned_units),
	}


def _gap(value, bound, status):
	if status == "Optimal":
		return 0.0
	if value is None or bound is None:
		return None
	return abs(value - bound) / max(abs(value), 1) * 100


def _explanation(key, metrics, status):
	return (
		f"{SCENARIO_LABELS[key]} finished with {status} status.",
		f"P0 on-time {metrics.p0_on_time_units}; critical unplanned {metrics.p0_critical_unplanned_units}; late {metrics.total_late_units}.",
		f"Changeovers {metrics.change_count}; setup/changeover minutes {metrics.setup_minutes}; optional completion {metrics.p1_p2_completed_units}.",
	)
