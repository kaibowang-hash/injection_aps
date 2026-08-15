from __future__ import annotations

import unittest
from datetime import date
from time import perf_counter
from unittest.mock import patch

from injection_aps.services.progress_v2 import (
	build_sparse_matrix_cells,
	classify_progress_row,
	summarize_rows,
)


class TestV2Phase8Progress(unittest.TestCase):
	def setUp(self):
		self.translation_patch = patch(
			"injection_aps.services.progress_v2._",
			lambda message, *args, **kwargs: message,
		)
		self.translation_patch.start()

	def tearDown(self):
		self.translation_patch.stop()

	def test_delivery_and_stock_are_distinct_terminal_states(self):
		delivered = self._row(delivered_qty=100)
		stock_covered = self._row(delivered_qty=20, stock_covered_qty=80)
		self.assertEqual(classify_progress_row(delivered, today=date(2026, 8, 14))[:2], ("Delivered", "green"))
		self.assertEqual(classify_progress_row(stock_covered, today=date(2026, 8, 14))[:2], ("Stock Covered", "blue"))

	def test_forecast_drives_on_track_at_risk_and_late(self):
		on_track = self._row(forecast_completion_time="2026-08-19 12:00:00")
		at_risk = self._row(forecast_completion_time="2026-08-20 12:00:00")
		late = self._row(forecast_completion_time="2026-08-21 00:00:00")
		self.assertEqual(classify_progress_row(on_track, today=date(2026, 8, 14))[0], "On Track")
		self.assertEqual(classify_progress_row(at_risk, today=date(2026, 8, 14))[0], "At Risk")
		self.assertEqual(classify_progress_row(late, today=date(2026, 8, 14))[0], "Late")

	def test_shortage_with_recovery_and_without_recovery_are_distinct(self):
		with_recovery = self._row(shortage_qty=10, recovery_completion_time="2026-08-22 08:00:00")
		without_recovery = self._row(shortage_qty=10)
		self.assertEqual(classify_progress_row(with_recovery, today=date(2026, 8, 14))[0], "At Risk")
		self.assertEqual(classify_progress_row(without_recovery, today=date(2026, 8, 14))[0], "Uncovered")

	def test_missing_identity_conflict_and_conservation_mismatch_are_unknown(self):
		missing = self._row(demand_identity=None)
		conflict = self._row(owner_conflict=1)
		mismatch = self._row(conservation_status="Mismatch")
		for row in (missing, conflict, mismatch):
			self.assertEqual(classify_progress_row(row, today=date(2026, 8, 14))[0], "Unknown")

	def test_sparse_matrix_aggregates_layers_and_deduplicates_sources(self):
		source = {"doctype": "APS Schedule Result", "name": "RES-1"}
		events = [
			{"date": "2026-08-20", "layer": "current_plan_qty", "qty": 30, "sources": [source]},
			{"date": "2026-08-20", "layer": "current_plan_qty", "qty": 20, "sources": [source]},
			{"date": "2026-08-20", "layer": "delivered_qty", "qty": 10, "sources": [source]},
			{"date": "2026-08-21", "layer": "forecast_qty", "qty": 50, "sources": []},
			{"date": "2026-08-20", "layer": "not_a_layer", "qty": 999, "sources": []},
		]
		cells = build_sparse_matrix_cells(events, ["2026-08-20"], default_status="At Risk")
		self.assertEqual(set(cells), {"2026-08-20"})
		self.assertEqual(cells["2026-08-20"]["current_plan_qty"], 50)
		self.assertEqual(cells["2026-08-20"]["delivered_qty"], 10)
		self.assertEqual(len(cells["2026-08-20"]["sources"]), 1)
		self.assertEqual(cells["2026-08-20"]["status"], "At Risk")

	def test_summary_keeps_comparison_layers_separate(self):
		rows = [
			{
				"schedule_qty": 100,
				"original_plan_qty": 100,
				"current_plan_qty": 90,
				"forecast_qty": 80,
				"actual_good_qty": 40,
				"actual_scrap_qty": 2,
				"delivery_plan_qty": 70,
				"delivered_qty": 20,
				"stock_covered_qty": 10,
				"shortage_qty": 10,
				"recovery_qty": 10,
				"status": "At Risk",
				"conservation_status": "OK",
			}
		]
		summary = summarize_rows(rows)
		self.assertEqual(summary["schedule_qty"], 100)
		self.assertEqual(summary["original_plan_qty"], 100)
		self.assertEqual(summary["current_plan_qty"], 90)
		self.assertEqual(summary["forecast_qty"], 80)
		self.assertNotIn("total_supply_qty", summary)
		self.assertEqual(summary["status_counts"], {"At Risk": 1})

	def test_ten_thousand_sparse_events_are_processed_within_budget(self):
		dates = [f"2026-08-{day:02d}" for day in range(1, 32)]
		events = [
			{
				"date": dates[index % len(dates)],
				"layer": "current_plan_qty" if index % 2 else "forecast_qty",
				"qty": 1,
				"sources": [{"doctype": "APS Schedule Result", "name": f"RES-{index % 200}"}],
			}
			for index in range(10_000)
		]
		started = perf_counter()
		cells = build_sparse_matrix_cells(events, dates)
		elapsed = perf_counter() - started
		self.assertEqual(len(cells), 31)
		self.assertEqual(sum(cell["current_plan_qty"] + cell["forecast_qty"] for cell in cells.values()), 10_000)
		self.assertLess(elapsed, 5.0)

	def test_ten_thousand_summary_rows_are_aggregated_without_cross_layer_double_counting(self):
		rows = [
			{
				"schedule_qty": 10, "original_plan_qty": 10, "current_plan_qty": 9,
				"forecast_qty": 8, "actual_good_qty": 4, "actual_scrap_qty": 1,
				"delivery_plan_qty": 7, "delivered_qty": 2, "stock_covered_qty": 1,
				"shortage_qty": 1, "recovery_qty": 1, "status": "At Risk",
				"conservation_status": "OK",
			}
			for _index in range(10_000)
		]
		started = perf_counter()
		summary = summarize_rows(rows)
		elapsed = perf_counter() - started
		self.assertEqual(summary["rows"], 10_000)
		self.assertEqual(summary["schedule_qty"], 100_000)
		self.assertEqual(summary["current_plan_qty"], 90_000)
		self.assertEqual(summary["delivered_qty"], 20_000)
		self.assertEqual(summary["status_counts"], {"At Risk": 10_000})
		self.assertNotIn("total_supply_qty", summary)
		self.assertLess(elapsed, 5.0)

	def _row(self, **updates):
		row = {
			"schedule_qty": 100,
			"delivered_qty": 0,
			"stock_covered_qty": 0,
			"current_plan_qty": 100,
			"shortage_qty": 0,
			"schedule_date": "2026-08-20",
			"effective_due_time": "2026-08-20 23:59:59",
			"forecast_completion_time": "2026-08-19 12:00:00",
			"demand_identity": "DEMAND-1",
			"owner_conflict": 0,
			"conservation_status": "OK",
		}
		row.update(updates)
		return row


if __name__ == "__main__":
	unittest.main()
