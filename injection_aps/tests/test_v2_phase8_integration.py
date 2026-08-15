from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from injection_aps.api import app as api
from injection_aps.services import progress_v2, schedule_revision
from injection_aps.tests.v2_phase0_gate import assert_isolated_environment


class TestPhase8ProgressIntegration(FrappeTestCase):
	def setUp(self):
		assert_isolated_environment(require_fixture=True)
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		self.original_v2 = frappe.db.get_single_value("APS Settings", "enable_aps_v2") or 0
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", 1)
		frappe.clear_cache(doctype="APS Settings")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		self.plant_floor = frappe.db.get_value("Plant Floor", {"company": self.company})
		self.workstation = frappe.db.get_value("Workstation", {"name": ("like", "APS-V2-FIXTURE-%")}) or frappe.db.get_value("Workstation", {})
		self.warehouse = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 0, "disabled": 0})
		if not all((self.company, self.customer, self.plant_floor, self.workstation, self.warehouse)):
			self.skipTest("Phase 8 integration needs isolated Company, Customer, Plant Floor, Workstation, and Warehouse fixtures.")
		self.item = self._create_item()
		self.schedule, self.schedule_items = self._create_schedule()
		self.run_one = self._create_run("P8 cross-run owner one")
		self.run_two = self._create_run("P8 cross-run owner two")
		self.commitment_one = self._create_commitment(
			self.run_one, self.schedule_items[0], requested_qty=80, stock_qty=10,
			new_plan_qty=70, on_time_qty=70, late_qty=0,
		)
		self.commitment_two = self._create_commitment(
			self.run_two, self.schedule_items[1], requested_qty=50, stock_qty=0,
			new_plan_qty=50, on_time_qty=30, late_qty=20,
		)
		self.result_one = self._create_result(
			self.run_one, self.commitment_one, planned_qty=70,
			baseline=("2026-08-18 08:00:00", "2026-08-18 10:00:00"),
			current=("2026-08-18 09:00:00", "2026-08-18 11:00:00"),
			forecast=("2026-08-18 09:30:00", "2026-08-18 11:30:00"),
			actual=("2026-08-18 09:00:00", "2026-08-18 10:00:00"),
			actual_good=30, actual_scrap=2,
		)
		self.result_two = self._create_result(
			self.run_two, self.commitment_two, planned_qty=50,
			baseline=("2026-08-21 08:00:00", "2026-08-21 10:00:00"),
			current=("2026-08-22 08:00:00", "2026-08-22 10:00:00"),
			forecast=("2026-08-22 09:00:00", "2026-08-22 11:00:00"),
			actual=(None, None), actual_good=0, actual_scrap=0,
			recovery_completion="2026-08-22 11:00:00",
		)
		self.stock_allocation = self._create_stock_allocation()
		self.delivery_plan, self.delivery_plan_detail = self._create_delivery_plan()
		self.delivery_note, self.delivery_allocation = self._create_delivery_allocation()

	def tearDown(self):
		if hasattr(self, "original_v2"):
			frappe.db.set_single_value("APS Settings", "enable_aps_v2", self.original_v2)
			frappe.clear_cache(doctype="APS Settings")

	def test_default_projection_joins_current_owners_from_two_formal_runs(self):
		response = progress_v2.get_progress_detail(
			company=self.company,
			item_code=self.item,
			date_from="2026-08-20",
			date_to="2026-08-21",
			page_length=100,
		)
		self.assertEqual(response["projection"]["type"], "Effective Cross-Run")
		self.assertEqual(set(response["projection"]["run_names"]), {self.run_one.name, self.run_two.name})
		self.assertEqual(response["pagination"]["total_rows"], 2)
		rows = {row["demand_identity"]: row for row in response["rows"]}
		first = rows[self.schedule_items[0].demand_identity]
		second = rows[self.schedule_items[1].demand_identity]
		self.assertEqual(first["commitment_names"], [self.commitment_one.name])
		self.assertEqual(second["commitment_names"], [self.commitment_two.name])
		self.assertEqual(first["original_plan_qty"], 70)
		self.assertEqual(first["current_plan_qty"], 70)
		self.assertEqual(first["forecast_qty"], 70)
		self.assertEqual(first["actual_good_qty"], 30)
		self.assertEqual(first["actual_scrap_qty"], 2)
		self.assertEqual(first["delivery_plan_qty"], 90)
		self.assertEqual(first["delivered_qty"], 20)
		self.assertEqual(first["stock_covered_qty"], 10)
		self.assertEqual(first["conservation_status"], "OK")
		self.assertEqual(first["status"], "On Track")
		self.assertEqual(second["recovery_qty"], 20)
		self.assertEqual(second["recovery_completion_time"].strftime("%Y-%m-%d %H:%M:%S"), "2026-08-22 11:00:00")
		self.assertEqual(second["status"], "Late")

	def test_explicit_run_is_clearly_single_run_and_does_not_borrow_other_owner(self):
		response = progress_v2.get_progress_detail(
			company=self.company,
			item_code=self.item,
			run_name=self.run_one.name,
			date_from="2026-08-20",
			date_to="2026-08-21",
		)
		self.assertEqual(response["projection"]["type"], "Single Run")
		self.assertEqual(response["projection"]["selected_run"], self.run_one.name)
		rows = {row["demand_identity"]: row for row in response["rows"]}
		self.assertEqual(rows[self.schedule_items[0].demand_identity]["commitment_names"], [self.commitment_one.name])
		self.assertEqual(rows[self.schedule_items[1].demand_identity]["commitment_names"], [])

	def test_matrix_and_drilldown_retain_exact_source_documents(self):
		matrix = progress_v2.get_progress_matrix_data(
			company=self.company,
			item_code=self.item,
			date_from="2026-08-18",
			date_to="2026-08-22",
			column_limit=5,
		)
		self.assertEqual(matrix["matrix"]["dates"], [
			"2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22",
		])
		row = next(candidate for candidate in matrix["rows"] if candidate["demand_identity"] == self.schedule_items[0].demand_identity)
		self.assertEqual(row["cells"]["2026-08-18"]["actual_good_qty"], 30)
		self.assertEqual(row["cells"]["2026-08-20"]["schedule_qty"], 100)
		detail = progress_v2.get_progress_cell(
			date_value="2026-08-20",
			demand_identity=self.schedule_items[0].demand_identity,
			schedule_item=self.schedule_items[0].name,
		)
		source_pairs = {(row["doctype"], row["name"]) for row in detail["row"]["source_documents"]}
		self.assertIn(("APS Demand Commitment", self.commitment_one.name), source_pairs)
		self.assertIn(("APS Schedule Result", self.result_one.name), source_pairs)
		self.assertIn(("APS Stock Coverage Allocation", self.stock_allocation), source_pairs)
		self.assertIn(("Delivery Plan", self.delivery_plan), source_pairs)
		self.assertIn(("Delivery Note", self.delivery_note), source_pairs)

	def test_public_api_dispatches_to_v2_and_flag_off_returns_legacy_shape(self):
		response = api.get_customer_schedule_progress_data(
			company=self.company,
			item_code=self.item,
			date_from="2026-08-20",
			date_to="2026-08-21",
			progress_view="Detail",
		)
		self.assertEqual(response["mode"], "V2")
		self.assertEqual(response["projection"]["type"], "Effective Cross-Run")
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", 0)
		frappe.clear_cache(doctype="APS Settings")
		legacy = api.get_customer_schedule_progress_data(
			company=self.company,
			item_code=self.item,
			date_from="2026-08-20",
			date_to="2026-08-21",
			limit=100,
		)
		self.assertNotEqual(legacy.get("mode"), "V2")
		self.assertIn("selected_run", legacy)
		self.assertIn("rows", legacy)

	def test_phase8_query_indexes_exist_after_idempotent_migration(self):
		expected = {
			"tabCustomer Delivery Schedule Item": "iaps_p8_sched_identity_date",
			"tabAPS Demand Commitment": "iaps_p8_commit_owner",
			"tabAPS Schedule Result": "iaps_p8_result_commit_run",
			"tabAPS Stock Coverage Allocation": "iaps_p8_stock_identity_status",
			"tabAPS Delivery Allocation": "iaps_p8_delivery_identity",
		}
		for table, index_name in expected.items():
			with self.subTest(table=table, index=index_name):
				indexes = {
					row.Key_name for row in frappe.db.sql(f"show index from `{table}`", as_dict=True)
				}
				self.assertIn(index_name, indexes)

	def _create_item(self):
		name = f"APS-V2-P8-{frappe.generate_hash(length=10)}"
		item = frappe.new_doc("Item")
		item.name = name
		item.item_code = name
		item.item_name = name
		item.item_group = frappe.db.get_value("Item Group", {"is_group": 0})
		item.stock_uom = frappe.db.get_value("UOM", {})
		item.is_stock_item = 1
		item.disabled = 0
		item.db_insert()
		return item.name

	def _create_schedule(self):
		schedule = frappe.get_doc({
			"doctype": "Customer Delivery Schedule",
			"customer": self.customer,
			"company": self.company,
			"schedule_scope": f"APS-V2-P8-{frappe.generate_hash(length=10)}",
			"version_no": "V1",
			"import_strategy": "Replace Scope",
			"source_type": "Customer Delivery Schedule",
			"status": "Active",
			"items": [
				{"item_code": self.item, "schedule_date": "2026-08-20", "qty": 100, "status": "Open", "source_origin": "manual_added", "external_line_reference": "P8-ONE"},
				{"item_code": self.item, "schedule_date": "2026-08-21", "qty": 50, "status": "Open", "source_origin": "manual_added", "external_line_reference": "P8-TWO"},
			],
		})
		schedule.flags.aps_schedule_import_transition = True
		schedule.insert(ignore_permissions=True)
		schedule_revision.backfill_active_demand_identities()
		rows = frappe.get_all(
			"Customer Delivery Schedule Item",
			filters={"parent": schedule.name},
			fields=["name", "demand_identity", "schedule_date", "qty", "parent"],
			order_by="schedule_date asc",
		)
		return schedule.name, rows

	def _create_run(self, note):
		return frappe.get_doc({
			"doctype": "APS Planning Run", "company": self.company, "plant_floor": self.plant_floor,
			"planning_date": "2026-08-14", "horizon_days": 14,
			"horizon_start": "2026-08-14 00:00:00", "horizon_end": "2026-08-27 23:59:59",
			"run_type": "Formal", "existing_work_order_policy": "Exclude",
			"status": "Applied", "approval_state": "Approved", "notes": note,
		}).insert(ignore_permissions=True)

	def _create_commitment(self, run, schedule_item, *, requested_qty, stock_qty, new_plan_qty, on_time_qty, late_qty):
		doc = frappe.get_doc({
			"doctype": "APS Demand Commitment", "planning_run": run.name,
			"company": self.company, "customer": self.customer, "item_code": self.item,
			"demand_identity": schedule_item.demand_identity, "schedule_item": schedule_item.name,
			"admission_class": "P0", "owner_state": "Owned", "execution_state": "Reschedulable",
			"status": "Approved", "formal_owner": 1,
			"original_due_date": schedule_item.schedule_date,
			"effective_due_time": f"{schedule_item.schedule_date} 23:59:59",
			"requested_qty": requested_qty, "stock_covered_qty": stock_qty,
			"carried_qty": 0, "newly_planned_qty": new_plan_qty,
			"on_time_qty": on_time_qty, "late_qty": late_qty, "unscheduled_qty": 0,
			"remaining_qty": requested_qty, "input_fingerprint": frappe.generate_hash(length=32),
			"ownership_fingerprint": frappe.generate_hash(length=32),
			"idempotency_key": frappe.generate_hash(length=32),
			"transition_reason": "Phase 8 integration owner",
			"transitioned_by": frappe.session.user, "transitioned_on": now_datetime(),
		})
		doc.flags.aps_phase2_transition = True
		doc.insert(ignore_permissions=True)
		return doc

	def _create_result(self, run, commitment, *, planned_qty, baseline, current, forecast, actual, actual_good, actual_scrap, recovery_completion=None):
		segment = {
			"workstation": self.workstation, "plant_floor": self.plant_floor,
			"start_time": current[0], "end_time": current[1],
			"baseline_start_time": baseline[0], "baseline_end_time": baseline[1],
			"solver_start_time": baseline[0], "solver_end_time": baseline[1],
			"current_start_time": current[0], "current_end_time": current[1],
			"forecast_start_time": forecast[0], "forecast_end_time": forecast[1],
			"actual_start_time": actual[0], "actual_end_time": actual[1],
			"actual_good_qty": actual_good, "actual_scrap_qty": actual_scrap,
			"last_actual_report_time": actual[1], "planned_qty": planned_qty,
			"segment_kind": "Primary", "segment_status": "Applied",
		}
		doc = frappe.get_doc({
			"doctype": "APS Schedule Result", "planning_run": run.name,
			"company": self.company, "plant_floor": self.plant_floor,
			"customer": self.customer, "item_code": self.item,
			"demand_commitment": commitment.name, "requested_date": commitment.original_due_date,
			"effective_due_time": commitment.effective_due_time,
			"demand_source": "Customer Delivery Schedule", "production_strategy": "Auto Balance",
			"planned_qty": planned_qty, "on_time_qty": commitment.on_time_qty,
			"recovery_qty": commitment.late_qty, "critical_unplanned_qty": 0,
			"projected_completion_time": forecast[1], "recovery_completion_time": recovery_completion,
			"status": "Applied", "risk_status": "Attention" if commitment.late_qty else "Normal",
			"good_produced_qty": actual_good, "scrap_qty": actual_scrap,
			"actual_start_time": actual[0], "actual_end_time": actual[1],
			"last_actual_report_time": actual[1], "segments": [segment],
		})
		doc.flags.aps_result_engine_transition = True
		doc.insert(ignore_permissions=True)
		return doc

	def _create_stock_allocation(self):
		doc = frappe.get_doc({
			"doctype": "APS Stock Coverage Allocation", "company": self.company,
			"item_code": self.item, "warehouse": self.warehouse,
			"demand_identity": self.schedule_items[0].demand_identity,
			"commitment": self.commitment_one.name, "owner_run": self.run_one.name,
			"allocated_qty": 10, "consumed_qty": 0, "released_qty": 0,
			"status": "Active", "source_snapshot_time": now_datetime(),
			"idempotency_key": frappe.generate_hash(length=32),
		})
		doc.flags.aps_phase2_transition = True
		doc.insert(ignore_permissions=True)
		return doc.name

	def _create_delivery_plan(self):
		plan = frappe.new_doc("Delivery Plan")
		plan.name = f"APS-V2-P8-DP-{frappe.generate_hash(length=10)}"
		plan.customer = self.customer
		plan.company = self.company
		plan.delivery_date = "2026-08-20"
		plan.arrival_date = "2026-08-20"
		plan.db_insert()
		row = frappe.new_doc("Delivery Plan Item Qty")
		row.name = f"APS-V2-P8-DPQ-{frappe.generate_hash(length=10)}"
		row.parent = plan.name
		row.parenttype = "Delivery Plan"
		row.parentfield = "item_qties"
		row.idx = 1
		row.item_code = self.item
		row.uom = frappe.db.get_value("Item", self.item, "stock_uom")
		row.required_arrival_date = "2026-08-20"
		row.planned_delivery_qty = 90
		row.custom_aps_demand_identity = self.schedule_items[0].demand_identity
		row.custom_aps_customer_schedule_item = self.schedule_items[0].name
		row.db_insert()
		return plan.name, row.name

	def _create_delivery_allocation(self):
		delivery_note = frappe.new_doc("Delivery Note")
		delivery_note.name = f"APS-V2-P8-DN-{frappe.generate_hash(length=10)}"
		delivery_note.docstatus = 1
		delivery_note.company = self.company
		delivery_note.customer = self.customer
		delivery_note.posting_date = "2026-08-19"
		delivery_note.posting_time = "12:00:00"
		delivery_note.db_insert()
		delivery_item = frappe.new_doc("Delivery Note Item")
		delivery_item.name = frappe.generate_hash(length=10)
		delivery_item.parent = delivery_note.name
		delivery_item.parenttype = "Delivery Note"
		delivery_item.parentfield = "items"
		delivery_item.idx = 1
		delivery_item.item_code = self.item
		delivery_item.qty = 20
		delivery_item.stock_qty = 20
		delivery_item.conversion_factor = 1
		delivery_item.db_insert()
		allocation = frappe.get_doc({
			"doctype": "APS Delivery Allocation", "allocation_key": frappe.generate_hash(length=32),
			"company": self.company, "customer": self.customer, "item_code": self.item,
			"schedule_date": "2026-08-20", "customer_schedule": self.schedule,
			"customer_schedule_item": self.schedule_items[0].name,
			"demand_identity": self.schedule_items[0].demand_identity,
			"delivery_plan_detail": self.delivery_plan_detail,
			"match_status": "Delivery Plan", "match_reason": "Phase 8 exact lineage",
			"source_delivery_note": delivery_note.name, "source_delivery_note_item": delivery_item.name,
			"source_docstatus": 1, "source_posting_time": "2026-08-19 12:00:00",
			"allocation_method": "Delivery Plan", "source_qty": 20,
			"allocated_qty": 20, "effective_qty": 20, "cumulative_delivered_qty": 20,
			"reversed_qty": 0, "is_effective": 1, "last_synced_on": now_datetime(),
		})
		allocation.insert(ignore_permissions=True)
		return delivery_note.name, allocation.name


if __name__ == "__main__":
	import unittest

	unittest.main()
