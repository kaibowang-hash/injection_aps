from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class APSSolverJob(Document):
	def validate(self):
		self.status = self.status or "Draft"
		self.engine = self.engine or "CP-SAT"

	def on_update(self):
		self._protect_engine_record()

	def on_trash(self):
		self._protect_engine_record()

	def _protect_engine_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_solver_transition"):
			return
		frappe.throw(_("Solver Jobs are maintained by the controlled APS solver service.", context="Injection APS"), frappe.PermissionError)
