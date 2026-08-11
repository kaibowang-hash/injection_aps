from __future__ import annotations

from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

import frappe

from injection_aps.api import app
from injection_aps.services import planning


class TestManualQuantityAdjustment(TestCase):
	def test_quantity_totals_replace_only_selected_segment(self):
		shrunk = planning._build_manual_quantity_totals(
			result_planned_qty=200,
			current_result_scheduled_qty=150,
			current_segment_qty=100,
			target_qty=60,
		)
		self.assertEqual(shrunk["projected_result_qty"], 110)
		self.assertEqual(shrunk["unscheduled_qty"], 90)
		self.assertEqual(shrunk["overproduction_qty"], 0)

		expanded = planning._build_manual_quantity_totals(
			result_planned_qty=200,
			current_result_scheduled_qty=180,
			current_segment_qty=60,
			target_qty=90,
		)
		self.assertEqual(expanded["projected_result_qty"], 210)
		self.assertEqual(expanded["unscheduled_qty"], 0)
		self.assertEqual(expanded["overproduction_qty"], 10)

	def test_quantity_duration_uses_capacity_and_minimum_runtime(self):
		settings = {"default_hourly_capacity_qty": 100}
		self.assertEqual(planning._estimate_run_hours(250, {}, settings), 2.5)
		self.assertEqual(planning._estimate_run_hours(10, {}, settings), 0.25)

	def test_whole_number_uom_rejects_fractional_target(self):
		def get_value(doctype, name, fieldname):
			if doctype == "Item":
				return "Nos"
			if doctype == "UOM":
				return 1
			return None

		with (
			patch("injection_aps.services.planning.frappe.get_precision", return_value=3),
			patch(
				"injection_aps.services.planning.frappe.get_system_settings",
				return_value="Banker's Rounding (legacy)",
			),
			patch("injection_aps.services.planning.frappe.db.get_value", side_effect=get_value),
		):
			with self.assertRaises(frappe.ValidationError):
				planning._normalize_manual_target_qty("ITEM-1", 1.5)
			self.assertEqual(planning._normalize_manual_target_qty("ITEM-1", 2), 2)

	def test_non_positive_target_is_rejected(self):
		with patch("injection_aps.services.planning.frappe.get_precision", return_value=2):
			with self.assertRaises(frappe.ValidationError):
				planning._normalize_manual_target_qty("ITEM-1", 0)
			with self.assertRaises(frappe.ValidationError):
				planning._normalize_manual_target_qty("ITEM-1", -1)

	def test_fractional_uom_uses_segment_field_precision(self):
		def get_value(doctype, name, fieldname):
			return "Kg" if doctype == "Item" else 0

		with (
			patch("injection_aps.services.planning.frappe.get_precision", return_value=2),
			patch(
				"injection_aps.services.planning.frappe.get_system_settings",
				return_value="Banker's Rounding (legacy)",
			),
			patch("injection_aps.services.planning.frappe.db.get_value", side_effect=get_value),
		):
			self.assertEqual(planning._normalize_manual_target_qty("ITEM-1", 1.236), 1.24)

	def test_max_conflict_free_qty_respects_next_conflict_and_minimum_runtime(self):
		start = datetime(2026, 8, 11, 8, 0, 0)
		with (
			patch("injection_aps.services.planning.frappe.get_precision", return_value=2),
			patch("injection_aps.services.planning._item_quantity_requires_integer", return_value=False),
		):
			self.assertEqual(
				planning._max_conflict_free_qty(start, 50, [start + timedelta(hours=2)], "ITEM-1"),
				100,
			)
			self.assertEqual(
				planning._max_conflict_free_qty(start, 50, [start + timedelta(minutes=10)], "ITEM-1"),
				0,
			)

	def test_quantity_and_end_time_are_mutually_exclusive_before_lookup(self):
		with patch("injection_aps.services.planning.frappe.get_all") as get_all:
			with self.assertRaises(frappe.ValidationError):
				planning.preview_manual_schedule_adjustment(
					segment_name="SEG-1",
					target_qty=100,
					target_end_time="2026-08-11 12:00:00",
				)
		get_all.assert_not_called()

	def test_overproduction_requires_explicit_confirmation_and_reason(self):
		preview = {"quantity_mode": 1, "overproduction_qty": 10}
		with self.assertRaises(frappe.ValidationError):
			planning._validate_manual_overproduction_confirmation(preview)
		with self.assertRaises(frappe.ValidationError):
			planning._validate_manual_overproduction_confirmation(preview, allow_overproduction=1)
		planning._validate_manual_overproduction_confirmation(
			preview,
			allow_overproduction=1,
			manual_note="Customer-approved extra production",
		)
		planning._validate_manual_overproduction_confirmation(
			{"quantity_mode": 0, "overproduction_qty": 10},
		)

	def test_released_and_started_segments_are_execution_protected(self):
		self.assertTrue(planning._is_segment_execution_protected({"linked_work_order": "WO-1"}))
		self.assertTrue(planning._is_segment_execution_protected({"actual_start_time": "2026-08-11 08:00:00"}))
		self.assertTrue(planning._is_segment_execution_protected({"actual_status": "Running"}))
		self.assertFalse(planning._is_segment_execution_protected({"segment_status": "Planned"}))

	def test_apply_rejects_unconfirmed_overproduction_before_writes(self):
		preview = {
			"allowed": 1,
			"quantity_mode": 1,
			"overproduction_qty": 10,
		}
		with (
			patch("injection_aps.services.planning.preview_manual_schedule_adjustment", return_value=preview),
			patch("injection_aps.services.planning.frappe.get_all") as get_all,
		):
			with self.assertRaises(frappe.ValidationError):
				planning.apply_manual_schedule_adjustment(segment_name="SEG-1", target_qty=100)
		self.assertNotIn(
			"APS Schedule Segment",
			[call.args[0] for call in get_all.call_args_list if call.args],
		)

	def test_public_apis_forward_quantity_arguments(self):
		with (
			patch("injection_aps.api.app._require_plan_access"),
			patch("injection_aps.api.app._require_release_access"),
			patch("injection_aps.api.app._require_scoped_document_access"),
			patch("injection_aps.api.app._require_document_access"),
			patch("injection_aps.api.app._lock_planning_run_scope"),
			patch("injection_aps.api.app._require_complete_run_mutation_scope"),
			patch(
				"injection_aps.api.app.frappe.db.get_value",
				side_effect=["RESULT-1", "RUN-1"],
			),
			patch(
				"injection_aps.api.app.planning.preview_manual_schedule_adjustment",
				return_value={},
			) as preview,
			patch(
				"injection_aps.api.app.planning.apply_manual_schedule_adjustment",
				return_value={},
			) as apply,
		):
			app.preview_manual_schedule_adjustment(segment_name="SEG-1", target_qty="12.5")
			app.apply_manual_schedule_adjustment(
				segment_name="SEG-1",
				target_qty="13.5",
				allow_overproduction="1",
				manual_note="Approved",
			)

		self.assertEqual(preview.call_args_list[0].kwargs["target_qty"], 12.5)
		self.assertEqual(preview.call_args_list[0].kwargs["allow_overproduction"], 0)
		self.assertEqual(preview.call_args_list[1].kwargs["target_qty"], 13.5)
		self.assertEqual(preview.call_args_list[1].kwargs["allow_overproduction"], 1)
		self.assertEqual(apply.call_args.kwargs["target_qty"], 13.5)
		self.assertEqual(apply.call_args.kwargs["allow_overproduction"], 1)
		self.assertEqual(apply.call_args.kwargs["manual_note"], "Approved")
