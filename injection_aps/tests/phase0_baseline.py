from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import frappe
import injection_aps
from frappe.utils import add_days, flt, get_datetime, getdate, now_datetime, today

from injection_aps.api import app
from injection_aps.services import planning


TEST_SITE = "aps-opt-test.localhost"
COMPANY = "APS Phase 0 Test Company"
PLANT_FLOOR = "APS-P0-FLOOR"
WORKSTATION = "APS-P0-MACHINE-01"
CUSTOMER_A = "APS-P0-CUSTOMER-A"
CUSTOMER_B = "APS-P0-CUSTOMER-B"
ITEM_GROUP = "Plastic Part"

ITEMS = {
	"stock": "APS-P0-STOCK",
	"flow": "APS-P0-FLOW",
	"cancel": "APS-P0-CANCEL",
	"increase": "APS-P0-INCREASE",
	"decrease": "APS-P0-DECREASE",
	"shared": "APS-P0-SHARED",
	"split": "APS-P0-SPLIT",
	"duplicate": "APS-P0-DUPLICATE",
	"zero": "APS-P0-ZERO",
	"append": "APS-P0-APPEND",
}

APS_TRANSACTION_DOCTYPES_CHILD_FIRST = (
	"APS Segment Adjustment",
	"APS Released WOS Item",
	"APS Work Order Proposal Item",
	"APS Shift Schedule Proposal Item",
	"APS Schedule Segment",
	"APS Planning Run Plant Floor",
	"Customer Delivery Schedule Item",
	"APS Demand Delta",
	"APS Exception Log",
	"APS Release Batch",
	"APS Shift Schedule Proposal Batch",
	"APS Work Order Proposal Batch",
	"APS Schedule Result",
	"APS Planning Run",
	"APS Net Requirement",
	"APS Demand Pool",
	"APS Change Request",
	"Customer Delivery Schedule",
	"APS Schedule Import Batch",
)

APS_CONFIG_DOCTYPES = (
	"APS Settings",
	"APS Machine Capability",
	"APS Mould-Machine Rule",
	"APS Color Transition Rule",
	"APS Freeze Rule",
)

PHASE0_SCREENSHOTS = (
	"01-schedule-console.png",
	"02-customer-progress.png",
	"03-net-requirement.png",
	"04-run-console.png",
	"05-gantt.png",
	"06-release-center.png",
)


def run_phase0_gate(output_dir: str | None = None) -> dict:
	_assert_test_site()
	artifact_dir = Path(output_dir or frappe.get_site_path("private", "files", "aps_phase0_baseline"))
	artifact_dir.mkdir(parents=True, exist_ok=True)

	runs = []
	for run_number in (1, 2):
		seed_summary = seed_phase0_scenarios()
		capture = capture_phase0_baseline()
		capture["seed_summary"] = seed_summary
		_write_json(artifact_dir / f"run-{run_number}.json", capture)
		runs.append(capture)

	signature_one = _build_repeatability_signature(runs[0])
	signature_two = _build_repeatability_signature(runs[1])
	repeatable = signature_one == signature_two
	manifest = {
		"site": frappe.local.site,
		"source_revision": "3a1fd63126cbd5a6f79a00eb212bf501b40c29c9",
		"source_module": str(Path(injection_aps.__file__).resolve()),
		"captured_on": str(now_datetime()),
		"repeatability": {
			"passed": repeatable,
			"run_1_sha256": _digest(signature_one),
			"run_2_sha256": _digest(signature_two),
		},
		"acceptance_gates": {
			"isolated_site": frappe.local.site == TEST_SITE,
			"two_runs_identical": repeatable,
			"api_capture_present": all(bool(run.get("api")) for run in runs),
			"database_capture_present": all(bool(run.get("database")) for run in runs),
			"known_defects_captured": all(len(run.get("known_defects") or {}) == 7 for run in runs),
		},
	}
	_write_json(artifact_dir / "repeatability-signature-1.json", signature_one)
	_write_json(artifact_dir / "repeatability-signature-2.json", signature_two)
	_write_json(artifact_dir / "manifest.json", manifest)
	capture_aps_configuration(str(artifact_dir / "test-site-aps-configuration.json"))
	frappe.db.commit()

	if not all(manifest["acceptance_gates"].values()):
		frappe.throw("Phase 0 repeatability gate failed. See the baseline manifest for details.")
	return {
		"passed": True,
		"artifact_dir": str(artifact_dir),
		"signature_sha256": manifest["repeatability"]["run_1_sha256"],
		"known_defects": sorted(runs[1]["known_defects"]),
	}


def finalize_phase0_gate(
	output_dir: str,
	screenshot_dir: str,
	source_config_snapshot: str,
	source_database_backup: str,
	source_config_backup: str,
	test_database_backup: str,
	test_config_backup: str,
	application_test_result: str,
) -> dict:
	"""Verify all archived Phase 0 evidence before declaring the phase complete."""
	_assert_test_site()
	artifact_dir = Path(output_dir)
	screenshots = Path(screenshot_dir)
	core_manifest = _read_json(artifact_dir / "manifest.json")
	screenshot_manifest = _read_json(screenshots / "manifest.json")
	application_tests = _read_json(Path(application_test_result))

	screenshot_records = {row["file"]: row for row in screenshot_manifest.get("evidence") or []}
	screenshot_checks = {}
	for filename in PHASE0_SCREENSHOTS:
		path = screenshots / filename
		record = screenshot_records.get(filename) or {}
		dimensions = _png_dimensions(path) if path.is_file() else None
		screenshot_checks[filename] = {
			"exists": path.is_file(),
			"bytes": path.stat().st_size if path.is_file() else 0,
			"dimensions": dimensions,
			"expected_text_found": bool(record.get("expected_text_found")),
			"page_errors": record.get("page_errors") or [],
			"sha256_matches_manifest": bool(
				path.is_file() and record.get("sha256") == _sha256_file(path)
			),
		}

	evidence_paths = {
		"source_aps_configuration": Path(source_config_snapshot),
		"test_aps_configuration": artifact_dir / "test-site-aps-configuration.json",
		"source_database_backup": Path(source_database_backup),
		"source_site_config_backup": Path(source_config_backup),
		"test_database_backup": Path(test_database_backup),
		"test_site_config_backup": Path(test_config_backup),
		"application_test_result": Path(application_test_result),
		"run_1": artifact_dir / "run-1.json",
		"run_2": artifact_dir / "run-2.json",
	}
	evidence_files = {
		key: {
			"path": str(path),
			"exists": path.is_file(),
			"bytes": path.stat().st_size if path.is_file() else 0,
			"sha256": _sha256_file(path) if path.is_file() else None,
		}
		for key, path in evidence_paths.items()
	}

	backup_checks = {
		"source_database_gzip_valid": _gzip_is_readable(Path(source_database_backup)),
		"test_database_gzip_valid": _gzip_is_readable(Path(test_database_backup)),
		"source_config_json_valid": _json_is_readable(Path(source_config_backup)),
		"test_config_json_valid": _json_is_readable(Path(test_config_backup)),
	}
	gates = {
		"isolated_test_site": frappe.local.site == TEST_SITE,
		"core_baseline_passed": all((core_manifest.get("acceptance_gates") or {}).values()),
		"repeatability_passed": bool((core_manifest.get("repeatability") or {}).get("passed")),
		"all_evidence_files_present": all(row["exists"] and row["bytes"] > 0 for row in evidence_files.values()),
		"all_backups_readable": all(backup_checks.values()),
		"application_test_suite_passed": bool(
			application_tests.get("passed")
			and application_tests.get("tests_run") == 27
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
		"source_revision": core_manifest.get("source_revision"),
		"finalized_on": str(now_datetime()),
		"passed": all(gates.values()),
		"acceptance_gates": gates,
		"backup_checks": backup_checks,
		"application_tests": application_tests,
		"evidence_files": evidence_files,
		"screenshots": screenshot_checks,
	}
	_write_json(artifact_dir / "phase0-final-manifest.json", manifest)
	if not manifest["passed"]:
		frappe.throw("Phase 0 final gate failed. See phase0-final-manifest.json for details.")
	return {
		"passed": True,
		"manifest": str(artifact_dir / "phase0-final-manifest.json"),
		"signature_sha256": (core_manifest.get("repeatability") or {}).get("run_1_sha256"),
	}


def seed_phase0_scenarios() -> dict:
	_assert_test_site()
	_reset_phase0_transactions()
	masters = _ensure_master_data()
	base_date = getdate(today())

	schedules = {
		"make_to_stock": _create_schedule(
			customer=CUSTOMER_A,
			scope="APS-P0-MAKE-TO-STOCK",
			item_code=ITEMS["stock"],
			schedule_date=add_days(base_date, 5),
			qty=100,
			remark="scenario:make-to-stock",
		),
		"produce_and_deliver": _create_schedule(
			customer=CUSTOMER_A,
			scope="APS-P0-PRODUCE-DELIVER",
			item_code=ITEMS["flow"],
			schedule_date=add_days(base_date, 4),
			qty=100,
			allocated_qty=60,
			produced_qty=60,
			delivered_qty=30,
			remark="scenario:produce-and-deliver",
		),
		"cancel_tomorrow": _create_schedule(
			customer=CUSTOMER_A,
			scope="APS-P0-CANCEL-TOMORROW",
			item_code=ITEMS["cancel"],
			schedule_date=add_days(base_date, 1),
			qty=50,
			remark="scenario:cancel-tomorrow",
		),
		"temporary_increase": _create_schedule(
			customer=CUSTOMER_A,
			scope="APS-P0-INCREASE",
			item_code=ITEMS["increase"],
			schedule_date=add_days(base_date, 3),
			qty=40,
			remark="scenario:temporary-increase",
		),
		"decrease_after_start": _create_schedule(
			customer=CUSTOMER_A,
			scope="APS-P0-DECREASE-AFTER-START",
			item_code=ITEMS["decrease"],
			schedule_date=add_days(base_date, 3),
			qty=100,
			allocated_qty=60,
			produced_qty=40,
			remark="scenario:decrease-after-start",
		),
		"shared_item_customer_a": _create_schedule(
			customer=CUSTOMER_A,
			scope="APS-P0-SHARED-ITEM",
			item_code=ITEMS["shared"],
			schedule_date=add_days(base_date, 2),
			qty=30,
			remark="scenario:shared-item-customer-a",
		),
		"shared_item_customer_b": _create_schedule(
			customer=CUSTOMER_B,
			scope="APS-P0-SHARED-ITEM",
			item_code=ITEMS["shared"],
			schedule_date=add_days(base_date, 3),
			qty=70,
			remark="scenario:shared-item-customer-b",
		),
		"split_work_order": _create_schedule(
			customer=CUSTOMER_B,
			scope="APS-P0-SPLIT-WORK-ORDER",
			item_code=ITEMS["split"],
			schedule_date=add_days(base_date, 4),
			qty=100,
			remark="scenario:split-work-order",
		),
		"overdue_normal": _create_schedule(
			customer=CUSTOMER_B,
			scope="APS-P0-OVERDUE-NORMAL",
			item_code=ITEMS["cancel"],
			schedule_date=add_days(base_date, -1),
			qty=50,
			remark="defect:overdue-shown-normal",
		),
	}

	for version in ("V1", "V2"):
		planning.import_customer_delivery_schedule(
			customer=CUSTOMER_B,
			company=COMPANY,
			version_no=version,
			schedule_scope="APS-P0-APPEND",
			import_strategy="Append",
			rows_json=[
				{
					"item_code": ITEMS["append"],
					"schedule_date": add_days(base_date, 2),
					"qty": 25,
					"remark": f"defect:append-accumulation:{version}",
				}
			],
		)

	demand_rebuild = planning.rebuild_demand_pool(company=COMPANY)
	net_rebuild = planning.rebuild_net_requirements(company=COMPANY, existing_work_order_policy="Exclude")
	run_name = _create_planning_evidence(base_date)
	change_request = frappe.get_doc(
		{
			"doctype": "APS Change Request",
			"planning_run": run_name,
			"company": COMPANY,
			"plant_floor": PLANT_FLOOR,
			"change_type": "Cancel",
			"item_code": ITEMS["cancel"],
			"customer": CUSTOMER_A,
			"required_date": add_days(base_date, 1),
			"qty": 50,
			"status": "Draft",
			"approval_state": "Pending",
			"notes": "defect:cancellation-routed-as-insert",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"base_date": str(base_date),
		"masters": masters,
		"scenario_schedules": {key: doc.name for key, doc in schedules.items()},
		"planning_run": run_name,
		"change_request": change_request.name,
		"demand_rebuild": demand_rebuild,
		"net_rebuild": net_rebuild,
	}


def capture_phase0_baseline() -> dict:
	_assert_test_site()
	frappe.set_user("Administrator")
	base_date = getdate(today())
	run_name = frappe.db.get_value("APS Planning Run", {"notes": "APS Phase 0 baseline run"}, "name")
	change_request = frappe.db.get_value(
		"APS Change Request", {"notes": "defect:cancellation-routed-as-insert"}, "name"
	)

	previews = {
		"cancel": planning.preview_customer_delivery_schedule(
			customer=CUSTOMER_A,
			company=COMPANY,
			version_no="CANCEL-V2",
			schedule_scope="APS-P0-CANCEL-TOMORROW",
			import_strategy="Replace Scope",
			rows_json=[],
		),
		"increase": planning.preview_customer_delivery_schedule(
			customer=CUSTOMER_A,
			company=COMPANY,
			version_no="INCREASE-V2",
			schedule_scope="APS-P0-INCREASE",
			import_strategy="Replace Scope",
			rows_json=[
				{
					"item_code": ITEMS["increase"],
					"schedule_date": add_days(base_date, 3),
					"qty": 70,
				}
			],
		),
		"decrease_after_start": planning.preview_customer_delivery_schedule(
			customer=CUSTOMER_A,
			company=COMPANY,
			version_no="DECREASE-V2",
			schedule_scope="APS-P0-DECREASE-AFTER-START",
			import_strategy="Replace Scope",
			rows_json=[
				{
					"item_code": ITEMS["decrease"],
					"schedule_date": add_days(base_date, 3),
					"qty": 70,
				}
			],
		),
	}

	duplicate_preview = planning.preview_customer_delivery_schedule(
		customer=CUSTOMER_A,
		company=COMPANY,
		version_no="DUP-V1",
		schedule_scope="APS-P0-DUPLICATE",
		import_strategy="Replace Scope",
		rows_json=[
			{"item_code": ITEMS["duplicate"], "schedule_date": add_days(base_date, 2), "qty": 40},
			{"item_code": ITEMS["duplicate"], "schedule_date": add_days(base_date, 2), "qty": 60},
		],
	)

	with patch(
		"injection_aps.services.planning._read_schedule_workbook_rows",
		return_value=(
			[["Item", str(add_days(base_date, 2))], [ITEMS["zero"], 0]],
			{"sheet_name": "Baseline", "sheet_names": ["Baseline"]},
		),
	):
		zero_rows, zero_context = planning._normalize_schedule_rows_from_matrix(
			file_url="/private/files/phase0-zero.xlsx",
			mapping={"parser_mode": "matrix", "item_reference_column": "A"},
		)

	insert_probe = {
		"scheduled_qty": 50,
		"unscheduled_qty": 0,
		"displaced_segments": [],
		"probe": "insert-order-analysis",
	}
	with patch("injection_aps.services.planning.analyze_insert_order_impact", return_value=insert_probe) as mocked:
		cancel_analysis = planning.analyze_change_request_impact(change_request)
		cancel_routed_to_insert = mocked.call_count == 1

	database = _capture_database_quantities()
	gantt = app.get_schedule_gantt_data(run_name)
	api = {
		"workspace": app.get_workspace_dashboard_data(),
		"schedule_console": app.get_schedule_console_data(company=COMPANY),
		"net_requirements": app.get_net_requirement_page_data(company=COMPANY, limit=1000),
		"customer_progress": app.get_customer_schedule_progress_data(company=COMPANY, run_name=run_name, limit=1000),
		"run_console": app.get_run_console_data(company=COMPANY),
		"gantt": gantt,
		"release_center": app.get_release_center_data(run_name=run_name),
	}

	overdue_tasks = [
		row
		for row in gantt.get("tasks") or []
		if row.get("item_code") == ITEMS["cancel"] and getdate(row.get("requested_date")) < base_date
	]
	known_defects = {
		"fresh_install_missing_gmc_role": {
			"observed": True,
			"evidence": "Initial after_install failed before ensure_roles_and_permissions because Workspace referenced GMC.",
		},
		"planning_header_segment_quantity_mismatch": database["quantity_reconciliation"],
		"overdue_gantt_shown_normal": {
			"observed": any((row.get("custom_class") or "").endswith("normal") for row in overdue_tasks),
			"matching_tasks": overdue_tasks,
		},
		"duplicate_same_date_keeps_last_row": {
			"observed": duplicate_preview.get("row_count") == 1,
			"input_qty": [40, 60],
			"output_rows": duplicate_preview.get("rows"),
		},
		"matrix_zero_quantity_skipped_by_default": {
			"observed": len(zero_rows) == 0,
			"output_rows": zero_rows,
			"parse_context": zero_context,
		},
		"append_accumulates_demand": {
			"observed": database["scenario_totals"]["append_demand_qty"] == 50,
			"two_input_rows_qty": [25, 25],
			"demand_qty": database["scenario_totals"]["append_demand_qty"],
		},
		"cancel_analyzed_as_insert": {
			"observed": cancel_routed_to_insert and cancel_analysis.get("probe") == "insert-order-analysis",
			"analysis": cancel_analysis,
		},
	}

	return _jsonable(
		{
			"metadata": {
				"site": frappe.local.site,
				"base_date": base_date,
				"captured_on": now_datetime(),
				"source_module": str(Path(injection_aps.__file__).resolve()),
			},
			"scenario_previews": previews,
			"known_defects": known_defects,
			"database": database,
			"api": api,
		}
	)


def capture_aps_configuration(output_path: str | None = None) -> dict:
	payload = {
		"site": frappe.local.site,
		"captured_on": str(now_datetime()),
		"source_module": str(Path(injection_aps.__file__).resolve()),
		"configuration": {},
	}
	for doctype in APS_CONFIG_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		if frappe.get_meta(doctype).issingle:
			payload["configuration"][doctype] = _clean_document(frappe.get_single(doctype).as_dict())
		else:
			payload["configuration"][doctype] = [
				_clean_document(row) for row in frappe.get_all(doctype, fields=["*"], order_by="name asc")
			]
	if output_path:
		_write_json(Path(output_path), payload)
	return payload


def _assert_test_site():
	if frappe.local.site != TEST_SITE:
		frappe.throw(
			f"Phase 0 scenario mutation is restricted to {TEST_SITE}; current site is {frappe.local.site}."
		)


def _reset_phase0_transactions():
	for doctype in APS_TRANSACTION_DOCTYPES_CHILD_FIRST:
		if frappe.db.exists("DocType", doctype):
			frappe.db.delete(doctype)
	frappe.db.delete("Bin", {"item_code": ("in", list(ITEMS.values()))})
	frappe.db.delete("Work Order", {"name": ("like", "APS-P0-WO-%")})
	frappe.db.commit()


def _ensure_master_data() -> dict:
	if not frappe.db.exists("Company", COMPANY):
		frappe.throw(f"Missing baseline Company {COMPANY}; initialize ERPNext fixtures before running Phase 0.")

	if not frappe.db.exists("Item Group", ITEM_GROUP):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": ITEM_GROUP,
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)

	stock_uom = frappe.db.get_value("UOM", "Nos", "name") or frappe.db.get_value("UOM", {}, "name")
	for item_code in ITEMS.values():
		if frappe.db.exists("Item", item_code):
			continue
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"description": "APS Phase 0 deterministic baseline item",
				"item_group": ITEM_GROUP,
				"stock_uom": stock_uom,
				"is_stock_item": 1,
				"include_item_in_manufacturing": 1,
			}
		).insert(ignore_permissions=True)

	customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	if not customer_group or not territory:
		frappe.throw("ERPNext baseline fixtures must include a leaf Customer Group and Territory.")
	for customer_name in (CUSTOMER_A, CUSTOMER_B):
		if frappe.db.exists("Customer", customer_name):
			continue
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": customer_name,
				"customer_type": "Company",
				"customer_group": customer_group,
				"territory": territory,
			}
		).insert(ignore_permissions=True)

	warehouse = frappe.db.get_value(
		"Warehouse", {"company": COMPANY, "is_group": 0, "warehouse_name": "Stores"}, "name"
	) or frappe.db.get_value("Warehouse", {"company": COMPANY, "is_group": 0}, "name")
	if not warehouse:
		frappe.throw(f"No leaf Warehouse exists for baseline Company {COMPANY}.")

	if not frappe.db.exists("Plant Floor", PLANT_FLOOR):
		frappe.get_doc(
			{
				"doctype": "Plant Floor",
				"floor_name": PLANT_FLOOR,
				"company": COMPANY,
				"warehouse": warehouse,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Workstation", WORKSTATION):
		frappe.get_doc(
			{
				"doctype": "Workstation",
				"workstation_name": WORKSTATION,
				"plant_floor": PLANT_FLOOR,
				"warehouse": warehouse,
				"production_capacity": 1,
				"status": "Idle",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("APS Machine Capability", WORKSTATION):
		frappe.get_doc(
			{
				"doctype": "APS Machine Capability",
				"workstation": WORKSTATION,
				"plant_floor": PLANT_FLOOR,
				"hourly_capacity_qty": 100,
				"daily_capacity_qty": 800,
				"queue_sequence": 1,
				"machine_status": "Available",
				"is_active": 1,
				"sync_source": "Phase 0 baseline",
			}
		).insert(ignore_permissions=True)

	settings = frappe.get_single("APS Settings")
	settings.default_company = COMPANY
	settings.default_plant_floor = PLANT_FLOOR
	settings.planning_horizon_days = 14
	settings.default_hourly_capacity_qty = 100
	settings.save(ignore_permissions=True)

	frappe.get_doc(
		{
			"doctype": "Bin",
			"item_code": ITEMS["stock"],
			"warehouse": warehouse,
			"actual_qty": 40,
			"projected_qty": 40,
		}
	).insert(ignore_permissions=True)
	return {
		"company": COMPANY,
		"customers": [CUSTOMER_A, CUSTOMER_B],
		"items": list(ITEMS.values()),
		"plant_floor": PLANT_FLOOR,
		"workstation": WORKSTATION,
		"warehouse": warehouse,
	}


def _create_schedule(
	*,
	customer: str,
	scope: str,
	item_code: str,
	schedule_date,
	qty: float,
	allocated_qty: float = 0,
	produced_qty: float = 0,
	delivered_qty: float = 0,
	remark: str = "",
):
	return frappe.get_doc(
		{
			"doctype": "Customer Delivery Schedule",
			"customer": customer,
			"company": COMPANY,
			"schedule_scope": scope,
			"version_no": "BASELINE-V1",
			"import_strategy": "Replace Scope",
			"source_type": "Customer Delivery Schedule",
			"status": "Active",
			"items": [
				{
					"item_code": item_code,
					"schedule_date": schedule_date,
					"qty": qty,
					"allocated_qty": allocated_qty,
					"produced_qty": produced_qty,
					"delivered_qty": delivered_qty,
					"balance_qty": max(flt(qty) - flt(delivered_qty), 0),
					"status": "Open" if flt(qty) > flt(delivered_qty) else "Covered",
					"source_origin": "manual_added",
					"remark": remark,
				}
			],
		}
	).insert(ignore_permissions=True)


def _create_planning_evidence(base_date) -> str:
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": COMPANY,
			"plant_floor": PLANT_FLOOR,
			"planning_date": base_date,
			"horizon_days": 14,
			"run_type": "Trial",
			"existing_work_order_policy": "Exclude",
			"status": "Planned",
			"approval_state": "Approved",
			"notes": "APS Phase 0 baseline run",
		}
	).insert(ignore_permissions=True)

	_create_raw_work_order("APS-P0-WO-FLOW", ITEMS["flow"], qty=100, produced_qty=60)
	_create_raw_work_order("APS-P0-WO-SPLIT", ITEMS["split"], qty=100, produced_qty=20)

	_create_result(
		run.name,
		item_code=ITEMS["flow"],
		customer=CUSTOMER_A,
		requested_date=add_days(base_date, 4),
		planned_qty=70,
		segments=[
			{"qty": 30, "start_days": 0, "end_days": 1, "work_order": "APS-P0-WO-FLOW", "actual_qty": 30, "actual_status": "Completed"},
			{"qty": 40, "start_days": 1, "end_days": 2, "work_order": "APS-P0-WO-FLOW", "actual_qty": 20, "actual_status": "Running"},
		],
	)
	_create_result(
		run.name,
		item_code=ITEMS["split"],
		customer=CUSTOMER_B,
		requested_date=add_days(base_date, 4),
		planned_qty=100,
		segments=[
			{"qty": 40, "start_days": 1, "end_days": 2, "work_order": "APS-P0-WO-SPLIT"},
			{"qty": 60, "start_days": 2, "end_days": 3, "work_order": "APS-P0-WO-SPLIT"},
		],
	)
	mismatch_result = _create_result(
		run.name,
		item_code=ITEMS["increase"],
		customer=CUSTOMER_A,
		requested_date=add_days(base_date, 3),
		planned_qty=100,
		segments=[
			{"qty": 30, "start_days": 0, "end_days": 1},
			{"qty": 50, "start_days": 1, "end_days": 2},
		],
	)
	frappe.db.set_value("APS Schedule Result", mismatch_result, "scheduled_qty", 100, update_modified=False)
	_create_result(
		run.name,
		item_code=ITEMS["cancel"],
		customer=CUSTOMER_B,
		requested_date=add_days(base_date, -1),
		planned_qty=50,
		segments=[{"qty": 50, "start_days": 0, "end_days": 1}],
		risk_status="Normal",
	)

	result_totals = frappe.db.sql(
		"""
		select sum(planned_qty), sum(scheduled_qty), sum(unscheduled_qty), count(*)
		from `tabAPS Schedule Result`
		where planning_run = %s
		""",
		(run.name,),
	)[0]
	frappe.db.set_value(
		"APS Planning Run",
		run.name,
		{
			"total_net_requirement_qty": flt(result_totals[0]),
			"total_scheduled_qty": flt(result_totals[1]),
			"total_unscheduled_qty": flt(result_totals[2]),
			"result_count": int(result_totals[3]),
		},
		update_modified=False,
	)
	return run.name


def _create_result(
	run_name: str,
	*,
	item_code: str,
	customer: str,
	requested_date,
	planned_qty: float,
	segments: list[dict],
	risk_status: str = "Normal",
) -> str:
	base_datetime = get_datetime(f"{today()} 08:00:00")
	doc = frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": run_name,
			"company": COMPANY,
			"plant_floor": PLANT_FLOOR,
			"customer": customer,
			"item_code": item_code,
			"requested_date": requested_date,
			"demand_source": "Customer Delivery Schedule",
			"planned_qty": planned_qty,
			"status": "Planned",
			"risk_status": risk_status,
			"actual_status": "Running" if any(row.get("actual_qty") for row in segments) else "Not Started",
			"segments": [
				{
					"workstation": WORKSTATION,
					"plant_floor": PLANT_FLOOR,
					"start_time": add_days(base_datetime, row["start_days"]),
					"end_time": add_days(base_datetime, row["end_days"]),
					"planned_qty": row["qty"],
					"sequence_no": idx,
					"lane_key": WORKSTATION,
					"segment_kind": "Primary",
					"primary_item_code": item_code,
					"segment_status": "Planned",
					"linked_work_order": row.get("work_order"),
					"actual_completed_qty": row.get("actual_qty") or 0,
					"actual_status": row.get("actual_status") or "Not Started",
				}
				for idx, row in enumerate(segments, start=1)
			],
		}
	)
	doc.insert(ignore_permissions=True, ignore_links=True)
	return doc.name


def _create_raw_work_order(name: str, item_code: str, qty: float, produced_qty: float):
	doc = frappe.new_doc("Work Order")
	doc.name = name
	doc.company = COMPANY
	doc.production_item = item_code
	doc.stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
	doc.qty = qty
	doc.produced_qty = produced_qty
	doc.status = "In Process" if produced_qty else "Not Started"
	doc.docstatus = 1
	doc.planned_start_date = now_datetime()
	doc.db_insert()


def _capture_database_quantities() -> dict:
	run_name = frappe.db.get_value("APS Planning Run", {"notes": "APS Phase 0 baseline run"}, "name")
	run = frappe.db.get_value(
		"APS Planning Run",
		run_name,
		[
			"total_net_requirement_qty",
			"total_scheduled_qty",
			"total_unscheduled_qty",
			"result_count",
		],
		as_dict=True,
	)
	result_rows = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=["name", "item_code", "planned_qty", "scheduled_qty", "unscheduled_qty", "risk_status"],
		order_by="item_code asc",
	)
	segment_rows = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": ("in", [row.name for row in result_rows])},
		fields=["name", "parent", "planned_qty", "linked_work_order", "actual_completed_qty", "actual_status"],
		order_by="parent asc, idx asc",
	)
	result_scheduled_qty = sum(flt(row.scheduled_qty) for row in result_rows)
	segment_planned_qty = sum(flt(row.planned_qty) for row in segment_rows)
	result_planned_qty = sum(flt(row.planned_qty) for row in result_rows)
	append_demand_qty = frappe.db.sql(
		"""
		select sum(d.qty)
		from `tabAPS Demand Pool` d
		inner join `tabCustomer Delivery Schedule` s on s.name = d.source_name
		where s.schedule_scope = 'APS-P0-APPEND'
		"""
	)[0][0]
	return {
		"totals": {
			"active_schedule_qty": flt(
				frappe.db.sql(
					"""
					select sum(i.qty)
					from `tabCustomer Delivery Schedule Item` i
					inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
					where s.status = 'Active' and s.company = %s
					""",
					(COMPANY,),
				)[0][0]
			),
			"demand_pool_qty": flt(frappe.db.sql("select sum(qty) from `tabAPS Demand Pool` where company = %s", (COMPANY,))[0][0]),
			"net_requirement_qty": flt(
				frappe.db.sql("select sum(net_requirement_qty) from `tabAPS Net Requirement` where company = %s", (COMPANY,))[0][0]
			),
			"result_planned_qty": result_planned_qty,
			"result_scheduled_qty": result_scheduled_qty,
			"segment_planned_qty": segment_planned_qty,
			"overproduction_qty": max(result_scheduled_qty - result_planned_qty, 0),
			"shortage_qty": sum(flt(row.unscheduled_qty) for row in result_rows),
			"actual_segment_qty": sum(flt(row.actual_completed_qty) for row in segment_rows),
		},
		"run_header": run,
		"results": result_rows,
		"segments": segment_rows,
		"scenario_totals": {"append_demand_qty": flt(append_demand_qty)},
		"quantity_reconciliation": {
			"observed": flt(run.total_scheduled_qty) != segment_planned_qty,
			"run_header_scheduled_qty": flt(run.total_scheduled_qty),
			"result_header_scheduled_qty": result_scheduled_qty,
			"segment_planned_qty": segment_planned_qty,
			"difference_run_to_segments": flt(run.total_scheduled_qty) - segment_planned_qty,
		},
	}


def _build_repeatability_signature(capture: dict) -> dict:
	known_defects = capture["known_defects"]
	api = capture["api"]
	gantt = api["gantt"]
	return {
		"known_defects": {key: bool(value.get("observed")) for key, value in sorted(known_defects.items())},
		"database_totals": capture["database"]["totals"],
		"quantity_reconciliation": capture["database"]["quantity_reconciliation"],
		"scenario_change_summaries": {
			key: value.get("summary") for key, value in sorted(capture["scenario_previews"].items())
		},
		"page_summaries": {
			"workspace": api["workspace"],
			"schedule_console": api["schedule_console"].get("summary"),
			"net_requirements": api["net_requirements"].get("summary"),
			"customer_progress": api["customer_progress"].get("summary"),
			"run_count": len(api["run_console"].get("runs") or []),
			"gantt_task_count": len(gantt.get("tasks") or []),
			"gantt_segment_qty": sum(flt(row.get("planned_qty")) for row in gantt.get("rows") or []),
			"release_batch_count": len(api["release_center"].get("release_batches") or []),
		},
	}


def _clean_document(row) -> dict:
	data = dict(row or {})
	for fieldname in ("creation", "modified", "owner", "modified_by", "_user_tags", "_comments", "_assign", "_liked_by"):
		data.pop(fieldname, None)
	return _jsonable(data)


def _jsonable(value):
	return json.loads(frappe.as_json(value))


def _digest(value) -> str:
	encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
	return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict:
	return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _gzip_is_readable(path: Path) -> bool:
	if not path.is_file() or path.stat().st_size == 0:
		return False
	try:
		with gzip.open(path, "rb") as handle:
			for _chunk in iter(lambda: handle.read(1024 * 1024), b""):
				pass
		return True
	except (OSError, EOFError):
		return False


def _json_is_readable(path: Path) -> bool:
	try:
		_read_json(path)
		return True
	except (OSError, json.JSONDecodeError):
		return False


def _png_dimensions(path: Path) -> tuple[int, int] | None:
	try:
		with path.open("rb") as handle:
			header = handle.read(24)
		if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
			return None
		return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
	except OSError:
		return None


def _write_json(path: Path, payload):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
