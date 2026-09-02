from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestPhase7Contracts(unittest.TestCase):
	def test_pegging_and_bom_policy_schema(self):
		pegging = json.loads((ROOT / "injection_aps/doctype/aps_bom_pegging/aps_bom_pegging.json").read_text())
		fields = {row["fieldname"] for row in pegging["fields"]}
		self.assertTrue({"root_demand_key", "parent_demand_key", "child_demand_key", "parent_result", "child_result", "required_gross_qty", "stock_covered_qty", "wip_covered_qty", "production_qty", "required_available_time", "batch_size", "batch_excess_qty", "component_uom", "stock_uom", "conversion_factor", "selection_source"} <= fields)
		settings = json.loads((ROOT / "injection_aps/doctype/aps_settings/aps_settings.json").read_text())
		self.assertTrue({"aps_producible_item_groups", "aps_bom_policy"} <= {row["fieldname"] for row in settings["fields"]})

	def test_validator_is_independent_and_checks_precedence(self):
		source = (ROOT / "services/solver/validator.py").read_text()
		self.assertIn("_validate_precedences", source)
		self.assertNotIn("from .sequence_solver import", source)

	def test_bom_decision_tree_and_drag_preview_surfaces_exist(self):
		api = (ROOT / "api/app.py").read_text()
		planning_run = (ROOT / "public/js/aps_planning_run.js").read_text()
		gantt = (ROOT / "injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js").read_text()
		planning = (ROOT / "services/planning.py").read_text()
		for method in ("get_run_bom_selections", "set_run_bom_selections", "get_bom_pegging_tree"):
			self.assertIn(f"def {method}", api)
		self.assertIn("BOM Decisions", planning_run)
		self.assertIn("renderDependencyLinks", gantt)
		self.assertIn("preview_segment_precedence", planning)


if __name__ == "__main__":
	unittest.main()
