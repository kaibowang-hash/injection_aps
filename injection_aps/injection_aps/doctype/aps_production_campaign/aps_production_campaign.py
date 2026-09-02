from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_datetime


class APSProductionCampaign(Document):
	def validate(self):
		self.status = self.status or "Draft"
		if flt(self.planned_cycles) < 0:
			frappe.throw(_("Campaign planned cycles cannot be negative.", context="Injection APS"))
		outputs = list(self.get("outputs") or [])
		if len(outputs) < 2:
			frappe.throw(_("A production campaign requires at least two physical outputs.", context="Injection APS"))
		if len({row.item_code for row in outputs if row.item_code}) != len(outputs):
			frappe.throw(_("Campaign output Item Codes must be unique.", context="Injection APS"))
		owners = [row for row in outputs if row.output_role == "Primary"]
		if len(owners) != 1:
			frappe.throw(_("A production campaign requires exactly one Primary output.", context="Injection APS"))
		if self.start_time and self.end_time and get_datetime(self.end_time) <= get_datetime(self.start_time):
			frappe.throw(_("Campaign end time must be later than start time.", context="Injection APS"))
		for row in outputs:
			if flt(row.output_per_cycle) <= 0:
				frappe.throw(_("Campaign output {0} requires a positive output per cycle.", context="Injection APS").format(row.item_code or "-"))
			expected = flt(self.planned_cycles) * flt(row.output_per_cycle)
			if abs(flt(row.planned_qty) - expected) > 0.000001:
				frappe.throw(_("Campaign output {0} planned quantity must equal planned cycles × output per cycle.", context="Injection APS").format(row.item_code or "-"))

	def on_update(self):
		self._protect_engine_record()

	def on_trash(self):
		self._protect_engine_record()

	def _protect_engine_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_campaign_transition"):
			return
		frappe.throw(_("Production Campaigns are maintained by the controlled APS campaign service.", context="Injection APS"), frappe.PermissionError)
