from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class CustomerDeliverySchedule(Document):
	def validate(self):
		self.status = self.status or "Draft"
		self.source_type = self.source_type or "Customer Delivery Schedule"
		self.import_strategy = self.import_strategy or "Replace Scope"
		self.schedule_scope = (self.schedule_scope or self.version_no or "").strip()
		self.schedule_total_qty = sum(flt(row.qty) for row in self.get("items") or [])
		self._validate_active_version()

	def _validate_active_version(self):
		if (
			self.status != "Active"
			or self.import_strategy == "Append"
			or not self.customer
			or not self.company
			or not self.schedule_scope
		):
			return

		existing = frappe.get_all(
			"Customer Delivery Schedule",
			filters={
				"customer": self.customer,
				"company": self.company,
				"schedule_scope": self.schedule_scope,
				"status": "Active",
				"name": ("!=", self.name),
			},
			pluck="name",
			limit=1,
		)
		if existing:
			frappe.throw(
				_("Only one active delivery schedule version is allowed per customer, company, and schedule scope.")
			)
