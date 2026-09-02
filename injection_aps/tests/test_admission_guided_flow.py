from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import frappe

from injection_aps.services import (
	demand_admission,
	demand_ledger,
	planning,
	run_preparation,
	solver_orchestration,
)


APP_ROOT = Path(__file__).resolve().parents[1]


class TestAdmissionGuidedFlow(unittest.TestCase):
	def setUp(self):
		self.original_flags = getattr(frappe.local, "flags", None)
		self.original_session = getattr(frappe.local, "session", None)
		self.original_db = getattr(frappe.local, "db", None)
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

	def tearDown(self):
		frappe.local.flags = self.original_flags
		frappe.local.session = self.original_session
		frappe.local.db = self.original_db

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
			existing_work_order_policy="Include",
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
		self.assertEqual(rows[0].demand_source, "Forecast")
		self.assertEqual(str(rows[0].demand_date), "2026-08-31")
		baseline = json.loads(rows[0].fulfillment_baseline_json)
		self.assertEqual(baseline["version"], 4)
		self.assertEqual(baseline["net_requirement"]["planning_qty"], 37.5)
		self.assertEqual(baseline["sales_order_items"], [])
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["customer"], "CUST-1")
		self.assertEqual(filters["item_code"], "ITEM-1")
		self.assertEqual(filters["newly_planned_qty"][0], ">")

	def test_v2_planning_input_uses_run_owned_commitment_quantities(self):
		run = frappe._dict(
			name="RUN-1",
			company="COMPANY-1",
			existing_work_order_policy="Include",
		)
		commitment = frappe._dict(
			name="COM-P0",
			company="COMPANY-1",
			customer="CUST-1",
			item_code="ITEM-1",
			demand_identity="DEMAND-1",
			schedule_item="SCH-ITEM-1",
			original_due_date="2026-08-31",
			requested_qty=100,
			stock_covered_qty=25,
			carried_qty=15,
			newly_planned_qty=60,
			source_work_orders_json="[]",
			source_snapshot_json=json.dumps(
				{
					"schedule_item": "SCH-ITEM-1",
					"demand_identity": "DEMAND-1",
					"customer": "CUST-1",
					"item_code": "ITEM-1",
					"effective_due_date": "2026-09-02",
					"effective_qty": 120,
					"delivered_qty": 20,
					"schedule_open_qty": 100,
				}
			),
			exclude_from_release=0,
		)
		schedule_item = frappe._dict(
			name="SCH-ITEM-1",
			parent="SCH-1",
			demand_identity="DEMAND-1",
			item_code="ITEM-1",
			sales_order="SO-1",
			schedule_date="2026-08-31",
			effective_schedule_date="2026-09-02",
			qty=100,
			effective_qty=120,
			allocated_qty=10,
			produced_qty=5,
			delivered_qty=20,
			status="Open",
			production_strategy="Auto Balance",
			demand_confidence="Confirmed",
			prebuild_allowed=1,
		)
		schedule = frappe._dict(
			name="SCH-1",
			company="COMPANY-1",
			customer="CUST-1",
			source_type="Customer Delivery Schedule",
			status="Active",
		)
		optional = frappe._dict(
			name=None,
			demand_commitment="COM-P1",
			planning_qty=37.5,
			admitted_planning_qty=37.5,
		)
		with (
			patch.object(demand_ledger.frappe, "get_doc", return_value=run),
			patch.object(
				demand_ledger.frappe,
				"get_all",
				side_effect=[[commitment], [schedule_item], [schedule]],
			),
			patch.object(
				demand_ledger,
				"get_selected_optional_planning_rows",
				return_value=[optional],
			),
			patch.object(
				demand_ledger.delivery_fulfillment,
				"get_schedule_delivery_lower_bounds",
				return_value={"SCH-ITEM-1": 20},
			),
			patch.object(planning, "_resolve_unique_sales_order_item", return_value="SO-ITEM-1"),
			patch.object(demand_ledger, "_", side_effect=lambda message, *args, **kwargs: message),
		):
			rows = demand_ledger.get_admitted_planning_rows("RUN-1")

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0].demand_commitment, "COM-P0")
		self.assertEqual(rows[0].demand_qty, 100)
		self.assertEqual(rows[0].available_stock_qty, 25)
		self.assertEqual(rows[0].open_work_order_qty, 15)
		self.assertEqual(rows[0].admitted_planning_qty, 60)
		self.assertEqual(str(rows[0].demand_date), "2026-09-02")
		self.assertEqual(rows[0].sales_order_item, "SO-ITEM-1")
		baseline = json.loads(rows[0].fulfillment_baseline_json)
		self.assertEqual(baseline["version"], 4)
		self.assertEqual(baseline["net_requirement"]["base_residual_qty"], 60)
		self.assertEqual(baseline["targets"][0]["source_open_qty"], 100)
		self.assertEqual(baseline["targets"][0]["opening_required_qty"], 120)
		self.assertEqual(baseline["targets"][0]["opening_delivered_qty"], 20)
		self.assertEqual(rows[1], optional)

	def test_v2_projection_fails_closed_when_schedule_changed_after_baseline(self):
		commitment = frappe._dict(
			name="COM-P0",
			company="COMPANY-1",
			customer="CUST-1",
			item_code="ITEM-1",
			demand_identity="DEMAND-1",
			schedule_item="SCH-ITEM-1",
			requested_qty=100,
			source_snapshot_json=json.dumps(
				{
					"schedule_item": "SCH-ITEM-1",
					"demand_identity": "DEMAND-1",
					"customer": "CUST-1",
					"item_code": "ITEM-1",
					"effective_due_date": "2026-09-02",
					"effective_qty": 120,
					"delivered_qty": 20,
					"schedule_open_qty": 100,
				}
			),
		)
		schedule_item = frappe._dict(
			name="SCH-ITEM-1",
			parent="SCH-1",
			demand_identity="DEMAND-1",
			item_code="ITEM-1",
			schedule_date="2026-08-31",
			effective_schedule_date="2026-09-03",
			qty=100,
			effective_qty=120,
			delivered_qty=20,
			status="Open",
		)
		parent = frappe._dict(
			name="SCH-1",
			company="COMPANY-1",
			customer="CUST-1",
			status="Active",
		)
		with (
			patch.object(
				demand_ledger.frappe,
				"get_doc",
				return_value=frappe._dict(name="RUN-1", company="COMPANY-1"),
			),
			patch.object(demand_ledger.frappe, "get_all", side_effect=[[commitment], [schedule_item], [parent]]),
			patch.object(
				demand_ledger.delivery_fulfillment,
				"get_schedule_delivery_lower_bounds",
				return_value={"SCH-ITEM-1": 20},
			),
			patch.object(
				demand_ledger.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(ValueError(message)),
			),
			patch.object(demand_ledger, "_", side_effect=lambda message, *args, **kwargs: message),
		):
			with self.assertRaisesRegex(ValueError, "no longer matches"):
				demand_ledger.get_admitted_planning_rows("RUN-1")

	def test_solver_target_rejects_stale_non_admitted_quantity(self):
		result = frappe._dict(
			name="RES-1",
			planned_qty=100,
			fulfillment_baseline_json=json.dumps(
				{
					"net_requirement": {
						"net_requirement_qty": 40,
						"planning_qty": 100,
						"minimum_batch_qty": 100,
						"open_work_order_qty": 0,
					}
				}
			),
		)
		commitment = frappe._dict(name="COM-1", newly_planned_qty=37.5)
		with patch.object(solver_orchestration, "_", side_effect=lambda message, *args, **kwargs: message):
			with self.assertRaisesRegex(solver_orchestration.SolverInputBlocked, "no longer matches"):
				solver_orchestration._admitted_result_target(result, commitment)

	def test_admission_batch_boundary_keeps_demand_800_and_plans_1200(self):
		row = frappe._dict(
			admitted_planning_qty=800,
			net_requirement_qty=800,
			open_work_order_qty=0,
			fulfillment_baseline_json=json.dumps(
				{
					"version": 4,
					"net_requirement": {
						"net_requirement_qty": 800,
						"planning_qty": 800,
						"open_work_order_qty": 0,
						"minimum_batch_qty": 0,
						"new_batch_surplus_qty": 0,
						"is_safety_stock_group": 0,
					}
				}
			),
		)

		planning._apply_admission_batch_evidence(row, 1200)

		self.assertEqual(row.admitted_planning_qty, 800)
		self.assertEqual(row.planning_qty, 1200)
		self.assertEqual(planning._net_requirement_production_target_qty(row), 1200)
		evidence = json.loads(row.fulfillment_baseline_json)["net_requirement"]
		self.assertEqual(evidence["minimum_batch_qty"], 1200)
		self.assertEqual(evidence["new_batch_surplus_qty"], 400)

		result = frappe._dict(
			name="RES-1",
			planned_qty=1200,
			fulfillment_baseline_json=row.fulfillment_baseline_json,
		)
		commitment = frappe._dict(name="COM-1", newly_planned_qty=800)
		self.assertEqual(
			solver_orchestration._admitted_result_target(result, commitment),
			1200,
		)

		outcome = frappe._dict(
			on_time_units=900_000,
			late_units=300_000,
			unscheduled_units=0,
		)
		self.assertEqual(
			solver_orchestration._commitment_outcome_quantities(
				commitment,
				outcome,
				scale=1000,
			),
			{"on_time_qty": 800, "late_qty": 0, "unscheduled_qty": 0},
		)

	def test_solver_outcome_keeps_late_carried_supply_in_commitment_partition(self):
		commitment = frappe._dict(carried_qty=60, newly_planned_qty=800)
		outcome = frappe._dict(
			on_time_units=800_000,
			late_units=60_000,
			unscheduled_units=0,
		)

		self.assertEqual(
			solver_orchestration._commitment_outcome_quantities(
				commitment,
				outcome,
				scale=1000,
			),
			{"on_time_qty": 800, "late_qty": 60, "unscheduled_qty": 0},
		)

	def test_admission_guided_recalculation_rejects_frozen_input_changes(self):
		run = frappe._dict(
			name="RUN-1",
			company="COMPANY-1",
			planning_customer_filter="CUST-1",
			planning_item_filter="ITEM-1",
			horizon_days=14,
			run_type="Trial",
			existing_work_order_policy="Include",
			selected_plant_floors=[frappe._dict(plant_floor="FLOOR-1")],
		)
		changes = (
			{"company": "COMPANY-2"},
			{"customer": "CUST-2"},
			{"item_code": "ITEM-2"},
			{"horizon_days": 7},
			{"run_type": "Formal"},
			{"existing_work_order_policy": "Exclude"},
			{"plant_floors": ["FLOOR-2"]},
		)
		with (
			patch.object(planning, "_resolve_item_name", side_effect=lambda value: value),
			patch.object(planning, "_", side_effect=lambda message, *args, **kwargs: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(ValueError(message)),
			),
		):
			for change in changes:
				with self.subTest(change=change), self.assertRaisesRegex(ValueError, "baseline freezes"):
					planning._frozen_admission_recalculation_inputs(
						run,
						{"planning_horizon_days": 30},
						**change,
					)

	def test_admission_guided_recalculation_uses_run_owned_inputs(self):
		run = frappe._dict(
			company="COMPANY-1",
			planning_customer_filter="CUST-1",
			planning_item_filter="ITEM-1",
			horizon_days=14,
			run_type="Trial",
			existing_work_order_policy="Include",
			selected_plant_floors=[frappe._dict(plant_floor="FLOOR-1")],
		)
		frozen = planning._frozen_admission_recalculation_inputs(
			run,
			{"planning_horizon_days": 30},
		)
		self.assertEqual(
			frozen,
			{
				"company": "COMPANY-1",
				"customer": "CUST-1",
				"item_code": "ITEM-1",
				"horizon_days": 14,
				"run_type": "Trial",
				"existing_work_order_policy": "Include",
				"plant_floors": ["FLOOR-1"],
			},
		)

	def test_solver_projection_requires_exactly_one_result_per_commitment(self):
		commitments = [
			frappe._dict(name="COM-1", newly_planned_qty=10, exclude_from_release=0),
			frappe._dict(name="COM-2", newly_planned_qty=20, exclude_from_release=0),
		]
		with patch.object(solver_orchestration, "_", side_effect=lambda message, *args, **kwargs: message):
			with self.assertRaisesRegex(solver_orchestration.SolverInputBlocked, "missing Results: COM-2"):
				solver_orchestration._validate_admitted_projection(
					"RUN-1",
					[frappe._dict(name="RES-1", demand_commitment="COM-1", planned_qty=10)],
					commitments,
				)
			with self.assertRaisesRegex(solver_orchestration.SolverInputBlocked, "duplicate Results: COM-1"):
				solver_orchestration._validate_admitted_projection(
					"RUN-1",
					[
						frappe._dict(name="RES-1", demand_commitment="COM-1", planned_qty=5),
						frappe._dict(name="RES-2", demand_commitment="COM-1", planned_qty=5),
						frappe._dict(name="RES-3", demand_commitment="COM-2", planned_qty=20),
					],
					commitments,
				)

	def test_solver_projection_allows_audited_minimum_batch_surplus(self):
		results = [
			frappe._dict(name="RES-1", demand_commitment="COM-1", planned_qty=100),
			frappe._dict(name="RES-2", demand_commitment="COM-2", planned_qty=20),
		]
		commitments = [
			frappe._dict(name="COM-1", newly_planned_qty=10, exclude_from_release=0),
			frappe._dict(name="COM-2", newly_planned_qty=20, exclude_from_release=0),
		]
		projected = solver_orchestration._validate_admitted_projection(
			"RUN-1", results, commitments
		)
		self.assertEqual(set(projected), {"COM-1", "COM-2"})

	def test_v2_direct_commitment_must_belong_to_the_same_run(self):
		row = frappe._dict(demand_commitment="COM-P1")
		with patch.object(planning.frappe.db, "get_value", return_value=None) as get_value:
			self.assertIsNone(planning._get_v2_commitment_for_result("RUN-1", row))

		filters = get_value.call_args.args[1]
		self.assertEqual(filters["name"], "COM-P1")
		self.assertEqual(filters["planning_run"], "RUN-1")
		self.assertNotIn("Cancelled", filters["status"][1])

	def test_solver_input_rejects_result_without_active_run_owned_commitment(self):
		run = frappe._dict(
			name="RUN-1",
			company="COMPANY-1",
			horizon_start="2026-08-21 00:00:00",
		)
		result = frappe._dict(
			name="RES-1",
			demand_commitment="COM-OTHER-RUN",
		)
		with (
			patch.object(planning, "get_settings_dict", return_value={}),
			patch.object(
				solver_orchestration.horizon_status,
				"run_horizon_values",
				return_value={"start_date": "2026-08-21", "solver_end_datetime": "2026-08-22 00:00:00"},
			),
			patch.object(
				solver_orchestration.frappe,
				"get_all",
				side_effect=[[result], [], []],
			) as get_all,
			patch.object(solver_orchestration, "_", side_effect=lambda message, *args, **kwargs: message),
		):
			with self.assertRaisesRegex(solver_orchestration.SolverInputBlocked, "no active demand commitment"):
				solver_orchestration._build_normalized_source(run)

		commitment_filters = get_all.call_args_list[2].kwargs["filters"]
		self.assertNotIn("name", commitment_filters)
		self.assertEqual(commitment_filters["planning_run"], "RUN-1")
		self.assertEqual(commitment_filters["status"], ("in", demand_ledger.ACTIVE_COMMITMENT_STATUSES))

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
			flags=frappe._dict(),
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
