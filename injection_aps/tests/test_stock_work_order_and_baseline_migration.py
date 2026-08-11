from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.patches.v0_0_2 import upgrade_applied_fulfillment_baseline_v3
from injection_aps.services import capacity_balance, planning


def _raise_validation(message, *_args, **_kwargs):
	raise frappe.ValidationError(message)


class _InsertedNetRequirement:
	def __init__(self, values, index):
		self.values = values
		self.name = f"APS-NET-STOCK-{index}"

	def insert(self, ignore_permissions=False):
		return self


class TestExplicitStockWorkOrderPools(TestCase):
	def test_demand_identity_never_falls_back_from_sales_order_to_item(self):
		self.assertEqual(
			planning._get_net_requirement_work_order_identity(
				company="COMPANY-1",
				item_code="FG-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				is_safety_stock=False,
			),
			("Sales Order", "COMPANY-1", "SO-1", "SOI-1", "FG-1"),
		)
		self.assertIsNone(
			planning._get_net_requirement_work_order_identity(
				company="COMPANY-1",
				item_code="FG-1",
				sales_order=None,
				sales_order_item=None,
				is_safety_stock=False,
			)
		)
		self.assertIsNone(
			planning._get_net_requirement_work_order_identity(
				company="COMPANY-1",
				item_code="FG-1",
				sales_order="SO-1",
				sales_order_item=None,
				is_safety_stock=False,
			)
		)
		self.assertEqual(
			planning._get_net_requirement_work_order_identity(
				company="COMPANY-1",
				item_code="FG-1",
				sales_order=None,
				sales_order_item=None,
				is_safety_stock=True,
			),
			("Stock Pool", "COMPANY-1", "Safety Stock", "", "FG-1"),
		)

	def test_open_work_orders_are_partitioned_by_company_so_item_or_explicit_stock_pool(self):
		rows = [
			frappe._dict(
				company="COMPANY-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				item_code="FG-1",
				custom_aps_source="Customer Delivery Schedule",
				open_qty=30,
			),
			frappe._dict(
				company="COMPANY-1",
				sales_order="SO-2",
				sales_order_item="SOI-2",
				item_code="FG-1",
				custom_aps_source="Customer Delivery Schedule",
				open_qty=40,
			),
			frappe._dict(
				company="COMPANY-1",
				sales_order=None,
				sales_order_item=None,
				item_code="FG-1",
				custom_aps_source="Stock Production",
				open_qty=50,
			),
			frappe._dict(
				company="COMPANY-1",
				sales_order=None,
				sales_order_item=None,
				item_code="FG-1",
				custom_aps_source="Safety Stock",
				open_qty=20,
			),
		]
		database = MagicMock()
		database.exists.return_value = True
		database.sql.return_value = rows
		with patch.object(planning.frappe, "db", database):
			result = planning._get_open_work_order_map("COMPANY-1")

		self.assertEqual(
			result,
			{
				("Sales Order", "COMPANY-1", "SO-1", "SOI-1", "FG-1"): 30,
				("Sales Order", "COMPANY-1", "SO-2", "SOI-2", "FG-1"): 40,
				("Stock Pool", "COMPANY-1", "Stock Production", "", "FG-1"): 50,
				("Stock Pool", "COMPANY-1", "Safety Stock", "", "FG-1"): 20,
			},
		)
		query, params = database.sql.call_args.args[:2]
		self.assertIn("custom_aps_run", query)
		self.assertIn("custom_aps_result_reference", query)
		self.assertIn("aps_result.planning_run = wo.custom_aps_run", query)
		self.assertIn("aps_result.item_code = wo.production_item", query)
		self.assertIn("aps_run.company = wo.company", query)
		self.assertIn("wo.produced_qty, 0) = 0", query)
		self.assertIn("wo.material_transferred_for_manufacturing, 0) = 0", query)
		self.assertIn("not exists", query)
		self.assertEqual(params, ["COMPANY-1"])

	def test_rebuild_consumes_stock_and_safety_work_orders_from_separate_pools(self):
		demand_rows = [
			frappe._dict(
				name="DEMAND-STOCK",
				company="COMPANY-1",
				customer=None,
				sales_order=None,
				sales_order_item=None,
				item_code="FG-1",
				demand_date="2026-08-11",
				qty=10,
				demand_source="Forecast",
			),
			frappe._dict(
				name="DEMAND-SAFETY",
				company="COMPANY-1",
				customer=None,
				sales_order=None,
				sales_order_item=None,
				item_code="FG-1",
				demand_date="2026-08-12",
				qty=10,
				demand_source="Safety Stock",
			),
		]
		open_work_orders = {
			("Stock Pool", "COMPANY-1", "Stock Production", "", "FG-1"): 6,
			("Stock Pool", "COMPANY-1", "Safety Stock", "", "FG-1"): 4,
			("Sales Order", "COMPANY-1", "SO-X", "SOI-X", "FG-1"): 99,
		}
		inserted = []

		def make_doc(values):
			inserted.append(values)
			return _InsertedNetRequirement(values, len(inserted))

		with ExitStack() as stack:
			stack.enter_context(patch.object(planning, "repair_item_references", return_value={}))
			stack.enter_context(patch.object(planning, "_delete_system_generated_rows"))
			stack.enter_context(patch.object(planning.frappe, "get_all", return_value=demand_rows))
			stack.enter_context(patch.object(planning, "_resolve_item_name", side_effect=lambda value: value))
			stack.enter_context(patch.object(planning, "_is_schedulable_item", return_value=True))
			stack.enter_context(patch.object(planning, "_get_available_stock_map", return_value={}))
			stack.enter_context(patch.object(planning, "_get_open_work_order_map", return_value=open_work_orders))
			stack.enter_context(patch.object(planning, "_get_item_mapping_value", return_value=0))
			stack.enter_context(
				patch.object(
					planning,
					"get_settings_dict",
					return_value={
						"item_safety_stock_field": "safety_stock",
						"item_max_stock_field": "max_stock",
						"item_min_batch_field": "minimum_batch",
					},
				)
			)
			stack.enter_context(patch.object(planning.frappe, "get_doc", side_effect=make_doc))
			stack.enter_context(patch.object(planning, "_", side_effect=lambda message, **_kwargs: message))
			planning.rebuild_net_requirements(
				company="COMPANY-1",
				existing_work_order_policy="Include",
			)

		stock_row = next(row for row in inserted if row["reason_text"].startswith("Demand 10") and row["demand_date"] == "2026-08-11")
		safety_row = next(row for row in inserted if row["demand_date"] == "2026-08-12")
		self.assertEqual(stock_row["open_work_order_qty"], 6)
		self.assertEqual(stock_row["net_requirement_qty"], 4)
		self.assertEqual(safety_row["open_work_order_qty"], 4)
		self.assertEqual(safety_row["net_requirement_qty"], 6)

	def test_no_so_work_order_creation_records_an_explicit_stock_purpose(self):
		captured = {}
		work_order = MagicMock()
		work_order.name = "WO-STOCK-1"

		def make_doc(values):
			captured.update(values)
			return work_order

		database = MagicMock()
		database.get_value.return_value = "BOM-1"
		with (
			patch.object(planning, "_resolve_item_name", return_value="FG-1"),
			patch.object(planning.frappe, "db", database),
			patch.object(planning, "_get_primary_segments_for_result", return_value=[]),
			patch.object(planning, "_get_primary_result_plant_floor", return_value=None),
			patch.object(planning, "_get_work_order_warehouse_values", return_value={}),
			patch.object(planning.frappe, "get_doc", side_effect=make_doc),
			patch.object(planning.frappe, "get_meta") as get_meta,
		):
			get_meta.return_value.has_field.return_value = False
			planning._create_formal_work_order(
				run_doc=frappe._dict(name="RUN-1", company="COMPANY-1", plant_floor=None),
				result=frappe._dict(
					name="RESULT-1",
					item_code="FG-1",
					demand_source="Stock Production",
					requested_date="2026-08-12",
					is_urgent=0,
				),
				qty=10,
				start_time="2026-08-11 08:00:00",
				end_time="2026-08-11 10:00:00",
				settings={},
				proposal_batch="WOP-1",
				sales_order=None,
				sales_order_item=None,
			)

		self.assertEqual(captured["custom_aps_source"], "Stock Production")
		self.assertIsNone(captured["sales_order"])
		self.assertIsNone(captured["sales_order_item"])


class TestAppliedBaselineV3Migration(TestCase):
	def test_v2_is_incomplete_and_valid_v3_is_durable(self):
		v2 = capacity_balance._persisted_net_requirement_evidence(
			{"fulfillment_baseline_json": {"version": 2, "targets": []}}
		)
		v3 = capacity_balance._persisted_net_requirement_evidence(
			{
				"fulfillment_baseline_json": {
					"version": 3,
					"net_requirement": {
						"demand_qty": 100,
						"available_stock_qty": 30,
						"open_work_order_qty": 20,
						"existing_work_order_policy": "Include",
					},
				}
			}
		)
		invalid = capacity_balance._persisted_net_requirement_evidence(
			{
				"fulfillment_baseline_json": {
					"version": 3,
					"net_requirement": {
						"demand_qty": 10,
						"available_stock_qty": 20,
						"open_work_order_qty": 0,
						"existing_work_order_policy": "Include",
					},
				}
			}
		)
		self.assertEqual(v2["complete"], 0)
		self.assertEqual(
			v3,
			{
				"version": 3,
				"complete": 1,
				"demand_qty": 100,
				"available_stock_qty": 30,
				"open_work_order_qty": 20,
				"existing_work_order_policy": "Include",
			},
		)
		self.assertEqual(invalid["complete"], 0)

	def test_missing_current_or_cross_run_evidence_blocks_capacity(self):
		analysis = {
			"summary": {"blocked_demands": 0},
			"demands": [{"result": "RESULT-NEW", "status": "Balanced", "checks": []}],
			"resource_blocks": [],
		}
		with patch.object(capacity_balance, "_", side_effect=lambda message, **_kwargs: message):
			capacity_balance._apply_missing_net_requirement_evidence_blocks(
				analysis,
				{
					"RESULT-NEW": {
						"name": "RESULT-NEW",
						"net_requirement_evidence_complete": 0,
					}
				},
				{
					"RESULT-OLD": {
						"name": "RESULT-OLD",
						"net_requirement_evidence_complete": 0,
					}
				},
			)
		self.assertEqual(analysis["summary"]["missing_net_requirement_evidence_results"], 2)
		self.assertEqual(analysis["summary"]["blocked_demands"], 1)
		self.assertEqual(len(analysis["resource_blocks"]), 2)
		self.assertEqual(analysis["demands"][0]["status"], "Blocked")
		self.assertEqual(len(analysis["demands"][0]["checks"]), 2)

	def test_release_guard_fails_closed_for_deleted_v2_source(self):
		with (
			patch.object(capacity_balance, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(capacity_balance.frappe, "throw", side_effect=_raise_validation),
			self.assertRaisesRegex(frappe.ValidationError, "Rebuild net requirements"),
		):
			capacity_balance._assert_net_requirement_evidence_complete(
				{
					"RESULT-OLD": {
						"name": "RESULT-OLD",
						"net_requirement_evidence_complete": 0,
					}
				},
				operation="formal release",
			)

	def test_run_loader_marks_deleted_v2_source_incomplete_and_v3_complete(self):
		results = [
			frappe._dict(
				name="RESULT-V2",
				net_requirement="NR-DELETED-2",
				fulfillment_baseline_json=json.dumps({"version": 2, "targets": []}),
			),
			frappe._dict(
				name="RESULT-V3",
				net_requirement="NR-DELETED-3",
				fulfillment_baseline_json=json.dumps(
					{
						"version": 3,
						"net_requirement": {
							"demand_qty": 20,
							"available_stock_qty": 5,
							"open_work_order_qty": 3,
							"existing_work_order_policy": "Include",
						},
					}
				),
			),
		]

		def get_all(doctype, **_kwargs):
			if doctype == "APS Schedule Result":
				return results
			if doctype in {"APS Net Requirement", "APS Schedule Segment"}:
				return []
			raise AssertionError(doctype)

		with patch.object(capacity_balance.frappe, "get_all", side_effect=get_all):
			loaded, segments = capacity_balance._get_run_balance_rows("RUN-1")

		self.assertEqual(segments, [])
		self.assertEqual(loaded["RESULT-V2"]["net_requirement_evidence_complete"], 0)
		self.assertEqual(loaded["RESULT-V3"]["net_requirement_evidence_complete"], 1)
		self.assertEqual(loaded["RESULT-V3"]["demand_qty"], 20)
		self.assertEqual(loaded["RESULT-V3"]["available_stock_qty"], 5)

	def test_patch_upgrades_only_v2_rows_with_a_live_source(self):
		rows = [
			frappe._dict(
				name="RESULT-UPGRADE",
				fulfillment_baseline_json=json.dumps(
					{"version": 2, "targets": [{"customer_schedule_item": "ROW-1"}]}
				),
				live_net_requirement="NR-1",
				demand_qty=100,
				available_stock_qty=25,
				open_work_order_qty=30,
				existing_work_order_policy="Include",
			),
			frappe._dict(
				name="RESULT-MISSING",
				fulfillment_baseline_json=json.dumps({"version": 2, "targets": []}),
				live_net_requirement=None,
				demand_qty=0,
				available_stock_qty=0,
				open_work_order_qty=0,
				existing_work_order_policy="Include",
			),
			frappe._dict(
				name="RESULT-V3",
				fulfillment_baseline_json=json.dumps(
					{
						"version": 3,
						"net_requirement": {
							"demand_qty": 5,
							"available_stock_qty": 1,
							"open_work_order_qty": 0,
							"existing_work_order_policy": "Exclude",
						},
					}
				),
				live_net_requirement="NR-3",
				demand_qty=5,
				available_stock_qty=1,
				open_work_order_qty=0,
				existing_work_order_policy="Exclude",
			),
			frappe._dict(
				name="RESULT-INVALID-NR",
				fulfillment_baseline_json=json.dumps({"version": 2, "targets": []}),
				live_net_requirement="NR-INVALID",
				demand_qty=100,
				available_stock_qty=60,
				open_work_order_qty=50,
				existing_work_order_policy="Include",
			),
		]
		database = MagicMock()
		database.sql.return_value = rows
		with (
			patch.object(upgrade_applied_fulfillment_baseline_v3, "_schema_is_ready", return_value=True),
			patch.object(upgrade_applied_fulfillment_baseline_v3.frappe, "db", database),
		):
			upgrade_applied_fulfillment_baseline_v3.execute()

		database.set_value.assert_called_once()
		self.assertEqual(database.set_value.call_args.args[:3], ("APS Schedule Result", "RESULT-UPGRADE", "fulfillment_baseline_json"))
		upgraded = json.loads(database.set_value.call_args.args[3])
		self.assertEqual(upgraded["version"], 3)
		self.assertEqual(
			upgraded["net_requirement"],
			{
				"demand_qty": 100.0,
				"available_stock_qty": 25.0,
				"open_work_order_qty": 30.0,
				"existing_work_order_policy": "Include",
			},
		)
		self.assertEqual(upgraded["targets"], [{"customer_schedule_item": "ROW-1"}])

	def test_patch_is_safe_when_fresh_install_schema_is_not_ready(self):
		database = MagicMock()
		database.exists.return_value = False
		with patch.object(upgrade_applied_fulfillment_baseline_v3.frappe, "db", database):
			upgrade_applied_fulfillment_baseline_v3.execute()
			database.sql.assert_not_called()

	def test_upgrade_patch_runs_after_existing_policy_backfill(self):
		patch_lines = [
			line.strip()
			for line in (Path(__file__).resolve().parents[1] / "patches.txt").read_text().splitlines()
			if line.strip() and not line.startswith("[")
		]
		self.assertLess(
			patch_lines.index("injection_aps.patches.v0_0_2.backfill_existing_work_order_policy"),
			patch_lines.index(
				"injection_aps.patches.v0_0_2.upgrade_applied_fulfillment_baseline_v3"
			),
		)
