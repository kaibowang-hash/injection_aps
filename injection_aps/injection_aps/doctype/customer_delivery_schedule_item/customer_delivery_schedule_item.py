from __future__ import annotations

from frappe.model.document import Document
from frappe.utils import flt

from injection_aps.services.v2_flags import is_v2_enabled


class CustomerDeliveryScheduleItem(Document):
	def validate(self):
		self.production_strategy = self.production_strategy or "Auto Balance"
		self.demand_confidence = self.demand_confidence or "Confirmed"
		if not is_v2_enabled():
			self.balance_qty = max(flt(self.qty) - flt(self.delivered_qty), 0)
			if self.balance_qty <= 0:
				self.status = "Covered"
			elif not self.status or self.status == "Covered":
				self.status = "Open"
			return
		self.original_schedule_date = self.original_schedule_date or self.schedule_date
		self.effective_schedule_date = self.schedule_date
		self.effective_qty = flt(self.qty)
		self.executed_floor_qty = max(
			flt(self.executed_floor_qty),
			flt(self.delivered_qty),
			flt(self.produced_qty),
		)
		self.excess_qty = max(flt(self.executed_floor_qty) - flt(self.effective_qty), 0)
		self.open_revised_qty = max(flt(self.effective_qty) - flt(self.delivered_qty), 0)
		self.balance_qty = self.open_revised_qty
		if flt(self.effective_qty) <= 0:
			self.status = "Cancelled"
		elif self.balance_qty <= 0:
			self.status = "Covered"
		elif not self.status or self.status in {"Covered", "Cancelled"}:
			self.status = "Open"
