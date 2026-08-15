from __future__ import annotations

import time
from collections import defaultdict

from .models import CapacityAllocation, ScheduledTask, SolverInput


class SequenceInfeasible(RuntimeError):
	pass


def solve_sequence(snapshot: SolverInput, allocations: tuple[CapacityAllocation, ...], *, time_limit_seconds: float | None = None) -> tuple[tuple[ScheduledTask, ...], dict[str, int], float, str]:
	from ortools.sat.python import cp_model

	started = time.monotonic()
	model = cp_model.CpModel()
	buckets = {row.key: row for row in snapshot.buckets}
	variables = {}
	machine_intervals = defaultdict(list)
	mold_intervals = defaultdict(list)
	group_tasks = defaultdict(list)
	for index, allocation in enumerate(sorted(allocations, key=lambda row: (row.machine, row.bucket_key, row.demand_key, row.alternative_key))):
		bucket = buckets[allocation.bucket_key]
		duration = allocation.base_setup_minutes + allocation.production_minutes
		if duration > bucket.end_minute - bucket.start_minute:
			raise SequenceInfeasible(f"Task {allocation.demand_key} does not fit bucket {bucket.key}.")
		start = model.NewIntVar(bucket.start_minute, bucket.end_minute - duration, f"start|{index}")
		end = model.NewIntVar(bucket.start_minute + duration, bucket.end_minute, f"end|{index}")
		model.Add(end == start + duration)
		interval = model.NewIntervalVar(start, duration, end, f"interval|{index}")
		variables[index] = {"allocation": allocation, "start": start, "end": end, "duration": duration}
		machine_intervals[allocation.machine].append(interval)
		if allocation.mold:
			mold_intervals[allocation.mold].append(interval)
		group_tasks[(allocation.machine, allocation.bucket_key)].append(index)

	for frozen in snapshot.frozen_intervals:
		duration = max(frozen.end_minute - frozen.start_minute, 0)
		if duration <= 0:
			continue
		interval = model.NewIntervalVar(frozen.start_minute, duration, frozen.end_minute, f"frozen|{frozen.key}")
		if frozen.resource_type == "machine":
			machine_intervals[frozen.resource].append(interval)
		elif frozen.resource_type == "mold":
			mold_intervals[frozen.resource].append(interval)
	for intervals in machine_intervals.values():
		model.AddNoOverlap(intervals)
	for intervals in mold_intervals.values():
		model.AddNoOverlap(intervals)
	by_demand = defaultdict(list)
	for index, values in variables.items():
		by_demand[values["allocation"].demand_key].append(index)
	for edge in snapshot.precedences:
		for predecessor in by_demand.get(edge.predecessor_demand) or ():
			for successor in by_demand.get(edge.successor_demand) or ():
				model.Add(variables[successor]["start"] >= variables[predecessor]["end"] + edge.lag_minutes)

	transition_terms = []
	change_terms = []
	incoming_arcs = {}
	for group, indexes in sorted(group_tasks.items()):
		if len(indexes) <= 1:
			continue
		arcs = []
		for index in indexes:
			start_arc = model.NewBoolVar(f"arc|{group}|0|{index + 1}")
			end_arc = model.NewBoolVar(f"arc|{group}|{index + 1}|0")
			arcs.extend([(0, index + 1, start_arc), (index + 1, 0, end_arc)])
			incoming_arcs[(group, index)] = []
		for left in indexes:
			for right in indexes:
				if left == right:
					continue
				arc = model.NewBoolVar(f"arc|{group}|{left + 1}|{right + 1}")
				arcs.append((left + 1, right + 1, arc))
				incoming_arcs[(group, right)].append((left, arc))
				minutes = _transition_minutes(snapshot, variables[left]["allocation"], variables[right]["allocation"])
				model.Add(variables[right]["start"] >= variables[left]["end"] + minutes).OnlyEnforceIf(arc)
				if minutes:
					transition_terms.append(arc * minutes)
					change_terms.append(arc)
		model.AddCircuit(arcs)

	# Stable tie-breaker follows transition minimization and is deterministic with one worker.
	model.Minimize(sum(transition_terms) * 1_000_000 + sum(change_terms) * 10_000 + sum(row["start"] for row in variables.values()))
	limit = max(float(time_limit_seconds or snapshot.time_limit_seconds), 0.01)
	remaining = limit - (time.monotonic() - started)
	if remaining <= 0.01:
		raise SequenceInfeasible("Time limit reached during sequence model construction.")
	solver = cp_model.CpSolver()
	solver.parameters.max_time_in_seconds = max(remaining, 0.01)
	solver.parameters.num_search_workers = 1
	solver.parameters.random_seed = snapshot.random_seed
	status = solver.Solve(model)
	if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
		raise SequenceInfeasible("Capacity allocation has no feasible exact machine/mold sequence.")

	selected_changeover = {}
	for (group, right), incoming in incoming_arcs.items():
		for left, arc in incoming:
			if solver.Value(arc):
				selected_changeover[right] = _transition_minutes(snapshot, variables[left]["allocation"], variables[right]["allocation"])
	by_machine = defaultdict(list)
	for index, values in variables.items():
		by_machine[values["allocation"].machine].append((solver.Value(values["start"]), index))
	sequence_by_index = {}
	for machine, rows in by_machine.items():
		for sequence_no, (_start, index) in enumerate(sorted(rows), start=1):
			sequence_by_index[index] = sequence_no

	tasks = []
	for index in sorted(variables, key=lambda value: (solver.Value(variables[value]["start"]), variables[value]["allocation"].machine, value)):
		values = variables[index]
		allocation = values["allocation"]
		start = int(solver.Value(values["start"]))
		tasks.append(ScheduledTask(
			key=f"{allocation.demand_key}|{allocation.alternative_key}|{allocation.bucket_key}",
			demand_key=allocation.demand_key,
			result=allocation.result,
			commitment=allocation.commitment,
			alternative_key=allocation.alternative_key,
			bucket_key=allocation.bucket_key,
			machine=allocation.machine,
			mold=allocation.mold,
			quantity_units=allocation.quantity_units,
			cycles=allocation.cycles,
			occupied_start_minute=start,
			production_start_minute=start + allocation.base_setup_minutes,
			end_minute=int(solver.Value(values["end"])),
			base_setup_minutes=allocation.base_setup_minutes,
			changeover_minutes=int(selected_changeover.get(index) or 0),
			horizon_zone=allocation.horizon_zone,
			sequence_no=sequence_by_index[index],
		))
	metrics = {"change_count": sum(1 for value in selected_changeover.values() if value), "changeover_minutes": sum(selected_changeover.values())}
	return tuple(tasks), metrics, time.monotonic() - started, ("Optimal" if status == cp_model.OPTIMAL else "Feasible")


def _transition_minutes(snapshot: SolverInput, left: CapacityAllocation, right: CapacityAllocation) -> int:
	if left.mold == right.mold:
		return 0
	for rule in snapshot.transition_rules:
		if rule.from_family == left.mold and rule.to_family == right.mold:
			if rule.blocked:
				return max(snapshot.buckets[-1].end_minute if snapshot.buckets else 1_000_000, 1_000_000)
			return max(rule.setup_minutes, 0)
	return 30
