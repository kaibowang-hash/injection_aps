from __future__ import annotations

import json
from datetime import timedelta

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, nowtime, today

from injection_aps.services import consistency


class TestPhase6QuantityAudit(FrappeTestCase):
	def setUp(self):
		required = [
			"APS Planning Run",
			"APS Schedule Result",
			"APS Schedule Segment",
			"APS Production Allocation",
			"APS Delivery Allocation",
			"Customer Delivery Schedule",
			"Stock Entry",
			"Delivery Note",
		]
		if any(not frappe.db.exists("DocType", doctype) for doctype in required):
			self.skipTest("Phase 6 quantity audit DocTypes are not synced.")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		self.item = frappe.db.get_value("Item", {"disabled": 0}) or frappe.db.get_value("Item", {})
		self.workstation = frappe.db.get_value("Workstation", {})
		self.stock_uom = frappe.db.get_value("Item", self.item, "stock_uom") or frappe.db.get_value("UOM", {})
		if not all((self.company, self.customer, self.item, self.workstation, self.stock_uom)):
			self.skipTest("Phase 6 quantity audit tests need Company, Customer, Item, Workstation, and UOM.")
		self.plant_floor = frappe.db.get_value("Workstation", self.workstation, "plant_floor")

	def test_balanced_run_reports_zero_differences_without_writing(self):
		fixture = self._create_audit_fixture(produced_qty=30, scrap_qty=2, delivered_qty=25)
		before_modified = frappe.db.get_value("APS Planning Run", fixture["run"], "modified")

		audit = consistency.audit_run_quantity_consistency(fixture["run"])

		self.assertTrue(audit["valid"])
		self.assertEqual(audit["difference_count"], 0)
		self.assertEqual(audit["totals"]["machine_scheduled_qty"], 100)
		self.assertEqual(audit["totals"]["produced_qty"], 30)
		self.assertEqual(audit["totals"]["delivered_qty"], 25)
		self.assertEqual(frappe.db.get_value("APS Planning Run", fixture["run"], "modified"), before_modified)

	def test_audit_reports_document_expected_actual_and_difference(self):
		fixture = self._create_audit_fixture(
			produced_qty=30,
			scrap_qty=2,
			delivered_qty=25,
			late=True,
			result_overrides={
				"machine_scheduled_qty": 90,
				"scheduled_qty": 90,
				"produced_qty": 35,
				"good_produced_qty": 35,
				"delivered_qty": 20,
				"risk_status": "Normal",
				"schedule_delay_minutes": 0,
			},
			run_overrides={
				"total_machine_scheduled_qty": 90,
				"total_scheduled_qty": 90,
				"total_produced_qty": 35,
				"total_delivered_qty": 20,
			},
		)

		audit = consistency.audit_run_quantity_consistency(fixture["run"])

		self.assertFalse(audit["valid"])
		by_field = {
			(row["doctype"], row["name"], row["fieldname"]): row
			for row in audit["differences"]
		}
		result_machine = by_field[("APS Schedule Result", fixture["result"], "machine_scheduled_qty")]
		self.assertEqual(result_machine["expected_qty"], 100)
		self.assertEqual(result_machine["actual_qty"], 90)
		self.assertEqual(result_machine["difference_qty"], -10)

		result_produced = by_field[("APS Schedule Result", fixture["result"], "produced_qty")]
		self.assertEqual(result_produced["expected_qty"], 30)
		self.assertEqual(result_produced["actual_qty"], 35)
		self.assertEqual(result_produced["difference_qty"], 5)

		result_delivered = by_field[("APS Schedule Result", fixture["result"], "delivered_qty")]
		self.assertEqual(result_delivered["expected_qty"], 25)
		self.assertEqual(result_delivered["actual_qty"], 20)
		self.assertEqual(result_delivered["difference_qty"], -5)

		run_machine = by_field[("APS Planning Run", fixture["run"], "total_machine_scheduled_qty")]
		self.assertEqual(run_machine["expected_qty"], 100)
		self.assertEqual(run_machine["actual_qty"], 90)
		self.assertEqual(run_machine["difference_qty"], -10)

		segment_delay = by_field[("APS Schedule Segment", fixture["segment"], "schedule_delay_minutes")]
		self.assertGreater(segment_delay["expected_qty"], 0)
		self.assertEqual(segment_delay["actual_qty"], 0)
		self.assertEqual(
			by_field[("APS Schedule Result", fixture["result"], "risk_status")]["expected_qty"],
			"Critical or Blocked",
		)

	def test_live_net_requirement_detects_result_and_run_shrunk_together(self):
		fixture = self._create_audit_fixture(produced_qty=0, scrap_qty=0, delivered_qty=0)
		frappe.db.set_value(
			"APS Schedule Segment",
			fixture["segment"],
			"planned_qty",
			80,
			update_modified=False,
		)
		frappe.db.set_value(
			"APS Schedule Result",
			fixture["result"],
			{
				"planned_qty": 80,
				"machine_scheduled_qty": 80,
				"demand_covered_qty": 80,
				"scheduled_qty": 80,
				"unscheduled_qty": 0,
			},
			update_modified=False,
		)
		frappe.db.set_value(
			"APS Planning Run",
			fixture["run"],
			{
				"total_net_requirement_qty": 80,
				"total_machine_scheduled_qty": 80,
				"total_demand_covered_qty": 80,
				"total_scheduled_qty": 80,
				"total_unscheduled_qty": 0,
			},
			update_modified=False,
		)

		audit = consistency.audit_run_quantity_consistency(fixture["run"])

		self.assertFalse(audit["valid"])
		fields = {(row["doctype"], row["fieldname"]) for row in audit["differences"]}
		self.assertIn(("APS Schedule Result", "planned_qty"), fields)
		self.assertIn(("APS Planning Run", "total_net_requirement_qty"), fields)

	def test_cancelled_live_stock_entry_is_not_credited_from_cached_ledger(self):
		fixture = self._create_audit_fixture(produced_qty=30, scrap_qty=0, delivered_qty=0)
		stock_entry = frappe.db.get_value(
			"APS Production Allocation",
			{"schedule_result": fixture["result"], "is_effective": 1},
			"source_stock_entry",
		)
		frappe.db.set_value("Stock Entry", stock_entry, "docstatus", 2, update_modified=False)

		audit = consistency.audit_run_quantity_consistency(fixture["run"])

		self.assertFalse(audit["valid"])
		self.assertEqual(audit["production_source_count"], 0)
		self.assertTrue(
			any(
				row["doctype"] == "APS Production Allocation"
				and row["fieldname"] == "source_docstatus"
				for row in audit["differences"]
			)
		)

	def _create_audit_fixture(
		self,
		*,
		produced_qty=0,
		scrap_qty=0,
		delivered_qty=0,
		late=False,
		result_overrides=None,
		run_overrides=None,
	):
		result_overrides = result_overrides or {}
		run_overrides = run_overrides or {}
		suffix = frappe.generate_hash(length=10)
		start = get_datetime(f"{getdate(add_days(today(), 1))} 08:00:00")
		requested_date = getdate(today() if late else add_days(today(), 2))
		end = start + (timedelta(hours=18) if late else timedelta(hours=2))
		work_order = self._create_work_order(suffix)
		schedule = self._create_schedule(suffix, requested_date)
		schedule_item = frappe.db.get_value("Customer Delivery Schedule Item", {"parent": schedule}, "name")
		run_values = {
			"doctype": "APS Planning Run",
			"company": self.company,
			"plant_floor": self.plant_floor,
			"planning_date": today(),
			"horizon_start": start,
			"horizon_end": start + timedelta(days=5),
			"horizon_days": 5,
			"run_type": "Trial",
			"existing_work_order_policy": "Exclude",
			"status": "Planned",
			"approval_state": "Pending",
			"total_net_requirement_qty": 100,
			"total_machine_scheduled_qty": 100,
			"total_demand_covered_qty": 100,
			"total_overproduction_qty": 0,
			"total_scheduled_qty": 100,
			"total_unscheduled_qty": 0,
			"total_produced_qty": produced_qty,
			"total_scrap_qty": scrap_qty,
			"total_delivered_qty": delivered_qty,
			"result_count": 1,
			**run_overrides,
		}
		run = frappe.get_doc(run_values).insert(ignore_permissions=True)
		demand_source_snapshot_json = json.dumps(
			[
				{
					"demand_pool": f"PHASE6-AUDIT-DEMAND-{suffix}",
					"source_doctype": "Customer Delivery Schedule",
					"source_name": schedule,
					"source_detail_name": schedule_item,
					"qty": 100,
				}
			]
		)
		fulfillment_baseline_json = json.dumps(
			{
				"version": 4,
				"net_requirement": {
					"formula_version": 1,
					"demand_qty": 100,
					"available_stock_qty": 0,
					"open_work_order_qty": 0,
					"existing_work_order_policy": "Exclude",
					"safety_stock_gap_qty": 0,
					"minimum_batch_qty": 0,
					"minimum_batch_coverage_qty": 0,
					"base_residual_qty": 100,
					"net_requirement_qty": 100,
					"planning_qty": 100,
					"new_batch_surplus_qty": 0,
					"is_safety_stock_group": 0,
				},
				"targets": [
					{
						"customer_schedule_item": schedule_item,
						"item_code": self.item,
						"schedule_date": str(requested_date),
						"source_open_qty": 100,
						"opening_required_qty": 100,
						"opening_delivered_qty": 0,
					}
				],
				"sales_order_items": [],
			}
		)
		net = frappe.get_doc(
			{
				"doctype": "APS Net Requirement",
				"company": self.company,
				"customer": self.customer,
				"item_code": self.item,
				"demand_date": requested_date,
				"demand_qty": 100,
				"available_stock_qty": 0,
				"open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
				"safety_stock_gap_qty": 0,
				"minimum_batch_qty": 0,
				"planning_qty": 100,
				"net_requirement_qty": 100,
					"demand_source_snapshot_json": demand_source_snapshot_json,
					"fulfillment_baseline_json": fulfillment_baseline_json,
				"is_system_generated": 1,
			}
		).insert(ignore_permissions=True)
		result_values = {
			"doctype": "APS Schedule Result",
			"planning_run": run.name,
			"company": self.company,
			"plant_floor": self.plant_floor,
			"net_requirement": net.name,
			"customer": self.customer,
			"item_code": self.item,
			"requested_date": requested_date,
			"demand_source": "Customer Delivery Schedule",
			"planned_qty": 100,
			"machine_scheduled_qty": 100,
			"demand_covered_qty": 100,
			"overproduction_qty": 0,
			"scheduled_qty": 100,
			"unscheduled_qty": 0,
			"produced_qty": produced_qty,
			"good_produced_qty": produced_qty,
			"scrap_qty": scrap_qty,
			"delivered_qty": delivered_qty,
			"risk_status": "Normal",
			"schedule_delay_minutes": 0,
			"status": "Planned",
				"fulfillment_baseline_json": fulfillment_baseline_json,
				"demand_source_snapshot_json": demand_source_snapshot_json,
			"segments": [
				{
					"workstation": self.workstation,
					"plant_floor": self.plant_floor,
					"start_time": start,
					"end_time": end,
					"planned_qty": 100,
					"sequence_no": 1,
					"segment_kind": "Primary",
					"segment_status": "Planned",
					"risk_status": "Normal",
					"schedule_delay_minutes": 0,
					"actual_good_qty": produced_qty,
					"actual_scrap_qty": scrap_qty,
					"actual_completed_qty": produced_qty + scrap_qty,
					"linked_work_order": work_order,
				}
			],
			**result_overrides,
		}
		result = frappe.get_doc(result_values).insert(ignore_permissions=True)
		if result_overrides:
			frappe.db.set_value(
				"APS Schedule Result",
				result.name,
				result_overrides,
				update_modified=False,
			)
		segment = frappe.db.get_value("APS Schedule Segment", {"parent": result.name}, "name")
		if produced_qty or scrap_qty:
			self._create_production_allocations(
				suffix,
				run.name,
				result.name,
				segment,
				work_order,
				produced_qty,
				scrap_qty,
			)
		if delivered_qty:
			self._create_delivery_allocation(suffix, schedule, schedule_item, delivered_qty)
			frappe.db.set_value(
				"Customer Delivery Schedule Item",
				schedule_item,
				{"delivered_qty": delivered_qty, "balance_qty": max(100 - delivered_qty, 0)},
				update_modified=False,
			)
		return {"run": run.name, "result": result.name, "segment": segment}

	def _create_schedule(self, suffix, requested_date):
		doc = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": self.customer,
				"company": self.company,
				"schedule_scope": f"PHASE6-AUDIT-{suffix}",
				"version_no": f"PHASE6-AUDIT-{suffix}",
				"import_strategy": "Append",
				"source_type": "Customer Delivery Schedule",
				"status": "Active",
				"items": [
					{
						"item_code": self.item,
						"schedule_date": requested_date,
						"qty": 100,
						"balance_qty": 100,
						"delivered_qty": 0,
						"status": "Open",
					}
				],
			}
		)
		doc.flags.aps_schedule_import_transition = True
		doc.insert(ignore_permissions=True)
		return doc.name

	def _create_work_order(self, suffix):
		doc = frappe.new_doc("Work Order")
		doc.name = f"PHASE6-AUDIT-WO-{suffix}"
		doc.docstatus = 1
		doc.status = "In Process"
		doc.company = self.company
		doc.production_item = self.item
		doc.stock_uom = self.stock_uom
		doc.qty = 100
		doc.skip_transfer = 1
		doc.db_insert()
		return doc.name

	def _create_production_allocations(self, suffix, run, result, segment, work_order, good_qty, scrap_qty):
		for output_type, qty, fieldname in (
			("Good", good_qty, "good_qty"),
			("Scrap", scrap_qty, "scrap_qty"),
		):
			if not qty:
				continue
			stock_entry, detail = self._create_stock_entry(
				suffix,
				work_order,
				qty,
				output_type=output_type,
			)
			doc = frappe.new_doc("APS Production Allocation")
			doc.allocation_key = f"PHASE6-AUDIT-PA-{suffix}-{output_type}"
			doc.planning_run = run
			doc.schedule_result = result
			doc.segment = segment
			doc.work_order = work_order
			doc.source_stock_entry = stock_entry
			doc.source_stock_entry_detail = detail
			doc.source_docstatus = 1
			doc.output_type = output_type
			doc.allocation_method = "Execution Detail FIFO"
			doc.source_qty = qty
			doc.allocated_qty = qty
			setattr(doc, fieldname, qty)
			doc.effective_qty = qty
			doc.is_effective = 1
			doc.db_insert()

	def _create_stock_entry(self, suffix, work_order, qty, *, output_type):
		doc = frappe.new_doc("Stock Entry")
		doc.name = f"PHASE6-AUDIT-SE-{suffix}-{output_type}"
		doc.docstatus = 1
		doc.company = self.company
		doc.purpose = "Manufacture"
		doc.stock_entry_type = "Manufacture"
		doc.work_order = work_order
		doc.custom_aps_output_type = output_type
		doc.posting_date = today()
		doc.posting_time = nowtime()
		doc.fg_completed_qty = qty
		doc.db_insert()
		detail = frappe.new_doc("Stock Entry Detail")
		detail.name = f"PHASE6-AUDIT-SED-{suffix}-{output_type}"
		detail.parent = doc.name
		detail.parenttype = "Stock Entry"
		detail.parentfield = "items"
		detail.idx = 1
		detail.item_code = self.item
		detail.qty = qty
		detail.transfer_qty = qty
		detail.is_finished_item = 1
		detail.db_insert()
		return doc.name, detail.name

	def _create_delivery_allocation(self, suffix, schedule, schedule_item, qty):
		delivery_note, delivery_item = self._create_delivery_note(suffix, schedule_item, qty)
		doc = frappe.new_doc("APS Delivery Allocation")
		doc.allocation_key = f"PHASE6-AUDIT-DA-{suffix}"
		doc.company = self.company
		doc.customer = self.customer
		doc.item_code = self.item
		doc.schedule_date = frappe.db.get_value("Customer Delivery Schedule Item", schedule_item, "schedule_date")
		doc.customer_schedule = schedule
		doc.customer_schedule_item = schedule_item
		doc.source_delivery_note = delivery_note
		doc.source_delivery_note_item = delivery_item
		doc.source_docstatus = 1
		doc.allocation_method = "Direct"
		doc.source_qty = qty
		doc.allocated_qty = qty
		doc.effective_qty = qty
		doc.cumulative_delivered_qty = qty
		doc.is_effective = 1
		doc.db_insert()

	def _create_delivery_note(self, suffix, schedule_item, qty):
		doc = frappe.new_doc("Delivery Note")
		doc.name = f"PHASE6-AUDIT-DN-{suffix}"
		doc.docstatus = 1
		doc.company = self.company
		doc.customer = self.customer
		doc.posting_date = today()
		doc.posting_time = nowtime()
		doc.db_insert()
		item = frappe.new_doc("Delivery Note Item")
		item.name = f"PHASE6-AUDIT-DNI-{suffix}"
		item.parent = doc.name
		item.parenttype = "Delivery Note"
		item.parentfield = "items"
		item.idx = 1
		item.item_code = self.item
		item.qty = qty
		item.stock_qty = qty
		item.conversion_factor = 1
		item.custom_aps_customer_schedule_item = schedule_item
		item.db_insert()
		return doc.name, item.name
