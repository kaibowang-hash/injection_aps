from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class APSScheduleImportBatch(Document):
	def validate(self):
		self._protect_engine_managed_record()
		self.status = self.status or "Draft"

	def on_trash(self):
		self._protect_engine_managed_record()

	def _protect_engine_managed_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_schedule_import_transition"):
			return
		frappe.throw(
			_(
				"Schedule Import Batch is an APS audit record. Use Schedule Import & Diff to create or update it.",
				context="Injection APS",
			),
			frappe.PermissionError,
		)
