from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from injection_aps.api import app


APP_ROOT = Path(__file__).resolve().parents[1]


class TestPhase2Contracts(unittest.TestCase):
	def setUp(self):
		app.frappe.local.flags = app.frappe._dict(in_test=True)
		translation = patch.object(app, "_", side_effect=lambda message, *args, **kwargs: message)
		translation.start()
		self.addCleanup(translation.stop)

	def test_phase2_doctypes_have_unique_owner_and_idempotency_guards(self):
		for doctype, filename in (
			("APS Demand Commitment", "aps_demand_commitment/aps_demand_commitment.json"),
			("APS Demand Admission", "aps_demand_admission/aps_demand_admission.json"),
			("APS Stock Coverage Allocation", "aps_stock_coverage_allocation/aps_stock_coverage_allocation.json"),
		):
			with self.subTest(doctype=doctype):
				data = json.loads((APP_ROOT / "injection_aps" / "doctype" / filename).read_text(encoding="utf-8"))
				fields = {row["fieldname"]: row for row in data["fields"]}
				self.assertEqual(fields["idempotency_key"]["unique"], 1)
				self.assertEqual(data["read_only"], 1)
		commitment = json.loads(
			(APP_ROOT / "injection_aps" / "doctype" / "aps_demand_commitment" / "aps_demand_commitment.json").read_text(encoding="utf-8")
		)
		fields = {row["fieldname"]: row for row in commitment["fields"]}
		self.assertEqual(fields["active_owner_key"]["unique"], 1)

	def test_admission_page_locks_p0_and_supports_batch_decisions(self):
		source = (
			APP_ROOT / "injection_aps" / "page" / "aps_demand_admission_workbench" / "aps_demand_admission_workbench.js"
		).read_text(encoding="utf-8")
		self.assertIn('P0 is mandatory and locked.', source)
		self.assertIn('this.decisionMap = new Map()', source)
		self.assertIn('data-select-filtered="1"', source)
		self.assertIn('applyBulkAction(action)', source)
		self.assertIn('preview_admission_impact', source)
		self.assertIn('Recalculate this Run before continuing.', source)
		self.assertIn('get_demand_admission_candidates', source)

	def test_prepare_api_requires_plan_role_and_run_write_scope(self):
		db = MagicMock()
		db.get_value.return_value = app.frappe._dict(
			company="COMPANY-1",
			planning_customer_filter="CUSTOMER-1",
			planning_item_filter="ITEM-1",
		)
		with (
			patch.object(app, "_require_plan_access") as require_plan,
			patch.object(app, "_require_scoped_document_access") as require_scope,
			patch.object(app.frappe, "db", db),
			patch.object(app, "_require_company_rebuild_scope") as require_company,
			patch.object(app, "_require_document_access") as require_document,
			patch.object(app.demand_ledger, "prepare_run_demand_baseline", return_value={"planning_run": "RUN-1"}) as service,
		):
			result = app.prepare_run_demand_baseline("RUN-1", "abc")
		require_plan.assert_called_once_with()
		require_scope.assert_called_once_with(
			"APS Planning Run", "RUN-1", ptype="write", linked_run_ptype="write"
		)
		require_company.assert_called_once_with("COMPANY-1")
		require_document.assert_any_call("Customer", "CUSTOMER-1", ptype="read")
		require_document.assert_any_call("Item", "ITEM-1", ptype="read")
		service.assert_called_once_with("RUN-1", expected_fingerprint="abc")
		self.assertEqual(result["planning_run"], "RUN-1")

	def test_create_admission_draft_api_reuses_plan_and_scope_guards(self):
		with (
			patch.object(app, "_require_plan_access") as require_plan,
			patch.object(app, "_require_explicit_company", return_value="COMPANY-1"),
			patch.object(app, "_require_scope_access") as require_scope,
			patch.object(app, "_require_company_rebuild_scope") as require_company,
			patch.object(app, "_require_planning_reference_access") as require_references,
			patch.object(
				app.run_preparation,
				"create_trial_run_for_admission",
				return_value={"run": "RUN-1", "next_route": "aps-demand-admission-workbench?run_name=RUN-1"},
			) as service,
		):
			result = app.create_trial_run_for_admission(
				company="COMPANY-1",
				plant_floor="FLOOR-1",
				item_code="ITEM-1",
				customer="CUSTOMER-1",
				horizon_days=14,
				existing_work_order_policy="Include",
			)

		require_plan.assert_called_once_with()
		require_scope.assert_called_once_with(company="COMPANY-1", customer="CUSTOMER-1")
		require_company.assert_called_once_with("COMPANY-1")
		require_references.assert_called_once_with(
			item_code="ITEM-1", plant_floor="FLOOR-1", plant_floors=None
		)
		service.assert_called_once_with(
			company="COMPANY-1",
			plant_floor="FLOOR-1",
			plant_floors=None,
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			horizon_days=14,
			existing_work_order_policy="Include",
		)
		self.assertEqual(result["run"], "RUN-1")

	def test_save_admission_api_rejects_non_array_payload_before_service(self):
		with (
			patch.object(app, "_require_plan_access"),
			patch.object(app, "_require_complete_run_mutation_scope"),
			patch.object(app.demand_admission, "save_demand_admission_decisions") as service,
			patch.object(
				app.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(ValueError(message)),
			),
		):
			with self.assertRaises(ValueError):
				app.save_demand_admission_decisions(
					planning_run="RUN-1", decisions='{"name":"ADM-1"}', input_fingerprint="abc", reason="PMC"
				)
		service.assert_not_called()

	def test_flag_off_candidate_api_returns_reason_without_reading_phase2_tables(self):
		with (
			patch.object(app, "_require_read_access"),
			patch.object(app, "_require_complete_run_mutation_scope"),
			patch.object(app.v2_flags, "is_v2_enabled", return_value=False),
			patch.object(app.demand_ledger, "get_run_demand_baseline") as baseline,
		):
			result = app.get_demand_admission_candidates("RUN-1")
		self.assertFalse(result["available"])
		self.assertIn("Legacy", result["reason"])
		baseline.assert_not_called()


if __name__ == "__main__":
	unittest.main()
