from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .models import SolverInput, SolverSolution


class InvalidSolverSolution(ValueError):
	def __init__(self, errors: list[dict[str, Any]]):
		self.errors = errors
		super().__init__("; ".join(row["message"] for row in errors))


def validate_solution(snapshot: SolverInput, solution: SolverSolution, *, required_p0_floor: dict[str, int] | None = None, raise_on_error: bool = True) -> dict[str, Any]:
	errors: list[dict[str, Any]] = []
	warnings: list[dict[str, Any]] = []
	demands = {row.key: row for row in snapshot.demands}
	buckets = {row.key: row for row in snapshot.buckets}
	alternatives = {(demand.key, alternative.key): alternative for demand in snapshot.demands for alternative in demand.alternatives}
	allocations_by_demand = defaultdict(list)
	tasks_by_demand = defaultdict(list)
	for task in solution.tasks:
		tasks_by_demand[task.demand_key].append(task)
	for row in solution.allocations:
		allocations_by_demand[row.demand_key].append(row)
		if row.demand_key not in demands:
			_error(errors, "unknown_demand", f"Allocation references unknown demand {row.demand_key}.")
		if row.bucket_key not in buckets:
			_error(errors, "unknown_bucket", f"Allocation references unknown bucket {row.bucket_key}.")
		alternative = alternatives.get((row.demand_key, row.alternative_key))
		if alternative is None:
			_error(errors, "illegal_alternative", f"Allocation uses illegal alternative {row.alternative_key} for {row.demand_key}.")
		elif alternative.machine != row.machine or alternative.mold != row.mold:
			_error(errors, "alternative_resource_mismatch", f"Allocation resource does not match alternative {row.alternative_key}.")
		bucket = buckets.get(row.bucket_key)
		if bucket and bucket.machine != row.machine:
			_error(errors, "bucket_machine_mismatch", f"Bucket {row.bucket_key} does not belong to {row.machine}.")

	outcomes = {row.demand_key: row for row in solution.outcomes}
	for demand in snapshot.demands:
		outcome = outcomes.get(demand.key)
		if outcome is None:
			_error(errors, "missing_outcome", f"Demand {demand.key} has no outcome.")
			continue
		allocated = sum(row.quantity_units for row in allocations_by_demand[demand.key])
		if allocated + outcome.unscheduled_units != demand.quantity_units:
			_error(errors, "demand_conservation", f"Demand {demand.key} does not conserve controllable quantity.")
		if 0 < allocated < demand.minimum_batch_units:
			_error(
				errors,
				"minimum_batch",
				f"Demand {demand.key} schedules {allocated} below minimum batch {demand.minimum_batch_units}.",
			)
		on_time = demand.fixed_on_time_units + sum(row.quantity_units for row in tasks_by_demand[demand.key] if row.end_minute <= demand.due_minute)
		late = demand.fixed_late_units + sum(row.quantity_units for row in tasks_by_demand[demand.key] if row.end_minute > demand.due_minute)
		if outcome.on_time_units != on_time or outcome.late_units != late:
			_error(errors, "delivery_classification", f"Demand {demand.key} on-time/late classification is inconsistent.")

	allocation_keys = {(row.demand_key, row.alternative_key, row.bucket_key, row.quantity_units, row.cycles) for row in solution.allocations}
	task_keys = {(row.demand_key, row.alternative_key, row.bucket_key, row.quantity_units, row.cycles) for row in solution.tasks}
	if allocation_keys != task_keys:
		_error(errors, "task_allocation_mismatch", "Sequenced tasks do not match capacity allocations.")
	for task in solution.tasks:
		bucket = buckets.get(task.bucket_key)
		alternative = alternatives.get((task.demand_key, task.alternative_key))
		if not bucket or not alternative:
			continue
		if task.occupied_start_minute < bucket.start_minute or task.end_minute > bucket.end_minute:
			_error(errors, "task_outside_bucket", f"Task {task.key} is outside bucket {bucket.key}.")
		expected_cycles = int(math.ceil(task.quantity_units / alternative.output_units_per_cycle)) if task.quantity_units else 0
		if task.cycles != expected_cycles:
			_error(errors, "cycle_quantity", f"Task {task.key} has inconsistent cycles and output quantity.")
		cycle_minutes_ppm = max(int(round(float(alternative.cycle_minutes) * 1_000_000)), 1)
		minimum_production = int(math.ceil(task.cycles * cycle_minutes_ppm / bucket.capacity_factor_ppm))
		if task.end_minute - task.production_start_minute < minimum_production:
			_error(errors, "task_duration", f"Task {task.key} is shorter than its cycle duration.")
		if task.production_start_minute - task.occupied_start_minute != task.base_setup_minutes:
			_error(errors, "setup_duration", f"Task {task.key} has inconsistent base setup time.")
	_validate_campaign_setup(errors, solution, alternatives)

	_validate_no_overlap(errors, solution, "machine")
	_validate_no_overlap(errors, solution, "mold")
	_validate_multi_output_capacity_owners(errors, snapshot, solution)
	_validate_precedences(errors, snapshot, solution)
	if tuple(asdict(row) for row in solution.frozen_intervals) != tuple(asdict(row) for row in snapshot.frozen_intervals):
		_error(errors, "frozen_changed", "The solution did not preserve the exact frozen interval snapshot.")

	campaign_owners = {row.capacity_owner_demand for row in snapshot.multi_output_groups}
	ordinary_p0 = [
		row
		for row in solution.outcomes
		if row.demand_key not in campaign_owners
		and demands.get(row.demand_key)
		and demands[row.demand_key].admission_class == "P0"
	]
	campaign_p0 = [row for row in _calculate_campaign_member_outcomes(snapshot, solution) if row["admission_class"] == "P0"]
	p0_on_time = sum(row.on_time_units for row in ordinary_p0) + sum(row["on_time_units"] for row in campaign_p0)
	p0_unscheduled = sum(row.unscheduled_units for row in ordinary_p0) + sum(row["unscheduled_units"] for row in campaign_p0)
	p0_weighted_tardiness = _calculate_p0_weighted_tardiness(snapshot, solution, demands, campaign_p0=campaign_p0)
	if solution.metrics.p0_on_time_units != p0_on_time:
		_error(errors, "p0_metric_mismatch", "Solution metrics do not match independently calculated P0 on-time quantity.")
	if solution.metrics.p0_critical_unplanned_units != p0_unscheduled:
		_error(errors, "p0_metric_mismatch", "Solution metrics do not match independently calculated P0 unplanned quantity.")
	if solution.metrics.p0_weighted_tardiness != p0_weighted_tardiness:
		_error(errors, "p0_metric_mismatch", "Solution metrics do not match independently calculated P0 weighted tardiness.")
	if required_p0_floor:
		if p0_on_time < int(required_p0_floor.get("p0_on_time", 0)):
			_error(errors, "p0_on_time_regression", "Optional demand or a lower objective reduced P0 on-time quantity.")
		if p0_unscheduled > int(required_p0_floor.get("p0_unplanned", p0_unscheduled)):
			_error(errors, "p0_unplanned_regression", "Optional demand increased P0 critical unplanned quantity.")
		if p0_weighted_tardiness > int(required_p0_floor.get("p0_weighted_tardiness", p0_weighted_tardiness)):
			_error(errors, "p0_tardiness_regression", "A lower objective increased P0 weighted tardiness.")

	result = {"valid": not errors, "errors": errors, "warnings": warnings, "checked": ["demand_conservation", "minimum_batch", "machine_no_overlap", "mold_no_overlap", "frozen_unchanged", "alternative_legality", "horizon", "p0_guard", "campaign_capacity_owner", "bom_precedence"]}
	if errors and raise_on_error:
		raise InvalidSolverSolution(errors)
	return result


def _calculate_p0_weighted_tardiness(snapshot, solution, demands, *, campaign_p0=()) -> int:
	horizon_end = max((row.end_minute for row in snapshot.buckets), default=0)
	campaign_owners = {row.capacity_owner_demand for row in snapshot.multi_output_groups}
	value = 0
	for outcome in solution.outcomes:
		demand = demands.get(outcome.demand_key)
		if not demand or demand.admission_class != "P0" or outcome.demand_key in campaign_owners:
			continue
		weight = max(min(demand.service_priority, 1000), 0) + 1
		if outcome.recovery_completion_minute is not None:
			value += outcome.late_units * max(outcome.recovery_completion_minute - demand.due_minute, 0) * weight
		value += outcome.unscheduled_units * max(horizon_end - demand.due_minute + 1, 1) * weight
	for outcome in campaign_p0:
		weight = max(min(outcome["service_priority"], 1000), 0) + 1
		if outcome["recovery_completion_minute"] is not None:
			value += outcome["late_units"] * max(outcome["recovery_completion_minute"] - outcome["due_minute"], 0) * weight
		value += outcome["unscheduled_units"] * max(horizon_end - outcome["due_minute"] + 1, 1) * weight
	return value


def _calculate_campaign_member_outcomes(snapshot, solution):
	"""Independently reconstruct demanded output coverage from shared cycles."""
	by_owner = defaultdict(list)
	for task in solution.tasks:
		by_owner[task.demand_key].append(task)
	rows = []
	for group in snapshot.multi_output_groups:
		owner_rows = sorted(
			by_owner.get(group.capacity_owner_demand) or (),
			key=lambda row: (row.end_minute, row.bucket_key, row.alternative_key),
		)
		for member in group.members:
			remaining = member.required_units
			on_time = member.fixed_on_time_units
			late = member.fixed_late_units
			recovery = None
			for task in owner_rows:
				covered = min(remaining, task.cycles * member.output_units_per_cycle)
				if covered <= 0:
					continue
				end_minute = task.end_minute
				if end_minute <= member.due_minute:
					on_time += covered
				else:
					late += covered
					recovery = max(recovery or end_minute, end_minute)
				remaining -= covered
			rows.append({
				"admission_class": member.admission_class,
				"service_priority": member.service_priority,
				"due_minute": member.due_minute,
				"on_time_units": on_time,
				"late_units": late,
				"unscheduled_units": remaining,
				"recovery_completion_minute": recovery,
			})
	return rows


def _validate_campaign_setup(errors, solution, alternatives):
	"""A continuous demand/alternative campaign pays base setup once."""
	by_campaign = defaultdict(list)
	for task in solution.tasks:
		by_campaign[(task.demand_key, task.alternative_key)].append(task)
	for key, tasks in by_campaign.items():
		alternative = alternatives.get(key)
		if not alternative:
			continue
		ordered = sorted(tasks, key=lambda row: (row.occupied_start_minute, row.bucket_key))
		setups = [row for row in ordered if row.base_setup_minutes]
		if len(setups) > 1:
			_error(errors, "duplicate_campaign_setup", f"Campaign {key[0]} / {key[1]} charges base setup more than once.")
		elif alternative.base_setup_minutes and (not setups or setups[0] is not ordered[0]):
			_error(errors, "missing_campaign_setup", f"Campaign {key[0]} / {key[1]} does not charge setup on its first task.")
		if setups and setups[0].base_setup_minutes != alternative.base_setup_minutes:
			_error(errors, "setup_value", f"Campaign {key[0]} / {key[1]} uses an unexpected base setup duration.")


def _validate_no_overlap(errors: list[dict[str, Any]], solution: SolverSolution, resource_type: str) -> None:
	by_resource = defaultdict(list)
	for task in solution.tasks:
		resource = task.machine if resource_type == "machine" else task.mold
		if resource:
			by_resource[resource].append((task.occupied_start_minute, task.end_minute, task.key, task.changeover_minutes))
	for frozen in solution.frozen_intervals:
		if frozen.resource_type == resource_type and frozen.resource:
			by_resource[frozen.resource].append((frozen.start_minute, frozen.end_minute, f"frozen:{frozen.key}", 0))
	for resource, rows in by_resource.items():
		previous = None
		for row in sorted(rows):
			if previous and row[0] < previous[1]:
				_error(errors, f"{resource_type}_overlap", f"{resource_type.title()} {resource} overlaps between {previous[2]} and {row[2]}.")
			previous = row if previous is None or row[1] > previous[1] else previous


def _validate_multi_output_capacity_owners(errors, snapshot, solution):
	task_demands = {row.demand_key for row in solution.tasks}
	for group in snapshot.multi_output_groups:
		if group.capacity_owner_demand not in {row.key for row in snapshot.demands}:
			_error(errors, "campaign_owner_missing", f"Campaign group {group.key} has no capacity-owner demand.")
		if len(group.members) < 2 or len({row.item_code for row in group.members}) < 2:
			_error(errors, "campaign_outputs_invalid", f"Campaign group {group.key} requires at least two explicit output items.")
		yields_by_item = defaultdict(set)
		for member in group.members:
			yields_by_item[member.item_code].add(member.output_units_per_cycle)
		if any(len(values) != 1 for values in yields_by_item.values()):
			_error(errors, "campaign_yield_ambiguous", f"Campaign group {group.key} has inconsistent yields for one output item.")
		if any(row.output_units_per_cycle <= 0 for row in group.members):
			_error(errors, "campaign_yield_invalid", f"Campaign group {group.key} has a non-positive output yield.")
		derived_demands = {row.demand_key for row in group.members if row.demand_key != group.capacity_owner_demand}
		if task_demands.intersection(derived_demands):
			_error(errors, "campaign_double_capacity", f"Campaign group {group.key} scheduled a derived output as another capacity task.")


def _validate_precedences(errors, snapshot, solution):
	tasks = defaultdict(list)
	outcomes = {row.demand_key: row for row in solution.outcomes}
	for task in solution.tasks:
		tasks[task.demand_key].append(task)
	for edge in snapshot.precedences:
		if not edge.predecessor_demand:
			continue
		successors = tasks.get(edge.successor_demand) or []
		if not successors:
			continue
		predecessors = tasks.get(edge.predecessor_demand) or []
		predecessor_outcome = outcomes.get(edge.predecessor_demand)
		if not predecessors or (predecessor_outcome and predecessor_outcome.unscheduled_units > 0):
			_error(errors, "bom_predecessor_incomplete", f"Parent demand {edge.successor_demand} starts without complete child {edge.predecessor_demand}.")
			continue
		if max(row.end_minute for row in predecessors) + edge.lag_minutes > min(row.occupied_start_minute for row in successors):
			_error(errors, "bom_precedence", f"Parent demand {edge.successor_demand} starts before child {edge.predecessor_demand} is available.")


def _error(errors: list[dict[str, Any]], code: str, message: str) -> None:
	errors.append({"code": code, "message": message})
