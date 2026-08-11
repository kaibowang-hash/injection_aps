from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from injection_aps.services.consistency import calculate_quantity_fields, is_effective_primary_segment


class APSScheduleResult(Document):
	def validate(self):
		self._protect_engine_managed_record()
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

	def on_trash(self):
		self._protect_engine_managed_record()

	def _protect_engine_managed_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_result_engine_transition"):
			return
		frappe.throw(
			_(
				"APS Schedule Results are maintained by planning, Change Impact, and controlled schedule actions.",
				context="Injection APS",
			),
			frappe.PermissionError,
		)
