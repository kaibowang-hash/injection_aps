from __future__ import annotations

from copy import deepcopy
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, now_datetime

from injection_aps.services import capacity_balance, planning, solver_orchestration, v2_baseline
from injection_aps.tests.v2_phase0_gate import assert_isolated_environment


class TestPhase4SolverIntegration(FrappeTestCase):
	def setUp(self):
		assert_isolated_environment(require_fixture=True)
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		self.original_settings = {
			fieldname: frappe.db.get_single_value("APS Settings", fieldname)
			for fieldname in (
				"enable_aps_v2",
				"solver_engine",
				"enable_coproduct_campaign",
				"enable_multilevel_bom_planning",
			)
		}
		for fieldname, value in {
			"enable_aps_v2": 1,
			"solver_engine": "CP-SAT",
			"enable_coproduct_campaign": 0,
			"enable_multilevel_bom_planning": 0,
		}.items():
			frappe.db.set_single_value("APS Settings", fieldname, value)
		frappe.clear_cache(doctype="APS Settings")
		self.company = frappe.db.get_value("Company", {})
		self.plant_floor = frappe.db.get_value("Plant Floor", {"company": self.company})
		self.item_code = frappe.db.get_value("Item", {"name": ("like", "APS-V2-FIXTURE-%")})
		self.workstation = frappe.db.get_value("Workstation", {"name": ("like", "APS-V2-FIXTURE-%")})
		if not all((self.company, self.plant_floor, self.item_code, self.workstation)):
			self.skipTest("Phase 4 integration requires the isolated APS V2 fixture masters.")
		self.run = self._create_run(run_type="Formal")
		self.result = self._create_result(self.run)
		self.source = self._solver_source(self.run, self.result)

	def tearDown(self):
		if hasattr(self, "original_settings"):
			for fieldname, value in self.original_settings.items():
				frappe.db.set_single_value("APS Settings", fieldname, value)
			frappe.clear_cache(doctype="APS Settings")

	def test_job_projection_idempotency_and_apply_are_persisted(self):
		with patch.object(solver_orchestration, "_build_normalized_source", return_value=deepcopy(self.source)):
			analyzed = solver_orchestration.analyze_v2_schedule(self.run.name, run_in_background=False)
			second = solver_orchestration.analyze_v2_schedule(self.run.name, run_in_background=False)

		self.assertEqual(analyzed["name"], second["name"])
		self.assertIn(analyzed["status"], {"Optimal", "Feasible"})
		self.assertEqual(frappe.db.count("APS Solver Job", {"planning_run": self.run.name}), 1)
		self.assertEqual(frappe.db.get_value("APS Planning Run", self.run.name, "capacity_balance_status"), "Ready")
		self.assertEqual(frappe.db.get_value("APS Schedule Result", self.result.name, "on_time_qty"), 5)

		with patch.object(solver_orchestration, "_build_normalized_source", return_value=deepcopy(self.source)):
			applied = solver_orchestration.apply_v2_schedule(
				self.run.name,
				expected_fingerprint=analyzed["input_fingerprint"],
			)

		self.assertEqual(applied["status"], "Applied")
		self.assertEqual(frappe.db.get_value("APS Solver Job", analyzed["name"], "status"), "Applied")
		analysis = json.loads(
			frappe.db.get_value(
				"APS Planning Run",
				self.run.name,
				"capacity_balance_analysis_json",
			)
		)
		self.assertTrue(analysis.get("applied_plan_fingerprint"))
		self.assertTrue(analysis.get("applied_resource_fingerprint"))
		self.assertEqual(
			capacity_balance.assert_applied_capacity_current(self.run.name)["solution_fingerprint"],
			analyzed["solution_fingerprint"],
		)
		segment = frappe.db.get_value(
			"APS Schedule Segment",
			{"parent": self.result.name, "solver_task_key": ("is", "set")},
			["planned_qty", "workstation", "solver_scenario", "current_start_time", "current_end_time"],
			as_dict=True,
		)
		self.assertEqual(segment.planned_qty, 5)
		self.assertEqual(segment.workstation, self.workstation)
		self.assertEqual(segment.solver_scenario, "recommended")
		self.assertTrue(segment.current_start_time)
		self.assertTrue(segment.current_end_time)
		segment_count_before_replay = frappe.db.count(
			"APS Schedule Segment", {"parent": self.result.name}
		)
		replay = solver_orchestration.apply_v2_schedule(
			self.run.name,
			expected_fingerprint=analyzed["input_fingerprint"],
		)
		self.assertEqual(replay["idempotent_replay"], 1)
		self.assertEqual(replay["created_segments"], 0)
		self.assertEqual(
			frappe.db.count("APS Schedule Segment", {"parent": self.result.name}),
			segment_count_before_replay,
		)
		with self.assertRaisesRegex(frappe.ValidationError, "selection is locked after Apply"):
			solver_orchestration.select_solver_scenario(
				self.run.name,
				"recommended",
				reason=None,
				expected_fingerprint=analyzed["input_fingerprint"],
			)
		with (
			patch.object(
				planning,
				"validate_run_mold_readiness",
				return_value={"rows": [], "blocking_count": 0},
			),
			patch.object(
				planning,
				"_validate_run_segment_overlaps",
				return_value={"count": 0, "messages": []},
			),
			patch.object(
				planning,
				"_validate_run_mold_overlaps",
				return_value={"count": 0, "messages": []},
			),
		):
			approved = planning.approve_planning_run(self.run.name)
		self.assertEqual(approved["status"], "Approved")
		self.assertTrue(approved["capacity_evidence"]["applied_plan_fingerprint"])
		self.assertTrue(approved["capacity_evidence"]["applied_resource_fingerprint"])
		self.assertEqual(
			capacity_balance.assert_applied_capacity_current(self.run.name)["solution_fingerprint"],
			analyzed["solution_fingerprint"],
		)

	def test_apply_rejects_changed_input_fingerprint(self):
		with patch.object(solver_orchestration, "_build_normalized_source", return_value=deepcopy(self.source)):
			analyzed = solver_orchestration.analyze_v2_schedule(self.run.name, run_in_background=False)
		changed = deepcopy(self.source)
		changed["demands"][0]["quantity"] = 6
		with patch.object(solver_orchestration, "_build_normalized_source", return_value=changed):
			with self.assertRaises(frappe.ValidationError):
				solver_orchestration.apply_v2_schedule(
					self.run.name,
					expected_fingerprint=analyzed["input_fingerprint"],
				)

	def test_trial_analysis_captures_legacy_comparison_and_cannot_apply(self):
		trial_run = self._create_run(run_type="Trial")
		trial_result = self._create_result(trial_run)
		trial_source = self._solver_source(trial_run, trial_result)
		segments_before = frappe.db.count("APS Schedule Segment", {"parent": trial_result.name})
		with patch.object(solver_orchestration, "_build_normalized_source", return_value=deepcopy(trial_source)):
			analyzed = solver_orchestration.analyze_v2_schedule(trial_run.name, run_in_background=False)
		comparison = v2_baseline.get_legacy_v2_comparison(trial_run.name)
		self.assertEqual(comparison["status"], "Ready")
		self.assertTrue(comparison["read_only"])
		self.assertFalse(comparison["apply_allowed"])
		self.assertFalse(comparison["formal_v2_writes_enabled"])
		self.assertEqual(comparison["legacy"]["engine"], "Legacy")
		self.assertEqual(comparison["v2"]["scenario"], "recommended")
		self.assertEqual(comparison["v2"]["metrics"]["on_time_qty"], 5)
		self.assertEqual(
			frappe.db.count("APS Schedule Segment", {"parent": trial_result.name}),
			segments_before,
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Trial run is read-only"):
			solver_orchestration.apply_v2_schedule(
				trial_run.name,
				expected_fingerprint=analyzed["input_fingerprint"],
			)

	def _create_run(self, *, run_type):
		start = get_datetime(now_datetime()).replace(second=0, microsecond=0)
		return frappe.get_doc({
			"doctype": "APS Planning Run",
			"company": self.company,
			"plant_floor": self.plant_floor,
			"planning_date": getdate(start),
			"horizon_days": 14,
			"horizon_start": start,
			"horizon_end": add_days(start, 14),
			"run_type": run_type,
			"existing_work_order_policy": "Exclude",
			"status": "Draft",
			"approval_state": "Pending",
			"notes": f"APS V2 Phase 4 integration {frappe.generate_hash(length=8)}",
		}).insert(ignore_permissions=True)

	def _create_result(self, run):
		doc = frappe.get_doc({
			"doctype": "APS Schedule Result",
			"planning_run": run.name,
			"company": self.company,
			"plant_floor": self.plant_floor,
			"item_code": self.item_code,
			"requested_date": "2026-08-14",
			"demand_source": "Forecast",
			"production_strategy": "Auto Balance",
			"planned_qty": 5,
			"fulfillment_baseline_json": json.dumps({
				"version": 3,
				"net_requirement": {
					"demand_qty": 5,
					"available_stock_qty": 0,
					"open_work_order_qty": 0,
					"existing_work_order_policy": "Exclude",
				},
			}),
			"status": "Draft",
			"risk_status": "Normal",
		})
		doc.flags.aps_result_engine_transition = True
		doc.insert(ignore_permissions=True)
		return doc

	def _solver_source(self, run, result):
		return {
			"run_key": run.name,
			"horizon_start": "2026-08-14T08:00:00",
			"horizon_end": "2026-08-15T08:00:00",
			"quantity_scale": 1000,
			"time_limit_seconds": 9,
			"random_seed": 20260814,
			"demands": [{
				"key": result.name,
				"result": result.name,
				"commitment": "",
				"item_code": self.item_code,
				"admission_class": "P0",
				"quantity": 5,
				"due_time": "2026-08-14T20:00:00",
				"earliest_time": "2026-08-14T08:00:00",
				"service_priority": 100,
				"alternatives": [{
					"key": f"{self.workstation}|APS-V2-TEST-MOLD",
					"machine": self.workstation,
					"mold": "APS-V2-TEST-MOLD",
					"plant_floor": self.plant_floor,
					"output_per_cycle": 1,
					"cycle_minutes": 1,
					"base_setup_minutes": 0,
				}],
			}],
			"buckets": [{
				"key": "SHIFT-1",
				"machine": self.workstation,
				"start": "2026-08-14T08:00:00",
				"end": "2026-08-14T20:00:00",
				"available_minutes": 720,
				"capacity_factor": 1,
				"horizon_zone": "Demand",
			}],
		}


if __name__ == "__main__":
	import unittest

	unittest.main()
