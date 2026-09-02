from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from injection_aps.api import app
from injection_aps.services import v2_baseline, v2_flags
from injection_aps.tests import v2_fixture_builder, v2_phase0_gate
from injection_aps.tests.v2_scenario_catalog import get_scenario_manifest


class TestV2Phase0Contracts(unittest.TestCase):
	def test_v2_flags_fail_closed_when_fields_are_missing(self):
		settings = v2_flags.get_v2_settings({})
		self.assertEqual(settings, dict(v2_flags.DEFAULTS))
		self.assertFalse(v2_flags.is_v2_enabled({}))

	def test_invalid_solver_engine_falls_back_to_legacy(self):
		settings = v2_flags.get_v2_settings(
			{"enable_aps_v2": 1, "solver_engine": "unknown", "solver_time_limit_seconds": -1}
		)
		self.assertEqual(settings["enable_aps_v2"], 1)
		self.assertEqual(settings["solver_engine"], "Legacy")
		self.assertEqual(settings["solver_time_limit_seconds"], 0)

	def test_capabilities_never_advertise_formal_v2_writes_in_phase0(self):
		with patch.object(v2_flags.util, "find_spec", return_value=None):
			capabilities = v2_flags.get_v2_capabilities({"enable_aps_v2": 1})
		self.assertFalse(capabilities["formal_v2_writes_enabled"])
		self.assertFalse(capabilities["read_only_trial_available"])
		self.assertFalse(capabilities["solver_runtime"]["available"])

	def test_baseline_fingerprint_is_independent_of_row_and_capture_order(self):
		first = v2_baseline.build_snapshot(
			scope={"planning_run": "RUN-1", "company": "C"},
			sections={"results": [{"name": "B", "qty": 2}, {"name": "A", "qty": 1}]},
			captured_at=datetime(2026, 8, 14, 9, 0),
		)
		second = v2_baseline.build_snapshot(
			scope={"company": "C", "planning_run": "RUN-1"},
			sections={"results": [{"qty": 1, "name": "A"}, {"qty": 2, "name": "B"}]},
			captured_at=datetime(2026, 8, 14, 10, 0),
		)
		self.assertEqual(first["content_fingerprint"], second["content_fingerprint"])
		self.assertNotEqual(first["captured_at"], second["captured_at"])
		self.assertEqual(first["capture_mode"], "READ_ONLY")

	def test_comparison_is_an_explicit_non_writable_phase0_stub(self):
		comparison = v2_baseline.get_legacy_v2_comparison(
			"RUN-1",
			legacy_fingerprint="abc",
			settings={"enable_aps_v2": 0},
		)
		self.assertEqual(comparison["status"], "Not Available")
		self.assertEqual(comparison["reason_code"], "PHASE_0_V2_ENGINE_NOT_IMPLEMENTED")
		self.assertFalse(comparison["formal_v2_writes_enabled"])

	def test_api_baseline_requires_read_and_complete_run_scope_access(self):
		app.frappe.local.flags = app.frappe._dict(in_test=True)
		with (
			patch.object(app, "_require_read_access") as require_read,
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(
				app.v2_baseline,
				"capture_legacy_baseline",
				return_value={"content_fingerprint": "abc"},
			) as capture,
		):
			result = app.capture_legacy_baseline("RUN-1")
		require_read.assert_called_once_with()
		require_run.assert_called_once_with("RUN-1", run_ptype="read")
		capture.assert_called_once_with("RUN-1")
		self.assertEqual(result["content_fingerprint"], "abc")

	def test_api_comparison_requires_complete_run_and_all_solver_jobs(self):
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(app, "_require_all_scoped_documents_visible") as require_jobs,
			patch.object(
				app.v2_baseline,
				"get_legacy_v2_comparison",
				return_value={"status": "Ready"},
			) as compare,
		):
			result = app.get_legacy_v2_comparison("RUN-1", legacy_fingerprint="legacy")

		require_run.assert_called_once_with("RUN-1", run_ptype="read")
		require_jobs.assert_called_once_with("APS Solver Job", {"planning_run": "RUN-1"})
		compare.assert_called_once_with("RUN-1", legacy_fingerprint="legacy")
		self.assertEqual(result["status"], "Ready")

	def test_settings_schema_keeps_all_v2_controls_hidden_and_disabled(self):
		path = (
			Path(__file__).resolve().parents[1]
			/ "injection_aps"
			/ "doctype"
			/ "aps_settings"
			/ "aps_settings.json"
		)
		data = json.loads(path.read_text(encoding="utf-8"))
		fields = {row["fieldname"]: row for row in data["fields"]}
		for fieldname in v2_flags.BOOLEAN_FLAGS:
			self.assertEqual(fields[fieldname]["default"], "0")
			self.assertEqual(fields[fieldname]["hidden"], 1)
		self.assertEqual(fields["solver_engine"]["default"], "Legacy")
		self.assertEqual(fields["solver_engine"]["hidden"], 1)

	def test_all_required_phase0_scenarios_are_catalogued_without_mutation(self):
		manifest = get_scenario_manifest()
		self.assertEqual(manifest["scenario_count"], 11)
		self.assertFalse(manifest["mutation_enabled"])
		self.assertTrue(manifest["fixture_prefix"].startswith("APS-V2-FIXTURE-"))
		for scenario in manifest["scenarios"].values():
			self.assertTrue(scenario["demands"])
			self.assertTrue(scenario["expected"])

	def test_fixture_and_solver_gates_are_pinned_to_isolated_boundaries(self):
		self.assertEqual(v2_fixture_builder.FIXTURE_SITE, "aps-opt-fixture.localhost")
		self.assertTrue(v2_fixture_builder.FIXTURE_CUSTOMER.startswith("APS-V2-FIXTURE-"))
		self.assertEqual(v2_phase0_gate.SELECTED_ORTOOLS_VERSION, "9.4.1874")
		self.assertEqual(v2_phase0_gate.REQUIRED_PROTOBUF_VERSION, "3.20.3")


if __name__ == "__main__":
	unittest.main()
