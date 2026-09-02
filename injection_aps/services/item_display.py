from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import frappe


def attach_item_display_fields(
	payload: Any,
	*,
	item_fields: tuple[str, ...] = ("item_code",),
) -> Any:
	"""Attach Item.customer_code and Item.item_name to nested APS response rows.

	The response is enriched in place.  Item metadata is loaded once for all unique
	Item identities so list pages never fall back to one database query per row.
	"""
	records: list[tuple[MutableMapping[str, Any], str]] = []
	item_codes: set[str] = set()
	visited: set[int] = set()

	def collect(value: Any) -> None:
		if isinstance(value, MutableMapping):
			identity = id(value)
			if identity in visited:
				return
			visited.add(identity)
			item_code = next(
				(
					str(value.get(fieldname) or "").strip()
					for fieldname in item_fields
					if value.get(fieldname)
				),
				"",
			)
			if item_code:
				records.append((value, item_code))
				item_codes.add(item_code)
			for child in value.values():
				collect(child)
		elif isinstance(value, (list, tuple)):
			identity = id(value)
			if identity in visited:
				return
			visited.add(identity)
			for child in value:
				collect(child)

	collect(payload)
	if not item_codes:
		return payload

	item_meta = frappe.get_meta("Item")
	fields = ["name", "item_name"]
	has_customer_code = bool(item_meta.has_field("customer_code"))
	if has_customer_code:
		fields.append("customer_code")
	item_rows = frappe.get_list(
		"Item",
		filters={"name": ("in", sorted(item_codes))},
		fields=fields,
		limit_page_length=0,
	)
	item_map = {str(row.get("name") or ""): row for row in item_rows}
	for record, item_code in records:
		item = item_map.get(item_code)
		if not item:
			continue
		record["customer_code"] = (
			item.get("customer_code") or "" if has_customer_code else ""
		)
		record["item_name"] = item.get("item_name") or ""
	return payload
