from __future__ import annotations

import json
from typing import Any, Iterable

import frappe
from frappe.utils import cint, flt


FROZEN_WORK_ORDER_STATUSES = {"In Process"}
COMPLETED_WORK_ORDER_STATUSES = {"Completed", "Closed"}
INACTIVE_WORK_ORDER_STATUSES = {"Cancelled", "Stopped"}
FROZEN_SCHEDULING_STATUSES = {"Material Transfer", "Job Card", "Manufacture"}
QTY_TOLERANCE = 0.000001


def classify_supply_anchors(rows: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
	"""Classify existing execution as supply, never as a new high-priority demand.

	Produced quantity is deliberately excluded from ``supply_remaining_qty``: once
	manufactured, it must be represented by eligible FG stock and a Stock Coverage
	Allocation.  Counting it here as well would violate INV-03.
	"""
	active = []
	for source in rows or []:
		row = dict(source or {})
		status = str(row.get("status") or "").strip()
		if cint(row.get("docstatus")) == 2 or status in INACTIVE_WORK_ORDER_STATUSES:
			continue
		qty = max(flt(row.get("qty")), 0)
		produced = min(max(flt(row.get("produced_qty")), 0), qty)
		remaining = max(qty - produced, 0)
		row.update({"qty": qty, "produced_qty": produced, "remaining_qty": remaining})
		active.append(row)

	if not active:
		return {"execution_state": "Reschedulable", "supply_remaining_qty": 0, "anchors": []}

	remaining_total = sum(row["remaining_qty"] for row in active)
	if remaining_total <= QTY_TOLERANCE or all(
		row.get("status") in COMPLETED_WORK_ORDER_STATUSES for row in active
	):
		state = "Completed"
	elif any(_is_frozen(row) for row in active):
		state = "Frozen"
	else:
		state = "Carried"
	return {
		"execution_state": state,
		"supply_remaining_qty": remaining_total,
		"anchors": sorted(active, key=lambda row: row.get("name") or ""),
	}


def get_commitment_supply_snapshot(commitment: dict[str, Any] | Any) -> dict[str, Any]:
	commitment_name = _value(commitment, "name")
	declared_work_order_names = set(_json_names(_value(commitment, "source_work_orders_json")))
	work_order_names = set(declared_work_order_names)
	if commitment_name and frappe.db.exists("DocType", "Work Order"):
		meta = frappe.get_meta("Work Order")
		if meta.has_field("custom_aps_commitment"):
			work_order_names.update(
				frappe.get_all(
					"Work Order",
					filters={"custom_aps_commitment": commitment_name, "docstatus": ("<", 2)},
					pluck="name",
					limit_page_length=0,
				)
			)
	snapshots = {name: _get_work_order_snapshot(name) for name in sorted(work_order_names)}
	result = classify_supply_anchors(row for row in snapshots.values() if row)
	result["unresolved_work_orders"] = sorted(name for name, row in snapshots.items() if not row)
	if not result["anchors"]:
		result["execution_state"] = _value(commitment, "execution_state") or "Reschedulable"
		# An explicit but unresolved WO lineage must not turn the entire demand
		# remainder into proven supply.  Only an already-audited carried quantity
		# survives that ambiguity.  With no declared WO lineage, the active Formal
		# commitment snapshot itself remains the supply promise.
		result["supply_remaining_qty"] = (
			max(flt(_value(commitment, "carried_qty")), 0)
			if declared_work_order_names
			else max(
				flt(_value(commitment, "carried_qty")),
				flt(_value(commitment, "remaining_qty")),
				0,
			)
		)
	return result


def _get_work_order_snapshot(name: str) -> dict[str, Any] | None:
	if not name or not frappe.db.exists("Work Order", name):
		return None
	fields = ["name", "docstatus", "status", "qty", "produced_qty"]
	meta = frappe.get_meta("Work Order")
	for fieldname in ("custom_aps_run", "custom_aps_result_reference", "custom_aps_locked_for_reschedule"):
		if meta.has_field(fieldname):
			fields.append(fieldname)
	row = frappe.db.get_value("Work Order", name, fields, as_dict=True) or {}
	row = dict(row)
	row["scheduling_statuses"] = _get_scheduling_statuses(name)
	return row


def _get_scheduling_statuses(work_order: str) -> list[str]:
	if not frappe.db.exists("DocType", "Work Order Scheduling"):
		return []
	parents = frappe.get_all(
		"Work Order Scheduling",
		filters={"work_order": work_order, "docstatus": ("<", 2)},
		pluck="name",
		limit_page_length=0,
	)
	if not parents or not frappe.db.exists("DocType", "Scheduling Item"):
		return []
	rows = frappe.get_all(
		"Scheduling Item",
		filters={"parent": ("in", parents)},
		fields=["status", "from_time", "completed_qty", "defect_qty"],
		limit_page_length=0,
	)
	statuses = []
	for row in rows:
		status = row.get("status") or ""
		if status or row.get("from_time") or flt(row.get("completed_qty")) > 0 or flt(row.get("defect_qty")) > 0:
			statuses.append(status or "Started")
	return sorted(set(statuses))


def _is_frozen(row: dict[str, Any]) -> bool:
	return bool(
		row.get("status") in FROZEN_WORK_ORDER_STATUSES
		or cint(row.get("custom_aps_locked_for_reschedule"))
		or flt(row.get("produced_qty")) > QTY_TOLERANCE
		or FROZEN_SCHEDULING_STATUSES.intersection(set(row.get("scheduling_statuses") or []))
		or "Started" in set(row.get("scheduling_statuses") or [])
	)


def _json_names(value: Any) -> list[str]:
	if isinstance(value, list):
		rows = value
	elif not value:
		return []
	else:
		try:
			rows = json.loads(value)
		except (TypeError, ValueError):
			return []
	if not isinstance(rows, list):
		return []
	result = []
	for row in rows:
		name = row.get("name") if isinstance(row, dict) else row
		if name:
			result.append(str(name))
	return result


def _value(source: dict[str, Any] | Any, fieldname: str):
	return source.get(fieldname) if isinstance(source, dict) else getattr(source, fieldname, None)
