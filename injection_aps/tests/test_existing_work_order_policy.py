from __future__ import annotations

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

	def test_public_apis_forward_explicit_policy(self):
		with (
			patch("injection_aps.api.app._require_plan_access"),
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

	def _run_rebuild(self, policy):
		demand_rows = [
			frappe._dict(
				name="DEMAND-1",
				company="Test Company",
				customer="Customer A",
				item_code="ITEM-A",
				demand_date="2026-08-12",
				qty=10,
				demand_source="Customer Delivery Schedule",
			),
			frappe._dict(
				name="DEMAND-2",
				company="Test Company",
				customer="Customer A",
				item_code="ITEM-A",
				demand_date="2026-08-13",
				qty=10,
				demand_source="Customer Delivery Schedule",
			),
		]
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
				patch("injection_aps.services.planning._get_available_stock_map", return_value={"ITEM-A": 5})
			)
			get_work_orders = stack.enter_context(
				patch("injection_aps.services.planning._get_open_work_order_map", return_value={"ITEM-A": 12})
			)
			stack.enter_context(patch("injection_aps.services.planning._get_item_mapping_value", return_value=0))
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
