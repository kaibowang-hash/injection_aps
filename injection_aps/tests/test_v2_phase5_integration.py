from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, getdate

from injection_aps.services import shift_replan
from injection_aps.tests.v2_phase0_gate import assert_isolated_environment


class TestPhase5ShiftReplanIntegration(FrappeTestCase):
	def setUp(self):
		assert_isolated_environment(require_fixture=True)
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		self.original_settings = {
			fieldname: frappe.db.get_single_value("APS Settings", fieldname)
			for fieldname in ("enable_aps_v2", "solver_engine", "enable_shift_replan")
		}
		for fieldname, value in {"enable_aps_v2": 1, "solver_engine": "CP-SAT", "enable_shift_replan": 1}.items():
			frappe.db.set_single_value("APS Settings", fieldname, value)
		frappe.clear_cache(doctype="APS Settings")
		self.company = frappe.db.get_value("Company", {})
		self.plant_floor = frappe.db.get_value("Plant Floor", {"company": self.company})
		self.item_code = frappe.db.get_value("Item", {"name": ("like", "APS-V2-FIXTURE-%")})
		self.workstation = frappe.db.get_value("Workstation", {"name": ("like", "APS-V2-FIXTURE-%")})
		if not all((self.company, self.plant_floor, self.item_code, self.workstation)):
			self.skipTest("Phase 5 integration requires isolated APS fixture masters.")
		self.run = self._create_run()
		self.result, self.segment = self._create_result()

	def tearDown(self):
		if hasattr(self, "original_settings"):
			for fieldname, value in self.original_settings.items():
				frappe.db.set_single_value("APS Settings", fieldname, value)
			frappe.clear_cache(doctype="APS Settings")

	def test_original_current_forecast_and_reviewed_apply_are_separate(self):
		cycle = shift_replan.create_replan_cycle(
			self.run.name,
			shift_date="2026-08-14",
			shift_type="Day Shift",
			execution_cutoff="2026-08-14 06:00:00",
		)
		self.assertEqual(cycle["status"], "Proposal Ready")
		diff = next(row for row in cycle["diffs"] if row["segment"] == self.segment)
		self.assertEqual(get_datetime(diff["current_start_time"]), get_datetime("2026-08-14 07:00:00"))
		self.assertEqual(get_datetime(diff["proposed_start_time"]), get_datetime("2026-08-14 08:00:00"))
		stored = frappe.db.get_value(
			"APS Schedule Segment", self.segment,
			["start_time", "baseline_start_time", "current_start_time", "forecast_start_time", "replan_cycle"],
			as_dict=True,
		)
		self.assertEqual(get_datetime(stored.start_time), get_datetime("2026-08-14 07:00:00"))
		self.assertEqual(get_datetime(stored.current_start_time), get_datetime("2026-08-14 07:00:00"))
		self.assertTrue(stored.forecast_start_time)
		self.assertEqual(stored.replan_cycle, cycle["name"])

		wo_batch = self._create_wo_batch()
		shift_batch = self._create_shift_batch(cycle["name"], wo_batch)
		with (
			patch.object(shift_replan, "_require_roles"),
			patch("injection_aps.services.planning.generate_shift_schedule_proposals", return_value={"shift_schedule_proposal_batch": shift_batch.name, "proposal_count": 0}),
		):
			generated = shift_replan.generate_replan_proposals(cycle["name"], expected_fingerprint=cycle["solution_fingerprint"])
			approved = shift_replan.approve_replan_cycle(cycle["name"], reason="Reviewed no-risk next-shift movement.", expected_fingerprint=cycle["solution_fingerprint"])
			applied = shift_replan.apply_replan_cycle(cycle["name"], expected_fingerprint=cycle["solution_fingerprint"])

		self.assertEqual(generated["shift_schedule_proposal_batch"], shift_batch.name)
		self.assertEqual(approved["status"], "Approved")
		self.assertEqual(applied["status"], "Applied")
		updated = frappe.db.get_value(
			"APS Schedule Segment", self.segment,
			["baseline_start_time", "current_start_time", "start_time", "replan_cycle"],
			as_dict=True,
		)
		self.assertEqual(get_datetime(updated.baseline_start_time), get_datetime("2026-08-14 07:00:00"))
		self.assertEqual(get_datetime(updated.current_start_time), get_datetime("2026-08-14 08:00:00"))
		self.assertEqual(get_datetime(updated.start_time), get_datetime("2026-08-14 08:00:00"))
		self.assertEqual(updated.replan_cycle, cycle["name"])

	def test_system_manager_cannot_replace_gmc_business_approval(self):
		with patch.object(shift_replan.frappe, "get_roles", return_value=["System Manager"]):
			with self.assertRaises(frappe.PermissionError):
				shift_replan._require_roles(shift_replan.APPROVAL_ROLES)

	def test_two_isolated_rolling_cycles_are_idempotent_and_never_auto_apply(self):
		first = shift_replan.create_replan_cycle(
			self.run.name,
			shift_date="2026-08-14",
			shift_type="Day Shift",
			execution_cutoff="2026-08-14 06:00:00",
		)
		first_replay = shift_replan.create_replan_cycle(
			self.run.name,
			shift_date="2026-08-14",
			shift_type="Day Shift",
			execution_cutoff="2026-08-14 06:00:00",
		)
		second = shift_replan.create_replan_cycle(
			self.run.name,
			shift_date="2026-08-14",
			shift_type="Night Shift",
			execution_cutoff="2026-08-14 18:00:00",
		)
		self.assertEqual(first["name"], first_replay["name"])
		self.assertNotEqual(first["name"], second["name"])
		self.assertEqual(
			frappe.db.count("APS Replan Cycle", {"baseline_run": self.run.name}),
			2,
		)
		self.assertNotIn(first["status"], {"Approved", "Applied"})
		self.assertNotIn(second["status"], {"Approved", "Applied"})
		stored = frappe.db.get_value(
			"APS Schedule Segment",
			self.segment,
			["baseline_start_time", "current_start_time", "start_time"],
			as_dict=True,
		)
		self.assertEqual(get_datetime(stored.baseline_start_time), get_datetime("2026-08-14 07:00:00"))
		self.assertEqual(get_datetime(stored.current_start_time), get_datetime("2026-08-14 07:00:00"))
		self.assertEqual(get_datetime(stored.start_time), get_datetime("2026-08-14 07:00:00"))

	def _create_run(self):
		return frappe.get_doc({
			"doctype": "APS Planning Run", "company": self.company, "plant_floor": self.plant_floor,
			"planning_date": "2026-08-14", "horizon_days": 14,
			"horizon_start": "2026-08-14 00:00:00", "horizon_end": "2026-08-28 00:00:00",
			"run_type": "Formal", "existing_work_order_policy": "Exclude",
			"status": "Applied", "approval_state": "Approved",
			"notes": f"APS V2 Phase 5 integration {frappe.generate_hash(length=8)}",
		}).insert(ignore_permissions=True)

	def _create_result(self):
		doc = frappe.get_doc({
			"doctype": "APS Schedule Result", "planning_run": self.run.name,
			"company": self.company, "plant_floor": self.plant_floor,
			"item_code": self.item_code, "requested_date": "2026-08-14",
			"demand_source": "Customer Delivery Schedule", "production_strategy": "Auto Balance",
			"planned_qty": 5, "status": "Applied", "risk_status": "Normal",
			"segments": [{
				"workstation": self.workstation, "mould_reference": "APS-V2-P5-MOLD",
				"start_time": "2026-08-14 07:00:00", "end_time": "2026-08-14 08:00:00",
				"baseline_start_time": "2026-08-14 07:00:00", "baseline_end_time": "2026-08-14 08:00:00",
				"current_start_time": "2026-08-14 07:00:00", "current_end_time": "2026-08-14 08:00:00",
				"planned_qty": 5, "segment_kind": "Primary", "segment_status": "Applied",
			}],
		})
		doc.flags.aps_result_engine_transition = True
		doc.insert(ignore_permissions=True)
		return doc, doc.segments[0].name

	def _create_wo_batch(self):
		doc = frappe.get_doc({
			"doctype": "APS Work Order Proposal Batch", "planning_run": self.run.name,
			"company": self.company, "plant_floor": self.plant_floor,
			"proposal_date": getdate("2026-08-14"), "proposal_fingerprint": frappe.generate_hash(length=32),
			"status": "Applied", "approval_state": "Approved", "proposal_count": 0, "applied_count": 0,
		})
		doc.flags.proposal_engine_transition = True
		doc.insert(ignore_permissions=True)
		return doc

	def _create_shift_batch(self, cycle_name, wo_batch):
		doc = frappe.get_doc({
			"doctype": "APS Shift Schedule Proposal Batch", "planning_run": self.run.name,
			"company": self.company, "plant_floor": self.plant_floor,
			"work_order_proposal_batch": wo_batch.name, "replan_cycle": cycle_name,
			"proposal_date": getdate("2026-08-14"), "proposal_fingerprint": frappe.generate_hash(length=32),
			"status": "Ready For Review", "approval_state": "Pending", "proposal_count": 0, "applied_count": 0,
		})
		doc.flags.proposal_engine_transition = True
		doc.insert(ignore_permissions=True)
		return doc


if __name__ == "__main__":
	import unittest

	unittest.main()
