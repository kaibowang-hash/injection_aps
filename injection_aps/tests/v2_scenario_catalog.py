from __future__ import annotations

from types import MappingProxyType


FIXTURE_PREFIX = "APS-V2-FIXTURE-"

SCENARIOS = MappingProxyType(
	{
		"overlapping_schedule_revisions": {
			"requirement": "R-REV-OVERLAP",
			"summary": "A 8.1-8.14 and B 8.8-8.14 overlap without double demand ownership.",
			"demands": (
				{"revision": "A", "status": "Superseded", "item": "REV", "day": 1, "qty": 100},
				{"revision": "A", "status": "Superseded", "item": "REV", "day": 8, "qty": 100},
				{"revision": "B", "status": "Active", "item": "REV", "day": 8, "qty": 100},
				{"revision": "B", "status": "Active", "item": "REV", "day": 14, "qty": 120},
			),
			"expected": {"active_revision": "B", "overlap_qty": 100, "delta_qty": 20},
		},
		"schedule_without_sales_order": {
			"requirement": "R-WO-NO-SO",
			"summary": "A customer schedule can plan, produce and later allocate delivery without a Work Order SO link.",
			"demands": ({"item": "NO-SO", "day": 3, "qty": 100, "sales_order": None},),
			"expected": {"work_order_sales_order": None, "planned_qty": 100},
		},
		"overdue_p0_demand": {
			"requirement": "R-HORIZON-OVERDUE",
			"summary": "An open demand before the run start remains an admitted P0 demand.",
			"demands": ({"item": "OVERDUE", "day": -1, "qty": 50},),
			"expected": {"admission_class": "P0", "planned_qty": 50},
		},
		"demand_1200_capacity_1000": {
			"requirement": "R-PARTIAL-CAPACITY",
			"summary": "Plan 1000, retain shortage 200 and report the recovery time.",
			"demands": ({"item": "CAPACITY", "day": 1, "qty": 1200},),
			"resources": ({"resource": "MACHINE-120T", "daily_capacity_qty": 1000},),
			"expected": {"planned_qty": 1000, "shortage_qty": 200},
		},
		"two_moulds_one_machine": {
			"requirement": "R-RESOURCE-NO-OVERLAP",
			"summary": "Mould A and B share one machine; delivery wins before changeover efficiency.",
			"demands": (
				{"item": "MOULD-A", "day": 2, "qty": 600, "mould": "MOULD-A"},
				{"item": "MOULD-B", "day": 1, "qty": 600, "mould": "MOULD-B"},
			),
			"resources": ({"resource": "MACHINE-120T", "daily_capacity_qty": 1000},),
			"expected": {"no_overlap": True, "first_item": "MOULD-B"},
		},
		"overlapping_formal_runs": {
			"requirement": "R-CROSS-RUN-OWNER",
			"summary": "Run 2 cannot claim quantity still owned by an effective Run 1 commitment.",
			"demands": ({"item": "RUN-OWNER", "day": 3, "qty": 500},),
			"formal_runs": (
				{"key": "RUN-1", "day": 0, "status": "Applied", "owned_qty": 300},
				{"key": "RUN-2", "day": 1, "status": "Approved", "requested_qty": 500},
			),
			"expected": {"run_2_max_claim_qty": 200},
		},
		"delayed_formal_wos": {
			"requirement": "R-SHIFT-REPLAN",
			"summary": "Actual delay creates a proposal without moving running or frozen work.",
			"demands": ({"item": "DELAYED", "day": 2, "qty": 300},),
			"formal_runs": (
				{"key": "DELAYED-RUN", "day": 0, "status": "Applied", "owned_qty": 300},
			),
			"execution": {"state": "Running", "delay_minutes": 180, "frozen": True},
			"expected": {"proposal_only": True, "running_task_moved": False},
		},
		"family_mould": {
			"requirement": "R-COPRODUCT",
			"summary": "Two output Work Orders share one Campaign and one resource interval.",
			"demands": (
				{"item": "FAMILY-A", "day": 3, "qty": 100, "mould": "FAMILY-01"},
				{"item": "FAMILY-B", "day": 3, "qty": 50, "mould": "FAMILY-01"},
			),
			"resources": ({"resource": "MACHINE-120T", "daily_capacity_qty": 1000},),
			"expected": {"work_order_count": 2, "campaign_count": 1, "resource_interval_count": 1},
		},
		"multilevel_c_a_x": {
			"requirement": "R-MULTILEVEL-BOM",
			"summary": "C precedes A and A precedes X while raw material remains advisory.",
			"demands": ({"item": "BOM-X", "day": 5, "qty": 100},),
			"bom_edges": (("BOM-C", "BOM-A"), ("BOM-A", "BOM-X")),
			"expected": {"precedence": ("BOM-C", "BOM-A", "BOM-X")},
		},
		"delivery_plan_multi_so_dn": {
			"requirement": "R-DELIVERY-LINEAGE",
			"summary": "Delivery Plan allocates multiple Sales Orders and Delivery Note returns fulfillment to demand.",
			"demands": ({"item": "DELIVERY", "day": 4, "qty": 100},),
			"delivery": {
				"sales_orders": ({"key": "SO-1", "qty": 60}, {"key": "SO-2", "qty": 40}),
				"delivery_plan_qty": 100,
				"delivery_note_qty": 75,
			},
			"expected": {"fulfilled_qty": 75, "open_qty": 25},
		},
		"zero_raw_material": {
			"requirement": "R-MATERIAL-ADVISORY",
			"summary": "Zero raw material is visible but does not change feasibility, quantity or Apply fingerprint.",
			"demands": ({"item": "RAW-ZERO-FG", "day": 2, "qty": 100},),
			"materials": ({"item": "RAW-ZERO-RM", "available_qty": 0, "required_qty": 100},),
			"expected": {"planned_qty": 100, "material_blocks_feasibility": False},
		},
	}
)


def get_scenario_manifest() -> dict:
	"""Return data-only fixture definitions; this function never writes a site."""
	return {
		"fixture_prefix": FIXTURE_PREFIX,
		"mutation_enabled": False,
		"scenario_count": len(SCENARIOS),
		"scenarios": {name: _plain(spec) for name, spec in SCENARIOS.items()},
	}


def _plain(value):
	if isinstance(value, dict):
		return {key: _plain(item) for key, item in value.items()}
	if isinstance(value, tuple):
		return [_plain(item) for item in value]
	return value
