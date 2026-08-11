from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


QTY_TOLERANCE = 0.000001


def sync_delivery_allocations(
	company: str,
	customer: str | None = None,
	item_codes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
	"""Rebuild Delivery Note Item allocations for one controlled company/customer/item scope."""
	from injection_aps.services import availability, consistency

	if not company:
		frappe.throw(_("Company is required for delivery synchronization."), frappe.ValidationError)
	item_codes = sorted({item for item in (item_codes or []) if item})
	save_point = "aps_delivery_sync_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		targets = _get_delivery_targets(company, customer=customer, item_codes=item_codes)
		existing_scope = _get_existing_scope_rows(company, customer=customer, item_codes=item_codes)
		scope_item_codes = sorted(
			{row["item_code"] for row in targets}
			| {row.item_code for row in existing_scope if row.item_code}
			| set(item_codes)
		)
		if not scope_item_codes:
			return {
				"company": company,
				"customer": customer,
				"source_item_count": 0,
				"desired_allocation_count": 0,
				"ledger": {"created": 0, "updated": 0, "reversed": 0},
				"rollup": {"schedule_item_count": 0, "delivered_qty": 0},
				"consistency_runs": [],
			}
		sources = _get_submitted_delivery_sources(
			company,
			customer=customer,
			item_codes=scope_item_codes,
		)
		desired = _build_desired_delivery_allocations(sources, targets)
		ledger_summary = _reconcile_delivery_ledger(
			company,
			desired,
			customer=customer,
			item_codes=scope_item_codes,
		)
		rollup = _rollup_delivery_allocations(
			company,
			customer=customer,
			item_codes=scope_item_codes,
		)
		consistency_runs = []
		for run_name in _get_affected_planning_runs(rollup.get("schedule_items") or []):
			consistency_result = consistency.recalculate_plan_consistency(
					run_name,
					reason="formal Delivery Note allocation synchronization",
			)
			consistency_result["fulfillment"] = availability.recalculate_run_fulfillment(run_name)
			consistency_runs.append(consistency_result)
		frappe.db.release_savepoint(save_point)
		return {
			"company": company,
			"customer": customer,
			"source_item_count": len(sources),
			"desired_allocation_count": len(desired),
			"ledger": ledger_summary,
			"rollup": rollup,
			"consistency_runs": consistency_runs,
		}
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise


def queue_delivery_sync(doc, method: str | None = None):
	item_codes = sorted({row.item_code for row in doc.get("items") or [] if row.item_code})
	if not doc.company or not item_codes:
		return
	frappe.enqueue(
		"injection_aps.services.delivery_sync.sync_delivery_allocations",
		queue="short",
		enqueue_after_commit=True,
		job_id="aps-delivery-sync-{0}-{1}".format(
			doc.company,
			hashlib.sha256("|".join([doc.customer or "", *item_codes]).encode("utf-8")).hexdigest()[:16],
		),
		deduplicate=True,
		company=doc.company,
		customer=doc.customer,
		item_codes=item_codes,
	)


def _get_delivery_targets(company, *, customer=None, item_codes=None) -> list[dict[str, Any]]:
	conditions = ["s.company = %(company)s", "s.status = 'Active'"]
	params: dict[str, Any] = {"company": company}
	if customer:
		conditions.append("s.customer = %(customer)s")
		params["customer"] = customer
	if item_codes:
		conditions.append("i.item_code in %(item_codes)s")
		params["item_codes"] = list(item_codes)
	rows = frappe.db.sql(
		"""
		select
			i.name,
			i.parent,
			i.item_code,
			i.sales_order,
			i.schedule_date,
			i.qty,
			i.idx,
			s.company,
			s.customer,
			s.creation as schedule_creation
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where {conditions}
		order by s.customer asc, i.item_code asc, i.schedule_date asc,
			ifnull(i.sales_order, '') asc, s.creation asc, i.idx asc, i.name asc
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)
	return [dict(row) for row in rows]


def _get_existing_scope_rows(company, *, customer=None, item_codes=None):
	filters: dict[str, Any] = {"company": company}
	if customer:
		filters["customer"] = customer
	if item_codes:
		filters["item_code"] = ("in", list(item_codes))
	return frappe.get_all(
		"APS Delivery Allocation",
		filters=filters,
		fields=["name", "item_code", "customer_schedule_item"],
	)


def _get_submitted_delivery_sources(company, *, customer=None, item_codes=None):
	conditions = ["dn.company = %(company)s", "dn.docstatus = 1", "dni.stock_qty != 0"]
	params: dict[str, Any] = {"company": company}
	if customer:
		conditions.append("dn.customer = %(customer)s")
		params["customer"] = customer
	if item_codes:
		conditions.append("dni.item_code in %(item_codes)s")
		params["item_codes"] = list(item_codes)
	rows = frappe.db.sql(
		"""
		select
			dn.name as source_delivery_note,
			dn.docstatus as source_docstatus,
			dn.company,
			dn.customer,
			dn.posting_date,
			dn.posting_time,
			dn.creation,
			dn.is_return,
			dn.return_against,
			dn.amended_from,
			dni.name as source_delivery_note_item,
			dni.item_code,
			dni.stock_qty,
			dni.qty,
			dni.against_sales_order as sales_order,
			dni.so_detail as sales_order_item,
			dni.dn_detail as original_delivery_note_item,
			dni.custom_aps_customer_schedule_item as direct_schedule_item,
			soi.delivery_date as sales_order_due_date
		from `tabDelivery Note` dn
		inner join `tabDelivery Note Item` dni on dni.parent = dn.name
		left join `tabSales Order Item` soi on soi.name = dni.so_detail
		where {conditions}
		order by dn.posting_date asc, dn.posting_time asc, dn.creation asc, dni.idx asc, dni.name asc
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)
	result = []
	for row in rows:
		qty = abs(flt(row.stock_qty or row.qty))
		posting_time = get_datetime(f"{getdate(row.posting_date)} {row.posting_time or '00:00:00'}")
		result.append(
			{
				**dict(row),
				"source_qty": qty,
				"signed_qty": -qty if cint(row.is_return) else qty,
				"source_posting_time": posting_time,
			}
		)
	return result


def _build_desired_delivery_allocations(sources, active_targets):
	active_by_name = {row["name"]: row for row in active_targets}
	all_direct_names = {row.get("direct_schedule_item") for row in sources if row.get("direct_schedule_item")}
	all_targets_by_name = dict(active_by_name)
	for name in all_direct_names:
		if name not in all_targets_by_name:
			target = _get_schedule_target_by_name(name)
			if target:
				all_targets_by_name[name] = target
	targets_by_scope = defaultdict(list)
	for target in active_targets:
		targets_by_scope[(target["company"], target["customer"], target["item_code"])].append(target)
	delivered_running = defaultdict(float)
	normal_allocations_by_source = defaultdict(list)
	return_used = defaultdict(float)
	desired = []
	for source in sources:
		if source["source_qty"] <= QTY_TOLERANCE:
			continue
		if cint(source.get("is_return")):
			parts = _allocate_return_source(
				source,
				normal_allocations_by_source,
				all_targets_by_name,
				return_used,
			)
		else:
			parts = _allocate_delivery_source(
				source,
				targets_by_scope,
				all_targets_by_name,
				delivered_running,
			)
		for target, qty, method in parts:
			signed_qty = -qty if cint(source.get("is_return")) else qty
			delivered_running[target["name"]] += signed_qty
			if delivered_running[target["name"]] < -QTY_TOLERANCE:
				frappe.throw(
					_("Return {0} would reduce schedule item {1} below zero delivered quantity.").format(
						source["source_delivery_note_item"], target["name"]
					),
					frappe.ValidationError,
				)
			allocation = {
				"allocation_key": _delivery_allocation_key(source, target),
				"company": source["company"],
				"customer": source["customer"],
				"item_code": source["item_code"],
				"sales_order": source.get("sales_order"),
				"sales_order_item": source.get("sales_order_item"),
				"schedule_date": target["schedule_date"],
				"customer_schedule": target["parent"],
				"customer_schedule_item": target["name"],
				"source_delivery_note": source["source_delivery_note"],
				"source_delivery_note_item": source["source_delivery_note_item"],
				"source_docstatus": 1,
				"source_posting_time": source["source_posting_time"],
				"is_return": cint(source.get("is_return")),
				"return_against": source.get("return_against"),
				"original_delivery_note_item": source.get("original_delivery_note_item"),
				"allocation_method": method,
				"source_qty": source["source_qty"],
				"allocated_qty": qty,
				"effective_qty": signed_qty,
				"cumulative_delivered_qty": max(delivered_running[target["name"]], 0),
				"reversed_qty": 0,
				"is_effective": 1,
				"reversal_reason": "",
				"source_fingerprint": _delivery_source_fingerprint(source),
				"last_synced_on": now_datetime(),
			}
			desired.append(allocation)
			if not cint(source.get("is_return")):
				normal_allocations_by_source[source["source_delivery_note_item"]].append(allocation)
	return desired


def _allocate_delivery_source(source, targets_by_scope, all_targets_by_name, delivered_running):
	direct_name = source.get("direct_schedule_item")
	if direct_name:
		target = all_targets_by_name.get(direct_name)
		_validate_direct_target(source, target)
		return [(target, source["source_qty"], "Direct")]
	eligible = list(
		targets_by_scope.get((source["company"], source["customer"], source["item_code"])) or []
	)
	if source.get("sales_order"):
		eligible = [row for row in eligible if row.get("sales_order") == source.get("sales_order")]
	else:
		eligible = [row for row in eligible if not row.get("sales_order")]
	if source.get("sales_order_due_date"):
		eligible = [
			row for row in eligible if getdate(row.get("schedule_date")) == getdate(source["sales_order_due_date"])
		]
	if not eligible:
		frappe.throw(
			_(
				"Delivery Note Item {0} has no active schedule target for customer {1}, item {2}, sales order {3}, due {4}."
			).format(
				source["source_delivery_note_item"],
				source["customer"],
				source["item_code"],
				source.get("sales_order") or "-",
				source.get("sales_order_due_date") or "-",
			),
			frappe.ValidationError,
		)
	remaining = source["source_qty"]
	parts = []
	for target in eligible:
		available = max(flt(target.get("qty")) - delivered_running[target["name"]], 0)
		qty = min(remaining, available)
		if qty > QTY_TOLERANCE:
			parts.append((target, qty, "Controlled FIFO"))
			remaining -= qty
		if remaining <= QTY_TOLERANCE:
			break
	if remaining > QTY_TOLERANCE:
		parts.append((eligible[-1], remaining, "Controlled FIFO"))
	return parts


def _allocate_return_source(source, normal_by_source, all_targets_by_name, return_used):
	direct_name = source.get("direct_schedule_item")
	if direct_name:
		target = all_targets_by_name.get(direct_name)
		_validate_direct_target(source, target)
		return [(target, source["source_qty"], "Return Trace")]
	original_items = []
	if source.get("original_delivery_note_item"):
		original_items.append(source["original_delivery_note_item"])
	elif source.get("return_against"):
		original_items.extend(
			frappe.get_all(
				"Delivery Note Item",
				filters={
					"parent": source["return_against"],
					"item_code": source["item_code"],
					"against_sales_order": source.get("sales_order") or ("is", "not set"),
				},
				pluck="name",
				order_by="idx asc",
			)
		)
	traces = []
	for original_item in original_items:
		allocations = normal_by_source.get(original_item) or _get_existing_original_allocations(original_item)
		for allocation in allocations:
			target_name = allocation.get("customer_schedule_item")
			target = all_targets_by_name.get(target_name) or _get_schedule_target_by_name(target_name)
			if target:
				traces.append((original_item, target, flt(allocation.get("allocated_qty"))))
	remaining = source["source_qty"]
	parts = []
	for original_item, target, original_qty in traces:
		available = max(original_qty - return_used[(original_item, target["name"])], 0)
		qty = min(remaining, available)
		if qty > QTY_TOLERANCE:
			parts.append((target, qty, "Return Trace"))
			return_used[(original_item, target["name"])] += qty
			remaining -= qty
		if remaining <= QTY_TOLERANCE:
			break
	if remaining > QTY_TOLERANCE:
		frappe.throw(
			_("Return Delivery Note Item {0} exceeds traceable original delivery by {1}.").format(
				source["source_delivery_note_item"], remaining
			),
			frappe.ValidationError,
		)
	return parts


def _validate_direct_target(source, target):
	if not target:
		frappe.throw(
			_("Direct customer schedule item {0} does not exist.").format(source.get("direct_schedule_item")),
			frappe.ValidationError,
		)
	for fieldname in ("company", "customer", "item_code"):
		if (target.get(fieldname) or "") != (source.get(fieldname) or ""):
			frappe.throw(
				_("Direct delivery allocation mismatch on {0} for Delivery Note Item {1}.").format(
					fieldname, source["source_delivery_note_item"]
				),
				frappe.ValidationError,
			)
	if source.get("sales_order") and target.get("sales_order") != source.get("sales_order"):
		frappe.throw(
			_("Direct delivery allocation sales order does not match Delivery Note Item {0}.").format(
				source["source_delivery_note_item"]
			),
			frappe.ValidationError,
		)


def _get_schedule_target_by_name(name):
	if not name:
		return None
	row = frappe.db.sql(
		"""
		select
			i.name, i.parent, i.item_code, i.sales_order, i.schedule_date, i.qty, i.idx,
			s.company, s.customer, s.creation as schedule_creation
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name = %s
		limit 1
		""",
		name,
		as_dict=True,
	)
	return dict(row[0]) if row else None


def _get_existing_original_allocations(source_item):
	return [
		dict(row)
		for row in frappe.get_all(
			"APS Delivery Allocation",
			filters={"source_delivery_note_item": source_item, "is_return": 0, "is_effective": 1},
			fields=["customer_schedule_item", "allocated_qty"],
			order_by="creation asc",
		)
	]


def _delivery_allocation_key(source, target):
	return hashlib.sha256(
		"|".join(
			[
				source["source_delivery_note_item"],
				target["name"],
				"Return" if cint(source.get("is_return")) else "Delivery",
			]
		).encode("utf-8")
	).hexdigest()


def _delivery_source_fingerprint(source):
	return hashlib.sha256(
		"|".join(
			str(source.get(fieldname) or "")
			for fieldname in (
				"source_delivery_note",
				"source_delivery_note_item",
				"source_docstatus",
				"source_qty",
				"source_posting_time",
				"is_return",
				"return_against",
				"original_delivery_note_item",
				"direct_schedule_item",
			)
		).encode("utf-8")
	).hexdigest()


def _reconcile_delivery_ledger(company, desired, *, customer=None, item_codes=None):
	desired_by_key = {row["allocation_key"]: row for row in desired}
	filters: dict[str, Any] = {"company": company}
	if customer:
		filters["customer"] = customer
	if item_codes:
		filters["item_code"] = ("in", list(item_codes))
	existing = {
		row.allocation_key: row.name
		for row in frappe.get_all(
			"APS Delivery Allocation",
			filters=filters,
			fields=["name", "allocation_key"],
		)
	}
	created = 0
	updated = 0
	reversed_count = 0
	for key, values in desired_by_key.items():
		if key in existing:
			doc = frappe.get_doc("APS Delivery Allocation", existing[key])
			doc.update(values)
			doc.save(ignore_permissions=True)
			updated += 1
		else:
			frappe.get_doc({"doctype": "APS Delivery Allocation", **values}).insert(ignore_permissions=True)
			created += 1
	for key, name in existing.items():
		if key in desired_by_key:
			continue
		doc = frappe.get_doc("APS Delivery Allocation", name)
		docstatus = cint(frappe.db.get_value("Delivery Note", doc.source_delivery_note, "docstatus"))
		previous_effective = flt(doc.effective_qty)
		doc.source_docstatus = docstatus
		doc.effective_qty = 0
		doc.reversed_qty = max(flt(doc.reversed_qty), abs(previous_effective))
		doc.is_effective = 0
		doc.reversal_reason = "Source Delivery Note cancelled" if docstatus == 2 else "Source is no longer eligible"
		doc.last_synced_on = now_datetime()
		doc.save(ignore_permissions=True)
		reversed_count += cint(abs(previous_effective) > QTY_TOLERANCE)
	return {"created": created, "updated": updated, "reversed": reversed_count}


def _rollup_delivery_allocations(company, *, customer=None, item_codes=None):
	filters: dict[str, Any] = {"company": company}
	if customer:
		filters["customer"] = customer
	if item_codes:
		filters["item_code"] = ("in", list(item_codes))
	touched = set(
		frappe.get_all(
			"APS Delivery Allocation",
			filters=filters,
			pluck="customer_schedule_item",
		)
	)
	total_delivered = 0.0
	for schedule_item in touched:
		delivered_qty = flt(
			frappe.db.sql(
				"""
				select coalesce(sum(effective_qty), 0)
				from `tabAPS Delivery Allocation`
				where customer_schedule_item = %s and is_effective = 1
				""",
				schedule_item,
			)[0][0]
		)
		delivered_qty = max(delivered_qty, 0)
		qty = flt(frappe.db.get_value("Customer Delivery Schedule Item", schedule_item, "qty"))
		status = "Cancelled" if qty <= QTY_TOLERANCE else "Covered" if delivered_qty >= qty else "Open"
		frappe.db.set_value(
			"Customer Delivery Schedule Item",
			schedule_item,
			{
				"delivered_qty": delivered_qty,
				"balance_qty": max(qty - delivered_qty, 0),
				"status": status,
			},
			update_modified=False,
		)
		total_delivered += delivered_qty
	return {
		"schedule_item_count": len(touched),
		"delivered_qty": total_delivered,
		"schedule_items": sorted(touched),
	}


def _get_affected_planning_runs(schedule_items):
	if not schedule_items:
		return []
	identities = frappe.db.sql(
		"""
		select distinct s.company, s.customer, i.item_code, i.schedule_date
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name in %(schedule_items)s
		""",
		{"schedule_items": list(schedule_items)},
		as_dict=True,
	)
	runs = set()
	for row in identities:
		runs.update(
			frappe.get_all(
				"APS Schedule Result",
				filters={
					"company": row.company,
					"customer": row.customer,
					"item_code": row.item_code,
					"requested_date": row.schedule_date,
				},
				pluck="planning_run",
			)
		)
	return sorted(run for run in runs if run)
