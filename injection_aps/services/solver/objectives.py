from __future__ import annotations


SCENARIO_PROFILES = {
	"recommended": (
		"p0_on_time",
		"p0_weighted_tardiness",
		"p0_unplanned",
		"assignment_changes",
		"setup_minutes",
		"continuity",
		"tonnage_gap",
		"utilization_spread",
		"optional_completion",
	),
	"delivery_priority": (
		"p0_on_time",
		"p0_weighted_tardiness",
		"p0_unplanned",
		"optional_completion",
	),
	"minimum_changeover": (
		"p0_on_time",
		"p0_weighted_tardiness",
		"p0_unplanned",
		"setup_minutes",
		"assignment_changes",
		"continuity",
		"tonnage_gap",
		"utilization_spread",
		"optional_completion",
	),
}


DELIVERY_OBJECTIVES = ("p0_on_time", "p0_weighted_tardiness", "p0_unplanned")


SCENARIO_LABELS = {
	"recommended": "Recommended",
	"delivery_priority": "Delivery Priority",
	"minimum_changeover": "Minimum Changeover",
}


MAXIMIZE_OBJECTIVES = {"p0_on_time", "continuity", "optional_completion"}


def objective_direction(name: str) -> str:
	return "max" if name in MAXIMIZE_OBJECTIVES else "min"
