from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime


class APSSegmentAdjustment(Document):
	def validate(self):
		self.status = self.status or "Confirmed"
		if self.target_start_time and self.target_end_time and get_datetime(self.target_end_time) <= get_datetime(self.target_start_time):
			frappe.throw(_("Adjustment end time must be later than start time."))
