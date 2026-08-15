from __future__ import annotations

import frappe


CANONICAL_FIELD = "custom_food_grade"
DEPRECATED_APS_FIELD = "custom_aps_food_grade"


def execute():
	"""Restore Item.custom_food_grade as the authoritative APS food-grade source.

	The namespaced field was introduced by Injection APS and duplicated the
	existing Item master data. Preserve any values that exist only in that field,
	then remove it after switching APS back to the shared master-data field.
	"""
	if not frappe.db.exists("DocType", "Item"):
		return

	if not frappe.db.exists("DocType", "APS Settings"):
		return
	configured_field = frappe.db.get_single_value("APS Settings", "item_food_grade_field") or ""
	if configured_field not in ("", CANONICAL_FIELD, DEPRECATED_APS_FIELD):
		# A site explicitly selected another field.  Do not reinterpret its ownership
		# or silently switch the configured business rule.
		return
	has_canonical_field = frappe.db.has_column("Item", CANONICAL_FIELD)
	has_deprecated_field = frappe.db.has_column("Item", DEPRECATED_APS_FIELD)
	if has_canonical_field and has_deprecated_field:
		frappe.db.sql(
			f"""
			update `tabItem`
			set `{CANONICAL_FIELD}` = `{DEPRECATED_APS_FIELD}`
			where ifnull(`{CANONICAL_FIELD}`, '') = ''
				and ifnull(`{DEPRECATED_APS_FIELD}`, '') != ''
			"""
		)
	frappe.db.set_single_value("APS Settings", "item_food_grade_field", CANONICAL_FIELD)

	deprecated_custom_field = f"Item-{DEPRECATED_APS_FIELD}"
	if has_canonical_field and frappe.db.exists("Custom Field", deprecated_custom_field):
		frappe.delete_doc(
			"Custom Field",
			deprecated_custom_field,
			force=1,
			ignore_permissions=True,
		)
