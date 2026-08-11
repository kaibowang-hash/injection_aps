from __future__ import annotations

import json
import inspect
import unittest
from pathlib import Path

from injection_aps.services import permissions


APP_ROOT = Path(__file__).resolve().parents[1]


class TestChangeImpactUIStatic(unittest.TestCase):
	def test_change_impact_page_supports_selection_summary_and_safe_routes(self):
		page = APP_ROOT / "injection_aps/page/aps_change_impact_center"
		definition = json.loads((page / "aps_change_impact_center.json").read_text(encoding="utf-8"))
		self.assertEqual(definition["page_name"], "aps-change-impact-center")
		source = (page / "aps_change_impact_center.js").read_text(encoding="utf-8")
		for marker in (
			'data-change-select=',
			'data-select-analyzable=',
			'get_change_impact_center_data',
			'batch_analyze_change_requests',
			'buildSummary(rows)',
			'openImpact(name)',
			'frappe.set_route("Form", "APS Change Request", name)',
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)
		for workflow_method in (
			"injection_aps.api.app.analyze_change_request_impact",
			"injection_aps.api.app.confirm_change_request",
			"injection_aps.api.app.approve_change_request",
			"injection_aps.api.app.reject_change_request",
			"injection_aps.api.app.apply_change_request",
		):
			self.assertIn(workflow_method, source)

	def test_capacity_actions_match_backend_plan_roles(self):
		shared = (APP_ROOT / "public/js/injection_aps_shared.js").read_text(encoding="utf-8")
		expected = '["System Manager", "GMC", "PMC", "Manufacturing Manager"]'
		for action in (
			"analyze_capacity_balance",
			"confirm_capacity_balance",
			"apply_capacity_balance",
		):
			with self.subTest(action=action):
				self.assertIn(f"{action}: {expected}", shared)

	def test_change_impact_page_is_registered_for_all_aps_read_roles(self):
		permissions = (APP_ROOT / "services/permissions.py").read_text(encoding="utf-8")
		self.assertIn('"aps-change-impact-center"', permissions)
		definition = json.loads(
			(
				APP_ROOT
				/ "injection_aps/page/aps_change_impact_center/aps_change_impact_center.json"
			).read_text(encoding="utf-8")
		)
		self.assertEqual(
			{entry["role"] for entry in definition["roles"]},
			{
				"System Manager",
				"GMC",
				"PMC",
				"Manufacturing Manager",
				"Manufacturing User",
				"Sales Manager",
				"Sales User",
				"Purchase Manager",
				"Purchase User",
				"Stock Manager",
				"Stock User",
			},
		)

	def test_install_role_sync_matches_workspace_stock_access(self):
		roles = set(permissions.APS_PAGE_ROLE_MAP["aps-change-impact-center"])
		self.assertIn("Stock Manager", roles)
		self.assertIn("Stock User", roles)
		self.assertEqual(
			roles,
			{
				"System Manager",
				"GMC",
				"PMC",
				"Sales Manager",
				"Sales User",
				"Purchase Manager",
				"Purchase User",
				"Manufacturing Manager",
				"Manufacturing User",
				"Stock Manager",
				"Stock User",
			},
		)
		sync_source = inspect.getsource(permissions.ensure_page_and_workspace_roles)
		self.assertIn("APS_PAGE_ROLE_MAP.get(page_name, APS_READ_ROLES)", sync_source)


if __name__ == "__main__":
	unittest.main()
