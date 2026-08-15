from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class Alternative:
	key: str
	machine: str
	mold: str = ""
	plant_floor: str = ""
	output_units_per_cycle: int = 1
	# Keep the source cycle precision.  The CP model converts this value to
	# integer micro-minutes before building expressions, so a 43.2-second cycle
	# remains 0.72 minutes instead of being rounded up to a full minute.
	cycle_minutes: float = 1.0
	base_setup_minutes: int = 0
	tonnage_gap: int = 0
	preference_rank: int = 0
	continuity_rank: int = 0
	color_code: str = ""
	material_code: str = ""
	capacity_source: str = "mold_cycle"


@dataclass(frozen=True, slots=True)
class Demand:
	key: str
	result: str
	commitment: str
	item_code: str
	admission_class: str
	quantity_units: int
	due_minute: int
	earliest_minute: int = 0
	service_priority: int = 0
	original_due_minute: int | None = None
	fixed_on_time_units: int = 0
	fixed_late_units: int = 0
	alternatives: tuple[Alternative, ...] = ()


@dataclass(frozen=True, slots=True)
class CapacityBucket:
	key: str
	machine: str
	start_minute: int
	end_minute: int
	available_minutes: int
	capacity_factor_ppm: int = 1_000_000
	horizon_zone: str = "Demand"
	shift_key: str = ""


@dataclass(frozen=True, slots=True)
class FrozenInterval:
	key: str
	resource_type: str
	resource: str
	start_minute: int
	end_minute: int
	source_document: str = ""


@dataclass(frozen=True, slots=True)
class TransitionRule:
	from_family: str
	to_family: str
	setup_minutes: int
	blocked: bool = False


@dataclass(frozen=True, slots=True)
class MultiOutputMember:
	demand_key: str
	result: str
	commitment: str
	item_code: str
	output_units_per_cycle: int
	required_units: int
	due_minute: int
	output_role: str = "Co-product"
	admission_class: str = "P0"
	service_priority: int = 0
	fixed_on_time_units: int = 0
	fixed_late_units: int = 0


@dataclass(frozen=True, slots=True)
class MultiOutputGroup:
	"""One capacity-owner demand and every physical output of its mold cycle."""

	key: str
	capacity_owner_demand: str
	members: tuple[MultiOutputMember, ...]


@dataclass(frozen=True, slots=True)
class Precedence:
	"""Manufacturing child must be available before its parent may start."""

	predecessor_demand: str
	successor_demand: str
	lag_minutes: int = 0
	parent_item: str = ""
	component_item: str = ""
	bom: str = ""
	level: int = 0
	required_units: int = 0
	stock_covered_units: int = 0
	wip_covered_units: int = 0
	production_units: int = 0
	required_available_minute: int = 0
	root_demand_key: str = ""
	root_demand_keys: tuple[str, ...] = ()
	# (root demand key, required, stock, WIP, production), all quantities scaled.
	root_allocations: tuple[tuple[str, int, int, int, int], ...] = ()
	is_raw_material_leaf: bool = False
	bom_fingerprint: str = ""
	selection_source: str = "Default BOM"
	qty_per_parent_units: int = 0
	bom_output_units: int = 0
	loss_percent_ppm: int = 0
	batch_size_units: int = 0
	batch_excess_units: int = 0
	component_uom: str = ""
	stock_uom: str = ""
	conversion_factor_ppm: int = 1_000_000


@dataclass(frozen=True, slots=True)
class BOMDecision:
	"""Exact BOM master decision frozen into one solver input."""

	item_code: str
	bom: str
	bom_fingerprint: str
	selection_source: str
	output_units: int


@dataclass(frozen=True, slots=True)
class SolverInput:
	run_key: str
	horizon_start: str
	horizon_end: str
	quantity_scale: int
	time_limit_seconds: int
	random_seed: int
	demands: tuple[Demand, ...]
	buckets: tuple[CapacityBucket, ...]
	frozen_intervals: tuple[FrozenInterval, ...] = ()
	transition_rules: tuple[TransitionRule, ...] = ()
	multi_output_groups: tuple[MultiOutputGroup, ...] = ()
	precedences: tuple[Precedence, ...] = ()
	bom_decisions: tuple[BOMDecision, ...] = ()
	approved_overrides: tuple[tuple[str, str], ...] = ()
	schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class CapacityAllocation:
	demand_key: str
	result: str
	commitment: str
	alternative_key: str
	bucket_key: str
	machine: str
	mold: str
	quantity_units: int
	cycles: int
	production_minutes: int
	base_setup_minutes: int
	horizon_zone: str


@dataclass(frozen=True, slots=True)
class ScheduledTask:
	key: str
	demand_key: str
	result: str
	commitment: str
	alternative_key: str
	bucket_key: str
	machine: str
	mold: str
	quantity_units: int
	cycles: int
	occupied_start_minute: int
	production_start_minute: int
	end_minute: int
	base_setup_minutes: int
	changeover_minutes: int
	horizon_zone: str
	sequence_no: int


@dataclass(frozen=True, slots=True)
class DemandOutcome:
	demand_key: str
	result: str
	commitment: str
	on_time_units: int
	late_units: int
	unscheduled_units: int
	completion_minute: int | None
	recovery_completion_minute: int | None


@dataclass(frozen=True, slots=True)
class SolverMetrics:
	p0_on_time_units: int = 0
	p0_weighted_tardiness: int = 0
	p0_critical_unplanned_units: int = 0
	change_count: int = 0
	setup_minutes: int = 0
	continuity_units: int = 0
	tonnage_gap_units: int = 0
	utilization_spread_minutes: int = 0
	p1_p2_completed_units: int = 0
	total_scheduled_units: int = 0
	total_late_units: int = 0
	total_unscheduled_units: int = 0
	max_lateness_minutes: int = 0


@dataclass(frozen=True, slots=True)
class SolverSolution:
	scenario_key: str
	scenario_label: str
	status: str
	engine: str
	input_fingerprint: str
	solution_fingerprint: str
	runtime_seconds: float
	objective_values: tuple[tuple[str, int], ...]
	allocations: tuple[CapacityAllocation, ...]
	tasks: tuple[ScheduledTask, ...]
	outcomes: tuple[DemandOutcome, ...]
	metrics: SolverMetrics
	frozen_intervals: tuple[FrozenInterval, ...]
	best_bound: float | None = None
	objective_value: float | None = None
	gap_percent: float | None = None
	warnings: tuple[str, ...] = ()
	explanation: tuple[str, ...] = ()
	validation: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class CapacityResult:
	status: str
	allocations: tuple[CapacityAllocation, ...]
	outcomes: tuple[DemandOutcome, ...]
	objective_values: tuple[tuple[str, int], ...]
	runtime_seconds: float
	best_bound: float | None = None
	objective_value: float | None = None
	warnings: tuple[str, ...] = ()
