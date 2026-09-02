from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from injection_aps.services.demand_ledger import backfill_active_run_commitments
from injection_aps.setup.resources import STANDARD_CUSTOM_FIELDS


PHASE2_EXTERNAL_FIELDS = {
	"Work Order": ("custom_aps_commitment",),
	"Scheduling Item": ("custom_aps_commitment",),
}


def execute() -> None:
	"""Create only missing lineage fields and conservatively backfill active owners.

	Existing Custom Field definitions and all user-facing resources remain untouched.
	Ambiguous Legacy ownership is stored as Conflict without a Formal owner key.
	"""
	definitions = {}
	for doctype, fieldnames in PHASE2_EXTERNAL_FIELDS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		selected = [
			field for field in STANDARD_CUSTOM_FIELDS.get(doctype, [])
			if field.get("fieldname") in fieldnames
		]
		if selected:
			definitions[doctype] = selected
	if definitions:
		create_custom_fields(definitions, update=False)
	backfill_active_run_commitments()
