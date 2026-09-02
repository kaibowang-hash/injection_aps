from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from injection_aps.services import planning, schedule_revision


class TestPhase1ScheduleRevision(unittest.TestCase):
	def _active(self, identity, item="ITEM-1", qty=100, date="2026-08-20", **values):
		return {
			"name": f"ROW-{identity}",
			"parent": "SCHEDULE-OLD",
			"demand_identity": identity,
			"item_code": item,
			"customer_part_no": values.pop("customer_part_no", "PART-1"),
			"sales_order": values.pop("sales_order", "SO-1"),
			"schedule_date": date,
			"qty": qty,
			"effective_qty": qty,
			"produced_qty": 0,
			"delivered_qty": 0,
			**values,
		}

	def test_mode_recommendation_requires_confirmation(self):
		initial = schedule_revision.recommend_mode([], [{"item_code": "ITEM-1"}])
		self.assertEqual(initial["recommended_mode"], "Full Replacement")
		self.assertTrue(initial["requires_user_confirmation"])

		previous = [self._active("ID-1"), self._active("ID-2", item="ITEM-2")]
		partial = schedule_revision.recommend_mode(
			previous,
			[{"demand_identity": "ID-1", "item_code": "ITEM-1"}],
		)
		self.assertEqual(partial["recommended_mode"], "Partial Revision")

		incremental = schedule_revision.recommend_mode(
			previous,
			[{"item_code": "ITEM-3", "customer_part_no": "PART-3"}],
		)
		self.assertEqual(incremental["recommended_mode"], "Incremental Demand")
		self.assertEqual(incremental["confidence"], "Low")

	def test_partial_revision_replaces_touched_and_retains_omitted_identity(self):
		previous = [self._active("ID-1"), self._active("ID-2", item="ITEM-2", qty=40)]
		incoming = [{**self._active("ID-1", qty=80), "previous_row": previous[0]}]
		plan = schedule_revision._build_revision_plan(previous, incoming, "Partial Revision")

		by_identity = {row["demand_identity"]: row for row in plan["effective_rows"]}
		self.assertEqual(by_identity["ID-1"]["effective_qty"], 80)
		self.assertEqual(by_identity["ID-1"]["revision_action"], "Changed")
		self.assertEqual(by_identity["ID-2"]["effective_qty"], 40)
		self.assertEqual(by_identity["ID-2"]["revision_action"], "Retained")
		self.assertEqual(sum(row["effective_qty"] for row in plan["effective_rows"]), 120)

	def test_full_replacement_records_cancelled_excess_without_rejecting_true_reduction(self):
		previous = [
			self._active("ID-1", qty=100, produced_qty=70, delivered_qty=20),
			self._active("ID-2", item="ITEM-2", qty=40, produced_qty=15),
		]
		incoming = [{**self._active("ID-1", qty=30), "previous_row": previous[0]}]
		plan = schedule_revision._build_revision_plan(previous, incoming, "Full Replacement")
		by_identity = {row["demand_identity"]: row for row in plan["rows"]}

		self.assertEqual(by_identity["ID-1"]["effective_qty"], 30)
		self.assertEqual(by_identity["ID-1"]["executed_floor_qty"], 70)
		self.assertEqual(by_identity["ID-1"]["excess_qty"], 40)
		self.assertEqual(by_identity["ID-1"]["open_revised_qty"], 10)
		self.assertEqual(by_identity["ID-2"]["effective_qty"], 0)
		self.assertEqual(by_identity["ID-2"]["excess_qty"], 15)
		self.assertEqual(by_identity["ID-2"]["revision_action"], "Cancelled")

	def test_incremental_overlap_creates_independent_demand(self):
		previous = [self._active("ID-1", qty=100)]
		incoming, issues = schedule_revision._resolve_incoming_identities(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			schedule_scope="SCOPE-1",
			previous_rows=previous,
			incoming_rows=[{"item_code": "ITEM-1", "customer_part_no": "PART-1", "sales_order": "SO-1", "schedule_date": "2026-08-20", "qty": 25}],
			revision_mode="Incremental Demand",
		)
		self.assertFalse(issues)
		self.assertIsNone(incoming[0]["demand_identity"])
		self.assertEqual(incoming[0]["identity_match_method"], "Incremental Identity")
		plan = schedule_revision._build_revision_plan(previous, incoming, "Incremental Demand")
		self.assertEqual(len(plan["effective_rows"]), 1)
		self.assertEqual(plan["effective_rows"][0]["delta_qty"], 25)

	def test_ambiguous_identity_is_reported_and_never_guessed(self):
		previous = [self._active("ID-1"), self._active("ID-2")]
		incoming, issues = schedule_revision._resolve_incoming_identities(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			schedule_scope="SCOPE-1",
			previous_rows=previous,
			incoming_rows=[{"item_code": "ITEM-1", "customer_part_no": "PART-1", "sales_order": "SO-1", "schedule_date": "2026-08-20", "qty": 60}],
			revision_mode="Partial Revision",
		)
		self.assertIsNone(incoming[0]["demand_identity"])
		self.assertEqual(issues[0]["reason_code"], "AMBIGUOUS_IDENTITY")
		self.assertEqual({row["demand_identity"] for row in issues[0]["candidates"]}, {"ID-1", "ID-2"})

	def test_manual_identity_requires_an_audit_reason(self):
		previous = [self._active("ID-1")]
		_rows, issues = schedule_revision._resolve_incoming_identities(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			schedule_scope="SCOPE-1",
			previous_rows=previous,
			incoming_rows=[{"demand_identity": "ID-1", "item_code": "ITEM-1", "qty": 80}],
			revision_mode="Partial Revision",
		)
		self.assertIn("MANUAL_RESOLUTION_REASON_REQUIRED", {row["reason_code"] for row in issues})

	def test_request_fingerprint_is_stable_while_concurrency_token_is_separate(self):
		values = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"schedule_scope": "SCOPE-1",
			"version_no": "V1",
			"revision_mode": "Incremental Demand",
			"source_type": "Customer Delivery Schedule",
			"source_contract": None,
			"rows": [{"item_code": "ITEM-1", "qty": 25}],
		}
		first = schedule_revision._build_revision_fingerprint(active_state_token="STATE-A", **values)
		second = schedule_revision._build_revision_fingerprint(active_state_token="STATE-B", **values)
		self.assertEqual(first, second)

	def test_legacy_import_contract_ignores_v2_lineage_until_flag_is_enabled(self):
		base_row = {
			"sales_order": "SO-1",
			"item_code": "ITEM-1",
			"customer_part_no": "PART-1",
			"schedule_date": "2026-08-20",
			"qty": 25,
		}
		lineage_row = {
			**base_row,
			"external_line_reference": "CUSTOMER-LINE-1",
			"demand_identity": "DEMAND-IDENTITY-1",
			"identity_resolution_reason": "Confirmed by planner",
		}
		fingerprint_args = {
			"customer": "CUSTOMER-1",
			"company": "COMPANY-1",
			"version_no": "V1",
			"schedule_scope": "DAILY",
			"import_strategy": "Replace Scope",
			"source_type": "Customer Delivery Schedule",
		}

		with patch("injection_aps.services.v2_flags.is_v2_enabled", return_value=False):
			legacy_snapshot = planning._build_schedule_source_snapshot_rows([lineage_row])[0]
			legacy_base = planning._build_schedule_import_fingerprint(**fingerprint_args, rows=[base_row])
			legacy_with_lineage = planning._build_schedule_import_fingerprint(
				**fingerprint_args, rows=[lineage_row]
			)

		self.assertNotIn("external_line_reference", legacy_snapshot)
		self.assertNotIn("demand_identity", legacy_snapshot)
		self.assertNotIn("identity_resolution_reason", legacy_snapshot)
		self.assertEqual(legacy_base, legacy_with_lineage)

		with patch("injection_aps.services.v2_flags.is_v2_enabled", return_value=True):
			v2_snapshot = planning._build_schedule_source_snapshot_rows([lineage_row])[0]
			v2_base = planning._build_schedule_import_fingerprint(**fingerprint_args, rows=[base_row])
			v2_with_lineage = planning._build_schedule_import_fingerprint(
				**fingerprint_args, rows=[lineage_row]
			)

		self.assertEqual(v2_snapshot["external_line_reference"], "CUSTOMER-LINE-1")
		self.assertEqual(v2_snapshot["demand_identity"], "DEMAND-IDENTITY-1")
		self.assertEqual(v2_snapshot["identity_resolution_reason"], "Confirmed by planner")
		self.assertNotEqual(v2_base, v2_with_lineage)

	def test_backfill_reports_duplicate_external_line_instead_of_merging(self):
		base = {
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"schedule_scope": "SCOPE-1",
			"item_code": "ITEM-1",
			"external_line_reference": "CUSTOMER-LINE-1",
			"parent": "SCHEDULE-1",
		}
		groups = schedule_revision._find_backfill_ambiguities(
			[{**base, "name": "ROW-1"}, {**base, "name": "ROW-2"}]
		)
		self.assertEqual(len(groups), 1)
		self.assertEqual(groups[0]["reason_code"], "DUPLICATE_EXTERNAL_LINE")
		self.assertEqual(groups[0]["schedule_items"], ["ROW-1", "ROW-2"])

	def test_no_sales_order_is_allowed_only_when_v2_is_enabled(self):
		result = {
			"demand_source": "Customer Delivery Schedule",
			"item_code": "ITEM-1",
			"demand_source_snapshot_json": "[]",
		}
		with patch("injection_aps.services.v2_flags.is_v2_enabled", return_value=False):
			legacy = planning._get_result_sales_order_lineage(result)
		with patch("injection_aps.services.v2_flags.is_v2_enabled", return_value=True):
			v2 = planning._get_result_sales_order_lineage(result)
		self.assertTrue(legacy["blocking_reason"])
		self.assertIsNone(v2["blocking_reason"])
		self.assertIsNone(v2["sales_order"])
		self.assertIsNone(v2["sales_order_item"])


if __name__ == "__main__":
	unittest.main()
