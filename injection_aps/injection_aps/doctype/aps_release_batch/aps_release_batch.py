from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class APSReleaseBatch(Document):
	def validate(self):
		self._protect_engine_managed_record()
		self.status = self.status or "Draft"

	def on_trash(self):
		self._protect_engine_managed_record()

	def _protect_engine_managed_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_release_engine_transition"):
			return
		frappe.throw(
			_(
				"APS Release Batch is maintained by the controlled release workflow.",
				context="Injection APS",
			),
			frappe.PermissionError,
		)
