from __future__ import annotations

import json
import unittest
from datetime import date
from time import perf_counter
from unittest.mock import MagicMock, patch

from injection_aps.services import progress_v2
from injection_aps.services.progress_v2 import (
	_build_row_events,
	_get_production_allocations,
	_segment_plan_change_reason,
	aggregate_matrix_rows,
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

	def test_matrix_groups_all_schedule_dates_by_customer_and_item(self):
		rows = [
			{
				"company": "COMPANY-1", "customer": "CUSTOMER-1", "item_code": "ITEM-1",
				"schedule_item": "ROW-1", "demand_identity": "DEMAND-1", "schedule_date": "2026-08-20",
				"schedule_qty": 100, "current_plan_qty": 80, "status": "On Track", "status_tone": "green",
				"conservation_status": "OK", "events": [{"date": "2026-08-20", "layer": "schedule_qty", "qty": 100, "sources": []}],
			},
			{
				"company": "COMPANY-1", "customer": "CUSTOMER-1", "item_code": "ITEM-1",
				"schedule_item": "ROW-2", "demand_identity": "DEMAND-2", "schedule_date": "2026-08-21",
				"schedule_qty": 50, "current_plan_qty": 20, "status": "Late", "status_tone": "red",
				"conservation_status": "OK", "events": [{"date": "2026-08-21", "layer": "schedule_qty", "qty": 50, "sources": []}],
			},
		]
		matrix_rows = aggregate_matrix_rows(rows, ["2026-08-20", "2026-08-21"])
		self.assertEqual(len(matrix_rows), 1)
		self.assertEqual(matrix_rows[0]["schedule_count"], 2)
		self.assertEqual(matrix_rows[0]["schedule_qty"], 150)
		self.assertEqual(matrix_rows[0]["current_plan_qty"], 100)
		self.assertEqual(matrix_rows[0]["status"], "Late")
		self.assertEqual(matrix_rows[0]["cells"]["2026-08-21"]["schedule_qty"], 50)

	def test_matrix_adds_layer_specific_cumulative_shortfall_alerts(self):
		rows = [{
			"company": "COMPANY-1", "customer": "CUSTOMER-1", "item_code": "ITEM-1",
			"schedule_item": "ROW-1", "demand_identity": "DEMAND-1", "schedule_date": "2020-01-02",
			"schedule_qty": 100, "current_plan_qty": 80, "status": "Late", "status_tone": "red",
			"conservation_status": "OK",
			"events": [
				{"date": "2020-01-02", "layer": "schedule_qty", "qty": 100, "sources": []},
				{"date": "2020-01-02", "layer": "current_plan_qty", "qty": 80, "sources": []},
				{"date": "2020-01-02", "layer": "actual_good_qty", "qty": 50, "sources": []},
				{"date": "2020-01-02", "layer": "delivered_qty", "qty": 40, "sources": []},
			],
		}]
		cell = aggregate_matrix_rows(rows, ["2020-01-02"])[0]["cells"]["2020-01-02"]
		alerts = {row["layer"]: row for row in cell["alerts"]}
		self.assertEqual(set(alerts), {"actual", "delivery"})
		self.assertIn("30", alerts["actual"]["reason"])
		self.assertIn("60", alerts["delivery"]["reason"])

	def test_default_matrix_query_is_limited_to_the_visible_date_window(self):
		window_start = date(2026, 8, 7)
		window_end = date(2026, 9, 4)
		with (
			patch.object(progress_v2, "_resolve_company", return_value="COMPANY-1"),
			patch.object(progress_v2, "_matrix_window", return_value=(window_start, window_end)),
			patch.object(progress_v2, "_get_matrix_schedule_rows", return_value=([], 0, 0)) as get_rows,
			patch.object(
				progress_v2,
				"_build_projection",
				return_value={"rows": [], "projection": {}},
			),
		):
			progress_v2.get_progress_matrix_data(company="COMPANY-1")
		self.assertEqual(get_rows.call_args.kwargs["date_from"], window_start)
		self.assertEqual(get_rows.call_args.kwargs["date_to"], window_end)

	def test_cross_run_actual_inbound_does_not_require_current_result(self):
		db = MagicMock()
		db.exists.return_value = True
		with (
			patch.object(progress_v2.frappe, "db", db),
			patch.object(progress_v2.frappe, "get_list", return_value=[]) as get_list,
		):
			_get_production_allocations(["ROW-1"], [], run_name=None)
		filters = get_list.call_args.kwargs["filters"]
		self.assertNotIn("schedule_result", filters)
		self.assertEqual(filters["customer_schedule_item"], ("in", ["ROW-1"]))

	def test_single_run_actual_inbound_requires_a_visible_result(self):
		db = MagicMock()
		db.exists.return_value = True
		with (
			patch.object(progress_v2.frappe, "db", db),
			patch.object(progress_v2.frappe, "get_list") as get_list,
		):
			rows = _get_production_allocations(["ROW-1"], [], run_name="RUN-1")
		self.assertEqual(rows, [])
		get_list.assert_not_called()

	def test_actual_inbound_uses_effective_stock_entry_allocation_date(self):
		segment = {
			"name": "SEG-1", "parent": "RESULT-1", "planned_qty": 100,
			"baseline_start_time": "2026-08-18 08:00:00", "baseline_end_time": "2026-08-18 12:00:00",
			"current_start_time": "2026-08-19 08:00:00", "current_end_time": "2026-08-19 14:00:00",
			"actual_end_time": "2026-08-19 18:00:00", "actual_good_qty": 999,
		}
		events = _build_row_events(
			{"schedule_date": "2026-08-20", "schedule_qty": 100, "schedule": "SCHEDULE-1", "schedule_item": "ROW-1"},
			active_segments=[segment], results=[{"name": "RESULT-1"}],
			production_rows=[{
				"name": "ALLOCATION-1", "schedule_result": "RESULT-1", "segment": "SEG-1",
				"source_stock_entry": "MAT-STE-1", "source_posting_time": "2026-08-21 09:30:00",
				"good_qty": 40, "scrap_qty": 2,
			}],
			stock_qty=0, shortage_qty=0, recovery_qty=0, recovery_completion=None,
			delivery_rows=[], delivery_plan_rows=[],
		)
		actual_events = [event for event in events if event["layer"] in ("actual_good_qty", "actual_scrap_qty")]
		self.assertEqual([(event["date"], event["qty"]) for event in actual_events], [("2026-08-21", 40.0), ("2026-08-21", 2.0)])
		self.assertIn({"doctype": "Stock Entry", "name": "MAT-STE-1", "label": "MAT-STE-1"}, actual_events[0]["sources"])
		self.assertIn("moved", _segment_plan_change_reason(segment))
		self.assertIn("duration", _segment_plan_change_reason(segment))

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

	def test_hidden_segments_are_filtered_before_progress_quantities_and_sources(self):
		schedule_rows = [{
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"item_code": "ITEM-1",
			"schedule": "SCHEDULE-1",
			"schedule_item": "SCHEDULE-ITEM-1",
			"demand_identity": "IDENTITY-1",
			"schedule_date": "2026-08-20",
			"schedule_qty": 100,
			"schedule_delivered_qty": 0,
		}]
		commitments = [{
			"name": "COMMITMENT-1",
			"planning_run": "RUN-1",
			"demand_identity": "IDENTITY-1",
			"requested_qty": 100,
			"newly_planned_qty": 100,
			"on_time_qty": 100,
			"late_qty": 0,
			"unscheduled_qty": 0,
		}]
		results = [{"name": "RESULT-1", "planning_run": "RUN-1", "demand_commitment": "COMMITMENT-1"}]
		segments = [
			{
				"name": "SEGMENT-ALLOWED", "parent": "RESULT-1", "planned_qty": 40,
				"segment_status": "Planned", "start_time": "2026-08-18 08:00:00",
				"end_time": "2026-08-18 12:00:00",
			},
			{
				"name": "SEGMENT-DENIED", "parent": "RESULT-1", "planned_qty": 60,
				"segment_status": "Planned", "start_time": "2026-08-19 08:00:00",
				"end_time": "2026-08-19 12:00:00",
			},
		]
		production_rows = [
			{
				"name": "ALLOCATION-ALLOWED", "customer_schedule_item": "SCHEDULE-ITEM-1",
				"schedule_result": "RESULT-1", "segment": "SEGMENT-ALLOWED", "good_qty": 4,
			},
			{
				"name": "ALLOCATION-DENIED", "customer_schedule_item": "SCHEDULE-ITEM-1",
				"schedule_result": "RESULT-1", "segment": "SEGMENT-DENIED", "good_qty": 60,
			},
		]
		segment_filter = MagicMock(
			side_effect=lambda values: [row for row in values if row["name"] == "SEGMENT-ALLOWED"]
		)

		with (
			patch.object(progress_v2, "_get_commitments", return_value=commitments),
			patch.object(progress_v2, "_get_results", return_value=results),
			patch.object(progress_v2, "_get_segments", return_value=segments),
			patch.object(progress_v2, "_get_production_allocations", return_value=production_rows),
			patch.object(progress_v2, "_get_stock_allocations", return_value=[]),
			patch.object(progress_v2, "_get_delivery_allocations", return_value=[]),
			patch.object(progress_v2, "_get_delivery_plan_rows", return_value=[]),
			patch.object(progress_v2, "_get_bom_peggings", return_value=[]),
			patch.object(progress_v2, "classify_progress_row", return_value=("On Track", "green", "ok")),
		):
			projection = progress_v2._build_projection(
				schedule_rows,
				run_name="RUN-1",
				segment_access_filter=segment_filter,
			)

		row = projection["rows"][0]
		self.assertEqual(row["original_plan_qty"], 40)
		self.assertEqual(row["current_plan_qty"], 40)
		self.assertEqual(row["actual_good_qty"], 4)
		source_names = {source["name"] for source in row["source_documents"]}
		self.assertNotIn("SEGMENT-DENIED", source_names)
		self.assertNotIn("ALLOCATION-DENIED", source_names)
		segment_filter.assert_called_once_with(segments)

	def test_hidden_owner_documents_are_filtered_before_projection_math(self):
		schedule_rows = [{
			"company": "COMPANY-1", "customer": "CUSTOMER-1", "item_code": "ITEM-1",
			"schedule": "SCHEDULE-1", "schedule_item": "SCHEDULE-ITEM-1",
			"demand_identity": "IDENTITY-1", "schedule_date": "2026-08-20",
			"schedule_qty": 100, "schedule_delivered_qty": 0,
		}]
		commitments = [
			{
				"name": "COMMITMENT-ALLOWED", "planning_run": "RUN-1",
				"demand_identity": "IDENTITY-1", "requested_qty": 30,
				"newly_planned_qty": 30, "on_time_qty": 30,
				"late_qty": 0, "unscheduled_qty": 0,
			},
			{
				"name": "COMMITMENT-DENIED", "planning_run": "RUN-HIDDEN",
				"demand_identity": "IDENTITY-1", "requested_qty": 70,
				"newly_planned_qty": 70, "on_time_qty": 70,
				"late_qty": 0, "unscheduled_qty": 0,
			},
		]
		results = [
			{"name": "RESULT-ALLOWED", "demand_commitment": "COMMITMENT-ALLOWED"},
			{"name": "RESULT-DENIED", "demand_commitment": "COMMITMENT-DENIED"},
		]
		segments = [
			{"name": "SEGMENT-ALLOWED", "parent": "RESULT-ALLOWED", "planned_qty": 30, "segment_status": "Planned"},
			{"name": "SEGMENT-DENIED", "parent": "RESULT-DENIED", "planned_qty": 70, "segment_status": "Planned"},
		]

		with (
			patch.object(progress_v2, "_get_commitments", return_value=commitments),
			patch.object(progress_v2, "_get_results", return_value=results),
			patch.object(progress_v2, "_get_segments", return_value=segments),
			patch.object(progress_v2, "_get_production_allocations", return_value=[]),
			patch.object(progress_v2, "_get_stock_allocations", return_value=[]),
			patch.object(progress_v2, "_get_delivery_allocations", return_value=[]),
			patch.object(progress_v2, "_get_delivery_plan_rows", return_value=[]),
			patch.object(progress_v2, "_get_bom_peggings", return_value=[]),
			patch.object(progress_v2, "classify_progress_row", return_value=("On Track", "green", "ok")),
		):
			projection = progress_v2._build_projection(
				schedule_rows,
				run_name=None,
				commitment_access_filter=lambda rows: [row for row in rows if row["name"] == "COMMITMENT-ALLOWED"],
				result_access_filter=lambda rows: [row for row in rows if row["name"] == "RESULT-ALLOWED"],
				segment_access_filter=lambda rows: [row for row in rows if row["name"] == "SEGMENT-ALLOWED"],
			)

		row = projection["rows"][0]
		self.assertEqual(row["new_plan_qty"], 30)
		self.assertEqual(row["on_time_qty"], 30)
		self.assertEqual(row["current_plan_qty"], 30)
		self.assertEqual(row["commitment_names"], ["COMMITMENT-ALLOWED"])
		self.assertEqual(row["result_names"], ["RESULT-ALLOWED"])
		self.assertEqual(row["run_names"], ["RUN-1"])

	def test_carried_work_order_is_not_counted_twice_after_physical_plan_expansion(self):
		baseline = {
			"net_requirement": {
				"open_work_order_qty": 40,
				"net_requirement_qty": 60,
			}
		}
		segments = [{"parent": "RESULT-1", "planned_qty": 100}]
		new_result = [{
			"name": "RESULT-1",
			"planned_qty": 100,
			"fulfillment_baseline_json": json.dumps(baseline),
		}]
		legacy_result = [{
			"name": "RESULT-1",
			"planned_qty": 60,
			"fulfillment_baseline_json": json.dumps(baseline),
		}]

		self.assertEqual(
			progress_v2._unrepresented_carried_qty(new_result, segments, carried_qty=40),
			0,
		)
		self.assertEqual(
			progress_v2._unrepresented_carried_qty(
				legacy_result,
				[{"parent": "RESULT-1", "planned_qty": 60}],
				carried_qty=40,
			),
			40,
		)

	def test_carried_late_qty_is_included_in_solver_partition_and_recovery(self):
		classification = patch.object(
			progress_v2,
			"classify_progress_row",
			return_value=("Late", "red", "carried supply is late"),
		)
		classification.start()
		self.addCleanup(classification.stop)
		row = progress_v2._project_row(
			{
				"schedule": "SCHEDULE-1",
				"schedule_item": "SCHEDULE-ITEM-1",
				"demand_identity": "IDENTITY-1",
				"schedule_date": "2026-08-20",
				"schedule_qty": 860,
				"schedule_delivered_qty": 0,
			},
			commitments=[
				{
					"name": "COMMITMENT-1",
					"planning_run": "RUN-1",
					"requested_qty": 860,
					"stock_covered_qty": 0,
					"carried_qty": 60,
					"newly_planned_qty": 800,
					"on_time_qty": 800,
					"late_qty": 60,
					"unscheduled_qty": 0,
					"effective_due_time": "2026-08-20 23:59:59",
				}
			],
			results=[],
			segments=[],
			production_rows=[],
			stock_rows=[],
			delivery_rows=[],
			delivery_plan_rows=[],
			pegging_rows=[],
		)

		self.assertEqual(row["recovery_qty"], 60)
		self.assertEqual(row["solver_partition_delta"], 0)
		self.assertEqual(row["conservation_status"], "OK")

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
