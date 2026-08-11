from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from injection_aps.services.availability import project_segment_output


class TestJITAvailabilityProjection(unittest.TestCase):
	def setUp(self):
		self.start = datetime(2026, 8, 11, 8)
		self.segment = {
			"start_time": self.start,
			"end_time": self.start + timedelta(hours=10),
			"planned_qty": 100,
		}

	def test_jit_releases_output_gradually_before_segment_completion(self):
		row = project_segment_output(self.segment, self.start + timedelta(hours=5))
		self.assertEqual(row["actual_qty"], 0)
		self.assertEqual(row["projected_cumulative_qty"], 50)
		self.assertEqual(row["projected_remaining_qty"], 50)

	def test_actual_report_replaces_curve_and_reforecasts_only_the_remainder(self):
		row = project_segment_output(
			self.segment,
			self.start + timedelta(hours=7),
			actual_events=[
				{
					"time": self.start + timedelta(hours=4),
					"qty": 40,
					"source": "STE-1",
				}
			],
		)
		self.assertEqual(row["actual_qty"], 40)
		self.assertEqual(row["projected_cumulative_qty"], 70)
		self.assertEqual(row["projected_remaining_qty"], 30)

	def test_underproduction_at_end_remains_visible_as_projected_shortfall_work(self):
		row = project_segment_output(
			self.segment,
			self.start + timedelta(hours=10),
			actual_events=[
				{
					"time": self.start + timedelta(hours=9),
					"qty": 40,
					"source": "STE-1",
				}
			],
		)
		self.assertEqual(row["actual_qty"], 40)
		self.assertEqual(row["projected_cumulative_qty"], 100)
		self.assertEqual(row["projected_remaining_qty"], 60)

	def test_overproduction_never_creates_negative_projected_remainder(self):
		row = project_segment_output(
			self.segment,
			self.start + timedelta(hours=6),
			actual_events=[
				{
					"time": self.start + timedelta(hours=5),
					"qty": 110,
					"source": "STE-1",
				}
			],
		)
		self.assertEqual(row["actual_qty"], 110)
		self.assertEqual(row["projected_cumulative_qty"], 110)
		self.assertEqual(row["projected_remaining_qty"], 0)


if __name__ == "__main__":
	unittest.main()
