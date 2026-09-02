from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Iterable

from frappe.utils import cint, flt, get_datetime, getdate


QTY_TOLERANCE = 0.000001

# The registry is intentionally explicit.  Unknown blocking input fails closed and
# is never silently promoted into a business acknowledgment.
BLOCKER_REGISTRY = {
	"capacity": "Acknowledgment",
	"minimum_batch_qty": "Acknowledgment",
	"prebuild_allowed": "Acknowledgment",
	"max_prebuild_days": "Acknowledgment",
	"item_inventory_limit": "Acknowledgment",
	"warehouse_capacity": "Acknowledgment",
	"stock_retained_capacity": "Acknowledgment",
	"cross_run_finished_goods_stock": "Exclude Only",
	"warehouse_stock_uom": "Exclude Only",
	"fixed_resource_overcommit": "Never Override",
	"locked_workstation_overlap": "Never Override",
	"locked_mold_overlap": "Never Override",
	"bom_cycle": "Never Override",
	"negative_quantity": "Never Override",
	"missing_machine_capability": "Temporary Override",
	"missing_cycle_time": "Temporary Override",
	"machine_mold_compatibility": "Temporary Override",
	"stale_fingerprint": "Never Override",
	"lineage_damage": "Never Override",
}


def calculate_horizon_windows(
	start: Any,
	*,
	demand_days: int,
	freeze_days: int,
	restricted_days: int,
	recovery_days: int,
) -> dict[str, Any]:
	"""Return inclusive natural-date windows and an inclusive solver datetime.

	Demand and Recovery are intentionally separate.  The solver may use the
	recovery end, while demand loading must stop at ``demand_end_date``.
	"""
	start_datetime = get_datetime(start)
	start_date = getdate(start_datetime)
	demand_days = max(cint(demand_days), 1)
	freeze_days = max(cint(freeze_days), 0)
	restricted_days = max(cint(restricted_days), freeze_days)
	recovery_days = max(cint(recovery_days), 0)

	demand_end = start_date + timedelta(days=demand_days - 1)
	freeze_end = start_date + timedelta(days=freeze_days - 1) if freeze_days else None
	restricted_end = (
		start_date + timedelta(days=restricted_days - 1) if restricted_days else None
	)
	recovery_start = demand_end + timedelta(days=1) if recovery_days else None
	recovery_end = demand_end + timedelta(days=recovery_days) if recovery_days else demand_end
	return {
		"start_datetime": start_datetime,
		"start_date": start_date,
		"demand_start_date": start_date,
		"demand_end_date": demand_end,
		"freeze_end_date": freeze_end,
		"restricted_end_date": restricted_end,
		"recovery_start_date": recovery_start,
		"recovery_end_date": recovery_end,
		"solver_end_datetime": datetime.combine(recovery_end, time(23, 59, 59)),
		"demand_days": demand_days,
		"freeze_days": freeze_days,
		"restricted_days": restricted_days,
		"recovery_days": recovery_days,
	}


def run_horizon_values(run: Any, settings: dict[str, Any] | None = None) -> dict[str, Any]:
	settings = settings or {}
	start = _value(run, "horizon_start") or _value(run, "planning_date")
	return calculate_horizon_windows(
		start,
		demand_days=cint(
			_value(run, "horizon_days")
			or settings.get("demand_horizon_days")
			or settings.get("planning_horizon_days")
			or 14
		),
		freeze_days=cint(
			_value(run, "freeze_horizon_days")
			or settings.get("default_freeze_horizon_days")
			or settings.get("freeze_days")
			or 2
		),
		restricted_days=cint(
			_value(run, "restricted_horizon_days")
			or settings.get("default_restricted_horizon_days")
			or 7
		),
		recovery_days=cint(
			_value(run, "recovery_horizon_days")
			or settings.get("default_recovery_horizon_days")
			or 7
		),
	)


def planning_run_window_fields(run: Any, settings: dict[str, Any] | None = None) -> dict[str, Any]:
	windows = run_horizon_values(run, settings)
	return {
		"horizon_days": windows["demand_days"],
		"horizon_end": windows["solver_end_datetime"],
		"demand_horizon_start_date": windows["demand_start_date"],
		"demand_horizon_end_date": windows["demand_end_date"],
		"freeze_horizon_days": windows["freeze_days"],
		"freeze_horizon_end_date": windows["freeze_end_date"],
		"restricted_horizon_days": windows["restricted_days"],
		"restricted_horizon_end_date": windows["restricted_end_date"],
		"recovery_horizon_days": windows["recovery_days"],
		"recovery_horizon_start_date": windows["recovery_start_date"],
		"recovery_horizon_end_date": windows["recovery_end_date"],
	}


def classify_due_date(due_date: Any, windows: dict[str, Any]) -> tuple[int, str]:
	due = getdate(due_date)
	start = windows["demand_start_date"]
	if due < start:
		return 1, "Overdue"
	freeze_end = windows.get("freeze_end_date")
	if freeze_end and due <= freeze_end:
		return 0, "Freeze"
	restricted_end = windows.get("restricted_end_date")
	if restricted_end and due <= restricted_end:
		return 0, "Restricted"
	if due <= windows["demand_end_date"]:
		return 0, "Demand"
	return 0, "Recovery"


def build_material_advisory(demands: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
	rows = []
	for demand in demands or []:
		ready = demand.get("material_ready_qty")
		qty = max(flt(demand.get("qty")), 0)
		status = "Unknown" if ready is None else ("Ready" if flt(ready) + QTY_TOLERANCE >= qty else "Short")
		rows.append(
			{
				"result": demand.get("result"),
				"segment": demand.get("segment"),
				"planned_qty": qty,
				"material_ready_qty": None if ready is None else max(flt(ready), 0),
				"status": status,
			}
		)
	return sorted(rows, key=lambda row: (str(row.get("result") or ""), str(row.get("segment") or "")))


def remove_material_constraints(demands: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Remove raw-material data before balancing, validation and fingerprinting."""
	rows = []
	for source in demands or []:
		row = dict(source)
		for fieldname in (
			"material_ready_qty",
			"material_schedulable_qty",
			"material_requirements",
			"resource_material_consumption_qty",
			"existing_work_order_material_credit_qty",
		):
			row.pop(fieldname, None)
		row["material_advisory_only"] = 1
		rows.append(row)
	return rows


def blocker_policy(key: str | None) -> str:
	key = str(key or "")
	if key.startswith("missing_net_requirement_evidence"):
		return "Never Override"
	if key.startswith("bom_cycle"):
		return "Never Override"
	return BLOCKER_REGISTRY.get(key, "Never Override")


def classify_v2_analysis(
	analysis: dict[str, Any], *, optional_admission_qty: float = 0, excluded_qty: float = 0
) -> dict[str, Any]:
	"""Normalize Legacy balance evidence into the V2 readiness state machine."""
	hard_blockers = []
	acknowledgments = []
	for row in analysis.get("demands") or []:
		row_hard = []
		row_ack = []
		for check in row.get("checks") or []:
			if check.get("status") not in {"blocked", "failed", "warning"}:
				continue
			key = check.get("key") or "unknown"
			policy = "Acknowledgment" if check.get("status") == "warning" else blocker_policy(key)
			evidence = {
				"key": key,
				"policy": policy,
				"message": check.get("message") or "",
				"result": row.get("result"),
				"segment": row.get("segment"),
				"planned_qty": flt(row.get("planned_qty")),
			}
			if policy == "Acknowledgment":
				row_ack.append(evidence)
			else:
				row_hard.append(evidence)
		if flt(row.get("late_qty")) > QTY_TOLERANCE:
			row_ack.append(_risk(row, "late_qty", "Late quantity requires business acknowledgment."))
		if flt(row.get("unscheduled_qty")) > QTY_TOLERANCE:
			row_ack.append(_risk(row, "unscheduled_qty", "Critical unplanned quantity requires business acknowledgment."))
		if flt(row.get("prebuild_qty")) > QTY_TOLERANCE:
			row_ack.append(_risk(row, "prebuild_qty", "Prebuild quantity requires business acknowledgment."))
		if row_hard:
			row["status"] = "Hard Blocked"
			row["requires_confirmation"] = 0
			hard_blockers.extend(row_hard)
		elif row_ack or cint(row.get("requires_confirmation")):
			row["status"] = "Acknowledgment Required"
			row["requires_confirmation"] = 1
			acknowledgments.extend(row_ack)
		else:
			row["status"] = "Ready"
			row["requires_confirmation"] = 0

	if flt(optional_admission_qty) > QTY_TOLERANCE:
		acknowledgments.append(
			{"key": "optional_admission", "policy": "Acknowledgment", "message": "Selected P1/P2 quantity requires acknowledgment.", "planned_qty": flt(optional_admission_qty)}
		)
	if flt(excluded_qty) > QTY_TOLERANCE:
		acknowledgments.append(
			{"key": "excluded_commitment", "policy": "Acknowledgment", "message": "Excluded commitment remains Critical Unplanned for the next run.", "planned_qty": flt(excluded_qty)}
		)
	summary = analysis.setdefault("summary", {})
	summary["hard_blocker_count"] = len(hard_blockers)
	summary["acknowledgment_count"] = len(acknowledgments)
	summary["blocked_demands"] = sum(1 for row in analysis.get("demands") or [] if row.get("status") == "Hard Blocked")
	summary["requires_confirmation"] = len(acknowledgments)
	analysis["hard_blockers"] = hard_blockers
	analysis["acknowledgments"] = acknowledgments
	analysis["readiness_status"] = (
		"Hard Blocked" if hard_blockers else "Acknowledgment Required" if acknowledgments else "Ready"
	)
	analysis["next_action"] = {
		"Ready": "Apply the analyzed schedule.",
		"Acknowledgment Required": "Review and acknowledge risks, then Apply.",
		"Hard Blocked": "Open Constraint Resolution Center to fix, override, or exclude the affected commitment.",
	}[analysis["readiness_status"]]
	return analysis


def _risk(row: dict[str, Any], key: str, message: str) -> dict[str, Any]:
	return {
		"key": key,
		"policy": "Acknowledgment",
		"message": message,
		"result": row.get("result"),
		"segment": row.get("segment"),
		"planned_qty": flt(row.get("planned_qty")),
	}


def _value(source: Any, fieldname: str) -> Any:
	return source.get(fieldname) if isinstance(source, dict) else getattr(source, fieldname, None)
