from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_datetime, getdate, now_datetime, today

from injection_aps.services import availability, demand_admission, demand_ledger, schedule_revision
from injection_aps.tests.v2_phase0_gate import assert_isolated_environment


class TestPhase2DemandIntegration(FrappeTestCase):
	def setUp(self):
		assert_isolated_environment(require_fixture=True)
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		for doctype in (
			"APS Demand Commitment", "APS Demand Admission", "APS Stock Coverage Allocation", "APS Demand Identity"
		):
			if not frappe.db.exists("DocType", doctype):
				self.skipTest("Phase 2 DocTypes are not synced.")
		self.original_v2 = frappe.db.get_single_value("APS Settings", "enable_aps_v2") or 0
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", 1)
		frappe.clear_cache(doctype="APS Settings")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		self.plant_floor = frappe.db.get_value("Plant Floor", {"company": self.company})
		fg_warehouses = availability._get_finished_goods_warehouses(self.company)
		self.warehouse = fg_warehouses[0] if fg_warehouses else None
		if not all((self.company, self.customer, self.plant_floor, self.warehouse)):
			self.skipTest("Phase 2 integration needs Company, Customer, Plant Floor, and Finished Goods Warehouse.")
		self.item = self._create_item()
		self.schedule_items = self._create_two_demands()
		self.identities = [row.demand_identity for row in self.schedule_items]
		self._create_stock_bin(100)

	def tearDown(self):
		if hasattr(self, "original_v2"):
			frappe.db.set_single_value("APS Settings", "enable_aps_v2", self.original_v2)
			frappe.clear_cache(doctype="APS Settings")

	def test_stock_frozen_reference_optional_admission_and_idempotency(self):
		source_run = self._create_run("Formal")
		source = self._create_commitment(
			source_run,
			self.schedule_items[1],
			formal_owner=1,
			execution_state="Frozen",
			requested_qty=50,
			carried_qty=20,
			newly_planned_qty=30,
			remaining_qty=20,
		)
		trial = self._create_run("Trial")
		first = demand_ledger.prepare_run_demand_baseline(trial.name)
		p0 = {row["demand_identity"]: row for row in first["commitments"] if row["admission_class"] == "P0"}
		target_rows = [p0[identity] for identity in self.identities]

		self.assertEqual(sum(row["requested_qty"] for row in target_rows), 120)
		self.assertEqual(sum(row["stock_covered_qty"] for row in target_rows), 100)
		self.assertEqual(sum(row["carried_qty"] for row in target_rows), 20)
		self.assertEqual(sum(row["newly_planned_qty"] for row in target_rows), 0)
		self.assertEqual(p0[self.identities[1]]["owner_state"], "Referenced")
		self.assertEqual(p0[self.identities[1]]["source_run"], source_run.name)
		self.assertEqual(
			frappe.db.get_value("APS Demand Commitment", source.name, "owner_state"),
			"Owned",
		)

		allocations = frappe.get_all(
			"APS Stock Coverage Allocation",
			filters={"owner_run": trial.name, "item_code": self.item, "status": "Active"},
			fields=["demand_identity", "allocated_qty"],
			limit_page_length=0,
		)
		self.assertEqual(sum(row.allocated_qty for row in allocations), 100)
		self.assertEqual(len({row.demand_identity for row in allocations}), 2)
		self.assertTrue(first["validation"]["valid"])

		second = demand_ledger.prepare_run_demand_baseline(
			trial.name,
			expected_fingerprint=first["demand_baseline_fingerprint"],
		)
		self.assertEqual(second["demand_baseline_fingerprint"], first["demand_baseline_fingerprint"])
		self.assertEqual(
			frappe.db.count("APS Demand Commitment", {"planning_run": trial.name, "status": ("in", demand_ledger.ACTIVE_COMMITMENT_STATUSES)}),
			len(second["commitments"]),
		)

		p2 = next(
			row for row in second["admission"]["rows"]
			if row["admission_class"] == "P2" and row["item_code"] == self.item
		)
		frappe.db.set_value(
			"APS Planning Run", trial.name,
			{"capacity_balance_status": "Suggestion Ready", "capacity_balance_fingerprint": "STALE"},
			update_modified=False,
		)
		decisions = [
			{"name": row["name"], "selected_qty": 10 if row["name"] == p2["name"] else row["selected_qty"]}
			for row in second["admission"]["rows"]
		]
		saved = demand_admission.save_demand_admission_decisions(
			trial.name,
			decisions,
			expected_fingerprint=second["admission_fingerprint"],
			reason="PMC selected a limited safety-stock trial quantity.",
		)
		self.assertEqual(saved["summary"]["selected_p2_qty"], 10)
		optional = frappe.db.get_value(
			"APS Demand Commitment",
			{"planning_run": trial.name, "admission": p2["name"], "status": "Proposed"},
			["requested_qty", "newly_planned_qty", "formal_owner"],
			as_dict=True,
		)
		self.assertEqual(optional.requested_qty, 10)
		self.assertEqual(optional.newly_planned_qty, 10)
		self.assertEqual(optional.formal_owner, 0)
		self.assertEqual(frappe.db.get_value("APS Planning Run", trial.name, "capacity_balance_status"), "Not Analyzed")
		self.assertIsNone(frappe.db.get_value("APS Planning Run", trial.name, "capacity_balance_fingerprint"))

	def test_p0_cannot_be_deselected(self):
		trial = self._create_run("Trial")
		baseline = demand_ledger.prepare_run_demand_baseline(trial.name)
		p0 = next(row for row in baseline["admission"]["rows"] if row["admission_class"] == "P0")
		with self.assertRaises(frappe.ValidationError):
			demand_admission.preview_admission_impact(
				trial.name,
				[{"name": p0["name"], "selected_qty": 0}],
				expected_fingerprint=baseline["admission_fingerprint"],
			)

	def test_formal_owner_transfer_is_atomic_and_unique(self):
		source_run = self._create_run("Formal")
		target_run = self._create_run("Formal")
		source = self._create_commitment(
			source_run,
			self.schedule_items[0],
			formal_owner=1,
			execution_state="Carried",
			requested_qty=70,
			carried_qty=70,
			newly_planned_qty=0,
			remaining_qty=70,
		)
		transferred = demand_ledger.transfer_commitment_owner(
			source.name,
			target_run.name,
			reason="Run2 supersedes the unstarted Run1 supply.",
			allow_formal=True,
		)
		self.assertEqual(transferred["owner_state"], "Owned")
		self.assertEqual(transferred["formal_owner"], 1)
		self.assertEqual(frappe.db.get_value("APS Demand Commitment", source.name, "owner_state"), "Superseded")
		owners = frappe.db.count(
			"APS Demand Commitment",
			{
				"demand_identity": self.identities[0], "formal_owner": 1, "owner_state": "Owned",
				"status": ("in", demand_ledger.ACTIVE_COMMITMENT_STATUSES),
			},
		)
		self.assertEqual(owners, 1)

	def test_frozen_owner_cannot_be_transferred(self):
		source_run = self._create_run("Formal")
		target_run = self._create_run("Formal")
		source = self._create_commitment(
			source_run,
			self.schedule_items[0],
			formal_owner=1,
			execution_state="Frozen",
			requested_qty=70,
			carried_qty=70,
			newly_planned_qty=0,
			remaining_qty=70,
		)
		with self.assertRaises(frappe.ValidationError):
			demand_ledger.transfer_commitment_owner(
				source.name,
				target_run.name,
				reason="Attempted move",
				allow_formal=True,
			)
		self.assertEqual(frappe.db.get_value("APS Demand Commitment", source.name, "owner_state"), "Owned")

	def test_database_rejects_two_active_formal_owners_for_one_identity(self):
		first_run = self._create_run("Formal")
		second_run = self._create_run("Formal")
		self._create_commitment(
			first_run, self.schedule_items[0], formal_owner=1, execution_state="Carried",
			requested_qty=70, carried_qty=70, newly_planned_qty=0, remaining_qty=70,
		)
		with self.assertRaises(frappe.UniqueValidationError):
			self._create_commitment(
				second_run, self.schedule_items[0], formal_owner=1, execution_state="Carried",
				requested_qty=70, carried_qty=70, newly_planned_qty=0, remaining_qty=70,
			)

	def test_completed_and_excess_demand_is_recorded_without_new_plan_qty(self):
		first = self.schedule_items[0]
		frappe.db.set_value(
			"Customer Delivery Schedule Item",
			first.name,
			{"delivered_qty": 75, "executed_floor_qty": 75, "excess_qty": 5},
			update_modified=False,
		)
		trial = self._create_run("Trial")
		baseline = demand_ledger.prepare_run_demand_baseline(trial.name)
		self.assertNotIn(first.demand_identity, {row["demand_identity"] for row in baseline["commitments"]})
		terminal = frappe.db.get_value(
			"APS Demand Commitment",
			{"planning_run": trial.name, "demand_identity": first.demand_identity},
			["status", "execution_state", "requested_qty", "newly_planned_qty", "remaining_qty", "excess_qty"],
			as_dict=True,
		)
		self.assertEqual(terminal.status, "Excess")
		self.assertEqual(terminal.execution_state, "Excess")
		self.assertEqual(terminal.requested_qty, 0)
		self.assertEqual(terminal.newly_planned_qty, 0)
		self.assertEqual(terminal.remaining_qty, 0)
		self.assertEqual(terminal.excess_qty, 5)
		self.assertEqual(baseline["terminal_summary"]["excess_count"], 1)

	def _create_item(self):
		name = f"APS-V2-P2-{frappe.generate_hash(length=10)}"
		item = frappe.new_doc("Item")
		item.item_code = name
		item.item_name = name
		item.item_group = frappe.db.get_value("Item Group", {"is_group": 0})
		item.stock_uom = frappe.db.get_value("UOM", {})
		item.is_stock_item = 1
		item.disabled = 0
		safety_field = frappe.db.get_single_value("APS Settings", "item_safety_stock_field") or "safety_stock"
		if frappe.get_meta("Item").has_field(safety_field):
			item.set(safety_field, 30)
		item.db_insert()
		return item.name

	def _create_two_demands(self):
		schedule = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": self.customer,
				"company": self.company,
				"schedule_scope": f"APS-V2-P2-{frappe.generate_hash(length=10)}",
				"version_no": "V1",
				"import_strategy": "Replace Scope",
				"source_type": "Customer Delivery Schedule",
				"status": "Active",
				"items": [
					{"item_code": self.item, "schedule_date": getdate(today()), "qty": 70, "status": "Open", "source_origin": "manual_added"},
					{"item_code": self.item, "schedule_date": getdate(add_days(today(), 1)), "qty": 50, "status": "Open", "source_origin": "manual_added"},
				],
			}
		)
		schedule.flags.aps_schedule_import_transition = True
		schedule.insert(ignore_permissions=True)
		schedule_revision.backfill_active_demand_identities()
		return frappe.get_all(
			"Customer Delivery Schedule Item",
			filters={"parent": schedule.name},
			fields=["name", "item_code", "schedule_date", "effective_qty", "demand_identity"],
			order_by="schedule_date asc, name asc",
		)

	def _create_stock_bin(self, qty):
		name = frappe.db.get_value("Bin", {"item_code": self.item, "warehouse": self.warehouse})
		if name:
			frappe.db.set_value(
				"Bin", name,
				{
					"actual_qty": qty, "reserved_qty": 0, "reserved_stock": 0,
					"reserved_qty_for_production": 0, "reserved_qty_for_sub_contract": 0,
					"reserved_qty_for_production_plan": 0,
				},
				update_modified=False,
			)
			return name
		return frappe.get_doc(
			{"doctype": "Bin", "item_code": self.item, "warehouse": self.warehouse, "actual_qty": qty}
		).insert(ignore_permissions=True).name

	def _create_run(self, run_type):
		start = now_datetime()
		return frappe.get_doc(
			{
				"doctype": "APS Planning Run", "company": self.company, "plant_floor": self.plant_floor,
				"planning_date": getdate(today()), "horizon_days": 14, "horizon_start": start,
				"horizon_end": get_datetime(add_days(start, 13)), "run_type": run_type,
				"existing_work_order_policy": "Exclude", "status": "Draft", "approval_state": "Pending",
				"notes": "APS V2 Phase 2 integration",
			}
		).insert(ignore_permissions=True)

	def _create_commitment(
		self,
		run,
		schedule_item,
		*,
		formal_owner,
		execution_state,
		requested_qty,
		carried_qty,
		newly_planned_qty,
		remaining_qty,
	):
		values = {
			"doctype": "APS Demand Commitment", "planning_run": run.name, "company": self.company,
			"customer": self.customer, "item_code": self.item, "demand_identity": schedule_item.demand_identity,
			"schedule_item": schedule_item.name, "admission_class": "P0", "owner_state": "Owned",
			"execution_state": execution_state, "status": "Approved", "formal_owner": formal_owner,
			"original_due_date": schedule_item.schedule_date, "requested_qty": requested_qty,
			"stock_covered_qty": 0, "carried_qty": carried_qty, "newly_planned_qty": newly_planned_qty,
			"remaining_qty": remaining_qty, "input_fingerprint": frappe.generate_hash(length=32),
			"ownership_fingerprint": frappe.generate_hash(length=32),
			"idempotency_key": frappe.generate_hash(length=32), "transition_reason": "Integration fixture",
			"transitioned_by": frappe.session.user, "transitioned_on": now_datetime(),
		}
		doc = frappe.get_doc(values)
		doc.flags.aps_phase2_transition = True
		doc.insert(ignore_permissions=True)
		return doc
