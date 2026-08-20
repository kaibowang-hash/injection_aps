from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import frappe

from injection_aps.services import demand_admission, demand_ledger, planning, run_preparation


APP_ROOT = Path(__file__).resolve().parents[1]


class TestAdmissionGuidedFlow(unittest.TestCase):
	def setUp(self):
		frappe.local.flags = frappe._dict(in_test=True)
		frappe.local.session = frappe._dict(user="pmc@example.com")
		frappe.local.db = MagicMock()
		translation = patch.object(
			demand_admission,
			"_",
			side_effect=lambda message, *args, **kwargs: message,
		)
		translation.start()
		self.addCleanup(translation.stop)

	def test_planning_run_schema_tracks_confirmation_and_calculation_fingerprints(self):
		data = json.loads(
			(
				APP_ROOT
				/ "injection_aps/doctype/aps_planning_run/aps_planning_run.json"
			).read_text(encoding="utf-8")
		)
		fields = {row["fieldname"] for row in data["fields"]}
		self.assertTrue(
			{
				"planning_customer_filter",
				"planning_item_filter",
				"admission_confirmed_fingerprint",
				"admission_confirmed_by",
				"admission_confirmed_on",
				"admission_strategy",
				"admission_decision_reason",
				"planned_admission_fingerprint",
			}.issubset(fields)
		)

	def test_ui_connects_import_net_admission_and_targeted_recalculation(self):
		schedule = (
			APP_ROOT / "injection_aps/page/aps_schedule_console/aps_schedule_console.js"
		).read_text(encoding="utf-8")
		net = (
			APP_ROOT / "injection_aps/page/aps_net_requirement_workbench/aps_net_requirement_workbench.js"
		).read_text(encoding="utf-8")
		admission = (
			APP_ROOT / "injection_aps/page/aps_demand_admission_workbench/aps_demand_admission_workbench.js"
		).read_text(encoding="utf-8")
		run_console = (
			APP_ROOT / "injection_aps/page/aps_run_console/aps_run_console.js"
		).read_text(encoding="utf-8")
		self.assertIn("Continue to net requirement review", schedule)
		self.assertIn("create_trial_run_for_admission", net)
		self.assertIn("response.next_route", net)
		self.assertIn("result.next_route", admission)
		self.assertIn('get_url_arg("run_name")', run_console)
		self.assertIn("from_admission", run_console)

	def test_optional_projection_uses_exact_confirmed_quantity_and_commitment_lineage(self):
		run = frappe._dict(
			name="RUN-1",
			demand_horizon_end_date="2026-08-31",
			planning_date="2026-08-21",
		)
		commitment = frappe._dict(
			name="COM-1",
			customer="CUST-1",
			item_code="ITEM-1",
			admission="ADM-1",
			admission_class="P1",
			newly_planned_qty=37.5,
		)
		with (
			patch.object(demand_ledger.frappe, "get_doc", return_value=run),
			patch.object(demand_ledger.frappe, "get_all", return_value=[commitment]) as get_all,
			patch.object(demand_ledger, "_", side_effect=lambda message, *args, **kwargs: message),
		):
			rows = demand_ledger.get_selected_optional_planning_rows(
				"RUN-1", customer="CUST-1", item_code="ITEM-1"
			)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].planning_qty, 37.5)
		self.assertEqual(rows[0].net_requirement_qty, 37.5)
		self.assertEqual(rows[0].demand_commitment, "COM-1")
		self.assertEqual(rows[0].source_doctype, "APS Demand Commitment")
		self.assertEqual(str(rows[0].demand_date), "2026-08-31")
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["customer"], "CUST-1")
		self.assertEqual(filters["item_code"], "ITEM-1")
		self.assertEqual(filters["newly_planned_qty"][0], ">")

	def test_zero_optional_quantity_is_saved_as_an_explicit_exclusion(self):
		run = frappe._dict(
			name="RUN-1",
			status="Draft",
			approval_state="Pending",
			planned_admission_fingerprint="",
		)
		row = {
			"name": "ADM-1",
			"admission_class": "P1",
			"candidate_qty": 25,
			"recommended_qty": 25,
			"selected_qty": 0,
			"input_fingerprint": "INPUT-1",
		}
		current = {
			"planning_run": "RUN-1",
			"demand_baseline_fingerprint": "BASE-1",
			"admission_fingerprint": "FP-1",
			"rows": [dict(row)],
		}
		updated = {**current, "rows": [dict(row)]}
		final = {**updated, "summary": demand_admission._summarize(updated["rows"])}
		with (
			patch.object(demand_admission, "is_v2_enabled", return_value=True),
			patch.object(demand_admission, "_lock_run_scope"),
			patch.object(demand_admission.frappe, "get_doc", return_value=run),
			patch.object(demand_admission, "now_datetime", return_value="2026-08-21 09:00:00"),
			patch.object(
				demand_admission,
				"get_demand_admission_candidates",
				side_effect=[current, updated, final],
			),
			patch.object(demand_admission, "_set_admission_values") as set_admission,
			patch.object(demand_ledger, "sync_optional_admission_commitments"),
			patch.object(demand_admission.frappe.db, "set_value") as set_run,
		):
			result = demand_admission.save_demand_admission_decisions(
				"RUN-1",
				[{"name": "ADM-1", "selected_qty": 0}],
				expected_fingerprint="FP-1",
				reason="本批次不提前生产",
				strategy="conservative",
			)

		self.assertEqual(result["recalculation_required"], 1)
		self.assertTrue(
			any(
				item.args[1].get("status") == "Excluded"
				for item in set_admission.call_args_list
				if len(item.args) > 1
			)
		)
		run_values = set_run.call_args.args[2]
		self.assertEqual(run_values["admission_strategy"], "conservative")
		self.assertEqual(run_values["status"], "Draft")

	def test_api_validation_rejects_p0_changes_negative_and_excess_quantities(self):
		rows = {
			"P0": {"name": "P0", "admission_class": "P0", "candidate_qty": 10, "selected_qty": 10},
			"P1": {"name": "P1", "admission_class": "P1", "candidate_qty": 20, "selected_qty": 0},
		}
		with patch.object(
			demand_admission.frappe,
			"throw",
			side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(ValueError(message)),
		):
			with self.assertRaisesRegex(ValueError, "mandatory"):
				demand_admission._validate_decisions(rows, [{"name": "P0", "selected_qty": 9}])
			with self.assertRaisesRegex(ValueError, "negative"):
				demand_admission._validate_decisions(rows, [{"name": "P1", "selected_qty": -1}])
			with self.assertRaisesRegex(ValueError, "exceed"):
				demand_admission._validate_decisions(rows, [{"name": "P1", "selected_qty": 21}])

	def test_stale_admission_fingerprint_is_rejected_before_preview(self):
		with (
			patch.object(
				demand_admission,
				"get_demand_admission_candidates",
				return_value={"admission_fingerprint": "CURRENT", "rows": []},
			),
			patch.object(
				demand_admission.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(ValueError(message)),
			),
		):
			with self.assertRaisesRegex(ValueError, "changed"):
				demand_admission.preview_admission_impact(
					"RUN-1", [], expected_fingerprint="STALE", strategy="standard"
				)

	def test_capacity_guard_rejects_a_confirmed_but_unrecalculated_admission(self):
		with (
			patch.object(
				demand_admission,
				"get_admission_state",
				return_value={"legacy_unchecked": 0, "confirmed": 1, "recalculation_required": 1},
			),
			patch.object(
				demand_admission.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(ValueError(message)),
			),
		):
			with self.assertRaisesRegex(ValueError, "Recalculate"):
				demand_admission.require_admission_ready("RUN-1", require_planned=True)

	def test_run_context_prioritizes_admission_then_recalculation(self):
		doc = frappe._dict(
			name="RUN-1",
			company="COMPANY",
			status="Draft",
			approval_state="Pending",
			capacity_balance_status="Not Analyzed",
			capacity_balance_analysis_json="",
			consistency_status="Unchecked",
			exception_count=0,
			horizon_days=14,
			planning_date="2026-08-21",
			modified="2026-08-21 10:00:00",
			plant_floor="FLOOR-1",
		)
		base_patches = (
			patch.object(planning.frappe.db, "exists", return_value=False),
			patch("injection_aps.services.v2_flags.is_v2_enabled", return_value=True),
			patch.object(planning, "_get_run_selected_plant_floors", return_value=["FLOOR-1"]),
			patch.object(planning.consistency, "get_run_quantity_summary", return_value={}),
		)
		with base_patches[0], base_patches[1], base_patches[2], base_patches[3], patch.object(
			demand_admission,
			"get_admission_state",
			return_value={"baseline_ready": 1, "confirmed": 0, "recalculation_required": 0},
		):
			context = planning._build_planning_run_context(doc)
		self.assertEqual(context["next_step"], "Demand Admission")

		with (
			patch.object(planning.frappe.db, "exists", return_value=False),
			patch("injection_aps.services.v2_flags.is_v2_enabled", return_value=True),
			patch.object(planning, "_get_run_selected_plant_floors", return_value=["FLOOR-1"]),
			patch.object(planning.consistency, "get_run_quantity_summary", return_value={}),
			patch.object(
				demand_admission,
				"get_admission_state",
				return_value={"baseline_ready": 1, "confirmed": 1, "recalculation_required": 1},
			),
		):
			context = planning._build_planning_run_context(doc)
		self.assertEqual(context["next_step"], "Recalculate")

	def test_p0_only_draft_skips_the_admission_page(self):
		run = frappe._dict(
			name="RUN-P0",
			horizon_start="2026-08-21 09:00:00",
			planning_date="2026-08-21",
			horizon_days=14,
		)
		run.insert = MagicMock()
		with (
			patch.object(run_preparation, "is_v2_enabled", return_value=True),
			patch.object(run_preparation.frappe, "get_doc", return_value=run),
			patch.object(run_preparation, "today", return_value="2026-08-21"),
			patch.object(run_preparation, "now_datetime", return_value="2026-08-21 09:00:00"),
			patch("injection_aps.services.planning.get_settings_dict", return_value={"planning_horizon_days": 14}),
			patch("injection_aps.services.planning._normalize_existing_work_order_policy", return_value="Include"),
			patch("injection_aps.services.planning._normalize_selected_plant_floors", return_value=["FLOOR-1"]),
			patch("injection_aps.services.planning._apply_selected_plant_floors_to_run"),
			patch("injection_aps.services.planning._lock_company_for_aps_planning"),
			patch(
				"injection_aps.services.demand_ledger.prepare_run_demand_baseline",
				return_value={
					"admission": {
						"summary": {"p0_count": 4, "optional_count": 0},
						"admission_state": {"optional_row_count": 0, "confirmed": 1},
					}
				},
			),
		):
			result = run_preparation.create_trial_run_for_admission(
				company="COMPANY",
				plant_floor="FLOOR-1",
				existing_work_order_policy="Include",
			)

		self.assertEqual(result["auto_skipped"], 1)
		self.assertIn("aps-run-console?run_name=RUN-P0", result["next_route"])
		self.assertEqual(str(run.demand_horizon_start_date), "2026-08-21")
		self.assertEqual(str(run.demand_horizon_end_date), "2026-09-03")
		self.assertTrue(run.horizon_end)


if __name__ == "__main__":
	unittest.main()
