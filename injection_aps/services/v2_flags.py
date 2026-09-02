from __future__ import annotations

from importlib import metadata, util
from types import MappingProxyType
from typing import Any

import frappe
from frappe.utils import cint


DEFAULTS = MappingProxyType(
	{
		"enable_aps_v2": 0,
		"solver_engine": "Legacy",
		"enable_shift_replan": 0,
		"enable_coproduct_campaign": 0,
		"enable_multilevel_bom_planning": 0,
		"delivery_legacy_match_tolerance_days": 3,
		"max_execution_staleness_minutes": 30,
		"solver_time_limit_seconds": 120,
		"shift_solver_time_limit_seconds": 30,
	}
)
PINNED_ORTOOLS_VERSION = "9.4.1874"

BOOLEAN_FLAGS = (
	"enable_aps_v2",
	"enable_shift_replan",
	"enable_coproduct_campaign",
	"enable_multilevel_bom_planning",
)
INTEGER_SETTINGS = (
	"delivery_legacy_match_tolerance_days",
	"max_execution_staleness_minutes",
	"solver_time_limit_seconds",
	"shift_solver_time_limit_seconds",
)


def get_v2_settings(settings: Any | None = None) -> dict[str, Any]:
	"""Return the complete V2 configuration with fail-closed defaults.

	The function intentionally tolerates a pre-Phase-0 database schema.  Missing
	fields never enable V2 behavior and therefore do not require a migration just
	to inspect capabilities.
	"""
	settings = settings if settings is not None else frappe.get_cached_doc("APS Settings")
	values = dict(DEFAULTS)
	for fieldname in BOOLEAN_FLAGS:
		values[fieldname] = 1 if cint(_get_value(settings, fieldname, DEFAULTS[fieldname])) else 0
	for fieldname in INTEGER_SETTINGS:
		value = cint(_get_value(settings, fieldname, DEFAULTS[fieldname]))
		values[fieldname] = max(value, 0)
	values["solver_engine"] = str(
		_get_value(settings, "solver_engine", DEFAULTS["solver_engine"]) or "Legacy"
	).strip()
	if values["solver_engine"] not in {"Legacy", "CP-SAT"}:
		values["solver_engine"] = "Legacy"
	return values


def is_v2_enabled(settings: Any | None = None) -> bool:
	return bool(get_v2_settings(settings)["enable_aps_v2"])


def get_v2_capabilities(settings: Any | None = None) -> dict[str, Any]:
	values = get_v2_settings(settings)
	ortools = _get_ortools_status()
	solver_selected = bool(values["enable_aps_v2"] and values["solver_engine"] == "CP-SAT")
	compatible = bool(ortools["available"] and ortools.get("version") == PINNED_ORTOOLS_VERSION)
	return {
		"mode": "V2" if values["enable_aps_v2"] else "Legacy",
		"formal_v2_writes_enabled": bool(solver_selected and compatible),
		"comparison_available": bool(solver_selected and compatible),
		"settings": values,
		"solver_runtime": ortools,
		"read_only_trial_available": bool(values["enable_aps_v2"] and compatible),
	}


def _get_value(settings: Any, fieldname: str, default: Any) -> Any:
	if isinstance(settings, dict):
		value = settings.get(fieldname)
	else:
		value = getattr(settings, fieldname, None)
	return default if value in (None, "") else value


def _get_ortools_status() -> dict[str, Any]:
	if util.find_spec("ortools") is None:
		return {
			"available": False,
			"version": None,
			"reason": "OR-Tools is not installed in the current Python environment.",
		}
	try:
		version = metadata.version("ortools")
	except metadata.PackageNotFoundError:
		version = "unknown"
	return {
		"available": True,
		"version": version,
		"compatible": version == PINNED_ORTOOLS_VERSION,
		"reason": None if version == PINNED_ORTOOLS_VERSION else f"Expected OR-Tools {PINNED_ORTOOLS_VERSION}; found {version}.",
	}
