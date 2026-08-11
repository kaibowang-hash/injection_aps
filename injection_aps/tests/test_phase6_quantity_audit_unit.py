from __future__ import annotations

import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.services import consistency, planning


def _net_formula_baseline(
	*,
	demand_qty,
	available_stock_qty,
	open_work_order_qty,
	existing_work_order_policy,
	safety_stock_gap_qty=0,
	minimum_batch_qty=0,
	net_requirement_qty,
	planning_qty,
	minimum_batch_coverage_qty=None,
	new_batch_surplus_qty=None,
	is_safety_stock_group=0,
	**extra,
):
	base_residual_qty = max(
		demand_qty - available_stock_qty - open_work_order_qty + safety_stock_gap_qty,
		0,
	)
	if minimum_batch_coverage_qty is None:
		minimum_batch_coverage_qty = max(base_residual_qty - net_requirement_qty, 0)
	if new_batch_surplus_qty is None:
		new_batch_surplus_qty = (
			0 if is_safety_stock_group else max(planning_qty - net_requirement_qty, 0)
		)
	return json.dumps(
		{
			"version": 4,
			"net_requirement": {
				"formula_version": 1,
				"demand_qty": demand_qty,
				"available_stock_qty": available_stock_qty,
				"open_work_order_qty": open_work_order_qty,
				"existing_work_order_policy": existing_work_order_policy,
				"safety_stock_gap_qty": safety_stock_gap_qty,
				"minimum_batch_qty": minimum_batch_qty,
				"minimum_batch_coverage_qty": minimum_batch_coverage_qty,
				"base_residual_qty": base_residual_qty,
				"net_requirement_qty": net_requirement_qty,
				"planning_qty": planning_qty,
				"new_batch_surplus_qty": new_batch_surplus_qty,
				"is_safety_stock_group": is_safety_stock_group,
			},
			**extra,
		},
		sort_keys=True,
	)


def _production_row(**overrides):
	values = {
		"name": "PA-1",
		"planning_run": "RUN-1",
		"schedule_result": "RESULT-1",
		"segment": "SEG-1",
		"work_order": "WO-1",
		"work_order_scheduling": "WOS-1",
		"scheduling_item": "SI-1",
		"allocation_method": "Direct",
		"source_stock_entry": "SE-1",
		"source_stock_entry_detail": "SED-1",
		"source_docstatus": 1,
		"output_type": "Good",
		"source_qty": 100,
		"allocated_qty": 100,
		"good_qty": 100,
		"scrap_qty": 0,
		"effective_qty": 100,
		"live_stock_entry": "SE-1",
		"live_docstatus": 1,
		"live_purpose": "Manufacture",
		"live_work_order": "WO-1",
		"live_work_order_scheduling": "WOS-1",
		"live_direct_scheduling_item": "SI-1",
		"live_direct_segment": "SEG-1",
		"live_explicit_output_type": "Good",
		"live_detail": "SED-1",
		"live_detail_parent": "SE-1",
		"live_item_code": "FG-1",
		"live_document_qty": 100,
		"live_stock_qty": 100,
		"live_is_finished_item": 1,
		"live_is_scrap_item": 0,
		"live_target_warehouse": "FG-WH",
		"live_work_order_name": "WO-1",
		"live_production_item": "FG-1",
		"live_scrap_warehouse": "SCRAP-WH",
		"live_work_order_sales_order": "SO-1",
		"live_work_order_sales_order_item": "SOI-1",
		"live_work_order_run": "RUN-1",
		"live_work_order_result": "RESULT-1",
		"live_result": "RESULT-1",
		"live_result_run": "RUN-1",
		"result_item_code": "FG-1",
		"result_sales_order": "SO-1",
		"result_sales_order_item": "SOI-1",
		"live_segment": "SEG-1",
		"live_segment_result": "RESULT-1",
		"segment_linked_work_order": "WO-1",
		"segment_linked_work_order_scheduling": "WOS-1",
		"segment_linked_scheduling_item": "SI-1",
		"live_scheduling_item": "SI-1",
		"scheduling_item_parent": "WOS-1",
		"scheduling_item_work_order": "WO-1",
		"scheduling_item_run": "RUN-1",
		"scheduling_item_result": "RESULT-1",
		"scheduling_item_segment": "SEG-1",
		"live_allocation_wos": "WOS-1",
		"allocation_wos_run": "RUN-1",
		"allocation_wos_approval_state": "Approved",
	}
	values.update(overrides)
	return frappe._dict(values)


def _delivery_row(**overrides):
	values = {
		"name": "DA-1",
		"company": "COMPANY-1",
		"customer": "CUSTOMER-1",
		"item_code": "FG-1",
		"sales_order": "SO-1",
		"sales_order_item": "SOI-1",
		"schedule_date": "2026-08-20",
		"customer_schedule_item": "TARGET-1",
		"allocation_method": "Direct",
		"source_delivery_note": "DN-1",
		"source_delivery_note_item": "DNI-1",
		"source_docstatus": 1,
		"is_return": 0,
		"return_against": None,
		"original_delivery_note_item": None,
		"source_qty": 25,
		"allocated_qty": 25,
		"effective_qty": 25,
		"live_delivery_note": "DN-1",
		"live_docstatus": 1,
		"live_company": "COMPANY-1",
		"live_customer": "CUSTOMER-1",
		"live_is_return": 0,
		"live_posting_date": "2026-08-20",
		"live_return_against": None,
		"live_detail": "DNI-1",
		"live_detail_parent": "DN-1",
		"live_item_code": "FG-1",
		"live_stock_qty": 25,
		"live_document_qty": 25,
		"live_sales_order": "SO-1",
		"live_sales_order_item": "SOI-1",
		"live_original_delivery_note_item": None,
		"live_direct_schedule_item": "TARGET-1",
		"live_original_detail": None,
		"live_original_delivery_note": None,
		"live_original_item_code": None,
		"live_original_sales_order": None,
		"live_original_sales_order_item": None,
		"live_original_docstatus": None,
		"live_original_allocated_qty": 0,
	}
	values.update(overrides)
	return frappe._dict(values)


def _delivery_result(fulfillment_baseline_json):
	return frappe._dict(
		name="RESULT-1",
		company="COMPANY-1",
		customer="CUSTOMER-1",
		item_code="FG-1",
		sales_order="SO-1",
		sales_order_item="SOI-1",
		fulfillment_baseline_json=fulfillment_baseline_json,
	)


class TestPhase6QuantityAuditUnit(TestCase):
	def test_live_net_requirement_restores_open_work_order_boundary(self):
		source_snapshot = json.dumps(
			[
				{
					"demand_pool": "DEMAND-1",
					"source_doctype": "Customer Delivery Schedule",
					"source_name": "SCHEDULE-1",
					"source_detail_name": "TARGET-1",
					"qty": 100,
				}
			]
		)
		baseline = _net_formula_baseline(
			demand_qty=100,
			available_stock_qty=0,
			open_work_order_qty=60,
			existing_work_order_policy="Include",
			net_requirement_qty=40,
			planning_qty=40,
		)
		result = frappe._dict(
			name="RESULT-1",
			net_requirement="NR-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			item_code="FG-1",
			requested_date="2026-08-20",
			planned_qty=40,
			demand_source_snapshot_json=source_snapshot,
			fulfillment_baseline_json=baseline,
		)
		net = frappe._dict(
			name="NR-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			item_code="FG-1",
			demand_date="2026-08-20",
			demand_qty=100,
			available_stock_qty=0,
			planning_qty=40,
			net_requirement_qty=40,
			open_work_order_qty=60,
			safety_stock_gap_qty=0,
			minimum_batch_qty=0,
			existing_work_order_policy="Include",
			demand_source_snapshot_json=source_snapshot,
			fulfillment_baseline_json=baseline,
			is_system_generated=1,
		)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(consistency.frappe, "get_all", return_value=[net]):
			totals = consistency._get_audit_authoritative_plan(
				[result],
				run_existing_work_order_policy="Include",
				differences=differences,
			)
		self.assertEqual(totals, {"RESULT-1": 100})
		self.assertEqual(differences.total_count, 0)

	def test_missing_live_net_requirement_is_not_silently_accepted(self):
		result = frappe._dict(name="RESULT-1", net_requirement="NR-DELETED", planned_qty=20)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(consistency.frappe, "get_all", return_value=[]):
			totals = consistency._get_audit_authoritative_plan(
				[result],
				run_existing_work_order_policy="Exclude",
				differences=differences,
			)
		self.assertEqual(totals["RESULT-1"], 20)
		self.assertGreaterEqual(differences.total_count, 2)
		self.assertTrue(any(row["fieldname"] == "net_requirement" for row in differences))
		self.assertTrue(any(row["fieldname"] == "fulfillment_baseline_json" for row in differences))

	def test_cleared_live_net_requirement_uses_complete_v4_frozen_evidence(self):
		source_snapshot = json.dumps(
			[
				{
					"demand_pool": "D-1",
					"source_doctype": "Customer Delivery Schedule",
					"source_name": "SCHEDULE-1",
					"source_detail_name": "TARGET-1",
					"qty": 100,
				}
			]
		)
		result = frappe._dict(
			name="RESULT-1",
			net_requirement="",
			planned_qty=100,
			demand_source_snapshot_json=source_snapshot,
			fulfillment_baseline_json=_net_formula_baseline(
				demand_qty=100,
				available_stock_qty=0,
				open_work_order_qty=0,
				existing_work_order_policy="Exclude",
				net_requirement_qty=100,
				planning_qty=100,
			),
		)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(consistency.frappe, "get_all", return_value=[]):
			totals = consistency._get_audit_authoritative_plan(
				[result],
				run_existing_work_order_policy="Exclude",
				differences=differences,
			)
		self.assertEqual(totals, {"RESULT-1": 100})
		self.assertEqual(differences.total_count, 0)

	def test_net_requirement_cannot_self_prove_with_corrupted_derived_quantities(self):
		source_snapshot = json.dumps(
			[
				{
					"demand_pool": "D-1",
					"source_doctype": "Sales Order",
					"source_name": "SO-1",
					"source_detail_name": "SOI-1",
					"sales_order": "SO-1",
					"sales_order_item": "SOI-1",
					"qty": 100,
				}
			]
		)
		baseline = _net_formula_baseline(
			demand_qty=100,
			available_stock_qty=0,
			open_work_order_qty=0,
			existing_work_order_policy="Exclude",
			net_requirement_qty=80,
			planning_qty=80,
			minimum_batch_coverage_qty=20,
			new_batch_surplus_qty=0,
		)
		result = frappe._dict(
			name="RESULT-1",
			net_requirement="NR-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			item_code="FG-1",
			requested_date="2026-08-20",
			planned_qty=80,
			demand_source_snapshot_json=source_snapshot,
			fulfillment_baseline_json=baseline,
		)
		net = frappe._dict(
			name="NR-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			item_code="FG-1",
			demand_date="2026-08-20",
			demand_qty=100,
			available_stock_qty=0,
			open_work_order_qty=0,
			safety_stock_gap_qty=0,
			minimum_batch_qty=0,
			net_requirement_qty=80,
			planning_qty=80,
			existing_work_order_policy="Exclude",
			is_system_generated=1,
			demand_source_snapshot_json=source_snapshot,
			fulfillment_baseline_json=baseline,
		)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(consistency.frappe, "get_all", return_value=[net]):
			totals = consistency._get_audit_authoritative_plan(
				[result],
				run_existing_work_order_policy="Exclude",
				differences=differences,
			)
		self.assertEqual(totals, {"RESULT-1": 80})
		self.assertTrue(
			any(row["fieldname"] == "minimum_batch_coverage_qty" for row in differences)
		)

	def test_safety_stock_minimum_batch_coverage_requires_conserved_run_donor(self):
		def result_and_net(name, *, demand_qty, net_qty, planning_qty, coverage, surplus, safety):
			source_doctype = "Item" if safety else "Forecast"
			minimum_batch_qty = planning_qty if surplus > 0 else 0
			source_snapshot = json.dumps(
				[
					{
						"demand_pool": "POOL-{0}".format(name),
						"source_doctype": source_doctype,
						"source_name": "FG-1" if safety else "FORECAST-1",
						"source_detail_name": name,
						"qty": demand_qty,
					}
				]
			)
			baseline = _net_formula_baseline(
				demand_qty=demand_qty,
				available_stock_qty=0,
				open_work_order_qty=0,
				existing_work_order_policy="Exclude",
				minimum_batch_qty=minimum_batch_qty,
				net_requirement_qty=net_qty,
				planning_qty=planning_qty,
				minimum_batch_coverage_qty=coverage,
				new_batch_surplus_qty=surplus,
				is_safety_stock_group=safety,
			)
			result = frappe._dict(
				name=name,
				net_requirement="NR-{0}".format(name),
				company="COMPANY-1",
				customer="",
				sales_order="",
				sales_order_item="",
				item_code="FG-1",
				requested_date="2026-08-20",
				planned_qty=planning_qty,
				demand_source_snapshot_json=source_snapshot,
				fulfillment_baseline_json=baseline,
			)
			net = frappe._dict(
				name="NR-{0}".format(name),
				company="COMPANY-1",
				customer="",
				sales_order="",
				sales_order_item="",
				item_code="FG-1",
				demand_date="2026-08-20",
				demand_qty=demand_qty,
				available_stock_qty=0,
				open_work_order_qty=0,
				existing_work_order_policy="Exclude",
				safety_stock_gap_qty=0,
				minimum_batch_qty=minimum_batch_qty,
				net_requirement_qty=net_qty,
				planning_qty=planning_qty,
				is_system_generated=1,
				demand_source_snapshot_json=source_snapshot,
				fulfillment_baseline_json=baseline,
			)
			return result, net

		donor = result_and_net(
			"DONOR", demand_qty=20, net_qty=20, planning_qty=100, coverage=0, surplus=80, safety=0
		)
		safety = result_and_net(
			"SAFETY-1", demand_qty=100, net_qty=20, planning_qty=20, coverage=80, surplus=0, safety=1
		)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(consistency.frappe, "get_all", return_value=[donor[1], safety[1]]):
			consistency._get_audit_authoritative_plan(
				[donor[0], safety[0]],
				run_existing_work_order_policy="Exclude",
				differences=differences,
			)
		self.assertEqual(differences.total_count, 0)

		regular_consumer = result_and_net(
			"REGULAR-CONSUMER",
			demand_qty=60,
			net_qty=0,
			planning_qty=0,
			coverage=60,
			surplus=0,
			safety=0,
		)
		overdrawn_safety = result_and_net(
			"SAFETY-OVERDRAWN",
			demand_qty=50,
			net_qty=0,
			planning_qty=0,
			coverage=50,
			surplus=0,
			safety=1,
		)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(
			consistency.frappe,
			"get_all",
			return_value=[donor[1], regular_consumer[1], overdrawn_safety[1]],
		):
			consistency._get_audit_authoritative_plan(
				[donor[0], regular_consumer[0], overdrawn_safety[0]],
				run_existing_work_order_policy="Exclude",
				differences=differences,
			)
		self.assertTrue(
			any(
				row["source"].startswith("minimum_batch_surplus_conservation")
				for row in differences
			)
		)

		duplicate_safety = result_and_net(
			"SAFETY-2", demand_qty=100, net_qty=20, planning_qty=20, coverage=80, surplus=0, safety=1
		)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(
			consistency.frappe,
			"get_all",
			return_value=[donor[1], safety[1], duplicate_safety[1]],
		):
			consistency._get_audit_authoritative_plan(
				[donor[0], safety[0], duplicate_safety[0]],
				run_existing_work_order_policy="Exclude",
				differences=differences,
			)
		self.assertTrue(
			any(row["source"].startswith("minimum_batch_surplus_conservation") for row in differences)
		)

		direct_differences = consistency._AuditDifferenceCollector()
		consistency._validate_audit_net_requirement(
			safety[1],
			run_existing_work_order_policy="Exclude",
			differences=direct_differences,
		)
		self.assertTrue(
			any(row["source"] == "minimum_batch_surplus_conservation" for row in direct_differences)
		)

	def test_production_requires_live_submitted_parent_and_matching_detail(self):
		database = MagicMock()
		database.exists.return_value = True
		database.has_column.return_value = True
		database.sql.return_value = [_production_row()]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_production_totals(
				"RUN-1", result_names=["RESULT-1"]
			)
		self.assertEqual(totals["by_result"]["RESULT-1"]["good_qty"], 100)
		self.assertEqual(totals["source_count"], 1)
		self.assertEqual(totals["invalid_sources"].total_count, 0)

		database.sql.return_value = [
			_production_row(live_stock_entry=None, live_detail=None, live_docstatus=None)
		]
		with patch.object(consistency.frappe, "db", database):
			invalid = consistency._get_audit_production_totals(
				"RUN-1", result_names=["RESULT-1"]
			)
		self.assertEqual(invalid["by_result"], {})
		self.assertEqual(invalid["source_count"], 0)
		self.assertGreaterEqual(invalid["invalid_sources"].total_count, 2)

	def test_production_rejects_cached_qty_and_output_that_disagree_with_live_detail(self):
		database = MagicMock()
		database.exists.return_value = True
		database.has_column.side_effect = (
			lambda doctype, fieldname: not (doctype == "Stock Entry Detail" and fieldname == "is_scrap_item")
		)
		database.sql.return_value = [
			_production_row(
				output_type="Scrap",
				source_qty=90,
				allocated_qty=90,
				good_qty=0,
				scrap_qty=90,
				effective_qty=90,
				live_explicit_output_type="Good",
			)
		]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_production_totals(
				"RUN-1", result_names=["RESULT-1"]
			)
		fields = {row["fieldname"] for row in totals["invalid_sources"]}
		self.assertIn("output_type", fields)
		self.assertIn("source_qty", fields)
		self.assertIn("effective_allocation_qty", fields)
		self.assertEqual(totals["source_count"], 0)

	def test_production_source_cannot_be_claimed_by_another_run(self):
		database = MagicMock()
		database.exists.return_value = True
		database.has_column.return_value = True
		database.sql.return_value = [
			_production_row(
				name="PA-OTHER",
				planning_run="RUN-OTHER",
				live_result_run="RUN-OTHER",
			),
		]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_production_totals(
				"RUN-1", result_names=["RESULT-1"]
			)
		self.assertEqual(totals["source_count"], 0)
		self.assertTrue(
			any(
				row["fieldname"] == "planning_run"
				and row["source"] == "cross_run_source_claim"
				for row in totals["invalid_sources"]
			)
		)

	def test_production_rejects_wrong_direct_execution_lineage(self):
		database = MagicMock()
		database.exists.return_value = True
		database.has_column.return_value = True
		database.sql.return_value = [
			_production_row(
				live_direct_segment="SEG-OTHER",
				live_direct_scheduling_item="SI-OTHER",
			)
		]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_production_totals("RUN-1", result_names=["RESULT-1"])
		fields = {row["fieldname"] for row in totals["invalid_sources"]}
		self.assertIn("direct_segment", fields)
		self.assertIn("direct_scheduling_item", fields)
		self.assertEqual(totals["source_count"], 0)

		database.sql.return_value = [
			_production_row(
				live_work_order_result=None,
				live_work_order_sales_order_item="SOI-OTHER",
			)
		]
		with patch.object(consistency.frappe, "db", database):
			owner_mismatch = consistency._get_audit_production_totals(
				"RUN-1", result_names=["RESULT-1"]
			)
		owner_fields = {row["fieldname"] for row in owner_mismatch["invalid_sources"]}
		self.assertIn("work_order_aps_owner", owner_fields)
		self.assertIn("work_order_sales_order_item", owner_fields)

	def test_production_null_detail_and_missing_schema_fail_closed(self):
		database = MagicMock()
		database.exists.return_value = True
		database.has_column.return_value = True
		database.sql.return_value = [
			_production_row(source_stock_entry_detail=None, live_detail=None)
		]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_production_totals("RUN-1", result_names=["RESULT-1"])
		self.assertTrue(any(row["fieldname"] == "source_stock_entry_detail" for row in totals["invalid_sources"]))
		self.assertIn("pa.planning_run = %(run_name)s", database.sql.call_args_list[0].args[0])

		database = MagicMock()
		database.exists.return_value = False
		with patch.object(consistency.frappe, "db", database):
			missing = consistency._get_audit_production_totals("RUN-1", result_names=[])
		self.assertGreaterEqual(missing["invalid_sources"].total_count, 1)
		self.assertTrue(all(row["source"] == "phase6_schema" for row in missing["invalid_sources"]))

		database = MagicMock()
		database.exists.return_value = True
		database.has_column.side_effect = lambda doctype, fieldname: not (
			doctype == "APS Production Allocation" and fieldname == "source_stock_entry_detail"
		)
		with patch.object(consistency.frappe, "db", database):
			missing_column = consistency._get_audit_production_totals("RUN-1", result_names=[])
		self.assertTrue(
			any(
				row["name"] == "APS Production Allocation"
				and row["fieldname"] == "source_stock_entry_detail"
				for row in missing_column["invalid_sources"]
			)
		)
		database.sql.assert_not_called()

	def test_submitted_manufacture_detail_missing_from_ledger_fails_audit(self):
		database = MagicMock()
		database.exists.return_value = True
		database.has_column.return_value = True
		database.sql.side_effect = [
			[],
			[
				frappe._dict(
					source_stock_entry="SE-UNSYNCED",
					source_stock_entry_detail="SED-UNSYNCED",
					live_document_qty=15,
					live_stock_qty=15,
				)
			],
		]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_production_totals(
				"RUN-1", result_names=["RESULT-1"]
			)
		self.assertEqual(totals["source_count"], 0)
		self.assertEqual(totals["invalid_sources"].total_count, 1)
		self.assertEqual(
			totals["invalid_sources"][0]["source"],
			"submitted_manufacture_output_missing_from_aps_ledger",
		)
		self.assertIn(
			"wo.production_item is null or sed.item_code = wo.production_item",
			database.sql.call_args_list[1].args[0],
		)

	def test_delivery_requires_live_parent_detail_and_exact_sales_order_item(self):
		result = _delivery_result(
			json.dumps({"targets": [{"customer_schedule_item": "TARGET-1"}]})
		)
		database = MagicMock()
		database.exists.return_value = True
		database.sql.return_value = [_delivery_row()]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_delivery_totals([result])
		self.assertEqual(totals["by_target"], {"TARGET-1": 25})
		self.assertEqual(totals["source_count"], 1)

		database.sql.return_value = [
			_delivery_row(live_sales_order_item="SOI-OTHER", live_detail_parent="DN-OTHER")
		]
		with patch.object(consistency.frappe, "db", database):
			invalid = consistency._get_audit_delivery_totals([result])
		fields = {row["fieldname"] for row in invalid["invalid_sources"]}
		self.assertIn("sales_order_item", fields)
		self.assertIn("source_detail_parent", fields)
		self.assertEqual(invalid["source_count"], 0)

		database.sql.return_value = [
			_delivery_row(sales_order_item="SOI-OTHER", live_sales_order_item="SOI-OTHER")
		]
		with patch.object(consistency.frappe, "db", database):
			wrong_owner = consistency._get_audit_delivery_totals([result])
		self.assertTrue(
			any(
				row["fieldname"] == "fulfillment_owner_sales_order_item"
				for row in wrong_owner["invalid_sources"]
			)
		)

	def test_submitted_delivery_detail_missing_from_ledger_fails_audit(self):
		result = _delivery_result(
			json.dumps({"targets": [{"customer_schedule_item": "TARGET-1"}]})
		)
		database = MagicMock()
		database.exists.return_value = True
		database.sql.side_effect = [
			[],
			[
				frappe._dict(
					source_delivery_note="DN-UNSYNCED",
					source_delivery_note_item="DNI-UNSYNCED",
					live_document_qty=12,
					live_stock_qty=12,
				)
			],
		]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_delivery_totals([result])
		self.assertEqual(totals["source_count"], 0)
		self.assertEqual(totals["invalid_sources"].total_count, 1)
		self.assertEqual(
			totals["invalid_sources"][0]["source"],
			"submitted_delivery_detail_missing_from_aps_ledger",
		)

	def test_delivery_rejects_wrong_fifo_date_and_null_detail(self):
		result = _delivery_result(
			json.dumps(
				{
					"targets": [
						{
							"customer_schedule_item": "TARGET-1",
							"schedule_date": "2026-08-20",
						}
					]
				}
			)
		)
		database = MagicMock()
		database.exists.return_value = True
		database.sql.return_value = [
			_delivery_row(
				allocation_method="Controlled FIFO",
				live_direct_schedule_item=None,
				live_posting_date="2026-08-19",
			)
		]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_delivery_totals([result])
		self.assertTrue(any(row["fieldname"] == "fifo_schedule_date" for row in totals["invalid_sources"]))
		self.assertEqual(totals["source_count"], 0)

		database.sql.return_value = [
			_delivery_row(source_delivery_note_item=None, live_detail=None)
		]
		with patch.object(consistency.frappe, "db", database):
			null_detail = consistency._get_audit_delivery_totals([result])
		self.assertTrue(
			any(row["fieldname"] == "source_delivery_note_item" for row in null_detail["invalid_sources"])
		)
		self.assertIn("da.customer_schedule_item in %(target_names)s", database.sql.call_args_list[-2].args[0])

	def test_delivery_return_trace_requires_exact_original_allocation(self):
		result = _delivery_result(
			json.dumps(
				{
					"targets": [
						{
							"customer_schedule_item": "TARGET-1",
							"schedule_date": "2026-08-20",
						}
					]
				}
			)
		)
		returned = _delivery_row(
			name="DA-RETURN",
			allocation_method="Return Trace",
			source_delivery_note="DN-2",
			source_delivery_note_item="DNI-2",
			is_return=1,
			return_against="DN-1",
			original_delivery_note_item="DNI-1",
			source_qty=10,
			allocated_qty=10,
			effective_qty=-10,
			live_delivery_note="DN-2",
			live_is_return=1,
			live_return_against="DN-1",
			live_detail="DNI-2",
			live_detail_parent="DN-2",
			live_stock_qty=10,
			live_document_qty=10,
			live_original_delivery_note_item="DNI-1",
			live_direct_schedule_item=None,
			live_original_detail="DNI-1",
			live_original_delivery_note="DN-1",
			live_original_item_code="FG-1",
			live_original_sales_order="SO-1",
			live_original_sales_order_item="SOI-1",
			live_original_docstatus=1,
			live_original_allocated_qty=25,
		)
		database = MagicMock()
		database.exists.return_value = True
		database.sql.return_value = [_delivery_row(), returned]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_delivery_totals([result])
		self.assertEqual(totals["by_target"], {"TARGET-1": 15})
		self.assertEqual(totals["invalid_sources"].total_count, 0)

		returned.live_original_sales_order_item = "SOI-OTHER"
		with patch.object(consistency.frappe, "db", database):
			invalid = consistency._get_audit_delivery_totals([result])
		self.assertTrue(
			any(row["fieldname"] == "original_sales_order_item" for row in invalid["invalid_sources"])
		)

	def test_backlog_delivery_is_rebuilt_from_submitted_details_and_return(self):
		result = frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="FG-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			demand_source="Sales Order Backlog",
			fulfillment_baseline_json=json.dumps(
				{
					"sales_order_items": [
						{
							"sales_order": "SO-1",
							"sales_order_item": "SOI-1",
								"item_code": "FG-1",
								"source_open_qty": 100,
								"opening_ordered_qty": 100,
								"opening_delivered_qty": 0,
						}
					]
				}
			),
		)
		live_so_item = frappe._dict(
			sales_order_item="SOI-1",
			sales_order="SO-1",
			item_code="FG-1",
			ordered_qty=100,
			delivered_qty=20,
			live_sales_order="SO-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			docstatus=1,
		)
		normal = frappe._dict(
			source_delivery_note="DN-1",
			source_delivery_note_item="DNI-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="FG-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			is_return=0,
			live_document_qty=30,
			live_stock_qty=30,
		)
		returned = frappe._dict(
			{
				**dict(normal),
				"source_delivery_note": "DN-2",
				"source_delivery_note_item": "DNI-2",
				"is_return": 1,
				"return_against": "DN-1",
				"sales_order": None,
				"sales_order_item": None,
				"original_delivery_note_item": None,
				"live_document_qty": 10,
				"live_stock_qty": 10,
			}
		)
		database = MagicMock()
		database.exists.return_value = True
		database.sql.side_effect = [[live_so_item], [normal, returned]]
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_delivery_totals([result])
		key = ("COMPANY-1", "CUSTOMER-1", "FG-1", "SO-1", "SOI-1")
		self.assertEqual(totals["backlog_by_key"], {key: 20})
		self.assertEqual(totals["source_count"], 2)
		self.assertEqual(totals["invalid_sources"].total_count, 0)
		self.assertIn("return_scope", database.sql.call_args_list[1].args[0])

		live_so_item.ordered_qty = 120
		database.sql.side_effect = [[live_so_item], [normal, returned]]
		with patch.object(consistency.frappe, "db", database):
			changed_order = consistency._get_audit_delivery_totals([result])
		self.assertTrue(
			any(
				row["doctype"] == "Sales Order Item" and row["fieldname"] == "qty"
				for row in changed_order["invalid_sources"]
			)
		)

	def test_missing_delivery_schema_fails_closed(self):
		database = MagicMock()
		database.exists.return_value = False
		with patch.object(consistency.frappe, "db", database):
			totals = consistency._get_audit_delivery_totals([])
		self.assertGreaterEqual(totals["invalid_sources"].total_count, 1)
		self.assertTrue(all(row["source"] == "phase6_schema" for row in totals["invalid_sources"]))

	def test_customer_schedule_baseline_is_required_and_complete(self):
		row = frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="FG-1",
			sales_order="SO-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json="{}",
		)
		differences = consistency._AuditDifferenceCollector()
		with (
			patch.object(consistency, "_get_audit_live_schedule_targets", return_value={}),
			patch.object(consistency, "_get_audit_backlog_delivery_by_result", return_value={}),
		):
			expected = consistency._get_audit_expected_delivery_by_result(
				[row], delivery={"by_target": {}, "backlog_by_key": {}}, differences=differences
			)
		self.assertEqual(expected, {"RESULT-1": 0})
		self.assertGreater(differences.total_count, 0)
		self.assertTrue(any(row["fieldname"] == "fulfillment_baseline_json" for row in differences))

	def test_cross_run_target_owner_is_blocking(self):
		baseline = {
			"targets": [
				{
					"customer_schedule": "SCHEDULE-1",
					"customer_schedule_item": "TARGET-1",
					"sales_order": "SO-1",
					"sales_order_item": "SOI-1",
					"item_code": "FG-1",
					"schedule_date": "2026-08-20",
					"source_open_qty": 100,
					"opening_required_qty": 100,
					"opening_delivered_qty": 0,
				}
			]
		}
		row = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="FG-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			demand_source_snapshot_json=json.dumps(
				[
					{
						"source_doctype": "Customer Delivery Schedule",
						"source_name": "SCHEDULE-1",
						"source_detail_name": "TARGET-1",
						"sales_order": "SO-1",
						"sales_order_item": "SOI-1",
						"qty": 100,
					}
				]
			),
		)
		live = frappe._dict(
			name="TARGET-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_code="FG-1",
			sales_order="SO-1",
			schedule_date="2026-08-20",
			qty=100,
			delivered_qty=0,
			schedule_status="Active",
			item_status="Open",
		)
		differences = consistency._AuditDifferenceCollector()
		with (
			patch.object(consistency, "_get_audit_live_schedule_targets", return_value={"TARGET-1": live}),
			patch.object(
				consistency,
				"_get_audit_cross_run_fulfillment_claims",
				return_value={"targets": {"TARGET-1": ["RESULT-OTHER"]}, "sales_order_items": {}},
			),
			patch.object(consistency, "_get_audit_backlog_delivery_by_result", return_value={}),
		):
			consistency._get_audit_expected_delivery_by_result(
				[row],
				delivery={"by_target": {}, "backlog_by_key": {}},
				differences=differences,
				audited_run="RUN-1",
			)
		self.assertTrue(any(row["fieldname"] == "active_run_owner" for row in differences))

	def test_run_scrap_total_is_independently_audited(self):
		run = frappe._dict(
			status="Applied",
			existing_work_order_policy="Exclude",
			total_net_requirement_qty=0,
			total_machine_scheduled_qty=0,
			total_demand_covered_qty=0,
			total_overproduction_qty=0,
			total_scheduled_qty=0,
			total_unscheduled_qty=0,
			total_produced_qty=0,
			total_scrap_qty=2,
			total_delivered_qty=0,
			result_count=0,
		)
		database = MagicMock()
		database.get_value.return_value = run
		empty_production = {
			"by_result": {},
			"by_segment": {},
			"invalid_sources": consistency._AuditDifferenceCollector(),
			"source_count": 0,
		}
		empty_delivery = {
			"by_target": {},
			"backlog_by_key": {},
			"invalid_sources": consistency._AuditDifferenceCollector(),
			"source_count": 0,
		}
		with (
			patch.object(consistency.frappe, "db", database),
			patch.object(consistency.frappe, "get_all", return_value=[]),
			patch.object(consistency, "_get_audit_production_totals", return_value=empty_production),
			patch.object(consistency, "_get_audit_delivery_totals", return_value=empty_delivery),
		):
			audit = consistency.audit_run_quantity_consistency("RUN-1")
		self.assertFalse(audit["valid"])
		self.assertTrue(any(row["fieldname"] == "total_scrap_qty" for row in audit["differences"]))

	def test_schedule_delivery_uses_opening_cap_and_claims_target_once(self):
		baseline = {
			"targets": [
				{
					"customer_schedule": "SCHEDULE-1",
					"customer_schedule_item": "TARGET-1",
					"sales_order": "SO-1",
					"sales_order_item": "SOI-1",
					"item_code": "FG-1",
					"schedule_date": "2026-08-20",
					"opening_required_qty": 100,
					"opening_delivered_qty": 20,
					"source_open_qty": 80,
					"attributed_qty": 30,
				}
			]
		}
		rows = [
			frappe._dict(
				name="RESULT-1",
				company="COMPANY-1",
				customer="CUSTOMER-1",
					sales_order="SO-1",
					sales_order_item="SOI-1",
					item_code="FG-1",
				demand_source="Customer Delivery Schedule",
					fulfillment_baseline_json=json.dumps(baseline),
					demand_source_snapshot_json=json.dumps(
						[
							{
								"source_doctype": "Customer Delivery Schedule",
								"source_name": "SCHEDULE-1",
								"source_detail_name": "TARGET-1",
								"sales_order": "SO-1",
								"sales_order_item": "SOI-1",
								"qty": 80,
							}
						]
					),
			),
			frappe._dict(
				name="RESULT-2",
				company="COMPANY-1",
				customer="CUSTOMER-1",
					sales_order="SO-1",
					sales_order_item="SOI-1",
					item_code="FG-1",
				demand_source="Customer Delivery Schedule",
					fulfillment_baseline_json=json.dumps(baseline),
					demand_source_snapshot_json=json.dumps(
						[
							{
								"source_doctype": "Customer Delivery Schedule",
								"source_name": "SCHEDULE-1",
								"source_detail_name": "TARGET-1",
								"sales_order": "SO-1",
								"sales_order_item": "SOI-1",
								"qty": 80,
							}
						]
					),
			),
		]
		live = frappe._dict(
			name="TARGET-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			item_code="FG-1",
			qty=100,
			delivered_qty=80,
			schedule_status="Active",
			item_status="Open",
			schedule_date="2026-08-20",
		)
		differences = consistency._AuditDifferenceCollector()
		with (
			patch.object(consistency, "_get_audit_live_schedule_targets", return_value={"TARGET-1": live}),
			patch.object(consistency, "_get_audit_backlog_delivery_by_result", return_value={}),
		):
			expected = consistency._get_audit_expected_delivery_by_result(
				rows,
				delivery={"by_target": {"TARGET-1": 80}},
				differences=differences,
			)
		self.assertEqual(expected, {"RESULT-1": 30, "RESULT-2": 0})
		self.assertTrue(
			any(row["fieldname"] == "schedule_result_claim_count" for row in differences)
		)

	def test_accepted_demand_delta_epoch_preserves_opening_and_rebases_current_demand(self):
		baseline = {
			"accepted_source_demand_delta": "DELTA-1",
			"accepted_by_change_request": "CHANGE-1",
			"targets": [
				{
					"customer_schedule": "SCHEDULE-1",
					"customer_schedule_item": "TARGET-1",
					"sales_order": "SO-1",
					"sales_order_item": "SOI-1",
					"item_code": "FG-1",
					"schedule_date": "2026-08-20",
					"opening_required_qty": 100,
					"opening_delivered_qty": 20,
					"source_open_qty": 80,
					"accepted_required_qty": 120,
					"accepted_delivered_qty": 50,
					"accepted_source_open_qty": 100,
					"accepted_current_open_qty": 70,
					"accepted_schedule_date": "2026-08-22",
					"accepted_source_demand_delta": "DELTA-1",
					"accepted_by_change_request": "CHANGE-1",
				}
			]
		}
		row = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			item_code="FG-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			demand_source_snapshot_json=json.dumps(
				[
					{
						"source_doctype": "Customer Delivery Schedule",
						"source_name": "SCHEDULE-1",
						"source_detail_name": "TARGET-1",
						"sales_order": "SO-1",
						"sales_order_item": "SOI-1",
						"qty": 70,
					}
				]
			),
		)
		after_snapshot = {
			"run": {"name": "RUN-1"},
			"results": [
				{
					"name": "RESULT-1",
					"fulfillment_baseline_json": row.fulfillment_baseline_json,
					"demand_source_snapshot_json": row.demand_source_snapshot_json,
				}
			],
		}
		proposal = {
			"engine_version": "test-v1",
			"change_request": "CHANGE-1",
			"source_demand_delta": "DELTA-1",
		}
		analysis_fingerprint = "ANALYSIS-1"
		application_fingerprint = consistency._hash_audit_payload(
			{
				"change_request": "CHANGE-1",
				"analysis_fingerprint": analysis_fingerprint,
				"engine_version": "test-v1",
			}
		)
		request = frappe._dict(
			name="CHANGE-1",
			status="Applied",
			planning_run="RUN-1",
			target_result="RESULT-1",
			source_demand_delta="DELTA-1",
			application_log="LOG-1",
			application_fingerprint=application_fingerprint,
			analysis_fingerprint=analysis_fingerprint,
			apply_count=1,
			proposal_json=json.dumps(proposal),
			after_snapshot_json=json.dumps(after_snapshot),
		)
		application_log = frappe._dict(
			name="LOG-1",
			change_request="CHANGE-1",
			planning_run="RUN-1",
			application_fingerprint=application_fingerprint,
			analysis_fingerprint=analysis_fingerprint,
			proposal_json=json.dumps(proposal),
			after_snapshot_hash=consistency._hash_audit_payload(after_snapshot),
			after_snapshot_json=json.dumps(after_snapshot),
		)

		def get_all(doctype, **_kwargs):
			return [request] if doctype == "APS Change Request" else [application_log]

		live = frappe._dict(
			name="TARGET-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			item_code="FG-1",
			qty=120,
			delivered_qty=80,
			schedule_status="Active",
			item_status="Open",
			schedule_date="2026-08-22",
		)
		differences = consistency._AuditDifferenceCollector()
		with (
			patch.object(consistency, "_get_audit_live_schedule_targets", return_value={"TARGET-1": live}),
			patch.object(consistency, "_get_audit_backlog_delivery_by_result", return_value={}),
			patch.object(consistency.frappe, "get_all", side_effect=get_all),
		):
			expected = consistency._get_audit_expected_delivery_by_result(
				[row],
				delivery={"by_target": {"TARGET-1": 80}},
				differences=differences,
			)
		self.assertEqual(expected, {"RESULT-1": 60})
		self.assertEqual(differences.total_count, 0)

		valid_snapshot_hash = application_log.after_snapshot_hash
		application_log.after_snapshot_hash = "forged"
		provenance_differences = consistency._AuditDifferenceCollector()
		with patch.object(consistency.frappe, "get_all", side_effect=get_all):
			consistency._validate_audit_accepted_epoch_provenance(
				[row],
				audited_run="RUN-1",
				differences=provenance_differences,
			)
		self.assertTrue(
			any(row["fieldname"] == "after_snapshot_hash" for row in provenance_differences)
		)
		application_log.after_snapshot_hash = valid_snapshot_hash

		live.delivered_qty = 10
		differences = consistency._AuditDifferenceCollector()
		with (
			patch.object(consistency, "_get_audit_live_schedule_targets", return_value={"TARGET-1": live}),
			patch.object(consistency, "_get_audit_backlog_delivery_by_result", return_value={}),
			patch.object(consistency.frappe, "get_all", side_effect=get_all),
		):
			consistency._get_audit_expected_delivery_by_result(
				[row],
				delivery={"by_target": {"TARGET-1": 10}},
				differences=differences,
			)
		self.assertTrue(
			any(
				difference["source"] == "return_below_frozen_opening_requires_run_rebuild"
				for difference in differences
			)
		)
		live.delivered_qty = 80

		baseline["targets"][0]["accepted_source_open_qty"] = 70
		row.fulfillment_baseline_json = json.dumps(baseline)
		differences = consistency._AuditDifferenceCollector()
		consistency._get_audit_validated_schedule_targets(row, differences=differences)
		self.assertTrue(
			any(row["fieldname"] == "accepted_source_open_qty" for row in differences)
		)

	def test_accepted_epoch_rejects_missing_or_forged_external_apply_evidence(self):
		baseline = {
			"accepted_source_demand_delta": "DELTA-1",
			"accepted_by_change_request": "CHANGE-1",
			"targets": [
				{
					"customer_schedule_item": "TARGET-1",
					"accepted_required_qty": 120,
					"accepted_delivered_qty": 50,
					"accepted_source_open_qty": 100,
					"accepted_current_open_qty": 70,
					"accepted_schedule_date": "2026-08-22",
					"accepted_source_demand_delta": "DELTA-1",
					"accepted_by_change_request": "CHANGE-1",
				}
			],
		}
		result = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			fulfillment_baseline_json=json.dumps(baseline),
			demand_source_snapshot_json="[]",
		)
		differences = consistency._AuditDifferenceCollector()
		with patch.object(consistency.frappe, "get_all", return_value=[]):
			consistency._validate_audit_accepted_epoch_provenance(
				[result],
				audited_run="RUN-1",
				differences=differences,
			)
		self.assertTrue(any(row["fieldname"] == "accepted_by_change_request" for row in differences))

		request = frappe._dict(
			name="CHANGE-1",
			status="Applied",
			planning_run="RUN-1",
			target_result="RESULT-1",
			source_demand_delta="DELTA-1",
			application_log="LOG-1",
			application_fingerprint="FINGERPRINT-1",
			apply_count=1,
			after_snapshot_json="{}",
		)
		logs = [
			frappe._dict(
				name="LOG-1",
				change_request="CHANGE-1",
				planning_run="RUN-1",
				application_fingerprint="FINGERPRINT-1",
				after_snapshot_hash="forged",
				after_snapshot_json="{}",
			),
			frappe._dict(
				name="LOG-2",
				change_request="CHANGE-1",
				planning_run="RUN-1",
				application_fingerprint="FINGERPRINT-1",
				after_snapshot_hash="forged",
				after_snapshot_json="{}",
			),
		]
		differences = consistency._AuditDifferenceCollector()
		with patch.object(
			consistency.frappe,
			"get_all",
			side_effect=lambda doctype, **_kwargs: [request]
			if doctype == "APS Change Request"
			else logs,
		):
			consistency._validate_audit_accepted_epoch_provenance(
				[result],
				audited_run="RUN-1",
				differences=differences,
			)
		self.assertTrue(any(row["fieldname"] == "application_log" for row in differences))

	def test_normal_consistency_uses_accepted_target_epoch(self):
		baseline = json.dumps(
			{
				"targets": [
					{
						"customer_schedule_item": "TARGET-1",
						"schedule_date": "2026-08-20",
						"opening_required_qty": 100,
						"opening_produced_qty": 10,
						"opening_delivered_qty": 20,
						"source_open_qty": 80,
						"accepted_required_qty": 120,
						"accepted_source_open_qty": 100,
						"accepted_schedule_date": "2026-08-22",
					}
				]
			}
		)
		result = frappe._dict(
			name="RESULT-1",
			planned_qty=100,
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			item_code="FG-1",
			requested_date="2026-08-22",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=baseline,
		)
		current = frappe._dict(
			name="TARGET-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			item_code="FG-1",
			schedule_date="2026-08-22",
			qty=120,
			delivered_qty=80,
			item_status="Open",
			schedule_status="Active",
		)
		self.assertEqual(
			consistency._get_customer_schedule_lineage_errors([result], target_rows=[current]),
			{},
		)
		progress = consistency._progress_from_claimed_schedule_targets(
			[result],
			{
				"RESULT-1": [
					frappe._dict(
						accepted_source_open_qty=100,
						attributed_qty=1,
						opening_produced_qty=10,
						produced_qty=50,
						opening_delivered_qty=20,
						delivered_qty=80,
					)
				]
			},
		)
		self.assertEqual(progress["RESULT-1"], {"produced_qty": 40, "delivered_qty": 60})

		current.delivered_qty = 10
		with patch.object(consistency, "_", side_effect=lambda message, **_kwargs: message):
			lineage_errors = consistency._get_customer_schedule_lineage_errors(
				[result], target_rows=[current]
			)
		self.assertTrue(
			any(
				"returned below" in error["message"]
				for error in lineage_errors["RESULT-1"]
			)
		)

	def test_customer_schedule_source_set_and_sales_order_item_are_exact(self):
		baseline = {
			"targets": [
				{
					"customer_schedule": "SCHEDULE-1",
					"customer_schedule_item": "TARGET-1",
					"sales_order": "SO-1",
					"sales_order_item": "SOI-WRONG",
					"item_code": "FG-1",
					"schedule_date": "2026-08-20",
					"opening_required_qty": 10,
					"opening_delivered_qty": 0,
					"source_open_qty": 10,
				}
			]
		}
		result = frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-RIGHT",
			item_code="FG-1",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json=json.dumps(baseline),
			demand_source_snapshot_json=json.dumps(
				[
					{
						"source_doctype": "Customer Delivery Schedule",
						"source_name": "SCHEDULE-1",
						"source_detail_name": "TARGET-1",
						"sales_order": "SO-1",
						"sales_order_item": "SOI-WRONG",
						"qty": 10,
					},
					{
						"source_doctype": "Customer Delivery Schedule",
						"source_name": "SCHEDULE-GHOST",
						"source_detail_name": "TARGET-GHOST",
						"sales_order": "SO-1",
						"sales_order_item": "SOI-RIGHT",
						"qty": 90,
					},
				]
			),
		)
		differences = consistency._AuditDifferenceCollector()
		consistency._get_audit_validated_schedule_targets(result, differences=differences)

		self.assertTrue(
			any(row["fieldname"] == "fulfillment_target_sales_order_item" for row in differences)
		)
		self.assertTrue(
			any(row["source"] == "customer_schedule_source_target_set" for row in differences)
		)

	def test_accepted_epoch_cannot_cross_opening_or_required_quantity_bounds(self):
		def validate(*, accepted_required, accepted_delivered):
			target = {
				"customer_schedule": "SCHEDULE-1",
				"customer_schedule_item": "TARGET-1",
				"sales_order": "SO-1",
				"sales_order_item": "SOI-1",
				"item_code": "FG-1",
				"schedule_date": "2026-08-20",
				"opening_required_qty": 100,
				"opening_delivered_qty": 20,
				"source_open_qty": 80,
				"accepted_required_qty": accepted_required,
				"accepted_delivered_qty": accepted_delivered,
				"accepted_source_open_qty": max(accepted_required - 20, 0),
				"accepted_current_open_qty": max(accepted_required - accepted_delivered, 0),
				"accepted_schedule_date": "2026-08-20",
				"accepted_source_demand_delta": "DELTA-1",
				"accepted_by_change_request": "CHANGE-1",
			}
			result = frappe._dict(
				name="RESULT-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				item_code="FG-1",
				demand_source="Customer Delivery Schedule",
				fulfillment_baseline_json=json.dumps(
					{
						"accepted_source_demand_delta": "DELTA-1",
						"accepted_by_change_request": "CHANGE-1",
						"targets": [target],
					}
				),
				demand_source_snapshot_json=json.dumps(
					[
						{
							"source_doctype": "Customer Delivery Schedule",
							"source_name": "SCHEDULE-1",
							"source_detail_name": "TARGET-1",
							"sales_order": "SO-1",
							"sales_order_item": "SOI-1",
							"qty": max(accepted_required - accepted_delivered, 0),
						}
					]
				),
			)
			differences = consistency._AuditDifferenceCollector()
			consistency._get_audit_validated_schedule_targets(result, differences=differences)
			return differences

		below_opening = validate(accepted_required=100, accepted_delivered=10)
		self.assertTrue(any(row["fieldname"] == "accepted_delivered_qty" for row in below_opening))
		above_required = validate(accepted_required=100, accepted_delivered=120)
		self.assertTrue(any(row["fieldname"] == "accepted_delivered_qty" for row in above_required))

	def test_backlog_delivery_uses_exact_sales_order_item_and_does_not_double_claim(self):
		def result(name, opening, cap):
			return frappe._dict(
				name=name,
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="FG-1",
				sales_order="SO-1",
				sales_order_item="SOI-1",
				fulfillment_baseline_json=json.dumps(
					{
						"sales_order_items": [
							{
								"sales_order": "SO-1",
								"sales_order_item": "SOI-1",
									"item_code": "FG-1",
									"opening_delivered_qty": opening,
									"opening_ordered_qty": opening + cap,
									"source_open_qty": cap,
							}
						]
					}
				),
			)

		differences = consistency._AuditDifferenceCollector()
		claimed = consistency._get_audit_backlog_delivery_by_result(
			[result("RESULT-1", 40, 20), result("RESULT-2", 60, 20)],
			delivery={
				"backlog_by_key": {
					("COMPANY-1", "CUSTOMER-1", "FG-1", "SO-1", "SOI-1"): 70
				}
			},
			differences=differences,
		)
		self.assertEqual(claimed, {"RESULT-1": 20, "RESULT-2": 10})
		self.assertEqual(sum(claimed.values()), 30)
		self.assertEqual(differences.total_count, 0)

		differences = consistency._AuditDifferenceCollector()
		consistency._get_audit_backlog_delivery_by_result(
			[result("RESULT-1", 40, 20)],
			delivery={
				"backlog_by_key": {
					("COMPANY-1", "CUSTOMER-1", "FG-1", "SO-1", "SOI-1"): 70
				}
			},
			differences=differences,
			cross_run_claims={"SOI-1": ["RESULT-OTHER"]},
		)
		self.assertTrue(any(row["fieldname"] == "active_run_owner" for row in differences))

	def test_v4_formula_snapshot_and_minimum_batch_owner_extension_are_auditable(self):
		owner_row = frappe._dict(
			name="DEMAND-1",
			demand_source="Forecast",
			source_doctype="Forecast",
			source_name="FORECAST-1",
			source_detail_name="ROW-1",
			qty=20,
		)
		snapshot_json, baseline_json = planning._build_net_requirement_lineage_snapshot(
			[owner_row],
			demand_qty=20,
			available_stock_qty=0,
			open_work_order_qty=0,
			existing_work_order_policy="Exclude",
			safety_stock_gap_qty=0,
			minimum_batch_qty=100,
			minimum_batch_coverage_qty=0,
			net_requirement_qty=20,
			planning_qty=100,
			new_batch_surplus_qty=80,
			is_safety_stock_group=0,
		)
		baseline = json.loads(baseline_json)
		self.assertEqual(baseline["version"], 4)
		self.assertEqual(baseline["net_requirement"]["base_residual_qty"], 20)
		self.assertEqual(baseline["net_requirement"]["new_batch_surplus_qty"], 80)

		owner = {
			"name": "NR-1",
			"values": {
				"demand_qty": 20,
				"available_stock_qty": 0,
				"open_work_order_qty": 0,
				"existing_work_order_policy": "Exclude",
				"safety_stock_gap_qty": 0,
				"minimum_batch_qty": 100,
				"net_requirement_qty": 20,
				"planning_qty": 100,
				"reason_text": "Minimum batch",
				"demand_source_snapshot_json": snapshot_json,
				"fulfillment_baseline_json": baseline_json,
			},
			"rows": [owner_row],
			"surplus_qty": 30,
			"minimum_batch_coverage_qty": 0,
			"new_batch_surplus_qty": 80,
			"is_safety_stock_group": 0,
			"base_reason_text": "Minimum batch",
		}
		covered_row = frappe._dict(
			name="DEMAND-2",
			demand_source="Forecast",
			source_doctype="Forecast",
			source_name="FORECAST-1",
			source_detail_name="ROW-2",
			qty=50,
		)
		database = MagicMock()
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning, "_", side_effect=lambda message, **_kwargs: message),
		):
			planning._extend_minimum_batch_owner_lineage(owner, [covered_row])
		extended = json.loads(owner["values"]["fulfillment_baseline_json"])["net_requirement"]
		self.assertEqual(extended["demand_qty"], 70)
		self.assertEqual(extended["base_residual_qty"], 70)
		self.assertEqual(extended["minimum_batch_coverage_qty"], 50)
		self.assertEqual(extended["new_batch_surplus_qty"], 80)
		differences = consistency._AuditDifferenceCollector()
		frozen = frappe._dict(
			name="RESULT-1",
			**extended,
			demand_source_snapshot_json=owner["values"]["demand_source_snapshot_json"],
			fulfillment_baseline_json=owner["values"]["fulfillment_baseline_json"],
		)
		consistency._validate_audit_net_requirement(
			frozen,
			run_existing_work_order_policy="Exclude",
			differences=differences,
			doctype="APS Schedule Result",
			require_system_generated=False,
		)
		self.assertEqual(differences.total_count, 0)

	def test_unknown_existing_work_order_policy_fails_closed(self):
		source_snapshot = json.dumps(
			[
				{
					"demand_pool": "DEMAND-1",
					"source_doctype": "Forecast",
					"source_name": "FORECAST-1",
					"source_detail_name": "ROW-1",
					"qty": 10,
				}
			]
		)
		net = frappe._dict(
			name="NR-BAD-POLICY",
			demand_qty=10,
			available_stock_qty=0,
			open_work_order_qty=0,
			existing_work_order_policy="Bogus",
			safety_stock_gap_qty=0,
			minimum_batch_qty=0,
			net_requirement_qty=10,
			planning_qty=10,
			demand_source_snapshot_json=source_snapshot,
			fulfillment_baseline_json=_net_formula_baseline(
				demand_qty=10,
				available_stock_qty=0,
				open_work_order_qty=0,
				existing_work_order_policy="Bogus",
				net_requirement_qty=10,
				planning_qty=10,
			),
		)
		differences = consistency._AuditDifferenceCollector()
		consistency._validate_audit_net_requirement(
			net,
			run_existing_work_order_policy="Bogus",
			differences=differences,
			doctype="APS Net Requirement",
			require_system_generated=False,
		)

		self.assertTrue(
			any(row["fieldname"] == "existing_work_order_policy" for row in differences)
		)

	def test_difference_payload_is_bounded_but_total_count_is_exact(self):
		differences = consistency._AuditDifferenceCollector(limit=3)
		for index in range(10):
			differences.append({"index": index})
		self.assertEqual(differences.total_count, 10)
		self.assertEqual(len(differences), 3)
