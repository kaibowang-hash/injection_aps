from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.services.campaign_planning import collapse_family_demands_for_solver, plan_campaign_outputs, sync_campaign_actuals
from injection_aps.services.solver.input_builder import build_solver_input
from injection_aps.services.solver.scenarios import solve_scenarios


class TestPhase6Coproduct(unittest.TestCase):
	def test_max_cycles_covers_different_one_to_one_demands(self):
		plan = plan_campaign_outputs([
			{"item_code": "A", "output_per_cycle": 1, "required_qty": 10},
			{"item_code": "B", "output_per_cycle": 1, "required_qty": 7},
		], primary_item="A")
		self.assertEqual(plan["campaign_cycles"], 10)
		by_item = {row["item_code"]: row for row in plan["outputs"]}
		self.assertEqual(by_item["A"]["planned_qty"], 10)
		self.assertEqual(by_item["B"]["planned_qty"], 10)
		self.assertEqual(by_item["B"]["excess_qty"], 3)

	def test_undemanded_output_is_still_planned_with_explanation(self):
		plan = plan_campaign_outputs([
			{"item_code": "A", "output_per_cycle": 2, "required_qty": 10},
			{"item_code": "B", "output_per_cycle": 1, "required_qty": 0},
		], primary_item="A")
		by_item = {row["item_code"]: row for row in plan["outputs"]}
		self.assertEqual(plan["campaign_cycles"], 5)
		self.assertEqual(by_item["B"]["planned_qty"], 5)
		self.assertTrue(by_item["B"]["output_note"])

	def test_two_demanded_outputs_do_not_get_false_brought_out_note(self):
		plan = plan_campaign_outputs([
			{"item_code": "A", "output_per_cycle": 1, "required_qty": 3},
			{"item_code": "B", "output_per_cycle": 1, "required_qty": 2},
		])
		self.assertTrue(all(not row["output_note"] for row in plan["outputs"]))

	def test_solver_validates_one_capacity_owner_for_multiple_outputs(self):
		source = {
			"run_key": "RUN", "horizon_start": "2026-08-14T08:00:00", "horizon_end": "2026-08-14T20:00:00",
			"quantity_scale": 1, "time_limit_seconds": 6, "random_seed": 1,
			"demands": [{"key": "A", "result": "RA", "commitment": "CA", "item_code": "A", "admission_class": "P0", "quantity": 10, "due_time": "2026-08-14T20:00:00", "earliest_time": "2026-08-14T08:00:00", "alternatives": [{"key": "M1|F1", "machine": "M1", "mold": "F1", "output_per_cycle": 1, "cycle_minutes": 1}]}],
			"buckets": [{"key": "B1", "machine": "M1", "start": "2026-08-14T08:00:00", "end": "2026-08-14T20:00:00", "available_minutes": 720}],
			"multi_output_groups": [{"key": "F1|Default", "capacity_owner_demand": "A", "members": [
				{"demand_key": "A", "result": "RA", "commitment": "CA", "item_code": "A", "output_per_cycle": 1, "required_qty": 10, "due_time": "2026-08-14T20:00:00", "output_role": "Primary"},
				{"demand_key": "B", "result": "RB", "commitment": "CB", "item_code": "B", "output_per_cycle": 1, "required_qty": 7, "due_time": "2026-08-14T20:00:00", "output_role": "Co-product"},
			]}],
		}
		snapshot = build_solver_input(source)
		solution = solve_scenarios(snapshot)[0]
		self.assertTrue(dict(solution.validation)["valid"])
		self.assertEqual({row.demand_key for row in solution.tasks}, {"A"})

	def test_single_demand_still_collapses_and_uses_family_master_yield(self):
		demand = {
			"key": "A", "result": "RA", "commitment": "CA", "item_code": "A",
			"quantity": 10, "due_time": "2026-08-14T20:00:00", "admission_class": "P0",
			"alternatives": [{"key": "M1|F1", "machine": "M1", "mold": "F1", "output_per_cycle": 99}],
		}
		fake = MagicMock()
		fake.db.exists.return_value = True
		fake.get_all.side_effect = [
			["F1"],
			[
				frappe._dict(item_code="A", output_group="Default", output_qty=2, cavity_output_qty=2, idx=1),
				frappe._dict(item_code="B", output_group="Default", output_qty=1, cavity_output_qty=1, idx=2),
			],
		]
		with patch("injection_aps.services.campaign_planning.frappe", fake):
			collapsed, groups = collapse_family_demands_for_solver([demand])
		self.assertEqual(len(collapsed), 1)
		self.assertEqual(len(groups), 1)
		self.assertEqual(collapsed[0]["alternatives"][0]["output_per_cycle"], 2)
		self.assertEqual({row["item_code"] for row in groups[0]["members"]}, {"A", "B"})
		self.assertEqual(next(row for row in groups[0]["members"] if row["item_code"] == "B")["required_qty"], 0)

	def test_member_due_dates_drive_campaign_delivery_metrics(self):
		source = {
			"run_key": "RUN", "horizon_start": "2026-08-14T08:00:00", "horizon_end": "2026-08-14T09:00:00",
			"quantity_scale": 1, "time_limit_seconds": 6, "random_seed": 1,
			"demands": [{"key": "A", "result": "RA", "commitment": "CA", "item_code": "A", "admission_class": "P0", "quantity": 10, "due_time": "2026-08-14T09:00:00", "earliest_time": "2026-08-14T08:00:00", "alternatives": [{"key": "M1|F1", "machine": "M1", "mold": "F1", "output_per_cycle": 1, "cycle_minutes": 1}]}],
			"buckets": [{"key": "B1", "machine": "M1", "start": "2026-08-14T08:00:00", "end": "2026-08-14T09:00:00", "available_minutes": 60}],
			"multi_output_groups": [{"key": "F1|Default", "capacity_owner_demand": "A", "members": [
				{"demand_key": "A", "result": "RA", "commitment": "CA", "item_code": "A", "output_per_cycle": 1, "required_qty": 10, "due_time": "2026-08-14T09:00:00", "output_role": "Primary"},
				{"demand_key": "B", "result": "RB", "commitment": "CB", "item_code": "B", "output_per_cycle": 1, "required_qty": 7, "due_time": "2026-08-14T08:05:00", "output_role": "Co-product"},
			]}],
		}
		solution = solve_scenarios(build_solver_input(source))[0]
		self.assertGreaterEqual(solution.metrics.total_late_units, 7)

	def test_campaign_actuals_are_read_from_each_output_work_order(self):
		outputs = [
			SimpleNamespace(work_order="WO-A", actual_good_qty=0, actual_scrap_qty=0, output_role="Primary", output_per_cycle=2, planned_qty=10),
			SimpleNamespace(work_order="WO-B", actual_good_qty=0, actual_scrap_qty=0, output_role="Co-product", output_per_cycle=1, planned_qty=5),
		]
		doc = SimpleNamespace(outputs=outputs, actual_cycles=0, status="Released", flags=frappe._dict(), save=MagicMock())
		fake = MagicMock()
		fake.db.exists.return_value = True
		fake.get_all.return_value = ["CAMPAIGN-1"]
		fake.get_doc.return_value = doc
		fake.db.sql.side_effect = [
			[frappe._dict(good_qty=8, scrap_qty=1)],
			[frappe._dict(good_qty=3, scrap_qty=2)],
		]
		with patch("injection_aps.services.campaign_planning.frappe", fake):
			sync_campaign_actuals("RUN")
		self.assertEqual(outputs[0].actual_good_qty, 8)
		self.assertEqual(outputs[1].actual_good_qty, 3)
		self.assertEqual(outputs[0].actual_scrap_qty, 1)
		self.assertEqual(outputs[1].actual_scrap_qty, 2)
		self.assertEqual(doc.actual_cycles, 4)


if __name__ == "__main__":
	unittest.main()
