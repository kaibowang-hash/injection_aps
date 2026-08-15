from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from injection_aps.setup.resources import STANDARD_CUSTOM_FIELDS


EXTERNAL_DOCTYPES = ("Work Order", "Work Order Scheduling", "Scheduling Item")


def execute() -> None:
	definitions = {
		doctype: STANDARD_CUSTOM_FIELDS[doctype]
		for doctype in EXTERNAL_DOCTYPES
		if doctype in STANDARD_CUSTOM_FIELDS and frappe.db.exists("DocType", doctype)
	}
	if definitions:
		create_custom_fields(definitions, update=False)
	# Historical Family Co-Product rows have no exact output ledger. Keep them
	# read-only; only new V2 Campaign records become formal capacity owners.
