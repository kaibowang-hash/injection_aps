from __future__ import annotations

import frappe
from frappe.utils import cint

from injection_aps.services.v2_flags import DEFAULTS


BOOLEAN_FLAGS = {
	"enable_aps_v2",
	"enable_shift_replan",
	"enable_coproduct_campaign",
	"enable_multilevel_bom_planning",
}


def execute() -> None:
	"""Install Phase 0 controls fail-closed without changing Legacy horizons."""
	if not frappe.db.exists("DocType", "APS Settings"):
		return
	meta = frappe.get_meta("APS Settings")
	for fieldname, default in DEFAULTS.items():
		if not meta.has_field(fieldname):
			continue
		current = frappe.db.get_single_value("APS Settings", fieldname)
		if fieldname in BOOLEAN_FLAGS:
			value = 0
		elif fieldname == "solver_engine":
			value = "Legacy"
		else:
			value = default if current in (None, "") or cint(current) <= 0 else current
		if current != value:
			frappe.db.set_single_value("APS Settings", fieldname, value)
