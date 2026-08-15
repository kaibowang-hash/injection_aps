from __future__ import annotations

import unittest
from datetime import datetime

from injection_aps.services import capacity_balance, horizon_status


class TestPhase3HorizonAndStatus(unittest.TestCase):
	def test_fourteen_day_horizon_covers_exactly_fourteen_natural_dates(self):
		windows = horizon_status.calculate_horizon_windows(
			datetime(2026, 8, 2, 11, 30),
			demand_days=14,
			freeze_days=2,
			restricted_days=7,
			recovery_days=5,
		)
		self.assertEqual(str(windows["demand_start_date"]), "2026-08-02")
		self.assertEqual(str(windows["demand_end_date"]), "2026-08-15")
		self.assertEqual(str(windows["recovery_start_date"]), "2026-08-16")
		self.assertEqual(str(windows["recovery_end_date"]), "2026-08-20")

	def test_overdue_and_window_zones_are_explicit(self):
		windows = horizon_status.calculate_horizon_windows(
			"2026-08-02 08:00:00", demand_days=14, freeze_days=2, restricted_days=7, recovery_days=3
		)
		self.assertEqual(horizon_status.classify_due_date("2026-08-01", windows), (1, "Overdue"))
		self.assertEqual(horizon_status.classify_due_date("2026-08-03", windows), (0, "Freeze"))
		self.assertEqual(horizon_status.classify_due_date("2026-08-07", windows), (0, "Restricted"))
		self.assertEqual(horizon_status.classify_due_date("2026-08-15", windows), (0, "Demand"))
		self.assertEqual(horizon_status.classify_due_date("2026-08-16", windows), (0, "Recovery"))

	def test_capacity_shortage_is_acknowledgment_not_hard_blocked(self):
		analysis = {
			"summary": {"unscheduled_qty": 200},
			"demands": [{
				"result": "RES-1", "segment": "SEG-1", "planned_qty": 1200,
				"late_qty": 200, "unscheduled_qty": 200, "prebuild_qty": 0,
				"status": "Blocked", "checks": [{"status": "blocked", "key": "capacity", "message": "1000 available"}],
			}],
		}
		horizon_status.classify_v2_analysis(analysis)
		self.assertEqual(analysis["readiness_status"], "Acknowledgment Required")
		self.assertEqual(analysis["summary"]["hard_blocker_count"], 0)

	def test_locked_overlap_and_bom_cycle_never_override(self):
		for key in ("locked_workstation_overlap", "locked_mold_overlap", "bom_cycle", "negative_quantity"):
			with self.subTest(key=key):
				self.assertEqual(horizon_status.blocker_policy(key), "Never Override")

	def test_material_changes_do_not_change_capacity_fingerprint(self):
		base = {
			"key": "SEG-1", "qty": 100, "result": "RES-1", "segment": "SEG-1",
			"material_ready_qty": 0,
			"material_requirements": [{"resource_key": "RM-A", "available_qty": 0, "qty_per_unit": 1}],
		}
		changed = {
			**base,
			"material_ready_qty": 1000,
			"material_requirements": [{"resource_key": "RM-A", "available_qty": 1000, "qty_per_unit": 1}],
		}
		left = horizon_status.remove_material_constraints([base])
		right = horizon_status.remove_material_constraints([changed])
		self.assertEqual(capacity_balance.fingerprint(left), capacity_balance.fingerprint(right))
		self.assertNotEqual(
			horizon_status.build_material_advisory([base]),
			horizon_status.build_material_advisory([changed]),
		)


if __name__ == "__main__":
	unittest.main()
