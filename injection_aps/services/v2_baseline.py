from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

import frappe
from frappe.utils import flt, now_datetime

from injection_aps.services import v2_flags


BASELINE_SCHEMA_VERSION = 1
MAX_SECTION_ROWS = 50_000

SECTION_FIELDS = {
	"APS Planning Run": (
		"name",
		"company",
		"plant_floor",
		"selected_plant_floor_summary",
		"planning_date",
		"horizon_days",
		"horizon_start",
		"horizon_end",
		"run_type",
		"existing_work_order_policy",
		"status",
		"approval_state",
		"total_net_requirement_qty",
		"total_machine_scheduled_qty",
		"total_demand_covered_qty",
		"total_overproduction_qty",
		"total_scheduled_qty",
		"total_unscheduled_qty",
		"total_produced_qty",
		"total_delivered_qty",
		"capacity_balance_status",
		"capacity_balance_fingerprint",
		"consistency_status",
		"exception_count",
		"result_count",
		"modified",
	),
	"APS Schedule Result": (
		"name",
		"planning_run",
		"company",
		"plant_floor",
		"net_requirement",
		"customer",
		"sales_order",
		"sales_order_item",
		"item_code",
		"requested_date",
		"demand_source",
		"planned_qty",
		"machine_scheduled_qty",
		"demand_covered_qty",
		"overproduction_qty",
		"scheduled_qty",
		"unscheduled_qty",
		"produced_qty",
		"good_produced_qty",
		"scrap_qty",
		"delivered_qty",
		"prebuild_qty",
		"jit_qty",
		"status",
		"risk_status",
		"actual_status",
		"is_locked",
		"is_manual",
		"demand_source_snapshot_json",
		"fulfillment_baseline_json",
		"modified",
	),
	"APS Schedule Segment": (
		"name",
		"parent",
		"workstation",
		"plant_floor",
		"start_time",
		"end_time",
		"planned_qty",
		"sequence_no",
		"campaign_key",
		"mould_reference",
		"segment_status",
		"linked_work_order",
		"linked_work_order_scheduling",
		"linked_scheduling_item",
		"actual_status",
		"actual_completed_qty",
		"actual_good_qty",
		"actual_scrap_qty",
		"is_locked",
		"is_manual",
	),
	"APS Net Requirement": (
		"name",
		"company",
		"customer",
		"sales_order",
		"sales_order_item",
		"item_code",
		"demand_date",
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"safety_stock_gap_qty",
		"planning_qty",
		"net_requirement_qty",
		"existing_work_order_policy",
		"production_strategy",
		"demand_source_snapshot_json",
		"fulfillment_baseline_json",
		"modified",
	),
	"APS Demand Pool": (
		"name",
		"company",
		"customer",
		"sales_order",
		"sales_order_item",
		"item_code",
		"demand_date",
		"demand_qty",
		"remaining_qty",
		"delivered_qty",
		"status",
		"modified",
	),
	"APS Exception Log": (
		"name",
		"planning_run",
		"severity",
		"exception_type",
		"status",
		"item_code",
		"customer",
		"workstation",
		"is_blocking",
		"source_doctype",
		"source_name",
		"modified",
	),
	"APS Work Order Proposal Batch": (
		"name",
		"planning_run",
		"company",
		"plant_floor",
		"proposal_fingerprint",
		"status",
		"approval_state",
		"proposal_count",
		"applied_count",
		"modified",
	),
	"APS Shift Schedule Proposal Batch": (
		"name",
		"planning_run",
		"company",
		"plant_floor",
		"proposal_fingerprint",
		"status",
		"approval_state",
		"proposal_count",
		"applied_count",
		"release_batch",
		"modified",
	),
	"APS Release Batch": (
		"name",
		"planning_run",
		"company",
		"release_from_date",
		"release_to_date",
		"status",
		"generated_work_orders",
		"work_order_scheduling",
		"modified",
	),
	"APS Production Allocation": (
		"name",
		"planning_run",
		"schedule_result",
		"segment",
		"customer_schedule",
		"customer_schedule_item",
		"work_order",
		"work_order_scheduling",
		"scheduling_item",
		"source_stock_entry",
		"allocated_qty",
		"good_qty",
		"scrap_qty",
		"effective_qty",
		"is_effective",
		"modified",
	),
	"APS Delivery Allocation": (
		"name",
		"company",
		"customer",
		"item_code",
		"sales_order",
		"sales_order_item",
		"customer_schedule",
		"customer_schedule_item",
		"source_delivery_note",
		"source_delivery_note_item",
		"allocated_qty",
		"effective_qty",
		"cumulative_delivered_qty",
		"is_effective",
		"modified",
	),
	"Work Order": (
		"name",
		"company",
		"production_item",
		"qty",
		"produced_qty",
		"status",
		"docstatus",
		"custom_aps_run",
		"custom_aps_result",
		"modified",
	),
	"Work Order Scheduling": (
		"name",
		"work_order",
		"docstatus",
		"custom_aps_run",
		"custom_aps_result",
		"modified",
	),
}


def capture_legacy_baseline(planning_run: str) -> dict[str, Any]:
	"""Capture a read-only, tamper-evident snapshot for one visible V1 run."""
	if not planning_run:
		frappe.throw("Planning Run is required.", frappe.ValidationError)

	run_rows = _fetch_rows("APS Planning Run", {"name": planning_run})
	if not run_rows:
		frappe.throw(f"APS Planning Run {planning_run} was not found.", frappe.DoesNotExistError)
	run = run_rows[0]
	results = _fetch_rows("APS Schedule Result", {"planning_run": planning_run})
	result_names = _names(results)
	net_requirement_names = sorted({row.get("net_requirement") for row in results if row.get("net_requirement")})
	customers = sorted({row.get("customer") for row in results if row.get("customer")})
	items = sorted({row.get("item_code") for row in results if row.get("item_code")})

	sections = {
		"planning_run": run_rows,
		"demand_pool": _fetch_context_rows(
			"APS Demand Pool",
			company=run.get("company"),
			customers=customers,
			items=items,
		),
		"net_requirements": _fetch_rows(
			"APS Net Requirement",
			{"name": ("in", net_requirement_names)},
		)
		if net_requirement_names
		else [],
		"schedule_results": results,
		"schedule_segments": _fetch_rows(
			"APS Schedule Segment",
			{"parent": ("in", result_names)},
			permission_aware=False,
		)
		if result_names
		else [],
		"exceptions": _fetch_rows("APS Exception Log", {"planning_run": planning_run}),
		"work_order_proposals": _fetch_rows(
			"APS Work Order Proposal Batch", {"planning_run": planning_run}
		),
		"shift_schedule_proposals": _fetch_rows(
			"APS Shift Schedule Proposal Batch", {"planning_run": planning_run}
		),
		"release_batches": _fetch_rows("APS Release Batch", {"planning_run": planning_run}),
		"production_allocations": _fetch_rows(
			"APS Production Allocation", {"planning_run": planning_run}
		),
		"delivery_allocations": _fetch_context_rows(
			"APS Delivery Allocation",
			company=run.get("company"),
			customers=customers,
			items=items,
		),
		"work_orders": _fetch_rows("Work Order", {"custom_aps_run": planning_run}),
		"work_order_scheduling": _fetch_rows(
			"Work Order Scheduling", {"custom_aps_run": planning_run}
		),
	}
	return build_snapshot(
		scope={"planning_run": planning_run, "company": run.get("company")},
		sections=sections,
	)


def build_snapshot(
	*,
	scope: dict[str, Any],
	sections: dict[str, Iterable[dict[str, Any]]],
	captured_at: Any | None = None,
) -> dict[str, Any]:
	normalized_sections = {
		name: sorted(
			(_normalize(row) for row in rows),
			key=lambda row: (str(row.get("name") or ""), _canonical_json(row)),
		)
		for name, rows in sorted(sections.items())
	}
	fingerprint_payload = {
		"schema_version": BASELINE_SCHEMA_VERSION,
		"source_engine": "Legacy",
		"scope": _normalize(scope),
		"sections": normalized_sections,
	}
	fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
	return {
		**fingerprint_payload,
		"capture_mode": "READ_ONLY",
		"captured_at": _normalize(captured_at or now_datetime()),
		"counts": {name: len(rows) for name, rows in normalized_sections.items()},
		"content_fingerprint": fingerprint,
	}


def capture_legacy_trial_baseline(planning_run: str) -> dict[str, Any]:
	"""Capture the compact Legacy side of a read-only Trial comparison.

	The full baseline is hashed using the same Phase 0 canonical snapshot. Only
	aggregated metrics are copied into the Solver Job audit payload, so a 10,000-row
	Trial does not duplicate its source documents in the job record.
	"""
	snapshot = capture_legacy_baseline(planning_run)
	analysis = {}
	try:
		analysis = json.loads(
			frappe.db.get_value("APS Planning Run", planning_run, "capacity_balance_analysis_json") or "{}"
		)
	except (TypeError, ValueError):
		analysis = {}
	return {
		"available": True,
		"captured_at": snapshot["captured_at"],
		"content_fingerprint": snapshot["content_fingerprint"],
		"metrics": _legacy_trial_metrics(snapshot, analysis=analysis),
	}


def _legacy_trial_metrics(snapshot: dict[str, Any], *, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
	sections = snapshot.get("sections") or {}
	run = ((sections.get("planning_run") or [{}])[0])
	results = sections.get("schedule_results") or []
	segments = sections.get("schedule_segments") or []
	planned_qty = sum(flt(row.get("planned_qty")) for row in results)
	scheduled_qty = sum(flt(row.get("machine_scheduled_qty") or row.get("scheduled_qty")) for row in results)
	unscheduled_qty = sum(flt(row.get("unscheduled_qty")) for row in results)
	late_qty = sum(flt(row.get("late_qty")) for row in results)
	summary = (analysis or {}).get("summary") or {}
	if not scheduled_qty:
		scheduled_qty = flt(run.get("total_machine_scheduled_qty") or run.get("total_scheduled_qty"))
	if not unscheduled_qty:
		unscheduled_qty = flt(run.get("total_unscheduled_qty"))
	if "unscheduled_qty" in summary:
		unscheduled_qty = flt(summary.get("unscheduled_qty"))
	if "late_qty" in summary:
		late_qty = flt(summary.get("late_qty"))
	change_count = sum(1 for row in segments if flt(row.get("changeover_minutes")) > 0)
	setup_minutes = sum(flt(row.get("setup_minutes")) + flt(row.get("changeover_minutes")) for row in segments)
	if "change_count" in summary:
		change_count = flt(summary.get("change_count"))
	if "setup_minutes" in summary:
		setup_minutes = flt(summary.get("setup_minutes"))
	return {
		"planned_qty": planned_qty or flt(run.get("total_net_requirement_qty")),
		"scheduled_qty": scheduled_qty,
		"on_time_qty": max(scheduled_qty - late_qty, 0),
		"late_qty": late_qty,
		"critical_unplanned_qty": unscheduled_qty,
		"change_count": change_count,
		"setup_minutes": setup_minutes,
		"result_count": len(results),
		"segment_count": len(segments),
		"source_status": run.get("capacity_balance_status") or run.get("status") or "Unknown",
	}


def _v2_trial_metrics(job: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
	input_snapshot = json.loads(job.get("input_snapshot_json") or "{}")
	scale = max(flt(input_snapshot.get("quantity_scale")), 1)
	metrics = scenario.get("metrics") or {}
	outcomes = scenario.get("outcomes") or []
	return {
		"planned_qty": sum(
			(flt(row.get("on_time_units")) + flt(row.get("late_units")) + flt(row.get("unscheduled_units"))) / scale
			for row in outcomes
		),
		"scheduled_qty": sum(
			(flt(row.get("on_time_units")) + flt(row.get("late_units"))) / scale
			for row in outcomes
		),
		"on_time_qty": flt(metrics.get("p0_on_time_units")) / scale,
		"late_qty": flt(metrics.get("total_late_units")) / scale,
		"critical_unplanned_qty": flt(metrics.get("p0_critical_unplanned_units")) / scale,
		"change_count": flt(metrics.get("change_count")),
		"setup_minutes": flt(metrics.get("setup_minutes")),
		"result_count": len(outcomes),
		"segment_count": len(scenario.get("tasks") or []),
		"source_status": scenario.get("status") or job.get("status") or "Unknown",
	}


def get_legacy_v2_comparison(
	planning_run: str,
	*,
	legacy_fingerprint: str | None = None,
	settings: Any | None = None,
) -> dict[str, Any]:
	config = v2_flags.get_v2_settings(settings)
	base = {
		"planning_run": planning_run,
		"enable_aps_v2": config["enable_aps_v2"],
		"formal_v2_writes_enabled": False,
		"read_only": True,
	}
	if not config["enable_aps_v2"] or config["solver_engine"] != "CP-SAT":
		return {
			**base,
			"status": "Not Available",
			"reason_code": "PHASE_0_V2_ENGINE_NOT_IMPLEMENTED",
			"message": "Enable APS V2 with the CP-SAT engine to produce a read-only Trial comparison.",
			"legacy_fingerprint": legacy_fingerprint,
			"v2_fingerprint": None,
		}
	run = frappe.db.get_value("APS Planning Run", planning_run, ["name", "run_type"], as_dict=True)
	if not run:
		frappe.throw(f"APS Planning Run {planning_run} was not found.", frappe.DoesNotExistError)
	job = frappe.db.get_value(
		"APS Solver Job",
		{"planning_run": planning_run, "status": ("in", ["Optimal", "Feasible", "Fallback", "Applied"])},
		[
			"name", "status", "selected_scenario", "solution_fingerprint",
			"input_snapshot_json", "scenarios_json", "audit_json",
		],
		order_by="modified desc",
		as_dict=True,
	)
	if not job:
		return {
			**base,
			"run_type": run.run_type,
			"status": "Awaiting Analysis",
			"reason_code": "V2_TRIAL_ANALYSIS_REQUIRED",
			"message": "Run the V2 analysis before opening the Trial comparison.",
			"legacy_fingerprint": legacy_fingerprint,
			"v2_fingerprint": None,
		}
	audit = json.loads(job.audit_json or "{}")
	legacy = audit.get("legacy_trial_baseline") or {}
	if not legacy.get("available"):
		return {
			**base,
			"run_type": run.run_type,
			"solver_job": job.name,
			"status": "Not Available",
			"reason_code": "LEGACY_TRIAL_BASELINE_MISSING",
			"message": "This Solver Job predates the Trial baseline capture. Analyze a new Trial run.",
			"legacy_fingerprint": legacy_fingerprint,
			"v2_fingerprint": job.solution_fingerprint,
		}
	stored_fingerprint = legacy.get("content_fingerprint")
	if legacy_fingerprint and legacy_fingerprint != stored_fingerprint:
		return {
			**base,
			"run_type": run.run_type,
			"solver_job": job.name,
			"status": "Stale",
			"reason_code": "LEGACY_TRIAL_BASELINE_CHANGED",
			"message": "The requested Legacy fingerprint does not match the baseline captured for this Solver Job.",
			"legacy_fingerprint": stored_fingerprint,
			"requested_legacy_fingerprint": legacy_fingerprint,
			"v2_fingerprint": job.solution_fingerprint,
		}
	scenarios = json.loads(job.scenarios_json or "[]")
	selected = next(
		(row for row in scenarios if row.get("scenario_key") == job.selected_scenario),
		None,
	)
	if not selected:
		return {
			**base,
			"run_type": run.run_type,
			"solver_job": job.name,
			"status": "Awaiting Selection",
			"reason_code": "V2_TRIAL_SCENARIO_REQUIRED",
			"message": "Select a validated V2 scenario before comparing it with Legacy.",
			"legacy_fingerprint": stored_fingerprint,
			"v2_fingerprint": job.solution_fingerprint,
		}
	legacy_metrics = legacy.get("metrics") or {}
	v2_metrics = _v2_trial_metrics(dict(job), selected)
	delta = {
		key: flt(v2_metrics.get(key)) - flt(legacy_metrics.get(key))
		for key in (
			"planned_qty", "scheduled_qty", "on_time_qty", "late_qty",
			"critical_unplanned_qty", "change_count", "setup_minutes",
		)
	}
	return {
		**base,
		"run_type": run.run_type,
		"solver_job": job.name,
		"status": "Ready",
		"reason_code": "V2_TRIAL_COMPARISON_READY",
		"message": "Trial comparison is read-only; only a Formal run can be applied.",
		"apply_allowed": run.run_type == "Formal",
		"legacy_fingerprint": stored_fingerprint,
		"v2_fingerprint": job.solution_fingerprint,
		"legacy": {"engine": "Legacy", "captured_at": legacy.get("captured_at"), "metrics": legacy_metrics},
		"v2": {"engine": selected.get("engine") or job.get("status"), "scenario": selected.get("scenario_key"), "metrics": v2_metrics},
		"delta": delta,
	}


def _fetch_context_rows(
	doctype: str,
	*,
	company: str | None,
	customers: list[str],
	items: list[str],
) -> list[dict[str, Any]]:
	if not company or not customers or not items:
		return []
	return _fetch_rows(
		doctype,
		{
			"company": company,
			"customer": ("in", customers),
			"item_code": ("in", items),
		},
	)


def _fetch_rows(
	doctype: str,
	filters: dict[str, Any],
	*,
	permission_aware: bool = True,
) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	if any(fieldname != "name" and not meta.has_field(fieldname) for fieldname in filters):
		return []
	fields = _available_fields(meta, SECTION_FIELDS[doctype])
	reader = frappe.get_list if permission_aware else frappe.get_all
	rows = reader(
		doctype,
		filters=filters,
		fields=fields,
		order_by="name asc",
		limit_page_length=MAX_SECTION_ROWS + 1,
	)
	if len(rows) > MAX_SECTION_ROWS:
		frappe.throw(
			f"Baseline section {doctype} exceeds the {MAX_SECTION_ROWS} row safety limit.",
			frappe.ValidationError,
		)
	return [dict(row) for row in rows]


def _available_fields(meta: Any, requested: Iterable[str]) -> list[str]:
	return [fieldname for fieldname in requested if fieldname == "name" or meta.has_field(fieldname)]


def _names(rows: Iterable[dict[str, Any]]) -> list[str]:
	return sorted({row.get("name") for row in rows if row.get("name")})


def _normalize(value: Any) -> Any:
	if isinstance(value, dict):
		return {str(key): _normalize(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
	if isinstance(value, (list, tuple)):
		return [_normalize(item) for item in value]
	if isinstance(value, (datetime, date)):
		return value.isoformat()
	if isinstance(value, Decimal):
		return str(value)
	if isinstance(value, bytes):
		return value.decode("utf-8", errors="replace")
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	return str(value)


def _canonical_json(value: Any) -> str:
	return json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
