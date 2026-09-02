from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from injection_aps.services.schedule_revision import backfill_active_demand_identities
from injection_aps.setup.resources import STANDARD_CUSTOM_FIELDS


PHASE1_EXTERNAL_DOCTYPES = (
	"Delivery Plan Item Qty",
	"Delivery Plan Item",
	"Delivery Note Item",
)


def execute() -> None:
	"""Create missing Phase 1 lineage fields, then backfill only unique active rows.

	The patch never updates an existing Custom Field definition or any field-order
	Property Setter, Workspace, Page, Client Script, or Custom HTML Block.
	"""
	definitions = {
		doctype: STANDARD_CUSTOM_FIELDS[doctype]
		for doctype in PHASE1_EXTERNAL_DOCTYPES
		if doctype in STANDARD_CUSTOM_FIELDS and frappe.db.exists("DocType", doctype)
	}
	if definitions:
		create_custom_fields(definitions, update=False)
	backfill_active_demand_identities()
