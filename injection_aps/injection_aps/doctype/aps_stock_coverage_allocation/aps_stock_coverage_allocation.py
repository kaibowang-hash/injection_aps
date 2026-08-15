from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


QTY_TOLERANCE = 0.000001


class APSStockCoverageAllocation(Document):
	def validate(self):
		self.status = self.status or "Active"
		allocated = flt(self.allocated_qty)
		consumed = flt(self.consumed_qty)
		released = flt(self.released_qty)
		if min(allocated, consumed, released) < -QTY_TOLERANCE:
			frappe.throw(_("Stock coverage quantities cannot be negative."), frappe.ValidationError)
		if consumed + released > allocated + QTY_TOLERANCE:
			frappe.throw(_("Consumed and released stock coverage cannot exceed its allocation."), frappe.ValidationError)
		self.remaining_qty = max(allocated - consumed - released, 0)
		self.source_snapshot_time = self.source_snapshot_time or now_datetime()
		if self.status == "Active" and self.remaining_qty <= QTY_TOLERANCE:
			self.status = "Consumed" if consumed > 0 else "Released"

	def on_update(self):
		self._protect_engine_managed_record()

	def on_trash(self):
		self._protect_engine_managed_record()

	def _protect_engine_managed_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_phase2_transition"):
			return
		frappe.throw(_("Stock Coverage Allocations are maintained by the controlled APS demand ledger."), frappe.PermissionError)
