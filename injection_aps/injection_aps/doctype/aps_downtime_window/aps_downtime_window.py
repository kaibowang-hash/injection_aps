from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime


class APSDowntimeWindow(Document):
	def validate(self):
		self.status = self.status or "Active"
		self.scope = self.scope or "Plant Floor"
		if self.start_time and self.end_time and get_datetime(self.end_time) <= get_datetime(self.start_time):
			frappe.throw(_("Downtime end time must be later than start time."))
		if self.scope == "Workstation" and not self.workstation:
			frappe.throw(_("Workstation is required for workstation downtime."))
		if self.scope == "Plant Floor" and not self.plant_floor:
			frappe.throw(_("Plant Floor is required for plant floor downtime."))
