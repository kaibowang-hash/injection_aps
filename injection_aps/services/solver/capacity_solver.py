from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

from .models import CapacityAllocation, CapacityResult, DemandOutcome, SolverInput
from .objectives import objective_direction


class SolverRuntimeUnavailable(RuntimeError):
	pass


def solve_capacity(
	snapshot: SolverInput,
	objective_names: tuple[str, ...],
	*,
	required_objectives: dict[str, int] | None = None,
	time_limit_seconds: float | None = None,
) -> CapacityResult:
	try:
		from ortools.sat.python import cp_model
	except ImportError as exc:  # pragma: no cover - exercised by orchestration fallback
		raise SolverRuntimeUnavailable(str(exc)) from exc

	started = time.monotonic()
	model = cp_model.CpModel()
	bucket_by_key = {row.key: row for row in snapshot.buckets}
	demand_by_key = {row.key: row for row in snapshot.demands}
	variables: dict[tuple[str, str, str], dict[str, Any]] = {}
	unscheduled: dict[str, Any] = {}
	machine_bucket_durations: dict[str, list[Any]] = defaultdict(list)
	mold_time_durations: dict[tuple[str, int, int], list[Any]] = defaultdict(list)
	machine_load_terms: dict[str, list[Any]] = defaultdict(list)

	for demand in snapshot.demands:
		unscheduled[demand.key] = model.NewIntVar(0, demand.quantity_units, f"unscheduled|{demand.key}")
		quantity_terms = []
		for alternative in demand.alternatives:
			alternative_variables = []
			for bucket in snapshot.buckets:
				if bucket.machine != alternative.machine or bucket.start_minute < demand.earliest_minute:
					continue
				if bucket.available_minutes <= 0 or bucket.end_minute <= bucket.start_minute:
					continue
				key = (demand.key, alternative.key, bucket.key)
				used = model.NewBoolVar(f"used|{'|'.join(key)}")
				qty = model.NewIntVar(0, demand.quantity_units, f"qty|{'|'.join(key)}")
				max_cycles = int(math.ceil(demand.quantity_units / alternative.output_units_per_cycle)) if demand.quantity_units else 0
				cycles = model.NewIntVar(0, max_cycles, f"cycles|{'|'.join(key)}")
				model.Add(qty <= demand.quantity_units * used)
				model.Add(qty >= used)
				model.Add(cycles * alternative.output_units_per_cycle >= qty)
				model.Add(cycles * alternative.output_units_per_cycle <= qty + alternative.output_units_per_cycle - 1)
				cycle_minutes_ppm = _cycle_minutes_ppm(alternative.cycle_minutes)
				max_production = max_cycles * cycle_minutes_ppm
				max_wall_minutes = int(math.ceil(max_production / bucket.capacity_factor_ppm)) if max_production else 0
				numerator = model.NewIntVar(0, max_production + bucket.capacity_factor_ppm, f"duration_numerator|{'|'.join(key)}")
				model.Add(numerator == cycles * cycle_minutes_ppm + bucket.capacity_factor_ppm - 1)
				production_minutes = model.NewIntVar(0, max(bucket.end_minute - bucket.start_minute, max_wall_minutes), f"production_minutes|{'|'.join(key)}")
				model.AddDivisionEquality(production_minutes, numerator, bucket.capacity_factor_ppm)
				values = {
					"used": used,
					"qty": qty,
					"cycles": cycles,
					"production_minutes": production_minutes,
					"max_wall_minutes": max_wall_minutes,
					"alternative": alternative,
					"bucket": bucket,
				}
				variables[key] = values
				alternative_variables.append(values)
				quantity_terms.append(qty)
			if alternative_variables:
				# One demand/alternative is one continuous campaign.  If it spans
				# adjacent capacity buckets, base setup belongs only to the first
				# used bucket rather than being charged once per bucket.
				alternative_variables.sort(key=lambda values: (values["bucket"].start_minute, values["bucket"].key))
				campaign_used = model.NewBoolVar(f"campaign_used|{demand.key}|{alternative.key}")
				model.AddMaxEquality(campaign_used, [values["used"] for values in alternative_variables])
				setup_here = []
				for index, values in enumerate(alternative_variables):
					setup = model.NewBoolVar(f"setup|{demand.key}|{alternative.key}|{values['bucket'].key}")
					model.Add(setup <= values["used"])
					for prior in alternative_variables[:index]:
						model.Add(setup + prior["used"] <= 1)
					setup_here.append(setup)
					values["setup_here"] = setup
				model.Add(sum(setup_here) == campaign_used)
				for values in alternative_variables:
					bucket = values["bucket"]
					duration = model.NewIntVar(
						0,
						max(bucket.end_minute - bucket.start_minute, values["max_wall_minutes"]) + alternative.base_setup_minutes,
						f"duration|{demand.key}|{alternative.key}|{bucket.key}",
					)
					model.Add(duration == values["production_minutes"] + alternative.base_setup_minutes * values["setup_here"])
					values["duration"] = duration
					machine_bucket_durations[bucket.key].append(duration)
					machine_load_terms[alternative.machine].append(duration)
					if alternative.mold:
						mold_time_durations[(alternative.mold, bucket.start_minute, bucket.end_minute)].append(duration)
		scheduled_qty = sum(quantity_terms)
		model.Add(scheduled_qty + unscheduled[demand.key] == demand.quantity_units)
		if demand.minimum_batch_units and quantity_terms:
			has_scheduled_qty = model.NewBoolVar(f"has_scheduled_qty|{demand.key}")
			model.Add(scheduled_qty <= demand.quantity_units * has_scheduled_qty)
			model.Add(scheduled_qty >= demand.minimum_batch_units * has_scheduled_qty)

	for bucket in snapshot.buckets:
		model.Add(sum(machine_bucket_durations.get(bucket.key) or ()) <= bucket.available_minutes)
	for (_mold, start, end), terms in mold_time_durations.items():
		model.Add(sum(terms) <= max(end - start, 0))
	_apply_precedence_constraints(model, snapshot, variables, unscheduled)

	objective_expressions = _objective_expressions(
		model,
		snapshot,
		variables,
		unscheduled,
		machine_load_terms,
	)
	for name, value in sorted((required_objectives or {}).items()):
		if name in objective_expressions:
			model.Add(objective_expressions[name] == int(value))

	limit = max(float(time_limit_seconds or snapshot.time_limit_seconds), 0.01)
	objective_values: list[tuple[str, int]] = []
	last_solver = None
	last_status = None
	for index, name in enumerate(objective_names):
		remaining = limit - (time.monotonic() - started)
		if remaining <= 0.01:
			break
		expression = objective_expressions[name]
		model.ClearObjective()
		if objective_direction(name) == "max":
			model.Maximize(expression)
		else:
			model.Minimize(expression)
		solver = cp_model.CpSolver()
		solver.parameters.max_time_in_seconds = max(remaining, 0.01)
		solver.parameters.num_search_workers = 1
		solver.parameters.random_seed = snapshot.random_seed
		solver.parameters.log_search_progress = False
		status = solver.Solve(model)
		if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
			if last_solver is None:
				return CapacityResult(status="No Feasible", allocations=(), outcomes=(), objective_values=tuple(objective_values), runtime_seconds=time.monotonic() - started, warnings=(f"No feasible capacity solution at objective {name}.",))
			break
		value = int(round(solver.Value(expression)))
		objective_values.append((name, value))
		model.Add(expression == value)
		last_solver = solver
		last_status = status

	if last_solver is None:
		# A profile with no objectives still needs one feasibility solve.
		remaining = limit - (time.monotonic() - started)
		if remaining <= 0.01:
			return CapacityResult(
				status="No Feasible",
				allocations=(),
				outcomes=(),
				objective_values=tuple(objective_values),
				runtime_seconds=time.monotonic() - started,
				warnings=("Time limit reached during capacity model construction.",),
			)
		last_solver = cp_model.CpSolver()
		last_solver.parameters.max_time_in_seconds = max(remaining, 0.01)
		last_solver.parameters.num_search_workers = 1
		last_solver.parameters.random_seed = snapshot.random_seed
		last_status = last_solver.Solve(model)
		if last_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
			return CapacityResult(status="No Feasible", allocations=(), outcomes=(), objective_values=(), runtime_seconds=time.monotonic() - started)

	allocations = []
	for key in sorted(variables):
		values = variables[key]
		qty = int(last_solver.Value(values["qty"]))
		if qty <= 0:
			continue
		alternative = values["alternative"]
		bucket = values["bucket"]
		allocations.append(
			CapacityAllocation(
				demand_key=key[0],
				result=demand_by_key[key[0]].result,
				commitment=demand_by_key[key[0]].commitment,
				alternative_key=key[1],
				bucket_key=key[2],
				machine=alternative.machine,
				mold=alternative.mold,
				quantity_units=qty,
				cycles=int(last_solver.Value(values["cycles"])),
				production_minutes=int(last_solver.Value(values["production_minutes"])),
				base_setup_minutes=alternative.base_setup_minutes if last_solver.Value(values["setup_here"]) else 0,
				horizon_zone=bucket.horizon_zone,
			)
		)
	outcomes = _build_outcomes(snapshot, allocations, {key: int(last_solver.Value(value)) for key, value in unscheduled.items()})
	status_name = "Optimal" if last_status == cp_model.OPTIMAL and len(objective_values) == len(objective_names) else "Feasible"
	return CapacityResult(
		status=status_name,
		allocations=tuple(allocations),
		outcomes=tuple(outcomes),
		objective_values=tuple(objective_values),
		runtime_seconds=time.monotonic() - started,
		best_bound=float(last_solver.BestObjectiveBound()) if objective_values else None,
		objective_value=float(last_solver.ObjectiveValue()) if objective_values else None,
		warnings=(() if len(objective_values) == len(objective_names) else ("Time limit reached after a feasible lexicographic layer.",)),
	)


def _objective_expressions(model, snapshot, variables, unscheduled, machine_load_terms):
	p0_on_time = []
	p0_tardiness = []
	p0_unscheduled = []
	assignment_changes = []
	setup_minutes = []
	continuity = []
	tonnage_gap = []
	optional_completion = []
	demand_by_key = {row.key: row for row in snapshot.demands}
	campaign_groups = {row.capacity_owner_demand: row for row in snapshot.multi_output_groups}
	horizon_end = max((row.end_minute for row in snapshot.buckets), default=0)
	for (demand_key, _alternative_key, _bucket_key), values in variables.items():
		demand = demand_by_key[demand_key]
		alternative = values["alternative"]
		bucket = values["bucket"]
		qty = values["qty"]
		# A collapsed family demand is only the physical cycle owner. Delivery
		# objectives are calculated from every demanded output below; counting the
		# artificial owner quantity here would over-prioritize the owner's item and
		# hide co-product due dates.
		if demand_key in campaign_groups:
			pass
		elif demand.admission_class == "P0":
			if bucket.end_minute <= demand.due_minute:
				p0_on_time.append(qty)
			else:
				delay = max(bucket.end_minute - demand.due_minute, 1)
				weight = max(min(demand.service_priority, 1000), 0) + 1
				p0_tardiness.append(qty * delay * weight)
		else:
			optional_completion.append(qty)
		assignment_changes.append(values["used"] * (1 + alternative.preference_rank))
		setup_minutes.append(values["setup_here"] * alternative.base_setup_minutes)
		continuity.append(qty * max(100 - alternative.continuity_rank, 0))
		tonnage_gap.append(qty * alternative.tonnage_gap)
	for demand in snapshot.demands:
		if demand.admission_class != "P0" or demand.key in campaign_groups:
			continue
		weight = max(min(demand.service_priority, 1000), 0) + 1
		p0_tardiness.append(unscheduled[demand.key] * max(horizon_end - demand.due_minute + 1, 1) * weight)
		p0_unscheduled.append(unscheduled[demand.key])

	# One owner cycle simultaneously produces every member output. Each member
	# therefore has an independent coverage variable bounded by the same selected
	# owner cycles. This preserves real output due dates and service priorities
	# without creating another machine or mold interval.
	for group in snapshot.multi_output_groups:
		owner_variables = [
			values
			for (demand_key, _alternative_key, _bucket_key), values in variables.items()
			if demand_key == group.capacity_owner_demand
		]
		for member_index, member in enumerate(group.members):
			if member.required_units <= 0:
				continue
			coverage_terms = []
			for variable_index, values in enumerate(owner_variables):
				cover = model.NewIntVar(
					0,
					member.required_units,
					f"campaign_cover|{group.key}|{member_index}|{variable_index}",
				)
				model.Add(cover <= values["cycles"] * member.output_units_per_cycle)
				coverage_terms.append(cover)
				bucket = values["bucket"]
				if member.admission_class == "P0":
					if bucket.end_minute <= member.due_minute:
						p0_on_time.append(cover)
					else:
						delay = max(bucket.end_minute - member.due_minute, 1)
						weight = max(min(member.service_priority, 1000), 0) + 1
						p0_tardiness.append(cover * delay * weight)
				else:
					optional_completion.append(cover)
			member_unscheduled = model.NewIntVar(
				0,
				member.required_units,
				f"campaign_unscheduled|{group.key}|{member_index}",
			)
			model.Add(sum(coverage_terms) + member_unscheduled == member.required_units)
			if member.admission_class == "P0":
				weight = max(min(member.service_priority, 1000), 0) + 1
				p0_tardiness.append(
					member_unscheduled * max(horizon_end - member.due_minute + 1, 1) * weight
				)
				p0_unscheduled.append(member_unscheduled)

	machines = sorted(machine_load_terms)
	utilization_spread = 0
	if machines:
		upper = sum(max(bucket.available_minutes, 0) for bucket in snapshot.buckets)
		loads = []
		for machine in machines:
			load = model.NewIntVar(0, upper, f"machine_load|{machine}")
			model.Add(load == sum(machine_load_terms[machine]))
			loads.append(load)
		maximum = model.NewIntVar(0, upper, "max_machine_load")
		minimum = model.NewIntVar(0, upper, "min_machine_load")
		model.AddMaxEquality(maximum, loads)
		model.AddMinEquality(minimum, loads)
		utilization_spread = maximum - minimum
	return {
		"p0_on_time": sum(p0_on_time),
		"p0_weighted_tardiness": sum(p0_tardiness),
		"p0_unplanned": sum(p0_unscheduled),
		"assignment_changes": sum(assignment_changes),
		"setup_minutes": sum(setup_minutes),
		"continuity": sum(continuity),
		"tonnage_gap": sum(tonnage_gap),
		"utilization_spread": utilization_spread,
		"optional_completion": sum(optional_completion),
	}


def _apply_precedence_constraints(model, snapshot, variables, unscheduled):
	by_demand = defaultdict(list)
	for key, values in variables.items():
		by_demand[key[0]].append(values)
	for index, edge in enumerate(snapshot.precedences):
		if edge.predecessor_demand not in unscheduled or edge.successor_demand not in unscheduled:
			continue
		complete = model.NewBoolVar(f"precedence_complete|{index}|{edge.predecessor_demand}")
		model.Add(unscheduled[edge.predecessor_demand] == 0).OnlyEnforceIf(complete)
		model.Add(unscheduled[edge.predecessor_demand] >= 1).OnlyEnforceIf(complete.Not())
		for successor in by_demand.get(edge.successor_demand) or ():
			model.Add(successor["used"] <= complete)
			for predecessor in by_demand.get(edge.predecessor_demand) or ():
				if predecessor["bucket"].key == successor["bucket"].key:
					continue
				if predecessor["bucket"].start_minute + edge.lag_minutes >= successor["bucket"].end_minute:
					model.Add(predecessor["used"] + successor["used"] <= 1)


def _build_outcomes(snapshot, allocations, unscheduled):
	bucket_by_key = {row.key: row for row in snapshot.buckets}
	by_demand = defaultdict(list)
	for row in allocations:
		by_demand[row.demand_key].append(row)
	result = []
	for demand in snapshot.demands:
		rows = by_demand.get(demand.key) or []
		on_time = demand.fixed_on_time_units + sum(row.quantity_units for row in rows if bucket_by_key[row.bucket_key].end_minute <= demand.due_minute)
		late = demand.fixed_late_units + sum(row.quantity_units for row in rows if bucket_by_key[row.bucket_key].end_minute > demand.due_minute)
		completion = max((bucket_by_key[row.bucket_key].end_minute for row in rows), default=None)
		recovery = max((bucket_by_key[row.bucket_key].end_minute for row in rows if bucket_by_key[row.bucket_key].end_minute > demand.due_minute), default=None)
		result.append(DemandOutcome(demand_key=demand.key, result=demand.result, commitment=demand.commitment, on_time_units=on_time, late_units=late, unscheduled_units=unscheduled[demand.key], completion_minute=completion, recovery_completion_minute=recovery))
	return result


def _cycle_minutes_ppm(value: float) -> int:
	"""Return positive integer micro-minutes for CP-SAT expressions."""
	return max(int(round(float(value) * 1_000_000)), 1)
