from __future__ import annotations

from frappe.model.document import Document
from frappe.utils import flt

from injection_aps.services.consistency import calculate_quantity_fields, is_effective_primary_segment


class APSScheduleResult(Document):
	def validate(self):
		machine_scheduled_qty = sum(
			flt(row.planned_qty)
			for row in (self.get("segments") or [])
			if is_effective_primary_segment(row)
		)
		quantities = calculate_quantity_fields(self.planned_qty, machine_scheduled_qty)
		self.machine_scheduled_qty = quantities["machine_scheduled_qty"]
		self.demand_covered_qty = quantities["demand_covered_qty"]
		self.overproduction_qty = quantities["overproduction_qty"]
		self.scheduled_qty = quantities["machine_scheduled_qty"]
		self.unscheduled_qty = quantities["unscheduled_qty"]
		self.status = self.status or "Draft"
		self.risk_status = self.risk_status or "Normal"
