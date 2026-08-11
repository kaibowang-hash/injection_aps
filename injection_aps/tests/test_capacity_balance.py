from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

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

	def test_material_limit_caps_prebuild(self):
		row = self._run(
			node1_minutes=720,
			node2_minutes=420,
			qty=100,
			material_ready_qty=15,
		)["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 15)
		self.assertAlmostEqual(row["late_qty"], 15)

	def test_max_early_days_blocks_out_of_window_capacity(self):
		row = self._run(
			node1_minutes=720,
			node2_minutes=420,
			qty=100,
			max_prebuild_days=0,
		)["demands"][0]
		self.assertAlmostEqual(row["prebuild_qty"], 0)
		self.assertAlmostEqual(row["late_qty"], 30)

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

	def test_same_input_is_deterministic(self):
		kwargs = {"node1_minutes": 720, "node2_minutes": 420, "qty": 100}
		first = self._run(**kwargs)
		second = self._run(**kwargs)
		self.assertEqual(canonical_json(first), canonical_json(second))

	def _run(self, node1_minutes, node2_minutes, qty, **overrides):
		buckets = [
			self._bucket("node-1", self.start, node1_minutes),
			self._bucket("node-2", self.start + timedelta(hours=12), node2_minutes),
			self._bucket("node-3", self.start + timedelta(hours=24), 720),
		]
		demand = {
			"key": "D-1",
			"result": "R-1",
			"segment": "S-1",
			"workstation": "M-1",
			"mould_reference": "MOULD-1",
			"qty": qty,
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
		return balance_capacity_nodes([demand], deepcopy(buckets))

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
