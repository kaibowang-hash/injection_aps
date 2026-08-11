from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, today

from injection_aps.services import planning


class TestScheduleImportSafety(FrappeTestCase):
	def setUp(self):
		required_doctypes = [
			"APS Demand Delta",
			"APS Schedule Import Batch",
			"Customer Delivery Schedule",
		]
		if any(not frappe.db.exists("DocType", doctype) for doctype in required_doctypes):
			self.skipTest("Schedule import safety DocTypes are not synced.")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		self.item = frappe.db.get_value("Item", {"disabled": 0}) or frappe.db.get_value("Item", {})
		if not self.company or not self.customer or not self.item:
			self.skipTest("Schedule import safety tests need a Company, Customer, and Item.")
		self.scope = f"APS-P2-{frappe.generate_hash(length=8)}"
		self.schedule_date = getdate(add_days(today(), 2))

	def test_duplicate_rows_block_by_default_and_sum_only_when_explicit(self):
		rows = [
			self._row(100, source_excel_row=7),
			self._row(200, source_excel_row=11),
		]
		blocked = self._preview(rows, duplicate_policy="Block")

		self.assertFalse(blocked["can_import"])
		self.assertEqual(blocked["duplicate_groups"][0]["excel_rows"], [7, 11])
		self.assertEqual(blocked["duplicate_groups"][0]["quantities"], [100, 200])
		self.assertTrue(any(check["blocking"] for check in blocked["checks"]))

		summed = self._preview(rows, duplicate_policy="Sum")
		self.assertTrue(summed["can_import"])
		self.assertEqual(summed["incoming_total_qty"], 300)
		self.assertEqual(summed["post_import_total_qty"], 300)
		self.assertEqual(len(summed["effective_schedule_rows"]), 1)
		self.assertEqual(summed["effective_schedule_rows"][0]["source_excel_rows"], "7, 11")

	def test_zero_quantity_is_an_explicit_cancellation_with_execution_impact(self):
		self._create_active_schedule([self._row(500, produced_qty=120, delivered_qty=0)])
		frozen_key = (self.item, str(self.schedule_date))
		preview = self._preview(
			[self._row(0, source_excel_row=9)],
			import_strategy="Partial Update",
			frozen_qty={frozen_key: 80},
		)

		row = next(row for row in preview["rows"] if row["item_code"] == self.item)
		self.assertTrue(preview["can_import"])
		self.assertEqual(row["previous_qty"], 500)
		self.assertEqual(row["new_qty"], 0)
		self.assertEqual(row["delta_qty"], -500)
		self.assertEqual(row["change_type"], "Cancelled")
		self.assertEqual(row["produced_qty"], 120)
		self.assertEqual(row["delivered_qty"], 0)
		self.assertEqual(row["frozen_qty"], 80)
		self.assertTrue(row["affects_produced"])
		self.assertFalse(row["affects_delivered"])
		self.assertTrue(row["affects_frozen"])

		result = self._import([self._row(0, source_excel_row=9)], import_strategy="Partial Update")
		item = frappe.db.get_value(
			"Customer Delivery Schedule Item",
			{"parent": result["schedule"], "item_code": self.item},
			["qty", "produced_qty", "delivered_qty", "status"],
			as_dict=True,
		)
		self.assertEqual(item.qty, 0)
		self.assertEqual(item.produced_qty, 120)
		self.assertEqual(item.delivered_qty, 0)
		self.assertEqual(item.status, "Cancelled")

	def test_quantity_cannot_be_reduced_below_delivered_lower_bound(self):
		self._create_active_schedule([self._row(500, produced_qty=120, delivered_qty=40)])
		preview = self._preview(
			[self._row(0, source_excel_row=9)],
			import_strategy="Partial Update",
		)

		self.assertFalse(preview["can_import"])
		lower_bound = next(check for check in preview["checks"] if check["title"] == "Delivered quantity lower bound")
		self.assertTrue(lower_bound["blocking"])
		self.assertEqual(lower_bound["status"], "failed")

	def test_append_shows_post_total_and_reimport_is_idempotent(self):
		self._create_active_schedule([self._row(100)])
		rows = [self._row(25, source_excel_row=5)]
		preview = self._preview(rows, import_strategy="Append")

		self.assertTrue(preview["can_import"])
		self.assertEqual(preview["previous_total_qty"], 100)
		self.assertEqual(preview["post_import_total_qty"], 125)
		self.assertEqual(preview["total_delta_qty"], 25)
		self.assertEqual(preview["rows"][0]["previous_qty"], 100)
		self.assertEqual(preview["rows"][0]["new_qty"], 125)
		self.assertEqual(preview["rows"][0]["import_qty"], 25)
		self.assertEqual(preview["rows"][0]["change_type"], "Appended")

		first = self._import(rows, import_strategy="Append")
		second = self._import(rows, import_strategy="Append", version_no="V2")
		self.assertFalse(first["idempotent_replay"])
		self.assertTrue(second["idempotent_replay"])
		self.assertEqual(second["import_batch"], first["import_batch"])
		self.assertEqual(second["schedule"], first["schedule"])
		self.assertEqual(
			frappe.db.count("APS Schedule Import Batch", {"schedule_scope": self.scope}),
			1,
		)
		self.assertEqual(
			sum(
				row.qty
				for row in frappe.get_all(
					"Customer Delivery Schedule Item",
					filters={
						"parent": (
							"in",
							frappe.get_all(
								"Customer Delivery Schedule",
								filters={"schedule_scope": self.scope, "status": "Active"},
								pluck="name",
							),
						),
					},
					fields=["qty"],
				)
			),
			125,
		)

	def test_replace_cancels_omitted_rows_and_partial_update_retains_them(self):
		second_date = getdate(add_days(self.schedule_date, 1))
		self._create_active_schedule([self._row(100), self._row(200, schedule_date=second_date)])

		replace = self._preview([self._row(100)], import_strategy="Replace Scope")
		cancelled = next(row for row in replace["rows"] if row["schedule_date"] == second_date)
		self.assertEqual(cancelled["change_type"], "Cancelled")
		self.assertEqual(cancelled["delta_qty"], -200)
		self.assertEqual(replace["post_import_total_qty"], 100)

		partial = self._preview([self._row(120)], import_strategy="Partial Update")
		retained = next(row for row in partial["rows"] if row["schedule_date"] == second_date)
		self.assertEqual(retained["change_type"], "Unchanged")
		self.assertEqual(retained["new_qty"], 200)
		self.assertEqual(partial["post_import_total_qty"], 320)

	def test_append_zero_is_blocked(self):
		preview = self._preview([self._row(0, source_excel_row=6)], import_strategy="Append")
		self.assertFalse(preview["can_import"])
		self.assertTrue(any(check["title"] == "Zero quantity semantics" and check["blocking"] for check in preview["checks"]))

	def test_import_and_rebuild_failure_rolls_back_every_write(self):
		active = self._create_active_schedule([self._row(100)])
		before_batches = frappe.db.count("APS Schedule Import Batch", {"schedule_scope": self.scope})
		before_schedules = frappe.db.count("Customer Delivery Schedule", {"schedule_scope": self.scope})

		with patch("injection_aps.services.planning.rebuild_demand_pool", side_effect=RuntimeError("forced rebuild failure")):
			with self.assertRaisesRegex(RuntimeError, "forced rebuild failure"):
				self._import(
					[self._row(150)],
					import_strategy="Replace Scope",
					rebuild=1,
					existing_work_order_policy="Exclude",
				)

		self.assertEqual(frappe.db.count("APS Schedule Import Batch", {"schedule_scope": self.scope}), before_batches)
		self.assertEqual(frappe.db.count("Customer Delivery Schedule", {"schedule_scope": self.scope}), before_schedules)
		self.assertEqual(frappe.db.get_value("Customer Delivery Schedule", active.name, "status"), "Active")

	def test_matrix_parser_preserves_zero_and_real_excel_row_number(self):
		with patch(
			"injection_aps.services.planning._read_schedule_workbook_rows",
			return_value=(
				[["Item", str(self.schedule_date)], [self.item, 0]],
				{"sheet_name": "Schedule", "sheet_names": ["Schedule"]},
			),
		):
			rows, _context = planning._normalize_schedule_rows_from_matrix(
				file_url="/private/files/phase2-zero.xlsx",
				mapping={"parser_mode": "matrix", "item_reference_column": "A"},
			)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["qty"], 0)
		self.assertEqual(rows[0]["source_excel_row"], 2)

	def test_combined_import_and_rebuild_requires_both_permissions(self):
		from injection_aps.api import app

		with (
			patch("injection_aps.api.app._require_demand_access") as require_demand,
			patch("injection_aps.api.app._require_scope_access") as require_scope,
			patch("injection_aps.api.app._require_plan_access") as require_plan,
			patch("injection_aps.api.app.planning.import_customer_delivery_schedule", return_value={}) as service,
		):
			app.import_customer_delivery_schedule(
				customer=self.customer,
				company=self.company,
				version_no="V1",
				rebuild=1,
				existing_work_order_policy="Exclude",
			)

		require_demand.assert_called_once_with()
		require_scope.assert_called_once_with(company=self.company, customer=self.customer)
		require_plan.assert_called_once_with()
		self.assertEqual(service.call_args.kwargs["rebuild"], 1)
		self.assertEqual(service.call_args.kwargs["existing_work_order_policy"], "Exclude")

	def _row(self, qty, schedule_date=None, source_excel_row=2, produced_qty=0, delivered_qty=0):
		return {
			"sales_order": "",
			"item_code": self.item,
			"customer_part_no": "APS-P2-PART",
			"schedule_date": schedule_date or self.schedule_date,
			"qty": qty,
			"produced_qty": produced_qty,
			"delivered_qty": delivered_qty,
			"balance_qty": max(qty - delivered_qty, 0),
			"source_excel_row": source_excel_row,
		}

	def _preview(self, rows, import_strategy="Replace Scope", duplicate_policy="Block", frozen_qty=None):
		with patch("injection_aps.services.planning._get_schedule_frozen_qty", return_value=frozen_qty or {}):
			return planning.preview_customer_delivery_schedule(
				customer=self.customer,
				company=self.company,
				version_no="V1",
				schedule_scope=self.scope,
				import_strategy=import_strategy,
				duplicate_policy=duplicate_policy,
				rows_json=rows,
			)

	def _import(
		self,
		rows,
		import_strategy="Replace Scope",
		duplicate_policy="Block",
		version_no="V1",
		rebuild=0,
		existing_work_order_policy=None,
	):
		with patch("injection_aps.services.planning._get_schedule_frozen_qty", return_value={}):
			return planning.import_customer_delivery_schedule(
				customer=self.customer,
				company=self.company,
				version_no=version_no,
				schedule_scope=self.scope,
				import_strategy=import_strategy,
				duplicate_policy=duplicate_policy,
				rows_json=rows,
				rebuild=rebuild,
				existing_work_order_policy=existing_work_order_policy,
			)

	def _create_active_schedule(self, rows):
		doc = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": self.customer,
				"company": self.company,
				"schedule_scope": self.scope,
				"version_no": "BASE",
				"import_strategy": "Replace Scope",
				"status": "Active",
				"items": [
					{
						**row,
						"status": "Open" if row["qty"] > row.get("delivered_qty", 0) else "Covered",
					}
					for row in rows
				],
			}
		)
		return doc.insert(ignore_permissions=True)
