from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, cint, getdate, nowtime, today

from injection_aps.services import delivery_sync


class TestDeliveryAllocationSync(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("DocType", "APS Delivery Allocation"):
			self.skipTest("Phase 4 delivery allocation DocType is not synced.")
		self.company = frappe.db.get_value("Company", {})
		self.customers = frappe.get_all("Customer", pluck="name", limit=2)
		if not self.company or len(self.customers) < 2:
			self.skipTest("Delivery tests need one Company and two Customers.")
		self.customer_a, self.customer_b = self.customers
		self.item = self._create_test_item()
		self.due_date_1 = getdate(add_days(today(), 400))
		self.due_date_2 = getdate(add_days(today(), 401))
		self.schedule_a = self._create_schedule(
			self.customer_a,
			[(self.due_date_1, 50), (self.due_date_1, 50)],
		)
		self.schedule_b = self._create_schedule(
			self.customer_b,
			[(self.due_date_1, 100)],
		)

	def test_controlled_fifo_is_customer_safe_and_idempotent(self):
		delivery_note, _item = self._create_delivery_note(self.customer_a, 80)
		first = self._sync(self.customer_a)
		self.assertEqual(first["ledger"]["created"], 2)
		self.assertEqual(first["rollup"]["delivered_qty"], 80)
		allocations = frappe.get_all(
			"APS Delivery Allocation",
			filters={"source_delivery_note": delivery_note},
			fields=["customer", "customer_schedule_item", "allocated_qty", "allocation_method"],
		)
		qty_by_target = {row.customer_schedule_item: row.allocated_qty for row in allocations}
		self.assertEqual(
			qty_by_target,
			{self.schedule_a["items"][0]: 50, self.schedule_a["items"][1]: 30},
		)
		self.assertEqual({row.customer for row in allocations}, {self.customer_a})
		self.assertEqual({row.allocation_method for row in allocations}, {"Controlled FIFO"})
		self.assertEqual(self._delivered(self.schedule_a["items"][0]), 50)
		self.assertEqual(self._delivered(self.schedule_a["items"][1]), 30)
		self.assertEqual(self._delivered(self.schedule_b["items"][0]), 0)

		replay = self._sync(self.customer_a)
		self.assertEqual(replay["ledger"]["created"], 0)
		self.assertEqual(replay["ledger"]["updated"], 0)
		self.assertEqual(
			frappe.db.count("APS Delivery Allocation", {"source_delivery_note": delivery_note}),
			2,
		)
		self.assertEqual(replay["rollup"]["delivered_qty"], 80)

	def test_cancelled_delivery_note_reverses_every_allocation(self):
		delivery_note, _item = self._create_delivery_note(self.customer_a, 80)
		self._sync(self.customer_a)
		frappe.db.set_value("Delivery Note", delivery_note, "docstatus", 2, update_modified=False)
		cancelled = self._sync(self.customer_a)
		self.assertEqual(cancelled["ledger"]["reversed"], 2)
		self.assertEqual(cancelled["rollup"]["delivered_qty"], 0)
		self.assertEqual(self._delivered(self.schedule_a["items"][0]), 0)
		self.assertEqual(self._delivered(self.schedule_a["items"][1]), 0)
		ledger = frappe.get_all(
			"APS Delivery Allocation",
			filters={"source_delivery_note": delivery_note},
			fields=["effective_qty", "reversed_qty", "is_effective", "source_docstatus"],
		)
		self.assertEqual({row.effective_qty for row in ledger}, {0})
		self.assertEqual({row.is_effective for row in ledger}, {0})
		self.assertEqual({row.source_docstatus for row in ledger}, {2})
		self.assertEqual(sum(row.reversed_qty for row in ledger), 80)

		replay = self._sync(self.customer_a)
		self.assertEqual(replay["ledger"]["reversed"], 0)
		self.assertEqual(replay["rollup"]["delivered_qty"], 0)

	def test_return_traces_original_item_and_cancelled_return_restores_delivery(self):
		delivery_note, delivery_item = self._create_delivery_note(self.customer_a, 80)
		self._sync(self.customer_a)
		return_note, _return_item = self._create_delivery_note(
			self.customer_a,
			30,
			is_return=True,
			return_against=delivery_note,
			original_delivery_note_item=delivery_item,
		)
		returned = self._sync(self.customer_a)
		self.assertEqual(returned["rollup"]["delivered_qty"], 50)
		return_rows = frappe.get_all(
			"APS Delivery Allocation",
			filters={"source_delivery_note": return_note},
			fields=["effective_qty", "allocated_qty", "allocation_method", "original_delivery_note_item"],
		)
		self.assertEqual(sum(row.effective_qty for row in return_rows), -30)
		self.assertEqual(sum(row.allocated_qty for row in return_rows), 30)
		self.assertEqual({row.allocation_method for row in return_rows}, {"Return Trace"})
		self.assertEqual({row.original_delivery_note_item for row in return_rows}, {delivery_item})

		frappe.db.set_value("Delivery Note", return_note, "docstatus", 2, update_modified=False)
		restored = self._sync(self.customer_a)
		self.assertEqual(restored["rollup"]["delivered_qty"], 80)
		self.assertEqual(self._delivered(self.schedule_a["items"][0]), 50)
		self.assertEqual(self._delivered(self.schedule_a["items"][1]), 30)

	def test_direct_target_cannot_cross_customer(self):
		delivery_note, _item = self._create_delivery_note(
			self.customer_a,
			20,
			direct_schedule_item=self.schedule_b["items"][0],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "mismatch on customer"):
			self._sync(self.customer_a)
		self.assertEqual(
			frappe.db.count("APS Delivery Allocation", {"source_delivery_note": delivery_note}),
			0,
		)
		self.assertEqual(self._delivered(self.schedule_b["items"][0]), 0)

	def test_direct_target_preserves_source_document_and_detail_trace(self):
		target = self.schedule_a["items"][0]
		delivery_note, delivery_item = self._create_delivery_note(
			self.customer_a,
			20,
			direct_schedule_item=target,
		)
		result = self._sync(self.customer_a)
		self.assertEqual(result["rollup"]["delivered_qty"], 20)
		row = frappe.db.get_value(
			"APS Delivery Allocation",
			{"source_delivery_note": delivery_note},
			[
				"source_delivery_note_item",
				"customer_schedule_item",
				"allocation_method",
				"effective_qty",
				"cumulative_delivered_qty",
			],
			as_dict=True,
		)
		self.assertEqual(row.source_delivery_note_item, delivery_item)
		self.assertEqual(row.customer_schedule_item, target)
		self.assertEqual(row.allocation_method, "Direct")
		self.assertEqual(row.effective_qty, 20)
		self.assertEqual(row.cumulative_delivered_qty, 20)

	def _sync(self, customer):
		return delivery_sync.sync_delivery_allocations(
			company=self.company,
			customer=customer,
			item_codes=[self.item],
		)

	def _create_test_item(self):
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or frappe.db.get_value("Item Group", {})
		stock_uom = frappe.db.get_value("UOM", {})
		if not item_group or not stock_uom:
			self.skipTest("Delivery tests need an Item Group and UOM.")
		name = "TEST-DELIVERY-{0}".format(frappe.generate_hash(length=10))
		item = frappe.new_doc("Item")
		item.name = name
		item.item_code = name
		item.item_name = name
		item.item_group = item_group
		item.stock_uom = stock_uom
		item.is_stock_item = 1
		item.disabled = 0
		item.db_insert()
		return item.name

	def _create_schedule(self, customer, rows):
		suffix = frappe.generate_hash(length=10)
		doc = frappe.get_doc(
			{
				"doctype": "Customer Delivery Schedule",
				"customer": customer,
				"company": self.company,
				"schedule_scope": "PHASE4-{0}".format(suffix),
				"version_no": "PHASE4-{0}".format(suffix),
				"import_strategy": "Append",
				"source_type": "Customer Delivery Schedule",
				"status": "Active",
				"items": [
					{
						"item_code": self.item,
						"schedule_date": schedule_date,
						"qty": qty,
						"balance_qty": qty,
						"status": "Open",
					}
					for schedule_date, qty in rows
				],
			}
		)
		doc.flags.aps_schedule_import_transition = True
		doc.insert(ignore_permissions=True)
		return {
			"name": doc.name,
			"items": frappe.get_all(
				"Customer Delivery Schedule Item",
				filters={"parent": doc.name},
				pluck="name",
				order_by="idx asc",
			),
		}

	def _create_delivery_note(
		self,
		customer,
		qty,
		*,
		is_return=False,
		return_against=None,
		original_delivery_note_item=None,
		direct_schedule_item=None,
	):
		name = "TEST-DN-{0}".format(frappe.generate_hash(length=10))
		doc = frappe.new_doc("Delivery Note")
		doc.name = name
		doc.docstatus = 1
		doc.company = self.company
		doc.customer = customer
		# Controlled FIFO only auto-claims the matching daily customer schedule.
		doc.posting_date = self.due_date_1
		doc.posting_time = nowtime()
		doc.is_return = cint(is_return)
		doc.return_against = return_against
		doc.db_insert()
		item = frappe.new_doc("Delivery Note Item")
		item.name = frappe.generate_hash(length=10)
		item.parent = doc.name
		item.parenttype = "Delivery Note"
		item.parentfield = "items"
		item.idx = 1
		item.item_code = self.item
		item.qty = -qty if is_return else qty
		item.stock_qty = -qty if is_return else qty
		item.conversion_factor = 1
		item.dn_detail = original_delivery_note_item
		item.custom_aps_customer_schedule_item = direct_schedule_item
		item.db_insert()
		return doc.name, item.name

	@staticmethod
	def _delivered(schedule_item):
		return frappe.db.get_value("Customer Delivery Schedule Item", schedule_item, "delivered_qty") or 0
