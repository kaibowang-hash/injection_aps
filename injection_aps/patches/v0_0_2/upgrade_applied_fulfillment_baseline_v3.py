from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, flt


def execute():
	"""Make historical Applied stock claims durable when the source NR survives.

	APS Net Requirements are transient rebuild artefacts. Version-2 Result baselines
	did not copy their demand/stock quantities, so this patch upgrades only rows for
	which the original Net Requirement is still available. Rows whose source was
	already deleted are intentionally left unchanged; runtime guards reject them
	instead of guessing historical inventory.
	"""
	if not _schema_is_ready():
		return

	rows = frappe.db.sql(
		"""
		select
			result.name,
			result.fulfillment_baseline_json,
			net_requirement.name as live_net_requirement,
			net_requirement.demand_qty,
			net_requirement.available_stock_qty,
			net_requirement.open_work_order_qty,
			net_requirement.existing_work_order_policy
		from `tabAPS Schedule Result` result
		inner join `tabAPS Planning Run` run on run.name = result.planning_run
		left join `tabAPS Net Requirement` net_requirement
			on net_requirement.name = result.net_requirement
		where (
			run.capacity_balance_status = 'Applied'
			or result.status = 'Applied'
		)
		order by result.name
		""",
		as_dict=True,
	)
	for row in rows:
		baseline = _parse_baseline(row.get("fulfillment_baseline_json"))
		if not baseline or _has_complete_v3_evidence(baseline):
			continue
		if cint(baseline.get("version")) not in (2, 3) or not row.get("live_net_requirement"):
			continue
		if not _net_requirement_quantities_are_valid(
			row.get("demand_qty"),
			row.get("available_stock_qty"),
			row.get("open_work_order_qty"),
			row.get("existing_work_order_policy"),
		):
			continue
		upgraded = _upgrade_baseline(
			baseline,
			demand_qty=row.get("demand_qty"),
			available_stock_qty=row.get("available_stock_qty"),
			open_work_order_qty=row.get("open_work_order_qty"),
			existing_work_order_policy=row.get("existing_work_order_policy"),
		)
		frappe.db.set_value(
			"APS Schedule Result",
			row.get("name"),
			"fulfillment_baseline_json",
			json.dumps(
				upgraded,
				ensure_ascii=True,
				sort_keys=True,
				separators=(",", ":"),
				default=str,
			),
			update_modified=False,
		)


def _schema_is_ready() -> bool:
	for doctype in ("APS Schedule Result", "APS Planning Run", "APS Net Requirement"):
		if not frappe.db.exists("DocType", doctype):
			return False
	required_fields = {
		"APS Schedule Result": ("fulfillment_baseline_json", "net_requirement", "planning_run", "status"),
		"APS Planning Run": ("capacity_balance_status",),
		"APS Net Requirement": (
			"demand_qty",
			"available_stock_qty",
			"open_work_order_qty",
			"existing_work_order_policy",
		),
	}
	return all(
		frappe.get_meta(doctype).has_field(fieldname)
		for doctype, fieldnames in required_fields.items()
		for fieldname in fieldnames
	)


def _parse_baseline(value: Any) -> dict[str, Any]:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			return {}
	return dict(value) if isinstance(value, dict) else {}


def _has_complete_v3_evidence(baseline: dict[str, Any]) -> bool:
	evidence = baseline.get("net_requirement")
	if cint(baseline.get("version")) < 3 or not isinstance(evidence, dict):
		return False
	if not {
		"demand_qty",
		"available_stock_qty",
		"open_work_order_qty",
		"existing_work_order_policy",
	}.issubset(evidence):
		return False
	return _net_requirement_quantities_are_valid(
		evidence.get("demand_qty"),
		evidence.get("available_stock_qty"),
		evidence.get("open_work_order_qty"),
		evidence.get("existing_work_order_policy"),
	)


def _net_requirement_quantities_are_valid(
	demand_qty: Any,
	available_stock_qty: Any,
	open_work_order_qty: Any,
	existing_work_order_policy: Any,
) -> bool:
	demand_qty = flt(demand_qty)
	available_stock_qty = flt(available_stock_qty)
	open_work_order_qty = flt(open_work_order_qty)
	return bool(
		demand_qty >= 0
		and 0 <= available_stock_qty <= demand_qty
		and 0 <= open_work_order_qty <= demand_qty
		and available_stock_qty + open_work_order_qty <= demand_qty
		and existing_work_order_policy in {"Include", "Exclude"}
	)


def _upgrade_baseline(
	baseline: dict[str, Any],
	*,
	demand_qty: Any,
	available_stock_qty: Any,
	open_work_order_qty: Any,
	existing_work_order_policy: str,
) -> dict[str, Any]:
	upgraded = dict(baseline)
	upgraded["version"] = 3
	upgraded["net_requirement"] = {
		"demand_qty": max(flt(demand_qty), 0),
		"available_stock_qty": max(flt(available_stock_qty), 0),
		"open_work_order_qty": max(flt(open_work_order_qty), 0),
		"existing_work_order_policy": existing_work_order_policy,
	}
	return upgraded
