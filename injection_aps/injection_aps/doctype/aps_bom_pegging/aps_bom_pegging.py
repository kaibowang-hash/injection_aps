from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class APSBOMPegging(Document):
	def validate(self):
		if self.get("is_raw_material_leaf"):
			if flt(self.get("production_qty")) > 0:
				frappe.throw(_("Raw-material advisory Pegging cannot own machine production.", context="Injection APS"))
			return
		required = flt(self.get("required_gross_qty"))
		covered = flt(self.get("stock_covered_qty")) + flt(self.get("wip_covered_qty")) + flt(self.get("production_qty"))
		excess = flt(self.get("batch_excess_qty"))
		if abs(covered - required - excess) > 0.000001:
			frappe.throw(_("BOM Pegging quantity does not conserve stock, WIP, production, and batch excess.", context="Injection APS"))
		if flt(self.get("production_qty")) > 0 and not self.get("child_demand_key"):
			frappe.throw(_("Manufactured BOM production requires an exact child demand key.", context="Injection APS"))

	def on_update(self):
		self._protect_engine_record()

	def on_trash(self):
		self._protect_engine_record()

	def _protect_engine_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_bom_transition"):
			return
		frappe.throw(_("BOM Pegging rows are maintained by the controlled APS BOM service.", context="Injection APS"), frappe.PermissionError)
