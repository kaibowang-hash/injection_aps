from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from injection_aps.api import app
from injection_aps.services import constraint_resolution


APP_ROOT = Path(__file__).resolve().parents[1]


class TestPhase3Contracts(unittest.TestCase):
	def setUp(self):
		app.frappe.local.flags = app.frappe._dict(in_test=True)

	def test_resolution_doctype_is_engine_managed_and_idempotent(self):
		data = json.loads((APP_ROOT / "injection_aps" / "doctype" / "aps_constraint_resolution" / "aps_constraint_resolution.json").read_text())
		fields = {row["fieldname"]: row for row in data["fields"]}
		self.assertEqual(data["read_only"], 1)
		self.assertEqual(fields["idempotency_key"]["unique"], 1)
		self.assertIn("Never Override", fields["blocker_policy"]["options"])
		self.assertIn("Exclude Only", fields["blocker_policy"]["options"])

	def test_run_schema_contains_four_windows_and_canonical_states(self):
		data = json.loads((APP_ROOT / "injection_aps" / "doctype" / "aps_planning_run" / "aps_planning_run.json").read_text())
		fields = {row["fieldname"]: row for row in data["fields"]}
		for fieldname in (
			"demand_horizon_end_date", "freeze_horizon_end_date", "restricted_horizon_end_date",
			"recovery_horizon_start_date", "recovery_horizon_end_date",
		):
			self.assertIn(fieldname, fields)
		for state in ("Ready", "Acknowledgment Required", "Hard Blocked", "Applied with Exceptions"):
			self.assertIn(state, fields["capacity_balance_status"]["options"])

	def test_ui_never_hides_hard_blocked_next_action(self):
		source = (APP_ROOT / "public" / "js" / "aps_planning_run.js").read_text()
		self.assertIn('capacity_balance_status === "Hard Blocked"', source)
		self.assertIn('Open Resolution Center', source)
		self.assertIn('Review and Acknowledge', source)
		self.assertIn('analysis.next_action', source)

	def test_recovery_is_not_used_as_demand_query_boundary(self):
		source = (APP_ROOT / "services" / "demand_ledger.py").read_text()
		self.assertIn('run.get("demand_horizon_end_date") or run.horizon_end', source)
		planning = (APP_ROOT / "services" / "planning.py").read_text()
		self.assertIn('(\"<=\", run_doc.demand_horizon_end_date)', planning)

	def test_apply_live_path_explicitly_disables_material_resources(self):
		source = (APP_ROOT / "services" / "capacity_balance.py").read_text()
		self.assertIn('include_material_resources=not v2_enabled', source)
		self.assertIn('analysis["material_advisory"] = material_advisory', source)

	def test_v2_risk_acknowledgment_requires_approver_role(self):
		with (
			patch.object(app.v2_flags, "is_v2_enabled", return_value=True),
			patch.object(app, "_require_approve_access") as approve,
			patch.object(app, "_require_plan_access") as plan,
			patch.object(app, "_require_complete_run_mutation_scope"),
			patch.object(app.capacity_balance, "confirm_capacity_balance", return_value={"status": "Acknowledgment Required"}),
		):
			app.confirm_capacity_balance("RUN-1")
		approve.assert_called_once_with()
		plan.assert_not_called()

	def test_exclusion_api_requires_approver_and_scoped_resolution(self):
		with (
			patch.object(app, "_require_approve_access") as approve,
			patch.object(app, "_require_scoped_document_access") as scoped,
			patch.object(app.constraint_resolution, "exclude_commitment_from_release", return_value={"status": "Excluded"}) as service,
		):
			result = app.exclude_commitment_from_release("CR-1", "approved exception", "fp-1")
		approve.assert_called_once_with()
		scoped.assert_called_once_with("APS Constraint Resolution", "CR-1", ptype="read")
		service.assert_called_once_with("CR-1", reason="approved exception", expected_fingerprint="fp-1")
		self.assertEqual(result["status"], "Excluded")

	def test_resolution_recompute_reenters_cp_sat_instead_of_legacy_analysis(self):
		run = app.frappe._dict(name="RUN-1", capacity_balance_fingerprint="fp-1")
		with (
			patch.object(constraint_resolution, "_require_v2"),
			patch.object(constraint_resolution.frappe, "get_doc", return_value=run),
			patch("injection_aps.services.v2_flags.get_v2_settings", return_value={"enable_aps_v2": 1, "solver_engine": "CP-SAT"}),
			patch("injection_aps.services.solver_orchestration.analyze_v2_schedule", return_value={"status": "Queued"}) as solver,
			patch("injection_aps.services.capacity_balance.analyze_capacity_balance") as legacy,
		):
			result = constraint_resolution.recompute_after_resolution("RUN-1", expected_fingerprint="fp-1")
		self.assertEqual(result["status"], "Queued")
		solver.assert_called_once_with("RUN-1", run_in_background=True)
		legacy.assert_not_called()


if __name__ == "__main__":
	unittest.main()
