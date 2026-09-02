from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


class APSConstraintResolution(Document):
	def validate(self):
		self.status = self.status or "Open"
		self.blocker_policy = self.blocker_policy or "Never Override"
		if self.blocker_policy == "Temporary Override" and self.status == "Approved":
			if not self.expires_on or get_datetime(self.expires_on) <= get_datetime(now_datetime()):
				frappe.throw(_("An approved temporary override requires a future expiry time.", context="Injection APS"), frappe.ValidationError)
		if self.blocker_policy != "Temporary Override" and str(self.resolution_type or "").startswith("Temporary"):
			frappe.throw(_("This blocker does not allow a temporary override.", context="Injection APS"), frappe.ValidationError)

	def on_update(self):
		self._protect_engine_managed_record()

	def on_trash(self):
		self._protect_engine_managed_record()

	def _protect_engine_managed_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_resolution_transition"):
			return
		frappe.throw(_("Constraint Resolutions are maintained by the controlled APS resolution service.", context="Injection APS"), frappe.PermissionError)
