from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from injection_aps.services import bom_planning, constraint_resolution, solver_orchestration
from injection_aps.services.solver.input_builder import build_solver_input
from injection_aps.services.solver.scenarios import solve_scenarios
from injection_aps.tests.v2_phase0_gate import assert_isolated_environment


class TestPhase7BOMIntegration(FrappeTestCase):
	def setUp(self):
		assert_isolated_environment(require_fixture=True)
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		self.original_settings = {
			fieldname: frappe.db.get_single_value("APS Settings", fieldname)
			for fieldname in ("enable_aps_v2", "solver_engine", "enable_multilevel_bom_planning", "aps_bom_policy", "aps_producible_item_groups")
		}
		for fieldname, value in {
			"enable_aps_v2": 1, "solver_engine": "CP-SAT", "enable_multilevel_bom_planning": 1,
			"aps_bom_policy": "Explicit Approved Alternative", "aps_producible_item_groups": "Plastic Part",
		}.items():
			frappe.db.set_single_value("APS Settings", fieldname, value)
		frappe.clear_cache(doctype="APS Settings")
		self.company = frappe.db.get_value("Company", {})
		self.plant_floor = frappe.db.get_value("Plant Floor", {"company": self.company})
		self.workstation = frappe.db.get_value("Workstation", {"name": ("like", "APS-V2-FIXTURE-%")})
		self.mold = frappe.db.get_value("Mold", {"docstatus": 1})
		self.x = "APS-V2-FIXTURE-BOM-X"
		self.a = "APS-V2-FIXTURE-BOM-A"
		self.c = "APS-V2-FIXTURE-BOM-C"
		self.raw = "APS-V2-FIXTURE-RAW-ZERO-RM"
		if not all((self.company, self.plant_floor, self.workstation, self.mold)) or not all(frappe.db.exists("Item", item) for item in (self.x, self.a, self.c, self.raw)):
			self.skipTest("Phase 7 integration requires the isolated APS fixture masters.")
		self.raw_group = frappe.db.get_value("Item", self.raw, "item_group")
		non_producible_group = frappe.db.get_value("Item Group", {"name": ("not in", ["Plastic Part"]), "is_group": 0}, "name")
		if not non_producible_group:
			self.skipTest("Phase 7 integration needs one non-producible Item Group for the raw advisory leaf.")
		frappe.db.set_value("Item", self.raw, "item_group", non_producible_group, update_modified=False)
		self.bom_c = self._ensure_bom(self.c, [(self.raw, 2)], is_default=True)
		self.bom_a = self._ensure_bom(self.a, [(self.c, 1)], is_default=True)
		self.bom_x = self._ensure_bom(self.x, [(self.a, 1)], is_default=True)
		self.bom_x_alt = self._ensure_bom(self.x, [(self.a, 1), (self.raw, 1)], is_default=False)

	def tearDown(self):
		if hasattr(self, "raw_group"):
			frappe.db.set_value("Item", self.raw, "item_group", self.raw_group, update_modified=False)
		if hasattr(self, "original_settings"):
			for fieldname, value in self.original_settings.items():
				frappe.db.set_single_value("APS Settings", fieldname, value)
			frappe.clear_cache(doctype="APS Settings")

	def test_solver_apply_persists_exact_pegging_tree_and_forecast_propagation(self):
		run = self._create_run(status="Planned")
		root = self._create_root_result(run)
		horizon_start = "2026-08-14T08:00:00"
		horizon_end = "2026-08-14T20:00:00"
		child_key = "BOM:APS-V2-FIXTURE-BOM-A:202608142000"
		x_fingerprint = bom_planning.current_bom_fingerprint(self.x, self.bom_x)
		a_fingerprint = bom_planning.current_bom_fingerprint(self.a, self.bom_a)
		snapshot = build_solver_input({
			"run_key": run.name, "horizon_start": horizon_start, "horizon_end": horizon_end,
			"quantity_scale": 1000, "time_limit_seconds": 9, "random_seed": 7,
			"demands": [
				{"key": child_key, "item_code": self.a, "admission_class": "P0", "quantity": 5, "due_time": horizon_end, "alternatives": [{"key": f"{self.workstation}|{self.mold}", "machine": self.workstation, "mold": self.mold, "output_per_cycle": 1, "cycle_minutes": 1}]},
				{"key": root.name, "result": root.name, "item_code": self.x, "admission_class": "P0", "quantity": 5, "due_time": horizon_end, "alternatives": [{"key": f"{self.workstation}|{self.mold}", "machine": self.workstation, "mold": self.mold, "output_per_cycle": 1, "cycle_minutes": 1}]},
			],
			"buckets": [{"key": "DAY", "machine": self.workstation, "start": horizon_start, "end": horizon_end, "available_minutes": 720}],
			"bom_decisions": [
				{"item_code": self.x, "bom": self.bom_x, "bom_fingerprint": x_fingerprint, "output_qty": 1},
				{"item_code": self.a, "bom": self.bom_a, "bom_fingerprint": a_fingerprint, "output_qty": 1},
			],
			"precedences": [{
				"predecessor_demand": child_key, "successor_demand": root.name,
				"parent_item": self.x, "component_item": self.a, "bom": self.bom_x,
				"bom_fingerprint": x_fingerprint, "level": 1, "qty_per_parent": 1,
				"bom_output_qty": 1, "required_qty": 5, "production_qty": 5,
				"required_available_time": horizon_end, "root_demand_key": root.name,
				"root_demand_keys": [root.name],
				"root_allocations": [{"root_demand_key": root.name, "required_qty": 5, "production_qty": 5}],
			}],
		})
		solution = solve_scenarios(snapshot)[0]
		self.assertNotEqual(solution.status, "Failed", solution.warnings)
		applied = solver_orchestration._apply_solution_documents(run, snapshot, solution)
		self.assertEqual(applied["pegging_count"], 1)
		pegging = frappe.db.get_value(
			"APS BOM Pegging", {"planning_run": run.name},
			["root_demand_key", "parent_result", "child_result", "bom_fingerprint", "production_qty", "status"],
			as_dict=True,
		)
		self.assertEqual(pegging.root_demand_key, root.name)
		self.assertEqual(pegging.parent_result, root.name)
		self.assertTrue(pegging.child_result)
		self.assertEqual(pegging.bom_fingerprint, x_fingerprint)
		self.assertEqual(pegging.production_qty, 5)
		self.assertTrue(bom_planning.validate_run_precedence(run.name)["valid"])
		tree = bom_planning.get_bom_pegging_tree(run.name)
		self.assertEqual(tree["summary"]["root_count"], 1)
		self.assertEqual(tree["summary"]["link_count"], 1)

		child_segment = frappe.db.get_value("APS Schedule Segment", {"parent": pegging.child_result, "segment_status": ("!=", "Cancelled")}, ["name", "start_time", "end_time"], as_dict=True)
		preview = bom_planning.preview_segment_precedence(
			run.name, root.name,
			frappe.db.get_value("APS Schedule Segment", {"parent": root.name, "segment_status": ("!=", "Cancelled")}, "name"),
			proposed_start=child_segment.start_time, proposed_end=child_segment.end_time,
		)
		self.assertFalse(preview["valid"])

		late_time = get_datetime("2026-08-14 21:00:00")
		impacts = bom_planning.propagate_forecast_to_roots(run.name, [{"result": pegging.child_result, "forecast_end_time": late_time}])
		self.assertEqual(impacts[0]["result"], root.name)
		self.assertTrue(impacts[0]["late"])
		self.assertEqual(frappe.db.get_value("APS Schedule Result", root.name, "shortage_code"), "BOM_CHILD_DELAY")

	def test_explicit_alternative_is_audited_and_enters_the_frozen_decision(self):
		run = self._create_run(status="Draft")
		self._create_root_result(run)
		response = bom_planning.set_run_bom_selections(
			run.name,
			[{"item_code": self.x, "bom": self.bom_x_alt}],
			reason="Validated Phase 7 alternative route.",
			expected_run_modified=str(run.modified),
		)
		self.assertEqual(response["selections"][0]["bom"], self.bom_x_alt)
		expansion = bom_planning.expand_solver_demands(
			self.company,
			[{"key": "ROOT", "item_code": self.x, "quantity": 2, "due_time": "2026-08-20 20:00:00", "admission_class": "P0"}],
			producible_groups={"Plastic Part"}, planning_run=run.name,
		)
		decision = next(row for row in expansion["bom_decisions"] if row["item_code"] == self.x)
		self.assertEqual(decision["bom"], self.bom_x_alt)
		self.assertEqual(decision["selection_source"], "Explicit Approved Alternative")
		self.assertEqual(decision["bom_fingerprint"], bom_planning.current_bom_fingerprint(self.x, self.bom_x_alt))
		self.assertTrue({self.x, self.a, self.c} <= {row["item_code"] for row in expansion["bom_decisions"]})
		self.assertTrue(any(row["component_item"] == self.raw and row["is_raw_material_leaf"] for row in expansion["peggings"]))
		self.assertTrue({self.a, self.c} <= {row["component_item"] for row in expansion["peggings"]})

	def test_bom_input_blocker_is_never_override(self):
		run = self._create_run(status="Draft")
		constraint_resolution.sync_solver_input_blocker(run, blocker_key="bom_cycle", message="BOM cycle: X -> A -> X")
		row = frappe.db.get_value(
			"APS Constraint Resolution", {"planning_run": run.name, "blocker_key": "bom_cycle", "status": "Open"},
			["blocker_policy", "severity"], as_dict=True,
		)
		self.assertEqual(row.blocker_policy, "Never Override")
		self.assertEqual(row.severity, "Blocking")
		constraint_resolution.sync_solver_input_blocker(run, blocker_key=None, message=None)
		self.assertEqual(frappe.db.get_value("APS Constraint Resolution", {"planning_run": run.name, "blocker_key": "bom_cycle"}, "status"), "Superseded")

	def _ensure_bom(self, item_code, components, *, is_default):
		filters = {"item": item_code, "docstatus": 1, "is_active": 1, "is_default": 1 if is_default else 0}
		existing = frappe.db.get_value("BOM", filters, "name")
		if existing:
			return existing
		stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
		doc = frappe.get_doc({
			"doctype": "BOM", "item": item_code, "company": self.company, "quantity": 1,
			"uom": stock_uom, "is_active": 1, "is_default": 1 if is_default else 0,
			"custom_temporary_bom": "No",
			"items": [{"item_code": component, "qty": qty, "uom": frappe.db.get_value("Item", component, "stock_uom"), "conversion_factor": 1, "rate": 0} for component, qty in components],
		})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc.name

	def _create_run(self, *, status):
		return frappe.get_doc({
			"doctype": "APS Planning Run", "company": self.company, "plant_floor": self.plant_floor,
			"planning_date": "2026-08-14", "horizon_days": 14,
			"horizon_start": "2026-08-14 08:00:00", "horizon_end": "2026-08-28 20:00:00",
			"run_type": "Formal", "existing_work_order_policy": "Exclude", "status": status,
			"approval_state": "Pending", "notes": f"APS V2 Phase 7 integration {frappe.generate_hash(length=8)}",
		}).insert(ignore_permissions=True)

	def _create_root_result(self, run):
		doc = frappe.get_doc({
			"doctype": "APS Schedule Result", "planning_run": run.name, "company": self.company,
			"plant_floor": self.plant_floor, "item_code": self.x, "requested_date": "2026-08-14",
			"effective_due_time": "2026-08-14 20:00:00", "demand_source": "Customer Delivery Schedule",
			"production_strategy": "Auto Balance", "planned_qty": 5, "status": "Planned", "risk_status": "Normal",
		})
		doc.flags.aps_result_engine_transition = True
		doc.insert(ignore_permissions=True)
		return doc


if __name__ == "__main__":
	import unittest

	unittest.main()
