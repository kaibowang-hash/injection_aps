from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from injection_aps.setup.resources import STANDARD_CUSTOM_FIELDS


LEGACY_FIELD = "custom_food_grade"
APS_FIELD = "custom_aps_food_grade"


def execute():
	"""Move only Injection APS' legacy default to its namespaced Item field.

	The legacy field may also be owned by another app or by the site.  This patch
	therefore copies blank targets and changes the APS setting, but never removes,
	renames or alters the old Custom Field.
	"""
	if not frappe.db.exists("DocType", "Item"):
		return
	field = next(
		(row for row in STANDARD_CUSTOM_FIELDS.get("Item", []) if row.get("fieldname") == APS_FIELD),
		None,
	)
	if field:
		create_custom_fields({"Item": [dict(field)]}, update=True)

	if not frappe.db.exists("DocType", "APS Settings"):
		return
	configured_field = frappe.db.get_single_value("APS Settings", "item_food_grade_field") or ""
	if configured_field not in ("", LEGACY_FIELD):
		# A site explicitly selected another field.  Do not reinterpret its ownership
		# or silently switch the configured business rule.
		return
	if frappe.db.has_column("Item", LEGACY_FIELD) and frappe.db.has_column("Item", APS_FIELD):
		frappe.db.sql(
			f"""
			update `tabItem`
			set `{APS_FIELD}` = `{LEGACY_FIELD}`
			where ifnull(`{APS_FIELD}`, '') = ''
				and ifnull(`{LEGACY_FIELD}`, '') != ''
			"""
		)
	frappe.db.set_single_value("APS Settings", "item_food_grade_field", APS_FIELD)
