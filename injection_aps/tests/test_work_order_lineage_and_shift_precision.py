from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

import frappe

from injection_aps.services import planning


def _raise_validation(message, *_args, **_kwargs):
	raise frappe.ValidationError(str(message))


class TestWorkOrderLineageAndShiftPrecision(unittest.TestCase):
	def test_legacy_customer_demand_without_sales_order_never_becomes_stock_production(self):
		with patch("injection_aps.services.v2_flags.is_v2_enabled", return_value=False):
			lineage = planning._get_result_sales_order_lineage(
				frappe._dict(
					name="RESULT-NO-SO",
					item_code="FG-1",
					customer="CUSTOMER-1",
					demand_source="Customer Delivery Schedule",
					demand_source_snapshot_json=[],
				)
			)
		self.assertIn("no exact Sales Order", lineage["blocking_reason"])
		self.assertIsNone(
			planning._get_aps_work_order_stock_pool(
				demand_source="Customer Delivery Schedule",
				sales_order=None,
				sales_order_item=None,
			)
		)
		self.assertEqual(
			planning._get_aps_work_order_stock_pool(
				demand_source="Stock Production",
				sales_order=None,
				sales_order_item=None,
			),
			"Stock Production",
		)

	def test_stock_production_work_order_reuse_requires_exact_stock_purpose(self):
		row = frappe._dict(name="WO-STOCK-1")
		snapshot = {
			"name": "WO-STOCK-1",
			"company": "COMPANY-1",
			"production_item": "FG-1",
			"sales_order": None,
			"sales_order_item": None,
			"custom_aps_source": "Stock Production",
			"custom_aps_result_reference": None,
			"custom_aps_run": None,
			"has_execution": False,
			"planned_start_date": "2026-08-11 08:00:00",
		}
		with (
			patch.object(planning.frappe, "get_all", return_value=[row]),
			patch.object(
				planning,
				"_get_work_order_reconciliation_snapshot",
				return_value=snapshot,
			),
		):
			matched = planning._find_existing_work_order_for_result(
				"RESULT-1",
				"FG-1",
				company="COMPANY-1",
				run_name="RUN-1",
				stock_purpose="Stock Production",
				target_result={"name": "RESULT-1"},
				require_unique=True,
			)
			wrong_pool = planning._find_existing_work_order_for_result(
				"RESULT-1",
				"FG-1",
				company="COMPANY-1",
				run_name="RUN-1",
				stock_purpose="Safety Stock",
				target_result={"name": "RESULT-1"},
				require_unique=True,
			)

		self.assertEqual(matched["name"], "WO-STOCK-1")
		self.assertIsNone(wrong_pool)

	def test_changed_schedule_can_transfer_only_unstarted_exact_work_order(self):
		baseline = {
			"version": 3,
			"targets": [
				{"customer_schedule_item": "TARGET-NEW", "retired": 0}
			],
		}
		owner = frappe._dict(
			name="RESULT-OLD",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			item_code="FG-1",
			status="Blocked",
			flow_step="Customer Schedule Changed",
			fulfillment_baseline_json=baseline,
		)
		target = {
			"name": "RESULT-NEW",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"item_code": "FG-1",
			"fulfillment_baseline_json": baseline,
		}
		base_snapshot = {
			"custom_aps_result_reference": "RESULT-OLD",
			"custom_aps_run": "RUN-OLD",
			"has_execution": False,
		}
		with patch.object(planning.frappe, "get_all", return_value=[owner]):
			self.assertTrue(
				planning._work_order_can_be_controlled_reused(
					base_snapshot,
					result_name="RESULT-NEW",
					run_name="RUN-NEW",
					target_result=target,
				)
			)
			self.assertFalse(
				planning._work_order_can_be_controlled_reused(
					{**base_snapshot, "has_execution": True},
					result_name="RESULT-NEW",
					run_name="RUN-NEW",
					target_result=target,
				)
			)

	def test_submitted_work_order_rewrite_refreshes_erpnext_derivatives(self):
		work_order = MagicMock()
		work_order.docstatus = 1
		work_order.production_plan = None

		planning._save_work_order_with_controller(work_order)

		work_order.save.assert_called_once_with(ignore_permissions=True)
		work_order.update_work_order_qty_in_so.assert_called_once_with()
		work_order.update_ordered_qty.assert_called_once_with()
		work_order.update_planned_qty.assert_called_once_with()
		work_order.update_reserved_qty_for_production.assert_called_once_with()

	def test_existing_work_order_coverage_restores_one_total_production_boundary(self):
		self.assertEqual(
			planning._net_requirement_production_target_qty(
				frappe._dict(
					open_work_order_qty=60,
					net_requirement_qty=40,
					planning_qty=40,
				)
			),
			100,
		)
		# Minimum-batch expansion is not added twice to the covered quantity.
		self.assertEqual(
			planning._net_requirement_production_target_qty(
				frappe._dict(
					open_work_order_qty=60,
					net_requirement_qty=40,
					planning_qty=100,
				)
			),
			100,
		)
		self.assertTrue(
			planning._net_requirement_requires_result(
				frappe._dict(
					open_work_order_qty=100,
					net_requirement_qty=0,
					available_stock_qty=0,
				)
			)
		)

	def test_frozen_existing_work_order_coverage_is_part_of_proposal_fingerprint(self):
		base = {
			"result_reference": "RESULT-1",
			"item_code": "ITEM-1",
			"action": "Update Existing",
			"proposed_qty": 100,
			"existing_work_order": "WO-1",
			"existing_qty": 60,
		}
		first = planning._work_order_proposal_fingerprint(
			"RUN-1", [dict(base, covered_existing_qty=60)]
		)
		changed = planning._work_order_proposal_fingerprint(
			"RUN-1", [dict(base, covered_existing_qty=50)]
		)
		self.assertNotEqual(first, changed)

	def test_fractional_shift_slices_conserve_exact_total(self):
		segment = {
			"name": "SEG-1",
			"item_code": "ITEM-1",
			"start_time": "2026-08-11 08:00:00",
			"end_time": "2026-08-12 08:00:00",
			"planned_qty": 1.5,
		}
		with (
			patch.object(planning.frappe, "get_precision", return_value=6),
			patch.object(planning, "_item_quantity_requires_integer", return_value=False),
		):
			slices = planning._split_segment_into_shift_slices(segment)
		self.assertEqual([row["planned_qty"] for row in slices], [0.75, 0.75])
		self.assertAlmostEqual(sum(row["planned_qty"] for row in slices), 1.5, places=6)

	def test_last_shift_slice_absorbs_target_precision_difference(self):
		segment = {
			"name": "SEG-1",
			"item_code": "ITEM-1",
			"start_time": "2026-08-11 08:00:00",
			"end_time": "2026-08-12 08:00:00",
			"planned_qty": 1.23,
		}
		with (
			patch.object(planning.frappe, "get_precision", return_value=2),
			patch.object(planning, "_item_quantity_requires_integer", return_value=False),
		):
			slices = planning._split_segment_into_shift_slices(segment)
		self.assertEqual([row["planned_qty"] for row in slices], [0.61, 0.62])
		self.assertAlmostEqual(sum(row["planned_qty"] for row in slices), 1.23, places=2)

	def test_unrepresentable_target_precision_is_blocked_not_rounded(self):
		segment = {
			"name": "SEG-FRACTION",
			"item_code": "ITEM-1",
			"start_time": "2026-08-11 08:00:00",
			"end_time": "2026-08-12 08:00:00",
			"planned_qty": 1.5,
		}
		with (
			patch.object(planning.frappe, "get_precision", return_value=0),
			patch.object(planning, "_item_quantity_requires_integer", return_value=False),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "cannot be represented"):
				planning._split_segment_into_shift_slices(segment)

	def test_whole_number_uom_produces_only_integer_slices(self):
		segment = {
			"name": "SEG-INTEGER",
			"item_code": "ITEM-INTEGER",
			"start_time": "2026-08-11 08:00:00",
			"end_time": "2026-08-12 08:00:00",
			"planned_qty": 3,
		}
		with (
			patch.object(planning.frappe, "get_precision", return_value=6),
			patch.object(planning, "_item_quantity_requires_integer", return_value=True),
		):
			slices = planning._split_segment_into_shift_slices(segment)
		quantities = [row["planned_qty"] for row in slices]
		self.assertEqual(quantities, [1.0, 2.0])
		self.assertEqual(sum(quantities), 3.0)
		self.assertTrue(all(quantity.is_integer() for quantity in quantities))

	def test_unique_soi_fallback_is_written_to_created_work_order(self):
		result = frappe._dict(
			name="RESULT-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item=None,
			demand_source="Customer Delivery Schedule",
			demand_source_snapshot_json=[{"sales_order": "SO-1"}],
			requested_date="2026-08-12",
			is_urgent=0,
		)
		with patch.object(planning, "_resolve_unique_sales_order_item", return_value="SOI-1"):
			lineage = planning._get_result_sales_order_lineage(result)
		self.assertEqual(lineage["sales_order_item"], "SOI-1")

		captured = {}
		work_order = MagicMock(name="formal_work_order")
		work_order.name = "WO-1"

		def make_doc(values):
			captured.update(values)
			return work_order

		with (
			patch.object(planning, "_resolve_item_name", side_effect=lambda value: value),
			patch.object(planning.frappe.db, "get_value", return_value="BOM-1"),
			patch.object(planning, "_get_primary_segments_for_result", return_value=[]),
			patch.object(planning, "_get_primary_result_plant_floor", return_value=None),
			patch.object(planning, "_get_work_order_warehouse_values", return_value={}),
			patch.object(planning.frappe, "get_doc", side_effect=make_doc),
			patch.object(planning.frappe, "get_meta") as get_meta,
		):
			get_meta.return_value.has_field.return_value = False
			planning._create_formal_work_order(
				run_doc=frappe._dict(name="RUN-1", company="COMPANY-1", plant_floor=None),
				result=result,
				qty=10,
				start_time="2026-08-11 08:00:00",
				end_time="2026-08-11 10:00:00",
				settings={},
				proposal_batch="WOP-1",
				sales_order=lineage["sales_order"],
				sales_order_item=lineage["sales_order_item"],
			)
		self.assertEqual(captured["sales_order"], "SO-1")
		self.assertEqual(captured["sales_order_item"], "SOI-1")
		work_order.insert.assert_called_once_with(ignore_permissions=True)
		work_order.submit.assert_called_once_with()

	def test_apply_state_locks_exact_so_and_soi_before_work_orders(self):
		row = frappe._dict(
			result_reference="RESULT-1",
			existing_work_order=None,
			sales_order="SO-1",
			sales_order_item="SOI-1",
		)
		with (
			patch.object(planning, "_lock_named_rows") as lock_rows,
			patch.object(planning, "_get_open_work_orders_by_result", return_value={}),
		):
			state = planning._prepare_work_order_apply_state(
				frappe._dict(name="WOP-1"),
				[row],
			)
		self.assertEqual(
			lock_rows.call_args_list[:3],
			[
				call("APS Schedule Result", ["RESULT-1"]),
				call("Sales Order", {"SO-1"}),
				call("Sales Order Item", {"SOI-1"}),
			],
		)
		self.assertTrue(state["exact_sales_order_lineage_locked"])

	def test_exact_soi_validator_rejects_detail_from_another_order(self):
		def get_value(doctype, _name, _fields, **_kwargs):
			if doctype == "Sales Order":
				return frappe._dict(
					name="SO-1",
					docstatus=1,
					status="To Deliver and Bill",
					company="COMPANY-1",
					customer="CUSTOMER-1",
				)
			return frappe._dict(
				name="SOI-WRONG",
				parent="SO-2",
				parenttype="Sales Order",
				item_code="ITEM-1",
			)

		with (
			patch.object(planning.frappe.db, "get_value", side_effect=get_value),
			patch.object(planning, "_normalize_item_code", side_effect=lambda value: value or ""),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not belong"):
				planning._validate_exact_sales_order_lineage(
					{"sales_order": "SO-1", "sales_order_item": "SOI-WRONG"},
					item_code="ITEM-1",
					company="COMPANY-1",
					customer="CUSTOMER-1",
				)

	def test_other_active_result_work_order_is_not_selected_or_reassigned(self):
		snapshot = {
			"name": "WO-OLD",
			"company": "COMPANY-1",
			"production_item": "ITEM-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"custom_aps_run": "RUN-OLD",
			"custom_aps_result_reference": "RESULT-OLD",
			"planned_start_date": "2026-08-10 08:00:00",
		}
		with (
			patch.object(
				planning.frappe,
				"get_all",
				return_value=[frappe._dict(name="WO-OLD")],
			),
			patch.object(planning, "_get_work_order_reconciliation_snapshot", return_value=snapshot),
			patch.object(planning, "_result_lineage_is_explicitly_retired", return_value=False),
			patch.object(planning, "_normalize_item_code", side_effect=lambda value: value or ""),
		):
			selected = planning._find_existing_work_order_for_result(
				"RESULT-NEW",
				"ITEM-1",
				company="COMPANY-1",
				run_name="RUN-NEW",
				sales_order="SO-1",
				sales_order_item="SOI-1",
			)
		self.assertIsNone(selected)

	def test_apply_revalidation_blocks_other_active_result_owner(self):
		result = frappe._dict(
			name="RESULT-NEW",
			planning_run="RUN-NEW",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			machine_scheduled_qty=10,
		)
		snapshot = {
			"name": "WO-OLD",
			"company": "COMPANY-1",
			"production_item": "ITEM-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"qty": 10,
			"produced_qty": 0,
			"material_transferred_for_manufacturing": 0,
			"docstatus": 1,
			"status": "Not Started",
			"custom_aps_run": "RUN-OLD",
			"custom_aps_result_reference": "RESULT-OLD",
			"scheduling_rows": [],
		}
		row = frappe._dict(
			action="Keep Existing",
			result_reference="RESULT-NEW",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			proposed_qty=10,
			existing_work_order="WO-OLD",
			existing_state_token=planning._work_order_proposal_state_token(snapshot),
			result_state_token=planning._work_order_result_proposal_state_token(result, []),
		)
		with (
			patch.object(
				planning,
				"_validate_exact_sales_order_lineage",
				return_value={
					"sales_order": "SO-1",
					"sales_order_item": "SOI-1",
					"can_reuse": True,
				},
			),
			patch.object(planning, "_result_lineage_is_explicitly_retired", return_value=False),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "another active APS run or result"):
				planning._validate_work_order_proposal_row_current(
					row=row,
					batch=frappe._dict(name="WOP-1", planning_run="RUN-NEW"),
					run_doc=frappe._dict(name="RUN-NEW", company="COMPANY-1"),
					result_doc=result,
					primary_segments=[],
					apply_state={
						"open_by_result": {},
						"work_order_snapshots": {"WO-OLD": snapshot},
						"exact_sales_order_lineage_locked": True,
					},
				)

	def test_explicitly_retired_result_allows_controlled_reuse(self):
		snapshot = {
			"custom_aps_run": "RUN-OLD",
			"custom_aps_result_reference": "RESULT-OLD",
		}
		with patch.object(planning, "_result_lineage_is_explicitly_retired", return_value=True):
			self.assertTrue(
				planning._work_order_can_be_controlled_reused(
					snapshot,
					result_name="RESULT-NEW",
					run_name="RUN-NEW",
				)
			)

	def test_item_only_legacy_release_paths_are_removed(self):
		self.assertFalse(hasattr(planning, "_sync_existing_work_orders_to_scheduling"))
		self.assertFalse(hasattr(planning, "_ensure_released_work_order"))


if __name__ == "__main__":
	unittest.main()
