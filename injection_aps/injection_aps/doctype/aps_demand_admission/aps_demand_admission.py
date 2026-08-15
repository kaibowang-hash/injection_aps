from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


QTY_TOLERANCE = 0.000001


class APSDemandAdmission(Document):
	def validate(self):
		self.admission_class = self.admission_class or "P0"
		self.status = self.status or "Candidate"
		candidate = max(flt(self.candidate_qty), 0)
		recommended = max(flt(self.recommended_qty), 0)
		selected = max(flt(self.selected_qty), 0)
		if recommended > candidate + QTY_TOLERANCE or selected > candidate + QTY_TOLERANCE:
			frappe.throw(_("Recommended and selected admission quantities cannot exceed the candidate quantity."), frappe.ValidationError)
		if self.admission_class == "P0":
			if not self.demand_identity:
				frappe.throw(_("P0 Admission requires a Demand Identity."), frappe.ValidationError)
			if not cint(self.mandatory) or abs(selected - candidate) > QTY_TOLERANCE:
				frappe.throw(_("P0 Admission is mandatory and its full candidate quantity must remain selected."), frappe.ValidationError)
		elif cint(self.mandatory):
			frappe.throw(_("Only P0 Admission can be mandatory."), frappe.ValidationError)

	def on_update(self):
		self._protect_engine_managed_record()

	def on_trash(self):
		self._protect_engine_managed_record()

	def _protect_engine_managed_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_phase2_transition"):
			return
		frappe.throw(_("Demand Admissions are maintained by the controlled APS admission service."), frappe.PermissionError)
