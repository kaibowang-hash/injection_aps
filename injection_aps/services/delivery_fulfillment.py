from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime

from injection_aps.services.v2_flags import get_v2_settings, is_v2_enabled


QTY_TOLERANCE = 0.000001


def sync_delivery_plan_lineage(doc, method: str | None = None):
	"""Suggest DP quantity lineage and copy it to generated SO allocation rows.

	This hook never blocks save/submit and does nothing while APS V2 is disabled.
	"""
	if not is_v2_enabled() or not doc.get("company") or not doc.get("customer"):
		return
	try:
		quantity_rows = list(doc.get("item_qties") or [])
		for row in quantity_rows:
			_resolve_delivery_plan_qty_lineage(doc, row)
		_propagate_delivery_plan_item_lineage(doc, quantity_rows)
	except Exception:
		frappe.log_error(frappe.get_traceback(), _("APS delivery-plan lineage suggestion failed"))


def inherit_delivery_note_lineage(doc, method: str | None = None):
	"""Populate unambiguous DN lineage without adding a logistics validation step."""
	if not is_v2_enabled() or not doc.get("company") or not doc.get("customer"):
		return
	for item in doc.get("items") or []:
		try:
			lineage = _resolve_draft_delivery_note_item_lineage(doc, item)
			if not lineage:
				item.custom_aps_match_method = "Unallocated"
				continue
			item.custom_aps_demand_identity = lineage.get("demand_identity")
			item.custom_aps_customer_schedule_item = lineage.get("schedule_item")
			item.custom_aps_delivery_plan_detail = lineage.get("delivery_plan_detail")
			item.custom_aps_match_method = lineage.get("match_method")
		except Exception:
			# APS lineage is informational at this point. ERPNext's native SO and stock
			# validation remains authoritative and must not be replaced by an APS error.
			item.custom_aps_match_method = "Unallocated"


def validate_delivery_nonblocking(doc, method: str | None = None):
	inherit_delivery_note_lineage(doc, method=method)


def sync_delivery_allocations(
	company: str,
	customer: str | None = None,
	item_codes: list[str] | tuple[str, ...] | None = None,
	source_delivery_note: str | None = None,
) -> dict[str, Any]:
	"""Synchronize V2 delivery lineage; unresolved rows become work-queue items."""
	if not company:
		frappe.throw(_("Company is required for delivery synchronization."), frappe.ValidationError)
	item_codes = sorted({item for item in (item_codes or []) if item})
	savepoint = f"aps_v2_delivery_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	try:
		_lock_scope(company, customer)
		sources = _get_delivery_sources(
			company=company,
			customer=customer,
			item_codes=item_codes,
			source_delivery_note=source_delivery_note,
		)
		if not sources:
			frappe.db.release_savepoint(savepoint)
			return _empty_summary(company, customer)
		source_item_names = sorted({row["source_delivery_note_item"] for row in sources})
		existing_by_source = _get_existing_allocations(source_item_names)
		delivered_running = _get_identity_delivery_totals(exclude_source_items=source_item_names)
		desired = []
		unallocated = []
		for source in sources:
			parts, issue = _allocate_source(
				source,
				delivered_running=delivered_running,
				existing_lineage=existing_by_source.get(source["source_delivery_note_item"]) or [],
			)
			for part in parts:
				desired.append(_build_allocation(source, part, delivered_running))
			if issue:
				unallocated.append(issue)
		ledger = _reconcile_source_allocations(sources, desired)
		queue_summary = _reconcile_unallocated_queue(sources, unallocated)
		identity_names = sorted(
			{row.get("demand_identity") for row in desired if row.get("demand_identity")}
			| {
				row.get("demand_identity")
				for allocations in existing_by_source.values()
				for row in allocations
				if row.get("demand_identity")
			}
		)
		rollup = _rollup_identity_delivery(identity_names)
		frappe.db.release_savepoint(savepoint)
		return {
			"company": company,
			"customer": customer,
			"source_item_count": len(sources),
			"desired_allocation_count": len(desired),
			"unallocated_count": len(unallocated),
			"ledger": ledger,
			"unallocated": queue_summary,
			"rollup": rollup,
			"consistency_runs": [],
		}
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise


def get_schedule_delivery_lower_bounds(
	company: str,
	customer: str,
	schedule_item_names: list[str] | tuple[str, ...],
) -> dict[str, float]:
	requested = sorted({name for name in schedule_item_names or [] if name})
	if not requested:
		return {}
	rows = frappe.get_all(
		"Customer Delivery Schedule Item",
		filters={"name": ("in", requested)},
		fields=["name", "demand_identity", "delivered_qty"],
	)
	identities = {row.get("demand_identity") for row in rows if row.get("demand_identity")}
	totals = _get_identity_delivery_totals(identity_names=identities)
	return {
		row.name: max(flt(totals.get(row.get("demand_identity"))), flt(row.get("delivered_qty")), 0)
		if row.get("demand_identity")
		else max(flt(row.get("delivered_qty")), 0)
		for row in rows
	}


def resolve_unallocated_delivery(*, name: str, demand_identity: str, reason: str) -> dict[str, Any]:
	if not is_v2_enabled():
		frappe.throw(_("APS V2 is disabled."), frappe.PermissionError)
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A resolution reason is required."), frappe.ValidationError)
	queue_doc = frappe.get_doc("APS Unallocated Delivery", name)
	identity = frappe.get_doc("APS Demand Identity", demand_identity)
	if (queue_doc.company, queue_doc.customer, queue_doc.item_code) != (
		identity.company,
		identity.customer,
		identity.item_code,
	):
		frappe.throw(_("The selected Demand Identity does not match this delivery row."), frappe.ValidationError)
	if not identity.current_schedule_item:
		frappe.throw(_("The selected Demand Identity has no current schedule row."), frappe.ValidationError)
	source_parent = frappe.db.get_value(
		"Delivery Note Item", queue_doc.source_delivery_note_item, "parent"
	)
	if not source_parent or source_parent != queue_doc.source_delivery_note:
		frappe.throw(
			_("The source Delivery Note Item no longer belongs to this delivery row."),
			frappe.ValidationError,
		)
	target_parent = frappe.db.get_value(
		"Customer Delivery Schedule Item", identity.current_schedule_item, "parent"
	)
	if not target_parent or target_parent != identity.current_schedule:
		frappe.throw(
			_("The selected Demand Identity current schedule row is no longer valid."),
			frappe.ValidationError,
		)
	frappe.db.set_value(
		"Delivery Note Item",
		queue_doc.source_delivery_note_item,
		{
			"custom_aps_demand_identity": identity.name,
			"custom_aps_customer_schedule_item": identity.current_schedule_item,
			"custom_aps_match_method": "Explicit Identity",
		},
		update_modified=False,
	)
	frappe.db.set_value(
		"APS Unallocated Delivery",
		name,
		{
			"resolved_demand_identity": identity.name,
			"resolved_schedule_item": identity.current_schedule_item,
			"resolution_reason": reason,
			"resolved_by": frappe.session.user,
			"resolved_on": now_datetime(),
		},
		update_modified=False,
	)
	result = sync_delivery_allocations(
		company=queue_doc.company,
		customer=queue_doc.customer,
		item_codes=[queue_doc.item_code],
		source_delivery_note=queue_doc.source_delivery_note,
	)
	return {"name": name, "demand_identity": identity.name, "sync": result}


def _resolve_delivery_plan_qty_lineage(doc, row):
	identity_name = row.get("custom_aps_demand_identity")
	schedule_item = row.get("custom_aps_customer_schedule_item")
	if identity_name:
		target = _get_identity_target(identity_name, allow_cancelled=True)
		if _target_matches(target, doc.company, doc.customer, row.item_code):
			row.custom_aps_customer_schedule_item = target.get("name")
			row.custom_aps_match_method = "Explicit Identity"
			return
	if schedule_item:
		target = _get_schedule_target(schedule_item)
		if _target_matches(target, doc.company, doc.customer, row.item_code):
			row.custom_aps_demand_identity = target.get("demand_identity")
			row.custom_aps_match_method = "Schedule Item"
			return
	date_value = row.get("required_arrival_date") or doc.get("arrival_date") or doc.get("delivery_date")
	candidates = _legacy_candidates(
		company=doc.company,
		customer=doc.customer,
		item_code=row.item_code,
		posting_date=date_value,
	)
	if len(candidates) == 1:
		row.custom_aps_demand_identity = candidates[0]["demand_identity"]
		row.custom_aps_customer_schedule_item = candidates[0]["name"]
		row.custom_aps_match_method = "Suggested"
	else:
		row.custom_aps_match_method = "Unallocated"


def _propagate_delivery_plan_item_lineage(doc, quantity_rows):
	queues = defaultdict(list)
	for qty_row in sorted(quantity_rows, key=lambda row: cint(row.get("idx"))):
		remaining = flt(qty_row.get("staging_qty") or qty_row.get("planned_delivery_qty"))
		queues[qty_row.get("item_code")].append({"row": qty_row, "remaining": remaining})
	for item in sorted(doc.get("items") or [], key=lambda row: cint(row.get("idx"))):
		remaining = flt(item.get("planned_delivery_qty"))
		parts = []
		for source in queues.get(item.get("item_code")) or []:
			if source["remaining"] <= QTY_TOLERANCE or remaining <= QTY_TOLERANCE:
				continue
			qty = min(source["remaining"], remaining)
			parts.append((source["row"], qty))
			source["remaining"] -= qty
			remaining -= qty
		part_identities = {
			part[0].get("custom_aps_demand_identity")
			for part in parts
			if part[0].get("custom_aps_demand_identity")
		}
		if parts and remaining <= QTY_TOLERANCE and len(part_identities) == 1:
			qty_row = parts[0][0]
			item.custom_aps_demand_identity = qty_row.get("custom_aps_demand_identity")
			item.custom_aps_customer_schedule_item = qty_row.get("custom_aps_customer_schedule_item")
			item.custom_aps_delivery_plan_qty = qty_row.get("name") if len(parts) == 1 else None
			item.custom_aps_required_delivery_date = qty_row.get("required_arrival_date")
			item.custom_aps_match_method = "Delivery Plan FIFO"
		else:
			item.custom_aps_demand_identity = None
			item.custom_aps_customer_schedule_item = None
			item.custom_aps_delivery_plan_qty = None
			item.custom_aps_match_method = "Unallocated"


def _resolve_draft_delivery_note_item_lineage(doc, item):
	if cint(doc.get("is_return")):
		return _get_return_lineage(item)
	identity_name = item.get("custom_aps_demand_identity")
	if identity_name:
		target = _get_identity_target(identity_name, allow_cancelled=True)
		if _target_matches(target, doc.company, doc.customer, item.item_code):
			return {"demand_identity": identity_name, "schedule_item": target.get("name"), "match_method": "Explicit Identity"}
	if doc.get("delivery_plan"):
		lineages = _get_delivery_plan_lineages(doc.delivery_plan, item.get("so_detail"), item.get("item_code"))
		identities = {row.get("demand_identity") for row in lineages if row.get("demand_identity")}
		if len(identities) == 1:
			lineage = next(row for row in lineages if row.get("demand_identity"))
			return {**lineage, "match_method": "Delivery Plan"}
	schedule_item = item.get("custom_aps_customer_schedule_item")
	if schedule_item:
		target = _get_schedule_target(schedule_item)
		if _target_matches(target, doc.company, doc.customer, item.item_code):
			return {"demand_identity": target.get("demand_identity"), "schedule_item": schedule_item, "match_method": "Schedule Item"}
	return None


def _get_return_lineage(item):
	original_item = item.get("dn_detail")
	if not original_item:
		return None
	rows = frappe.get_all(
		"APS Delivery Allocation",
		filters={"source_delivery_note_item": original_item, "is_return": 0, "is_effective": 1},
		fields=["demand_identity", "customer_schedule_item"],
	)
	identities = {row.get("demand_identity") for row in rows if row.get("demand_identity")}
	if len(identities) != 1:
		return None
	row = next(row for row in rows if row.get("demand_identity"))
	return {"demand_identity": row.demand_identity, "schedule_item": row.customer_schedule_item, "match_method": "Return Trace"}


def _get_delivery_sources(*, company, customer, item_codes, source_delivery_note):
	conditions = ["dn.company = %(company)s"]
	params: dict[str, Any] = {"company": company}
	if customer:
		conditions.append("dn.customer = %(customer)s")
		params["customer"] = customer
	if item_codes:
		conditions.append("dni.item_code in %(item_codes)s")
		params["item_codes"] = item_codes
	if source_delivery_note:
		conditions.append("dn.name = %(source_delivery_note)s")
		params["source_delivery_note"] = source_delivery_note
	else:
		tracked = set(
			frappe.get_all("APS Delivery Allocation", filters={"company": company}, pluck="source_delivery_note")
			+ frappe.get_all("APS Unallocated Delivery", filters={"company": company, "status": "Open"}, pluck="source_delivery_note")
		)
		if not tracked:
			return []
		conditions.append("dn.name in %(tracked)s")
		params["tracked"] = sorted(tracked)
	rows = frappe.db.sql(
		"""
		select dn.name as source_delivery_note, dn.docstatus as source_docstatus,
			dn.company, dn.customer, dn.posting_date, dn.posting_time, dn.creation,
			dn.is_return, dn.return_against, dn.delivery_plan,
			dni.name as source_delivery_note_item, dni.idx as source_idx,
			dni.item_code, dni.stock_uom, dni.stock_qty, dni.qty,
			dni.against_sales_order as sales_order, dni.so_detail as sales_order_item,
			dni.dn_detail as original_delivery_note_item,
			dni.custom_aps_demand_identity as direct_demand_identity,
			dni.custom_aps_customer_schedule_item as direct_schedule_item,
			dni.custom_aps_delivery_plan_detail as delivery_plan_detail
		from `tabDelivery Note` dn
		inner join `tabDelivery Note Item` dni on dni.parent = dn.name
		where {conditions}
		order by dn.posting_date, dn.posting_time, dn.creation, dni.idx, dni.name
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)
	result = []
	for row in rows:
		qty = abs(flt(row.stock_qty or row.qty))
		if qty <= QTY_TOLERANCE:
			continue
		result.append(
			{
				**dict(row),
				"source_qty": qty,
				"source_posting_time": get_datetime(f"{getdate(row.posting_date)} {row.posting_time or '00:00:00'}"),
			}
		)
	return result


def _get_existing_allocations(source_items):
	grouped = defaultdict(list)
	if not source_items:
		return grouped
	for row in frappe.get_all(
		"APS Delivery Allocation",
		filters={"source_delivery_note_item": ("in", source_items)},
		fields=["name", "allocation_key", "source_delivery_note_item", "demand_identity", "customer_schedule_item", "allocated_qty", "effective_qty", "is_return", "is_effective", "allocation_method", "original_delivery_note_item"],
		order_by="creation, name",
	):
		grouped[row.source_delivery_note_item].append(dict(row))
	return grouped


def _get_identity_delivery_totals(*, identity_names=None, exclude_source_items=None):
	conditions = ["is_effective = 1", "ifnull(demand_identity, '') != ''"]
	params = {}
	if identity_names is not None:
		identity_names = sorted({name for name in identity_names if name})
		if not identity_names:
			return {}
		conditions.append("demand_identity in %(identity_names)s")
		params["identity_names"] = identity_names
	if exclude_source_items:
		conditions.append("source_delivery_note_item not in %(exclude_source_items)s")
		params["exclude_source_items"] = list(exclude_source_items)
	rows = frappe.db.sql(
		"select demand_identity, coalesce(sum(effective_qty), 0) as qty from `tabAPS Delivery Allocation` where {0} group by demand_identity".format(" and ".join(conditions)),
		params,
		as_dict=True,
	)
	return {row.demand_identity: max(flt(row.qty), 0) for row in rows}


def _allocate_source(source, *, delivered_running, existing_lineage):
	if cint(source.get("source_docstatus")) != 1:
		return [], None
	if cint(source.get("is_return")):
		return _allocate_return(source, delivered_running)
	remaining = flt(source.get("source_qty"))
	parts = []
	# Existing effective lineage is an immutable historical fact. A later
	# schedule reduction may make it Excess, but must not rewrite the shipment.
	existing_effective = [row for row in existing_lineage if cint(row.get("is_effective")) and not cint(row.get("is_return")) and row.get("demand_identity")]
	if existing_effective:
		for row in existing_effective:
			target = _get_identity_target(row.get("demand_identity"), allow_cancelled=True)
			qty = min(remaining, flt(row.get("allocated_qty")))
			if target and qty > QTY_TOLERANCE:
				parts.append(_part(target, qty, row.get("allocation_method") or "Explicit Identity", None, "Existing delivery lineage"))
				remaining -= qty
		if remaining <= QTY_TOLERANCE:
			return parts, None
	candidates, method, reason = _resolve_source_candidates(source)
	explicit_single_target = method in {"Demand Identity", "Delivery Plan", "Direct"} and len(candidates) == 1
	for target in candidates:
		available = (
			remaining
			if explicit_single_target
			else max(flt(target.get("effective_qty")) - flt(delivered_running.get(target.get("demand_identity"))), 0)
		)
		qty = min(remaining, available)
		if qty <= QTY_TOLERANCE:
			continue
		parts.append(_part(target, qty, method, None, reason))
		remaining -= qty
		if remaining <= QTY_TOLERANCE:
			break
	if remaining <= QTY_TOLERANCE:
		return parts, None
	return parts, _unallocated_issue(source, remaining, method, reason or _("No unambiguous open APS demand is available."), candidates)


def _allocate_return(source, delivered_running):
	original_items = []
	if source.get("original_delivery_note_item"):
		original_items = [source.get("original_delivery_note_item")]
	elif source.get("return_against"):
		original_items = frappe.get_all(
			"Delivery Note Item",
			filters={"parent": source.get("return_against"), "item_code": source.get("item_code")},
			pluck="name",
		)
	allocations = []
	if original_items:
		allocations = frappe.get_all(
			"APS Delivery Allocation",
			filters={"source_delivery_note_item": ("in", original_items), "is_return": 0, "is_effective": 1},
			fields=["demand_identity", "allocated_qty", "source_delivery_note_item"],
			order_by="creation, name",
		)
	remaining = flt(source.get("source_qty"))
	parts = []
	for allocation in allocations:
		target = _get_identity_target(allocation.demand_identity, allow_cancelled=True)
		available = min(flt(allocation.allocated_qty), flt(delivered_running.get(allocation.demand_identity)))
		qty = min(remaining, available)
		if target and qty > QTY_TOLERANCE:
			parts.append(_part(target, qty, "Return Trace", allocation.source_delivery_note_item, "Original submitted delivery allocation"))
			remaining -= qty
		if remaining <= QTY_TOLERANCE:
			break
	if remaining <= QTY_TOLERANCE:
		return parts, None
	return parts, _unallocated_issue(source, remaining, "Return Trace", _("The return has no unique original APS delivery lineage or exceeds its reversible quantity."), [])


def _resolve_source_candidates(source):
	identity_name = source.get("direct_demand_identity")
	if identity_name:
		target = _get_identity_target(identity_name, allow_cancelled=True)
		if _target_matches(target, source.get("company"), source.get("customer"), source.get("item_code")):
			return [target], "Demand Identity", "Delivery Note Item explicit Demand Identity"
	if source.get("delivery_plan"):
		lineages = _get_delivery_plan_lineages(source.get("delivery_plan"), source.get("sales_order_item"), source.get("item_code"))
		targets = _unique_targets((row.get("demand_identity") for row in lineages), allow_cancelled=True)
		if targets:
			return targets, "Delivery Plan", "Delivery Plan demand lineage"
	if source.get("direct_schedule_item"):
		target = _get_schedule_target(source.get("direct_schedule_item"))
		if _target_matches(target, source.get("company"), source.get("customer"), source.get("item_code")):
			return [target], "Direct", "Legacy explicit schedule-item link"
	candidates = _legacy_candidates(
		company=source.get("company"),
		customer=source.get("customer"),
		item_code=source.get("item_code"),
		posting_date=source.get("posting_date"),
	)
	if not candidates:
		return [], "Legacy Controlled Match", _("No active demand falls in the controlled date window.")
	scopes = {row.get("schedule_scope") for row in candidates}
	if len(scopes) > 1:
		return [], "Legacy Controlled Match", _("More than one schedule scope is eligible; APS will not guess.")
	return candidates, "Legacy Controlled Match", "Company/customer/item controlled date match"


def _legacy_candidates(*, company, customer, item_code, posting_date):
	if not all((company, customer, item_code, posting_date)):
		return []
	tolerance = max(cint(get_v2_settings().get("delivery_legacy_match_tolerance_days")), 0)
	rows = frappe.db.sql(
		"""
		select i.name, i.parent, i.item_code, i.schedule_date,
			i.effective_qty, i.qty, i.demand_identity,
			s.company, s.customer, s.schedule_scope
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where s.status = 'Active' and s.company = %s and s.customer = %s
			and i.item_code = %s and ifnull(i.demand_identity, '') != ''
			and greatest(ifnull(i.effective_qty, i.qty), 0) > %s
			and i.schedule_date <= %s
		order by case when i.schedule_date < %s then 0 when i.schedule_date = %s then 1 else 2 end,
			i.schedule_date, i.idx, i.name
		""",
		(company, customer, item_code, QTY_TOLERANCE, add_days(getdate(posting_date), tolerance), getdate(posting_date), getdate(posting_date)),
		as_dict=True,
	)
	return [dict(row) for row in rows]


def _get_identity_target(identity_name, *, allow_cancelled: bool = False):
	if not identity_name:
		return None
	status_condition = "identity.status in ('Active', 'Cancelled')" if allow_cancelled else "identity.status = 'Active'"
	rows = frappe.db.sql(
		"""
		select i.name, i.parent, i.item_code, i.schedule_date,
			i.effective_qty, i.qty, i.demand_identity,
			s.company, s.customer, s.schedule_scope
		from `tabAPS Demand Identity` identity
		left join `tabCustomer Delivery Schedule Item` i on i.name = identity.current_schedule_item
		left join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where identity.name = %s and {status_condition}
		limit 1
		""".format(status_condition=status_condition),
		identity_name,
		as_dict=True,
	)
	return dict(rows[0]) if rows and rows[0].get("name") else None


def _get_schedule_target(schedule_item):
	if not schedule_item:
		return None
	rows = frappe.db.sql(
		"""
		select i.name, i.parent, i.item_code, i.schedule_date,
			i.effective_qty, i.qty, i.demand_identity,
			s.company, s.customer, s.schedule_scope, s.status as schedule_status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where i.name = %s
		limit 1
		""",
		schedule_item,
		as_dict=True,
	)
	if not rows or rows[0].get("schedule_status") != "Active":
		return None
	return dict(rows[0])


def _target_matches(target, company, customer, item_code):
	return bool(
		target
		and target.get("company") == company
		and target.get("customer") == customer
		and target.get("item_code") == item_code
		and target.get("demand_identity")
	)


def _unique_targets(identity_names, *, allow_cancelled: bool = False):
	result = []
	seen = set()
	for name in identity_names:
		if not name or name in seen:
			continue
		seen.add(name)
		target = _get_identity_target(name, allow_cancelled=allow_cancelled)
		if target:
			result.append(target)
	return result


def _get_delivery_plan_lineages(delivery_plan, sales_order_item, item_code):
	if not delivery_plan or not frappe.db.exists("Delivery Plan", delivery_plan):
		return []
	conditions = ["parent = %(delivery_plan)s", "item_code = %(item_code)s"]
	params = {"delivery_plan": delivery_plan, "item_code": item_code}
	if sales_order_item:
		conditions.append("so_detail = %(sales_order_item)s")
		params["sales_order_item"] = sales_order_item
	rows = frappe.db.sql(
		"""
		select name as delivery_plan_detail,
			custom_aps_demand_identity as demand_identity,
			custom_aps_customer_schedule_item as schedule_item,
			custom_aps_required_delivery_date as required_delivery_date,
			idx
		from `tabDelivery Plan Item`
		where {conditions}
		order by custom_aps_required_delivery_date, idx, name
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)
	return [dict(row) for row in rows]


def _part(target, qty, method, original_item, reason):
	return {"target": target, "qty": qty, "method": method, "original_delivery_note_item": original_item, "reason": reason}


def _build_allocation(source, part, delivered_running):
	target = part["target"]
	qty = flt(part["qty"])
	signed_qty = -qty if cint(source.get("is_return")) else qty
	identity_name = target["demand_identity"]
	delivered_running[identity_name] = max(flt(delivered_running.get(identity_name)) + signed_qty, 0)
	return {
		"allocation_key": _allocation_key(source, target, part.get("original_delivery_note_item")),
		"company": source["company"],
		"customer": source["customer"],
		"item_code": source["item_code"],
		"sales_order": source.get("sales_order"),
		"sales_order_item": source.get("sales_order_item"),
		"schedule_date": target.get("schedule_date"),
		"customer_schedule": target.get("parent"),
		"customer_schedule_item": target.get("name"),
		"demand_identity": identity_name,
		"delivery_plan_detail": source.get("delivery_plan_detail"),
		"match_status": _match_status(part.get("method")),
		"match_reason": part.get("reason"),
		"source_delivery_note": source["source_delivery_note"],
		"source_delivery_note_item": source["source_delivery_note_item"],
		"source_docstatus": cint(source.get("source_docstatus")),
		"source_posting_time": source.get("source_posting_time"),
		"is_return": cint(source.get("is_return")),
		"return_against": source.get("return_against"),
		"original_delivery_note_item": part.get("original_delivery_note_item") or source.get("original_delivery_note_item"),
		"allocation_method": part.get("method"),
		"source_qty": source.get("source_qty"),
		"allocated_qty": qty,
		"effective_qty": signed_qty,
		"cumulative_delivered_qty": delivered_running[identity_name],
		"reversed_qty": 0,
		"is_effective": 1,
		"reversal_reason": "",
		"source_fingerprint": _source_fingerprint(source),
		"last_synced_on": now_datetime(),
	}


def _match_status(method):
	if method == "Return Trace":
		return "Return Trace"
	if method == "Delivery Plan":
		return "Delivery Plan"
	if method == "Legacy Controlled Match":
		return "Legacy Controlled Match"
	return "Explicit Identity"


def _allocation_key(source, target, original_item=None):
	parts = [source["source_delivery_note_item"], target["name"], "Return" if cint(source.get("is_return")) else "Delivery"]
	if cint(source.get("is_return")):
		parts.extend([source.get("return_against") or "", original_item or source.get("original_delivery_note_item") or ""])
	return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _source_fingerprint(source):
	return hashlib.sha256(
		"|".join(
			str(source.get(fieldname) or "")
			for fieldname in (
				"source_delivery_note", "source_delivery_note_item", "source_docstatus", "source_qty",
				"source_posting_time", "is_return", "return_against", "original_delivery_note_item",
				"direct_demand_identity", "direct_schedule_item", "delivery_plan_detail",
			)
		).encode()
	).hexdigest()


def _reconcile_source_allocations(sources, desired):
	source_items = sorted({row["source_delivery_note_item"] for row in sources})
	desired_by_key = {row["allocation_key"]: row for row in desired}
	existing = {
		row.allocation_key: row
		for row in frappe.get_all(
			"APS Delivery Allocation",
			filters={"source_delivery_note_item": ("in", source_items)},
			fields=["name", "allocation_key", "source_delivery_note", "source_delivery_note_item", "effective_qty", "reversed_qty"],
		)
	}
	created = updated = reversed_count = 0
	for key, values in desired_by_key.items():
		if key in existing:
			doc = frappe.get_doc("APS Delivery Allocation", existing[key].name)
			doc.update(values)
			doc.save(ignore_permissions=True)
			updated += 1
		else:
			frappe.get_doc({"doctype": "APS Delivery Allocation", **values}).insert(ignore_permissions=True)
			created += 1
	for key, row in existing.items():
		if key in desired_by_key:
			continue
		doc = frappe.get_doc("APS Delivery Allocation", row.name)
		previous_effective = flt(doc.effective_qty)
		doc.source_docstatus = cint(frappe.db.get_value("Delivery Note", doc.source_delivery_note, "docstatus"))
		doc.effective_qty = 0
		doc.reversed_qty = max(flt(doc.reversed_qty), abs(previous_effective))
		doc.is_effective = 0
		doc.reversal_reason = "Source Delivery Note cancelled" if doc.source_docstatus == 2 else "Source lineage changed"
		doc.last_synced_on = now_datetime()
		doc.save(ignore_permissions=True)
		reversed_count += cint(abs(previous_effective) > QTY_TOLERANCE)
	return {"created": created, "updated": updated, "reversed": reversed_count}


def _unallocated_issue(source, unallocated_qty, method, reason, candidates):
	return {
		"source_delivery_note": source["source_delivery_note"],
		"source_delivery_note_item": source["source_delivery_note_item"],
		"source_qty": flt(source["source_qty"]),
		"matched_qty": max(flt(source["source_qty"]) - flt(unallocated_qty), 0),
		"unallocated_qty": flt(unallocated_qty),
		"company": source["company"],
		"customer": source["customer"],
		"item_code": source["item_code"],
		"stock_uom": source.get("stock_uom"),
		"is_return": cint(source.get("is_return")),
		"posting_date": source.get("posting_date"),
		"reason_code": "RETURN_LINEAGE_UNRESOLVED" if cint(source.get("is_return")) else "DELIVERY_LINEAGE_UNRESOLVED",
		"reason": reason,
		"match_method": method,
		"candidate_json": json.dumps(
			[
				{"demand_identity": row.get("demand_identity"), "schedule_item": row.get("name"), "schedule_date": str(row.get("schedule_date") or "")}
				for row in candidates or []
			],
			ensure_ascii=True,
			sort_keys=True,
		),
		"source_fingerprint": _source_fingerprint(source),
	}


def _reconcile_unallocated_queue(sources, issues):
	issues_by_source = {row["source_delivery_note_item"]: row for row in issues}
	existing = {
		row.source_delivery_note_item: row.name
		for row in frappe.get_all(
			"APS Unallocated Delivery",
			filters={"source_delivery_note_item": ("in", [row["source_delivery_note_item"] for row in sources])},
			fields=["name", "source_delivery_note_item"],
		)
	}
	created = updated = resolved = 0
	for source_item, values in issues_by_source.items():
		values = {**values, "status": "Open", "resolved_demand_identity": None, "resolved_schedule_item": None}
		if source_item in existing:
			frappe.db.set_value("APS Unallocated Delivery", existing[source_item], values, update_modified=False)
			updated += 1
		else:
			frappe.get_doc({"doctype": "APS Unallocated Delivery", **values}).insert(ignore_permissions=True)
			created += 1
	for source in sources:
		source_item = source["source_delivery_note_item"]
		if source_item in issues_by_source or source_item not in existing:
			continue
		status = "Source Cancelled" if cint(source.get("source_docstatus")) == 2 else "Resolved"
		frappe.db.set_value("APS Unallocated Delivery", existing[source_item], {"status": status, "unallocated_qty": 0, "resolved_on": now_datetime()}, update_modified=False)
		resolved += 1
	return {"created": created, "updated": updated, "resolved": resolved}


def _rollup_identity_delivery(identity_names):
	if not identity_names:
		return {"identity_count": 0, "schedule_item_count": 0, "delivered_qty": 0}
	totals = _get_identity_delivery_totals(identity_names=identity_names)
	match_statuses = _get_identity_match_statuses(identity_names)
	identities = frappe.get_all(
		"APS Demand Identity",
		filters={"name": ("in", identity_names)},
		fields=["name", "current_schedule_item"],
	)
	updated = 0
	for identity in identities:
		if not identity.current_schedule_item:
			continue
		row = frappe.db.get_value(
			"Customer Delivery Schedule Item",
			identity.current_schedule_item,
			["effective_qty", "qty", "executed_floor_qty", "produced_qty", "excess_qty"],
			as_dict=True,
		)
		if not row:
			continue
		qty = flt(row.get("effective_qty") if row.get("effective_qty") is not None else row.get("qty"))
		delivered = max(flt(totals.get(identity.name)), 0)
		frappe.db.set_value(
			"Customer Delivery Schedule Item",
			identity.current_schedule_item,
			{
				"delivered_qty": delivered,
				"open_revised_qty": max(qty - delivered, 0),
				"balance_qty": max(qty - delivered, 0),
				"excess_qty": max(
					flt(row.get("excess_qty")),
					max(flt(row.get("executed_floor_qty")), flt(row.get("produced_qty")), delivered) - qty,
					0,
				),
				"delivery_match_status": match_statuses.get(identity.name) or "Not Delivered",
				"status": "Cancelled" if qty <= QTY_TOLERANCE else ("Covered" if delivered >= qty else "Open"),
			},
			update_modified=False,
		)
		updated += 1
	return {"identity_count": len(identities), "schedule_item_count": updated, "delivered_qty": sum(totals.values())}


def _get_identity_match_statuses(identity_names):
	identity_names = sorted({name for name in identity_names or [] if name})
	if not identity_names:
		return {}
	rows = frappe.get_all(
		"APS Delivery Allocation",
		filters={
			"demand_identity": ("in", identity_names),
			"is_effective": 1,
			"is_return": 0,
		},
		fields=["demand_identity", "match_status"],
	)
	precedence = {"Legacy Controlled Match": 1, "Delivery Plan": 2, "Explicit Identity": 3}
	result = {}
	for row in rows:
		current = result.get(row.demand_identity)
		if precedence.get(row.match_status, 0) > precedence.get(current, 0):
			result[row.demand_identity] = row.match_status
	return result


def _lock_scope(company, customer):
	frappe.db.sql("select name from `tabCompany` where name = %s for update", company)
	if customer:
		frappe.db.sql("select name from `tabCustomer` where name = %s for update", customer)


def _empty_summary(company, customer):
	return {
		"company": company,
		"customer": customer,
		"source_item_count": 0,
		"desired_allocation_count": 0,
		"unallocated_count": 0,
		"ledger": {"created": 0, "updated": 0, "reversed": 0},
		"unallocated": {"created": 0, "updated": 0, "resolved": 0},
		"rollup": {"identity_count": 0, "schedule_item_count": 0, "delivered_qty": 0},
		"consistency_runs": [],
	}
