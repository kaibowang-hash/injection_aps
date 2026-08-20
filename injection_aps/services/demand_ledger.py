from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

from injection_aps.services import delivery_fulfillment, run_transition
from injection_aps.services.v2_flags import is_v2_enabled


QTY_TOLERANCE = 0.000001
ACTIVE_COMMITMENT_STATUSES = ("Draft", "Proposed", "Approved", "Released", "In Progress")
OPEN_RUN_STATUSES = ("Draft", "Planned", "Approved", "Work Order Proposed", "Shift Proposed", "Applied")
FORMAL_BACKFILL_RUN_STATUSES = ("Approved", "Work Order Proposed", "Shift Proposed", "Applied")


def calculate_new_plan_qty(schedule_open_qty: float, stock_covered_qty: float, carried_remaining: float) -> float:
	return max(flt(schedule_open_qty) - flt(stock_covered_qty) - flt(carried_remaining), 0)


def allocate_stock_once(
	demands: Iterable[dict[str, Any]], stock_rows: Iterable[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
	"""Allocate a finite warehouse pool in deterministic P0 order."""
	pools = [dict(row) for row in stock_rows]
	for pool in pools:
		pool["available_qty"] = max(flt(pool.get("available_qty")), 0)
	pools.sort(key=lambda row: (row.get("item_code") or "", row.get("warehouse") or ""))
	by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for pool in pools:
		by_item[pool.get("item_code")].append(pool)

	allocations: dict[str, list[dict[str, Any]]] = defaultdict(list)
	ordered_demands = sorted(demands, key=_demand_allocation_key)
	for demand in ordered_demands:
		identity = demand.get("demand_identity")
		remaining = max(flt(demand.get("schedule_open_qty")), 0)
		for pool in by_item.get(demand.get("item_code"), []):
			qty = min(remaining, flt(pool.get("available_qty")))
			if qty <= QTY_TOLERANCE:
				continue
			pool["available_qty"] = max(flt(pool["available_qty"]) - qty, 0)
			remaining = max(remaining - qty, 0)
			allocations[identity].append(
				{"warehouse": pool.get("warehouse"), "allocated_qty": qty, "stock_snapshot": pool.get("snapshot") or {}}
			)
			if remaining <= QTY_TOLERANCE:
				break
	return dict(allocations), pools


def prepare_run_demand_baseline(
	planning_run: str,
	*,
	expected_fingerprint: str | None = None,
	allow_formal: bool = False,
) -> dict[str, Any]:
	if not is_v2_enabled():
		frappe.throw(_("APS V2 is disabled; the V2 demand baseline is unavailable."), frappe.ValidationError)
	run = frappe.get_doc("APS Planning Run", planning_run)
	from injection_aps.services import horizon_status, planning

	windows = horizon_status.run_horizon_values(run, planning.get_settings_dict())
	if run.status not in OPEN_RUN_STATUSES:
		frappe.throw(_("Planning Run {0} is not open for demand preparation.").format(run.name), frappe.ValidationError)
	if run.run_type == "Formal" and not allow_formal:
		frappe.throw(
			_("Formal V2 ownership writes are not enabled in this phase. Use a Trial run."),
			frappe.ValidationError,
		)
	if expected_fingerprint and expected_fingerprint != (run.get("demand_baseline_fingerprint") or ""):
		frappe.throw(_("The run demand baseline changed. Refresh before preparing it again."), frappe.ValidationError)

	_lock_run_scope(run)
	all_demands = _get_active_p0_demands(run)
	_missing_identity_guard(all_demands)
	delivery_floors = delivery_fulfillment.get_schedule_delivery_lower_bounds(
		run.company,
		"",
		[row["schedule_item"] for row in all_demands],
	)
	for row in all_demands:
		row["delivered_qty"] = max(flt(row.get("delivered_qty")), flt(delivery_floors.get(row["schedule_item"])), 0)
		row["schedule_open_qty"] = max(flt(row.get("effective_qty")) - flt(row["delivered_qty"]), 0)
	demands = [row for row in all_demands if row["schedule_open_qty"] > QTY_TOLERANCE]
	terminal_demands = [row for row in all_demands if row["schedule_open_qty"] <= QTY_TOLERANCE]
	identity_names = [row["demand_identity"] for row in demands]

	prior_owners = _get_active_formal_owners(identity_names, exclude_run=run.name)
	owner_supply = {
		identity: run_transition.get_commitment_supply_snapshot(owner)
		for identity, owner in prior_owners.items()
	}
	stock_rows = _get_eligible_stock_rows(run.company, {row["item_code"] for row in demands}, exclude_run=run.name)
	stock_allocations, projected_stock_rows = allocate_stock_once(demands, stock_rows)
	input_snapshot = {
		"run": run.name,
		"company": run.company,
		"demands": [_demand_snapshot(row) for row in sorted(all_demands, key=_demand_allocation_key)],
		"prior_owners": [_owner_snapshot(prior_owners[name], owner_supply[name]) for name in sorted(prior_owners)],
		"stock": [_stock_fingerprint_row(row) for row in stock_rows],
	}
	input_fingerprint = _fingerprint(input_snapshot)
	if input_fingerprint == (run.get("demand_baseline_fingerprint") or "") and frappe.db.exists(
		"APS Demand Commitment", {"planning_run": run.name, "admission_class": "P0", "input_fingerprint": input_fingerprint}
	):
		return get_run_demand_baseline(run.name)

	_release_run_stock_allocations(run.name, reason="Demand baseline rebuilt")
	_supersede_run_p0_commitments(run.name, reason="Demand baseline rebuilt")

	commitments = []
	for demand in sorted(demands, key=_demand_allocation_key):
		identity = demand["demand_identity"]
		stock_parts = stock_allocations.get(identity) or []
		stock_qty = sum(flt(row.get("allocated_qty")) for row in stock_parts)
		prior = prior_owners.get(identity)
		supply = owner_supply.get(identity) or {
			"execution_state": "Reschedulable", "supply_remaining_qty": 0, "anchors": []
		}
		owner_state, formal_owner = _resolve_target_ownership(run, prior, supply, allow_formal=allow_formal)
		remaining_after_stock = max(flt(demand["schedule_open_qty"]) - stock_qty, 0)
		carried_qty = min(remaining_after_stock, flt(supply.get("supply_remaining_qty"))) if prior else 0
		new_plan_qty = calculate_new_plan_qty(demand["schedule_open_qty"], stock_qty, carried_qty)
		overdue_at_run_start, horizon_zone = horizon_status.classify_due_date(
			demand.get("effective_due_date") or demand.get("original_due_date"), windows
		)
		values = {
			"planning_run": run.name,
			"source_run": prior.get("planning_run") if prior else None,
			"company": run.company,
			"customer": demand.get("customer"),
			"item_code": demand.get("item_code"),
			"demand_identity": identity,
			"schedule_item": demand.get("schedule_item"),
			"admission_class": "P0",
			"service_priority": cint(demand.get("service_priority")),
			"owner_state": owner_state,
			"execution_state": supply.get("execution_state") if prior else "Reschedulable",
			"status": "Proposed",
			"formal_owner": formal_owner,
			"original_due_date": demand.get("original_due_date"),
			"effective_due_time": None,
			"overdue_at_run_start": overdue_at_run_start,
			"horizon_zone": horizon_zone,
			"requested_qty": demand["schedule_open_qty"],
			"stock_covered_qty": stock_qty,
			"carried_qty": carried_qty,
			"newly_planned_qty": new_plan_qty,
			"on_time_qty": 0,
			"late_qty": 0,
			"unscheduled_qty": 0,
			"produced_qty": max(flt(demand.get("produced_qty")), 0),
			"delivered_qty": demand["delivered_qty"],
			"remaining_qty": demand["schedule_open_qty"],
			"executed_floor_qty": max(flt(demand.get("executed_floor_qty")), demand["delivered_qty"]),
			"excess_qty": max(flt(demand.get("excess_qty")), 0),
			"source_work_orders_json": json.dumps(_supply_lineage_payload(supply), ensure_ascii=True, sort_keys=True, default=str),
			"source_snapshot_json": json.dumps(_demand_snapshot(demand), ensure_ascii=True, sort_keys=True, default=str),
			"input_fingerprint": input_fingerprint,
			"ownership_fingerprint": _fingerprint(
				{"identity": identity, "run": run.name, "source": prior.get("name") if prior else None, "state": owner_state}
			),
			"idempotency_key": _commitment_key(run.name, "P0", identity),
			"transition_reason": _transition_reason(prior, supply, owner_state),
			"transitioned_by": frappe.session.user,
			"transitioned_on": now_datetime(),
		}
		commitment = _upsert_commitment(values)
		commitments.append(_commitment_dict(commitment))
		for part in stock_parts:
			_upsert_stock_allocation(
				run=run,
				commitment=commitment,
				demand=demand,
				part=part,
				input_fingerprint=input_fingerprint,
			)

	for demand in sorted(terminal_demands, key=_demand_allocation_key):
		prior = frappe.db.get_value(
			"APS Demand Commitment",
			{
				"demand_identity": demand["demand_identity"],
				"planning_run": ("!=", run.name),
				"formal_owner": 1,
				"owner_state": "Owned",
				"status": ("in", ACTIVE_COMMITMENT_STATUSES),
			},
			["name", "planning_run"],
			as_dict=True,
		)
		_upsert_terminal_commitment(run, demand, input_fingerprint=input_fingerprint, prior=prior)

	projected_unallocated = defaultdict(float)
	for row in projected_stock_rows:
		projected_unallocated[row.get("item_code")] += max(flt(row.get("available_qty")), 0)

	from injection_aps.services import demand_admission

	admission = demand_admission.rebuild_admission_candidates(
		run.name,
		p0_commitments=commitments,
		baseline_fingerprint=input_fingerprint,
		projected_unallocated_fg=dict(projected_unallocated),
	)
	admission_by_identity = {
		row.get("demand_identity"): row.get("name")
		for row in admission.get("rows") or [] if row.get("admission_class") == "P0"
	}
	for commitment in commitments:
		if admission_by_identity.get(commitment.get("demand_identity")):
			_set_commitment_values(commitment["name"], {"admission": admission_by_identity[commitment["demand_identity"]]})

	summary = _summarize_commitments(commitments)
	frappe.db.set_value(
		"APS Planning Run",
		run.name,
		{
			"baseline_run": _first_source_run(commitments),
			"demand_baseline_fingerprint": input_fingerprint,
			"baseline_prepared_by": frappe.session.user,
			"baseline_prepared_on": now_datetime(),
			"total_p0_qty": summary["p0_qty"],
			"total_stock_covered_qty": summary["stock_covered_qty"],
			"total_carried_qty": summary["carried_qty"],
			"total_new_plan_qty": summary["new_plan_qty"],
			"carried_commitment_count": summary["carried_commitment_count"],
			"source_run_count": summary["source_run_count"],
		},
		update_modified=True,
	)
	validation = validate_commitment_conservation(run.name)
	if not validation["valid"]:
		frappe.throw(_("Demand baseline failed quantity conservation: {0}").format("; ".join(validation["errors"])), frappe.ValidationError)
	return get_run_demand_baseline(run.name)


def get_run_demand_baseline(planning_run: str) -> dict[str, Any]:
	run = frappe.get_doc("APS Planning Run", planning_run)
	commitments = frappe.get_all(
		"APS Demand Commitment",
		filters={"planning_run": run.name, "status": ("in", ACTIVE_COMMITMENT_STATUSES)},
		fields=[
			"name", "planning_run", "source_run", "company", "customer", "item_code", "demand_identity",
			"schedule_item", "admission", "admission_class", "owner_state", "execution_state", "status",
			"formal_owner", "original_due_date", "requested_qty", "stock_covered_qty", "carried_qty",
			"newly_planned_qty", "produced_qty", "delivered_qty", "remaining_qty", "excess_qty",
			"input_fingerprint", "ownership_fingerprint", "transition_reason",
		],
		order_by="admission_class asc, original_due_date asc, customer asc, item_code asc, demand_identity asc",
		limit_page_length=0,
	)
	terminal_commitments = frappe.get_all(
		"APS Demand Commitment",
		filters={"planning_run": run.name, "admission_class": "P0", "status": ("in", ["Completed", "Excess"])},
		fields=[
			"name", "planning_run", "source_run", "company", "customer", "item_code", "demand_identity",
			"schedule_item", "admission_class", "owner_state", "execution_state", "status",
			"original_due_date", "produced_qty", "delivered_qty", "remaining_qty", "excess_qty",
			"input_fingerprint", "ownership_fingerprint", "transition_reason",
		],
		order_by="original_due_date asc, customer asc, item_code asc, demand_identity asc",
		limit_page_length=0,
	)
	from injection_aps.services import demand_admission

	return {
		"planning_run": run.name,
		"company": run.company,
		"run_type": run.run_type,
		"demand_baseline_fingerprint": run.get("demand_baseline_fingerprint"),
		"admission_fingerprint": run.get("admission_fingerprint"),
		"summary": _summarize_commitments(commitments),
		"commitments": [dict(row) for row in commitments],
		"terminal_commitments": [dict(row) for row in terminal_commitments],
		"terminal_summary": {
			"completed_count": sum(1 for row in terminal_commitments if row.status == "Completed"),
			"excess_count": sum(1 for row in terminal_commitments if row.status == "Excess"),
			"excess_qty": round(sum(flt(row.excess_qty) for row in terminal_commitments), 6),
		},
		"admission": demand_admission.get_demand_admission_candidates(run.name),
		"validation": validate_commitment_conservation(run.name),
	}


def sync_optional_admission_commitments(
	planning_run: str,
	admissions: Iterable[dict[str, Any]],
	decision_fingerprint: str,
) -> None:
	run = frappe.get_doc("APS Planning Run", planning_run)
	optional_due_date = getdate(
		run.get("demand_horizon_end_date")
		or run.get("horizon_end")
		or run.get("planning_date")
		or now_datetime()
	)
	active_keys = set()
	for row in admissions:
		if row.get("admission_class") not in {"P1", "P2"}:
			continue
		key = _commitment_key(run.name, row["admission_class"], row.get("name"))
		active_keys.add(key)
		selected = max(flt(row.get("selected_qty")), 0)
		name = frappe.db.get_value("APS Demand Commitment", {"idempotency_key": key})
		if selected <= QTY_TOLERANCE:
			if name:
				_set_commitment_values(
					name,
					{"status": "Cancelled", "owner_state": "Released", "formal_owner": 0, "remaining_qty": 0, "newly_planned_qty": 0},
				)
			continue
		values = {
			"planning_run": run.name, "company": run.company, "customer": row.get("customer"),
			"item_code": row.get("item_code"), "admission": row.get("name"),
			"admission_class": row.get("admission_class"), "owner_state": "Owned",
			"execution_state": "Reschedulable", "status": "Proposed", "formal_owner": 0,
			"requested_qty": selected, "stock_covered_qty": 0, "carried_qty": 0,
			"newly_planned_qty": selected, "remaining_qty": selected,
			"original_due_date": optional_due_date,
			"effective_due_time": f"{optional_due_date} 23:59:59",
			"input_fingerprint": decision_fingerprint,
			"ownership_fingerprint": _fingerprint({"run": run.name, "admission": row.get("name"), "qty": selected}),
			"idempotency_key": key, "transition_reason": "Selected optional admission",
			"transitioned_by": frappe.session.user, "transitioned_on": now_datetime(),
		}
		_upsert_commitment(values)
	for stale in frappe.get_all(
		"APS Demand Commitment",
		filters={"planning_run": run.name, "admission_class": ("in", ["P1", "P2"]), "status": ("in", ACTIVE_COMMITMENT_STATUSES)},
		fields=["name", "idempotency_key"],
		limit_page_length=0,
	):
		if stale.get("idempotency_key") not in active_keys:
			_set_commitment_values(stale.name, {"status": "Cancelled", "owner_state": "Released", "formal_owner": 0})


def get_selected_optional_planning_rows(
	planning_run: str,
	*,
	customer: str | None = None,
	item_code: str | None = None,
) -> list[Any]:
	"""Project confirmed P1/P2 commitments into the planning input.

	Optional admission is Run-owned and must not be inserted into the shared Net
	Requirement ledger.  These synthetic rows give the scheduler an auditable,
	quantity-exact input while keeping their APS Demand Commitment lineage.
	"""
	run = frappe.get_doc("APS Planning Run", planning_run)
	filters: dict[str, Any] = {
		"planning_run": run.name,
		"admission_class": ("in", ["P1", "P2"]),
		"status": ("in", ACTIVE_COMMITMENT_STATUSES),
		"newly_planned_qty": (">", QTY_TOLERANCE),
	}
	if customer:
		filters["customer"] = customer
	if item_code:
		filters["item_code"] = item_code
	commitments = frappe.get_all(
		"APS Demand Commitment",
		filters=filters,
		fields=[
			"name", "customer", "item_code", "admission", "admission_class",
			"original_due_date", "effective_due_time", "newly_planned_qty",
		],
		order_by="admission_class asc, customer asc, item_code asc, name asc",
		limit_page_length=0,
	)
	rows = []
	for commitment in commitments:
		qty = max(flt(commitment.get("newly_planned_qty")), 0)
		if qty <= QTY_TOLERANCE:
			continue
		due_date = getdate(
			commitment.get("original_due_date")
			or run.get("demand_horizon_end_date")
			or run.get("horizon_end")
			or run.get("planning_date")
		)
		admission_class = commitment.get("admission_class") or "P2"
		snapshot = {
			"source": "APS Demand Admission",
			"admission": commitment.get("admission"),
			"commitment": commitment.get("name"),
			"admission_class": admission_class,
			"confirmed_qty": qty,
		}
		rows.append(
			frappe._dict(
				{
					"name": None,
					"source_doctype": "APS Demand Commitment",
					"source_name": commitment.get("name"),
					"demand_commitment": commitment.get("name"),
					"customer": commitment.get("customer"),
					"sales_order": None,
					"sales_order_item": None,
					"item_code": commitment.get("item_code"),
					"demand_date": due_date,
					"demand_qty": qty,
					"available_stock_qty": 0,
					"open_work_order_qty": 0,
					"planning_qty": qty,
					"minimum_batch_qty": 0,
					"production_strategy": "Auto Balance",
					"demand_confidence": "Forecast",
					"cancellation_risk_percent": 0,
					"prebuild_allowed": 1,
					"max_prebuild_days": 0,
					"net_requirement_qty": qty,
					"reason_text": _(
						"Confirmed {0} optional admission quantity.",
						context="Injection APS",
					).format(admission_class),
					"demand_source": "Sales Order Backlog" if admission_class == "P1" else "Safety Stock",
					"demand_source_snapshot_json": json.dumps(snapshot, ensure_ascii=True, sort_keys=True),
					"fulfillment_baseline_json": json.dumps(
						{"optional_admission": snapshot}, ensure_ascii=True, sort_keys=True
					),
				}
			)
		)
	return rows


def transfer_commitment_owner(
	source_commitment: str,
	target_run: str,
	*,
	reason: str,
	allow_formal: bool = False,
) -> dict[str, Any]:
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("Ownership transfer requires a reason."), frappe.ValidationError)
	source = frappe.get_doc("APS Demand Commitment", source_commitment)
	target = frappe.get_doc("APS Planning Run", target_run)
	if source.company != target.company or not source.demand_identity:
		frappe.throw(_("Commitment transfer scope is invalid."), frappe.ValidationError)
	if target.run_type == "Formal" and not allow_formal:
		frappe.throw(_("Formal V2 ownership writes are disabled."), frappe.ValidationError)
	_lock_run_scope(target)
	frappe.db.sql("select name from `tabAPS Demand Commitment` where name = %s for update", source.name)
	supply = run_transition.get_commitment_supply_snapshot(source)
	if supply["execution_state"] == "Frozen":
		frappe.throw(_("Frozen execution remains owned by its source Run and can only be referenced."), frappe.ValidationError)
	_set_commitment_values(source.name, {"owner_state": "Superseded", "formal_owner": 0, "transition_reason": reason})
	values = {fieldname: source.get(fieldname) for fieldname in _COPY_COMMITMENT_FIELDS}
	values.update(
		{
			"planning_run": target.name, "source_run": source.planning_run, "owner_state": "Owned",
			"formal_owner": 1 if target.run_type == "Formal" else 0, "status": "Proposed",
			"idempotency_key": _commitment_key(target.name, source.admission_class, source.demand_identity or source.admission),
			"ownership_fingerprint": _fingerprint({"source": source.name, "target": target.name, "reason": reason}),
			"transition_reason": reason, "transitioned_by": frappe.session.user, "transitioned_on": now_datetime(),
		}
	)
	return _commitment_dict(_upsert_commitment(values))


def validate_commitment_conservation(planning_run: str) -> dict[str, Any]:
	rows = frappe.get_all(
		"APS Demand Commitment",
		filters={"planning_run": planning_run, "admission_class": "P0", "status": ("in", ACTIVE_COMMITMENT_STATUSES)},
		fields=["name", "demand_identity", "requested_qty", "stock_covered_qty", "carried_qty", "newly_planned_qty"],
		limit_page_length=0,
	)
	errors = []
	for row in rows:
		covered = flt(row.stock_covered_qty) + flt(row.carried_qty) + flt(row.newly_planned_qty)
		if abs(flt(row.requested_qty) - covered) > QTY_TOLERANCE:
			errors.append(f"{row.name}: requested {flt(row.requested_qty)} != covered {covered}")
		allocated = frappe.db.sql(
			"""
			select sum(greatest(ifnull(allocated_qty, 0) - ifnull(released_qty, 0), 0))
			from `tabAPS Stock Coverage Allocation`
			where commitment = %s and status in ('Active', 'Consumed')
			""",
			row.name,
		)[0][0] or 0
		if abs(flt(row.stock_covered_qty) - flt(allocated)) > QTY_TOLERANCE:
			errors.append(f"{row.name}: stock coverage {flt(row.stock_covered_qty)} != allocations {flt(allocated)}")
	duplicate_owners = frappe.db.sql(
		"""
		select demand_identity, count(*) as owner_count
		from `tabAPS Demand Commitment`
		where formal_owner = 1 and owner_state = 'Owned'
			and status in ('Draft', 'Proposed', 'Approved', 'Released', 'In Progress')
			and ifnull(demand_identity, '') != ''
		group by demand_identity having count(*) > 1
		""",
		as_dict=True,
	)
	for row in duplicate_owners:
		errors.append(f"{row.demand_identity}: {row.owner_count} active formal owners")
	return {"valid": not errors, "commitment_count": len(rows), "errors": errors}


def backfill_active_run_commitments() -> dict[str, int]:
	"""Conservatively backfill only unambiguous active Legacy result lineage."""
	if not frappe.db.exists("DocType", "APS Demand Commitment"):
		return {"created": 0, "updated": 0, "conflicts": 0, "skipped": 0}
	runs = frappe.get_all(
		"APS Planning Run",
		filters={"run_type": "Formal", "status": ("in", FORMAL_BACKFILL_RUN_STATUSES)},
		fields=["name", "company", "status", "modified"],
		order_by="modified asc, name asc",
		limit_page_length=0,
	)
	candidates: list[dict[str, Any]] = []
	skipped = 0
	for run in runs:
		results = frappe.get_all(
			"APS Schedule Result",
			filters={"planning_run": run.name},
			fields=["name", "company", "customer", "item_code", "requested_date", "planned_qty", "produced_qty", "delivered_qty", "fulfillment_baseline_json"],
			limit_page_length=0,
		)
		for result in results:
			targets = _legacy_result_targets(result)
			if len(targets) != 1:
				skipped += 1
				continue
			target = targets[0]
			if not target.get("demand_identity"):
				skipped += 1
				continue
			work_orders = frappe.get_all(
				"Work Order", filters={"custom_aps_result_reference": result.name, "docstatus": ("<", 2)},
				fields=["name", "docstatus", "status", "qty", "produced_qty"], limit_page_length=0,
			) if frappe.get_meta("Work Order").has_field("custom_aps_result_reference") else []
			supply = run_transition.classify_supply_anchors(work_orders)
			requested = max(flt(target.get("source_open_qty")), flt(result.get("planned_qty")), 0)
			candidates.append(
				{
					"run": run, "result": result, "target": target, "supply": supply, "requested": requested,
					"idempotency_key": _commitment_key(run.name, "P0", target["demand_identity"]),
				}
			)
	by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for row in candidates:
		by_identity[row["target"]["demand_identity"]].append(row)
	created = updated = conflicts = 0
	for identity, rows in sorted(by_identity.items()):
		# More than one active result claiming the same Identity is ambiguous even
		# when the competing results belong to the same Run.  Do not let the stable
		# idempotency key silently pick the last row.
		conflict = len(rows) > 1
		for row in rows:
			values = _legacy_commitment_values(row, conflict=conflict)
			name = frappe.db.get_value("APS Demand Commitment", {"idempotency_key": values["idempotency_key"]})
			_upsert_commitment(values)
			if name:
				updated += 1
			else:
				created += 1
			if conflict:
				conflicts += 1
	return {"created": created, "updated": updated, "conflicts": conflicts, "skipped": skipped}


def _get_active_p0_demands(run) -> list[dict[str, Any]]:
	conditions = []
	params = {
		"company": run.company,
		"horizon_end": getdate(run.get("demand_horizon_end_date") or run.horizon_end),
	}
	if run.get("planning_customer_filter"):
		conditions.append("and s.customer = %(customer)s")
		params["customer"] = run.get("planning_customer_filter")
	if run.get("planning_item_filter"):
		conditions.append("and i.item_code = %(item_code)s")
		params["item_code"] = run.get("planning_item_filter")
	rows = frappe.db.sql(
		"""
		select i.name as schedule_item, i.demand_identity, i.item_code,
			coalesce(i.original_schedule_date, i.schedule_date) as original_due_date,
			coalesce(i.effective_schedule_date, i.schedule_date) as effective_due_date,
			coalesce(i.effective_qty, i.qty, 0) as effective_qty,
			ifnull(i.delivered_qty, 0) as delivered_qty, ifnull(i.produced_qty, 0) as produced_qty,
			ifnull(i.executed_floor_qty, 0) as executed_floor_qty, ifnull(i.excess_qty, 0) as excess_qty,
			s.customer, s.company, s.schedule_scope
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where s.company = %(company)s and s.status = 'Active'
			and (
				ifnull(i.status, '') != 'Cancelled'
				or ifnull(i.excess_qty, 0) > 0
				or ifnull(i.delivered_qty, 0) > 0
				or ifnull(i.produced_qty, 0) > 0
			)
			and coalesce(i.effective_schedule_date, i.schedule_date) <= %(horizon_end)s
			{scope_conditions}
		order by coalesce(i.original_schedule_date, i.schedule_date), i.demand_identity, i.name
		""".format(scope_conditions="\n\t\t\t".join(conditions)),
		params,
		as_dict=True,
	)
	return [dict(row) for row in rows]


def _upsert_terminal_commitment(run, demand: dict[str, Any], *, input_fingerprint: str, prior=None):
	is_excess = flt(demand.get("excess_qty")) > QTY_TOLERANCE
	state = "Excess" if is_excess else "Completed"
	values = {
		"planning_run": run.name,
		"source_run": prior.get("planning_run") if prior else None,
		"company": run.company,
		"customer": demand.get("customer"),
		"item_code": demand.get("item_code"),
		"demand_identity": demand.get("demand_identity"),
		"schedule_item": demand.get("schedule_item"),
		"admission_class": "P0",
		"owner_state": "Referenced" if prior else "Released",
		"execution_state": state,
		"status": state,
		"formal_owner": 0,
		"original_due_date": demand.get("original_due_date"),
		"requested_qty": 0,
		"stock_covered_qty": 0,
		"carried_qty": 0,
		"newly_planned_qty": 0,
		"produced_qty": max(flt(demand.get("produced_qty")), 0),
		"delivered_qty": max(flt(demand.get("delivered_qty")), 0),
		"remaining_qty": 0,
		"executed_floor_qty": max(flt(demand.get("executed_floor_qty")), flt(demand.get("delivered_qty"))),
		"excess_qty": max(flt(demand.get("excess_qty")), 0),
		"source_snapshot_json": json.dumps(_demand_snapshot(demand), ensure_ascii=True, sort_keys=True, default=str),
		"input_fingerprint": input_fingerprint,
		"ownership_fingerprint": _fingerprint(
			{"identity": demand.get("demand_identity"), "run": run.name, "source": prior.get("name") if prior else None, "state": state}
		),
		"idempotency_key": _commitment_key(run.name, "P0", demand.get("demand_identity")),
		"transition_reason": (
			"Executed quantity exceeds revised demand and is recorded independently as Excess"
			if is_excess else "Demand is completed by delivery or inventory facts"
		),
		"transitioned_by": frappe.session.user,
		"transitioned_on": now_datetime(),
	}
	return _upsert_commitment(values)


def _get_active_formal_owners(identity_names: list[str], *, exclude_run: str) -> dict[str, dict[str, Any]]:
	if not identity_names:
		return {}
	rows = frappe.db.sql(
		"""
		select * from `tabAPS Demand Commitment`
		where demand_identity in %(identities)s and planning_run != %(exclude_run)s
			and formal_owner = 1 and owner_state = 'Owned'
			and status in ('Draft', 'Proposed', 'Approved', 'Released', 'In Progress')
		order by demand_identity, transitioned_on desc, name desc for update
		""",
		{"identities": tuple(sorted(set(identity_names))), "exclude_run": exclude_run},
		as_dict=True,
	)
	result = {}
	for row in rows:
		identity = row.get("demand_identity")
		if identity in result:
			frappe.throw(_("Demand Identity {0} has multiple active Formal owners and requires manual resolution.").format(identity), frappe.ValidationError)
		result[identity] = dict(row)
	return result


def _get_eligible_stock_rows(company: str, item_codes: set[str], *, exclude_run: str) -> list[dict[str, Any]]:
	if not item_codes:
		return []
	from injection_aps.services import availability

	warehouses = availability._get_finished_goods_warehouses(company)
	if not warehouses:
		return []
	rows = frappe.db.sql(
		"""
		select bin.item_code, bin.warehouse, ifnull(bin.actual_qty, 0) as actual_qty,
			ifnull(bin.reserved_qty, 0) as reserved_qty, ifnull(bin.reserved_stock, 0) as reserved_stock,
			ifnull(bin.reserved_qty_for_production, 0) as reserved_qty_for_production,
			ifnull(bin.reserved_qty_for_sub_contract, 0) as reserved_qty_for_sub_contract,
			ifnull(bin.reserved_qty_for_production_plan, 0) as reserved_qty_for_production_plan,
			bin.modified
		from `tabBin` bin
		inner join `tabWarehouse` wh on wh.name = bin.warehouse
		where wh.company = %(company)s and wh.is_group = 0 and wh.disabled = 0
			and bin.warehouse in %(warehouses)s and bin.item_code in %(items)s
		order by bin.item_code, bin.warehouse
		""",
		{"company": company, "warehouses": tuple(warehouses), "items": tuple(sorted(item_codes))},
		as_dict=True,
	)
	claims = frappe.db.sql(
		"""
		select item_code, warehouse,
			sum(greatest(ifnull(allocated_qty, 0) - ifnull(consumed_qty, 0) - ifnull(released_qty, 0), 0)) as claimed_qty
		from `tabAPS Stock Coverage Allocation`
		where company = %(company)s and owner_run != %(exclude_run)s and status = 'Active'
			and item_code in %(items)s
		group by item_code, warehouse
		""",
		{"company": company, "exclude_run": exclude_run, "items": tuple(sorted(item_codes))},
		as_dict=True,
	) if frappe.db.exists("DocType", "APS Stock Coverage Allocation") else []
	claim_map = {(row.item_code, row.warehouse): flt(row.claimed_qty) for row in claims}
	result = []
	for row in rows:
		sales_reserved = max(flt(row.reserved_qty), flt(row.reserved_stock))
		production_reserved = flt(row.reserved_qty_for_production) + flt(row.reserved_qty_for_sub_contract) + flt(row.reserved_qty_for_production_plan)
		hard_available = max(flt(row.actual_qty) - sales_reserved - production_reserved, 0)
		claimed = max(flt(claim_map.get((row.item_code, row.warehouse))), 0)
		result.append(
			{
				"item_code": row.item_code, "warehouse": row.warehouse,
				"available_qty": max(hard_available - claimed, 0),
				"snapshot": {
					"actual_qty": flt(row.actual_qty), "sales_or_stock_reserved_qty": sales_reserved,
					"production_reserved_qty": production_reserved, "other_aps_claim_qty": claimed,
					"bin_modified": str(row.modified or ""),
				},
			}
		)
	return result


def _resolve_target_ownership(run, prior, supply, *, allow_formal: bool) -> tuple[str, int]:
	if not prior:
		return "Owned", 1 if run.run_type == "Formal" and allow_formal else 0
	if run.run_type != "Formal" or supply.get("execution_state") == "Frozen":
		return "Referenced", 0
	if not allow_formal:
		return "Referenced", 0
	_set_commitment_values(
		prior["name"],
		{
			"owner_state": "Superseded", "formal_owner": 0,
			"transition_reason": f"Ownership transferred to {run.name}",
			"transitioned_by": frappe.session.user, "transitioned_on": now_datetime(),
		},
	)
	return "Owned", 1


def _upsert_commitment(values: dict[str, Any]):
	name = frappe.db.get_value("APS Demand Commitment", {"idempotency_key": values["idempotency_key"]})
	if name:
		doc = frappe.get_doc("APS Demand Commitment", name)
		for fieldname, value in values.items():
			doc.set(fieldname, value)
		doc.flags.aps_phase2_transition = True
		doc.save(ignore_permissions=True)
		return doc
	doc = frappe.get_doc({"doctype": "APS Demand Commitment", **values})
	doc.flags.aps_phase2_transition = True
	doc.insert(ignore_permissions=True)
	return doc


def _set_commitment_values(name: str, values: dict[str, Any]) -> None:
	doc = frappe.get_doc("APS Demand Commitment", name)
	for fieldname, value in values.items():
		doc.set(fieldname, value)
	doc.flags.aps_phase2_transition = True
	doc.save(ignore_permissions=True)


def _upsert_stock_allocation(*, run, commitment, demand, part, input_fingerprint):
	key = hashlib.sha256(f"{run.name}|{demand['demand_identity']}|{part['warehouse']}".encode()).hexdigest()
	values = {
		"company": run.company, "item_code": demand["item_code"], "warehouse": part["warehouse"],
		"demand_identity": demand["demand_identity"], "commitment": commitment.name, "owner_run": run.name,
		"allocated_qty": part["allocated_qty"], "consumed_qty": 0, "released_qty": 0, "status": "Active",
		"source_snapshot_time": now_datetime(),
		"source_snapshot_json": json.dumps(part.get("stock_snapshot") or {}, ensure_ascii=True, sort_keys=True, default=str),
		"fingerprint": input_fingerprint, "idempotency_key": key,
	}
	name = frappe.db.get_value("APS Stock Coverage Allocation", {"idempotency_key": key})
	if name:
		doc = frappe.get_doc("APS Stock Coverage Allocation", name)
		for fieldname, value in values.items():
			doc.set(fieldname, value)
	else:
		doc = frappe.get_doc({"doctype": "APS Stock Coverage Allocation", **values})
	doc.flags.aps_phase2_transition = True
	if name:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
	return doc


def _release_run_stock_allocations(run_name: str, *, reason: str) -> None:
	for name in frappe.get_all(
		"APS Stock Coverage Allocation", filters={"owner_run": run_name, "status": "Active"}, pluck="name", limit_page_length=0
	):
		doc = frappe.get_doc("APS Stock Coverage Allocation", name)
		doc.released_qty = max(flt(doc.allocated_qty) - flt(doc.consumed_qty), 0)
		doc.status = "Released"
		doc.flags.aps_phase2_transition = True
		doc.save(ignore_permissions=True)


def _supersede_run_p0_commitments(run_name: str, *, reason: str) -> None:
	for name in frappe.get_all(
		"APS Demand Commitment",
		filters={"planning_run": run_name, "admission_class": "P0", "status": ("in", ACTIVE_COMMITMENT_STATUSES)},
		pluck="name", limit_page_length=0,
	):
		_set_commitment_values(
			name,
			{"owner_state": "Superseded", "formal_owner": 0, "status": "Cancelled", "transition_reason": reason, "transitioned_on": now_datetime()},
		)


def _missing_identity_guard(rows: list[dict[str, Any]]) -> None:
	missing = [row["schedule_item"] for row in rows if not row.get("demand_identity")]
	if missing:
		frappe.throw(
			_("{0} active schedule rows have no Demand Identity. Resolve the Phase 1 migration exceptions first: {1}").format(
				len(missing), ", ".join(missing[:10])
			),
			frappe.ValidationError,
		)


def _demand_allocation_key(row: dict[str, Any]):
	due = getdate(row.get("original_due_date") or row.get("effective_due_date"))
	return (due, getdate(row.get("effective_due_date") or due), -cint(row.get("service_priority")), row.get("demand_identity") or "")


def _demand_snapshot(row: dict[str, Any]) -> dict[str, Any]:
	return {
		"schedule_item": row.get("schedule_item"), "demand_identity": row.get("demand_identity"),
		"customer": row.get("customer"), "item_code": row.get("item_code"),
		"original_due_date": str(row.get("original_due_date") or ""),
		"effective_due_date": str(row.get("effective_due_date") or ""),
		"effective_qty": round(flt(row.get("effective_qty")), 6),
		"delivered_qty": round(flt(row.get("delivered_qty")), 6),
		"schedule_open_qty": round(flt(row.get("schedule_open_qty")), 6),
		"executed_floor_qty": round(flt(row.get("executed_floor_qty")), 6),
		"excess_qty": round(flt(row.get("excess_qty")), 6),
	}


def _owner_snapshot(owner: dict[str, Any], supply: dict[str, Any]) -> dict[str, Any]:
	return {
		"name": owner.get("name"), "run": owner.get("planning_run"), "identity": owner.get("demand_identity"),
		"owner_state": owner.get("owner_state"), "execution_state": supply.get("execution_state"),
		"supply_remaining_qty": round(flt(supply.get("supply_remaining_qty")), 6),
		"work_orders": [row.get("name") for row in supply.get("anchors") or []],
		"unresolved_work_orders": sorted(supply.get("unresolved_work_orders") or []),
	}


def _supply_lineage_payload(supply: dict[str, Any]) -> list[Any]:
	rows = [dict(row) for row in supply.get("anchors") or []]
	known = {row.get("name") for row in rows if row.get("name")}
	rows.extend(
		{"name": name, "unresolved": 1}
		for name in sorted(supply.get("unresolved_work_orders") or [])
		if name not in known
	)
	return rows


def _stock_fingerprint_row(row: dict[str, Any]) -> dict[str, Any]:
	return {
		"item_code": row.get("item_code"), "warehouse": row.get("warehouse"),
		"available_qty": round(flt(row.get("available_qty")), 6), "snapshot": row.get("snapshot") or {},
	}


def _transition_reason(prior, supply, owner_state: str) -> str:
	if not prior:
		return "New P0 demand baseline owner"
	if owner_state == "Referenced":
		return f"References {supply.get('execution_state') or 'existing'} supply from {prior.get('planning_run')}"
	return f"Ownership transferred from {prior.get('planning_run')}"


def _summarize_commitments(rows: Iterable[Any]) -> dict[str, Any]:
	rows = list(rows)
	p0 = [row for row in rows if _get(row, "admission_class") == "P0"]
	source_runs = {_get(row, "source_run") for row in p0 if _get(row, "source_run")}
	return {
		"commitment_count": len(rows), "p0_count": len(p0),
		"p0_qty": round(sum(flt(_get(row, "requested_qty")) for row in p0), 6),
		"stock_covered_qty": round(sum(flt(_get(row, "stock_covered_qty")) for row in p0), 6),
		"carried_qty": round(sum(flt(_get(row, "carried_qty")) for row in p0), 6),
		"new_plan_qty": round(sum(flt(_get(row, "newly_planned_qty")) for row in p0), 6),
		"carried_commitment_count": sum(1 for row in p0 if flt(_get(row, "carried_qty")) > QTY_TOLERANCE),
		"source_run_count": len(source_runs), "source_runs": sorted(source_runs),
	}


def _first_source_run(rows: Iterable[Any]) -> str | None:
	values = sorted({_get(row, "source_run") for row in rows if _get(row, "source_run")})
	return values[0] if len(values) == 1 else None


def _commitment_key(run: str, admission_class: str, source_key: str | None) -> str:
	return hashlib.sha256(f"{run}|{admission_class}|{source_key or ''}".encode()).hexdigest()


def _commitment_dict(doc) -> dict[str, Any]:
	return {fieldname: doc.get(fieldname) for fieldname in (
		"name", "planning_run", "source_run", "company", "customer", "item_code", "demand_identity",
		"schedule_item", "admission", "admission_class", "owner_state", "execution_state", "status",
		"formal_owner", "original_due_date", "requested_qty", "stock_covered_qty", "carried_qty",
		"newly_planned_qty", "produced_qty", "delivered_qty", "remaining_qty", "excess_qty",
		"input_fingerprint", "ownership_fingerprint", "transition_reason",
	)}


def _legacy_result_targets(result) -> list[dict[str, Any]]:
	try:
		baseline = json.loads(result.get("fulfillment_baseline_json") or "{}")
	except (TypeError, ValueError):
		return []
	targets = [dict(row) for row in baseline.get("targets") or [] if isinstance(row, dict) and row.get("customer_schedule_item")]
	if not targets:
		return []
	names = [row["customer_schedule_item"] for row in targets]
	identity_map = {
		row.name: row.demand_identity
		for row in frappe.get_all(
			"Customer Delivery Schedule Item", filters={"name": ("in", names)}, fields=["name", "demand_identity"], limit_page_length=0
		)
	}
	for row in targets:
		row["demand_identity"] = identity_map.get(row["customer_schedule_item"])
	return targets


def _legacy_commitment_values(row: dict[str, Any], *, conflict: bool) -> dict[str, Any]:
	run = row["run"]
	result = row["result"]
	target = row["target"]
	supply = row["supply"]
	requested = row["requested"]
	carried = min(requested, flt(supply.get("supply_remaining_qty")))
	return {
		"planning_run": run.name, "company": run.company, "customer": result.customer,
		"item_code": result.item_code, "demand_identity": target["demand_identity"],
		"schedule_item": target["customer_schedule_item"], "admission_class": "P0",
		"owner_state": "Conflict" if conflict else "Owned", "execution_state": supply.get("execution_state") or "Carried",
		"status": "Approved" if run.status in FORMAL_BACKFILL_RUN_STATUSES else "Proposed",
		"formal_owner": 0 if conflict else 1, "original_due_date": result.requested_date,
		"requested_qty": requested, "stock_covered_qty": 0, "carried_qty": carried,
		"newly_planned_qty": max(requested - carried, 0), "produced_qty": max(flt(result.produced_qty), 0),
		"delivered_qty": max(flt(result.delivered_qty), 0), "remaining_qty": requested,
		"source_work_orders_json": json.dumps(_supply_lineage_payload(supply), ensure_ascii=True, sort_keys=True, default=str),
		"source_snapshot_json": json.dumps({"legacy_result": result.name, "target": target}, ensure_ascii=True, sort_keys=True, default=str),
		"input_fingerprint": _fingerprint({"legacy_result": result.name, "target": target, "requested": requested}),
		"ownership_fingerprint": _fingerprint({"legacy_result": result.name, "identity": target["demand_identity"], "conflict": conflict}),
		# Preserve every ambiguous Legacy claimant for manual resolution.  A shared
		# run/identity key would otherwise overwrite all but the final result.
		"idempotency_key": (
			_commitment_key(run.name, "P0-CONFLICT", result.name)
			if conflict else row["idempotency_key"]
		),
		"transition_reason": "Legacy active Run backfill requires manual owner resolution" if conflict else "Legacy active Run backfill",
		"transitioned_by": frappe.session.user, "transitioned_on": now_datetime(),
	}


def _lock_run_scope(run) -> None:
	frappe.db.sql("select name from `tabCompany` where name = %s for update", run.company)
	frappe.db.sql("select name from `tabAPS Planning Run` where name = %s for update", run.name)


def _fingerprint(value: Any) -> str:
	return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _get(row: Any, fieldname: str):
	return row.get(fieldname) if isinstance(row, dict) else getattr(row, fieldname, None)


_COPY_COMMITMENT_FIELDS = (
	"company", "customer", "item_code", "demand_identity", "schedule_item", "admission", "admission_class",
	"service_priority", "execution_state", "original_due_date", "effective_due_time", "requested_qty",
	"stock_covered_qty", "carried_qty", "newly_planned_qty", "on_time_qty", "late_qty", "unscheduled_qty",
	"produced_qty", "delivered_qty", "remaining_qty", "executed_floor_qty", "excess_qty",
	"source_work_orders_json", "source_snapshot_json", "input_fingerprint",
)
