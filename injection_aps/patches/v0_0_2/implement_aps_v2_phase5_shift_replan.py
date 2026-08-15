from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from injection_aps.setup.resources import STANDARD_CUSTOM_FIELDS


EXTERNAL_DOCTYPES = ("Work Order", "Work Order Scheduling", "Scheduling Item")


def execute() -> None:
	"""Install missing lineage fields without updating any existing customization."""
	definitions = {
		doctype: STANDARD_CUSTOM_FIELDS[doctype]
		for doctype in EXTERNAL_DOCTYPES
		if doctype in STANDARD_CUSTOM_FIELDS and frappe.db.exists("DocType", doctype)
	}
	if definitions:
		create_custom_fields(definitions, update=False)
	if frappe.db.exists("DocType", "APS Schedule Segment"):
		frappe.db.sql(
			"""
			update `tabAPS Schedule Segment`
			set baseline_start_time=coalesce(baseline_start_time, start_time),
				baseline_end_time=coalesce(baseline_end_time, end_time),
				current_start_time=coalesce(current_start_time, start_time),
				current_end_time=coalesce(current_end_time, end_time),
				forecast_start_time=coalesce(forecast_start_time, start_time),
				forecast_end_time=coalesce(forecast_end_time, end_time)
			where start_time is not null and end_time is not null
			"""
		)
