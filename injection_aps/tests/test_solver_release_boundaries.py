from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.services import (
	capacity_balance,
	constraint_resolution,
	planning,
	solver_orchestration,
)


class _Segment(frappe._dict):
	def as_dict(self):
		return dict(self)


class _Result:
	def __init__(self, segments):
		self.segments = segments
		self.flags = frappe._dict()
		self.saved = False

	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def save(self, **_kwargs):
		self.saved = True


class TestSolverReleaseBoundaries(unittest.TestCase):
	def setUp(self):
		self.original_flags = getattr(frappe.local, "flags", None)
		self.original_session = getattr(frappe.local, "session", None)
		self.original_db = getattr(frappe.local, "db", None)
		frappe.local.flags = frappe._dict(in_test=True)
		frappe.local.session = frappe._dict(user="pmc@example.com")
		frappe.local.db = MagicMock()

	def tearDown(self):
		frappe.local.flags = self.original_flags
		frappe.local.session = self.original_session
		frappe.local.db = self.original_db

	def test_fixed_actual_good_is_not_planned_twice_and_scrap_alone_is_replaced(self):
		good = _Segment(
			planned_qty=100,
			actual_completed_qty=20,
			actual_good_qty=20,
			actual_scrap_qty=0,
			end_time="2026-08-20 12:00:00",
		)
		on_time, late, controllable = solver_orchestration._fixed_demand_quantities(
			[good], due="2026-08-21 23:59:59", target=100, result_name="RES-1"
		)
		self.assertEqual((on_time, late, controllable), (100, 0, 0))

		scrap = _Segment(
			planned_qty=100,
			actual_completed_qty=20,
			actual_good_qty=15,
			actual_scrap_qty=5,
			end_time="2026-08-20 12:00:00",
		)
		on_time, late, controllable = solver_orchestration._fixed_demand_quantities(
			[scrap], due="2026-08-21 23:59:59", target=100, result_name="RES-1"
		)
		self.assertEqual((on_time, late, controllable), (95, 0, 5))

	def test_zero_quantity_optional_commitments_are_not_solver_projection_rows(self):
		commitments = [
			frappe._dict(
				name="P0-ZERO",
				admission_class="P0",
				newly_planned_qty=0,
				exclude_from_release=0,
			),
			frappe._dict(
				name="P1-ZERO",
				admission_class="P1",
				newly_planned_qty=0,
				exclude_from_release=0,
			),
			frappe._dict(
				name="P2-SELECTED",
				admission_class="P2",
				newly_planned_qty=50,
				exclude_from_release=0,
			),
		]
		results = [
			frappe._dict(name="RES-P0", demand_commitment="P0-ZERO", planned_qty=0),
			frappe._dict(
				name="RES-P2", demand_commitment="P2-SELECTED", planned_qty=50
			),
		]

		projected = solver_orchestration._validate_admitted_projection(
			"RUN-1", results, commitments
		)

		self.assertEqual(set(projected), {"P0-ZERO", "P2-SELECTED"})

	def test_fixed_coverage_above_admitted_quantity_fails_closed(self):
		segment = _Segment(
			planned_qty=100,
			actual_completed_qty=0,
			actual_good_qty=0,
			actual_scrap_qty=0,
			end_time="2026-08-20 12:00:00",
		)
		with patch.object(solver_orchestration, "_", side_effect=lambda message, *args, **kwargs: message):
			with self.assertRaises(solver_orchestration.SolverInputBlocked):
				solver_orchestration._fixed_demand_quantities(
					[segment], due="2026-08-21 23:59:59", target=90, result_name="RES-1"
				)

	def test_solver_apply_retires_only_nonfixed_segments_of_excluded_results(self):
		flexible = _Segment(name="SEG-FLEX", segment_status="Applied")
		frozen = _Segment(name="SEG-FROZEN", segment_status="Applied")
		doc = _Result([flexible, frozen])
		with (
			patch.object(
				solver_orchestration.frappe.db,
				"sql",
				side_effect=[[frappe._dict(name="RES-EXCLUDED")], []],
			),
			patch.object(solver_orchestration.frappe, "get_doc", return_value=doc),
			patch.object(
				capacity_balance,
				"_is_fixed_segment",
				side_effect=lambda row: row.get("name") == "SEG-FROZEN",
			),
		):
			retired = solver_orchestration._retire_excluded_result_segments("RUN-1")

		self.assertEqual(retired, 1)
		self.assertEqual(flexible.segment_status, "Cancelled")
		self.assertEqual(frozen.segment_status, "Applied")
		self.assertEqual(doc.flow_step, "Excluded from Release")
		self.assertTrue(doc.saved)

	def test_work_order_apply_rejects_an_excluded_result_even_for_an_old_batch(self):
		row = frappe._dict(result_reference="RES-EXCLUDED")
		with (
			patch.object(planning.frappe, "get_all", return_value=["RES-EXCLUDED"]),
			patch.object(planning, "_", side_effect=lambda message, *args, **kwargs: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError("excluded")),
		):
			with self.assertRaises(frappe.ValidationError):
				planning._assert_work_order_proposal_results_releasable("RUN-1", [row])

	def test_approved_override_is_bound_to_the_analysis_it_was_approved_against(self):
		rows = [
			frappe._dict(
				name="OVERRIDE-OLD",
				input_fingerprint="FP-OLD",
				expires_on="2026-09-03 00:00:00",
			),
			frappe._dict(
				name="OVERRIDE-CURRENT",
				input_fingerprint="FP-CURRENT",
				expires_on="2026-09-03 00:00:00",
			),
		]
		with (
			patch.object(constraint_resolution.frappe.db, "exists", return_value=True),
			patch.object(constraint_resolution.frappe, "get_all", return_value=rows),
		):
			current = constraint_resolution.approved_overrides(
				"RUN-1",
				at_time="2026-09-02 00:00:00",
				expected_input_fingerprint="FP-CURRENT",
			)

		self.assertEqual([row["name"] for row in current], ["OVERRIDE-CURRENT"])

	def test_stale_approved_override_is_superseded_after_reanalysis(self):
		stale = frappe._dict(name="OVERRIDE-OLD", status="Approved", idempotency_key="OLD")
		run = frappe._dict(name="RUN-1", company="COMPANY-1", plant_floor="FLOOR-1")
		with (
			patch.object(constraint_resolution, "is_v2_enabled", return_value=True),
			patch.object(constraint_resolution, "now_datetime", return_value="2026-09-02 00:00:00"),
			patch.object(constraint_resolution.frappe.db, "exists", return_value=True),
			patch.object(constraint_resolution.frappe, "get_all", return_value=[stale]),
			patch.object(constraint_resolution, "_set_values") as set_values,
		):
			constraint_resolution.sync_from_analysis(
				run,
				{"analysis_fingerprint": "FP-CURRENT", "hard_blockers": [], "approved_overrides": []},
			)

		self.assertEqual(set_values.call_args.args[0], "OVERRIDE-OLD")
		self.assertEqual(set_values.call_args.args[1]["status"], "Superseded")

	def test_cp_sat_capability_is_exposed_and_override_mutations_fail_before_locking(self):
		settings = {"enable_aps_v2": 1, "solver_engine": "CP-SAT"}
		with (
			patch(
				"injection_aps.services.v2_flags.get_v2_settings", return_value=settings
			),
			patch.object(
				constraint_resolution, "_", side_effect=lambda message, *args, **kwargs: message
			),
		):
			supported, reason = constraint_resolution._temporary_override_capability()
		self.assertFalse(supported)
		self.assertIn("CP-SAT", reason)

		with (
			patch.object(constraint_resolution, "is_v2_enabled", return_value=True),
			patch(
				"injection_aps.services.v2_flags.get_v2_settings", return_value=settings
			),
			patch.object(
				constraint_resolution, "_", side_effect=lambda message, *args, **kwargs: message
			),
			patch.object(
				constraint_resolution.frappe,
				"throw",
				side_effect=frappe.ValidationError("unsupported"),
			),
			patch.object(constraint_resolution, "_lock_resolution") as lock_resolution,
		):
			with self.assertRaises(frappe.ValidationError):
				constraint_resolution.request_temporary_override(
					"RESOLUTION-1",
					resolution_type="Temporary Capacity Override",
					proposed_value={},
					expires_on="2026-09-03 00:00:00",
					reason="test",
					expected_fingerprint="FP-1",
				)
			with self.assertRaises(frappe.ValidationError):
				constraint_resolution.approve_temporary_override(
					"RESOLUTION-1", reason="test", expected_fingerprint="FP-1"
				)
			lock_resolution.assert_not_called()

	def test_constraint_payload_exposes_temporary_override_capability(self):
		run = frappe._dict(
			name="RUN-1",
			company="COMPANY-1",
			capacity_balance_status="Blocked",
			capacity_balance_fingerprint="FP-1",
		)
		with (
			patch.object(constraint_resolution, "_require_v2"),
			patch.object(constraint_resolution.frappe, "get_doc", return_value=run),
			patch.object(constraint_resolution.frappe, "get_all", return_value=[]),
			patch.object(
				constraint_resolution,
				"_temporary_override_capability",
				return_value=(False, "unsupported"),
			),
		):
			payload = constraint_resolution.get_constraint_resolutions("RUN-1")

		self.assertFalse(payload["temporary_override_supported"])
		self.assertEqual(payload["temporary_override_reason"], "unsupported")

	def test_legacy_override_chain_replays_each_approved_fingerprint_generation(self):
		analysis = {
			"demands": [
				{
					"checks": [
						{"key": "capacity", "status": "blocked"},
						{"key": "compatibility", "status": "blocked"},
					]
				}
			],
			"buckets": [],
		}
		fingerprints = []

		def apply_generation(_run, current_analysis, *, expected_input_fingerprint):
			fingerprints.append(expected_input_fingerprint)
			generation = len(fingerprints)
			if generation <= 2:
				check = current_analysis["demands"][0]["checks"][generation - 1]
				check["status"] = "warning"
				check["key"] = f"approved_override|OVERRIDE-{generation}"
				return [{"name": f"OVERRIDE-{generation}"}]
			return []

		with (
			patch.object(
				constraint_resolution,
				"apply_approved_overrides_to_analysis",
				side_effect=apply_generation,
			),
			patch(
				"injection_aps.services.horizon_status.classify_v2_analysis"
			),
		):
			used = capacity_balance._apply_approved_override_chain(
				"RUN-1",
				analysis,
				source_snapshot={"source": "stable"},
				optional_admission_qty=0,
				excluded_qty=0,
			)

		self.assertEqual([row["name"] for row in used], ["OVERRIDE-1", "OVERRIDE-2"])
		self.assertEqual(len(fingerprints), 3)
		self.assertEqual(len(set(fingerprints)), 3)

	def test_pre_job_solver_blocker_persists_matching_run_and_resolution_fingerprint(self):
		run = frappe._dict(name="RUN-1", company="COMPANY-1", run_type="Formal")
		blocked = solver_orchestration.SolverInputBlocked(
			"No legal machine",
			blocker_key="machine_mold_alternative",
			blocker_policy="Exclude Only",
			schedule_result="RESULT-1",
			demand_commitment="COMMITMENT-1",
		)
		with (
			patch.object(solver_orchestration, "_require_v2_solver"),
			patch(
				"injection_aps.services.demand_admission.require_admission_ready"
			),
			patch.object(solver_orchestration.frappe, "get_doc", return_value=run),
			patch.object(
				solver_orchestration, "_build_normalized_source", side_effect=blocked
			),
			patch.object(
				constraint_resolution,
				"sync_solver_input_blockers",
				return_value="BLOCK-FP",
			) as sync_blockers,
			patch.object(solver_orchestration, "_set_run_solver_values") as set_run,
		):
			response = solver_orchestration.analyze_v2_schedule(
				"RUN-1", run_in_background=False
			)

		self.assertEqual(response["status"], "Hard Blocked")
		sync_blockers.assert_called_once_with(run, blocked.blockers)
		values = set_run.call_args.args[1]
		self.assertEqual(values["capacity_balance_fingerprint"], "BLOCK-FP")
		self.assertIn('"analysis_fingerprint":"BLOCK-FP"', values["capacity_balance_analysis_json"])

	def test_bom_input_blocker_uses_the_same_pre_job_persistence_path(self):
		run = frappe._dict(name="RUN-1", company="COMPANY-1", run_type="Formal")
		with (
			patch.object(solver_orchestration, "_require_v2_solver"),
			patch(
				"injection_aps.services.demand_admission.require_admission_ready"
			),
			patch.object(solver_orchestration.frappe, "get_doc", return_value=run),
			patch.object(
				solver_orchestration,
				"_build_normalized_source",
				side_effect=solver_orchestration.bom_planning.BOMCycleError("cycle"),
			),
			patch.object(
				constraint_resolution,
				"sync_solver_input_blockers",
				return_value="BOM-FP",
			) as sync_blockers,
			patch.object(solver_orchestration, "_set_run_solver_values") as set_run,
		):
			response = solver_orchestration.analyze_v2_schedule(
				"RUN-1", run_in_background=False
			)

		self.assertEqual(response["status"], "Hard Blocked")
		self.assertEqual(sync_blockers.call_args.args[1][0]["blocker_key"], "bom_cycle")
		self.assertEqual(
			set_run.call_args.args[1]["capacity_balance_fingerprint"], "BOM-FP"
		)

	def test_job_level_solver_blocker_is_visible_with_the_job_input_fingerprint(self):
		job = frappe._dict(
			name="JOB-1",
			planning_run="RUN-1",
			status="Queued",
			cancel_requested=0,
			input_snapshot_json="{}",
			input_fingerprint="JOB-FP",
		)
		run = frappe._dict(name="RUN-1", company="COMPANY-1")
		with (
			patch.object(
				solver_orchestration.frappe, "get_doc", side_effect=[job, run]
			),
			patch.object(solver_orchestration, "input_from_dict", return_value=object()),
			patch.object(solver_orchestration, "solve_scenarios", return_value=()),
			patch.object(
				solver_orchestration,
				"now_datetime",
				return_value=frappe.utils.get_datetime("2026-09-02 00:00:00"),
			),
			patch.object(solver_orchestration, "_update_job"),
			patch.object(
				constraint_resolution,
				"sync_solver_input_blockers",
				return_value="JOB-FP",
			) as sync_blockers,
			patch.object(solver_orchestration, "_set_run_solver_values") as set_run,
			patch.object(
				solver_orchestration, "get_solver_job", return_value={"status": "Failed"}
			),
		):
			response = solver_orchestration.execute_solver_job("JOB-1")

		self.assertEqual(response["status"], "Failed")
		self.assertEqual(
			sync_blockers.call_args.kwargs["input_fingerprint"], "JOB-FP"
		)
		self.assertEqual(
			set_run.call_args.args[1]["capacity_balance_fingerprint"], "JOB-FP"
		)

	def test_failed_projection_rolls_back_success_writes_before_recording_failure(self):
		job = frappe._dict(
			name="JOB-1",
			planning_run="RUN-1",
			status="Queued",
			cancel_requested=0,
			input_snapshot_json="{}",
		)
		solution = frappe._dict(
			status="Optimal",
			validation={"valid": True},
			scenario_key="recommended",
			engine="CP-SAT",
			solution_fingerprint="SOLUTION-FP",
			best_bound=0,
			objective_value=0,
			gap_percent=0,
		)
		events = []

		def update_job(_name, values):
			events.append(("job", values.get("status")))

		def fail_projection(*_args):
			events.append(("projection", "partial"))
			raise RuntimeError("projection failed")

		with (
			patch.object(solver_orchestration.frappe, "get_doc", return_value=job),
			patch.object(solver_orchestration, "input_from_dict", return_value=object()),
			patch.object(solver_orchestration, "solve_scenarios", return_value=[solution]),
			patch.object(solver_orchestration, "solution_to_dict", return_value={}),
			patch.object(
				solver_orchestration,
				"now_datetime",
				return_value=frappe.utils.get_datetime("2026-09-02 00:00:00"),
			),
			patch.object(solver_orchestration, "_update_job", side_effect=update_job),
			patch.object(solver_orchestration, "_set_run_solver_values"),
			patch.object(
				solver_orchestration,
				"_persist_selected_projection",
				side_effect=fail_projection,
			),
			patch.object(
				solver_orchestration.frappe.db,
				"rollback",
				side_effect=lambda **_kwargs: events.append(("rollback", "success writes")),
			) as rollback,
			patch.object(
				solver_orchestration, "get_solver_job", return_value={"status": "Failed"}
			),
		):
			response = solver_orchestration.execute_solver_job("JOB-1")

		self.assertEqual(response["status"], "Failed")
		rollback.assert_called_once_with(save_point="aps_solver_projection")
		self.assertLess(events.index(("rollback", "success writes")), events.index(("job", "Failed")))
		solver_orchestration.frappe.db.release_savepoint.assert_not_called()

	def test_solver_blocker_resolution_input_uses_the_supplied_stable_fingerprint(self):
		run = frappe._dict(name="RUN-1", company="COMPANY-1", plant_floor=None)
		captured = []
		with (
			patch.object(constraint_resolution, "is_v2_enabled", return_value=True),
			patch.object(constraint_resolution.frappe.db, "exists", return_value=True),
			patch.object(constraint_resolution, "_result_context", return_value={}),
			patch.object(constraint_resolution, "_resolve_commitment", return_value=None),
			patch.object(constraint_resolution, "_upsert_engine_record", side_effect=captured.append),
			patch.object(constraint_resolution.frappe, "get_all", return_value=[]),
			patch.object(
				constraint_resolution, "_", side_effect=lambda message, *args, **kwargs: message
			),
		):
			fingerprint = constraint_resolution.sync_solver_input_blockers(
				run,
				[{"blocker_key": "invalid_input", "message": "blocked"}],
				input_fingerprint="STABLE-FP",
			)

		self.assertEqual(fingerprint, "STABLE-FP")
		self.assertEqual(captured[0]["input_fingerprint"], "STABLE-FP")

	def test_solver_blockers_keep_distinct_bom_demand_scopes(self):
		run = frappe._dict(name="RUN-1", company="COMPANY-1", plant_floor=None)
		captured = []
		with (
			patch.object(constraint_resolution, "is_v2_enabled", return_value=True),
			patch.object(constraint_resolution.frappe.db, "exists", return_value=True),
			patch.object(constraint_resolution, "_result_context", return_value={}),
			patch.object(constraint_resolution, "_resolve_commitment", return_value=None),
			patch.object(constraint_resolution, "_upsert_engine_record", side_effect=captured.append),
			patch.object(constraint_resolution.frappe, "get_all", return_value=[]),
			patch.object(
				constraint_resolution, "_", side_effect=lambda message, *args, **kwargs: message
			),
		):
			constraint_resolution.sync_solver_input_blockers(
				run,
				[
					{"blocker_key": "bom_minimum_batch", "scope_key": "BOM-A", "message": "A"},
					{"blocker_key": "bom_minimum_batch", "scope_key": "BOM-B", "message": "B"},
				],
				input_fingerprint="STABLE-FP",
			)

		self.assertEqual(len(captured), 2)
		self.assertEqual(len({row["idempotency_key"] for row in captured}), 2)

	def test_bom_solver_reads_the_configured_minimum_batch_field(self):
		run = frappe._dict(
			name="RUN-1",
			company="COMPANY-1",
			horizon_start="2026-09-02 00:00:00",
		)
		settings = {"item_min_batch_field": "custom_minimum_batch_qty"}
		windows = {
			"start_date": "2026-09-02",
			"solver_end_datetime": "2026-09-03 00:00:00",
		}
		expansion = {
			"demands": [{"key": "BOM-1", "item_code": "ITEM-1", "quantity": 5}],
			"precedences": [],
			"bom_decisions": [],
		}
		with (
			patch.object(solver_orchestration.planning, "get_settings_dict", return_value=settings),
			patch.object(solver_orchestration.horizon_status, "run_horizon_values", return_value=windows),
			patch.object(solver_orchestration.frappe, "get_all", side_effect=[[], []]),
			patch.object(solver_orchestration.planning, "_get_run_selected_plant_floors", return_value=[]),
			patch.object(solver_orchestration.planning, "_get_machine_capability_rows", return_value=[]),
			patch.object(
				solver_orchestration.v2_flags,
				"get_v2_settings",
				return_value={"enable_multilevel_bom_planning": 1},
			),
			patch.object(solver_orchestration.frappe, "get_cached_doc", return_value=frappe._dict()),
			patch.object(solver_orchestration.bom_planning, "configured_producible_groups", return_value=set()),
			patch.object(solver_orchestration.bom_planning, "expand_solver_demands", return_value=expansion),
			patch.object(solver_orchestration.planning, "_get_item_context", return_value={}),
			patch.object(
				solver_orchestration.planning,
				"_get_item_mapping_value",
				return_value=10,
			) as get_mapping,
			patch.object(solver_orchestration, "_", side_effect=lambda message, *args, **kwargs: message),
		):
			with self.assertRaises(solver_orchestration.SolverInputBlocked) as error:
				solver_orchestration._build_normalized_source(run)

		self.assertEqual(error.exception.blockers[0]["blocker_key"], "bom_minimum_batch")
		get_mapping.assert_called_once_with("ITEM-1", "custom_minimum_batch_qty")


if __name__ == "__main__":
	unittest.main()
