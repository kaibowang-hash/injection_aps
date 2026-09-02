from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from injection_aps.api import app


class TestPermissionAccessCache(unittest.TestCase):
	def test_permission_filtered_rows_do_not_bypass_custom_document_hook(self):
		row = frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer=None,
			planning_run="RUN-1",
			item_code="ITEM-1",
			sales_order=None,
			sales_order_item=None,
			plant_floor=None,
			net_requirement=None,
			demand_commitment=None,
		)
		cache = {}
		with (
			patch.object(app.frappe, "session", frappe._dict(user="planner@example.com")),
			patch.object(app, "_has_document_permission_hook", return_value=True),
		):
			app._seed_permission_filtered_rows([row], "APS Schedule Result", cache)

		self.assertNotIn(("document", "APS Schedule Result", "RESULT-1", "read"), cache)
		self.assertEqual(cache[("scope", "APS Schedule Result", "RESULT-1")].item_code, "ITEM-1")

	def test_direct_access_is_resolved_in_one_permission_aware_list_query(self):
		cache = {}
		with (
			patch.object(app.frappe, "session", frappe._dict(user="planner@example.com")),
			patch.object(app, "_has_document_permission_hook", return_value=False),
			patch.object(app.frappe, "get_list", return_value=[frappe._dict(name="ITEM-1")]) as get_list,
		):
			app._prime_document_access("Item", ["ITEM-1", "ITEM-2"], cache)

		get_list.assert_called_once()
		self.assertTrue(cache[("document", "Item", "ITEM-1", "read")])
		self.assertFalse(cache[("document", "Item", "ITEM-2", "read")])

	def test_direct_access_falls_back_when_a_document_hook_exists(self):
		cache = {}

		def check(_doctype, name, **_kwargs):
			cache[("document", "Item", name, "read")] = name == "ITEM-1"
			return name == "ITEM-1"

		with (
			patch.object(app.frappe, "session", frappe._dict(user="planner@example.com")),
			patch.object(app, "_has_document_permission_hook", return_value=True),
			patch.object(app, "_has_document_access", side_effect=check) as has_access,
			patch.object(app.frappe, "get_list") as get_list,
		):
			app._prime_document_access("Item", ["ITEM-1", "ITEM-2"], cache)

		self.assertEqual(has_access.call_count, 2)
		get_list.assert_not_called()
		self.assertTrue(cache[("document", "Item", "ITEM-1", "read")])
		self.assertFalse(cache[("document", "Item", "ITEM-2", "read")])

	def test_scoped_batch_reuses_cached_rows_instead_of_treating_them_as_hidden(self):
		cache = {
			("document", "APS Schedule Result", "RESULT-1", "read"): True,
			("scope", "APS Schedule Result", "RESULT-1"): frappe._dict(
				company="COMPANY-1",
				planning_run="RUN-1",
			),
		}
		with (
			patch.object(app.frappe, "session", frappe._dict(user="planner@example.com")),
			patch.object(app, "_has_document_permission_hook", return_value=False),
			patch.object(app.frappe, "get_list") as get_list,
		):
			rows = app._get_permission_filtered_scoped_rows(
				"APS Schedule Result", ["RESULT-1"], cache
			)

		get_list.assert_not_called()
		self.assertEqual([row.name for row in rows], ["RESULT-1"])

	def test_accessible_document_filter_loads_complete_scope_before_priming(self):
		row = frappe._dict(name="RESULT-1", planned_qty=10)
		scope_row = frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			planning_run="RUN-1",
			item_code="ITEM-1",
		)
		cache = {}
		with (
			patch.object(app, "_seed_permission_filtered_rows"),
			patch.object(app, "_get_scoped_rows_for_priming", return_value=[scope_row]) as get_scope,
			patch.object(app, "_prime_scoped_document_dependencies") as prime,
			patch.object(app, "_has_scoped_document_access", return_value=True),
		):
			visible = app._filter_accessible_documents([row], "APS Schedule Result", cache)

		self.assertEqual(visible, [row])
		get_scope.assert_called_once_with("APS Schedule Result", {"RESULT-1"}, cache)
		prime.assert_called_once_with([scope_row], "APS Schedule Result", cache)

	def test_recent_run_cards_do_not_build_full_action_contexts(self):
		row = frappe._dict(
			name="RUN-1",
			company="COMPANY-1",
			plant_floor="FLOOR-1",
			selected_plant_floor_summary="FLOOR-1, FLOOR-2",
			planning_date="2026-09-01",
			horizon_days=14,
			status="Planned",
			approval_state="Pending",
			consistency_status="Valid",
			exception_count=0,
			modified="2026-09-01 12:00:00",
		)
		with (
			patch.object(app.planning.frappe, "get_list", return_value=[row]),
			patch.object(app.planning, "_build_planning_run_context") as build_context,
		):
			cards = app.planning.get_recent_run_contexts(limit=8)

		build_context.assert_not_called()
		self.assertEqual(cards[0]["selected_plant_floors"], ["FLOOR-1", "FLOOR-2"])
		self.assertEqual(cards[0]["status_label"], "Recalculated")

	def test_planning_run_context_primes_full_result_scope_with_one_shared_cache(self):
		result = frappe._dict(
			name="RESULT-1",
			company="COMPANY-1",
			customer="CUSTOMER-1",
			planning_run="RUN-1",
			item_code="ITEM-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			plant_floor="FLOOR-1",
			net_requirement="NR-1",
			planned_qty=10,
		)
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scoped_document_access"),
			patch.object(app.planning, "get_next_actions_for_context", return_value={}),
			patch.object(app.frappe, "get_list", return_value=[result]) as get_list,
			patch.object(app, "_prime_scoped_document_dependencies") as prime,
			patch.object(app, "_filter_accessible_documents", side_effect=lambda rows, _doctype, _cache: rows) as filter_rows,
			patch.object(app, "_sanitize_planning_run_context", side_effect=lambda context, **_kwargs: context),
		):
			app.get_next_actions_for_context.__wrapped__("APS Planning Run", "RUN-1")

		fields = get_list.call_args.kwargs["fields"]
		self.assertTrue(set(app.APS_SCOPED_DOCUMENT_FIELDS["APS Schedule Result"]).issubset(fields))
		self.assertIs(prime.call_args.args[2], filter_rows.call_args.args[2])

	def test_schedule_segment_filter_drops_hidden_campaign_item_and_child_links(self):
		rows = [
			frappe._dict(name="SEG-OK", production_campaign="CAMPAIGN-OK", primary_item_code="ITEM-OK"),
			frappe._dict(name="SEG-CAMPAIGN", production_campaign="CAMPAIGN-DENIED"),
			frappe._dict(name="SEG-ITEM", co_product_item_code="ITEM-DENIED"),
			frappe._dict(name="SEG-CHILD", linked_scheduling_item="SCHEDULING-ITEM-DENIED"),
		]

		def scoped_rows(doctype, names, _cache):
			if doctype == "APS Production Campaign":
				return [frappe._dict(name="CAMPAIGN-OK")]
			return []

		with (
			patch.object(app, "_prime_scoped_document_dependencies"),
			patch.object(app, "_filter_accessible_documents", side_effect=lambda values, *_args: values),
			patch.object(app, "_get_scoped_rows_for_priming", side_effect=scoped_rows) as get_scoped,
			patch.object(app, "_has_scoped_document_access", return_value=True),
			patch.object(app, "_prime_document_access") as prime_document,
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda _doctype, name, **_kwargs: name != "ITEM-DENIED",
			),
			patch.object(app, "_prime_linked_document_access") as prime_linked,
			patch.object(
				app,
				"_has_linked_document_access",
				side_effect=lambda _doctype, name, **_kwargs: name != "SCHEDULING-ITEM-DENIED",
			),
		):
			visible = app._filter_visible_schedule_segments(rows)

		self.assertEqual([row.name for row in visible], ["SEG-OK"])
		get_scoped.assert_any_call(
			"APS Schedule Segment",
			{"SEG-OK", "SEG-CAMPAIGN", "SEG-ITEM", "SEG-CHILD"},
			unittest.mock.ANY,
		)
		prime_document.assert_called_once_with("Item", {"ITEM-OK", "ITEM-DENIED"}, unittest.mock.ANY)
		prime_linked.assert_called_once_with(
			{"Scheduling Item": {"SCHEDULING-ITEM-DENIED"}},
			unittest.mock.ANY,
		)

	def test_gantt_result_filter_drops_hidden_bom_campaign_or_mold(self):
		rows = [
			frappe._dict(name="RESULT-OK", selected_bom="BOM-OK", primary_mould_reference="MOLD-OK"),
			frappe._dict(name="RESULT-BOM", selected_bom="BOM-DENIED"),
			frappe._dict(name="RESULT-CAMPAIGN", production_campaign="CAMPAIGN-DENIED"),
			frappe._dict(name="RESULT-MOLD", selected_moulds="MOLD-OK\nMOLD-DENIED"),
		]

		with (
			patch.object(app, "_prime_document_access"),
			patch.object(app, "_get_scoped_rows_for_priming", return_value=[]),
			patch.object(app, "_prime_scoped_document_dependencies"),
			patch.object(
				app,
				"_has_document_access",
				side_effect=lambda _doctype, name, **_kwargs: not str(name).endswith("DENIED"),
			),
		):
			visible = app._filter_visible_gantt_results(rows)

		self.assertEqual([row.name for row in visible], ["RESULT-OK"])

	def test_progress_v2_api_filters_all_owner_rows_before_projection(self):
		response = {"rows": [], "projection": {}}
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_scope_access"),
			patch.object(app.v2_flags, "is_v2_enabled", return_value=True),
			patch.object(app.progress_v2, "get_progress_detail", return_value=response) as get_progress,
			patch.object(app, "_sanitize_progress_v2_response", side_effect=lambda value, **_kwargs: value),
		):
			result = app.get_customer_schedule_progress_v2.__wrapped__(company="COMPANY-1")

		self.assertIs(result, response)
		for fieldname in (
			"commitment_access_filter",
			"result_access_filter",
			"segment_access_filter",
		):
			self.assertTrue(callable(get_progress.call_args.kwargs[fieldname]))


if __name__ == "__main__":
	unittest.main()
