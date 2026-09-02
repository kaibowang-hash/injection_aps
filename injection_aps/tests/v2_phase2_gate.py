from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe

from injection_aps.setup.resources import APS_OWNED_PAGE_NAMES, get_standard_custom_field_names
from injection_aps.tests.v2_phase0_gate import assert_isolated_environment


def capture_frontend_customization_fingerprint() -> dict[str, Any]:
	"""Read a semantic frontend/customization fingerprint before and after migrate."""
	assert_isolated_environment(require_fixture=True)
	sections = {
		"workspace": _rows(
			"Workspace",
			{"name": "Injection APS"},
			("name", "label", "title", "content", "public", "is_hidden", "module", "for_user"),
		),
		"pages": _rows(
			"Page",
			{"name": ("in", APS_OWNED_PAGE_NAMES)},
			("name", "title", "content", "module", "standard", "system_page"),
		),
		"custom_html": _rows(
			"Custom HTML Block",
			{"name": "Injection APS Dashboard"},
			("name", "html", "script", "style"),
		),
		"client_scripts": _rows(
			"Client Script",
			{"dt": ("like", "APS%")},
			("name", "dt", "view", "enabled", "script"),
		),
		"property_setters": _rows(
			"Property Setter",
			{"doc_type": ("in", ["Item", "APS Planning Run", "Customer Delivery Schedule"])},
			("name", "doc_type", "field_name", "property", "property_type", "value"),
		),
		"custom_fields": _rows(
			"Custom Field",
			{"name": ("in", get_standard_custom_field_names())},
			(
				"name", "dt", "fieldname", "label", "fieldtype", "options", "insert_after",
				"hidden", "read_only", "reqd", "allow_on_submit", "no_copy", "default",
			),
		),
	}
	section_fingerprints = {key: _fingerprint(value) for key, value in sections.items()}
	return {
		"site": frappe.local.site,
		"section_fingerprints": section_fingerprints,
		"record_fingerprints": {
			key: {str(row.get("name") or index): _fingerprint(row) for index, row in enumerate(value)}
			for key, value in sections.items()
		},
		"fingerprint": _fingerprint(section_fingerprints),
		"counts": {key: len(value) for key, value in sections.items()},
	}


def capture_phase2_flag_state() -> dict[str, Any]:
	assert_isolated_environment(require_fixture=True)
	settings = frappe.get_cached_doc("APS Settings")
	return {
		"site": frappe.local.site,
		"enable_aps_v2": int(settings.get("enable_aps_v2") or 0),
		"solver_engine": settings.get("solver_engine") or "Legacy",
		"enable_shift_replan": int(settings.get("enable_shift_replan") or 0),
		"enable_coproduct_campaign": int(settings.get("enable_coproduct_campaign") or 0),
		"enable_multilevel_bom_planning": int(settings.get("enable_multilevel_bom_planning") or 0),
	}


def _rows(doctype: str, filters: dict[str, Any], fields: tuple[str, ...]) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	available = [fieldname for fieldname in fields if fieldname == "name" or meta.has_field(fieldname)]
	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=available,
		order_by="name asc",
		limit_page_length=0,
	)
	return [
		{fieldname: row.get(fieldname) for fieldname in available}
		for row in rows
	]


def _fingerprint(value: Any) -> str:
	return hashlib.sha256(
		json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()
