from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class APSProductionAllocation(Document):
	def validate(self):
		if not self.allocation_key:
			frappe.throw(_("Production allocation key is required."))
		for fieldname in ("source_qty", "allocated_qty", "good_qty", "scrap_qty", "effective_qty", "reversed_qty"):
			if flt(self.get(fieldname)) < 0:
				frappe.throw(_("{0} cannot be negative.").format(self.meta.get_label(fieldname)))
		if flt(self.good_qty) + flt(self.scrap_qty) > flt(self.allocated_qty) + 0.0001:
			frappe.throw(_("Good and scrap quantities cannot exceed the allocated production quantity."))
