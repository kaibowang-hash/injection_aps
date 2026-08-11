from __future__ import annotations

import json
from datetime import timedelta

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, nowtime, today

from injection_aps.services import consistency, execution_sync, planning


class TestProductionExecutionSync(FrappeTestCase):
	def setUp(self):
		required = [
			"APS Production Allocation",
			"APS Planning Run",
			"Work Order Scheduling",
			"Scheduling Item",
			"Stock Entry",
		]
		if any(not frappe.db.exists("DocType", doctype) for doctype in required):
			self.skipTest("Phase 4 production synchronization DocTypes are not synced.")
		self.work_order = frappe.db.get_value(
			"Work Order",
			{},
			["name", "company", "production_item", "scrap_warehouse", "sales_order", "sales_order_item"],
			as_dict=True,
		)
		self.customer = (
			frappe.db.get_value("Sales Order", self.work_order.sales_order, "customer")
			if self.work_order and self.work_order.sales_order
			else frappe.db.get_value("Customer", {})
		)
		self.workstation = frappe.db.get_value("Workstation", {})
		if not self.work_order or not self.customer or not self.workstation:
			self.skipTest("Production sync tests need a Work Order, Customer, and Workstation.")
		self.plant_floor = frappe.db.get_value("Workstation", self.workstation, "plant_floor")
		self.fixture = self._create_plan_fixture()

	def test_legacy_execution_fifo_splits_one_work_order_without_duplicate_production(self):
		stock_entry = self._create_manufacture_entry(100)
		first = execution_sync.sync_production_for_run(self.fixture["run"])
		self.assertTrue(first["consistency"]["valid"])
		self.assertEqual(first["ledger"]["created"], 2)
		self.assertEqual(first["rollup"]["good_qty"], 100)
		allocations = frappe.get_all(
			"APS Production Allocation",
			filters={"source_stock_entry": stock_entry},
			fields=["segment", "allocated_qty", "allocation_method", "is_effective"],
			order_by="allocated_qty desc",
		)
		self.assertEqual([row.allocated_qty for row in allocations], [60, 40])
		self.assertEqual({row.allocation_method for row in allocations}, {"Execution Detail FIFO"})
		self.assertEqual({row.is_effective for row in allocations}, {1})
		self.assertEqual(
			frappe.db.get_value("APS Schedule Result", self.fixture["result"], "produced_qty"),
			100,
		)
		segment_qty = frappe.get_all(
			"APS Schedule Segment",
			filters={"parent": self.fixture["result"]},
			fields=["actual_good_qty"],
			order_by="sequence_no asc",
		)
		self.assertEqual([row.actual_good_qty for row in segment_qty], [60, 40])

		replay = execution_sync.sync_production_for_run(self.fixture["run"])
		self.assertEqual(replay["ledger"]["created"], 0)
		self.assertEqual(replay["ledger"]["updated"], 2)
		self.assertEqual(
			frappe.db.count("APS Production Allocation", {"source_stock_entry": stock_entry}),
			2,
		)
		self.assertEqual(
			frappe.db.get_value("APS Schedule Result", self.fixture["result"], "produced_qty"),
			100,
		)

	def test_cancelled_report_reverses_allocations_and_replay_stays_zero(self):
		stock_entry = self._create_manufacture_entry(100)
		execution_sync.sync_production_for_run(self.fixture["run"])
		frappe.db.set_value("Stock Entry", stock_entry, "docstatus", 2, update_modified=False)

		cancelled = execution_sync.sync_production_for_run(self.fixture["run"])
		self.assertEqual(cancelled["ledger"]["reversed"], 2)
		self.assertEqual(cancelled["rollup"]["good_qty"], 0)
		self.assertEqual(
			frappe.db.get_value("APS Schedule Result", self.fixture["result"], "produced_qty"),
			0,
		)
		ledger = frappe.get_all(
			"APS Production Allocation",
			filters={"source_stock_entry": stock_entry},
			fields=["effective_qty", "reversed_qty", "is_effective", "source_docstatus", "reversal_reason"],
		)
		self.assertEqual({row.effective_qty for row in ledger}, {0})
		self.assertEqual({row.is_effective for row in ledger}, {0})
		self.assertEqual({row.source_docstatus for row in ledger}, {2})
		self.assertEqual(sum(row.reversed_qty for row in ledger), 100)
		self.assertTrue(all("cancelled" in row.reversal_reason for row in ledger))

		replay = execution_sync.sync_production_for_run(self.fixture["run"])
		self.assertEqual(replay["ledger"]["reversed"], 0)
		self.assertEqual(replay["rollup"]["good_qty"], 0)

	def test_direct_scheduling_item_and_scrap_remain_separate(self):
		good_entry = self._create_manufacture_entry(
			60,
			direct_scheduling_item=self.fixture["scheduling_items"][0],
			output_type="Good",
		)
		scrap_entry = self._create_manufacture_entry(
			5,
			direct_scheduling_item=self.fixture["scheduling_items"][0],
			output_type="Scrap",
		)
		result = execution_sync.sync_production_for_run(self.fixture["run"])
		self.assertEqual(result["rollup"]["good_qty"], 60)
		self.assertEqual(result["rollup"]["scrap_qty"], 5)
		result_row = frappe.db.get_value(
			"APS Schedule Result",
			self.fixture["result"],
			["produced_qty", "good_produced_qty", "scrap_qty"],
			as_dict=True,
		)
		self.assertEqual(result_row.produced_qty, 60)
		self.assertEqual(result_row.good_produced_qty, 60)
		self.assertEqual(result_row.scrap_qty, 5)
		segment = frappe.db.get_value(
			"APS Schedule Segment",
			self.fixture["segments"][0],
			["actual_completed_qty", "actual_good_qty", "actual_scrap_qty"],
			as_dict=True,
		)
		self.assertEqual(segment.actual_completed_qty, 65)
		self.assertEqual(segment.actual_good_qty, 60)
		self.assertEqual(segment.actual_scrap_qty, 5)
		methods = frappe.get_all(
			"APS Production Allocation",
			filters={"source_stock_entry": ("in", [good_entry, scrap_entry])},
			pluck="allocation_method",
		)
		self.assertEqual(set(methods), {"Direct"})
		detail = planning.get_schedule_result_detail(self.fixture["result"])
		segments = {row.name: row for row in detail["segments"]}
		self.assertEqual(segments[self.fixture["segments"][0]].latest_stock_entry, scrap_entry)
		self.assertIsNone(segments[self.fixture["segments"][1]].latest_stock_entry)

	def test_draft_manufacture_entry_never_counts(self):
		draft_entry = self._create_manufacture_entry(100, docstatus=0)
		contexts = execution_sync._get_run_segment_contexts(self.fixture["run"])
		sources = execution_sync._get_formal_manufacture_sources(contexts)
		self.assertNotIn(draft_entry, {row["source_stock_entry"] for row in sources})
		result = execution_sync.sync_production_for_run(self.fixture["run"])
		self.assertEqual(result["rollup"]["good_qty"], 0)
		self.assertEqual(
			frappe.db.count("APS Production Allocation", {"source_stock_entry": draft_entry}),
			0,
		)
		self.assertEqual(
			frappe.db.get_value("APS Schedule Result", self.fixture["result"], "produced_qty"),
			0,
		)

	def test_bare_work_order_report_is_ignored_when_multiple_active_runs_claim_it(self):
		competing_run = self._create_competing_run_reference()
		stock_entry = self._create_manufacture_entry(40, use_execution_detail=False)

		first = execution_sync.sync_production_for_run(self.fixture["run"])
		second = execution_sync.sync_production_for_run(competing_run)

		self.assertEqual(first["source_detail_count"], 0)
		self.assertEqual(second["source_detail_count"], 0)
		self.assertEqual(first["desired_allocation_count"], 0)
		self.assertEqual(second["desired_allocation_count"], 0)
		self.assertEqual(first["rollup"]["good_qty"], 0)
		self.assertEqual(second["rollup"]["good_qty"], 0)
		self.assertEqual(
			frappe.db.count("APS Production Allocation", {"source_stock_entry": stock_entry}),
			0,
		)
		self.assertTrue(
			{self.fixture["run"], competing_run}.issubset(
				set(execution_sync.get_affected_production_runs(frappe.get_doc("Stock Entry", stock_entry)))
			)
		)
		frappe.db.set_value("Stock Entry", stock_entry, "docstatus", 2, update_modified=False)

	def test_direct_overproduction_preserves_source_without_overstating_customer_demand(self):
		stock_entry = self._create_manufacture_entry(
			130,
			direct_scheduling_item=self.fixture["scheduling_items"][0],
			output_type="Good",
		)

		result = execution_sync.sync_production_for_run(self.fixture["run"])

		self.assertEqual(result["rollup"]["good_qty"], 130)
		allocations = frappe.get_all(
			"APS Production Allocation",
			filters={"source_stock_entry": stock_entry},
			fields=["customer_schedule_item", "good_qty", "effective_qty"],
		)
		self.assertEqual(len(allocations), 2)
		qty_by_target = {row.customer_schedule_item: row.good_qty for row in allocations}
		self.assertEqual(qty_by_target, {self.fixture["schedule_item"]: 100, None: 30})
		self.assertEqual(sum(row.effective_qty for row in allocations), 130)
		self.assertEqual(
			frappe.db.get_value("Customer Delivery Schedule Item", self.fixture["schedule_item"], "produced_qty"),
			100,
		)
		replay = execution_sync.sync_production_for_run(self.fixture["run"])
		self.assertEqual(replay["ledger"]["created"], 0)
		self.assertEqual(
			frappe.db.count("APS Production Allocation", {"source_stock_entry": stock_entry}),
			2,
		)

	def _create_plan_fixture(self):
		start = get_datetime(f"{getdate(add_days(today(), 1))} 08:00:00")
		due_date = getdate(add_days(today(), 730))
		suffix = frappe.generate_hash(length=10)
		schedule = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": self.customer,
				"company": self.work_order.company,
				"schedule_scope": "PHASE4-PROD-SYNC-{0}".format(suffix),
				"version_no": "PHASE4-PROD-SYNC-{0}".format(suffix),
				"import_strategy": "Append",
				"source_type": "Customer Delivery Schedule",
				"status": "Active",
				"items": [
					{
						"item_code": self.work_order.production_item,
						"sales_order": self.work_order.sales_order,
						"schedule_date": due_date,
						"qty": 100,
						"balance_qty": 100,
						"status": "Open",
					},
				],
			}
		)
		schedule.flags.aps_schedule_import_transition = True
		schedule.insert(ignore_permissions=True)
		schedule_item = frappe.db.get_value(
			"Customer Delivery Schedule Item", {"parent": schedule.name}, "name"
		)
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
						"item_code": self.work_order.production_item,
						"schedule_date": str(due_date),
					}
				],
			},
			sort_keys=True,
		)
		run = frappe.get_doc(
			{
				"doctype": "APS Planning Run",
				"company": self.work_order.company,
				"plant_floor": self.plant_floor,
				"planning_date": today(),
				"horizon_start": start,
				"horizon_end": start + timedelta(days=5),
				"horizon_days": 5,
				"run_type": "Trial",
				"existing_work_order_policy": "Exclude",
				"status": "Planned",
				"approval_state": "Pending",
			}
		).insert(ignore_permissions=True)
		net = frappe.get_doc(
			{
				"doctype": "APS Net Requirement",
				"company": self.work_order.company,
				"customer": self.customer,
				"item_code": self.work_order.production_item,
				"sales_order": self.work_order.sales_order,
				"sales_order_item": self.work_order.sales_order_item,
				"demand_date": due_date,
				"demand_qty": 100,
				"available_stock_qty": 0,
				"open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
				"planning_qty": 100,
				"net_requirement_qty": 100,
				"is_system_generated": 1,
				"fulfillment_baseline_json": baseline_json,
			}
		).insert(ignore_permissions=True)
		result = frappe.get_doc(
			{
				"doctype": "APS Schedule Result",
				"planning_run": run.name,
				"company": self.work_order.company,
				"plant_floor": self.plant_floor,
				"net_requirement": net.name,
				"customer": self.customer,
				"item_code": self.work_order.production_item,
				"sales_order": self.work_order.sales_order,
				"sales_order_item": self.work_order.sales_order_item,
				"requested_date": due_date,
				"demand_source": "Customer Delivery Schedule",
				"planned_qty": 100,
				"status": "Planned",
				"risk_status": "Normal",
				"fulfillment_baseline_json": baseline_json,
				"segments": [
					{
						"workstation": self.workstation,
						"plant_floor": self.plant_floor,
						"start_time": start,
						"end_time": start + timedelta(hours=1),
						"planned_qty": 60,
						"sequence_no": 1,
						"segment_kind": "Primary",
						"segment_status": "Planned",
						"linked_work_order": self.work_order.name,
					},
					{
						"workstation": self.workstation,
						"plant_floor": self.plant_floor,
						"start_time": start + timedelta(hours=1),
						"end_time": start + timedelta(hours=2),
						"planned_qty": 40,
						"sequence_no": 2,
						"segment_kind": "Primary",
						"segment_status": "Planned",
						"linked_work_order": self.work_order.name,
					},
				],
			}
		).insert(ignore_permissions=True)
		segments = frappe.get_all(
			"APS Schedule Segment",
			filters={"parent": result.name},
			pluck="name",
			order_by="sequence_no asc",
		)
		wos_name = "TEST-WOS-{0}".format(frappe.generate_hash(length=10))
		wos = frappe.new_doc("Work Order Scheduling")
		wos.name = wos_name
		wos.company = self.work_order.company
		wos.posting_date = today()
		wos.plant_floor = self.plant_floor
		wos.shift_type = "白班"
		wos.status = "Manufacture"
		wos.custom_aps_run = run.name
		wos.custom_aps_approval_state = "Approved"
		wos.db_insert()
		scheduling_items = []
		for idx, (segment, qty) in enumerate(zip(segments, (60, 40), strict=True), start=1):
			item = frappe.new_doc("Scheduling Item")
			item.name = frappe.generate_hash(length=10)
			item.parent = wos.name
			item.parenttype = "Work Order Scheduling"
			item.parentfield = "scheduling_items"
			item.idx = idx
			item.work_order = self.work_order.name
			item.workstation = self.workstation
			item.scheduling_qty = qty
			item.completed_qty = qty
			item.defect_qty = 5 if idx == 1 else 0
			item.planned_start_date = start + timedelta(hours=idx - 1)
			item.planned_end_date = start + timedelta(hours=idx)
			item.custom_aps_run = run.name
			item.custom_aps_result_reference = result.name
			item.custom_aps_segment_reference = segment
			item.db_insert()
			scheduling_items.append(item.name)
		frappe.db.set_value(
			"APS Schedule Segment",
			segments[0],
			{
				"linked_work_order_scheduling": wos.name,
				"linked_scheduling_item": scheduling_items[0],
			},
			update_modified=False,
		)
		frappe.db.set_value(
			"APS Schedule Segment",
			segments[1],
			{
				"linked_work_order_scheduling": wos.name,
				"linked_scheduling_item": scheduling_items[1],
			},
			update_modified=False,
		)
		consistency.recalculate_plan_consistency(run.name, reason="Phase 4 production fixture")
		return {
			"run": run.name,
			"result": result.name,
			"segments": segments,
			"wos": wos.name,
			"scheduling_items": scheduling_items,
			"schedule": schedule.name,
			"schedule_item": schedule_item,
		}

	def _create_competing_run_reference(self):
		base_run = frappe.db.get_value(
			"APS Planning Run",
			self.fixture["run"],
			["company", "plant_floor", "planning_date", "horizon_start", "horizon_end", "horizon_days"],
			as_dict=True,
		)
		base_result = frappe.db.get_value(
			"APS Schedule Result",
			self.fixture["result"],
			[
				"company",
				"plant_floor",
				"net_requirement",
				"customer",
				"item_code",
				"sales_order",
				"sales_order_item",
				"requested_date",
				"demand_source",
			],
			as_dict=True,
		)
		base_segment = frappe.db.get_value(
			"APS Schedule Segment",
			self.fixture["segments"][0],
			["workstation", "plant_floor", "start_time", "end_time"],
			as_dict=True,
		)
		run = frappe.get_doc(
			{
				"doctype": "APS Planning Run",
				**base_run,
				"run_type": "Trial",
				"existing_work_order_policy": "Exclude",
				"status": "Planned",
				"approval_state": "Pending",
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "APS Schedule Result",
				**base_result,
				"planning_run": run.name,
				"planned_qty": 40,
				"status": "Planned",
				"risk_status": "Normal",
				"segments": [
					{
						**base_segment,
						"planned_qty": 40,
						"sequence_no": 1,
						"segment_kind": "Primary",
						"segment_status": "Planned",
						"linked_work_order": self.work_order.name,
					}
				],
			}
		).insert(ignore_permissions=True)
		return run.name

	def _create_manufacture_entry(
		self,
		qty,
		*,
		direct_scheduling_item=None,
		output_type=None,
		docstatus=1,
		use_execution_detail=True,
	):
		name = "TEST-MFG-{0}".format(frappe.generate_hash(length=10))
		doc = frappe.new_doc("Stock Entry")
		doc.name = name
		doc.docstatus = docstatus
		doc.company = self.work_order.company
		doc.purpose = "Manufacture"
		doc.stock_entry_type = "Manufacture"
		doc.work_order = self.work_order.name
		doc.work_order_scheduling = self.fixture["wos"] if use_execution_detail else None
		doc.posting_date = today()
		doc.posting_time = nowtime()
		doc.fg_completed_qty = qty
		doc.custom_aps_scheduling_item = direct_scheduling_item
		doc.custom_aps_output_type = output_type
		if direct_scheduling_item:
			doc.custom_aps_segment_reference = frappe.db.get_value(
				"Scheduling Item", direct_scheduling_item, "custom_aps_segment_reference"
			)
		doc.db_insert()
		detail = frappe.new_doc("Stock Entry Detail")
		detail.name = frappe.generate_hash(length=10)
		detail.parent = doc.name
		detail.parenttype = "Stock Entry"
		detail.parentfield = "items"
		detail.idx = 1
		detail.item_code = self.work_order.production_item
		detail.qty = qty
		detail.transfer_qty = qty
		detail.is_finished_item = 0 if output_type == "Scrap" else 1
		detail.is_scrap_item = 1 if output_type == "Scrap" else 0
		detail.t_warehouse = self.work_order.scrap_warehouse if output_type == "Scrap" else None
		detail.db_insert()
		return doc.name
