from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from injection_aps.services.solver.input_builder import build_solver_input
from injection_aps.services.solver.scenarios import solve_scenarios
from injection_aps.services.solver.capacity_solver import SolverRuntimeUnavailable, solve_capacity
from injection_aps.services.solver.sequence_solver import SequenceInfeasible, solve_sequence
from injection_aps.services.solver.serialization import solution_fingerprint
from injection_aps.services.solver.validator import validate_solution


class TestV2Phase4Solver(unittest.TestCase):
	def test_global_delivery_priority_and_quantity_conservation(self):
		snapshot = self._snapshot(
			demands=[
				self._demand("EARLY", 6, "2026-08-14T14:00:00", priority=10),
				self._demand("LATE", 6, "2026-08-14T20:00:00", priority=1),
			],
			buckets=[
				self._bucket("B1", "2026-08-14T08:00:00", "2026-08-14T14:00:00", 6),
				self._bucket("B2", "2026-08-14T14:00:00", "2026-08-14T18:00:00", 4),
			],
		)
		recommended = solve_scenarios(snapshot)[0]
		self.assertTrue(dict(recommended.validation)["valid"])
		self.assertEqual(sum(row.on_time_units + row.late_units + row.unscheduled_units for row in recommended.outcomes), 12)
		self.assertGreaterEqual(next(row.on_time_units for row in recommended.outcomes if row.demand_key == "EARLY"), 6)

	def test_p1_never_displaces_p0(self):
		p0_only = self._snapshot(demands=[self._demand("P0", 8, "2026-08-14T20:00:00")], buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)])
		with_optional = self._snapshot(demands=[self._demand("P0", 8, "2026-08-14T20:00:00"), self._demand("P1", 8, "2026-08-14T20:00:00", admission="P1")], buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)])
		baseline = solve_scenarios(p0_only)[0]
		result = solve_scenarios(with_optional)[0]
		self.assertGreaterEqual(result.metrics.p0_on_time_units, baseline.metrics.p0_on_time_units)

	def test_recovery_reports_earliest_completion(self):
		snapshot = self._snapshot(
			demands=[self._demand("D", 8, "2026-08-14T12:00:00")],
			buckets=[
				self._bucket("DUE", "2026-08-14T08:00:00", "2026-08-14T12:00:00", 4),
				self._bucket("REC", "2026-08-14T12:00:00", "2026-08-14T18:00:00", 6, zone="Recovery"),
			],
		)
		outcome = solve_scenarios(snapshot)[0].outcomes[0]
		self.assertEqual(outcome.on_time_units, 4)
		self.assertEqual(outcome.late_units, 4)
		self.assertEqual(outcome.recovery_completion_minute, 244)

	def test_subminute_cycle_and_continuous_setup_are_preserved(self):
		demand = self._demand("D", 1200, "2026-08-14T20:00:00")
		demand["alternatives"][0]["cycle_minutes"] = 0.72
		demand["alternatives"][0]["base_setup_minutes"] = 30
		snapshot = self._snapshot(
			demands=[demand],
			buckets=[
				self._bucket("B1", "2026-08-14T08:00:00", "2026-08-14T20:00:00", 720),
				self._bucket("B2", "2026-08-14T20:00:00", "2026-08-15T00:00:00", 240, zone="Recovery"),
				self._bucket("B3", "2026-08-15T00:00:00", "2026-08-15T05:30:00", 330, zone="Recovery"),
			],
		)
		self.assertEqual(snapshot.demands[0].alternatives[0].cycle_minutes, 0.72)
		solution = solve_scenarios(snapshot)[0]
		self.assertTrue(dict(solution.validation)["valid"])
		self.assertEqual(sum(row.base_setup_minutes for row in solution.tasks), 30)
		self.assertEqual(solution.metrics.p0_on_time_units, 958)
		self.assertEqual(solution.metrics.total_late_units, 242)
		self.assertEqual(solution.outcomes[0].recovery_completion_minute, 895)

	def test_frozen_interval_is_immutable_and_not_overlapped(self):
		source = self._source(
			demands=[self._demand("D", 4, "2026-08-14T20:00:00")],
			buckets=[self._bucket("B", "2026-08-14T12:00:00", "2026-08-14T18:00:00", 6)],
		)
		source["frozen_intervals"] = [{"key": "F", "resource_type": "machine", "resource": "M1", "start": "2026-08-14T08:00:00", "end": "2026-08-14T12:00:00"}]
		snapshot = build_solver_input(source)
		solution = solve_scenarios(snapshot)[0]
		self.assertEqual(solution.frozen_intervals, snapshot.frozen_intervals)
		self.assertGreaterEqual(solution.tasks[0].occupied_start_minute, 240)

	def test_scenario_delivery_metrics_are_not_worse_than_recommended_floor(self):
		snapshot = self._snapshot(
			demands=[self._demand("A", 6, "2026-08-14T14:00:00"), self._demand("B", 6, "2026-08-14T20:00:00", mold="MO2")],
			buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T20:00:00", 12)],
		)
		solutions = solve_scenarios(snapshot)
		recommended = solutions[0]
		for solution in solutions:
			self.assertGreaterEqual(solution.metrics.p0_on_time_units, recommended.metrics.p0_on_time_units)
			self.assertLessEqual(solution.metrics.p0_critical_unplanned_units, recommended.metrics.p0_critical_unplanned_units)

	def test_deterministic_seed_produces_identical_solution_fingerprint(self):
		snapshot = self._snapshot(demands=[self._demand("A", 4, "2026-08-14T20:00:00")], buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)])
		first = solve_scenarios(snapshot)[0]
		second = solve_scenarios(snapshot)[0]
		self.assertEqual(first.solution_fingerprint, second.solution_fingerprint)
		self.assertEqual(first.tasks, second.tasks)

	def test_independent_validator_rejects_quantity_tampering(self):
		snapshot = self._snapshot(demands=[self._demand("A", 4, "2026-08-14T20:00:00")], buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)])
		solution = solve_scenarios(snapshot)[0]
		bad_outcome = replace(solution.outcomes[0], unscheduled_units=solution.outcomes[0].unscheduled_units + 1)
		bad = replace(solution, outcomes=(bad_outcome,), solution_fingerprint="")
		bad = replace(bad, solution_fingerprint=solution_fingerprint(bad))
		result = validate_solution(snapshot, bad, raise_on_error=False)
		self.assertFalse(result["valid"])
		self.assertIn("demand_conservation", {row["code"] for row in result["errors"]})

	def test_independent_validator_recalculates_delivery_metrics(self):
		snapshot = self._snapshot(demands=[self._demand("A", 4, "2026-08-14T20:00:00")], buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)])
		solution = solve_scenarios(snapshot)[0]
		bad_metrics = replace(
			solution.metrics,
			p0_weighted_tardiness=solution.metrics.p0_weighted_tardiness + 1,
		)
		bad = replace(solution, metrics=bad_metrics, solution_fingerprint="")
		bad = replace(bad, solution_fingerprint=solution_fingerprint(bad))
		result = validate_solution(snapshot, bad, raise_on_error=False)
		self.assertFalse(result["valid"])
		self.assertIn("p0_metric_mismatch", {row["code"] for row in result["errors"]})

	def test_fractional_capacity_factor_extends_wall_duration(self):
		bucket = self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)
		bucket["capacity_factor"] = 0.5
		snapshot = self._snapshot(demands=[self._demand("A", 4, "2026-08-14T20:00:00")], buckets=[bucket])
		task = solve_scenarios(snapshot)[0].tasks[0]
		self.assertEqual(task.end_minute - task.production_start_minute, 8)

	def test_minimum_batch_prevents_partial_schedule_below_the_lot_size(self):
		demand = self._demand("A", 100, "2026-08-14T20:00:00")
		demand["minimum_batch_qty"] = 60
		snapshot = self._snapshot(
			demands=[demand],
			buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T09:00:00", 50)],
		)
		for solution in solve_scenarios(snapshot):
			self.assertTrue(dict(solution.validation)["valid"])
			self.assertEqual(sum(row.quantity_units for row in solution.tasks), 0)
			self.assertEqual(solution.outcomes[0].unscheduled_units, 100)

	def test_minimum_batch_applies_to_total_new_schedule_across_buckets(self):
		demand = self._demand("A", 100, "2026-08-14T20:00:00")
		demand["minimum_batch_qty"] = 60
		snapshot = self._snapshot(
			demands=[demand],
			buckets=[
				self._bucket("B1", "2026-08-14T08:00:00", "2026-08-14T08:30:00", 30),
				self._bucket("B2", "2026-08-14T08:30:00", "2026-08-14T09:00:00", 30),
			],
		)
		solution = solve_scenarios(snapshot)[0]
		self.assertTrue(dict(solution.validation)["valid"])
		self.assertEqual(sum(row.quantity_units for row in solution.tasks), 60)
		self.assertEqual(solution.outcomes[0].unscheduled_units, 40)

	def test_validator_rejects_a_tampered_schedule_below_minimum_batch(self):
		demand = self._demand("A", 100, "2026-08-14T20:00:00")
		demand["minimum_batch_qty"] = 60
		snapshot = self._snapshot(
			demands=[demand],
			buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T10:00:00", 120)],
		)
		solution = solve_scenarios(snapshot)[0]
		allocation = replace(solution.allocations[0], quantity_units=50, cycles=50)
		task = replace(solution.tasks[0], quantity_units=50, cycles=50)
		outcome = replace(solution.outcomes[0], on_time_units=50, unscheduled_units=50)
		tampered = replace(
			solution,
			allocations=(allocation,),
			tasks=(task,),
			outcomes=(outcome,),
		)
		validation = validate_solution(snapshot, tampered, raise_on_error=False)
		self.assertIn("minimum_batch", {row["code"] for row in validation["errors"]})

	def test_global_time_limit_stops_rebuilding_models_for_later_scenarios(self):
		snapshot = replace(
			self._snapshot(
				demands=[self._demand("A", 4, "2026-08-14T20:00:00")],
				buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)],
			),
			time_limit_seconds=3,
		)
		clock = [0.0]

		def exhaust_global_budget(*_args, **_kwargs):
			clock[0] = 4.0
			raise SolverRuntimeUnavailable("forced slow model build")

		with patch("injection_aps.services.solver.scenarios.time.monotonic", side_effect=lambda: clock[0]), patch(
			"injection_aps.services.solver.scenarios.solve_capacity",
			side_effect=exhaust_global_budget,
		) as capacity:
			solutions = solve_scenarios(snapshot)
		self.assertEqual(capacity.call_count, 1)
		self.assertEqual([row.status for row in solutions], ["Fallback", "Fallback", "Fallback"])
		self.assertTrue(any("Global solver time limit" in warning for warning in solutions[1].warnings))

	def test_solver_runtime_unavailable_is_an_explicit_valid_fallback(self):
		snapshot = self._snapshot(
			demands=[self._demand("A", 4, "2026-08-14T20:00:00")],
			buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)],
		)
		with patch(
			"injection_aps.services.solver.scenarios.solve_capacity",
			side_effect=SolverRuntimeUnavailable("forced solver outage"),
		):
			solutions = solve_scenarios(snapshot)
		self.assertEqual([row.status for row in solutions], ["Fallback", "Fallback", "Fallback"])
		self.assertTrue(all(dict(row.validation)["valid"] for row in solutions))
		self.assertTrue(all("forced solver outage" in row.warnings for row in solutions))

	def test_capacity_model_build_time_is_not_reset_before_feasibility_solve(self):
		snapshot = self._snapshot(
			demands=[self._demand("A", 4, "2026-08-14T20:00:00")],
			buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)],
		)
		clock = iter((0.0, 2.0, 2.0, 2.0))
		with patch(
			"injection_aps.services.solver.capacity_solver.time.monotonic",
			side_effect=lambda: next(clock),
		), patch("ortools.sat.python.cp_model.CpSolver.Solve") as solve:
			result = solve_capacity(snapshot, ("p0_on_time",), time_limit_seconds=1)
		self.assertEqual(result.status, "No Feasible")
		self.assertIn("model construction", result.warnings[0])
		solve.assert_not_called()

	def test_sequence_model_build_time_is_not_reset_before_solve(self):
		snapshot = self._snapshot(
			demands=[self._demand("A", 4, "2026-08-14T20:00:00")],
			buckets=[self._bucket("B", "2026-08-14T08:00:00", "2026-08-14T18:00:00", 10)],
		)
		clock = iter((0.0, 2.0))
		with patch(
			"injection_aps.services.solver.sequence_solver.time.monotonic",
			side_effect=lambda: next(clock),
		), patch("ortools.sat.python.cp_model.CpSolver.Solve") as solve:
			with self.assertRaisesRegex(SequenceInfeasible, "model construction"):
				solve_sequence(snapshot, (), time_limit_seconds=1)
		solve.assert_not_called()

	def _snapshot(self, *, demands, buckets):
		return build_solver_input(self._source(demands=demands, buckets=buckets))

	def _source(self, *, demands, buckets):
		return {"run_key": "RUN", "horizon_start": "2026-08-14T08:00:00", "horizon_end": "2026-08-15T08:00:00", "quantity_scale": 1, "time_limit_seconds": 9, "random_seed": 42, "demands": demands, "buckets": buckets}

	def _demand(self, key, qty, due, *, priority=0, admission="P0", mold="MO1"):
		return {"key": key, "result": f"RES-{key}", "commitment": f"COM-{key}", "item_code": key, "admission_class": admission, "quantity": qty, "due_time": due, "earliest_time": "2026-08-14T08:00:00", "service_priority": priority, "alternatives": [{"key": f"M1|{mold}", "machine": "M1", "mold": mold, "output_per_cycle": 1, "cycle_minutes": 1, "base_setup_minutes": 0}]}

	def _bucket(self, key, start, end, minutes, *, zone="Demand"):
		return {"key": key, "machine": "M1", "start": start, "end": end, "available_minutes": minutes, "capacity_factor": 1, "horizon_zone": zone}


if __name__ == "__main__":
	unittest.main()
