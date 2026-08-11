from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class APSChangeApplicationLog(Document):
	def validate(self):
		if not self.is_new() and not self.flags.get("allow_change_engine_update"):
			frappe.throw(_("APS Change Application Log records are immutable."), frappe.ValidationError)

	def on_trash(self):
		if not self.flags.get("allow_change_engine_delete") and not getattr(frappe.flags, "in_uninstall", False):
			frappe.throw(_("APS Change Application Log records cannot be deleted."), frappe.ValidationError)
