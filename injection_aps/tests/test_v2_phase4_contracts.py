from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from injection_aps.api import app


APP_ROOT = Path(__file__).resolve().parents[1]


class TestV2Phase4Contracts(unittest.TestCase):
	def test_solver_modules_do_not_import_frappe(self):
		for filename in ("models.py", "input_builder.py", "capacity_solver.py", "sequence_solver.py", "objectives.py", "scenarios.py", "serialization.py", "validator.py"):
			source = (APP_ROOT / "services" / "solver" / filename).read_text(encoding="utf-8")
			self.assertNotIn("import frappe", source, filename)

	def test_schema_has_solver_job_and_projection_fields(self):
		run = json.loads((APP_ROOT / "injection_aps/doctype/aps_planning_run/aps_planning_run.json").read_text())
		result = json.loads((APP_ROOT / "injection_aps/doctype/aps_schedule_result/aps_schedule_result.json").read_text())
		segment = json.loads((APP_ROOT / "injection_aps/doctype/aps_schedule_segment/aps_schedule_segment.json").read_text())
		self.assertTrue({"solver_job", "solver_status", "solver_input_fingerprint", "selected_solver_scenario", "total_critical_unplanned_qty"} <= {row["fieldname"] for row in run["fields"]})
		self.assertTrue({"on_time_qty", "recovery_qty", "critical_unplanned_qty", "solver_decision_json"} <= {row["fieldname"] for row in result["fields"]})
		self.assertTrue({"capacity_owner", "solver_start_time", "current_start_time", "solver_task_key"} <= {row["fieldname"] for row in segment["fields"]})

	def test_pinned_runtime_dependency(self):
		self.assertIn('install_requires=["ortools==9.4.1874"]', (APP_ROOT.parent / "setup.py").read_text())

	def test_solver_api_roles_and_scope(self):
		app.frappe.local.flags = app.frappe._dict(in_test=True)
		with patch.object(app, "_require_plan_access") as role, patch.object(app, "_require_complete_run_mutation_scope") as scope, patch.object(app.solver_orchestration, "analyze_v2_schedule", return_value={"status": "Queued"}) as service:
			app.analyze_v2_schedule("RUN", 0)
			role.assert_called_once_with()
			scope.assert_called_once_with("RUN", run_ptype="write")
			service.assert_called_once_with("RUN", run_in_background=False)

	def test_apply_api_requires_release_access(self):
		app.frappe.local.flags = app.frappe._dict(in_test=True)
		with patch.object(app, "_require_release_access") as role, patch.object(app, "_require_complete_run_mutation_scope") as scope, patch.object(app.solver_orchestration, "apply_v2_schedule", return_value={"status": "Applied"}) as service:
			app.apply_v2_schedule("RUN", "fp")
			role.assert_called_once_with()
			scope.assert_called_once_with("RUN", run_ptype="write")
			service.assert_called_once_with("RUN", expected_fingerprint="fp")

	def test_v2_apply_binds_formal_capacity_evidence(self):
		orchestration = (APP_ROOT / "services/solver_orchestration.py").read_text()
		capacity = (APP_ROOT / "services/capacity_balance.py").read_text()
		self.assertIn("finalize_v2_solver_apply_evidence", orchestration)
		self.assertIn("def finalize_v2_solver_apply_evidence", capacity)
		self.assertIn('analysis["applied_plan_fingerprint"]', capacity)
		self.assertIn('analysis["applied_resource_fingerprint"]', capacity)

	def test_solver_preserves_subminute_cycle_precision(self):
		builder = (APP_ROOT / "services/solver/input_builder.py").read_text()
		capacity = (APP_ROOT / "services/solver/capacity_solver.py").read_text()
		self.assertNotIn('math.ceil(float(item.get("cycle_minutes")', builder)
		self.assertIn("_cycle_minutes_ppm", capacity)
		self.assertIn('values["setup_here"]', capacity)


if __name__ == "__main__":
	unittest.main()
