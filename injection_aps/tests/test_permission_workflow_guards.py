from __future__ import annotations

import csv
import ast
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import frappe

from injection_aps.api import app
from injection_aps.injection_aps.doctype.aps_work_order_proposal_batch.aps_work_order_proposal_batch import (
	APSWorkOrderProposalBatch,
)
from injection_aps.injection_aps.doctype.aps_work_order_proposal_item.aps_work_order_proposal_item import (
	APSWorkOrderProposalItem,
)
from injection_aps.injection_aps.doctype.aps_shift_schedule_proposal_batch.aps_shift_schedule_proposal_batch import (
	APSShiftScheduleProposalBatch,
)
from injection_aps.injection_aps.doctype.aps_shift_schedule_proposal_item.aps_shift_schedule_proposal_item import (
	APSShiftScheduleProposalItem,
)
from injection_aps.injection_aps.doctype.aps_release_batch.aps_release_batch import APSReleaseBatch
from injection_aps.injection_aps.doctype.aps_schedule_import_batch.aps_schedule_import_batch import (
	APSScheduleImportBatch,
)
from injection_aps.injection_aps.doctype.aps_schedule_result.aps_schedule_result import APSScheduleResult
from injection_aps.injection_aps.doctype.customer_delivery_schedule.customer_delivery_schedule import (
	CustomerDeliverySchedule,
)
from injection_aps.services import change_engine, permissions, planning


APP_ROOT = Path(__file__).resolve().parents[1]


class TestPermissionWorkflowGuards(unittest.TestCase):
	def test_future_demand_hint_is_company_customer_scoped_and_permission_aware(self):
		with patch.object(
			planning.frappe,
			"get_list",
			return_value=[frappe._dict(demand_date="2026-08-20", qty=25, demand_source="Schedule")],
		) as get_list:
			message = planning._get_future_demand_hint(
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_code="ITEM-1",
				demand_date="2026-08-12",
			)

		self.assertIn("25", message)
		filters = get_list.call_args.kwargs["filters"]
		self.assertEqual(filters["company"], "COMPANY-1")
		self.assertEqual(filters["customer"], "CUSTOMER-1")
		self.assertEqual(filters["item_code"], "ITEM-1")

	def test_note_update_uses_controlled_read_scope(self):
		with (
			patch.object(app, "_require_plan_access"),
			patch.object(app, "_require_scoped_document_access") as require_scoped,
			patch.object(app.planning, "update_schedule_notes", return_value={"ok": 1}),
		):
			app.update_schedule_notes(
				result_name="RESULT-1",
				segment_name="SEGMENT-1",
				result_note="result",
				segment_note="segment",
			)

		require_scoped.assert_any_call("APS Schedule Result", "RESULT-1", ptype="read")
		require_scoped.assert_any_call("APS Schedule Segment", "SEGMENT-1", ptype="read")

	def test_controlled_proposal_transitions_require_release_role_and_batch_read_scope(self):
		cases = (
			(app.apply_work_order_proposals, "APS Work Order Proposal Batch", "apply_work_order_proposals", ("BATCH-1",)),
			(app.reject_work_order_proposals, "APS Work Order Proposal Batch", "reject_work_order_proposals", ("BATCH-1", "reason")),
			(app.apply_shift_schedule_proposals, "APS Shift Schedule Proposal Batch", "apply_shift_schedule_proposals", ("BATCH-1",)),
			(app.reject_shift_schedule_proposals, "APS Shift Schedule Proposal Batch", "reject_shift_schedule_proposals", ("BATCH-1", "reason")),
		)
		for endpoint, doctype, service_name, args in cases:
			with self.subTest(endpoint=endpoint.__name__):
				with (
					patch.object(app, "_require_release_access") as require_release,
					patch.object(app, "_require_complete_proposal_batch_scope") as require_batch_scope,
					patch.object(app.planning, service_name, return_value={"ok": 1}),
				):
					endpoint(*args)
					require_release.assert_called_once_with()
					require_batch_scope.assert_called_once_with(doctype, "BATCH-1")

	def test_change_request_apply_uses_run_write_and_engine_managed_target_read_scope(self):
		with (
			patch.object(app, "_require_approve_access"),
			patch.object(app, "_require_change_request_access") as require_change,
			patch.object(app, "_require_change_request_impact_access") as require_impact,
			patch.object(
				app.frappe.db,
				"get_value",
				return_value=frappe._dict(planning_run="RUN-1", target_result="RESULT-1"),
			),
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(app.planning, "apply_change_request", return_value={"status": "Applied"}),
		):
			app.apply_change_request("CHANGE-1")

		require_change.assert_called_once_with(
			"CHANGE-1",
			ptype="write",
			target_ptype="read",
		)
		require_run.assert_called_once_with("RUN-1", run_ptype="write")
		require_impact.assert_called_once_with("CHANGE-1")

	def test_nested_change_impact_fails_closed_when_any_result_is_hidden(self):
		preview = {
			"impact": {
				"affected_orders": [
					{"result_name": "RESULT-ALLOWED", "customer": "CUSTOMER-1"},
					{"result_name": "RESULT-DENIED", "customer": "CUSTOMER-2"},
				]
			},
			"proposal": {
				"segment_actions": [
					{"segment_name": "SEGMENT-ALLOWED", "result_name": "RESULT-ALLOWED"}
				]
			},
		}
		with (
			patch.object(
				app,
				"_has_scoped_document_access",
				side_effect=lambda _doctype, name, **_kwargs: name != "RESULT-DENIED",
			),
			patch.object(app, "_has_linked_document_access", return_value=True),
		):
			self.assertFalse(app._impact_preview_is_accessible(preview))

	def test_new_plan_actions_reject_hidden_item_or_plant_floor_before_service(self):
		with (
			patch.object(app, "_require_plan_access"),
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(app, "_require_scope_access"),
			patch.object(app, "_require_company_rebuild_scope"),
			patch.object(
				app,
				"_require_planning_reference_access",
				side_effect=frappe.PermissionError("hidden reference"),
			),
			patch.object(app.planning, "run_planning_run") as service,
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden reference"):
				app.run_planning_run(
					company="COMPANY-1",
					item_code="ITEM-DENIED",
					plant_floor="FLOOR-DENIED",
				)
		service.assert_not_called()

	def test_run_scope_checks_every_selected_plant_floor(self):
		with (
			patch.object(
				app,
				"_get_document_scope",
				return_value=frappe._dict(
					plant_floor="FLOOR-1",
					selected_plant_floor_summary="FLOOR-1, FLOOR-2",
				),
			),
			patch.object(app, "_require_document_access") as require_document,
		):
			app._require_scope_access(planning_run="RUN-1", ptype="write")

		require_document.assert_any_call("APS Planning Run", "RUN-1", ptype="write")
		require_document.assert_any_call("Plant Floor", "FLOOR-1", ptype="read")
		require_document.assert_any_call("Plant Floor", "FLOOR-2", ptype="read")

	def test_company_wide_rebuild_rejects_partially_visible_sources(self):
		with (
			patch.object(app.frappe, "get_all", return_value=["SCHEDULE-1", "SCHEDULE-2"]),
			patch.object(app.frappe, "get_list", return_value=[{"name": "SCHEDULE-1"}]),
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.PermissionError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.PermissionError, "outside your permitted scope"):
				app._require_all_documents_visible(
					"Customer Delivery Schedule",
					{"company": "COMPANY-1", "status": "Active"},
				)

	def test_company_scoped_mutations_reject_an_empty_company_before_service(self):
		cases = (
			(app.rebuild_demand_pool, (), "rebuild_demand_pool"),
			(app.rebuild_net_requirements, (), "rebuild_net_requirements"),
			(app.repair_item_references, (), "repair_item_references"),
			(app.detach_standard_references, (), "detach_standard_references"),
		)
		for endpoint, args, service_name in cases:
			with self.subTest(endpoint=endpoint.__name__):
				with (
					patch.object(app, "_require_plan_access"),
					patch.object(app, "_require_admin_access"),
					patch.object(
						app.frappe,
						"throw",
						side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
							frappe.ValidationError(message)
						),
					),
					patch.object(app.planning, service_name) as service,
				):
					with self.assertRaisesRegex(frappe.ValidationError, "Company"):
						endpoint(*args, company=None)
				service.assert_not_called()

	def test_rebuild_passes_the_authorized_explicit_company_to_service(self):
		with (
			patch.object(app, "_require_plan_access"),
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(app, "_require_scope_access"),
			patch.object(app, "_require_company_rebuild_scope"),
			patch.object(app.planning, "rebuild_demand_pool", return_value={"created_rows": 0}) as service,
		):
			app.rebuild_demand_pool(company=" COMPANY-1 ")

		service.assert_called_once_with(company="COMPANY-1")

	def test_schedule_revision_and_optional_rebuild_execute_in_one_endpoint_call(self):
		applied = {"schedule": "SCHEDULE-1", "import_batch": "BATCH-1"}
		promotion = {"net_requirement_rows": 3}
		with (
			patch.object(app, "_require_demand_access"),
			patch.object(app, "_require_plan_access") as require_plan,
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(app, "_require_document_access"),
			patch.object(app, "_require_scope_access"),
			patch.object(app, "_require_company_rebuild_scope") as require_rebuild_scope,
			patch.object(app.schedule_revision, "apply_revision", return_value=applied.copy()) as apply_revision,
			patch.object(
				app.planning,
				"promote_schedule_import_to_net_requirement",
				return_value=promotion,
			) as promote,
		):
			result = app.apply_schedule_revision(
				customer="CUSTOMER-1",
				company=" COMPANY-1 ",
				version_no="V2",
				confirmed_revision_mode="Partial Revision",
				rows_json=[{"item_code": "ITEM-1", "qty": 25}],
				rebuild=1,
				existing_work_order_policy="Exclude",
			)

		require_plan.assert_called_once_with()
		require_rebuild_scope.assert_called_once_with("COMPANY-1")
		self.assertEqual(apply_revision.call_args.kwargs["company"], "COMPANY-1")
		promote.assert_called_once_with(
			import_batch="BATCH-1",
			schedule="SCHEDULE-1",
			company="COMPANY-1",
			existing_work_order_policy="Exclude",
		)
		self.assertEqual(result["promotion"], promotion)

	def test_planning_services_also_reject_empty_company_before_any_write(self):
		cases = (
			(lambda: planning.preview_customer_delivery_schedule("CUSTOMER-1", "", "V1")),
			(lambda: planning.import_customer_delivery_schedule("CUSTOMER-1", "", "V1")),
			(lambda: planning.rebuild_demand_pool()),
			(lambda: planning.rebuild_net_requirements(existing_work_order_policy="Exclude")),
			(lambda: planning.repair_item_references()),
			(lambda: planning.detach_standard_references(None)),
		)
		for invoke in cases:
			with self.subTest(invoke=invoke):
				with patch.object(
					planning.frappe,
					"throw",
					side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
						frappe.ValidationError(message)
					),
				):
					with self.assertRaisesRegex(frappe.ValidationError, "Company"):
						invoke()

	def test_standard_reference_cleanup_query_is_company_scoped(self):
		meta = MagicMock()
		meta.has_field.side_effect = lambda fieldname: fieldname in {"company", "custom_aps_run"}
		meta.get_field.return_value = frappe._dict(fieldtype="Data")
		database = MagicMock()
		database.sql.return_value = []
		with (
			patch.object(planning.frappe, "get_meta", return_value=meta),
			patch.object(planning.frappe, "db", database),
		):
			planning._get_records_with_any_field_set(
				"Work Order",
				["custom_aps_run"],
				company="COMPANY-1",
			)

		query, params = database.sql.call_args.args[:2]
		self.assertIn("source.company = %s", query)
		self.assertEqual(params, ("COMPANY-1",))

	def test_complete_run_scope_traverses_every_result(self):
		with (
			patch.object(app, "_require_scoped_document_access") as require_scoped,
			patch.object(app, "_require_all_documents_visible") as require_visible,
			patch.object(app.frappe, "get_all", return_value=["RESULT-2", "RESULT-1"]),
		):
			app._require_complete_run_mutation_scope("RUN-1", run_ptype="write")

		require_visible.assert_called_once_with("APS Schedule Result", {"planning_run": "RUN-1"})
		self.assertEqual(
			require_scoped.call_args_list,
			[
				call("APS Planning Run", "RUN-1", ptype="write", linked_run_ptype="write"),
				call("APS Schedule Result", "RESULT-1", ptype="read", linked_run_ptype="read"),
				call("APS Schedule Result", "RESULT-2", ptype="read", linked_run_ptype="read"),
			],
		)

	def test_proposal_scope_checks_result_customer_sales_order_and_item_child(self):
		row = frappe._dict(
			name="ROW-1",
			result_reference="RESULT-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			existing_work_order="WO-1",
		)
		with (
			patch.object(app, "_require_scoped_document_access") as require_scoped,
			patch.object(
				app,
				"_get_document_scope",
				return_value=frappe._dict(company="COMPANY-1", planning_run="RUN-1"),
			),
			patch.object(app.frappe.db, "get_value", return_value="COMPANY-1"),
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(app.frappe, "get_all", return_value=[row]),
			patch.object(app, "_require_document_access") as require_document,
			patch.object(app, "_require_sales_order_item_access") as require_so_item,
		):
			app._require_complete_proposal_batch_scope(
				"APS Work Order Proposal Batch",
				"BATCH-1",
			)

		require_run.assert_called_once_with("RUN-1", run_ptype="write")
		require_scoped.assert_any_call(
			"APS Schedule Result", "RESULT-1", ptype="read", linked_run_ptype="read"
		)
		require_document.assert_any_call("Customer", "CUSTOMER-1", ptype="read")
		require_document.assert_any_call("Item", "ITEM-1", ptype="read")
		require_document.assert_any_call("Sales Order", "SO-1", ptype="read")
		require_document.assert_any_call("Work Order", "WO-1", ptype="read")
		require_so_item.assert_called_once_with(
			"SOI-1",
			sales_order="SO-1",
			item_code="ITEM-1",
		)

	def test_proposal_scope_rejects_a_result_from_another_run(self):
		row = frappe._dict(name="ROW-1", result_reference="RESULT-OTHER")
		with (
			patch.object(app, "_require_scoped_document_access"),
			patch.object(
				app,
				"_get_document_scope",
				side_effect=[
					frappe._dict(company="COMPANY-1", planning_run="RUN-1"),
					frappe._dict(company="COMPANY-1", planning_run="RUN-OTHER"),
				],
			),
			patch.object(app.frappe.db, "get_value", return_value="COMPANY-1"),
			patch.object(app, "_require_complete_run_mutation_scope"),
			patch.object(app.frappe, "get_all", return_value=[row]),
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "no longer belongs"):
				app._require_complete_proposal_batch_scope(
					"APS Work Order Proposal Batch",
					"BATCH-1",
				)

	def test_import_scope_checks_every_item_order_and_matching_order_item(self):
		preview = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"source_rows": [{"item_code": "ITEM-1", "sales_order": "SO-1"}],
			"effective_schedule_rows": [],
		}
		with (
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(app, "_require_document_access") as require_document,
			patch.object(
				app.frappe,
				"get_all",
				return_value=[frappe._dict(name="SOI-1", parent="SO-1", item_code="ITEM-1")],
			),
			patch.object(app, "_require_sales_order_item_access") as require_so_item,
		):
			app._require_schedule_import_reference_access(
				preview,
				customer="CUSTOMER-1",
				company="COMPANY-1",
			)

		require_document.assert_any_call("Customer", "CUSTOMER-1", ptype="read")
		require_document.assert_any_call("Item", "ITEM-1", ptype="read")
		require_document.assert_any_call("Sales Order", "SO-1", ptype="read")
		require_so_item.assert_called_once_with(
			"SOI-1",
			sales_order="SO-1",
			item_code="ITEM-1",
		)

	def test_import_scope_fails_closed_before_importing_a_hidden_item(self):
		preview = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"source_rows": [{"item_code": "ITEM-DENIED"}],
		}
		with (
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(
				app,
				"_require_document_access",
				side_effect=lambda doctype, name, **_kwargs: (
					(_ for _ in ()).throw(frappe.PermissionError("hidden item"))
					if (doctype, name) == ("Item", "ITEM-DENIED")
					else None
				),
			),
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden item"):
				app._require_schedule_import_reference_access(
					preview,
					customer="CUSTOMER-1",
					company="COMPANY-1",
				)

	def test_import_rechecks_reference_permission_inside_customer_lock(self):
		preview = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"active_state_token": "STATE-1",
			"import_fingerprint": "FINGERPRINT-1",
			"is_idempotent_replay": 0,
			"can_import": 1,
			"schedule_scope": "DAILY",
			"import_strategy": "Replace Version",
			"version_no": "V1",
			"duplicate_policy": "Block",
		}
		database = MagicMock()
		database.sql.return_value = [("CUSTOMER-1",)]
		validator = MagicMock()
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="import1234"),
			patch.object(planning, "preview_customer_delivery_schedule", return_value=preview),
			patch.object(
				planning,
				"_apply_customer_delivery_schedule_import",
				return_value={"schedule": "SCHEDULE-1"},
			),
		):
			result = planning.import_customer_delivery_schedule(
				customer="CUSTOMER-1",
				company="COMPANY-1",
				version_no="V1",
				active_state_token="STATE-1",
				expected_import_fingerprint="FINGERPRINT-1",
				reference_access_validator=validator,
			)

		self.assertEqual(result["schedule"], "SCHEDULE-1")
		validator.assert_called_once_with(preview)
		database.sql.assert_called_once()
		self.assertIn("for update", database.sql.call_args.args[0].lower())

	def test_locked_import_permission_failure_rolls_back_before_schedule_write(self):
		preview = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"active_state_token": "STATE-1",
			"import_fingerprint": "FINGERPRINT-1",
			"can_import": 1,
		}
		database = MagicMock()
		database.sql.return_value = [("CUSTOMER-1",)]
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="importdeny"),
			patch.object(planning, "preview_customer_delivery_schedule", return_value=preview),
			patch.object(planning, "_apply_customer_delivery_schedule_import") as apply_import,
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden reference"):
				planning.import_customer_delivery_schedule(
					customer="CUSTOMER-1",
					company="COMPANY-1",
					version_no="V1",
					active_state_token="STATE-1",
					expected_import_fingerprint="FINGERPRINT-1",
					reference_access_validator=MagicMock(
						side_effect=frappe.PermissionError("hidden reference")
					),
				)

		apply_import.assert_not_called()
		database.rollback.assert_called_once_with(save_point="aps_schedule_import_importdeny")

	def test_result_detail_hides_unreadable_linked_source_documents(self):
		detail = {
			"result": {
				"name": "RESULT-1",
				"planned_qty": 10,
				"execution_source_documents": "STOCK-ALLOWED\nSTOCK-DENIED",
				"segments": [{"linked_work_order": "WO-DENIED"}],
				"demand_source_snapshot_json": '{"source_name":"SO-DENIED"}',
				"fulfillment_baseline_json": '{"target":"SCHEDULE-DENIED"}',
				"capacity_balance_details": '{"warehouse":"WAREHOUSE-DENIED"}',
				"selected_moulds": "MOLD-DENIED",
				"primary_mould_reference": "MOLD-DENIED",
			},
			"segments": [
				{
					"name": "SEG-1",
					"linked_work_order": "WO-DENIED",
					"work_order_route": "Form/Work Order/WO-DENIED",
					"latest_stock_entry": "STOCK-ALLOWED",
					"latest_stock_entry_route": "Form/Stock Entry/STOCK-ALLOWED",
					"execution_source_documents": "STOCK-ALLOWED\nSTOCK-DENIED",
				}
			],
			"source_rows": [
				{"name": "DP-ALLOWED", "source_doctype": "Sales Order", "source_name": "SO-ALLOWED"},
				{"name": "DP-DENIED", "source_doctype": "Sales Order", "source_name": "SO-DENIED"},
			],
			"exception_rows": [],
			"mold_rows": [{"mold": "MOLD-DENIED"}],
			"production_allocations": [
				{"source_stock_entry": "STOCK-ALLOWED", "scheduling_item": ""},
				{"source_stock_entry": "STOCK-DENIED", "scheduling_item": ""},
			],
			"delivery_allocations": [
				{"source_delivery_note": "DN-ALLOWED"},
				{"source_delivery_note": "DN-DENIED"},
			],
			"fulfillment_projection": {
				"production_source_documents": ["STOCK-ALLOWED", "STOCK-DENIED"],
				"delivery_source_documents": ["DN-ALLOWED", "DN-DENIED"],
			},
			"next_actions": {},
		}

		def allowed(_doctype, name, **_kwargs):
			return not name or not str(name).endswith("DENIED")

		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scoped_document_access", side_effect=allowed),
			patch.object(app, "_has_scoped_document_access", side_effect=allowed),
			patch.object(app, "_has_linked_document_access", side_effect=allowed),
			patch.object(app, "_has_document_access", side_effect=allowed),
			patch.object(app.planning, "get_schedule_result_detail", return_value=detail),
		):
			result = app.get_schedule_result_detail("RESULT-1")

		self.assertEqual(result["result"]["execution_source_documents"], "STOCK-ALLOWED")
		for hidden_field in (
			"segments",
			"demand_source_snapshot_json",
			"fulfillment_baseline_json",
			"capacity_balance_details",
			"selected_moulds",
			"primary_mould_reference",
		):
			self.assertNotIn(hidden_field, result["result"])
		self.assertEqual(result["segments"][0]["linked_work_order"], "")
		self.assertEqual(result["segments"][0]["latest_stock_entry"], "STOCK-ALLOWED")
		self.assertEqual([row["name"] for row in result["source_rows"]], ["DP-ALLOWED"])
		self.assertEqual(result["mold_rows"], [])
		self.assertEqual(len(result["production_allocations"]), 1)
		self.assertEqual(len(result["delivery_allocations"]), 1)
		self.assertEqual(
			result["fulfillment_projection"]["production_source_documents"],
			["STOCK-ALLOWED"],
		)
		self.assertEqual(
			result["fulfillment_projection"]["delivery_source_documents"],
			["DN-ALLOWED"],
		)

	def test_change_impact_preview_excludes_hidden_cascading_results(self):
		source = {
			"name": "CR-1",
			"impact_json": json.dumps(
				{
					"affected_orders": [
						{
							"affected_order": "RESULT-ALLOWED",
							"result_name": "RESULT-ALLOWED",
							"customer": "CUSTOMER-ALLOWED",
							"item_code": "ITEM-1",
							"delay_minutes": 20,
						},
						{
							"affected_order": "RESULT-DENIED",
							"result_name": "RESULT-DENIED",
							"customer": "CUSTOMER-DENIED",
							"item_code": "ITEM-1",
							"delay_minutes": 30,
						},
					],
					"additional_mold_changes": 3,
				}
			),
			"proposal_json": json.dumps(
				{
					"segment_actions": [
						{"segment_name": "SEG-ALLOWED"},
						{"segment_name": "SEG-DENIED"},
					]
				}
			),
		}

		def scoped(_doctype, name, **_kwargs):
			return not str(name or "").endswith("DENIED")

		with (
			patch.object(app, "_has_scoped_document_access", side_effect=scoped),
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda _doctype, name, **_kwargs: not str(name or "").endswith("DENIED"),
			),
		):
			row = app._compact_change_impact_row(source)

		self.assertEqual(row["affected_order_count"], 1)
		self.assertEqual(row["affected_customers"], ["CUSTOMER-ALLOWED"])
		self.assertEqual(row["cascading_delay_count"], 1)
		self.assertEqual(row["segment_action_count"], 1)
		self.assertEqual(row["additional_mold_changes"], 0)

	def test_exception_detail_blocks_an_unreadable_standard_source(self):
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scoped_document_access"),
			patch.object(
				app.frappe.db,
				"get_value",
				return_value={"source_doctype": "Work Order", "source_name": "WO-DENIED"},
			),
			patch.object(app, "_has_linked_document_access", return_value=False),
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.PermissionError(message)
				),
			),
			patch.object(app.planning, "get_exception_resolution_context") as service,
		):
			with self.assertRaisesRegex(frappe.PermissionError, "exception source"):
				app.get_exception_resolution_context("EXC-1")
		service.assert_not_called()

	def test_targeted_run_pages_reject_record_permission_before_service_reads(self):
		for endpoint, service_patch in (
			(app.get_schedule_gantt_data, "get_settings_dict"),
			(app.get_release_center_data, "get_recent_run_contexts"),
		):
			with self.subTest(endpoint=endpoint.__name__):
				with (
					patch.object(app, "_require_read_access"),
					patch.object(
						app,
						"_require_scoped_document_access",
						side_effect=frappe.PermissionError("denied"),
					),
					patch.object(app.planning, service_patch) as service,
				):
					with self.assertRaises(frappe.PermissionError):
						endpoint("RUN-DENIED")
					service.assert_not_called()

	def test_net_requirement_page_filters_denied_records_and_recalculates_summary(self):
		rows = [
			frappe._dict(name="NR-ALLOWED", net_requirement_qty=7, planning_qty=9),
			frappe._dict(name="NR-DENIED", net_requirement_qty=100, planning_qty=120),
		]
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scope_access"),
			patch.object(app.frappe, "get_list", return_value=rows) as get_list,
			patch.object(
				app,
				"_has_scoped_document_access",
				side_effect=lambda _doctype, name, **_kwargs: name == "NR-ALLOWED",
			),
		):
			result = app.get_net_requirement_page_data(company="COMPANY-1")

		self.assertEqual([row.name for row in result["rows"]], ["NR-ALLOWED"])
		self.assertEqual(result["summary"], {"rows": 1, "net_requirement_qty": 7, "planning_qty": 9})
		get_list.assert_called_once()

	def test_net_requirement_update_invalidates_linked_run_and_consistency(self):
		database = MagicMock()
		document = MagicMock()
		document.name = "NR-1"
		with (
			patch.object(app, "_require_plan_access"),
			patch.object(app, "_require_scoped_document_access") as require_scoped,
			patch.object(app, "_require_complete_run_mutation_scope") as require_run_scope,
			patch.object(
				app.frappe,
				"get_all",
				return_value=[frappe._dict(name="RESULT-1", planning_run="RUN-1")],
			),
			patch.object(app.frappe, "get_doc", return_value=document),
			patch.object(app, "now_datetime", return_value="2026-08-11 08:00:00"),
			patch.object(app.frappe, "db", database),
			patch.object(app.capacity_balance, "invalidate_capacity_balance") as invalidate,
		):
			result = app.update_net_requirement_row("NR-1", {"planning_qty": 12})

		self.assertEqual(result, {"name": "NR-1"})
		require_scoped.assert_any_call("APS Net Requirement", "NR-1", ptype="read")
		require_scoped.assert_any_call("APS Schedule Result", "RESULT-1", ptype="read")
		require_run_scope.assert_called_once_with("RUN-1", run_ptype="write")
		invalidate.assert_called_once_with("RUN-1")
		self.assertEqual(database.set_value.call_args.args[:2], ("APS Planning Run", "RUN-1"))
		self.assertEqual(database.set_value.call_args.args[2]["consistency_status"], "Unchecked")

	def test_net_requirement_delete_is_blocked_when_results_exist(self):
		with (
			patch.object(app, "_require_plan_access"),
			patch.object(app, "_require_scoped_document_access"),
			patch.object(
				app.frappe,
				"get_all",
				return_value=[frappe._dict(name="RESULT-1", planning_run="RUN-1")],
			),
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
			patch.object(app.frappe, "delete_doc") as delete_doc,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "already linked"):
				app.delete_net_requirement_row("NR-1")
		delete_doc.assert_not_called()

	def test_context_endpoint_rejects_arbitrary_doctype(self):
		with (
			patch.object(app, "_require_read_access"),
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
			patch.object(app.planning, "get_next_actions_for_context") as service,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "Unsupported APS context"):
				app.get_next_actions_for_context("User", "Administrator")
		service.assert_not_called()

	def test_active_schedule_controller_rejects_direct_activation(self):
		document = MagicMock()
		document.flags = frappe._dict()
		document.is_new.return_value = True
		document.status = "Active"
		with (
			patch(
				"injection_aps.injection_aps.doctype.customer_delivery_schedule.customer_delivery_schedule._",
				side_effect=lambda message, **_kwargs: message,
			),
			patch(
				"injection_aps.injection_aps.doctype.customer_delivery_schedule.customer_delivery_schedule.frappe.throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.PermissionError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.PermissionError, "only be created through"):
				CustomerDeliverySchedule._protect_import_managed_schedule(document)

	def test_proposal_controllers_reject_all_ordinary_child_or_status_writes(self):
		for module_path, controller in (
			(
				"injection_aps.injection_aps.doctype.aps_work_order_proposal_batch.aps_work_order_proposal_batch",
				APSWorkOrderProposalBatch,
			),
			(
				"injection_aps.injection_aps.doctype.aps_shift_schedule_proposal_batch.aps_shift_schedule_proposal_batch",
				APSShiftScheduleProposalBatch,
			),
		):
			with self.subTest(controller=controller.__name__):
				batch = object.__new__(controller)
				batch.flags = frappe._dict()
				with (
					patch(f"{module_path}._", side_effect=lambda message, **_kwargs: message),
					patch(
						f"{module_path}.frappe.throw",
						side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
							frappe.PermissionError(message)
						),
					),
				):
					with self.assertRaisesRegex(frappe.PermissionError, "APS review and release actions"):
						controller._protect_system_review_statuses(batch)
					with self.assertRaisesRegex(frappe.PermissionError, "APS review and release actions"):
						controller.on_trash(batch)

	def test_proposal_controllers_allow_only_private_engine_transition(self):
		for controller in (APSWorkOrderProposalBatch, APSShiftScheduleProposalBatch):
			with self.subTest(controller=controller.__name__):
				batch = MagicMock()
				batch.flags = frappe._dict(proposal_engine_transition=True)
				controller._protect_system_review_statuses(batch)

	def test_change_apply_locks_customer_run_results_segments_then_request(self):
		database = MagicMock()
		database.get_value.side_effect = [
			frappe._dict(
				planning_run="RUN-1",
				target_result="RESULT-1",
				customer="CUSTOMER-1",
				source_demand_delta="DELTA-1",
				change_type="Increase Qty",
			),
		]
		database.sql.side_effect = [
			[("CUSTOMER-1",)],
			[frappe._dict(name="RUN-1", company="COMPANY-1")],
			[],
			[
				frappe._dict(
					name="RESULT-1",
					planning_run="RUN-1",
					net_requirement="NET-1",
					sales_order="SO-1",
					item_code="ITEM-1",
					fulfillment_baseline_json='{"targets": [{"customer_schedule": "SCHEDULE-1"}]}',
				),
				frappe._dict(name="RESULT-2", planning_run="RUN-1"),
			],
			[frappe._dict(name="NET-1")],
			[frappe._dict(name="SEG-1", parent="RESULT-1")],
			[
				frappe._dict(
					name="DELTA-1",
					schedule_reference="SCHEDULE-1",
					sales_order="SO-1",
					item_code="ITEM-1",
				)
			],
			[frappe._dict(name="SCHEDULE-1")],
			[frappe._dict(name="ROW-1", parent="SCHEDULE-1", idx=1)],
			[frappe._dict(name="SOI-1", parent="SO-1", item_code="ITEM-1", idx=1)],
			[],
			[frappe._dict(name="DN-1")],
			[frappe._dict(name="DNI-1", parent="DN-1", item_code="ITEM-1")],
			[],
			[frappe._dict(name="WO-1", custom_aps_run="RUN-1")],
			[frappe._dict(name="STE-1", purpose="Manufacture", work_order="WO-1")],
			[frappe._dict(name="SED-1", parent="STE-1", item_code="ITEM-1")],
			[],
			[
				frappe._dict(
					name="CR-1",
					planning_run="RUN-1",
					target_result="RESULT-1",
					customer="CUSTOMER-1",
					source_demand_delta="DELTA-1",
					change_type="Increase Qty",
				)
			],
		]
		document = frappe._dict(
			name="CR-1",
			planning_run="RUN-1",
			target_result="RESULT-1",
			customer="CUSTOMER-1",
			source_demand_delta="DELTA-1",
			change_type="Increase Qty",
			flags=frappe._dict(),
		)
		with (
			patch.object(change_engine.frappe, "db", database),
			patch.object(change_engine.frappe, "get_doc", return_value=document),
		):
			locked = change_engine._get_application_scope_locked_change_request("CR-1")

		self.assertIs(locked, document)
		tables = [call.args[0].split("`")[1] for call in database.sql.call_args_list]
		self.assertEqual(
			tables,
			[
				"tabCustomer",
				"tabAPS Planning Run",
				"tabAPS Planning Run Plant Floor",
				"tabAPS Schedule Result",
				"tabAPS Net Requirement",
				"tabAPS Schedule Segment",
				"tabAPS Demand Delta",
				"tabCustomer Delivery Schedule",
				"tabCustomer Delivery Schedule Item",
				"tabSales Order Item",
				"tabAPS Delivery Allocation",
				"tabDelivery Note",
				"tabDelivery Note Item",
				"tabAPS Production Allocation",
				"tabWork Order",
				"tabStock Entry",
				"tabStock Entry Detail",
				"tabAPS Downtime Window",
				"tabAPS Change Request",
			],
		)
		for call in database.sql.call_args_list:
			self.assertIn("for update", call.args[0].lower())

	def test_schedule_impact_mutation_locks_run_results_and_segments(self):
		database = MagicMock()
		with patch.object(app.frappe, "db", database):
			app._lock_planning_run_scope("RUN-1")

		tables = [call.args[0].split("`")[1] for call in database.sql.call_args_list]
		self.assertEqual(
			tables,
			["tabAPS Planning Run", "tabAPS Schedule Result", "tabAPS Schedule Segment"],
		)
		for call in database.sql.call_args_list:
			self.assertIn("for update", call.args[0].lower())

	def test_manual_adjustment_uses_run_write_and_engine_managed_segment_read_scope(self):
		database = MagicMock()
		database.get_value.side_effect = ["RESULT-1", "RUN-1"]
		with (
			patch.object(app, "_require_release_access"),
			patch.object(app, "_require_scoped_document_access") as require_scoped,
			patch.object(app, "_lock_planning_run_scope"),
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(app, "_require_impact_preview_access"),
			patch.object(app.frappe, "db", database),
			patch.object(app.planning, "preview_manual_schedule_adjustment", return_value={}),
			patch.object(
				app.planning,
				"apply_manual_schedule_adjustment",
				return_value={"segment": "SEG-1"},
			),
		):
			app.apply_manual_schedule_adjustment("SEG-1")

		require_run.assert_called_once_with("RUN-1", run_ptype="write")
		self.assertEqual(
			require_scoped.call_args_list,
			[
				call("APS Schedule Segment", "SEG-1", ptype="read"),
				call("APS Schedule Segment", "SEG-1", ptype="read"),
			],
		)

	def test_schedule_impact_uses_run_write_and_readable_impact_rows(self):
		preview = {"impact": {"segment_name": "SEG-1"}}
		with (
			patch.object(app, "_require_release_access"),
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(app, "_lock_planning_run_scope"),
			patch.object(app, "_require_impact_preview_access") as require_impact,
			patch.object(app.planning, "preview_schedule_impact", return_value=preview),
			patch.object(app.planning, "apply_schedule_impact", return_value={"ok": 1}),
		):
			app.apply_schedule_impact("RUN-1")

		self.assertEqual(require_run.call_count, 2)
		for mutation_call in require_run.call_args_list:
			self.assertEqual(mutation_call, call("RUN-1", run_ptype="write"))
		require_impact.assert_called_once_with(preview)

	def test_segment_split_locks_run_result_segment_then_revalidates_scope(self):
		database = MagicMock()
		database.get_value.side_effect = ["RESULT-1", "RUN-1", "RESULT-1", "RUN-1"]
		with (
			patch.object(app, "_require_release_access"),
			patch.object(app, "_require_scoped_document_access") as require_scoped,
			patch.object(app, "_lock_planning_run_scope") as lock_scope,
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(app.frappe, "db", database),
			patch.object(app.planning, "apply_segment_split", return_value={"segment": "SEG-1"}) as service,
		):
			result = app.apply_segment_split("SEG-1", split_qty=4)

		self.assertEqual(result, {"segment": "SEG-1"})
		lock_scope.assert_called_once_with("RUN-1")
		require_run.assert_called_once_with("RUN-1", run_ptype="write")
		self.assertEqual(
			require_scoped.call_args_list,
			[
				call("APS Schedule Segment", "SEG-1", ptype="read"),
				call("APS Schedule Segment", "SEG-1", ptype="read"),
			],
		)
		service.assert_called_once()

	def test_segment_split_blocks_if_lineage_changes_while_locking(self):
		database = MagicMock()
		database.get_value.side_effect = ["RESULT-1", "RUN-1", "RESULT-2", "RUN-2"]
		with (
			patch.object(app, "_require_release_access"),
			patch.object(app, "_require_scoped_document_access"),
			patch.object(app, "_lock_planning_run_scope"),
			patch.object(app.frappe, "db", database),
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
			patch.object(app.planning, "apply_segment_split") as service,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "scope changed"):
				app.apply_segment_split("SEG-1", split_qty=4)

		service.assert_not_called()

	def test_downtime_update_checks_every_run_it_will_invalidate(self):
		with (
			patch.object(app, "_require_release_access"),
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(app.frappe.db, "get_value", return_value="COMPANY-1"),
			patch.object(app.frappe, "get_all", return_value=["RUN-2"]),
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(
				app.planning,
				"create_or_update_downtime_window",
				return_value={"downtime_window": "DOWN-1"},
			) as service,
		):
			result = app.create_or_update_downtime_window(
				company="COMPANY-1",
				planning_run="RUN-1",
				start_time="2026-08-12 08:00:00",
				end_time="2026-08-12 12:00:00",
			)

		self.assertEqual(result, {"downtime_window": "DOWN-1"})
		self.assertEqual(
			require_run.call_args_list,
			[
				call("RUN-1", run_ptype="write"),
				call("RUN-2", run_ptype="write"),
			],
		)
		self.assertEqual(service.call_args.kwargs["company"], "COMPANY-1")

	def test_downtime_update_authorizes_union_of_old_and_new_window_scopes(self):
		current = frappe._dict(
			company="COMPANY-OLD",
			planning_run=None,
			start_time="2026-08-12 08:00:00",
			end_time="2026-08-12 10:00:00",
		)
		with (
			patch.object(app, "_require_release_access"),
			patch.object(app, "_require_scoped_document_access"),
			patch.object(app, "_require_explicit_company", return_value="COMPANY-NEW"),
			patch.object(app.frappe.db, "get_value", return_value=current),
			patch.object(
				app.frappe,
				"get_all",
				side_effect=[
					["RUN-OLD", "RUN-BOTH"],
					["RUN-NEW", "RUN-BOTH"],
				],
			) as get_all,
			patch.object(app, "_require_complete_run_mutation_scope") as require_run,
			patch.object(
				app.planning,
				"create_or_update_downtime_window",
				return_value={"downtime_window": "DOWN-1"},
			) as service,
		):
			result = app.create_or_update_downtime_window(
				name="DOWN-1",
				company="COMPANY-NEW",
				start_time="2026-08-13 12:00:00",
				end_time="2026-08-13 14:00:00",
			)

		self.assertEqual(result, {"downtime_window": "DOWN-1"})
		self.assertEqual(
			require_run.call_args_list,
			[
				call("RUN-BOTH", run_ptype="write"),
				call("RUN-NEW", run_ptype="write"),
				call("RUN-OLD", run_ptype="write"),
			],
		)
		self.assertEqual(get_all.call_count, 2)
		self.assertEqual(get_all.call_args_list[0].kwargs["filters"]["company"], "COMPANY-OLD")
		self.assertEqual(get_all.call_args_list[1].kwargs["filters"]["company"], "COMPANY-NEW")
		service.assert_called_once()

	def test_downtime_service_invalidates_union_of_old_and_new_window_scopes(self):
		doc = frappe._dict(
			name="DOWN-1",
			company="COMPANY-OLD",
			scope="Plant Floor",
			plant_floor="FLOOR-1",
			workstation=None,
			start_time="2026-08-12 08:00:00",
			end_time="2026-08-12 10:00:00",
			available_capacity_percent=0,
			reason="old",
			status="Active",
			planning_run=None,
			notes=None,
		)
		doc.is_new = MagicMock(return_value=False)
		doc.save = MagicMock()
		with (
			patch.object(planning.frappe.db, "exists", return_value=True),
			patch.object(planning.frappe, "get_doc", return_value=doc),
			patch.object(
				planning.frappe,
				"get_all",
				side_effect=[
					["RUN-OLD", "RUN-BOTH"],
					["RUN-NEW", "RUN-BOTH"],
				],
			) as get_all,
			patch(
				"injection_aps.services.capacity_balance.invalidate_capacity_balance"
			) as invalidate,
		):
			result = planning.create_or_update_downtime_window(
				name="DOWN-1",
				company="COMPANY-NEW",
				start_time="2026-08-13 12:00:00",
				end_time="2026-08-13 14:00:00",
			)

		self.assertEqual(result["downtime_window"], "DOWN-1")
		doc.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(get_all.call_count, 2)
		self.assertEqual(get_all.call_args_list[0].kwargs["filters"]["company"], "COMPANY-OLD")
		self.assertEqual(get_all.call_args_list[1].kwargs["filters"]["company"], "COMPANY-NEW")
		self.assertTrue(all(entry.kwargs["limit_page_length"] == 0 for entry in get_all.call_args_list))
		self.assertEqual(
			invalidate.call_args_list,
			[call("RUN-BOTH"), call("RUN-NEW"), call("RUN-OLD")],
		)

	def test_proposal_review_uses_server_transition_and_atomic_boundary(self):
		database = MagicMock()
		database.sql.return_value = [("BATCH-1",)]
		row = frappe._dict(name="ROW-1", review_status="Pending", review_note="")
		batch = MagicMock()
		batch.name = "BATCH-1"
		batch.status = "Reviewed"
		batch.approval_state = "Approved"
		batch.flags = frappe._dict()
		batch.get.side_effect = lambda key: {
			"company": "COMPANY-1",
			"planning_run": "RUN-1",
			"status": "Ready For Review",
			"items": [row],
		}.get(key)
		with (
			patch.object(app.frappe, "db", database),
			patch.object(app.frappe, "generate_hash", return_value="review1234"),
			patch.object(app.frappe, "get_doc", return_value=batch),
			patch.object(app, "_require_complete_proposal_batch_scope") as require_batch_scope,
			patch.object(app, "_require_scope_access") as require_scope,
		):
			result = app._review_proposal_rows(
				batch_doctype="APS Work Order Proposal Batch",
				batch_name="BATCH-1",
				review_status="Approved",
			)

		self.assertEqual(
			require_batch_scope.call_args_list,
			[
				call("APS Work Order Proposal Batch", "BATCH-1"),
				call("APS Work Order Proposal Batch", "BATCH-1"),
			],
		)
		require_scope.assert_called_once_with(company="COMPANY-1", planning_run="RUN-1")
		self.assertEqual(row.review_status, "Approved")
		self.assertTrue(batch.flags.proposal_engine_transition)
		batch.save.assert_called_once_with(ignore_permissions=True)
		database.savepoint.assert_called_once_with("aps_proposal_review_review1234")
		database.release_savepoint.assert_called_once_with("aps_proposal_review_review1234")
		database.rollback.assert_not_called()
		self.assertEqual(result["reviewed_rows"], 1)

	def test_customer_progress_filters_rows_by_customer_and_schedule_permission(self):
		response = {
			"selected_run": None,
			"filters": {"company": "COMPANY-1"},
			"rows": [
				{
					"company": "COMPANY-1",
					"customer": "CUSTOMER-ALLOWED",
					"schedule": "SCHEDULE-1",
					"required_qty": 10,
					"delivered_qty": 0,
					"uncovered_qty": 10,
					"status": "At Risk",
					"result_names": ["RESULT-DENIED"],
					"production_source_documents": ["STOCK-DENIED"],
					"delivery_source_documents": ["DELIVERY-DENIED"],
				},
				{
					"company": "COMPANY-1",
					"customer": "CUSTOMER-DENIED",
					"schedule": "SCHEDULE-2",
					"required_qty": 20,
					"delivered_qty": 0,
					"uncovered_qty": 20,
					"status": "Delayed",
				},
			],
			"truncated": False,
		}
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scope_access"),
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda doctype, name, ptype="read": name
				not in {"CUSTOMER-DENIED", "RESULT-DENIED", "STOCK-DENIED", "DELIVERY-DENIED"},
			),
			patch.object(app.planning, "get_customer_schedule_progress_data", return_value=response),
		):
			result = app.get_customer_schedule_progress_data(company="COMPANY-1")

		self.assertEqual([row["customer"] for row in result["rows"]], ["CUSTOMER-ALLOWED"])
		self.assertEqual(result["summary"]["rows"], 1)
		self.assertEqual(result["rows"][0]["result_names"], [])
		self.assertEqual(result["rows"][0]["production_source_documents"], [])
		self.assertEqual(result["rows"][0]["delivery_source_documents"], [])

	def test_customer_progress_v2_filters_rows_lineage_and_owner_documents(self):
		response = {
			"mode": "V2",
			"projection": {"run_names": ["RUN-ALLOWED", "RUN-DENIED"]},
			"pagination": {"total_rows": 2, "returned_rows": 2, "has_more": False},
			"rows": [
				{
					"company": "COMPANY-1", "customer": "CUSTOMER-ALLOWED",
					"schedule": "SCHEDULE-1", "schedule_item": "ITEM-ROW-1",
					"item_code": "ITEM-1", "demand_identity": "IDENTITY-1",
					"schedule_qty": 10, "status": "On Track", "conservation_status": "OK",
					"commitment_names": ["COMMITMENT-ALLOWED", "COMMITMENT-DENIED"],
					"result_names": ["RESULT-ALLOWED", "RESULT-DENIED"],
					"run_names": ["RUN-ALLOWED", "RUN-DENIED"],
					"source_documents": [
						{"doctype": "APS Schedule Result", "name": "RESULT-ALLOWED"},
						{"doctype": "APS Schedule Result", "name": "RESULT-DENIED"},
					],
					"events": [],
				},
				{
					"company": "COMPANY-1", "customer": "CUSTOMER-DENIED",
					"schedule": "SCHEDULE-2", "schedule_item": "ITEM-ROW-2",
					"item_code": "ITEM-1", "demand_identity": "IDENTITY-2",
					"schedule_qty": 20, "status": "Late", "conservation_status": "OK",
					"source_documents": [], "events": [],
				},
			],
		}
		denied = {"CUSTOMER-DENIED", "RESULT-DENIED", "COMMITMENT-DENIED", "RUN-DENIED"}
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scope_access"),
			patch.object(app.v2_flags, "is_v2_enabled", return_value=True),
			patch.object(app.progress_v2, "get_progress_detail", return_value=response),
			patch.object(
				app, "_has_linked_document_access",
				side_effect=lambda doctype, name, **kwargs: name not in denied,
			),
			patch.object(
				app, "_has_scoped_document_access",
				side_effect=lambda doctype, name, **kwargs: name not in denied,
			),
		):
			result = app.get_customer_schedule_progress_data(company="COMPANY-1")

		self.assertEqual([row["customer"] for row in result["rows"]], ["CUSTOMER-ALLOWED"])
		self.assertEqual(result["rows"][0]["commitment_names"], ["COMMITMENT-ALLOWED"])
		self.assertEqual(result["rows"][0]["result_names"], ["RESULT-ALLOWED"])
		self.assertEqual(result["rows"][0]["run_names"], ["RUN-ALLOWED"])
		self.assertEqual(result["rows"][0]["source_documents"], [{"doctype": "APS Schedule Result", "name": "RESULT-ALLOWED"}])
		self.assertEqual(result["projection"]["run_names"], ["RUN-ALLOWED"])
		self.assertEqual(result["pagination"]["permission_filtered"], 1)

	def test_progress_cell_drilldown_checks_parent_scope_and_filters_sources(self):
		response = {
			"source_documents": [
				{"doctype": "Delivery Note", "name": "DN-ALLOWED"},
				{"doctype": "Delivery Note", "name": "DN-DENIED"},
			],
			"lineage": {"delivery": [{"doctype": "Delivery Note", "name": "DN-DENIED"}]},
			"row": {"source_documents": [], "commitment_names": [], "result_names": []},
		}
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scoped_document_access") as require_scoped,
			patch.object(app.frappe.db, "get_value", return_value="SCHEDULE-1"),
			patch.object(app.v2_flags, "is_v2_enabled", return_value=True),
			patch.object(app.progress_v2, "get_progress_cell", return_value=response),
			patch.object(
				app, "_has_linked_document_access",
				side_effect=lambda doctype, name, **kwargs: name != "DN-DENIED",
			),
		):
			result = app.get_progress_cell_drilldown(
				date_value="2026-08-20", demand_identity="IDENTITY-1", schedule_item="ITEM-ROW-1",
			)

		require_scoped.assert_any_call("APS Demand Identity", "IDENTITY-1", ptype="read")
		require_scoped.assert_any_call("Customer Delivery Schedule", "SCHEDULE-1", ptype="read")
		self.assertEqual(result["source_documents"], [{"doctype": "Delivery Note", "name": "DN-ALLOWED"}])
		self.assertEqual(result["lineage"]["delivery"], [])

	def test_ui_and_doctype_definitions_make_engine_managed_records_read_only(self):
		for child_name in ("aps_work_order_proposal_item", "aps_shift_schedule_proposal_item"):
			definition = json.loads(
				(
					APP_ROOT
					/ "injection_aps/doctype"
					/ child_name
					/ f"{child_name}.json"
				).read_text(encoding="utf-8")
			)
			review_field = next(field for field in definition["fields"] if field["fieldname"] == "review_status")
			self.assertEqual(review_field.get("read_only"), 1)
			review_note = next(field for field in definition["fields"] if field["fieldname"] == "review_note")
			self.assertEqual(review_note.get("read_only"), 1)

		for doctype, protected_fields in {
			"APS Schedule Result": {"notes", "segments"},
			"APS Schedule Import Batch": {"customer", "company", "version_no", "source_type", "uploaded_file"},
			"APS Release Batch": set(),
			"APS Work Order Proposal Batch": {"notes", "items"},
			"APS Shift Schedule Proposal Batch": {"notes", "items"},
		}.items():
			with self.subTest(engine_doctype=doctype):
				definition_path = APP_ROOT / "injection_aps/doctype" / frappe.scrub(doctype) / f"{frappe.scrub(doctype)}.json"
				definition = json.loads(definition_path.read_text(encoding="utf-8"))
				self.assertEqual(definition.get("allow_rename"), 0)
				for permission in definition.get("permissions") or []:
					self.assertFalse({"create", "write", "delete"}.intersection(
						{name for name, enabled in permission.items() if enabled == 1}
					))
				fields = {row.get("fieldname"): row for row in definition.get("fields") or []}
				for fieldname in protected_fields:
					self.assertEqual(fields[fieldname].get("read_only"), 1)
				for flags in permissions.APS_DOCTYPE_PERMISSIONS[doctype].values():
					self.assertFalse({"create", "write", "delete"}.intersection(flags))

		shared_source = (APP_ROOT / "public/js/injection_aps_shared.js").read_text(encoding="utf-8")
		for action_name in ("preview_segment_split", "update_schedule_notes"):
			role_line = next(line for line in shared_source.splitlines() if f"{action_name}:" in line)
			role_values = ast.literal_eval(role_line.split(":", 1)[1].strip().rstrip(","))
			self.assertEqual(set(role_values), permissions.APS_PLAN_ROLES)
		for script_name, method_name in (
			("aps_work_order_proposal_batch.js", "review_work_order_proposals"),
			("aps_shift_schedule_proposal_batch.js", "review_shift_schedule_proposals"),
		):
			source = (APP_ROOT / "public/js" / script_name).read_text(encoding="utf-8")
			self.assertIn("frm.get_selected", source)
			self.assertIn("row_names: JSON.stringify", source)
			self.assertIn(f"injection_aps.api.app.{method_name}", source)

		controller = (
			APP_ROOT
			/ "injection_aps/doctype/customer_delivery_schedule/customer_delivery_schedule.py"
		).read_text(encoding="utf-8")
		self.assertIn("_protect_import_managed_schedule", controller)
		self.assertIn("aps_schedule_import_transition", controller)
		for role, flags in permissions.APS_DOCTYPE_PERMISSIONS["Customer Delivery Schedule"].items():
			with self.subTest(role=role):
				self.assertNotIn("write", flags)
				self.assertNotIn("create", flags)
				self.assertNotIn("delete", flags)
		workspace = json.loads(
			(
				APP_ROOT
				/ "injection_aps/workspace/injection_aps/injection_aps.json"
			).read_text(encoding="utf-8")
		)
		self.assertFalse(
			any(row.get("link_to") == "Customer Delivery Schedule" for row in workspace.get("links", []))
		)
		page = json.loads(
			(
				APP_ROOT
				/ "injection_aps/page/aps_change_impact_center/aps_change_impact_center.json"
			).read_text(encoding="utf-8")
		)
		self.assertEqual(
			{row["role"] for row in page.get("roles", [])},
			set(permissions.APS_PAGE_ROLE_MAP["aps-change-impact-center"]),
		)

	def test_raw_change_payloads_are_hidden_behind_an_ungranted_permission_level(self):
		for doctype, raw_fields in {
			"APS Change Request": {
				"impact_json",
				"proposal_json",
				"before_snapshot_json",
				"after_snapshot_json",
				"application_result_json",
			},
			"APS Change Application Log": {
				"proposal_json",
				"before_snapshot_json",
				"after_snapshot_json",
				"application_result_json",
			},
		}.items():
			with self.subTest(doctype=doctype):
				path = APP_ROOT / "injection_aps/doctype" / frappe.scrub(doctype) / f"{frappe.scrub(doctype)}.json"
				definition = json.loads(path.read_text(encoding="utf-8"))
				fields = {row.get("fieldname"): row for row in definition.get("fields") or []}
				for fieldname in raw_fields:
					self.assertEqual(fields[fieldname].get("hidden"), 1)
					self.assertEqual(fields[fieldname].get("permlevel"), 1)
				self.assertFalse(any(row.get("permlevel") == 1 for row in definition.get("permissions") or []))

	def test_dependency_permission_setup_does_not_grant_standard_master_access(self):
		for dangerous_doctype in (
			"Company",
			"Customer",
			"Item",
			"Sales Order",
			"Supplier",
			"Employee",
			"User",
		):
			self.assertNotIn(dangerous_doctype, permissions.DEPENDENCY_READ_DOCTYPES)
		self.assertEqual(
			set(permissions.DEPENDENCY_READ_DOCTYPES),
			set(permissions.DEPENDENCY_DOCTYPE_ROLE_PERMISSIONS),
		)
		with (
			patch.object(permissions, "_restore_legacy_dependency_link_permissions") as restore,
			patch.object(permissions.frappe.db, "exists", return_value=True),
			patch.object(permissions, "ensure_custom_docperm") as ensure,
		):
			permissions.ensure_dependency_link_permissions()

		restore.assert_not_called()
		self.assertTrue(ensure.call_args_list)
		self.assertEqual(
			{entry.kwargs["doctype"] for entry in ensure.call_args_list},
			{"Work Order Scheduling"},
		)

	def test_legacy_dependency_grant_without_standard_permission_is_removed(self):
		row = frappe._dict(name="CUSTOM-PERM-1", role="PMC")
		with (
			patch.object(permissions, "LEGACY_DEPENDENCY_READ_DOCTYPES", ("Employee",)),
			patch.object(permissions.frappe.db, "exists", return_value=True),
			patch.object(permissions.frappe, "get_all", return_value=[row]),
			patch.object(permissions.frappe.db, "get_value", return_value=None),
			patch.object(permissions.frappe, "delete_doc") as delete_doc,
		):
			permissions._restore_legacy_dependency_link_permissions()

		delete_doc.assert_called_once_with(
			"Custom DocPerm",
			"CUSTOM-PERM-1",
			ignore_permissions=True,
		)

	def test_proposal_child_rows_reject_direct_save_and_delete(self):
		for controller in (APSWorkOrderProposalItem, APSShiftScheduleProposalItem):
			row = frappe._dict(flags=frappe._dict())
			with self.subTest(controller=controller.__name__), patch.object(
				frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.PermissionError(message)
				),
			):
				with self.assertRaises(frappe.PermissionError):
					controller._block_direct_mutation(row)
				with self.assertRaises(frappe.PermissionError):
					controller._block_direct_mutation(row)

	def test_engine_audit_controllers_block_direct_mutation_but_allow_internal_services(self):
		for controller in (APSScheduleResult, APSScheduleImportBatch, APSReleaseBatch):
			with self.subTest(controller=controller.__name__):
				document = frappe._dict(flags=frappe._dict())
				with patch.object(
					frappe,
					"throw",
					side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
						frappe.PermissionError(message)
					),
				):
					with self.assertRaises(frappe.PermissionError):
						controller._protect_engine_managed_record(document)
				document.flags.ignore_permissions = True
				controller._protect_engine_managed_record(document)

	def test_new_security_and_ui_strings_are_context_scoped_in_chinese(self):
		with (APP_ROOT / "translations/zh.csv").open(encoding="utf-8-sig", newline="") as handle:
			rows = list(csv.reader(handle))
		context_keys = [row[0] for row in rows if len(row) > 2 and row[2] == "Injection APS"]
		self.assertEqual(len(context_keys), len(set(context_keys)))
		by_key = {(row[0], row[2] if len(row) > 2 else ""): row[1] for row in rows if len(row) >= 2}
		for source in (
			"Planning and Risk",
			"Active customer delivery schedules are read-only. Use Schedule Import & Diff or Change Impact Center.",
			"Applied and Skipped proposal states are maintained by the APS release engine.",
			"Confirm Change Apply",
		):
			with self.subTest(source=source):
				self.assertTrue(by_key.get((source, "Injection APS")))

	def test_state_changing_whitelisted_apis_are_post_only(self):
		mutating = {
			"export_table_xlsx",
			"import_customer_delivery_schedule",
			"rebuild_demand_pool",
			"rebuild_net_requirements",
			"run_planning_run",
			"recalculate_plan_consistency",
			"approve_planning_run",
			"sync_planning_run_to_execution",
			"release_planning_run",
			"validate_run_mold_readiness",
			"generate_work_order_proposals",
			"apply_work_order_proposals",
			"review_work_order_proposals",
			"reject_work_order_proposals",
			"generate_shift_schedule_proposals",
			"apply_shift_schedule_proposals",
			"review_shift_schedule_proposals",
			"reject_shift_schedule_proposals",
			"update_schedule_notes",
			"sync_execution_feedback_to_aps",
			"sync_delivery_allocations",
			"apply_schedule_revision",
			"resolve_schedule_identity_ambiguity",
			"resolve_unallocated_delivery",
			"analyze_capacity_balance",
			"confirm_capacity_balance",
			"apply_capacity_balance",
			"get_execution_health_for_run",
			"sync_machine_capabilities_from_workstations",
			"analyze_change_request_impact",
			"batch_analyze_change_requests",
			"confirm_change_request",
			"approve_change_request",
			"reject_change_request",
			"apply_change_request",
			"rebuild_exceptions",
			"promote_schedule_import_to_net_requirement",
			"create_trial_run_from_net_requirement_context",
			"apply_manual_schedule_adjustment",
			"apply_segment_split",
			"create_or_update_downtime_window",
			"apply_schedule_impact",
			"repair_item_references",
			"detach_standard_references",
			"update_net_requirement_row",
			"delete_net_requirement_row",
		}
		tree = ast.parse((APP_ROOT / "api/app.py").read_text(encoding="utf-8"))
		decorators = {}
		for node in tree.body:
			if not isinstance(node, ast.FunctionDef):
				continue
			for decorator in node.decorator_list:
				if not (
					isinstance(decorator, ast.Call)
					and isinstance(decorator.func, ast.Attribute)
					and decorator.func.attr == "whitelist"
				):
					continue
				methods = next(
					(keyword.value for keyword in decorator.keywords if keyword.arg == "methods"),
					None,
				)
				decorators[node.name] = ast.literal_eval(methods) if methods else None
		for method_name in sorted(mutating):
			with self.subTest(method=method_name):
				self.assertEqual(decorators.get(method_name), ["POST"])

		repair = next(
			node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "repair_item_references"
		)
		defaults = {
			argument.arg: ast.literal_eval(default)
			for argument, default in zip(repair.args.args[-len(repair.args.defaults):], repair.args.defaults)
		}
		self.assertEqual(defaults["commit"], 0)

	def test_every_run_and_proposal_mutation_uses_complete_scope_guard(self):
		tree = ast.parse((APP_ROOT / "api/app.py").read_text(encoding="utf-8"))
		functions = {
			node.name: node
			for node in tree.body
			if isinstance(node, ast.FunctionDef)
		}

		def called(function_name, guard_name):
			return any(
				isinstance(node, ast.Call)
				and (
					(isinstance(node.func, ast.Name) and node.func.id == guard_name)
					or (isinstance(node.func, ast.Attribute) and node.func.attr == guard_name)
				)
				for node in ast.walk(functions[function_name])
			)

		for function_name in (
			"run_planning_run",
			"recalculate_plan_consistency",
			"approve_planning_run",
			"sync_planning_run_to_execution",
			"release_planning_run",
			"validate_run_mold_readiness",
			"generate_work_order_proposals",
			"generate_shift_schedule_proposals",
			"sync_execution_feedback_to_aps",
			"analyze_capacity_balance",
			"confirm_capacity_balance",
			"apply_capacity_balance",
			"get_execution_health_for_run",
			"rebuild_exceptions",
			"apply_manual_schedule_adjustment",
			"apply_segment_split",
			"create_or_update_downtime_window",
			"apply_schedule_impact",
			"update_net_requirement_row",
		):
			with self.subTest(function=function_name):
				self.assertTrue(called(function_name, "_require_complete_run_mutation_scope"))

		for function_name in (
			"apply_work_order_proposals",
			"review_work_order_proposals",
			"reject_work_order_proposals",
			"apply_shift_schedule_proposals",
			"review_shift_schedule_proposals",
			"reject_shift_schedule_proposals",
		):
			with self.subTest(function=function_name):
				self.assertTrue(
					called(function_name, "_require_complete_proposal_batch_scope")
					or called(function_name, "_review_proposal_rows")
				)


if __name__ == "__main__":
	unittest.main()
