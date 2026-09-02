from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ortools.sat.python import cp_model

from injection_aps.services.shift_replan import forecast_segment, propagate_resource_forecast, solve_local_replan


class TestPhase5ShiftReplan(unittest.TestCase):
	def setUp(self):
		self.start = datetime(2026, 8, 14, 8)

	def test_stable_actual_rate_forecasts_only_remaining_pieces(self):
		row = forecast_segment(
			{"name": "S1", "start_time": self.start, "end_time": self.start + timedelta(hours=2), "planned_qty": 120, "actual_good_qty": 40, "actual_scrap_qty": 20},
			execution_cutoff=self.start + timedelta(hours=1),
			rate_samples=[1.0, 1.1, 0.9],
			standard_rate_per_minute=0.5,
		)
		self.assertEqual(row["remaining_qty"], 80)
		self.assertEqual(row["rate_source"], "Recent Stable Actual")
		self.assertFalse(row["fallback_rate_used"])
		self.assertEqual(row["forecast_end_time"], self.start + timedelta(hours=2, minutes=20))

	def test_insufficient_sample_uses_explicit_standard_fallback(self):
		row = forecast_segment(
			{"name": "S1", "start_time": self.start, "end_time": self.start + timedelta(hours=2), "planned_qty": 120, "actual_good_qty": 60, "execution_state": "In Progress"},
			execution_cutoff=self.start + timedelta(hours=1),
			rate_samples=[2.0],
			standard_rate_per_minute=1.0,
		)
		self.assertTrue(row["fallback_rate_used"])
		self.assertEqual(row["forecast_end_time"], self.start + timedelta(hours=2))

	def test_running_task_never_moves_and_delays_shared_machine_successor(self):
		rows = propagate_resource_forecast([
			{"segment": "RUNNING", "workstation": "M1", "mould_reference": "F1", "current_start_time": self.start, "current_end_time": self.start + timedelta(hours=1), "forecast_end_time": self.start + timedelta(hours=2), "execution_state": "In Progress", "is_frozen": 1},
			{"segment": "NEXT", "workstation": "M1", "mould_reference": "F2", "current_start_time": self.start + timedelta(hours=1), "current_end_time": self.start + timedelta(hours=2), "forecast_end_time": self.start + timedelta(hours=2), "execution_state": "Not Started", "is_frozen": 0},
		], execution_cutoff=self.start + timedelta(minutes=30), next_shift_start=self.start + timedelta(hours=1))
		by_name = {row["segment"]: row for row in rows}
		self.assertEqual(by_name["RUNNING"]["proposed_start_time"], self.start)
		self.assertEqual(by_name["RUNNING"]["diff_type"], "Delayed by Execution")
		self.assertEqual(by_name["NEXT"]["proposed_start_time"], self.start + timedelta(hours=2))

	def test_normal_cycle_does_not_move_flexible_work_into_current_shift(self):
		rows = propagate_resource_forecast([
			{"segment": "NEXT", "workstation": "M1", "mould_reference": "F1", "current_start_time": self.start, "current_end_time": self.start + timedelta(hours=1), "forecast_end_time": self.start + timedelta(hours=1), "execution_state": "Not Started", "is_frozen": 0},
		], execution_cutoff=self.start, next_shift_start=self.start + timedelta(hours=12))
		self.assertEqual(rows[0]["proposed_start_time"], self.start + timedelta(hours=12))

	def test_local_cp_sat_keeps_running_task_fixed_and_prevents_overlap(self):
		result = solve_local_replan([
			{"segment": "RUNNING", "workstation": "M1", "mould_reference": "F1", "current_start_time": self.start, "current_end_time": self.start + timedelta(hours=1), "forecast_end_time": self.start + timedelta(hours=2), "execution_state": "In Progress", "is_frozen": 1},
			{"segment": "NEXT", "workstation": "M1", "mould_reference": "F2", "current_start_time": self.start + timedelta(hours=1), "current_end_time": self.start + timedelta(hours=2), "forecast_end_time": self.start + timedelta(hours=2), "execution_state": "Not Started", "is_frozen": 0},
		], execution_cutoff=self.start + timedelta(minutes=30), next_shift_start=self.start + timedelta(hours=1), time_limit_seconds=5)
		self.assertIn(result["status"], {"Optimal", "Feasible"})
		by_name = {row["segment"]: row for row in result["rows"]}
		self.assertEqual(by_name["RUNNING"]["proposed_start_time"], self.start)
		self.assertGreaterEqual(by_name["NEXT"]["proposed_start_time"], by_name["RUNNING"]["proposed_end_time"])

	def test_zero_capacity_downtime_is_an_exact_local_solver_interval(self):
		result = solve_local_replan(
			[{
				"segment": "NEXT", "workstation": "M1", "mould_reference": "F1",
				"current_start_time": self.start, "current_end_time": self.start + timedelta(hours=1),
				"forecast_end_time": self.start + timedelta(hours=1),
				"execution_state": "Not Started", "is_frozen": 0,
			}],
			execution_cutoff=self.start,
			next_shift_start=self.start,
			blocked_intervals=[{
				"resource_type": "machine", "resource": "M1",
				"start_time": self.start, "end_time": self.start + timedelta(hours=2),
			}],
			time_limit_seconds=5,
		)
		self.assertIn(result["status"], {"Optimal", "Feasible"})
		self.assertGreaterEqual(result["rows"][0]["proposed_start_time"], self.start + timedelta(hours=2))

	def test_solver_timeout_fallback_still_respects_resource_order_and_downtime(self):
		segments = [
			{
				"segment": "FIRST", "workstation": "M1", "mould_reference": "F1",
				"current_start_time": self.start, "current_end_time": self.start + timedelta(hours=1),
				"forecast_end_time": self.start + timedelta(hours=1),
				"execution_state": "Not Started", "is_frozen": 0,
			},
			{
				"segment": "SECOND", "workstation": "M1", "mould_reference": "F2",
				"current_start_time": self.start + timedelta(hours=1), "current_end_time": self.start + timedelta(hours=2),
				"forecast_end_time": self.start + timedelta(hours=2),
				"execution_state": "Not Started", "is_frozen": 0,
			},
		]
		blocked = [{
			"resource_type": "machine", "resource": "M1",
			"start_time": self.start, "end_time": self.start + timedelta(hours=2),
		}]
		with patch("ortools.sat.python.cp_model.CpSolver.Solve", return_value=cp_model.UNKNOWN):
			result = solve_local_replan(
				segments,
				execution_cutoff=self.start,
				next_shift_start=self.start,
				blocked_intervals=blocked,
				time_limit_seconds=1,
			)
		self.assertEqual(result["status"], "Fallback")
		self.assertTrue(result["fallback_used"])
		by_name = {row["segment"]: row for row in result["rows"]}
		self.assertGreaterEqual(by_name["FIRST"]["proposed_start_time"], self.start + timedelta(hours=2))
		self.assertGreaterEqual(by_name["SECOND"]["proposed_start_time"], by_name["FIRST"]["proposed_end_time"])


if __name__ == "__main__":
	unittest.main()
