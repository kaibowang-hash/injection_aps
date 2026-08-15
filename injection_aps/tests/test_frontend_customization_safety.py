from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from injection_aps import install
from injection_aps.services import customizations, workspace


class TestFrontendCustomizationSafety(unittest.TestCase):
	def test_existing_dashboard_block_is_never_overwritten(self):
		database = MagicMock()
		database.exists.return_value = "Injection APS Dashboard"
		with (
			patch.object(workspace.frappe, "db", database),
			patch.object(workspace.frappe, "new_doc") as new_doc,
			patch.object(workspace.frappe, "get_doc") as get_doc,
		):
			created = workspace._ensure_dashboard_custom_block()

		self.assertFalse(created)
		new_doc.assert_not_called()
		get_doc.assert_not_called()

	def test_missing_dashboard_block_is_created_once(self):
		database = MagicMock()
		database.exists.return_value = None
		doc = MagicMock()
		with (
			patch.object(workspace.frappe, "db", database),
			patch.object(workspace.frappe, "new_doc", return_value=doc),
		):
			created = workspace._ensure_dashboard_custom_block()

		self.assertTrue(created)
		self.assertEqual(doc.name, workspace.DASHBOARD_BLOCK_NAME)
		self.assertEqual(doc.html, workspace.DASHBOARD_HTML)
		doc.insert.assert_called_once_with(ignore_permissions=True)
		doc.save.assert_not_called()

	def test_invalid_workspace_json_is_not_replaced_or_saved(self):
		database = MagicMock()
		database.exists.return_value = "Injection APS"
		doc = MagicMock()
		doc.content = "{invalid-json"
		doc.custom_blocks = []
		with (
			patch.object(workspace.frappe, "db", database),
			patch.object(workspace.frappe, "get_doc", return_value=doc),
		):
			changed = workspace._ensure_workspace_dashboard_layout()

		self.assertFalse(changed)
		doc.save.assert_not_called()

	def test_after_migrate_has_no_implicit_site_or_frontend_mutations(self):
		with (
			patch.object(install, "ensure_standard_customizations") as ensure_customizations,
			patch.object(install, "ensure_default_settings") as ensure_settings,
			patch.object(install, "ensure_seed_records") as ensure_seeds,
			patch.object(install, "ensure_roles") as ensure_roles,
			patch.object(install, "ensure_workspace_resources") as ensure_workspace,
			patch.object(install, "ensure_roles_and_permissions") as ensure_permissions,
			patch.object(install.frappe, "clear_cache") as clear_cache,
		):
			install.after_migrate()

		ensure_customizations.assert_not_called()
		ensure_roles.assert_not_called()
		ensure_permissions.assert_not_called()
		ensure_settings.assert_not_called()
		ensure_seeds.assert_not_called()
		ensure_workspace.assert_not_called()
		clear_cache.assert_not_called()

	def test_custom_field_creation_requires_explicit_layout_reorder(self):
		with (
			patch.object(customizations, "create_custom_fields") as create_custom_fields,
			patch.object(customizations, "_ensure_item_aps_field_order") as reorder_fields,
		):
			customizations.ensure_standard_customizations(update_existing=False)

		create_custom_fields.assert_called_once_with(
			customizations.STANDARD_CUSTOM_FIELDS,
			update=False,
		)
		reorder_fields.assert_not_called()

	def test_standard_frontend_json_uses_non_refreshing_timestamp(self):
		module_root = Path(__file__).resolve().parents[1] / "injection_aps"
		paths = [
			module_root / "workspace" / "injection_aps" / "injection_aps.json",
			*sorted((module_root / "page").glob("*/*.json")),
		]
		self.assertGreater(len(paths), 1)
		for path in paths:
			with self.subTest(path=path):
				data = json.loads(path.read_text(encoding="utf-8"))
				self.assertEqual(data.get("modified"), "1970-01-01 00:00:00.000000")


if __name__ == "__main__":
	unittest.main()
