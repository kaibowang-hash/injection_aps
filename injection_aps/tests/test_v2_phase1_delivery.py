from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from injection_aps.services import delivery_fulfillment, planning
from injection_aps.setup.resources import STANDARD_CUSTOM_FIELDS


class TestPhase1DeliveryFulfillment(unittest.TestCase):
	def test_new_external_lineage_fields_are_hidden_and_existing_dn_target_is_not_redefined(self):
		for doctype in ("Delivery Plan Item Qty", "Delivery Plan Item"):
			for field in STANDARD_CUSTOM_FIELDS[doctype]:
				self.assertEqual(field.get("hidden"), 1, (doctype, field.get("fieldname")))
		dn_fields = {row["fieldname"]: row for row in STANDARD_CUSTOM_FIELDS["Delivery Note Item"]}
		for fieldname in (
			"custom_aps_demand_identity",
			"custom_aps_delivery_plan_detail",
			"custom_aps_match_method",
		):
			self.assertEqual(dn_fields[fieldname].get("hidden"), 1)
		self.assertNotIn("hidden", dn_fields["custom_aps_customer_schedule_item"])

	def _target(self, identity="ID-1", qty=100):
		return {
			"name": f"ROW-{identity}",
			"parent": "SCHEDULE-1",
			"demand_identity": identity,
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"item_code": "ITEM-1",
			"schedule_scope": "SCOPE-1",
			"schedule_date": "2026-08-20",
			"effective_qty": qty,
		}

	def _source(self, **values):
		return {
			"source_delivery_note": "DN-1",
			"source_delivery_note_item": "DNI-1",
			"source_docstatus": 1,
			"source_qty": 50,
			"company": "COMPANY-1",
			"customer": "CUSTOMER-1",
			"item_code": "ITEM-1",
			"posting_date": "2026-08-20",
			"is_return": 0,
			**values,
		}

	def test_explicit_identity_attributes_the_full_shipment_even_above_revised_qty(self):
		target = self._target(qty=10)
		with patch.object(delivery_fulfillment, "_resolve_source_candidates", return_value=([target], "Demand Identity", "Explicit")):
			parts, issue = delivery_fulfillment._allocate_source(
				self._source(source_qty=50, direct_demand_identity="ID-1"),
				delivered_running={"ID-1": 10},
				existing_lineage=[],
			)
		self.assertIsNone(issue)
		self.assertEqual(sum(row["qty"] for row in parts), 50)

	def test_legacy_fifo_caps_open_demand_and_creates_unallocated_remainder(self):
		target = self._target(qty=20)
		with patch.object(delivery_fulfillment, "_resolve_source_candidates", return_value=([target], "Legacy Controlled Match", "Controlled")):
			parts, issue = delivery_fulfillment._allocate_source(
				self._source(source_qty=50),
				delivered_running={"ID-1": 5},
				existing_lineage=[],
			)
		self.assertEqual(sum(row["qty"] for row in parts), 15)
		self.assertEqual(issue["unallocated_qty"], 35)

	def test_candidate_precedence_prefers_explicit_identity(self):
		target = self._target()
		with (
			patch.object(delivery_fulfillment, "_get_identity_target", return_value=target) as get_identity,
			patch.object(delivery_fulfillment, "_get_delivery_plan_lineages") as get_dp,
			patch.object(delivery_fulfillment, "_legacy_candidates") as get_legacy,
		):
			candidates, method, _reason = delivery_fulfillment._resolve_source_candidates(
				self._source(direct_demand_identity="ID-1", delivery_plan="DP-1")
			)
		self.assertEqual(candidates, [target])
		self.assertEqual(method, "Demand Identity")
		get_identity.assert_called_once_with("ID-1", allow_cancelled=True)
		get_dp.assert_not_called()
		get_legacy.assert_not_called()

	def test_delivery_plan_item_is_not_given_false_lineage_when_quantity_is_only_partly_covered(self):
		qty_row = frappe._dict(
			idx=1,
			name="DPQ-1",
			item_code="ITEM-1",
			staging_qty=20,
			planned_delivery_qty=20,
			custom_aps_demand_identity="ID-1",
			custom_aps_customer_schedule_item="ROW-ID-1",
			required_arrival_date="2026-08-20",
		)
		item = frappe._dict(idx=1, item_code="ITEM-1", planned_delivery_qty=30)
		doc = frappe._dict(items=[item])
		delivery_fulfillment._propagate_delivery_plan_item_lineage(doc, [qty_row])
		self.assertIsNone(item.custom_aps_demand_identity)
		self.assertEqual(item.custom_aps_match_method, "Unallocated")

	def test_delivery_plan_item_inherits_one_complete_identity(self):
		qty_row = frappe._dict(
			idx=1,
			name="DPQ-1",
			item_code="ITEM-1",
			staging_qty=20,
			planned_delivery_qty=20,
			custom_aps_demand_identity="ID-1",
			custom_aps_customer_schedule_item="ROW-ID-1",
			required_arrival_date="2026-08-20",
		)
		item = frappe._dict(idx=1, item_code="ITEM-1", planned_delivery_qty=20)
		doc = frappe._dict(items=[item])
		delivery_fulfillment._propagate_delivery_plan_item_lineage(doc, [qty_row])
		self.assertEqual(item.custom_aps_demand_identity, "ID-1")
		self.assertEqual(item.custom_aps_customer_schedule_item, "ROW-ID-1")
		self.assertEqual(item.custom_aps_match_method, "Delivery Plan FIFO")

	def test_delivery_note_lineage_failure_is_nonblocking(self):
		item = frappe._dict(item_code="ITEM-1")
		doc = frappe._dict(company="COMPANY-1", customer="CUSTOMER-1", items=[item])
		with (
			patch.object(delivery_fulfillment, "is_v2_enabled", return_value=True),
			patch.object(delivery_fulfillment, "_resolve_draft_delivery_note_item_lineage", side_effect=RuntimeError("ambiguous")),
		):
			delivery_fulfillment.inherit_delivery_note_lineage(doc)
		self.assertEqual(item.custom_aps_match_method, "Unallocated")

	def test_delivery_plan_generation_groups_by_customer_date_and_address(self):
		results = [
			frappe._dict(name="RES-1", customer="CUSTOMER-A", sales_order="SO-1", item_code="ITEM-1", requested_date="2026-08-20", machine_scheduled_qty=10),
			frappe._dict(name="RES-2", customer="CUSTOMER-A", sales_order="SO-2", item_code="ITEM-2", requested_date="2026-08-20", machine_scheduled_qty=20),
			frappe._dict(name="RES-3", customer="CUSTOMER-B", sales_order=None, item_code="ITEM-3", requested_date="2026-08-21", machine_scheduled_qty=30),
		]
		orders = [
			frappe._dict(name="SO-1", customer_address="ADDRESS-1"),
			frappe._dict(name="SO-2", customer_address="ADDRESS-2"),
		]
		payloads = []

		class DeliveryPlanStub(frappe._dict):
			def insert(self, ignore_permissions=False):
				self.name = f"DP-{len(payloads)}"
				return self

		def get_all(doctype, **_kwargs):
			return results if doctype == "APS Schedule Result" else orders

		def get_doc(values):
			payloads.append(values)
			return DeliveryPlanStub(values)

		with (
			patch.object(planning.frappe.db, "exists", return_value=True),
			patch.object(planning.frappe, "get_all", side_effect=get_all),
			patch.object(planning.frappe, "get_doc", side_effect=get_doc),
			patch.object(
				planning,
				"_resolve_delivery_plan_result_lineage",
				return_value={"demand_identity": "ID-1", "schedule_item": "ROW-1", "match_method": "Explicit Identity"},
			),
		):
			created = planning._sync_delivery_plan(frappe._dict(name="RUN-1", company="COMPANY-1"))

		self.assertEqual(len(created), 3)
		self.assertEqual({row["customer"] for row in payloads}, {"CUSTOMER-A", "CUSTOMER-B"})
		self.assertEqual(
			{(row["customer"], str(row["delivery_date"]), (row["item_qties"][0].get("customer_address") or "")) for row in payloads},
			{
				("CUSTOMER-A", "2026-08-20", "ADDRESS-1"),
				("CUSTOMER-A", "2026-08-20", "ADDRESS-2"),
				("CUSTOMER-B", "2026-08-21", ""),
			},
		)


if __name__ == "__main__":
	unittest.main()
