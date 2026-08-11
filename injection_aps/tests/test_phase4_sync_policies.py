from __future__ import annotations

import unittest
import inspect
from collections import defaultdict
from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

from injection_aps.services import availability, consistency, delivery_sync, execution_sync, planning


def _raise_validation(message, *args, **kwargs):
	raise ValueError(str(message))


class TestProductionSyncPolicies(unittest.TestCase):
	def test_submit_validation_uses_same_scrap_warehouse_signal_as_reconciliation(self):
		doc = execution_sync.frappe._dict(
			{
				"name": "STE-1",
				"purpose": "Manufacture",
				"work_order": "WO-1",
				"custom_aps_output_type": "Scrap",
				"items": [
					execution_sync.frappe._dict(
						{
							"name": "SED-1",
							"item_code": "FG-1",
							"qty": 5,
							"transfer_qty": 5,
							"is_finished_item": 0,
							"is_scrap_item": 0,
							"t_warehouse": "SCRAP-WH",
						}
					)
				],
			}
		)
		work_order = execution_sync.frappe._dict(
			production_item="FG-1",
			scrap_warehouse="SCRAP-WH",
			sales_order="SO-1",
			sales_order_item="SOI-1",
		)
		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
			patch.object(execution_sync, "_get_run_segment_contexts", return_value=[]),
			patch.object(execution_sync, "_get_source_candidates", return_value=([], "none")) as candidates,
			patch.object(execution_sync.frappe.db, "sql", return_value=[]),
			patch.object(execution_sync.frappe.db, "get_value", return_value=work_order),
		):
			execution_sync.validate_manufacture_before_submit(doc)

		candidates.assert_called_once()
		source = candidates.call_args.args[1]
		self.assertEqual(source["output_type"], "Scrap")
		self.assertEqual(source["source_qty"], 5)

	def test_production_queue_job_id_is_unique_per_committed_source_event(self):
		docs = [
			execution_sync.frappe._dict(
				{
					"name": name,
					"purpose": "Manufacture",
					"docstatus": 1,
					"modified": "2026-08-11 12:00:00",
				}
			)
			for name in ("STE-1", "STE-2")
		]
		with (
			patch.object(execution_sync, "get_affected_production_runs", return_value=["RUN-1"]),
			patch.object(execution_sync.frappe, "enqueue") as enqueue,
		):
			for doc in docs:
				execution_sync.queue_production_sync(doc, method="on_submit")
		job_ids = [call.kwargs["job_id"] for call in enqueue.call_args_list]
		self.assertEqual(len(set(job_ids)), 2)

	def test_real_erpnext_scrap_flag_has_priority_over_conflicting_header_hint(self):
		self.assertEqual(
			execution_sync._classify_manufacture_output(
				{"is_scrap_item": 1, "is_finished_item": 0, "explicit_output_type": "Good"}
			),
			"Scrap",
		)

	def test_zelin_defect_finished_item_in_scrap_warehouse_is_scrap(self):
		self.assertEqual(
			execution_sync._classify_manufacture_output(
				{
					"is_scrap_item": 0,
					"is_finished_item": 1,
					"explicit_output_type": "Good",
					"t_warehouse": "SCRAP-WH",
					"scrap_warehouse": "SCRAP-WH",
				}
			),
			"Scrap",
		)

	def test_bom_scrap_item_is_not_counted_as_finished_unit_scrap(self):
		self.assertFalse(
			execution_sync._is_work_order_finished_output(
				{
					"item_code": "SCRAP-BYPRODUCT",
					"work_order_item": "FG-1",
					"is_scrap_item": 1,
				}
			)
		)
		self.assertTrue(
			execution_sync._is_work_order_finished_output(
				{
					"item_code": "FG-1",
					"work_order_item": "FG-1",
					"is_scrap_item": 1,
				}
			)
		)
		self.assertFalse(
			execution_sync._is_work_order_finished_output(
				{
					"item_code": "OTHER-FG",
					"work_order_item": "FG-1",
					"is_finished_item": 1,
				}
			)
		)

	def test_wrong_finished_item_is_rejected_but_bom_scrap_is_only_excluded(self):
		with (
			patch.object(execution_sync, "_", side_effect=lambda value, **_kwargs: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "Work Order production item"):
				execution_sync._assert_work_order_output_item(
					{
						"item_code": "OTHER-FG",
						"work_order_item": "FG-1",
						"is_finished_item": 1,
					}
				)
			# ERPNext BOM scrap/by-products are valid Stock Entry rows, but not APS
			# finished-unit production. They must not block Manufacture submission.
			execution_sync._assert_work_order_output_item(
				{
					"item_code": "SCRAP-BYPRODUCT",
					"work_order_item": "FG-1",
					"is_scrap_item": 1,
				}
			)

	def test_phase4_audit_uses_same_zelin_scrap_and_stock_uom_semantics(self):
		from injection_aps.tests import phase4_ui_fixture

		source = inspect.getsource(phase4_ui_fixture.validate)
		self.assertIn("sed.t_warehouse = wo.scrap_warehouse", source)
		self.assertIn("coalesce(nullif(sed.transfer_qty, 0), sed.qty)", source)
		self.assertIn("left join `tabWork Order` wo", source)
		self.assertIn("sed.item_code = wo.production_item", source)
		self.assertEqual(
			execution_sync._classify_manufacture_output(
				{
					"is_scrap_item": 0,
					"is_finished_item": 1,
					"explicit_output_type": "Scrap",
					"t_warehouse": "FG-WH",
					"scrap_warehouse": "SCRAP-WH",
				}
			),
			"Scrap",
		)

	def test_one_manufacture_entry_keeps_good_and_scrap_details_separate(self):
		base = {
			"source_stock_entry": "STE-1",
			"source_docstatus": 1,
			"work_order": "WO-1",
			"work_order_scheduling": "WOS-1",
			"direct_scheduling_item": "SI-1",
			"direct_segment": "SEG-1",
			"explicit_output_type": None,
			"posting_date": date(2026, 8, 11),
			"posting_time": time(8),
			"modified": None,
			"amended_from": None,
			"item_code": "FG-1",
			"source_qty": 5,
			"t_warehouse": "FG-WH",
			"work_order_item": "FG-1",
			"scrap_warehouse": "SCRAP-WH",
		}
		rows = [
			execution_sync.frappe._dict(
				{**base, "source_stock_entry_detail": "SED-GOOD", "is_finished_item": 1, "is_scrap_item": 0}
			),
				execution_sync.frappe._dict(
					{
						**base,
						"source_stock_entry_detail": "SED-SCRAP",
						"is_finished_item": 0,
						"is_scrap_item": 1,
						"t_warehouse": "SCRAP-WH",
					}
				),
				execution_sync.frappe._dict(
					{
						**base,
						"source_stock_entry_detail": "SED-BOM-SCRAP",
						"item_code": "SCRAP-1",
						"is_finished_item": 0,
						"is_scrap_item": 1,
						"t_warehouse": "SCRAP-WH",
					}
				),
			]
		with (
			patch.object(execution_sync.frappe.db, "has_column", return_value=True),
			patch.object(execution_sync.frappe.db, "sql", return_value=rows) as sql,
		):
			sources = execution_sync._get_formal_manufacture_sources(
				[{"segment": "SEG-1", "scheduling_item": "SI-1", "scheduling_items": []}]
			)
		self.assertEqual([row["output_type"] for row in sources], ["Good", "Scrap"])
		self.assertEqual(
			[row["source_stock_entry_detail"] for row in sources],
			["SED-GOOD", "SED-SCRAP"],
		)
		self.assertIn("detail.is_scrap_item = 1", sql.call_args.args[0])
		self.assertIn("detail.transfer_qty", sql.call_args.args[0])
		self.assertIn("coalesce(nullif(detail.transfer_qty, 0), detail.qty)", sql.call_args.args[0])

	def test_manufacture_source_query_handles_missing_scrap_detail_flag(self):
		with (
			patch.object(execution_sync.frappe.db, "has_column", return_value=False),
			patch.object(execution_sync.frappe.db, "sql", return_value=[]) as sql,
		):
			execution_sync._get_formal_manufacture_sources(
				[{"segment": "SEG-1", "scheduling_item": "SI-1", "scheduling_items": []}]
			)
		query = sql.call_args.args[0]
		self.assertIn("0 as is_scrap_item", query)
		self.assertIn("detail.is_finished_item = 1", query)
		self.assertIn("se.custom_aps_output_type in ('Good', 'Scrap')", query)
		self.assertIn("detail.t_warehouse = wo.scrap_warehouse", query)
		self.assertNotIn("detail.is_scrap_item = 1", query)

	def test_unique_aps_work_order_source_query_does_not_hide_populated_wrong_wos(self):
		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
			patch.object(execution_sync.frappe.db, "has_column", return_value=True),
			patch.object(execution_sync.frappe.db, "sql", return_value=[]) as sql,
		):
			execution_sync._get_formal_manufacture_sources(
				[
					{
						"planning_run": "RUN-1",
						"segment": "SEG-1",
						"work_order": "WO-1",
						"scheduling_items": [],
					}
				]
			)
		query = sql.call_args.args[0]
		self.assertIn("se.work_order in %(work_orders)s", query)
		self.assertNotIn("ifnull(se.work_order_scheduling, '') = ''", query)

	def test_delta_split_work_orders_are_both_in_production_source_scope(self):
		contexts = [
			{
				"planning_run": "RUN-1",
				"segment": "SEG-1",
				"work_order": "WO-DELTA",
				"scheduling_items": [
					{"name": "SI-BASE", "work_order": "WO-BASE", "parent": "WOS-1"},
					{"name": "SI-DELTA", "work_order": "WO-DELTA", "parent": "WOS-1"},
				],
			}
		]
		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
			patch.object(execution_sync.frappe.db, "has_column", return_value=True),
			patch.object(execution_sync.frappe.db, "sql", return_value=[]) as sql,
		):
			execution_sync._get_formal_manufacture_sources(contexts)

		params = sql.call_args.args[1]
		self.assertEqual(params["work_orders"], ["WO-BASE", "WO-DELTA"])

	def test_pre_wos_owned_work_order_is_attached_in_one_query_and_enters_source_scope(self):
		contexts = [
			{
				"planning_run": "RUN-1",
				"schedule_result": "RES-1",
				"segment": "SEG-1",
				"scheduling_items": [],
			}
		]
		with patch.object(
			execution_sync.frappe.db,
			"sql",
			return_value=[
				execution_sync.frappe._dict(
					name="WO-PRE-WOS", custom_aps_result_reference="RES-1"
				)
			],
		) as sql:
			execution_sync._attach_aps_owned_work_orders(contexts, "RUN-1")

		self.assertEqual(contexts[0]["aps_owned_work_orders"], ["WO-PRE-WOS"])
		self.assertEqual(sql.call_count, 1)
		self.assertIn("wo.custom_aps_run = %(run_name)s", sql.call_args.args[0])
		self.assertNotIn("wo.status", sql.call_args.args[0])

		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
			patch.object(execution_sync.frappe.db, "has_column", return_value=True),
			patch.object(execution_sync.frappe.db, "sql", return_value=[]) as source_sql,
		):
			execution_sync._get_formal_manufacture_sources(contexts)
		self.assertEqual(source_sql.call_args.args[1]["work_orders"], ["WO-PRE-WOS"])

	def test_stopped_pre_wos_work_order_keeps_submitted_history_but_rejects_new_output(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"planned_qty": 100,
			"item_code": "FG-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"aps_owned_work_orders": ["WO-STOPPED"],
			"scheduling_items": [],
			"start_time": datetime(2026, 8, 11, 8),
		}
		historical_source = {
			"source_stock_entry": "SE-HISTORY",
			"source_stock_entry_detail": "SED-HISTORY",
			"source_docstatus": 1,
			"source_qty": 20,
			"source_posting_time": datetime(2026, 8, 11, 10),
			"output_type": "Good",
			"work_order_scheduling": None,
			"direct_scheduling_item": None,
			"direct_segment": None,
			"modified": None,
			"work_order": "WO-STOPPED",
			"work_order_docstatus": 1,
			"work_order_status": "Stopped",
			"work_order_item": "FG-1",
			"work_order_sales_order": "SO-1",
			"work_order_sales_order_item": "SOI-1",
			"work_order_aps_run": "RUN-1",
			"work_order_aps_result": "RES-1",
			"item_code": "FG-1",
		}
		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=[]),
			patch.object(execution_sync.frappe.db, "has_column", return_value=True),
			patch.object(execution_sync.frappe.db, "sql", return_value=[]) as source_sql,
		):
			execution_sync._get_formal_manufacture_sources([context])
		self.assertEqual(source_sql.call_args.args[1]["work_orders"], ["WO-STOPPED"])
		self.assertIn("wo.status as work_order_status", source_sql.call_args.args[0])

		with patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=[]):
			matching, method = execution_sync._get_source_candidates(
				"RUN-1", historical_source, [context], {"SEG-1": context}, {}
			)
		self.assertEqual(matching, [context])
		self.assertEqual(method, "Execution Detail FIFO")
		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=[]),
			patch.object(execution_sync, "_get_customer_schedule_targets", return_value={}),
		):
			first = execution_sync._build_desired_production_allocations(
				"RUN-1", [context], [historical_source]
			)
			second = execution_sync._build_desired_production_allocations(
				"RUN-1", [context], [historical_source]
			)
		self.assertEqual(
			[{key: value for key, value in row.items() if key != "last_synced_on"} for row in first],
			[{key: value for key, value in row.items() if key != "last_synced_on"} for row in second],
		)
		self.assertEqual(sum(row["effective_qty"] for row in first), 20)

		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=[]),
			patch.object(execution_sync, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "not uniquely linked"):
				execution_sync._get_source_candidates(
					"RUN-1",
					{**historical_source, "source_docstatus": 0},
					[context],
					{"SEG-1": context},
					{},
				)
			with self.assertRaisesRegex(ValueError, "not uniquely linked"):
				execution_sync._get_source_candidates(
					"RUN-1",
					{**historical_source, "work_order_aps_result": "RES-OTHER"},
					[context],
					{"SEG-1": context},
					{},
				)

	def test_delta_base_work_order_without_wos_still_matches_its_segment_context(self):
		context = {
			"planning_run": "RUN-1",
			"segment": "SEG-1",
			"work_order": "WO-DELTA",
			"item_code": "FG-1",
			"scheduling_items": [{"name": "SI-BASE", "work_order": "WO-BASE"}],
		}
		source = {
			"work_order": "WO-BASE",
			"item_code": "FG-1",
			"work_order_item": "FG-1",
		}
		with patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]):
			matching, method = execution_sync._get_source_candidates(
				"RUN-1",
				source,
				[context],
				{"SEG-1": context},
				{"SI-BASE": context},
			)

		self.assertEqual(matching, [context])
		self.assertEqual(method, "Execution Detail FIFO")

	def test_eligible_work_order_run_query_includes_split_scheduling_item_lineage(self):
		with patch.object(execution_sync.frappe.db, "sql_list", return_value=["RUN-1"]) as sql_list:
			self.assertEqual(execution_sync._get_eligible_work_order_runs("WO-BASE"), ["RUN-1"])

		query, params = sql_list.call_args.args
		self.assertIn("from `tabScheduling Item` si", query)
		self.assertIn("si.custom_aps_segment_reference", query)
		self.assertIn("si.custom_aps_result_reference = r.name", query)
		self.assertIn("from `tabWork Order` wo", query)
		self.assertIn("wo.custom_aps_run = r.planning_run", query)
		self.assertIn("wo.production_item = r.item_code", query)
		self.assertIn("wo.sales_order_item", query)
		self.assertEqual(params, ("WO-BASE", "WO-BASE", "WO-BASE"))

	def test_pre_wos_work_order_owner_fields_are_validated_and_allocatable(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"item_code": "FG-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"aps_owned_work_orders": ["WO-1"],
			"scheduling_items": [],
			"start_time": datetime(2026, 8, 11, 8),
		}
		source = {
			"work_order": "WO-1",
			"work_order_item": "FG-1",
			"work_order_sales_order": "SO-1",
			"work_order_sales_order_item": "SOI-1",
			"work_order_aps_run": "RUN-1",
			"work_order_aps_result": "RES-1",
			"item_code": "FG-1",
		}
		with patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]):
			matching, method = execution_sync._get_source_candidates(
				"RUN-1", source, [context], {"SEG-1": context}, {}
			)
		self.assertEqual(matching, [context])
		self.assertEqual(method, "Execution Detail FIFO")

		with (
			patch.object(execution_sync, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
				with self.assertRaisesRegex(ValueError, "APS owner Run/Result"):
					execution_sync._validate_work_order_owner_context(
						{**source, "work_order_aps_result": "RES-OTHER"}, context
					)

	def test_pre_wos_good_and_scrap_share_each_segment_total_fifo_quota(self):
		contexts = [
			{
				"planning_run": "RUN-1",
				"schedule_result": "RES-1",
				"segment": f"SEG-{index}",
				"planned_qty": 50,
				"scheduling_items": [],
				"start_time": datetime(2026, 8, 11, 8 + index),
			}
			for index in (1, 2)
		]
		base_source = {
			"work_order": "WO-1",
			"source_stock_entry": "SE-1",
			"source_docstatus": 1,
			"source_posting_time": datetime(2026, 8, 11, 10),
			"work_order_scheduling": None,
			"direct_scheduling_item": None,
			"modified": None,
		}
		sources = [
			{
				**base_source,
				"source_stock_entry_detail": "SED-GOOD",
				"source_qty": 90,
				"output_type": "Good",
			},
			{
				**base_source,
				"source_stock_entry_detail": "SED-SCRAP",
				"source_qty": 10,
				"output_type": "Scrap",
			},
		]
		with (
			patch.object(
				execution_sync,
				"_get_source_candidates",
				return_value=(contexts, "Execution Detail FIFO"),
			),
			patch.object(execution_sync, "_get_customer_schedule_targets", return_value={}),
		):
			desired = execution_sync._build_desired_production_allocations(
				"RUN-1", contexts, sources
			)

		by_segment = defaultdict(float)
		for row in desired:
			by_segment[row["segment"]] += row["effective_qty"]
		self.assertEqual(dict(by_segment), {"SEG-1": 50, "SEG-2": 50})

	def test_manufacture_before_submit_uses_work_order_owner_before_wos_exists(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"item_code": "FG-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"aps_owned_work_orders": ["WO-1"],
			"scheduling_items": [],
			"start_time": datetime(2026, 8, 11, 8),
		}
		doc = execution_sync.frappe._dict(
			name="STE-NEW",
			purpose="Manufacture",
			work_order="WO-1",
			items=[
				execution_sync.frappe._dict(
					name="SED-1",
					item_code="FG-1",
					qty=10,
					transfer_qty=10,
					is_finished_item=1,
					is_scrap_item=0,
				)
			],
		)
		work_order = execution_sync.frappe._dict(
			production_item="FG-1",
			scrap_warehouse="SCRAP-WH",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			custom_aps_run="RUN-1",
			custom_aps_result_reference="RES-1",
		)
		with (
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
			patch.object(execution_sync, "_get_run_segment_contexts", return_value=[context]),
			patch.object(execution_sync.frappe.db, "get_value", return_value=work_order),
			patch.object(execution_sync.frappe.db, "sql") as sql,
		):
			execution_sync.validate_manufacture_before_submit(doc)
		self.assertIn("for update", sql.call_args.args[0].lower())

	def test_pre_wos_work_order_owner_enters_affected_run_queue_scope(self):
		stock_entry = execution_sync.frappe._dict(
			name="STE-1",
			work_order="WO-1",
			custom_aps_segment_reference=None,
			custom_aps_scheduling_item=None,
			work_order_scheduling=None,
		)
		with (
			patch.object(execution_sync.frappe, "get_all", return_value=[]),
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
			patch.object(execution_sync.frappe.db, "exists", return_value=True),
		):
			self.assertEqual(execution_sync.get_affected_production_runs(stock_entry), ["RUN-1"])

	def test_wrong_wos_on_unique_aps_work_order_is_an_error_not_a_silent_skip(self):
		context = {
			"segment": "SEG-1",
			"work_order": "WO-1",
			"work_order_scheduling": "WOS-EXPECTED",
			"scheduling_items": [],
			"start_time": datetime(2026, 8, 11, 8),
		}
		with (
			patch.object(execution_sync, "_", side_effect=lambda value, **_kwargs: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
			patch.object(
				execution_sync.frappe.db,
				"get_value",
				return_value=execution_sync.frappe._dict(
					{
						"status": "Rejected",
						"custom_aps_run": "RUN-1",
						"custom_aps_approval_state": "Rejected",
					}
				),
			),
		):
			with self.assertRaisesRegex(ValueError, "not in Manufacture status"):
				execution_sync._get_source_candidates(
					"RUN-1",
					{"work_order": "WO-1", "work_order_scheduling": "WOS-WRONG"},
					[context],
					{"SEG-1": context},
					{},
				)

	def test_direct_production_link_requires_same_work_order_and_finished_item(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"item_code": "FG-1",
			"work_order": "WO-1",
			"work_order_scheduling": "WOS-1",
			"scheduling_items": [
				{
					"name": "SI-1",
					"parent": "WOS-1",
					"work_order": "WO-1",
					"custom_aps_run": "RUN-1",
					"custom_aps_result_reference": "RES-1",
					"custom_aps_segment_reference": "SEG-1",
					"wos_status": "Manufacture",
					"approval_state": "Approved",
				}
			],
		}
		valid_source = {
			"direct_segment": "SEG-1",
			"work_order": "WO-1",
			"work_order_scheduling": "WOS-1",
			"work_order_item": "FG-1",
			"item_code": "FG-1",
			"output_type": "Good",
		}
		candidates, method = execution_sync._get_source_candidates(
			"RUN-1", valid_source, [context], {"SEG-1": context}, {}
		)
		self.assertEqual(candidates, [context])
		self.assertEqual(method, "Direct")

		with (
			patch.object(execution_sync, "_", side_effect=lambda value: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "APS segment"):
				execution_sync._get_source_candidates(
					"RUN-1",
					{**valid_source, "work_order": "WO-WRONG"},
					[context],
					{"SEG-1": context},
					{},
				)
			with self.assertRaisesRegex(ValueError, "Finished item"):
				execution_sync._get_source_candidates(
					"RUN-1",
					{**valid_source, "item_code": "FG-WRONG"},
					[context],
					{"SEG-1": context},
					{},
				)
			with self.assertRaisesRegex(ValueError, "Stock Entry Work Order"):
				execution_sync._get_source_candidates(
					"RUN-1",
					{**valid_source, "work_order": None},
					[context],
					{"SEG-1": context},
					{},
				)

	def test_work_order_fifo_rejects_finished_item_that_differs_from_result(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"item_code": "FG-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"work_order": "WO-1",
			"scheduling_items": [],
		}
		source = {
			"work_order": "WO-1",
			"work_order_item": "FG-1",
			"work_order_sales_order": "SO-1",
			"work_order_sales_order_item": "SOI-1",
			"item_code": "OTHER-FG",
			"output_type": "Good",
		}
		with (
			patch.object(execution_sync, "_", side_effect=lambda value, **_kwargs: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
		):
			with self.assertRaisesRegex(ValueError, "Finished item"):
				execution_sync._get_source_candidates(
					"RUN-1", source, [context], {"SEG-1": context}, {}
				)

	def test_manufacture_before_submit_blocks_bad_direct_item_and_locks_run(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"item_code": "FG-1",
			"work_order": "WO-1",
			"scheduling_items": [],
		}
		doc = execution_sync.frappe._dict(
			{
				"name": "STE-NEW",
				"purpose": "Manufacture",
				"work_order": "WO-1",
				"custom_aps_segment_reference": "SEG-1",
				"items": [
					execution_sync.frappe._dict(
						{
							"name": "SED-1",
							"item_code": "FG-WRONG",
							"qty": 10,
							"transfer_qty": 10,
							"is_finished_item": 1,
							"is_scrap_item": 0,
						}
					)
				],
			}
		)
		with (
			patch.object(execution_sync, "_", side_effect=lambda value, **_kwargs: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
			patch.object(execution_sync, "_get_segment_run", return_value="RUN-1"),
			patch.object(execution_sync, "_get_eligible_work_order_runs", return_value=["RUN-1"]),
			patch.object(execution_sync, "_get_run_segment_contexts", return_value=[context]),
			patch.object(
				execution_sync.frappe.db,
				"get_value",
				return_value=execution_sync.frappe._dict(
					{"production_item": "FG-1", "scrap_warehouse": "SCRAP-WH"}
				),
			),
			patch.object(execution_sync.frappe.db, "sql") as sql,
		):
			with self.assertRaisesRegex(ValueError, "Finished item"):
				execution_sync.validate_manufacture_before_submit(doc)
		self.assertIn("for update", sql.call_args.args[0].lower())

	def test_unknown_direct_segment_is_blocked_instead_of_falling_back_to_fifo(self):
		with (
			patch.object(execution_sync, "_", side_effect=lambda value: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "not an active segment"):
				execution_sync._get_source_candidates(
					"RUN-1", {"direct_segment": "SEG-OTHER"}, [], {}, {}
				)

	def test_direct_segment_and_scheduling_item_must_identify_the_same_context(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"item_code": "FG-1",
			"work_order": "WO-1",
			"work_order_scheduling": "WOS-1",
			"scheduling_items": [
				{
					"name": "SI-1",
					"parent": "WOS-1",
					"work_order": "WO-1",
					"custom_aps_run": "RUN-1",
					"custom_aps_result_reference": "RES-1",
					"custom_aps_segment_reference": "SEG-1",
					"wos_status": "Manufacture",
					"approval_state": "Approved",
				}
			],
		}
		source = {
			"direct_segment": "SEG-1",
			"direct_scheduling_item": "SI-WRONG",
			"work_order": "WO-1",
			"work_order_scheduling": "WOS-1",
			"work_order_item": "FG-1",
			"item_code": "FG-1",
			"output_type": "Good",
		}
		with (
			patch.object(execution_sync, "_", side_effect=lambda value: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "not linked to APS segment"):
				execution_sync._get_source_candidates(
					"RUN-1", source, [context], {"SEG-1": context}, {"SI-1": context}
				)

	def test_direct_execution_item_must_be_manufacture_and_approved(self):
		base = {
			"name": "SI-1",
			"parent": "WOS-1",
			"work_order": "WO-1",
			"custom_aps_run": "RUN-1",
			"approval_state": "Approved",
			"wos_status": "Manufacture",
		}
		with (
			patch.object(execution_sync, "_", side_effect=lambda value: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "not in Manufacture status"):
				execution_sync._validate_direct_execution_item_state(
					{**base, "wos_status": "Schedule Confirmed"}
				)
			with self.assertRaisesRegex(ValueError, "valid APS approval"):
				execution_sync._validate_direct_execution_item_state(
					{**base, "approval_state": "Rejected"}
				)

	def test_segment_only_direct_link_still_validates_formal_execution_state(self):
		context = {
			"planning_run": "RUN-1",
			"schedule_result": "RES-1",
			"segment": "SEG-1",
			"item_code": "FG-1",
			"work_order": "WO-1",
			"scheduling_items": [
				{
					"name": "SI-1",
					"parent": "WOS-1",
					"work_order": "WO-1",
					"custom_aps_run": "RUN-1",
					"wos_status": "Manufacture",
					"approval_state": "Rejected",
				}
			],
		}
		source = {
			"direct_segment": "SEG-1",
			"work_order": "WO-1",
			"work_order_item": "FG-1",
			"item_code": "FG-1",
			"output_type": "Good",
		}
		with (
			patch.object(execution_sync, "_", side_effect=lambda value: value),
			patch.object(execution_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "valid APS approval"):
				execution_sync._get_source_candidates(
					"RUN-1", source, [context], {"SEG-1": context}, {"SI-1": context}
				)
			with self.assertRaisesRegex(ValueError, "multiple execution items"):
				execution_sync._get_source_candidates(
					"RUN-1",
					source,
					[{**context, "scheduling_items": [*context["scheduling_items"], {**context["scheduling_items"][0], "name": "SI-2"}]}],
					{"SEG-1": {**context, "scheduling_items": [*context["scheduling_items"], {**context["scheduling_items"][0], "name": "SI-2"}]}},
					{},
				)

	def test_cancelled_and_blocked_segments_are_excluded_from_execution_context(self):
		with patch.object(execution_sync.frappe.db, "sql", return_value=[]) as sql:
			self.assertEqual(execution_sync._get_run_segment_contexts("RUN-1"), [])
		query = sql.call_args.args[0]
		self.assertIn("not in ('Blocked', 'Cancelled')", query)

	def test_cancelled_segment_derived_actual_cache_is_reset(self):
		now_value = datetime(2026, 8, 11, 12)
		with (
			patch.object(execution_sync.frappe, "get_all", return_value=["SEG-CANCELLED"]),
			patch.object(execution_sync.frappe.db, "set_value") as set_value,
		):
			execution_sync._reset_inactive_segment_actuals(["RES-1"], now_value)
		values = set_value.call_args.args[2]
		self.assertEqual(values["actual_status"], "Not Started")
		self.assertEqual(values["actual_good_qty"], 0)
		self.assertEqual(values["actual_scrap_qty"], 0)
		self.assertEqual(values["execution_source_documents"], "")

	def test_blank_child_policy_matches_normalized_result_and_caps_production_target(self):
		context = {
			"schedule_result": "RES-1",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"item_code": "FG-1",
			"requested_date": date(2026, 8, 12),
			"result_planned_qty": 50,
			"production_strategy": "Auto Balance",
			"demand_confidence": "Confirmed",
			"cancellation_risk_percent": 5,
			"prebuild_allowed": 1,
			"max_prebuild_days": 7,
		}
		target = execution_sync.frappe._dict(
			{
				"name": "SCHEDULE-ITEM-1",
				"qty": 100,
				"production_strategy": "",
				"demand_confidence": "",
				"cancellation_risk_percent": 0,
				"prebuild_allowed": 1,
				"max_prebuild_days": 0,
			}
		)
		with patch.object(execution_sync.frappe.db, "sql", return_value=[target]) as sql:
			result = execution_sync._get_customer_schedule_targets([context])
		self.assertEqual(result["RES-1"][0]["attributed_qty"], 50)
		self.assertNotIn("and ifnull(i.max_prebuild_days", sql.call_args.args[0])
		allocations = execution_sync._split_production_to_schedule_targets(
			context,
			80,
			"Good",
			result,
			defaultdict(float),
		)
		self.assertEqual([(row[0] and row[0]["name"], row[1]) for row in allocations], [
			("SCHEDULE-ITEM-1", 50),
			(None, 30),
		])


class TestDeliverySyncPolicies(unittest.TestCase):
	def setUp(self):
		self.source = {
			"source_delivery_note_item": "DNI-1",
			"source_qty": 60,
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"item_code": "ITEM-1",
			"sales_order": "SO-FRAMEWORK",
			"sales_order_due_date": "2030-12-31",
			"posting_date": date(2026, 8, 12),
		}
		self.target = {
			"name": "SCHEDULE-ITEM-1",
			"parent": "SCHEDULE-1",
			"schedule_status": "Active",
			"qty": 100,
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"item_code": "ITEM-1",
			"sales_order": "SO-FRAMEWORK",
			"schedule_date": "2026-08-12",
		}

	def test_delivery_queue_job_id_is_unique_per_committed_source_event(self):
		docs = [
			delivery_sync.frappe._dict(
				{
					"name": name,
					"company": "COMPANY-1",
					"customer": "CUSTOMER-1",
					"docstatus": 1,
					"modified": "2026-08-11 12:00:00",
					"items": [delivery_sync.frappe._dict({"item_code": "ITEM-1"})],
				}
			)
			for name in ("DN-1", "DN-2")
		]
		with patch.object(delivery_sync.frappe, "enqueue") as enqueue:
			for doc in docs:
				delivery_sync.queue_delivery_sync(doc, method="on_submit")
		job_ids = [call.kwargs["job_id"] for call in enqueue.call_args_list]
		self.assertEqual(len(set(job_ids)), 2)
		self.assertEqual(
			[call.kwargs["source_delivery_note"] for call in enqueue.call_args_list],
			["DN-1", "DN-2"],
		)
		self.assertTrue(all(call.kwargs["item_codes"] == ["ITEM-1"] for call in enqueue.call_args_list))
		self.assertTrue(all("isolate_source_errors" not in call.kwargs for call in enqueue.call_args_list))

	def test_delivery_queue_enqueues_one_strict_job_per_item(self):
		doc = delivery_sync.frappe._dict(
			{
				"name": "DN-MULTI",
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"docstatus": 1,
				"modified": "2026-08-11 12:00:00",
				"items": [
					delivery_sync.frappe._dict({"item_code": "ITEM-B"}),
					delivery_sync.frappe._dict({"item_code": "ITEM-A"}),
					delivery_sync.frappe._dict({"item_code": "ITEM-B"}),
				],
			}
		)
		with patch.object(delivery_sync.frappe, "enqueue") as enqueue:
			delivery_sync.queue_delivery_sync(doc, method="on_cancel")
		self.assertEqual(
			[call.kwargs["item_codes"] for call in enqueue.call_args_list],
			[["ITEM-A"], ["ITEM-B"]],
		)
		self.assertEqual(len({call.kwargs["job_id"] for call in enqueue.call_args_list}), 2)
		self.assertTrue(all("isolate_source_errors" not in call.kwargs for call in enqueue.call_args_list))

	def test_delivery_planning_run_locks_are_sorted_and_exclusive(self):
		with patch.object(delivery_sync.frappe.db, "sql") as sql:
			delivery_sync._lock_planning_runs(["RUN-B", "RUN-A", "RUN-B"])
		self.assertEqual([call.args[1] for call in sql.call_args_list], ["RUN-A", "RUN-B"])
		self.assertTrue(all("for update" in call.args[0].lower() for call in sql.call_args_list))

	def test_before_submit_replays_submitted_sources_instead_of_stale_child_cache(self):
		target = {**self.target, "delivered_qty": 0}
		existing_source = {
			**self.source,
			"source_delivery_note": "DN-SUBMITTED",
			"source_delivery_note_item": "DNI-SUBMITTED",
			"source_qty": 80,
			"signed_qty": 80,
			"source_docstatus": 1,
			"posting_date": date(2026, 8, 12),
			"source_posting_time": datetime(2026, 8, 12, 8),
			"creation": "2026-08-12 08:00:00",
			"source_idx": 1,
			"is_return": 0,
			"direct_schedule_item": None,
		}
		doc = delivery_sync.frappe._dict(
			{
				"name": "DN-CURRENT",
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"posting_date": date(2026, 8, 12),
				"posting_time": time(9),
				"creation": "2026-08-12 09:00:00",
				"is_return": 0,
				"items": [
					delivery_sync.frappe._dict(
						{
							"name": "DNI-CURRENT",
							"idx": 1,
							"item_code": "ITEM-1",
							"stock_qty": 30,
							"against_sales_order": "SO-FRAMEWORK",
						}
					)
				],
			}
		)
		with (
			patch.object(delivery_sync, "_", side_effect=lambda value: value),
			patch.object(delivery_sync.frappe, "throw", side_effect=_raise_validation),
			patch.object(delivery_sync, "_lock_delivery_scope"),
			patch.object(delivery_sync, "_get_delivery_targets", return_value=[target]),
			patch.object(delivery_sync, "_get_existing_scope_rows", return_value=[]),
			patch.object(delivery_sync, "_get_submitted_delivery_sources", return_value=[existing_source]),
			patch.object(delivery_sync, "now_datetime", return_value=datetime(2026, 8, 12, 9)),
		):
			with self.assertRaisesRegex(ValueError, "exceeds the remaining active schedule quantity by 10"):
				delivery_sync.validate_delivery_before_submit(doc)

	def test_schedule_delivery_lower_bound_replays_submitted_original_and_return_under_lock(self):
		target = {**self.target, "delivered_qty": 0}
		normal = {
			**self.source,
			"source_delivery_note": "DN-1",
			"source_delivery_note_item": "DNI-1",
			"source_qty": 60,
			"signed_qty": 60,
			"source_posting_time": datetime(2026, 8, 12, 8),
			"is_return": 0,
			"direct_schedule_item": target["name"],
		}
		returned = {
			**self.source,
			"source_delivery_note": "DN-RETURN-1",
			"source_delivery_note_item": "DNI-RETURN-1",
			"source_qty": 10,
			"signed_qty": -10,
			"source_posting_time": datetime(2026, 8, 12, 9),
			"is_return": 1,
			"return_against": "DN-1",
			"original_delivery_note_item": "DNI-1",
			"direct_schedule_item": None,
		}
		events = []
		with (
			patch.object(delivery_sync, "_lock_delivery_scope", side_effect=lambda *args: events.append("lock")),
			patch.object(delivery_sync, "_get_delivery_targets", side_effect=lambda *args, **kwargs: events.append("targets") or [target]),
			patch.object(delivery_sync, "_get_existing_scope_rows", return_value=[]),
			patch.object(delivery_sync, "_get_submitted_delivery_sources", return_value=[normal, returned]),
			patch.object(delivery_sync, "now_datetime", return_value=datetime(2026, 8, 12, 10)),
		):
			bounds = delivery_sync.get_schedule_delivery_lower_bounds(
				"COMPANY-1", "CUSTOMER-1", [target["name"]]
			)
		self.assertEqual(events[:2], ["lock", "targets"])
		self.assertEqual(bounds, {target["name"]: 50})

	def test_non_aps_delivery_scope_is_skipped_before_history_scan(self):
		doc = delivery_sync.frappe._dict(
			{
				"name": "DN-NON-APS",
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"posting_date": date(2026, 8, 12),
				"items": [
					delivery_sync.frappe._dict(
						{"name": "DNI-NON-APS", "item_code": "NON-APS", "stock_qty": 10}
					)
				],
			}
		)
		with (
			patch.object(delivery_sync, "_lock_delivery_scope"),
			patch.object(delivery_sync, "_get_delivery_targets", return_value=[]),
			patch.object(delivery_sync, "_get_existing_scope_rows", return_value=[]),
			patch.object(delivery_sync, "_get_submitted_delivery_sources") as get_sources,
		):
			delivery_sync.validate_delivery_before_submit(doc)
		get_sources.assert_not_called()

	def test_async_non_aps_delivery_scope_is_a_noop(self):
		events = []
		with (
			patch.object(delivery_sync.frappe, "generate_hash", return_value="HASH"),
			patch.object(delivery_sync, "_lock_delivery_scope", side_effect=lambda *args: events.append("lock")),
			patch.object(delivery_sync, "_get_delivery_targets", side_effect=lambda *args, **kwargs: events.append("targets") or []),
			patch.object(delivery_sync, "_get_existing_scope_rows", return_value=[]),
			patch.object(delivery_sync, "_get_explicitly_aps_linked_item_codes", return_value=[]),
			patch.object(delivery_sync, "_get_submitted_delivery_sources") as get_sources,
			patch.object(delivery_sync.frappe.db, "savepoint"),
			patch.object(delivery_sync.frappe.db, "release_savepoint"),
		):
			result = delivery_sync.sync_delivery_allocations(
				"COMPANY-1",
				customer="CUSTOMER-1",
				item_codes=["NON-APS"],
				source_delivery_note="DN-NON-APS",
			)
		self.assertEqual(result["source_item_count"], 0)
		self.assertEqual(events[:2], ["lock", "targets"])
		get_sources.assert_not_called()

	def test_submitted_source_query_has_controlled_history_boundary(self):
		with patch.object(delivery_sync.frappe.db, "sql", return_value=[]) as sql:
			delivery_sync._get_submitted_delivery_sources(
				"COMPANY-1",
				customer="CUSTOMER-1",
				item_codes=["ITEM-1"],
				active_targets=[self.target],
			)
		query = sql.call_args.args[0]
		self.assertIn("linked.source_delivery_note_item = dni.name", query)
		self.assertIn("dni.custom_aps_customer_schedule_item", query)
		self.assertIn("active_item.schedule_date = dn.posting_date", query)
		self.assertIn("active_schedule.status = 'Active'", query)
		self.assertIn("active_schedule.customer = dn.customer", query)
		self.assertIn("active_item.sales_order", query)
		self.assertIn("traced.source_delivery_note = dn.return_against", query)
		self.assertIn("original_direct.custom_aps_customer_schedule_item", query)
		self.assertIn("original_direct.parent = dn.return_against", query)

	def test_submitted_source_query_size_does_not_expand_with_active_target_count(self):
		many_targets = [
			{
				**self.target,
				"name": f"SCHEDULE-ITEM-{index}",
				"schedule_date": date(2026, 8, 12),
			}
			for index in range(50000)
		]
		with patch.object(delivery_sync.frappe.db, "sql", return_value=[]) as sql:
			delivery_sync._get_submitted_delivery_sources(
				"COMPANY-1",
				customer="CUSTOMER-1",
				item_codes=["ITEM-1"],
				active_targets=many_targets,
			)
		source_query_call = next(
			call for call in sql.call_args_list if "from `tabDelivery Note` dn" in call.args[0]
		)
		query, params = source_query_call.args[:2]
		self.assertLess(len(query), 5000)
		self.assertEqual(set(params), {"company", "customer", "item_codes", "qty_tolerance"})
		self.assertNotIn("target_0", query)

	def test_schedule_move_discovers_fifo_delivery_committed_before_ledger_sync(self):
		with patch.object(delivery_sync.frappe.db, "sql", return_value=[]) as sql:
			delivery_sync._get_submitted_delivery_sources(
				"COMPANY-1",
				customer="CUSTOMER-1",
				item_codes=["ITEM-1"],
				active_targets=[],
				historical_target_remap={
					"OLD-TARGET": {
						"customer_schedule_item": "NEW-TARGET",
						"customer_schedule": "SCHEDULE-NEW",
						"schedule_date": date(2026, 8, 13),
					}
				},
			)
		source_query_call = next(
			call for call in sql.call_args_list if "from `tabDelivery Note` dn" in call.args[0]
		)
		query, params = source_query_call.args[:2]
		self.assertIn("historical_item.name in %(historical_target_names)s", query)
		self.assertEqual(params["historical_target_names"], ["OLD-TARGET"])

	def test_discovered_unledgered_fifo_delivery_rebuilds_on_explicit_date_remap(self):
		new_target = {**self.target, "name": "NEW-TARGET", "schedule_date": date(2026, 8, 13)}
		parts = delivery_sync._allocate_delivery_source(
			{
				**self.source,
				"direct_schedule_item": None,
				"remap_source_targets": ["OLD-TARGET"],
			},
			{},
			{"NEW-TARGET": new_target},
			defaultdict(float),
			target_remap={
				"OLD-TARGET": {
					"customer_schedule_item": "NEW-TARGET",
					"customer_schedule": new_target["parent"],
					"schedule_date": new_target["schedule_date"],
				}
			},
		)
		self.assertEqual(
			[(row[0]["name"], row[1], row[2]) for row in parts],
			[("NEW-TARGET", 60, "Replacement FIFO")],
		)

	def test_cancelled_unsynced_direct_delivery_and_return_remain_in_remap_scope(self):
		normal = {
			**self.source,
			"source_delivery_note": "DN-ORIGINAL-1",
			"source_delivery_note_item": "DNI-ORIGINAL-1",
			"source_qty": 60,
			"signed_qty": 60,
			"is_return": 0,
			"direct_schedule_item": "OLD-DIRECT-TARGET",
			"source_posting_time": datetime(2026, 8, 12, 8),
		}
		returned = {
			**self.source,
			"source_delivery_note": "DN-RETURN-1",
			"source_delivery_note_item": "DNI-RETURN-1",
			"source_qty": 60,
			"signed_qty": -60,
			"is_return": 1,
			"return_against": "DN-ORIGINAL-1",
			"original_delivery_note_item": "DNI-ORIGINAL-1",
			"direct_schedule_item": None,
			"source_posting_time": datetime(2026, 8, 12, 9),
		}
		old_target = {**self.target, "name": "OLD-DIRECT-TARGET", "schedule_status": "Superseded"}
		with (
			patch.object(delivery_sync.frappe, "generate_hash", return_value="HASH"),
			patch.object(delivery_sync.frappe.db, "savepoint"),
			patch.object(delivery_sync.frappe.db, "release_savepoint"),
			patch.object(delivery_sync, "_lock_delivery_scope"),
			patch.object(delivery_sync, "_get_delivery_targets", return_value=[]),
			patch.object(delivery_sync, "_get_existing_scope_rows", return_value=[]),
			patch.object(delivery_sync, "_get_target_remap_scope_item_codes", return_value=["ITEM-1"]),
			patch.object(delivery_sync, "_get_explicitly_aps_linked_item_codes", return_value=[]),
			patch.object(delivery_sync, "_get_submitted_delivery_sources", return_value=[normal, returned]),
			patch.object(delivery_sync, "_get_schedule_target_by_name", return_value=old_target),
			patch.object(delivery_sync, "_reconcile_delivery_ledger", return_value={"created": 0, "updated": 0, "reversed": 0}),
			patch.object(delivery_sync, "_rollup_delivery_allocations", return_value={"schedule_item_count": 0, "delivered_qty": 0, "schedule_items": []}),
		):
			result = delivery_sync.sync_delivery_allocations(
				"COMPANY-1",
				customer="CUSTOMER-1",
				item_codes=["ITEM-1"],
				target_remap={"OLD-DIRECT-TARGET": None},
			)
		self.assertEqual(result["source_item_count"], 2)
		self.assertEqual(result["settled_history_source_count"], 2)
		self.assertEqual(result["desired_allocation_count"], 0)

	def test_framework_sales_order_fifo_does_not_force_so_item_delivery_date(self):
		parts = delivery_sync._allocate_delivery_source(
			self.source,
			{("COMPANY-1", "CUSTOMER-1", "ITEM-1"): [self.target]},
			{self.target["name"]: self.target},
			defaultdict(float),
		)
		self.assertEqual([(row[0]["name"], row[1], row[2]) for row in parts], [
			("SCHEDULE-ITEM-1", 60, "Controlled FIFO")
		])

	def test_unsynced_direct_source_is_redirected_only_by_explicit_target_remap(self):
		old_target = "OLD-DIRECT-TARGET"
		new_target = {**self.target, "name": "NEW-DIRECT-TARGET"}
		mapping = {
			"customer_schedule_item": new_target["name"],
			"customer_schedule": new_target["parent"],
			"schedule_date": new_target["schedule_date"],
		}
		parts = delivery_sync._allocate_delivery_source(
			{**self.source, "direct_schedule_item": old_target},
			{},
			{new_target["name"]: new_target},
			defaultdict(float),
			target_remap={old_target: mapping},
		)
		self.assertEqual([(row[0]["name"], row[1], row[2]) for row in parts], [
			("NEW-DIRECT-TARGET", 60, "Direct")
		])
		with (
			patch.object(delivery_sync, "_", side_effect=lambda value: value),
			patch.object(delivery_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "no replacement target"):
				delivery_sync._allocate_delivery_source(
					{**self.source, "direct_schedule_item": old_target},
					{},
					{},
					defaultdict(float),
					target_remap={old_target: None},
				)

	def test_unsynced_direct_gross_peak_can_remap_when_later_return_fits_net_target(self):
		old_target = "OLD-DIRECT-TARGET"
		new_target = {**self.target, "name": "NEW-DIRECT-TARGET", "qty": 100}
		normal = {
			**self.source,
			"source_delivery_note": "DN-1",
			"source_delivery_note_item": "DNI-1",
			"source_qty": 120,
			"signed_qty": 120,
			"source_posting_time": datetime(2026, 8, 12, 8),
			"is_return": 0,
			"direct_schedule_item": old_target,
		}
		returned = {
			**self.source,
			"source_delivery_note": "DN-RETURN-1",
			"source_delivery_note_item": "DNI-RETURN-1",
			"source_qty": 20,
			"signed_qty": -20,
			"source_posting_time": datetime(2026, 8, 12, 9),
			"is_return": 1,
			"return_against": "DN-1",
			"original_delivery_note_item": "DNI-1",
			"direct_schedule_item": None,
		}
		mapping = {
			old_target: {
				"customer_schedule_item": new_target["name"],
				"customer_schedule": new_target["parent"],
				"schedule_date": new_target["schedule_date"],
			}
		}
		old_row = {**self.target, "name": old_target, "qty": 120, "schedule_status": "Superseded"}
		with (
			patch.object(delivery_sync, "_get_schedule_target_by_name", return_value=old_row),
			patch.object(delivery_sync, "now_datetime", return_value=datetime(2026, 8, 12, 10)),
		):
			desired = delivery_sync._build_desired_delivery_allocations(
				[normal, returned],
				[new_target],
				target_remap=mapping,
			)
		self.assertEqual(sum(row["effective_qty"] for row in desired), 100)
		lineage = {
			"DNI-1": [
				{
					"customer_schedule_item": row["customer_schedule_item"],
					"allocated_qty": row["allocated_qty"],
					"allocation_method": row["allocation_method"],
				}
				for row in desired
				if not row["is_return"]
			]
		}
		with patch.object(delivery_sync, "now_datetime", return_value=datetime(2026, 8, 12, 10)):
			repeated = delivery_sync._build_desired_delivery_allocations(
				[{**normal, "direct_schedule_item": new_target["name"]}, returned],
				[new_target],
				existing_lineage_by_source=lineage,
			)
		self.assertEqual(
			[(row["allocation_key"], row["effective_qty"]) for row in repeated],
			[(row["allocation_key"], row["effective_qty"]) for row in desired],
		)

	def test_fifo_overdelivery_is_blocked(self):
		with (
			patch.object(delivery_sync, "_", side_effect=lambda value: value),
			patch.object(delivery_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "exceeds the remaining active schedule quantity by 10"):
				delivery_sync._allocate_delivery_source(
					{**self.source, "source_qty": 110},
					{("COMPANY-1", "CUSTOMER-1", "ITEM-1"): [self.target]},
					{self.target["name"]: self.target},
					defaultdict(float),
				)

	def test_delivery_target_query_contains_one_where_clause(self):
		with patch.object(delivery_sync.frappe.db, "sql", return_value=[]) as sql:
			delivery_sync._get_delivery_targets(
				"COMPANY-1",
				customer="CUSTOMER-1",
				item_codes=["ITEM-1"],
			)
		self.assertEqual(sql.call_args.args[0].lower().count("where "), 1)

	def test_unlinked_historical_delivery_cannot_be_fifo_claimed_by_future_schedule(self):
		with (
			patch.object(delivery_sync, "_", side_effect=lambda value: value),
			patch.object(delivery_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "posting date.*does not match"):
				delivery_sync._allocate_delivery_source(
					{**self.source, "posting_date": date(2026, 1, 1)},
					{("COMPANY-1", "CUSTOMER-1", "ITEM-1"): [self.target]},
					{self.target["name"]: self.target},
					defaultdict(float),
				)
		parts = delivery_sync._allocate_delivery_source(
			{**self.source, "posting_date": date(2026, 1, 1)},
			{("COMPANY-1", "CUSTOMER-1", "ITEM-1"): [self.target]},
			{self.target["name"]: self.target},
			defaultdict(float),
			existing_lineage=[
				{
					"customer_schedule_item": self.target["name"],
					"allocated_qty": 60,
					"allocation_method": "Controlled FIFO",
				}
			],
		)
		self.assertEqual(parts[0][0]["name"], self.target["name"])

	def test_existing_fifo_lineage_cannot_silently_move_to_a_future_target(self):
		inactive = {**self.target, "name": "OLD-TARGET", "schedule_status": "Superseded"}
		future = {**self.target, "name": "FUTURE-TARGET", "schedule_date": date(2026, 9, 1)}
		lineage = [
			{
				"customer_schedule_item": "OLD-TARGET",
				"allocated_qty": 60,
				"allocation_method": "Controlled FIFO",
			}
		]
		with (
			patch.object(delivery_sync, "_", side_effect=lambda value: value),
			patch.object(delivery_sync.frappe, "throw", side_effect=_raise_validation),
			patch.object(delivery_sync, "_get_schedule_target_by_name", return_value=inactive),
		):
			with self.assertRaisesRegex(ValueError, "is not Active"):
				delivery_sync._allocate_delivery_source(
					self.source,
					{("COMPANY-1", "CUSTOMER-1", "ITEM-1"): [future]},
					{future["name"]: future},
					defaultdict(float),
					existing_lineage=lineage,
				)
		parts = delivery_sync._allocate_delivery_source(
			self.source,
			{("COMPANY-1", "CUSTOMER-1", "ITEM-1"): [future]},
			{future["name"]: future},
			defaultdict(float),
			existing_lineage=lineage,
			target_remap={
				"OLD-TARGET": {
					"customer_schedule_item": "FUTURE-TARGET",
					"customer_schedule": future["parent"],
					"schedule_date": future["schedule_date"],
				}
			},
		)
		self.assertEqual([(row[0]["name"], row[1]) for row in parts], [("FUTURE-TARGET", 60)])

	def test_remap_allows_historical_gross_delivery_when_traced_return_fits_new_net_qty(self):
		old_target = "OLD-TARGET"
		new_target = {**self.target, "name": "NEW-TARGET", "qty": 40}
		normal = {
			**self.source,
			"source_delivery_note": "DN-1",
			"source_delivery_note_item": "DNI-1",
			"source_qty": 60,
			"signed_qty": 60,
			"source_posting_time": datetime(2026, 8, 12, 8),
			"is_return": 0,
			"direct_schedule_item": None,
		}
		returned = {
			**self.source,
			"source_delivery_note": "DN-RET-1",
			"source_delivery_note_item": "DNI-RET-1",
			"source_qty": 20,
			"signed_qty": -20,
			"source_posting_time": datetime(2026, 8, 12, 9),
			"is_return": 1,
			"return_against": "DN-1",
			"original_delivery_note_item": "DNI-1",
			"direct_schedule_item": None,
		}
		with patch.object(delivery_sync, "now_datetime", return_value=datetime(2026, 8, 12, 10)):
			desired = delivery_sync._build_desired_delivery_allocations(
				[normal, returned],
				[new_target],
				existing_lineage_by_source={
					"DNI-1": [
						{
							"customer_schedule_item": old_target,
							"allocated_qty": 60,
							"allocation_method": "Controlled FIFO",
						}
					]
				},
				target_remap={
					old_target: {
						"customer_schedule_item": new_target["name"],
						"customer_schedule": new_target["parent"],
						"schedule_date": new_target["schedule_date"],
					}
				},
			)
		self.assertEqual(sum(row["effective_qty"] for row in desired), 40)
		self.assertEqual({row["customer_schedule_item"] for row in desired}, {"NEW-TARGET"})
		remapped_lineage = {
			"DNI-1": [
				{
					"customer_schedule_item": row["customer_schedule_item"],
					"allocated_qty": row["allocated_qty"],
					"allocation_method": row["allocation_method"],
				}
				for row in desired
				if not row["is_return"]
			]
		}
		with patch.object(delivery_sync, "now_datetime", return_value=datetime(2026, 8, 12, 10)):
			repeated = delivery_sync._build_desired_delivery_allocations(
				[normal, returned],
				[new_target],
				existing_lineage_by_source=remapped_lineage,
			)
		self.assertEqual(
			[(row["allocation_key"], row["effective_qty"]) for row in repeated],
			[(row["allocation_key"], row["effective_qty"]) for row in desired],
		)

	def test_return_trace_key_and_detail_are_unique_per_original_item(self):
		return_source = {
			**self.source,
			"source_delivery_note": "DN-RETURN-1",
			"source_delivery_note_item": "DNI-RETURN-1",
			"source_qty": 50,
			"is_return": 1,
			"return_against": "DN-ORIGINAL-1",
			"original_delivery_note_item": None,
		}
		normal_by_source = {
			"DNI-ORIGINAL-1": [{"customer_schedule_item": self.target["name"], "allocated_qty": 30}],
			"DNI-ORIGINAL-2": [{"customer_schedule_item": self.target["name"], "allocated_qty": 30}],
		}
		with patch.object(
			delivery_sync.frappe,
			"get_all",
			return_value=["DNI-ORIGINAL-1", "DNI-ORIGINAL-2"],
		):
			parts = delivery_sync._allocate_return_source(
				return_source,
				normal_by_source,
				{self.target["name"]: self.target},
				defaultdict(float),
			)
		self.assertEqual([(row[1], row[3]) for row in parts], [
			(30, "DNI-ORIGINAL-1"),
			(20, "DNI-ORIGINAL-2"),
		])
		keys = {
			delivery_sync._delivery_allocation_key(
				return_source,
				row[0],
				original_delivery_note_item=row[3],
			)
			for row in parts
		}
		self.assertEqual(len(keys), 2)

	def test_fully_returned_targetless_history_settles_and_replays_idempotently(self):
		normal = {
			**self.source,
			"source_delivery_note": "DN-ORIGINAL-1",
			"source_delivery_note_item": "DNI-ORIGINAL-1",
			"source_qty": 60,
			"signed_qty": 60,
			"is_return": 0,
			"direct_schedule_item": None,
		}
		returned = {
			**self.source,
			"source_delivery_note": "DN-RETURN-1",
			"source_delivery_note_item": "DNI-RETURN-1",
			"source_qty": 60,
			"signed_qty": -60,
			"is_return": 1,
			"return_against": "DN-ORIGINAL-1",
			"original_delivery_note_item": "DNI-ORIGINAL-1",
			"direct_schedule_item": None,
		}
		settled, chains = delivery_sync._find_settled_delivery_history([normal, returned], [])
		self.assertEqual(settled, {"DNI-ORIGINAL-1", "DNI-RETURN-1"})
		self.assertEqual(chains[0]["net_qty"], 0)
		self.assertEqual(
			delivery_sync._build_desired_delivery_allocations(
				[normal, returned], [], settled_source_items=settled
			),
			[],
		)

		docs = {}
		for name, effective_qty in (("ALLOC-DELIVERY", 60), ("ALLOC-RETURN", -60)):
			doc = MagicMock()
			doc.source_delivery_note = "DN-ORIGINAL-1"
			doc.effective_qty = effective_qty
			doc.reversed_qty = 0
			docs[name] = doc
		existing = [
			delivery_sync.frappe._dict({"name": "ALLOC-DELIVERY", "allocation_key": "KEY-1"}),
			delivery_sync.frappe._dict({"name": "ALLOC-RETURN", "allocation_key": "KEY-2"}),
		]
		with (
			patch.object(delivery_sync.frappe, "get_all", return_value=existing),
			patch.object(delivery_sync.frappe, "get_doc", side_effect=lambda doctype, name: docs[name]),
			patch.object(delivery_sync.frappe.db, "get_value", return_value=1),
			patch.object(delivery_sync, "now_datetime", return_value=datetime(2026, 8, 11, 12)),
		):
			first = delivery_sync._reconcile_delivery_ledger(
				"COMPANY-1", [], customer="CUSTOMER-1", item_codes=["ITEM-1"]
			)
			second = delivery_sync._reconcile_delivery_ledger(
				"COMPANY-1", [], customer="CUSTOMER-1", item_codes=["ITEM-1"]
			)
		self.assertEqual(first["reversed"], 2)
		self.assertEqual(second["reversed"], 0)
		self.assertTrue(all(doc.is_effective == 0 for doc in docs.values()))

	def test_nonzero_targetless_history_is_not_treated_as_settled(self):
		sources = [
			{
				**self.source,
				"source_delivery_note": "DN-1",
				"signed_qty": 60,
				"direct_schedule_item": None,
			},
			{
				**self.source,
				"source_delivery_note": "DN-RET-1",
				"source_delivery_note_item": "DNI-RET-1",
				"return_against": "DN-1",
				"signed_qty": -50,
				"direct_schedule_item": None,
			},
		]
		settled, _chains = delivery_sync._find_settled_delivery_history(sources, [])
		self.assertEqual(settled, set())

	def test_fully_returned_superseded_direct_history_can_settle(self):
		sources = [
			{
				**self.source,
				"source_delivery_note": "DN-1",
				"signed_qty": 60,
				"direct_schedule_item": "OLD-SUPERSEDED-TARGET",
			},
			{
				**self.source,
				"source_delivery_note": "DN-RET-1",
				"source_delivery_note_item": "DNI-RET-1",
				"return_against": "DN-1",
				"signed_qty": -60,
				"direct_schedule_item": None,
			},
		]
		settled, _chains = delivery_sync._find_settled_delivery_history(sources, [])
		self.assertEqual(settled, {"DNI-1", "DNI-RET-1"})
		self.assertEqual(
			delivery_sync._build_desired_delivery_allocations(
				sources,
				[],
				settled_source_items=settled,
				target_remap={"OLD-SUPERSEDED-TARGET": None},
			),
			[],
		)

	def test_delivery_rollup_marks_superseded_target_cancelled_with_zero_balance(self):
		rows = [
			delivery_sync.frappe._dict(
				{"name": "OLD-TARGET", "qty": 100, "schedule_status": "Superseded", "delivered_qty": 0}
			),
			delivery_sync.frappe._dict(
				{"name": "ACTIVE-TARGET", "qty": 100, "schedule_status": "Active", "delivered_qty": 30}
			),
		]
		with (
			patch.object(
				delivery_sync.frappe,
				"get_all",
				return_value=["OLD-TARGET", "ACTIVE-TARGET"],
			),
			patch.object(delivery_sync.frappe.db, "sql", return_value=rows),
			patch.object(delivery_sync.frappe.db, "set_value") as set_value,
		):
			summary = delivery_sync._rollup_delivery_allocations("COMPANY-1")
		updates = {call.args[1]: call.args[2] for call in set_value.call_args_list}
		self.assertEqual(updates["OLD-TARGET"]["status"], "Cancelled")
		self.assertEqual(updates["OLD-TARGET"]["balance_qty"], 0)
		self.assertEqual(updates["ACTIVE-TARGET"]["status"], "Open")
		self.assertEqual(summary["delivered_qty"], 30)

	def test_direct_target_must_be_active_positive_and_exact_sales_order_scope(self):
		with (
			patch.object(delivery_sync, "_", side_effect=lambda value: value),
			patch.object(delivery_sync.frappe, "throw", side_effect=_raise_validation),
		):
			with self.assertRaisesRegex(ValueError, "is not Active"):
				delivery_sync._validate_direct_target(
					self.source, {**self.target, "schedule_status": "Superseded"}
				)
			with self.assertRaisesRegex(ValueError, "no positive demand"):
				delivery_sync._validate_direct_target(self.source, {**self.target, "qty": 0})
			with self.assertRaisesRegex(ValueError, "sales order does not match"):
				delivery_sync._validate_direct_target(
					{**self.source, "sales_order": None}, self.target
				)


class TestAvailabilityScopePolicies(unittest.TestCase):
	def test_opening_stock_rewinds_only_future_events_and_current_run_replay(self):
		production_after = [availability.frappe._dict({"item_code": "FG-1", "qty": 10})]
		delivery_after = [availability.frappe._dict({"item_code": "FG-1", "qty": 3})]
		with (
			patch.object(availability.frappe.db, "sql", side_effect=[production_after, delivery_after]),
			patch.object(availability, "now_datetime", return_value=datetime(2026, 8, 11, 12)),
		):
			opening = availability._derive_opening_stock_by_item(
				"COMPANY-1",
				{"FG-1": 100},
				datetime(2026, 8, 11, 12),
				run_produced_through={"FG-1": 20},
				run_delivered_through={"FG-1": 5},
			)
		self.assertEqual(opening, {"FG-1": 78})

	def test_prior_run_production_is_not_subtracted_from_current_fg_bin(self):
		with (
			patch.object(availability.frappe.db, "sql", side_effect=[[], []]),
			patch.object(availability, "now_datetime", return_value=datetime(2026, 8, 11, 12)),
		):
			opening = availability._derive_opening_stock_by_item(
				"COMPANY-1", {"FG-1": 100}, datetime(2026, 8, 11, 12)
			)
		self.assertEqual(opening, {"FG-1": 100})

	def test_missing_finished_goods_scope_returns_zero_with_explicit_warning(self):
		warnings = []
		with (
			patch.object(availability, "_get_finished_goods_warehouses", return_value=[]),
			patch.object(availability, "_", side_effect=lambda value: value),
		):
			stock = availability._get_company_finished_goods_stock("COMPANY-1", warnings=warnings)
		self.assertEqual(stock, {})
		self.assertEqual(warnings[0]["code"], "FG_WAREHOUSE_SCOPE_MISSING")

	def test_blank_child_policy_inherits_result_default_max_prebuild_days(self):
		target = {
			"production_strategy": "",
			"demand_confidence": None,
			"cancellation_risk_percent": 0,
			"prebuild_allowed": 1,
			"max_prebuild_days": 0,
		}
		result = {
			"production_strategy": "Auto Balance",
			"demand_confidence": "Confirmed",
			"cancellation_risk_percent": 10,
			"prebuild_allowed": 1,
			"max_prebuild_days": 7,
		}
		self.assertTrue(availability._schedule_policy_matches(target, result))
		self.assertFalse(
			availability._schedule_policy_matches(
				{**target, "max_prebuild_days": 3},
				result,
			)
		)

	def test_schedule_target_can_only_be_claimed_by_one_result(self):
		claimed = set()
		rows = [{"name": "ROW-1", "qty": 50}, {"name": "ROW-2", "qty": 50}]
		self.assertEqual(
			[row["name"] for row in availability._claim_schedule_targets(rows, claimed, max_qty=50)],
			["ROW-1"],
		)
		first = availability._claim_schedule_targets(
			[{"name": "ROW-OVER", "qty": 100, "allocated_qty": 80}],
			set(),
			max_qty=50,
		)[0]
		self.assertEqual(first["attributed_qty"], 50)
		self.assertEqual(first["allocated_qty"], 50)
		self.assertEqual(
			[row["name"] for row in availability._claim_schedule_targets(rows, claimed, max_qty=50)],
			["ROW-2"],
		)

	def test_finished_goods_scope_uses_only_type_or_configured_warehouses(self):
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(availability.frappe, "get_meta", return_value=meta),
			patch.object(availability.frappe.db, "get_single_value", return_value="custom_fg"),
			patch.object(availability.frappe.db, "exists", return_value=True),
			patch.object(
				availability.frappe,
				"get_all",
				side_effect=[["FG-TYPE"], ["FG-CONFIG", "RAW-WAREHOUSE"], ["FG-CONFIG"]],
			) as get_all,
		):
			warehouses = availability._get_finished_goods_warehouses("COMPANY-1")
		self.assertEqual(warehouses, ["FG-CONFIG", "FG-TYPE"])
		first_filters = get_all.call_args_list[0].kwargs["filters"]
		valid_configured_filters = get_all.call_args_list[2].kwargs["filters"]
		self.assertEqual(first_filters["warehouse_type"], "Finished Goods")
		self.assertEqual(first_filters["disabled"], 0)
		self.assertEqual(valid_configured_filters["disabled"], 0)

	def test_finished_goods_stock_excludes_disabled_warehouses_in_sql(self):
		rows = [availability.frappe._dict({"item_code": "FG-1", "qty": 12})]
		with (
			patch.object(availability, "_get_finished_goods_warehouses", return_value=["FG-WH"]),
			patch.object(availability.frappe.db, "sql", return_value=rows) as sql,
		):
			stock = availability._get_company_finished_goods_stock("COMPANY-1")
		self.assertEqual(stock, {"FG-1": 12})
		self.assertIn("warehouse.disabled = 0", sql.call_args.args[0])

	def test_delivery_history_is_capped_without_mishandling_overflow_return(self):
		target = {"name": "ROW-1", "qty": 100, "attributed_qty": 50}
		rows = [
			{"effective_qty": 80, "source_delivery_note": "DN-1"},
			{"effective_qty": -30, "source_delivery_note": "DN-RET-1"},
		]
		attributed = availability._get_attributed_delivery_rows(target, rows)
		self.assertEqual([row["effective_qty"] for row in attributed], [50])
		self.assertEqual(sum(row["effective_qty"] for row in attributed), 50)

	def test_two_results_do_not_both_fallback_to_the_same_result_delivery_total(self):
		base = {
			"customer": "CUSTOMER-1",
			"item_code": "FG-1",
			"requested_date": date(2026, 8, 12),
			"demand_source": "Customer Delivery Schedule",
			"production_strategy": "Auto Balance",
			"planned_qty": 50,
			"prebuild_qty": 0,
			"jit_qty": 50,
			"early_days": 0,
			"late_qty_before_balance": 0,
			"late_qty_after_balance": 0,
			"delivered_qty": 30,
		}
		first = availability._build_result_projection(
			availability.frappe._dict({**base, "name": "RES-1"}),
			[],
			{},
			[],
			[{"name": "ROW-1", "qty": 50, "attributed_qty": 50, "allocated_qty": 0, "delivered_qty": 30}],
			{},
			opening_qty=0,
			as_of=datetime(2026, 8, 11, 12),
		)
		second = availability._build_result_projection(
			availability.frappe._dict({**base, "name": "RES-2"}),
			[],
			{},
			[],
			[{"name": "ROW-2", "qty": 50, "attributed_qty": 50, "allocated_qty": 0, "delivered_qty": 0}],
			{},
			opening_qty=0,
			as_of=datetime(2026, 8, 11, 12),
		)
		self.assertEqual(first["delivered_qty"], 30)
		self.assertEqual(second["delivered_qty"], 0)
		self.assertEqual(first["delivered_qty"] + second["delivered_qty"], 30)

	def test_only_sales_order_backlog_without_schedule_targets_uses_result_delivery_fallback(self):
		base = {
			"name": "RES-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"item_code": "FG-1",
			"requested_date": date(2026, 8, 12),
			"production_strategy": "Auto Balance",
			"planned_qty": 50,
			"prebuild_qty": 0,
			"jit_qty": 50,
			"early_days": 0,
			"late_qty_before_balance": 0,
			"late_qty_after_balance": 0,
			"delivered_qty": 30,
			"fulfillment_baseline_json": {
				"version": 3,
				"net_requirement": {"demand_qty": 50},
				"sales_order_items": [
					{
						"sales_order": "SO-1",
						"sales_order_item": "SOI-1",
						"item_code": "FG-1",
						"source_open_qty": 50,
						"opening_delivered_qty": 10,
					}
				],
			},
		}
		values = {}
		for demand_source in ("Sales Order Backlog", "Safety Stock"):
			projection = availability._build_result_projection(
				availability.frappe._dict({**base, "demand_source": demand_source}),
				[],
				{},
				[],
				[],
				{},
				opening_qty=0,
				as_of=datetime(2026, 8, 11, 12),
			)
			values[demand_source] = projection["delivered_qty"]
		self.assertEqual(values, {"Sales Order Backlog": 30, "Safety Stock": 0})


class TestConsistencySourceClaims(unittest.TestCase):
	def test_sales_order_backlog_physical_delivery_is_claimed_once_across_results(self):
		results = [
			consistency.frappe._dict({"name": "RES-1", "planned_qty": 50}),
			consistency.frappe._dict({"name": "RES-2", "planned_qty": 50}),
		]
		claims = consistency._claim_backlog_delivered_qty(results, 80)
		self.assertEqual([(row.name, qty) for row, qty in claims], [("RES-1", 50), ("RES-2", 30)])
		self.assertEqual(sum(qty for _row, qty in claims), 80)

	def test_two_results_cannot_both_claim_the_same_schedule_delivery(self):
		results = [
			consistency.frappe._dict({"name": "RES-1"}),
			consistency.frappe._dict({"name": "RES-2"}),
		]
		progress = consistency._progress_from_claimed_schedule_targets(
			results,
			{
				"RES-1": [
					{
						"name": "TARGET-1",
						"qty": 100,
						"attributed_qty": 50,
						"produced_qty": 80,
						"delivered_qty": 80,
					}
				],
				"RES-2": [],
			},
		)
		self.assertEqual(progress["RES-1"], {"produced_qty": 50, "delivered_qty": 50})
		self.assertEqual(progress["RES-2"], {"produced_qty": 0, "delivered_qty": 0})

	def test_multiple_claimed_targets_are_capped_independently(self):
		result = consistency.frappe._dict({"name": "RES-1"})
		progress = consistency._progress_from_claimed_schedule_targets(
			[result],
			{
				"RES-1": [
					{"name": "TARGET-1", "qty": 30, "attributed_qty": 30, "produced_qty": 40, "delivered_qty": 20},
					{"name": "TARGET-2", "qty": 20, "attributed_qty": 20, "produced_qty": 10, "delivered_qty": 25},
				]
			},
		)
		self.assertEqual(progress["RES-1"], {"produced_qty": 40, "delivered_qty": 40})

	def test_schedule_progress_subtracts_the_frozen_opening_fulfillment(self):
		result = consistency.frappe._dict({"name": "RES-1"})
		progress = consistency._progress_from_claimed_schedule_targets(
			[result],
			{
				"RES-1": [
					{
						"name": "TARGET-1",
						"qty": 100,
						"attributed_qty": 60,
						"produced_qty": 75,
						"delivered_qty": 55,
						"opening_produced_qty": 30,
						"opening_delivered_qty": 20,
					}
				]
			},
		)
		self.assertEqual(progress["RES-1"], {"produced_qty": 45, "delivered_qty": 35})

	def test_same_customer_item_and_date_keep_backlog_delivery_separate_by_sales_order_item(self):
		def baseline(sales_order, sales_order_item, opening):
			return {
				"version": 2,
				"sales_order_items": [
					{
						"sales_order": sales_order,
						"sales_order_item": sales_order_item,
						"item_code": "FG-1",
						"opening_delivered_qty": opening,
					}
				],
			}

		results = [
			consistency.frappe._dict(
				name="RES-SO-1",
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="FG-1",
				requested_date=date(2026, 8, 12),
				demand_source="Sales Order Backlog",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				planned_qty=100,
				fulfillment_baseline_json=baseline("SO-1", "SOI-1", 40),
			),
			consistency.frappe._dict(
				name="RES-SO-2",
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="FG-1",
				requested_date=date(2026, 8, 12),
				demand_source="Sales Order Backlog",
				sales_order="SO-2",
				sales_order_item="SOI-2",
				planned_qty=100,
				fulfillment_baseline_json=baseline("SO-2", "SOI-2", 5),
			),
		]
		with (
			patch.object(
				consistency.frappe.db,
				"exists",
				side_effect=lambda doctype, name=None, *_args, **_kwargs: doctype == "DocType" and name == "Sales Order",
			),
			patch.object(
				consistency,
				"_get_sales_order_delivered_total",
				side_effect=lambda _company, _customer, _item, sales_order, _soi: {"SO-1": 50, "SO-2": 20}[sales_order],
			),
		):
			progress = consistency._get_run_source_progress(results)
		self.assertEqual(progress["RES-SO-1"]["delivered_qty"], 10)
		self.assertEqual(progress["RES-SO-2"]["delivered_qty"], 15)

	def test_live_backlog_projection_uses_exact_so_item_and_opening_baseline(self):
		def result(name, sales_order, sales_order_item, opening):
			return availability.frappe._dict(
				name=name,
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="FG-1",
				demand_source="Sales Order Backlog",
				sales_order=sales_order,
				sales_order_item=sales_order_item,
				planned_qty=100,
				fulfillment_baseline_json={
					"version": 3,
					"sales_order_items": [
						{
							"sales_order": sales_order,
							"sales_order_item": sales_order_item,
							"item_code": "FG-1",
							"source_open_qty": 100,
							"opening_delivered_qty": opening,
						}
					],
				},
			)

		results = [result("RES-SO-1", "SO-1", "SOI-1", 40), result("RES-SO-2", "SO-2", "SOI-2", 5)]
		live_rows = [
			availability.frappe._dict(
				company="COMPANY-1", customer="CUSTOMER-1", item_code="FG-1",
				sales_order="SO-1", sales_order_item="SOI-1", delivered_qty=50, docstatus=1,
			),
			availability.frappe._dict(
				company="COMPANY-1", customer="CUSTOMER-1", item_code="FG-1",
				sales_order="SO-2", sales_order_item="SOI-2", delivered_qty=20, docstatus=1,
			),
		]
		with patch.object(availability.frappe.db, "sql", return_value=live_rows):
			progress = availability._get_backlog_incremental_delivery_map(results)
		self.assertEqual(progress, {"RES-SO-1": 10, "RES-SO-2": 15})


class TestCustomerProgressAttributionPolicies(unittest.TestCase):
	def test_family_or_blocked_result_segments_never_become_customer_supply(self):
		base_segment = planning.frappe._dict(
			name="SEG-1",
			segment_kind="Primary",
			segment_status="Planned",
			workstation="MACHINE-1",
			start_time=datetime(2026, 8, 11, 8),
			end_time=datetime(2026, 8, 11, 9),
			planned_qty=50,
		)
		self.assertTrue(
			planning._is_customer_schedule_progress_supply_segment(
				base_segment,
				planning.frappe._dict(status="Planned"),
			)
		)
		self.assertFalse(
			planning._is_customer_schedule_progress_supply_segment(
				planning.frappe._dict({**base_segment, "segment_kind": "Family Co-Product"}),
				planning.frappe._dict(status="Planned"),
			)
		)
		self.assertFalse(
			planning._is_customer_schedule_progress_supply_segment(
				base_segment,
				planning.frappe._dict(status="Blocked"),
			)
		)

	def test_minimum_batch_result_supply_is_split_across_frozen_schedule_dates(self):
		result = planning.frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			item_code="FG-1",
			requested_date=date(2026, 8, 11),
			machine_scheduled_qty=100,
			fulfillment_baseline_json={
				"version": 3,
				"targets": [
					{
						"customer_schedule_item": f"TARGET-{index}",
						"sales_order": "SO-1",
						"item_code": "FG-1",
						"schedule_date": str(date(2026, 8, 11 + index)),
						"source_open_qty": 10,
					}
					for index in range(3)
				],
			},
		)
		segment = planning.frappe._dict(
			name="SEG-1",
			end_time=datetime(2026, 8, 11, 12),
			actual_status="Not Started",
		)
		targets = planning._get_customer_schedule_progress_result_targets(result, "FG-1")
		supply = defaultdict(list)
		planning._emit_customer_schedule_progress_supply(
			supply,
			targets,
			result=result,
			segment=segment,
			item_code="FG-1",
			qty=100,
			completion_time=segment.end_time,
			source="Planned",
			execution={},
		)

		self.assertEqual(
			[(key[-1], sum(row["remaining_qty"] for row in rows)) for key, rows in sorted(supply.items())],
			[(date(2026, 8, 11), 10), (date(2026, 8, 12), 10), (date(2026, 8, 13), 10)],
		)

	def test_work_order_fallback_produced_qty_is_not_copied_to_each_segment(self):
		segments = [
			planning.frappe._dict(
				name="SEG-1",
				linked_work_order="WO-1",
				linked_scheduling_item=None,
				planned_qty=50,
				start_time=datetime(2026, 8, 11, 8),
				end_time=datetime(2026, 8, 11, 12),
			),
			planning.frappe._dict(
				name="SEG-2",
				linked_work_order="WO-1",
				linked_scheduling_item=None,
				planned_qty=50,
				start_time=datetime(2026, 8, 11, 12),
				end_time=datetime(2026, 8, 11, 16),
			),
		]
		with (
			patch.object(
				planning.frappe.db,
				"exists",
				side_effect=lambda doctype, name=None, **_kwargs: (
					doctype == "DocType" and name == "Work Order"
				),
			),
			patch.object(
				planning.frappe,
				"get_all",
				return_value=[planning.frappe._dict(name="WO-1", produced_qty=50)],
			),
		):
			snapshots = planning._get_customer_schedule_progress_execution_snapshots(segments)

		self.assertEqual(
			[ snapshots[name]["actual_completed_qty"] for name in ("SEG-1", "SEG-2") ],
			[50, 0],
		)
		self.assertEqual(sum(row["actual_completed_qty"] for row in snapshots.values()), 50)

	def test_selected_run_actual_output_is_rewound_from_current_fg_bin_before_replay(self):
		adjusted = planning._adjust_progress_stock_for_selected_run(
			{"FG-1": 100},
			[{"item_code": "FG-1", "actual_good_qty": 40, "delivered_qty": 10}],
		)
		self.assertEqual(adjusted, {"FG-1": 70})

	def test_production_supply_never_crosses_customer_sales_order_or_date(self):
		requested_date = date(2026, 8, 12)
		supply_map = {
			("COMPANY-1", "CUSTOMER-A", "SO-A", "FG-1", requested_date): [
				{"remaining_qty": 50, "result_name": "RES-A", "completion_time": datetime(2026, 8, 11, 10)}
			],
			("COMPANY-1", "CUSTOMER-B", "SO-B", "FG-1", requested_date): [
				{"remaining_qty": 60, "result_name": "RES-B", "completion_time": datetime(2026, 8, 11, 10)}
			],
		}
		row = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-A",
			"sales_order": "SO-A",
			"item_code": "FG-1",
			"schedule_date": requested_date,
			"uncovered_qty": 70,
		}
		planning._allocate_customer_schedule_progress_supply(row, supply_map)
		self.assertEqual(row["production_covered_qty"], 50)
		self.assertEqual(row["result_names"], ["RES-A"])
		self.assertEqual(supply_map[("COMPANY-1", "CUSTOMER-B", "SO-B", "FG-1", requested_date)][0]["remaining_qty"], 60)

	def test_duplicate_schedule_rows_claim_one_result_fulfillment_only_once(self):
		requested_date = date(2026, 8, 12)
		supply_map = {
			("COMPANY-1", "CUSTOMER-A", "SO-A", "FG-1", requested_date): [
				{
					"remaining_qty": 100,
					"result_name": "RES-A",
					"completion_time": datetime(2026, 8, 11, 10),
				}
			]
		}
		projection = {
			"result": "RES-A",
			"company": "COMPANY-1",
			"customer": "CUSTOMER-A",
			"sales_order": "SO-A",
			"item_code": "FG-1",
			"requested_date": requested_date,
			"planned_qty": 100,
			"prebuild_qty": 100,
			"jit_qty": 0,
			"actual_good_qty": 100,
			"current_deliverable_qty": 100,
		}
		rows = []
		for required_qty in (60, 60):
			row = {
				"company": "COMPANY-1",
				"customer": "CUSTOMER-A",
				"sales_order": "SO-A",
				"item_code": "FG-1",
				"schedule_date": requested_date,
				"required_qty": required_qty,
				"delivered_qty": 0,
				"uncovered_qty": required_qty,
			}
			planning._allocate_customer_schedule_progress_supply(row, supply_map)
			planning._attach_customer_schedule_fulfillment(row, [projection])
			rows.append(row)
		self.assertEqual([row["production_covered_qty"] for row in rows], [60, 40])
		self.assertEqual(sum(row["actual_good_qty"] for row in rows), 100)


if __name__ == "__main__":
	unittest.main()
