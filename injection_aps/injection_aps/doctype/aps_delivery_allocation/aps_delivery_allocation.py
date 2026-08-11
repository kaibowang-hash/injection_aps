from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class APSDeliveryAllocation(Document):
	def validate(self):
		if not self.allocation_key:
			frappe.throw(_("Delivery allocation key is required."))
		for fieldname in ("source_qty", "allocated_qty", "reversed_qty"):
			if flt(self.get(fieldname)) < 0:
				frappe.throw(
					_("{0} cannot be negative.", context="APS Delivery Allocation").format(
						self.meta.get_label(fieldname)
					)
				)
