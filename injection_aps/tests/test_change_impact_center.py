from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

import frappe

from injection_aps.api import app


class TestChangeImpactCenterAPI(unittest.TestCase):
	def setUp(self):
		self.original_flags = getattr(frappe.local, "flags", None)
		frappe.local.flags = frappe._dict(in_test=True)

	def tearDown(self):
		if self.original_flags is None:
			del frappe.local.flags
		else:
			frappe.local.flags = self.original_flags

	def test_compact_row_removes_raw_json_and_builds_bounded_preview(self):
		affected_orders = [
			{
				"affected_order": f"SO-{index}",
				"customer": "CUST-1",
				"item_code": "ITEM-1",
				"delayed_qty": index,
				"private_payload": "must not leak",
			}
			for index in range(12)
		]
		row = app._compact_change_impact_row(
			{
				"name": "APS-CR-1",
				"analysis_fingerprint": "fingerprint",
				"retained_excess_qty": 5,
				"impact_json": frappe.as_json(
					{
						"affected_orders": affected_orders,
						"affected_customers": ["CUST-1", "CUST-1"],
						"freeze_conflicts": [{"name": "F-1"}],
					}
				),
				"proposal_json": frappe.as_json({"allowed": 0, "segment_actions": [{}, {}]}),
			}
		)
		self.assertNotIn("impact_json", row)
		self.assertNotIn("proposal_json", row)
		self.assertEqual(row["affected_order_count"], 12)
		self.assertEqual(len(row["affected_orders_preview"]), 10)
		self.assertEqual(row["affected_orders_truncated"], 2)
		self.assertNotIn("private_payload", row["affected_orders_preview"][0])
		self.assertEqual(row["affected_customers"], ["CUST-1"])
		self.assertEqual(row["freeze_conflict_count"], 1)
		self.assertEqual(row["segment_action_count"], 2)
		self.assertEqual(row["blocking"], 1)

	def test_center_read_uses_permission_aware_get_list(self):
		source_rows = [
			{
				"name": "APS-CR-1",
				"company": "ACME",
				"customer": None,
				"planning_run": None,
				"item_code": None,
				"plant_floor": None,
				"status": "Analyzed",
				"analysis_fingerprint": "fingerprint",
				"impact_json": "{}",
				"proposal_json": "{}",
			}
		]
		with (
			patch.object(app, "_require_read_access") as require_read,
			patch.object(app, "_require_scope_access") as require_scope,
			patch.object(app.frappe, "get_list", return_value=source_rows) as get_list,
			patch.object(app.frappe, "get_all", return_value=source_rows) as get_all,
		):
			result = app.get_change_impact_center_data(company="ACME", limit=50)
		require_read.assert_called_once_with()
		require_scope.assert_called_once_with(company="ACME", customer=None, planning_run=None)
		self.assertEqual(get_list.call_args.args[0], "APS Change Request")
		self.assertEqual(get_list.call_args.kwargs["filters"], {"company": "ACME"})
		self.assertNotIn("impact_json", get_list.call_args.kwargs["fields"])
		self.assertEqual(get_all.call_args.kwargs["filters"], {"name": ("in", ["APS-CR-1"])})
		self.assertEqual(result["rows"][0]["name"], "APS-CR-1")
		self.assertEqual(result["summary"]["analyzed_count"], 1)

	def test_batch_analysis_sorts_locks_and_releases_after_full_success(self):
		database = MagicMock()
		database.sql.return_value = [
			{"name": "APS-CR-1", "status": "Draft"},
			{"name": "APS-CR-2", "status": "Analyzed"},
		]
		responses = {
			"APS-CR-1": {
				"change_request": "APS-CR-1",
				"status": "Analyzed",
				"analysis_revision": 1,
				"allowed": 1,
			},
			"APS-CR-2": {
				"change_request": "APS-CR-2",
				"status": "Analyzed",
				"analysis_revision": 2,
				"allowed": 0,
			},
		}
		with (
			patch.object(app, "_require_demand_access") as require_demand,
			patch.object(app.frappe, "db", database),
			patch.object(app.frappe, "generate_hash", return_value="batch1234"),
			patch.object(app.frappe, "has_permission", return_value=True) as has_permission,
			patch.object(
				app.planning,
				"analyze_change_request_impact",
				side_effect=lambda name: responses[name],
			) as analyze,
		):
			result = app.batch_analyze_change_requests(["APS-CR-2", "APS-CR-1", "APS-CR-2"])

		require_demand.assert_called_once_with()
		database.savepoint.assert_called_once_with("aps_change_batch_analyze_batch1234")
		self.assertEqual(database.sql.call_args.args[1], ("APS-CR-1", "APS-CR-2"))
		self.assertIn("order by name", database.sql.call_args.args[0].lower())
		self.assertIn("for update", database.sql.call_args.args[0].lower())
		self.assertEqual(
			has_permission.call_args_list,
			[
				call("APS Change Request", ptype="write", doc="APS-CR-1"),
				call("APS Change Request", ptype="write", doc="APS-CR-2"),
			],
		)
		self.assertEqual([call.args[0] for call in analyze.call_args_list], ["APS-CR-1", "APS-CR-2"])
		database.release_savepoint.assert_called_once_with("aps_change_batch_analyze_batch1234")
		database.rollback.assert_not_called()
		self.assertEqual(result["analyzed_count"], 2)
		self.assertEqual(result["blocking_count"], 1)

	def test_batch_analysis_failure_rolls_back_every_request(self):
		database = MagicMock()
		database.sql.return_value = [
			{"name": "APS-CR-1", "status": "Draft"},
			{"name": "APS-CR-2", "status": "Draft"},
		]
		with (
			patch.object(app, "_require_demand_access"),
			patch.object(app.frappe, "db", database),
			patch.object(app.frappe, "generate_hash", return_value="rollback1"),
			patch.object(app.frappe, "has_permission", return_value=True),
			patch.object(
				app.planning,
				"analyze_change_request_impact",
				side_effect=[{"change_request": "APS-CR-1", "allowed": 1}, RuntimeError("analysis failed")],
			),
		):
			with self.assertRaisesRegex(RuntimeError, "analysis failed"):
				app.batch_analyze_change_requests(["APS-CR-1", "APS-CR-2"])
		database.rollback.assert_called_once_with(save_point="aps_change_batch_analyze_rollback1")
		database.release_savepoint.assert_not_called()

	def test_batch_analysis_refuses_approved_requests_before_analysis(self):
		database = MagicMock()
		database.sql.return_value = [{"name": "APS-CR-1", "status": "Approved"}]
		with (
			patch.object(app, "_require_demand_access"),
			patch.object(app.frappe, "db", database),
			patch.object(app.frappe, "generate_hash", return_value="approved1"),
			patch.object(app.frappe, "has_permission", return_value=True),
			patch.object(app.planning, "analyze_change_request_impact") as analyze,
			patch.object(app, "_", side_effect=lambda message, *args, **kwargs: message),
			patch.object(app.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				app.batch_analyze_change_requests(["APS-CR-1"])
		analyze.assert_not_called()
		database.rollback.assert_called_once_with(save_point="aps_change_batch_analyze_approved1")

	def test_batch_analysis_denies_one_document_without_write_permission_and_rolls_back(self):
		database = MagicMock()
		database.sql.return_value = [
			{"name": "APS-CR-1", "status": "Draft"},
			{"name": "APS-CR-2", "status": "Draft"},
		]
		with (
			patch.object(app, "_require_demand_access"),
			patch.object(app.frappe, "db", database),
			patch.object(app.frappe, "generate_hash", return_value="permission1"),
			patch.object(
				app.frappe,
				"has_permission",
				side_effect=lambda doctype, ptype, doc: doc != "APS-CR-2",
			) as has_permission,
			patch.object(app.planning, "analyze_change_request_impact") as analyze,
			patch.object(app, "_", side_effect=lambda message, *args, **kwargs: message),
			patch.object(app.frappe, "throw", side_effect=frappe.PermissionError),
		):
			with self.assertRaises(frappe.PermissionError):
				app.batch_analyze_change_requests(["APS-CR-2", "APS-CR-1"])
		self.assertEqual(
			has_permission.call_args_list,
			[
				call("APS Change Request", ptype="write", doc="APS-CR-1"),
				call("APS Change Request", ptype="write", doc="APS-CR-2"),
			],
		)
		analyze.assert_not_called()
		database.rollback.assert_called_once_with(save_point="aps_change_batch_analyze_permission1")
		database.release_savepoint.assert_not_called()

	def test_batch_selection_is_bounded_and_deduplicated(self):
		self.assertEqual(
			app._normalize_change_request_batch(["APS-CR-2", "APS-CR-1", "APS-CR-2"]),
			["APS-CR-1", "APS-CR-2"],
		)
		with (
			patch.object(app, "MAX_CHANGE_ANALYSIS_BATCH", 1),
			patch.object(app, "_", side_effect=lambda message, **kwargs: message),
			patch.object(app.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				app._normalize_change_request_batch(["APS-CR-1", "APS-CR-2"])


if __name__ == "__main__":
	unittest.main()
