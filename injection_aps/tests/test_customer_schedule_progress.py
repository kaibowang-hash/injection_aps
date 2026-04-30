from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, now_datetime, today

from injection_aps.services import planning


class TestCustomerScheduleProgress(FrappeTestCase):
	def setUp(self):
		required_doctypes = [
			"Customer Delivery Schedule",
			"APS Planning Run",
			"APS Schedule Result",
			"APS Schedule Segment",
		]
		missing_doctypes = [doctype for doctype in required_doctypes if not frappe.db.exists("DocType", doctype)]
		if missing_doctypes:
			self.skipTest("Injection APS DocTypes are not synced in this test database.")
		self.company = frappe.db.get_value("Company", {})
		if not self.company:
			self.skipTest("Customer schedule progress tests need at least one Company.")
		self.customer_a = f"APS Progress Customer A {frappe.generate_hash(length=6)}"
		self.customer_b = f"APS Progress Customer B {frappe.generate_hash(length=6)}"
		self.item = f"APS-PROGRESS-{frappe.generate_hash(length=6)}"
		self.co_product_item = f"APS-PROGRESS-CO-{frappe.generate_hash(length=6)}"
		self.workstation = f"APS Progress Workstation {frappe.generate_hash(length=6)}"
		self.scope = f"Progress Scope {frappe.generate_hash(length=6)}"

	def test_stock_allocation_uses_company_item_timeline_before_customer_filter(self):
		early_schedule = self._create_schedule(
			customer=self.customer_b,
			item_code=self.item,
			schedule_date=add_days(today(), 1),
			qty=60,
		)
		late_schedule = self._create_schedule(
			customer=self.customer_a,
			item_code=self.item,
			schedule_date=add_days(today(), 2),
			qty=60,
		)

		with patch("injection_aps.services.planning._get_available_stock_map", return_value={self.item: 60}):
			data = planning.get_customer_schedule_progress_data(
				company=self.company,
				customer=self.customer_a,
				item_code=self.item,
				run_name=None,
			)

		assert len(data["rows"]) == 1
		row = data["rows"][0]
		assert row["schedule"] == late_schedule.name
		assert row["stock_covered_qty"] == 0
		assert row["uncovered_qty"] == 60

		with patch("injection_aps.services.planning._get_available_stock_map", return_value={self.item: 60}):
			all_rows = planning.get_customer_schedule_progress_data(company=self.company, item_code=self.item)["rows"]
		early_row = next(row for row in all_rows if row["schedule"] == early_schedule.name)
		assert early_row["stock_covered_qty"] == 60

	def test_auto_run_prefers_status_priority_with_existing_results(self):
		self._create_schedule(
			customer=self.customer_a,
			item_code=self.item,
			schedule_date=add_days(today(), 3),
			qty=10,
		)
		applied_run = self._create_run(status="Applied")
		planned_run = self._create_run(status="Planned")
		self._create_result(applied_run, self.item, add_days(today(), 2), 10)
		self._create_result(planned_run, self.item, add_days(today(), 2), 10)

		with patch("injection_aps.services.planning._get_available_stock_map", return_value={}):
			data = planning.get_customer_schedule_progress_data(company=self.company, item_code=self.item)

		assert data["selected_run"]["name"] == applied_run.name

	def test_planned_segment_marks_schedule_on_track_or_late(self):
		on_time_schedule = self._create_schedule(
			customer=self.customer_a,
			item_code=self.item,
			schedule_date=add_days(today(), 5),
			qty=100,
		)
		late_schedule = self._create_schedule(
			customer=self.customer_a,
			item_code=self.co_product_item,
			schedule_date=add_days(today(), 1),
			qty=100,
		)
		run = self._create_run(status="Planned")
		self._create_result(
			run,
			self.item,
			add_days(today(), 5),
			100,
			segment_end=get_datetime(f"{add_days(today(), 3)} 10:00:00"),
		)
		self._create_result(
			run,
			self.co_product_item,
			add_days(today(), 1),
			100,
			segment_end=get_datetime(f"{add_days(today(), 2)} 10:00:00"),
		)

		with patch("injection_aps.services.planning._get_available_stock_map", return_value={}):
			rows = planning.get_customer_schedule_progress_data(company=self.company, run_name=run.name)["rows"]

		on_time_row = next(row for row in rows if row["schedule"] == on_time_schedule.name)
		late_row = next(row for row in rows if row["schedule"] == late_schedule.name)
		assert on_time_row["status"] == "On Track"
		assert late_row["status"] == "Late"

	def test_actual_completion_overrides_future_plan_projection(self):
		self._create_schedule(
			customer=self.customer_a,
			item_code=self.item,
			schedule_date=add_days(today(), 5),
			qty=100,
		)
		run = self._create_run(status="Planned")
		result = self._create_result(
			run,
			self.item,
			add_days(today(), 5),
			100,
			segment_end=get_datetime(f"{add_days(today(), 4)} 18:00:00"),
		)
		segment_name = frappe.db.get_value("APS Schedule Segment", {"parent": result.name}, "name")
		actual_time = now_datetime()

		with (
			patch("injection_aps.services.planning._get_available_stock_map", return_value={}),
			patch(
				"injection_aps.services.planning._get_customer_schedule_progress_execution_snapshots",
				return_value={
					segment_name: {
						"linked_work_order": None,
						"linked_work_order_scheduling": None,
						"linked_scheduling_item": None,
						"actual_completed_qty": 100,
						"actual_start_time": actual_time,
						"actual_end_time": actual_time,
						"delay_minutes": 0,
						"actual_status": "Completed",
					}
				},
			),
		):
			row = planning.get_customer_schedule_progress_data(
				company=self.company,
				item_code=self.item,
				run_name=run.name,
			)["rows"][0]

		assert row["production_covered_qty"] == 100
		assert get_datetime(row["projected_completion_time"]) == get_datetime(actual_time)

	def test_family_co_product_segment_covers_co_product_schedule(self):
		self._create_schedule(
			customer=self.customer_a,
			item_code=self.co_product_item,
			schedule_date=add_days(today(), 4),
			qty=50,
		)
		run = self._create_run(status="Planned")
		self._create_result(
			run,
			self.item,
			add_days(today(), 4),
			50,
			segment_kind="Family Co-Product",
			co_product_item_code=self.co_product_item,
			segment_end=get_datetime(f"{add_days(today(), 3)} 10:00:00"),
		)

		with patch("injection_aps.services.planning._get_available_stock_map", return_value={}):
			row = planning.get_customer_schedule_progress_data(
				company=self.company,
				item_code=self.co_product_item,
				run_name=run.name,
			)["rows"][0]

		assert row["production_covered_qty"] == 50
		assert row["status"] == "On Track"

	def test_api_requires_read_access_only(self):
		from injection_aps.api import app

		with (
			patch("injection_aps.api.app.require_any_role") as require_any_role,
			patch("injection_aps.api.app.planning.get_customer_schedule_progress_data", return_value={"rows": []}) as service,
		):
			data = app.get_customer_schedule_progress_data(company=self.company, item_code=self.item, limit=1)

		assert data == {"rows": []}
		assert require_any_role.call_count == 1
		assert require_any_role.call_args.args[0] == app.APS_READ_ROLES
		service.assert_called_once_with(
			company=self.company,
			customer=None,
			item_code=self.item,
			schedule_scope=None,
			date_from=None,
			date_to=None,
			status=None,
			run_name=None,
			limit=1,
		)

	def _create_schedule(self, customer: str, item_code: str, schedule_date, qty: float):
		doc = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": customer,
				"company": self.company,
				"schedule_scope": f"{self.scope}-{frappe.generate_hash(length=6)}",
				"version_no": f"V-{frappe.generate_hash(length=6)}",
				"status": "Active",
				"items": [
					{
						"item_code": item_code,
						"schedule_date": schedule_date,
						"qty": qty,
						"delivered_qty": 0,
						"balance_qty": qty,
						"status": "Open",
					}
				],
			}
		)
		self._insert_doc(doc)
		return doc

	def _create_run(self, status: str):
		doc = frappe.get_doc(
			{
				"doctype": "APS Planning Run",
				"company": self.company,
				"planning_date": today(),
				"run_type": "Trial",
				"status": status,
				"approval_state": "Approved",
			}
		)
		self._insert_doc(doc)
		return doc

	def _create_result(
		self,
		run,
		item_code: str,
		requested_date,
		qty: float,
		segment_end=None,
		segment_kind: str = "Primary",
		co_product_item_code: str | None = None,
	):
		segment_end = segment_end or get_datetime(f"{add_days(today(), 2)} 10:00:00")
		segment_start = get_datetime(f"{add_days(today(), 1)} 10:00:00")
		doc = frappe.get_doc(
			{
				"doctype": "APS Schedule Result",
				"planning_run": run.name,
				"company": self.company,
				"customer": self.customer_a,
				"item_code": item_code,
				"requested_date": requested_date,
				"demand_source": "Customer Delivery Schedule",
				"planned_qty": qty,
				"scheduled_qty": qty,
				"status": "Planned",
				"risk_status": "Normal",
				"segments": [
					{
						"workstation": self.workstation,
						"start_time": segment_start,
						"end_time": segment_end,
						"planned_qty": qty,
						"segment_kind": segment_kind,
						"primary_item_code": item_code,
						"co_product_item_code": co_product_item_code,
						"segment_status": "Planned",
					}
				],
			}
		)
		self._insert_doc(doc)
		return doc

	def _insert_doc(self, doc):
		previous = getattr(frappe.flags, "in_install", None)
		frappe.flags.in_install = "frappe"
		try:
			return doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
		finally:
			frappe.flags.in_install = previous
