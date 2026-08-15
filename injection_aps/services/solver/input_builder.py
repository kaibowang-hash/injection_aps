from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .models import (
	Alternative,
	BOMDecision,
	CapacityBucket,
	Demand,
	FrozenInterval,
	MultiOutputGroup,
	MultiOutputMember,
	Precedence,
	SolverInput,
	TransitionRule,
)


DEFAULT_QUANTITY_SCALE = 1000


def build_solver_input(source: dict[str, Any]) -> SolverInput:
	"""Convert a normalized application snapshot into immutable solver values.

	``source`` contains plain dict/list/scalar values.  This boundary intentionally
	does not accept Frappe Documents, which keeps unit and property tests independent
	from database state.
	"""
	horizon_start = _datetime(source["horizon_start"])
	horizon_end = _datetime(source["horizon_end"])
	if horizon_end <= horizon_start:
		raise ValueError("Solver horizon end must be after its start.")
	scale = max(int(source.get("quantity_scale") or DEFAULT_QUANTITY_SCALE), 1)

	demands = []
	for row in sorted(source.get("demands") or (), key=_demand_sort_key):
		alternatives = tuple(
			Alternative(
				key=str(item["key"]),
				machine=str(item["machine"]),
				mold=str(item.get("mold") or ""),
				plant_floor=str(item.get("plant_floor") or ""),
				output_units_per_cycle=max(_units(item.get("output_per_cycle") or 1, scale), 1),
				cycle_minutes=max(float(item.get("cycle_minutes") or 0), 0.000001),
				base_setup_minutes=max(int(math.ceil(float(item.get("base_setup_minutes") or 0))), 0),
				tonnage_gap=max(int(round(float(item.get("tonnage_gap") or 0) * 1000)), 0),
				preference_rank=max(int(item.get("preference_rank") or 0), 0),
				continuity_rank=max(int(item.get("continuity_rank") or 0), 0),
				color_code=str(item.get("color_code") or ""),
				material_code=str(item.get("material_code") or ""),
				capacity_source=str(item.get("capacity_source") or "mold_cycle"),
			)
			for item in sorted(row.get("alternatives") or (), key=lambda value: str(value.get("key") or ""))
		)
		demands.append(
			Demand(
				key=str(row["key"]),
				result=str(row.get("result") or ""),
				commitment=str(row.get("commitment") or ""),
				item_code=str(row.get("item_code") or ""),
				admission_class=str(row.get("admission_class") or "P0"),
				quantity_units=max(_units(row.get("quantity") or 0, scale), 0),
				due_minute=_minute(row.get("due_time"), horizon_start),
				earliest_minute=max(_minute(row.get("earliest_time") or horizon_start, horizon_start), 0),
				service_priority=int(row.get("service_priority") or 0),
				original_due_minute=_minute(row.get("original_due_time"), horizon_start) if row.get("original_due_time") else None,
				fixed_on_time_units=max(_units(row.get("fixed_on_time_qty") or 0, scale), 0),
				fixed_late_units=max(_units(row.get("fixed_late_qty") or 0, scale), 0),
				alternatives=alternatives,
			)
		)

	buckets = tuple(
		CapacityBucket(
			key=str(row["key"]),
			machine=str(row["machine"]),
			start_minute=_minute(row["start"], horizon_start),
			end_minute=_minute(row["end"], horizon_start),
			available_minutes=max(int(math.floor(float(row.get("available_minutes") or 0))), 0),
			capacity_factor_ppm=max(min(int(round(float(row.get("capacity_factor") or 1) * 1_000_000)), 1_000_000), 1),
			horizon_zone=str(row.get("horizon_zone") or "Demand"),
			shift_key=str(row.get("shift_key") or row["key"]),
		)
		for row in sorted(source.get("buckets") or (), key=lambda value: (str(value.get("machine") or ""), str(value.get("start") or ""), str(value.get("key") or "")))
		if _datetime(row["end"]) > _datetime(row["start"])
	)

	frozen = tuple(
		FrozenInterval(
			key=str(row["key"]),
			resource_type=str(row["resource_type"]),
			resource=str(row["resource"]),
			start_minute=_minute(row["start"], horizon_start),
			end_minute=_minute(row["end"], horizon_start),
			source_document=str(row.get("source_document") or ""),
		)
		for row in sorted(source.get("frozen_intervals") or (), key=lambda value: (str(value.get("resource_type") or ""), str(value.get("resource") or ""), str(value.get("start") or ""), str(value.get("key") or "")))
	)
	return SolverInput(
		run_key=str(source["run_key"]),
		horizon_start=horizon_start.isoformat(),
		horizon_end=horizon_end.isoformat(),
		quantity_scale=scale,
		time_limit_seconds=max(int(source.get("time_limit_seconds") or 120), 1),
		random_seed=int(source.get("random_seed") or 20260814),
		demands=tuple(demands),
		buckets=buckets,
		frozen_intervals=frozen,
		transition_rules=tuple(TransitionRule(**row) for row in source.get("transition_rules") or ()),
		multi_output_groups=tuple(
			MultiOutputGroup(
				key=str(row["key"]),
				capacity_owner_demand=str(row["capacity_owner_demand"]),
				members=tuple(
					MultiOutputMember(
						demand_key=str(member["demand_key"]),
						result=str(member.get("result") or ""),
						commitment=str(member.get("commitment") or ""),
						item_code=str(member["item_code"]),
						output_units_per_cycle=max(_units(member["output_per_cycle"], scale), 1),
						required_units=max(_units(member.get("required_qty") or 0, scale), 0),
						due_minute=_minute(member["due_time"], horizon_start),
						output_role=str(member.get("output_role") or "Co-product"),
						admission_class=str(member.get("admission_class") or "P0"),
						service_priority=int(member.get("service_priority") or 0),
						fixed_on_time_units=max(_units(member.get("fixed_on_time_qty") or 0, scale), 0),
						fixed_late_units=max(_units(member.get("fixed_late_qty") or 0, scale), 0),
					)
					for member in row.get("members") or ()
				),
			)
			for row in source.get("multi_output_groups") or ()
		),
		precedences=tuple(
			Precedence(
				predecessor_demand=str(row["predecessor_demand"]),
				successor_demand=str(row["successor_demand"]),
				lag_minutes=max(int(row.get("lag_minutes") or 0), 0),
				parent_item=str(row.get("parent_item") or ""), component_item=str(row.get("component_item") or ""),
				bom=str(row.get("bom") or ""), level=max(int(row.get("level") or 0), 0),
				required_units=max(_units(row.get("required_qty") or 0, scale), 0),
				stock_covered_units=max(_units(row.get("stock_covered_qty") or 0, scale), 0),
				wip_covered_units=max(_units(row.get("wip_covered_qty") or 0, scale), 0),
				production_units=max(_units(row.get("production_qty") or 0, scale), 0),
				required_available_minute=_minute(row.get("required_available_time") or horizon_start, horizon_start),
				root_demand_key=str(row.get("root_demand_key") or ""),
				root_demand_keys=tuple(str(value) for value in row.get("root_demand_keys") or ()),
				root_allocations=tuple(
					(
						str(allocation.get("root_demand_key") or ""),
						max(_units(allocation.get("required_qty") or 0, scale), 0),
						max(_units(allocation.get("stock_covered_qty") or 0, scale), 0),
						max(_units(allocation.get("wip_covered_qty") or 0, scale), 0),
						max(_units(allocation.get("production_qty") or 0, scale), 0),
					)
					for allocation in row.get("root_allocations") or ()
				),
				is_raw_material_leaf=bool(row.get("is_raw_material_leaf")),
				bom_fingerprint=str(row.get("bom_fingerprint") or ""),
				selection_source=str(row.get("selection_source") or "Default BOM"),
				qty_per_parent_units=max(_units(row.get("qty_per_parent") or 0, scale), 0),
				bom_output_units=max(_units(row.get("bom_output_qty") or 0, scale), 0),
				loss_percent_ppm=max(int(round(float(row.get("loss_percent") or 0) * 10_000)), 0),
				batch_size_units=max(_units(row.get("batch_size") or 0, scale), 0),
				batch_excess_units=max(_units(row.get("batch_excess_qty") or 0, scale), 0),
				component_uom=str(row.get("component_uom") or ""),
				stock_uom=str(row.get("stock_uom") or ""),
				conversion_factor_ppm=max(int(round(float(row.get("conversion_factor") or 1) * 1_000_000)), 1),
			)
			for row in source.get("precedences") or ()
		),
		bom_decisions=tuple(
			BOMDecision(
				item_code=str(row["item_code"]), bom=str(row["bom"]),
				bom_fingerprint=str(row["bom_fingerprint"]),
				selection_source=str(row.get("selection_source") or "Default BOM"),
				output_units=max(_units(row.get("output_qty") or 0, scale), 1),
			)
			for row in sorted(source.get("bom_decisions") or (), key=lambda value: (str(value.get("item_code") or ""), str(value.get("bom") or "")))
		),
		approved_overrides=tuple(sorted((str(row[0]), str(row[1])) for row in source.get("approved_overrides") or ())),
	)


def _datetime(value: Any) -> datetime:
	if isinstance(value, datetime):
		return value.replace(tzinfo=None)
	return datetime.fromisoformat(str(value)).replace(tzinfo=None)


def _minute(value: Any, origin: datetime) -> int:
	return int(math.floor((_datetime(value) - origin).total_seconds() / 60))


def _units(value: Any, scale: int) -> int:
	return int(round(float(value or 0) * scale))


def _demand_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
	return (
		0 if str(row.get("admission_class") or "P0") == "P0" else 1,
		str(row.get("due_time") or ""),
		-int(row.get("service_priority") or 0),
		str(row.get("key") or ""),
	)
