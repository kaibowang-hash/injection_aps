from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from injection_aps.api import app as api
from injection_aps.services import campaign_planning, consistency, planning
from injection_aps.tests.v2_phase0_gate import assert_isolated_environment


class TestPhase6CampaignIntegration(FrappeTestCase):
	def setUp(self):
		assert_isolated_environment(require_fixture=True)
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		self.original_settings = {
			fieldname: frappe.db.get_single_value("APS Settings", fieldname)
			for fieldname in ("enable_aps_v2", "solver_engine", "enable_coproduct_campaign")
		}
		for fieldname, value in {
			"enable_aps_v2": 1,
			"solver_engine": "CP-SAT",
			"enable_coproduct_campaign": 1,
		}.items():
			frappe.db.set_single_value("APS Settings", fieldname, value)
		frappe.clear_cache(doctype="APS Settings")
		self.company = frappe.db.get_value("Company", {})
		self.plant_floor = frappe.db.get_value("Plant Floor", {"company": self.company})
		self.workstation = frappe.db.get_value("Workstation", {"name": ("like", "APS-V2-FIXTURE-%")})
		self.mold = frappe.db.get_value("Mold", {"docstatus": 1})
		self.items = frappe.get_all("Item", filters={"default_bom": ("is", "set"), "disabled": 0}, pluck="name", limit=2)
		if not all((self.company, self.plant_floor, self.workstation, self.mold)) or len(self.items) < 2:
			self.skipTest("Phase 6 integration requires isolated APS fixture masters.")
		self.run = self._create_run()
		self.primary_result, self.owner_segment = self._create_result(self.items[0], 10, 10, "Primary")
		self.co_result, _ = self._create_result(self.items[1], 0, 0, None)
		self.campaign = self._create_campaign()

	def tearDown(self):
		if hasattr(self, "original_settings"):
			for fieldname, value in self.original_settings.items():
				frappe.db.set_single_value("APS Settings", fieldname, value)
			frappe.clear_cache(doctype="APS Settings")

	def test_campaign_quantity_is_counted_per_output_but_capacity_owner_is_unique(self):
		validation = consistency.recalculate_plan_consistency(self.run.name, reason="Phase 6 integration")
		self.assertTrue(validation["valid"], validation["errors"])
		primary_qty = frappe.db.get_value("APS Schedule Result", self.primary_result.name, "machine_scheduled_qty")
		co_qty = frappe.db.get_value("APS Schedule Result", self.co_result.name, "machine_scheduled_qty")
		self.assertEqual(primary_qty, 10)
		self.assertEqual(co_qty, 5)
		segments = frappe.get_all(
			"APS Schedule Segment",
			filters={"production_campaign": self.campaign.name},
			fields=["name", "segment_kind", "capacity_owner"],
		)
		self.assertEqual(len(segments), 2)
		self.assertEqual({row.capacity_owner for row in segments}, {self.owner_segment})
		self.assertEqual(sum(row.segment_kind in ("Primary", "Manual") for row in segments), 1)

		gantt = api.get_schedule_gantt_data(self.run.name)
		campaign_tasks = [
			task for task in gantt["tasks"]
			if (task.get("details") or {}).get("production_campaign") == self.campaign.name
		]
		self.assertEqual(len(campaign_tasks), 2)
		owner = next(task for task in campaign_tasks if task["details"].get("is_campaign_owner"))
		derived = next(task for task in campaign_tasks if task["details"].get("is_campaign_derived"))
		self.assertEqual(owner["details"]["campaign_output_count"], 2)
		self.assertEqual(len(owner["details"]["campaign_outputs"]), 2)
		self.assertEqual(derived["details"]["campaign_owner_segment"], owner["id"])
		self.assertEqual(gantt["campaigns"][0]["capacity_owner_segment"], owner["id"])

	def test_proposal_review_expands_to_the_whole_campaign(self):
		scope = campaign_planning.campaign_proposal_rows(self.run.name)
		self.assertEqual(len(scope["rows"]), 2)
		self.assertTrue(all(not row["sales_order"] and row["sales_order_requirement"] == "Not Required" for row in scope["rows"]))
		batch = frappe.get_doc({
			"doctype": "APS Work Order Proposal Batch",
			"planning_run": self.run.name,
			"company": self.company,
			"plant_floor": self.plant_floor,
			"proposal_date": "2026-08-14",
			"proposal_fingerprint": planning._work_order_proposal_fingerprint(self.run.name, scope["rows"]),
			"status": "Ready For Review",
			"approval_state": "Pending",
			"items": scope["rows"],
		})
		batch.flags.proposal_engine_transition = True
		batch.insert(ignore_permissions=True)
		with (
			patch.object(api, "_require_complete_proposal_batch_scope"),
			patch.object(api, "_require_scope_access"),
		):
			response = api._review_proposal_rows(
				batch_doctype="APS Work Order Proposal Batch",
				batch_name=batch.name,
				review_status="Approved",
				row_names=[batch.items[0].name],
			)
		self.assertEqual(response["reviewed_rows"], 2)
		self.assertEqual({row.review_status for row in frappe.get_doc(batch.doctype, batch.name).items}, {"Approved"})

	def test_approved_campaign_creates_two_work_orders_atomically_without_sales_order(self):
		scope = campaign_planning.campaign_proposal_rows(self.run.name)
		batch = frappe.get_doc({
			"doctype": "APS Work Order Proposal Batch",
			"planning_run": self.run.name,
			"company": self.company,
			"plant_floor": self.plant_floor,
			"proposal_date": "2026-08-14",
			"proposal_fingerprint": planning._work_order_proposal_fingerprint(self.run.name, scope["rows"]),
			"status": "Reviewed",
			"approval_state": "Approved",
			"items": [{**row, "review_status": "Approved"} for row in scope["rows"]],
		})
		batch.flags.proposal_engine_transition = True
		batch.insert(ignore_permissions=True)
		with (
			patch.object(planning.consistency, "assert_plan_consistent", return_value={"valid": True}),
			patch.object(planning, "_assert_release_capacity_current", return_value={"status": "Applied"}),
			patch.object(planning, "validate_run_mold_readiness", return_value={"blocking_count": 0}),
			patch.object(planning, "_rebind_release_capacity_resources", return_value={"status": "Applied"}),
		):
			response = planning.apply_work_order_proposals(batch.name)
		self.assertEqual(len(response["applied_work_orders"]), 2)
		campaign = frappe.get_doc("APS Production Campaign", self.campaign.name)
		self.assertEqual(campaign.status, "Released")
		self.assertTrue(all(row.work_order for row in campaign.outputs))
		for output in campaign.outputs:
			work_order = frappe.db.get_value(
				"Work Order", output.work_order,
				["docstatus", "sales_order", "sales_order_item", "custom_aps_campaign", "custom_aps_output_role", "custom_aps_capacity_owner", "custom_aps_result_reference"],
				as_dict=True,
			)
			self.assertEqual(work_order.docstatus, 1)
			self.assertFalse(work_order.sales_order)
			self.assertFalse(work_order.sales_order_item)
			self.assertEqual(work_order.custom_aps_campaign, campaign.name)
			self.assertEqual(work_order.custom_aps_output_role, output.output_role)
			self.assertEqual(work_order.custom_aps_capacity_owner, self.owner_segment)
			self.assertEqual(work_order.custom_aps_result_reference, output.schedule_result)

		with (
			patch.object(planning.consistency, "assert_plan_consistent", return_value={"valid": True}),
			patch.object(planning, "_assert_release_capacity_current", return_value={"status": "Applied"}),
		):
			shift_response = planning.generate_shift_schedule_proposals(
				work_order_proposal_batch=batch.name,
				release_horizon_days=0,
				release_from_date="2026-08-14",
			)
		shift_batch = frappe.get_doc("APS Shift Schedule Proposal Batch", shift_response["shift_schedule_proposal_batch"])
		self.assertEqual(len(shift_batch.items), 2)
		self.assertEqual({row.production_campaign for row in shift_batch.items}, {campaign.name})
		self.assertEqual({row.capacity_owner for row in shift_batch.items}, {self.owner_segment})
		self.assertEqual({(str(row.planned_start_time), str(row.planned_end_time)) for row in shift_batch.items}, {("2026-08-14 08:00:00", "2026-08-14 09:00:00")})
		with (
			patch.object(api, "_require_complete_proposal_batch_scope"),
			patch.object(api, "_require_scope_access"),
		):
			api._review_proposal_rows(
				batch_doctype="APS Shift Schedule Proposal Batch",
				batch_name=shift_batch.name,
				review_status="Approved",
				row_names=[shift_batch.items[0].name],
			)
		with (
			patch.object(planning.consistency, "assert_plan_consistent", return_value={"valid": True}),
			patch.object(planning, "_assert_release_capacity_current", return_value={"status": "Applied"}),
			patch.object(planning, "_validate_run_segment_overlaps", return_value={"count": 0}),
			patch.object(planning, "_validate_run_mold_overlaps", return_value={"count": 0}),
			patch.object(planning, "validate_run_mold_readiness", return_value={"blocking_count": 0}),
			patch.object(planning, "_rebind_release_capacity_resources", return_value={"status": "Applied"}),
		):
			release = planning.apply_shift_schedule_proposals(shift_batch.name)
		self.assertEqual(release["applied_rows"], 2)
		self.assertEqual(len(release["work_order_schedulings"]), 1)
		wos = frappe.get_doc("Work Order Scheduling", release["work_order_schedulings"][0])
		self.assertEqual(len(wos.scheduling_items), 2)
		self.assertEqual({row.custom_aps_campaign for row in wos.scheduling_items}, {campaign.name})
		self.assertEqual({row.custom_aps_capacity_owner for row in wos.scheduling_items}, {self.owner_segment})

	def test_second_campaign_work_order_failure_rolls_back_the_whole_group(self):
		scope = campaign_planning.campaign_proposal_rows(self.run.name)
		batch = frappe.get_doc({
			"doctype": "APS Work Order Proposal Batch",
			"planning_run": self.run.name,
			"company": self.company,
			"plant_floor": self.plant_floor,
			"proposal_date": "2026-08-14",
			"proposal_fingerprint": planning._work_order_proposal_fingerprint(self.run.name, scope["rows"]),
			"status": "Reviewed",
			"approval_state": "Approved",
			"items": [{**row, "review_status": "Approved"} for row in scope["rows"]],
		})
		batch.flags.proposal_engine_transition = True
		batch.insert(ignore_permissions=True)
		original_create = planning._create_formal_work_order
		call_count = 0

		def fail_second_output(**kwargs):
			nonlocal call_count
			call_count += 1
			if call_count == 2:
				raise frappe.ValidationError("Injected second output failure")
			return original_create(**kwargs)

		before = frappe.db.count("Work Order", {"custom_aps_campaign": self.campaign.name})
		with patch.object(planning, "_create_formal_work_order", side_effect=fail_second_output):
			with self.assertRaisesRegex(frappe.ValidationError, "Injected second output failure"):
				campaign_planning.create_campaign_work_orders(
					self.campaign.name,
					proposal_batch=batch.name,
					proposal_rows=[row.as_dict() for row in batch.items],
				)
		self.assertEqual(
			frappe.db.count("Work Order", {"custom_aps_campaign": self.campaign.name}),
			before,
		)
		campaign = frappe.get_doc("APS Production Campaign", self.campaign.name)
		self.assertEqual(campaign.status, "Planned")
		self.assertTrue(all(not row.work_order for row in campaign.outputs))

	def _create_run(self):
		return frappe.get_doc({
			"doctype": "APS Planning Run",
			"company": self.company,
			"plant_floor": self.plant_floor,
			"planning_date": "2026-08-14",
			"horizon_days": 14,
			"horizon_start": "2026-08-14 00:00:00",
			"horizon_end": "2026-08-28 00:00:00",
			"run_type": "Formal",
			"existing_work_order_policy": "Exclude",
			"status": "Applied",
			"approval_state": "Approved",
			"notes": f"APS V2 Phase 6 integration {frappe.generate_hash(length=8)}",
		}).insert(ignore_permissions=True)

	def _create_result(self, item_code, planned_qty, segment_qty, kind):
		doc = frappe.get_doc({
			"doctype": "APS Schedule Result",
			"planning_run": self.run.name,
			"company": self.company,
			"plant_floor": self.plant_floor,
			"item_code": item_code,
			"requested_date": "2026-08-20",
			"demand_source": "Campaign Co-product" if not planned_qty else "Safety Stock",
			"production_strategy": "Auto Balance",
			"planned_qty": planned_qty,
			"status": "Applied",
			"risk_status": "Normal",
			"segments": ([{
				"workstation": self.workstation,
				"plant_floor": self.plant_floor,
				"mould_reference": self.mold,
				"start_time": "2026-08-14 08:00:00",
				"end_time": "2026-08-14 09:00:00",
				"current_start_time": "2026-08-14 08:00:00",
				"current_end_time": "2026-08-14 09:00:00",
				"planned_qty": segment_qty,
				"segment_kind": kind,
				"segment_status": "Applied",
			}] if kind else []),
		})
		doc.flags.aps_result_engine_transition = True
		doc.insert(ignore_permissions=True)
		return doc, doc.segments[0].name if doc.segments else None

	def _create_campaign(self):
		campaign = frappe.get_doc({
			"doctype": "APS Production Campaign",
			"planning_run": self.run.name,
			"campaign_key": f"P6-{frappe.generate_hash(length=20)}",
			"company": self.company,
			"plant_floor": self.plant_floor,
			"machine": self.workstation,
			"mold": self.mold,
			"start_time": "2026-08-14 08:00:00",
			"end_time": "2026-08-14 09:00:00",
			"planned_cycles": 5,
			"capacity_owner_segment": self.owner_segment,
			"status": "Planned",
			"outputs": [
				{"item_code": self.items[0], "output_role": "Primary", "output_per_cycle": 2, "planned_qty": 10, "demand_covered_qty": 10, "excess_qty": 0, "schedule_result": self.primary_result.name},
				{"item_code": self.items[1], "output_role": "Co-product", "output_per_cycle": 1, "planned_qty": 5, "demand_covered_qty": 0, "excess_qty": 5, "schedule_result": self.co_result.name, "output_note": "Produced together with demanded output."},
			],
		})
		campaign.flags.aps_campaign_transition = True
		campaign.insert(ignore_permissions=True)
		frappe.db.set_value("APS Schedule Segment", self.owner_segment, {
			"production_campaign": campaign.name,
			"campaign_key": campaign.campaign_key,
			"capacity_owner": self.owner_segment,
		})
		frappe.db.set_value("APS Schedule Result", self.primary_result.name, {"production_campaign": campaign.name, "campaign_output_role": "Primary"})
		co = frappe.get_doc("APS Schedule Result", self.co_result.name)
		co.production_campaign = campaign.name
		co.campaign_output_role = "Co-product"
		co.append("segments", {
			"workstation": self.workstation,
			"plant_floor": self.plant_floor,
			"mould_reference": self.mold,
			"start_time": "2026-08-14 08:00:00",
			"end_time": "2026-08-14 09:00:00",
			"current_start_time": "2026-08-14 08:00:00",
			"current_end_time": "2026-08-14 09:00:00",
			"planned_qty": 5,
			"segment_kind": "Family Co-Product",
			"segment_status": "Applied",
			"primary_item_code": self.items[0],
			"co_product_item_code": self.items[1],
			"production_campaign": campaign.name,
			"campaign_key": campaign.campaign_key,
			"capacity_owner": self.owner_segment,
		})
		co.flags.aps_result_engine_transition = True
		co.save(ignore_permissions=True)
		return campaign


if __name__ == "__main__":
	import unittest

	unittest.main()
