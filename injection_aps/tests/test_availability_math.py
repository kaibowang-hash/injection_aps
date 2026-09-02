from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from injection_aps.services import availability, planning


class TestAvailabilityQuantityMath(unittest.TestCase):
	@staticmethod
	def _result(**overrides):
		values = {
			"name": "RES-1",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"item_code": "FG-1",
			"requested_date": date(2026, 8, 12),
			"demand_source": "Sales Order Backlog",
			"production_strategy": "Auto Balance",
			"planned_qty": 100,
			"prebuild_qty": 0,
			"jit_qty": 100,
			"early_days": 0,
			"late_qty_before_balance": 0,
			"late_qty_after_balance": 0,
			"delivered_qty": 0,
			"fulfillment_baseline_json": {
				"version": 3,
				"net_requirement": {
					"demand_qty": 100,
					"available_stock_qty": 0,
					"open_work_order_qty": 0,
					"existing_work_order_policy": "Include",
				},
				"sales_order_items": [
					{
						"sales_order": "SO-1",
						"sales_order_item": "SOI-1",
						"item_code": "FG-1",
						"source_open_qty": 100,
						"opening_delivered_qty": 10,
					}
				],
			},
		}
		values.update(overrides)
		return availability.frappe._dict(values)

	@staticmethod
	def _production(qty):
		return [
			{
				"segment": "SEG-1",
				"good_qty": qty,
				"scrap_qty": 0,
				"source_posting_time": datetime(2026, 8, 11, 12),
				"source_stock_entry": "STE-1",
			}
		]

	@staticmethod
	def _segment(qty):
		return {
			"name": "SEG-1",
			"start_time": datetime(2026, 8, 11, 8),
			"end_time": datetime(2026, 8, 11, 18),
			"planned_qty": qty,
			"production_mode": "JIT",
		}

	def test_minimum_batch_output_is_capped_by_frozen_customer_demand(self):
		result = self._result(
			planned_qty=100,
			fulfillment_baseline_json={
				"version": 3,
				"targets": [
					{
						"customer_schedule_item": "TARGET-1",
						"sales_order": "SO-1",
						"item_code": "FG-1",
						"source_open_qty": 10,
					}
				],
				"net_requirement": {"demand_qty": 999},
			},
		)
		production = self._production(100)
		projection = availability._build_result_projection(
			result,
			[self._segment(100)],
			{"SEG-1": production},
			production,
			[],
			{},
			opening_qty=0,
			as_of=datetime(2026, 8, 11, 14),
		)

		self.assertEqual(projection["fulfillment_demand_qty"], 10)
		self.assertEqual(projection["current_deliverable_qty"], 10)
		self.assertEqual(projection["cancellation_inventory_risk_qty"], 90)

	def test_live_net_requirement_is_the_legacy_fallback_not_planned_qty(self):
		result = self._result(
			net_requirement="NR-1",
			planned_qty=100,
			fulfillment_baseline_json={"version": 2, "targets": []},
		)
		with patch.object(
			availability.frappe,
			"get_all",
			return_value=[availability.frappe._dict(name="NR-1", demand_qty=25)],
		):
			availability._annotate_result_fulfillment_demands([result])

		self.assertEqual(result.fulfillment_demand_qty, 25)
		self.assertNotEqual(result.fulfillment_demand_qty, result.planned_qty)

	def test_stock_only_result_with_zero_planned_qty_can_use_opening_fg(self):
		result = self._result(
			sales_order=None,
			sales_order_item=None,
			demand_source="Forecast",
			planned_qty=0,
			prebuild_qty=0,
			jit_qty=0,
			fulfillment_baseline_json={
				"version": 3,
				"targets": [],
				"sales_order_items": [],
				"net_requirement": {"demand_qty": 50},
			},
		)
		projection = availability._build_result_projection(
			result,
			[],
			{},
			[],
			[],
			{},
			opening_qty=50,
			as_of=datetime(2026, 8, 11, 14),
		)

		self.assertEqual(projection["fulfillment_demand_qty"], 50)
		self.assertEqual(projection["current_deliverable_qty"], 50)

	def test_actual_output_reserved_for_another_so_is_not_deliverable(self):
		result = self._result(
			planned_qty=50,
			fulfillment_baseline_json={
				"version": 3,
				"sales_order_items": [
					{
						"sales_order": "SO-1",
						"sales_order_item": "SOI-1",
						"item_code": "FG-1",
						"source_open_qty": 50,
					}
				],
				"net_requirement": {"demand_qty": 50},
			},
		)
		fake_db = MagicMock()
		fake_db.exists.return_value = True
		fake_db.sql.return_value = [
			availability.frappe._dict(
				item_code="FG-1",
				actual_qty=50,
				reserved_qty=50,
				reserved_stock=0,
				reserved_qty_for_production=0,
				reserved_qty_for_sub_contract=0,
				reserved_qty_for_production_plan=0,
			)
		]
		original_db = getattr(availability.frappe.local, "db", None)
		availability.frappe.local.db = fake_db
		try:
			with (
				patch.object(availability, "_get_finished_goods_warehouses", return_value=["FG-WH"]),
				# The only reservation belongs to SO-2, so the exact SO-1/SOI-1
				# Result receives no reservation credit.
				patch.object(planning, "_get_sales_order_reservation_rows", return_value=[]),
				patch.object(
					planning,
					"get_settings_dict",
					return_value={"item_safety_stock_field": "safety_stock"},
				),
				patch.object(planning, "_get_item_mapping_values", return_value={"FG-1": 0}),
			):
				usable_stock = availability._get_company_fulfillment_finished_goods_stock(
					"COMPANY-1",
					[result],
				)
		finally:
			if original_db is None:
				del availability.frappe.local.db
			else:
				availability.frappe.local.db = original_db

		self.assertEqual(usable_stock, {"FG-1": 0})
		production = self._production(50)
		projection = availability._build_result_projection(
			result,
			[self._segment(50)],
			{"SEG-1": production},
			production,
			[],
			{},
			opening_qty=0,
			current_physical_stock_limit=usable_stock["FG-1"],
			as_of=datetime(2026, 8, 11, 14),
		)
		self.assertEqual(projection["actual_good_qty"], 50)
		self.assertEqual(projection["current_deliverable_qty"], 0)

	def test_safety_stock_is_not_released_to_customer_fulfillment(self):
		result = self._result()
		with (
			patch.object(availability, "_get_finished_goods_warehouses", return_value=["FG-WH"]),
			patch.object(planning, "_get_available_stock_map", return_value={"FG-1": 100}),
			patch.object(
				planning,
				"get_settings_dict",
				return_value={"item_safety_stock_field": "safety_stock"},
			),
			patch.object(planning, "_get_item_mapping_values", return_value={"FG-1": 20}),
		):
			usable_stock = availability._get_company_fulfillment_finished_goods_stock(
				"COMPANY-1",
				[result],
			)
		self.assertEqual(usable_stock, {"FG-1": 80})

	def test_partial_target_with_existing_history_emits_unresolved_baseline_warning(self):
		warnings = []
		with patch.object(availability, "_", side_effect=lambda value: value):
			availability._append_unresolved_fulfillment_warnings(
				{
					"RES-1": [
						{
							"name": "TARGET-1",
							"qty": 100,
							"attributed_qty": 10,
							"allocated_qty": 60,
							"delivered_qty": 30,
						}
					]
				},
				warnings,
			)

		self.assertEqual(len(warnings), 1)
		self.assertEqual(warnings[0]["code"], "FULFILLMENT_BASELINE_UNRESOLVED")
		self.assertEqual(warnings[0]["result"], "RES-1")
		self.assertEqual(warnings[0]["customer_schedule_item"], "TARGET-1")

	def test_full_target_or_partial_target_without_history_does_not_warn(self):
		warnings = []
		availability._append_unresolved_fulfillment_warnings(
			{
				"RES-1": [
					{"name": "FULL", "qty": 100, "attributed_qty": 100, "delivered_qty": 30},
					{"name": "CLEAN", "qty": 100, "attributed_qty": 10, "delivered_qty": 0},
				]
			},
			warnings,
		)

		self.assertEqual(warnings, [])

	def test_target_allocation_is_not_deducted_twice_from_physical_availability(self):
		"""Allocated demand coverage is not another stock/production movement."""
		result = availability.frappe._dict(
			{
				"name": "RES-1",
				"customer": "CUSTOMER-1",
				"item_code": "FG-1",
				"requested_date": date(2026, 8, 12),
				"demand_source": "Customer Delivery Schedule",
				"production_strategy": "Auto Balance",
				"planned_qty": 100,
				"prebuild_qty": 0,
				"jit_qty": 100,
				"early_days": 0,
				"late_qty_before_balance": 0,
				"late_qty_after_balance": 0,
				"delivered_qty": 30,
			}
		)
		segment = {
			"name": "SEG-1",
			"start_time": datetime(2026, 8, 11, 8),
			"end_time": datetime(2026, 8, 11, 18),
			"planned_qty": 100,
			"production_mode": "JIT",
		}
		production = [
			{
				"segment": "SEG-1",
				"good_qty": 60,
				"scrap_qty": 0,
				"source_posting_time": datetime(2026, 8, 11, 12),
				"source_stock_entry": "STE-1",
			}
		]
		target = {
			"name": "TARGET-1",
			"qty": 100,
			"attributed_qty": 100,
			"allocated_qty": 60,
			"delivered_qty": 30,
		}
		deliveries = {
			"TARGET-1": [
				{
					"effective_qty": 30,
					"source_posting_time": datetime(2026, 8, 11, 13),
					"source_delivery_note": "DN-1",
				}
			]
		}

		projection = availability._build_result_projection(
			result,
			[segment],
			{"SEG-1": production},
			production,
			[target],
			deliveries,
			opening_qty=0,
			as_of=datetime(2026, 8, 11, 14),
		)

		self.assertEqual(projection["allocated_qty"], 60)
		self.assertEqual(projection["actual_good_qty"], 60)
		self.assertEqual(projection["delivered_qty"], 30)
		self.assertEqual(projection["current_available_to_promise_qty"], 30)
		self.assertEqual(projection["current_deliverable_qty"], 30)

	def test_backlog_delivery_events_reduce_atp_at_the_exact_as_of_time(self):
		result = self._result()
		delivery_history = [
			availability.frappe._dict(
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="FG-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				source_delivery_note="DN-OPENING",
				source_delivery_note_item="DNI-OPENING",
				source_posting_time=datetime(2026, 8, 11, 9),
				effective_qty=10,
			),
			availability.frappe._dict(
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="FG-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				source_delivery_note="DN-POST-BASELINE",
				source_delivery_note_item="DNI-POST-BASELINE",
				source_posting_time=datetime(2026, 8, 11, 13),
				effective_qty=30,
			),
			availability.frappe._dict(
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="FG-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				source_delivery_note="DN-FUTURE",
				source_delivery_note_item="DNI-FUTURE",
				source_posting_time=datetime(2026, 8, 11, 16),
				effective_qty=20,
			),
			# Same SO item text is still rejected when the customer scope differs.
			availability.frappe._dict(
				company="COMPANY-1",
				customer="CUSTOMER-2",
				item_code="FG-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				source_delivery_note="DN-OTHER-CUSTOMER",
				source_delivery_note_item="DNI-OTHER-CUSTOMER",
				source_posting_time=datetime(2026, 8, 11, 13),
				effective_qty=99,
			),
		]
		with patch.object(availability.frappe.db, "sql", return_value=delivery_history):
			events = availability._get_backlog_incremental_delivery_events_map([result])

		self.assertEqual([row["qty"] for row in events["RES-1"]], [30, 20])
		segment = {
			"name": "SEG-1",
			"start_time": datetime(2026, 8, 11, 8),
			"end_time": datetime(2026, 8, 11, 18),
			"planned_qty": 100,
			"production_mode": "JIT",
		}
		production = [
			{
				"segment": "SEG-1",
				"good_qty": 100,
				"scrap_qty": 0,
				"source_posting_time": datetime(2026, 8, 11, 12),
				"source_stock_entry": "STE-1",
			}
		]
		projection = availability._build_result_projection(
			result,
			[segment],
			{"SEG-1": production},
			production,
			[],
			{},
			opening_qty=0,
			as_of=datetime(2026, 8, 11, 14),
			backlog_delivery_events=events["RES-1"],
		)

		self.assertEqual(projection["actual_good_qty"], 100)
		self.assertEqual(projection["delivered_qty"], 30)
		self.assertEqual(projection["current_available_to_promise_qty"], 70)
		self.assertEqual(projection["current_deliverable_qty"], 70)
		self.assertEqual(projection["delivery_source_documents"], ["DN-POST-BASELINE"])

	def test_late_and_future_execution_do_not_leak_into_jit_or_as_of_headlines(self):
		result = self._result(
			requested_date=date(2026, 8, 11),
			demand_source="Customer Delivery Schedule",
			planned_qty=200,
			jit_qty=160,
			fulfillment_baseline_json={"version": 2, "targets": []},
		)
		segments = [
			{
				"name": name,
				"start_time": start_time,
				"end_time": end_time,
				"planned_qty": qty,
				"production_mode": mode,
			}
			for name, mode, qty, start_time, end_time in (
				("SEG-PRE", "Prebuild", 10, datetime(2026, 8, 10, 8), datetime(2026, 8, 10, 18)),
				("SEG-JIT", "JIT", 60, datetime(2026, 8, 11, 8), datetime(2026, 8, 11, 18)),
				("SEG-LATE", "Late", 30, datetime(2026, 8, 12, 8), datetime(2026, 8, 12, 18)),
				("SEG-FUTURE", "Late", 40, datetime(2026, 8, 12, 14), datetime(2026, 8, 12, 18)),
			)
		]
		production = [
			{
				"segment": segment,
				"good_qty": good,
				"scrap_qty": scrap,
				"source_posting_time": posting_time,
				"source_stock_entry": source,
			}
			for segment, good, scrap, posting_time, source in (
				("SEG-PRE", 10, 0, datetime(2026, 8, 10, 9), "STE-PRE"),
				("SEG-JIT", 20, 0, datetime(2026, 8, 11, 10), "STE-JIT"),
				("SEG-LATE", 30, 5, datetime(2026, 8, 12, 11), "STE-LATE"),
				("SEG-FUTURE", 40, 7, datetime(2026, 8, 12, 16), "STE-FUTURE"),
			)
		]
		deliveries = {
			"TARGET-1": [
				{
					"effective_qty": 5,
					"source_posting_time": datetime(2026, 8, 12, 11, 30),
					"source_delivery_note": "DN-PAST",
				},
				{
					"effective_qty": 7,
					"source_posting_time": datetime(2026, 8, 12, 15),
					"source_delivery_note": "DN-FUTURE",
				},
			]
		}
		projection = availability._build_result_projection(
			result,
			segments,
			{},
			production,
			[{"name": "TARGET-1", "qty": 200, "attributed_qty": 200, "allocated_qty": 0}],
			deliveries,
			opening_qty=0,
			as_of=datetime(2026, 8, 12, 12),
		)

		self.assertEqual(projection["actual_good_qty"], 60)
		self.assertEqual(projection["scrap_qty"], 5)
		self.assertEqual(projection["prebuild_actual_good_qty"], 10)
		self.assertEqual(projection["jit_actual_good_qty"], 20)
		self.assertEqual(projection["late_actual_good_qty"], 30)
		self.assertEqual(projection["delivered_qty"], 5)
		self.assertEqual(projection["current_available_to_promise_qty"], 55)
		self.assertEqual(projection["last_actual_report_time"], datetime(2026, 8, 12, 11))
		self.assertEqual(
			projection["production_source_documents"],
			["STE-PRE", "STE-JIT", "STE-LATE"],
		)
		self.assertEqual(projection["delivery_source_documents"], ["DN-PAST"])

	def test_cross_midnight_fixed_segment_actuals_use_natural_day_not_stale_mode(self):
		result = self._result(
			requested_date=date(2026, 8, 11),
			demand_source="Customer Delivery Schedule",
			planned_qty=90,
			prebuild_qty=30,
			jit_qty=60,
		)
		segment = {
			"name": "SEG-FIXED",
			"start_time": datetime(2026, 8, 10, 20),
			"end_time": datetime(2026, 8, 11, 8),
			"planned_qty": 90,
			# A protected segment cannot be physically split; this whole-row value
			# is stale for the post-midnight part.
			"production_mode": "Prebuild",
		}
		production = [
			{
				"segment": "SEG-FIXED",
				"good_qty": 30,
				"scrap_qty": 0,
				"source_posting_time": datetime(2026, 8, 10, 23),
				"source_stock_entry": "STE-PRE",
			},
			{
				"segment": "SEG-FIXED",
				"good_qty": 60,
				"scrap_qty": 0,
				"source_posting_time": datetime(2026, 8, 11, 7),
				"source_stock_entry": "STE-JIT",
			},
		]

		projection = availability._build_result_projection(
			result,
			[segment],
			{"SEG-FIXED": production},
			production,
			[],
			{},
			opening_qty=0,
			as_of=datetime(2026, 8, 11, 9),
		)

		self.assertEqual(projection["prebuild_actual_good_qty"], 30)
		self.assertEqual(projection["jit_actual_good_qty"], 60)
		self.assertEqual(projection["late_actual_good_qty"], 0)

	def test_opening_stock_replay_handles_delivery_returns_as_inverse_movements(self):
		with (
			patch.object(
				availability.frappe.db,
				"sql",
				side_effect=[
					[availability.frappe._dict({"item_code": "FG-1", "qty": 10})],
					[availability.frappe._dict({"item_code": "FG-1", "qty": -4})],
				],
			),
			patch.object(availability, "now_datetime", return_value=datetime(2026, 8, 11, 12)),
		):
			opening = availability._derive_opening_stock_by_item(
				"COMPANY-1",
				{"FG-1": 106},
				datetime(2026, 8, 11, 12),
				run_produced_through={"FG-1": 20},
				run_delivered_through={"FG-1": -5},
			)

		# 106 - future production 10 - future return 4 - current-run production 20
		# - current-run return 5 = 67.  Replaying both returns later restores stock.
		self.assertEqual(opening, {"FG-1": 67})


if __name__ == "__main__":
	unittest.main()
