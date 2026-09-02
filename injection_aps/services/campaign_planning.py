from __future__ import annotations

import math
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from injection_aps.services import capacity_balance, planning, v2_flags
from injection_aps.services.solver.serialization import canonical_json, fingerprint


QTY_TOLERANCE = 0.000001


def collapse_family_demands_for_solver(demands: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	"""Collapse explicit family-output demands to one capacity-owner solver demand.

	The returned member snapshot retains every output/result target. Side outputs
	with zero demand are included so Apply can create their Work Order and excess
	ledger without inventing another machine interval.
	"""
	if not demands or not frappe.db.exists("DocType", "Mold Product"):
		return demands, []
	molds = sorted({alt.get("mold") for demand in demands for alt in demand.get("alternatives") or [] if alt.get("mold")})
	if not molds:
		return demands, []
	family_molds = set(frappe.get_all("Mold", filters={"name": ("in", molds), "docstatus": 1, "is_family_mold": 1}, pluck="name"))
	original_demands = list(demands)
	groups = []
	replacements = []
	claimed = set()
	for mold in sorted(family_molds):
		products = frappe.get_all("Mold Product", filters={"parent": mold, "parenttype": "Mold"}, fields=["item_code", "output_group", "output_qty", "cavity_output_qty", "idx"], order_by="idx asc", limit_page_length=0)
		by_output_group: dict[str, list[dict[str, Any]]] = {}
		for product in products:
			by_output_group.setdefault(product.output_group or "Default", []).append(dict(product))
		for output_group, output_rows in sorted(by_output_group.items()):
			if len(output_rows) < 2:
				continue
			items = {row["item_code"] for row in output_rows}
			candidates = [row for row in original_demands if row["key"] not in claimed and row.get("item_code") in items and any(alt.get("mold") == mold for alt in row.get("alternatives") or [])]
			if not candidates:
				continue
			by_item: dict[str, list[dict[str, Any]]] = {}
			for candidate in candidates:
				by_item.setdefault(candidate["item_code"], []).append(candidate)
			duplicate_items = sorted(item for item, rows in by_item.items() if len(rows) > 1)
			if duplicate_items:
				raise ValueError(
					"Family campaign output demand is ambiguous for item(s) "
					+ ", ".join(duplicate_items)
					+ "; consolidate the Run-level output ownership before solving."
				)
			owner = min(candidates, key=lambda row: (str(row.get("due_time") or ""), -cint(row.get("service_priority")), str(row["key"])))
			outputs_by_item = {row["item_code"]: max(flt(row.get("cavity_output_qty") or row.get("output_qty")), 0) for row in output_rows}
			if any(value <= 0 for value in outputs_by_item.values()):
				continue
			required_cycles = max(int(math.ceil(flt(row.get("quantity")) / outputs_by_item[row["item_code"]])) for row in candidates)
			minimum_batch_cycles = max(
				int(
					math.ceil(
						flt(row.get("minimum_batch_qty"))
						/ outputs_by_item[row["item_code"]]
					)
				)
				for row in candidates
			)
			owner_copy = dict(owner)
			owner_copy["quantity"] = required_cycles * outputs_by_item[owner["item_code"]]
			owner_copy["minimum_batch_qty"] = (
				minimum_batch_cycles * outputs_by_item[owner["item_code"]]
			)
			owner_copy["alternatives"] = [
				{**alt, "output_per_cycle": outputs_by_item[owner["item_code"]]}
				for alt in owner.get("alternatives") or []
				if alt.get("mold") == mold
			]
			if not owner_copy["alternatives"]:
				continue
			member_by_item = {row["item_code"]: row for row in candidates}
			members = []
			for product in output_rows:
				demand = member_by_item.get(product["item_code"]) or {}
				members.append({
					"demand_key": demand.get("key") or f"SIDE:{mold}:{output_group}:{product['item_code']}",
					"result": demand.get("result") or "",
					"commitment": demand.get("commitment") or "",
					"item_code": product["item_code"],
					"output_per_cycle": outputs_by_item[product["item_code"]],
					"required_qty": demand.get("quantity") or 0,
					"due_time": demand.get("due_time") or owner["due_time"],
					"output_role": "Primary" if product["item_code"] == owner["item_code"] else "Co-product",
					"admission_class": demand.get("admission_class") or "P0",
					"service_priority": cint(demand.get("service_priority")),
					"fixed_on_time_qty": demand.get("fixed_on_time_qty") or 0,
					"fixed_late_qty": demand.get("fixed_late_qty") or 0,
				})
			groups.append({"key": f"{mold}|{output_group}|{owner['key']}", "capacity_owner_demand": owner["key"], "members": members})
			claimed.update(row["key"] for row in candidates)
			owner_copy["multi_output_group"] = groups[-1]["key"]
			owner_copy["_collapsed_member_keys"] = [row["key"] for row in candidates]
			replacements.append(owner_copy)
	collapsed = [row for row in original_demands if row["key"] not in claimed] + replacements
	return sorted(collapsed, key=lambda row: (str(row.get("due_time") or ""), str(row["key"]))), groups


def plan_campaign_outputs(outputs: list[dict[str, Any]], *, primary_item: str | None = None) -> dict[str, Any]:
	"""Apply the locked family-mold max-cycle formula to explicit master outputs."""
	if len(outputs or []) < 2:
		raise ValueError("A family campaign requires at least two explicit outputs.")
	seen = set()
	normalized = []
	for row in outputs:
		item = str(row.get("item_code") or "").strip()
		per_cycle = float(row.get("output_per_cycle") or 0)
		required = max(float(row.get("required_qty") or 0), 0)
		if not item or item in seen:
			raise ValueError("Campaign outputs require unique item codes.")
		if per_cycle <= 0:
			raise ValueError(f"Campaign output {item} has no reliable output-per-cycle value.")
		seen.add(item)
		normalized.append({**row, "item_code": item, "output_per_cycle": per_cycle, "required_qty": required, "required_cycles": int(math.ceil(required / per_cycle)) if required > QTY_TOLERANCE else 0})
	primary_item = primary_item or next((row["item_code"] for row in normalized if row.get("output_role") == "Primary"), normalized[0]["item_code"])
	if primary_item not in seen:
		raise ValueError("Primary campaign output must be one of the explicit outputs.")
	cycles = max((row["required_cycles"] for row in normalized), default=0)
	demanded_outputs = sum(1 for row in normalized if row["required_qty"] > QTY_TOLERANCE)
	planned = []
	for row in normalized:
		planned_qty = cycles * row["output_per_cycle"]
		covered = min(row["required_qty"], planned_qty)
		role = "Primary" if row["item_code"] == primary_item else "Co-product"
		note = ""
		if demanded_outputs == 1 and row["required_qty"] <= QTY_TOLERANCE:
			note = f"Produced together with demand-driven output {primary_item}."
		planned.append({**row, "output_role": role, "planned_qty": planned_qty, "demand_covered_qty": covered, "excess_qty": max(planned_qty - covered, 0), "output_note": note})
	return {"campaign_cycles": cycles, "outputs": planned, "demanded_output_count": demanded_outputs}


def materialize_campaigns_for_run(run_name: str) -> dict[str, Any]:
	_require_campaign_feature()
	rows = frappe.db.sql(
		"""
		select s.name, s.parent as schedule_result, s.workstation, s.plant_floor,
			coalesce(s.current_start_time, s.start_time) as start_time,
			coalesce(s.current_end_time, s.end_time) as end_time,
			s.planned_qty, s.mould_reference, r.item_code, r.company
		from `tabAPS Schedule Segment` s
		inner join `tabAPS Schedule Result` r on r.name=s.parent
		inner join `tabMold` m on m.name=s.mould_reference and m.is_family_mold=1 and m.docstatus=1
		where r.planning_run=%s and s.parenttype='APS Schedule Result'
			and ifnull(s.segment_kind, 'Primary') in ('Primary', 'Manual')
			and ifnull(s.segment_status, '') not in ('Blocked', 'Cancelled')
		order by s.start_time, s.name
		""",
		run_name,
		as_dict=True,
	)
	created = []
	reused = []
	for row in rows:
		result = create_campaign_from_segment(run_name, dict(row))
		(result["created"] and created or reused).append(result["campaign"])
	return {"planning_run": run_name, "created": created, "reused": reused, "campaign_count": len(created) + len(reused)}


def project_solver_campaign_results(snapshot, solution, *, readiness: str) -> None:
	"""Project every derived output to its existing Result without adding capacity."""
	for outcome in _campaign_member_outcomes(snapshot, solution):
		if not outcome["result"]:
			continue
		values = {
			"on_time_qty": outcome["on_time_units"] / snapshot.quantity_scale,
			"recovery_qty": outcome["late_units"] / snapshot.quantity_scale,
			"critical_unplanned_qty": outcome["unscheduled_units"] / snapshot.quantity_scale,
			"capacity_balance_status": readiness,
			"capacity_balance_requires_confirmation": cint(readiness == "Acknowledgment Required"),
			"solver_explanation": "Projected from one shared multi-output campaign capacity owner.",
		}
		frappe.db.set_value("APS Schedule Result", outcome["result"], values, update_modified=False)


def apply_solver_campaigns(run, snapshot, solution) -> dict[str, Any]:
	"""Persist campaign/output ledgers after validated owner segments are applied."""
	created = []
	origin = get_datetime(snapshot.horizon_start)
	outcomes = {row["demand_key"]: row for row in _campaign_member_outcomes(snapshot, solution)}
	result_by_demand = {}
	for group in snapshot.multi_output_groups:
		owner_tasks = [row for row in solution.tasks if row.demand_key == group.capacity_owner_demand]
		member_results = {row.result for row in group.members if row.result}
		owner_results = {row.result for row in snapshot.demands if row.key == group.capacity_owner_demand and row.result}
		for result_name in sorted(member_results - owner_results):
			doc = frappe.get_doc("APS Schedule Result", result_name)
			for segment in doc.get("segments") or []:
				if not capacity_balance._is_fixed_segment(segment.as_dict()):
					segment.segment_status = "Cancelled"
			doc.flags.aps_result_engine_transition = True
			doc.save(ignore_permissions=True)
		member_result_names = {}
		for member in group.members:
			if member.result:
				member_result_names[member.demand_key] = member.result
			elif owner_tasks:
				member_result_names[member.demand_key] = _get_or_create_campaign_result(
					run, snapshot, solution, group, member
				)
		result_by_demand.update(member_result_names)
		remaining_required = {row.demand_key: row.required_units for row in group.members}
		for task in sorted(owner_tasks, key=lambda row: (row.occupied_start_minute, row.key)):
			segment_name = frappe.db.get_value("APS Schedule Segment", {"solver_task_key": task.key, "parenttype": "APS Schedule Result"}, "name")
			if not segment_name:
				continue
			campaign_key = fingerprint({"run": run.name, "group": group.key, "task": task.key, "solution": solution.solution_fingerprint})
			existing = frappe.db.get_value("APS Production Campaign", {"campaign_key": campaign_key}, "name")
			if existing:
				created.append(existing)
				continue
			outputs = []
			for member in group.members:
				planned_units = task.cycles * member.output_units_per_cycle
				covered_units = min(remaining_required[member.demand_key], planned_units)
				remaining_required[member.demand_key] -= covered_units
				outputs.append({
					"item_code": member.item_code,
					"output_role": member.output_role,
					"output_per_cycle": member.output_units_per_cycle / snapshot.quantity_scale,
					"planned_qty": planned_units / snapshot.quantity_scale,
					"demand_covered_qty": covered_units / snapshot.quantity_scale,
					"excess_qty": max(planned_units - covered_units, 0) / snapshot.quantity_scale,
					"demand_commitment": member.commitment or None,
					"schedule_result": member_result_names[member.demand_key],
					"output_note": "" if member.required_units else "Produced together with another demanded family output.",
				})
			campaign = frappe.get_doc({
				"doctype": "APS Production Campaign", "planning_run": run.name, "campaign_key": campaign_key,
				"company": run.company, "plant_floor": task.machine and frappe.db.get_value("Workstation", task.machine, "plant_floor"),
				"machine": task.machine, "mold": task.mold,
				"start_time": origin + timedelta(minutes=task.occupied_start_minute),
				"end_time": origin + timedelta(minutes=task.end_minute),
				"planned_cycles": task.cycles, "capacity_owner_segment": segment_name,
				"is_family_campaign": 1, "status": "Planned",
				"source_summary": f"Solver group {group.key}; capacity counted once", "input_fingerprint": solution.input_fingerprint,
				"outputs": outputs, "audit_json": canonical_json({"solution_fingerprint": solution.solution_fingerprint, "created_on": now_datetime()}),
			})
			campaign.flags.aps_campaign_transition = True
			campaign.insert(ignore_permissions=True)
			frappe.db.set_value("APS Schedule Segment", segment_name, {
				"production_campaign": campaign.name,
				"campaign_key": campaign_key,
				"capacity_owner": segment_name,
				"planned_qty": next(
					row["planned_qty"] for row in outputs
					if row["output_role"] == "Primary"
				),
			}, update_modified=False)
			for output in campaign.outputs:
				frappe.db.set_value("APS Schedule Result", output.schedule_result, {
					"production_campaign": campaign.name,
					"campaign_group_key": group.key,
					"campaign_output_role": output.output_role,
				}, update_modified=False)
				if output.output_role != "Primary":
					_append_derived_output_segment(
						output.schedule_result,
						campaign,
						owner_segment_name=segment_name,
						planned_qty=output.planned_qty,
						item_code=output.item_code,
						start_time=origin + timedelta(minutes=task.production_start_minute),
						end_time=origin + timedelta(minutes=task.end_minute),
						task=task,
					)
			created.append(campaign.name)
	for member in (row for group in snapshot.multi_output_groups for row in group.members):
		outcome = outcomes.get(member.demand_key)
		result_name = result_by_demand.get(member.demand_key) or member.result
		if not outcome or not result_name:
			continue
		result = frappe.get_doc("APS Schedule Result", result_name)
		bom_decision = next((row for row in snapshot.bom_decisions if row.item_code == member.item_code), None)
		if bom_decision:
			result.selected_bom = bom_decision.bom
			result.selected_bom_fingerprint = bom_decision.bom_fingerprint
			result.bom_selection_source = bom_decision.selection_source
		result.on_time_qty = outcome["on_time_units"] / snapshot.quantity_scale
		result.recovery_qty = outcome["late_units"] / snapshot.quantity_scale
		result.critical_unplanned_qty = outcome["unscheduled_units"] / snapshot.quantity_scale
		result.status = "Risk" if outcome["late_units"] or outcome["unscheduled_units"] else "Planned"
		result.risk_status = "Critical" if outcome["unscheduled_units"] else "Attention" if outcome["late_units"] else "Normal"
		result.flags.aps_result_engine_transition = True
		result.save(ignore_permissions=True)
		if member.commitment:
			_commit_campaign_outcome(member.commitment, outcome, snapshot.quantity_scale, solution.scenario_key)
	return {"campaigns": created, "campaign_count": len(created)}


def _campaign_member_outcomes(snapshot, solution) -> list[dict[str, Any]]:
	rows = []
	for group in snapshot.multi_output_groups:
		tasks = sorted(
			(row for row in solution.tasks if row.demand_key == group.capacity_owner_demand),
			key=lambda row: (row.end_minute, row.key),
		)
		for member in group.members:
			remaining = member.required_units
			on_time = member.fixed_on_time_units
			late = member.fixed_late_units
			for task in tasks:
				covered = min(remaining, task.cycles * member.output_units_per_cycle)
				if covered <= 0:
					continue
				if task.end_minute <= member.due_minute:
					on_time += covered
				else:
					late += covered
				remaining -= covered
			rows.append({
				"demand_key": member.demand_key,
				"result": member.result,
				"on_time_units": on_time,
				"late_units": late,
				"unscheduled_units": remaining,
			})
	return rows


def _get_or_create_campaign_result(run, snapshot, solution, group, member) -> str:
	existing = frappe.db.get_value(
		"APS Schedule Result",
		{"planning_run": run.name, "campaign_group_key": group.key, "item_code": member.item_code},
		"name",
	)
	if existing:
		return existing
	origin = get_datetime(snapshot.horizon_start)
	bom_decision = next((row for row in snapshot.bom_decisions if row.item_code == member.item_code), None)
	doc = frappe.get_doc({
		"doctype": "APS Schedule Result",
		"planning_run": run.name,
		"company": run.company,
		"plant_floor": run.get("plant_floor"),
		"item_code": member.item_code,
		"requested_date": (origin + timedelta(minutes=member.due_minute)).date(),
		"demand_source": "Campaign Co-product",
		"production_strategy": "Auto Balance",
		"planned_qty": member.required_units / snapshot.quantity_scale,
		"status": "Planned",
		"risk_status": "Normal",
		"admission_class": member.admission_class,
		"service_priority": member.service_priority,
		"effective_due_time": origin + timedelta(minutes=member.due_minute),
		"campaign_group_key": group.key,
		"campaign_output_role": member.output_role,
		"selected_bom": bom_decision.bom if bom_decision else None,
		"selected_bom_fingerprint": bom_decision.bom_fingerprint if bom_decision else None,
		"bom_selection_source": bom_decision.selection_source if bom_decision else None,
		"solver_decision_json": canonical_json({
			"scenario": solution.scenario_key,
			"solution_fingerprint": solution.solution_fingerprint,
			"campaign_generated": True,
		}),
		"schedule_explanation": "Physical co-product created by a shared family-mold campaign.",
	})
	doc.flags.aps_result_engine_transition = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _append_derived_output_segment(result_name, campaign, *, owner_segment_name, planned_qty, item_code, start_time, end_time, task):
	doc = frappe.get_doc("APS Schedule Result", result_name)
	doc.append("segments", {
		"workstation": task.machine,
		"plant_floor": campaign.plant_floor,
		"start_time": start_time,
		"end_time": end_time,
		"current_start_time": start_time,
		"current_end_time": end_time,
		"solver_start_time": start_time,
		"solver_end_time": end_time,
		"horizon_zone": task.horizon_zone,
		"planned_qty": planned_qty,
		"sequence_no": task.sequence_no,
		"lane_key": task.alternative_key,
		"campaign_key": campaign.campaign_key,
		"segment_kind": "Family Co-Product",
		"primary_item_code": next(row.item_code for row in campaign.outputs if row.output_role == "Primary"),
		"co_product_item_code": item_code,
		"mould_reference": task.mold,
		"production_campaign": campaign.name,
		"capacity_owner": owner_segment_name,
		"assignment_reason": "Derived physical output; capacity owned by the campaign primary segment.",
		"solver_task_key": task.key,
		"segment_status": "Applied",
		"risk_status": "Normal",
	})
	doc.flags.aps_result_engine_transition = True
	doc.save(ignore_permissions=True)


def _commit_campaign_outcome(commitment_name, outcome, scale, scenario_key):
	commitment = frappe.get_doc("APS Demand Commitment", commitment_name)
	commitment.on_time_qty = outcome["on_time_units"] / scale
	commitment.late_qty = outcome["late_units"] / scale
	commitment.unscheduled_qty = outcome["unscheduled_units"] / scale
	commitment.transition_reason = f"Applied multi-output solver campaign {scenario_key}"
	commitment.transitioned_by = frappe.session.user
	commitment.transitioned_on = now_datetime()
	commitment.flags.aps_phase2_transition = True
	commitment.save(ignore_permissions=True)


def create_campaign_from_segment(run_name: str, segment: dict[str, Any]) -> dict[str, Any]:
	outputs = _family_outputs(segment["mould_reference"], segment["item_code"])
	if len(outputs) < 2:
		frappe.throw(_("Family mold {0} does not have at least two reliable outputs.", context="Injection APS").format(segment["mould_reference"]), frappe.ValidationError)
	demands = _run_output_demands(run_name, [row["item_code"] for row in outputs])
	for row in outputs:
		row.update(demands.get(row["item_code"]) or {})
		if row["item_code"] == segment["item_code"]:
			row["required_qty"] = max(flt(row.get("required_qty")), flt(segment.get("planned_qty")))
	plan = plan_campaign_outputs(outputs, primary_item=segment["item_code"])
	key = fingerprint({"run": run_name, "mold": segment["mould_reference"], "capacity_owner_segment": segment["name"]})
	existing = frappe.db.get_value("APS Production Campaign", {"campaign_key": key}, "name")
	if existing:
		return {"campaign": existing, "created": False}
	for output in plan["outputs"]:
		if not output.get("schedule_result"):
			output["schedule_result"] = _create_materialized_campaign_result(
				run_name, segment, key, output
			)
	doc = frappe.get_doc({
		"doctype": "APS Production Campaign",
		"planning_run": run_name,
		"campaign_key": key,
		"company": segment["company"],
		"plant_floor": segment.get("plant_floor"),
		"machine": segment["workstation"],
		"mold": segment["mould_reference"],
		"start_time": segment["start_time"],
		"end_time": segment["end_time"],
		"planned_cycles": plan["campaign_cycles"],
		"capacity_owner_segment": segment["name"],
		"is_family_campaign": 1,
		"status": "Planned",
		"source_summary": f"{len(plan['outputs'])} outputs; {plan['campaign_cycles']} shared cycles; capacity counted once",
		"input_fingerprint": fingerprint(plan),
		"outputs": [{key: row.get(key) for key in ("item_code", "output_role", "output_per_cycle", "planned_qty", "demand_covered_qty", "excess_qty", "demand_commitment", "schedule_result", "output_note")} for row in plan["outputs"]],
		"audit_json": canonical_json({"created_by": frappe.session.user, "created_on": now_datetime(), "capacity_count": 1}),
	})
	doc.flags.aps_campaign_transition = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("APS Schedule Segment", segment["name"], {
		"production_campaign": doc.name,
		"campaign_key": key,
		"capacity_owner": segment["name"],
		"planned_qty": next(row.planned_qty for row in doc.outputs if row.output_role == "Primary"),
	}, update_modified=False)
	for output in doc.outputs:
		frappe.db.set_value("APS Schedule Result", output.schedule_result, {
			"production_campaign": doc.name,
			"campaign_group_key": key,
			"campaign_output_role": output.output_role,
		}, update_modified=False)
		if output.output_role == "Co-product":
			_append_derived_output_segment(
				output.schedule_result,
				doc,
				owner_segment_name=segment["name"],
				planned_qty=output.planned_qty,
				item_code=output.item_code,
				start_time=segment["start_time"],
				end_time=segment["end_time"],
				task=SimpleNamespace(
					machine=segment["workstation"], mold=segment["mould_reference"],
					horizon_zone="Demand", sequence_no=1,
					alternative_key=f"{segment['workstation']}|{segment['mould_reference']}",
					key=f"MATERIALIZED:{segment['name']}",
				),
			)
	return {"campaign": doc.name, "created": True}


def _create_materialized_campaign_result(run_name, segment, group_key, output) -> str:
	doc = frappe.get_doc({
		"doctype": "APS Schedule Result",
		"planning_run": run_name,
		"company": segment["company"],
		"plant_floor": segment.get("plant_floor"),
		"item_code": output["item_code"],
		"requested_date": get_datetime(segment["end_time"]).date(),
		"demand_source": "Campaign Co-product",
		"production_strategy": "Auto Balance",
		"planned_qty": flt(output.get("required_qty")),
		"status": "Planned",
		"risk_status": "Normal",
		"campaign_group_key": group_key,
		"campaign_output_role": output["output_role"],
		"schedule_explanation": "Physical output materialized from an exact family-mold interval.",
	})
	doc.flags.aps_result_engine_transition = True
	doc.insert(ignore_permissions=True)
	return doc.name


def campaign_proposal_rows(run_name: str) -> dict[str, Any]:
	"""Return one auditable Work Order proposal row per physical output."""
	_require_campaign_feature()
	rows = []
	result_names = set()
	work_orders = set()
	for name in frappe.get_all(
		"APS Production Campaign",
		filters={"planning_run": run_name, "status": ("!=", "Cancelled")},
		pluck="name",
		order_by="start_time asc, name asc",
	):
		campaign = frappe.get_doc("APS Production Campaign", name)
		for output in campaign.outputs:
			if output.schedule_result:
				result_names.add(output.schedule_result)
			if output.work_order:
				work_orders.add(output.work_order)
			if campaign.status != "Planned" or output.work_order:
				continue
			result = frappe.db.get_value(
				"APS Schedule Result",
				output.schedule_result,
				["customer", "requested_date", "selected_bom"],
				as_dict=True,
			) or {}
			rows.append({
				"result_reference": output.schedule_result,
				"production_campaign": campaign.name,
				"campaign_output_row": output.name,
				"output_role": output.output_role,
				"demand_commitment": output.demand_commitment,
				"capacity_owner": campaign.capacity_owner_segment,
				"item_code": output.item_code,
				"selected_bom": result.get("selected_bom"),
				"customer": result.get("customer"),
				"sales_order": None,
				"sales_order_item": None,
				"sales_order_requirement": "Not Required",
				"required_delivery_date": result.get("requested_date"),
				"action": "New",
				"proposed_qty": output.planned_qty,
				"excess_qty": output.excess_qty,
				"source_reason": output.output_note or "Physical output of one shared family-mold campaign.",
				"result_state_token": campaign_output_state_token(campaign, output),
				"existing_work_order": None,
				"existing_qty": 0,
				"covered_existing_qty": 0,
				"existing_state_token": "",
				"target_start_time": campaign.start_time,
				"target_end_time": campaign.end_time,
				"review_status": "Pending",
				"review_note": "Approve or reject the complete campaign group; all physical outputs are atomic.",
			})
	return {"rows": rows, "result_names": result_names, "work_orders": work_orders}


def campaign_output_state_token(campaign, output) -> str:
	return fingerprint({
		"campaign": campaign.name,
		"campaign_key": campaign.campaign_key,
		"status": campaign.status,
		"capacity_owner_segment": campaign.capacity_owner_segment,
		"machine": campaign.machine,
		"mold": campaign.mold,
		"start_time": campaign.start_time,
		"end_time": campaign.end_time,
		"planned_cycles": campaign.planned_cycles,
		"output_row": output.name,
		"item_code": output.item_code,
		"output_role": output.output_role,
		"output_per_cycle": output.output_per_cycle,
		"planned_qty": output.planned_qty,
		"demand_covered_qty": output.demand_covered_qty,
		"excess_qty": output.excess_qty,
		"demand_commitment": output.demand_commitment,
		"schedule_result": output.schedule_result,
		"work_order": output.work_order,
	})


def create_campaign_work_orders(campaign_name: str, *, proposal_batch: str | None = None, proposal_rows=None) -> dict[str, Any]:
	_require_campaign_feature()
	if not proposal_batch or not proposal_rows:
		frappe.throw(_("Campaign Work Orders must be approved and applied through a Work Order Proposal batch.", context="Injection APS"), frappe.PermissionError)
	identity = frappe.db.get_value("APS Production Campaign", campaign_name, ["company", "planning_run"], as_dict=True)
	if not identity:
		frappe.throw(_("Production Campaign {0} was not found.", context="Injection APS").format(campaign_name), frappe.DoesNotExistError)
	frappe.db.sql("select name from `tabCompany` where name=%s for update", identity.company)
	frappe.db.sql("select name from `tabAPS Planning Run` where name=%s for update", identity.planning_run)
	frappe.db.sql("select name from `tabAPS Production Campaign` where name=%s for update", campaign_name)
	campaign = frappe.get_doc("APS Production Campaign", campaign_name)
	if campaign.status in {"Released", "In Progress", "Completed"} and all(row.work_order for row in campaign.outputs):
		return {"campaign": campaign.name, "work_orders": [row.work_order for row in campaign.outputs], "idempotent_replay": True}
	if campaign.status != "Planned":
		frappe.throw(_("Only a Planned campaign can create Work Orders.", context="Injection APS"), frappe.ValidationError)
	rows_by_output = {row.get("campaign_output_row"): row for row in proposal_rows}
	if set(rows_by_output) != {row.name for row in campaign.outputs}:
		frappe.throw(_("The approved proposal does not contain every Campaign output exactly once.", context="Injection APS"), frappe.ValidationError)
	for output in campaign.outputs:
		proposal = rows_by_output[output.name]
		if proposal.get("review_status") != "Approved":
			frappe.throw(_("Every Campaign output must be Approved before atomic Work Order creation.", context="Injection APS"), frappe.ValidationError)
		if proposal.get("result_state_token") != campaign_output_state_token(campaign, output):
			frappe.throw(_("Campaign {0} changed after review. Regenerate the proposal batch.", context="Injection APS").format(campaign.name), frappe.ValidationError)
	run = frappe.get_doc("APS Planning Run", campaign.planning_run)
	settings = planning.get_settings_dict()
	savepoint = f"aps_campaign_wo_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(savepoint)
	created = []
	try:
		for output in campaign.outputs:
			if output.work_order:
				created.append(output.work_order)
				continue
			result = frappe.get_doc("APS Schedule Result", output.schedule_result) if output.schedule_result else SimpleNamespace(
				name=f"{campaign.name}:{output.item_code}", item_code=output.item_code,
				demand_source="Stock Production", requested_date=get_datetime(campaign.end_time).date(), is_urgent=0,
			)
			work_order = planning._create_formal_work_order(
				run_doc=run,
				result=result,
				qty=flt(output.planned_qty),
				start_time=campaign.start_time,
				end_time=campaign.end_time,
				settings=settings,
				proposal_batch=proposal_batch,
				sales_order=None,
				sales_order_item=None,
				aps_campaign=campaign.name,
				aps_output_role=output.output_role,
				aps_capacity_owner=campaign.capacity_owner_segment,
				aps_commitment=output.demand_commitment,
				aps_source_reason=output.output_note or "APS multi-output production campaign",
			)
			output.work_order = work_order
			created.append(work_order)
		campaign.status = "Released"
		campaign.audit_json = canonical_json({
			"event": "released",
			"proposal_batch": proposal_batch,
			"released_by": frappe.session.user,
			"released_on": now_datetime(),
			"work_orders": created,
		})
		campaign.flags.aps_campaign_transition = True
		campaign.save(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return {"campaign": campaign.name, "work_orders": created, "idempotent_replay": False}


def sync_campaign_actuals(run_name: str) -> dict[str, Any]:
	if not frappe.db.exists("DocType", "APS Production Campaign"):
		return {"campaign_count": 0}
	campaigns = frappe.get_all("APS Production Campaign", filters={"planning_run": run_name, "status": ("!=", "Cancelled")}, pluck="name")
	for name in campaigns:
		doc = frappe.get_doc("APS Production Campaign", name)
		for output in doc.outputs:
			if not output.work_order:
				continue
			actual = frappe.db.sql(
				"""select coalesce(sum(good_qty),0) as good_qty, coalesce(sum(scrap_qty),0) as scrap_qty
				from `tabAPS Production Allocation` where work_order=%s and is_effective=1""",
				output.work_order,
				as_dict=True,
			)[0]
			output.actual_good_qty = flt(actual.good_qty) or flt(frappe.db.get_value("Work Order", output.work_order, "produced_qty"))
			output.actual_scrap_qty = flt(actual.scrap_qty)
		primary = next((row for row in doc.outputs if row.output_role == "Primary"), None)
		doc.actual_cycles = flt(primary.actual_good_qty) / max(flt(primary.output_per_cycle), QTY_TOLERANCE) if primary else 0
		if all(flt(row.actual_good_qty) + QTY_TOLERANCE >= flt(row.planned_qty) for row in doc.outputs):
			doc.status = "Completed"
		elif any(flt(row.actual_good_qty) > QTY_TOLERANCE for row in doc.outputs):
			doc.status = "In Progress"
		doc.flags.aps_campaign_transition = True
		doc.save(ignore_permissions=True)
	return {"campaign_count": len(campaigns)}


def sync_campaign_derived_segments(campaign_name: str, owner_segment) -> None:
	"""Move the physical Campaign once and mirror its non-capacity output rows."""
	if not campaign_name:
		return
	campaign = frappe.get_doc("APS Production Campaign", campaign_name)
	if campaign.capacity_owner_segment != owner_segment.name:
		frappe.throw(_("Only Campaign capacity owner {0} may move the shared interval.", context="Injection APS").format(campaign.capacity_owner_segment), frappe.ValidationError)
	values = {
		"workstation": owner_segment.workstation,
		"plant_floor": owner_segment.plant_floor,
		"start_time": owner_segment.start_time,
		"end_time": owner_segment.end_time,
		"current_start_time": owner_segment.get("current_start_time") or owner_segment.start_time,
		"current_end_time": owner_segment.get("current_end_time") or owner_segment.end_time,
		"forecast_start_time": owner_segment.get("forecast_start_time"),
		"forecast_end_time": owner_segment.get("forecast_end_time"),
		"replan_cycle": owner_segment.get("replan_cycle"),
		"mould_reference": owner_segment.mould_reference,
	}
	for name in frappe.get_all(
		"APS Schedule Segment",
		filters={"production_campaign": campaign.name, "segment_kind": "Family Co-Product"},
		pluck="name",
	):
		frappe.db.set_value("APS Schedule Segment", name, values, update_modified=False)
	campaign.start_time = owner_segment.start_time
	campaign.end_time = owner_segment.end_time
	campaign.machine = owner_segment.workstation
	campaign.plant_floor = owner_segment.plant_floor
	campaign.mold = owner_segment.mould_reference
	campaign.flags.aps_campaign_transition = True
	campaign.save(ignore_permissions=True)


def _family_outputs(mold, primary_item):
	primary_group = frappe.db.get_value("Mold Product", {"parent": mold, "parenttype": "Mold", "item_code": primary_item}, "output_group") or "Default"
	rows = frappe.get_all("Mold Product", filters={"parent": mold, "parenttype": "Mold", "output_group": primary_group}, fields=["item_code", "output_qty", "cavity_output_qty"], order_by="idx asc", limit_page_length=0)
	return [{"item_code": row.item_code, "output_per_cycle": flt(row.cavity_output_qty or row.output_qty)} for row in rows]


def _run_output_demands(run_name, items):
	rows = frappe.get_all("APS Schedule Result", filters={"planning_run": run_name, "item_code": ("in", items), "exclude_from_release": 0}, fields=["name", "item_code", "planned_qty", "demand_commitment"], order_by="requested_date asc, name asc", limit_page_length=0)
	result = {}
	for row in rows:
		if row.item_code in result:
			frappe.throw(_("Family Campaign output {0} has more than one Run-level Result; consolidate exact demand ownership before materializing.", context="Injection APS").format(row.item_code), frappe.ValidationError)
		result[row.item_code] = {"required_qty": flt(row.planned_qty), "schedule_result": row.name, "demand_commitment": row.demand_commitment}
	return result


def _require_campaign_feature():
	settings = v2_flags.get_v2_settings()
	if not (settings["enable_aps_v2"] and settings["solver_engine"] == "CP-SAT" and settings["enable_coproduct_campaign"]):
		frappe.throw(_("Enable APS V2, CP-SAT, and Co-product Campaign before using campaign planning.", context="Injection APS"), frappe.PermissionError)
