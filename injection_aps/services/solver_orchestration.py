from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime

from injection_aps.services import (
	bom_planning,
	campaign_planning,
	capacity_balance,
	constraint_resolution,
	horizon_status,
	planning,
	v2_baseline,
	v2_flags,
)
from injection_aps.services.solver.input_builder import build_solver_input
from injection_aps.services.solver.scenarios import solve_scenarios
from injection_aps.services.solver.serialization import (
	canonical_json,
	fingerprint,
	input_from_dict,
	input_to_dict,
	solution_from_dict,
	solution_to_dict,
)
from injection_aps.services.solver.validator import validate_solution


PINNED_ORTOOLS_VERSION = "9.4.1874"
QTY_TOLERANCE = 0.000001


class SolverInputBlocked(frappe.ValidationError):
	pass


def analyze_v2_schedule(run_name: str, *, run_in_background: bool = True) -> dict[str, Any]:
	_require_v2_solver()
	run = frappe.get_doc("APS Planning Run", run_name)
	try:
		snapshot = build_solver_input(_build_normalized_source(run))
	except (bom_planning.BOMCycleError, bom_planning.BOMInputError) as exc:
		code = "bom_cycle" if isinstance(exc, bom_planning.BOMCycleError) else "bom_input"
		constraint_resolution.sync_solver_input_blocker(run, blocker_key=code, message=str(exc))
		_set_run_solver_values(run.name, {
			"solver_status": "Failed", "solver_phase": "Input Snapshot", "capacity_balance_status": "Hard Blocked",
			"unresolved_blocker_count": 1,
		})
		raise SolverInputBlocked(str(exc)) from exc
	constraint_resolution.sync_solver_input_blocker(run, blocker_key=None, message=None)
	input_data = input_to_dict(snapshot)
	input_hash = fingerprint(input_data)
	idempotency_key = fingerprint({"run": run.name, "input": input_hash, "engine": "CP-SAT", "schema": snapshot.schema_version})
	existing = frappe.db.get_value("APS Solver Job", {"idempotency_key": idempotency_key}, "name")
	if existing:
		return get_solver_job(existing)
	audit = {"requested_by": frappe.session.user, "requested_on": now_datetime()}
	if run.run_type == "Trial":
		# A Trial is a read-only comparison run. Preserve the Legacy facts before
		# projecting V2 metrics so the two engines remain directly comparable.
		audit["legacy_trial_baseline"] = v2_baseline.capture_legacy_trial_baseline(run.name)
	job = frappe.get_doc({
		"doctype": "APS Solver Job",
		"planning_run": run.name,
		"company": run.company,
		"plant_floor": run.get("plant_floor"),
		"status": "Queued",
		"engine": "CP-SAT",
		"phase": "Input Snapshot",
		"progress_percent": 5,
		"queued_on": now_datetime(),
		"input_fingerprint": input_hash,
		"idempotency_key": idempotency_key,
		"input_snapshot_json": canonical_json(input_data),
		"audit_json": canonical_json(audit),
	})
	job.flags.aps_solver_transition = True
	job.insert(ignore_permissions=True)
	_set_run_solver_values(run.name, {"solver_engine_used": "CP-SAT", "solver_status": "Queued", "solver_phase": "Input Snapshot", "solver_input_fingerprint": input_hash, "solver_job": job.name})
	if run_in_background and not _async_disabled():
		frappe.enqueue(
			"injection_aps.services.solver_orchestration.execute_solver_job",
			queue="long",
			job_name=f"aps-solver:{job.name}",
			enqueue_after_commit=True,
			solver_job=job.name,
		)
		return get_solver_job(job.name)
	return execute_solver_job(job.name)


def execute_solver_job(solver_job: str) -> dict[str, Any]:
	job = frappe.get_doc("APS Solver Job", solver_job)
	if job.status in {"Optimal", "Feasible", "Fallback", "Applied"} and job.scenarios_json:
		return get_solver_job(job.name)
	if cint(job.cancel_requested):
		_update_job(job.name, {"status": "Cancelled", "phase": "Cancelled", "completed_on": now_datetime(), "progress_percent": 100})
		return get_solver_job(job.name)
	started = now_datetime()
	_update_job(job.name, {"status": "Running", "phase": "Capacity Allocation", "started_on": started, "progress_percent": 15})
	_set_run_solver_values(job.planning_run, {"solver_status": "Running", "solver_phase": "Capacity Allocation", "solver_started_on": started})
	try:
		snapshot = input_from_dict(json.loads(job.input_snapshot_json or "{}"))
		solutions = solve_scenarios(snapshot)
		valid = [row for row in solutions if row.status != "Failed" and dict(row.validation).get("valid")]
		if not valid:
			raise SolverInputBlocked("No validated solver scenario is available.")
		selected = next((row for row in valid if row.scenario_key == "recommended"), valid[0])
		status = selected.status if selected.status in {"Optimal", "Feasible", "Fallback"} else "Feasible"
		runtime = max((now_datetime() - started).total_seconds(), 0)
		_update_job(job.name, {
			"status": status,
			"engine": selected.engine,
			"phase": "Validated",
			"progress_percent": 100,
			"completed_on": now_datetime(),
			"runtime_seconds": runtime,
			"scenario_count": len(solutions),
			"selected_scenario": selected.scenario_key,
			"solution_fingerprint": selected.solution_fingerprint,
			"best_bound": selected.best_bound,
			"objective_value": selected.objective_value,
			"gap_percent": selected.gap_percent,
			"scenarios_json": canonical_json([solution_to_dict(row) for row in solutions]),
		})
		_set_run_solver_values(job.planning_run, {"solver_completed_on": now_datetime()})
		_persist_selected_projection(job.planning_run, job.name, selected)
	except Exception as exc:
		_update_job(job.name, {
			"status": "Failed",
			"phase": "Failed",
			"progress_percent": 100,
			"completed_on": now_datetime(),
			"runtime_seconds": max((now_datetime() - started).total_seconds(), 0),
			"error_code": exc.__class__.__name__,
			"error_message": str(exc)[:1000],
			"retryable": cint(not isinstance(exc, SolverInputBlocked)),
		})
		_set_run_solver_values(job.planning_run, {"solver_status": "Failed", "solver_phase": "Failed", "solver_completed_on": now_datetime(), "capacity_balance_status": "Hard Blocked", "unresolved_blocker_count": 1})
		if not getattr(frappe.flags, "in_test", False):
			frappe.log_error(frappe.get_traceback(), f"APS Solver Job {job.name}")
	return get_solver_job(job.name)


def get_solver_job(solver_job: str) -> dict[str, Any]:
	row = frappe.db.get_value("APS Solver Job", solver_job, ["name", "planning_run", "company", "plant_floor", "status", "engine", "phase", "progress_percent", "queued_on", "started_on", "completed_on", "runtime_seconds", "input_fingerprint", "solution_fingerprint", "selected_scenario", "selected_reason", "selected_by", "selected_on", "scenario_count", "best_bound", "objective_value", "gap_percent", "error_code", "error_message", "retryable", "cancel_requested"], as_dict=True)
	if not row:
		frappe.throw(_("Solver Job {0} was not found.", context="Injection APS").format(solver_job), frappe.DoesNotExistError)
	return dict(row)


def get_solver_scenarios(planning_run: str) -> dict[str, Any]:
	run = frappe.get_doc("APS Planning Run", planning_run)
	job = _latest_job(planning_run)
	rows = json.loads(job.scenarios_json or "[]")
	snapshot = input_from_dict(json.loads(job.input_snapshot_json or "{}"))
	selection_locked = (
		run.capacity_balance_status in {"Applied", "Applied with Exceptions"}
		or run.approval_state == "Approved"
		or job.status == "Applied"
	)
	return {
		"planning_run": planning_run,
		"solver_job": job.name,
		"status": job.status,
		"input_fingerprint": job.input_fingerprint,
		"selected_scenario": job.selected_scenario,
		"selection_locked": selection_locked,
		"selection_locked_reason": (
			_(
				"Scenario selection is locked after Apply. Create a new run or use the controlled change/reanalysis workflow to choose another scenario.",
				context="Injection APS",
			)
			if selection_locked
			else ""
		),
		"quantity_scale": snapshot.quantity_scale,
		"scenarios": [_scenario_summary(row, snapshot) for row in rows],
	}


def select_solver_scenario(planning_run: str, scenario_key: str, *, reason: str | None, expected_fingerprint: str) -> dict[str, Any]:
	_require_v2_solver()
	run = frappe.get_doc("APS Planning Run", planning_run)
	job = _latest_job(planning_run)
	if (
		run.capacity_balance_status in {"Applied", "Applied with Exceptions"}
		or run.approval_state == "Approved"
		or job.status == "Applied"
	):
		frappe.throw(
			_(
				"Scenario selection is locked after Apply. Create a new run or use the controlled change/reanalysis workflow to choose another scenario.",
				context="Injection APS",
			),
			frappe.ValidationError,
		)
	_assert_job_fingerprint(job, expected_fingerprint)
	rows = json.loads(job.scenarios_json or "[]")
	selected_row = next((row for row in rows if row.get("scenario_key") == scenario_key and row.get("status") != "Failed"), None)
	if not selected_row:
		frappe.throw(_("Select a validated solver scenario.", context="Injection APS"), frappe.ValidationError)
	if scenario_key != "recommended" and not str(reason or "").strip():
		frappe.throw(_("A reason is required when selecting a non-recommended scenario.", context="Injection APS"), frappe.ValidationError)
	solution = solution_from_dict(selected_row)
	_update_job(job.name, {"selected_scenario": scenario_key, "selected_reason": str(reason or "").strip(), "selected_by": frappe.session.user, "selected_on": now_datetime(), "solution_fingerprint": solution.solution_fingerprint})
	_persist_selected_projection(planning_run, job.name, solution)
	return get_solver_scenarios(planning_run)


def acknowledge_schedule_risks(planning_run: str, *, reason: str, expected_fingerprint: str) -> dict[str, Any]:
	job = _latest_job(planning_run)
	_assert_job_fingerprint(job, expected_fingerprint)
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required to acknowledge solver risks.", context="Injection APS"), frappe.ValidationError)
	run = frappe.get_doc("APS Planning Run", planning_run)
	if run.capacity_balance_status != "Acknowledgment Required":
		frappe.throw(_("This solver result does not currently require risk acknowledgment.", context="Injection APS"), frappe.ValidationError)
	_set_run_solver_values(planning_run, {"solver_acknowledged_by": frappe.session.user, "solver_acknowledged_on": now_datetime(), "solver_acknowledgment_reason": reason, "solver_acknowledgment_fingerprint": job.solution_fingerprint, "capacity_balance_confirmed_by": frappe.session.user, "capacity_balance_confirmed_on": now_datetime()})
	return {"planning_run": planning_run, "status": "Acknowledged", "solution_fingerprint": job.solution_fingerprint}


def cancel_solver_job(solver_job: str) -> dict[str, Any]:
	job = frappe.get_doc("APS Solver Job", solver_job)
	if job.status in {"Optimal", "Feasible", "Fallback", "Failed", "Cancelled", "Applied"}:
		return get_solver_job(job.name)
	_update_job(job.name, {"cancel_requested": 1})
	return get_solver_job(job.name)


def apply_v2_schedule(planning_run: str, *, expected_fingerprint: str) -> dict[str, Any]:
	_require_v2_solver()
	run = frappe.get_doc("APS Planning Run", planning_run)
	if run.run_type != "Formal":
		frappe.throw(
			_("A Trial run is read-only. Create or approve a Formal run before applying the V2 schedule.", context="Injection APS"),
			frappe.ValidationError,
		)
	job = _latest_job(planning_run)
	_assert_job_fingerprint(job, expected_fingerprint)
	if run.capacity_balance_status == "Hard Blocked":
		frappe.throw(_("Resolve hard blockers before applying the solver result.", context="Injection APS"), frappe.ValidationError)
	if run.capacity_balance_status == "Acknowledgment Required" and (run.get("solver_acknowledgment_fingerprint") or "") != (job.solution_fingerprint or ""):
		frappe.throw(_("Acknowledge the risks for the selected solver fingerprint before Apply.", context="Injection APS"), frappe.ValidationError)
	rows = json.loads(job.scenarios_json or "[]")
	selected = next((solution_from_dict(row) for row in rows if row.get("scenario_key") == job.selected_scenario), None)
	if not selected:
		frappe.throw(_("The selected solver scenario is no longer available.", context="Injection APS"), frappe.ValidationError)
	locked_state = _lock_apply_scope(run, selected, solver_job=job.name)
	locked_run = locked_state.run
	locked_job = locked_state.job
	if locked_job.input_fingerprint != expected_fingerprint:
		frappe.throw(_("The solver input fingerprint changed. Refresh and analyze again.", context="Injection APS"), frappe.ValidationError)
	if (
		locked_job.selected_scenario != job.selected_scenario
		or locked_job.solution_fingerprint != selected.solution_fingerprint
	):
		frappe.throw(_("The selected solver fingerprint changed during Apply.", context="Injection APS"), frappe.ValidationError)
	applied_states = {"Applied", "Applied with Exceptions"}
	already_applied = locked_run.capacity_balance_status in applied_states or locked_job.status == "Applied"
	if already_applied:
		if (
			locked_run.capacity_balance_status not in applied_states
			or locked_job.status != "Applied"
			or (locked_run.solver_solution_fingerprint or "") != (selected.solution_fingerprint or "")
		):
			frappe.throw(_("The applied Run and Solver Job evidence are inconsistent. Reconciliation is required before another Apply.", context="Injection APS"), frappe.ValidationError)
		return {
			"planning_run": locked_run.name,
			"solver_job": locked_job.name,
			"status": locked_run.capacity_balance_status,
			"solution_fingerprint": selected.solution_fingerprint,
			"updated_results": 0,
			"created_segments": 0,
			"campaigns": [],
			"campaign_count": 0,
			"pegging_count": 0,
			"peggings": [],
			"idempotent_replay": 1,
		}
	if locked_run.capacity_balance_status == "Hard Blocked":
		frappe.throw(_("Resolve hard blockers before applying the solver result.", context="Injection APS"), frappe.ValidationError)
	if locked_run.capacity_balance_status == "Acknowledgment Required" and (
		locked_run.solver_acknowledgment_fingerprint or ""
	) != (locked_job.solution_fingerprint or ""):
		frappe.throw(_("Acknowledge the risks for the selected solver fingerprint before Apply.", context="Injection APS"), frappe.ValidationError)
	run = frappe.get_doc({"doctype": "APS Planning Run", **dict(locked_run)})
	current_snapshot = build_solver_input(_build_normalized_source(run))
	if fingerprint(input_to_dict(current_snapshot)) != locked_job.input_fingerprint:
		frappe.throw(_("Solver inputs changed. Analyze again before Apply.", context="Injection APS"), frappe.ValidationError)
	validation = validate_solution(current_snapshot, selected, raise_on_error=False)
	if not validation["valid"]:
		frappe.throw(_("The selected solver solution failed independent validation.", context="Injection APS"), frappe.ValidationError)
	if not selected.tasks and any(row.quantity_units > 0 for row in current_snapshot.demands):
		frappe.throw(_("No feasible scheduled task exists; an empty formal schedule cannot be applied.", context="Injection APS"), frappe.ValidationError)
	result = _apply_solution_documents(run, current_snapshot, selected)
	applied_status = "Applied with Exceptions" if selected.metrics.total_unscheduled_units > 0 or cint(run.excluded_commitment_count) else "Applied"
	evidence = capacity_balance.finalize_v2_solver_apply_evidence(
		run.name,
		applied_status=applied_status,
	)
	_set_run_solver_values(run.name, {"solver_status": "Applied", "solver_phase": "Applied", "solver_solution_fingerprint": selected.solution_fingerprint})
	_update_job(job.name, {"status": "Applied", "phase": "Applied", "solution_fingerprint": selected.solution_fingerprint})
	return {
		"planning_run": run.name,
		"solver_job": job.name,
		"status": applied_status,
		"solution_fingerprint": selected.solution_fingerprint,
		"idempotent_replay": 0,
		**result,
		**{key: value for key, value in evidence.items() if key != "analysis"},
	}


def _build_normalized_source(run) -> dict[str, Any]:
	settings = planning.get_settings_dict()
	windows = horizon_status.run_horizon_values(run, settings)
	horizon_start = get_datetime(run.horizon_start or datetime.combine(windows["start_date"], time.min))
	horizon_end = get_datetime(windows["solver_end_datetime"])
	result_rows = frappe.get_all("APS Schedule Result", filters={"planning_run": run.name, "exclude_from_release": 0}, fields=["name", "company", "plant_floor", "item_code", "requested_date", "planned_qty", "demand_commitment", "is_urgent", "status"], order_by="requested_date asc, name asc", limit_page_length=0)
	result_names = [row.name for row in result_rows]
	segments = frappe.get_all("APS Schedule Segment", filters={"parent": ("in", result_names), "parenttype": "APS Schedule Result", "segment_status": ("not in", ["Blocked", "Cancelled"])}, fields=["name", "parent", "workstation", "plant_floor", "start_time", "end_time", "planned_qty", "actual_completed_qty", "setup_minutes", "changeover_minutes", "mould_reference", "is_locked", "segment_status", "actual_status", "linked_work_order", "anchor_strength", "color_code", "material_code"], order_by="start_time asc, name asc", limit_page_length=0) if result_names else []
	work_order_names = sorted({row.linked_work_order for row in segments if row.linked_work_order})
	work_order_statuses = {
		row.name: row.status
		for row in frappe.get_all("Work Order", filters={"name": ("in", work_order_names)}, fields=["name", "status"], limit_page_length=0)
	} if work_order_names else {}
	segments_by_result: dict[str, list[dict[str, Any]]] = {}
	for row in segments:
		value = dict(row)
		status = work_order_statuses.get(row.linked_work_order)
		value["linked_work_order_active"] = cint(bool(status) and status not in {"Stopped", "Completed", "Closed", "Cancelled"})
		value["linked_work_order_stopped"] = cint(status == "Stopped")
		segments_by_result.setdefault(row.parent, []).append(value)
	commitment_names = sorted({row.demand_commitment for row in result_rows if row.demand_commitment})
	commitments = {row.name: row for row in frappe.get_all("APS Demand Commitment", filters={"name": ("in", commitment_names)}, fields=["name", "admission_class", "service_priority", "newly_planned_qty", "effective_due_time", "original_due_date", "exclude_from_release"], limit_page_length=0)} if commitment_names else {}
	plant_floors = planning._get_run_selected_plant_floors(run)
	capabilities = planning._get_machine_capability_rows(plant_floors)
	demand_sources = []
	hard_errors = []
	all_alternatives = []
	for result in result_rows:
		commitment = commitments.get(result.demand_commitment)
		if commitment and cint(commitment.exclude_from_release):
			continue
		due = get_datetime(commitment.effective_due_time) if commitment and commitment.effective_due_time else _due_time(result.requested_date, run.get("due_time_policy"))
		fixed_rows = [row for row in segments_by_result.get(result.name, []) if capacity_balance._is_fixed_segment(row)]
		fixed_remaining = sum(max(flt(row.get("planned_qty")) - flt(row.get("actual_completed_qty")), 0) for row in fixed_rows)
		fixed_on_time = sum(max(flt(row.get("planned_qty")) - flt(row.get("actual_completed_qty")), 0) for row in fixed_rows if get_datetime(row.get("end_time")) <= due)
		fixed_late = max(fixed_remaining - fixed_on_time, 0)
		target = max(flt(result.planned_qty), flt(commitment.newly_planned_qty) if commitment else 0)
		controllable = max(target - fixed_remaining, 0)
		if target <= QTY_TOLERANCE:
			continue
		item_context = planning._get_item_context(result.item_code, settings)
		item_context["is_urgent"] = cint(result.is_urgent)
		candidates = planning._select_machine_candidates(result.item_code, item_context, capabilities, plant_floors)
		current_lanes = {(row.get("workstation"), row.get("mould_reference")) for row in segments_by_result.get(result.name, [])}
		alternatives = []
		for candidate in candidates:
			cycle_seconds = flt(candidate.get("cycle_time_seconds"))
			output = flt(candidate.get("effective_output_qty"))
			if cycle_seconds <= 0 or output <= 0:
				continue
			key = f"{candidate.get('workstation')}|{candidate.get('mould_reference')}"
			alternative = {
				"key": key,
				"machine": candidate.get("workstation"),
				"mold": candidate.get("mould_reference"),
				"plant_floor": candidate.get("plant_floor"),
				"output_per_cycle": output,
				"cycle_minutes": cycle_seconds / 60,
				"base_setup_minutes": flt(settings.get("default_setup_minutes")),
				"tonnage_gap": flt(candidate.get("tonnage_gap")),
				"preference_rank": cint(candidate.get("priority")),
				"continuity_rank": 0 if (candidate.get("workstation"), candidate.get("mould_reference")) in current_lanes else 1,
				"color_code": item_context.get("color_code"),
				"material_code": item_context.get("material_code"),
				"capacity_source": "mold_cycle",
			}
			alternatives.append(alternative)
			all_alternatives.append(alternative)
		if controllable > QTY_TOLERANCE and not alternatives:
			hard_errors.append(f"{result.name}: no legal machine/mold alternative with a valid cycle exists")
		demand_sources.append({
			"key": result.demand_commitment or result.name,
			"result": result.name,
			"commitment": result.demand_commitment or "",
			"item_code": result.item_code,
			"admission_class": commitment.admission_class if commitment else "P0",
			"quantity": controllable,
			"due_time": due,
			"original_due_time": _due_time(commitment.original_due_date, run.get("due_time_policy")) if commitment and commitment.original_due_date else due,
			"earliest_time": horizon_start,
			"service_priority": cint(commitment.service_priority) if commitment else (100 if cint(result.is_urgent) else 0),
			"fixed_on_time_qty": fixed_on_time,
			"fixed_late_qty": fixed_late,
			"alternatives": alternatives,
		})
	precedences = []
	bom_decisions = []
	feature_settings = v2_flags.get_v2_settings()
	if feature_settings["enable_multilevel_bom_planning"]:
		expansion = bom_planning.expand_solver_demands(
			run.company,
			demand_sources,
			producible_groups=bom_planning.configured_producible_groups(frappe.get_cached_doc("APS Settings")),
			planning_run=run.name,
		)
		for demand in expansion["demands"]:
			demand["earliest_time"] = horizon_start
			item_context = planning._get_item_context(demand["item_code"], settings)
			candidates = planning._select_machine_candidates(demand["item_code"], item_context, capabilities, plant_floors)
			alternatives = []
			for candidate in candidates:
				cycle_seconds = flt(candidate.get("cycle_time_seconds"))
				output = flt(candidate.get("effective_output_qty"))
				if cycle_seconds <= 0 or output <= 0:
					continue
				alternatives.append({
					"key": f"{candidate.get('workstation')}|{candidate.get('mould_reference')}", "machine": candidate.get("workstation"),
					"mold": candidate.get("mould_reference"), "plant_floor": candidate.get("plant_floor"), "output_per_cycle": output,
					"cycle_minutes": cycle_seconds / 60, "base_setup_minutes": flt(settings.get("default_setup_minutes")),
					"tonnage_gap": flt(candidate.get("tonnage_gap")), "preference_rank": cint(candidate.get("priority")), "continuity_rank": 1,
					"color_code": item_context.get("color_code"), "material_code": item_context.get("material_code"), "capacity_source": "bom_manufactured_component",
				})
			if not alternatives:
				hard_errors.append(f"{demand['key']}: manufactured BOM component has no legal machine/mold alternative")
			demand["alternatives"] = alternatives
			all_alternatives.extend(alternatives)
			demand_sources.append(demand)
		precedences = expansion["precedences"]
		bom_decisions = expansion["bom_decisions"]
	if hard_errors:
		raise SolverInputBlocked("; ".join(hard_errors))
	multi_output_groups = []
	if feature_settings["enable_coproduct_campaign"]:
		demand_sources, multi_output_groups = campaign_planning.collapse_family_demands_for_solver(demand_sources)
		owner_by_member = {
			member["demand_key"]: group["capacity_owner_demand"]
			for group in multi_output_groups
			for member in group.get("members") or []
			if member.get("demand_key")
		}
		for edge in precedences:
			edge["predecessor_demand"] = owner_by_member.get(edge["predecessor_demand"], edge["predecessor_demand"])
			edge["successor_demand"] = owner_by_member.get(edge["successor_demand"], edge["successor_demand"])
	workstations = sorted({row["machine"] for row in all_alternatives if row.get("machine")})
	result_map, current_segments = capacity_balance._get_run_balance_rows(run.name)
	cross_results, cross_segments = capacity_balance._get_cross_run_applied_commitments(run)
	fixed_by_machine, fixed_by_mold = capacity_balance._get_fixed_execution_intervals(run, current_segments, cross_run_segments=cross_segments)
	downtime = planning._get_active_downtime_windows(company=run.company, plant_floors=plant_floors, horizon_start=horizon_start, horizon_end=horizon_end, run_name=run.name)
	base_buckets = capacity_balance.build_capacity_buckets(workstations, horizon_start, horizon_end, blocked_intervals=fixed_by_machine, downtime_windows=downtime, workstation_plant_floors={row.get("workstation"): row.get("plant_floor") for row in capabilities}, company=run.company)
	split_points = {horizon_start, horizon_end}
	for demand in demand_sources:
		split_points.add(get_datetime(demand["due_time"]))
		split_points.add(get_datetime(demand["earliest_time"]))
	for value in (windows.get("freeze_end_date"), windows.get("restricted_end_date"), windows.get("demand_end_date"), windows.get("recovery_start_date")):
		if value:
			split_points.add(datetime.combine(getdate(value) + timedelta(days=1), time.min))
	bucket_sources = _split_capacity_buckets(base_buckets, sorted(split_points), windows)
	frozen_sources = _frozen_sources(fixed_by_machine, fixed_by_mold)
	transition_rules = [{"from_family": row.from_color, "to_family": row.to_color, "setup_minutes": max(int(math.ceil(flt(row.setup_minutes))), 0), "blocked": bool(cint(row.is_blocking) or row.change_level == "Blocked")} for row in frappe.get_all("APS Color Transition Rule", filters={"is_active": 1}, fields=["from_color", "to_color", "setup_minutes", "is_blocking", "change_level"], limit_page_length=0)] if frappe.db.exists("DocType", "APS Color Transition Rule") else []
	overrides = tuple((row["name"], row.get("blocker_key") or "") for row in constraint_resolution.approved_overrides(run.name))
	return {"run_key": run.name, "horizon_start": horizon_start, "horizon_end": horizon_end, "quantity_scale": 1000, "time_limit_seconds": max(cint(feature_settings.get("solver_time_limit_seconds")), 1), "random_seed": 20260814, "demands": demand_sources, "buckets": bucket_sources, "frozen_intervals": frozen_sources, "transition_rules": transition_rules, "multi_output_groups": multi_output_groups, "precedences": precedences, "bom_decisions": bom_decisions, "approved_overrides": overrides}


def _split_capacity_buckets(base_buckets, split_points, windows):
	rows = []
	for bucket in base_buckets:
		for factor_row in bucket.get("capacity_factor_intervals") or []:
			factor = flt(factor_row.get("factor"))
			if factor <= 0:
				continue
			start = get_datetime(factor_row.get("start")); end = get_datetime(factor_row.get("end"))
			points = [start] + [point for point in split_points if start < point < end] + [end]
			for index, (left, right) in enumerate(zip(points, points[1:]), start=1):
				minutes = int(math.floor((right - left).total_seconds() / 60))
				if minutes <= 0:
					continue
				rows.append({"key": f"{bucket['key']}|{left.isoformat()}|{index}", "machine": bucket.get("workstation"), "start": left, "end": right, "available_minutes": minutes, "capacity_factor": factor, "horizon_zone": _zone(left, windows), "shift_key": bucket.get("key")})
	return rows


def _zone(value, windows):
	date = getdate(value)
	if windows.get("freeze_end_date") and date <= windows["freeze_end_date"]:
		return "Freeze"
	if windows.get("restricted_end_date") and date <= windows["restricted_end_date"]:
		return "Restricted"
	if date <= windows["demand_end_date"]:
		return "Demand"
	return "Recovery"


def _frozen_sources(machine_intervals, mold_intervals):
	rows = []
	for resource_type, mapping in (("machine", machine_intervals), ("mold", mold_intervals)):
		for resource, intervals in sorted(mapping.items()):
			merged = []
			for start, end in sorted((get_datetime(start), get_datetime(end)) for start, end in intervals):
				if end <= start:
					continue
				if merged and start <= merged[-1][1]:
					merged[-1] = (merged[-1][0], max(merged[-1][1], end))
				else:
					merged.append((start, end))
			for index, (start, end) in enumerate(merged, start=1):
				rows.append({"key": f"{resource_type}|{resource}|{index}|{get_datetime(start).isoformat()}", "resource_type": resource_type, "resource": resource, "start": start, "end": end})
	return rows


def _due_time(value, policy):
	date = getdate(value)
	return datetime.combine(date, time(20, 0)) if policy == "Delivery Date Last Production Shift End" else datetime.combine(date, time(23, 59, 59))


def _persist_selected_projection(planning_run, solver_job, solution):
	input_json = frappe.db.get_value("APS Solver Job", solver_job, "input_snapshot_json")
	snapshot = input_from_dict(json.loads(input_json or "{}"))
	scale = snapshot.quantity_scale
	readiness = "Acknowledgment Required" if solution.status == "Fallback" or solution.metrics.total_late_units > 0 or solution.metrics.total_unscheduled_units > 0 else "Ready"
	_set_run_solver_values(planning_run, {"solver_job": solver_job, "solver_engine_used": solution.engine, "solver_status": solution.status, "solver_phase": "Validated", "solver_runtime_seconds": solution.runtime_seconds, "solver_best_bound": solution.best_bound, "solver_objective_value": solution.objective_value, "solver_gap_percent": solution.gap_percent, "solver_solution_fingerprint": solution.solution_fingerprint, "selected_solver_scenario": solution.scenario_key, "solver_quality_json": canonical_json({"metrics": solution_to_dict(solution)["metrics"], "warnings": list(solution.warnings), "explanation": list(solution.explanation)}), "total_on_time_qty": solution.metrics.p0_on_time_units / scale, "total_late_qty": solution.metrics.total_late_units / scale, "total_recovery_qty": solution.metrics.total_late_units / scale, "total_critical_unplanned_qty": solution.metrics.p0_critical_unplanned_units / scale, "acknowledgment_count": 1 if readiness == "Acknowledgment Required" else 0, "unresolved_blocker_count": 0, "capacity_balance_status": readiness, "capacity_balance_fingerprint": solution.input_fingerprint, "capacity_balance_analysis_json": canonical_json(_solution_analysis(solution, scale, readiness, snapshot))})
	for outcome in solution.outcomes:
		if not outcome.result:
			continue
		values = {"on_time_qty": outcome.on_time_units / scale, "recovery_qty": outcome.late_units / scale, "critical_unplanned_qty": outcome.unscheduled_units / scale, "capacity_balance_status": readiness, "capacity_balance_requires_confirmation": cint(readiness == "Acknowledgment Required"), "solver_decision_json": canonical_json({"solver_job": solver_job, "scenario": solution.scenario_key, "solution_fingerprint": solution.solution_fingerprint}), "solver_explanation": " ".join(solution.explanation), "acknowledgment_required": cint(readiness == "Acknowledgment Required")}
		frappe.db.set_value("APS Schedule Result", outcome.result, values, update_modified=False)
	if solution.engine == "CP-SAT" and input_json:
		campaign_planning.project_solver_campaign_results(snapshot, solution, readiness=readiness)


def _solution_analysis(solution, scale, readiness, snapshot):
	tasks_by_demand = {}
	for task in solution.tasks:
		tasks_by_demand.setdefault(task.demand_key, []).append(_task_summary(task, snapshot))
	return {
		"solver_v2": 1,
		"solver_job_status": solution.status,
		"engine": solution.engine,
		"readiness_status": readiness,
		"analysis_fingerprint": solution.input_fingerprint,
		"solution_fingerprint": solution.solution_fingerprint,
		"summary": {
			"on_time_qty": solution.metrics.p0_on_time_units / scale,
			"late_qty": solution.metrics.total_late_units / scale,
			"unscheduled_qty": solution.metrics.total_unscheduled_units / scale,
			"change_count": solution.metrics.change_count,
			"setup_minutes": solution.metrics.setup_minutes,
			"requires_confirmation": cint(readiness == "Acknowledgment Required"),
			"blocked_demands": 0,
			"proposed_task_count": len(solution.tasks),
		},
		"demands": [
			{
				"demand_key": row.demand_key,
				"result": row.result,
				"planned_qty": (row.on_time_units + row.late_units + row.unscheduled_units) / scale,
				"on_time_qty": row.on_time_units / scale,
				"late_qty": row.late_units / scale,
				"unscheduled_qty": row.unscheduled_units / scale,
				"status": readiness,
				"requires_confirmation": cint(readiness == "Acknowledgment Required"),
				"allocations": tasks_by_demand.get(row.demand_key) or [],
			}
			for row in solution.outcomes
		],
		"warnings": list(solution.warnings),
		"next_action": "Review and acknowledge risks, then Apply." if readiness == "Acknowledgment Required" else "Apply the analyzed schedule.",
	}


def _apply_solution_documents(run, snapshot, solution):
	scale = snapshot.quantity_scale
	origin = get_datetime(snapshot.horizon_start)
	bom_decisions = {row.item_code: row for row in snapshot.bom_decisions}
	tasks_by_demand = {}
	for task in solution.tasks:
		tasks_by_demand.setdefault(task.demand_key, []).append(task)
	demands = {row.key: row for row in snapshot.demands}
	resolved_results = {}
	for demand in snapshot.demands:
		if demand.result:
			resolved_results[demand.key] = demand.result
		elif demand.key.startswith("BOM:"):
			resolved_results[demand.key] = _get_or_create_bom_result(run, demand, snapshot, solution)
	updated_segments = 0
	for outcome in sorted(solution.outcomes, key=lambda row: row.demand_key):
		result_name = resolved_results.get(outcome.demand_key)
		if not result_name:
			continue
		doc = frappe.get_doc("APS Schedule Result", result_name)
		bom_decision = bom_decisions.get(demand.item_code if (demand := demands.get(outcome.demand_key)) else doc.item_code)
		if bom_decision:
			doc.selected_bom = bom_decision.bom
			doc.selected_bom_fingerprint = bom_decision.bom_fingerprint
			doc.bom_selection_source = bom_decision.selection_source
		for segment in doc.get("segments") or []:
			if not capacity_balance._is_fixed_segment(segment.as_dict()):
				segment.segment_status = "Cancelled"
		for task in sorted(tasks_by_demand.get(outcome.demand_key) or [], key=lambda row: (row.occupied_start_minute, row.machine, row.key)):
			start = origin + timedelta(minutes=task.production_start_minute)
			end = origin + timedelta(minutes=task.end_minute)
			doc.append("segments", {"workstation": task.machine, "plant_floor": doc.plant_floor, "start_time": start, "end_time": end, "horizon_zone": task.horizon_zone, "is_frozen_window": cint(task.horizon_zone == "Freeze"), "is_restricted_window": cint(task.horizon_zone == "Restricted"), "planned_qty": task.quantity_units / scale, "production_mode": "Late" if task.horizon_zone == "Recovery" else "JIT", "sequence_no": task.sequence_no, "lane_key": task.alternative_key, "campaign_key": task.key, "segment_kind": "Primary", "primary_item_code": doc.item_code, "setup_minutes": task.base_setup_minutes, "changeover_minutes": task.changeover_minutes, "mould_reference": task.mold, "schedule_explanation": f"{solution.scenario_label}; {solution.status}; {task.cycles} cycles", "risk_status": "Attention" if task.horizon_zone == "Recovery" else "Normal", "segment_status": "Applied", "solver_start_time": start, "solver_end_time": end, "current_start_time": start, "current_end_time": end, "capacity_owner": task.key, "assignment_reason": solution.scenario_label, "solver_scenario": solution.scenario_key, "solver_task_key": task.key})
			updated_segments += 1
		doc.on_time_qty = outcome.on_time_units / scale
		doc.recovery_qty = outcome.late_units / scale
		doc.critical_unplanned_qty = outcome.unscheduled_units / scale
		doc.status = "Risk" if outcome.unscheduled_units or outcome.late_units else "Planned"
		doc.risk_status = "Critical" if outcome.unscheduled_units else "Attention" if outcome.late_units else "Normal"
		doc.solver_decision_json = canonical_json({"scenario": solution.scenario_key, "solution_fingerprint": solution.solution_fingerprint})
		doc.flags.aps_result_engine_transition = True
		doc.save(ignore_permissions=True)
		if outcome.commitment:
			commitment = frappe.get_doc("APS Demand Commitment", outcome.commitment)
			commitment.on_time_qty = outcome.on_time_units / scale
			commitment.late_qty = outcome.late_units / scale
			commitment.unscheduled_qty = outcome.unscheduled_units / scale
			commitment.transition_reason = f"Applied solver scenario {solution.scenario_key}"
			commitment.transitioned_by = frappe.session.user
			commitment.transitioned_on = now_datetime()
			commitment.flags.aps_phase2_transition = True
			commitment.save(ignore_permissions=True)
	campaigns = campaign_planning.apply_solver_campaigns(run, snapshot, solution) if snapshot.multi_output_groups else {"campaigns": [], "campaign_count": 0}
	peggings = bom_planning.persist_solver_peggings(run, snapshot, solution) if snapshot.precedences else {"pegging_count": 0, "peggings": []}
	return {"updated_results": len(resolved_results), "created_segments": updated_segments, **campaigns, **peggings}


def _get_or_create_bom_result(run, demand, snapshot, solution):
	existing = frappe.db.get_value("APS Schedule Result", {"planning_run": run.name, "bom_demand_key": demand.key}, "name")
	if existing:
		return existing
	origin = get_datetime(snapshot.horizon_start)
	bom_decision = next((row for row in snapshot.bom_decisions if row.item_code == demand.item_code), None)
	root_commitments = set()
	parent_commitments = set()
	for edge in snapshot.precedences:
		if edge.predecessor_demand == demand.key and edge.root_demand_key:
			parent = next((row for row in snapshot.demands if row.key == edge.successor_demand), None)
			if parent and parent.commitment:
				parent_commitments.add(parent.commitment)
			for root_key in edge.root_demand_keys or (edge.root_demand_key,):
				root = next((row for row in snapshot.demands if row.key == root_key), None)
				if root and root.commitment:
					root_commitments.add(root.commitment)
				elif frappe.db.exists("APS Demand Commitment", root_key):
					root_commitments.add(root_key)
	root_commitment = next(iter(root_commitments)) if len(root_commitments) == 1 else ""
	parent_commitment = next(iter(parent_commitments)) if len(parent_commitments) == 1 else ""
	doc = frappe.get_doc({
		"doctype": "APS Schedule Result", "planning_run": run.name, "company": run.company,
		"plant_floor": run.get("plant_floor"), "item_code": demand.item_code,
		"requested_date": (origin + timedelta(minutes=demand.due_minute)).date(),
		"demand_source": "BOM Component", "production_strategy": "Auto Balance",
		"planned_qty": demand.quantity_units / snapshot.quantity_scale, "status": "Planned", "risk_status": "Normal",
		"admission_class": demand.admission_class, "service_priority": demand.service_priority,
		"effective_due_time": origin + timedelta(minutes=demand.due_minute),
		"bom_demand_key": demand.key, "root_commitment": root_commitment or None,
		"parent_commitment": parent_commitment or None,
		"selected_bom": bom_decision.bom if bom_decision else None,
		"selected_bom_fingerprint": bom_decision.bom_fingerprint if bom_decision else None,
		"bom_selection_source": bom_decision.selection_source if bom_decision else None,
		"solver_decision_json": canonical_json({"scenario": solution.scenario_key, "solution_fingerprint": solution.solution_fingerprint, "bom_generated": True}),
		"schedule_explanation": "Manufactured BOM component created from validated precedence planning.",
	})
	doc.flags.aps_result_engine_transition = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _lock_apply_scope(run, solution, *, solver_job):
	frappe.db.sql("select name from `tabCompany` where name=%s for update", run.company)
	locked_runs = frappe.db.sql("select * from `tabAPS Planning Run` where name=%s for update", run.name, as_dict=True)
	if not locked_runs or (locked_runs[0].get("company") or "") != (run.company or ""):
		frappe.throw(_("The Planning Run scope changed while Apply was waiting for its lock.", context="Injection APS"), frappe.ValidationError)
	locked_jobs = frappe.db.sql(
		"select * from `tabAPS Solver Job` where planning_run=%s order by creation desc, name desc limit 1 for update",
		run.name,
		as_dict=True,
	)
	if not locked_jobs or locked_jobs[0].get("name") != solver_job:
		frappe.throw(_("A newer Solver Job exists. Refresh before Apply.", context="Injection APS"), frappe.ValidationError)
	commitments = sorted({row.commitment for row in solution.outcomes if row.commitment})
	results = sorted({row.result for row in solution.outcomes if row.result})
	if commitments:
		frappe.db.sql("select name from `tabAPS Demand Commitment` where name in %s order by name for update", (tuple(commitments),))
	if results:
		frappe.db.sql("select name from `tabAPS Schedule Result` where name in %s order by name for update", (tuple(results),))
		frappe.db.sql("select name from `tabAPS Schedule Segment` where parent in %s order by name for update", (tuple(results),))
	return frappe._dict(run=frappe._dict(locked_runs[0]), job=frappe._dict(locked_jobs[0]))


def _latest_job(planning_run):
	name = frappe.db.get_value("APS Solver Job", {"planning_run": planning_run}, "name", order_by="creation desc")
	if not name:
		frappe.throw(_("Analyze the V2 schedule before opening solver scenarios.", context="Injection APS"), frappe.ValidationError)
	return frappe.get_doc("APS Solver Job", name)


def _assert_job_fingerprint(job, expected):
	if not expected or expected != job.input_fingerprint:
		frappe.throw(_("The solver input fingerprint changed. Refresh and analyze again.", context="Injection APS"), frappe.ValidationError)


def _scenario_summary(row, snapshot):
	metrics = row.get("metrics") or {}
	return {
		"scenario_key": row.get("scenario_key"),
		"scenario_label": row.get("scenario_label"),
		"status": row.get("status"),
		"engine": row.get("engine"),
		"runtime_seconds": row.get("runtime_seconds"),
		"gap_percent": row.get("gap_percent"),
		"solution_fingerprint": row.get("solution_fingerprint"),
		"metrics": metrics,
		"warnings": row.get("warnings") or [],
		"explanation": row.get("explanation") or [],
		"valid": dict(row.get("validation") or []).get("valid"),
		"tasks": [_task_summary(task, snapshot) for task in row.get("tasks") or []],
	}


def _task_summary(task, snapshot):
	value = task if isinstance(task, dict) else solution_to_dict_task(task)
	origin = get_datetime(snapshot.horizon_start)
	scale = snapshot.quantity_scale
	demand = next((row for row in snapshot.demands if row.key == value.get("demand_key")), None)
	end_minute = int(value.get("end_minute") or 0)
	return {
		"key": value.get("key"),
		"demand_key": value.get("demand_key"),
		"result": value.get("result"),
		"commitment": value.get("commitment"),
		"alternative_key": value.get("alternative_key"),
		"bucket_key": value.get("bucket_key"),
		"workstation": value.get("machine"),
		"mold": value.get("mold"),
		"qty": int(value.get("quantity_units") or 0) / scale,
		"cycles": int(value.get("cycles") or 0),
		"occupied_start": origin + timedelta(minutes=int(value.get("occupied_start_minute") or 0)),
		"production_start": origin + timedelta(minutes=int(value.get("production_start_minute") or 0)),
		"end": origin + timedelta(minutes=end_minute),
		"due": origin + timedelta(minutes=demand.due_minute) if demand else None,
		"delivery_status": "On Time" if demand and end_minute <= demand.due_minute else "Late",
		"setup_minutes": int(value.get("base_setup_minutes") or 0),
		"changeover_minutes": int(value.get("changeover_minutes") or 0),
		"horizon_zone": value.get("horizon_zone"),
		"sequence_no": int(value.get("sequence_no") or 0),
		"provisional": 1,
	}


def solution_to_dict_task(task):
	return {
		field: getattr(task, field)
		for field in task.__dataclass_fields__
	}


def _update_job(name, values):
	doc = frappe.get_doc("APS Solver Job", name)
	for key, value in values.items():
		doc.set(key, value)
	doc.flags.aps_solver_transition = True
	doc.save(ignore_permissions=True)


def _set_run_solver_values(run_name, values):
	frappe.db.set_value("APS Planning Run", run_name, values, update_modified=False)


def _require_v2_solver():
	settings = v2_flags.get_v2_settings()
	if not settings["enable_aps_v2"] or settings["solver_engine"] != "CP-SAT":
		frappe.throw(_("Enable APS V2 and select CP-SAT before running the V2 solver.", context="Injection APS"), frappe.PermissionError)


def _async_disabled():
	return bool(cint(getattr(frappe.conf, "disable_async", 0)) or cint(getattr(frappe.conf, "aps_v2_isolated_environment", 0)))
