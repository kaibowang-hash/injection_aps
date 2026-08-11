from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import add_days, get_datetime, getdate, today
from frappe.utils.password import update_password

from injection_aps.services import change_engine, consistency


TEST_SITE = "aps-opt-test.localhost"
TEST_USER = "aps-phase3-ui@example.com"
TEST_PASSWORD = "Phase3UI2026Test"
MARKER = "PHASE3_UI_EVIDENCE"


def prepare():
	_assert_test_site()
	cleanup(delete_user=True, commit=False)
	_create_test_user()
	desktop = _create_analyzed_cancel_fixture("DESKTOP", day_offset=1)
	mobile = _create_analyzed_cancel_fixture("MOBILE", day_offset=2)
	frappe.db.commit()
	return {
		"site": TEST_SITE,
		"user": TEST_USER,
		"password": TEST_PASSWORD,
		"desktop_request": desktop["request"],
		"mobile_request": mobile["request"],
		"desktop_result": desktop["result"],
		"mobile_result": mobile["result"],
	}


def cleanup(delete_user=True, commit=True):
	_assert_test_site()
	requests = frappe.get_all(
		"APS Change Request",
		filters={"notes": ["like", f"{MARKER}%"]},
		fields=["name", "planning_run", "target_result"],
	)
	run_names = {row.planning_run for row in requests if row.planning_run}
	result_names = {row.target_result for row in requests if row.target_result}
	requirement_names = {
		frappe.db.get_value("APS Schedule Result", name, "net_requirement")
		for name in result_names
		if frappe.db.exists("APS Schedule Result", name)
	}
	requirement_names.discard(None)

	for row in requests:
		for log_name in frappe.get_all(
			"APS Change Application Log",
			filters={"change_request": row.name},
			pluck="name",
		):
			_delete("APS Change Application Log", log_name, allow_immutable=True)
		_delete("APS Change Request", row.name)
	for name in result_names:
		_delete("APS Schedule Result", name)
	for name in requirement_names:
		_delete("APS Net Requirement", name)
	for name in run_names:
		_delete("APS Planning Run", name)
	if delete_user:
		_delete("User", TEST_USER)
	if commit:
		frappe.db.commit()
	return {"deleted_requests": len(requests), "deleted_user": int(bool(delete_user))}


def _create_test_user():
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": TEST_USER,
			"first_name": "APS Phase 3 UI",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	).insert(ignore_permissions=True)
	user.add_roles("System Manager")
	update_password(TEST_USER, TEST_PASSWORD, logout_all_sessions=True)


def _create_analyzed_cancel_fixture(label: str, day_offset: int):
	company = frappe.db.get_value("Company", {})
	customer = frappe.db.get_value("Customer", {})
	item = frappe.db.get_value("Item", {"disabled": 0}) or frappe.db.get_value("Item", {})
	workstation = frappe.db.get_value("Workstation", {})
	if not all((company, customer, item, workstation)):
		frappe.throw("Phase 3 UI fixture needs a Company, Customer, Item, and Workstation.")

	plant_floor = frappe.db.get_value("Workstation", workstation, "plant_floor")
	start = get_datetime(add_days(today(), day_offset)) + timedelta(hours=8)
	due_date = getdate(add_days(today(), day_offset + 3))
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": company,
			"plant_floor": plant_floor,
			"planning_date": today(),
			"horizon_start": start,
			"horizon_end": start + timedelta(days=7),
			"horizon_days": 7,
			"run_type": "Trial",
			"existing_work_order_policy": "Exclude",
			"status": "Planned",
			"approval_state": "Pending",
		}
	).insert(ignore_permissions=True)
	requirement = frappe.get_doc(
		{
			"doctype": "APS Net Requirement",
			"company": company,
			"customer": customer,
			"item_code": item,
			"demand_date": due_date,
			"demand_qty": 100,
			"available_stock_qty": 0,
			"open_work_order_qty": 0,
			"existing_work_order_policy": "Exclude",
			"planning_qty": 100,
			"net_requirement_qty": 100,
			"is_system_generated": 1,
		}
	).insert(ignore_permissions=True)
	result = frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": run.name,
			"company": company,
			"plant_floor": plant_floor,
			"net_requirement": requirement.name,
			"customer": customer,
			"item_code": item,
			"requested_date": due_date,
			"demand_source": "Customer Delivery Schedule",
			"planned_qty": 100,
			"produced_qty": 40,
			"delivered_qty": 20,
			"status": "Planned",
			"risk_status": "Normal",
			"segments": [
				{
					"workstation": workstation,
					"plant_floor": plant_floor,
					"start_time": start,
					"end_time": start + timedelta(hours=1),
					"planned_qty": 40,
					"sequence_no": 1,
					"segment_kind": "Primary",
					"segment_status": "Approved",
					"is_locked": 1,
				},
				{
					"workstation": workstation,
					"plant_floor": plant_floor,
					"start_time": start + timedelta(hours=1),
					"end_time": start + timedelta(hours=2),
					"planned_qty": 60,
					"sequence_no": 2,
					"segment_kind": "Primary",
					"segment_status": "Planned",
					"is_locked": 0,
				},
			],
		}
	).insert(ignore_permissions=True)
	consistency.recalculate_plan_consistency(run.name, reason=f"{MARKER}_{label}")
	request = frappe.get_doc(
		{
			"doctype": "APS Change Request",
			"planning_run": run.name,
			"company": company,
			"plant_floor": plant_floor,
			"change_type": "Cancel",
			"target_result": result.name,
			"item_code": item,
			"customer": customer,
			"qty": 100,
			"target_planned_qty": 0,
			"retained_disposition": "Inventory",
			"notes": f"{MARKER}_{label}",
		}
	).insert(ignore_permissions=True)
	change_engine.analyze_change_request(request.name)
	return {"request": request.name, "result": result.name, "run": run.name}


def _delete(doctype: str, name: str, allow_immutable=False):
	if not name or not frappe.db.exists(doctype, name):
		return
	if doctype == "APS Change Request":
		doc = frappe.get_doc(doctype, name)
		doc.flags.allow_change_engine_delete = True
		doc.delete(ignore_permissions=True, force=True)
		return
	if allow_immutable:
		frappe.flags.in_uninstall = True
	try:
		frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
	finally:
		if allow_immutable:
			frappe.flags.in_uninstall = False


def _assert_test_site():
	if frappe.local.site != TEST_SITE:
		frappe.throw(f"Phase 3 UI fixture may run only on {TEST_SITE}.")
