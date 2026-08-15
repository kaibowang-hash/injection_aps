from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import frappe
from frappe.utils import add_days, date_diff, flt, getdate, now_datetime, today

from injection_aps.tests.v2_phase0_gate import assert_isolated_environment
from injection_aps.tests.v2_scenario_catalog import FIXTURE_PREFIX, SCENARIOS, get_scenario_manifest


FIXTURE_SITE = "aps-opt-fixture.localhost"
FIXTURE_CUSTOMER = f"{FIXTURE_PREFIX}CUSTOMER"
FIXTURE_PLANT_FLOOR = f"{FIXTURE_PREFIX}FLOOR"
FIXTURE_WORKSTATION = f"{FIXTURE_PREFIX}MACHINE-120T"
RUN_NOTE_PREFIX = f"{FIXTURE_PREFIX}SCENARIO|"


def run_phase0_fixture_gate(output_dir: str | None = None) -> dict:
	"""Create the 11 input fixtures twice and archive a deterministic comparison."""
	_assert_fixture_site()
	artifact_dir = Path(output_dir or frappe.get_site_path("private", "files", "aps_v2_phase0_fixtures"))
	artifact_dir.mkdir(parents=True, exist_ok=True)
	base_date = getdate(today())
	runs = []
	for run_number in (1, 2):
		cleanup_phase0_scenario_fixtures()
		build_summary = build_phase0_scenario_fixtures(base_date=base_date)
		capture = capture_phase0_scenario_fixtures(base_date=base_date)
		capture["build_summary"] = build_summary
		_write_json(artifact_dir / f"run-{run_number}.json", capture)
		runs.append(capture)

	signature_one = _repeatability_signature(runs[0])
	signature_two = _repeatability_signature(runs[1])
	digest_one = _digest(signature_one)
	digest_two = _digest(signature_two)
	scenario_names = set(SCENARIOS)
	materialized_names = set(runs[1]["scenario_schedule_counts"])
	manifest = {
		"site": frappe.local.site,
		"source_revision": "APS-V2-PHASE0-INPUT-FIXTURE-V1",
		"captured_on": str(now_datetime()),
		"passed": bool(
			digest_one == digest_two
			and len(scenario_names) == 11
			and scenario_names == materialized_names
			and all(runs[1]["scenario_schedule_counts"].values())
		),
		"acceptance_gates": {
			"isolated_fixture_site": frappe.local.site == FIXTURE_SITE,
			"catalog_has_11_scenarios": len(scenario_names) == 11,
			"all_scenarios_materialized": scenario_names == materialized_names,
			"each_scenario_has_schedule_input": all(runs[1]["scenario_schedule_counts"].values()),
			"two_runs_identical": digest_one == digest_two,
			"all_document_names_are_prefixed": bool(runs[1]["all_document_names_are_prefixed"]),
		},
		"repeatability": {"run_1_sha256": digest_one, "run_2_sha256": digest_two},
	}
	manifest["passed"] = all(manifest["acceptance_gates"].values())
	_write_json(artifact_dir / "repeatability-signature-1.json", signature_one)
	_write_json(artifact_dir / "repeatability-signature-2.json", signature_two)
	_write_json(artifact_dir / "manifest.json", manifest)
	frappe.db.commit()
	if not manifest["passed"]:
		frappe.throw("APS V2 Phase 0 fixture gate failed. See the fixture manifest for details.")
	return {
		"passed": True,
		"artifact_dir": str(artifact_dir),
		"scenario_count": len(scenario_names),
		"signature_sha256": digest_one,
	}


def build_phase0_scenario_fixtures(base_date=None) -> dict:
	"""Materialize only Phase 0 inputs; later phases remain responsible for V2 outputs."""
	company = _assert_fixture_site()
	base_date = getdate(base_date or today())
	master = _ensure_fixture_masters(company)
	created_schedules = []
	created_runs = []
	created_rules = []

	for scenario_name, spec in SCENARIOS.items():
		groups = defaultdict(list)
		for demand in spec.get("demands") or ():
			groups[(demand.get("revision") or "INPUT-V1", demand.get("status") or "Active")].append(demand)
		for (revision, status), demands in sorted(groups.items()):
			created_schedules.append(
				_create_schedule(
					company=company,
					scenario_name=scenario_name,
					revision=revision,
					status=status,
					demands=demands,
					base_date=base_date,
				)
			)

		for run_spec in spec.get("formal_runs") or ():
			created_runs.append(
				_create_formal_run(company, scenario_name, run_spec, spec, base_date)
			)

		for demand in spec.get("demands") or ():
			if demand.get("mould"):
				created_rules.append(_create_mould_rule(scenario_name, demand))

	_ensure_zero_material_bin(master["warehouse"])
	frappe.db.commit()
	return {
		"base_date": str(base_date),
		"company": company,
		"customer": FIXTURE_CUSTOMER,
		"schedule_count": len(created_schedules),
		"formal_run_count": len(created_runs),
		"mould_rule_count": len(created_rules),
		"scenario_count": len(SCENARIOS),
		"catalog": get_scenario_manifest(),
	}


def cleanup_phase0_scenario_fixtures(remove_masters: bool = False) -> dict:
	"""Delete fixture-prefixed records only; production-like names are never accepted."""
	_assert_fixture_site()
	run_names = frappe.get_all(
		"APS Planning Run",
		filters={"notes": ("like", f"{RUN_NOTE_PREFIX}%")},
		pluck="name",
		limit_page_length=0,
	)
	result_names = (
		frappe.get_all(
			"APS Schedule Result",
			filters={"planning_run": ("in", run_names)},
			pluck="name",
			limit_page_length=0,
		)
		if run_names
		else []
	)
	schedule_names = frappe.get_all(
		"Customer Delivery Schedule",
		filters={"schedule_scope": ("like", f"{FIXTURE_PREFIX}%")},
		pluck="name",
		limit_page_length=0,
	)

	_delete_names("APS Schedule Segment", "parent", result_names)
	_delete_names("APS Planning Run Plant Floor", "parent", run_names)
	_delete_names("APS Schedule Result", "name", result_names)
	_delete_names("APS Planning Run", "name", run_names)
	_delete_names("Customer Delivery Schedule Item", "parent", schedule_names)
	_delete_names("Customer Delivery Schedule", "name", schedule_names)
	frappe.db.delete("APS Mould-Machine Rule", {"notes": ("like", f"{RUN_NOTE_PREFIX}%")})

	if remove_masters:
		item_codes = _fixture_item_codes()
		frappe.db.delete("Bin", {"item_code": ("in", item_codes)})
		frappe.db.delete("APS Machine Capability", {"name": FIXTURE_WORKSTATION})
		frappe.db.delete("Workstation", {"name": FIXTURE_WORKSTATION})
		frappe.db.delete("Plant Floor", {"name": FIXTURE_PLANT_FLOOR})
		frappe.db.delete("Item", {"name": ("in", item_codes)})
		frappe.db.delete("Customer", {"name": FIXTURE_CUSTOMER})

	frappe.db.commit()
	return {
		"removed_runs": len(run_names),
		"removed_results": len(result_names),
		"removed_schedules": len(schedule_names),
		"removed_masters": bool(remove_masters),
	}


def capture_phase0_scenario_fixtures(base_date=None) -> dict:
	company = _assert_fixture_site()
	base_date = getdate(base_date or today())
	schedules = frappe.get_all(
		"Customer Delivery Schedule",
		filters={"schedule_scope": ("like", f"{FIXTURE_PREFIX}%")},
		fields=["name", "schedule_scope", "version_no", "status", "customer", "company"],
		order_by="schedule_scope asc, version_no asc",
		limit_page_length=0,
	)
	schedule_rows = []
	scenario_schedule_counts = defaultdict(int)
	document_names = [FIXTURE_CUSTOMER, FIXTURE_PLANT_FLOOR, FIXTURE_WORKSTATION]
	for schedule in schedules:
		scenario_name = schedule.schedule_scope.removeprefix(FIXTURE_PREFIX)
		scenario_schedule_counts[scenario_name] += 1
		document_names.append(schedule.schedule_scope)
		items = frappe.get_all(
			"Customer Delivery Schedule Item",
			filters={"parent": schedule.name},
			fields=["item_code", "schedule_date", "qty", "sales_order", "remark"],
			order_by="schedule_date asc, item_code asc",
		)
		schedule_rows.append(
			{
				"scenario": scenario_name,
				"revision": schedule.version_no,
				"status": schedule.status,
				"customer": schedule.customer,
				"company": schedule.company,
				"items": [
					{
						"item_code": row.item_code,
						"day": date_diff(row.schedule_date, base_date),
						"qty": flt(row.qty),
						"sales_order": row.sales_order or None,
						"remark": row.remark,
					}
					for row in items
				],
			}
		)

	runs = frappe.get_all(
		"APS Planning Run",
		filters={"notes": ("like", f"{RUN_NOTE_PREFIX}%")},
		fields=["name", "planning_date", "run_type", "status", "approval_state", "notes", "total_net_requirement_qty"],
		order_by="notes asc",
		limit_page_length=0,
	)
	run_rows = []
	for run in runs:
		parts = run.notes.split("|")
		document_names.append(run.notes)
		results = frappe.get_all(
			"APS Schedule Result",
			filters={"planning_run": run.name},
			fields=["item_code", "requested_date", "planned_qty", "machine_scheduled_qty", "status", "actual_status", "delay_minutes", "is_locked"],
			order_by="item_code asc",
		)
		run_rows.append(
			{
				"scenario": parts[1],
				"key": parts[2],
				"day": date_diff(run.planning_date, base_date),
				"run_type": run.run_type,
				"status": run.status,
				"approval_state": run.approval_state,
				"total_net_requirement_qty": flt(run.total_net_requirement_qty),
				"results": [
					{
						"item_code": row.item_code,
						"requested_day": date_diff(row.requested_date, base_date),
						"planned_qty": flt(row.planned_qty),
						"machine_scheduled_qty": flt(row.machine_scheduled_qty),
						"status": row.status,
						"actual_status": row.actual_status,
						"delay_minutes": flt(row.delay_minutes),
						"is_locked": int(row.is_locked or 0),
					}
					for row in results
				],
			}
		)

	mould_rules = frappe.get_all(
		"APS Mould-Machine Rule",
		filters={"notes": ("like", f"{RUN_NOTE_PREFIX}%")},
		fields=["item_code", "workstation", "mould_reference", "preferred", "priority", "notes"],
		order_by="notes asc, item_code asc",
		limit_page_length=0,
	)
	resource_rows = [dict(row) for row in mould_rules]
	for row in resource_rows:
		document_names.extend([row["item_code"], row["workstation"], row["notes"]])

	zero_item = _item_code("RAW-ZERO-RM")
	zero_qty = frappe.db.get_value("Bin", {"item_code": zero_item}, "actual_qty")
	return {
		"site": frappe.local.site,
		"company": company,
		"base_date": str(base_date),
		"catalog": get_scenario_manifest(),
		"scenario_schedule_counts": dict(sorted(scenario_schedule_counts.items())),
		"schedule_rows": schedule_rows,
		"formal_run_rows": run_rows,
		"mould_rules": resource_rows,
		"zero_material": {"item_code": zero_item, "actual_qty": flt(zero_qty)},
		"all_document_names_are_prefixed": all(
			str(value).startswith(FIXTURE_PREFIX) for value in document_names if value
		),
	}


def _ensure_fixture_masters(company: str) -> dict:
	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	stock_uom = frappe.db.get_value("UOM", "Nos", "name") or frappe.db.get_value("UOM", {}, "name")
	customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	if not all((item_group, stock_uom, customer_group, territory)):
		frappe.throw("The isolated fixture clone is missing basic ERPNext master data.")

	if not frappe.db.exists("Customer", FIXTURE_CUSTOMER):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": FIXTURE_CUSTOMER,
				"customer_type": "Company",
				"customer_group": customer_group,
				"territory": territory,
			}
		).insert(ignore_permissions=True, set_name=FIXTURE_CUSTOMER)

	for item_code in _fixture_item_codes():
		if frappe.db.exists("Item", item_code):
			continue
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"description": "APS V2 isolated Phase 0 scenario fixture",
				"item_group": item_group,
				"stock_uom": stock_uom,
				"is_stock_item": 1,
				"include_item_in_manufacturing": 1,
			}
		).insert(ignore_permissions=True, set_name=item_code)

	warehouse = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 0, "warehouse_name": "Stores"}, "name"
	) or frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
	if not warehouse:
		frappe.throw(f"No leaf Warehouse exists for fixture company {company}.")
	if not frappe.db.exists("Plant Floor", FIXTURE_PLANT_FLOOR):
		frappe.get_doc(
			{
				"doctype": "Plant Floor",
				"floor_name": FIXTURE_PLANT_FLOOR,
				"company": company,
				"warehouse": warehouse,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Workstation", FIXTURE_WORKSTATION):
		frappe.get_doc(
			{
				"doctype": "Workstation",
				"workstation_name": FIXTURE_WORKSTATION,
				"plant_floor": FIXTURE_PLANT_FLOOR,
				"warehouse": warehouse,
				"production_capacity": 1,
				"status": "Idle",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("APS Machine Capability", FIXTURE_WORKSTATION):
		frappe.get_doc(
			{
				"doctype": "APS Machine Capability",
				"workstation": FIXTURE_WORKSTATION,
				"plant_floor": FIXTURE_PLANT_FLOOR,
				"hourly_capacity_qty": 125,
				"daily_capacity_qty": 1000,
				"queue_sequence": 1,
				"machine_status": "Available",
				"is_active": 1,
				"sync_source": "APS V2 Phase 0 fixture",
			}
		).insert(ignore_permissions=True)
	else:
		frappe.db.set_value(
			"APS Machine Capability",
			FIXTURE_WORKSTATION,
			{"hourly_capacity_qty": 125, "daily_capacity_qty": 1000, "is_active": 1},
			update_modified=False,
		)
	return {"warehouse": warehouse}


def _create_schedule(*, company, scenario_name, revision, status, demands, base_date) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Customer Delivery Schedule",
			"customer": FIXTURE_CUSTOMER,
			"company": company,
			"schedule_scope": f"{FIXTURE_PREFIX}{scenario_name}",
			"version_no": revision,
			"import_strategy": "Replace Scope",
			"source_type": "Customer Delivery Schedule",
			"status": status,
			"items": [
				{
					"item_code": _item_code(row["item"]),
					"schedule_date": add_days(base_date, row["day"]),
					"qty": row["qty"],
					"balance_qty": row["qty"],
					"status": "Open",
					"source_origin": "manual_added",
					"remark": f"{RUN_NOTE_PREFIX}{scenario_name}",
				}
				for row in demands
			],
		}
	)
	doc.flags.aps_schedule_import_transition = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _create_formal_run(company, scenario_name, run_spec, scenario_spec, base_date) -> str:
	qty = flt(run_spec.get("owned_qty") or run_spec.get("requested_qty"))
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": company,
			"plant_floor": FIXTURE_PLANT_FLOOR,
			"planning_date": add_days(base_date, run_spec.get("day") or 0),
			"horizon_days": 14,
			"run_type": "Formal",
			"existing_work_order_policy": "Exclude",
			"status": run_spec["status"],
			"approval_state": "Approved",
			"total_net_requirement_qty": qty,
			"notes": f"{RUN_NOTE_PREFIX}{scenario_name}|{run_spec['key']}",
		}
	).insert(ignore_permissions=True)
	demand = (scenario_spec.get("demands") or ({"item": "UNKNOWN", "day": 0, "qty": qty},))[0]
	execution = scenario_spec.get("execution") or {}
	frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": run.name,
			"company": company,
			"plant_floor": FIXTURE_PLANT_FLOOR,
			"customer": FIXTURE_CUSTOMER,
			"item_code": _item_code(demand["item"]),
			"requested_date": add_days(base_date, demand["day"]),
			"demand_source": "Customer Delivery Schedule",
			"planned_qty": qty,
			"machine_scheduled_qty": qty,
			"scheduled_qty": qty,
			"status": run_spec["status"],
			"risk_status": "Critical" if execution.get("delay_minutes") else "Normal",
			"actual_status": "Delayed" if execution.get("delay_minutes") else "Not Started",
			"delay_minutes": execution.get("delay_minutes") or 0,
			"is_locked": 1 if execution.get("frozen") else 0,
			"notes": f"{RUN_NOTE_PREFIX}{scenario_name}|{run_spec['key']}",
		}
	).insert(ignore_permissions=True)
	return run.name


def _create_mould_rule(scenario_name: str, demand: dict) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "APS Mould-Machine Rule",
			"item_code": _item_code(demand["item"]),
			"workstation": FIXTURE_WORKSTATION,
			"mould_reference": f"{FIXTURE_PREFIX}{demand['mould']}",
			"preferred": 1,
			"priority": 1,
			"is_active": 1,
			"notes": f"{RUN_NOTE_PREFIX}{scenario_name}",
		}
	).insert(ignore_permissions=True)
	return doc.name


def _ensure_zero_material_bin(warehouse: str) -> None:
	item_code = _item_code("RAW-ZERO-RM")
	name = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "name")
	if name:
		frappe.db.set_value("Bin", name, {"actual_qty": 0, "projected_qty": 0}, update_modified=False)
		return
	frappe.get_doc(
		{
			"doctype": "Bin",
			"item_code": item_code,
			"warehouse": warehouse,
			"actual_qty": 0,
			"projected_qty": 0,
		}
	).insert(ignore_permissions=True)


def _fixture_item_codes() -> list[str]:
	item_keys = set()
	for spec in SCENARIOS.values():
		item_keys.update(row["item"] for row in spec.get("demands") or ())
		item_keys.update(row["item"] for row in spec.get("materials") or ())
		for source, target in spec.get("bom_edges") or ():
			item_keys.update((source, target))
	return [_item_code(key) for key in sorted(item_keys)]


def _item_code(key: str) -> str:
	return f"{FIXTURE_PREFIX}{key}"


def _delete_names(doctype: str, fieldname: str, names: list[str]) -> None:
	if names and frappe.db.exists("DocType", doctype):
		frappe.db.delete(doctype, {fieldname: ("in", names)})


def _assert_fixture_site() -> str:
	assert_isolated_environment(require_fixture=True)
	if frappe.local.site != FIXTURE_SITE:
		frappe.throw(f"APS V2 scenario mutation is restricted to {FIXTURE_SITE}.")
	company = str(frappe.conf.get("aps_phase0_fixture_company") or "").strip()
	if not company or not frappe.db.exists("Company", company):
		frappe.throw("Fixture site must configure an existing aps_phase0_fixture_company.")
	return company


def _repeatability_signature(capture: dict) -> dict:
	return {
		"catalog": capture["catalog"],
		"scenario_schedule_counts": capture["scenario_schedule_counts"],
		"schedule_rows": capture["schedule_rows"],
		"formal_run_rows": capture["formal_run_rows"],
		"mould_rules": capture["mould_rules"],
		"zero_material": capture["zero_material"],
		"all_document_names_are_prefixed": capture["all_document_names_are_prefixed"],
	}


def _digest(payload: dict) -> str:
	encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
