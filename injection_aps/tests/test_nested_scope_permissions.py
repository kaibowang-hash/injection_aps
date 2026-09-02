from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.api import app


class TestNestedScopePermissions(unittest.TestCase):
	def test_permission_errors_escape_request_document_names(self):
		database = MagicMock()
		database.exists.return_value = False
		with (
			patch.object(app.frappe, "db", database),
			patch.object(app, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, exception, **_kwargs: (_ for _ in ()).throw(exception(message)),
			),
		):
			with self.assertRaises(frappe.DoesNotExistError) as error:
				app._require_document_access("APS Planning Run", '<img src=x onerror="alert(1)">')

		self.assertNotIn("<img", str(error.exception))
		self.assertIn("&lt;img", str(error.exception))

	def test_progress_response_collects_permission_references_once(self):
		response = {
			"projection": {"run_names": ["RUN-1"], "selected_run": "RUN-1"},
			"rows": [
				{
					"company": "COMPANY-1",
					"customer": "CUSTOMER-1",
					"item_code": "ITEM-1",
					"schedule": "SCHEDULE-1",
					"schedule_item": "SCHEDULE-ITEM-1",
					"demand_identity": "IDENTITY-1",
					"commitment_names": ["COMMITMENT-1"],
					"result_names": ["RESULT-1"],
					"run_names": ["RUN-1"],
					"source_documents": [{"doctype": "APS Production Allocation", "name": "ALLOC-1"}],
					"events": [{"sources": [{"doctype": "Stock Entry", "name": "STE-1"}]}],
					"cells": {"2026-09-01": {"sources": [{"doctype": "Delivery Note", "name": "DN-1"}]}},
				}
			],
		}
		cache = {}
		with patch.object(app, "_prime_linked_document_access") as prime:
			app._prime_progress_response_access(response, cache)

		prime.assert_called_once()
		references = prime.call_args.args[0]
		self.assertEqual(references["Customer Delivery Schedule Item"], {"SCHEDULE-ITEM-1"})
		self.assertEqual(references["APS Demand Commitment"], {"COMMITMENT-1"})
		self.assertEqual(references["APS Schedule Result"], {"RESULT-1"})
		self.assertEqual(references["APS Planning Run"], {"RUN-1"})
		self.assertEqual(references["APS Production Allocation"], {"ALLOC-1"})
		self.assertEqual(references["Stock Entry"], {"STE-1"})
		self.assertEqual(references["Delivery Note"], {"DN-1"})

	def test_progress_child_sources_prime_parent_scope_in_one_batch(self):
		cache = {}
		child_rows = [
			frappe._dict(name="ROW-1", parent="SCHEDULE-1"),
			frappe._dict(name="ROW-2", parent="SCHEDULE-2"),
		]
		parent_rows = [
			frappe._dict(name="SCHEDULE-1"),
			frappe._dict(name="SCHEDULE-2"),
		]
		database = MagicMock()
		database.exists.return_value = True
		with (
			patch.object(app.frappe, "db", database),
			patch.object(app.frappe, "get_all", return_value=child_rows) as get_all,
			patch.object(app, "_get_scoped_rows_for_priming", return_value=parent_rows) as get_parents,
			patch.object(app, "_prime_scoped_document_dependencies"),
			patch.object(app, "_has_scoped_document_access", return_value=True),
		):
			app._prime_linked_document_access(
				{"Customer Delivery Schedule Item": {"ROW-1", "ROW-2"}},
				cache,
			)

		get_all.assert_called_once()
		get_parents.assert_called_once_with(
			"Customer Delivery Schedule",
			{"SCHEDULE-1", "SCHEDULE-2"},
			cache,
		)
		self.assertTrue(cache[("linked", "Customer Delivery Schedule Item", "ROW-1", "read")])
		self.assertTrue(cache[("linked", "Customer Delivery Schedule Item", "ROW-2", "read")])

	def test_scoped_access_fails_closed_for_inherited_child_reference(self):
		with (
			patch.object(
				app,
				"_get_document_scope",
				return_value=frappe._dict(current_schedule_item="SCHEDULE-ITEM-DENIED"),
			),
			patch.object(app, "_has_document_access", return_value=True),
			patch.object(app, "_has_linked_document_access", return_value=False) as has_linked,
		):
			self.assertFalse(app._has_scoped_document_access("APS Demand Identity", "IDENTITY-1"))

		has_linked.assert_called_once_with(
			"Customer Delivery Schedule Item",
			"SCHEDULE-ITEM-DENIED",
			access_cache=unittest.mock.ANY,
		)

	def test_scope_dependency_priming_batches_dynamic_source_reference(self):
		rows = [
			frappe._dict(
				name="ADMISSION-1",
				source_doctype="Delivery Note Item",
				source_name="DNI-1",
			)
		]
		with (
			patch.object(app, "_seed_permission_filtered_rows"),
			patch.object(app, "_get_permission_filtered_scoped_rows", return_value=[]),
			patch.object(app, "_prime_linked_document_access") as prime_linked,
		):
			app._prime_scoped_document_dependencies(rows, "APS Demand Admission", {})

		prime_linked.assert_called_once_with(
			{"Delivery Note Item": {"DNI-1"}},
			unittest.mock.ANY,
		)

	def test_active_schedule_scope_is_normalized_and_fully_visible(self):
		with (
			patch.object(app.planning, "_normalize_schedule_scope", return_value="Default Scope"),
			patch.object(app, "_require_all_scoped_documents_visible") as require_visible,
		):
			scope = app._require_active_schedule_scope_visible(
				customer="CUSTOMER-1",
				company="COMPANY-1",
				schedule_scope="",
			)

		self.assertEqual(scope, "Default Scope")
		require_visible.assert_called_once_with(
			"Customer Delivery Schedule",
			{
				"company": "COMPANY-1",
				"customer": "CUSTOMER-1",
				"schedule_scope": "Default Scope",
				"status": "Active",
			},
		)

	def test_schedule_preview_blocks_hidden_active_schedule_before_service(self):
		with (
			patch.object(app, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(app, "_require_demand_access"),
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(app, "_require_document_access"),
			patch.object(app, "_require_scope_access"),
			patch.object(
				app,
				"_require_active_schedule_scope_visible",
				side_effect=frappe.PermissionError("hidden active schedule"),
			),
			patch.object(app.planning, "preview_customer_delivery_schedule") as preview,
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden active schedule"):
				app.preview_customer_delivery_schedule.__wrapped__(
					customer="CUSTOMER-1",
					company="COMPANY-1",
					version_no="V1",
				)

		preview.assert_not_called()

	def test_schedule_mutations_recheck_hidden_active_scope_in_service_validator(self):
		preview = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"schedule_scope": "SCOPE-1",
			"source_rows": [],
			"effective_schedule_rows": [],
		}
		cases = (
			(app.apply_schedule_revision, app.schedule_revision, "apply_revision"),
			(app.import_customer_delivery_schedule, app.planning, "import_customer_delivery_schedule"),
		)
		for endpoint, service_module, service_name in cases:
			with self.subTest(endpoint=endpoint.__name__):
				def invoke_locked_validator(**kwargs):
					return kwargs["reference_access_validator"](preview)

				with (
					patch.object(app, "_", side_effect=lambda message, **_kwargs: message),
					patch.object(app, "_require_demand_access"),
					patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
					patch.object(app, "_require_document_access"),
					patch.object(app, "_require_scope_access"),
					patch.object(app, "_require_schedule_import_reference_access") as require_references,
					patch.object(
						app,
						"_require_active_schedule_scope_visible",
						side_effect=["SCOPE-1", frappe.PermissionError("hidden after lock")],
					) as require_active,
					patch.object(service_module, service_name, side_effect=invoke_locked_validator) as service,
				):
					with self.assertRaisesRegex(frappe.PermissionError, "hidden after lock"):
						endpoint.__wrapped__(
							customer="CUSTOMER-1",
							company="COMPANY-1",
							version_no="V1",
						)

				service.assert_called_once()
				require_references.assert_called_once_with(
					preview,
					customer="CUSTOMER-1",
					company="COMPANY-1",
				)
				self.assertEqual(require_active.call_count, 2)

	def test_revision_service_validates_after_lock_and_before_persist(self):
		events = []
		database = MagicMock()

		def validate(_preview):
			events.append("validate")
			raise frappe.PermissionError("hidden after lock")

		with (
			patch.object(app.schedule_revision.frappe, "db", database),
			patch.object(app.schedule_revision.frappe, "generate_hash", return_value="scopecheck"),
			patch.object(app.schedule_revision, "_require_v2_enabled"),
			patch.object(
				app.schedule_revision,
				"_normalize_scope",
				return_value=("CUSTOMER-1", "COMPANY-1", "SCOPE-1"),
			),
			patch.object(app.schedule_revision, "normalize_revision_mode", return_value="Full Replacement"),
			patch.object(
				app.schedule_revision,
				"_lock_revision_scope",
				side_effect=lambda *_args: events.append("lock"),
			),
			patch.object(
				app.schedule_revision,
				"preview_revision",
				side_effect=lambda **_kwargs: events.append("preview") or {},
			),
			patch.object(app.schedule_revision, "_persist_revision") as persist,
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden after lock"):
				app.schedule_revision.apply_revision(
					customer="CUSTOMER-1",
					company="COMPANY-1",
					version_no="V1",
					schedule_scope="SCOPE-1",
					confirmed_revision_mode="Full Replacement",
					reference_access_validator=validate,
				)

		self.assertEqual(events, ["lock", "preview", "validate"])
		persist.assert_not_called()
		database.rollback.assert_called_once_with(save_point="aps_revision_scopecheck")

	def test_import_service_validates_after_customer_lock_and_before_write(self):
		events = []
		database = MagicMock()

		def lock(*_args, **_kwargs):
			events.append("lock")
			return [("CUSTOMER-1",)]

		def preview(**_kwargs):
			events.append("preview")
			return {}

		def validate(_preview):
			events.append("validate")
			raise frappe.PermissionError("hidden after lock")

		database.sql.side_effect = lock
		with (
			patch.object(app.planning.frappe, "db", database),
			patch.object(app.planning.frappe, "generate_hash", return_value="scopecheck"),
			patch.object(app.planning, "preview_customer_delivery_schedule", side_effect=preview),
			patch.object(app.planning, "_apply_customer_delivery_schedule_import") as apply_import,
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden after lock"):
				app.planning.import_customer_delivery_schedule(
					customer="CUSTOMER-1",
					company="COMPANY-1",
					version_no="V1",
					active_state_token="STATE-1",
					expected_import_fingerprint="FINGERPRINT-1",
					reference_access_validator=validate,
				)

		self.assertEqual(events, ["lock", "preview", "validate"])
		apply_import.assert_not_called()
		database.rollback.assert_called_once_with(save_point="aps_schedule_import_scopecheck")

	def test_payload_link_checks_batch_direct_references(self):
		def prime(doctype, names, cache):
			for name in names:
				cache[("document", doctype, name, "read")] = True

		with (
			patch.object(app, "_prime_document_access", side_effect=prime) as prime_access,
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda doctype, name, ptype="read", access_cache=None: access_cache[
					("document", doctype, name, ptype)
				],
			),
		):
			app._require_payload_link_access(
				[{"workstation": "WS-2"}, {"workstation": "WS-1"}],
				(("workstation", "Workstation", False),),
			)

		prime_access.assert_called_once()
		self.assertEqual(prime_access.call_args.args[:2], ("Workstation", {"WS-1", "WS-2"}))

	def test_schedule_result_checks_demand_commitment(self):
		with (
			patch.object(
				app,
				"_get_document_scope",
				side_effect=lambda doctype, _name, **_kwargs: (
					frappe._dict(demand_commitment="COMMITMENT-DENIED")
					if doctype == "APS Schedule Result"
					else frappe._dict()
				),
			),
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda _doctype, name, **_kwargs: name != "COMMITMENT-DENIED",
			),
		):
			self.assertFalse(app._has_scoped_document_access("APS Schedule Result", "RESULT-1"))

	def test_campaign_scope_checks_mold(self):
		with (
			patch.object(app, "_get_document_scope", return_value=frappe._dict(mold="MOLD-DENIED")),
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda doctype, name, **_kwargs: (doctype, name) != ("Mold", "MOLD-DENIED"),
			),
		):
			self.assertFalse(app._has_scoped_document_access("APS Production Campaign", "CAMPAIGN-1"))

	def test_constraint_scope_checks_affected_customer_in_both_access_paths(self):
		scope = frappe._dict(affected_customer="CUSTOMER-DENIED")

		def has_access(doctype, name, **_kwargs):
			return (doctype, name) != ("Customer", "CUSTOMER-DENIED")

		with (
			patch.object(app, "_get_document_scope", return_value=scope),
			patch.object(app, "_has_document_access", side_effect=has_access),
		):
			self.assertFalse(app._has_scoped_document_access("APS Constraint Resolution", "RES-1"))

		def require_access(doctype, name, **_kwargs):
			if (doctype, name) == ("Customer", "CUSTOMER-DENIED"):
				raise frappe.PermissionError("hidden customer")

		with (
			patch.object(app, "_get_document_scope", return_value=scope),
			patch.object(app, "_require_scope_access"),
			patch.object(app, "_require_document_access", side_effect=require_access),
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden customer"):
				app._require_scoped_document_access("APS Constraint Resolution", "RES-1")

	def test_bom_scope_rejects_hidden_nested_result_and_bom(self):
		def scope(doctype, _name, **_kwargs):
			if doctype == "APS BOM Pegging":
				return frappe._dict(child_result="RESULT-DENIED", bom="BOM-DENIED")
			return frappe._dict()

		with (
			patch.object(app, "_get_document_scope", side_effect=scope),
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda doctype, name, **_kwargs: name != "RESULT-DENIED",
			),
		):
			self.assertFalse(app._has_scoped_document_access("APS BOM Pegging", "PEG-1"))

		def require_access(_doctype, name, **_kwargs):
			if name == "BOM-DENIED":
				raise frappe.PermissionError("hidden BOM")

		with (
			patch.object(app, "_get_document_scope", side_effect=scope),
			patch.object(app, "_require_scope_access"),
			patch.object(app, "_require_document_access", side_effect=require_access),
		):
			with self.assertRaisesRegex(frappe.PermissionError, "hidden BOM"):
				app._require_scoped_document_access("APS BOM Pegging", "PEG-1")

	def test_segment_scope_checks_its_own_workstation_after_parent(self):
		def scope(doctype, _name, **_kwargs):
			return (
				frappe._dict(parent="RESULT-1", workstation="WS-DENIED")
				if doctype == "APS Schedule Segment"
				else frappe._dict()
			)

		with (
			patch.object(app, "_get_document_scope", side_effect=scope),
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda doctype, name, **_kwargs: (doctype, name) != ("Workstation", "WS-DENIED"),
			),
		):
			self.assertFalse(app._has_scoped_document_access("APS Schedule Segment", "SEG-1"))

	def test_complete_scoped_payload_checks_every_document(self):
		with (
			patch.object(app, "_require_all_documents_visible", return_value={"ROW-2", "ROW-1"}),
			patch.object(
				app,
				"_get_permission_filtered_scoped_rows",
				return_value=[frappe._dict(name="ROW-1"), frappe._dict(name="ROW-2")],
			),
			patch.object(app, "_prime_scoped_document_dependencies"),
			patch.object(app, "_has_scoped_document_access", return_value=True) as has_scoped,
		):
			app._require_all_scoped_documents_visible("APS Solver Job", {"planning_run": "RUN-1"})

		self.assertEqual(
			[(args.args[0], args.args[1]) for args in has_scoped.call_args_list],
			[("APS Solver Job", "ROW-1"), ("APS Solver Job", "ROW-2")],
		)

	def test_aggregate_endpoints_use_complete_scoped_payload_guard(self):
		cases = (
			(app.get_constraint_resolutions, app.constraint_resolution, "get_constraint_resolutions", "APS Constraint Resolution"),
			(app.get_solver_scenarios, app.solver_orchestration, "get_solver_scenarios", "APS Solver Job"),
			(app.get_run_bom_selections, app.bom_planning, "get_run_bom_selections", "APS BOM Pegging"),
			(app.get_bom_pegging_tree, app.bom_planning, "get_bom_pegging_tree", "APS BOM Pegging"),
		)
		for endpoint, service_module, service_name, doctype in cases:
			with self.subTest(endpoint=endpoint.__name__):
				with (
					patch.object(app, "_require_read_access"),
					patch.object(app, "_require_complete_run_mutation_scope"),
					patch.object(app, "_require_all_scoped_documents_visible") as require_payload,
					patch.object(service_module, service_name, return_value={}),
					patch.object(app.item_display, "attach_item_display_fields", return_value={}),
				):
					endpoint.__wrapped__("RUN-1")
					require_payload.assert_called_once_with(doctype, {"planning_run": "RUN-1"})

	def test_solver_bom_and_replan_getters_validate_embedded_links(self):
		solver_payload = {"scenarios": [{"tasks": [{"workstation": "WS-1", "mold": "MOLD-1"}]}]}
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_complete_run_mutation_scope"),
			patch.object(app, "_require_all_scoped_documents_visible"),
			patch.object(app.solver_orchestration, "get_solver_scenarios", return_value=solver_payload),
			patch.object(app, "_require_payload_link_access") as require_links,
		):
			app.get_solver_scenarios.__wrapped__("RUN-1")
		require_links.assert_called_once()
		self.assertEqual(require_links.call_args.args[0], solver_payload["scenarios"][0]["tasks"])

		bom_payload = {
			"selections": [{"item_code": "ITEM-1", "bom": "BOM-1"}],
			"options": [{"item": "ITEM-1", "name": "BOM-1"}],
		}
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_complete_run_mutation_scope"),
			patch.object(app, "_require_all_scoped_documents_visible"),
			patch.object(app.bom_planning, "get_run_bom_selections", return_value=bom_payload),
			patch.object(app, "_require_payload_link_access") as require_links,
			patch.object(app.item_display, "attach_item_display_fields", return_value=bom_payload),
		):
			app.get_run_bom_selections.__wrapped__("RUN-1")
		self.assertEqual([entry.args[0] for entry in require_links.call_args_list], [bom_payload["selections"], bom_payload["options"]])

		replan_payload = {"diffs": [{"segment": "SEG-1", "linked_work_order": "WO-1"}]}
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_derived_run_scope"),
			patch.object(app.shift_replan, "get_replan_cycle", return_value=replan_payload),
			patch.object(app, "_require_payload_link_access") as require_links,
		):
			app.get_replan_cycle.__wrapped__("CYCLE-1")
		require_links.assert_called_once()
		self.assertEqual(require_links.call_args.args[0], replan_payload["diffs"])


if __name__ == "__main__":
	unittest.main()
