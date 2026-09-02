from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, today

from injection_aps.api import app
from injection_aps.services import change_engine, consistency, planning


class TestChangeEngineCalculations(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._created_offline_frappe_context = False
		try:
			getattr(frappe.db, "get_value")
		except RuntimeError:
			# These calculations are deliberately runnable without frappe.init/site/DB.
			# Bind only process-local proxies; every database interaction remains mocked.
			frappe.local.db = MagicMock(name="offline_change_engine_db")
			frappe.local.cache = MagicMock(name="offline_change_engine_cache")
			frappe.local.session = frappe._dict(user="Administrator")
			frappe.local.flags = frappe._dict(in_test=True)
			frappe.local.lang = "en"
			frappe.local.conf = frappe._dict()
			frappe.local.message_log = []
			frappe.local.response = frappe._dict()
			frappe.local.form_dict = frappe._dict()
			frappe.local.dev_server = False
			cls._created_offline_frappe_context = True
		cls._translation_patch = patch.object(
			change_engine,
			"_",
			side_effect=lambda message, *args, **kwargs: message,
		)
		cls._translation_patch.start()

		def raise_validation(message, exc=frappe.ValidationError, *args, **kwargs):
			exception_type = exc if isinstance(exc, type) and issubclass(exc, Exception) else frappe.ValidationError
			raise exception_type(message)

		cls._throw_patch = patch.object(frappe, "throw", side_effect=raise_validation)
		cls._throw_patch.start()

	@classmethod
	def tearDownClass(cls):
		cls._throw_patch.stop()
		cls._translation_patch.stop()
		if cls._created_offline_frappe_context:
			frappe.local.__release_local__()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		self._sales_order_item_patch = patch.object(
			planning,
			"_resolve_unique_sales_order_item",
			side_effect=lambda sales_order, item_code: (
				"{0}::{1}".format(sales_order, item_code)
				if sales_order and item_code
				else None
			),
		)
		self._sales_order_item_patch.start()
		self._production_lower_bound_patch = patch.object(
			change_engine,
			"_get_authoritative_target_production",
			return_value={},
		)
		self._production_lower_bound_mock = self._production_lower_bound_patch.start()

	def tearDown(self):
		self._production_lower_bound_patch.stop()
		self._sales_order_item_patch.stop()
		super().tearDown()

	def test_customer_delta_rejects_a_non_customer_result_with_validation_error(self):
		result = frappe._dict(demand_source="Forecast")
		proposal = {"customer_demand_change": {"source_demand_delta": "DELTA-1"}}

		with self.assertRaisesRegex(frappe.ValidationError, "non-customer APS result"):
			change_engine._accept_customer_schedule_delta_baseline(result, proposal)

	def test_plan_only_net_requirement_update_never_overwrites_gross_customer_demand(self):
		result = frappe._dict(
			name="RESULT-1",
			net_requirement="NET-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(
				{"net_requirement": {"demand_qty": 100}, "targets": []}
			),
		)
		proposal = {
			"change_request": "CR-1",
			"target_net_requirement": "NET-1",
			"target_planned_qty": 40,
		}
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(open_work_order_qty=0, existing_work_order_policy="Exclude"),
			),
			patch.object(frappe.db, "set_value") as set_value,
		):
			change_engine._update_target_net_requirement(result, proposal)

		set_value.assert_called_once()
		self.assertEqual(set_value.call_args.args[:2], ("APS Net Requirement", "NET-1"))
		values = set_value.call_args.args[2]
		self.assertEqual(values["planning_qty"], 40)
		self.assertEqual(values["net_requirement_qty"], 40)
		self.assertNotIn("demand_qty", values)
		self.assertNotIn("fulfillment_baseline_json", values)

	def test_plan_only_update_keeps_existing_wo_inside_one_total_boundary(self):
		result = frappe._dict(
			name="RESULT-1",
			net_requirement="NET-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json="{}",
		)
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(open_work_order_qty=30, existing_work_order_policy="Include"),
			),
			patch.object(frappe.db, "set_value") as set_value,
		):
			change_engine._update_target_net_requirement(
				result,
				{
					"change_request": "CR-1",
					"target_net_requirement": "NET-1",
					"target_planned_qty": 80,
				},
			)

		values = set_value.call_args.args[2]
		self.assertEqual(values["planning_qty"], 50)
		self.assertEqual(values["net_requirement_qty"], 50)
		self.assertEqual(max(values["planning_qty"], 30 + values["net_requirement_qty"]), 80)

	def test_imported_gross_demand_is_converted_to_net_plan_after_offsets(self):
		baseline = {
			"version": 4,
			"net_requirement": {
				"formula_version": 1,
				"demand_qty": 100,
				"available_stock_qty": 60,
				"open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
				"safety_stock_gap_qty": 0,
				"minimum_batch_qty": 0,
				"minimum_batch_coverage_qty": 0,
				"base_residual_qty": 40,
				"net_requirement_qty": 40,
				"planning_qty": 40,
				"new_batch_surplus_qty": 0,
				"is_safety_stock_group": 0,
			},
			"targets": [
				{
					"customer_schedule_item": "ROW-1",
					"opening_required_qty": 100,
					"source_open_qty": 100,
				}
			],
		}
		result = frappe._dict(
			name="RESULT-1",
			net_requirement="NET-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
			requested_date="2026-08-20",
			planned_qty=40,
		)
		doc = frappe._dict(source_demand_delta="DELTA-1", change_type="Decrease Qty")
		delta = frappe._dict(
			name="DELTA-1",
			schedule_reference="SCHEDULE-2",
			change_type="Reduced",
			previous_qty=100,
			current_qty=80,
			delta_qty=-20,
			current_schedule_date="2026-08-20",
			sales_order="SO-1",
			item_code="ITEM-1",
		)
		live_row = {
			"name": "ROW-1",
			"parent": "SCHEDULE-2",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"item_code": "ITEM-1",
			"qty": 80,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
		}

		def get_value(doctype, name, fieldname, **kwargs):
			if doctype == "APS Demand Delta":
				return delta
			if doctype == "APS Net Requirement":
				return frappe._dict(
					name="NET-1",
					company="COMPANY-1",
					customer="CUSTOMER-1",
					sales_order="SO-1",
					sales_order_item="SO-1::ITEM-1",
					item_code="ITEM-1",
					demand_qty=100,
					available_stock_qty=60,
					open_work_order_qty=0,
					existing_work_order_policy="Exclude",
					safety_stock_gap_qty=0,
					minimum_batch_qty=0,
				)
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[live_row]),
			patch.object(change_engine, "_get_authoritative_target_deliveries", return_value={"ROW-1": 0}),
		):
			context = change_engine._build_customer_schedule_demand_change(doc, result)

		self.assertEqual(context["current_customer_qty"], 80)
		self.assertEqual(context["target_demand_qty"], 80)
		self.assertEqual(context["target_net_requirement_qty"], 20)
		self.assertEqual(context["target_planned_qty"], 20)

	def test_appended_delta_adds_exact_new_target_without_item_only_reuse(self):
		baseline = {
			"version": 4,
			"net_requirement": {
				"formula_version": 1,
				"demand_qty": 100,
				"available_stock_qty": 60,
				"open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
				"safety_stock_gap_qty": 0,
				"minimum_batch_qty": 0,
				"minimum_batch_coverage_qty": 0,
				"base_residual_qty": 40,
				"net_requirement_qty": 40,
				"planning_qty": 40,
				"new_batch_surplus_qty": 0,
				"is_safety_stock_group": 0,
			},
			"targets": [{"customer_schedule_item": "OLD-ROW", "source_open_qty": 100}],
		}
		result = frappe._dict(
			name="RESULT-1",
			net_requirement="NET-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
			requested_date="2026-08-20",
			planned_qty=40,
		)
		doc = frappe._dict(source_demand_delta="DELTA-APPEND", change_type="Increase Qty")
		delta = frappe._dict(
			name="DELTA-APPEND",
			schedule_reference="APPEND-SCHEDULE",
			change_type="Appended",
			previous_qty=0,
			current_qty=20,
			delta_qty=20,
			current_schedule_date="2026-08-20",
			sales_order="SO-1",
			item_code="ITEM-1",
		)
		old_row = {
			"name": "OLD-ROW",
			"parent": "OLD-SCHEDULE",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"item_code": "ITEM-1",
			"qty": 100,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
		}
		new_row = {
			"name": "NEW-ROW",
			"parent": "APPEND-SCHEDULE",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"item_code": "ITEM-1",
			"qty": 20,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
		}

		def get_value(doctype, name, fieldname, **kwargs):
			if doctype == "APS Demand Delta":
				return delta
			if doctype == "APS Net Requirement":
				return frappe._dict(
					name="NET-1",
					company="COMPANY-1",
					customer="CUSTOMER-1",
					sales_order="SO-1",
					sales_order_item="SO-1::ITEM-1",
					item_code="ITEM-1",
					demand_qty=100,
					available_stock_qty=60,
					open_work_order_qty=0,
					existing_work_order_policy="Exclude",
					safety_stock_gap_qty=0,
					minimum_batch_qty=0,
				)
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(change_engine, "_get_delta_customer_schedule_target_rows", return_value=[new_row]),
			patch.object(
				change_engine,
				"_get_customer_schedule_target_rows",
				return_value=[old_row, new_row],
			),
			patch.object(
				change_engine,
				"_get_authoritative_target_deliveries",
				return_value={"OLD-ROW": 0, "NEW-ROW": 0},
			),
		):
			context = change_engine._build_customer_schedule_demand_change(doc, result)

		self.assertEqual(context["target_demand_qty"], 120)
		self.assertEqual(context["target_planned_qty"], 60)
		self.assertEqual({row["name"] for row in context["targets"]}, {"OLD-ROW", "NEW-ROW"})

	def test_appended_delta_cannot_cross_sales_order_boundary(self):
		result = frappe._dict(
			name="RESULT-A",
			net_requirement="NET-A",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(
				{
					"net_requirement": {
						"demand_qty": 100,
						"available_stock_qty": 0,
						"open_work_order_qty": 50,
						"existing_work_order_policy": "Include",
					},
					"targets": [{"customer_schedule_item": "ROW-A"}],
				}
			),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-A",
			sales_order_item="SO-A::ITEM-1",
			item_code="ITEM-1",
			planned_qty=100,
		)
		delta = frappe._dict(
			name="DELTA-B",
			schedule_reference="SCHEDULE-B",
			change_type="Appended",
			previous_qty=0,
			current_qty=20,
			delta_qty=20,
			sales_order="SO-B",
			item_code="ITEM-1",
		)
		with patch.object(frappe.db, "get_value", return_value=delta):
			with self.assertRaisesRegex(frappe.ValidationError, "Sales Order lineage does not match"):
				change_engine._build_customer_schedule_demand_change(
					frappe._dict(source_demand_delta="DELTA-B", change_type="Increase Qty"),
					result,
				)

	def test_appended_delta_cannot_merge_a_different_delivery_date(self):
		result = frappe._dict(
			name="RESULT-DATE-A",
			net_requirement="NET-DATE-A",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(
				{
					"net_requirement": {
						"demand_qty": 100,
						"available_stock_qty": 0,
						"open_work_order_qty": 0,
						"existing_work_order_policy": "Exclude",
					},
					"targets": [{"customer_schedule_item": "ROW-DATE-A"}],
				}
			),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
			requested_date="2026-08-20",
			planned_qty=100,
		)
		delta = frappe._dict(
			name="DELTA-DATE-B",
			change_type="Appended",
			current_schedule_date="2026-08-21",
			sales_order="SO-1",
			item_code="ITEM-1",
		)
		with patch.object(frappe.db, "get_value", return_value=delta):
			with self.assertRaisesRegex(frappe.ValidationError, "instead of merging dates"):
				change_engine._build_customer_schedule_demand_change(
					frappe._dict(source_demand_delta="DELTA-DATE-B", change_type="Increase Qty"),
					result,
				)

	def test_date_delta_requires_exact_previous_current_and_request_dates(self):
		result = frappe._dict(name="RESULT-1", requested_date="2026-08-20")
		delta = frappe._dict(
			name="DELTA-DATE",
			previous_schedule_date="2026-08-20",
			current_schedule_date="2026-08-19",
		)
		resolved = change_engine._resolve_demand_delta_target_date(
			frappe._dict(
				source_demand_delta="DELTA-DATE",
				change_type="Pull In",
				required_date="2026-08-19",
			),
			result,
			delta,
		)
		self.assertEqual(resolved, getdate("2026-08-19"))
		with self.assertRaisesRegex(frappe.ValidationError, "must exactly match"):
			change_engine._resolve_demand_delta_target_date(
				frappe._dict(
					source_demand_delta="DELTA-DATE",
					change_type="Pull In",
					required_date="2026-08-18",
				),
				result,
				delta,
			)

	def test_customer_schedule_qty_below_authoritative_fulfillment_floor_is_blocked(self):
		baseline = {
			"net_requirement": {
				"demand_qty": 20,
				"available_stock_qty": 0,
				"open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
			},
			"targets": [{"customer_schedule_item": "ROW-L"}],
		}
		result = frappe._dict(
			name="RESULT-L",
			net_requirement="NET-L",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
			requested_date="2026-08-20",
			planned_qty=20,
		)
		delta = frappe._dict(
			name="DELTA-L",
			schedule_reference="SCHEDULE-L",
			change_type="Reduced",
			previous_qty=20,
			current_qty=5,
			delta_qty=-15,
			current_schedule_date="2026-08-20",
			sales_order="SO-1",
			item_code="ITEM-1",
		)
		live_row = {
			"name": "ROW-L",
			"parent": "SCHEDULE-L",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"item_code": "ITEM-1",
			"qty": 5,
			"allocated_qty": 12,
			"produced_qty": 10,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
		}

		def get_value(doctype, name, fieldname, **kwargs):
			if doctype == "APS Demand Delta":
				return delta
			if doctype == "APS Net Requirement":
				return 0
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[live_row]),
			patch.object(change_engine, "_get_authoritative_target_deliveries", return_value={"ROW-L": 7}),
		):
			with self.assertRaisesRegex(
				frappe.ValidationError,
				"ROW-L quantity 5.*lower bound 12",
			):
				change_engine._build_customer_schedule_demand_change(
					frappe._dict(source_demand_delta="DELTA-L", change_type="Decrease Qty"),
					result,
				)

		# A stale zero in the schedule child must not hide effective production.
		live_row["allocated_qty"] = 0
		live_row["produced_qty"] = 0
		self._production_lower_bound_mock.return_value = {"ROW-L": 10}
		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[live_row]),
			patch.object(change_engine, "_get_authoritative_target_deliveries", return_value={"ROW-L": 7}),
		):
			with self.assertRaisesRegex(
				frappe.ValidationError,
				"ROW-L quantity 5.*lower bound 10",
			):
				change_engine._build_customer_schedule_demand_change(
					frappe._dict(source_demand_delta="DELTA-L", change_type="Decrease Qty"),
					result,
				)

	def test_net_and_minimum_batch_formula_keep_one_total_work_order_boundary(self):
		baseline = {
			"version": 4,
			"net_requirement": {
				"formula_version": 1,
				"demand_qty": 140,
				"available_stock_qty": 20,
				"open_work_order_qty": 30,
				"existing_work_order_policy": "Include",
				"safety_stock_gap_qty": 10,
				"minimum_batch_qty": 120,
				"minimum_batch_coverage_qty": 0,
				"base_residual_qty": 100,
				"net_requirement_qty": 100,
				"planning_qty": 120,
				"new_batch_surplus_qty": 20,
				"is_safety_stock_group": 0,
			},
			"targets": [{"customer_schedule_item": "ROW-M"}],
		}
		result = frappe._dict(
			name="RESULT-M",
			net_requirement="NET-M",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
			requested_date="2026-08-20",
			planned_qty=120,
		)
		delta = frappe._dict(
			name="DELTA-M",
			schedule_reference="SCHEDULE-M",
			change_type="Increased",
			previous_qty=140,
			current_qty=160,
			delta_qty=20,
			current_schedule_date="2026-08-20",
			sales_order="SO-1",
			item_code="ITEM-1",
		)
		live_row = {
			"name": "ROW-M",
			"parent": "SCHEDULE-M",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"item_code": "ITEM-1",
			"qty": 160,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
		}

		def get_value(doctype, name, fieldname, **kwargs):
			if doctype == "APS Demand Delta":
				return delta
			if doctype == "APS Net Requirement":
				return frappe._dict(
					name="NET-M",
					company="COMPANY-1",
					customer="CUSTOMER-1",
					sales_order="SO-1",
					sales_order_item="SO-1::ITEM-1",
					item_code="ITEM-1",
					demand_qty=140,
					available_stock_qty=20,
					open_work_order_qty=30,
					existing_work_order_policy="Include",
					safety_stock_gap_qty=10,
					minimum_batch_qty=120,
				)
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[live_row]),
			patch.object(change_engine, "_get_authoritative_target_deliveries", return_value={"ROW-M": 0}),
		):
			context = change_engine._build_customer_schedule_demand_change(
				frappe._dict(source_demand_delta="DELTA-M", change_type="Increase Qty"),
				result,
			)

		self.assertEqual(context["target_demand_qty"], 160)
		self.assertEqual(context["safety_stock_gap_qty"], 10)
		self.assertEqual(context["target_base_residual_qty"], 120)
		self.assertEqual(context["target_net_requirement_qty"], 120)
		self.assertEqual(context["target_residual_planning_qty"], 120)
		self.assertEqual(context["target_planned_qty"], 150)

	def test_cross_target_minimum_batch_coverage_requires_full_run_rebuild(self):
		result = frappe._dict(
			name="RESULT-COVERED",
			net_requirement="NET-COVERED",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(
				{
					"version": 4,
					"net_requirement": {
						"formula_version": 1,
						"demand_qty": 120,
						"available_stock_qty": 0,
						"open_work_order_qty": 0,
						"existing_work_order_policy": "Exclude",
						"safety_stock_gap_qty": 0,
						"minimum_batch_qty": 100,
						"minimum_batch_coverage_qty": 20,
						"base_residual_qty": 120,
						"net_requirement_qty": 120,
						"planning_qty": 120,
						"new_batch_surplus_qty": 0,
						"is_safety_stock_group": 0,
					},
					"targets": [{"customer_schedule_item": "ROW-COVERED"}],
				}
			),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
			requested_date="2026-08-20",
			planned_qty=100,
		)
		delta = frappe._dict(
			name="DELTA-COVERED",
			change_type="Increased",
			current_schedule_date="2026-08-20",
			sales_order="SO-1",
			item_code="ITEM-1",
		)
		row = {
			"name": "ROW-COVERED",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"item_code": "ITEM-1",
			"qty": 120,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
		}
		def get_value(doctype, name, fieldname, **kwargs):
			if doctype == "APS Demand Delta":
				return delta
			if doctype == "APS Net Requirement":
				return frappe._dict(
					name="NET-COVERED",
					company="COMPANY-1",
					customer="CUSTOMER-1",
					sales_order="SO-1",
					sales_order_item="SO-1::ITEM-1",
					item_code="ITEM-1",
					demand_qty=120,
					available_stock_qty=0,
					open_work_order_qty=0,
					existing_work_order_policy="Exclude",
					safety_stock_gap_qty=0,
					minimum_batch_qty=0,
				)
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[row]),
			patch.object(change_engine, "_get_authoritative_target_deliveries", return_value={"ROW-COVERED": 0}),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "Rebuild the complete Planning Run"):
				change_engine._build_customer_schedule_demand_change(
					frappe._dict(source_demand_delta="DELTA-COVERED", change_type="Increase Qty"),
					result,
				)

	def test_delta_baseline_acceptance_preserves_historical_offsets_and_source_rows(self):
		baseline = {
			"version": 4,
			"net_requirement": {
				"formula_version": 1,
				"demand_qty": 100,
				"available_stock_qty": 10,
				"open_work_order_qty": 5,
				"existing_work_order_policy": "Include",
				"safety_stock_gap_qty": 0,
				"minimum_batch_qty": 0,
				"minimum_batch_coverage_qty": 0,
				"base_residual_qty": 85,
				"net_requirement_qty": 85,
				"planning_qty": 85,
				"new_batch_surplus_qty": 0,
				"is_safety_stock_group": 0,
			},
			"targets": [
				{
					"customer_schedule_item": "ROW-1",
					"sales_order": "SO-1",
					"sales_order_item": "SO-1::ITEM-1",
					"item_code": "ITEM-1",
					"schedule_date": "2026-08-18",
					"opening_required_qty": 100,
					"source_open_qty": 93,
					"opening_allocated_qty": 3,
					"opening_produced_qty": 5,
					"opening_delivered_qty": 7,
				}
			],
		}
		result = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
		)
		live_row = {
			"name": "ROW-1",
			"parent": "SCHEDULE-2",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"sales_order_item": "SO-1::ITEM-1",
			"item_code": "ITEM-1",
			"qty": 80,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
		}
		state_row = dict(live_row)
		state_row.update({"delivered_qty": 7, "open_qty": 73, "fulfillment_lower_bound_qty": 7})
		proposal = {
			"change_request": "CR-1",
			"source_demand_delta": "DELTA-1",
			"customer_demand_change": {
				"source_demand_delta": "DELTA-1",
				"sales_order": "SO-1",
				"sales_order_item": "SO-1::ITEM-1",
				"target_schedule_date": "2026-08-20",
				"target_demand_qty": 73,
				"available_stock_qty": 10,
				"credited_open_work_order_qty": 5,
				"existing_work_order_policy": "Include",
				"safety_stock_gap_qty": 0,
				"minimum_batch_qty": 0,
				"minimum_batch_coverage_qty": 0,
				"target_base_residual_qty": 58,
				"target_net_requirement_qty": 58,
				"target_residual_planning_qty": 58,
				"new_batch_surplus_qty": 0,
				"is_safety_stock_group": 0,
				"target_state_token": change_engine._hash_payload(
					[change_engine._customer_schedule_target_state(state_row)]
				),
			},
		}
		with (
			patch.object(
				change_engine,
				"_get_source_demand_delta_sales_lineage",
				return_value={
					"sales_order": "SO-1",
					"sales_order_item": "SO-1::ITEM-1",
					"item_code": "ITEM-1",
				},
			),
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[live_row]),
			patch.object(change_engine, "_get_authoritative_target_deliveries", return_value={"ROW-1": 7}),
			patch.object(frappe.db, "set_value") as set_value,
		):
			accepted = change_engine._accept_customer_schedule_delta_baseline(result, proposal)

		updated = json.loads(accepted["baseline_json"])
		target = updated["targets"][0]
		self.assertEqual(target["opening_required_qty"], 100)
		self.assertEqual(target["source_open_qty"], 93)
		self.assertEqual(target["opening_allocated_qty"], 3)
		self.assertEqual(target["opening_produced_qty"], 5)
		self.assertEqual(target["opening_delivered_qty"], 7)
		self.assertEqual(target["schedule_date"], "2026-08-18")
		self.assertEqual(target["accepted_required_qty"], 80)
		self.assertEqual(target["accepted_delivered_qty"], 7)
		self.assertEqual(target["accepted_source_open_qty"], 73)
		self.assertEqual(target["accepted_current_open_qty"], 73)
		self.assertEqual(target["accepted_schedule_date"], "2026-08-20")
		self.assertEqual(target["attributed_qty"], 73)
		self.assertEqual(updated["version"], 4)
		self.assertEqual(
			updated["net_requirement"],
			{
				"formula_version": 1,
				"demand_qty": 73.0,
				"available_stock_qty": 10.0,
				"open_work_order_qty": 5.0,
				"existing_work_order_policy": "Include",
				"safety_stock_gap_qty": 0.0,
				"minimum_batch_qty": 0.0,
				"minimum_batch_coverage_qty": 0.0,
				"base_residual_qty": 58.0,
				"net_requirement_qty": 58.0,
				"planning_qty": 58.0,
				"new_batch_surplus_qty": 0.0,
				"is_safety_stock_group": 0,
			},
		)
		source = json.loads(accepted["demand_source_snapshot_json"])[0]
		self.assertEqual(source["source_detail_name"], "ROW-1")
		self.assertEqual(source["qty"], 73)
		self.assertEqual(set_value.call_args.args[:2], ("APS Schedule Result", "RESULT-1"))
		self.assertNotEqual(set_value.call_args.args[0], "Customer Delivery Schedule Item")

	def test_delta_acceptance_fails_closed_when_original_epoch_does_not_conserve(self):
		with self.assertRaisesRegex(frappe.ValidationError, "original gross.*not conserved"):
			change_engine._get_frozen_customer_schedule_target_offsets(
				{
					"customer_schedule_item": "ROW-BROKEN",
					"opening_required_qty": 100,
					"opening_allocated_qty": 0,
					"opening_produced_qty": 0,
					"opening_delivered_qty": 7,
					"source_open_qty": 100,
				}
			)

	def test_delta_acceptance_fails_closed_for_return_crossing_original_delivery_epoch(self):
		target = {
			"customer_schedule_item": "ROW-RETURN",
			"opening_required_qty": 100,
			"opening_allocated_qty": 0,
			"opening_produced_qty": 0,
			"opening_delivered_qty": 7,
			"source_open_qty": 93,
		}
		with self.assertRaisesRegex(frappe.ValidationError, "return crossing"):
			change_engine._build_customer_schedule_accepted_epoch(
				target,
				{
					"name": "ROW-RETURN",
					"qty": 100,
					"delivered_qty": 5,
					"open_qty": 95,
				},
			)

	def test_appended_delta_acceptance_freezes_new_target_offsets_once(self):
		baseline = {
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
					"customer_schedule_item": "OLD-ROW",
					"sales_order": "SO-1",
					"sales_order_item": "SO-1::ITEM-1",
					"item_code": "ITEM-1",
					"schedule_date": "2026-08-20",
					"opening_required_qty": 100,
					"opening_allocated_qty": 0,
					"opening_produced_qty": 0,
					"opening_delivered_qty": 0,
					"source_open_qty": 100,
				}
			],
		}
		result = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
			item_code="ITEM-1",
		)
		rows = [
			{
				"name": "OLD-ROW",
				"parent": "OLD-SCHEDULE",
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"sales_order": "SO-1",
				"sales_order_item": "SO-1::ITEM-1",
				"item_code": "ITEM-1",
				"qty": 100,
				"schedule_date": "2026-08-20",
				"schedule_status": "Active",
			},
			{
				"name": "NEW-ROW",
				"parent": "APPEND-SCHEDULE",
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"sales_order": "SO-1",
				"sales_order_item": "SO-1::ITEM-1",
				"item_code": "ITEM-1",
				"qty": 20,
				"allocated_qty": 2,
				"produced_qty": 3,
				"schedule_date": "2026-08-20",
				"schedule_status": "Active",
			},
		]
		state_rows = []
		for row in rows:
			state = dict(row)
			state.update(
				{
					"delivered_qty": 0,
					"open_qty": row["qty"],
					"fulfillment_lower_bound_qty": max(
						row.get("allocated_qty", 0), row.get("produced_qty", 0)
					),
				}
			)
			state_rows.append(change_engine._customer_schedule_target_state(state))
		proposal = {
			"change_request": "CR-APPEND",
			"source_demand_delta": "DELTA-APPEND",
			"customer_demand_change": {
				"source_demand_delta": "DELTA-APPEND",
				"sales_order": "SO-1",
				"sales_order_item": "SO-1::ITEM-1",
				"target_schedule_date": "2026-08-20",
				"target_demand_qty": 120,
				"available_stock_qty": 0,
				"credited_open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
				"safety_stock_gap_qty": 0,
				"minimum_batch_qty": 0,
				"minimum_batch_coverage_qty": 0,
				"target_base_residual_qty": 120,
				"target_net_requirement_qty": 120,
				"target_residual_planning_qty": 120,
				"new_batch_surplus_qty": 0,
				"is_safety_stock_group": 0,
				"target_state_token": change_engine._hash_payload(state_rows),
				"targets": state_rows,
			},
		}
		self._production_lower_bound_mock.return_value = {"OLD-ROW": 0, "NEW-ROW": 3}
		with (
			patch.object(
				change_engine,
				"_get_source_demand_delta_sales_lineage",
				return_value={
					"sales_order": "SO-1",
					"sales_order_item": "SO-1::ITEM-1",
					"item_code": "ITEM-1",
				},
			),
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=rows),
			patch.object(
				change_engine,
				"_get_authoritative_target_deliveries",
				return_value={"OLD-ROW": 0, "NEW-ROW": 0},
			),
			patch.object(frappe.db, "set_value"),
		):
			accepted = change_engine._accept_customer_schedule_delta_baseline(result, proposal)

		updated = json.loads(accepted["baseline_json"])
		new_target = next(
			row for row in updated["targets"] if row["customer_schedule_item"] == "NEW-ROW"
		)
		self.assertEqual(new_target["opening_required_qty"], 20)
		self.assertEqual(new_target["opening_allocated_qty"], 2)
		self.assertEqual(new_target["opening_produced_qty"], 3)
		self.assertEqual(new_target["opening_delivered_qty"], 0)
		self.assertEqual(new_target["source_open_qty"], 20)
		self.assertEqual(new_target["accepted_required_qty"], 20)
		self.assertEqual(new_target["accepted_delivered_qty"], 0)
		self.assertEqual(new_target["accepted_source_open_qty"], 20)
		self.assertEqual(new_target["accepted_current_open_qty"], 20)
		self.assertEqual(new_target["accepted_schedule_date"], "2026-08-20")
		self.assertEqual(len(updated["targets"]), 2)

	def test_customer_schedule_date_change_requires_imported_delta(self):
		doc = frappe._dict(
			change_type="Pull In",
			source_demand_delta=None,
			target_result="RESULT-1",
		)
		with patch.object(frappe.db, "get_value", return_value="Customer Delivery Schedule"):
			with self.assertRaisesRegex(frappe.ValidationError, "Schedule Import & Diff"):
				change_engine._validate_customer_schedule_change_source(doc)

	def test_customer_schedule_snapshot_token_changes_with_source_row(self):
		results = [
			frappe._dict(
				fulfillment_baseline_json=json.dumps(
					{"targets": [{"customer_schedule_item": "ROW-1"}]}
				)
			)
		]
		row = {
			"name": "ROW-1",
			"parent": "SCHEDULE-1",
			"qty": 100,
			"schedule_date": "2026-08-20",
			"schedule_status": "Active",
			"schedule_modified": "2026-08-11 10:00:00",
		}
		with patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[row]):
			before = change_engine._capture_customer_schedule_target_snapshot(results)
		changed = {**row, "qty": 80, "schedule_modified": "2026-08-11 10:01:00"}
		with patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[changed]):
			after = change_engine._capture_customer_schedule_target_snapshot(results)

		self.assertNotEqual(change_engine._hash_payload(before), change_engine._hash_payload(after))

	def test_source_delta_snapshot_includes_exact_sales_order_lineage(self):
		delta = frappe._dict(
			name="DELTA-1",
			import_batch="IMPORT-1",
			schedule_reference="SCHEDULE-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			item_code="ITEM-1",
			customer_part_no="PART-1",
			change_type="Appended",
			modified="2026-08-11 10:00:00",
		)
		schedule = frappe._dict(status="Active", modified="2026-08-11 10:00:01")
		with patch.object(frappe.db, "get_value", side_effect=[delta, schedule]):
			snapshot = change_engine._capture_source_demand_delta_snapshot("DELTA-1")

		self.assertEqual(snapshot["sales_order"], "SO-1")
		self.assertEqual(snapshot["resolved_sales_order_item"], "SO-1::ITEM-1")
		self.assertEqual(snapshot["customer_part_no"], "PART-1")

	def test_net_requirement_must_share_the_result_exact_sales_order_item(self):
		result = frappe._dict(
			name="RESULT-A",
			net_requirement="NET-B",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-A",
			sales_order_item="SO-A::ITEM-1",
			item_code="ITEM-1",
		)
		wrong_state = frappe._dict(
			name="NET-B",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-B",
			sales_order_item="SO-B::ITEM-1",
			item_code="ITEM-1",
		)
		with patch.object(frappe.db, "get_value", return_value=wrong_state):
			with self.assertRaisesRegex(frappe.ValidationError, "exact Company/Customer/Sales Order/Item"):
				change_engine._get_exact_customer_change_net_requirement_state(
					result,
					{
						"sales_order": "SO-A",
						"sales_order_item": "SO-A::ITEM-1",
						"item_code": "ITEM-1",
					},
				)

	def test_append_target_identity_does_not_treat_blank_customer_part_as_wildcard(self):
		result = frappe._dict(
			name="RESULT-1",
			item_code="ITEM-1",
			sales_order="SO-1",
			sales_order_item="SO-1::ITEM-1",
		)
		delta = frappe._dict(
			name="DELTA-1",
			schedule_reference="SCHEDULE-1",
			current_schedule_date="2026-08-20",
			change_type="Appended",
			sales_order="SO-1",
			item_code="ITEM-1",
			customer_part_no="",
		)
		row = {
			"name": "ROW-PART-X",
			"sales_order": "SO-1",
			"customer_part_no": "PART-X",
		}
		with (
			patch.object(frappe, "get_all", return_value=["ROW-PART-X"]) as get_all,
			patch.object(change_engine, "_get_customer_schedule_target_rows", return_value=[row]),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "no exact active schedule row"):
				change_engine._get_delta_customer_schedule_target_rows(delta, result)
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["parent"], "SCHEDULE-1")
		self.assertEqual(filters["item_code"], "ITEM-1")
		self.assertEqual(getdate(filters["schedule_date"]), getdate("2026-08-20"))

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

		cross_order_delta = frappe._dict(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="ITEM-1",
			change_type="Cancelled",
			sales_order="SO-A",
		)
		cross_order_target = frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="ITEM-1",
			sales_order="SO-B",
			sales_order_item="SO-B::ITEM-1",
		)
		with patch.object(frappe.db, "get_value", side_effect=[cross_order_delta, cross_order_target]):
			with self.assertRaisesRegex(frappe.ValidationError, "Sales Order lineage does not match"):
				change_engine._validate_source_demand_delta(doc)

		added_exact_delta = frappe._dict(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="ITEM-1",
			change_type="Added",
			sales_order="SO-A",
		)
		added_doc = frappe._dict(
			source_demand_delta="DELTA-ADDED",
			change_type="Urgent Order",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="ITEM-1",
			target_result=None,
		)
		with (
			patch.object(frappe.db, "get_value", return_value=added_exact_delta),
			patch.object(planning, "_resolve_unique_sales_order_item", return_value=None),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "exactly one Sales Order Item"):
				change_engine._validate_source_demand_delta(added_doc)

		missing_customer_doc = frappe._dict(added_doc)
		missing_customer_doc.customer = None
		with patch.object(frappe.db, "get_value", return_value=added_exact_delta):
			with self.assertRaisesRegex(frappe.ValidationError, "Customer does not match"):
				change_engine._validate_source_demand_delta(missing_customer_doc)

	def test_public_workflow_apis_enforce_separate_permission_checks(self):
		with (
			patch("injection_aps.api.app._require_plan_access") as plan_access,
			patch("injection_aps.api.app._require_approve_access") as approve_access,
			patch("injection_aps.api.app._require_change_request_access") as document_access,
			patch("injection_aps.api.app._require_change_request_impact_access") as impact_access,
			patch("injection_aps.api.app._require_complete_run_mutation_scope") as run_scope,
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(planning_run="RUN-1", target_result="RESULT-1"),
			),
			patch("injection_aps.api.app.planning.confirm_change_request", return_value={}),
			patch("injection_aps.api.app.planning.approve_change_request", return_value={}),
			patch("injection_aps.api.app.planning.apply_change_request", return_value={}),
		):
			app.confirm_change_request("CR-1")
			app.approve_change_request("CR-1")
			app.apply_change_request("CR-1")
		plan_access.assert_called_once()
		self.assertEqual(approve_access.call_count, 2)
		self.assertEqual(document_access.call_count, 3)
		self.assertEqual(impact_access.call_count, 3)
		run_scope.assert_called_once_with("RUN-1", run_ptype="write")

	def test_apply_failure_rolls_back_to_its_savepoint_without_release(self):
		doc = frappe._dict(
			name="CR-1",
			status="Approved",
			planning_run="RUN-1",
			application_fingerprint="APP-FP",
			analysis_fingerprint="ANALYSIS-FP",
			proposal_json="{}",
			retained_disposition=None,
		)
		with (
			patch.object(frappe, "generate_hash", return_value="FIXED"),
			patch.object(frappe.db, "savepoint") as savepoint,
			patch.object(frappe.db, "release_savepoint") as release,
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe.db, "sql", return_value=[]),
			patch.object(change_engine, "_get_application_scope_locked_change_request", return_value=doc),
			patch.object(change_engine, "_assert_snapshot_current", return_value={}),
			patch.object(change_engine, "_dispatch_apply", return_value={"target_result": "RESULT-1"}),
			patch.object(change_engine, "_validate_changed_schedule"),
			patch.object(
				change_engine.consistency,
				"recalculate_plan_consistency",
				side_effect=RuntimeError("forced consistency failure"),
			),
		):
			with self.assertRaisesRegex(RuntimeError, "forced consistency failure"):
				change_engine.apply_change_request("CR-1")

		savepoint.assert_called_once_with("aps_change_apply_FIXED")
		rollback.assert_called_once_with(save_point="aps_change_apply_FIXED")
		release.assert_not_called()

	def test_apply_scope_token_rejects_customer_or_delta_change_after_prelock_read(self):
		scope = frappe._dict(
			planning_run="RUN-1",
			target_result="RESULT-1",
			customer="CUSTOMER-A",
			source_demand_delta="DELTA-A",
			change_type="Decrease Qty",
		)
		locked_doc = frappe._dict(
			name="CR-1",
			planning_run="RUN-1",
			target_result="RESULT-1",
			customer="CUSTOMER-B",
			source_demand_delta="DELTA-A",
			change_type="Decrease Qty",
		)
		with (
			patch.object(frappe.db, "get_value", return_value=scope),
			patch.object(
				frappe.db,
				"sql",
				side_effect=[
					[("CUSTOMER-A",)],
					[frappe._dict(name="RUN-1", company="COMPANY-1")],
					[],
					[frappe._dict(name="RESULT-1", planning_run="RUN-1")],
					[],
					[frappe._dict(name="DELTA-A")],
					[],
					[],
					[],
					[
						frappe._dict(
							name="CR-1",
							planning_run="RUN-1",
							target_result="RESULT-1",
							customer="CUSTOMER-B",
							source_demand_delta="DELTA-A",
							change_type="Decrease Qty",
						)
					],
				],
			) as sql,
			patch.object(frappe, "get_doc", return_value=locked_doc),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "scope changed"):
				change_engine._get_application_scope_locked_change_request("CR-1")

		self.assertIn("tabCustomer", sql.call_args_list[0].args[0])
		self.assertIn("tabAPS Planning Run", sql.call_args_list[1].args[0])

	def test_apply_snapshot_uses_locked_payload_and_requires_rr_equivalence(self):
		locked_state = frappe._dict(
			run=frappe._dict(name="RUN-1", company="CURRENT-COMPANY"),
			run_plant_floors=[],
			results=[
				frappe._dict(
					name="RESULT-1",
					planning_run="RUN-1",
					company="CURRENT-COMPANY",
					planned_qty=80,
				)
			],
			net_requirements=[],
			segments=[],
			delta=None,
			schedules=[],
			schedule_items=[],
			sales_order_items=[],
			capacity_windows=[],
		)
		doc = frappe._dict(
			name="CR-1",
			planning_run="RUN-1",
			target_result="RESULT-1",
			source_demand_delta=None,
			flags=frappe._dict(aps_application_locked_state=locked_state),
		)
		expected = change_engine._capture_plan_snapshot(doc, "target", locked_state=locked_state)
		proposal = {
			"snapshot_scope": "target",
			"source_snapshot_hash": change_engine._hash_payload(expected),
		}

		with patch.object(
			change_engine,
			"_capture_plan_snapshot",
			side_effect=[expected, expected],
		) as capture:
			current = change_engine._assert_snapshot_current(doc, proposal)

		self.assertEqual(current["run"]["company"], "CURRENT-COMPANY")
		self.assertEqual(current["results"][0]["planned_qty"], 80)
		self.assertIs(capture.call_args_list[0].kwargs["locked_state"], locked_state)
		self.assertNotIn("locked_state", capture.call_args_list[1].kwargs)

	def test_apply_rejects_stale_repeatable_read_snapshot_before_mutation(self):
		locked_state = frappe._dict(marker="current")
		doc = frappe._dict(
			name="CR-1",
			planning_run="RUN-1",
			target_result="RESULT-1",
			source_snapshot_hash=None,
			flags=frappe._dict(aps_application_locked_state=locked_state),
		)
		locked_snapshot = {"scope": "target", "run": {"name": "RUN-1", "status": "Approved"}}
		stale_snapshot = {"scope": "target", "run": {"name": "RUN-1", "status": "Planned"}}
		proposal = {"source_snapshot_hash": change_engine._hash_payload(locked_snapshot)}

		with patch.object(
			change_engine,
			"_capture_plan_snapshot",
			side_effect=[locked_snapshot, stale_snapshot],
		):
			with self.assertRaisesRegex(frappe.ValidationError, "transaction snapshot differs"):
				change_engine._assert_snapshot_current(doc, proposal)

	def test_apply_resolves_exact_sales_order_item_from_locked_current_rows(self):
		locked_state = frappe._dict(
			delta=frappe._dict(
				name="DELTA-1",
				sales_order="SO-1",
				item_code="ITEM-1",
			),
			sales_order_items=[
				frappe._dict(name="SOI-CURRENT", parent="SO-1", item_code="ITEM-1")
			],
		)
		target = frappe._dict(
			name="RESULT-1",
			sales_order="SO-1",
			sales_order_item="SOI-CURRENT",
		)
		with patch.object(
			planning,
			"_resolve_unique_sales_order_item",
			side_effect=AssertionError("stale Sales Order Item lookup used"),
		):
			lineage = change_engine._get_source_demand_delta_sales_lineage(
				"DELTA-1",
				target=target,
				locked_state=locked_state,
			)

		self.assertEqual(lineage["sales_order_item"], "SOI-CURRENT")

	def test_demand_delta_rejects_unsynchronized_locked_delivery_source(self):
		locked_state = frappe._dict(
			schedules=[
				frappe._dict(
					name="SCHEDULE-1",
					status="Active",
					company="COMPANY-1",
					customer="CUSTOMER-1",
				)
			],
			schedule_items=[
				frappe._dict(
					name="ROW-1",
					parent="SCHEDULE-1",
					item_code="ITEM-1",
					sales_order="SO-1",
					schedule_date="2026-08-20",
					qty=100,
				)
			],
			delivery_allocations=[],
			delivery_notes=[
				frappe._dict(
					name="DN-1",
					docstatus=1,
					company="COMPANY-1",
					customer="CUSTOMER-1",
					posting_date="2026-08-20",
					posting_time="08:00:00",
					is_return=0,
				)
			],
			delivery_note_items=[
				frappe._dict(
					name="DNI-1",
					parent="DN-1",
					item_code="ITEM-1",
					against_sales_order="SO-1",
					stock_qty=10,
				)
			],
		)
		result = frappe._dict(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			flags=frappe._dict(aps_application_locked_state=locked_state),
		)
		with patch(
			"injection_aps.services.delivery_sync.get_schedule_delivery_lower_bounds",
			side_effect=AssertionError("ordinary delivery read used"),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "not fully represented"):
				change_engine._get_authoritative_target_deliveries(result, ["ROW-1"])

	def test_demand_delta_rejects_unsynchronized_locked_production_source(self):
		locked_state = frappe._dict(
			production_allocations=[],
			stock_entries=[
				frappe._dict(
					name="STE-1",
					docstatus=1,
					purpose="Manufacture",
					work_order="WO-1",
					posting_date="2026-08-20",
					posting_time="08:00:00",
				)
			],
			stock_entry_details=[
				frappe._dict(
					name="SED-1",
					parent="STE-1",
					item_code="ITEM-1",
					qty=10,
					is_finished_item=1,
				)
			],
			work_orders=[frappe._dict(name="WO-1", production_item="ITEM-1")],
		)
		with patch.object(frappe.db, "sql", side_effect=AssertionError("ordinary production read used")):
			with self.assertRaisesRegex(frappe.ValidationError, "not fully represented"):
				change_engine._get_locked_target_production_lower_bounds(
					frappe._dict(),
					["ROW-1"],
					locked_state,
				)

	def test_apply_idempotent_replay_releases_savepoint_without_mutation(self):
		doc = frappe._dict(
			name="CR-1",
			status="Applied",
			application_log="LOG-1",
			application_result_json='{"planning_run": "RUN-1", "idempotent_replay": 0}',
		)
		with (
			patch.object(frappe, "generate_hash", return_value="FIXED"),
			patch.object(frappe.db, "savepoint") as savepoint,
			patch.object(frappe.db, "release_savepoint") as release,
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(change_engine, "_get_application_scope_locked_change_request", return_value=doc),
			patch.object(change_engine, "_dispatch_apply") as dispatch,
		):
			result = change_engine.apply_change_request("CR-1")

		self.assertEqual(result["idempotent_replay"], 1)
		self.assertEqual(result["application_log"], "LOG-1")
		savepoint.assert_called_once_with("aps_change_apply_FIXED")
		release.assert_called_once_with("aps_change_apply_FIXED")
		rollback.assert_not_called()
		dispatch.assert_not_called()

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
		# Cancelling the production response must not rewrite gross customer demand.
		schedule_row = frappe.db.get_value(
			"Customer Delivery Schedule Item",
			self.fixture["schedule_item"],
			["qty", "schedule_date", "status"],
			as_dict=True,
		)
		self.assertEqual(schedule_row.qty, 100)
		self.assertEqual(getdate(schedule_row.schedule_date), self.fixture["due_date"])
		self.assertEqual(schedule_row.status, "Open")
		net_row = frappe.db.get_value(
			"APS Net Requirement",
			self.fixture["net_requirement"].name,
			["demand_qty", "planning_qty", "net_requirement_qty"],
			as_dict=True,
		)
		self.assertEqual(net_row.demand_qty, 100)
		self.assertEqual(net_row.planning_qty, 0)
		self.assertEqual(net_row.net_requirement_qty, 0)

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
		before_net = frappe.db.get_value(
			"APS Net Requirement",
			self.fixture["net_requirement"].name,
			["demand_qty", "planning_qty", "net_requirement_qty", "fulfillment_baseline_json"],
			as_dict=True,
		)
		before_schedule = frappe.db.get_value(
			"Customer Delivery Schedule Item",
			self.fixture["schedule_item"],
			["qty", "schedule_date", "balance_qty", "status"],
			as_dict=True,
		)

		with patch(
			"injection_aps.services.change_engine.consistency.recalculate_plan_consistency",
			side_effect=RuntimeError("forced consistency failure"),
		):
			with self.assertRaisesRegex(RuntimeError, "forced consistency failure"):
				change_engine.apply_change_request(request.name)

		self.assertEqual(frappe.db.get_value("APS Schedule Result", self.fixture["result"].name, "planned_qty"), before_result)
		after_segment = frappe.db.get_value("APS Schedule Segment", self.fixture["open_segment"], ["planned_qty", "segment_status"], as_dict=True)
		self.assertEqual(after_segment, before_segment)
		self.assertEqual(
			frappe.db.get_value(
				"APS Net Requirement",
				self.fixture["net_requirement"].name,
				["demand_qty", "planning_qty", "net_requirement_qty", "fulfillment_baseline_json"],
				as_dict=True,
			),
			before_net,
		)
		self.assertEqual(
			frappe.db.get_value(
				"Customer Delivery Schedule Item",
				self.fixture["schedule_item"],
				["qty", "schedule_date", "balance_qty", "status"],
				as_dict=True,
			),
			before_schedule,
		)
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
		self.assertEqual(
			frappe.db.get_value("Customer Delivery Schedule Item", self.fixture["schedule_item"], "qty"),
			100,
		)
		self.assertEqual(
			frappe.db.get_value("APS Net Requirement", self.fixture["net_requirement"].name, "demand_qty"),
			100,
		)

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
		self.assertEqual(
			frappe.db.get_value("Customer Delivery Schedule Item", self.fixture["schedule_item"], "qty"),
			100,
		)
		self.assertEqual(
			frappe.db.get_value("APS Net Requirement", self.fixture["net_requirement"].name, "demand_qty"),
			100,
		)

	def test_customer_schedule_date_change_requires_versioned_demand_delta(self):
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
				with self.assertRaisesRegex(frappe.ValidationError, "Schedule Import & Diff"):
					change_engine.analyze_change_request(request.name)
				self.assertEqual(
					getdate(frappe.db.get_value("APS Schedule Result", fixture["result"].name, "requested_date")),
					fixture["due_date"],
				)
				self.assertEqual(
					getdate(frappe.db.get_value("APS Net Requirement", fixture["net_requirement"].name, "demand_date")),
					fixture["due_date"],
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
		suffix = frappe.generate_hash(length=10)
		schedule = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": self.customer,
				"company": self.company,
				"schedule_scope": "CHANGE-ENGINE-{0}".format(suffix),
				"version_no": "CHANGE-ENGINE-{0}".format(suffix),
				"import_strategy": "Append",
				"source_type": "Customer Delivery Schedule",
				"status": "Active",
				"items": [
					{
						"item_code": self.item,
						"schedule_date": due_date,
						"qty": 100,
						"balance_qty": 100,
						"status": "Open",
					}
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
							"opening_allocated_qty": 0.0,
							"opening_produced_qty": 0.0,
							"opening_delivered_qty": 0.0,
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
				"fulfillment_baseline_json": baseline_json,
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
				"fulfillment_baseline_json": baseline_json,
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
			"schedule": schedule,
			"schedule_item": schedule_item,
			"due_date": due_date,
			"run": run,
			"net_requirement": net_requirement,
			"result": result,
			"locked_segment": next(row.name for row in segments if row.is_locked),
			"open_segment": next(row.name for row in segments if not row.is_locked),
			"request": request,
		}
