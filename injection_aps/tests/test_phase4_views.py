from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, today

from injection_aps.api import app
from injection_aps.services import availability, consistency, planning


class TestReleaseCenterQueries(FrappeTestCase):
	def test_review_counts_use_structured_count_field(self):
		rows = [frappe._dict(name="BATCH-1")]
		captured = {}

		def fake_get_all(_doctype, **kwargs):
			captured["fields"] = kwargs.get("fields")
			return [frappe._dict(parent="BATCH-1", review_status="Pending", count=2)]

		with patch.object(app.frappe, "get_all", side_effect=fake_get_all):
			app._attach_review_counts(rows, "APS Work Order Proposal Item")

		self.assertIn({"COUNT": "name", "as": "count"}, captured["fields"])
		self.assertEqual(rows[0].pending_count, 2)


class TestPhase4ViewConsistency(FrappeTestCase):
	def setUp(self):
		self.customer = frappe.db.get_value("Customer", {})
		self.workstation = None
		self.plant_floor = None
		self.company = None
		for row in frappe.get_all(
			"Workstation",
			filters={"plant_floor": ["!=", ""]},
			fields=["name", "plant_floor"],
			limit=50,
		):
			company = frappe.db.get_value("Plant Floor", row.plant_floor, "company")
			if company:
				self.workstation = row.name
				self.plant_floor = row.plant_floor
				self.company = company
				break
		if not self.company or not self.customer or not self.workstation:
			self.skipTest("Phase 4 view tests need Customer and Workstation records linked to a company Plant Floor.")
		self.item = self._create_item()
		self.fixture = self._create_fixture()

	def test_plan_gantt_release_progress_and_detail_share_fulfillment_values(self):
		run_name = self.fixture["run"]
		result_name = self.fixture["result"]
		run_row = frappe.db.get_value(
			"APS Planning Run",
			run_name,
			[
				"total_prebuild_qty",
				"total_jit_qty",
				"total_current_deliverable_qty",
				"total_prebuild_inventory_qty",
				"total_cancellation_inventory_risk_qty",
			],
			as_dict=True,
		)
		gantt = app.get_schedule_gantt_data(run_name)
		release = app.get_release_center_data(run_name)
		progress = planning.get_customer_schedule_progress_data(
			company=self.company,
			item_code=self.item,
			run_name=run_name,
		)
		detail = planning.get_schedule_result_detail(result_name)

		self.assertEqual(run_row.total_prebuild_qty, 30)
		self.assertEqual(run_row.total_jit_qty, 70)
		self.assertEqual(gantt["fulfillment_summary"]["prebuild_qty"], run_row.total_prebuild_qty)
		self.assertEqual(gantt["fulfillment_summary"]["jit_qty"], run_row.total_jit_qty)
		self.assertEqual(release["fulfillment_summary"]["prebuild_qty"], run_row.total_prebuild_qty)
		self.assertEqual(release["fulfillment_summary"]["jit_qty"], run_row.total_jit_qty)
		self.assertEqual(
			release["fulfillment_summary"]["current_deliverable_qty"],
			run_row.total_current_deliverable_qty,
		)

		self.assertEqual(len(gantt["tasks"]), 2)
		self.assertEqual({row["details"]["production_mode"] for row in gantt["tasks"]}, {"Prebuild", "JIT"})
		self.assertEqual({row["details"]["production_strategy"] for row in gantt["tasks"]}, {"Auto Balance"})
		self.assertEqual(sum(row["details"]["prebuild_qty"] for row in gantt["tasks"][:1]), 30)
		self.assertTrue(all("load_percent" in row["details"] for row in gantt["tasks"]))

		projection = detail["fulfillment_projection"]
		self.assertEqual(projection["prebuild_qty"], 30)
		self.assertEqual(projection["jit_qty"], 70)
		self.assertGreater(len(projection["timeline"]), 2)
		self.assertEqual({row.production_mode for row in detail["segments"]}, {"Prebuild", "JIT"})
		self.assertTrue(all(hasattr(row, "actual_good_qty") for row in detail["segments"]))

		progress_row = next(row for row in progress["rows"] if row["schedule_item"] == self.fixture["schedule_item"])
		self.assertEqual(progress_row["production_strategy"], "Auto Balance")
		self.assertEqual(progress_row["prebuild_qty"], 30)
		self.assertEqual(progress_row["jit_qty"], 70)
		self.assertEqual(progress_row["current_deliverable_qty"], run_row.total_current_deliverable_qty)
		self.assertIn("last_actual_report_time", progress_row)
		self.assertIn("production_source_documents", progress_row)
		self.assertIn("delivery_source_documents", progress_row)

	def _create_item(self):
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or frappe.db.get_value("Item Group", {})
		stock_uom = frappe.db.get_value("UOM", {})
		name = "TEST-PHASE4-VIEW-{0}".format(frappe.generate_hash(length=10))
		item = frappe.new_doc("Item")
		item.name = name
		item.item_code = name
		item.item_name = name
		item.item_group = item_group
		item.stock_uom = stock_uom
		item.is_stock_item = 1
		item.disabled = 0
		item.db_insert()
		return item.name

	def _create_fixture(self):
		start = get_datetime(f"{getdate(add_days(today(), 1))} 08:00:00")
		due_date = getdate(add_days(today(), 2))
		suffix = frappe.generate_hash(length=10)
		schedule = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": self.customer,
				"company": self.company,
				"schedule_scope": "PHASE4-VIEW-{0}".format(suffix),
				"version_no": "PHASE4-VIEW-{0}".format(suffix),
				"import_strategy": "Append",
				"source_type": "Customer Delivery Schedule",
				"status": "Active",
				"items": [
					{
						"item_code": self.item,
						"schedule_date": due_date,
						"qty": 100,
						"production_strategy": "Auto Balance",
						"demand_confidence": "Confirmed",
						"prebuild_allowed": 1,
						"max_prebuild_days": 7,
						"status": "Open",
					},
				],
			}
		)
		schedule.flags.aps_schedule_import_transition = True
		schedule.insert(ignore_permissions=True)
		schedule_item = frappe.db.get_value("Customer Delivery Schedule Item", {"parent": schedule.name}, "name")
		baseline_json = json.dumps(
			{
				"version": 3,
				"net_requirement": {
					"demand_qty": 100.0,
					"available_stock_qty": 0.0,
					"open_work_order_qty": 0.0,
					"existing_work_order_policy": "Exclude",
				},
				"targets": [
					{
						"customer_schedule_item": schedule_item,
						"opening_required_qty": 100.0,
						"source_open_qty": 100.0,
						"item_code": self.item,
						"schedule_date": str(due_date),
					}
				],
			},
			sort_keys=True,
		)
		run = frappe.get_doc(
			{
				"doctype": "APS Planning Run",
				"company": self.company,
				"plant_floor": self.plant_floor,
				"planning_date": today(),
				"horizon_start": start,
				"horizon_end": start + timedelta(days=4),
				"horizon_days": 4,
				"run_type": "Trial",
				"existing_work_order_policy": "Exclude",
				"status": "Planned",
				"approval_state": "Pending",
				"total_prebuild_qty": 30,
				"total_jit_qty": 70,
			}
		).insert(ignore_permissions=True)
		net = frappe.get_doc(
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
				"production_strategy": "Auto Balance",
				"demand_confidence": "Confirmed",
				"prebuild_allowed": 1,
				"max_prebuild_days": 7,
				"is_system_generated": 1,
				"fulfillment_baseline_json": baseline_json,
			}
		).insert(ignore_permissions=True)
		result = frappe.get_doc(
			{
				"doctype": "APS Schedule Result",
				"planning_run": run.name,
				"company": self.company,
				"plant_floor": self.plant_floor,
				"net_requirement": net.name,
				"customer": self.customer,
				"item_code": self.item,
				"requested_date": due_date,
				"demand_source": "Customer Delivery Schedule",
				"production_strategy": "Auto Balance",
				"demand_confidence": "Confirmed",
				"prebuild_allowed": 1,
				"max_prebuild_days": 7,
				"planned_qty": 100,
				"prebuild_qty": 30,
				"jit_qty": 70,
				"early_days": 1,
				"late_qty_before_balance": 30,
				"late_qty_after_balance": 0,
				"status": "Planned",
				"risk_status": "Normal",
				"fulfillment_baseline_json": baseline_json,
				"segments": [
					{
						"workstation": self.workstation,
						"plant_floor": self.plant_floor,
						"start_time": start,
						"end_time": start + timedelta(hours=3),
						"planned_qty": 30,
						"sequence_no": 1,
						"segment_kind": "Primary",
						"segment_status": "Planned",
						"production_mode": "Prebuild",
						"load_percent": 50,
					},
					{
						"workstation": self.workstation,
						"plant_floor": self.plant_floor,
						"start_time": start + timedelta(days=1),
						"end_time": start + timedelta(days=1, hours=7),
						"planned_qty": 70,
						"sequence_no": 2,
						"segment_kind": "Primary",
						"segment_status": "Planned",
						"production_mode": "JIT",
						"load_percent": 90,
					},
				],
			}
		).insert(ignore_permissions=True)
		consistency.recalculate_plan_consistency(run.name, reason="Phase 4 shared-view fixture")
		availability.recalculate_run_fulfillment(run.name)
		return {
			"run": run.name,
			"result": result.name,
			"schedule": schedule.name,
			"schedule_item": schedule_item,
		}
