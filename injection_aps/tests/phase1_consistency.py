from __future__ import annotations

from pathlib import Path

import frappe
from frappe.utils import flt, getdate, now_datetime, today

from injection_aps.api import app
from injection_aps.services import consistency, planning
from injection_aps.tests.phase0_baseline import (
	COMPANY,
	ITEMS,
	TEST_SITE,
	_png_dimensions,
	_read_json,
	_sha256_file,
	_write_json,
	seed_phase0_scenarios,
)


QUANTITY_FIELDS = (
	"planned_qty",
	"machine_scheduled_qty",
	"demand_covered_qty",
	"overproduction_qty",
	"unscheduled_qty",
	"produced_qty",
	"delivered_qty",
)
PHASE1_SCREENSHOTS = (
	"01-run-console.png",
	"02-gantt-late-risk.png",
	"03-release-center.png",
)


def run_phase1_gate(output_dir: str | None = None) -> dict:
	_assert_test_site()
	frappe.set_user("Administrator")
	artifact_dir = Path(output_dir or frappe.get_site_path("private", "files", "aps_phase1_consistency"))
	artifact_dir.mkdir(parents=True, exist_ok=True)

	seed = seed_phase0_scenarios()
	run_name = seed["planning_run"]
	initial = consistency.recalculate_plan_consistency(run_name, reason="phase1 canonical baseline")
	initial_rows = _get_result_rows(run_name)
	initial_formula_errors = _get_formula_errors(run_name, initial_rows)

	late_result = next(
		row
		for row in initial_rows
		if row.item_code == ITEMS["cancel"] and getdate(row.requested_date) < getdate(today())
	)
	late_segment = frappe.db.get_value(
		"APS Schedule Segment",
		{"parent": late_result.name, "risk_status": "Critical"},
		["name", "risk_status", "schedule_delay_minutes"],
		as_dict=True,
	)
	late_exception = frappe.db.get_value(
		"APS Exception Log",
		{
			"planning_run": run_name,
			"exception_type": "Late Delivery",
			"source_doctype": "APS Schedule Segment",
			"source_name": late_segment.name,
			"status": "Open",
		},
		["name", "severity", "source_name"],
		as_dict=True,
	)
	gantt_before = app.get_schedule_gantt_data(run_name)
	late_task = next(task for task in gantt_before["tasks"] if task["id"] == late_segment.name)

	progress_result = next(row for row in initial_rows if row.item_code == ITEMS["flow"])
	progress_values = {
		"produced_qty": flt(progress_result.produced_qty),
		"delivered_qty": flt(progress_result.delivered_qty),
	}

	manual_result = next(row for row in initial_rows if row.item_code == ITEMS["increase"])
	manual_segment = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": manual_result.name, "segment_status": "Planned"},
		fields=["name", "planned_qty"],
		order_by="idx asc",
		limit=1,
	)[0]
	frappe.db.set_value(
		"APS Schedule Segment",
		manual_segment.name,
		{"planned_qty": 90, "segment_kind": "Manual", "is_manual": 1},
		update_modified=False,
	)
	manual_recalculation = planning._refresh_result_after_manual_adjustment(manual_result.name)
	manual_result_after = frappe.db.get_value(
		"APS Schedule Result",
		manual_result.name,
		list(QUANTITY_FIELDS) + ["risk_status"],
		as_dict=True,
	)
	manual_effective_sum = _get_effective_segment_sum(manual_result.name)

	gantt_after = app.get_schedule_gantt_data(run_name)
	release_after = app.get_release_center_data(run_name)
	console_after = app.get_run_console_data(company=COMPANY)
	console_run = next(row for row in console_after["runs"] if row["name"] == run_name)
	manual_task = next(task for task in gantt_after["tasks"] if task["id"] == manual_segment.name)
	page_summaries_match = _summaries_match(
		manual_recalculation["totals"],
		gantt_after["quantity_summary"],
		release_after["quantity_summary"],
		{
			"planned_qty": console_run["total_net_requirement_qty"],
			"machine_scheduled_qty": console_run["total_machine_scheduled_qty"],
			"demand_covered_qty": console_run["total_demand_covered_qty"],
			"overproduction_qty": console_run["total_overproduction_qty"],
			"unscheduled_qty": console_run["total_unscheduled_qty"],
			"produced_qty": console_run["total_produced_qty"],
			"delivered_qty": console_run["total_delivered_qty"],
		},
	)

	cancel_result = next(row for row in initial_rows if row.item_code == ITEMS["split"])
	cancel_segment = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": cancel_result.name},
		fields=["name", "segment_status", "planned_qty"],
		order_by="idx asc",
		limit=1,
	)[0]
	frappe.db.set_value(
		"APS Schedule Segment", cancel_segment.name, "segment_status", "Cancelled", update_modified=False
	)
	cancelled_recalculation = consistency.recalculate_plan_consistency(
		run_name, reason="phase1 cancelled segment exclusion"
	)
	cancelled_machine_qty = flt(
		frappe.db.get_value("APS Schedule Result", cancel_result.name, "machine_scheduled_qty")
	)
	cancelled_gantt = app.get_schedule_gantt_data(run_name)
	cancelled_segment_excluded = abs(
		cancelled_machine_qty - (flt(cancel_result.machine_scheduled_qty) - flt(cancel_segment.planned_qty))
	) < consistency.QTY_TOLERANCE and cancel_segment.name not in {
		task["id"] for task in cancelled_gantt["tasks"]
	}
	frappe.db.set_value(
		"APS Schedule Segment", cancel_segment.name, "segment_status", cancel_segment.segment_status, update_modified=False
	)
	consistency.recalculate_plan_consistency(run_name, reason="phase1 cancelled segment restoration")

	invalid_segment = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": cancel_result.name},
		fields=["name", "workstation"],
		order_by="idx asc",
		limit=1,
	)[0]
	frappe.db.set_value("APS Schedule Segment", invalid_segment.name, "workstation", None, update_modified=False)
	invalid_recalculation = consistency.recalculate_plan_consistency(
		run_name, reason="phase1 release-blocking probe"
	)
	invalid_consistency_status = frappe.db.get_value("APS Planning Run", run_name, "consistency_status")
	invalid_gantt = app.get_schedule_gantt_data(run_name)
	invalid_gantt_result = next(row for row in invalid_gantt["blocked_results"] if row["name"] == cancel_result.name)
	release_blocked = False
	release_error = ""
	try:
		planning.approve_planning_run(run_name)
	except frappe.ValidationError as exc:
		release_blocked = True
		release_error = str(exc)
	invalid_exception_exists = bool(
		frappe.db.exists(
			"APS Exception Log",
			{
				"planning_run": run_name,
				"exception_type": "Plan Consistency Error",
				"status": "Open",
			},
		)
	)
	frappe.db.set_value(
		"APS Schedule Segment", invalid_segment.name, "workstation", invalid_segment.workstation, update_modified=False
	)
	final_recalculation = consistency.recalculate_plan_consistency(run_name, reason="phase1 final valid state")
	final_result = frappe.db.get_value(
		"APS Schedule Result",
		cancel_result.name,
		["status", "risk_status", "blocking_reason"],
		as_dict=True,
	)

	gates = {
		"isolated_test_site": frappe.local.site == TEST_SITE,
		"initial_recalculation_valid": bool(initial["valid"]),
		"canonical_formulas_match_segments": not initial_formula_errors,
		"late_segment_is_critical": bool(
			late_segment
			and late_segment.risk_status == "Critical"
			and flt(late_segment.schedule_delay_minutes) > 0
		),
		"late_result_is_critical": late_result.risk_status == "Critical",
		"late_exception_matches_segment": bool(late_exception and late_exception.source_name == late_segment.name),
		"gantt_late_segment_is_red": bool(
			late_task["custom_class"] == "ia-risk-critical"
			and late_task["details"]["risk_status"] == "Critical"
			and "Late Delivery" in late_task["details"]["risk_badges"]
		),
		"produced_and_delivered_are_canonical": progress_values == {"produced_qty": 60.0, "delivered_qty": 30.0},
		"manual_adjustment_recalculated_header": bool(
			manual_recalculation["valid"]
			and abs(flt(manual_result_after.machine_scheduled_qty) - manual_effective_sum)
			< consistency.QTY_TOLERANCE
			and flt(manual_result_after.demand_covered_qty) == 100
			and flt(manual_result_after.overproduction_qty) == 40
			and flt(manual_result_after.unscheduled_qty) == 0
		),
		"gantt_uses_unambiguous_quantities": bool(
			flt(manual_task["details"]["planned_qty"]) == 100
			and flt(manual_task["details"]["segment_planned_qty"]) == 90
		),
		"plan_head_gantt_and_release_totals_match": page_summaries_match,
		"cancelled_segment_is_excluded": bool(cancelled_recalculation["valid"] and cancelled_segment_excluded),
		"invalid_segment_marks_run_invalid": bool(
			not invalid_recalculation["valid"]
			and invalid_consistency_status == "Invalid"
		),
		"invalid_plan_creates_exception": invalid_exception_exists,
		"invalid_segment_is_a_gantt_blocker": bool(
			invalid_segment.name not in {task["id"] for task in invalid_gantt["tasks"]}
			and invalid_gantt_result["risk_status"] == "Blocked"
			and "Plan Consistency Error" in invalid_gantt_result["exception_types"]
		),
		"invalid_plan_cannot_be_released": release_blocked,
		"final_state_is_valid": bool(
			final_recalculation["valid"]
			and frappe.db.get_value("APS Planning Run", run_name, "consistency_status") == "Valid"
			and not (final_result.blocking_reason or "").startswith("Plan consistency: ")
		),
	}
	manifest = {
		"site": frappe.local.site,
		"planning_run": run_name,
		"captured_on": str(now_datetime()),
		"passed": all(gates.values()),
		"acceptance_gates": gates,
		"canonical_fields": list(QUANTITY_FIELDS),
		"initial_totals": initial["totals"],
		"manual_adjustment": {
			"segment": manual_segment.name,
			"result": manual_result.name,
			"result_quantities": dict(manual_result_after),
			"effective_segment_sum": manual_effective_sum,
		},
		"late_risk": {
			"result": late_result.name,
			"segment": dict(late_segment),
			"exception": dict(late_exception or {}),
			"gantt_class": late_task["custom_class"],
		},
		"release_block": {
			"segment": invalid_segment.name,
			"errors": invalid_recalculation["errors"],
			"exception_exists": invalid_exception_exists,
			"blocked": release_blocked,
			"message": release_error,
		},
		"formula_errors": initial_formula_errors,
		"final_totals": final_recalculation["totals"],
	}
	_write_json(artifact_dir / "phase1-gate.json", manifest)
	frappe.db.commit()
	if not manifest["passed"]:
		frappe.throw("Phase 1 consistency gate failed. See phase1-gate.json for details.")
	return {
		"passed": True,
		"planning_run": run_name,
		"artifact": str(artifact_dir / "phase1-gate.json"),
		"acceptance_gates": gates,
	}


def finalize_phase1_gate(
	output_dir: str,
	screenshot_dir: str,
	application_test_result: str,
) -> dict:
	_assert_test_site()
	artifact_dir = Path(output_dir)
	screenshots = Path(screenshot_dir)
	core_manifest = _read_json(artifact_dir / "phase1-gate.json")
	screenshot_manifest = _read_json(screenshots / "manifest.json")
	application_tests = _read_json(Path(application_test_result))
	records = {row["file"]: row for row in screenshot_manifest.get("evidence") or []}
	screenshot_checks = {}
	for filename in PHASE1_SCREENSHOTS:
		path = screenshots / filename
		record = records.get(filename) or {}
		screenshot_checks[filename] = {
			"exists": path.is_file(),
			"bytes": path.stat().st_size if path.is_file() else 0,
			"dimensions": _png_dimensions(path) if path.is_file() else None,
			"expected_text_found": bool(record.get("expected_text_found")),
			"page_errors": record.get("page_errors") or [],
			"sha256_matches_manifest": bool(
				path.is_file() and record.get("sha256") == _sha256_file(path)
			),
		}
	gates = {
		"core_consistency_gate_passed": bool(core_manifest.get("passed")),
		"application_test_suite_passed": bool(
			application_tests.get("passed")
			and not (application_tests.get("final_attempt") or {}).get("errors")
			and not (application_tests.get("final_attempt") or {}).get("failures")
		),
		"screenshot_capture_passed": bool(screenshot_manifest.get("passed")),
		"all_screenshots_verified": all(
			row["exists"]
			and row["bytes"] >= 20_000
			and row["dimensions"]
			and row["dimensions"][0] >= 1_200
			and row["dimensions"][1] >= 700
			and row["expected_text_found"]
			and not row["page_errors"]
			and row["sha256_matches_manifest"]
			for row in screenshot_checks.values()
		),
	}
	manifest = {
		"site": frappe.local.site,
		"finalized_on": str(now_datetime()),
		"passed": all(gates.values()),
		"acceptance_gates": gates,
		"core_manifest": str(artifact_dir / "phase1-gate.json"),
		"application_tests": application_tests,
		"screenshots": screenshot_checks,
	}
	_write_json(artifact_dir / "phase1-final-manifest.json", manifest)
	if not manifest["passed"]:
		frappe.throw("Phase 1 final gate failed. See phase1-final-manifest.json for details.")
	return {"passed": True, "manifest": str(artifact_dir / "phase1-final-manifest.json")}


def _get_result_rows(run_name: str):
	return frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=["name", "item_code", "customer", "requested_date", "risk_status", *QUANTITY_FIELDS],
		order_by="name asc",
	)


def _get_formula_errors(run_name: str, result_rows) -> list[dict]:
	errors = []
	for row in result_rows:
		effective_sum = _get_effective_segment_sum(row.name)
		expected = consistency.calculate_quantity_fields(row.planned_qty, effective_sum)
		for fieldname in (
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"unscheduled_qty",
		):
			if abs(flt(row.get(fieldname)) - flt(expected[fieldname])) >= consistency.QTY_TOLERANCE:
				errors.append(
					{
						"result": row.name,
						"field": fieldname,
						"actual": flt(row.get(fieldname)),
						"expected": flt(expected[fieldname]),
					}
				)
	return errors


def _get_effective_segment_sum(result_name: str) -> float:
	rows = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": result_name, "parenttype": "APS Schedule Result"},
		fields=[
			"name",
			"workstation",
			"start_time",
			"end_time",
			"planned_qty",
			"segment_kind",
			"segment_status",
		],
	)
	return sum(flt(row.planned_qty) for row in rows if consistency.is_effective_primary_segment(row))


def _summaries_match(*summaries) -> bool:
	if not summaries:
		return True
	reference = summaries[0]
	return all(
		all(abs(flt(summary.get(fieldname)) - flt(reference.get(fieldname))) < consistency.QTY_TOLERANCE for fieldname in QUANTITY_FIELDS)
		for summary in summaries[1:]
	)


def _assert_test_site():
	if frappe.local.site != TEST_SITE:
		frappe.throw(f"Phase 1 gate is restricted to {TEST_SITE}; current site is {frappe.local.site}.")
