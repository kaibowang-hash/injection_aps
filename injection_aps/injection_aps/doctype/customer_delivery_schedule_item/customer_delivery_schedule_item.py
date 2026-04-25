from __future__ import annotations

from frappe.model.document import Document
from frappe.utils import flt


class CustomerDeliveryScheduleItem(Document):
	def validate(self):
		self.balance_qty = max(flt(self.qty) - flt(self.delivered_qty), 0)
		if self.balance_qty <= 0:
			self.status = "Covered"
		elif not self.status or self.status == "Covered":
			self.status = "Open"
