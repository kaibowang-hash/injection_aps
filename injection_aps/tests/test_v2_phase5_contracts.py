from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestPhase5Contracts(unittest.TestCase):
	def test_replan_doctype_is_engine_managed_and_auditable(self):
		definition = json.loads((ROOT / "injection_aps/doctype/aps_replan_cycle/aps_replan_cycle.json").read_text())
		fields = {row["fieldname"] for row in definition["fields"]}
		self.assertTrue({"execution_cutoff", "freshness_status", "baseline_json", "forecast_json", "diff_json", "input_fingerprint", "solution_fingerprint", "idempotency_key"} <= fields)
		self.assertTrue(all(not row.get("write") and not row.get("create") and not row.get("delete") for row in definition["permissions"]))

	def test_scheduler_entry_point_is_proposal_only(self):
		source = (ROOT / "services/shift_replan.py").read_text()
		body = source[source.index("def scheduled_shift_replan") : source.index("\ndef _load_baseline_segments")]
		self.assertNotIn("apply_replan_cycle(", body)
		self.assertIn("refresh_shift_actuals(run_name)", body)
		self.assertIn("now.hour not in {7, 19}", body)
		self.assertIn('"applied": 0', body)

	def test_shift_replan_page_is_not_forced_into_workspace(self):
		workspace = (ROOT / "injection_aps/workspace/injection_aps/injection_aps.json").read_text()
		self.assertNotIn("aps-shift-replan-center", workspace)

	def test_replan_generates_reviewable_shift_proposal_before_apply(self):
		replan = (ROOT / "services/shift_replan.py").read_text()
		planning = (ROOT / "services/planning.py").read_text()
		self.assertIn("def generate_replan_proposals", replan)
		self.assertIn("planning.apply_shift_schedule_proposals", replan)
		self.assertIn("segment_overrides", planning)
		self.assertIn('"replan_cycle": replan_cycle', planning)

	def test_public_shift_api_contract_names_remain_available(self):
		api = (ROOT / "api/app.py").read_text()
		for method in (
			"create_shift_replan_cycle",
			"analyze_shift_replan",
			"get_shift_replan_diff",
			"generate_shift_replan_proposals",
			"apply_shift_replan_proposal",
		):
			self.assertIn(f"def {method}", api)

	def test_campaign_sync_dependency_is_available_during_replan_apply(self):
		source = (ROOT / "services/shift_replan.py").read_text()
		self.assertIn("from injection_aps.services import bom_planning, campaign_planning, v2_flags", source)
		apply_body = source[source.index("def apply_replan_cycle") : source.index("\ndef get_replan_cycle")]
		self.assertIn("campaign_planning.sync_campaign_derived_segments", apply_body)

	def test_shift_replan_page_matches_backend_role_boundaries(self):
		page = (ROOT / "injection_aps/page/aps_shift_replan_center/aps_shift_replan_center.js").read_text()
		shared = (ROOT / "public/js/injection_aps_shared.js").read_text()
		self.assertIn('frappe.require("/assets/injection_aps/js/injection_aps_shared.js"', page)
		for action in (
			"create_replan_cycle",
			"refresh_shift_actuals",
			"generate_replan_proposals",
			"acknowledge_replan_fallback",
			"approve_replan_cycle",
			"apply_replan_cycle",
		):
			self.assertIn(action, page)
			self.assertIn(f"{action}:", shared)


if __name__ == "__main__":
	unittest.main()
