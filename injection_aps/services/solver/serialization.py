from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, TypeVar

from .models import (
	Alternative,
	BOMDecision,
	CapacityAllocation,
	CapacityBucket,
	Demand,
	DemandOutcome,
	FrozenInterval,
	MultiOutputGroup,
	MultiOutputMember,
	Precedence,
	ScheduledTask,
	SolverInput,
	SolverMetrics,
	SolverSolution,
	TransitionRule,
)


T = TypeVar("T")


def canonical_json(value: Any) -> str:
	if hasattr(value, "__dataclass_fields__"):
		value = asdict(value)
	return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value: Any) -> str:
	return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def input_fingerprint(snapshot: SolverInput) -> str:
	return fingerprint(snapshot)


def solution_fingerprint(solution: SolverSolution | dict[str, Any]) -> str:
	value = asdict(solution) if hasattr(solution, "__dataclass_fields__") else dict(solution)
	for key in ("solution_fingerprint", "runtime_seconds", "best_bound", "objective_value", "gap_percent"):
		value.pop(key, None)
	return fingerprint(value)


def input_to_dict(snapshot: SolverInput) -> dict[str, Any]:
	return asdict(snapshot)


def input_from_dict(value: dict[str, Any]) -> SolverInput:
	return SolverInput(
		run_key=value["run_key"],
		horizon_start=value["horizon_start"],
		horizon_end=value["horizon_end"],
		quantity_scale=int(value["quantity_scale"]),
		time_limit_seconds=int(value["time_limit_seconds"]),
		random_seed=int(value["random_seed"]),
		demands=tuple(
			Demand(
				**{
					**row,
					"alternatives": tuple(Alternative(**item) for item in row.get("alternatives") or ()),
				}
			)
			for row in value.get("demands") or ()
		),
		buckets=tuple(CapacityBucket(**row) for row in value.get("buckets") or ()),
		frozen_intervals=tuple(FrozenInterval(**row) for row in value.get("frozen_intervals") or ()),
		transition_rules=tuple(TransitionRule(**row) for row in value.get("transition_rules") or ()),
		multi_output_groups=tuple(
			MultiOutputGroup(
				key=row["key"],
				capacity_owner_demand=row["capacity_owner_demand"],
				members=tuple(MultiOutputMember(**item) for item in row.get("members") or ()),
			)
			for row in value.get("multi_output_groups") or ()
		),
		precedences=tuple(Precedence(**row) for row in value.get("precedences") or ()),
		bom_decisions=tuple(BOMDecision(**row) for row in value.get("bom_decisions") or ()),
		approved_overrides=tuple(tuple(row) for row in value.get("approved_overrides") or ()),
		schema_version=int(value.get("schema_version") or 1),
	)


def solution_to_dict(solution: SolverSolution) -> dict[str, Any]:
	return asdict(solution)


def solution_from_dict(value: dict[str, Any]) -> SolverSolution:
	return SolverSolution(
		scenario_key=value["scenario_key"],
		scenario_label=value["scenario_label"],
		status=value["status"],
		engine=value["engine"],
		input_fingerprint=value["input_fingerprint"],
		solution_fingerprint=value["solution_fingerprint"],
		runtime_seconds=float(value.get("runtime_seconds") or 0),
		objective_values=tuple(tuple(row) for row in value.get("objective_values") or ()),
		allocations=tuple(CapacityAllocation(**row) for row in value.get("allocations") or ()),
		tasks=tuple(ScheduledTask(**row) for row in value.get("tasks") or ()),
		outcomes=tuple(DemandOutcome(**row) for row in value.get("outcomes") or ()),
		metrics=SolverMetrics(**(value.get("metrics") or {})),
		frozen_intervals=tuple(FrozenInterval(**row) for row in value.get("frozen_intervals") or ()),
		best_bound=value.get("best_bound"),
		objective_value=value.get("objective_value"),
		gap_percent=value.get("gap_percent"),
		warnings=tuple(value.get("warnings") or ()),
		explanation=tuple(value.get("explanation") or ()),
		validation=tuple(tuple(row) for row in value.get("validation") or ()),
	)
