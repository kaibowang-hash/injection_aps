from __future__ import annotations

import frappe


def execute() -> None:
	settings = frappe.get_single("APS Settings")
	values = {}
	if not settings.get("aps_producible_item_groups"):
		values["aps_producible_item_groups"] = "Plastic Part\nSub-assemblies"
	if not settings.get("aps_bom_policy"):
		values["aps_bom_policy"] = "Default BOM Only"
	if values:
		frappe.db.set_value("APS Settings", "APS Settings", values, update_modified=False)
	# Pegging is created only from a validated V2 Apply. Historical BOM remarks
	# are deliberately not guessed or backfilled into formal lineage.
