from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.services import bom_planning, capacity_balance, solver_orchestration


class _ResultDoc:
	def __init__(self):
		self.bom_demand_key = "BOM:A:202608202000"
		self.item_code = "A"
		self.planned_qty = 5
		self.segments = []
		self.flags = frappe._dict()

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, _fieldname, value):
		self.segments.append(frappe._dict(value))

	def save(self, **_kwargs):
		return self


class TestBOMDerivedResultEvidence(unittest.TestCase):
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

	def test_multilevel_bom_result_lineage_and_quantity_are_conserved(self):
		results = [
			self._result("RES-A", "BOM:A:202608202000", "A", "BOM-A", "FP-A", 5),
			self._result("RES-C", "BOM:C:202608202000", "C", "BOM-C", "FP-C", 5),
		]
		peggings = [
			self._pegging(
				"PEG-X-A", parent_key="COM-X", child_key="BOM:A:202608202000",
				parent_result="RES-X", child_result="RES-A", parent_item="X",
				component_item="A", bom="BOM-X", bom_fingerprint="FP-X",
				required=5, production=5,
			),
			self._pegging(
				"PEG-A-C", parent_key="BOM:A:202608202000", child_key="BOM:C:202608202000",
				parent_result="RES-A", child_result="RES-C", parent_item="A",
				component_item="C", bom="BOM-A", bom_fingerprint="FP-A",
				required=5, production=5,
			),
			self._pegging(
				"PEG-C-RAW", parent_key="BOM:C:202608202000", child_key="",
				parent_result="RES-C", child_result=None, parent_item="C",
				component_item="RAW", bom="BOM-C", bom_fingerprint="FP-C",
				required=10, production=0,
			),
		]
		with patch.object(bom_planning.frappe, "get_all", return_value=peggings):
			evidence = bom_planning.validate_derived_result_evidence("RUN-1", results)

		self.assertTrue(evidence["valid"], evidence["errors"])
		self.assertEqual(evidence["checked"], 2)

	def test_bom_result_quantity_must_equal_same_solution_pegged_production(self):
		result = self._result("RES-A", "BOM:A:202608202000", "A", "BOM-A", "FP-A", 6)
		peggings = [
			self._pegging(
				"PEG-X-A", parent_key="COM-X", child_key="BOM:A:202608202000",
				parent_result="RES-X", child_result="RES-A", parent_item="X",
				component_item="A", bom="BOM-X", bom_fingerprint="FP-X",
				required=5, production=5,
			),
			self._pegging(
				"PEG-A-RAW", parent_key="BOM:A:202608202000", child_key="",
				parent_result="RES-A", child_result=None, parent_item="A",
				component_item="RAW", bom="BOM-A", bom_fingerprint="FP-A",
				required=5, production=0,
			),
		]
		with patch.object(bom_planning.frappe, "get_all", return_value=peggings):
			evidence = bom_planning.validate_derived_result_evidence("RUN-1", [result])

		self.assertFalse(evidence["valid"])
		self.assertIn("bom_result_quantity", {row["code"] for row in evidence["errors"]})

	def test_solver_reanalysis_validates_then_excludes_bom_results(self):
		customer = frappe._dict(name="RES-X", exclude_from_release=0)
		derived = frappe._dict(
			name="RES-A", bom_demand_key="BOM:A:202608202000", exclude_from_release=0
		)
		excluded = frappe._dict(
			name="RES-OLD", bom_demand_key="BOM:OLD:202608202000", exclude_from_release=1
		)
		with patch.object(
			bom_planning,
			"validate_derived_result_evidence",
			return_value={"valid": True, "checked": 1, "errors": []},
		) as validate:
			rows = solver_orchestration._customer_demand_results_for_solver(
				"RUN-1", [customer, derived, excluded]
			)

		self.assertEqual(rows, [customer])
		self.assertEqual(validate.call_args.args, ("RUN-1", [derived]))

	def test_net_requirement_gate_keeps_bom_evidence_separate(self):
		rows = {
			"RES-X": {
				"name": "RES-X", "planning_run": "RUN-1",
				"net_requirement_evidence_complete": 1,
			},
			"RES-A": {
				"name": "RES-A", "planning_run": "RUN-1",
				"bom_demand_key": "BOM:A:202608202000",
				"net_requirement_evidence_complete": 0,
			},
		}
		with patch.object(
			bom_planning,
			"validate_derived_result_evidence",
			return_value={"valid": True, "checked": 1, "errors": []},
		) as validate:
			capacity_balance._assert_net_requirement_evidence_complete(
				rows, operation="V2 solver Apply"
			)

		validate.assert_called_once_with("RUN-1", [rows["RES-A"]])
		self.assertEqual(capacity_balance._missing_net_requirement_evidence_results(rows), [])

	def test_reused_bom_result_receives_current_solver_quantity(self):
		doc = _ResultDoc()
		demand = SimpleNamespace(
			key="BOM:A:202608202000", result="", item_code="A",
			quantity_units=7000, due_minute=120,
		)
		outcome = SimpleNamespace(
			demand_key=demand.key, on_time_units=7000, late_units=0,
			unscheduled_units=0, commitment="",
		)
		snapshot = SimpleNamespace(
			quantity_scale=1000, horizon_start=datetime(2026, 8, 20, 8),
			bom_decisions=(), tasks=(), demands=(demand,), multi_output_groups=(),
			precedences=(),
		)
		solution = SimpleNamespace(
			outcomes=(outcome,), tasks=(), scenario_key="balanced",
			solution_fingerprint="SOL-2",
		)
		run = frappe._dict(name="RUN-1", company="COMPANY-1")
		with (
			patch.object(solver_orchestration, "_get_or_create_bom_result", return_value="RES-A"),
			patch.object(solver_orchestration.frappe, "get_doc", return_value=doc),
		):
			solver_orchestration._apply_solution_documents(run, snapshot, solution)

		self.assertEqual(doc.planned_qty, 7)
		self.assertEqual(doc.requested_date.isoformat(), "2026-08-20")
		self.assertEqual(doc.effective_due_time, datetime(2026, 8, 20, 10))

	@staticmethod
	def _result(name, key, item, bom, bom_fingerprint, planned_qty):
		return frappe._dict(
			name=name,
			planning_run="RUN-1",
			item_code=item,
			demand_source="BOM Component",
			demand_commitment=None,
			bom_demand_key=key,
			selected_bom=bom,
			selected_bom_fingerprint=bom_fingerprint,
			planned_qty=planned_qty,
			solver_decision_json=json.dumps({"solution_fingerprint": "SOL-1"}),
		)

	@staticmethod
	def _pegging(
		name, *, parent_key, child_key, parent_result, child_result,
		parent_item, component_item, bom, bom_fingerprint, required, production,
	):
		return frappe._dict(
			name=name,
			parent_demand_key=parent_key,
			child_demand_key=child_key,
			parent_result=parent_result,
			child_result=child_result,
			parent_item=parent_item,
			component_item=component_item,
			bom=bom,
			bom_fingerprint=bom_fingerprint,
			required_gross_qty=required,
			stock_covered_qty=0,
			wip_covered_qty=0,
			production_qty=production,
			batch_excess_qty=max(production - required, 0),
			root_demand_key="COM-X",
			source_snapshot_json=json.dumps({"solution_fingerprint": "SOL-1"}),
		)


if __name__ == "__main__":
	unittest.main()
