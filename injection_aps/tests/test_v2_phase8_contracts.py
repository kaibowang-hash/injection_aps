from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestPhase8Contracts(unittest.TestCase):
	def test_progress_projection_uses_current_formal_owner_across_runs(self):
		source = (ROOT / "services/progress_v2.py").read_text()
		self.assertIn("formal_owner = 1", source)
		self.assertIn("owner_state = 'Owned'", source)
		self.assertIn('projection_type = "Single Run" if run_name else "Effective Cross-Run"', source)
		self.assertNotIn("order by status", source.lower())
		for layer in (
			"schedule_qty", "original_plan_qty", "current_plan_qty", "forecast_qty",
			"actual_good_qty", "actual_scrap_qty", "delivery_plan_qty", "delivered_qty",
			"stock_covered_qty", "shortage_qty", "recovery_qty",
		):
			self.assertIn(f'"{layer}"', source)

	def test_progress_api_is_flagged_permission_filtered_and_backward_compatible(self):
		source = (ROOT / "api/app.py").read_text()
		for method in (
			"get_customer_schedule_progress_v2", "get_progress_matrix",
			"get_progress_cell_drilldown", "_sanitize_progress_v2_response",
		):
			self.assertIn(f"def {method}", source)
		self.assertIn("if v2_flags.is_v2_enabled():", source)
		self.assertIn("planning.get_customer_schedule_progress_data(", source)
		self.assertIn("_filter_progress_source_documents", source)

	def test_progress_page_has_detail_matrix_paging_export_and_drilldown(self):
		source = (ROOT / "injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js").read_text()
		for token in (
			"Date Matrix", "renderV2Table", "renderMatrix", "column_offset", "page_length",
			"export_rows_to_excel", "get_progress_cell_drilldown", "Current effective cross-Run projection",
		):
			self.assertIn(token, source)
		self.assertIn("this.viewField.$wrapper.toggle(this.v2Enabled)", source)
		self.assertEqual(source.count("injection_aps.api.app.get_customer_schedule_progress_data"), 1)

	def test_gantt_collapses_campaign_and_exposes_all_four_layers(self):
		api = (ROOT / "api/app.py").read_text()
		ui = (ROOT / "injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js").read_text()
		css = (ROOT / "public/css/injection_aps.css").read_text()
		for token in (
			"campaign_outputs", "is_campaign_derived", "campaign_owner_segment",
			"original_start_time", "current_start_time", "forecast_start_time", "actual_start_time",
		):
			self.assertIn(token, api)
		for token in (
			"is_campaign_derived", "openCampaignOutputs", "campaign_outputs",
			"renderTaskLayer", "original", "forecast", "actual",
		):
			self.assertIn(token, ui)
		for selector in (
			".ia-gantt-plan-layer.original", ".ia-gantt-plan-layer.forecast", ".ia-gantt-plan-layer.actual",
		):
			self.assertIn(selector, css)

	def test_phase8_patch_only_adds_query_indexes(self):
		patch = (ROOT / "patches/v0_0_2/implement_aps_v2_phase8_progress_ui.py").read_text()
		self.assertIn("add_index", patch)
		self.assertNotIn("Property Setter", patch)
		self.assertNotIn("Workspace", patch)
		self.assertNotIn("delete_doc", patch)
		self.assertNotIn("db.set_value", patch)

	def test_trial_comparison_is_audited_read_only_and_visible(self):
		baseline = (ROOT / "services/v2_baseline.py").read_text()
		solver = (ROOT / "services/solver_orchestration.py").read_text()
		ui = (ROOT / "injection_aps/page/aps_solver_scenario_comparison/aps_solver_scenario_comparison.js").read_text()
		self.assertIn("legacy_trial_baseline", baseline)
		self.assertIn("V2_TRIAL_COMPARISON_READY", baseline)
		self.assertIn('run.run_type != "Formal"', solver)
		self.assertIn("Legacy / V2 Trial Comparison", ui)
		self.assertIn("Trial comparison is read-only", ui)

	def test_solver_preview_is_visible_and_invalid_scenarios_are_disabled(self):
		solver = (ROOT / "services/solver_orchestration.py").read_text()
		ui = (ROOT / "injection_aps/page/aps_solver_scenario_comparison/aps_solver_scenario_comparison.js").read_text()
		planning_form = (ROOT / "public/js/aps_planning_run.js").read_text()
		self.assertIn('"tasks": [_task_summary', solver)
		self.assertIn("Proposed tasks before Apply", ui)
		self.assertIn('row.valid === true && row.status !== "Failed"', ui)
		self.assertIn('selectable ? "" : "disabled"', ui)
		self.assertIn("selection_locked", solver)
		self.assertIn("selection_locked", ui)
		self.assertIn("Scenario selection is locked after Apply", solver)
		self.assertIn("Proposed tasks before Apply", planning_form)

	def test_progress_identity_is_readable_by_pmc_and_gmc(self):
		import json

		definition = json.loads(
			(ROOT / "injection_aps/doctype/aps_demand_identity/aps_demand_identity.json").read_text()
		)
		read_roles = {row["role"] for row in definition["permissions"] if row.get("read")}
		self.assertTrue({"PMC", "GMC"} <= read_roles)

	def test_confirm_run_ui_requires_applied_capacity_evidence(self):
		planning = (ROOT / "services/planning.py").read_text()
		form = (ROOT / "public/js/aps_planning_run.js").read_text()
		for token in ("capacity_release_ready", "applied_plan_fingerprint", "applied_resource_fingerprint"):
			self.assertIn(token, planning)
		self.assertIn("Approved V2 runs cannot be recalculated in place", planning)
		self.assertIn("Work Order proposals have already been generated", planning)
		self.assertIn("Apply the reviewed Work Order proposal batch", planning)
		self.assertIn("Re-analyze and Apply capacity after the Work Order change", planning)
		for token in ("applied_plan_fingerprint", "applied_resource_fingerprint"):
			self.assertIn(token, form)


if __name__ == "__main__":
	unittest.main()
