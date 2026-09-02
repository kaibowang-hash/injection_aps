from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime


QTY_TOLERANCE = 0.000001


def get_schedule_delivery_lower_bounds(
	company: str,
	customer: str,
	schedule_item_names: list[str] | tuple[str, ...],
) -> dict[str, float]:
	"""Return authoritative submitted-DN net quantities for active schedule rows.

	The Customer lock makes this suitable for schedule-change validation: it rebuilds
	allocation from submitted physical sources and therefore does not trust an
	asynchronously maintained child-row or ledger cache.
	"""
	from injection_aps.services.v2_flags import is_v2_enabled

	if is_v2_enabled():
		from injection_aps.services.delivery_fulfillment import get_schedule_delivery_lower_bounds as get_v2_lower_bounds

		return get_v2_lower_bounds(company, customer, schedule_item_names)
	requested_names = sorted({name for name in (schedule_item_names or []) if name})
	if not company or not customer or not requested_names:
		return {}
	_lock_delivery_scope(company, customer)
	all_customer_targets = _get_delivery_targets(company, customer=customer)
	requested_targets = [row for row in all_customer_targets if row["name"] in set(requested_names)]
	found_names = {row["name"] for row in requested_targets}
	missing_names = sorted(set(requested_names) - found_names)
	if missing_names:
		frappe.throw(
			_("Customer schedule rows are no longer Active or do not belong to this scope: {0}.").format(
				", ".join(missing_names)
			),
			frappe.ValidationError,
		)
	item_codes = sorted({row["item_code"] for row in requested_targets})
	active_targets = [row for row in all_customer_targets if row["item_code"] in set(item_codes)]
	existing_scope = _get_existing_scope_rows(company, customer=customer, item_codes=item_codes)
	sources = _get_submitted_delivery_sources(
		company,
		customer=customer,
		item_codes=item_codes,
		active_targets=active_targets,
	)
	existing_lineage_by_source = defaultdict(list)
	for row in existing_scope:
		if row.source_delivery_note_item and not cint(row.is_return) and cint(row.is_effective):
			existing_lineage_by_source[row.source_delivery_note_item].append(dict(row))
	settled_source_items, _settled_chains = _find_settled_delivery_history(sources, active_targets)
	desired = _build_desired_delivery_allocations(
		sources,
		active_targets,
		existing_lineage_by_source=existing_lineage_by_source,
		settled_source_items=settled_source_items,
	)
	result = {name: 0.0 for name in requested_names}
	for row in desired:
		target_name = row.get("customer_schedule_item")
		if target_name in result:
			result[target_name] += flt(row.get("effective_qty"))
	return {name: max(flt(qty), 0) for name, qty in result.items()}


def sync_delivery_allocations(
	company: str,
	customer: str | None = None,
	item_codes: list[str] | tuple[str, ...] | None = None,
	target_remap: dict[str, dict[str, Any] | None] | None = None,
	source_delivery_note: str | None = None,
) -> dict[str, Any]:
	"""Rebuild Delivery Note Item allocations for one controlled company/customer/item scope."""
	from injection_aps.services.v2_flags import is_v2_enabled

	# A replacement remap is an explicit legacy-ledger migration operation and
	# must not consult the V2 feature flag before taking that deterministic path.
	# Besides preserving the old contract, this keeps transaction tests and
	# maintenance callers independent from APS Settings reads.
	if not target_remap and is_v2_enabled():
		from injection_aps.services.delivery_fulfillment import sync_delivery_allocations as sync_v2_delivery

		return sync_v2_delivery(
			company=company,
			customer=customer,
			item_codes=item_codes,
			source_delivery_note=source_delivery_note,
		)
	from injection_aps.services import availability, consistency

	if not company:
		frappe.throw(_("Company is required for delivery synchronization."), frappe.ValidationError)
	item_codes = sorted({item for item in (item_codes or []) if item})
	save_point = "aps_delivery_sync_{0}".format(frappe.generate_hash(length=10))
	frappe.db.savepoint(save_point)
	try:
		_lock_delivery_scope(company, customer)
		targets = _get_delivery_targets(company, customer=customer, item_codes=item_codes)
		existing_scope = _get_existing_scope_rows(company, customer=customer, item_codes=item_codes)
		remap_scope_item_codes = _get_target_remap_scope_item_codes(
			company,
			customer=customer,
			item_codes=item_codes,
			target_remap=target_remap,
		)
		linked_source_items = _get_explicitly_aps_linked_item_codes(
			company,
			customer=customer,
			item_codes=item_codes,
			source_delivery_note=source_delivery_note,
		)
		scope_item_codes = sorted(
			{row["item_code"] for row in targets}
			| {row.get("item_code") for row in existing_scope if row.get("item_code")}
			| set(remap_scope_item_codes)
			| set(linked_source_items)
		)
		if not scope_item_codes:
			frappe.db.release_savepoint(save_point)
			return {
				"company": company,
				"customer": customer,
				"source_item_count": 0,
				"settled_history_source_count": 0,
				"settled_history_chains": [],
				"desired_allocation_count": 0,
				"ledger": {"created": 0, "updated": 0, "reversed": 0},
				"rollup": {"schedule_item_count": 0, "delivered_qty": 0},
				"consistency_runs": [],
			}
		sources = _get_submitted_delivery_sources(
			company,
			customer=customer,
			item_codes=scope_item_codes,
			active_targets=targets,
			historical_target_remap=target_remap,
		)
		existing_lineage_by_source = defaultdict(list)
		for row in existing_scope:
			if row.get("source_delivery_note_item") and not cint(row.get("is_return")) and cint(row.get("is_effective")):
				existing_lineage_by_source[row.get("source_delivery_note_item")].append(dict(row))
		settled_source_items, settled_chains = _find_settled_delivery_history(sources, targets)
		desired = _build_desired_delivery_allocations(
			sources,
			targets,
			existing_lineage_by_source=existing_lineage_by_source,
			settled_source_items=settled_source_items,
			target_remap=target_remap,
		)
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
		affected_runs = _get_affected_planning_runs(rollup.get("schedule_items") or [])
		_lock_planning_runs(affected_runs)
		for run_name in affected_runs:
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
			"settled_history_source_count": len(settled_source_items),
			"settled_history_chains": settled_chains,
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
	for item_code in item_codes:
		event_identity = "|".join(
			str(value or "")
			for value in (
				doc.name,
				doc.get("docstatus"),
				doc.get("modified"),
				method,
				doc.company,
				doc.customer,
				item_code,
			)
		)
		frappe.enqueue(
			"injection_aps.services.delivery_sync.sync_delivery_allocations",
			queue="short",
			enqueue_after_commit=True,
			job_id="aps-delivery-sync-{0}-{1}".format(
				doc.company,
				hashlib.sha256(event_identity.encode("utf-8")).hexdigest()[:20],
			),
			deduplicate=True,
			company=doc.company,
			customer=doc.customer,
			item_codes=[item_code],
			source_delivery_note=doc.name,
		)


def retire_delivery_artifacts(doc, method: str | None = None) -> None:
	"""Immediately retire APS rows when their source Delivery Note is cancelled.

	The full allocation rebuild remains queued because it can be expensive, but the
	derived rows must stop looking live before a user can try to delete the cancelled
	Delivery Note.  These database-level updates intentionally do not depend on the
	current user's APS permissions.
	"""
	if not doc.get("name"):
		return

	resolved_on = now_datetime()
	for name in frappe.get_all(
		"APS Unallocated Delivery",
		filters={"source_delivery_note": doc.name},
		pluck="name",
	):
		frappe.db.set_value(
			"APS Unallocated Delivery",
			name,
			{
				"status": "Source Cancelled",
				"unallocated_qty": 0,
				"resolved_on": resolved_on,
			},
			update_modified=False,
		)

	for row in frappe.get_all(
		"APS Delivery Allocation",
		filters={"source_delivery_note": doc.name},
		fields=["name", "effective_qty", "reversed_qty"],
	):
		frappe.db.set_value(
			"APS Delivery Allocation",
			row.name,
			{
				"source_docstatus": 2,
				"effective_qty": 0,
				"reversed_qty": max(flt(row.reversed_qty), abs(flt(row.effective_qty))),
				"is_effective": 0,
				"reversal_reason": "Source Delivery Note cancelled",
				"last_synced_on": resolved_on,
			},
			update_modified=False,
		)


def delete_delivery_artifacts(doc, method: str | None = None) -> None:
	"""Delete APS-derived rows before Frappe checks Delivery Note back-links.

	Delivery users are allowed to remove an erroneous cancelled Delivery Note without
	being granted delete access to internal APS queue or ledger DocTypes.  Both kinds
	of APS rows are fully derived and will be rebuilt from a submitted source.
	"""
	if not doc.get("name"):
		return

	for doctype in ("APS Unallocated Delivery", "APS Delivery Allocation"):
		frappe.db.delete(doctype, {"source_delivery_note": doc.name})


def _lock_delivery_scope(company: str, customer: str | None = None) -> None:
	if customer:
		frappe.db.sql("select name from `tabCustomer` where name = %s for update", customer)
	else:
		frappe.db.sql("select name from `tabCompany` where name = %s for update", company)


def _lock_planning_runs(run_names) -> None:
	for run_name in sorted(set(run_names or [])):
		frappe.db.sql("select name from `tabAPS Planning Run` where name = %s for update", run_name)


def validate_delivery_before_submit(doc, method: str | None = None):
	"""Validate against submitted physical sources while holding the customer transaction lock."""
	from injection_aps.services.v2_flags import is_v2_enabled

	if is_v2_enabled():
		from injection_aps.services.delivery_fulfillment import validate_delivery_nonblocking

		validate_delivery_nonblocking(doc, method=method)
		return
	item_codes = sorted({row.get("item_code") for row in doc.get("items") or [] if row.get("item_code")})
	if not doc.get("company") or not doc.get("customer") or not item_codes:
		return
	_lock_delivery_scope(doc.get("company"), doc.get("customer"))
	active_targets = _get_delivery_targets(
		doc.get("company"),
		customer=doc.get("customer"),
		item_codes=item_codes,
	)
	existing_scope = _get_existing_scope_rows(
		doc.get("company"),
		customer=doc.get("customer"),
		item_codes=item_codes,
	)
	current_sources = _delivery_sources_from_draft(doc)
	active_items = {row["item_code"] for row in active_targets}
	existing_items = {row.item_code for row in existing_scope if row.item_code}
	explicit_items = {
		row["item_code"]
		for row in current_sources
		if row.get("direct_schedule_item") or (cint(row.get("is_return")) and _return_has_aps_trace(row))
	}
	controlled_items = active_items | existing_items | explicit_items
	if not controlled_items:
		return
	active_targets = [row for row in active_targets if row["item_code"] in controlled_items]
	current_sources = [row for row in current_sources if row["item_code"] in controlled_items]
	submitted_sources = _get_submitted_delivery_sources(
		doc.get("company"),
		customer=doc.get("customer"),
		item_codes=sorted(controlled_items),
		active_targets=active_targets,
	)
	sources = sorted(
		[*submitted_sources, *current_sources],
		key=lambda row: (
			get_datetime(row.get("source_posting_time")),
			str(row.get("creation") or ""),
			cint(row.get("source_idx")),
			row.get("source_delivery_note_item") or "",
		),
	)
	existing_lineage_by_source = defaultdict(list)
	for row in existing_scope:
		if row.source_delivery_note_item and not cint(row.is_return) and cint(row.is_effective):
			existing_lineage_by_source[row.source_delivery_note_item].append(dict(row))
	settled_source_items, _settled_chains = _find_settled_delivery_history(sources, active_targets)
	_build_desired_delivery_allocations(
		sources,
		active_targets,
		existing_lineage_by_source=existing_lineage_by_source,
		settled_source_items=settled_source_items,
	)


def _delivery_sources_from_draft(doc):
	posting_time = get_datetime(
		"{0} {1}".format(getdate(doc.get("posting_date")), doc.get("posting_time") or "00:00:00")
	)
	result = []
	for detail in doc.get("items") or []:
		qty = abs(flt(detail.get("stock_qty")) or flt(detail.get("qty")))
		if qty <= QTY_TOLERANCE:
			continue
		result.append(
			{
				"source_delivery_note": doc.get("name") or "New Delivery Note",
				"source_delivery_note_item": detail.get("name") or str(detail.get("idx") or "New Row"),
				"source_docstatus": 0,
				"source_qty": qty,
				"signed_qty": -qty if cint(doc.get("is_return")) else qty,
				"company": doc.get("company"),
				"customer": doc.get("customer"),
				"item_code": detail.get("item_code"),
				"sales_order": detail.get("against_sales_order"),
				"sales_order_item": detail.get("so_detail"),
				"posting_date": doc.get("posting_date"),
				"source_posting_time": posting_time,
				"creation": doc.get("creation"),
				"source_idx": detail.get("idx"),
				"is_return": cint(doc.get("is_return")),
				"return_against": doc.get("return_against"),
				"original_delivery_note_item": detail.get("dn_detail"),
				"direct_schedule_item": detail.get("custom_aps_customer_schedule_item"),
			}
		)
	return result


def _return_has_aps_trace(source) -> bool:
	if source.get("original_delivery_note_item"):
		return bool(_get_existing_original_allocations(source["original_delivery_note_item"]))
	if not source.get("return_against"):
		return False
	original_items = frappe.get_all(
		"Delivery Note Item",
		filters={
			"parent": source["return_against"],
			"item_code": source["item_code"],
			"against_sales_order": source.get("sales_order") or ("is", "not set"),
		},
		pluck="name",
	)
	return any(_get_existing_original_allocations(name) for name in original_items)


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
			i.delivered_qty,
			i.idx,
			s.company,
			s.customer,
			s.status as schedule_status,
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
		fields=[
			"name",
			"allocation_key",
			"item_code",
			"customer_schedule_item",
			"customer_schedule",
			"schedule_date",
			"source_delivery_note",
			"source_delivery_note_item",
			"allocated_qty",
			"allocation_method",
			"is_return",
			"is_effective",
		],
		order_by="source_posting_time asc, schedule_date asc, creation asc",
	)


def _get_explicitly_aps_linked_item_codes(
	company,
	*,
	customer=None,
	item_codes=None,
	source_delivery_note=None,
):
	if not source_delivery_note:
		return []
	conditions = [
		"dn.name = %(source_delivery_note)s",
		"dn.company = %(company)s",
		"""(
			ifnull(dni.custom_aps_customer_schedule_item, '') != ''
			or trace.name is not null
			or return_trace.name is not null
			or exists (
				select 1
				from `tabDelivery Note Item` original_direct
				where ifnull(original_direct.custom_aps_customer_schedule_item, '') != ''
					and (
						original_direct.name = dni.dn_detail
						or (
							original_direct.parent = dn.return_against
							and original_direct.item_code = dni.item_code
							and ifnull(original_direct.against_sales_order, '') = ifnull(dni.against_sales_order, '')
						)
					)
			)
		)""",
	]
	params = {"source_delivery_note": source_delivery_note, "company": company}
	if customer:
		conditions.append("dn.customer = %(customer)s")
		params["customer"] = customer
	if item_codes:
		conditions.append("dni.item_code in %(item_codes)s")
		params["item_codes"] = list(item_codes)
	return frappe.db.sql_list(
		"""
		select distinct dni.item_code
		from `tabDelivery Note` dn
		inner join `tabDelivery Note Item` dni on dni.parent = dn.name
		left join `tabAPS Delivery Allocation` trace
			on trace.source_delivery_note_item = dni.dn_detail
			and trace.is_return = 0
		left join `tabAPS Delivery Allocation` return_trace
			on return_trace.source_delivery_note = dn.return_against
			and return_trace.item_code = dni.item_code
			and return_trace.is_return = 0
		where {conditions}
		""".format(conditions=" and ".join(conditions)),
		params,
	)


def _get_target_remap_scope_item_codes(
	company,
	*,
	customer=None,
	item_codes=None,
	target_remap=None,
):
	old_target_names = sorted(name for name in (target_remap or {}) if name)
	if not old_target_names:
		return []
	conditions = ["i.name in %(target_names)s", "s.company = %(company)s"]
	params: dict[str, Any] = {"target_names": old_target_names, "company": company}
	if customer:
		conditions.append("s.customer = %(customer)s")
		params["customer"] = customer
	if item_codes:
		conditions.append("i.item_code in %(item_codes)s")
		params["item_codes"] = list(item_codes)
	return frappe.db.sql_list(
		"""
		select distinct i.item_code
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where {conditions}
		""".format(conditions=" and ".join(conditions)),
		params,
	)


def _get_submitted_delivery_sources(
	company,
	*,
	customer=None,
	item_codes=None,
	active_targets=None,
	historical_target_remap=None,
):
	# ``active_targets`` is retained for call compatibility, but the database-side
	# correlated EXISTS below deliberately avoids expanding one OR branch per row.
	conditions = ["dn.company = %(company)s", "dn.docstatus = 1", "dni.stock_qty != 0"]
	params: dict[str, Any] = {"company": company, "qty_tolerance": QTY_TOLERANCE}
	if customer:
		conditions.append("dn.customer = %(customer)s")
		params["customer"] = customer
	if item_codes:
		conditions.append("dni.item_code in %(item_codes)s")
		params["item_codes"] = list(item_codes)
	source_conditions = [
		"ifnull(dni.custom_aps_customer_schedule_item, '') != ''",
		"exists (select 1 from `tabAPS Delivery Allocation` linked where linked.source_delivery_note_item = dni.name)",
		"exists (select 1 from `tabAPS Delivery Allocation` traced where traced.is_return = 0 and (traced.source_delivery_note_item = dni.dn_detail or (traced.source_delivery_note = dn.return_against and traced.item_code = dni.item_code and ifnull(traced.sales_order, '') = ifnull(dni.against_sales_order, ''))))",
		"""exists (
			select 1
			from `tabDelivery Note Item` original_direct
			where ifnull(original_direct.custom_aps_customer_schedule_item, '') != ''
				and (
					original_direct.name = dni.dn_detail
					or (
						original_direct.parent = dn.return_against
						and original_direct.item_code = dni.item_code
						and ifnull(original_direct.against_sales_order, '') = ifnull(dni.against_sales_order, '')
					)
				)
		)""",
		"""exists (
			select 1
			from `tabCustomer Delivery Schedule Item` active_item
			inner join `tabCustomer Delivery Schedule` active_schedule
				on active_schedule.name = active_item.parent
			where active_schedule.status = 'Active'
				and active_schedule.company = dn.company
				and active_schedule.customer = dn.customer
				and active_item.item_code = dni.item_code
				and active_item.schedule_date = dn.posting_date
				and active_item.qty > %(qty_tolerance)s
				and ifnull(active_item.sales_order, '') = ifnull(dni.against_sales_order, '')
		)""",
	]
	historical_target_names = sorted(name for name in (historical_target_remap or {}) if name)
	if historical_target_names:
		params["historical_target_names"] = historical_target_names
		source_conditions.append(
			"""exists (
				select 1
				from `tabCustomer Delivery Schedule Item` historical_item
				inner join `tabCustomer Delivery Schedule` historical_schedule
					on historical_schedule.name = historical_item.parent
				where historical_item.name in %(historical_target_names)s
					and historical_schedule.company = dn.company
					and historical_schedule.customer = dn.customer
					and historical_item.item_code = dni.item_code
					and historical_item.schedule_date = dn.posting_date
					and ifnull(historical_item.sales_order, '') = ifnull(dni.against_sales_order, '')
			)"""
		)
	conditions.append("({0})".format(" or ".join(source_conditions)))
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
			dni.idx as source_idx,
			dni.item_code,
			dni.stock_qty,
			dni.qty,
			dni.against_sales_order as sales_order,
			dni.so_detail as sales_order_item,
			dni.dn_detail as original_delivery_note_item,
			original_dni.parent as original_delivery_note,
			dni.custom_aps_customer_schedule_item as direct_schedule_item
		from `tabDelivery Note` dn
		inner join `tabDelivery Note Item` dni on dni.parent = dn.name
		left join `tabDelivery Note Item` original_dni on original_dni.name = dni.dn_detail
		where {conditions}
		order by dn.posting_date asc, dn.posting_time asc, dn.creation asc, dni.idx asc, dni.name asc
		""".format(conditions=" and ".join(conditions)),
		params,
		as_dict=True,
	)
	historical_targets = [
		target
		for target in (_get_schedule_target_by_name(name) for name in historical_target_names)
		if target
	]
	result = []
	for row in rows:
		qty = abs(flt(row.stock_qty or row.qty))
		posting_time = get_datetime(f"{getdate(row.posting_date)} {row.posting_time or '00:00:00'}")
		remap_source_targets = [
			target.get("name")
			for target in historical_targets
			if (target.get("company") or "") == (row.get("company") or "")
			and (target.get("customer") or "") == (row.get("customer") or "")
			and (target.get("item_code") or "") == (row.get("item_code") or "")
			and (target.get("sales_order") or "") == (row.get("sales_order") or "")
			and getdate(target.get("schedule_date")) == getdate(row.get("posting_date"))
		]
		result.append(
			{
				**dict(row),
				"source_qty": qty,
				"signed_qty": -qty if cint(row.is_return) else qty,
				"source_posting_time": posting_time,
				"remap_source_targets": remap_source_targets,
			}
		)
	return result


def _build_desired_delivery_allocations(
	sources,
	active_targets,
	*,
	existing_lineage_by_source=None,
	settled_source_items=None,
	target_remap=None,
):
	existing_lineage_by_source = existing_lineage_by_source or {}
	settled_source_items = set(settled_source_items or [])
	target_remap = target_remap or {}
	active_by_name = {row["name"]: row for row in active_targets}
	all_direct_names = {row.get("direct_schedule_item") for row in sources if row.get("direct_schedule_item")}
	all_targets_by_name = dict(active_by_name)
	remapped_names = {
		row.get("customer_schedule_item")
		for row in target_remap.values()
		if row and row.get("customer_schedule_item")
	}
	for name in all_direct_names | remapped_names:
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
		if source["source_delivery_note_item"] in settled_source_items:
			continue
		if cint(source.get("is_return")):
			parts = _allocate_return_source(
				source,
				normal_allocations_by_source,
				all_targets_by_name,
				return_used,
				target_remap=target_remap,
			)
		else:
			parts = _allocate_delivery_source(
				source,
				targets_by_scope,
				all_targets_by_name,
				delivered_running,
				existing_lineage=existing_lineage_by_source.get(source["source_delivery_note_item"]),
				target_remap=target_remap,
			)
		for target, qty, method, traced_original_item in parts:
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
				"allocation_key": _delivery_allocation_key(
					source,
					target,
					original_delivery_note_item=traced_original_item,
				),
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
				"original_delivery_note_item": traced_original_item or source.get("original_delivery_note_item"),
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
	for target_name, delivered_qty in delivered_running.items():
		target = all_targets_by_name.get(target_name) or _get_schedule_target_by_name(target_name)
		if target and delivered_qty > flt(target.get("qty")) + QTY_TOLERANCE:
			frappe.throw(
				_("Net delivered quantity for APS schedule item {0} exceeds its active quantity by {1}.").format(
					target_name, flt(delivered_qty - flt(target.get("qty")))
				),
				frappe.ValidationError,
			)
	return desired


def _eligible_targets_for_source(source, targets_by_scope):
	eligible = list(
		targets_by_scope.get((source["company"], source["customer"], source["item_code"])) or []
	)
	sales_order = source.get("sales_order") or ""
	return [
		row
		for row in eligible
		if (row.get("sales_order") or "") == sales_order and flt(row.get("qty")) > QTY_TOLERANCE
	]


def _find_settled_delivery_history(sources, active_targets):
	"""Identify fully returned, target-less historical chains without relaxing normal FIFO rules."""
	active_target_names = {row["name"] for row in active_targets}
	targets_by_scope = defaultdict(list)
	for target in active_targets:
		targets_by_scope[(target["company"], target["customer"], target["item_code"])].append(target)
	groups = defaultdict(list)
	for source in sources:
		root_delivery_note = (
			source.get("return_against")
			or source.get("original_delivery_note")
			or source.get("source_delivery_note")
		)
		key = (
			root_delivery_note,
			source.get("company"),
			source.get("customer"),
			source.get("item_code"),
			source.get("sales_order") or "",
		)
		groups[key].append(source)
	settled_source_items = set()
	settled_chains = []
	for key, group in groups.items():
		# A still-active explicit target remains authoritative. A superseded explicit
		# target may be closed only when the complete physical chain is net zero.
		if any(row.get("direct_schedule_item") in active_target_names for row in group):
			continue
		positive_rows = [row for row in group if flt(row.get("signed_qty")) > QTY_TOLERANCE]
		negative_rows = [row for row in group if flt(row.get("signed_qty")) < -QTY_TOLERANCE]
		net_qty = sum(flt(row.get("signed_qty")) for row in group)
		if not positive_rows or not negative_rows or abs(net_qty) > QTY_TOLERANCE:
			continue
		matching_dates = {getdate(row.get("posting_date")) for row in positive_rows}
		compatible_targets = _eligible_targets_for_source(group[0], targets_by_scope)
		if any(getdate(row.get("schedule_date")) in matching_dates for row in compatible_targets):
			continue
		settled_source_items.update(row["source_delivery_note_item"] for row in group)
		settled_chains.append(
			{
				"original_delivery_note": key[0],
				"company": key[1],
				"customer": key[2],
				"item_code": key[3],
				"sales_order": key[4] or None,
				"net_qty": 0,
				"source_items": sorted(row["source_delivery_note_item"] for row in group),
			}
		)
	return settled_source_items, settled_chains


def _allocate_delivery_source(
	source,
	targets_by_scope,
	all_targets_by_name,
	delivered_running,
	*,
	existing_lineage=None,
	target_remap=None,
):
	if existing_lineage:
		return _allocate_existing_delivery_lineage(
			source,
			existing_lineage,
			all_targets_by_name,
			delivered_running,
			target_remap=target_remap,
		)
	direct_name = source.get("direct_schedule_item")
	if direct_name:
		mapping_supplied = direct_name in (target_remap or {})
		mapping = (target_remap or {}).get(direct_name) if mapping_supplied else None
		if mapping_supplied and not mapping:
			frappe.throw(
				_(
					"Direct APS delivery source row {0} has no replacement target; only a fully returned zero-net chain may be settled without one."
				).format(source["source_delivery_note_item"]),
				frappe.ValidationError,
			)
		target_name = mapping.get("customer_schedule_item") if mapping else direct_name
		target = all_targets_by_name.get(target_name) or _get_schedule_target_by_name(target_name)
		_validate_direct_target(source, target)
		_validate_delivery_target_remap(mapping, target)
		available = max(flt(target.get("qty")) - delivered_running[target["name"]], 0)
		if not mapping_supplied and source["source_qty"] > available + QTY_TOLERANCE:
			frappe.throw(
				_("Delivery Note Item {0} exceeds direct schedule item {1} by {2}.").format(
					source["source_delivery_note_item"],
					target["name"],
					flt(source["source_qty"] - available),
				),
				frappe.ValidationError,
			)
		return [(target, source["source_qty"], "Direct", None)]
	remap_source_targets = source.get("remap_source_targets") or []
	if remap_source_targets:
		remaining = flt(source.get("source_qty"))
		parts = []
		for old_target_name in remap_source_targets:
			if old_target_name not in (target_remap or {}) or not target_remap.get(old_target_name):
				frappe.throw(
					_("Submitted Delivery Note Item {0} has an unresolved replacement target.").format(
						source["source_delivery_note_item"]
					),
					frappe.ValidationError,
				)
			mapping = target_remap[old_target_name]
			target_name = mapping.get("customer_schedule_item")
			target = all_targets_by_name.get(target_name) or _get_schedule_target_by_name(target_name)
			_validate_direct_target(source, target)
			_validate_delivery_target_remap(mapping, target)
			available = max(flt(target.get("qty")) - delivered_running[target["name"]], 0)
			qty = min(remaining, available)
			if qty > QTY_TOLERANCE:
				parts.append((target, qty, "Replacement FIFO", None))
				remaining -= qty
			if remaining <= QTY_TOLERANCE:
				break
		if remaining > QTY_TOLERANCE:
			frappe.throw(
				_("Submitted Delivery Note Item {0} exceeds its replacement schedule quantity by {1}.").format(
					source["source_delivery_note_item"],
					flt(remaining),
				),
				frappe.ValidationError,
			)
		return parts
	eligible = _eligible_targets_for_source(source, targets_by_scope)
	if not eligible:
		frappe.throw(
			_(
				"Delivery Note Item {0} has no active schedule target for customer {1}, item {2}, sales order {3}."
			).format(
				source["source_delivery_note_item"],
				source["customer"],
				source["item_code"],
				source.get("sales_order") or "-",
			),
			frappe.ValidationError,
		)
	same_day = [
		row
		for row in eligible
		if getdate(row.get("schedule_date")) == getdate(source.get("posting_date"))
	]
	if not same_day:
		frappe.throw(
			_(
				"Delivery Note Item {0} is not linked to APS and its posting date {1} does not match any active customer schedule date. Select APS Customer Schedule Item explicitly before submit."
			).format(source["source_delivery_note_item"], getdate(source.get("posting_date"))),
			frappe.ValidationError,
		)
	eligible = same_day
	remaining = source["source_qty"]
	parts = []
	for target in eligible:
		available = max(flt(target.get("qty")) - delivered_running[target["name"]], 0)
		qty = min(remaining, available)
		if qty > QTY_TOLERANCE:
			parts.append((target, qty, "Controlled FIFO", None))
			remaining -= qty
		if remaining <= QTY_TOLERANCE:
			break
	if remaining > QTY_TOLERANCE:
		frappe.throw(
			_("Delivery Note Item {0} exceeds the remaining active schedule quantity by {1}.").format(
				source["source_delivery_note_item"], flt(remaining)
			),
			frappe.ValidationError,
		)
	return parts


def _allocate_existing_delivery_lineage(
	source,
	existing_lineage,
	all_targets_by_name,
	delivered_running,
	*,
	target_remap=None,
):
	target_remap = target_remap or {}
	parts_by_target = {}
	lineage_qty = 0.0
	for allocation in existing_lineage:
		old_target_name = allocation.get("customer_schedule_item")
		mapping_supplied = old_target_name in target_remap
		mapping = target_remap.get(old_target_name) if mapping_supplied else None
		if mapping_supplied and not mapping:
			frappe.throw(
				_(
					"Existing APS delivery lineage from source row {0} has no replacement target; only a fully returned zero-net chain may be settled without one."
				).format(source["source_delivery_note_item"]),
				frappe.ValidationError,
			)
		target_name = mapping.get("customer_schedule_item") if mapping else old_target_name
		target = all_targets_by_name.get(target_name) or _get_schedule_target_by_name(target_name)
		_validate_direct_target(source, target)
		if mapping:
			if mapping.get("customer_schedule") and mapping.get("customer_schedule") != target.get("parent"):
				frappe.throw(_("APS delivery target remap parent does not match its target row."), frappe.ValidationError)
			if mapping.get("schedule_date") and getdate(mapping.get("schedule_date")) != getdate(
				target.get("schedule_date")
			):
				frappe.throw(_("APS delivery target remap date does not match its target row."), frappe.ValidationError)
		qty = flt(allocation.get("allocated_qty"))
		if qty <= QTY_TOLERANCE:
			continue
		lineage_qty += qty
		current = parts_by_target.setdefault(
			target_name,
			{
				"target": target,
				"qty": 0.0,
				"method": allocation.get("allocation_method") or "Controlled FIFO",
			},
		)
		current["qty"] += qty
	if abs(lineage_qty - flt(source.get("source_qty"))) > QTY_TOLERANCE:
		frappe.throw(
			_("Existing APS delivery lineage for source row {0} totals {1}, but the submitted source quantity is {2}.").format(
				source["source_delivery_note_item"], lineage_qty, flt(source.get("source_qty"))
			),
			frappe.ValidationError,
		)
	parts = []
	for row in parts_by_target.values():
		target = row["target"]
		# Existing ledger rows are historical facts and can have a gross peak above
		# the replacement quantity when a later traced return offsets them. The
		# caller validates the final net balance after the full source chain.
		parts.append((target, row["qty"], row["method"], None))
	return parts


def _allocate_return_source(
	source,
	normal_by_source,
	all_targets_by_name,
	return_used,
	*,
	target_remap=None,
):
	target_remap = target_remap or {}
	direct_name = source.get("direct_schedule_item")
	if direct_name:
		mapping_supplied = direct_name in target_remap
		mapping = target_remap.get(direct_name) if mapping_supplied else None
		if mapping_supplied and not mapping:
			frappe.throw(
				_(
					"Direct return source row {0} has no replacement target for its APS delivery lineage."
				).format(source["source_delivery_note_item"]),
				frappe.ValidationError,
			)
		target_name = mapping.get("customer_schedule_item") if mapping else direct_name
		target = all_targets_by_name.get(target_name) or _get_schedule_target_by_name(target_name)
		_validate_direct_target(source, target)
		_validate_delivery_target_remap(mapping, target)
		return [
			(
				target,
				source["source_qty"],
				"Return Trace",
				source.get("original_delivery_note_item"),
			)
		]
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
			if target_name in target_remap:
				mapping = target_remap[target_name]
				if not mapping:
					frappe.throw(
						_("Return source row {0} has no replacement target for its original APS delivery lineage.").format(
							source["source_delivery_note_item"]
						),
						frappe.ValidationError,
					)
				target_name = mapping.get("customer_schedule_item")
			target = all_targets_by_name.get(target_name) or _get_schedule_target_by_name(target_name)
			if target:
				_validate_direct_target(source, target)
				traces.append((original_item, target, flt(allocation.get("allocated_qty"))))
	remaining = source["source_qty"]
	parts = []
	for original_item, target, original_qty in traces:
		available = max(original_qty - return_used[(original_item, target["name"])], 0)
		qty = min(remaining, available)
		if qty > QTY_TOLERANCE:
			parts.append((target, qty, "Return Trace", original_item))
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


def _validate_delivery_target_remap(mapping, target) -> None:
	if not mapping:
		return
	if mapping.get("customer_schedule") and mapping.get("customer_schedule") != target.get("parent"):
		frappe.throw(_("APS delivery target remap parent does not match its target row."), frappe.ValidationError)
	if mapping.get("schedule_date") and getdate(mapping.get("schedule_date")) != getdate(
		target.get("schedule_date")
	):
		frappe.throw(_("APS delivery target remap date does not match its target row."), frappe.ValidationError)


def _validate_direct_target(source, target):
	if not target:
		frappe.throw(
			_("Direct customer schedule item {0} does not exist.").format(source.get("direct_schedule_item")),
			frappe.ValidationError,
		)
	if target.get("schedule_status") != "Active":
		frappe.throw(
			_("Direct customer schedule item {0} is not Active.").format(target.get("name")),
			frappe.ValidationError,
		)
	if flt(target.get("qty")) <= QTY_TOLERANCE:
		frappe.throw(
			_("Direct customer schedule item {0} has no positive demand quantity.").format(target.get("name")),
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
	if (target.get("sales_order") or "") != (source.get("sales_order") or ""):
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
			i.name, i.parent, i.item_code, i.sales_order, i.schedule_date, i.qty,
			i.delivered_qty, i.idx,
			s.company, s.customer, s.status as schedule_status, s.creation as schedule_creation
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


def _delivery_allocation_key(source, target, *, original_delivery_note_item=None):
	parts = [
		source["source_delivery_note_item"],
		target["name"],
		"Return" if cint(source.get("is_return")) else "Delivery",
	]
	if cint(source.get("is_return")):
		parts.extend(
			[
				source.get("return_against") or "",
				original_delivery_note_item or source.get("original_delivery_note_item") or "",
			]
		)
	return hashlib.sha256(
		"|".join(parts).encode("utf-8")
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


def _ledger_values_changed(doc, values: dict[str, Any]) -> bool:
	for fieldname, expected in values.items():
		if fieldname == "last_synced_on":
			continue
		actual = doc.get(fieldname)
		if actual in (None, "") and expected in (None, ""):
			continue
		field = doc.meta.get_field(fieldname)
		fieldtype = field.fieldtype if field else ""
		if fieldtype in ("Float", "Currency", "Percent"):
			if abs(flt(actual) - flt(expected)) > QTY_TOLERANCE:
				return True
		elif fieldtype in ("Check", "Int"):
			if cint(actual) != cint(expected):
				return True
		elif fieldtype == "Date":
			if getdate(actual) != getdate(expected):
				return True
		elif fieldtype == "Datetime":
			if get_datetime(actual) != get_datetime(expected):
				return True
		elif str(actual or "") != str(expected or ""):
			return True
	return False


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
			if _ledger_values_changed(doc, values):
				doc.update(values)
				doc.last_synced_on = now_datetime()
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
		reversal_values = {
			"source_docstatus": docstatus,
			"effective_qty": 0,
			"reversed_qty": max(flt(doc.reversed_qty), abs(previous_effective)),
			"is_effective": 0,
			"reversal_reason": "Source Delivery Note cancelled" if docstatus == 2 else "Source is no longer eligible",
		}
		if _ledger_values_changed(doc, reversal_values):
			doc.update(reversal_values)
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
	touched = {
		name
		for name in frappe.get_all(
			"APS Delivery Allocation",
			filters=filters,
			pluck="customer_schedule_item",
		)
		if name
	}
	if not touched:
		return {"schedule_item_count": 0, "delivered_qty": 0, "schedule_items": []}
	rollup_rows = frappe.db.sql(
		"""
		select
			i.name,
			i.qty,
			s.status as schedule_status,
			coalesce(sum(case when a.is_effective = 1 then a.effective_qty else 0 end), 0) as delivered_qty
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		left join `tabAPS Delivery Allocation` a on a.customer_schedule_item = i.name
		where i.name in %(schedule_items)s
		group by i.name, i.qty, s.status
		""",
		{"schedule_items": sorted(touched)},
		as_dict=True,
	)
	rollup_by_item = {row.name: row for row in rollup_rows}
	total_delivered = 0.0
	for schedule_item in sorted(touched):
		row = rollup_by_item.get(schedule_item)
		if not row:
			continue
		delivered_qty = flt(row.delivered_qty)
		delivered_qty = max(delivered_qty, 0)
		qty = flt(row.qty)
		is_active = row.schedule_status == "Active"
		status = (
			"Cancelled"
			if not is_active or qty <= QTY_TOLERANCE
			else "Covered"
			if delivered_qty >= qty
			else "Open"
		)
		frappe.db.set_value(
			"Customer Delivery Schedule Item",
			schedule_item,
			{
				"delivered_qty": delivered_qty,
				"balance_qty": max(qty - delivered_qty, 0) if is_active else 0,
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
