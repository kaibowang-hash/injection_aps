from __future__ import annotations

import json
from datetime import timedelta

import frappe
from frappe.utils import add_days, flt, get_datetime, getdate, now_datetime, today
from frappe.utils.password import update_password

from injection_aps.services import availability, consistency, delivery_sync, execution_sync


TEST_SITE = "aps-opt-test.localhost"
TEST_USER = "aps-phase4-ui@example.com"
TEST_PASSWORD = "Phase4UI2026Test"
MARKER = "PHASE4_UI_EVIDENCE"
DOC_PREFIX = "PHASE4-UI-"


def prepare():
	_assert_test_site()
	cleanup(delete_user=True, commit=False)
	_create_test_user()
	fixture = _create_fixture()
	frappe.db.commit()
	return {
		"site": TEST_SITE,
		"user": TEST_USER,
		"password": TEST_PASSWORD,
		**fixture,
	}


def validate():
	_assert_test_site()
	run_names = frappe.get_all(
		"APS Planning Run",
		filters={"notes": ["like", f"{MARKER}%"]},
		pluck="name",
	)
	if len(run_names) != 1:
		frappe.throw(f"Expected exactly one Phase 4 UI run, found {len(run_names)}.")
	run_name = run_names[0]
	identity = frappe.db.get_value(
		"APS Schedule Result",
		{"planning_run": run_name},
		["company", "customer", "item_code"],
		as_dict=True,
	)
	if not identity:
		frappe.throw("Phase 4 UI run has no result identity.")

	production_replay = execution_sync.sync_production_for_run(run_name)
	delivery_replay = delivery_sync.sync_delivery_allocations(
		company=identity.company,
		customer=identity.customer,
		item_codes=[identity.item_code],
	)
	fulfillment = availability.recalculate_run_fulfillment(run_name)
	from injection_aps.api import app
	from injection_aps.services import planning

	gantt = app.get_schedule_gantt_data(run_name)
	release = app.get_release_center_data(run_name)
	progress = planning.get_customer_schedule_progress_data(
		company=identity.company,
		customer=identity.customer,
		item_code=identity.item_code,
		schedule_scope=MARKER,
		run_name=run_name,
	)
	result_names = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		pluck="name",
		order_by="requested_date asc",
	)
	details = [planning.get_schedule_result_detail(name) for name in result_names]
	run_row = frappe.db.get_value(
		"APS Planning Run",
		run_name,
		[
			"total_net_requirement_qty",
			"total_machine_scheduled_qty",
			"total_demand_covered_qty",
			"total_overproduction_qty",
			"total_unscheduled_qty",
			"total_produced_qty",
			"total_delivered_qty",
			"total_prebuild_qty",
			"total_jit_qty",
			"total_scrap_qty",
			"total_current_deliverable_qty",
			"total_prebuild_inventory_qty",
			"total_cancellation_inventory_risk_qty",
			"consistency_status",
		],
		as_dict=True,
	)
	formal_production = frappe.db.sql(
		"""
		select
			coalesce(sum(case
				when ifnull(sed.is_scrap_item, 0) = 1
					or (ifnull(wo.scrap_warehouse, '') != '' and sed.t_warehouse = wo.scrap_warehouse)
					or se.custom_aps_output_type = 'Scrap'
				then 0
				when se.custom_aps_output_type = 'Good' or sed.is_finished_item = 1
				then coalesce(nullif(sed.transfer_qty, 0), sed.qty)
				else 0
			end), 0) as good_qty,
			coalesce(sum(case
				when ifnull(sed.is_scrap_item, 0) = 1
					or (ifnull(wo.scrap_warehouse, '') != '' and sed.t_warehouse = wo.scrap_warehouse)
					or se.custom_aps_output_type = 'Scrap'
				then coalesce(nullif(sed.transfer_qty, 0), sed.qty)
				else 0
			end), 0) as scrap_qty
		from `tabStock Entry` se
		inner join `tabStock Entry Detail` sed on sed.parent = se.name
		left join `tabWork Order` wo on wo.name = se.work_order
			where se.docstatus = 1 and se.name like %(prefix)s
				and (sed.is_finished_item = 1 or sed.is_scrap_item = 1)
				and sed.item_code = wo.production_item
		""",
		{"prefix": f"{DOC_PREFIX}MFG-%"},
		as_dict=True,
	)[0]
	production_ledger = frappe.db.sql(
		"""
		select coalesce(sum(good_qty), 0) as good_qty, coalesce(sum(scrap_qty), 0) as scrap_qty
		from `tabAPS Production Allocation`
		where planning_run = %s and is_effective = 1
		""",
		run_name,
		as_dict=True,
	)[0]
	formal_delivery = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(dni.stock_qty), 0)
			from `tabDelivery Note` dn
			inner join `tabDelivery Note Item` dni on dni.parent = dn.name
			where dn.docstatus = 1 and dn.name like %s
			""",
			f"{DOC_PREFIX}DN-%",
		)[0][0]
	)
	delivery_ledger = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(effective_qty), 0)
			from `tabAPS Delivery Allocation`
			where company = %s and customer = %s and item_code = %s and is_effective = 1
			""",
			(identity.company, identity.customer, identity.item_code),
		)[0][0]
	)

	expected_quantities = {
		"planned_qty": 120,
		"machine_scheduled_qty": 130,
		"demand_covered_qty": 120,
		"overproduction_qty": 10,
		"unscheduled_qty": 0,
		"produced_qty": 75,
		"delivered_qty": 40,
	}
	expected_fulfillment = {
		"prebuild_qty": 60,
		"jit_qty": 70,
		"actual_good_qty": 75,
		"scrap_qty": 5,
		"current_deliverable_qty": 25,
		"prebuild_inventory_qty": 20,
		"cancellation_inventory_risk_qty": 10,
	}
	run_quantity_map = {
		"planned_qty": run_row.total_net_requirement_qty,
		"machine_scheduled_qty": run_row.total_machine_scheduled_qty,
		"demand_covered_qty": run_row.total_demand_covered_qty,
		"overproduction_qty": run_row.total_overproduction_qty,
		"unscheduled_qty": run_row.total_unscheduled_qty,
		"produced_qty": run_row.total_produced_qty,
		"delivered_qty": run_row.total_delivered_qty,
	}
	run_fulfillment_map = {
		"prebuild_qty": run_row.total_prebuild_qty,
		"jit_qty": run_row.total_jit_qty,
		"actual_good_qty": run_row.total_produced_qty,
		"scrap_qty": run_row.total_scrap_qty,
		"current_deliverable_qty": run_row.total_current_deliverable_qty,
		"prebuild_inventory_qty": run_row.total_prebuild_inventory_qty,
		"cancellation_inventory_risk_qty": run_row.total_cancellation_inventory_risk_qty,
	}
	checks = {
		"consistency_valid": run_row.consistency_status == "Valid",
		"run_quantities": _qty_map_equal(run_quantity_map, expected_quantities),
		"gantt_quantities": _qty_map_equal(gantt["quantity_summary"], expected_quantities),
		"release_quantities": _qty_map_equal(release["quantity_summary"], expected_quantities),
		"run_fulfillment": _qty_map_equal(run_fulfillment_map, expected_fulfillment),
		"gantt_fulfillment": _qty_map_equal(gantt["fulfillment_summary"], expected_fulfillment),
		"release_fulfillment": _qty_map_equal(release["fulfillment_summary"], expected_fulfillment),
		"projection_fulfillment": _qty_map_equal(fulfillment["summary"], expected_fulfillment),
		"progress_fulfillment": _qty_map_equal(progress["summary"], expected_fulfillment),
		"formal_production_matches_ledger": _qty_equal(formal_production.good_qty, 75)
		and _qty_equal(formal_production.scrap_qty, 5)
		and _qty_equal(production_ledger.good_qty, 75)
		and _qty_equal(production_ledger.scrap_qty, 5),
		"formal_delivery_matches_ledger": _qty_equal(formal_delivery, 40)
		and _qty_equal(delivery_ledger, 40),
		"production_replay_idempotent": production_replay["ledger"]["created"] == 0
		and production_replay["ledger"]["reversed"] == 0
		and frappe.db.count("APS Production Allocation", {"planning_run": run_name}) == 5,
		"delivery_replay_idempotent": delivery_replay["ledger"]["created"] == 0
		and delivery_replay["ledger"]["reversed"] == 0
		and delivery_replay["rollup"]["schedule_item_count"] == 2,
		"gantt_segments": len(gantt["tasks"]) == 3
		and {row["details"]["production_mode"] for row in gantt["tasks"]} == {"Prebuild", "JIT"},
		"progress_rows": progress["summary"]["rows"] == 2 and len(progress["rows"]) == 2,
		"detail_traces": sum(len(row["production_allocations"]) for row in details) == 5
		and sum(len(row["delivery_allocations"]) for row in details) == 2,
		"detail_segment_sources": all(
			segment.get("latest_stock_entry")
			in {
				allocation.source_stock_entry
				for allocation in detail["production_allocations"]
				if allocation.segment == segment.name and allocation.is_effective
			}
			for detail in details
			for segment in detail["segments"]
			if flt(segment.get("actual_good_qty")) + flt(segment.get("actual_scrap_qty")) > 0
		),
	}
	failed = [name for name, passed in checks.items() if not passed]
	if failed:
		frappe.throw(
			"Phase 4 UI fixture validation failed: {0}<br>{1}".format(
				", ".join(failed),
				json.dumps(checks, sort_keys=True),
			)
		)
	frappe.db.commit()
	return {
		"passed": True,
		"run": run_name,
		"checks": checks,
		"quantities": expected_quantities,
		"fulfillment": expected_fulfillment,
		"production_allocations": frappe.db.count("APS Production Allocation", {"planning_run": run_name}),
		"delivery_allocations": frappe.db.count(
			"APS Delivery Allocation",
			{"company": identity.company, "customer": identity.customer, "item_code": identity.item_code},
		),
	}


def cleanup(delete_user=True, commit=True):
	_assert_test_site()
	run_names = frappe.get_all(
		"APS Planning Run",
		filters={"notes": ["like", f"{MARKER}%"]},
		pluck="name",
	)
	result_names = (
		frappe.get_all(
			"APS Schedule Result",
			filters={"planning_run": ["in", run_names]},
			pluck="name",
		)
		if run_names
		else []
	)
	net_names = (
		frappe.get_all(
			"APS Schedule Result",
			filters={"name": ["in", result_names]},
			pluck="net_requirement",
		)
		if result_names
		else []
	)
	schedule_names = frappe.get_all(
		"Customer Delivery Schedule",
		filters={"schedule_scope": MARKER},
		pluck="name",
	)
	schedule_items = (
		frappe.get_all(
			"Customer Delivery Schedule Item",
			filters={"parent": ["in", schedule_names]},
			pluck="name",
		)
		if schedule_names
		else []
	)
	production_allocations = (
		frappe.get_all(
			"APS Production Allocation",
			filters={"planning_run": ["in", run_names]},
			fields=["name", "source_stock_entry", "work_order_scheduling", "work_order"],
		)
		if run_names
		else []
	)
	delivery_allocations = (
		frappe.get_all(
			"APS Delivery Allocation",
			filters={"customer_schedule_item": ["in", schedule_items]},
			fields=["name", "source_delivery_note"],
		)
		if schedule_items
		else []
	)

	stock_entries = {row.source_stock_entry for row in production_allocations if row.source_stock_entry}
	delivery_notes = {row.source_delivery_note for row in delivery_allocations if row.source_delivery_note}
	wos_names = {row.work_order_scheduling for row in production_allocations if row.work_order_scheduling}
	work_orders = {row.work_order for row in production_allocations if row.work_order}
	stock_entries.update(frappe.get_all("Stock Entry", filters={"name": ["like", f"{DOC_PREFIX}MFG-%"]}, pluck="name"))
	delivery_notes.update(
		frappe.get_all("Delivery Note", filters={"name": ["like", f"{DOC_PREFIX}DN-%"]}, pluck="name")
	)
	wos_names.update(
		frappe.get_all(
			"Work Order Scheduling",
			filters={"name": ["like", f"{DOC_PREFIX}WOS-%"]},
			pluck="name",
		)
	)
	work_orders.update(
		frappe.get_all("Work Order", filters={"name": ["like", f"{DOC_PREFIX}WO-%"]}, pluck="name")
	)

	for row in production_allocations:
		frappe.db.delete("APS Production Allocation", {"name": row.name})
	for row in delivery_allocations:
		frappe.db.delete("APS Delivery Allocation", {"name": row.name})
	for name in stock_entries:
		frappe.db.delete("Stock Entry Detail", {"parent": name})
		frappe.db.delete("Stock Entry", {"name": name})
	for name in delivery_notes:
		frappe.db.delete("Delivery Note Item", {"parent": name})
		frappe.db.delete("Delivery Note", {"name": name})
	for name in wos_names:
		frappe.db.delete("Scheduling Item", {"parent": name})
		frappe.db.delete("Work Order Scheduling", {"name": name})
	for name in result_names:
		frappe.db.delete("APS Schedule Segment", {"parent": name})
		frappe.db.delete("APS Schedule Result", {"name": name})
	for name in set(net_names):
		if name:
			frappe.db.delete("APS Net Requirement", {"name": name})
	for name in run_names:
		frappe.db.delete("APS Planning Run", {"name": name})
	for name in schedule_names:
		frappe.db.delete("Customer Delivery Schedule Item", {"parent": name})
		frappe.db.delete("Customer Delivery Schedule", {"name": name})
	for name in work_orders:
		frappe.db.delete("Work Order Operation", {"parent": name})
		frappe.db.delete("Work Order Item", {"parent": name})
		frappe.db.delete("Work Order", {"name": name})
	frappe.db.delete("Item", {"name": ["like", f"{DOC_PREFIX}ITEM-%"]})
	frappe.db.delete("Customer", {"name": ["like", f"{DOC_PREFIX}CUSTOMER-%"]})
	if delete_user and frappe.db.exists("User", TEST_USER):
		frappe.delete_doc("User", TEST_USER, ignore_permissions=True, force=True)
	if commit:
		frappe.db.commit()
	return {
		"deleted_runs": len(run_names),
		"deleted_stock_entries": len(stock_entries),
		"deleted_delivery_notes": len(delivery_notes),
		"deleted_user": int(bool(delete_user)),
	}


def _create_test_user():
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": TEST_USER,
			"first_name": "APS Phase 4 UI",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	).insert(ignore_permissions=True)
	user.add_roles("System Manager")
	update_password(TEST_USER, TEST_PASSWORD, logout_all_sessions=True)


def _create_fixture():
	company = frappe.db.get_value("Company", {})
	workstation = frappe.db.get_value("Workstation", {})
	item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or frappe.db.get_value("Item Group", {})
	stock_uom = frappe.db.get_value("UOM", {})
	customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}) or frappe.db.get_value("Customer Group", {})
	territory = frappe.db.get_value("Territory", {"is_group": 0}) or frappe.db.get_value("Territory", {})
	warehouses = frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0},
		pluck="name",
		limit=2,
	)
	warehouse = warehouses[0] if warehouses else None
	scrap_warehouse = warehouses[1] if len(warehouses) > 1 else None
	if not all((company, workstation, item_group, stock_uom, customer_group, territory, warehouse, scrap_warehouse)):
		frappe.throw(
			"Phase 4 UI fixture needs Company, Workstation, Item Group, UOM, Customer Group, Territory, and two Warehouse records."
		)

	suffix = frappe.generate_hash(length=8).upper()
	customer = _create_customer(customer_group, territory, suffix)
	item = _create_item(item_group, stock_uom, suffix)
	work_order = _create_work_order(company, item, stock_uom, warehouse, scrap_warehouse, suffix)
	plant_floor = frappe.db.get_value("Workstation", workstation, "plant_floor")
	start = get_datetime(f"{getdate(add_days(today(), 1))} 08:00:00")
	first_due = getdate(add_days(today(), 2))
	second_due = getdate(add_days(today(), 3))
	report_anchor = now_datetime() - timedelta(hours=3)

	schedule = frappe.get_doc(
		{
			"doctype": "Customer Delivery Schedule",
			"customer": customer,
			"company": company,
			"schedule_scope": MARKER,
			"version_no": f"{MARKER}-{suffix}",
			"import_strategy": "Append",
			"source_type": "Customer Delivery Schedule",
			"status": "Active",
			"items": [
				{
					"item_code": item,
					"customer_part_no": "AUTO-100",
					"schedule_date": first_due,
					"qty": 100,
					"balance_qty": 100,
					"production_strategy": "Auto Balance",
					"demand_confidence": "Confirmed",
					"prebuild_allowed": 1,
					"max_prebuild_days": 5,
					"status": "Open",
					"remark": f"{MARKER}: 30 Prebuild + 70 JIT",
				},
				{
					"item_code": item,
					"customer_part_no": "REDUCED-20",
					"schedule_date": second_due,
					"qty": 20,
					"balance_qty": 20,
					"production_strategy": "Force Prebuild",
					"demand_confidence": "Confirmed",
					"prebuild_allowed": 1,
					"max_prebuild_days": 5,
					"status": "Open",
					"change_type": "Reduced",
					"remark": f"{MARKER}: reduced after 30 finished",
				},
			],
		}
	)
	schedule.flags.aps_schedule_import_transition = True
	schedule.insert(ignore_permissions=True)
	schedule_items = frappe.get_all(
		"Customer Delivery Schedule Item",
		filters={"parent": schedule.name},
		pluck="name",
		order_by="idx asc",
	)
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": company,
			"plant_floor": plant_floor,
			"planning_date": today(),
			"horizon_start": start,
			"horizon_end": start + timedelta(days=5),
			"horizon_days": 5,
			"run_type": "Trial",
			"existing_work_order_policy": "Exclude",
			"status": "Planned",
			"approval_state": "Pending",
			"capacity_balance_status": "Applied",
			"total_prebuild_qty": 60,
			"total_jit_qty": 70,
			"notes": f"{MARKER}: browser acceptance",
		}
	).insert(ignore_permissions=True)

	first = _create_result(
		run=run.name,
		company=company,
		customer=customer,
		item=item,
		plant_floor=plant_floor,
		workstation=workstation,
		work_order=work_order,
		start=start,
		due_date=first_due,
		planned_qty=100,
		strategy="Auto Balance",
		segments=[
			{"qty": 30, "offset_days": 0, "hours": 3, "mode": "Prebuild", "load": 50},
			{"qty": 70, "offset_days": 1, "hours": 7, "mode": "JIT", "load": 90},
		],
		late_before=30,
		late_after=0,
	)
	second = _create_result(
		run=run.name,
		company=company,
		customer=customer,
		item=item,
		plant_floor=plant_floor,
		workstation=workstation,
		work_order=work_order,
		start=start + timedelta(days=1, hours=8),
		due_date=second_due,
		planned_qty=20,
		strategy="Force Prebuild",
		segments=[
			{"qty": 30, "offset_days": 0, "hours": 3, "mode": "Prebuild", "load": 65},
		],
		late_before=0,
		late_after=0,
	)
	segments = [*first["segments"], *second["segments"]]
	wos, scheduling_items = _create_execution_details(
		company=company,
		plant_floor=plant_floor,
		workstation=workstation,
		work_order=work_order,
		run=run.name,
		result_segments=[
			(first["result"], first["segments"][0], 30, 30, 0),
			(first["result"], first["segments"][1], 70, 15, 5),
			(second["result"], second["segments"][0], 30, 30, 0),
		],
		start=start,
		suffix=suffix,
		report_anchor=report_anchor,
	)
	_create_manufacture_entry(
		company, item, warehouse, work_order, wos, scheduling_items[0], segments[0], 30, "Good", report_anchor, suffix, 1
	)
	_create_manufacture_entry(
		company,
		item,
		warehouse,
		work_order,
		wos,
		scheduling_items[1],
		segments[1],
		15,
		"Good",
		report_anchor + timedelta(minutes=35),
		suffix,
		2,
	)
	_create_manufacture_entry(
		company,
		item,
		scrap_warehouse,
		work_order,
		wos,
		scheduling_items[1],
		segments[1],
		5,
		"Scrap",
		report_anchor + timedelta(minutes=40),
		suffix,
		3,
	)
	_create_manufacture_entry(
		company,
		item,
		warehouse,
		work_order,
		wos,
		scheduling_items[2],
		segments[2],
		30,
		"Good",
		report_anchor + timedelta(minutes=70),
		suffix,
		4,
	)
	delivery_note = _create_delivery_note(
		company=company,
		customer=customer,
		item=item,
		schedule_items=schedule_items,
		qtys=(20, 20),
		posting_at=report_anchor + timedelta(minutes=100),
		suffix=suffix,
	)

	consistency.recalculate_plan_consistency(run.name, reason=f"{MARKER}: initial fixture")
	production_sync = execution_sync.sync_production_for_run(run.name)
	delivery_sync_result = delivery_sync.sync_delivery_allocations(
		company=company,
		customer=customer,
		item_codes=[item],
	)
	fulfillment = availability.recalculate_run_fulfillment(run.name)
	return {
		"run": run.name,
		"results": [first["result"], second["result"]],
		"schedule": schedule.name,
		"schedule_scope": MARKER,
		"schedule_items": schedule_items,
		"company": company,
		"customer": customer,
		"item": item,
		"work_order": work_order,
		"work_order_scheduling": wos,
		"delivery_note": delivery_note,
		"production_sync": {
			"ledger": production_sync["ledger"],
			"rollup": production_sync["rollup"],
		},
		"delivery_sync": {
			"ledger": delivery_sync_result["ledger"],
			"rollup": delivery_sync_result["rollup"],
		},
		"consistency": delivery_sync_result["consistency_runs"][0]["totals"],
		"fulfillment": fulfillment["summary"],
	}


def _create_customer(customer_group, territory, suffix):
	name = f"{DOC_PREFIX}CUSTOMER-{suffix}"
	doc = frappe.new_doc("Customer")
	doc.name = name
	doc.customer_name = name
	doc.customer_type = "Company"
	doc.customer_group = customer_group
	doc.territory = territory
	doc.db_insert()
	return doc.name


def _create_item(item_group, stock_uom, suffix):
	name = f"{DOC_PREFIX}ITEM-{suffix}"
	doc = frappe.new_doc("Item")
	doc.name = name
	doc.item_code = name
	doc.item_name = "Phase 4 Balance Product"
	doc.item_group = item_group
	doc.stock_uom = stock_uom
	doc.is_stock_item = 1
	doc.disabled = 0
	doc.shelf_life_in_days = 30
	doc.custom_aps_prebuild_allowed = 1
	doc.custom_aps_max_prebuild_days = 5
	doc.custom_aps_cancellation_risk_percent = 15
	doc.custom_aps_max_stock_qty = 200
	doc.db_insert()
	return doc.name


def _create_work_order(company, item, stock_uom, warehouse, scrap_warehouse, suffix):
	name = f"{DOC_PREFIX}WO-{suffix}"
	doc = frappe.new_doc("Work Order")
	doc.name = name
	doc.naming_series = "MFG-WO-.YYYY.-"
	doc.docstatus = 1
	doc.status = "In Process"
	doc.company = company
	doc.production_item = item
	doc.item_name = "Phase 4 Balance Product"
	doc.stock_uom = stock_uom
	doc.qty = 130
	doc.skip_transfer = 1
	doc.wip_warehouse = warehouse
	doc.fg_warehouse = warehouse
	doc.scrap_warehouse = scrap_warehouse
	doc.db_insert()
	return doc.name


def _create_result(
	*,
	run,
	company,
	customer,
	item,
	plant_floor,
	workstation,
	work_order,
	start,
	due_date,
	planned_qty,
	strategy,
	segments,
	late_before,
	late_after,
):
	net = frappe.get_doc(
		{
			"doctype": "APS Net Requirement",
			"company": company,
			"customer": customer,
			"item_code": item,
			"demand_date": due_date,
			"demand_qty": planned_qty,
			"planning_qty": planned_qty,
			"net_requirement_qty": planned_qty,
			"production_strategy": strategy,
			"demand_confidence": "Confirmed",
			"prebuild_allowed": 1,
			"max_prebuild_days": 5,
			"is_system_generated": 1,
			"reason_text": MARKER,
		}
	).insert(ignore_permissions=True)
	result = frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": run,
			"company": company,
			"plant_floor": plant_floor,
			"net_requirement": net.name,
			"customer": customer,
			"item_code": item,
			"requested_date": due_date,
			"demand_source": "Customer Delivery Schedule",
			"production_strategy": strategy,
			"planned_qty": planned_qty,
			"prebuild_qty": sum(row["qty"] for row in segments if row["mode"] == "Prebuild"),
			"jit_qty": sum(row["qty"] for row in segments if row["mode"] == "JIT"),
			"early_days": max((row["offset_days"] for row in segments if row["mode"] == "Prebuild"), default=0) + 1,
			"late_qty_before_balance": late_before,
			"late_qty_after_balance": late_after,
			"capacity_balance_status": "Balanced",
			"status": "Planned",
			"risk_status": "Normal",
			"notes": f"{MARKER}: {strategy}",
			"segments": [
				{
					"workstation": workstation,
					"plant_floor": plant_floor,
					"start_time": start + timedelta(days=row["offset_days"]),
					"end_time": start + timedelta(days=row["offset_days"], hours=row["hours"]),
					"planned_qty": row["qty"],
					"sequence_no": index,
					"segment_kind": "Primary",
					"segment_status": "Planned",
					"production_mode": row["mode"],
					"capacity_bucket_start": start + timedelta(days=row["offset_days"]),
					"capacity_bucket_end": start + timedelta(days=row["offset_days"], hours=12),
					"available_capacity_qty": 100,
					"occupied_capacity_qty": row["qty"],
					"remaining_capacity_qty": 100 - row["qty"],
					"load_percent": row["load"],
					"projected_late_qty": late_after,
					"prebuildable_qty": 100 - row["qty"],
					"linked_work_order": work_order,
					"schedule_explanation": f"{MARKER}: {row['mode']}",
				}
				for index, row in enumerate(segments, start=1)
			],
		}
	).insert(ignore_permissions=True)
	segment_names = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": result.name},
		pluck="name",
		order_by="idx asc",
	)
	return {"result": result.name, "net": net.name, "segments": segment_names}


def _create_execution_details(
	*,
	company,
	plant_floor,
	workstation,
	work_order,
	run,
	result_segments,
	start,
	suffix,
	report_anchor,
):
	wos_name = f"{DOC_PREFIX}WOS-{suffix}"
	wos = frappe.new_doc("Work Order Scheduling")
	wos.name = wos_name
	wos.company = company
	wos.posting_date = today()
	wos.plant_floor = plant_floor
	wos.shift_type = "白班"
	wos.status = "Manufacture"
	wos.custom_aps_run = run
	wos.custom_aps_approval_state = "Approved"
	wos.db_insert()
	items = []
	for index, (result, segment, planned_qty, good_qty, scrap_qty) in enumerate(result_segments, start=1):
		item = frappe.new_doc("Scheduling Item")
		item.name = f"{DOC_PREFIX}SI-{suffix}-{index}"
		item.parent = wos.name
		item.parenttype = "Work Order Scheduling"
		item.parentfield = "scheduling_items"
		item.idx = index
		item.work_order = work_order
		item.workstation = workstation
		item.scheduling_qty = planned_qty
		item.completed_qty = good_qty
		item.defect_qty = scrap_qty
		item.planned_start_date = start + timedelta(hours=index - 1)
		item.planned_end_date = start + timedelta(hours=index)
		item.from_time = report_anchor + timedelta(minutes=index * 5)
		item.to_time = report_anchor + timedelta(minutes=index * 20)
		item.custom_aps_run = run
		item.custom_aps_result_reference = result
		item.custom_aps_segment_reference = segment
		item.db_insert()
		items.append(item.name)
		frappe.db.set_value(
			"APS Schedule Segment",
			segment,
			{
				"linked_work_order_scheduling": wos.name,
				"linked_scheduling_item": item.name,
			},
			update_modified=False,
		)
	return wos.name, items


def _create_manufacture_entry(
	company,
	item,
	warehouse,
	work_order,
	wos,
	scheduling_item,
	segment,
	qty,
	output_type,
	posting_at,
	suffix,
	index,
):
	name = f"{DOC_PREFIX}MFG-{suffix}-{index}"
	doc = frappe.new_doc("Stock Entry")
	doc.name = name
	doc.docstatus = 1
	doc.company = company
	doc.purpose = "Manufacture"
	doc.stock_entry_type = "Manufacture"
	doc.work_order = work_order
	doc.work_order_scheduling = wos
	doc.posting_date = posting_at.date()
	doc.posting_time = posting_at.time().replace(microsecond=0)
	doc.fg_completed_qty = qty
	doc.custom_aps_scheduling_item = scheduling_item
	doc.custom_aps_segment_reference = segment
	doc.custom_aps_output_type = output_type
	doc.db_insert()
	detail = frappe.new_doc("Stock Entry Detail")
	detail.name = f"{DOC_PREFIX}SED-{suffix}-{index}"
	detail.parent = doc.name
	detail.parenttype = "Stock Entry"
	detail.parentfield = "items"
	detail.idx = 1
	detail.item_code = item
	detail.qty = qty
	detail.transfer_qty = qty
	# zelin_pp represents defect FG as a finished row routed to WO.scrap_warehouse.
	detail.is_finished_item = 1
	detail.is_scrap_item = 0
	detail.t_warehouse = warehouse
	detail.db_insert()
	return doc.name


def _create_delivery_note(*, company, customer, item, schedule_items, qtys, posting_at, suffix):
	name = f"{DOC_PREFIX}DN-{suffix}"
	doc = frappe.new_doc("Delivery Note")
	doc.name = name
	doc.docstatus = 1
	doc.company = company
	doc.customer = customer
	doc.posting_date = posting_at.date()
	doc.posting_time = posting_at.time().replace(microsecond=0)
	doc.db_insert()
	for index, (schedule_item, qty) in enumerate(zip(schedule_items, qtys, strict=True), start=1):
		row = frappe.new_doc("Delivery Note Item")
		row.name = f"{DOC_PREFIX}DNI-{suffix}-{index}"
		row.parent = doc.name
		row.parenttype = "Delivery Note"
		row.parentfield = "items"
		row.idx = index
		row.item_code = item
		row.qty = qty
		row.stock_qty = qty
		row.conversion_factor = 1
		row.custom_aps_customer_schedule_item = schedule_item
		row.db_insert()
	return doc.name


def _qty_equal(left, right):
	return abs(flt(left) - flt(right)) <= 0.000001


def _qty_map_equal(actual, expected):
	return all(_qty_equal(actual.get(key), value) for key, value in expected.items())


def _assert_test_site():
	if frappe.local.site != TEST_SITE:
		frappe.throw(f"Phase 4 UI fixture may run only on {TEST_SITE}.")
