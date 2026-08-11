from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, today

from injection_aps.api import app
from injection_aps.services import change_engine, consistency, planning


class TestChangeEngineCalculations(TestCase):
	def test_retained_floor_uses_exact_maximum_and_reduces_only_unprotected_segments(self):
		start = datetime(2026, 8, 11, 8, 0, 0)
		segments = [
			self._segment("LOCKED", 40, start, is_locked=1, segment_status="Approved"),
			self._segment("OPEN", 60, start + timedelta(hours=1)),
		]
		result = SimpleNamespace(produced_qty=35, delivered_qty=20)
		protection = change_engine._calculate_quantity_protection(result, segments, target_qty=0)

		self.assertEqual(protection["minimum_retained_qty"], 40)
		self.assertEqual(protection["cancellable_qty"], 60)
		self.assertEqual(protection["retained_excess_qty"], 40)
		actions = change_engine._build_segment_reduction_actions(segments, qty_to_remove=60)
		self.assertEqual(len(actions), 1)
		self.assertEqual(actions[0]["segment_name"], "OPEN")
		self.assertEqual(actions[0]["action"], "Cancel")
		self.assertEqual(actions[0]["after_qty"], 0)

	def test_reduced_capacity_helpers_weight_available_time_and_completion(self):
		start = datetime(2026, 8, 11, 8, 0, 0)
		windows = [
			{
				"start_time": start,
				"end_time": start + timedelta(hours=2),
				"available_capacity_percent": 50,
			}
		]
		self.assertEqual(
			planning._available_run_hours_between(start, start + timedelta(hours=2), windows),
			1,
		)
		self.assertEqual(
			planning._estimate_end_for_qty_around_downtime(
				start_time=start,
				qty=100,
				hourly_capacity_qty=100,
				downtime_windows=windows,
			),
			start + timedelta(hours=2),
		)
		chunks, remaining = planning._allocate_qty_around_downtime(
			start_time=start,
			qty=100,
			hourly_capacity_qty=100,
			horizon_end=start + timedelta(hours=2),
			downtime_windows=windows,
		)
		self.assertEqual(remaining, 0)
		self.assertEqual(sum(row["planned_qty"] for row in chunks), 100)

	def test_family_coproduct_does_not_inflate_change_machine_quantity(self):
		start = datetime(2026, 8, 11, 8, 0, 0)
		primary = self._segment("PRIMARY", 100, start, segment_kind="Primary")
		coproduct = self._segment("COPRODUCT", 50, start, segment_kind="Family Co-Product")
		result = SimpleNamespace(
			name="RESULT-1",
			net_requirement="NET-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			plant_floor="FLOOR-1",
			requested_date=start.date(),
			planned_qty=0,
			produced_qty=0,
			delivered_qty=0,
		)
		doc = frappe._dict(
			target_result=result.name,
			target_planned_qty=100,
			qty=100,
			change_type="Increase Qty",
		)
		with (
			patch.object(change_engine, "_target_result_context", return_value=(result, [])),
			patch.object(
				change_engine,
				"_build_append_capacity_proposal",
				return_value={"segments": [primary, coproduct], "exceptions": []},
			),
		):
			analysis = change_engine._analyze_increase(doc, {})

		self.assertEqual(analysis["proposal"]["projected_machine_scheduled_qty"], 100)
		self.assertEqual(analysis["impact"]["scheduled_addition_qty"], 100)
		self.assertEqual(analysis["impact"]["unscheduled_addition_qty"], 0)
		self.assertEqual(
			change_engine._prepare_new_change_segment(primary, "test")["segment_kind"],
			"Manual",
		)
		self.assertEqual(
			change_engine._prepare_new_change_segment(coproduct, "test")["segment_kind"],
			"Family Co-Product",
		)

	def test_urgent_insertion_skips_frozen_work_and_cascades_open_segments(self):
		start = datetime(2026, 8, 11, 8, 0, 0)
		rows = [
			self._segment("OPEN-1", 100, start),
			self._segment("FROZEN", 100, start + timedelta(hours=2), is_locked=1),
			self._segment("OPEN-2", 100, start + timedelta(hours=3)),
		]
		actions = change_engine._build_cascade_actions(
			insert_start=start + timedelta(minutes=30),
			insert_end=start + timedelta(hours=1, minutes=30),
			workstation="MACHINE-1",
			schedule_rows=rows,
			downtime_windows=[],
		)

		self.assertEqual([row["segment_name"] for row in actions], ["OPEN-1", "OPEN-2"])
		self.assertEqual(actions[0]["after_start_time"], start + timedelta(hours=3))
		self.assertEqual(actions[0]["after_end_time"], start + timedelta(hours=4))
		self.assertEqual(actions[1]["after_start_time"], start + timedelta(hours=4))
		self.assertEqual(actions[1]["after_end_time"], start + timedelta(hours=5))

	def test_all_seven_analysis_types_have_separate_dispatch(self):
		mapping = {
			"Increase Qty": "_analyze_increase",
			"Decrease Qty": "_analyze_decrease_or_cancel",
			"Cancel": "_analyze_decrease_or_cancel",
			"Pull In": "_analyze_date_change",
			"Push Out": "_analyze_date_change",
			"Urgent Order": "_analyze_urgent_order",
			"Machine Exception": "_analyze_machine_exception",
		}
		for change_type, function_name in mapping.items():
			with patch.object(change_engine, function_name, return_value={"proposal": {}, "impact": {}}) as handler:
				change_engine._dispatch_analysis(SimpleNamespace(change_type=change_type), {})
				handler.assert_called_once()

	def test_cancel_dispatch_never_calls_insert_order_analyzer(self):
		with (
			patch.object(change_engine, "_analyze_decrease_or_cancel", return_value={"proposal": {}, "impact": {}}),
			patch("injection_aps.services.planning.analyze_insert_order_impact") as insert_analyzer,
		):
			change_engine._dispatch_analysis(SimpleNamespace(change_type="Cancel"), {})
		insert_analyzer.assert_not_called()

	def test_source_demand_delta_must_match_change_type_and_target(self):
		doc = frappe._dict(
			{
				"source_demand_delta": "DELTA-1",
				"change_type": "Cancel",
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"item_code": "ITEM-1",
				"target_result": "RESULT-1",
			}
		)
		added_delta = frappe._dict(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="ITEM-1",
			change_type="Added",
		)
		with patch.object(frappe.db, "get_value", return_value=added_delta):
			with self.assertRaisesRegex(frappe.ValidationError, "requires change type Urgent Order"):
				change_engine._validate_source_demand_delta(doc)

		cancelled_delta = frappe._dict(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="ITEM-1",
			change_type="Cancelled",
		)
		wrong_target = frappe._dict(company="COMPANY-1", customer="CUSTOMER-1", item_code="ITEM-2")
		with patch.object(frappe.db, "get_value", side_effect=[cancelled_delta, wrong_target]):
			with self.assertRaisesRegex(frappe.ValidationError, "does not match target result"):
				change_engine._validate_source_demand_delta(doc)

	def test_public_workflow_apis_enforce_separate_permission_checks(self):
		with (
			patch("injection_aps.api.app._require_plan_access") as plan_access,
			patch("injection_aps.api.app._require_approve_access") as approve_access,
			patch("injection_aps.api.app.planning.confirm_change_request", return_value={}),
			patch("injection_aps.api.app.planning.approve_change_request", return_value={}),
			patch("injection_aps.api.app.planning.apply_change_request", return_value={}),
		):
			app.confirm_change_request("CR-1")
			app.approve_change_request("CR-1")
			app.apply_change_request("CR-1")
		plan_access.assert_called_once()
		self.assertEqual(approve_access.call_count, 2)

	@staticmethod
	def _segment(name, qty, start, **overrides):
		return {
			"name": name,
			"parent": "RESULT-1",
			"workstation": "MACHINE-1",
			"start_time": start,
			"end_time": start + timedelta(hours=1),
			"planned_qty": qty,
			"segment_kind": "Primary",
			"segment_status": "Planned",
			**overrides,
		}


class TestChangeEngineTransactions(FrappeTestCase):
	def setUp(self):
		required_doctypes = [
			"APS Change Application Log",
			"APS Change Request",
			"APS Planning Run",
			"APS Schedule Result",
		]
		if any(not frappe.db.exists("DocType", doctype) for doctype in required_doctypes):
			self.skipTest("Phase 3 DocTypes are not synced.")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		self.item = frappe.db.get_value("Item", {"disabled": 0}) or frappe.db.get_value("Item", {})
		self.workstation = frappe.db.get_value("Workstation", {})
		if not self.company or not self.customer or not self.item or not self.workstation:
			self.skipTest("Change-engine tests need a Company, Customer, Item, and Workstation.")
		self.plant_floor = frappe.db.get_value("Workstation", self.workstation, "plant_floor")
		self.fixture = self._create_cancel_fixture()

	def test_cancel_workflow_applies_real_plan_change_once_with_full_audit(self):
		request = self.fixture["request"]
		with patch("injection_aps.services.planning.analyze_insert_order_impact") as insert_analyzer:
			analysis = change_engine.analyze_change_request(request.name)
		insert_analyzer.assert_not_called()
		self.assertEqual(analysis["status"], "Analyzed")
		self.assertEqual(analysis["quantity_protection"]["minimum_retained_qty"], 40)
		self.assertEqual(analysis["quantity_protection"]["cancellable_qty"], 60)

		with self.assertRaises(frappe.ValidationError):
			change_engine.approve_change_request(request.name)
		change_engine.confirm_change_request(request.name)
		change_engine.approve_change_request(request.name)
		applied = change_engine.apply_change_request(request.name)

		result = frappe.db.get_value(
			"APS Schedule Result",
			self.fixture["result"].name,
			["planned_qty", "machine_scheduled_qty", "overproduction_qty", "unscheduled_qty"],
			as_dict=True,
		)
		self.assertEqual(result.planned_qty, 0)
		self.assertEqual(result.machine_scheduled_qty, 40)
		self.assertEqual(result.overproduction_qty, 40)
		self.assertEqual(result.unscheduled_qty, 0)
		self.assertEqual(
			frappe.db.get_value("APS Schedule Segment", self.fixture["locked_segment"], "planned_qty"),
			40,
		)
		open_segment = frappe.db.get_value(
			"APS Schedule Segment",
			self.fixture["open_segment"],
			["planned_qty", "segment_status"],
			as_dict=True,
		)
		self.assertEqual(open_segment.planned_qty, 0)
		self.assertEqual(open_segment.segment_status, "Cancelled")
		self.assertTrue(applied["consistency"]["valid"])

		request_row = frappe.db.get_value(
			"APS Change Request",
			request.name,
			["status", "apply_count", "applied_by", "application_log", "before_snapshot_json", "after_snapshot_json"],
			as_dict=True,
		)
		self.assertEqual(request_row.status, "Applied")
		self.assertEqual(request_row.apply_count, 1)
		self.assertEqual(request_row.applied_by, frappe.session.user)
		self.assertTrue(request_row.before_snapshot_json)
		self.assertTrue(request_row.after_snapshot_json)
		self.assertEqual(
			frappe.db.count("APS Change Application Log", {"change_request": request.name}),
			1,
		)
		log_row = frappe.db.get_value(
			"APS Change Application Log",
			request_row.application_log,
			[
				"analyzed_by",
				"pmc_confirmed_by",
				"approved_by",
				"applied_by",
				"before_snapshot_hash",
				"after_snapshot_hash",
				"application_fingerprint",
			],
			as_dict=True,
		)
		self.assertEqual(log_row.analyzed_by, frappe.session.user)
		self.assertEqual(log_row.pmc_confirmed_by, frappe.session.user)
		self.assertEqual(log_row.approved_by, frappe.session.user)
		self.assertEqual(log_row.applied_by, frappe.session.user)
		self.assertTrue(log_row.before_snapshot_hash)
		self.assertTrue(log_row.after_snapshot_hash)
		self.assertTrue(log_row.application_fingerprint)
		log_doc = frappe.get_doc("APS Change Application Log", request_row.application_log)
		log_doc.change_type = "Tampered"
		with self.assertRaises(frappe.ValidationError):
			log_doc.save(ignore_permissions=True)

		replay = change_engine.apply_change_request(request.name)
		self.assertEqual(replay["idempotent_replay"], 1)
		self.assertEqual(replay["application_log"], request_row.application_log)
		self.assertEqual(frappe.db.get_value("APS Change Request", request.name, "apply_count"), 1)
		self.assertEqual(
			frappe.db.count("APS Change Application Log", {"change_request": request.name}),
			1,
		)

	def test_apply_failure_rolls_back_plan_request_and_audit(self):
		request = self.fixture["request"]
		change_engine.analyze_change_request(request.name)
		change_engine.confirm_change_request(request.name)
		change_engine.approve_change_request(request.name)
		before_result = frappe.db.get_value("APS Schedule Result", self.fixture["result"].name, "planned_qty")
		before_segment = frappe.db.get_value("APS Schedule Segment", self.fixture["open_segment"], ["planned_qty", "segment_status"], as_dict=True)

		with patch(
			"injection_aps.services.change_engine.consistency.recalculate_plan_consistency",
			side_effect=RuntimeError("forced consistency failure"),
		):
			with self.assertRaisesRegex(RuntimeError, "forced consistency failure"):
				change_engine.apply_change_request(request.name)

		self.assertEqual(frappe.db.get_value("APS Schedule Result", self.fixture["result"].name, "planned_qty"), before_result)
		after_segment = frappe.db.get_value("APS Schedule Segment", self.fixture["open_segment"], ["planned_qty", "segment_status"], as_dict=True)
		self.assertEqual(after_segment, before_segment)
		self.assertEqual(frappe.db.get_value("APS Change Request", request.name, "status"), "Approved")
		self.assertEqual(frappe.db.get_value("APS Change Request", request.name, "apply_count"), 0)
		self.assertEqual(frappe.db.count("APS Change Application Log", {"change_request": request.name}), 0)

	def test_decrease_resizes_only_the_unprotected_remainder(self):
		request = self._configure_request(
			change_type="Decrease Qty",
			qty=30,
			target_planned_qty=70,
		)
		analysis = change_engine.analyze_change_request(request.name)
		self.assertEqual(analysis["proposal"]["segment_actions"][0]["action"], "Resize")
		self.assertEqual(analysis["proposal"]["segment_actions"][0]["after_qty"], 30)
		applied = self._confirm_approve_apply(request.name)
		self.assertTrue(applied["consistency"]["valid"])
		result = frappe.db.get_value(
			"APS Schedule Result",
			self.fixture["result"].name,
			["planned_qty", "machine_scheduled_qty", "overproduction_qty", "unscheduled_qty"],
			as_dict=True,
		)
		self.assertEqual(result.planned_qty, 70)
		self.assertEqual(result.machine_scheduled_qty, 70)
		self.assertEqual(result.overproduction_qty, 0)
		self.assertEqual(result.unscheduled_qty, 0)

	def test_increase_changes_demand_and_exposes_any_capacity_shortage(self):
		request = self._configure_request(
			change_type="Increase Qty",
			qty=20,
			target_planned_qty=120,
		)
		analysis = change_engine.analyze_change_request(request.name)
		self.assertEqual(analysis["proposal"]["quantity_delta"], 20)
		self.assertIn("projected_unscheduled_qty", analysis["proposal"])
		applied = self._confirm_approve_apply(request.name)
		self.assertTrue(applied["consistency"]["valid"])
		result = frappe.db.get_value(
			"APS Schedule Result",
			self.fixture["result"].name,
			["planned_qty", "machine_scheduled_qty", "unscheduled_qty"],
			as_dict=True,
		)
		self.assertEqual(result.planned_qty, 120)
		self.assertGreaterEqual(result.machine_scheduled_qty, 100)
		self.assertEqual(result.unscheduled_qty, max(120 - result.machine_scheduled_qty, 0))

	def test_pull_in_and_push_out_apply_new_due_date_to_result_and_requirement(self):
		for change_type, target_date in (
			("Pull In", getdate(add_days(today(), 2))),
			("Push Out", getdate(add_days(today(), 5))),
		):
			with self.subTest(change_type=change_type):
				fixture = self.fixture if change_type == "Pull In" else self._create_cancel_fixture()
				request = frappe.get_doc("APS Change Request", fixture["request"].name)
				request.change_type = change_type
				request.required_date = target_date
				request.target_planned_qty = 0
				request.save(ignore_permissions=True)
				analysis = change_engine.analyze_change_request(request.name)
				self.assertEqual(getdate(analysis["proposal"]["new_required_date"]), target_date)
				self._confirm_approve_apply(request.name)
				self.assertEqual(
					getdate(frappe.db.get_value("APS Schedule Result", fixture["result"].name, "requested_date")),
					target_date,
				)
				self.assertEqual(
					getdate(frappe.db.get_value("APS Net Requirement", fixture["net_requirement"].name, "demand_date")),
					target_date,
				)

	def test_machine_exception_analyzes_and_applies_capacity_window(self):
		open_segment = frappe.db.get_value(
			"APS Schedule Segment",
			self.fixture["open_segment"],
			["start_time", "end_time"],
			as_dict=True,
		)
		request = self._configure_request(
			change_type="Machine Exception",
			target_result=None,
			item_code=None,
			customer=None,
			workstation=self.workstation,
			exception_start_time=open_segment.start_time,
			exception_end_time=get_datetime(open_segment.start_time) + timedelta(minutes=30),
			machine_exception_mode="Downtime",
			available_capacity_percent=0,
			target_planned_qty=0,
		)
		analysis = change_engine.analyze_change_request(request.name)
		self.assertTrue(analysis["allowed"])
		self.assertEqual(len(analysis["proposal"]["segment_actions"]), 1)
		self.assertGreater(
			get_datetime(analysis["proposal"]["segment_actions"][0]["after_end_time"]),
			get_datetime(open_segment.end_time),
		)
		applied = self._confirm_approve_apply(request.name)
		self.assertTrue(applied["consistency"]["valid"])
		window = frappe.db.get_value(
			"APS Downtime Window",
			{"planning_run": self.fixture["run"].name, "workstation": self.workstation},
			["status", "available_capacity_percent"],
			as_dict=True,
		)
		self.assertEqual(window.status, "Applied")
		self.assertEqual(window.available_capacity_percent, 0)

	def test_urgent_order_full_workflow_creates_demand_requirement_result_and_complete_impact(self):
		open_segment_end = get_datetime(
			frappe.db.get_value("APS Schedule Segment", self.fixture["open_segment"], "end_time")
		)
		urgent_segment = {
			"workstation": self.workstation,
			"plant_floor": self.plant_floor,
			"start_time": open_segment_end,
			"end_time": open_segment_end + timedelta(hours=1),
			"planned_qty": 25,
			"sequence_no": 1,
			"segment_kind": "Primary",
			"segment_status": "Planned",
			"mould_reference": "",
			"is_locked": 0,
			"is_manual": 1,
		}
		impact_row = {
			"affected_order": "New Urgent Order",
			"old_completion_time": None,
			"new_completion_time": urgent_segment["end_time"],
			"delayed_qty": 0,
			"customer": self.customer,
			"due_date": getdate(add_days(today(), 3)),
			"item_code": self.item,
		}
		urgent_analysis = {
			"selected_option": {
				"workstation": self.workstation,
				"plant_floor": self.plant_floor,
				"mould_reference": "",
				"new_segments": [urgent_segment],
				"segment_actions": [],
				"affected_orders": [impact_row],
				"additional_mold_changes": 0,
				"freeze_conflicts": [],
			},
			"machine_options": [
				{
					"selected": 1,
					"workstation": self.workstation,
					"completion_time": urgent_segment["end_time"],
				}
			],
			"freeze_conflicts": [],
			"exceptions": [],
			"overtime_suggestion": "No overtime required.",
			"subcontract_suggestion": "Fallback only.",
		}
		request = self._configure_request(
			change_type="Urgent Order",
			target_result=None,
			item_code=self.item,
			customer=self.customer,
			required_date=getdate(add_days(today(), 3)),
			qty=25,
			target_planned_qty=25,
		)
		with patch.object(change_engine, "_build_urgent_insertion_proposal", return_value=urgent_analysis):
			analysis = change_engine.analyze_change_request(request.name)
		row = analysis["impact"]["affected_orders"][0]
		for fieldname in (
			"affected_order",
			"old_completion_time",
			"new_completion_time",
			"delayed_qty",
			"customer",
			"due_date",
		):
			self.assertIn(fieldname, row)
		self.assertIn("alternate_machine_options", analysis["impact"])
		self.assertIn("overtime_suggestion", analysis["impact"])
		self.assertIn("subcontract_suggestion", analysis["impact"])
		applied = self._confirm_approve_apply(request.name)
		self.assertTrue(applied["consistency"]["valid"])
		mutation = applied["mutation"]
		self.assertTrue(frappe.db.exists("APS Demand Pool", mutation["demand_pool"]))
		self.assertTrue(frappe.db.exists("APS Net Requirement", mutation["net_requirement"]))
		self.assertTrue(frappe.db.exists("APS Schedule Result", mutation["created_result"]))

	def test_stale_plan_blocks_confirmation_and_direct_state_edit_is_rejected(self):
		request = self.fixture["request"]
		change_engine.analyze_change_request(request.name)
		doc = frappe.get_doc("APS Change Request", request.name)
		doc.status = "Approved"
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

		frappe.db.set_value("APS Schedule Segment", self.fixture["open_segment"], "planned_qty", 55)
		with self.assertRaises(frappe.ValidationError):
			change_engine.confirm_change_request(request.name)

	def test_protected_quantity_above_effective_segments_blocks_confirmation(self):
		request = self.fixture["request"]
		frappe.db.set_value("APS Schedule Result", self.fixture["result"].name, "produced_qty", 120)
		analysis = change_engine.analyze_change_request(request.name)
		self.assertFalse(analysis["allowed"])
		self.assertEqual(
			analysis["quantity_protection"]["protection_reconciliation_gap"],
			20,
		)
		with self.assertRaises(frappe.ValidationError):
			change_engine.confirm_change_request(request.name)

	def test_reanalysis_after_stale_pmc_confirmation_resets_both_gates(self):
		request = self.fixture["request"]
		change_engine.analyze_change_request(request.name)
		change_engine.confirm_change_request(request.name)
		frappe.db.set_value("APS Schedule Segment", self.fixture["open_segment"], "planned_qty", 55)
		with self.assertRaises(frappe.ValidationError):
			change_engine.approve_change_request(request.name)
		refreshed = change_engine.analyze_change_request(request.name)
		self.assertEqual(refreshed["status"], "Analyzed")
		row = frappe.db.get_value(
			"APS Change Request",
			request.name,
			["pmc_confirmed_by", "approved_by", "approval_state"],
			as_dict=True,
		)
		self.assertFalse(row.pmc_confirmed_by)
		self.assertFalse(row.approved_by)
		self.assertEqual(row.approval_state, "Pending")

	def _configure_request(self, **values):
		doc = frappe.get_doc("APS Change Request", self.fixture["request"].name)
		for fieldname, value in values.items():
			doc.set(fieldname, value)
		doc.save(ignore_permissions=True)
		return doc

	@staticmethod
	def _confirm_approve_apply(request_name: str):
		change_engine.confirm_change_request(request_name)
		change_engine.approve_change_request(request_name)
		return change_engine.apply_change_request(request_name)

	def _create_cancel_fixture(self):
		start = get_datetime(add_days(today(), 1)) + timedelta(hours=8)
		due_date = getdate(add_days(today(), 3))
		run = frappe.get_doc(
			{
				"doctype": "APS Planning Run",
				"company": self.company,
				"plant_floor": self.plant_floor,
				"planning_date": today(),
				"horizon_start": start,
				"horizon_end": start + timedelta(days=7),
				"horizon_days": 7,
				"run_type": "Trial",
				"existing_work_order_policy": "Exclude",
				"status": "Planned",
				"approval_state": "Pending",
			}
		).insert(ignore_permissions=True)
		net_requirement = frappe.get_doc(
			{
				"doctype": "APS Net Requirement",
				"company": self.company,
				"customer": self.customer,
				"item_code": self.item,
				"demand_date": due_date,
				"demand_qty": 100,
				"available_stock_qty": 0,
				"open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
				"planning_qty": 100,
				"net_requirement_qty": 100,
				"is_system_generated": 1,
			}
		).insert(ignore_permissions=True)
		result = frappe.get_doc(
			{
				"doctype": "APS Schedule Result",
				"planning_run": run.name,
				"company": self.company,
				"plant_floor": self.plant_floor,
				"net_requirement": net_requirement.name,
				"customer": self.customer,
				"item_code": self.item,
				"requested_date": due_date,
				"demand_source": "Customer Delivery Schedule",
				"planned_qty": 100,
				"produced_qty": 40,
				"delivered_qty": 20,
				"status": "Planned",
				"risk_status": "Normal",
				"segments": [
					{
						"workstation": self.workstation,
						"plant_floor": self.plant_floor,
						"start_time": start,
						"end_time": start + timedelta(hours=1),
						"planned_qty": 40,
						"sequence_no": 1,
						"segment_kind": "Primary",
						"segment_status": "Approved",
						"is_locked": 1,
					},
					{
						"workstation": self.workstation,
						"plant_floor": self.plant_floor,
						"start_time": start + timedelta(hours=1),
						"end_time": start + timedelta(hours=2),
						"planned_qty": 60,
						"sequence_no": 2,
						"segment_kind": "Primary",
						"segment_status": "Planned",
						"is_locked": 0,
					},
				],
			}
		).insert(ignore_permissions=True)
		consistency.recalculate_plan_consistency(run.name, reason="Phase 3 test fixture")
		segments = frappe.get_all(
			"APS Schedule Segment",
			filters={"parent": result.name},
			fields=["name", "is_locked"],
			order_by="sequence_no asc",
		)
		request = frappe.get_doc(
			{
				"doctype": "APS Change Request",
				"planning_run": run.name,
				"company": self.company,
				"plant_floor": self.plant_floor,
				"change_type": "Cancel",
				"target_result": result.name,
				"item_code": self.item,
				"customer": self.customer,
				"qty": 100,
				"target_planned_qty": 0,
				"retained_disposition": "Inventory",
			}
		).insert(ignore_permissions=True)
		return {
			"run": run,
			"net_requirement": net_requirement,
			"result": result,
			"locked_segment": next(row.name for row in segments if row.is_locked),
			"open_segment": next(row.name for row in segments if not row.is_locked),
			"request": request,
		}
