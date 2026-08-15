from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class APSReplanCycle(Document):
	def validate(self):
		self.status = self.status or "Draft"
		self.cycle_type = self.cycle_type or "Scheduled Shift"

	def on_update(self):
		self._protect_engine_record()

	def on_trash(self):
		self._protect_engine_record()

	def _protect_engine_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_replan_transition"):
			return
		frappe.throw(
			_("Replan Cycles are maintained by the controlled APS shift-replan service.", context="Injection APS"),
			frappe.PermissionError,
		)
