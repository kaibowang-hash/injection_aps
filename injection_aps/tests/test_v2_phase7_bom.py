from __future__ import annotations

import unittest

from injection_aps.services.bom_planning import BOMCycleError, expand_bom_requirements
from injection_aps.services.solver.input_builder import build_solver_input
from injection_aps.services.solver.scenarios import solve_scenarios


class TestPhase7BOM(unittest.TestCase):
	def _masters(self):
		return {
			"X": {"name": "BOM-X", "output_qty": 1, "components": [{"item_code": "A", "qty": 1, "loss_percent": 0}, {"item_code": "RM", "qty": 2, "loss_percent": 0}]},
			"Y": {"name": "BOM-Y", "output_qty": 1, "components": [{"item_code": "A", "qty": 1, "loss_percent": 0}]},
			"A": {"name": "BOM-A", "output_qty": 1, "components": [{"item_code": "C", "qty": 1, "loss_percent": 0}]},
			"C": {"name": "BOM-C", "output_qty": 1, "components": []},
		}

	def _expand(self, roots, **kwargs):
		return expand_bom_requirements(
			roots, bom_by_item=self._masters(),
			item_group_by_item={"X": "Finished", "Y": "Finished", "A": "Sub", "C": "Sub", "RM": "Raw"},
			producible_groups={"Sub"}, **kwargs,
		)

	def test_c_a_x_demands_and_precedence_are_created(self):
		result = self._expand([{"key": "X-DEMAND", "item_code": "X", "quantity": 10, "due_time": "2026-08-20T20:00:00"}])
		self.assertEqual({row["item_code"] for row in result["demands"]}, {"A", "C"})
		edges = {(row["predecessor_demand"], row["successor_demand"]) for row in result["precedences"] if row["predecessor_demand"]}
		self.assertIn(("BOM:C:202608202000", "BOM:A:202608202000"), edges)
		self.assertIn(("BOM:A:202608202000", "X-DEMAND"), edges)

	def test_shared_semifinished_is_merged_but_keeps_two_parent_peggings(self):
		result = self._expand([
			{"key": "X-DEMAND", "item_code": "X", "quantity": 5, "due_time": "2026-08-20T20:00:00"},
			{"key": "Y-DEMAND", "item_code": "Y", "quantity": 7, "due_time": "2026-08-20T20:00:00"},
		])
		a = next(row for row in result["demands"] if row["item_code"] == "A")
		self.assertEqual(a["quantity"], 12)
		self.assertEqual(len([row for row in result["peggings"] if row["component_item"] == "A"]), 2)
		self.assertEqual(
			{row["root_demand_key"] for row in result["peggings"] if row["component_item"] == "C"},
			{"X-DEMAND", "Y-DEMAND"},
		)
		c_edges = [row for row in result["precedences"] if row["component_item"] == "C"]
		self.assertEqual(len(c_edges), 1)
		self.assertEqual(set(c_edges[0]["root_demand_keys"]), {"X-DEMAND", "Y-DEMAND"})
		self.assertEqual(len(c_edges[0]["root_allocations"]), 2)

	def test_semifinished_stock_reduces_child_production_and_raw_zero_never_blocks(self):
		result = self._expand([{"key": "X", "item_code": "X", "quantity": 10, "due_time": "2026-08-20T20:00:00"}], stock_by_item={"A": 4, "RM": 0})
		a = next(row for row in result["demands"] if row["item_code"] == "A")
		c = next(row for row in result["demands"] if row["item_code"] == "C")
		self.assertEqual(a["quantity"], 6)
		self.assertEqual(c["quantity"], 6)
		raw = next(row for row in result["peggings"] if row["component_item"] == "RM")
		self.assertTrue(raw["is_raw_material_leaf"])
		self.assertFalse(any(row["item_code"] == "RM" for row in result["demands"]))

	def test_loss_and_batch_rounding_conserve_explicit_quantities(self):
		masters = self._masters()
		masters["X"]["components"][0]["loss_percent"] = 20
		result = expand_bom_requirements(
			[{"key": "X", "item_code": "X", "quantity": 8, "due_time": "2026-08-20T20:00:00"}],
			bom_by_item=masters, item_group_by_item={"X": "Finished", "A": "Sub", "C": "Sub", "RM": "Raw"},
			producible_groups={"Sub"}, batch_by_item={"A": 6},
		)
		a = next(row for row in result["demands"] if row["item_code"] == "A")
		self.assertEqual(a["quantity"], 12)
		pegging = next(row for row in result["peggings"] if row["component_item"] == "A")
		self.assertAlmostEqual(
			pegging["stock_covered_qty"] + pegging["wip_covered_qty"] + pegging["production_qty"],
			pegging["required_gross_qty"] + pegging["batch_excess_qty"],
		)

	def test_child_priority_inherits_the_strictest_root_that_requires_production(self):
		result = self._expand([
			{"key": "P2", "item_code": "X", "quantity": 4, "due_time": "2026-08-20T20:00:00", "admission_class": "P2", "service_priority": 10},
			{"key": "P1", "item_code": "Y", "quantity": 3, "due_time": "2026-08-20T20:00:00", "admission_class": "P1", "service_priority": 30},
		])
		a = next(row for row in result["demands"] if row["item_code"] == "A")
		self.assertEqual(a["admission_class"], "P1")
		self.assertEqual(a["service_priority"], 30)

	def test_bom_decision_changes_solver_input_fingerprint(self):
		base = {
			"run_key": "RUN", "horizon_start": "2026-08-14T08:00:00", "horizon_end": "2026-08-14T20:00:00",
			"quantity_scale": 1000, "time_limit_seconds": 3, "random_seed": 1,
			"demands": [{"key": "X", "item_code": "X", "quantity": 1, "due_time": "2026-08-14T20:00:00", "alternatives": [{"key": "M|F", "machine": "M", "mold": "F", "output_per_cycle": 1, "cycle_minutes": 1}]}],
			"buckets": [{"key": "B", "machine": "M", "start": "2026-08-14T08:00:00", "end": "2026-08-14T20:00:00", "available_minutes": 720}],
		}
		from injection_aps.services.solver.serialization import input_fingerprint
		left = build_solver_input({**base, "bom_decisions": [{"item_code": "X", "bom": "BOM-X", "bom_fingerprint": "a", "output_qty": 1}]})
		right = build_solver_input({**base, "bom_decisions": [{"item_code": "X", "bom": "BOM-X-ALT", "bom_fingerprint": "b", "output_qty": 1, "selection_source": "Explicit Approved Alternative"}]})
		self.assertNotEqual(input_fingerprint(left), input_fingerprint(right))

	def test_bom_cycle_is_hard_error(self):
		masters = self._masters()
		masters["C"]["components"] = [{"item_code": "A", "qty": 1, "loss_percent": 0}]
		with self.assertRaises(BOMCycleError):
			expand_bom_requirements(
				[{"key": "X", "item_code": "X", "quantity": 1, "due_time": "2026-08-20T20:00:00"}],
				bom_by_item=masters, item_group_by_item={"X": "Finished", "A": "Sub", "C": "Sub", "RM": "Raw"}, producible_groups={"Sub"},
			)

	def test_solver_enforces_child_completion_before_parent_start(self):
		snapshot = build_solver_input({
			"run_key": "RUN", "horizon_start": "2026-08-14T08:00:00", "horizon_end": "2026-08-14T20:00:00", "quantity_scale": 1, "time_limit_seconds": 9, "random_seed": 1,
			"demands": [
				{"key": "CHILD", "item_code": "A", "admission_class": "P0", "quantity": 10, "due_time": "2026-08-14T20:00:00", "alternatives": [{"key": "MC|F1", "machine": "MC", "mold": "F1", "output_per_cycle": 1, "cycle_minutes": 1}]},
				{"key": "PARENT", "item_code": "X", "admission_class": "P0", "quantity": 10, "due_time": "2026-08-14T20:00:00", "alternatives": [{"key": "MP|F2", "machine": "MP", "mold": "F2", "output_per_cycle": 1, "cycle_minutes": 1}]},
			],
			"buckets": [
				{"key": "BC", "machine": "MC", "start": "2026-08-14T08:00:00", "end": "2026-08-14T20:00:00", "available_minutes": 720},
				{"key": "BP", "machine": "MP", "start": "2026-08-14T08:00:00", "end": "2026-08-14T20:00:00", "available_minutes": 720},
			],
			"precedences": [{"predecessor_demand": "CHILD", "successor_demand": "PARENT", "required_available_time": "2026-08-14T20:00:00"}],
		})
		solution = solve_scenarios(snapshot)[0]
		self.assertTrue(dict(solution.validation)["valid"])
		child_end = max(row.end_minute for row in solution.tasks if row.demand_key == "CHILD")
		parent_start = min(row.occupied_start_minute for row in solution.tasks if row.demand_key == "PARENT")
		self.assertLessEqual(child_end, parent_start)


if __name__ == "__main__":
	unittest.main()
