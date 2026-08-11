from __future__ import annotations

import json
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.api import app
from injection_aps.patches.v0_0_2 import backfill_existing_work_order_policy
from injection_aps.services import planning


class _InsertedNetRequirement:
	def __init__(self, values, index):
		self.values = values
		self.name = f"APS-NET-TEST-{index}"

	def insert(self, ignore_permissions=False):
		return self


class TestExistingWorkOrderPolicy(TestCase):
	def test_backlog_lineage_freezes_exact_sales_order_item_delivery_baseline(self):
		rows = [
			frappe._dict(
				name="DEMAND-1",
				demand_source="Sales Order Backlog",
				source_doctype="Sales Order",
				source_name="SO-1",
				source_detail_name="SOI-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				item_code="FG-1",
				qty=60,
			)
		]
		with patch.object(
			planning.frappe,
			"get_all",
			return_value=[
				frappe._dict(
					name="SOI-1",
					parent="SO-1",
					item_code="FG-1",
					qty=100,
					delivered_qty=40,
				)
			],
		):
			source_json, baseline_json = planning._build_net_requirement_lineage_snapshot(
				rows,
				demand_qty=60,
				available_stock_qty=15,
				open_work_order_qty=20,
				existing_work_order_policy="Include",
				safety_stock_gap_qty=0,
				minimum_batch_qty=0,
				minimum_batch_coverage_qty=0,
				net_requirement_qty=25,
				planning_qty=25,
				new_batch_surplus_qty=0,
				is_safety_stock_group=0,
			)
		self.assertEqual(json.loads(source_json)[0]["sales_order_item"], "SOI-1")
		baseline = json.loads(baseline_json)
		self.assertEqual(baseline["version"], 4)
		self.assertEqual(
			baseline["net_requirement"],
			{
				"formula_version": 1,
				"demand_qty": 60.0,
				"available_stock_qty": 15.0,
				"open_work_order_qty": 20.0,
				"existing_work_order_policy": "Include",
				"safety_stock_gap_qty": 0.0,
				"minimum_batch_qty": 0.0,
				"minimum_batch_coverage_qty": 0.0,
				"base_residual_qty": 25.0,
				"net_requirement_qty": 25.0,
				"planning_qty": 25.0,
				"new_batch_surplus_qty": 0.0,
				"is_safety_stock_group": 0,
			},
		)
		self.assertEqual(
			baseline["sales_order_items"],
			[
				{
					"item_code": "FG-1",
					"opening_delivered_qty": 40.0,
					"opening_ordered_qty": 100.0,
					"sales_order": "SO-1",
					"sales_order_item": "SOI-1",
					"source_open_qty": 60.0,
				}
			],
		)

	def test_missing_policy_is_rejected_before_rebuild_side_effects(self):
		with (
			patch("injection_aps.services.planning.repair_item_references") as repair,
			patch("injection_aps.services.planning._delete_system_generated_rows") as delete_rows,
		):
			with self.assertRaises(frappe.ValidationError):
				planning.rebuild_net_requirements(company="Test Company")

		repair.assert_not_called()
		delete_rows.assert_not_called()

	def test_invalid_policy_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			planning._normalize_existing_work_order_policy("Default")

	def test_all_calculation_services_require_policy_before_side_effects(self):
		with patch("injection_aps.services.planning.get_settings_dict") as get_settings:
			with self.assertRaises(frappe.ValidationError):
				planning.run_planning_run()
		get_settings.assert_not_called()

		with patch("injection_aps.services.planning.rebuild_demand_pool") as rebuild_demand:
			with self.assertRaises(frappe.ValidationError):
				planning.promote_schedule_import_to_net_requirement()
		rebuild_demand.assert_not_called()

		with patch("injection_aps.services.planning.run_planning_run") as run:
			with self.assertRaises(frappe.ValidationError):
				planning.create_trial_run_from_net_requirement_context()
		run.assert_not_called()

	def test_include_policy_allocates_open_work_orders_once_in_date_order(self):
		result, rows, get_work_orders = self._run_rebuild("Include")

		self.assertEqual(result["existing_work_order_policy"], "Include")
		self.assertEqual([row["open_work_order_qty"] for row in rows], [5, 7])
		self.assertEqual([row["net_requirement_qty"] for row in rows], [0, 3])
		self.assertTrue(all(row["existing_work_order_policy"] == "Include" for row in rows))
		self.assertIn("included existing open work orders", rows[0]["reason_text"])
		get_work_orders.assert_called_once_with("Test Company")

	def test_exclude_policy_skips_work_order_query_and_deduction(self):
		result, rows, get_work_orders = self._run_rebuild("Exclude")

		self.assertEqual(result["existing_work_order_policy"], "Exclude")
		self.assertEqual([row["open_work_order_qty"] for row in rows], [0, 0])
		self.assertEqual([row["net_requirement_qty"] for row in rows], [5, 10])
		self.assertTrue(all(row["existing_work_order_policy"] == "Exclude" for row in rows))
		self.assertIn("explicitly excluded", rows[0]["reason_text"])
		get_work_orders.assert_not_called()

	def test_safety_stock_floor_adds_shortfall_without_reusing_customer_stock(self):
		_result, rows, _get_work_orders = self._run_rebuild(
			"Exclude",
			demand_qtys=[100],
			stock_qty=5,
			safety_stock_qty=20,
		)
		self.assertEqual(rows[0]["available_stock_qty"], 0)
		self.assertEqual(rows[0]["safety_stock_gap_qty"], 15)
		self.assertEqual(rows[0]["planning_qty"], 115)

	def test_stock_above_safety_floor_offsets_only_the_customer_excess(self):
		_result, rows, _get_work_orders = self._run_rebuild(
			"Exclude",
			demand_qtys=[40],
			stock_qty=50,
			safety_stock_qty=20,
		)
		self.assertEqual(rows[0]["available_stock_qty"], 30)
		self.assertEqual(rows[0]["safety_stock_gap_qty"], 0)
		self.assertEqual(rows[0]["planning_qty"], 10)

	def test_customer_minimum_batch_surplus_covers_explicit_safety_row_once(self):
		_result, rows, _get_work_orders = self._run_rebuild(
			"Exclude",
			demand_qtys=[40],
			stock_qty=10,
			safety_stock_qty=20,
			minimum_batch_qty=100,
			safety_demand_qty=10,
		)
		customer_row = next(row for row in rows if row["customer"] == "Customer A")
		safety_row = next(row for row in rows if not row["customer"])

		self.assertEqual(customer_row["net_requirement_qty"], 40)
		self.assertEqual(customer_row["planning_qty"], 100)
		self.assertEqual(safety_row["net_requirement_qty"], 0)
		self.assertEqual(safety_row["planning_qty"], 0)
		self.assertEqual(sum(row["planning_qty"] for row in rows), 100)
		self.assertIn("minimum-batch surplus covers 10", safety_row["reason_text"])

	def test_minimum_batch_surplus_is_reused_only_within_exact_so_item_lineage(self):
		_result, rows, _get_work_orders = self._run_rebuild(
			"Exclude",
			demand_qtys=[10, 10],
			stock_qty=0,
			minimum_batch_qty=100,
		)

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["demand_qty"], 20)
		self.assertEqual([row["net_requirement_qty"] for row in rows], [10, 0])
		self.assertEqual([row["planning_qty"] for row in rows], [100, 0])
		self.assertEqual(sum(row["planning_qty"] for row in rows), 100)
		self.assertEqual(
			[row["demand_pool"] for row in json.loads(rows[0]["demand_source_snapshot_json"])],
			["DEMAND-1", "DEMAND-2"],
		)

	def test_minimum_batch_lot_coverage_is_bounded_and_keeps_each_target_lineage(self):
		_result, rows, _get_work_orders = self._run_rebuild(
			"Exclude",
			demand_qtys=[10] * 12,
			stock_qty=0,
			minimum_batch_qty=100,
		)
		self.assertEqual([index for index, row in enumerate(rows) if row["planning_qty"]], [0, 10])
		self.assertEqual(sum(row["planning_qty"] for row in rows), 200)
		self.assertEqual(len(json.loads(rows[0]["demand_source_snapshot_json"])), 10)
		self.assertEqual(len(json.loads(rows[10]["demand_source_snapshot_json"])), 2)

	def test_minimum_batch_surplus_never_crosses_sales_order_item(self):
		_result, rows, _get_work_orders = self._run_rebuild(
			"Exclude",
			demand_qtys=[10, 10],
			stock_qty=0,
			minimum_batch_qty=100,
			sales_orders=["SO-1", "SO-2"],
			sales_order_items=["SOI-1", "SOI-2"],
		)
		self.assertEqual([row["planning_qty"] for row in rows], [100, 100])

	def test_minimum_batch_owner_persists_all_fully_covered_schedule_targets(self):
		owner_row = frappe._dict(
			name="DEMAND-1",
			source_doctype="Customer Delivery Schedule",
			source_name="SCHEDULE-1",
			source_detail_name="TARGET-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			qty=10,
		)
		covered_row = frappe._dict(
			name="DEMAND-2",
			source_doctype="Customer Delivery Schedule",
			source_name="SCHEDULE-1",
			source_detail_name="TARGET-2",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			qty=10,
		)
		targets = [
			frappe._dict(
				name=f"TARGET-{index}",
				parent="SCHEDULE-1",
				sales_order="SO-1",
				item_code="ITEM-A",
				schedule_date=f"2026-08-{10 + index:02d}",
				qty=10,
				allocated_qty=0,
				produced_qty=0,
				delivered_qty=0,
			)
			for index in (1, 2)
		]
		owner = {
			"name": "NET-1",
			"rows": [owner_row],
			"values": {"demand_qty": 10, "available_stock_qty": 0},
		}
		with (
			patch.object(planning.frappe, "get_all", return_value=targets),
			patch.object(planning.frappe.db, "set_value") as set_value,
		):
			planning._extend_minimum_batch_owner_lineage(owner, [covered_row])
		baseline = json.loads(owner["values"]["fulfillment_baseline_json"])
		self.assertEqual(owner["values"]["demand_qty"], 20)
		self.assertEqual(
			[row["customer_schedule_item"] for row in baseline["targets"]],
			["TARGET-1", "TARGET-2"],
		)
		self.assertEqual(set_value.call_args.args[0:2], ("APS Net Requirement", "NET-1"))

	def test_legacy_allocated_cache_does_not_double_deduct_exact_open_work_order(self):
		row = frappe._dict(qty=100, delivered_qty=0, balance_qty=100, allocated_qty=60)
		self.assertEqual(planning._schedule_row_open_demand_qty(row), 100)
		# The exact submitted WO is deducted once in Net Requirements, leaving 40.
		self.assertEqual(planning._schedule_row_open_demand_qty(row) - 60, 40)
		# Cancelling that WO removes the authoritative 60; stale allocated_qty must
		# not keep the demand suppressed.
		self.assertEqual(planning._schedule_row_open_demand_qty(row), 100)

	def test_stock_only_net_requirement_still_creates_run_evidence(self):
		self.assertTrue(
			planning._net_requirement_requires_result(
				frappe._dict(net_requirement_qty=0, available_stock_qty=25)
			)
		)
		self.assertTrue(
			planning._net_requirement_requires_result(
				frappe._dict(net_requirement_qty=10, available_stock_qty=0)
			)
		)
		self.assertTrue(
			planning._net_requirement_requires_result(
				frappe._dict(
					net_requirement_qty=0,
					available_stock_qty=0,
					open_work_order_qty=0,
					fulfillment_baseline_json=json.dumps(
						{
							"version": 4,
							"net_requirement": {"minimum_batch_coverage_qty": 25},
						}
					),
				)
			)
		)
		self.assertFalse(
			planning._net_requirement_requires_result(
				frappe._dict(net_requirement_qty=0, available_stock_qty=0)
			)
		)

	def test_available_stock_uses_only_enabled_fg_and_deducts_complete_reservations_once(self):
		bin_row = frappe._dict(
			item_code="FG-1",
			actual_qty=100,
			reserved_qty=20,
			reserved_stock=25,
			reserved_qty_for_production=10,
			reserved_qty_for_sub_contract=5,
			reserved_qty_for_production_plan=5,
		)
		with (
			patch.object(planning.frappe.db, "exists", return_value=True),
			patch("injection_aps.services.availability._get_finished_goods_warehouses", return_value=["FG-WH"]),
			patch.object(planning, "_get_aps_sales_order_reservation_credit_map", return_value={"FG-1": 5}),
			patch.object(planning.frappe.db, "sql", return_value=[bin_row]) as sql,
		):
			stock = planning._get_available_stock_map("COMPANY-1", demand_rows=[])
		self.assertEqual(stock, {"FG-1": 60})
		query, params = sql.call_args.args[:2]
		self.assertIn("wh.disabled = 0", query)
		self.assertIn("wh.is_group = 0", query)
		self.assertEqual(params["warehouses"], ["FG-WH"])

	def test_ambiguous_demand_without_sales_order_never_credits_other_so_reservations(self):
		demand_rows = [
			{
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"sales_order": None,
				"item_code": "FG-1",
				"qty": 50,
				"demand_source": "Customer Delivery Schedule",
			}
		]
		with (
			patch.object(planning.frappe.db, "exists", return_value=True),
			patch.object(planning, "_get_sales_order_reservation_rows") as get_rows,
		):
			self.assertEqual(
				planning._get_aps_sales_order_reservation_credit_map("COMPANY-1", demand_rows),
				{},
			)
		get_rows.assert_not_called()

	def test_public_apis_forward_explicit_policy(self):
		with (
			patch("injection_aps.api.app._require_plan_access"),
			patch("injection_aps.api.app._require_scoped_document_access"),
			patch("injection_aps.api.app._require_explicit_company", side_effect=lambda company, **_: company),
			patch("injection_aps.api.app._require_scope_access"),
			patch("injection_aps.api.app._require_company_rebuild_scope"),
			patch("injection_aps.api.app._require_complete_run_mutation_scope"),
			patch("injection_aps.api.app._require_planning_reference_access"),
			patch.object(app.frappe.db, "get_value", return_value="Test Company"),
			patch("injection_aps.api.app.planning.rebuild_net_requirements", return_value={}) as rebuild,
			patch("injection_aps.api.app.planning.run_planning_run", return_value={}) as run,
			patch(
				"injection_aps.api.app.planning.promote_schedule_import_to_net_requirement",
				return_value={},
			) as promote,
			patch(
				"injection_aps.api.app.planning.create_trial_run_from_net_requirement_context",
				return_value={},
			) as create_trial,
		):
			app.rebuild_net_requirements(company="Test Company", existing_work_order_policy="Exclude")
			app.run_planning_run(run_name="APS-RUN-1", existing_work_order_policy="Include")
			app.promote_schedule_import_to_net_requirement(
				import_batch="APS-IMPORT-1",
				existing_work_order_policy="Exclude",
			)
			app.create_trial_run_from_net_requirement_context(
				company="Test Company",
				existing_work_order_policy="Include",
			)

		rebuild.assert_called_once_with(company="Test Company", existing_work_order_policy="Exclude")
		self.assertEqual(run.call_args.kwargs["existing_work_order_policy"], "Include")
		self.assertEqual(promote.call_args.kwargs["existing_work_order_policy"], "Exclude")
		self.assertEqual(create_trial.call_args.kwargs["existing_work_order_policy"], "Include")

	def test_backfill_only_targets_historical_calculation_evidence(self):
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(backfill_existing_work_order_policy.frappe.db, "exists", return_value=True),
			patch.object(backfill_existing_work_order_policy.frappe, "get_meta", return_value=meta),
			patch.object(backfill_existing_work_order_policy.frappe.db, "sql") as sql,
		):
			backfill_existing_work_order_policy.execute()

		self.assertEqual(sql.call_count, 2)
		self.assertIn("is_system_generated", sql.call_args_list[0].args[0])
		self.assertIn("ifnull(run.status, 'Draft') != 'Draft'", sql.call_args_list[1].args[0])
		self.assertIn("tabAPS Schedule Result", sql.call_args_list[1].args[0])

	def _run_rebuild(
		self,
		policy,
		*,
		demand_qtys=None,
		stock_qty=5,
		safety_stock_qty=0,
		minimum_batch_qty=0,
		safety_demand_qty=None,
		sales_orders=None,
		sales_order_items=None,
	):
		demand_qtys = demand_qtys or [10, 10]
		sales_orders = sales_orders or ["SO-1"] * len(demand_qtys)
		sales_order_items = sales_order_items or ["SOI-1"] * len(demand_qtys)
		demand_rows = [
			frappe._dict(
				name=f"DEMAND-{index}",
				company="Test Company",
				customer="Customer A",
				sales_order=sales_orders[index - 1],
				sales_order_item=sales_order_items[index - 1],
				item_code="ITEM-A",
				demand_date=f"2026-08-{11 + index:02d}",
				qty=qty,
				demand_source="Customer Delivery Schedule",
			)
			for index, qty in enumerate(demand_qtys, start=1)
		]
		if safety_demand_qty is not None:
			demand_rows.append(
				frappe._dict(
					name="DEMAND-SAFETY",
					company="Test Company",
					customer=None,
					sales_order=None,
					sales_order_item=None,
					item_code="ITEM-A",
					demand_date="2026-08-11",
					qty=safety_demand_qty,
					demand_source="Safety Stock",
				)
			)
		inserted_rows = []

		def make_doc(values):
			inserted_rows.append(values)
			return _InsertedNetRequirement(values, len(inserted_rows))

		with ExitStack() as stack:
			stack.enter_context(patch("injection_aps.services.planning.repair_item_references", return_value={}))
			stack.enter_context(patch("injection_aps.services.planning._delete_system_generated_rows"))
			stack.enter_context(patch("injection_aps.services.planning.frappe.get_all", return_value=demand_rows))
			stack.enter_context(patch("injection_aps.services.planning._resolve_item_name", side_effect=lambda value: value))
			stack.enter_context(patch("injection_aps.services.planning._is_schedulable_item", return_value=True))
			stack.enter_context(
				patch(
					"injection_aps.services.planning._get_available_stock_map",
					return_value={"ITEM-A": stock_qty},
				)
			)
			get_work_orders = stack.enter_context(
				patch(
					"injection_aps.services.planning._get_open_work_order_map",
					return_value={("Sales Order", "Test Company", "SO-1", "SOI-1", "ITEM-A"): 12},
				)
			)
			stack.enter_context(
				patch(
					"injection_aps.services.planning._get_item_mapping_value",
						side_effect=lambda _item, fieldname: (
							safety_stock_qty
							if fieldname == "safety_stock"
							else minimum_batch_qty if fieldname == "minimum_batch" else 0
						),
				)
			)
			stack.enter_context(
				patch(
					"injection_aps.services.planning.get_settings_dict",
					return_value={
						"item_safety_stock_field": "safety_stock",
						"item_max_stock_field": "max_stock",
						"item_min_batch_field": "minimum_batch",
					},
				)
			)
			stack.enter_context(patch("injection_aps.services.planning.frappe.get_doc", side_effect=make_doc))
			result = planning.rebuild_net_requirements(
				company="Test Company",
				existing_work_order_policy=policy,
			)

		return result, inserted_rows, get_work_orders
