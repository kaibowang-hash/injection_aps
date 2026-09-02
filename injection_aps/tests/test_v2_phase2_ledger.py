from __future__ import annotations

import unittest

from unittest.mock import patch

from injection_aps.services import demand_admission, demand_ledger, run_transition


class TestPhase2DemandLedger(unittest.TestCase):
	def test_new_plan_quantity_uses_mutually_exclusive_stock_and_carried_supply(self):
		self.assertEqual(demand_ledger.calculate_new_plan_qty(100, 30, 20), 50)
		self.assertEqual(demand_ledger.calculate_new_plan_qty(100, 80, 40), 0)

	def test_two_customers_can_only_consume_the_same_finished_goods_pool_once(self):
		demands = [
			{"demand_identity": "ID-LATE", "item_code": "ITEM-1", "schedule_open_qty": 70, "original_due_date": "2026-08-01", "effective_due_date": "2026-08-01"},
			{"demand_identity": "ID-NEXT", "item_code": "ITEM-1", "schedule_open_qty": 50, "original_due_date": "2026-08-02", "effective_due_date": "2026-08-02"},
		]
		allocations, pools = demand_ledger.allocate_stock_once(
			demands,
			[{"item_code": "ITEM-1", "warehouse": "FG-1", "available_qty": 100}],
		)
		self.assertEqual(sum(row["allocated_qty"] for row in allocations["ID-LATE"]), 70)
		self.assertEqual(sum(row["allocated_qty"] for row in allocations["ID-NEXT"]), 30)
		self.assertEqual(pools[0]["available_qty"], 0)
		self.assertEqual(
			sum(row["allocated_qty"] for parts in allocations.values() for row in parts),
			100,
		)

	def test_stock_allocation_is_stable_across_input_order(self):
		demands = [
			{"demand_identity": "ID-B", "item_code": "ITEM-1", "schedule_open_qty": 40, "original_due_date": "2026-08-02", "effective_due_date": "2026-08-02"},
			{"demand_identity": "ID-A", "item_code": "ITEM-1", "schedule_open_qty": 40, "original_due_date": "2026-08-01", "effective_due_date": "2026-08-01"},
		]
		stocks = [
			{"item_code": "ITEM-1", "warehouse": "FG-B", "available_qty": 30},
			{"item_code": "ITEM-1", "warehouse": "FG-A", "available_qty": 30},
		]
		first, _ = demand_ledger.allocate_stock_once(demands, stocks)
		second, _ = demand_ledger.allocate_stock_once(reversed(demands), reversed(stocks))
		self.assertEqual(first, second)
		self.assertEqual(sum(row["allocated_qty"] for row in first["ID-A"]), 40)
		self.assertEqual(sum(row["allocated_qty"] for row in first["ID-B"]), 20)

	def test_produced_qty_is_not_counted_as_an_extra_carried_supply(self):
		result = run_transition.classify_supply_anchors(
			[{"name": "WO-1", "docstatus": 1, "status": "In Process", "qty": 100, "produced_qty": 40}]
		)
		self.assertEqual(result["execution_state"], "Frozen")
		self.assertEqual(result["supply_remaining_qty"], 60)
		self.assertEqual(result["anchors"][0]["produced_qty"], 40)

	def test_unstarted_work_order_is_carried_and_completed_order_has_no_remaining_supply(self):
		carried = run_transition.classify_supply_anchors(
			[{"name": "WO-1", "docstatus": 1, "status": "Not Started", "qty": 80, "produced_qty": 0}]
		)
		completed = run_transition.classify_supply_anchors(
			[{"name": "WO-2", "docstatus": 1, "status": "Completed", "qty": 80, "produced_qty": 80}]
		)
		self.assertEqual(carried["execution_state"], "Carried")
		self.assertEqual(carried["supply_remaining_qty"], 80)
		self.assertEqual(completed["execution_state"], "Completed")
		self.assertEqual(completed["supply_remaining_qty"], 0)

	def test_explicitly_locked_unstarted_work_order_is_frozen(self):
		result = run_transition.classify_supply_anchors(
			[{
				"name": "WO-LOCKED", "docstatus": 1, "status": "Not Started", "qty": 80,
				"produced_qty": 0, "custom_aps_locked_for_reschedule": 1,
			}]
		)
		self.assertEqual(result["execution_state"], "Frozen")
		self.assertEqual(result["supply_remaining_qty"], 80)

	def test_missing_or_incomplete_work_order_lineage_does_not_invent_extra_supply(self):
		commitment = {
			"execution_state": "Carried", "remaining_qty": 100,
			"carried_qty": 25, "source_work_orders_json": '["MISSING-WO", {"unknown": true}]',
		}
		with patch.object(run_transition, "_get_work_order_snapshot", return_value=None):
			result = run_transition.get_commitment_supply_snapshot(commitment)
		self.assertEqual(result["execution_state"], "Carried")
		self.assertEqual(result["supply_remaining_qty"], 25)
		self.assertEqual(result["anchors"], [])
		self.assertEqual(result["unresolved_work_orders"], ["MISSING-WO"])

	def test_partial_work_order_lineage_uses_only_verified_remaining_supply(self):
		commitment = {
			"execution_state": "Carried", "remaining_qty": 100, "carried_qty": 40,
			"source_work_orders_json": '["WO-VALID", "WO-MISSING"]',
		}
		def snapshot(name):
			return (
				{"name": name, "docstatus": 1, "status": "Not Started", "qty": 15, "produced_qty": 0}
				if name == "WO-VALID" else None
			)
		with patch.object(run_transition, "_get_work_order_snapshot", side_effect=snapshot):
			result = run_transition.get_commitment_supply_snapshot(commitment)
		self.assertEqual(result["supply_remaining_qty"], 15)
		self.assertEqual(result["unresolved_work_orders"], ["WO-MISSING"])

	def test_p1_and_p2_follow_the_confirmed_formulas(self):
		self.assertEqual(demand_admission.calculate_p1_candidate(500, 300, 50), 150)
		self.assertEqual(demand_admission.calculate_p1_candidate(200, 300, 0), 0)
		self.assertEqual(demand_admission.calculate_p2_candidate(100, 35), 65)
		self.assertEqual(demand_admission.calculate_p2_candidate(100, 120), 0)

	def test_active_p1_deduction_uses_only_formal_owned_commitments(self):
		with patch.object(demand_admission.frappe, "get_all", return_value=[]) as get_all:
			self.assertEqual(
				demand_admission._get_active_p1_commitment_qty("COMPANY", exclude_run="TRIAL-1"),
				{},
			)
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["formal_owner"], 1)
		self.assertEqual(filters["owner_state"], "Owned")
		self.assertEqual(filters["planning_run"], ("!=", "TRIAL-1"))


if __name__ == "__main__":
	unittest.main()
