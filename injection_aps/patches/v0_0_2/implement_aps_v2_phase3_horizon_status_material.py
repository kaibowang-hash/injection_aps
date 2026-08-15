from __future__ import annotations

import frappe


SETTING_DEFAULTS = {
	"default_freeze_horizon_days": 2,
	"default_restricted_horizon_days": 7,
	"default_recovery_horizon_days": 7,
	"due_time_policy": "Delivery Date End Of Day",
}


def execute() -> None:
	"""Initialize only blank Phase 3 settings; never rewrite existing UI records."""
	if not frappe.db.exists("DocType", "APS Settings"):
		return
	for fieldname, default in SETTING_DEFAULTS.items():
		if not frappe.get_meta("APS Settings").has_field(fieldname):
			continue
		value = frappe.db.get_single_value("APS Settings", fieldname)
		if value in (None, "") or (fieldname != "due_time_policy" and int(value or 0) <= 0):
			frappe.db.set_single_value("APS Settings", fieldname, default)
