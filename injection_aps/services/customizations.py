from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from injection_aps.setup.resources import APS_OWNED_RECORDS, STANDARD_CUSTOM_FIELDS, get_standard_custom_field_names
from injection_aps.services.workspace import remove_workspace_resources


APS_TRANSACTION_DOCTYPES = (
	"APS Planning Run",
	"Customer Delivery Schedule",
	"APS Schedule Import Batch",
	"APS Demand Pool",
	"APS Demand Delta",
	"APS Net Requirement",
	"APS Schedule Result",
	"APS Work Order Proposal Batch",
	"APS Shift Schedule Proposal Batch",
	"APS Change Request",
	"APS Release Batch",
	"APS Exception Log",
)


def ensure_standard_customizations():
	create_custom_fields(STANDARD_CUSTOM_FIELDS, update=True)
	frappe.clear_cache()


def ensure_default_settings():
	settings = frappe.get_single("APS Settings")
	settings.default_company = settings.default_company or frappe.defaults.get_user_default("Company")
	settings.planning_horizon_days = settings.planning_horizon_days or 14
	settings.release_horizon_days = settings.release_horizon_days or 3
	settings.freeze_days = settings.freeze_days or 2
	settings.minimum_parallel_split_qty = settings.minimum_parallel_split_qty or 500
	settings.minimum_run_window_hours = settings.minimum_run_window_hours or 2
	settings.default_setup_minutes = settings.default_setup_minutes or 30
	settings.default_first_article_minutes = settings.default_first_article_minutes or 45
	settings.mold_change_penalty_minutes = settings.mold_change_penalty_minutes or 30
	settings.missing_cycle_fallback_seconds = settings.missing_cycle_fallback_seconds or 60
	settings.default_hourly_capacity_qty = settings.default_hourly_capacity_qty or 120
	settings.item_food_grade_field = settings.item_food_grade_field or "custom_food_grade"
	settings.item_first_article_field = settings.item_first_article_field or "custom_is_first_article"
	settings.item_color_field = settings.item_color_field or "color"
	settings.item_material_field = settings.item_material_field or "material"
	settings.item_safety_stock_field = settings.item_safety_stock_field or "safety_stock"
	settings.item_max_stock_field = settings.item_max_stock_field or "max_stock_qty"
	settings.item_min_batch_field = settings.item_min_batch_field or "min_order_qty"
	settings.customer_short_name_field = settings.customer_short_name_field or "custom_customer_short_name"
	settings.workstation_risk_field = settings.workstation_risk_field or "custom_production_risk_category"
	settings.scheduling_item_risk_field = (
		settings.scheduling_item_risk_field or "custom_workstation_risk_category_"
	)
	settings.plant_floor_source_warehouse_field = (
		settings.plant_floor_source_warehouse_field or "custom_default_source_warehouse"
	)
	settings.plant_floor_wip_warehouse_field = settings.plant_floor_wip_warehouse_field or "warehouse"
	settings.plant_floor_fg_warehouse_field = (
		settings.plant_floor_fg_warehouse_field or "custom_default_finished_goods_warehouse"
	)
	settings.plant_floor_scrap_warehouse_field = (
		settings.plant_floor_scrap_warehouse_field or "custom_default_scrap_warehouse"
	)
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)


def ensure_seed_records():
	_ensure_default_freeze_rule()
	sync_machine_capabilities_from_workstations()
	backfill_schedule_scope_defaults()
	frappe.clear_cache()


def ensure_safe_to_uninstall():
	blockers = []

	for doctype in APS_TRANSACTION_DOCTYPES:
		if frappe.db.exists("DocType", doctype) and frappe.db.count(doctype):
			blockers.append(doctype)

	for doctype, fields in STANDARD_CUSTOM_FIELDS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for field in fields:
			fieldname = field.get("fieldname")
			if not fieldname or not frappe.get_meta(doctype).has_field(fieldname):
				continue
			if frappe.db.sql(
				f"""
				select name
				from `tab{doctype}`
				where ifnull(`{fieldname}`, '') != ''
				limit 1
				"""
			):
				blockers.append(f"{doctype}.{fieldname}")

	if blockers:
		raise frappe.ValidationError(
			_(
				"Cannot uninstall Injection APS while APS business data or standard-document APS references still exist: {0}"
			).format(", ".join(blockers))
		)


def remove_standard_customizations():
	for name in get_standard_custom_field_names():
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, force=1, ignore_permissions=True)

	frappe.clear_cache()


def remove_owned_records():
	remove_workspace_resources()

	for doctype, names in APS_OWNED_RECORDS.items():
		for name in names:
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)

	frappe.clear_cache()


def _ensure_default_freeze_rule():
	if not frappe.db.exists("DocType", "APS Freeze Rule"):
		return
	if frappe.db.exists("APS Freeze Rule", {"is_default": 1}):
		return

	settings = frappe.get_single("APS Settings")
	frappe.get_doc(
		{
			"doctype": "APS Freeze Rule",
			"rule_name": "Default Frozen Horizon",
			"freeze_scope": "Global",
			"freeze_days": settings.freeze_days or 2,
			"requires_management_approval": 1,
			"apply_to_released_segments": 1,
			"is_default": 1,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)


def sync_machine_capabilities_from_workstations():
	if not frappe.db.exists("DocType", "APS Machine Capability"):
		return {"created": 0, "updated": 0}
	if not frappe.db.exists("DocType", "Workstation"):
		return {"created": 0, "updated": 0}

	settings = frappe.get_single("APS Settings")
	workstation_meta = frappe.get_meta("Workstation")
	risk_field = settings.workstation_risk_field
	fields = ["name", "plant_floor", "status"]
	if risk_field and workstation_meta.has_field(risk_field):
		fields.append(risk_field)

	workstations = frappe.get_all("Workstation", fields=fields, order_by="name asc")
	existing = {
		row.workstation: row
		for row in frappe.get_all(
			"APS Machine Capability",
			fields=[
				"name",
				"workstation",
				"plant_floor",
				"machine_tonnage",
				"risk_category",
				"machine_status",
				"queue_sequence",
				"is_active",
			],
		)
	}
	created = 0
	updated = 0

	for idx, workstation in enumerate(workstations, start=1):
		values = {
			"workstation": workstation.name,
			"plant_floor": _get_valid_plant_floor(workstation.plant_floor),
			"risk_category": workstation.get(risk_field) if risk_field in workstation else "",
			"machine_status": _normalize_machine_status(workstation.status),
			"last_synced_on": frappe.utils.now_datetime(),
			"sync_source": "Workstation",
		}
		existing_row = existing.get(workstation.name)
		if not existing_row:
			doc = frappe.get_doc(
				{
					"doctype": "APS Machine Capability",
					"machine_tonnage": _extract_tonnage_from_name(workstation.name),
					"hourly_capacity_qty": 0,
					"daily_capacity_qty": 0,
					"queue_sequence": idx,
					"is_active": 1,
					**values,
				}
			)
			doc.insert(ignore_permissions=True)
			created += 1
			continue

		if not frappe.db.exists("APS Machine Capability", existing_row.name):
			continue
		extracted_tonnage = _extract_tonnage_from_name(workstation.name)
		if extracted_tonnage and abs(frappe.utils.flt(existing_row.machine_tonnage) - extracted_tonnage) > 0.001:
			values["machine_tonnage"] = extracted_tonnage
		frappe.db.set_value("APS Machine Capability", existing_row.name, values)
		updated += 1

	return {"created": created, "updated": updated}


def backfill_schedule_scope_defaults():
	if frappe.db.exists("DocType", "Customer Delivery Schedule"):
		frappe.db.sql(
			"""
			update `tabCustomer Delivery Schedule`
			set
				schedule_scope = ifnull(nullif(version_no, ''), 'Default Scope'),
				import_strategy = ifnull(nullif(import_strategy, ''), 'Replace Scope')
			where ifnull(schedule_scope, '') = '' or ifnull(import_strategy, '') = ''
			"""
		)
	if frappe.db.exists("DocType", "APS Schedule Import Batch"):
		frappe.db.sql(
			"""
			update `tabAPS Schedule Import Batch`
			set
				schedule_scope = ifnull(nullif(version_no, ''), 'Default Scope'),
				import_strategy = ifnull(nullif(import_strategy, ''), 'Replace Scope')
			where ifnull(schedule_scope, '') = '' or ifnull(import_strategy, '') = ''
			"""
		)
	if frappe.db.exists("DocType", "APS Demand Delta") and frappe.db.exists("DocType", "Customer Delivery Schedule"):
		frappe.db.sql(
			"""
			update `tabAPS Demand Delta` delta
			inner join `tabCustomer Delivery Schedule` schedule
				on schedule.name = delta.schedule_reference
			set delta.schedule_scope = schedule.schedule_scope
			where ifnull(delta.schedule_scope, '') = ''
			"""
		)


def _extract_tonnage_from_name(workstation_name: str | None) -> float:
	if not workstation_name:
		return 0

	name = str(workstation_name)
	patterns = [
		r"(\d+(?:\.\d+)?)\s*[Tt]\b",
		r"(\d+(?:\.\d+)?)\s*吨",
	]
	for pattern in patterns:
		matches = re.findall(pattern, name)
		if matches:
			return float(matches[-1])

	digits = []
	for token in name.replace("/", " ").replace("_", " ").split():
		filtered = "".join(ch for ch in token if ch.isdigit())
		if filtered:
			digits.append(filtered)

	return float(max(digits, key=len)) if digits else 0


def _normalize_machine_status(workstation_status: str | None) -> str:
	status = (workstation_status or "").strip().lower()
	if status in {"off", "offline", "停机", "停用", "离线"}:
		return "Unavailable"
	if status in {"problem", "fault", "error", "breakdown", "故障", "异常"}:
		return "Fault"
	if any(token in status for token in ("production", "running", "生产", "运行")):
		return "Running"
	if any(token in status for token in ("idle", "available", "ready", "空闲", "可用", "待机", "就绪")):
		return "Available"
	if any(token in status for token in ("setup", "changeover", "换模", "调机", "准备")):
		return "Setup"
	if any(token in status for token in ("maintenance", "maintain", "保养", "维护", "维修")):
		return "Maintenance"
	if any(token in status for token in ("problem", "fault", "error", "breakdown", "故障", "异常", "损坏")):
		return "Fault"
	if any(token in status for token in ("unavailable", "stopped", "stop", "offline", "停机", "停产", "暂停", "离线", "停用")):
		return "Unavailable"
	if any(token in status for token in ("disabled", "禁用")):
		return "Disabled"
	return "Available"


def _get_valid_plant_floor(plant_floor: str | None) -> str | None:
	if not plant_floor:
		return None
	return plant_floor if frappe.db.exists("Plant Floor", plant_floor) else None
import re
