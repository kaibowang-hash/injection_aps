from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, today

from injection_aps.services import capacity_balance, consistency, planning
from injection_aps.services.capacity_balance import balance_capacity_nodes, canonical_json


class TestCapacityBalanceEngine(unittest.TestCase):
	def setUp(self):
		self.start = datetime(2026, 8, 10, 8)

	def test_auto_balance_prebuilds_only_overloaded_quantity(self):
		result = self._run(node1_minutes=720, node2_minutes=420, qty=100)
		row = result["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 30)
		self.assertAlmostEqual(row["jit_qty"], 70)
		self.assertAlmostEqual(row["late_qty"], 0)

	def test_minimum_batch_applies_to_total_lot_not_prebuild_mode_fragment(self):
		row = self._run(
			node1_minutes=720,
			node2_minutes=420,
			qty=100,
			minimum_batch_qty=100,
		)["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 30)
		self.assertAlmostEqual(row["jit_qty"], 70)
		self.assertAlmostEqual(row["late_qty"], 0)

	def test_auto_balance_does_not_prebuild_when_due_bucket_is_sufficient(self):
		row = self._run(node1_minutes=720, node2_minutes=720, qty=100)["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["jit_qty"], 100)

	def test_inventory_limit_caps_prebuild_and_exposes_late_quantity(self):
		row = self._run(
			node1_minutes=720,
			node2_minutes=420,
			qty=100,
			inventory_room_qty=10,
		)["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 10)
		self.assertAlmostEqual(row["jit_qty"], 70)
		self.assertAlmostEqual(row["late_qty"], 20)

	def test_material_limit_caps_all_scheduled_production(self):
		row = self._run(
			node1_minutes=720,
			node2_minutes=420,
			qty=100,
			material_ready_qty=15,
		)["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["jit_qty"], 15)
		self.assertAlmostEqual(row["late_qty"], 85)
		self.assertAlmostEqual(row["unscheduled_qty"], 85)
		self.assertEqual(row["status"], "Blocked")

	def test_max_early_days_blocks_out_of_window_capacity(self):
		row = self._run(
			node1_minutes=720,
			node2_minutes=420,
			qty=100,
			max_prebuild_days=0,
		)["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["late_qty"], 30)

	def test_prebuild_below_minimum_batch_is_not_reported_as_balanced(self):
		result = self._run(
			node1_minutes=720,
			node2_minutes=0,
			qty=20,
			minimum_batch_qty=30,
		)
		row = result["demands"][0]

		self.assertAlmostEqual(row["prebuild_qty"], 20)
		self.assertEqual(row["status"], "Blocked")
		self.assertEqual(result["summary"]["blocked_demands"], 1)
		self.assertIn(
			"minimum_batch_qty",
			{check["key"] for check in row["checks"] if check["status"] == "blocked"},
		)

	def test_force_jit_below_minimum_batch_is_blocked(self):
		result = self._run(
			node1_minutes=720,
			node2_minutes=720,
			qty=20,
			strategy="Force JIT",
			minimum_batch_qty=30,
		)
		row = result["demands"][0]

		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["jit_qty"], 20)
		self.assertEqual(row["status"], "Blocked")
		self.assertIn(
			"minimum_batch_qty",
			{check["key"] for check in row["checks"] if check["status"] == "blocked"},
		)

	def test_force_jit_never_uses_earlier_bucket(self):
		row = self._run(node1_minutes=720, node2_minutes=420, qty=100, strategy="Force JIT")["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["jit_qty"], 70)
		self.assertAlmostEqual(row["late_qty"], 30)

	def test_force_prebuild_uses_earliest_allowed_capacity(self):
		row = self._run(node1_minutes=720, node2_minutes=720, qty=100, strategy="Force Prebuild")["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 100)
		self.assertAlmostEqual(row["jit_qty"], 0)

	def test_forecast_prebuild_requires_pmc_confirmation(self):
		row = self._run(
			node1_minutes=720,
			node2_minutes=420,
			qty=100,
			demand_confidence="Forecast",
		)["demands"][0]
		self.assertEqual(row["requires_confirmation"], 1)
		self.assertIn("Forecast demand", row["confirmation_reasons"])

	def test_prebuild_with_unconfigured_inventory_or_warehouse_limit_requires_confirmation(self):
		for fieldname, reason in (
			("inventory_room_qty", "Item inventory limit not configured"),
			("warehouse_room_qty", "FG warehouse capacity not configured"),
		):
			with self.subTest(fieldname=fieldname):
				row = self._run(
					node1_minutes=720,
					node2_minutes=420,
					qty=100,
					**{fieldname: None},
				)["demands"][0]
				self.assertGreater(row["prebuild_qty"], 0)
				self.assertEqual(row["status"], "Confirmation Required")
				self.assertIn(reason, row["confirmation_reasons"])

	def test_same_input_is_deterministic(self):
		kwargs = {"node1_minutes": 720, "node2_minutes": 420, "qty": 100}
		first = self._run(**kwargs)
		second = self._run(**kwargs)
		self.assertEqual(canonical_json(first), canonical_json(second))

	def test_shared_resource_source_snapshot_is_order_stable(self):
		run = SimpleNamespace(
			name="RUN-1",
			modified="2026-08-11 08:00:00",
			horizon_start="2026-08-10 08:00:00",
			horizon_end="2026-08-13 08:00:00",
		)
		demands = [
			self._resource_snapshot_demand("S-2", "RM-B"),
			self._resource_snapshot_demand("S-1", "RM-A"),
		]
		reordered = deepcopy(list(reversed(demands)))
		for demand in reordered:
			demand["material_requirements"].reverse()

		first = capacity_balance._build_source_snapshot(
			run, {}, [], resource_demands=demands
		)
		second = capacity_balance._build_source_snapshot(
			run, {}, [], resource_demands=reordered
		)

		self.assertEqual(canonical_json(first), canonical_json(second))
		self.assertEqual(capacity_balance.fingerprint(first), capacity_balance.fingerprint(second))

	def test_shared_resource_source_snapshot_changes_for_each_guard_input(self):
		run = SimpleNamespace(
			name="RUN-1",
			modified="2026-08-11 08:00:00",
			horizon_start="2026-08-10 08:00:00",
			horizon_end="2026-08-13 08:00:00",
		)
		baseline_demands = [self._resource_snapshot_demand("S-1", "RM-A")]
		baseline = capacity_balance.fingerprint(
			capacity_balance._build_source_snapshot(
				run, {}, [], resource_demands=baseline_demands
			)
		)
		mutators = {
			"strategy": lambda row: row.__setitem__("strategy", "Force JIT"),
			"setup": lambda row: row.__setitem__("setup_minutes", 15),
			"prebuild policy": lambda row: row.__setitem__("prebuild_allowed", 1),
			"prebuild days": lambda row: row.__setitem__("max_prebuild_days", 3),
			"current inventory": lambda row: row.__setitem__("current_inventory_qty", 11),
			"inventory room": lambda row: row.__setitem__("inventory_room_qty", 89),
			"inventory key": lambda row: row.__setitem__("inventory_resource_key", "COMPANY|FG-2"),
			"warehouse room": lambda row: row.__setitem__("warehouse_room_qty", 79),
			"warehouse key": lambda row: row.__setitem__("warehouse_resource_key", "FG-WH-2"),
			"target stock UOM": lambda row: row.__setitem__("target_stock_uom", "Kg"),
			"material ready": lambda row: row.__setitem__("material_ready_qty", 69),
			"resource priority": lambda row: row.__setitem__("priority", 100),
			"material key": lambda row: row["material_requirements"][0].__setitem__(
				"resource_key", "COMPANY|*|RM-CHANGED"
			),
			"material ratio": lambda row: row["material_requirements"][0].__setitem__("qty_per_unit", 2),
			"material available": lambda row: row["material_requirements"][0].__setitem__(
				"available_qty", 59
			),
		}
		for label, mutate in mutators.items():
			with self.subTest(label=label):
				changed_demands = deepcopy(baseline_demands)
				mutate(changed_demands[0])
				changed = capacity_balance.fingerprint(
					capacity_balance._build_source_snapshot(
						run, {}, [], resource_demands=changed_demands
					)
				)
				self.assertNotEqual(baseline, changed)

	def test_source_snapshot_changes_with_downtime_and_formal_occupation(self):
		run = SimpleNamespace(
			name="RUN-1",
			modified="2026-08-11 08:00:00",
			horizon_start="2026-08-10 08:00:00",
			horizon_end="2026-08-13 08:00:00",
		)
		baseline = capacity_balance._build_source_snapshot(run, {}, [])
		with_constraints = capacity_balance._build_source_snapshot(
			run,
			{},
			[],
			downtime_windows=[
				{
					"name": "DOWN-1",
					"scope": "Workstation",
					"workstation": "M-1",
					"start_time": self.start,
					"end_time": self.start + timedelta(hours=1),
					"available_capacity_percent": 0,
				}
			],
			fixed_intervals={"M-1": [(self.start, self.start + timedelta(hours=2))]},
			mold_fixed_intervals={"MOULD-1": [(self.start, self.start + timedelta(hours=2))]},
		)

		self.assertNotEqual(capacity_balance.fingerprint(baseline), capacity_balance.fingerprint(with_constraints))
		self.assertEqual(len(with_constraints["capacity_constraints"]["downtime"]), 1)
		self.assertEqual(len(with_constraints["capacity_constraints"]["workstation_fixed_intervals"]), 1)

	def test_late_baseline_is_computed_per_segment_not_from_persisted_result_total(self):
		due = datetime(2026, 8, 11, 23, 59, 59)

		self.assertEqual(
			capacity_balance._segment_late_qty_before_balance(
				datetime(2026, 8, 11, 8), datetime(2026, 8, 11, 18), 100, due
			),
			0,
		)
		self.assertAlmostEqual(
			capacity_balance._segment_late_qty_before_balance(
				datetime(2026, 8, 11, 20), datetime(2026, 8, 12, 4), 80, due
			),
			40,
			places=3,
		)

	def test_fixed_setup_is_part_of_reserved_machine_and_mold_interval(self):
		run = SimpleNamespace(
			horizon_start=self.start - timedelta(days=1),
			horizon_end=self.start + timedelta(days=1),
		)
		segment = {
			"name": "SEG-1",
			"workstation": "M-1",
			"mould_reference": "MOULD-1",
			"start_time": self.start,
			"end_time": self.start + timedelta(hours=2),
			"setup_minutes": 30,
			"is_locked": 1,
		}
		with patch.object(capacity_balance.frappe.db, "exists", return_value=False):
			workstation_intervals, mold_intervals = capacity_balance._get_fixed_execution_intervals(
				run, [segment]
			)

		self.assertEqual(
			workstation_intervals["M-1"],
			[(self.start - timedelta(minutes=30), self.start + timedelta(hours=2))],
		)
		self.assertEqual(mold_intervals["MOULD-1"], workstation_intervals["M-1"])

	def test_apply_locks_external_formal_occupation_rows(self):
		run = SimpleNamespace(
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=1),
		)
		with (
			patch.object(capacity_balance.frappe.db, "exists", return_value=True),
			patch.object(capacity_balance.frappe.db, "sql", return_value=[]) as sql,
		):
			capacity_balance._get_fixed_execution_intervals(
				run, [], lock_external_rows=True
			)

		query = sql.call_args.args[0]
		self.assertIn("order by si.name asc", query)
		self.assertTrue(query.rstrip().endswith("for update"))

	def test_cross_run_reservation_query_uses_only_unfinished_applied_segments(self):
		run = SimpleNamespace(
			name="RUN-2",
			company="COMPANY-A",
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=2),
		)
		with patch.object(capacity_balance.frappe.db, "sql", return_value=[]) as sql:
			results, segments = capacity_balance._get_cross_run_applied_commitments(run)

		self.assertEqual((results, segments), ({}, []))
		query = sql.call_args.args[0]
		self.assertIn("run.capacity_balance_status = 'Applied'", query)
		self.assertIn("seg.actual_status, '') != 'Completed'", query)
		self.assertIn("seg.segment_status, '') not in ('Blocked', 'Cancelled', 'Completed')", query)
		self.assertNotIn("Suggestion Ready", query)
		self.assertNotIn("seg.start_time <", query)
		self.assertNotIn("seg.end_time >", query)

	def test_cross_run_stock_claim_is_loaded_even_without_a_segment(self):
		run = SimpleNamespace(
			name="RUN-2",
			company="COMPANY-A",
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=2),
		)
		stock_only_result = capacity_balance.frappe._dict(
			{
				"name": "RESULT-STOCK-ONLY",
				"company": "COMPANY-A",
				"item_code": "FG-1",
				"demand_qty": 100,
					"available_stock_qty": 100,
					"live_net_requirement": "NR-1",
					"delivered_qty": 0,
				"reservation_run": "RUN-1",
			}
		)
		with patch.object(
			capacity_balance.frappe.db,
			"sql",
			side_effect=[[stock_only_result], []],
		) as sql:
			results, segments = capacity_balance._get_cross_run_applied_commitments(run)

		self.assertEqual(segments, [])
		self.assertIn("RESULT-STOCK-ONLY", results)
		self.assertEqual(
			capacity_balance._remaining_finished_goods_stock_claim(
				results["RESULT-STOCK-ONLY"]
			),
			100,
		)
		self.assertIn("tabAPS Schedule Result", sql.call_args_list[0].args[0])
		self.assertNotIn("tabAPS Schedule Segment", sql.call_args_list[0].args[0])

	def test_cross_run_stock_claim_survives_net_requirement_rebuild(self):
		run = SimpleNamespace(
			name="RUN-2",
			company="COMPANY-A",
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=2),
		)
		stock_only_result = capacity_balance.frappe._dict(
			{
				"name": "RESULT-STOCK-DURABLE",
				"company": "COMPANY-A",
				"item_code": "FG-1",
				"demand_qty": 0,
				"available_stock_qty": 0,
				"live_net_requirement": None,
				"fulfillment_baseline_json": json.dumps(
					{
						"version": 3,
						"net_requirement": {
							"demand_qty": 100,
							"available_stock_qty": 80,
							"open_work_order_qty": 0,
							"existing_work_order_policy": "Include",
						},
					}
				),
				"delivered_qty": 0,
				"reservation_run": "RUN-1",
			}
		)
		with patch.object(
			capacity_balance.frappe.db,
			"sql",
			side_effect=[[stock_only_result], []],
		):
			results, segments = capacity_balance._get_cross_run_applied_commitments(run)

		self.assertEqual(segments, [])
		self.assertEqual(results["RESULT-STOCK-DURABLE"]["demand_qty"], 100)
		self.assertEqual(results["RESULT-STOCK-DURABLE"]["available_stock_qty"], 80)
		self.assertEqual(
			capacity_balance._remaining_finished_goods_stock_claim(
				results["RESULT-STOCK-DURABLE"]
			),
			80,
		)

	def test_cross_run_reservation_decision_uses_company_row_lock(self):
		run = SimpleNamespace(name="RUN-1", company="COMPANY-A")
		with patch.object(capacity_balance.frappe.db, "sql") as sql:
			capacity_balance._lock_capacity_reservation_scope(run, lock=True)

		query, company = sql.call_args.args
		self.assertIn("tabCompany", query)
		self.assertIn("for update", query)
		self.assertEqual(company, "COMPANY-A")

	def test_run_lock_consumes_values_returned_by_current_read_not_second_get_doc(self):
		run = MagicMock()
		run.name = "RUN-1"
		run.company = "COMPANY-A"
		current = frappe._dict(
			name="RUN-1",
			company="COMPANY-A",
			plant_floor="PF-2",
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=2),
			status="Planned",
			approval_state="Pending",
			modified="2026-08-11 10:00:00",
			capacity_balance_status="Suggestion Ready",
			capacity_balance_fingerprint="CURRENT-FP",
			capacity_balance_analysis_json='{"analysis_fingerprint":"CURRENT-FP"}',
			capacity_balance_confirmed_by=None,
			capacity_balance_confirmed_on=None,
			capacity_balance_applied_on=None,
		)
		with patch.object(
			capacity_balance.frappe.db,
			"sql",
			side_effect=[[], [current], [frappe._dict(plant_floor="PF-2")]],
		) as sql:
			locked = capacity_balance._lock_and_refresh_capacity_run(run)

		self.assertIs(locked, run)
		self.assertEqual(run.capacity_balance_fingerprint, "CURRENT-FP")
		self.assertIn("tabCompany", sql.call_args_list[0].args[0])
		self.assertTrue(sql.call_args_list[1].args[0].rstrip().endswith("for update"))
		self.assertTrue(sql.call_args_list[2].args[0].rstrip().endswith("for update"))
		run.set.assert_called_once_with(
			"selected_plant_floors",
			[{"plant_floor": "PF-2"}],
		)

	def test_locked_run_rows_use_locking_query_payloads_and_never_get_all_snapshot(self):
		result = frappe._dict(
			name="RESULT-1",
			company="COMPANY-A",
			item_code="FG-1",
			live_net_requirement="NR-1",
			demand_qty=100,
			available_stock_qty=10,
			open_work_order_qty=0,
			existing_work_order_policy="Include",
		)
		segment = frappe._dict(
			name="SEG-1",
			parent="RESULT-1",
			linked_work_order=None,
		)
		with (
			patch.object(
				capacity_balance.frappe.db,
				"sql",
				side_effect=[[result], [segment]],
			) as sql,
			patch.object(capacity_balance.frappe, "get_all") as get_all,
		):
			results, segments = capacity_balance._get_run_balance_rows(
				"RUN-1",
				lock_rows=True,
				lock_linked_work_orders=True,
			)

		self.assertEqual(results["RESULT-1"]["demand_qty"], 100)
		self.assertEqual(segments[0]["name"], "SEG-1")
		get_all.assert_not_called()
		self.assertEqual(len(sql.call_args_list), 2)
		for call in sql.call_args_list:
			self.assertTrue(call.args[0].rstrip().endswith("for update"))
		self.assertIn("nr.demand_qty", sql.call_args_list[0].args[0])
		self.assertIn("seg.planned_qty", sql.call_args_list[1].args[0])

	def test_live_resource_guard_locks_relevant_finished_material_and_warehouse_bins(self):
		demands = [
			{
				"inventory_resource_key": "COMPANY-A|FG-1",
				"warehouse_resource_key": "FG-WH",
				"material_requirements": [
					{"item_code": "RM-1", "warehouse": "RM-WH"},
				],
			}
		]
		with patch.object(capacity_balance.frappe.db, "sql") as sql:
			capacity_balance._lock_capacity_resource_bins(
				"COMPANY-A",
				[{"item_code": "FG-2"}],
				demands,
			)

		query, params = sql.call_args.args
		self.assertIn("tabBin", query)
		self.assertIn("order by bin.name", query)
		self.assertTrue(query.rstrip().endswith("for update"))
		self.assertEqual(params["company"], "COMPANY-A")
		self.assertEqual(params["item_codes"], ("FG-1", "FG-2", "RM-1"))
		self.assertEqual(params["warehouses"], ("FG-WH", "RM-WH"))

	def test_apply_source_downtime_rows_are_locked_in_stable_order(self):
		with patch.object(capacity_balance.frappe.db, "sql") as sql:
			capacity_balance._lock_downtime_windows(
				[{"name": "DOWN-2"}, {"name": "DOWN-1"}, {"name": "DOWN-2"}]
			)

		query, params = sql.call_args.args
		self.assertIn("order by name for update", query)
		self.assertEqual(params, (("DOWN-1", "DOWN-2"),))

	def test_current_downtime_is_loaded_by_one_locking_scope_query(self):
		run = frappe._dict(
			name="RUN-1",
			company="COMPANY-A",
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=2),
		)
		with (
			patch.object(capacity_balance.frappe.db, "exists", return_value=True),
			patch.object(planning, "_get_run_selected_plant_floors", return_value=["PF-1"]),
			patch.object(capacity_balance.frappe.db, "sql", return_value=[]) as sql,
		):
			rows = capacity_balance._get_current_active_downtime_windows(run)

		self.assertEqual(rows, [])
		query, params = sql.call_args.args[:2]
		self.assertTrue(query.rstrip().endswith("for update"))
		self.assertIn("start_time < %(horizon_end)s", query)
		self.assertEqual(params["plant_floors"], ("PF-1",))

	def test_date_due_treats_every_shift_on_delivery_date_as_jit(self):
		buckets = [
			self._bucket("prebuild-day", self.start, 720),
			self._bucket("prebuild-night", self.start + timedelta(hours=12), 720),
			self._bucket("due-day", self.start + timedelta(hours=24), 720),
			self._bucket("due-night", self.start + timedelta(hours=36), 720),
			self._bucket("late-day", self.start + timedelta(hours=48), 720),
		]
		demand = self._base_demand(
			key="DATE-DUE",
			qty=200,
			due_time=self.start + timedelta(hours=39, minutes=59, seconds=59),
			due_granularity="Date",
			strategy="Force JIT",
		)
		row = balance_capacity_nodes([demand], deepcopy(buckets))["demands"][0]

		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["jit_qty"], 200)
		self.assertAlmostEqual(row["late_qty"], 0)
		self.assertEqual(
			{allocation["bucket_key"] for allocation in row["allocations"]},
			{"prebuild-night", "due-day", "due-night"},
		)
		due_start = datetime(2026, 8, 11)
		due_end = due_start + timedelta(days=1)
		self.assertTrue(
			all(due_start <= allocation["start"] < allocation["end"] <= due_end for allocation in row["allocations"])
		)

	def test_cross_midnight_night_shift_is_split_between_prebuild_jit_and_late(self):
		buckets = [
			self._bucket("previous-night", datetime(2026, 8, 10, 20), 720),
			self._bucket("due-night", datetime(2026, 8, 11, 20), 720),
		]
		demand = self._base_demand(
			qty=160,
			due_time=datetime(2026, 8, 11, 23, 59, 59),
			due_granularity="Date",
			strategy="Force JIT",
		)
		row = balance_capacity_nodes([demand], deepcopy(buckets))["demands"][0]

		self.assertAlmostEqual(row["jit_qty"], 120)
		self.assertAlmostEqual(row["late_qty"], 40)
		self.assertEqual(
			[(allocation["start"], allocation["end"], allocation["mode"]) for allocation in row["allocations"]],
			[
				(datetime(2026, 8, 11), datetime(2026, 8, 11, 8), "JIT"),
				(datetime(2026, 8, 11, 20), datetime(2026, 8, 12), "JIT"),
				(datetime(2026, 8, 12), datetime(2026, 8, 12, 4), "Late"),
			],
		)

	def test_fixed_date_segment_ignores_stale_whole_shift_mode(self):
		demand = self._base_demand(
			qty=120,
			due_time=datetime(2026, 8, 11, 23, 59, 59),
			due_granularity="Date",
		)
		demand.update(
			{
				"start_time": datetime(2026, 8, 11, 20),
				"end_time": datetime(2026, 8, 12, 8),
				"production_mode": "JIT",
			}
		)

		self.assertEqual(capacity_balance._fixed_mode_quantities(demand), (0, 40, 80))

	def test_fixed_date_segment_modes_follow_effective_partial_downtime_capacity(self):
		demand = self._base_demand(
			qty=100,
			due_time=datetime(2026, 8, 11, 23, 59, 59),
			due_granularity="Date",
		)
		demand.update(
			{
				"start_time": datetime(2026, 8, 11, 20),
				"end_time": datetime(2026, 8, 12, 8),
				"production_mode": "JIT",
				"capacity_factor_intervals": [
					{
						"start": datetime(2026, 8, 11, 20),
						"end": datetime(2026, 8, 12),
						"factor": 0.5,
					},
					{
						"start": datetime(2026, 8, 12),
						"end": datetime(2026, 8, 12, 8),
						"factor": 1,
					},
				],
			}
		)

		prebuild_qty, jit_qty, late_qty = capacity_balance._fixed_mode_quantities(demand)

		self.assertAlmostEqual(prebuild_qty, 0)
		self.assertAlmostEqual(jit_qty, 20)
		self.assertAlmostEqual(late_qty, 80)

	def test_cross_midnight_prebuild_bucket_is_clipped_not_discarded_by_posting_date(self):
		buckets = [self._bucket("previous-night", datetime(2026, 8, 10, 20), 720)]
		demand = self._base_demand(
			qty=40,
			due_time=datetime(2026, 8, 12, 23, 59, 59),
			due_granularity="Date",
			strategy="Force Prebuild",
			max_prebuild_days=1,
		)

		row = balance_capacity_nodes([demand], deepcopy(buckets))["demands"][0]

		self.assertAlmostEqual(row["prebuild_qty"], 40)
		self.assertEqual(row["allocations"][0]["start"], datetime(2026, 8, 11))
		self.assertLessEqual(row["allocations"][0]["end"], datetime(2026, 8, 11, 8))

	def test_fixed_prebuild_peak_inventory_includes_prior_shared_remaining_commitment(self):
		demands = []
		for index in (1, 2):
			demand = self._base_demand(
				key=f"FIXED-{index}",
				qty=60,
				due_time=datetime(2026, 8, 12, 23, 59, 59),
				due_granularity="Date",
			)
			demand.update(
				{
					"fixed_commitment": 1,
					"start_time": datetime(2026, 8, 11, 8 + index),
					"end_time": datetime(2026, 8, 11, 14 + index),
					"production_mode": "Prebuild",
					"resource_consumption_qty": 60,
					"inventory_resource_key": "COMPANY|FG-1",
					"inventory_room_qty": 120,
					"warehouse_resource_key": "FG-WH",
					"warehouse_room_qty": 120,
				}
			)
			demands.append(demand)

		rows = balance_capacity_nodes(demands, [])["demands"]

		self.assertEqual([row["projected_peak_inventory_qty"] for row in rows], [60, 120])

	def test_source_snapshot_covers_lineage_status_policy_threshold_and_fixed_mode(self):
		run = SimpleNamespace(
			name="RUN-1",
			modified="2026-08-11 08:00:00",
			horizon_start="2026-08-10 08:00:00",
			horizon_end="2026-08-13 08:00:00",
		)
		result = {
			"name": "RESULT-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"demand_source_snapshot_json": '[{"schedule":"SCH-1"}]',
			"fulfillment_baseline_json": '{"version":2}',
			"status": "Planned",
			"risk_status": "Normal",
			"blocking_reason": "",
		}
		demand = self._resource_snapshot_demand("SEG-1", "RM-1")
		demand.update(
			{
				"fixed_commitment": 1,
				"production_mode": "Prebuild",
				"start_time": self.start,
				"end_time": self.start + timedelta(hours=6),
				"qty": 60,
				"resource_consumption_qty": 30,
			}
		)
		baseline = capacity_balance._build_source_snapshot(
			run,
			{"RESULT-1": result},
			[],
			resource_demands=[demand],
			high_cancellation_risk_percent=60,
		)

		for label, mutate in (
			("sales order", lambda: result.__setitem__("sales_order", "SO-2")),
			("baseline", lambda: result.__setitem__("fulfillment_baseline_json", '{"version":3}')),
			("status", lambda: result.__setitem__("status", "Blocked")),
			("mode", lambda: demand.__setitem__("production_mode", "JIT")),
		):
			with self.subTest(label=label):
				before_result = deepcopy(result)
				before_demand = deepcopy(demand)
				mutate()
				changed = capacity_balance._build_source_snapshot(
					run,
					{"RESULT-1": result},
					[],
					resource_demands=[demand],
					high_cancellation_risk_percent=60,
				)
				self.assertNotEqual(capacity_balance.fingerprint(baseline), capacity_balance.fingerprint(changed))
				result.clear()
				result.update(before_result)
				demand.clear()
				demand.update(before_demand)
		changed_threshold = capacity_balance._build_source_snapshot(
			run,
			{"RESULT-1": result},
			[],
			resource_demands=[demand],
			high_cancellation_risk_percent=61,
		)
		self.assertNotEqual(
			capacity_balance.fingerprint(baseline),
			capacity_balance.fingerprint(changed_threshold),
		)

	def test_new_analysis_clears_prior_confirmation_and_applied_timestamp(self):
		run = SimpleNamespace(name="RUN-1")
		analysis = {
			"summary": {
				"blocked_demands": 0,
				"requires_confirmation": 1,
				"prebuild_qty": 10,
				"jit_qty": 20,
			},
			"demands": [],
			"analysis_fingerprint": "FP-NEW",
		}
		with patch.object(capacity_balance.frappe.db, "set_value") as set_value:
			capacity_balance._persist_capacity_analysis(run, analysis)

		values = set_value.call_args.args[2]
		self.assertIsNone(values["capacity_balance_confirmed_by"])
		self.assertIsNone(values["capacity_balance_confirmed_on"])
		self.assertIsNone(values["capacity_balance_applied_on"])

	def test_applied_plan_fingerprint_rejects_quantity_date_machine_and_lineage_changes(self):
		run = SimpleNamespace(
			name="RUN-1",
			company="COMPANY-A",
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=3),
		)
		result = {
			"name": "RESULT-1",
			"customer": "CUSTOMER-A",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"item_code": "FG-1",
			"requested_date": getdate(self.start + timedelta(days=1)),
			"planned_qty": 100,
			"production_strategy": "Auto Balance",
			"demand_confidence": "Confirmed",
			"cancellation_risk_percent": 0,
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
			"demand_source_snapshot_json": "[]",
			"fulfillment_baseline_json": '{"version":2}',
		}
		segment = {
			"name": "SEG-1",
			"parent": "RESULT-1",
			"workstation": "M-1",
			"plant_floor": "FLOOR-1",
			"start_time": self.start,
			"end_time": self.start + timedelta(hours=10),
			"planned_qty": 100,
			"setup_minutes": 10,
			"changeover_minutes": 10,
			"production_mode": "JIT",
			"mould_reference": "MOULD-1",
			"is_locked": 0,
			"anchor_strength": 10,
		}
		policy = {
			"production_strategy": "Auto Balance",
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
			"shelf_life_days": 0,
			"minimum_batch_qty": 0,
			"max_stock_qty": 0,
			"cancellation_risk_percent": 0,
		}
		with (
			patch.object(planning, "get_settings_dict", return_value={"high_cancellation_risk_percent": 60}),
			patch.object(capacity_balance, "_get_item_prebuild_policy", return_value=policy),
		):
			baseline = capacity_balance._build_applied_plan_fingerprint(
				run, {"RESULT-1": result}, [segment]
			)
			for target, fieldname, value in (
				(result, "planned_qty", 90),
				(result, "requested_date", getdate(self.start + timedelta(days=2))),
				(result, "sales_order_item", "SOI-2"),
				(segment, "workstation", "M-2"),
				(segment, "planned_qty", 90),
			):
				with self.subTest(fieldname=fieldname):
					old = target[fieldname]
					target[fieldname] = value
					changed = capacity_balance._build_applied_plan_fingerprint(
						run, {"RESULT-1": result}, [segment]
					)
					self.assertNotEqual(baseline, changed)
					target[fieldname] = old

	def test_work_order_active_and_stopped_state_changes_live_resource_snapshot(self):
		demand = self._resource_snapshot_demand("SEG-1", "RM-1")
		demand.update(
			{
				"linked_work_order": "WO-1",
				"linked_work_order_active": 1,
				"linked_work_order_stopped": 0,
				"resource_material_consumption_qty": 0,
			}
		)
		active = capacity_balance._build_shared_resource_snapshot([demand])
		demand.update(
			{
				"linked_work_order_active": 0,
				"linked_work_order_stopped": 1,
				"resource_material_consumption_qty": 60,
			}
		)
		stopped = capacity_balance._build_shared_resource_snapshot([demand])

		self.assertNotEqual(
			capacity_balance.fingerprint(active),
			capacity_balance.fingerprint(stopped),
		)

	def test_approval_gate_rejects_changed_live_resource_fingerprint(self):
		run = SimpleNamespace(
			name="RUN-1",
			company="COMPANY-A",
			capacity_balance_status="Applied",
		)
		analysis = {
			"analysis_fingerprint": "ANALYSIS-FP",
			"applied_plan_fingerprint": "PLAN-FP",
			"applied_resource_fingerprint": "RESOURCE-OLD",
		}
		with (
			patch.object(capacity_balance.frappe, "get_doc", return_value=run),
			patch.object(
				capacity_balance,
				"_lock_and_refresh_capacity_run",
				return_value=run,
			) as run_lock,
			patch.object(capacity_balance, "_load_capacity_analysis", return_value=analysis),
			patch.object(capacity_balance, "_assert_applied_plan_snapshot_current"),
			patch.object(capacity_balance, "_get_run_balance_rows", return_value=({}, [])),
			patch.object(
				capacity_balance,
				"_build_live_capacity_resource_fingerprint",
				return_value="RESOURCE-NEW",
			),
			self.assertRaises(frappe.ValidationError),
		):
			capacity_balance.assert_applied_capacity_current("RUN-1", lock_rows=True)
		run_lock.assert_called_once_with(run)

	def test_every_formal_release_entrypoint_rechecks_applied_live_capacity(self):
		blocked = frappe.ValidationError("live capacity changed")
		run_doc = frappe._dict(name="RUN-1", approval_state="Approved")
		with (
			patch.object(planning.frappe, "get_doc", return_value=run_doc),
			patch.object(planning.consistency, "assert_plan_consistent"),
			patch.object(
				planning,
				"_assert_release_capacity_current",
				side_effect=blocked,
			) as capacity_gate,
			patch.object(planning, "validate_run_mold_readiness") as mold_gate,
			self.assertRaisesRegex(frappe.ValidationError, "live capacity changed"),
		):
			planning.generate_work_order_proposals("RUN-1")
		capacity_gate.assert_called_once_with("RUN-1", lock_rows=True)
		mold_gate.assert_not_called()

		for apply_function, batch_doctype in (
			(planning._apply_work_order_proposals, "APS Work Order Proposal Batch"),
			(planning._apply_shift_schedule_proposals, "APS Shift Schedule Proposal Batch"),
		):
			batch = frappe._dict(
				name="BATCH-1",
				status="Ready For Review",
				planning_run="RUN-1",
				items=[frappe._dict(idx=1, review_status="Approved")],
			)
			with (
				self.subTest(batch_doctype=batch_doctype),
				patch.object(planning.frappe.db, "sql"),
				patch.object(planning.frappe, "get_doc", return_value=batch),
				patch.object(planning.consistency, "assert_plan_consistent"),
				patch.object(planning, "_assert_work_order_proposal_batch_fingerprint_current"),
				patch.object(planning, "_assert_shift_schedule_proposal_batch_fingerprint_current"),
				patch.object(
					planning,
					"_assert_release_capacity_current",
					side_effect=blocked,
				) as capacity_gate,
				self.assertRaisesRegex(frappe.ValidationError, "live capacity changed"),
			):
				apply_function("BATCH-1")
			capacity_gate.assert_called_once_with("RUN-1", lock_rows=True)

		context = {"run_doc": run_doc}
		with (
			patch.object(planning, "_build_shift_schedule_release_context", return_value=context),
			patch.object(planning.consistency, "assert_plan_consistent"),
			patch.object(
				planning,
				"_assert_release_capacity_current",
				side_effect=blocked,
			) as capacity_gate,
			self.assertRaisesRegex(frappe.ValidationError, "live capacity changed"),
		):
			planning.generate_shift_schedule_proposals(run_name="RUN-1")
		capacity_gate.assert_called_once_with("RUN-1", lock_rows=True)

	def test_guarded_release_invalidates_capacity_instead_of_absorbing_unproven_changes(self):
		run = frappe._dict(
			name="RUN-1",
			company="COMPANY-A",
			capacity_balance_status="Applied",
		)
		analysis = {
			"analysis_fingerprint": "ANALYSIS-FP",
			"applied_plan_fingerprint": "PLAN-FP",
			"applied_resource_fingerprint": "RESOURCE-BEFORE",
		}
		with (
			patch.object(capacity_balance.frappe, "get_doc", return_value=run),
			patch.object(
				capacity_balance,
				"_lock_and_refresh_capacity_run",
				return_value=run,
			) as run_lock,
			patch.object(capacity_balance, "_load_capacity_analysis", return_value=analysis),
			patch.object(capacity_balance, "_assert_applied_plan_snapshot_current") as plan_gate,
			patch.object(capacity_balance, "now_datetime", return_value=datetime(2026, 8, 11, 12)),
			patch.object(capacity_balance, "invalidate_capacity_balance") as invalidate,
		):
			result = capacity_balance.rebind_applied_capacity_resources_after_release(
				"RUN-1",
				reason="Work Order proposal batch WOP-1 applied",
			)

		run_lock.assert_called_once_with(run, lock_company=False)
		plan_gate.assert_called_once_with(run, analysis, lock_rows=True)
		invalidate.assert_called_once_with("RUN-1")
		self.assertEqual(result["previous_fingerprint"], "RESOURCE-BEFORE")
		self.assertIsNone(result["resource_fingerprint"])
		self.assertEqual(result["status"], "Not Analyzed")
		self.assertEqual(result["requires_reanalysis"], 1)

	def test_capacity_invalidation_clears_run_and_result_evidence(self):
		with (
			patch.object(capacity_balance.frappe.db, "set_value") as set_value,
			patch.object(capacity_balance.frappe.db, "sql") as sql,
		):
			capacity_balance.invalidate_capacity_balance("RUN-1")

		values = set_value.call_args.args[2]
		self.assertEqual(values["capacity_balance_status"], "Not Analyzed")
		self.assertIsNone(values["capacity_balance_fingerprint"])
		self.assertIsNone(values["capacity_balance_confirmed_by"])
		self.assertIsNone(values["capacity_balance_applied_on"])
		self.assertIn("capacity_balance_status = 'Not Analyzed'", sql.call_args.args[0])

	def test_cross_run_finished_goods_stock_claim_is_not_silently_reused(self):
		current_results = {
			"RESULT-NEW": {
				"name": "RESULT-NEW",
				"item_code": "FG-1",
				"requested_date": getdate(self.start + timedelta(days=1)),
				"demand_qty": 100,
				"available_stock_qty": 80,
				"delivered_qty": 0,
			}
		}
		other_results = {
			"RESULT-OLD": {
				"name": "RESULT-OLD",
				"item_code": "FG-1",
				"demand_qty": 100,
				"available_stock_qty": 80,
				"delivered_qty": 20,
			}
		}
		demands = [
			{
				"result": "RESULT-NEW",
				"current_inventory_qty": 100,
			}
		]

		conflicts = capacity_balance._find_finished_goods_stock_claim_conflicts(
			current_results, other_results, demands
		)

		# The old run still claims 80 (its stock claim, capped by remaining demand),
		# so only 20 of the physical 100 can cover the new run's claim of 80.
		self.assertEqual(conflicts, {"RESULT-NEW": 60})
		analysis = {
			"summary": {"blocked_demands": 0},
			"demands": [{"result": "RESULT-NEW", "status": "Balanced", "checks": []}],
		}
		capacity_balance._apply_finished_goods_stock_claim_blocks(analysis, conflicts)
		self.assertEqual(analysis["summary"]["blocked_demands"], 1)
		self.assertEqual(analysis["demands"][0]["checks"][0]["key"], "cross_run_finished_goods_stock")

	def test_stock_only_result_has_persisted_capacity_evidence_and_is_blocked_on_conflict(self):
		result_rows = {
			"RESULT-STOCK": {
				"name": "RESULT-STOCK",
				"item_code": "FG-1",
				"requested_date": getdate(self.start),
				"demand_qty": 80,
				"available_stock_qty": 80,
				"delivered_qty": 0,
			}
		}
		analysis = {"summary": {"demand_count": 0, "blocked_demands": 0}, "demands": []}
		capacity_balance._append_finished_goods_stock_claim_evidence(analysis, result_rows)

		self.assertEqual(analysis["summary"]["demand_count"], 1)
		self.assertEqual(analysis["demands"][0]["status"], "Balanced")
		self.assertEqual(analysis["demands"][0]["stock_claim_only"], 1)
		capacity_balance._apply_finished_goods_stock_claim_blocks(
			analysis, {"RESULT-STOCK": 30}
		)
		self.assertEqual(analysis["summary"]["blocked_demands"], 1)
		self.assertEqual(analysis["demands"][0]["status"], "Blocked")
		self.assertEqual(
			analysis["demands"][0]["checks"][-1]["key"],
			"cross_run_finished_goods_stock",
		)

	def test_safe_stock_only_result_keeps_one_non_machine_evidence_node(self):
		result_rows = {
			"RESULT-STOCK": {
				"name": "RESULT-STOCK",
				"item_code": "FG-1",
				"requested_date": getdate(self.start),
				"demand_qty": 25,
				"available_stock_qty": 25,
				"delivered_qty": 0,
			}
		}
		analysis = {"summary": {"demand_count": 0, "blocked_demands": 0}, "demands": []}
		capacity_balance._append_finished_goods_stock_claim_evidence(analysis, result_rows)
		capacity_balance._append_finished_goods_stock_claim_evidence(analysis, result_rows)
		capacity_balance._apply_finished_goods_stock_claim_blocks(analysis, {})
		self.assertEqual(len(analysis["demands"]), 1)
		self.assertEqual(analysis["summary"]["blocked_demands"], 0)

	def test_cross_run_stock_claim_changes_capacity_source_fingerprint(self):
		run = SimpleNamespace(
			name="RUN-NEW",
			modified="2026-08-11 08:00:00",
			horizon_start=self.start,
			horizon_end=self.start + timedelta(days=2),
		)
		old_result = {
			"name": "RESULT-OLD",
			"reservation_run": "RUN-OLD",
			"item_code": "FG-1",
			"demand_qty": 100,
			"available_stock_qty": 80,
			"delivered_qty": 0,
		}
		baseline = capacity_balance._build_source_snapshot(
			run,
			{},
			[],
			cross_result_rows={"RESULT-OLD": old_result},
		)
		old_result["delivered_qty"] = 30
		changed = capacity_balance._build_source_snapshot(
			run,
			{},
			[],
			cross_result_rows={"RESULT-OLD": old_result},
		)

		self.assertNotEqual(
			capacity_balance.fingerprint(baseline),
			capacity_balance.fingerprint(changed),
		)

	def test_current_finished_goods_atp_uses_locking_bin_and_safety_reads(self):
		bin_row = frappe._dict(
			item_code="FG-1",
			actual_qty=100,
			reserved_qty=20,
			reserved_stock=10,
			reserved_qty_for_production=5,
			reserved_qty_for_sub_contract=0,
			reserved_qty_for_production_plan=0,
		)
		safety_row = frappe._dict(name="FG-1", safety_stock_qty=10)
		item_meta = MagicMock()
		item_meta.has_field.return_value = True
		with (
			patch.object(
				capacity_balance,
				"_get_finished_goods_warehouses_for_update",
				return_value=["FG-WH"],
			),
			patch.object(
				capacity_balance,
				"_get_current_sales_order_reservation_credit_map",
				return_value={"FG-1": 20},
			),
			patch.object(
				capacity_balance,
				"_get_capacity_settings",
				return_value={"item_safety_stock_field": "safety_stock"},
			),
			patch.object(capacity_balance.frappe, "get_meta", return_value=item_meta),
			patch.object(
				capacity_balance.frappe.db,
				"sql",
				side_effect=[[bin_row], [safety_row]],
			) as sql,
		):
			stock = capacity_balance._get_current_customer_claimable_stock_map(
				"COMPANY-A",
				[
					{
						"item_code": "FG-1",
						"sales_order": "SO-1",
						"demand_source": "Customer Delivery Schedule",
						"qty": 100,
					}
				],
			)

		self.assertEqual(stock, {"FG-1": 85})
		self.assertEqual(len(sql.call_args_list), 2)
		for call in sql.call_args_list:
			self.assertTrue(call.args[0].rstrip().endswith("for update"))

	def test_partial_downtime_and_disjoint_fixed_occupation_are_additive(self):
		buckets = capacity_balance.build_capacity_buckets(
			["M-1"],
			self.start,
			self.start + timedelta(hours=12),
			blocked_intervals={"M-1": [(self.start, self.start + timedelta(hours=2))]},
			downtime_windows=[
				{
					"scope": "Workstation",
					"workstation": "M-1",
					"start_time": self.start + timedelta(hours=2),
					"end_time": self.start + timedelta(hours=6),
					"available_capacity_percent": 50,
				}
			],
		)

		self.assertEqual(len(buckets), 1)
		self.assertAlmostEqual(buckets[0]["initial_available_minutes"], 480)
		self.assertAlmostEqual(buckets[0]["initial_occupied_minutes"], 120)

	def test_partial_downtime_retains_factor_across_natural_day_mode_boundary(self):
		start = datetime(2026, 8, 10, 20)
		buckets = capacity_balance.build_capacity_buckets(
			["M-1"],
			start,
			datetime(2026, 8, 11, 8),
			downtime_windows=[
				{
					"scope": "Workstation",
					"workstation": "M-1",
					"start_time": start,
					"end_time": datetime(2026, 8, 11),
					"available_capacity_percent": 50,
				}
			],
		)
		demand = self._base_demand(
			qty=120,
			due_time=datetime(2026, 8, 11, 23, 59, 59),
			due_granularity="Date",
			strategy="Auto Balance",
		)

		row = balance_capacity_nodes([demand], buckets)["demands"][0]

		# 20:00-00:00 at 50% yields 20 pieces; 00:00-08:00 at 100%
		# yields 80. The old scalar bucket budget mislabeled these as 40/60.
		self.assertAlmostEqual(row["prebuild_qty"], 20)
		self.assertAlmostEqual(row["jit_qty"], 80)
		self.assertAlmostEqual(row["unscheduled_qty"], 20)
		self.assertEqual(
			[(allocation["mode"], allocation["qty"]) for allocation in row["allocations"]],
			[("Prebuild", 20), ("JIT", 80)],
		)

	def test_segment_base_rate_is_not_reduced_twice_by_partial_downtime(self):
		result = {
			"name": "RESULT-1",
			"company": "COMPANY-A",
			"plant_floor": "FLOOR-A",
			"item_code": "FG-1",
			"requested_date": getdate(self.start),
			"production_strategy": "Auto Balance",
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
		}
		segment = {
			"name": "SEG-1",
			"workstation": "M-1",
			"plant_floor": "FLOOR-A",
			"start_time": self.start,
			"end_time": self.start + timedelta(hours=12),
			# Planning stretches 60 pieces across 12 wall hours at 50%; the base
			# production rate remains 10/hour, not the observed wall rate 5/hour.
			"planned_qty": 60,
			"actual_status": "Not Started",
		}
		downtime = [
			{
				"company": "COMPANY-A",
				"scope": "Workstation",
				"workstation": "M-1",
				"start_time": self.start,
				"end_time": self.start + timedelta(hours=12),
				"available_capacity_percent": 50,
			}
		]
		policy = {
			"production_strategy": "Auto Balance",
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
			"shelf_life_days": 0,
			"minimum_batch_qty": 0,
			"stock_uom": "Nos",
		}
		with (
			patch.object(planning, "get_settings_dict", return_value={}),
			patch.object(capacity_balance, "_get_item_prebuild_policy", return_value=policy),
			patch.object(capacity_balance, "_get_item_inventory_room", return_value=(0, 100)),
			patch.object(capacity_balance, "_get_warehouse_capacity_room", return_value=("FG-WH", 100)),
			patch.object(capacity_balance, "_get_valid_plant_floor_warehouse", return_value=None),
			patch.object(capacity_balance, "_get_material_ready_qty", return_value=100),
		):
			demand = capacity_balance._build_segment_balance_demand(
				None,
				result,
				segment,
				downtime_windows=downtime,
			)

		self.assertAlmostEqual(demand["hourly_rate"], 10)
		self.assertEqual(
			demand["capacity_factor_intervals"],
			[
				{
					"start": self.start,
					"end": self.start + timedelta(hours=12),
					"factor": 0.5,
				}
			],
		)

	def test_disconnected_free_intervals_each_require_setup(self):
		buckets = capacity_balance.build_capacity_buckets(
			["M-1"],
			self.start,
			self.start + timedelta(hours=12),
			blocked_intervals={
				"M-1": [(self.start + timedelta(hours=4), self.start + timedelta(hours=8))]
			},
		)
		demand = self._base_demand(
			qty=70,
			due_time=datetime(2026, 8, 10, 23, 59, 59),
			due_granularity="Date",
			strategy="Force JIT",
			setup_minutes=60,
		)

		row = balance_capacity_nodes([demand], buckets)["demands"][0]

		# A fixed job splits the campaign into two four-hour windows. Each window
		# needs one setup, leaving three production hours (30 pieces) on each side.
		self.assertAlmostEqual(row["jit_qty"], 60)
		self.assertAlmostEqual(row["unscheduled_qty"], 10)
		self.assertEqual(
			[allocation["setup_minutes"] for allocation in row["allocations"]],
			[60, 60],
		)

	def test_latest_multi_bucket_campaign_never_produces_before_setup(self):
		buckets = []
		for index in range(2):
			start = self.start + timedelta(hours=index * 4)
			end = start + timedelta(hours=4)
			buckets.append(
				{
					"key": f"bucket-{index}",
					"workstation": "M-1",
					"start": start,
					"end": end,
					"posting_date": start.date(),
					"free_intervals": [(start, end)],
					"capacity_factor_intervals": [
						{"start": start, "end": end, "factor": 1}
					],
					"initial_available_minutes": 240,
					"remaining_budget_minutes": 240,
					"initial_occupied_minutes": 0,
				}
			)
		demand = self._base_demand(
			qty=60,
			due_time=datetime(2026, 8, 10, 23, 59, 59),
			due_granularity="Date",
			strategy="Force JIT",
			setup_minutes=60,
		)

		row = balance_capacity_nodes([demand], buckets)["demands"][0]

		self.assertAlmostEqual(row["jit_qty"], 60)
		self.assertAlmostEqual(row["unscheduled_qty"], 0)
		self.assertEqual(row["allocations"][0]["start"], self.start + timedelta(hours=2))
		self.assertEqual(row["allocations"][0]["setup_minutes"], 60)
		self.assertEqual(row["allocations"][1]["start"], self.start + timedelta(hours=4))
		self.assertEqual(row["allocations"][1]["setup_minutes"], 0)

	def test_latest_partial_factor_campaign_places_setup_before_all_output(self):
		buckets = capacity_balance.build_capacity_buckets(
			["M-1"],
			self.start,
			self.start + timedelta(hours=4),
			downtime_windows=[
				{
					"scope": "Workstation",
					"workstation": "M-1",
					"start_time": self.start,
					"end_time": self.start + timedelta(hours=2),
					"available_capacity_percent": 50,
				}
			],
		)
		demand = self._base_demand(
			qty=25,
			due_time=datetime(2026, 8, 10, 23, 59, 59),
			due_granularity="Date",
			strategy="Force JIT",
			setup_minutes=60,
		)

		row = balance_capacity_nodes([demand], buckets)["demands"][0]

		self.assertAlmostEqual(row["jit_qty"], 25)
		self.assertAlmostEqual(row["unscheduled_qty"], 0)
		self.assertEqual(row["allocations"][0]["start"], self.start + timedelta(hours=1))
		self.assertEqual(row["allocations"][0]["setup_minutes"], 60)
		self.assertAlmostEqual(row["allocations"][0]["qty"], 5)
		self.assertAlmostEqual(row["allocations"][1]["qty"], 20)

	def test_plant_floor_downtime_does_not_leak_to_another_selected_floor(self):
		buckets = capacity_balance.build_capacity_buckets(
			["M-A", "M-B"],
			self.start,
			self.start + timedelta(hours=12),
			downtime_windows=[
				{
					"scope": "Plant Floor",
					"plant_floor": "FLOOR-A",
					"start_time": self.start,
					"end_time": self.start + timedelta(hours=12),
					"available_capacity_percent": 0,
				}
			],
			workstation_plant_floors={"M-A": "FLOOR-A", "M-B": "FLOOR-B"},
		)

		available = {row["workstation"]: row["initial_available_minutes"] for row in buckets}
		self.assertEqual(available, {"M-A": 0, "M-B": 720})

	def test_fixed_started_segment_consumes_shared_resources_before_flexible_work(self):
		demands, buckets = self._shared_resource_fixture()
		fixed, flexible = demands
		fixed.update(
			{
				"fixed_commitment": 1,
				"production_mode": "Prebuild",
				"start_time": self.start,
				"end_time": self.start + timedelta(hours=8),
				"resource_consumption_qty": 60,
			}
		)
		for demand in demands:
			demand.update(
				{
					"inventory_resource_key": "COMPANY|FG-1",
					"inventory_room_qty": 100,
					"warehouse_resource_key": "FG-WH",
					"warehouse_room_qty": 100,
					"material_ready_qty": 100,
					"material_requirements": [
						{
							"resource_key": "COMPANY|*|RM-1",
							"available_qty": 100,
							"qty_per_unit": 1,
						}
					],
				}
			)
		result = balance_capacity_nodes(demands, buckets)

		self.assertEqual(result["demands"][0]["fixed_commitment"], 1)
		self.assertAlmostEqual(result["demands"][0]["prebuild_qty"], 80)
		self.assertAlmostEqual(result["demands"][1]["prebuild_qty"], 40)
		self.assertAlmostEqual(result["demands"][1]["unscheduled_qty"], 40)
		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 120)

	def test_cross_run_reservation_consumes_resources_without_inflating_current_run_totals(self):
		demands, buckets = self._shared_resource_fixture()
		reserved, current = demands
		reserved.update(
			{
				"fixed_commitment": 1,
				"production_mode": "Prebuild",
				"start_time": self.start,
				"end_time": self.start + timedelta(hours=8),
				"resource_consumption_qty": 60,
			}
		)
		for demand in demands:
			demand.update(
				{
					"inventory_resource_key": "COMPANY|FG-1",
					"inventory_room_qty": 100,
					"warehouse_resource_key": "FG-WH",
					"warehouse_room_qty": 100,
					"material_ready_qty": 100,
					"material_requirements": [
						{
							"resource_key": "COMPANY|*|RM-1",
							"available_qty": 100,
							"qty_per_unit": 1,
						}
					],
				}
			)

		result = balance_capacity_nodes([current], buckets, reserved_demands=[reserved])

		self.assertEqual(len(result["demands"]), 1)
		self.assertAlmostEqual(result["demands"][0]["prebuild_qty"], 40)
		self.assertAlmostEqual(result["summary"]["planned_qty"], 80)
		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 40)

	def test_cross_run_material_credit_is_explicit_and_not_inferred_from_link(self):
		base_demand = {
			"key": "SEG-OTHER",
			"material_requirements": [
				{"resource_key": "COMPANY|*|RM-1", "available_qty": 100, "qty_per_unit": 1}
			],
		}
		result = {"reservation_run": "RUN-OTHER"}
		segment = {
			"name": "SEG-OTHER",
			"planned_qty": 80,
			"actual_completed_qty": 20,
			"linked_work_order": "WO-1",
			"linked_work_order_active": 1,
		}
		with patch.object(
			capacity_balance,
			"_build_segment_balance_demand",
			side_effect=lambda *_args: deepcopy(base_demand),
		):
			linked = capacity_balance._build_cross_run_reservation_demand(
				None, result, segment, material_credit_qty=60
			)
			unlinked = capacity_balance._build_cross_run_reservation_demand(
				None, result, {**segment, "linked_work_order": None}
			)

		self.assertEqual(linked["resource_consumption_qty"], 60)
		self.assertEqual(linked["resource_material_consumption_qty"], 0)
		self.assertEqual(unlinked["resource_material_consumption_qty"], 60)
		self.assertNotEqual(
			capacity_balance.fingerprint(capacity_balance._build_shared_resource_snapshot([linked])),
			capacity_balance.fingerprint(capacity_balance._build_shared_resource_snapshot([unlinked])),
		)

	def test_current_active_linked_work_order_is_fixed_but_link_alone_gets_no_material_credit(self):
		result = {
			"name": "RESULT-1",
			"company": "COMPANY-A",
			"plant_floor": "FLOOR-A",
			"item_code": "FG-1",
			"requested_date": datetime(2026, 8, 11).date(),
			"production_strategy": "Auto Balance",
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
		}
		segment = {
			"name": "SEG-1",
			"workstation": "M-1",
			"start_time": self.start,
			"end_time": self.start + timedelta(hours=8),
			"planned_qty": 80,
			"actual_completed_qty": 20,
			"actual_status": "Not Started",
			"linked_work_order": "WO-1",
			"linked_work_order_active": 1,
		}
		policy = {
			"production_strategy": "Auto Balance",
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
			"shelf_life_days": 0,
			"minimum_batch_qty": 0,
			"stock_uom": "Nos",
		}
		with (
			patch.object(planning, "get_settings_dict", return_value={}),
			patch.object(capacity_balance, "_get_item_prebuild_policy", return_value=policy),
			patch.object(capacity_balance, "_get_item_inventory_room", return_value=(0, 100)),
			patch.object(capacity_balance, "_get_warehouse_capacity_room", return_value=("FG-WH", 100)),
			patch.object(capacity_balance, "_get_valid_plant_floor_warehouse", return_value="RM-WH"),
			patch.object(capacity_balance, "_get_material_ready_qty", return_value=100),
		):
			demand = capacity_balance._build_segment_balance_demand(None, result, segment)

		self.assertTrue(capacity_balance._is_fixed_segment(segment))
		self.assertEqual(demand["fixed_commitment"], 1)
		self.assertEqual(demand["resource_consumption_qty"], 60)
		self.assertEqual(demand["resource_material_consumption_qty"], 60)

	def test_exact_existing_work_order_material_credit_is_consumed_once_across_segments(self):
		results = {
			"RESULT-1": {
				"name": "RESULT-1",
				"open_work_order_qty": 60,
			}
		}
		segments = [
			{"name": "SEG-1", "parent": "RESULT-1", "idx": 1, "planned_qty": 50},
			{"name": "SEG-2", "parent": "RESULT-1", "idx": 2, "planned_qty": 50},
		]

		def build(_run, _result, segment, **_kwargs):
			return {
				"key": segment["name"],
				"segment": segment["name"],
				"qty": segment["planned_qty"],
				"resource_consumption_qty": None,
				"material_requirements": [
					{
						"resource_key": "COMPANY|RM-WH|RM-1",
						"item_code": "RM-1",
						"warehouse": "RM-WH",
						"qty_per_unit": 1,
					}
				],
			}

		with (
			patch.object(capacity_balance, "_build_segment_balance_demand", side_effect=build),
			patch.object(
				capacity_balance,
				"_get_proven_existing_work_order_material_credit",
				return_value=60,
			) as proof,
		):
			demands = capacity_balance._build_segment_balance_demands(
				None, results, segments
			)

		self.assertEqual(
			[row["existing_work_order_material_credit_qty"] for row in demands],
			[50, 10],
		)
		self.assertEqual(
			[row["resource_material_consumption_qty"] for row in demands],
			[0, 40],
		)
		proof.assert_called_once()

	def test_existing_work_order_without_source_warehouse_gets_no_material_credit(self):
		result = {
			"company": "COMPANY-A",
			"item_code": "FG-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"open_work_order_qty": 100,
		}
		requirements = [
			{
				"resource_key": "COMPANY-A|RM-WH|RM-1",
				"item_code": "RM-1",
				"warehouse": "RM-WH",
				"qty_per_unit": 1,
			}
		]
		database = MagicMock()
		database.sql.side_effect = [
			[frappe._dict(name="WO-1", qty=100, skip_transfer=0)],
			[
				frappe._dict(
					item_code="RM-1",
					source_warehouse=None,
					required_qty=100,
					transferred_qty=0,
					consumed_qty=0,
				)
			],
		]
		with patch.object(capacity_balance.frappe, "db", database):
			credit = capacity_balance._get_proven_existing_work_order_material_credit(
				result, requirements
			)

		self.assertEqual(credit, 0)
		self.assertEqual(database.sql.call_count, 2)

	def test_existing_work_order_credit_proof_uses_current_reads_for_wo_items_and_bin(self):
		result = {
			"company": "COMPANY-A",
			"item_code": "FG-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"open_work_order_qty": 100,
		}
		requirements = [
			{
				"resource_key": "COMPANY-A|RM-WH|RM-1",
				"item_code": "RM-1",
				"warehouse": "RM-WH",
				"qty_per_unit": 1,
			}
		]
		database = MagicMock()
		database.sql.side_effect = [
			[frappe._dict(name="WO-1", qty=100, skip_transfer=0)],
			[
				frappe._dict(
					item_code="RM-1",
					source_warehouse="RM-WH",
					required_qty=100,
					transferred_qty=0,
					consumed_qty=0,
				)
			],
			[frappe._dict(bin_reserved=100, expected_reserved=100)],
		]
		with patch.object(capacity_balance.frappe, "db", database):
			credit = capacity_balance._get_proven_existing_work_order_material_credit(
				result,
				requirements,
				lock_rows=True,
			)

		self.assertEqual(credit, 100)
		self.assertEqual(database.sql.call_count, 3)
		for call in database.sql.call_args_list:
			self.assertTrue(call.args[0].rstrip().endswith("for update"))

	def test_closed_linked_work_order_does_not_freeze_segment(self):
		segments = [
			{"name": "ACTIVE", "linked_work_order": "WO-A"},
			{"name": "CLOSED", "linked_work_order": "WO-C"},
			{"name": "STOPPED", "linked_work_order": "WO-S"},
		]
		with patch.object(
			capacity_balance.frappe,
			"get_all",
			return_value=[
				SimpleNamespace(name="WO-A", docstatus=1, status="In Process"),
				SimpleNamespace(name="WO-C", docstatus=1, status="Closed"),
				SimpleNamespace(name="WO-S", docstatus=1, status="Stopped"),
			],
		):
			capacity_balance._annotate_active_linked_work_orders(segments)

		self.assertEqual(segments[0]["linked_work_order_active"], 1)
		self.assertEqual(segments[1]["linked_work_order_active"], 0)
		self.assertEqual(segments[2]["linked_work_order_active"], 0)
		self.assertTrue(capacity_balance._is_fixed_segment(segments[0]))
		self.assertFalse(capacity_balance._is_fixed_segment(segments[1]))
		self.assertTrue(capacity_balance._is_fixed_segment(segments[2]))

	def test_stopped_work_order_is_preserved_but_blocks_capacity_and_gets_no_material_credit(self):
		demand = self._base_demand(
			qty=60,
			due_time=datetime(2026, 8, 12, 23, 59, 59),
			due_granularity="Date",
		)
		demand.update(
			{
				"fixed_commitment": 1,
				"linked_work_order_stopped": 1,
				"start_time": self.start,
				"end_time": self.start + timedelta(hours=6),
				"resource_consumption_qty": 60,
				"resource_material_consumption_qty": 60,
				"material_ready_qty": 60,
			}
		)

		row = capacity_balance._build_fixed_commitment_result(demand)

		self.assertEqual(row["status"], "Blocked")
		self.assertIn("Stopped Work Order", row["checks"][-1]["message"])

	def test_apply_locks_linked_work_order_before_material_credit(self):
		segments = [{"name": "SEG-1", "linked_work_order": "WO-1"}]
		with patch.object(
			capacity_balance.frappe.db,
			"sql",
			return_value=[SimpleNamespace(name="WO-1", docstatus=1, status="In Process")],
		) as sql:
			capacity_balance._annotate_active_linked_work_orders(segments, lock_rows=True)

		query, params = sql.call_args.args
		self.assertIn("order by name", query)
		self.assertIn("for update", query)
		self.assertEqual(params, (("WO-1",),))
		self.assertEqual(segments[0]["linked_work_order_active"], 1)

	def test_date_due_without_same_day_shift_can_use_prebuild_capacity(self):
		buckets = [
			self._bucket("previous-day", self.start, 720),
			self._bucket("following-day", self.start + timedelta(hours=48), 720),
		]
		demand = self._base_demand(
			key="DATE-NO-SHIFT",
			qty=100,
			due_time=self.start + timedelta(hours=39, minutes=59, seconds=59),
			due_granularity="Date",
		)
		row = balance_capacity_nodes([demand], deepcopy(buckets))["demands"][0]

		self.assertEqual(row["status"], "Balanced")
		self.assertAlmostEqual(row["prebuild_qty"], 100)
		self.assertAlmostEqual(row["jit_qty"], 0)
		self.assertAlmostEqual(row["late_qty"], 0)

	def test_force_jit_without_same_day_shift_uses_late_capacity(self):
		buckets = [
			self._bucket("previous-day", self.start, 720),
			self._bucket("following-day", self.start + timedelta(hours=48), 720),
		]
		demand = self._base_demand(
			key="DATE-NO-SHIFT-FORCE-JIT",
			qty=100,
			due_time=self.start + timedelta(hours=39, minutes=59, seconds=59),
			due_granularity="Date",
			strategy="Force JIT",
		)
		row = balance_capacity_nodes([demand], deepcopy(buckets))["demands"][0]

		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["jit_qty"], 0)
		self.assertAlmostEqual(row["late_qty"], 100)
		self.assertAlmostEqual(row["unscheduled_qty"], 0)
		self.assertEqual([allocation["bucket_key"] for allocation in row["allocations"]], ["following-day"])

	def test_duplicate_bom_component_rows_are_aggregated_before_readiness(self):
		requirements = capacity_balance._aggregate_bom_component_requirements(
			"COMPANY-A",
			2,
			[
				SimpleNamespace(item_code="RESIN-A", source_warehouse=None, qty=1),
				SimpleNamespace(item_code="RESIN-A", source_warehouse=None, qty=3),
			],
		)

		self.assertEqual(len(requirements), 1)
		self.assertEqual(requirements[0]["resource_key"], "COMPANY-A|*|RESIN-A")
		self.assertAlmostEqual(requirements[0]["qty_per_unit"], 2)

	def test_fractional_bom_output_preserves_material_unit_ratio(self):
		requirements = capacity_balance._aggregate_bom_component_requirements(
			"COMPANY-A",
			0.5,
			[SimpleNamespace(item_code="RESIN-A", source_warehouse=None, qty=1)],
		)

		self.assertEqual(len(requirements), 1)
		self.assertAlmostEqual(requirements[0]["qty_per_unit"], 2)

	def test_bom_component_without_warehouse_uses_configured_source_warehouse(self):
		requirements = capacity_balance._aggregate_bom_component_requirements(
			"COMPANY-A",
			1,
			[SimpleNamespace(item_code="RESIN-A", source_warehouse=None, qty=1)],
			default_warehouse="SOURCE-WH",
		)

		self.assertEqual(
			{requirement["resource_key"] for requirement in requirements},
			{"COMPANY-A|*|RESIN-A", "COMPANY-A|SOURCE-WH|RESIN-A"},
		)

	def test_explicit_untyped_source_does_not_pollute_raw_material_wildcard_pool(self):
		requirements = capacity_balance._aggregate_bom_component_requirements(
			"COMPANY-A",
			1,
			[SimpleNamespace(item_code="RESIN-A", source_warehouse="WIP-WH", qty=1)],
			raw_material_warehouses=set(),
		)

		self.assertEqual(
			{requirement["resource_key"] for requirement in requirements},
			{"COMPANY-A|WIP-WH|RESIN-A"},
		)

	def test_component_without_source_warehouse_is_limited_to_company(self):
		original_db = getattr(frappe.local, "db", None)
		fake_db = MagicMock()
		fake_db.sql.return_value = [SimpleNamespace(qty=75)]
		frappe.local.db = fake_db
		try:
			available = capacity_balance._get_component_available_qty("COMPANY-A", "RESIN-A", None)
		finally:
			if original_db is None:
				del frappe.local.db
			else:
				frappe.local.db = original_db

		self.assertAlmostEqual(available, 75)
		query, params = fake_db.sql.call_args.args[:2]
		self.assertIn("wh.company = %s", query)
		self.assertIn("reserved_qty_for_production", query)
		self.assertIn("reserved_qty_for_production_plan", query)
		self.assertIn("reserved_stock", query)
		self.assertIn("greatest(ifnull(bin.reserved_qty, 0)", query)
		self.assertIn("ifnull(wh.disabled, 0) = 0", query)
		self.assertIn("wh.warehouse_type = 'Raw Material'", query)
		self.assertNotIn("warehouse_type = 'Finished Goods'", query)
		self.assertEqual(params, ("RESIN-A", "COMPANY-A"))

	def test_component_source_warehouse_is_still_scoped_to_company(self):
		original_db = getattr(frappe.local, "db", None)
		fake_db = MagicMock()
		fake_db.sql.return_value = [SimpleNamespace(qty=25)]
		frappe.local.db = fake_db
		try:
			available = capacity_balance._get_component_available_qty("COMPANY-A", "RESIN-A", "RM-WH")
		finally:
			if original_db is None:
				del frappe.local.db
			else:
				frappe.local.db = original_db

		self.assertAlmostEqual(available, 25)
		query, params = fake_db.sql.call_args.args[:2]
		self.assertIn("wh.company = %s", query)
		self.assertIn("bin.warehouse = %s", query)
		self.assertNotIn("wh.warehouse_type = 'Raw Material'", query)
		self.assertEqual(params, ("RESIN-A", "COMPANY-A", "RM-WH"))

	def test_component_current_read_uses_locking_aggregate_after_bin_lock(self):
		original_db = getattr(frappe.local, "db", None)
		fake_db = MagicMock()
		fake_db.sql.return_value = [frappe._dict(qty=25)]
		frappe.local.db = fake_db
		try:
			available = capacity_balance._get_component_available_qty(
				"COMPANY-A",
				"RESIN-A",
				None,
				current_read=True,
			)
		finally:
			if original_db is None:
				del frappe.local.db
			else:
				frappe.local.db = original_db

		self.assertEqual(available, 25)
		query = fake_db.sql.call_args.args[0]
		self.assertTrue(query.rstrip().endswith("for update"))
		self.assertIn("wh.warehouse_type = 'Raw Material'", query)

	def test_item_inventory_room_uses_only_explicit_finished_goods_scope(self):
		original_db = getattr(frappe.local, "db", None)
		fake_db = MagicMock()
		fake_db.sql.return_value = [SimpleNamespace(qty=30)]
		frappe.local.db = fake_db
		try:
			with patch(
				"injection_aps.services.availability._get_finished_goods_warehouses",
				return_value=["FG-A", "FG-B"],
			):
				current, room = capacity_balance._get_item_inventory_room("COMPANY-A", "FG-ITEM", 100)
		finally:
			if original_db is None:
				del frappe.local.db
			else:
				frappe.local.db = original_db

		self.assertEqual((current, room), (30, 70))
		query, params = fake_db.sql.call_args.args[:2]
		self.assertIn("bin.warehouse in %(warehouses)s", query)
		self.assertEqual(params["warehouses"], ["FG-A", "FG-B"])
		self.assertEqual(params["company"], "COMPANY-A")

	def test_missing_finished_goods_scope_blocks_prebuild_inventory_room(self):
		original_db = getattr(frappe.local, "db", None)
		fake_db = MagicMock()
		frappe.local.db = fake_db
		try:
			with patch(
				"injection_aps.services.availability._get_finished_goods_warehouses",
				return_value=[],
			):
				current, room = capacity_balance._get_item_inventory_room("COMPANY-A", "FG-ITEM", 100)
		finally:
			if original_db is None:
				del frappe.local.db
			else:
				frappe.local.db = original_db

		self.assertEqual((current, room), (0, 0))
		fake_db.sql.assert_not_called()

	def test_mixed_stock_uoms_block_raw_warehouse_capacity_arithmetic(self):
		original_db = getattr(frappe.local, "db", None)
		fake_db = MagicMock()
		fake_db.get_value.side_effect = [
			"FG-WH",
			SimpleNamespace(company="COMPANY-A", is_group=0, disabled=0, custom_aps_capacity_qty=100),
			"Kg",
		]
		fake_db.sql.return_value = [
			SimpleNamespace(stock_uom="Nos", qty=20),
			SimpleNamespace(stock_uom="Kg", qty=10),
		]
		frappe.local.db = fake_db
		try:
			warehouse, room = capacity_balance._get_warehouse_capacity_room(
				"COMPANY-A", None, "FG-ITEM", {}
			)
		finally:
			if original_db is None:
				del frappe.local.db
			else:
				frappe.local.db = original_db

		self.assertEqual(warehouse, "FG-WH")
		self.assertEqual(room, 0)

	def test_same_item_demands_share_inventory_limit(self):
		demands, buckets = self._shared_resource_fixture()
		for demand in demands:
			demand.update(
				{
					"inventory_room_qty": 100,
					"inventory_resource_key": "COMPANY|ITEM-A",
				}
			)
		result = balance_capacity_nodes(demands, buckets)
		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 100)
		self.assertEqual([row["prebuild_qty"] for row in result["demands"]], [80, 20])
		self.assertEqual([row["projected_peak_inventory_qty"] for row in result["demands"]], [80, 100])

	def test_different_items_share_finished_goods_warehouse_capacity(self):
		demands, buckets = self._shared_resource_fixture()
		for index, demand in enumerate(demands, start=1):
			demand.update(
				{
					"inventory_resource_key": f"COMPANY|ITEM-{index}",
					"warehouse_room_qty": 100,
					"warehouse_resource_key": "FG-WAREHOUSE",
				}
			)
		result = balance_capacity_nodes(demands, buckets)
		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 100)
		self.assertEqual([row["prebuild_qty"] for row in result["demands"]], [80, 20])

	def test_different_finished_items_share_bom_component_balance(self):
		demands, buckets = self._shared_resource_fixture()
		for index, demand in enumerate(demands, start=1):
			demand.update(
				{
					"inventory_resource_key": f"COMPANY|ITEM-{index}",
					"warehouse_resource_key": f"FG-WAREHOUSE-{index}",
					"material_ready_qty": 120,
					"material_requirements": [
						{
							"resource_key": "COMPANY|RM-WAREHOUSE|RESIN-A",
							"available_qty": 120,
							"qty_per_unit": 1,
						}
					],
				}
			)
		result = balance_capacity_nodes(demands, buckets)
		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 120)
		self.assertEqual([row["prebuild_qty"] for row in result["demands"]], [80, 40])

	def test_source_warehouse_and_wildcard_boms_share_company_component_pool(self):
		demands, buckets = self._shared_resource_fixture()
		specified_requirements = capacity_balance._aggregate_bom_component_requirements(
			"COMPANY-A",
			1,
			[SimpleNamespace(item_code="RESIN-A", source_warehouse="RM-WH", qty=1)],
		)
		wildcard_requirements = capacity_balance._aggregate_bom_component_requirements(
			"COMPANY-A",
			1,
			[SimpleNamespace(item_code="RESIN-A", source_warehouse=None, qty=1)],
		)
		for requirement in specified_requirements:
			requirement["available_qty"] = 100 if requirement["warehouse"] is None else 80
		for requirement in wildcard_requirements:
			requirement["available_qty"] = 100
		demands[0]["material_ready_qty"] = 80
		demands[0]["material_requirements"] = specified_requirements
		demands[1]["material_ready_qty"] = 100
		demands[1]["material_requirements"] = wildcard_requirements

		result = balance_capacity_nodes(demands, buckets)

		self.assertEqual(
			{requirement["resource_key"] for requirement in specified_requirements},
			{"COMPANY-A|*|RESIN-A", "COMPANY-A|RM-WH|RESIN-A"},
		)
		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 100)
		self.assertEqual([row["prebuild_qty"] for row in result["demands"]], [80, 20])

	def test_force_jit_demands_cannot_reuse_shared_material(self):
		demands, buckets = self._shared_resource_fixture()
		for demand in demands:
			demand["strategy"] = "Force JIT"
			demand["material_ready_qty"] = 100
			demand["material_requirements"] = [
				{
					"resource_key": "COMPANY|*|RESIN-A",
					"available_qty": 100,
					"qty_per_unit": 1,
				}
			]

		result = balance_capacity_nodes(demands, buckets)

		self.assertAlmostEqual(result["summary"]["jit_qty"], 100)
		self.assertAlmostEqual(result["summary"]["unscheduled_qty"], 60)
		self.assertEqual([row["jit_qty"] for row in result["demands"]], [80, 20])
		self.assertEqual([row["unscheduled_qty"] for row in result["demands"]], [0, 60])
		self.assertEqual(result["demands"][1]["status"], "Blocked")

	def test_urgent_demand_receives_scarce_shared_material_first(self):
		demands, buckets = self._shared_resource_fixture()
		for demand in demands:
			demand["strategy"] = "Force JIT"
			demand["material_ready_qty"] = 100
			demand["material_requirements"] = [
				{
					"resource_key": "COMPANY|*|RESIN-A",
					"available_qty": 100,
					"qty_per_unit": 1,
				}
			]
		demands[1]["priority"] = 100

		result = balance_capacity_nodes(demands, buckets)

		self.assertEqual([row["key"] for row in result["demands"]], ["D-2", "D-1"])
		self.assertEqual([row["jit_qty"] for row in result["demands"]], [80, 20])
		self.assertEqual([row["unscheduled_qty"] for row in result["demands"]], [0, 60])

	def test_mixed_prebuild_and_jit_consume_material_but_only_prebuild_consumes_warehouse_room(self):
		demands, buckets = self._shared_resource_fixture()
		demands[0]["strategy"] = "Auto Balance"
		demands[1]["strategy"] = "Force JIT"
		for demand in demands:
			demand["warehouse_resource_key"] = "FG-WAREHOUSE"
			demand["warehouse_room_qty"] = 30
			demand["material_ready_qty"] = 100
			demand["material_requirements"] = [
				{
					"resource_key": "COMPANY|*|RESIN-A",
					"available_qty": 100,
					"qty_per_unit": 1,
				}
			]
		for bucket in buckets:
			if bucket["key"] == "M-1-12":
				bucket["initial_available_minutes"] = 300
				bucket["remaining_budget_minutes"] = 300

		result = balance_capacity_nodes(demands, buckets)

		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 30)
		self.assertAlmostEqual(result["summary"]["jit_qty"], 70)
		self.assertAlmostEqual(result["summary"]["unscheduled_qty"], 60)
		self.assertEqual(
			[(row["prebuild_qty"], row["jit_qty"], row["unscheduled_qty"]) for row in result["demands"]],
			[(30, 50, 0), (0, 20, 60)],
		)

	def test_stock_retained_jit_consumes_shared_finished_goods_room(self):
		demands, buckets = self._shared_resource_fixture()
		for demand in demands:
			demand.update(
				{
					"strategy": "Force JIT",
					"stock_retained": 1,
					"inventory_resource_key": "COMPANY|STOCK-ITEM",
					"inventory_room_qty": 100,
					"warehouse_resource_key": "FG-WAREHOUSE",
					"warehouse_room_qty": 100,
					"material_ready_qty": 200,
				}
			)

		result = balance_capacity_nodes(demands, buckets)

		self.assertEqual([row["jit_qty"] for row in result["demands"]], [80, 20])
		self.assertEqual([row["unscheduled_qty"] for row in result["demands"]], [0, 60])
		self.assertAlmostEqual(result["summary"]["jit_qty"], 100)
		self.assertAlmostEqual(result["summary"]["unscheduled_qty"], 60)

	def test_shared_warehouse_with_different_target_uoms_blocks_prebuild(self):
		demands, buckets = self._shared_resource_fixture()
		for demand, stock_uom in zip(demands, ("Nos", "Kg"), strict=True):
			demand["warehouse_resource_key"] = "FG-WAREHOUSE"
			demand["warehouse_room_qty"] = 100
			demand["target_stock_uom"] = stock_uom

		result = balance_capacity_nodes(demands, buckets)

		self.assertAlmostEqual(result["summary"]["prebuild_qty"], 0)
		for row in result["demands"]:
			self.assertAlmostEqual(row["prebuild_qty"], 0)
			self.assertIn(
				"warehouse_stock_uom",
				{check["key"] for check in row["checks"] if check["status"] == "blocked"},
			)

	def _run(self, node1_minutes, node2_minutes, qty, **overrides):
		buckets = [
			self._bucket("node-1", self.start, node1_minutes),
			self._bucket("node-2", self.start + timedelta(hours=12), node2_minutes),
			self._bucket("node-3", self.start + timedelta(hours=24), 720),
		]
		demand = self._base_demand(qty=qty)
		demand.update(overrides)
		return balance_capacity_nodes([demand], deepcopy(buckets))

	def _base_demand(self, **overrides):
		demand = {
			"key": "D-1",
			"result": "R-1",
			"segment": "S-1",
			"workstation": "M-1",
			"mould_reference": "MOULD-1",
			"qty": 100,
			"hourly_rate": 10,
			"setup_minutes": 0,
			"due_time": self.start + timedelta(hours=24),
			"strategy": "Auto Balance",
			"demand_confidence": "Confirmed",
			"cancellation_risk_percent": 0,
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
			"shelf_life_days": 30,
			"minimum_batch_qty": 0,
			"current_inventory_qty": 0,
			"inventory_room_qty": 1000,
			"warehouse_room_qty": 1000,
			"material_ready_qty": 1000,
			"overstock_risk": 0,
		}
		demand.update(overrides)
		return demand

	def _shared_resource_fixture(self):
		demands = []
		buckets = []
		for index in (1, 2):
			workstation = f"M-{index}"
			demand = self._base_demand(
				key=f"D-{index}",
				result=f"R-{index}",
				segment=f"S-{index}",
				workstation=workstation,
				mould_reference=None,
				qty=80,
				strategy="Force Prebuild",
				inventory_resource_key=f"ITEM-{index}",
				warehouse_resource_key=f"WAREHOUSE-{index}",
			)
			demands.append(demand)
			for offset in (0, 12, 24):
				bucket = self._bucket(f"{workstation}-{offset}", self.start + timedelta(hours=offset), 720)
				bucket["workstation"] = workstation
				buckets.append(bucket)
		return demands, deepcopy(buckets)

	def _resource_snapshot_demand(self, key, material_key):
		return {
			"key": key,
			"result": f"R-{key}",
			"segment": key,
			"priority": 0,
			"current_inventory_qty": 10,
			"inventory_room_qty": 90,
			"inventory_resource_key": "COMPANY|FG-1",
			"warehouse_room_qty": 80,
			"warehouse_resource_key": "FG-WH-1",
			"target_stock_uom": "Nos",
			"material_ready_qty": 70,
			"material_requirements": [
				{
					"resource_key": f"COMPANY|*|{material_key}",
					"qty_per_unit": 1,
					"available_qty": 60,
				},
				{
					"resource_key": f"COMPANY|SOURCE-WH|{material_key}",
					"qty_per_unit": 1,
					"available_qty": 50,
				},
			],
		}

	def _bucket(self, key, start, available_minutes):
		end = start + timedelta(hours=12)
		return {
			"key": key,
			"workstation": "M-1",
			"start": start,
			"end": end,
			"posting_date": start.date(),
			"shift_type": "白班" if start.hour == 8 else "晚班",
			"free_intervals": [(start, end)],
			"initial_available_minutes": available_minutes,
			"remaining_budget_minutes": available_minutes,
			"initial_occupied_minutes": 720 - available_minutes,
		}


class TestCapacityBalanceTransactions(FrappeTestCase):
	def setUp(self):
		required_doctypes = ["APS Planning Run", "APS Schedule Result", "APS Schedule Segment"]
		if any(not frappe.db.exists("DocType", doctype) for doctype in required_doctypes):
			self.skipTest("Phase 4 capacity DocTypes are not synced.")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		self.item = frappe.db.get_value("Item", {"disabled": 0}) or frappe.db.get_value("Item", {})
		self.workstation = frappe.db.get_value("Workstation", {})
		if not self.company or not self.customer or not self.item or not self.workstation:
			self.skipTest("Capacity tests need a Company, Customer, Item, and Workstation.")
		self.plant_floor = frappe.db.get_value("Workstation", self.workstation, "plant_floor")
		self.fixture = self._create_fixture()

	def test_analysis_apply_and_replay_split_real_segments_once(self):
		analysis = self._analyze()
		demand = analysis["demands"][0]
		self.assertGreater(demand["prebuild_qty"], 0)
		self.assertGreater(demand["jit_qty"], 0)
		self.assertAlmostEqual(demand["prebuild_qty"] + demand["jit_qty"], 100)
		self.assertEqual(
			frappe.db.get_value("APS Planning Run", self.fixture["run"].name, "capacity_balance_status"),
			"Suggestion Ready",
		)

		applied = capacity_balance.apply_capacity_balance(self.fixture["run"].name)
		self.assertEqual(applied["status"], "Applied")
		self.assertEqual(applied["overlap_count"], 0)
		self.assertEqual(applied["mold_overlap_count"], 0)
		segments = frappe.get_all(
			"APS Schedule Segment",
			filters={"parent": self.fixture["result"].name, "segment_kind": "Primary"},
			fields=["name", "planned_qty", "production_mode"],
		)
		self.assertGreaterEqual(len(segments), 2)
		self.assertAlmostEqual(sum(row.planned_qty for row in segments), 100)
		self.assertIn("Prebuild", {row.production_mode for row in segments})
		self.assertIn("JIT", {row.production_mode for row in segments})

		replay = capacity_balance.apply_capacity_balance(self.fixture["run"].name)
		self.assertEqual(replay["idempotent_replay"], 1)
		self.assertEqual(
			frappe.db.count(
				"APS Schedule Segment",
				{"parent": self.fixture["result"].name, "segment_kind": "Primary"},
			),
			len(segments),
		)

	def test_forecast_prebuild_requires_explicit_pmc_confirmation(self):
		frappe.db.set_value(
			"APS Schedule Result",
			self.fixture["result"].name,
			"demand_confidence",
			"Forecast",
		)
		analysis = self._analyze()
		self.assertEqual(analysis["summary"]["requires_confirmation"], 1)
		with self.assertRaises(frappe.ValidationError):
			capacity_balance.apply_capacity_balance(self.fixture["run"].name)
		confirmed = capacity_balance.confirm_capacity_balance(self.fixture["run"].name)
		self.assertEqual(confirmed["confirmed_by"], frappe.session.user)
		applied = capacity_balance.apply_capacity_balance(self.fixture["run"].name)
		self.assertEqual(applied["status"], "Applied")

	def test_stale_plan_blocks_apply(self):
		self._analyze()
		frappe.db.set_value("APS Schedule Segment", self.fixture["segment"], "planned_qty", 99)
		with self.assertRaisesRegex(frappe.ValidationError, "plan changed"):
			capacity_balance.apply_capacity_balance(self.fixture["run"].name)
		self.assertEqual(
			frappe.db.get_value("APS Planning Run", self.fixture["run"].name, "capacity_balance_status"),
			"Suggestion Ready",
		)

	def test_overlap_gate_failure_rolls_back_segment_mutation(self):
		self._analyze()
		before = frappe.db.get_value(
			"APS Schedule Segment",
			self.fixture["segment"],
			["planned_qty", "start_time", "end_time"],
			as_dict=True,
		)
		with patch.object(
			planning,
			"_validate_run_segment_overlaps",
			return_value={"count": 1, "messages": ["forced overlap"], "exception_names": []},
		):
			with self.assertRaisesRegex(frappe.ValidationError, "created 1 workstation overlap"):
				capacity_balance.apply_capacity_balance(self.fixture["run"].name)
		after = frappe.db.get_value(
			"APS Schedule Segment",
			self.fixture["segment"],
			["planned_qty", "start_time", "end_time"],
			as_dict=True,
		)
		self.assertEqual(after, before)
		self.assertEqual(
			frappe.db.count(
				"APS Schedule Segment",
				{"parent": self.fixture["result"].name, "segment_kind": "Primary"},
			),
			1,
		)
		self.assertEqual(
			frappe.db.get_value("APS Planning Run", self.fixture["run"].name, "capacity_balance_status"),
			"Suggestion Ready",
		)

	def _analyze(self):
		settings = {
			"default_production_strategy": "Auto Balance",
			"default_max_prebuild_days": 7,
			"high_cancellation_risk_percent": 60,
			"plant_floor_fg_warehouse_field": "",
		}
		with (
			patch.object(capacity_balance, "_get_fixed_execution_intervals", return_value=({}, {})),
			patch.object(planning, "_get_active_downtime_windows", return_value=[]),
			patch.object(planning, "get_settings_dict", return_value=settings),
			patch.object(
				capacity_balance,
				"_get_item_prebuild_policy",
				return_value={
					"production_strategy": "Auto Balance",
					"prebuild_allowed": 1,
					"max_prebuild_days": 7,
					"cancellation_risk_percent": 0,
					"max_stock_qty": 1000,
					"shelf_life_days": 30,
					"minimum_batch_qty": 0,
				},
			),
			patch.object(capacity_balance, "_get_item_inventory_room", return_value=(0, 1000)),
			patch.object(capacity_balance, "_get_warehouse_capacity_room", return_value=(None, 1000)),
			patch.object(capacity_balance, "_get_material_ready_qty", return_value=1000),
		):
			return capacity_balance.analyze_capacity_balance(self.fixture["run"].name)

	def _create_fixture(self):
		start = get_datetime(f"{getdate(add_days(today(), 1))} 08:00:00")
		due_date = getdate(add_days(today(), 2))
		run = frappe.get_doc(
			{
				"doctype": "APS Planning Run",
				"company": self.company,
				"plant_floor": self.plant_floor,
				"planning_date": today(),
				"horizon_start": start,
				"horizon_end": start + timedelta(days=4),
				"horizon_days": 4,
				"run_type": "Trial",
				"existing_work_order_policy": "Exclude",
				"status": "Planned",
				"approval_state": "Pending",
			}
		).insert(ignore_permissions=True)
		net_requirement = frappe.get_doc(
			{
				"doctype": "APS Net Requirement",
				"company": self.company,
				"customer": self.customer,
				"item_code": self.item,
				"demand_date": due_date,
				"demand_qty": 100,
				"planning_qty": 100,
				"net_requirement_qty": 100,
				"production_strategy": "Auto Balance",
				"demand_confidence": "Confirmed",
				"prebuild_allowed": 1,
				"max_prebuild_days": 7,
				"is_system_generated": 1,
			}
		).insert(ignore_permissions=True)
		result = frappe.get_doc(
			{
				"doctype": "APS Schedule Result",
				"planning_run": run.name,
				"company": self.company,
				"plant_floor": self.plant_floor,
				"net_requirement": net_requirement.name,
				"customer": self.customer,
				"item_code": self.item,
				"requested_date": due_date,
				"demand_source": "Customer Delivery Schedule",
				"production_strategy": "Auto Balance",
				"demand_confidence": "Confirmed",
				"prebuild_allowed": 1,
				"max_prebuild_days": 7,
				"planned_qty": 100,
				"status": "Planned",
				"risk_status": "Normal",
				"segments": [
					{
						"workstation": self.workstation,
						"plant_floor": self.plant_floor,
						"start_time": start,
						"end_time": start + timedelta(hours=20),
						"planned_qty": 100,
						"sequence_no": 1,
						"segment_kind": "Primary",
						"segment_status": "Planned",
						"is_locked": 0,
					}
				],
			}
		).insert(ignore_permissions=True)
		consistency.recalculate_plan_consistency(run.name, reason="Phase 4 capacity fixture")
		segment = frappe.db.get_value("APS Schedule Segment", {"parent": result.name}, "name")
		return {"run": run, "net_requirement": net_requirement, "result": result, "segment": segment}


if __name__ == "__main__":
	unittest.main()
