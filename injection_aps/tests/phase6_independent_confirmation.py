from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime, nowtime, today

from injection_aps.api import app
from injection_aps.services import availability, capacity_balance, consistency, delivery_sync, execution_sync, planning


CONFIRMED_TEST_SITES = {"aps-opt-test.localhost", "jce.1"}
TEST_SITE = "aps-opt-test.localhost"
PREFIX = "PHASE6-"
MARKER = "PHASE6_INDEPENDENT_CONFIRMATION"
QTY_TOLERANCE = 0.000001


def run_phase6_gate(output_dir: str | None = None) -> dict:
	"""Run the Phase 6 independent confirmation gate on the isolated test site."""
	_assert_test_site()
	frappe.set_user("Administrator")
	artifact_dir = Path(output_dir or frappe.get_site_path("private", "files", "aps_phase6_confirmation"))
	artifact_dir.mkdir(parents=True, exist_ok=True)

	cleanup(commit=True)
	payload: dict = {
		"site": frappe.local.site,
		"started_on": str(now_datetime()),
		"isolated_test_site": frappe.local.site in CONFIRMED_TEST_SITES,
		"status": "running",
		"full_chain": {},
		"scenarios": [],
		"audits": {},
		"ui_checks": {},
	}
	try:
		context = _ensure_master_data()
		payload["master_data"] = _jsonable(context)
		full_chain = _run_full_business_chain(context)
		payload["full_chain"] = _jsonable(full_chain)
		payload["audits"]["full_chain"] = _jsonable(full_chain["quantity_audit"])
		payload["scenarios"] = _jsonable(_run_pmc_scenarios(context, full_chain))
		payload["ui_checks"] = _jsonable(_run_ui_checks(context, full_chain))
		payload["automated_test_coverage"] = {
			"unit": [
				"quantity formula",
				"duplicate rows",
				"zero quantity",
				"change lower bound",
			],
			"integration": "import -> net requirement -> schedule -> release -> production feedback -> delivery",
			"idempotency": ["same schedule import", "production sync replay", "delivery sync replay"],
			"transaction": "schedule import plus rebuild rollback probe",
			"permission": "PMC, planner supervisor and production operator workflow guard tests in app suite",
		}
		payload["status"] = "passed"
		payload["finished_on"] = str(now_datetime())
		_write_json(artifact_dir / "phase6-independent-confirmation.json", payload)
		frappe.db.commit()
		return {
			"passed": True,
			"artifact": str(artifact_dir / "phase6-independent-confirmation.json"),
			"full_chain_run": full_chain["run"],
			"release_batch": full_chain["release"].get("release_batch"),
			"scenario_count": len(payload["scenarios"]),
			"audit_difference_count": full_chain["quantity_audit"]["difference_count"],
		}
	except Exception as exc:
		payload["status"] = "failed"
		payload["failed_on"] = str(now_datetime())
		payload["error"] = str(exc)
		_write_json(artifact_dir / "phase6-independent-confirmation.failed.json", payload)
		frappe.db.rollback()
		raise


def cleanup(delete_master_data: bool = True, commit: bool = True) -> dict:
	_assert_test_site()
	run_names = _names("APS Planning Run", {"notes": ["like", f"{MARKER}%"]})
	result_names = _names("APS Schedule Result", {"planning_run": ["in", run_names]}) if run_names else []
	segment_names = _names("APS Schedule Segment", {"parent": ["in", result_names]}) if result_names else []
	schedule_names = _names("Customer Delivery Schedule", {"schedule_scope": ["like", f"{PREFIX}%"]})
	schedule_items = (
		_names("Customer Delivery Schedule Item", {"parent": ["in", schedule_names]}) if schedule_names else []
	)
	sales_orders = _names("Sales Order", {"name": ["like", f"{PREFIX}%"]})
	import_batches = _names("APS Schedule Import Batch", {"schedule_scope": ["like", f"{PREFIX}%"]})
	item_names = _names("Item", {"name": ["like", f"{PREFIX}%"]})
	work_orders = set(_names("Work Order", {"name": ["like", f"{PREFIX}%"]}))
	if run_names:
		work_orders.update(_names("Work Order", {"custom_aps_run": ["in", run_names]}))
	stock_entries = set(_names("Stock Entry", {"name": ["like", f"{PREFIX}%"]}))
	delivery_notes = set(_names("Delivery Note", {"name": ["like", f"{PREFIX}%"]}))
	wos_names = set(_names("Work Order Scheduling", {"name": ["like", f"{PREFIX}%"]}))
	if run_names:
		wos_names.update(_names("Work Order Scheduling", {"custom_aps_run": ["in", run_names]}))
	if work_orders:
		stock_entries.update(_names("Stock Entry", {"work_order": ["in", sorted(work_orders)]}))
		wos_names.update(_parents_from_child("Scheduling Item", {"work_order": ["in", sorted(work_orders)]}))

	for doctype, filters in (
		("APS Production Allocation", {"planning_run": ["in", run_names]}),
		("APS Production Allocation", {"source_stock_entry": ["in", sorted(stock_entries)]}),
		("APS Delivery Allocation", {"customer_schedule_item": ["in", schedule_items]}),
		("APS Delivery Allocation", {"source_delivery_note": ["in", sorted(delivery_notes)]}),
	):
		_delete_where(doctype, filters)
	for name in sorted(stock_entries):
		_delete_where("Stock Entry Detail", {"parent": name})
		_delete_where("Stock Entry", {"name": name})
	for name in sorted(delivery_notes):
		_delete_where("Delivery Note Item", {"parent": name})
		_delete_where("Delivery Note", {"name": name})
	for name in sorted(wos_names):
		_delete_where("Scheduling Item", {"parent": name})
		_delete_where("Work Order Scheduling", {"name": name})
	for name in sorted(work_orders):
		_delete_where("Work Order Operation", {"parent": name})
		_delete_where("Work Order Item", {"parent": name})
		_delete_where("Work Order", {"name": name})
	for name in sales_orders:
		_delete_where("Sales Order Item", {"parent": name})
		_delete_where("Sales Order", {"name": name})

	release_batches = _names("APS Release Batch", {"planning_run": ["in", run_names]}) if run_names else []
	wo_batches = _names("APS Work Order Proposal Batch", {"planning_run": ["in", run_names]}) if run_names else []
	shift_batches = _names("APS Shift Schedule Proposal Batch", {"planning_run": ["in", run_names]}) if run_names else []
	for parent in release_batches:
		_delete_where("APS Released WOS Item", {"parent": parent})
	_delete_names("APS Release Batch", release_batches)
	for parent in wo_batches:
		_delete_where("APS Work Order Proposal Item", {"parent": parent})
	_delete_names("APS Work Order Proposal Batch", wo_batches)
	for parent in shift_batches:
		_delete_where("APS Shift Schedule Proposal Item", {"parent": parent})
	_delete_names("APS Shift Schedule Proposal Batch", shift_batches)
	for name in result_names:
		_delete_where("APS Schedule Segment", {"parent": name})
		_delete_where("APS Schedule Result", {"name": name})
	_delete_where("APS Exception Log", {"planning_run": ["in", run_names]})
	_delete_names("APS Planning Run", run_names)
	_delete_where("APS Net Requirement", {"item_code": ["in", item_names]})
	_delete_where("APS Demand Pool", {"item_code": ["in", item_names]})
	for batch in import_batches:
		_delete_where("APS Demand Delta", {"import_batch": batch})
	_delete_names("APS Schedule Import Batch", import_batches)
	for schedule in schedule_names:
		_delete_where("Customer Delivery Schedule Item", {"parent": schedule})
	_delete_names("Customer Delivery Schedule", schedule_names)
	_delete_where("APS Downtime Window", {"notes": ["like", f"{MARKER}%"]})
	_delete_where("APS Change Request", {"notes": ["like", f"{MARKER}%"]})

	if delete_master_data:
		bom_names = _names("BOM", {"item": ["in", item_names]}) if item_names else []
		for bom in bom_names:
			_delete_where("BOM Item", {"parent": bom})
			_delete_where("BOM Operation", {"parent": bom})
			_delete_where("BOM Scrap Item", {"parent": bom})
			_delete_where("BOM", {"name": bom})
		mold_names = _names("Mold", {"mold_name": ["like", f"{PREFIX}%"]})
		for mold in mold_names:
			_delete_where("Mold Product", {"parent": mold})
			_delete_where("Mold Default Material", {"parent": mold})
			_delete_where("Mold", {"name": mold})
		_delete_where("Bin", {"item_code": ["in", item_names]})
		_delete_names("Item", item_names)
		for doctype in ("APS Machine Capability", "Workstation", "Plant Floor", "Customer"):
			_delete_where(doctype, {"name": ["like", f"{PREFIX}%"]})
		_delete_where("Warehouse", {"name": ["like", f"{PREFIX}%"]})
	if commit:
		frappe.db.commit()
	return {
		"runs": len(run_names),
		"results": len(result_names),
		"schedules": len(schedule_names),
		"items": len(item_names),
	}


def _run_full_business_chain(context: dict) -> dict:
	due_date = getdate(add_days(today(), 1))
	rows = [
		{
			"sales_order": context["sales_order"],
			"item_code": context["flow_item"],
			"customer_part_no": "FLOW",
			"schedule_date": due_date,
			"qty": 120,
			"production_strategy": "Auto Balance",
			"demand_confidence": "Confirmed",
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
			"source_excel_row": 2,
		}
	]
	import_result = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no=f"{PREFIX}FLOW-V1",
		schedule_scope=f"{PREFIX}FLOW-{context['suffix']}",
		import_strategy="Replace Scope",
		duplicate_policy="Block",
		rows_json=rows,
		rebuild=1,
		existing_work_order_policy="Exclude",
	)
	run_result = planning.run_planning_run(
		company=context["company"],
		plant_floor=context["plant_floor"],
		plant_floors=[context["plant_floor"]],
		horizon_days=5,
		customer=context["customer"],
		item_code=context["flow_item"],
		existing_work_order_policy="Exclude",
		run_type="Trial",
	)
	run_name = run_result["run"]
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		"notes",
		f"{MARKER}: full import-net-schedule-release-report-delivery",
		update_modified=False,
	)
	initial_capacity = _ensure_capacity_applied(run_name)
	approve_result = planning.approve_planning_run(run_name)
	capacity_after_approval = _ensure_capacity_current(run_name, reason="after approval")
	work_order_proposal = planning.generate_work_order_proposals(run_name)
	_assert_positive(
		"work_order_proposal",
		"APS Work Order Proposal Batch",
		work_order_proposal.get("work_order_proposal_batch"),
		work_order_proposal.get("proposal_count"),
	)
	_approve_proposal_batch("APS Work Order Proposal Batch", work_order_proposal["work_order_proposal_batch"])
	work_order_apply = planning.apply_work_order_proposals(work_order_proposal["work_order_proposal_batch"])
	_assert_positive("work_order_apply", "Work Order", run_name, len(work_order_apply.get("applied_work_orders") or []))
	capacity_after_work_order = _ensure_capacity_current(run_name, reason="after work order proposal apply")
	shift_proposal = planning.generate_shift_schedule_proposals(
		run_name=run_name,
		work_order_proposal_batch=work_order_proposal["work_order_proposal_batch"],
		release_horizon_days=7,
		release_from_date=today(),
	)
	_assert_positive(
		"shift_schedule_proposal",
		"APS Shift Schedule Proposal Batch",
		shift_proposal.get("shift_schedule_proposal_batch"),
		shift_proposal.get("proposal_count"),
	)
	_approve_proposal_batch("APS Shift Schedule Proposal Batch", shift_proposal["shift_schedule_proposal_batch"])
	release_apply = planning.apply_shift_schedule_proposals(shift_proposal["shift_schedule_proposal_batch"])
	_assert_positive("release_apply", "APS Release Batch", release_apply.get("release_batch"), release_apply.get("applied_rows"))

	schedule_item = _single_value(
		"Customer Delivery Schedule Item",
		{"parent": import_result["schedule"], "item_code": context["flow_item"]},
		"name",
	)
	page_before_execution = _page_snapshot(run_name, context, import_result["schedule"])
	scheduling_rows = _get_released_scheduling_rows(run_name)
	_assert_positive("released_scheduling_rows", "Scheduling Item", run_name, len(scheduling_rows))
	_assert_qty(
		"released_scheduling_qty",
		"APS Planning Run",
		run_name,
		120,
		sum(flt(row.scheduling_qty) for row in scheduling_rows),
	)
	started_schedulings = _start_formal_scheduling_for_production(run_name, scheduling_rows)
	distinct_workstations = sorted({row.workstation for row in scheduling_rows if row.workstation})

	first_row = scheduling_rows[0]
	first_qty = flt(first_row.scheduling_qty)
	_create_manufacture_entry(context, run_name, first_row, first_qty, sequence=1)
	first_production_sync = execution_sync.sync_production_for_run(run_name)
	first_delivery = _create_delivery_note(
		context,
		schedule_item=schedule_item,
		qty=50,
		sequence=1,
		is_return=0,
	)
	first_delivery_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[context["flow_item"]],
	)
	jit_snapshot = _page_snapshot(run_name, context, import_result["schedule"])
	_assert_qty(
		"jit_partial_production",
		"APS Planning Run",
		run_name,
		first_qty,
		jit_snapshot["gantt_quantity_summary"].get("produced_qty"),
	)
	_assert_qty(
		"jit_partial_delivery",
		"APS Planning Run",
		run_name,
		50,
		jit_snapshot["gantt_quantity_summary"].get("delivered_qty"),
	)

	for index, row in enumerate(scheduling_rows[1:], start=2):
		_create_manufacture_entry(context, run_name, row, flt(row.scheduling_qty), sequence=index)
	second_delivery = _create_delivery_note(
		context,
		schedule_item=schedule_item,
		qty=70,
		sequence=2,
		is_return=0,
	)
	final_production_sync = execution_sync.sync_production_for_run(run_name)
	final_delivery_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[context["flow_item"]],
	)
	availability_result = availability.recalculate_run_fulfillment(run_name)
	consistency_result = consistency.recalculate_plan_consistency(
		run_name,
		reason="Phase 6 independent confirmation final reconciliation",
	)
	page_after_execution = _page_snapshot(run_name, context, import_result["schedule"])
	audit = consistency.audit_run_quantity_consistency(run_name)
	if not audit.get("valid"):
		_raise_audit_failure("quantity_consistency_audit", audit)
	_assert_qty(
		"final_produced_qty",
		"APS Planning Run",
		run_name,
		120,
		page_after_execution["gantt_quantity_summary"].get("produced_qty"),
	)
	_assert_qty(
		"final_delivered_qty",
		"APS Planning Run",
		run_name,
		120,
		page_after_execution["gantt_quantity_summary"].get("delivered_qty"),
	)
	return {
		"run": run_name,
		"import": import_result,
		"planning": run_result,
		"capacity": {
			"initial": initial_capacity,
			"after_approval": capacity_after_approval,
			"after_work_order_apply": capacity_after_work_order,
		},
		"approval": approve_result,
		"work_order_proposal": work_order_proposal,
		"work_order_apply": work_order_apply,
		"shift_proposal": shift_proposal,
		"release": release_apply,
		"schedule_item": schedule_item,
		"released_scheduling_rows": [dict(row) for row in scheduling_rows],
		"started_work_order_schedulings": started_schedulings,
		"distinct_workstations": distinct_workstations,
		"production_sync": {
			"partial": first_production_sync,
			"final": final_production_sync,
			"idempotent_replay": execution_sync.sync_production_for_run(run_name),
		},
		"delivery_sync": {
			"partial": first_delivery_sync,
			"final": final_delivery_sync,
			"idempotent_replay": delivery_sync.sync_delivery_allocations(
				company=context["company"],
				customer=context["customer"],
				item_codes=[context["flow_item"]],
			),
			"delivery_notes": [first_delivery, second_delivery],
		},
		"fulfillment": availability_result,
		"consistency": consistency_result,
		"page_comparison": {
			"before_execution": page_before_execution,
			"partial_jit": jit_snapshot,
			"after_execution": page_after_execution,
		},
		"quantity_audit": audit,
	}


def _run_pmc_scenarios(context: dict, full_chain: dict) -> list[dict]:
	return [
		_scenario_cancel_tomorrow(context),
		_scenario_temporary_increase(context),
		_scenario_decrease_after_start(context),
		_scenario_split_order_across_two_machines(full_chain),
		_scenario_urgent_insert_displaces(context),
		_scenario_jit_produce_and_deliver(full_chain),
		_scenario_machine_downtime(context, full_chain),
		_scenario_duplicate_rows(context),
		_scenario_repeat_import(context),
		_scenario_delivery_note_cancel_return(context),
		_scenario_transaction_rollback(context),
	]


def _scenario_cancel_tomorrow(context: dict) -> dict:
	scope = f"{PREFIX}CANCEL-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 1))
	_import_scope(context, scope, item, 30, due_date, version="V1")
	result = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 0, due_date, source_excel_row=3)],
	)
	row = _active_schedule_row(scope, item)
	_assert_qty("cancel_tomorrow_qty", "Customer Delivery Schedule Item", row.name, 0, row.qty)
	_assert_equal("cancel_tomorrow_status", "Customer Delivery Schedule Item", row.name, "Cancelled", row.status)
	return {"scenario": "cancel_tomorrow_delivery", "passed": True, "import": result, "schedule_item": row.name}


def _scenario_temporary_increase(context: dict) -> dict:
	scope = f"{PREFIX}INCREASE-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 1))
	_import_scope(context, scope, item, 40, due_date, version="V1")
	preview = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 70, due_date, source_excel_row=4)],
	)
	row_preview = next(row for row in preview["rows"] if row["item_code"] == item)
	_assert_equal("temporary_increase_type", "Customer Delivery Schedule", scope, "Increased", row_preview["change_type"])
	result = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 70, due_date, source_excel_row=4)],
	)
	row = _active_schedule_row(scope, item)
	_assert_qty("temporary_increase_qty", "Customer Delivery Schedule Item", row.name, 70, row.qty)
	return {"scenario": "temporary_increase_tomorrow", "passed": True, "preview": preview, "import": result}


def _scenario_decrease_after_start(context: dict) -> dict:
	scope = f"{PREFIX}DECREASE-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 2))
	_import_scope(context, scope, item, 100, due_date, version="V1")
	row = _active_schedule_row(scope, item)
	frappe.db.set_value(
		"Customer Delivery Schedule Item",
		row.name,
		{"produced_qty": 40, "balance_qty": 100, "status": "Open"},
		update_modified=False,
	)
	preview = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 70, due_date, source_excel_row=5)],
	)
	row_preview = next(row for row in preview["rows"] if row["item_code"] == item)
	_assert_equal("decrease_after_start_change_type", "Customer Delivery Schedule", scope, "Reduced", row_preview["change_type"])
	_assert_qty("decrease_after_start_produced_impact", "Customer Delivery Schedule Item", row.name, 1, row_preview["affects_produced"])
	result = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 70, due_date, source_excel_row=5)],
	)
	updated = _active_schedule_row(scope, item)
	_assert_qty("decrease_after_start_qty", "Customer Delivery Schedule Item", updated.name, 70, updated.qty)
	_assert_qty("decrease_after_start_retained_produced", "Customer Delivery Schedule Item", updated.name, 40, updated.produced_qty)
	return {"scenario": "decrease_after_start", "passed": True, "preview": preview, "import": result}


def _scenario_split_order_across_two_machines(full_chain: dict) -> dict:
	workstations = full_chain.get("distinct_workstations") or []
	_assert_qty("split_order_machine_count", "APS Planning Run", full_chain["run"], 2, len(workstations))
	total_scheduled = sum(flt(row.get("scheduling_qty")) for row in full_chain.get("released_scheduling_rows") or [])
	_assert_qty("split_order_total_qty", "APS Planning Run", full_chain["run"], 120, total_scheduled)
	return {
		"scenario": "one_order_split_to_two_machines",
		"passed": True,
		"workstations": workstations,
		"scheduled_qty": total_scheduled,
	}


def _scenario_urgent_insert_displaces(context: dict) -> dict:
	required_date = getdate(add_days(today(), 1))
	probe = _create_open_overlap_probe(context, required_date=required_date)
	analysis = planning.analyze_insert_order_impact(
		company=context["company"],
		plant_floor=context["plant_floor"],
		plant_floors=[context["plant_floor"]],
		item_code=context["flow_item"],
		qty=30,
		required_date=required_date,
		customer=context["customer"],
	)
	displaced_segments = analysis.get("displaced_segments") or []
	if not displaced_segments:
		_phase6_fail(
			"urgent_insert_displaced_segments",
			"APS Planning Run",
			probe["run"],
			"> 0",
			0,
			0,
			details=[
				{
					"probe": probe,
					"parallelization_plan": analysis.get("parallelization_plan") or [],
					"candidate_workstations": analysis.get("candidate_workstations") or [],
					"selected_plant_floors": analysis.get("selected_plant_floors") or [],
					"scheduled_qty": analysis.get("scheduled_qty"),
					"unscheduled_qty": analysis.get("unscheduled_qty"),
					"exceptions": analysis.get("exceptions") or [],
				}
			],
		)
	return {
		"scenario": "urgent_order_insert_displaces_original",
		"passed": True,
		"probe": probe,
		"displaced_segments": displaced_segments,
		"scheduled_qty": analysis.get("scheduled_qty"),
	}


def _scenario_jit_produce_and_deliver(full_chain: dict) -> dict:
	partial = full_chain["page_comparison"]["partial_jit"]["gantt_quantity_summary"]
	final = full_chain["page_comparison"]["after_execution"]["gantt_quantity_summary"]
	_assert_positive("jit_partial_produced", "APS Planning Run", full_chain["run"], partial.get("produced_qty"))
	_assert_positive("jit_partial_delivered", "APS Planning Run", full_chain["run"], partial.get("delivered_qty"))
	_assert_qty("jit_final_delivered", "APS Planning Run", full_chain["run"], final.get("planned_qty"), final.get("delivered_qty"))
	return {
		"scenario": "jit_produce_while_delivering",
		"passed": True,
		"partial": partial,
		"final": final,
	}


def _scenario_machine_downtime(context: dict, full_chain: dict) -> dict:
	segment = frappe.get_doc("APS Schedule Segment", full_chain["released_scheduling_rows"][0]["custom_aps_segment_reference"])
	window = frappe.get_doc(
		{
			"doctype": "APS Downtime Window",
			"company": context["company"],
			"scope": "Workstation",
			"plant_floor": context["plant_floor"],
			"workstation": segment.workstation,
			"start_time": segment.start_time,
			"end_time": min(get_datetime(segment.end_time), get_datetime(segment.start_time) + timedelta(minutes=20)),
			"available_capacity_percent": 0,
			"reason": "Phase 6 machine stop replay",
			"status": "Active",
			"planning_run": full_chain["run"],
			"notes": f"{MARKER}: machine downtime replay",
		}
	).insert(ignore_permissions=True)
	impact = planning.preview_schedule_impact(run_name=full_chain["run"], downtime_window=window.name)
	if not impact.get("blockers") and not impact.get("affected_count"):
		_phase6_fail("machine_downtime", "APS Downtime Window", window.name, "blockers or affected segments", "no impact", "")
	frappe.db.set_value("APS Downtime Window", window.name, "status", "Cancelled", update_modified=False)
	return {
		"scenario": "machine_stops_suddenly",
		"passed": True,
		"downtime_window": window.name,
		"impact": impact,
	}


def _scenario_duplicate_rows(context: dict) -> dict:
	scope = f"{PREFIX}DUP-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 2))
	rows = [
		_schedule_row(item, 40, due_date, source_excel_row=7),
		_schedule_row(item, 60, due_date, source_excel_row=8),
	]
	blocked = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V1",
		schedule_scope=scope,
		import_strategy="Replace Scope",
		duplicate_policy="Block",
		rows_json=rows,
	)
	_assert_equal("duplicate_block_can_import", "APS Schedule Import Batch", scope, False, bool(blocked["can_import"]))
	summed = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V1",
		schedule_scope=scope,
		import_strategy="Replace Scope",
		duplicate_policy="Sum",
		rows_json=rows,
	)
	_assert_qty("duplicate_sum_qty", "APS Schedule Import Batch", scope, 100, summed["effective_schedule_rows"][0]["qty"])
	return {"scenario": "duplicate_rows_in_schedule_file", "passed": True, "blocked": blocked, "summed": summed}


def _scenario_repeat_import(context: dict) -> dict:
	scope = f"{PREFIX}REPEAT-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 3))
	rows = [_schedule_row(item, 25, due_date, source_excel_row=9)]
	first = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V1",
		schedule_scope=scope,
		import_strategy="Append",
		duplicate_policy="Block",
		rows_json=rows,
	)
	second = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Append",
		duplicate_policy="Block",
		rows_json=rows,
	)
	_assert_qty("repeat_import_idempotent", "APS Schedule Import Batch", second.get("import_batch"), 1, second.get("idempotent_replay"))
	_assert_equal("repeat_import_batch", "APS Schedule Import Batch", second.get("import_batch"), first.get("import_batch"), second.get("import_batch"))
	return {"scenario": "same_file_repeated_import", "passed": True, "first": first, "second": second}


def _scenario_delivery_note_cancel_return(context: dict) -> dict:
	scope = f"{PREFIX}DN-CANCEL-{context['suffix']}"
	item = context["return_item"]
	due_date = getdate(add_days(today(), 2))
	import_result = _import_scope(context, scope, item, 30, due_date, version="V1")
	schedule_item = _single_value(
		"Customer Delivery Schedule Item",
		{"parent": import_result["schedule"], "item_code": item},
		"name",
	)
	delivery_note = _create_delivery_note(
		{**context, "flow_item": item},
		schedule_item=schedule_item,
		qty=30,
		sequence=31,
		is_return=0,
	)
	first_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[item],
	)
	_assert_qty("delivery_note_initial_qty", "Delivery Note", delivery_note, 30, first_sync["rollup"]["delivered_qty"])
	frappe.db.set_value("Delivery Note", delivery_note, "docstatus", 2, update_modified=False)
	cancel_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[item],
	)
	_assert_qty("delivery_note_cancel_qty", "Delivery Note", delivery_note, 0, cancel_sync["rollup"]["delivered_qty"])
	return {
		"scenario": "delivery_note_cancel_or_return",
		"passed": True,
		"delivery_note": delivery_note,
		"first_sync": first_sync,
		"cancel_sync": cancel_sync,
	}


def _scenario_transaction_rollback(context: dict) -> dict:
	scope = f"{PREFIX}TXN-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 4))
	_import_scope(context, scope, item, 20, due_date, version="V1")
	before = {
		"batches": frappe.db.count("APS Schedule Import Batch", {"schedule_scope": scope}),
		"schedules": frappe.db.count("Customer Delivery Schedule", {"schedule_scope": scope}),
	}
	with patch(
		"injection_aps.services.planning.rebuild_demand_pool",
		side_effect=RuntimeError("PHASE6 forced rollback"),
	):
		try:
			planning.import_customer_delivery_schedule(
				customer=context["customer"],
				company=context["company"],
				version_no="V2",
				schedule_scope=scope,
				import_strategy="Replace Scope",
				duplicate_policy="Block",
				rows_json=[_schedule_row(item, 35, due_date, source_excel_row=12)],
				rebuild=1,
				existing_work_order_policy="Exclude",
			)
		except RuntimeError:
			pass
		else:
			_phase6_fail("transaction_rollback", "APS Schedule Import Batch", scope, "forced rollback exception", "success", "")
	after = {
		"batches": frappe.db.count("APS Schedule Import Batch", {"schedule_scope": scope}),
		"schedules": frappe.db.count("Customer Delivery Schedule", {"schedule_scope": scope}),
	}
	_assert_equal("transaction_batches_rolled_back", "APS Schedule Import Batch", scope, before["batches"], after["batches"])
	_assert_equal("transaction_schedules_rolled_back", "Customer Delivery Schedule", scope, before["schedules"], after["schedules"])
	return {"scenario": "mid_import_failure_rolls_back", "passed": True, "before": before, "after": after}


def _run_ui_checks(context: dict, full_chain: dict) -> dict:
	run_name = full_chain["run"]
	gantt = app.get_schedule_gantt_data(run_name)
	release = app.get_release_center_data(run_name)
	progress = planning.get_customer_schedule_progress_data(
		company=context["company"],
		customer=context["customer"],
		item_code=context["flow_item"],
		schedule_scope=f"{PREFIX}FLOW-{context['suffix']}",
		run_name=run_name,
	)
	_assert_positive("ui_gantt_tasks", "APS Planning Run", run_name, len(gantt.get("tasks") or []))
	_assert_positive("ui_release_batches", "APS Planning Run", run_name, len(release.get("release_batches") or []))
	_assert_qty("ui_gantt_delivered", "APS Planning Run", run_name, 120, gantt["quantity_summary"].get("delivered_qty"))
	_assert_qty("ui_release_delivered", "APS Planning Run", run_name, 120, release["quantity_summary"].get("delivered_qty"))
	_assert_qty("ui_progress_delivered", "APS Planning Run", run_name, 120, progress["summary"].get("delivered_qty"))
	translation_path = Path(__file__).resolve().parents[1] / "translations" / "zh.csv"
	translation_text = translation_path.read_text(encoding="utf-8")
	required_terms = [
		"机台排程看板",
		"释放批次",
		"排程段",
		"客户排期目标",
	]
	missing = [term for term in required_terms if term not in translation_text]
	if missing:
		_phase6_fail("zh_translation_check", "File", str(translation_path), "all required Chinese terms", ", ".join(missing), "")
	return {
		"passed": True,
		"gantt_task_count": len(gantt.get("tasks") or []),
		"release_batch_count": len(release.get("release_batches") or []),
		"progress_rows": progress["summary"].get("rows"),
		"page_quantity_comparison": full_chain["page_comparison"],
		"translation_file": str(translation_path),
		"required_terms": required_terms,
	}


def _ensure_master_data() -> dict:
	suffix = frappe.generate_hash(length=8).upper()
	company = frappe.db.get_value("Company", "APS Phase 0 Test Company", "name") or frappe.db.get_value("Company", {}, "name")
	if not company:
		_phase6_fail("master_data", "Company", "Company", "existing test company", "missing", "")
	parent_warehouse = _root_warehouse(company)
	fallback_warehouse = _leaf_warehouse(company)
	item_group = _ensure_item_group()
	stock_uom = frappe.db.get_value("UOM", "Nos", "name") or frappe.db.get_value("UOM", {}, "name")
	customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	if not all((parent_warehouse or fallback_warehouse, item_group, stock_uom, customer_group, territory)):
		_phase6_fail("master_data", "Company", company, "warehouse/item group/uom/customer group/territory", "missing", "")

	customer = _create_customer(customer_group, territory, suffix)
	raw_warehouse = _create_warehouse(company, parent_warehouse, suffix, "RM", fallback_warehouse=fallback_warehouse)
	fg_warehouse = _create_warehouse(
		company,
		parent_warehouse,
		suffix,
		"FG",
		capacity_qty=10000,
		fallback_warehouse=fallback_warehouse,
	)
	plant_floor = _create_plant_floor(company, fg_warehouse, raw_warehouse, fg_warehouse, suffix)
	workstations = [
		_create_workstation(company, plant_floor, fg_warehouse, suffix, "A"),
		_create_workstation(company, plant_floor, fg_warehouse, suffix, "B"),
	]
	for index, workstation in enumerate(workstations, start=1):
		_create_machine_capability(workstation, plant_floor, index)
	raw_item = _create_item(f"{PREFIX}RM-{suffix}", item_group, stock_uom, raw_warehouse, is_fg=False)
	flow_item = _create_item(f"{PREFIX}FLOW-{suffix}", item_group, stock_uom, fg_warehouse)
	scenario_item = _create_item(f"{PREFIX}SCENARIO-{suffix}", item_group, stock_uom, fg_warehouse)
	return_item = _create_item(f"{PREFIX}RETURN-{suffix}", item_group, stock_uom, fg_warehouse)
	_upsert_bin(company, raw_item, raw_warehouse, stock_uom, actual_qty=10000)
	for item in (flow_item, scenario_item, return_item):
		_create_bom(item, raw_item, company, raw_warehouse)
		_create_mold(item, company, fg_warehouse, suffix, "A")
		_create_mold(item, company, fg_warehouse, suffix, "B")
	sales_order, sales_order_item = _create_sales_order(
		company=company,
		customer=customer,
		item_code=flow_item,
		warehouse=fg_warehouse,
		stock_uom=stock_uom,
		qty=120,
		delivery_date=getdate(add_days(today(), 1)),
		suffix=suffix,
	)
	settings = frappe.get_single("APS Settings")
	settings.default_company = company
	settings.default_plant_floor = plant_floor
	settings.planning_horizon_days = 7
	settings.release_horizon_days = 7
	settings.default_hourly_capacity_qty = 100
	settings.minimum_parallel_split_qty = 1
	settings.default_setup_minutes = 0
	settings.save(ignore_permissions=True)
	return {
		"suffix": suffix,
		"company": company,
		"warehouse": fg_warehouse,
		"raw_warehouse": raw_warehouse,
		"fg_warehouse": fg_warehouse,
		"item_group": item_group,
		"stock_uom": stock_uom,
		"customer": customer,
		"plant_floor": plant_floor,
		"workstations": workstations,
		"raw_item": raw_item,
		"flow_item": flow_item,
		"scenario_item": scenario_item,
		"return_item": return_item,
		"sales_order": sales_order,
		"sales_order_item": sales_order_item,
	}


def _ensure_item_group() -> str:
	if frappe.db.exists("Item Group", "Plastic Part"):
		return "Plastic Part"
	parent = frappe.db.get_value("Item Group", {"is_group": 1}, "name") or "All Item Groups"
	frappe.get_doc(
		{
			"doctype": "Item Group",
			"item_group_name": "Plastic Part",
			"parent_item_group": parent,
			"is_group": 0,
		}
	).insert(ignore_permissions=True)
	return "Plastic Part"


def _leaf_warehouse(company: str) -> str | None:
	return (
		frappe.db.get_value("Warehouse", {"company": company, "is_group": 0, "warehouse_name": "Stores"}, "name")
		or frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
	)


def _root_warehouse(company: str) -> str | None:
	return (
		frappe.db.get_value("Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ["is", "not set"]}, "name")
		or frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
	)


def _create_customer(customer_group: str, territory: str, suffix: str) -> str:
	name = f"{PREFIX}CUSTOMER-{suffix}"
	doc = frappe.new_doc("Customer")
	doc.name = name
	doc.customer_name = name
	doc.customer_type = "Company"
	doc.customer_group = customer_group
	doc.territory = territory
	if frappe.get_meta("Customer").has_field("custom_customer_abbreviation"):
		doc.custom_customer_abbreviation = f"P6-{suffix[:6]}"
	doc.insert(ignore_permissions=True)
	return doc.name


def _create_warehouse(
	company: str,
	parent_warehouse: str | None,
	suffix: str,
	code: str,
	*,
	capacity_qty: float = 0,
	fallback_warehouse: str | None = None,
) -> str:
	name = f"{PREFIX}{code}-{suffix}"
	doc = frappe.new_doc("Warehouse")
	doc.warehouse_name = name
	doc.company = company
	doc.parent_warehouse = parent_warehouse
	doc.is_group = 0
	if frappe.get_meta("Warehouse").has_field("custom_aps_capacity_qty"):
		doc.custom_aps_capacity_qty = capacity_qty
	try:
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		if fallback_warehouse:
			return fallback_warehouse
		raise


def _create_plant_floor(company: str, warehouse: str, source_warehouse: str, fg_warehouse: str, suffix: str) -> str:
	name = f"{PREFIX}FLOOR-{suffix}"
	doc = frappe.get_doc(
		{
			"doctype": "Plant Floor",
			"floor_name": name,
			"company": company,
			"warehouse": warehouse,
		}
	)
	meta = frappe.get_meta("Plant Floor")
	if meta.has_field("custom_default_source_warehouse"):
		doc.custom_default_source_warehouse = source_warehouse
	if meta.has_field("custom_default_finished_goods_warehouse"):
		doc.custom_default_finished_goods_warehouse = fg_warehouse
	if meta.has_field("custom_default_scrap_warehouse"):
		doc.custom_default_scrap_warehouse = source_warehouse
	doc.insert(ignore_permissions=True)
	return doc.name


def _create_workstation(company: str, plant_floor: str, warehouse: str, suffix: str, code: str) -> str:
	name = f"{PREFIX}MC-{code}-{suffix}"
	doc = frappe.get_doc(
		{
			"doctype": "Workstation",
			"workstation_name": name,
			"plant_floor": plant_floor,
			"warehouse": warehouse,
			"production_capacity": 1,
			"status": "Idle",
		}
	).insert(ignore_permissions=True)
	return doc.name


def _create_machine_capability(workstation: str, plant_floor: str, sequence: int) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "APS Machine Capability",
			"workstation": workstation,
			"plant_floor": plant_floor,
			"machine_tonnage": 120,
			"risk_category": "",
			"hourly_capacity_qty": 100,
			"daily_capacity_qty": 800,
			"queue_sequence": sequence,
			"machine_status": "Available",
			"max_run_hours": 1,
			"is_active": 1,
			"sync_source": MARKER,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _create_item(item_code: str, item_group: str, stock_uom: str, warehouse: str, is_fg: bool = True) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"description": f"{MARKER} test item",
			"item_group": item_group,
			"stock_uom": stock_uom,
			"is_stock_item": 1,
			"include_item_in_manufacturing": 1,
			"valuation_rate": 1,
			"custom_aps_prebuild_allowed": 1 if is_fg else 0,
			"custom_aps_max_prebuild_days": 2 if is_fg else 0,
			"custom_aps_cancellation_risk_percent": 0,
			"custom_aps_max_stock_qty": 10000,
			"item_defaults": [
				{
					"company": frappe.db.get_value("Warehouse", warehouse, "company"),
					"default_warehouse": warehouse,
				}
			],
		}
	).insert(ignore_permissions=True)
	return doc.name


def _create_bom(item_code: str, raw_item: str, company: str, warehouse: str) -> str:
	currency = frappe.db.get_value("Company", company, "default_currency") or "USD"
	raw_uom = frappe.db.get_value("Item", raw_item, "stock_uom")
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": item_code,
			"quantity": 1,
			"company": company,
			"currency": currency,
			"is_active": 1,
			"is_default": 1,
			"items": [
				{
					"item_code": raw_item,
					"qty": 1,
					"uom": raw_uom,
					"stock_uom": raw_uom,
					"rate": 1,
					"source_warehouse": warehouse,
				}
			],
		}
	)
	if frappe.get_meta("BOM").has_field("custom_temporary_bom"):
		bom.custom_temporary_bom = "No"
	bom.insert(ignore_permissions=True)
	bom.submit()
	frappe.db.set_value("Item", item_code, "default_bom", bom.name, update_modified=False)
	return bom.name


def _upsert_bin(company: str, item_code: str, warehouse: str, stock_uom: str, *, actual_qty: float) -> str:
	existing = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "name")
	values = {
		"actual_qty": actual_qty,
		"projected_qty": actual_qty,
		"stock_uom": stock_uom,
		"company": company,
		"valuation_rate": 1,
		"stock_value": actual_qty,
	}
	if existing:
		frappe.db.set_value("Bin", existing, values, update_modified=False)
		return existing
	doc = frappe.new_doc("Bin")
	doc.name = frappe.generate_hash(length=10)
	doc.item_code = item_code
	doc.warehouse = warehouse
	for fieldname, value in values.items():
		setattr(doc, fieldname, value)
	doc.db_insert()
	return doc.name


def _create_sales_order(
	*,
	company: str,
	customer: str,
	item_code: str,
	warehouse: str,
	stock_uom: str,
	qty: float,
	delivery_date,
	suffix: str,
) -> tuple[str, str]:
	currency = frappe.db.get_value("Company", company, "default_currency") or "USD"
	so_name = f"{PREFIX}SO-{suffix}"
	doc = frappe.new_doc("Sales Order")
	doc.name = so_name
	doc.docstatus = 1
	doc.company = company
	doc.customer = customer
	doc.transaction_date = today()
	doc.delivery_date = delivery_date
	doc.currency = currency
	doc.conversion_rate = 1
	doc.plc_conversion_rate = 1
	doc.selling_price_list = frappe.db.get_value("Price List", {"selling": 1}, "name") or ""
	doc.status = "To Deliver and Bill"
	doc.total_qty = qty
	doc.total = qty
	doc.net_total = qty
	doc.base_total = qty
	doc.base_net_total = qty
	doc.grand_total = qty
	doc.base_grand_total = qty
	doc.rounded_total = qty
	doc.base_rounded_total = qty
	doc.per_delivered = 0
	doc.per_billed = 0
	doc.db_insert()

	row = frappe.new_doc("Sales Order Item")
	row.name = f"{PREFIX}SOI-{suffix}"
	row.docstatus = 1
	row.parent = doc.name
	row.parenttype = "Sales Order"
	row.parentfield = "items"
	row.idx = 1
	row.item_code = item_code
	row.item_name = item_code
	row.description = item_code
	row.delivery_date = delivery_date
	row.qty = qty
	row.stock_qty = qty
	row.uom = stock_uom
	row.stock_uom = stock_uom
	row.conversion_factor = 1
	row.warehouse = warehouse
	row.rate = 1
	row.base_rate = 1
	row.amount = qty
	row.base_amount = qty
	row.net_rate = 1
	row.base_net_rate = 1
	row.net_amount = qty
	row.base_net_amount = qty
	row.delivered_qty = 0
	row.db_insert()
	return doc.name, row.name


def _create_mold(item_code: str, company: str, warehouse: str, suffix: str, code: str) -> str:
	mold = frappe.get_doc(
		{
			"doctype": "Mold",
			"mold_name": f"{PREFIX}MOLD-{code}-{item_code}-{suffix}",
			"company": company,
			"ownership_type": "Company",
			"default_warehouse": warehouse,
			"current_warehouse": warehouse,
			"mold_type": "INJ",
			"cavity_count": 1,
			"is_family_mold": 0,
			"standard_cycle_seconds": 60,
			"machine_tonnage": 80,
			"status": "Active",
			"mold_products": [
				{
					"item_code": item_code,
					"output_group": "Default",
					"configuration_label": code,
					"priority": 1 if code == "A" else 2,
					"is_default_product": 1 if code == "A" else 0,
					"output_qty": 1,
					"cavity_output_qty": 1,
					"cycle_time_seconds": 60,
				}
			],
		}
	)
	mold.insert(ignore_permissions=True)
	mold.submit()
	frappe.db.set_value("Mold", mold.name, "status", "Active", update_modified=False)
	return mold.name


def _ensure_capacity_applied(run_name: str) -> dict:
	status = frappe.db.get_value("APS Planning Run", run_name, "capacity_balance_status")
	if status == "Applied":
		return {"status": "Applied", "idempotent_replay": 1}
	analysis = capacity_balance.analyze_capacity_balance(run_name, persist=True)
	summary = analysis.get("summary") or {}
	if summary.get("blocked_demands") or summary.get("unscheduled_qty"):
		_raise_capacity_failure(run_name, analysis)
	status = frappe.db.get_value("APS Planning Run", run_name, "capacity_balance_status")
	if status == "Applied":
		return {"status": "Applied", "analysis": analysis, "idempotent_replay": 1}
	if status not in ("Suggestion Ready", "Confirmation Required"):
		_phase6_fail(
			"capacity_balance_status",
			"APS Planning Run",
			run_name,
			"Suggestion Ready, Confirmation Required, or Applied",
			status,
			"",
			details={"summary": summary, "demands": analysis.get("demands") or []},
		)
	if summary.get("requires_confirmation"):
		capacity_balance.confirm_capacity_balance(run_name)
	try:
		return capacity_balance.apply_capacity_balance(run_name, pmc_confirmed=1)
	except Exception:
		latest_status = frappe.db.get_value("APS Planning Run", run_name, "capacity_balance_status")
		_phase6_fail(
			"capacity_balance_apply",
			"APS Planning Run",
			run_name,
			"Applied",
			latest_status,
			"",
			details={"summary": summary, "demands": analysis.get("demands") or []},
		)


def _ensure_capacity_current(run_name: str, *, reason: str) -> dict:
	try:
		analysis = capacity_balance.assert_applied_capacity_current(run_name, lock_rows=True)
		return {"status": "Applied", "current": 1, "reason": reason, "summary": analysis.get("summary") or {}}
	except frappe.ValidationError as exc:
		capacity_balance.invalidate_capacity_balance(run_name)
		application = _ensure_capacity_applied(run_name)
		analysis = capacity_balance.assert_applied_capacity_current(run_name, lock_rows=True)
		return {
			"status": "Applied",
			"current": 0,
			"reason": reason,
			"previous_error": str(exc),
			"application": application,
			"summary": analysis.get("summary") or {},
		}


def _raise_capacity_failure(run_name: str, analysis: dict) -> None:
	summary = analysis.get("summary") or {}
	demands = analysis.get("demands") or []
	first_blocked = next(
		(row for row in demands if row.get("status") == "Blocked" or flt(row.get("unscheduled_qty")) > QTY_TOLERANCE),
		{},
	)
	_phase6_fail(
		"capacity_balance",
		"APS Planning Run",
		run_name,
		"blocked_demands=0 and unscheduled_qty=0",
		"blocked_demands={0}, unscheduled_qty={1}".format(
			summary.get("blocked_demands"),
			summary.get("unscheduled_qty"),
		),
		flt(summary.get("unscheduled_qty")),
		details={
			"summary": summary,
			"first_blocked_demand": first_blocked,
		},
	)


def _approve_proposal_batch(doctype: str, name: str) -> None:
	doc = frappe.get_doc(doctype, name)
	for row in doc.get("items") or []:
		if row.review_status == "Pending":
			row.review_status = "Approved"
	doc.flags.proposal_engine_transition = True
	doc.save(ignore_permissions=True)


def _get_released_scheduling_rows(run_name: str) -> list:
	return frappe.get_all(
		"Scheduling Item",
		filters={"custom_aps_run": run_name},
		fields=[
			"name",
			"parent",
			"work_order",
			"workstation",
			"scheduling_qty",
			"planned_start_date",
			"planned_end_date",
			"custom_aps_result_reference",
			"custom_aps_segment_reference",
		],
		order_by="planned_start_date asc, name asc",
		limit_page_length=0,
	)


def _start_formal_scheduling_for_production(run_name: str, scheduling_rows: list) -> list[str]:
	wos_names = sorted({row.parent for row in scheduling_rows if row.get("parent")})
	for wos_name in wos_names:
		wos = frappe.db.get_value(
			"Work Order Scheduling",
			wos_name,
			["custom_aps_run", "custom_aps_approval_state", "status"],
			as_dict=True,
		)
		if not wos:
			_record_failure("start_formal_scheduling", "Work Order Scheduling", wos_name, "exists", None)
		if wos.custom_aps_run != run_name:
			_record_failure("start_formal_scheduling", "Work Order Scheduling", wos_name, run_name, wos.custom_aps_run)
		if wos.custom_aps_approval_state != "Approved":
			_record_failure(
				"start_formal_scheduling",
				"Work Order Scheduling",
				wos_name,
				"Approved",
				wos.custom_aps_approval_state,
			)
		frappe.db.set_value(
			"Work Order Scheduling",
			wos_name,
			"status",
			"Manufacture",
			update_modified=False,
		)
	return wos_names


def _create_manufacture_entry(context: dict, run_name: str, scheduling_row, qty: float, sequence: int) -> str:
	name = f"{PREFIX}SE-{context['suffix']}-{sequence}"
	doc = frappe.new_doc("Stock Entry")
	doc.name = name
	doc.docstatus = 1
	doc.company = context["company"]
	doc.purpose = "Manufacture"
	doc.stock_entry_type = "Manufacture"
	doc.work_order = scheduling_row.work_order
	doc.work_order_scheduling = scheduling_row.parent
	doc.posting_date = today()
	doc.posting_time = nowtime()
	doc.fg_completed_qty = qty
	doc.custom_aps_scheduling_item = scheduling_row.name
	doc.custom_aps_segment_reference = scheduling_row.custom_aps_segment_reference
	doc.custom_aps_output_type = "Good"
	doc.db_insert()
	detail = frappe.new_doc("Stock Entry Detail")
	detail.name = f"{PREFIX}SED-{context['suffix']}-{sequence}"
	detail.parent = doc.name
	detail.parenttype = "Stock Entry"
	detail.parentfield = "items"
	detail.idx = 1
	detail.item_code = context["flow_item"]
	detail.qty = qty
	detail.transfer_qty = qty
	detail.is_finished_item = 1
	if frappe.db.has_column("Stock Entry Detail", "is_scrap_item"):
		detail.is_scrap_item = 0
	detail.t_warehouse = context["warehouse"]
	detail.db_insert()
	return doc.name


def _create_delivery_note(context: dict, *, schedule_item: str, qty: float, sequence: int, is_return: int = 0) -> str:
	name = f"{PREFIX}DN-{context['suffix']}-{sequence}"
	doc = frappe.new_doc("Delivery Note")
	doc.name = name
	doc.docstatus = 1
	doc.company = context["company"]
	doc.customer = context["customer"]
	doc.posting_date = today()
	doc.posting_time = nowtime()
	doc.is_return = is_return
	doc.db_insert()
	target = frappe.db.get_value(
		"Customer Delivery Schedule Item",
		schedule_item,
		["item_code", "sales_order"],
		as_dict=True,
	)
	if not target:
		_record_failure("create_delivery_note", "Customer Delivery Schedule Item", schedule_item, "exists", None)
	sales_order_item = ""
	if target.sales_order:
		sales_order_item = frappe.db.get_value(
			"Sales Order Item",
			{"parent": target.sales_order, "item_code": target.item_code, "docstatus": 1},
			"name",
		)
		if not sales_order_item:
			_record_failure(
				"create_delivery_note",
				"Sales Order Item",
				target.sales_order,
				f"submitted line for {target.item_code}",
				None,
			)
	row = frappe.new_doc("Delivery Note Item")
	row.name = f"{PREFIX}DNI-{context['suffix']}-{sequence}"
	row.parent = doc.name
	row.parenttype = "Delivery Note"
	row.parentfield = "items"
	row.idx = 1
	row.item_code = target.item_code
	row.qty = -abs(qty) if is_return else qty
	row.stock_qty = -abs(qty) if is_return else qty
	row.conversion_factor = 1
	row.against_sales_order = target.sales_order
	row.so_detail = sales_order_item
	row.custom_aps_customer_schedule_item = schedule_item
	row.db_insert()
	return doc.name


def _create_open_overlap_probe(context: dict, *, required_date) -> dict:
	probe_date = getdate(add_days(required_date, 1))
	start = get_datetime(f"{probe_date} 00:20:00")
	end = start + timedelta(hours=1)
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": context["company"],
			"plant_floor": context["plant_floor"],
			"planning_date": today(),
			"horizon_start": start,
			"horizon_end": start + timedelta(days=2),
			"horizon_days": 2,
			"run_type": "Trial",
			"existing_work_order_policy": "Exclude",
			"status": "Planned",
			"approval_state": "Pending",
			"notes": f"{MARKER}: urgent displacement probe",
		}
	).insert(ignore_permissions=True)
	segments = [
		{
			"workstation": workstation,
			"plant_floor": context["plant_floor"],
			"start_time": start,
			"end_time": end,
			"planned_qty": 40,
			"sequence_no": index,
			"segment_kind": "Primary",
			"segment_status": "Planned",
		}
		for index, workstation in enumerate(context["workstations"], start=1)
	]
	result = frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": run.name,
			"company": context["company"],
			"plant_floor": context["plant_floor"],
			"customer": context["customer"],
			"item_code": context["flow_item"],
			"requested_date": getdate(required_date),
			"demand_source": "Customer Delivery Schedule",
			"planned_qty": sum(row["planned_qty"] for row in segments),
			"machine_scheduled_qty": sum(row["planned_qty"] for row in segments),
			"scheduled_qty": sum(row["planned_qty"] for row in segments),
			"unscheduled_qty": 0,
			"status": "Planned",
			"risk_status": "Normal",
			"segments": segments,
		}
	).insert(ignore_permissions=True)
	segments = frappe.get_all("APS Schedule Segment", filters={"parent": result.name}, pluck="name")
	return {"run": run.name, "result": result.name, "segments": segments}


def _page_snapshot(run_name: str, context: dict, schedule_name: str) -> dict:
	gantt = app.get_schedule_gantt_data(run_name)
	release = app.get_release_center_data(run_name)
	progress = planning.get_customer_schedule_progress_data(
		company=context["company"],
		customer=context["customer"],
		item_code=context["flow_item"],
		schedule_scope=frappe.db.get_value("Customer Delivery Schedule", schedule_name, "schedule_scope"),
		run_name=run_name,
	)
	return {
		"gantt_quantity_summary": gantt.get("quantity_summary") or {},
		"gantt_fulfillment_summary": gantt.get("fulfillment_summary") or {},
		"gantt_task_count": len(gantt.get("tasks") or []),
		"release_quantity_summary": release.get("quantity_summary") or {},
		"release_fulfillment_summary": release.get("fulfillment_summary") or {},
		"release_batch_count": len(release.get("release_batches") or []),
		"progress_summary": progress.get("summary") or {},
	}


def _import_scope(context: dict, scope: str, item: str, qty: float, due_date, *, version: str) -> dict:
	return planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no=version,
		schedule_scope=scope,
		import_strategy="Replace Scope",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, qty, due_date, source_excel_row=2)],
	)


def _schedule_row(item: str, qty: float, due_date, *, source_excel_row: int = 2) -> dict:
	return {
		"sales_order": "",
		"item_code": item,
		"customer_part_no": item,
		"schedule_date": due_date,
		"qty": qty,
		"production_strategy": "Auto Balance",
		"demand_confidence": "Confirmed",
		"prebuild_allowed": 1,
		"max_prebuild_days": 2,
		"source_excel_row": source_excel_row,
	}


def _active_schedule_row(scope: str, item: str):
	return frappe.db.sql(
		"""
		select i.name, i.parent, i.item_code, i.qty, i.produced_qty, i.delivered_qty, i.balance_qty, i.status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where s.schedule_scope = %s and s.status = 'Active' and i.item_code = %s
		order by s.creation desc, i.idx asc
		limit 1
		""",
		(scope, item),
		as_dict=True,
	)[0]


def _single_value(doctype: str, filters: dict, fieldname: str):
	value = frappe.db.get_value(doctype, filters, fieldname)
	if not value:
		_phase6_fail("missing_document", doctype, json.dumps(filters, default=str), fieldname, value, "")
	return value


def _names(doctype: str, filters: dict) -> list[str]:
	if not frappe.db.exists("DocType", doctype):
		return []
	if _has_empty_in_filter(filters):
		return []
	return frappe.get_all(doctype, filters=filters, pluck="name", limit_page_length=0)


def _parents_from_child(doctype: str, filters: dict) -> list[str]:
	if not frappe.db.exists("DocType", doctype):
		return []
	if _has_empty_in_filter(filters):
		return []
	return frappe.get_all(doctype, filters=filters, pluck="parent", limit_page_length=0)


def _has_empty_in_filter(filters: dict) -> bool:
	for value in (filters or {}).values():
		if isinstance(value, (list, tuple)) and len(value) == 2 and value[0] == "in" and not value[1]:
			return True
	return False


def _delete_names(doctype: str, names: list[str]) -> None:
	if not names or not frappe.db.exists("DocType", doctype):
		return
	frappe.db.delete(doctype, {"name": ["in", names]})


def _delete_where(doctype: str, filters: dict) -> None:
	if not frappe.db.exists("DocType", doctype) or _has_empty_in_filter(filters):
		return
	frappe.db.delete(doctype, filters)


def _assert_positive(stage: str, doctype: str, name: str | None, value) -> None:
	if flt(value) <= 0:
		_phase6_fail(stage, doctype, name or "-", "> 0", value, flt(value))


def _assert_qty(stage: str, doctype: str, name: str | None, expected, actual) -> None:
	difference = flt(actual) - flt(expected)
	if abs(difference) > QTY_TOLERANCE:
		_phase6_fail(stage, doctype, name or "-", expected, actual, difference)


def _assert_equal(stage: str, doctype: str, name: str | None, expected, actual) -> None:
	if expected != actual:
		_phase6_fail(stage, doctype, name or "-", expected, actual, "")


def _raise_audit_failure(stage: str, audit: dict) -> None:
	first = (audit.get("differences") or [{}])[0]
	_phase6_fail(
		stage,
		first.get("doctype") or "APS Planning Run",
		first.get("name") or audit.get("run") or "-",
		first.get("expected_qty"),
		first.get("actual_qty"),
		first.get("difference_qty"),
		details=audit.get("differences") or [],
	)


def _phase6_fail(stage: str, doctype: str, name: str, expected, actual, difference, details=None) -> None:
	payload = {
		"stage": stage,
		"doctype": doctype,
		"document": name,
		"expected": expected,
		"actual": actual,
		"difference": difference,
		"details": details or [],
	}
	frappe.throw("Phase 6 failure: {0}".format(json.dumps(_jsonable(payload), ensure_ascii=False, default=str)), frappe.ValidationError)


def _jsonable(value):
	return json.loads(frappe.as_json(value))


def _write_json(path: Path, payload) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _assert_test_site() -> None:
	if frappe.local.site not in CONFIRMED_TEST_SITES:
		frappe.throw(
			"Phase 6 independent confirmation may run only on confirmed isolated test sites {0}; current site is {1}.".format(
				", ".join(sorted(CONFIRMED_TEST_SITES)),
				frappe.local.site,
			),
			frappe.PermissionError,
		)
