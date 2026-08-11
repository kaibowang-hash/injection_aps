from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from injection_aps.services import consistency, planning


class TestPlanConsistency(TestCase):
	def test_canonical_quantity_formulas_for_shortage_and_overproduction(self):
		shortage = consistency.calculate_quantity_fields(100, 70)
		self.assertEqual(
			shortage,
			{
				"planned_qty": 100.0,
				"machine_scheduled_qty": 70.0,
				"demand_covered_qty": 70.0,
				"overproduction_qty": 0,
				"unscheduled_qty": 30.0,
			},
		)

		overproduction = consistency.calculate_quantity_fields(100, 130)
		self.assertEqual(overproduction["demand_covered_qty"], 100)
		self.assertEqual(overproduction["overproduction_qty"], 30)
		self.assertEqual(overproduction["unscheduled_qty"], 0)

	def test_only_valid_primary_and_manual_segments_are_effective(self):
		base = {
			"workstation": "MACHINE-1",
			"start_time": "2026-08-11 08:00:00",
			"end_time": "2026-08-11 09:00:00",
			"planned_qty": 10,
			"segment_status": "Planned",
		}
		self.assertTrue(consistency.is_effective_primary_segment({**base, "segment_kind": "Primary"}))
		self.assertTrue(consistency.is_effective_primary_segment({**base, "segment_kind": "Manual"}))
		self.assertFalse(
			consistency.is_effective_primary_segment({**base, "segment_kind": "Family Co-Product"})
		)
		self.assertFalse(consistency.is_effective_primary_segment({**base, "segment_status": "Cancelled"}))
		self.assertFalse(consistency.is_effective_primary_segment({**base, "segment_status": "Blocked"}))
		self.assertFalse(consistency.is_effective_primary_segment({**base, "planned_qty": 0}))
		self.assertFalse(consistency.is_effective_primary_segment({**base, "workstation": None}))

	def test_risk_helpers_keep_the_worst_source(self):
		self.assertEqual(consistency.get_worst_risk("Normal", "Critical", "Attention"), "Critical")
		self.assertEqual(consistency.get_worst_risk("Blocked", "Critical"), "Blocked")
		self.assertEqual(
			consistency.get_exception_risk(
				[
					{"severity": "Warning", "is_blocking": 0},
					{"severity": "Critical", "is_blocking": 0},
				]
			),
			"Critical",
		)

	def test_approval_stops_immediately_when_consistency_gate_fails(self):
		with (
			patch("injection_aps.services.planning.frappe.get_doc", return_value=SimpleNamespace()),
			patch(
				"injection_aps.services.planning.consistency.assert_plan_consistent",
				side_effect=frappe.ValidationError("invalid plan"),
			) as gate,
			patch("injection_aps.services.planning.validate_run_mold_readiness") as mold_gate,
		):
			with self.assertRaises(frappe.ValidationError):
				planning.approve_planning_run("APS-RUN-1")
		gate.assert_called_once_with("APS-RUN-1", reason="planning run approval")
		mold_gate.assert_not_called()

	def test_work_order_proposal_stops_when_consistency_gate_fails(self):
		run_doc = SimpleNamespace(approval_state="Approved")
		with (
			patch("injection_aps.services.planning.frappe.get_doc", return_value=run_doc),
			patch(
				"injection_aps.services.planning.consistency.assert_plan_consistent",
				side_effect=frappe.ValidationError("invalid plan"),
			) as gate,
			patch("injection_aps.services.planning.validate_run_mold_readiness") as mold_gate,
		):
			with self.assertRaises(frappe.ValidationError):
				planning.generate_work_order_proposals("APS-RUN-1")
		gate.assert_called_once_with("APS-RUN-1", reason="work order proposal generation")
		mold_gate.assert_not_called()

	def test_proposal_apply_stops_when_consistency_gate_fails(self):
		batch = SimpleNamespace(planning_run="APS-RUN-1")
		with (
			patch(
				"injection_aps.services.planning.frappe.get_doc",
				side_effect=[batch, SimpleNamespace(name="APS-RUN-1")],
			),
			patch(
				"injection_aps.services.planning.consistency.assert_plan_consistent",
				side_effect=frappe.ValidationError("invalid plan"),
			) as gate,
			patch("injection_aps.services.planning.validate_run_mold_readiness") as mold_gate,
		):
			with self.assertRaises(frappe.ValidationError):
				planning.apply_work_order_proposals("APS-WOP-1")
		gate.assert_called_once_with("APS-RUN-1", reason="work order proposal apply")
		mold_gate.assert_not_called()

	def test_shift_generation_stops_when_consistency_gate_fails(self):
		context = {"run_doc": SimpleNamespace(name="APS-RUN-1")}
		with (
			patch(
				"injection_aps.services.planning._build_shift_schedule_release_context",
				return_value=context,
			),
			patch(
				"injection_aps.services.planning.consistency.assert_plan_consistent",
				side_effect=frappe.ValidationError("invalid plan"),
			) as gate,
		):
			with self.assertRaises(frappe.ValidationError):
				planning.generate_shift_schedule_proposals("APS-RUN-1")
		gate.assert_called_once_with("APS-RUN-1", reason="shift schedule proposal generation")

	def test_formal_shift_apply_stops_when_consistency_gate_fails(self):
		batch = SimpleNamespace(planning_run="APS-RUN-1")
		with (
			patch("injection_aps.services.planning.frappe.get_doc", return_value=batch),
			patch(
				"injection_aps.services.planning.consistency.assert_plan_consistent",
				side_effect=frappe.ValidationError("invalid plan"),
			) as gate,
			patch("injection_aps.services.planning._validate_run_segment_overlaps") as overlap_gate,
		):
			with self.assertRaises(frappe.ValidationError):
				planning.apply_shift_schedule_proposals("APS-SSP-1")
		gate.assert_called_once_with("APS-RUN-1", reason="formal shift schedule release")
		overlap_gate.assert_not_called()
