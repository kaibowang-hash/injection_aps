from __future__ import annotations

import inspect
from collections import defaultdict
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, now_datetime
from frappe.utils.xlsxutils import make_xlsx

from injection_aps.services import availability, capacity_balance, consistency, customizations, delivery_sync, planning
from injection_aps.services.permissions import (
	APS_ADMIN_ROLES,
	APS_APPROVE_ROLES,
	APS_DEMAND_ROLES,
	APS_EXECUTION_ROLES,
	APS_PLAN_ROLES,
	APS_READ_ROLES,
	APS_RELEASE_ROLES,
	require_any_role,
)


MAX_EXPORT_ROWS = 20_000
MAX_EXPORT_COLUMNS = 128
MAX_EXPORT_CELLS = 500_000
MAX_EXPORT_CELL_CHARACTERS = 32_767
MAX_EXPORT_PAYLOAD_BYTES = 25 * 1024 * 1024
EXCEL_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
EXCEL_SHEET_FORBIDDEN_CHARACTERS = frozenset("[]:*?/\\")
EXPORT_FILENAME_SAFE_CHARACTERS = frozenset(" ._-()")
MAX_CHANGE_IMPACT_ROWS = 100
MAX_CHANGE_ANALYSIS_BATCH = 50
CHANGE_REQUEST_STATUSES = (
	"Draft",
	"Analyzed",
	"PMC Confirmed",
	"Approved",
	"Applied",
	"Rejected",
	"Cancelled",
)
CHANGE_REQUEST_BATCH_ANALYSIS_STATUSES = ("Draft", "Analyzed")
PROPOSAL_REVIEW_STATUSES = ("Pending", "Approved", "Rejected")
PROPOSAL_SYSTEM_STATUSES = ("Applied", "Skipped")


def _require_read_access():
	require_any_role(APS_READ_ROLES, _("You need APS read access to view this data."))


def _require_demand_access():
	require_any_role(APS_DEMAND_ROLES, _("You need APS demand access to maintain customer schedules."))


def _require_plan_access():
	require_any_role(APS_PLAN_ROLES, _("You need APS planning access to run this action."))


def _require_approve_access():
	require_any_role(APS_APPROVE_ROLES, _("You need APS approval access to run this action."))


def _require_release_access():
	require_any_role(APS_RELEASE_ROLES, _("You need APS release access to apply formal production changes."))


def _require_execution_access():
	require_any_role(APS_EXECUTION_ROLES, _("You need APS execution access to sync execution feedback."))


def _require_admin_access():
	require_any_role(APS_ADMIN_ROLES, _("You need APS admin access to run this maintenance action."))


def _has_document_access(doctype, docname, ptype="read"):
	if not docname:
		return True
	if frappe.session.user == "Administrator":
		return True
	return bool(frappe.has_permission(doctype, ptype=ptype, doc=docname))


def _require_document_access(doctype, docname, ptype="read"):
	if not docname or not frappe.db.exists(doctype, docname):
		frappe.throw(
			_("{0} {1} was not found.", context="Injection APS").format(_(doctype), docname or "-"),
			frappe.DoesNotExistError,
		)
	if not _has_document_access(doctype, docname, ptype=ptype):
		frappe.throw(
			_("You do not have permission to access {0} {1}.", context="Injection APS").format(
				_(doctype),
				docname,
			),
			frappe.PermissionError,
		)


def _require_explicit_company(company, *, action_label=None):
	"""Require an explicit, readable Company for a company-scoped mutation."""
	company = str(company or "").strip()
	if not company:
		frappe.throw(
			_(
				"Select a Company before running {0}. Company-wide APS changes cannot use an empty Company.",
				context="Injection APS",
			).format(action_label or _("this action", context="Injection APS")),
			frappe.ValidationError,
		)
	_require_document_access("Company", company, ptype="read")
	return company


def _require_sales_order_item_access(sales_order_item, *, sales_order=None, item_code=None):
	"""Validate a Sales Order child identity and its inherited parent permission."""
	if not sales_order_item:
		return
	row = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		["parent", "parenttype", "item_code"],
		as_dict=True,
	) or {}
	if not row or row.get("parenttype") not in (None, "", "Sales Order"):
		frappe.throw(
			_("Sales Order Item {0} was not found.", context="Injection APS").format(
				sales_order_item
			),
			frappe.DoesNotExistError,
		)
	parent = row.get("parent")
	if not parent:
		frappe.throw(
			_("Sales Order Item {0} has no Sales Order parent.", context="Injection APS").format(
				sales_order_item
			),
			frappe.ValidationError,
		)
	if sales_order and parent != sales_order:
		frappe.throw(
			_(
				"Sales Order Item {0} no longer belongs to Sales Order {1}.",
				context="Injection APS",
			).format(sales_order_item, sales_order),
			frappe.ValidationError,
		)
	if item_code and row.get("item_code") and row.get("item_code") != item_code:
		frappe.throw(
			_(
				"Sales Order Item {0} no longer matches Item {1}.",
				context="Injection APS",
			).format(sales_order_item, item_code),
			frappe.ValidationError,
		)
	_require_document_access("Sales Order", parent, ptype="read")
	_require_document_access("Sales Order Item", sales_order_item, ptype="read")


def _require_scope_access(*, company=None, customer=None, planning_run=None, ptype="read"):
	for doctype, docname in (
		("Company", company),
		("Customer", customer),
		("APS Planning Run", planning_run),
	):
		if docname:
			_require_document_access(doctype, docname, ptype=ptype if doctype == "APS Planning Run" else "read")
	if planning_run:
		run_scope = _get_document_scope("APS Planning Run", planning_run)
		for plant_floor in planning._coerce_plant_floor_list(
			plant_floors=run_scope.get("selected_plant_floor_summary"),
			plant_floor=run_scope.get("plant_floor"),
		):
			_require_document_access("Plant Floor", plant_floor, ptype="read")


def _require_change_request_access(change_request, ptype="write", *, target_ptype="read"):
	_require_document_access("APS Change Request", change_request, ptype=ptype)
	scope = frappe.db.get_value(
		"APS Change Request",
		change_request,
		["company", "customer", "planning_run", "target_result"],
		as_dict=True,
	) or {}
	_require_scope_access(
		company=scope.get("company"),
		customer=scope.get("customer"),
		planning_run=scope.get("planning_run"),
		ptype=target_ptype,
	)
	if scope.get("target_result"):
		_require_scoped_document_access(
			"APS Schedule Result",
			scope.get("target_result"),
			ptype=target_ptype,
			linked_run_ptype=target_ptype,
		)


APS_SCOPED_DOCUMENT_FIELDS = {
	"APS Planning Run": ("company", "plant_floor", "selected_plant_floor_summary"),
	"APS Demand Pool": ("company", "customer", "item_code", "sales_order", "sales_order_item"),
	"APS Schedule Result": (
		"company",
		"customer",
		"planning_run",
		"item_code",
		"sales_order",
		"sales_order_item",
		"plant_floor",
		"net_requirement",
	),
	"APS Work Order Proposal Batch": ("company", "planning_run", "plant_floor"),
	"APS Shift Schedule Proposal Batch": ("company", "planning_run", "plant_floor"),
	"APS Release Batch": ("company", "planning_run"),
	"APS Exception Log": ("customer", "planning_run", "item_code", "workstation"),
	"APS Net Requirement": ("company", "customer", "item_code", "sales_order", "sales_order_item"),
	"APS Downtime Window": ("company", "planning_run", "plant_floor", "workstation"),
	"APS Schedule Import Batch": ("company", "customer"),
	"Customer Delivery Schedule": ("company", "customer"),
	"APS Change Request": ("company", "customer", "planning_run", "item_code", "plant_floor"),
}
APS_CONTEXT_DOCTYPES = frozenset((*APS_SCOPED_DOCUMENT_FIELDS, "APS Schedule Segment"))


def _has_linked_document_access(doctype, docname, *, access_cache=None):
	"""Check a linked source, including child rows that inherit parent permission."""
	if not doctype or not docname:
		return True
	cache = access_cache if access_cache is not None else {}
	cache_key = ("linked", doctype, docname, "read")
	if cache_key in cache:
		return cache[cache_key]

	# Exception sources are audit links, not durable business identities.  A source
	# may be retired between the list query and this permission check (notably an
	# APS Net Requirement during a rebuild).  Missing sources must make the linked
	# row inaccessible instead of letting ``frappe.has_permission(doc=name)`` raise
	# DoesNotExistError and break the whole workbench for non-Administrator users.
	try:
		if not frappe.db.exists(doctype, docname):
			cache[cache_key] = False
			return False
		if doctype == "Customer Delivery Schedule Item":
			parent = frappe.db.get_value(doctype, docname, "parent")
			allowed = bool(parent) and _has_scoped_document_access(
				"Customer Delivery Schedule", parent, access_cache=cache
			)
		elif doctype == "Scheduling Item":
			parent = frappe.db.get_value(doctype, docname, "parent")
			allowed = bool(parent) and _has_document_access("Work Order Scheduling", parent, ptype="read")
		elif doctype in APS_CONTEXT_DOCTYPES:
			allowed = _has_scoped_document_access(doctype, docname, access_cache=cache)
		else:
			allowed = _has_document_access(doctype, docname, ptype="read")
	except frappe.DoesNotExistError:
		# Covers the narrow race where the source is deleted after ``exists``.
		allowed = False
	cache[cache_key] = bool(allowed)
	return cache[cache_key]


def _get_document_scope(doctype, docname):
	fields = APS_SCOPED_DOCUMENT_FIELDS.get(doctype) or ()
	if not fields:
		return frappe._dict()
	row = frappe.db.get_value(doctype, docname, list(fields), as_dict=True)
	return frappe._dict(row) if isinstance(row, dict) else frappe._dict()


def _require_scoped_document_access(doctype, docname, ptype="read", *, linked_run_ptype="read"):
	"""Require both record permission and the linked Company/Customer/Run scope.

	Child schedule segments deliberately inherit their access from the parent result;
	they do not have an independent role permission table in Frappe.
	"""
	if doctype == "APS Schedule Segment":
		if not docname or not frappe.db.exists(doctype, docname):
			frappe.throw(
				_("{0} {1} was not found.", context="Injection APS").format(_(doctype), docname or "-"),
				frappe.DoesNotExistError,
			)
		parent = frappe.db.get_value(doctype, docname, "parent")
		_require_scoped_document_access(
			"APS Schedule Result",
			parent,
			ptype=ptype,
			linked_run_ptype=linked_run_ptype,
		)
		return

	_require_document_access(doctype, docname, ptype=ptype)
	scope = _get_document_scope(doctype, docname)
	_require_scope_access(
		company=scope.get("company"),
		customer=scope.get("customer"),
		planning_run=scope.get("planning_run"),
		ptype=linked_run_ptype,
	)
	for fieldname, linked_doctype in (
		("item_code", "Item"),
		("sales_order", "Sales Order"),
		("plant_floor", "Plant Floor"),
		("workstation", "Workstation"),
	):
		if scope.get(fieldname):
			_require_document_access(linked_doctype, scope.get(fieldname), ptype="read")
	if scope.get("sales_order_item"):
		_require_sales_order_item_access(
			scope.get("sales_order_item"),
			sales_order=scope.get("sales_order"),
			item_code=scope.get("item_code"),
		)
	if scope.get("net_requirement"):
		_require_scoped_document_access("APS Net Requirement", scope.get("net_requirement"), ptype="read")
	if doctype == "APS Planning Run":
		for plant_floor in planning._coerce_plant_floor_list(
			plant_floors=scope.get("selected_plant_floor_summary"),
			plant_floor=scope.get("plant_floor"),
		):
			_require_document_access("Plant Floor", plant_floor, ptype="read")


def _has_scoped_document_access(doctype, docname, ptype="read", access_cache=None):
	if not docname:
		return False
	cache = access_cache if access_cache is not None else {}
	key = (doctype, docname, ptype)
	if key in cache:
		return cache[key]
	if doctype == "APS Schedule Segment":
		parent = frappe.db.get_value(doctype, docname, "parent")
		allowed = bool(parent) and _has_scoped_document_access(
			"APS Schedule Result", parent, ptype=ptype, access_cache=cache
		)
		cache[key] = allowed
		return allowed
	if not _has_document_access(doctype, docname, ptype=ptype):
		cache[key] = False
		return False
	scope = _get_document_scope(doctype, docname)
	allowed = all(
		_has_document_access(scope_doctype, scope_name, ptype="read")
		for scope_doctype, scope_name in (
			("Company", scope.get("company")),
			("Customer", scope.get("customer")),
			("APS Planning Run", scope.get("planning_run")),
		)
		if scope_name
	)
	allowed = allowed and all(
		_has_document_access(linked_doctype, scope.get(fieldname), ptype="read")
		for fieldname, linked_doctype in (
			("item_code", "Item"),
			("sales_order", "Sales Order"),
			("plant_floor", "Plant Floor"),
			("workstation", "Workstation"),
		)
		if scope.get(fieldname)
	)
	if allowed and scope.get("sales_order_item"):
		try:
			_require_sales_order_item_access(
				scope.get("sales_order_item"),
				sales_order=scope.get("sales_order"),
				item_code=scope.get("item_code"),
			)
		except (frappe.DoesNotExistError, frappe.PermissionError, frappe.ValidationError):
			allowed = False
	if allowed and scope.get("planning_run"):
		run_scope = _get_document_scope("APS Planning Run", scope.get("planning_run"))
		allowed = all(
			_has_document_access("Plant Floor", plant_floor, ptype="read")
			for plant_floor in planning._coerce_plant_floor_list(
				plant_floors=run_scope.get("selected_plant_floor_summary"),
				plant_floor=run_scope.get("plant_floor"),
			)
		)
	if allowed and scope.get("net_requirement"):
		allowed = _has_scoped_document_access(
			"APS Net Requirement", scope.get("net_requirement"), ptype="read", access_cache=cache
		)
	if allowed and doctype == "APS Planning Run":
		allowed = all(
			_has_document_access("Plant Floor", plant_floor, ptype="read")
			for plant_floor in planning._coerce_plant_floor_list(
				plant_floors=scope.get("selected_plant_floor_summary"),
				plant_floor=scope.get("plant_floor"),
			)
		)
	cache[key] = allowed
	return allowed


def _filter_accessible_documents(rows, doctype, access_cache=None):
	cache = access_cache if access_cache is not None else {}
	return [
		row
		for row in rows or []
		if _has_scoped_document_access(doctype, row.get("name"), access_cache=cache)
	]


def _has_exception_source_access(row, access_cache=None):
	source_doctype = row.get("source_doctype")
	source_name = row.get("source_name")
	if not source_doctype or not source_name:
		return True
	return _has_linked_document_access(
		source_doctype,
		source_name,
		access_cache=access_cache,
	)


def _filter_link_list(value, doctype):
	return "\n".join(
		name
		for name in str(value or "").splitlines()
		if name and _has_document_access(doctype, name, ptype="read")
	)


def _require_planning_reference_access(*, item_code=None, plant_floor=None, plant_floors=None):
	if item_code:
		_require_document_access("Item", item_code, ptype="read")
	for name in planning._coerce_plant_floor_list(
		plant_floors=plant_floors,
		plant_floor=plant_floor,
	):
		_require_document_access("Plant Floor", name, ptype="read")


def _require_all_documents_visible(doctype, filters):
	"""Block company-wide rebuilds when record-level permissions hide any source."""
	all_names = set(
		frappe.get_all(
			doctype,
			filters=filters,
			pluck="name",
			limit_page_length=0,
		)
	)
	if not all_names:
		return
	visible_names = {
		row.get("name")
		for row in frappe.get_list(
			doctype,
			filters=filters,
			fields=["name"],
			limit_page_length=0,
		)
		if row.get("name")
	}
	if visible_names != all_names:
		frappe.throw(
			_(
				"This company-wide APS rebuild includes records outside your permitted scope. Ask an authorized planner to run it.",
				context="Injection APS",
			),
			frappe.PermissionError,
		)


def _require_company_rebuild_scope(company):
	company = _require_explicit_company(
		company,
		action_label=_("company-scoped rebuild", context="Injection APS"),
	)
	filters_by_doctype = (
		("Customer Delivery Schedule", {"company": company, "status": "Active"}),
		(
			"Sales Order",
			{
				"company": company,
				"docstatus": 1,
				"status": ("not in", ["Closed", "Completed", "Cancelled"]),
			},
		),
		("Item", {"disabled": 0}),
		("APS Demand Pool", {"company": company, "status": ("!=", "Cancelled")}),
		("APS Net Requirement", {"company": company}),
	)
	for doctype, filters in filters_by_doctype:
		if frappe.db.exists("DocType", doctype):
			_require_all_documents_visible(doctype, filters)


PROPOSAL_CHILD_DOCTYPES = {
	"APS Work Order Proposal Batch": "APS Work Order Proposal Item",
	"APS Shift Schedule Proposal Batch": "APS Shift Schedule Proposal Item",
}
PROPOSAL_CHILD_SCOPE_FIELDS = {
	"APS Work Order Proposal Item": (
		"name",
		"result_reference",
		"item_code",
		"customer",
		"sales_order",
		"sales_order_item",
		"existing_work_order",
		"target_work_order",
	),
	"APS Shift Schedule Proposal Item": (
		"name",
		"result_reference",
		"segment_reference",
		"item_code",
		"work_order",
		"plant_floor",
		"workstation",
		"existing_scheduling",
		"existing_scheduling_item",
		"target_scheduling",
	),
}


def _require_complete_run_mutation_scope(run_name, *, run_ptype="write"):
	"""Fail closed unless the caller can read every Result and linked demand identity."""
	_require_scoped_document_access(
		"APS Planning Run",
		run_name,
		ptype=run_ptype,
		linked_run_ptype=run_ptype,
	)
	filters = {"planning_run": run_name}
	_require_all_documents_visible("APS Schedule Result", filters)
	result_names = frappe.get_all(
		"APS Schedule Result",
		filters=filters,
		pluck="name",
		limit_page_length=0,
	)
	for result_name in sorted({name for name in result_names if name}):
		# Result rows are engine-managed and ordinarily read-only. The controlled
		# API role plus write permission on the Run authorizes the mutation, while
		# every descendant and its Customer/SO/SOI lineage must remain readable.
		_require_scoped_document_access(
			"APS Schedule Result",
			result_name,
			ptype="read",
			linked_run_ptype="read",
		)


def _require_complete_proposal_batch_scope(batch_doctype, batch_name):
	"""Authorize a proposal transition only when every child identity is visible."""
	child_doctype = PROPOSAL_CHILD_DOCTYPES.get(batch_doctype)
	if not child_doctype:
		frappe.throw(
			_("Unsupported APS proposal batch type.", context="Injection APS"),
			frappe.ValidationError,
		)
	_require_scoped_document_access(batch_doctype, batch_name, ptype="read")
	batch_scope = _get_document_scope(batch_doctype, batch_name)
	if not batch_scope.get("planning_run"):
		frappe.throw(
			_("The APS proposal batch is no longer linked to a Planning Run.", context="Injection APS"),
			frappe.ValidationError,
		)
	run_company = frappe.db.get_value("APS Planning Run", batch_scope.get("planning_run"), "company")
	if not batch_scope.get("company") or batch_scope.get("company") != run_company:
		frappe.throw(
			_(
				"The APS proposal batch Company no longer matches its Planning Run.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	_require_complete_run_mutation_scope(batch_scope.get("planning_run"), run_ptype="write")

	rows = frappe.get_all(
		child_doctype,
		filters={"parent": batch_name, "parenttype": batch_doctype},
		fields=list(PROPOSAL_CHILD_SCOPE_FIELDS[child_doctype]),
		order_by="name asc",
		limit_page_length=0,
	)
	for row in rows:
		if row.get("result_reference"):
			_require_scoped_document_access(
				"APS Schedule Result",
				row.get("result_reference"),
				ptype="read",
				linked_run_ptype="read",
			)
			result_scope = _get_document_scope("APS Schedule Result", row.get("result_reference"))
			if (
				result_scope.get("planning_run") != batch_scope.get("planning_run")
				or result_scope.get("company") != batch_scope.get("company")
			):
				frappe.throw(
					_(
						"A proposal row no longer belongs to the batch Planning Run and Company.",
						context="Injection APS",
					),
					frappe.ValidationError,
				)
		for doctype, docname in (
			("Customer", row.get("customer")),
			("Item", row.get("item_code")),
			("Sales Order", row.get("sales_order")),
			("Plant Floor", row.get("plant_floor")),
			("Workstation", row.get("workstation")),
			("Work Order", row.get("work_order")),
			("Work Order", row.get("existing_work_order")),
			("Work Order", row.get("target_work_order")),
			("Work Order Scheduling", row.get("existing_scheduling")),
			("Scheduling Item", row.get("existing_scheduling_item")),
			("Work Order Scheduling", row.get("target_scheduling")),
		):
			if docname:
				_require_document_access(doctype, docname, ptype="read")
		if row.get("segment_reference"):
			_require_scoped_document_access(
				"APS Schedule Segment",
				row.get("segment_reference"),
				ptype="read",
			)
			if frappe.db.get_value(
				"APS Schedule Segment", row.get("segment_reference"), "parent"
			) != row.get("result_reference"):
				frappe.throw(
					_(
						"A proposal Segment no longer belongs to its APS Result.",
						context="Injection APS",
					),
					frappe.ValidationError,
				)
		if row.get("sales_order_item"):
			_require_sales_order_item_access(
				row.get("sales_order_item"),
				sales_order=row.get("sales_order"),
				item_code=row.get("item_code"),
			)


def _require_schedule_import_reference_access(preview, *, customer, company):
	"""Check every normalized import identity, including inherited SO Item access."""
	company = _require_explicit_company(company, action_label=_("schedule import", context="Injection APS"))
	_require_document_access("Customer", customer, ptype="read")
	if (preview or {}).get("company") != company or (preview or {}).get("customer") != customer:
		frappe.throw(
			_(
				"The schedule preview scope changed. Refresh Preview before importing.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	rows = []
	for key in ("source_rows", "effective_schedule_rows"):
		rows.extend(row for row in (preview or {}).get(key) or [] if isinstance(row, dict))
	items = sorted({str(row.get("item_code") or "").strip() for row in rows if row.get("item_code")})
	orders = sorted({str(row.get("sales_order") or "").strip() for row in rows if row.get("sales_order")})
	for item_code in items:
		_require_document_access("Item", item_code, ptype="read")
	for sales_order in orders:
		_require_document_access("Sales Order", sales_order, ptype="read")

	requested_pairs = {
		(str(row.get("sales_order") or "").strip(), str(row.get("item_code") or "").strip())
		for row in rows
		if row.get("sales_order") and row.get("item_code")
	}
	for start in range(0, len(orders), 500):
		for order_item in frappe.get_all(
			"Sales Order Item",
			filters={
				"parent": ("in", orders[start : start + 500]),
				"parenttype": "Sales Order",
			},
			fields=["name", "parent", "item_code"],
			limit_page_length=0,
		):
			if (order_item.get("parent"), order_item.get("item_code")) not in requested_pairs:
				continue
			_require_sales_order_item_access(
				order_item.get("name"),
				sales_order=order_item.get("parent"),
				item_code=order_item.get("item_code"),
			)
	for row in rows:
		if row.get("sales_order_item"):
			_require_sales_order_item_access(
				row.get("sales_order_item"),
				sales_order=row.get("sales_order"),
				item_code=row.get("item_code"),
			)
	return preview


def _sanitize_schedule_result_detail(detail):
	"""Remove linked source identities the current user cannot read."""
	value = dict(detail or {})
	access_cache = {}
	result = dict(value.get("result") or {})
	# The API has dedicated, permission-filtered collections for these details.
	# Returning the raw child table or engine JSON from ``result.as_dict()`` would
	# reintroduce hidden Work Orders, schedule targets, warehouses, and source rows.
	for fieldname in (
		"segments",
		"demand_source_snapshot_json",
		"fulfillment_baseline_json",
		"capacity_balance_details",
		"primary_mould_reference",
		"selected_moulds",
	):
		result.pop(fieldname, None)
	result["execution_source_documents"] = _filter_link_list(
		result.get("execution_source_documents"), "Stock Entry"
	)
	value["result"] = result

	segments = []
	for source in value.get("segments") or []:
		row = dict(source)
		for fieldname, route_field, doctype in (
			("linked_work_order", "work_order_route", "Work Order"),
			("linked_work_order_scheduling", "work_order_scheduling_route", "Work Order Scheduling"),
			("linked_scheduling_item", "scheduling_item_route", "Scheduling Item"),
			("latest_stock_entry", "latest_stock_entry_route", "Stock Entry"),
		):
			name = row.get(fieldname)
			if name and not _has_linked_document_access(doctype, name, access_cache=access_cache):
				row[fieldname] = ""
				row[route_field] = ""
		row["execution_source_documents"] = _filter_link_list(
			row.get("execution_source_documents"), "Stock Entry"
		)
		segments.append(row)
	value["segments"] = segments

	value["source_rows"] = [
		row
		for row in value.get("source_rows") or []
		if _has_scoped_document_access("APS Demand Pool", row.get("name"), access_cache=access_cache)
		and _has_linked_document_access(
			row.get("source_doctype"), row.get("source_name"), access_cache=access_cache
		)
		and (
			not row.get("sales_order")
			or _has_document_access("Sales Order", row.get("sales_order"), ptype="read")
		)
	]
	value["exception_rows"] = [
		row
		for row in value.get("exception_rows") or []
		if _has_scoped_document_access("APS Exception Log", row.get("name"), access_cache=access_cache)
		and _has_exception_source_access(row, access_cache=access_cache)
	]
	value["mold_rows"] = [
		row
		for row in value.get("mold_rows") or []
		if not row.get("mold") or _has_document_access("Mold", row.get("mold"), ptype="read")
	]
	value["production_allocations"] = [
		row
		for row in value.get("production_allocations") or []
		if _has_linked_document_access(
			"Stock Entry", row.get("source_stock_entry"), access_cache=access_cache
		)
		and _has_linked_document_access(
			"Scheduling Item", row.get("scheduling_item"), access_cache=access_cache
		)
	]
	value["delivery_allocations"] = [
		row
		for row in value.get("delivery_allocations") or []
		if _has_linked_document_access(
			"Delivery Note", row.get("source_delivery_note"), access_cache=access_cache
		)
	]
	fulfillment = dict(value.get("fulfillment_projection") or {})
	fulfillment["production_source_documents"] = [
		name
		for name in fulfillment.get("production_source_documents") or []
		if _has_document_access("Stock Entry", name, ptype="read")
	]
	fulfillment["delivery_source_documents"] = [
		name
		for name in fulfillment.get("delivery_source_documents") or []
		if _has_document_access("Delivery Note", name, ptype="read")
	]
	value["fulfillment_projection"] = fulfillment
	value["next_actions"] = _sanitize_planning_run_context(
		value.get("next_actions"),
		quantity_summary=_summarize_visible_results([result]),
	)
	return value


def _walk_impact_mappings(value):
	if isinstance(value, dict):
		yield value
		for child in value.values():
			yield from _walk_impact_mappings(child)
	elif isinstance(value, (list, tuple)):
		for child in value:
			yield from _walk_impact_mappings(child)


def _impact_preview_is_accessible(preview, *, write_segments=False):
	"""Check every nested identity in an impact; partial previews are unsafe."""
	access_cache = {}
	for row in _walk_impact_mappings(preview or {}):
		segment_names = {
			row.get("segment_name"),
			row.get("segment_reference"),
			row.get("before_segment_name"),
		}
		for segment_name in segment_names:
			if segment_name and not _has_scoped_document_access(
				"APS Schedule Segment",
				segment_name,
				ptype="write" if write_segments else "read",
				access_cache=access_cache,
			):
				return False
		result_names = {
			row.get("result_name"),
			row.get("result_reference"),
			row.get("target_result"),
		}
		if row.get("affected_order") and any(
			key in row for key in ("old_planned_qty", "new_planned_qty", "delay_minutes")
		):
			result_names.add(row.get("affected_order"))
		for result_name in result_names:
			if result_name and not _has_scoped_document_access(
				"APS Schedule Result",
				result_name,
				ptype="write" if write_segments else "read",
				access_cache=access_cache,
			):
				return False
		checks = (
			("Company", row.get("company")),
			("Customer", row.get("customer")),
			("Item", row.get("item_code")),
			("Item", row.get("co_product_item_code")),
			("Plant Floor", row.get("plant_floor")),
			("Workstation", row.get("workstation")),
			("Workstation", row.get("current_workstation")),
			("Workstation", row.get("target_workstation")),
			("Mold", row.get("mould_reference") or row.get("mold")),
			("Mold", row.get("current_mould_reference")),
			("Mold", row.get("target_mould_reference")),
			(
				"Work Order",
				row.get("work_order")
				or row.get("linked_work_order")
				or row.get("existing_work_order")
				or row.get("target_work_order"),
			),
			(
				"Work Order Scheduling",
				row.get("existing_scheduling")
				or row.get("linked_work_order_scheduling")
				or row.get("target_scheduling"),
			),
			("Sales Order", row.get("sales_order")),
			("APS Net Requirement", row.get("target_net_requirement")),
			("APS Planning Run", row.get("planning_run")),
		)
		if any(
			name and not _has_linked_document_access(
				doctype,
				name,
				access_cache=access_cache,
			)
			for doctype, name in checks
		):
			return False
		for workstation in row.get("candidate_workstations") or []:
			if workstation and not _has_linked_document_access(
				"Workstation", workstation, access_cache=access_cache
			):
				return False
		for plant_floor in row.get("selected_plant_floors") or []:
			if plant_floor and not _has_linked_document_access(
				"Plant Floor", plant_floor, access_cache=access_cache
			):
				return False
		source_doctype = row.get("source_doctype")
		source_name = row.get("source_name")
		if source_doctype and source_name and not _has_linked_document_access(
			source_doctype,
			source_name,
			access_cache=access_cache,
		):
			return False
	return True


def _require_impact_preview_access(preview, *, write_segments=False):
	"""Reject an impact preview when it contains any hidden linked record."""
	if _impact_preview_is_accessible(preview, write_segments=write_segments):
		return preview
	frappe.throw(
		_(
			"This impact includes APS records outside your permitted scope. Ask an authorized planner to review it.",
			context="Injection APS",
		),
		frappe.PermissionError,
	)


def _require_change_request_impact_access(change_request, *, write_segments=False):
	stored = frappe.db.get_value(
		"APS Change Request",
		change_request,
		["impact_json", "proposal_json"],
		as_dict=True,
	) or {}
	return _require_impact_preview_access(
		{
			"impact": _parse_json_object(stored.get("impact_json")),
			"proposal": _parse_json_object(stored.get("proposal_json")),
		},
		write_segments=write_segments,
	)


def _lock_planning_run_scope(run_name):
	"""Lock a run and its mutable result/segment rows in the shared lock order."""
	frappe.db.sql(
		"select name from `tabAPS Planning Run` where name = %s for update",
		(run_name,),
	)
	frappe.db.sql(
		"select name from `tabAPS Schedule Result` where planning_run = %s order by name for update",
		(run_name,),
	)
	frappe.db.sql(
		"""
			select seg.name
			from `tabAPS Schedule Segment` seg
			inner join `tabAPS Schedule Result` res on res.name = seg.parent
			where res.planning_run = %s
			order by seg.name
			for update
		""",
		(run_name,),
	)


def _resolve_company_for_scope(company=None):
	return company or frappe.db.get_single_value("APS Settings", "default_company")


def _summarize_visible_results(results):
	field_map = {
		"planned_qty": "planned_qty",
		"machine_scheduled_qty": "machine_scheduled_qty",
		"demand_covered_qty": "demand_covered_qty",
		"overproduction_qty": "overproduction_qty",
		"unscheduled_qty": "unscheduled_qty",
		"produced_qty": "produced_qty",
		"delivered_qty": "delivered_qty",
		"prebuild_qty": "prebuild_qty",
		"jit_qty": "jit_qty",
		"scrap_qty": "scrap_qty",
		"current_deliverable_qty": "current_deliverable_qty",
		"prebuild_inventory_qty": "prebuild_inventory_qty",
		"cancellation_inventory_risk_qty": "cancellation_inventory_risk_qty",
	}
	return {
		target: sum(flt(row.get(source)) for row in results or [])
		for target, source in field_map.items()
	}


def _filter_fulfillment_projection(fulfillment, visible_result_names):
	projection = dict(fulfillment or {})
	visible_names = set(visible_result_names or [])
	rows = [row for row in projection.get("results") or [] if row.get("result") in visible_names]
	warnings = [
		row
		for row in projection.get("warnings") or []
		if not row.get("result") or row.get("result") in visible_names
	]
	projection["results"] = rows
	projection["warnings"] = warnings
	projection["warning_count"] = len(warnings)
	projection["summary"] = {
		"result_count": len(rows),
		"planned_qty": sum(flt(row.get("planned_qty")) for row in rows),
		"prebuild_qty": sum(flt(row.get("prebuild_qty")) for row in rows),
		"jit_qty": sum(flt(row.get("jit_qty")) for row in rows),
		"actual_good_qty": sum(flt(row.get("actual_good_qty")) for row in rows),
		"scrap_qty": sum(flt(row.get("scrap_qty")) for row in rows),
		"current_deliverable_qty": sum(flt(row.get("current_deliverable_qty")) for row in rows),
		"delivered_qty": sum(flt(row.get("delivered_qty")) for row in rows),
		"prebuild_inventory_qty": sum(flt(row.get("prebuild_inventory_qty")) for row in rows),
		"cancellation_inventory_risk_qty": sum(
			flt(row.get("cancellation_inventory_risk_qty")) for row in rows
		),
		"warning_count": len(warnings),
		"warnings": warnings,
	}
	return projection


def _sanitize_planning_run_context(context, *, quantity_summary=None):
	if not context:
		return context
	value = dict(context)
	visible_floors = [
		name
		for name in value.get("selected_plant_floors") or []
		if _has_document_access("Plant Floor", name, ptype="read")
	]
	value["selected_plant_floors"] = visible_floors
	value["selected_plant_floor_summary"] = ", ".join(visible_floors)
	if quantity_summary is not None:
		value["quantity_summary"] = quantity_summary
	for action in value.get("actions") or []:
		if action.get("confirm_summary"):
			action["confirm_summary"] = [
				f"Plant Floors: {value['selected_plant_floor_summary'] or '-'}"
				if str(line).startswith("Plant Floors:")
				else line
				for line in action.get("confirm_summary") or []
			]
	return value


def _build_visible_execution_health(run_name, results):
	status_counts = defaultdict(int)
	for row in results or []:
		status_counts[row.get("actual_status") or "Not Started"] += 1
	result_names = [row.get("name") for row in results or [] if row.get("name")]
	today_text = now_datetime().date().isoformat()
	production_rows = []
	if frappe.session.user == "Administrator" or frappe.has_permission("APS Production Allocation", ptype="read"):
		production_rows = frappe.get_list(
			"APS Production Allocation",
			filters={
				"planning_run": run_name,
				"schedule_result": ("in", result_names or [""]),
				"source_posting_time": (
					"between",
					[f"{today_text} 00:00:00", f"{today_text} 23:59:59.999999"],
				),
			},
			fields=["source_stock_entry"],
			limit_page_length=0,
		)
	today_entries = {
		row.get("source_stock_entry")
		for row in production_rows
		if row.get("source_stock_entry")
		and _has_document_access("Stock Entry", row.get("source_stock_entry"), ptype="read")
	}
	return {
		"run": run_name,
		"status_counts": dict(status_counts),
		"running_segments": status_counts.get("Running", 0),
		"delayed_segments": status_counts.get("Delayed", 0) + status_counts.get("Slow Progress", 0),
		"no_recent_update_segments": status_counts.get("No Recent Update", 0),
		"today_completed_entries": len(today_entries),
	}


def _make_xlsx_compat(data, sheet_name, column_widths=None, header_index=None):
	kwargs = {"column_widths": column_widths}
	if "header_index" in inspect.signature(make_xlsx).parameters:
		kwargs["header_index"] = header_index
	return make_xlsx(data, sheet_name, **kwargs)


def _coerce_export_text(value):
	text = str(value)
	if text.startswith(EXCEL_FORMULA_PREFIXES):
		text = f"'{text}"
	return text[:MAX_EXPORT_CELL_CHARACTERS]


def _sanitize_export_filename(value):
	# Treat both POSIX and Windows separators as paths, then allow only display-
	# safe filename characters so the download header cannot contain CR/LF or
	# ambiguous path components.
	basename = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
	basename = "".join(character for character in basename if ord(character) >= 32 and ord(character) != 127)
	if basename.lower().endswith(".xlsx"):
		basename = basename[:-5]
	basename = "".join(
		character if character.isalnum() or character in EXPORT_FILENAME_SAFE_CHARACTERS else "_"
		for character in basename
	).strip(" ._")
	basename = basename[:195].rstrip(" ._") or "aps_export"
	return f"{basename}.xlsx"


def _sanitize_excel_sheet_name(value):
	name = "".join(
		"_" if character in EXCEL_SHEET_FORBIDDEN_CHARACTERS else character
		for character in str(value or "")
		if ord(character) >= 32 and ord(character) != 127
	).strip()
	return (name or "APS Export")[:31]


def _coerce_export_value(value, fieldtype=None):
	if value in (None, ""):
		return ""
	if fieldtype in {"Float", "Currency", "Percent"}:
		try:
			return float(value)
		except Exception:
			return _coerce_export_text(value)
	if fieldtype in {"Int", "Check"}:
		try:
			return int(value)
		except Exception:
			return _coerce_export_text(value)
	return _coerce_export_text(value)


def _estimate_column_width(label, values):
	width = len(str(label or ""))
	for value in values:
		width = max(width, len(str(value or "")))
	return min(max(width + 2, 12), 42)


def _validate_export_shape(columns, rows):
	if not isinstance(columns, list) or not isinstance(rows, list):
		frappe.throw(_("Export columns and rows must be arrays."), frappe.ValidationError)
	if not columns or not rows:
		frappe.throw(_("No rows available to export.", context="Injection APS"))
	if len(columns) > MAX_EXPORT_COLUMNS:
		frappe.throw(
			_("Exports are limited to {0} columns.").format(MAX_EXPORT_COLUMNS),
			frappe.ValidationError,
		)
	if len(rows) > MAX_EXPORT_ROWS:
		frappe.throw(
			_("Exports are limited to {0} rows.").format(MAX_EXPORT_ROWS),
			frappe.ValidationError,
		)
	if len(columns) * len(rows) > MAX_EXPORT_CELLS:
		frappe.throw(
			_("Exports are limited to {0} data cells.").format(MAX_EXPORT_CELLS),
			frappe.ValidationError,
		)
	if any(not isinstance(column, dict) for column in columns) or any(not isinstance(row, dict) for row in rows):
		frappe.throw(_("Every export column and row must be an object."), frappe.ValidationError)


def _parse_json_object(value):
	if isinstance(value, dict):
		return value
	if not value:
		return {}
	try:
		parsed = frappe.parse_json(value)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _compact_change_impact_row(source_row):
	row = dict(source_row or {})
	impact = _parse_json_object(row.pop("impact_json", None))
	proposal = _parse_json_object(row.pop("proposal_json", None))
	raw_affected_orders = [
		entry for entry in (impact.get("affected_orders") or []) if isinstance(entry, dict)
	]
	affected_orders = []
	for entry in raw_affected_orders:
		result_name = entry.get("result_name")
		if result_name and not _has_scoped_document_access("APS Schedule Result", result_name):
			continue
		if entry.get("customer") and not _has_document_access(
			"Customer", entry.get("customer"), ptype="read"
		):
			continue
		if entry.get("item_code") and not _has_document_access(
			"Item", entry.get("item_code"), ptype="read"
		):
			continue
		affected_orders.append(entry)
	affected_customers = sorted(
		{
			str(entry.get("customer")).strip()
			for entry in affected_orders
			if str(entry.get("customer") or "").strip()
		}
	)
	segment_actions = [
		entry
		for entry in proposal.get("segment_actions") or []
		if isinstance(entry, dict)
		and (
			not entry.get("segment_name")
			or _has_scoped_document_access("APS Schedule Segment", entry.get("segment_name"))
		)
	]
	freeze_conflicts = [
		entry
		for entry in impact.get("freeze_conflicts") or []
		if not isinstance(entry, dict)
		or not entry.get("segment_name")
		or _has_scoped_document_access("APS Schedule Segment", entry.get("segment_name"))
	]
	all_affected_orders_visible = len(affected_orders) == len(raw_affected_orders)
	preview_fields = (
		"affected_order",
		"customer",
		"item_code",
		"due_date",
		"old_completion_time",
		"new_completion_time",
		"delayed_qty",
		"delay_minutes",
	)
	row["affected_orders_preview"] = [
		{fieldname: entry.get(fieldname) for fieldname in preview_fields}
		for entry in affected_orders[:10]
	]
	row["affected_orders_truncated"] = max(len(affected_orders) - 10, 0)
	row["affected_order_count"] = len(affected_orders)
	row["affected_customers"] = affected_customers
	row["affected_customer_count"] = len(affected_customers)
	row["cascading_delay_count"] = sum(
		1 for entry in affected_orders if frappe.utils.flt(entry.get("delay_minutes")) > 0
	)
	row["additional_mold_changes"] = (
		frappe.utils.cint(impact.get("additional_mold_changes"))
		if all_affected_orders_visible
		else 0
	)
	row["freeze_conflict_count"] = len(freeze_conflicts)
	row["segment_action_count"] = len(segment_actions)
	row["allowed"] = frappe.utils.cint(proposal.get("allowed", 1))
	row["blocking"] = frappe.utils.cint(bool(row.get("analysis_fingerprint")) and not row["allowed"])
	row["delayed_qty"] = sum(frappe.utils.flt(entry.get("delayed_qty")) for entry in affected_orders)
	return row


def _summarize_change_impact_rows(rows):
	impacted_customers = set()
	for row in rows or []:
		impacted_customers.update(row.get("affected_customers") or [])
	return {
		"visible_count": len(rows or []),
		"draft_count": sum(1 for row in rows or [] if row.get("status") == "Draft"),
		"analyzed_count": sum(1 for row in rows or [] if row.get("analysis_fingerprint")),
		"blocking_count": sum(frappe.utils.cint(row.get("blocking")) for row in rows or []),
		"affected_order_count": sum(frappe.utils.cint(row.get("affected_order_count")) for row in rows or []),
		"affected_customer_count": len(impacted_customers),
		"delayed_qty": sum(frappe.utils.flt(row.get("delayed_qty")) for row in rows or []),
		"retained_excess_qty": sum(frappe.utils.flt(row.get("retained_excess_qty")) for row in rows or []),
	}


def _normalize_change_request_batch(change_requests):
	value = change_requests
	if isinstance(value, str):
		try:
			value = frappe.parse_json(value)
		except Exception:
			frappe.throw(_("Change Request selection must be a JSON array.", context="Injection APS"), frappe.ValidationError)
	if not isinstance(value, list):
		frappe.throw(_("Change Request selection must be an array.", context="Injection APS"), frappe.ValidationError)
	if len(value) > MAX_CHANGE_ANALYSIS_BATCH:
		frappe.throw(
			_("Select no more than {0} Change Requests for one batch analysis.", context="Injection APS").format(
				MAX_CHANGE_ANALYSIS_BATCH
			),
			frappe.ValidationError,
		)
	if any(not isinstance(name, str) or not name.strip() for name in value):
		frappe.throw(_("Every selected Change Request must have a valid name.", context="Injection APS"), frappe.ValidationError)
	names = sorted(set(name.strip() for name in value))
	if not names:
		frappe.throw(_("Select at least one Change Request.", context="Injection APS"), frappe.ValidationError)
	return names


def _normalize_proposal_row_names(row_names):
	if not row_names:
		return []
	value = row_names
	if isinstance(value, str):
		try:
			value = frappe.parse_json(value)
		except Exception:
			frappe.throw(_("Proposal row selection must be a JSON array.", context="Injection APS"), frappe.ValidationError)
	if not isinstance(value, list) or any(not isinstance(name, str) or not name.strip() for name in value):
		frappe.throw(
			_("Proposal row selection must contain valid row names.", context="Injection APS"),
			frappe.ValidationError,
		)
	return sorted(set(name.strip() for name in value))


def _review_proposal_rows(*, batch_doctype, batch_name, review_status, row_names=None, note=None):
	if review_status not in PROPOSAL_REVIEW_STATUSES:
		frappe.throw(
			_("Unsupported proposal review status: {0}.", context="Injection APS").format(review_status),
			frappe.ValidationError,
		)
	# Proposal batches are immutable audit/execution artifacts in ordinary Desk
	# forms.  The release role plus read access to the scoped batch authorizes this
	# narrowly controlled status transition; the service performs the write with
	# its private engine flag.
	_require_complete_proposal_batch_scope(batch_doctype, batch_name)
	selected_names = _normalize_proposal_row_names(row_names)
	save_point = "aps_proposal_review_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		locked = frappe.db.sql(
			f"select name from `tab{batch_doctype}` where name = %s for update",
			(batch_name,),
		)
		if not locked:
			frappe.throw(_("Proposal batch no longer exists.", context="Injection APS"), frappe.DoesNotExistError)
		_require_complete_proposal_batch_scope(batch_doctype, batch_name)
		batch = frappe.get_doc(batch_doctype, batch_name)
		_require_scope_access(company=batch.get("company"), planning_run=batch.get("planning_run"))
		if batch.get("status") == "Applied" or any(
			row.get("review_status") in PROPOSAL_SYSTEM_STATUSES for row in batch.get("items") or []
		):
			frappe.throw(
				_("Applied or skipped proposal rows cannot be reviewed again.", context="Injection APS"),
				frappe.ValidationError,
			)
		rows_by_name = {row.name: row for row in batch.get("items") or [] if row.name}
		missing = [name for name in selected_names if name not in rows_by_name]
		if missing:
			frappe.throw(
				_("Selected proposal rows no longer exist: {0}.", context="Injection APS").format(", ".join(missing)),
				frappe.ValidationError,
			)
		target_rows = (
			[rows_by_name[name] for name in selected_names]
			if selected_names
			else [
				row
				for row in batch.get("items") or []
				if row.get("review_status") in PROPOSAL_REVIEW_STATUSES
			]
		)
		if not target_rows:
			frappe.throw(_("No reviewable proposal rows were selected.", context="Injection APS"), frappe.ValidationError)
		note_text = str(note or "").strip()[:1000]
		for row in target_rows:
			if row.get("review_status") in PROPOSAL_SYSTEM_STATUSES:
				frappe.throw(
					_("Applied or skipped proposal rows cannot be reviewed again.", context="Injection APS"),
					frappe.ValidationError,
				)
			row.review_status = review_status
			if note_text:
				row.review_note = note_text
		batch.flags.proposal_engine_transition = True
		batch.save(ignore_permissions=True)
		frappe.db.release_savepoint(save_point)
		return {
			"batch": batch.name,
			"review_status": review_status,
			"reviewed_rows": len(target_rows),
			"status": batch.status,
			"approval_state": batch.approval_state,
		}
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def _attach_review_counts(rows, child_doctype):
	rows = rows or []
	names = [row.get("name") for row in rows if row.get("name")]
	if not names:
		return rows
	# Frappe 15 expects string fields in ``get_all`` while Frappe 16's query
	# builder rejects the legacy ``count(name) as count`` string.  Keep this tiny
	# aggregate version-neutral and, importantly, never interpolate a caller-
	# supplied DocType into SQL.
	table_by_doctype = {
		"APS Work Order Proposal Item": "tabAPS Work Order Proposal Item",
		"APS Shift Schedule Proposal Item": "tabAPS Shift Schedule Proposal Item",
	}
	table_name = table_by_doctype.get(child_doctype)
	if not table_name:
		frappe.throw(
			_("Unsupported APS proposal child DocType: {0}.").format(child_doctype or "-"),
			frappe.ValidationError,
		)
	count_rows = frappe.db.sql(
		f"""
		select parent, review_status, count(name) as count
		from `{table_name}`
		where parent in %s
		group by parent, review_status
		""",
		(tuple(names),),
		as_dict=True,
	)
	count_map = defaultdict(dict)
	for item in count_rows:
		count_map[item.get("parent")][item.get("review_status")] = item.get("count") or 0
	for row in rows or []:
		status_counts = count_map.get(row.get("name")) or {}
		row["pending_count"] = status_counts.get("Pending", 0)
		row["approved_count"] = status_counts.get("Approved", 0)
		row["rejected_count"] = status_counts.get("Rejected", 0)
		row["applied_count"] = status_counts.get("Applied", 0)
		row["skipped_count"] = status_counts.get("Skipped", 0)
	return rows


def _format_released_wos_label(row):
	parts = [row.get("work_order_scheduling")]
	meta = " ".join(str(value) for value in [row.get("posting_date"), row.get("shift_type")] if value)
	if meta:
		parts.append(f"({meta})")
	return " ".join(part for part in parts if part)


def _attach_release_wos_details(release_batches):
	release_batches = release_batches or []
	names = [row.get("name") for row in release_batches if row.get("name")]
	child_rows_by_parent = defaultdict(list)
	if names and frappe.db.exists("DocType", "APS Released WOS Item"):
		child_rows = frappe.get_all(
			"APS Released WOS Item",
			filters={"parent": ("in", names)},
			fields=[
				"parent",
				"work_order_scheduling",
				"posting_date",
				"shift_type",
				"total_qty",
				"scheduling_item_count",
				"status",
			],
			order_by="idx asc",
		)
		for child in child_rows:
			if not child.get("work_order_scheduling") or _has_document_access(
				"Work Order Scheduling", child.get("work_order_scheduling"), ptype="read"
			):
				child_rows_by_parent[child.parent].append(child)

	for row in release_batches:
		wos_rows = [dict(child) for child in child_rows_by_parent.get(row.get("name"), [])]
		if (
			not wos_rows
			and row.get("work_order_scheduling")
			and frappe.db.exists("Work Order Scheduling", row.work_order_scheduling)
			and _has_document_access("Work Order Scheduling", row.work_order_scheduling, ptype="read")
		):
			wos = frappe.db.get_value(
				"Work Order Scheduling",
				row.work_order_scheduling,
				["name", "posting_date", "shift_type", "total_qty", "status"],
				as_dict=True,
			)
			if wos:
				wos_rows = [
					{
						"work_order_scheduling": wos.name,
						"posting_date": wos.posting_date,
						"shift_type": wos.shift_type,
						"total_qty": wos.total_qty,
						"scheduling_item_count": frappe.db.count("Scheduling Item", {"parent": wos.name})
						if frappe.db.exists("DocType", "Scheduling Item")
						else 0,
						"status": wos.status,
					}
				]
		for wos_row in wos_rows:
			wos_row["route"] = (
				f"Form/Work Order Scheduling/{wos_row.get('work_order_scheduling')}"
				if wos_row.get("work_order_scheduling")
				else ""
			)
			wos_row["display_name"] = _format_released_wos_label(wos_row)
		row["work_order_schedulings"] = wos_rows
		row["work_order_scheduling_count"] = len(wos_rows)
		row["work_order_scheduling_list"] = ", ".join(wos_row.get("display_name") or "" for wos_row in wos_rows)
	return release_batches


def _build_exception_routes(row):
	run_name = row.get("planning_run")
	source_doctype = row.get("source_doctype")
	source_name = row.get("source_name")
	gantt_params = {"run_name": run_name} if run_name else {}
	if source_doctype == "APS Schedule Segment" and source_name:
		gantt_params["segment_name"] = source_name
	gantt_route = f"aps-schedule-gantt?{urlencode(gantt_params)}" if run_name else ""
	if source_doctype == "APS Schedule Segment":
		source_route = gantt_route
	else:
		source_route = f"Form/{source_doctype}/{source_name}" if source_doctype and source_name else ""
	return {"gantt_route": gantt_route, "source_route": source_route}


@frappe.whitelist(methods=["POST"])
def export_table_xlsx(payload_json):
	_require_read_access()
	if isinstance(payload_json, str) and len(payload_json.encode("utf-8")) > MAX_EXPORT_PAYLOAD_BYTES:
		frappe.throw(_("Export payload exceeds the 25 MB safety limit."), frappe.ValidationError)
	payload = frappe.parse_json(payload_json) if payload_json else {}
	if not isinstance(payload, dict):
		frappe.throw(_("Invalid export payload.", context="Injection APS"))

	columns = payload.get("columns") or []
	rows = payload.get("rows") or []
	_validate_export_shape(columns, rows)

	title = _coerce_export_text(
		payload.get("title") or _("Export Excel", context="Injection APS")
	)
	subtitle = _coerce_export_text(payload.get("subtitle") or "")
	sheet_name = _sanitize_excel_sheet_name(payload.get("sheet_name") or title)
	file_name = _sanitize_export_filename(payload.get("file_name"))

	header_row = [
		_coerce_export_text(column.get("label") or column.get("fieldname") or "")
		for column in columns
	]
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

	xlsx_file = _make_xlsx_compat(data, sheet_name, column_widths=column_widths, header_index=header_index)
	frappe.local.response.filecontent = xlsx_file.getvalue()
	frappe.local.response.type = "download"
	frappe.local.response.filename = file_name
	frappe.local.response.content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@frappe.whitelist()
def inspect_customer_delivery_schedule_file(file_url, sheet_name=None, header_row_no=None, max_rows=None):
	_require_demand_access()
	return planning.inspect_customer_delivery_schedule_file(
		file_url=file_url,
		sheet_name=sheet_name,
		header_row_no=frappe.utils.cint(header_row_no or 0) or None,
		max_rows=frappe.utils.cint(max_rows or 16),
	)


@frappe.whitelist()
def preview_customer_delivery_schedule(
	customer,
	company,
	version_no,
	schedule_scope=None,
	import_strategy=None,
	duplicate_policy=None,
	file_url=None,
	rows_json=None,
	mapping_json=None,
	source_type="Customer Delivery Schedule",
):
	_require_demand_access()
	company = _require_explicit_company(company, action_label=_("schedule preview", context="Injection APS"))
	_require_document_access("Customer", customer, ptype="read")
	_require_scope_access(company=company, customer=customer)
	preview = planning.preview_customer_delivery_schedule(
		customer=customer,
		company=company,
		version_no=version_no,
		schedule_scope=schedule_scope,
		import_strategy=import_strategy,
		duplicate_policy=duplicate_policy,
		file_url=file_url,
		rows_json=rows_json,
		mapping_json=mapping_json,
		source_type=source_type,
	)
	return _require_schedule_import_reference_access(
		preview,
		customer=customer,
		company=company,
	)


@frappe.whitelist(methods=["POST"])
def import_customer_delivery_schedule(
	customer,
	company,
	version_no,
	schedule_scope=None,
	import_strategy=None,
	duplicate_policy=None,
	file_url=None,
	rows_json=None,
	mapping_json=None,
	source_type="Customer Delivery Schedule",
	rebuild=0,
	existing_work_order_policy=None,
	active_state_token=None,
	expected_import_fingerprint=None,
):
	_require_demand_access()
	company = _require_explicit_company(company, action_label=_("schedule import", context="Injection APS"))
	_require_document_access("Customer", customer, ptype="read")
	_require_scope_access(company=company, customer=customer)
	if frappe.utils.cint(rebuild):
		_require_plan_access()
	return planning.import_customer_delivery_schedule(
		customer=customer,
		company=company,
		version_no=version_no,
		schedule_scope=schedule_scope,
		import_strategy=import_strategy,
		duplicate_policy=duplicate_policy,
		file_url=file_url,
		rows_json=rows_json,
		mapping_json=mapping_json,
		source_type=source_type,
		rebuild=frappe.utils.cint(rebuild),
		existing_work_order_policy=existing_work_order_policy,
		active_state_token=active_state_token,
		expected_import_fingerprint=expected_import_fingerprint,
		reference_access_validator=lambda preview: _require_schedule_import_reference_access(
			preview,
			customer=customer,
			company=company,
		),
	)


@frappe.whitelist(methods=["POST"])
def rebuild_demand_pool(company=None):
	_require_plan_access()
	resolved_company = _require_explicit_company(
		company,
		action_label=_("Demand Pool rebuild", context="Injection APS"),
	)
	_require_scope_access(company=resolved_company)
	_require_company_rebuild_scope(resolved_company)
	return planning.rebuild_demand_pool(company=resolved_company)


@frappe.whitelist(methods=["POST"])
def rebuild_net_requirements(company=None, existing_work_order_policy=None):
	_require_plan_access()
	resolved_company = _require_explicit_company(
		company,
		action_label=_("Net Requirement rebuild", context="Injection APS"),
	)
	_require_scope_access(company=resolved_company)
	_require_company_rebuild_scope(resolved_company)
	return planning.rebuild_net_requirements(
		company=resolved_company,
		existing_work_order_policy=existing_work_order_policy,
	)


@frappe.whitelist(methods=["POST"])
def run_planning_run(
	run_name=None,
	company=None,
	plant_floor=None,
	plant_floors=None,
	horizon_days=None,
	item_code=None,
	customer=None,
	run_type=None,
	existing_work_order_policy=None,
):
	_require_plan_access()
	if run_name:
		_require_complete_run_mutation_scope(run_name, run_ptype="write")
		resolved_company = frappe.db.get_value("APS Planning Run", run_name, "company")
		resolved_company = _require_explicit_company(
			resolved_company,
			action_label=_("planning run recalculation", context="Injection APS"),
		)
		if company and str(company).strip() != resolved_company:
			frappe.throw(
				_(
					"The supplied Company does not match the existing Planning Run.",
					context="Injection APS",
				),
				frappe.ValidationError,
			)
	else:
		resolved_company = _require_explicit_company(
			company,
			action_label=_("planning run", context="Injection APS"),
		)
		_require_scope_access(company=resolved_company, customer=customer)
	if run_name and customer:
		_require_document_access("Customer", customer, ptype="read")
	_require_company_rebuild_scope(resolved_company)
	_require_planning_reference_access(
		item_code=item_code,
		plant_floor=plant_floor,
		plant_floors=plant_floors,
	)
	return planning.run_planning_run(
		run_name=run_name,
		company=resolved_company,
		plant_floor=plant_floor,
		plant_floors=plant_floors,
		horizon_days=horizon_days,
		item_code=item_code,
		customer=customer,
		run_type=run_type,
		existing_work_order_policy=existing_work_order_policy,
	)


@frappe.whitelist(methods=["POST"])
def recalculate_plan_consistency(run_name):
	_require_plan_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return consistency.recalculate_plan_consistency(
		run_name,
		reason="manual API recalculation",
	)


@frappe.whitelist(methods=["POST"])
def approve_planning_run(run_name):
	_require_approve_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return planning.approve_planning_run(run_name)


@frappe.whitelist(methods=["POST"])
def sync_planning_run_to_execution(run_name):
	_require_release_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return planning.sync_planning_run_to_execution(run_name)


@frappe.whitelist(methods=["POST"])
def release_planning_run(run_name, release_horizon_days=None):
	_require_release_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return planning.release_planning_run(run_name, release_horizon_days=release_horizon_days)


@frappe.whitelist(methods=["POST"])
def validate_run_mold_readiness(run_name):
	_require_plan_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return planning.validate_run_mold_readiness(run_name, persist_exceptions=True)


@frappe.whitelist(methods=["POST"])
def generate_work_order_proposals(run_name):
	_require_release_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return planning.generate_work_order_proposals(run_name)


@frappe.whitelist(methods=["POST"])
def apply_work_order_proposals(batch_name):
	_require_release_access()
	_require_complete_proposal_batch_scope("APS Work Order Proposal Batch", batch_name)
	return planning.apply_work_order_proposals(batch_name)


@frappe.whitelist(methods=["POST"])
def review_work_order_proposals(batch_name, review_status, row_names=None, note=None):
	_require_release_access()
	return _review_proposal_rows(
		batch_doctype="APS Work Order Proposal Batch",
		batch_name=batch_name,
		review_status=review_status,
		row_names=row_names,
		note=note,
	)


@frappe.whitelist(methods=["POST"])
def reject_work_order_proposals(batch_name, reason):
	_require_release_access()
	_require_complete_proposal_batch_scope("APS Work Order Proposal Batch", batch_name)
	return planning.reject_work_order_proposals(batch_name, reason)


@frappe.whitelist()
def preview_shift_schedule_release(run_name=None, work_order_proposal_batch=None, release_horizon_days=None, release_from_date=None, shift_type=None):
	_require_release_access()
	if run_name:
		_require_scoped_document_access("APS Planning Run", run_name, ptype="read")
	if work_order_proposal_batch:
		_require_scoped_document_access(
			"APS Work Order Proposal Batch", work_order_proposal_batch, ptype="read"
		)
	if not run_name and not work_order_proposal_batch:
		frappe.throw(_("Provide run_name or work_order_proposal_batch."), frappe.ValidationError)
	return planning.preview_shift_schedule_release(
		run_name=run_name,
		work_order_proposal_batch=work_order_proposal_batch,
		release_horizon_days=release_horizon_days,
		release_from_date=release_from_date,
		shift_type=shift_type,
	)


@frappe.whitelist(methods=["POST"])
def generate_shift_schedule_proposals(run_name=None, work_order_proposal_batch=None, release_horizon_days=None, release_from_date=None, shift_type=None):
	_require_release_access()
	if run_name:
		_require_complete_run_mutation_scope(run_name, run_ptype="write")
	if work_order_proposal_batch:
		_require_complete_proposal_batch_scope(
			"APS Work Order Proposal Batch", work_order_proposal_batch
		)
	if not run_name and not work_order_proposal_batch:
		frappe.throw(_("Provide run_name or work_order_proposal_batch."), frappe.ValidationError)
	return planning.generate_shift_schedule_proposals(
		run_name=run_name,
		work_order_proposal_batch=work_order_proposal_batch,
		release_horizon_days=release_horizon_days,
		release_from_date=release_from_date,
		shift_type=shift_type,
	)


@frappe.whitelist(methods=["POST"])
def apply_shift_schedule_proposals(batch_name):
	_require_release_access()
	_require_complete_proposal_batch_scope("APS Shift Schedule Proposal Batch", batch_name)
	return planning.apply_shift_schedule_proposals(batch_name)


@frappe.whitelist(methods=["POST"])
def review_shift_schedule_proposals(batch_name, review_status, row_names=None, note=None):
	_require_release_access()
	return _review_proposal_rows(
		batch_doctype="APS Shift Schedule Proposal Batch",
		batch_name=batch_name,
		review_status=review_status,
		row_names=row_names,
		note=note,
	)


@frappe.whitelist(methods=["POST"])
def reject_shift_schedule_proposals(batch_name, reason):
	_require_release_access()
	_require_complete_proposal_batch_scope("APS Shift Schedule Proposal Batch", batch_name)
	return planning.reject_shift_schedule_proposals(batch_name, reason)


@frappe.whitelist(methods=["POST"])
def update_schedule_notes(result_name=None, segment_name=None, result_note=None, segment_note=None):
	_require_plan_access()
	if not result_name and not segment_name:
		frappe.throw(_("Provide result_name or segment_name."), frappe.ValidationError)
	if result_name:
		_require_scoped_document_access("APS Schedule Result", result_name, ptype="read")
	if segment_name:
		_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
	return planning.update_schedule_notes(
		result_name=result_name,
		segment_name=segment_name,
		result_note=result_note,
		segment_note=segment_note,
	)


@frappe.whitelist(methods=["POST"])
def sync_execution_feedback_to_aps(run_name):
	_require_execution_access()
	# Manufacturing users intentionally have read-only APS records; this API is
	# the controlled path that performs the execution sync on their behalf.
	_require_complete_run_mutation_scope(run_name, run_ptype="read")
	return planning.sync_execution_feedback_to_aps(run_name)


@frappe.whitelist(methods=["POST"])
def sync_delivery_allocations(company, customer=None, item_codes=None):
	_require_execution_access()
	_require_scope_access(company=company, customer=customer)
	if isinstance(item_codes, str):
		item_codes = frappe.parse_json(item_codes) if item_codes.strip().startswith("[") else [item_codes]
	return delivery_sync.sync_delivery_allocations(
		company=company,
		customer=customer,
		item_codes=item_codes or [],
	)


@frappe.whitelist()
def get_fulfillment_projection(run_name=None, result_name=None, as_of=None):
	_require_read_access()
	if result_name:
		_require_scoped_document_access("APS Schedule Result", result_name, ptype="read")
		return availability.get_result_fulfillment_projection(result_name, as_of=as_of)
	if not run_name:
		frappe.throw(_("Provide run_name or result_name."))
	_require_scoped_document_access("APS Planning Run", run_name, ptype="read")
	visible_results = frappe.get_list(
		"APS Schedule Result", filters={"planning_run": run_name}, fields=["name"], limit_page_length=0
	)
	return _filter_fulfillment_projection(
		availability.get_run_fulfillment_projection(run_name, persist=False, as_of=as_of),
		[row.get("name") for row in visible_results],
	)


@frappe.whitelist(methods=["POST"])
def analyze_capacity_balance(run_name):
	_require_plan_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return capacity_balance.analyze_capacity_balance(run_name, persist=True)


@frappe.whitelist(methods=["POST"])
def confirm_capacity_balance(run_name):
	_require_plan_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return capacity_balance.confirm_capacity_balance(run_name)


@frappe.whitelist(methods=["POST"])
def apply_capacity_balance(run_name, pmc_confirmed=0):
	_require_plan_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return capacity_balance.apply_capacity_balance(
		run_name,
		pmc_confirmed=bool(frappe.utils.cint(pmc_confirmed)),
	)


@frappe.whitelist(methods=["POST"])
def get_execution_health_for_run(run_name, sync=0):
	_require_execution_access()
	if frappe.utils.cint(sync):
		_require_complete_run_mutation_scope(run_name, run_ptype="read")
		planning.get_execution_health_for_run(run_name, sync=1)
	else:
		_require_scoped_document_access("APS Planning Run", run_name, ptype="read")
	visible_results = frappe.get_list(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=["name", "actual_status"],
		limit_page_length=0,
	)
	visible_results = _filter_accessible_documents(visible_results, "APS Schedule Result")
	return _build_visible_execution_health(run_name, visible_results)


@frappe.whitelist(methods=["POST"])
def sync_machine_capabilities_from_workstations():
	_require_admin_access()
	return customizations.sync_machine_capabilities_from_workstations()


@frappe.whitelist(methods=["POST"])
def analyze_change_request_impact(change_request):
	_require_demand_access()
	_require_change_request_access(change_request, ptype="write")
	return _require_impact_preview_access(
		planning.analyze_change_request_impact(change_request)
	)


@frappe.whitelist()
def get_change_impact_center_data(
	company=None,
	planning_run=None,
	status=None,
	customer=None,
	item_code=None,
	limit=50,
):
	_require_read_access()
	_require_scope_access(company=company, customer=customer, planning_run=planning_run)
	if status and status not in CHANGE_REQUEST_STATUSES:
		frappe.throw(
			_("Unsupported Change Request status: {0}.", context="Injection APS").format(status),
			frappe.ValidationError,
		)
	filters = {
		fieldname: value
		for fieldname, value in {
			"company": company,
			"planning_run": planning_run,
			"status": status,
			"customer": customer,
			"item_code": item_code,
		}.items()
		if value
	}
	page_length = min(max(frappe.utils.cint(limit or 50), 1), MAX_CHANGE_IMPACT_ROWS)
	rows = frappe.get_list(
		"APS Change Request",
		filters=filters,
		fields=[
			"name",
			"planning_run",
			"company",
			"plant_floor",
			"change_type",
			"status",
			"approval_state",
			"item_code",
			"customer",
			"current_required_date",
			"required_date",
			"current_planned_qty",
			"target_planned_qty",
			"minimum_retained_qty",
			"retained_excess_qty",
			"impact_summary",
			"analysis_revision",
			"analysis_fingerprint",
			"analyzed_on",
			"modified",
		],
		order_by="modified desc, name desc",
		limit_page_length=page_length,
	)
	rows = _filter_accessible_documents(rows, "APS Change Request")
	visible_names = [row.get("name") for row in rows if row.get("name")]
	raw_by_name = {
		row.get("name"): row
		for row in (
			frappe.get_all(
				"APS Change Request",
				filters={"name": ("in", visible_names)},
				fields=["name", "impact_json", "proposal_json"],
				limit_page_length=0,
			)
			if visible_names
			else []
		)
	}
	compact_rows = []
	for row in rows:
		if not _has_scoped_document_access("APS Change Request", row.get("name")):
			continue
		raw = raw_by_name.get(row.get("name")) or {}
		impact = _parse_json_object(raw.get("impact_json"))
		proposal = _parse_json_object(raw.get("proposal_json"))
		if not _impact_preview_is_accessible({"impact": impact, "proposal": proposal}):
			continue
		row["impact_json"] = raw.get("impact_json")
		row["proposal_json"] = raw.get("proposal_json")
		compact_rows.append(_compact_change_impact_row(row))
	return {
		"rows": compact_rows,
		"summary": _summarize_change_impact_rows(compact_rows),
		"limit": page_length,
		"may_have_more": frappe.utils.cint(len(compact_rows) >= page_length),
	}


@frappe.whitelist(methods=["POST"])
def batch_analyze_change_requests(change_requests):
	_require_demand_access()
	names = _normalize_change_request_batch(change_requests)
	for name in names:
		_require_change_request_access(name, ptype="write")
	save_point = "aps_change_batch_analyze_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		placeholders = ", ".join(["%s"] * len(names))
		locked_rows = frappe.db.sql(
			f"""
				select name, status
				from `tabAPS Change Request`
				where name in ({placeholders})
				order by name
				for update
			""",
			tuple(names),
			as_dict=True,
		)
		locked_by_name = {row.get("name"): row for row in locked_rows}
		missing = [name for name in names if name not in locked_by_name]
		if missing:
			frappe.throw(
				_("Selected Change Requests no longer exist: {0}.", context="Injection APS").format(
					", ".join(missing)
				),
				frappe.DoesNotExistError,
			)
		without_write_access = [
			name
			for name in names
			if not frappe.has_permission(
				"APS Change Request",
				ptype="write",
				doc=name,
			)
		]
		if without_write_access:
			frappe.throw(
				_("You do not have write permission for these Change Requests: {0}.", context="Injection APS").format(
					", ".join(without_write_access)
				),
				frappe.PermissionError,
			)
		not_analyzable = [
			"{0} ({1})".format(name, locked_by_name[name].get("status") or "-")
			for name in names
			if locked_by_name[name].get("status") not in CHANGE_REQUEST_BATCH_ANALYSIS_STATUSES
		]
		if not_analyzable:
			frappe.throw(
				_("Batch analysis accepts only Draft or Analyzed requests: {0}.", context="Injection APS").format(
					", ".join(not_analyzable)
				),
				frappe.ValidationError,
			)

		results = []
		for name in names:
			result = _require_impact_preview_access(
				planning.analyze_change_request_impact(name)
			)
			results.append(
				{
					"change_request": result.get("change_request") or name,
					"status": result.get("status"),
					"analysis_revision": frappe.utils.cint(result.get("analysis_revision")),
					"allowed": frappe.utils.cint(result.get("allowed", 1)),
				}
			)
		frappe.db.release_savepoint(save_point)
		return {
			"analyzed_count": len(results),
			"blocking_count": sum(1 for result in results if not result.get("allowed")),
			"results": results,
		}
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


@frappe.whitelist(methods=["POST"])
def confirm_change_request(change_request):
	_require_plan_access()
	_require_change_request_access(change_request, ptype="write")
	_require_change_request_impact_access(change_request)
	return planning.confirm_change_request(change_request)


@frappe.whitelist(methods=["POST"])
def approve_change_request(change_request):
	_require_approve_access()
	_require_change_request_access(change_request, ptype="write")
	_require_change_request_impact_access(change_request)
	return planning.approve_change_request(change_request)


@frappe.whitelist(methods=["POST"])
def reject_change_request(change_request, reason=None):
	_require_approve_access()
	_require_change_request_access(change_request, ptype="write")
	_require_change_request_impact_access(change_request)
	return planning.reject_change_request(change_request, reason=reason)


@frappe.whitelist(methods=["POST"])
def apply_change_request(change_request):
	_require_approve_access()
	# Results and Segments are engine-managed/read-only. A controlled change is
	# authorized by write access to the Change Request and its complete Run scope,
	# while every affected Result/Segment and demand identity must remain readable.
	_require_change_request_access(change_request, ptype="write", target_ptype="read")
	change_scope = frappe.db.get_value(
		"APS Change Request",
		change_request,
		["planning_run", "target_result"],
		as_dict=True,
	) or {}
	mutation_run = change_scope.get("planning_run")
	if not mutation_run and change_scope.get("target_result"):
		mutation_run = frappe.db.get_value(
			"APS Schedule Result", change_scope.get("target_result"), "planning_run"
		)
	if mutation_run:
		_require_complete_run_mutation_scope(mutation_run, run_ptype="write")
	_require_change_request_impact_access(change_request)
	return planning.apply_change_request(change_request)


@frappe.whitelist()
def analyze_insert_order_impact(company, plant_floor=None, plant_floors=None, item_code=None, qty=None, required_date=None, customer=None):
	_require_read_access()
	_require_scope_access(company=company, customer=customer)
	_require_planning_reference_access(
		item_code=item_code,
		plant_floor=plant_floor,
		plant_floors=plant_floors,
	)
	return _require_impact_preview_access(planning.analyze_insert_order_impact(
		company=company,
		plant_floor=plant_floor,
		plant_floors=plant_floors,
		item_code=item_code,
		qty=qty,
		required_date=required_date,
		customer=customer,
	))


@frappe.whitelist(methods=["POST"])
def rebuild_exceptions(run_name):
	_require_plan_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	return planning.rebuild_exceptions(run_name)


@frappe.whitelist()
def get_next_actions_for_context(doctype, docname):
	_require_read_access()
	if doctype not in APS_CONTEXT_DOCTYPES:
		frappe.throw(_("Unsupported APS context DocType."), frappe.ValidationError)
	_require_scoped_document_access(doctype, docname, ptype="read")
	context = planning.get_next_actions_for_context(doctype=doctype, docname=docname)
	if doctype == "APS Planning Run":
		visible_results = frappe.get_list(
			"APS Schedule Result",
			filters={"planning_run": docname},
			fields=[
				"name",
				"planned_qty",
				"machine_scheduled_qty",
				"demand_covered_qty",
				"overproduction_qty",
				"unscheduled_qty",
				"produced_qty",
				"delivered_qty",
				"prebuild_qty",
				"jit_qty",
				"scrap_qty",
				"current_deliverable_qty",
				"prebuild_inventory_qty",
				"cancellation_inventory_risk_qty",
			],
			limit_page_length=0,
		)
		visible_results = _filter_accessible_documents(visible_results, "APS Schedule Result")
		context = _sanitize_planning_run_context(
			context, quantity_summary=_summarize_visible_results(visible_results)
		)
	return context


@frappe.whitelist(methods=["POST"])
def promote_schedule_import_to_net_requirement(
	import_batch=None,
	schedule=None,
	company=None,
	existing_work_order_policy=None,
):
	_require_plan_access()
	company = str(company or "").strip() or None
	if import_batch:
		_require_scoped_document_access("APS Schedule Import Batch", import_batch, ptype="read")
	if schedule:
		_require_scoped_document_access("Customer Delivery Schedule", schedule, ptype="read")
	if not import_batch and not schedule and not company:
		frappe.throw(_("Provide import_batch, schedule, or company."), frappe.ValidationError)
	if company:
		_require_scope_access(company=company)
	batch_company = (
		frappe.db.get_value("APS Schedule Import Batch", import_batch, "company") if import_batch else None
	)
	schedule_company = (
		frappe.db.get_value("Customer Delivery Schedule", schedule, "company") if schedule else None
	)
	source_companies = {value for value in (batch_company, schedule_company) if value}
	if len(source_companies) > 1 or (company and source_companies and company not in source_companies):
		frappe.throw(
			_(
				"The selected schedule sources do not belong to the supplied Company.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	resolved_company = company or batch_company or schedule_company
	resolved_company = _require_explicit_company(
		resolved_company,
		action_label=_("schedule promotion", context="Injection APS"),
	)
	_require_company_rebuild_scope(resolved_company)
	return planning.promote_schedule_import_to_net_requirement(
		import_batch=import_batch,
		schedule=schedule,
		company=resolved_company,
		existing_work_order_policy=existing_work_order_policy,
	)


@frappe.whitelist(methods=["POST"])
def create_trial_run_from_net_requirement_context(
	company=None,
	plant_floor=None,
	plant_floors=None,
	item_code=None,
	customer=None,
	horizon_days=None,
	existing_work_order_policy=None,
):
	_require_plan_access()
	resolved_company = _require_explicit_company(
		company,
		action_label=_("trial Planning Run", context="Injection APS"),
	)
	_require_scope_access(company=resolved_company, customer=customer)
	_require_company_rebuild_scope(resolved_company)
	_require_planning_reference_access(
		item_code=item_code,
		plant_floor=plant_floor,
		plant_floors=plant_floors,
	)
	return planning.create_trial_run_from_net_requirement_context(
		company=resolved_company,
		plant_floor=plant_floor,
		plant_floors=plant_floors,
		item_code=item_code,
		customer=customer,
		horizon_days=horizon_days,
		existing_work_order_policy=existing_work_order_policy,
	)


@frappe.whitelist()
def preview_manual_schedule_adjustment(
	segment_name,
	target_workstation=None,
	before_segment_name=None,
	target_start_time=None,
	target_end_time=None,
	target_qty=None,
	allow_locked=0,
	allow_risk_override=0,
	allow_overproduction=0,
):
	_require_plan_access()
	_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
	if before_segment_name:
		_require_scoped_document_access("APS Schedule Segment", before_segment_name, ptype="read")
	if target_workstation:
		_require_document_access("Workstation", target_workstation, ptype="read")
	return _require_impact_preview_access(planning.preview_manual_schedule_adjustment(
		segment_name=segment_name,
		target_workstation=target_workstation,
		before_segment_name=before_segment_name,
		target_start_time=target_start_time,
		target_end_time=target_end_time,
		target_qty=frappe.utils.flt(target_qty) if target_qty not in (None, "") else None,
		allow_locked=frappe.utils.cint(allow_locked),
		allow_risk_override=frappe.utils.cint(allow_risk_override),
		allow_overproduction=frappe.utils.cint(allow_overproduction),
	))


@frappe.whitelist(methods=["POST"])
def apply_manual_schedule_adjustment(
	segment_name,
	target_workstation=None,
	before_segment_name=None,
	target_start_time=None,
	target_end_time=None,
	target_qty=None,
	manual_note=None,
	allow_locked=0,
	allow_risk_override=0,
	allow_overproduction=0,
):
	_require_release_access()
	_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
	if before_segment_name:
		_require_scoped_document_access("APS Schedule Segment", before_segment_name, ptype="read")
	if target_workstation:
		_require_document_access("Workstation", target_workstation, ptype="read")
	result_name = frappe.db.get_value("APS Schedule Segment", segment_name, "parent")
	locked_run_name = frappe.db.get_value("APS Schedule Result", result_name, "planning_run")
	if locked_run_name:
		_lock_planning_run_scope(locked_run_name)
		_require_complete_run_mutation_scope(locked_run_name, run_ptype="write")
		_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
		if before_segment_name:
			_require_scoped_document_access("APS Schedule Segment", before_segment_name, ptype="read")
	preview = planning.preview_manual_schedule_adjustment(
		segment_name=segment_name,
		target_workstation=target_workstation,
		before_segment_name=before_segment_name,
		target_start_time=target_start_time,
		target_end_time=target_end_time,
		target_qty=frappe.utils.flt(target_qty) if target_qty not in (None, "") else None,
		allow_locked=frappe.utils.cint(allow_locked),
		allow_risk_override=frappe.utils.cint(allow_risk_override),
		allow_overproduction=frappe.utils.cint(allow_overproduction),
	)
	_require_impact_preview_access(preview)
	return planning.apply_manual_schedule_adjustment(
		segment_name=segment_name,
		target_workstation=target_workstation,
		before_segment_name=before_segment_name,
		target_start_time=target_start_time,
		target_end_time=target_end_time,
		target_qty=frappe.utils.flt(target_qty) if target_qty not in (None, "") else None,
		manual_note=manual_note,
		allow_locked=frappe.utils.cint(allow_locked),
		allow_risk_override=frappe.utils.cint(allow_risk_override),
		allow_overproduction=frappe.utils.cint(allow_overproduction),
	)


@frappe.whitelist()
def preview_segment_split(segment_name, split_time=None, split_qty=None, downtime_window=None, split_reason=None):
	_require_plan_access()
	_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
	if downtime_window:
		_require_scoped_document_access("APS Downtime Window", downtime_window, ptype="read")
	return planning.preview_segment_split(
		segment_name=segment_name,
		split_time=split_time,
		split_qty=frappe.utils.flt(split_qty) if split_qty not in (None, "") else None,
		downtime_window=downtime_window,
		split_reason=split_reason,
	)


@frappe.whitelist(methods=["POST"])
def apply_segment_split(segment_name, split_time=None, split_qty=None, downtime_window=None, split_reason=None):
	_require_release_access()
	# Schedule Results/Segments are engine-managed and deliberately have no
	# ordinary write permission. The controlled mutation is authorized by the
	# release role plus write access to the complete Planning Run scope.
	_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
	if downtime_window:
		_require_scoped_document_access("APS Downtime Window", downtime_window, ptype="write")
	initial_result = frappe.db.get_value("APS Schedule Segment", segment_name, "parent")
	initial_run = frappe.db.get_value("APS Schedule Result", initial_result, "planning_run")
	if not initial_result or not initial_run:
		frappe.throw(
			_("The APS Segment is no longer linked to a Planning Run.", context="Injection APS"),
			frappe.ValidationError,
		)
	_lock_planning_run_scope(initial_run)
	locked_result = frappe.db.get_value("APS Schedule Segment", segment_name, "parent")
	locked_run = frappe.db.get_value("APS Schedule Result", locked_result, "planning_run")
	if locked_result != initial_result or locked_run != initial_run:
		frappe.throw(
			_(
				"The APS Segment scope changed while acquiring the planning lock. Refresh and retry.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	_require_complete_run_mutation_scope(locked_run, run_ptype="write")
	_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
	if downtime_window:
		_require_scoped_document_access("APS Downtime Window", downtime_window, ptype="write")
	return planning.apply_segment_split(
		segment_name=segment_name,
		split_time=split_time,
		split_qty=frappe.utils.flt(split_qty) if split_qty not in (None, "") else None,
		downtime_window=downtime_window,
		split_reason=split_reason,
	)


@frappe.whitelist(methods=["POST"])
def create_or_update_downtime_window(name=None, company=None, scope=None, plant_floor=None, workstation=None, start_time=None, end_time=None, available_capacity_percent=None, reason=None, status="Active", planning_run=None, notes=None):
	_require_release_access()
	current = frappe._dict()
	if name:
		_require_scoped_document_access("APS Downtime Window", name, ptype="write")
		current = frappe._dict(
			frappe.db.get_value(
				"APS Downtime Window",
				name,
				["company", "planning_run", "start_time", "end_time"],
				as_dict=True,
			)
			or {}
		)
	resolved_run = planning_run or current.get("planning_run")
	run_company = (
		frappe.db.get_value("APS Planning Run", resolved_run, "company") if resolved_run else None
	)
	resolved_company = _require_explicit_company(
		company or run_company or current.get("company"),
		action_label=_("downtime update", context="Injection APS"),
	)
	if run_company and run_company != resolved_company:
		frappe.throw(
			_("The downtime Company does not match its Planning Run.", context="Injection APS"),
			frappe.ValidationError,
		)
	effective_start = get_datetime(start_time or current.get("start_time"))
	effective_end = get_datetime(end_time or current.get("end_time"))
	if not effective_start or not effective_end:
		frappe.throw(
			_("Downtime Start Time and End Time are required.", context="Injection APS"),
			frappe.ValidationError,
		)
	affected_runs = set()
	window_scopes = []
	if name:
		window_scopes.append(
			{
				"company": current.get("company"),
				"start_time": get_datetime(current.get("start_time")),
				"end_time": get_datetime(current.get("end_time")),
			}
		)
	window_scopes.append(
		{
			"company": resolved_company,
			"start_time": effective_start,
			"end_time": effective_end,
		}
	)
	for window_scope in window_scopes:
		if not all(
			window_scope.get(fieldname) for fieldname in ("company", "start_time", "end_time")
		):
			continue
		affected_runs.update(
			frappe.get_all(
				"APS Planning Run",
				filters={
					"company": window_scope["company"],
					"status": ("!=", "Closed"),
					"horizon_start": ("<", window_scope["end_time"]),
					"horizon_end": (">", window_scope["start_time"]),
				},
				pluck="name",
				limit_page_length=0,
			)
		)
	if resolved_run:
		affected_runs.add(resolved_run)
	for affected_run in sorted(name for name in affected_runs if name):
		_require_complete_run_mutation_scope(affected_run, run_ptype="write")
	if plant_floor:
		_require_document_access("Plant Floor", plant_floor, ptype="read")
	if workstation:
		_require_document_access("Workstation", workstation, ptype="read")
	return planning.create_or_update_downtime_window(
		name=name,
		company=resolved_company,
		scope=scope,
		plant_floor=plant_floor,
		workstation=workstation,
		start_time=start_time,
		end_time=end_time,
		available_capacity_percent=available_capacity_percent,
		reason=reason,
		status=status,
		planning_run=planning_run,
		notes=notes,
	)


@frappe.whitelist()
def preview_schedule_impact(run_name=None, downtime_window=None, segment_name=None):
	_require_plan_access()
	if run_name:
		_require_scoped_document_access("APS Planning Run", run_name, ptype="read")
	if downtime_window:
		_require_scoped_document_access("APS Downtime Window", downtime_window, ptype="read")
	if segment_name:
		_require_scoped_document_access("APS Schedule Segment", segment_name, ptype="read")
	if not run_name and not downtime_window and not segment_name:
		frappe.throw(_("Provide run_name, downtime_window, or segment_name."), frappe.ValidationError)
	return _require_impact_preview_access(planning.preview_schedule_impact(
		run_name=run_name,
		downtime_window=downtime_window,
		segment_name=segment_name,
	))


@frappe.whitelist(methods=["POST"])
def apply_schedule_impact(run_name, downtime_window=None):
	_require_release_access()
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	if downtime_window:
		_require_scoped_document_access("APS Downtime Window", downtime_window, ptype="write")
	_lock_planning_run_scope(run_name)
	_require_complete_run_mutation_scope(run_name, run_ptype="write")
	preview = planning.preview_schedule_impact(run_name=run_name, downtime_window=downtime_window)
	_require_impact_preview_access(preview)
	return planning.apply_schedule_impact(run_name=run_name, downtime_window=downtime_window)


@frappe.whitelist()
def get_schedule_result_detail(result_name):
	_require_read_access()
	_require_scoped_document_access("APS Schedule Result", result_name, ptype="read")
	return _sanitize_schedule_result_detail(
		planning.get_schedule_result_detail(result_name=result_name)
	)


@frappe.whitelist()
def get_exception_resolution_context(exception_name):
	_require_read_access()
	_require_scoped_document_access("APS Exception Log", exception_name, ptype="read")
	source_row = frappe.db.get_value(
		"APS Exception Log", exception_name, ["source_doctype", "source_name"], as_dict=True
	)
	source = source_row if isinstance(source_row, dict) else {}
	if source.get("source_doctype") and source.get("source_name") and not _has_linked_document_access(
		source.get("source_doctype"), source.get("source_name")
	):
		frappe.throw(
			_("You do not have permission to view the exception source.", context="Injection APS"),
			frappe.PermissionError,
		)
	context = planning.get_exception_resolution_context(exception_name=exception_name)
	for fieldname, doctype in (("item_code", "Item"), ("workstation", "Workstation")):
		name = context.get(fieldname)
		if name and not _has_document_access(doctype, name, ptype="read"):
			context[fieldname] = ""
			context.setdefault("related_routes", {})[
				"item" if fieldname == "item_code" else "workstation"
			] = ""
			context.setdefault("gantt_focus", {})[
				"item_code" if fieldname == "item_code" else "workstation"
			] = ""
	return context


@frappe.whitelist(methods=["POST"])
def repair_item_references(company=None, include_standard=1, include_aps=1, commit=0):
	_require_admin_access()
	if frappe.utils.cint(commit):
		frappe.throw(
			_(
				"APS repair commits are controlled by the request transaction.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	company = _require_explicit_company(
		company,
		action_label=_("Item reference repair", context="Injection APS"),
	)
	return planning.repair_item_references(
		company=company,
		include_standard=frappe.utils.cint(include_standard),
		include_aps=frappe.utils.cint(include_aps),
		commit=False,
	)


@frappe.whitelist(methods=["POST"])
def detach_standard_references(company=None, dry_run=1):
	_require_admin_access()
	company = _require_explicit_company(
		company,
		action_label=_("standard reference cleanup", context="Injection APS"),
	)
	return planning.detach_standard_references(
		company=company,
		dry_run=frappe.utils.cint(dry_run),
	)


@frappe.whitelist()
def get_workspace_dashboard_data():
	_require_read_access()

	def visible_count(doctype, filters):
		rows = frappe.get_list(
			doctype,
			filters=filters,
			fields=["name"],
			limit_page_length=0,
		)
		return len(rows)

	return {
		"active_schedules": visible_count("Customer Delivery Schedule", {"status": "Active"}),
		"open_demands": visible_count("APS Demand Pool", {"status": "Open"}),
		"open_net_requirements": visible_count("APS Net Requirement", {"net_requirement_qty": (">", 0)}),
		"open_runs": visible_count("APS Planning Run", {"status": ("in", planning.RUN_OPEN_STATUSES)}),
		"blocking_exceptions": visible_count(
			"APS Exception Log", {"status": "Open", "severity": ("in", ["Critical", "Blocking"])}
		),
		"released_batches": visible_count("APS Release Batch", {"status": "Released"}),
		"synced_results": visible_count(
			"APS Schedule Result", {"status": ("in", ["Work Order Proposed", "Shift Proposed", "Applied"])}
		),
		"machine_capabilities": visible_count("APS Machine Capability", {"is_active": 1}),
	}


@frappe.whitelist()
def get_schedule_console_data(customer=None, company=None):
	_require_read_access()
	_require_scope_access(company=company, customer=customer)
	schedule_filters = planning._strip_none({"customer": customer, "company": company})
	active_schedules = frappe.get_list(
		"Customer Delivery Schedule",
		filters=schedule_filters,
		fields=[
			"name",
			"customer",
			"company",
			"schedule_scope",
			"version_no",
			"import_strategy",
			"source_type",
			"status",
			"schedule_total_qty",
			"modified",
		],
		order_by="modified desc",
		limit=50,
	)
	active_schedules = _filter_accessible_documents(active_schedules, "Customer Delivery Schedule")
	import_batches = frappe.get_list(
		"APS Schedule Import Batch",
		filters=schedule_filters,
		fields=[
			"name",
			"customer",
			"company",
			"schedule_scope",
			"version_no",
			"import_strategy",
			"source_type",
			"status",
			"imported_rows",
			"effective_rows",
			"modified",
		],
		order_by="modified desc",
		limit=50,
	)
	import_batches = _filter_accessible_documents(import_batches, "APS Schedule Import Batch")
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
def get_net_requirement_page_data(
	company=None,
	item_code=None,
	customer=None,
	date_from=None,
	date_to=None,
	positive_only=None,
	search_text=None,
	limit=None,
):
	_require_read_access()
	_require_scope_access(company=company, customer=customer)
	filters = planning._strip_none({"company": company, "item_code": item_code, "customer": customer})
	if date_from and date_to:
		filters["demand_date"] = ("between", [date_from, date_to])
	elif date_from:
		filters["demand_date"] = (">=", date_from)
	elif date_to:
		filters["demand_date"] = ("<=", date_to)
	if frappe.utils.cint(positive_only):
		filters["net_requirement_qty"] = (">", 0)
	search = (search_text or "").strip()
	if search and not item_code:
		filters["item_code"] = ("like", f"%{search}%")
	row_limit = min(max(frappe.utils.cint(limit or 500), 50), 1000)
	rows = frappe.get_list(
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
			"existing_work_order_policy",
			"safety_stock_gap_qty",
			"max_stock_qty",
			"overstock_qty",
			"minimum_batch_qty",
			"planning_qty",
			"net_requirement_qty",
			"reason_text",
			"is_system_generated",
			"modified",
		],
		order_by="demand_date asc, item_code asc",
		limit_page_length=row_limit,
	)
	rows = _filter_accessible_documents(rows, "APS Net Requirement")
	if search and item_code:
		search_lower = search.lower()
		rows = [
			row
			for row in rows
			if search_lower in str(row.get("item_code") or "").lower()
		]
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
def get_customer_schedule_progress_data(
	company=None,
	customer=None,
	item_code=None,
	schedule_scope=None,
	date_from=None,
	date_to=None,
	status=None,
	run_name=None,
	limit=None,
):
	_require_read_access()
	_require_scope_access(company=company, customer=customer, planning_run=run_name)
	response = planning.get_customer_schedule_progress_data(
		company=company,
		customer=customer,
		item_code=item_code,
		schedule_scope=schedule_scope,
		date_from=date_from,
		date_to=date_to,
		status=status,
		run_name=run_name,
		limit=limit,
	)
	resolved_company = (response.get("filters") or {}).get("company")
	if resolved_company and resolved_company != company:
		_require_scope_access(company=resolved_company)
	selected_run = (response.get("selected_run") or {}).get("name")
	if selected_run and selected_run != run_name:
		_require_scoped_document_access("APS Planning Run", selected_run, ptype="read")
	visible_rows = []
	access_cache = {}
	for row in response.get("rows") or []:
		allowed = True
		for doctype, docname in (
			("Company", row.get("company")),
			("Customer", row.get("customer")),
			("Customer Delivery Schedule", row.get("schedule")),
			("Sales Order", row.get("sales_order")),
			("Item", row.get("item_code")),
		):
			if not docname:
				continue
			key = (doctype, docname)
			if key not in access_cache:
				access_cache[key] = _has_document_access(doctype, docname, ptype="read")
			if not access_cache[key]:
				allowed = False
				break
		if allowed:
			row["result_names"] = [
				name
				for name in row.get("result_names") or []
				if _has_scoped_document_access("APS Schedule Result", name, ptype="read")
			]
			row["production_source_documents"] = [
				name
				for name in row.get("production_source_documents") or []
				if _has_document_access("Stock Entry", name, ptype="read")
			]
			row["delivery_source_documents"] = [
				name
				for name in row.get("delivery_source_documents") or []
				if _has_document_access("Delivery Note", name, ptype="read")
			]
			row["routes"] = planning._get_customer_schedule_progress_routes(row)
			visible_rows.append(row)
	response["rows"] = visible_rows
	response["summary"] = planning._summarize_customer_schedule_progress_rows(visible_rows)
	response["truncated"] = bool(response.get("truncated"))
	return response


@frappe.whitelist(methods=["POST"])
def update_net_requirement_row(name=None, values=None):
	_require_plan_access()
	_require_scoped_document_access("APS Net Requirement", name, ptype="read")
	linked_results = frappe.get_all(
		"APS Schedule Result",
		filters={"net_requirement": name},
		fields=["name", "planning_run"],
	)
	affected_runs = sorted({row.get("planning_run") for row in linked_results if row.get("planning_run")})
	for row in linked_results:
		_require_scoped_document_access("APS Schedule Result", row.get("name"), ptype="read")
	for run_name in affected_runs:
		_require_complete_run_mutation_scope(run_name, run_ptype="write")
	payload = frappe.parse_json(values) if isinstance(values, str) else (values or {})
	allowed_fields = {
		"demand_date",
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"safety_stock_gap_qty",
		"minimum_batch_qty",
		"planning_qty",
		"net_requirement_qty",
		"reason_text",
	}
	float_fields = {
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"safety_stock_gap_qty",
		"minimum_batch_qty",
		"planning_qty",
		"net_requirement_qty",
	}
	doc = frappe.get_doc("APS Net Requirement", name)
	for fieldname in allowed_fields:
		if fieldname not in payload:
			continue
		value = payload.get(fieldname)
		if fieldname in float_fields:
			doc.set(fieldname, frappe.utils.flt(value))
		else:
			doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	for run_name in affected_runs:
		capacity_balance.invalidate_capacity_balance(run_name)
		frappe.db.set_value(
			"APS Planning Run",
			run_name,
			{
				"consistency_status": "Unchecked",
				"consistency_checked_on": now_datetime(),
				"consistency_details": _(
					"Net Requirement {0} changed; recalculate the planning run before approval or release.",
					context="Injection APS",
				).format(name),
			},
			update_modified=False,
		)
	return {"name": doc.name}


@frappe.whitelist(methods=["POST"])
def delete_net_requirement_row(name=None):
	_require_plan_access()
	_require_scoped_document_access("APS Net Requirement", name, ptype="read")
	linked_results = frappe.get_all(
		"APS Schedule Result",
		filters={"net_requirement": name},
		fields=["name", "planning_run"],
	)
	if linked_results:
		for row in linked_results:
			_require_scoped_document_access("APS Schedule Result", row.get("name"), ptype="read")
		frappe.throw(
			_(
				"Net Requirement {0} is already linked to APS results. Recalculate or replace the planning run before deleting it.",
				context="Injection APS",
			).format(name),
			frappe.ValidationError,
		)
	frappe.delete_doc("APS Net Requirement", name, force=1, ignore_permissions=True)
	return {"deleted": name}


@frappe.whitelist()
def get_run_console_data(company=None, plant_floor=None):
	_require_read_access()
	_require_scope_access(company=company)
	filters = planning._strip_none({"company": company})
	runs = frappe.get_list(
		"APS Planning Run",
		filters=filters,
		fields=[
			"name",
			"company",
			"plant_floor",
			"selected_plant_floor_summary",
			"planning_date",
			"status",
			"approval_state",
			"existing_work_order_policy",
			"total_net_requirement_qty",
			"total_machine_scheduled_qty",
			"total_demand_covered_qty",
			"total_overproduction_qty",
			"total_scheduled_qty",
			"total_unscheduled_qty",
			"total_produced_qty",
			"total_delivered_qty",
			"consistency_status",
			"consistency_checked_on",
			"exception_count",
			"result_count",
			"notes",
		],
		order_by="modified desc",
		limit=50,
	)
	runs = _filter_accessible_documents(runs, "APS Planning Run")
	if plant_floor:
		runs = [
			row
			for row in runs
			if plant_floor in planning._coerce_plant_floor_list(
				plant_floors=(row.selected_plant_floor_summary or "").split(","),
				plant_floor=row.plant_floor,
			)
		]
	visible_result_rows = frappe.get_list(
		"APS Schedule Result",
		filters={"planning_run": ("in", [row.name for row in runs] or [""])},
		fields=[
			"name",
			"planning_run",
			"actual_status",
			"planned_qty",
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"unscheduled_qty",
			"produced_qty",
			"delivered_qty",
		],
		limit_page_length=0,
	)
	visible_result_rows = _filter_accessible_documents(visible_result_rows, "APS Schedule Result")
	execution_by_run = defaultdict(lambda: {"running": 0, "delayed": 0, "no_recent_update": 0})
	results_by_run = defaultdict(list)
	for result in visible_result_rows:
		results_by_run[result.get("planning_run")].append(result)
		if result.get("actual_status") == "Running":
			execution_by_run[result.get("planning_run")]["running"] += 1
		if result.get("actual_status") in ("Delayed", "Slow Progress"):
			execution_by_run[result.get("planning_run")]["delayed"] += 1
		if result.get("actual_status") == "No Recent Update":
			execution_by_run[result.get("planning_run")]["no_recent_update"] += 1
	visible_exceptions = frappe.get_list(
		"APS Exception Log",
		filters={
			"planning_run": ("in", [row.name for row in runs] or [""]),
			"status": "Open",
		},
		fields=["name", "planning_run", "source_doctype", "source_name"],
		limit_page_length=0,
	)
	exception_access_cache = {}
	exception_count_by_run = defaultdict(int)
	for row in _filter_accessible_documents(visible_exceptions, "APS Exception Log", exception_access_cache):
		if _has_exception_source_access(row, exception_access_cache):
			exception_count_by_run[row.get("planning_run")] += 1
	for row in runs:
		summary = _summarize_visible_results(results_by_run[row.name])
		row.total_net_requirement_qty = summary["planned_qty"]
		row.total_machine_scheduled_qty = summary["machine_scheduled_qty"]
		row.total_demand_covered_qty = summary["demand_covered_qty"]
		row.total_overproduction_qty = summary["overproduction_qty"]
		row.total_scheduled_qty = summary["machine_scheduled_qty"]
		row.total_unscheduled_qty = summary["unscheduled_qty"]
		row.total_produced_qty = summary["produced_qty"]
		row.total_delivered_qty = summary["delivered_qty"]
		row.result_count = len(results_by_run[row.name])
		row.exception_count = exception_count_by_run[row.name]
	run_contexts = {
		row.name: _sanitize_planning_run_context(
			planning.get_next_actions_for_context("APS Planning Run", row.name),
			quantity_summary=_summarize_visible_results(results_by_run[row.name]),
		)
		for row in runs
	}
	return {
		"runs": [
			{
				**row,
				"next_actions": run_contexts[row.name],
				"execution_health": execution_by_run[row.name],
			}
			for row in runs
		]
	}


@frappe.whitelist()
def get_schedule_gantt_data(run_name):
	_require_read_access()
	_require_scoped_document_access("APS Planning Run", run_name, ptype="read")
	settings = planning.get_settings_dict()
	run_doc = frappe.get_doc("APS Planning Run", run_name)
	selected_plant_floors = planning._get_run_selected_plant_floors(run_doc)
	selected_plant_floors = [
		name for name in selected_plant_floors if _has_document_access("Plant Floor", name, ptype="read")
	]
	lanes = planning._get_machine_capability_rows(selected_plant_floors)
	downtime_windows = planning._get_active_downtime_windows(
		company=run_doc.company,
		plant_floors=selected_plant_floors,
		horizon_start=run_doc.horizon_start,
		horizon_end=run_doc.horizon_end,
		run_name=run_name,
	)
	downtime_windows = [
		row
		for row in downtime_windows
		if not row.get("name") or _has_scoped_document_access("APS Downtime Window", row.get("name"))
	]
	lanes = [
		row
		for row in lanes
		if (not row.get("workstation") or _has_document_access("Workstation", row.get("workstation")))
		and (not row.get("plant_floor") or _has_document_access("Plant Floor", row.get("plant_floor")))
	]
	results = frappe.get_list(
		"APS Schedule Result",
		filters={"planning_run": run_name},
		fields=[
			"name",
			"net_requirement",
			"item_code",
			"customer",
			"requested_date",
			"demand_source",
			"production_strategy",
			"planned_qty",
			"machine_scheduled_qty",
			"demand_covered_qty",
			"overproduction_qty",
			"risk_status",
			"status",
			"unscheduled_qty",
			"produced_qty",
			"delivered_qty",
			"prebuild_qty",
			"jit_qty",
			"early_days",
			"projected_peak_inventory_qty",
			"late_qty_before_balance",
			"late_qty_after_balance",
			"good_produced_qty",
			"scrap_qty",
			"current_deliverable_qty",
			"prebuild_inventory_qty",
			"cancellation_inventory_risk_qty",
			"last_actual_report_time",
			"execution_source_documents",
			"projected_completion_time",
			"schedule_delay_minutes",
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
	results = _filter_accessible_documents(results, "APS Schedule Result")
	if not results:
		run_context = _sanitize_planning_run_context(
			planning.get_next_actions_for_context("APS Planning Run", run_name),
			quantity_summary=_summarize_visible_results([]),
		)
		return {
			"tasks": [],
			"rows": [],
			"lanes": lanes,
			"downtime_windows": downtime_windows,
			"selected_plant_floors": selected_plant_floors,
			"blocked_results": [],
			"run": run_context,
			"run_context": run_context,
			"quantity_summary": _summarize_visible_results([]),
		}
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
			"plant_floor",
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
			"risk_status",
			"is_locked",
			"is_manual",
			"schedule_explanation",
			"risk_flags",
			"schedule_delay_minutes",
			"segment_note",
			"manual_change_note",
			"original_segment",
			"split_group",
			"split_index",
			"split_reason",
			"linked_work_order",
			"linked_work_order_scheduling",
			"linked_scheduling_item",
			"actual_status",
			"actual_completed_qty",
			"actual_good_qty",
			"actual_scrap_qty",
			"actual_start_time",
			"actual_end_time",
			"delay_minutes",
			"last_actual_report_time",
			"execution_source_documents",
			"production_mode",
			"capacity_bucket_start",
			"capacity_bucket_end",
			"available_capacity_qty",
			"occupied_capacity_qty",
			"remaining_capacity_qty",
			"load_percent",
			"projected_late_qty",
			"prebuildable_qty",
		],
		order_by="start_time asc",
	)
	segments = [
		row
		for row in segments
		if (not row.get("workstation") or _has_document_access("Workstation", row.get("workstation")))
		and (not row.get("plant_floor") or _has_document_access("Plant Floor", row.get("plant_floor")))
	]
	for row in results:
		row.execution_source_documents = _filter_link_list(row.get("execution_source_documents"), "Stock Entry")
	for row in segments:
		if row.get("linked_work_order") and not _has_document_access(
			"Work Order", row.get("linked_work_order"), ptype="read"
		):
			row.linked_work_order = ""
		if row.get("linked_work_order_scheduling") and not _has_document_access(
			"Work Order Scheduling", row.get("linked_work_order_scheduling"), ptype="read"
		):
			row.linked_work_order_scheduling = ""
		if row.get("linked_scheduling_item") and not _has_document_access(
			"Scheduling Item", row.get("linked_scheduling_item"), ptype="read"
		):
			row.linked_scheduling_item = ""
		row.execution_source_documents = _filter_link_list(row.get("execution_source_documents"), "Stock Entry")
	exceptions = frappe.get_list(
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
			"diagnostic_json",
			"resolution_hint",
		],
		order_by="modified desc",
	)
	exception_access_cache = {}
	exceptions = [
		row
		for row in _filter_accessible_documents(exceptions, "APS Exception Log", exception_access_cache)
		if _has_exception_source_access(row, exception_access_cache)
	]
	result_map = {row.name: row for row in results}
	fulfillment = _filter_fulfillment_projection(
		availability.get_run_fulfillment_projection(run_name, persist=False),
		[row.name for row in results],
	)
	fulfillment_map = {row["result"]: row for row in fulfillment.get("results") or []}
	exception_map = {}
	for row in exceptions:
		exception_map.setdefault(row.source_name, []).append(row)
	primary_segment_count = {}
	segment_names_by_result = defaultdict(list)
	for row in segments:
		segment_names_by_result[row.parent].append(row.name)
		if consistency.is_effective_primary_segment(row):
			primary_segment_count[row.parent] = primary_segment_count.get(row.parent, 0) + 1
	tasks = []
	for row in segments:
		parent = result_map.get(row.parent)
		if not parent:
			continue
		if (
			row.segment_status == "Cancelled"
			or not row.planned_qty
			or not row.workstation
			or not row.start_time
			or not row.end_time
			or get_datetime(row.end_time) <= get_datetime(row.start_time)
		):
			continue
		item_detail = item_detail_map.get(parent.item_code) or {}
		fulfillment_row = fulfillment_map.get(parent.name) or {}
		risk_rows = (
			(exception_map.get(parent.name) or [])
			+ (exception_map.get(parent.net_requirement) or [])
			+ (exception_map.get(row.name) or [])
		)
		execution_risk = (
			"Critical"
			if row.actual_status in ("Delayed", "Overproduced")
			else "Attention" if row.actual_status in ("Slow Progress", "No Recent Update") else "Normal"
		)
		task_risk = consistency.get_worst_risk(
			parent.risk_status,
			row.risk_status,
			consistency.get_exception_risk(risk_rows),
			execution_risk,
			"Blocked" if row.segment_status == "Blocked" else "Normal",
		)
		risk_badges = [risk_row.exception_type for risk_row in risk_rows]
		if row.is_locked:
			risk_badges.append("Frozen / Locked")
		if row.actual_status in ("Delayed", "Slow Progress", "No Recent Update", "Overproduced"):
			risk_badges.append(f"Execution: {row.actual_status}")
		tasks.append(
			{
				"id": row.name,
				"name": f"{parent.item_code} / {item_detail.get('item_name') or row.workstation}",
				"start": row.start_time,
				"end": row.end_time,
				"progress": min(
					max((flt(row.actual_completed_qty) / flt(row.planned_qty) * 100) if flt(row.planned_qty) else 0, 0),
					100,
				),
				"custom_class": f"ia-risk-{task_risk.lower()}",
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
					"production_strategy": parent.production_strategy,
					"planned_qty": parent.planned_qty,
					"machine_scheduled_qty": parent.machine_scheduled_qty,
					"demand_covered_qty": parent.demand_covered_qty,
					"overproduction_qty": parent.overproduction_qty,
					"unscheduled_qty": parent.unscheduled_qty,
					"produced_qty": parent.produced_qty,
					"delivered_qty": parent.delivered_qty,
					"prebuild_qty": parent.prebuild_qty,
					"jit_qty": parent.jit_qty,
					"early_days": parent.early_days,
					"projected_peak_inventory_qty": parent.projected_peak_inventory_qty,
					"late_qty_before_balance": parent.late_qty_before_balance,
					"late_qty_after_balance": parent.late_qty_after_balance,
					"good_produced_qty": parent.good_produced_qty,
					"scrap_qty": parent.scrap_qty,
					"current_deliverable_qty": parent.current_deliverable_qty,
					"prebuild_inventory_qty": parent.prebuild_inventory_qty,
					"cancellation_inventory_risk_qty": parent.cancellation_inventory_risk_qty,
					"last_actual_report_time": parent.last_actual_report_time,
					"execution_source_documents": parent.execution_source_documents,
					"fulfillment_timeline": fulfillment_row.get("timeline") or [],
					"projected_completion_time": parent.projected_completion_time,
					"result_risk_status": parent.risk_status,
					"risk_status": task_risk,
					"net_requirement": parent.net_requirement,
					"plant_floor": row.plant_floor,
					"workstation": row.workstation,
					"segment_planned_qty": row.planned_qty,
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
					"original_segment": row.original_segment,
					"split_group": row.split_group,
					"split_index": row.split_index,
					"split_reason": row.split_reason,
					"risk_flags": row.risk_flags,
					"risk_badges": list(dict.fromkeys(risk_badges)),
					"actual_status": row.actual_status or parent.actual_status,
					"actual_completed_qty": row.actual_completed_qty,
					"actual_good_qty": row.actual_good_qty,
					"actual_scrap_qty": row.actual_scrap_qty,
					"actual_start_time": row.actual_start_time or parent.actual_start_time,
					"actual_end_time": row.actual_end_time or parent.actual_end_time,
					"schedule_delay_minutes": row.schedule_delay_minutes or parent.schedule_delay_minutes,
					"delay_minutes": row.delay_minutes or parent.delay_minutes,
					"linked_work_order": row.linked_work_order,
					"linked_work_order_scheduling": row.linked_work_order_scheduling,
					"linked_scheduling_item": row.linked_scheduling_item,
					"production_mode": row.production_mode,
					"capacity_bucket_start": row.capacity_bucket_start,
					"capacity_bucket_end": row.capacity_bucket_end,
					"available_capacity_qty": row.available_capacity_qty,
					"occupied_capacity_qty": row.occupied_capacity_qty,
					"remaining_capacity_qty": row.remaining_capacity_qty,
					"load_percent": row.load_percent,
					"projected_late_qty": row.projected_late_qty,
					"prebuildable_qty": row.prebuildable_qty,
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
		for segment_name in segment_names_by_result.get(row.name) or []:
			risk_rows.extend(exception_map.get(segment_name) or [])
		if (
			primary_segment_count.get(row.name)
			and row.risk_status == "Normal"
			and not row.unscheduled_qty
			and not row.overproduction_qty
		):
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
				"planned_qty": row.planned_qty,
				"machine_scheduled_qty": row.machine_scheduled_qty,
				"demand_covered_qty": row.demand_covered_qty,
				"overproduction_qty": row.overproduction_qty,
				"unscheduled_qty": row.unscheduled_qty,
				"produced_qty": row.produced_qty,
				"delivered_qty": row.delivered_qty,
				"production_strategy": row.production_strategy,
				"prebuild_qty": row.prebuild_qty,
				"jit_qty": row.jit_qty,
				"current_deliverable_qty": row.current_deliverable_qty,
				"cancellation_inventory_risk_qty": row.cancellation_inventory_risk_qty,
				"blocking_reason": row.blocking_reason,
				"exception_types": [risk_row.exception_type for risk_row in risk_rows],
				"diagnostic_summary": next(
					(
						(planning._parse_diagnostic_json(risk_row.get("diagnostic_json")) or {}).get("root_cause_text")
						or risk_row.get("resolution_hint")
						or risk_row.get("message")
						for risk_row in risk_rows
						if risk_row.get("message")
					),
					"",
				),
				"result_route": f"Form/APS Schedule Result/{row.name}",
			}
		)
	quantity_summary = _summarize_visible_results(results)
	run_context = _sanitize_planning_run_context(
		planning.get_next_actions_for_context("APS Planning Run", run_name),
		quantity_summary=quantity_summary,
	)
	return {
		"tasks": tasks,
		"rows": segments,
		"lanes": lanes,
		"downtime_windows": downtime_windows,
		"selected_plant_floors": selected_plant_floors,
		"blocked_results": blocked_results,
		"run": run_context,
		"run_context": run_context,
		"quantity_summary": quantity_summary,
		"fulfillment_summary": fulfillment.get("summary") or {},
		"fulfillment_results": fulfillment.get("results") or [],
		"fulfillment_warning_count": len(fulfillment.get("warnings") or []),
		"fulfillment_warnings": fulfillment.get("warnings") or [],
	}


@frappe.whitelist()
def get_release_center_data(run_name=None):
	_require_read_access()
	if run_name:
		_require_scoped_document_access("APS Planning Run", run_name, ptype="read")
	batch_filters = planning._strip_none({"planning_run": run_name})
	work_order_proposal_batches = frappe.get_list(
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
	work_order_proposal_batches = _filter_accessible_documents(
		work_order_proposal_batches, "APS Work Order Proposal Batch"
	)
	_attach_review_counts(work_order_proposal_batches, "APS Work Order Proposal Item")
	shift_schedule_proposal_batches = frappe.get_list(
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
	shift_schedule_proposal_batches = _filter_accessible_documents(
		shift_schedule_proposal_batches, "APS Shift Schedule Proposal Batch"
	)
	_attach_review_counts(shift_schedule_proposal_batches, "APS Shift Schedule Proposal Item")
	release_batches = frappe.get_list(
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
	release_batches = _filter_accessible_documents(release_batches, "APS Release Batch")
	_attach_release_wos_details(release_batches)
	exception_filters = {"status": "Open"}
	if run_name:
		exception_filters["planning_run"] = run_name
	exceptions = frappe.get_list(
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
			"diagnostic_json",
		],
		order_by="modified desc",
		limit=100,
	)
	exception_access_cache = {}
	exceptions = [
		row
		for row in _filter_accessible_documents(exceptions, "APS Exception Log", exception_access_cache)
		if _has_exception_source_access(row, exception_access_cache)
	]
	for row in exceptions:
		diagnostic = planning._parse_diagnostic_json(row.get("diagnostic_json"))
		row["diagnostic"] = diagnostic
		row["root_cause_codes"] = diagnostic.get("root_cause_codes") or []
		row["root_cause_text"] = diagnostic.get("root_cause_text") or row.get("resolution_hint") or row.get("message")
		row["suggested_actions"] = diagnostic.get("suggested_actions") or []
		row["has_resolution_context"] = 1
		row.update(_build_exception_routes(row))
		exception_run_name = row.get("planning_run") or run_name
		row["execution_route"] = f"aps-release-center?{urlencode({'run_name': exception_run_name})}" if exception_run_name else ""
		row["item_route"] = f"Form/Item/{row['item_code']}" if row.get("item_code") else ""
		row["workstation_route"] = f"Form/Workstation/{row['workstation']}" if row.get("workstation") else ""
	visible_results = (
		frappe.get_list(
			"APS Schedule Result",
			filters={"planning_run": run_name},
			fields=[
				"name",
				"actual_status",
				"planned_qty",
				"machine_scheduled_qty",
				"demand_covered_qty",
				"overproduction_qty",
				"unscheduled_qty",
				"produced_qty",
				"delivered_qty",
				"prebuild_qty",
				"jit_qty",
				"scrap_qty",
				"current_deliverable_qty",
				"prebuild_inventory_qty",
				"cancellation_inventory_risk_qty",
			],
			limit_page_length=0,
		)
		if run_name
		else []
	)
	visible_results = _filter_accessible_documents(visible_results, "APS Schedule Result")
	quantity_summary = _summarize_visible_results(visible_results) if run_name else None
	execution_health = _build_visible_execution_health(run_name, visible_results) if run_name else None
	run_context = (
		_sanitize_planning_run_context(
			planning.get_next_actions_for_context("APS Planning Run", run_name),
			quantity_summary=quantity_summary,
		)
		if run_name
		else None
	)
	fulfillment = (
		_filter_fulfillment_projection(
			availability.get_run_fulfillment_projection(run_name, persist=False),
			[row.get("name") for row in visible_results],
		)
		if run_name
		else None
	)
	recent_runs = []
	for row in planning.get_recent_run_contexts(limit=8):
		if not _has_scoped_document_access("APS Planning Run", row.get("name"), ptype="read"):
			continue
		row = dict(row)
		row["selected_plant_floors"] = [
			name
			for name in row.get("selected_plant_floors") or []
			if _has_document_access("Plant Floor", name, ptype="read")
		]
		recent_runs.append(row)
	return {
		"work_order_proposal_batches": work_order_proposal_batches,
		"shift_schedule_proposal_batches": shift_schedule_proposal_batches,
		"release_batches": release_batches,
		"exceptions": exceptions,
		"run_context": run_context,
		"quantity_summary": quantity_summary,
		"execution_health": execution_health,
		"fulfillment_summary": (fulfillment or {}).get("summary"),
		"fulfillment_warning_count": len((fulfillment or {}).get("warnings") or []),
		"fulfillment_warnings": (fulfillment or {}).get("warnings") or [],
		"recent_runs": recent_runs,
	}
