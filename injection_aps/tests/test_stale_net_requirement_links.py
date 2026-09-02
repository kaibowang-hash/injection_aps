from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, call, patch

import frappe

from injection_aps.api import app
from injection_aps.patches.v0_0_2 import retire_stale_net_requirement_exception_sources as retirement_patch
from injection_aps.services import planning


class _ExceptionDocument:
	def __init__(self, *, name, planning_run, status, source_name, diagnostic_json=""):
		self.name = name
		self.planning_run = planning_run
		self.status = status
		self.source_doctype = "APS Net Requirement"
		self.source_name = source_name
		self.diagnostic_json = diagnostic_json
		self.saved_with_ignore_permissions = False

	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def save(self, *, ignore_permissions=False):
		self.saved_with_ignore_permissions = ignore_permissions
		return self


class TestStaleNetRequirementLinks(unittest.TestCase):
	def test_net_requirement_rebuild_uses_the_shared_company_lock(self):
		database = MagicMock()
		with patch.object(planning.frappe, "db", database):
			planning._lock_company_for_aps_planning("COMPANY-1")

		database.sql.assert_called_once_with(
			"select name from `tabCompany` where name = %s for update",
			("COMPANY-1",),
		)

	def test_rebuild_locks_company_before_repair_or_generated_row_deletion(self):
		events = []
		with (
			patch.object(
				planning,
				"_lock_company_for_aps_planning",
				side_effect=lambda _company: events.append("lock-company"),
			),
			patch.object(
				planning,
				"repair_item_references",
				side_effect=lambda **_kwargs: events.append("repair-references") or {},
			),
			patch.object(
				planning,
				"_delete_system_generated_rows",
				side_effect=lambda *_args, **_kwargs: events.append("delete-generated"),
			),
			patch.object(planning.frappe, "get_all", return_value=[]),
			patch.object(planning, "_get_available_stock_map", return_value={}),
			patch.object(
				planning,
				"get_settings_dict",
				return_value={
					"item_safety_stock_field": "safety_stock",
					"item_max_stock_field": "max_stock",
					"item_min_batch_field": "minimum_batch",
				},
			),
		):
			planning.rebuild_net_requirements(
				company="COMPANY-1",
				existing_work_order_policy="Exclude",
			)

		self.assertEqual(events[:3], ["lock-company", "repair-references", "delete-generated"])

	def test_missing_linked_source_is_inaccessible_without_permission_lookup(self):
		database = MagicMock()
		database.exists.return_value = False
		with (
			patch.object(app.frappe, "db", database),
			patch.object(app, "_has_document_access") as has_document_access,
		):
			self.assertFalse(app._has_linked_document_access("APS Net Requirement", "APS-NET-MISSING"))

		has_document_access.assert_not_called()

	def test_link_deleted_during_permission_check_is_safely_skipped(self):
		database = MagicMock()
		database.exists.return_value = True
		with (
			patch.object(app.frappe, "db", database),
			patch.object(
				app,
				"_has_scoped_document_access",
				side_effect=frappe.DoesNotExistError("retired concurrently"),
			),
		):
			self.assertFalse(app._has_linked_document_access("APS Net Requirement", "APS-NET-OLD"))

	def test_real_permission_error_is_not_suppressed(self):
		database = MagicMock()
		database.exists.return_value = True
		with (
			patch.object(app.frappe, "db", database),
			patch.object(
				app,
				"_has_document_access",
				side_effect=frappe.PermissionError("not permitted"),
			),
		):
			with self.assertRaisesRegex(frappe.PermissionError, "not permitted"):
				app._has_linked_document_access("Work Order", "WO-DENIED")

	def test_run_console_skips_only_the_exception_with_a_missing_source(self):
		run = frappe._dict(name="RUN-1", company="COMPANY-1")
		exceptions = [
			frappe._dict(
				name="EXC-VALID",
				planning_run="RUN-1",
				source_doctype="APS Net Requirement",
				source_name="APS-NET-VALID",
			),
			frappe._dict(
				name="EXC-STALE",
				planning_run="RUN-1",
				source_doctype="APS Net Requirement",
				source_name="APS-NET-MISSING",
			),
		]

		def get_list(doctype, **_kwargs):
			return {
				"APS Planning Run": [run],
				"APS Schedule Result": [],
				"APS Exception Log": exceptions,
			}[doctype]
		database = MagicMock()
		database.exists.side_effect = lambda _doctype, name: name != "APS-NET-MISSING"

		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scope_access"),
			patch.object(app.frappe, "session", frappe._dict(user="planner@example.com")),
			patch.object(app.frappe, "db", database),
			patch.object(app.frappe, "get_list", side_effect=get_list),
			patch.object(app, "_has_document_permission_hook", return_value=False),
			patch.object(app, "_prime_scoped_document_dependencies"),
			patch.object(app, "_prime_exception_source_access"),
			patch.object(app, "_has_scoped_document_access", return_value=True),
			patch.object(app.v2_flags, "is_v2_enabled", return_value=False),
			patch.object(app.planning, "get_next_actions_for_context", return_value={}),
			patch.object(app, "_sanitize_planning_run_context", side_effect=lambda context, **_kwargs: context),
		):
			payload = app.get_run_console_data.__wrapped__(company="COMPANY-1")

		self.assertEqual(payload["runs"][0]["exception_count"], 1)

	def test_retirement_resolves_active_rows_detaches_links_and_preserves_audit(self):
		open_doc = _ExceptionDocument(
			name="EXC-OPEN",
			planning_run="RUN-1",
			status="Open",
			source_name="APS-NET-OLD",
			diagnostic_json=json.dumps({"existing": "evidence"}),
		)
		dismissed_doc = _ExceptionDocument(
			name="EXC-DISMISSED",
			planning_run="RUN-2",
			status="Dismissed",
			source_name="APS-NET-OLD",
		)
		documents = {row.name: row for row in (open_doc, dismissed_doc)}
		database = MagicMock()
		database.exists.return_value = True
		database.count.side_effect = [0, 2]

		with (
			patch.object(planning.frappe, "db", database),
			patch.object(
				planning.frappe,
				"get_all",
				return_value=[
					frappe._dict(name="EXC-OPEN", planning_run="RUN-1", source_name="APS-NET-OLD"),
					frappe._dict(name="EXC-DISMISSED", planning_run="RUN-2", source_name="APS-NET-OLD"),
				],
			),
			patch.object(planning.frappe, "get_doc", side_effect=lambda _doctype, name: documents[name]),
			patch.object(planning.frappe, "session", frappe._dict(user="planner@example.com")),
			patch.object(planning, "now_datetime", return_value="2026-08-12 08:00:00"),
		):
			result = planning._retire_net_requirement_exception_sources(["APS-NET-OLD"])

		self.assertEqual(result["retired"], ["EXC-OPEN", "EXC-DISMISSED"])
		self.assertEqual(open_doc.status, "Resolved")
		self.assertEqual(dismissed_doc.status, "Dismissed")
		for document in (open_doc, dismissed_doc):
			self.assertIsNone(document.source_name)
			self.assertTrue(document.saved_with_ignore_permissions)
			retirement = json.loads(document.diagnostic_json)["source_retirement"]
			self.assertEqual(retirement["source_name"], "APS-NET-OLD")
			self.assertEqual(retirement["retired_by"], "planner@example.com")
		self.assertEqual(json.loads(open_doc.diagnostic_json)["existing"], "evidence")
		self.assertEqual(
			database.set_value.call_args_list,
			[
				call("APS Planning Run", "RUN-1", "exception_count", 0, update_modified=False),
				call("APS Planning Run", "RUN-2", "exception_count", 2, update_modified=False),
			],
		)

	def test_net_requirement_delete_retires_exception_links_before_rows_are_deleted(self):
		events = []
		database = MagicMock()
		database.exists.return_value = True
		database.sql.side_effect = lambda *_args, **_kwargs: events.append("clear-result-links")
		meta = MagicMock()
		meta.has_field.return_value = True

		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_meta", return_value=meta),
			patch.object(planning.frappe, "get_all", return_value=["APS-NET-1", "APS-NET-2"]),
			patch.object(
				planning,
				"_retire_net_requirement_exception_sources",
				side_effect=lambda names, **_kwargs: events.append(("retire", tuple(names))),
			),
			patch.object(
				planning.frappe,
				"delete_doc",
				side_effect=lambda _doctype, name, **_kwargs: events.append(("delete", name)),
			),
		):
			planning._delete_system_generated_rows("APS Net Requirement", company="COMPANY-1")

		self.assertEqual(
			events,
			[
				("retire", ("APS-NET-1", "APS-NET-2")),
				"clear-result-links",
				("delete", "APS-NET-1"),
				("delete", "APS-NET-2"),
			],
		)

	def test_upgrade_patch_passes_only_stale_exception_names_to_retirement(self):
		database = MagicMock()
		database.exists.return_value = True
		database.sql.return_value = ["EXC-MISSING", "EXC-RECYCLED"]

		with (
			patch.object(retirement_patch.frappe, "db", database),
			patch.object(retirement_patch.planning, "_retire_net_requirement_exception_sources") as retire,
		):
			retirement_patch.execute()

		retire.assert_called_once_with(
			exception_names=["EXC-MISSING", "EXC-RECYCLED"],
			reason="Historical APS Net Requirement source retired during upgrade",
		)
		self.assertIn("exception_log.creation < net_requirement.creation", database.sql.call_args.args[0])


if __name__ == "__main__":
	unittest.main()
