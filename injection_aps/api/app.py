from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.xlsxutils import make_xlsx

from injection_aps.services import planning


def _coerce_export_value(value, fieldtype=None):
	if value in (None, ""):
		return ""
	if fieldtype in {"Float", "Currency", "Percent"}:
		try:
			return float(value)
		except Exception:
			return str(value)
	if fieldtype in {"Int", "Check"}:
		try:
			return int(value)
		except Exception:
			return str(value)
	return str(value)


def _estimate_column_width(label, values):
	width = len(str(label or ""))
	for value in values:
		width = max(width, len(str(value or "")))
	return min(max(width + 2, 12), 42)


@frappe.whitelist()
def export_table_xlsx(payload_json):
	payload = frappe.parse_json(payload_json) if payload_json else {}
	if not isinstance(payload, dict):
		frappe.throw(_("Invalid export payload."))

	columns = payload.get("columns") or []
	rows = payload.get("rows") or []
	if not columns or not rows:
		frappe.throw(_("No rows available to export."))

	title = str(payload.get("title") or _("Export Excel"))
	subtitle = str(payload.get("subtitle") or "")
	sheet_name = str(payload.get("sheet_name") or title)[:28]
	file_name = str(payload.get("file_name") or "aps_export.xlsx")
	if not file_name.lower().endswith(".xlsx"):
		file_name = f"{file_name}.xlsx"

	header_row = [str(column.get("label") or column.get("fieldname") or "") for column in columns]
	fieldnames = [str(column.get("fieldname") or "") for column in columns]
	fieldtypes = [str(column.get("fieldtype") or "") for column in columns]
	column_count = max(len(columns), 1)

	def pad_row(values):
		row_values = list(values)[:column_count]
		if len(row_values) < column_count:
			row_values.extend([""] * (column_count - len(row_values)))
		return row_values

	data = [pad_row([title])]
	if subtitle:
		data.append(pad_row([subtitle]))
	data.append(pad_row([_("Generated On"), now_datetime()]))
	data.append([""] * column_count)
	header_index = len(data)
	data.append(header_row)

	export_rows = []
	for row in rows:
		export_rows.append(
			[
				_coerce_export_value((row or {}).get(fieldname), fieldtype)
				for fieldname, fieldtype in zip(fieldnames, fieldtypes, strict=False)
			]
		)
	data.extend(export_rows)

	column_widths = [
		_estimate_column_width(
			header_row[idx],
			[export_row[idx] for export_row in export_rows],
		)
		for idx in range(len(header_row))
	]

	xlsx_file = make_xlsx(data, sheet_name, column_widths=column_widths, header_index=header_index)
	frappe.local.response.filecontent = xlsx_file.getvalue()
	frappe.local.response.type = "download"
	frappe.local.response.filename = file_name
	frappe.local.response.content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@frappe.whitelist()
def preview_customer_delivery_schedule(customer, company, version_no, file_url=None, rows_json=None):
	return planning.preview_customer_delivery_schedule(
		customer=customer,
		company=company,
		version_no=version_no,
		file_url=file_url,
		rows_json=rows_json,
	)


@frappe.whitelist()
def import_customer_delivery_schedule(customer, company, version_no, file_url=None, rows_json=None, source_type="Customer Delivery Schedule"):
	return planning.import_customer_delivery_schedule(
		customer=customer,
		company=company,
		version_no=version_no,
		file_url=file_url,
		rows_json=rows_json,
		source_type=source_type,
	)


@frappe.whitelist()
def rebuild_demand_pool(company=None):
	return planning.rebuild_demand_pool(company=company)


@frappe.whitelist()
def rebuild_net_requirements(company=None):
	return planning.rebuild_net_requirements(company=company)


@frappe.whitelist()
def run_planning_run(run_name=None, company=None, plant_floor=None, horizon_days=None, item_code=None, customer=None, run_type=None):
	return planning.run_planning_run(
		run_name=run_name,
		company=company,
		plant_floor=plant_floor,
		horizon_days=horizon_days,
		item_code=item_code,
		customer=customer,
		run_type=run_type,
	)


@frappe.whitelist()
def approve_planning_run(run_name):
	return planning.approve_planning_run(run_name)


@frappe.whitelist()
def sync_planning_run_to_execution(run_name):
	return planning.sync_planning_run_to_execution(run_name)


@frappe.whitelist()
def release_planning_run(run_name, release_horizon_days=None):
	return planning.release_planning_run(run_name, release_horizon_days=release_horizon_days)


@frappe.whitelist()
def validate_run_mold_readiness(run_name):
	return planning.validate_run_mold_readiness(run_name, persist_exceptions=True)


@frappe.whitelist()
def generate_work_order_proposals(run_name):
	return planning.generate_work_order_proposals(run_name)


@frappe.whitelist()
def apply_work_order_proposals(batch_name):
	return planning.apply_work_order_proposals(batch_name)


@frappe.whitelist()
def generate_shift_schedule_proposals(run_name=None, work_order_proposal_batch=None, release_horizon_days=None):
	return planning.generate_shift_schedule_proposals(
		run_name=run_name,
		work_order_proposal_batch=work_order_proposal_batch,
		release_horizon_days=release_horizon_days,
	)


@frappe.whitelist()
def apply_shift_schedule_proposals(batch_name):
	return planning.apply_shift_schedule_proposals(batch_name)


@frappe.whitelist()
def update_schedule_notes(result_name=None, segment_name=None, result_note=None, segment_note=None):
	return planning.update_schedule_notes(
		result_name=result_name,
		segment_name=segment_name,
		result_note=result_note,
		segment_note=segment_note,
	)


@frappe.whitelist()
def sync_execution_feedback_to_aps(run_name):
	return planning.sync_execution_feedback_to_aps(run_name)


@frappe.whitelist()
def get_execution_health_for_run(run_name):
	return planning.get_execution_health_for_run(run_name)


@frappe.whitelist()
def analyze_change_request_impact(change_request):
	return planning.analyze_change_request_impact(change_request)


@frappe.whitelist()
def apply_change_request(change_request):
	return planning.apply_change_request(change_request)


@frappe.whitelist()
def analyze_insert_order_impact(company, plant_floor, item_code, qty, required_date, customer=None):
	return planning.analyze_insert_order_impact(
		company=company,
		plant_floor=plant_floor,
		item_code=item_code,
		qty=qty,
		required_date=required_date,
		customer=customer,
	)


@frappe.whitelist()
def rebuild_exceptions(run_name):
	return planning.rebuild_exceptions(run_name)


@frappe.whitelist()
def get_next_actions_for_context(doctype, docname):
	return planning.get_next_actions_for_context(doctype=doctype, docname=docname)


@frappe.whitelist()
def promote_schedule_import_to_net_requirement(import_batch=None, schedule=None, company=None):
	return planning.promote_schedule_import_to_net_requirement(
		import_batch=import_batch,
		schedule=schedule,
		company=company,
	)


@frappe.whitelist()
def create_trial_run_from_net_requirement_context(company=None, plant_floor=None, item_code=None, customer=None, horizon_days=None):
	return planning.create_trial_run_from_net_requirement_context(
		company=company,
		plant_floor=plant_floor,
		item_code=item_code,
		customer=customer,
		horizon_days=horizon_days,
	)


@frappe.whitelist()
def preview_manual_schedule_adjustment(segment_name, target_workstation=None, before_segment_name=None, allow_locked=0, allow_risk_override=0):
	return planning.preview_manual_schedule_adjustment(
		segment_name=segment_name,
		target_workstation=target_workstation,
		before_segment_name=before_segment_name,
		allow_locked=frappe.utils.cint(allow_locked),
		allow_risk_override=frappe.utils.cint(allow_risk_override),
	)


@frappe.whitelist()
def apply_manual_schedule_adjustment(segment_name, target_workstation=None, before_segment_name=None, manual_note=None, allow_locked=0, allow_risk_override=0):
	return planning.apply_manual_schedule_adjustment(
		segment_name=segment_name,
		target_workstation=target_workstation,
		before_segment_name=before_segment_name,
		manual_note=manual_note,
		allow_locked=frappe.utils.cint(allow_locked),
		allow_risk_override=frappe.utils.cint(allow_risk_override),
	)


@frappe.whitelist()
def get_schedule_result_detail(result_name):
	return planning.get_schedule_result_detail(result_name=result_name)


@frappe.whitelist()
def repair_item_references(company=None, include_standard=1, include_aps=1, commit=1):
	return planning.repair_item_references(
		company=company,
		include_standard=frappe.utils.cint(include_standard),
		include_aps=frappe.utils.cint(include_aps),
		commit=frappe.utils.cint(commit),
	)


@frappe.whitelist()
def detach_standard_references(dry_run=1):
	return planning.detach_standard_references(dry_run=frappe.utils.cint(dry_run))


@frappe.whitelist()
def get_workspace_dashboard_data():
	return {
		"active_schedules": frappe.db.count("Customer Delivery Schedule", {"status": "Active"}),
		"open_demands": frappe.db.count("APS Demand Pool", {"status": "Open"}),
		"open_net_requirements": frappe.db.count("APS Net Requirement", {"net_requirement_qty": (">", 0)}),
		"open_runs": frappe.db.count("APS Planning Run", {"status": ("in", planning.RUN_OPEN_STATUSES)}),
		"blocking_exceptions": frappe.db.count("APS Exception Log", {"status": "Open", "severity": ("in", ["Critical", "Blocking"])}),
		"released_batches": frappe.db.count("APS Release Batch", {"status": "Released"}),
		"synced_results": frappe.db.count("APS Schedule Result", {"status": ("in", ["Work Order Proposed", "Shift Proposed", "Applied"])}),
		"machine_capabilities": frappe.db.count("APS Machine Capability", {"is_active": 1}),
	}


@frappe.whitelist()
def get_schedule_console_data(customer=None, company=None):
	schedule_filters = planning._strip_none({"customer": customer, "company": company})
	active_schedules = frappe.get_all(
		"Customer Delivery Schedule",
		filters=schedule_filters,
		fields=[
			"name",
			"customer",
			"company",
			"version_no",
			"source_type",
			"status",
			"schedule_total_qty",
			"modified",
		],
		order_by="modified desc",
		limit=50,
	)
	import_batches = frappe.get_all(
		"APS Schedule Import Batch",
		filters=schedule_filters,
		fields=[
			"name",
			"customer",
			"company",
			"version_no",
			"source_type",
			"status",
			"imported_rows",
			"effective_rows",
			"modified",
		],
		order_by="modified desc",
		limit=50,
	)
	return {
		"active_schedules": active_schedules,
		"import_batches": import_batches,
		"next_actions": {
			row.name: planning.get_next_actions_for_context("APS Schedule Import Batch", row.name)
			for row in import_batches[:10]
		},
		"summary": {
			"active_versions": len([row for row in active_schedules if row.status == "Active"]),
			"recent_batches": len(import_batches),
			"active_qty": sum(frappe.utils.flt(row.schedule_total_qty) for row in active_schedules),
		},
	}


@frappe.whitelist()
def get_net_requirement_page_data(company=None, item_code=None, customer=None):
	filters = planning._strip_none({"company": company, "item_code": item_code, "customer": customer})
	rows = frappe.get_all(
		"APS Net Requirement",
		filters=filters,
		fields=[
			"name",
			"company",
			"customer",
			"item_code",
			"demand_date",
			"demand_qty",
			"available_stock_qty",
			"open_work_order_qty",
			"safety_stock_gap_qty",
			"minimum_batch_qty",
			"planning_qty",
			"net_requirement_qty",
			"reason_text",
		],
		order_by="demand_date asc, item_code asc",
		limit=200,
	)
	return {
		"rows": rows,
		"summary": {
			"rows": len(rows),
			"net_requirement_qty": sum(frappe.utils.flt(row.net_requirement_qty) for row in rows),
			"planning_qty": sum(frappe.utils.flt(row.planning_qty) for row in rows),
		},
		"filters": {"company": company, "item_code": item_code, "customer": customer},
	}


@frappe.whitelist()
def get_run_console_data(company=None, plant_floor=None):
	filters = planning._strip_none({"company": company, "plant_floor": plant_floor})
	runs = frappe.get_all(
		"APS Planning Run",
		filters=filters,
		fields=[
			"name",
			"company",
			"plant_floor",
			"planning_date",
			"status",
			"approval_state",
			"total_net_requirement_qty",
			"total_scheduled_qty",
			"total_unscheduled_qty",
			"exception_count",
			"result_count",
			"notes",
		],
		order_by="modified desc",
		limit=50,
	)
	return {
		"runs": [
			{
				**row,
				"next_actions": planning.get_next_actions_for_context("APS Planning Run", row.name),
				"execution_health": {
					"running": frappe.db.count("APS Schedule Result", {"planning_run": row.name, "actual_status": "Running"}),
					"delayed": frappe.db.count("APS Schedule Result", {"planning_run": row.name, "actual_status": ("in", ["Delayed", "Slow Progress"])}),
					"no_recent_update": frappe.db.count("APS Schedule Result", {"planning_run": row.name, "actual_status": "No Recent Update"}),
				},
			}
			for row in runs
		]
	}


@frappe.whitelist()
def get_schedule_gantt_data(run_name):
	settings = planning.get_settings_dict()
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
			"name",
			"net_requirement",
			"item_code",
			"customer",
			"requested_date",
			"demand_source",
			"risk_status",
			"status",
			"unscheduled_qty",
			"copy_mold_parallel",
			"family_mold_result",
			"primary_mould_reference",
			"selected_moulds",
			"schedule_explanation",
			"flow_step",
			"next_step_hint",
			"blocking_reason",
			"notes",
			"actual_status",
			"actual_progress_qty",
			"actual_start_time",
			"actual_end_time",
			"delay_minutes",
		],
		order_by="modified asc",
	)
	if not results:
		return {"tasks": [], "rows": []}
	item_detail_map = {
		row.item_code: planning._get_item_detail_snapshot(row.item_code, row.customer, settings)
		for row in results
		if row.item_code
	}

	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": ("in", [row.name for row in results])},
		fields=[
			"name",
			"parent",
			"workstation",
			"start_time",
			"end_time",
			"planned_qty",
			"lane_key",
			"parallel_group",
			"family_group",
			"segment_kind",
			"primary_item_code",
			"co_product_item_code",
			"mould_reference",
			"segment_status",
			"is_locked",
			"is_manual",
			"schedule_explanation",
			"risk_flags",
			"segment_note",
			"manual_change_note",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"actual_status",
			"actual_completed_qty",
			"actual_start_time",
			"actual_end_time",
			"delay_minutes",
		],
		order_by="start_time asc",
	)
	exceptions = frappe.get_all(
		"APS Exception Log",
		filters={"planning_run": run_name, "status": "Open"},
		fields=[
			"name",
			"severity",
			"exception_type",
			"message",
			"is_blocking",
			"source_doctype",
			"source_name",
			"workstation",
		],
		order_by="modified desc",
	)
	result_map = {row.name: row for row in results}
	exception_map = {}
	for row in exceptions:
		exception_map.setdefault(row.source_name, []).append(row)
	primary_segment_count = {}
	for row in segments:
		if row.segment_kind != "Family Co-Product":
			primary_segment_count[row.parent] = primary_segment_count.get(row.parent, 0) + 1
	tasks = []
	for row in segments:
		parent = result_map.get(row.parent)
		if not parent:
			continue
		item_detail = item_detail_map.get(parent.item_code) or {}
		risk_rows = (exception_map.get(parent.name) or []) + (exception_map.get(parent.net_requirement) or [])
		tasks.append(
			{
				"id": row.name,
				"name": f"{parent.item_code} / {item_detail.get('item_name') or row.workstation}",
				"start": row.start_time,
				"end": row.end_time,
				"progress": 100 if row.actual_status in ("Completed", "Overproduced") or row.segment_status == "Completed" else 0,
				"custom_class": f"ia-risk-{(parent.risk_status or 'normal').lower()}",
				"details": {
					"segment_name": row.name,
					"result_name": row.parent,
					"item_code": parent.item_code,
					"item_name": item_detail.get("item_name"),
					"customer_reference": item_detail.get("customer_reference"),
					"food_grade": item_detail.get("food_grade"),
					"customer": parent.customer,
					"requested_date": parent.requested_date,
					"demand_source": parent.demand_source,
					"net_requirement": parent.net_requirement,
					"workstation": row.workstation,
					"planned_qty": row.planned_qty,
					"lane_key": row.lane_key,
					"parallel_group": row.parallel_group,
					"family_group": row.family_group,
					"segment_kind": row.segment_kind,
					"primary_item_code": row.primary_item_code,
					"co_product_item_code": row.co_product_item_code,
					"mould_reference": row.mould_reference,
					"segment_status": row.segment_status,
					"is_locked": row.is_locked,
					"is_manual": row.is_manual,
					"copy_mold_parallel": parent.copy_mold_parallel,
					"family_mold_result": parent.family_mold_result,
					"selected_moulds": parent.selected_moulds,
					"schedule_explanation": row.schedule_explanation or parent.schedule_explanation,
					"flow_step": parent.flow_step,
					"next_step_hint": parent.next_step_hint,
					"blocking_reason": parent.blocking_reason,
					"result_note": parent.notes,
					"segment_note": row.segment_note,
					"manual_change_note": row.manual_change_note,
					"risk_flags": row.risk_flags,
					"risk_badges": [risk_row.exception_type for risk_row in risk_rows],
					"actual_status": row.actual_status or parent.actual_status,
					"actual_completed_qty": row.actual_completed_qty,
					"actual_start_time": row.actual_start_time or parent.actual_start_time,
					"actual_end_time": row.actual_end_time or parent.actual_end_time,
					"delay_minutes": row.delay_minutes or parent.delay_minutes,
					"linked_work_order": row.linked_work_order,
					"linked_work_order_scheduling": row.linked_work_order_scheduling,
					"linked_scheduling_item": row.linked_scheduling_item,
					"item_route": item_detail.get("item_route"),
					"result_route": f"Form/APS Schedule Result/{row.parent}",
					"net_requirement_route": f"Form/APS Net Requirement/{parent.net_requirement}" if parent.net_requirement else "",
					"work_order_route": f"Form/Work Order/{row.linked_work_order}" if row.linked_work_order else "",
					"work_order_scheduling_route": f"Form/Work Order Scheduling/{row.linked_work_order_scheduling}" if row.linked_work_order_scheduling else "",
				},
			}
		)
	blocked_results = []
	for row in results:
		item_detail = item_detail_map.get(row.item_code) or {}
		risk_rows = (exception_map.get(row.name) or []) + (exception_map.get(row.net_requirement) or [])
		if primary_segment_count.get(row.name) and row.risk_status not in ("Critical", "Blocked") and not row.unscheduled_qty:
			continue
		blocked_results.append(
			{
				"name": row.name,
				"item_code": row.item_code,
				"item_name": item_detail.get("item_name"),
				"customer": row.customer,
				"requested_date": row.requested_date,
				"demand_source": row.demand_source,
				"risk_status": row.risk_status,
				"status": row.status,
				"unscheduled_qty": row.unscheduled_qty,
				"blocking_reason": row.blocking_reason,
				"exception_types": [risk_row.exception_type for risk_row in risk_rows],
				"result_route": f"Form/APS Schedule Result/{row.name}",
			}
		)
	return {
		"tasks": tasks,
		"rows": segments,
		"blocked_results": blocked_results,
		"run": planning.get_next_actions_for_context("APS Planning Run", run_name),
	}


@frappe.whitelist()
def get_release_center_data(run_name=None):
	batch_filters = planning._strip_none({"planning_run": run_name})
	work_order_proposal_batches = frappe.get_all(
		"APS Work Order Proposal Batch",
		filters=batch_filters,
		fields=[
			"name",
			"planning_run",
			"status",
			"approval_state",
			"proposal_date",
			"proposal_count",
			"applied_count",
		],
		order_by="modified desc",
		limit=50,
	)
	shift_schedule_proposal_batches = frappe.get_all(
		"APS Shift Schedule Proposal Batch",
		filters=batch_filters,
		fields=[
			"name",
			"planning_run",
			"status",
			"approval_state",
			"proposal_date",
			"proposal_count",
			"applied_count",
			"work_order_proposal_batch",
		],
		order_by="modified desc",
		limit=50,
	)
	release_batches = frappe.get_all(
		"APS Release Batch",
		filters=batch_filters,
		fields=[
			"name",
			"planning_run",
			"status",
			"release_from_date",
			"release_to_date",
			"generated_work_orders",
			"work_order_scheduling",
		],
		order_by="modified desc",
		limit=50,
	)
	exception_filters = {"status": "Open"}
	if run_name:
		exception_filters["planning_run"] = run_name
	exceptions = frappe.get_all(
		"APS Exception Log",
		filters=exception_filters,
		fields=[
			"name",
			"planning_run",
			"severity",
			"exception_type",
			"item_code",
			"customer",
			"workstation",
			"message",
			"is_blocking",
			"source_doctype",
			"source_name",
			"resolution_hint",
		],
		order_by="modified desc",
		limit=100,
	)
	run_context = planning.get_next_actions_for_context("APS Planning Run", run_name) if run_name else None
	execution_health = planning.get_execution_health_for_run(run_name) if run_name else None
	return {
		"work_order_proposal_batches": work_order_proposal_batches,
		"shift_schedule_proposal_batches": shift_schedule_proposal_batches,
		"release_batches": release_batches,
		"exceptions": exceptions,
		"run_context": run_context,
		"execution_health": execution_health,
	}
