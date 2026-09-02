from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Callable

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, now_datetime

from injection_aps.services import v2_flags
from injection_aps.services.solver.serialization import canonical_json, fingerprint


QTY_TOLERANCE = 0.000001


class BOMCycleError(ValueError):
	pass


class BOMInputError(ValueError):
	pass


def expand_bom_requirements(
	roots: list[dict[str, Any]],
	*,
	bom_by_item: dict[str, dict[str, Any]],
	item_group_by_item: dict[str, str],
	producible_groups: set[str],
	stock_by_item: dict[str, float] | None = None,
	wip_by_item: dict[str, float] | None = None,
	batch_by_item: dict[str, float] | None = None,
) -> dict[str, Any]:
	"""Expand a manufacturing DAG while keeping both merged capacity and exact root lineage."""
	stock_remaining = defaultdict(float, stock_by_item or {})
	wip_remaining = defaultdict(float, wip_by_item or {})
	batch_by_item = batch_by_item or {}
	root_by_key = {str(row["key"]): row for row in roots}
	if len(root_by_key) != len(roots):
		raise BOMInputError("Each root demand key must be unique.")
	_validate_master_cycles([row["item_code"] for row in roots], bom_by_item, item_group_by_item, producible_groups)

	nodes: dict[str, dict[str, Any]] = {}
	edges: dict[tuple[str, str], dict[str, Any]] = {}
	children: dict[str, set[str]] = defaultdict(set)
	indegree: dict[str, int] = defaultdict(int)

	def add_structure(parent_id: str, parent_item: str, due_time, level: int, root_key: str, path: tuple[str, ...]):
		bom = bom_by_item.get(parent_item)
		if not bom:
			return
		for component in bom.get("components") or []:
			item = component["item_code"]
			is_producible = item_group_by_item.get(item) in producible_groups
			bucket = get_datetime(due_time).strftime("%Y%m%d%H%M")
			child_id = f"BOM:{item}:{bucket}" if is_producible else f"RAW:{item}:{bucket}"
			if child_id not in nodes:
				nodes[child_id] = {
					"id": child_id, "item_code": item, "due_time": get_datetime(due_time), "level": level,
					"is_root": False, "is_producible": is_producible, "gross_qty": 0.0,
					"gross_by_root": defaultdict(float), "root_keys": set(),
				}
			nodes[child_id]["root_keys"].add(root_key)
			key = (parent_id, child_id)
			if key not in edges:
				edges[key] = {
					"parent_id": parent_id, "child_id": child_id, "parent_item": parent_item, "component_item": item,
					"bom": bom.get("name") or "", "bom_output_qty": max(flt(bom.get("output_qty")), QTY_TOLERANCE),
					"component_qty": max(flt(component.get("qty")), 0),
					"loss_percent": max(min(flt(component.get("loss_percent")), 99.999), 0),
					"component_uom": component.get("component_uom") or component.get("uom") or "",
					"stock_uom": component.get("stock_uom") or "",
					"conversion_factor": max(flt(component.get("conversion_factor")), QTY_TOLERANCE),
					"bom_fingerprint": bom.get("fingerprint") or fingerprint(bom),
					"selection_source": bom.get("selection_source") or "Default BOM",
					"level": level, "root_keys": set(), "required_by_root": defaultdict(float),
				}
				children[parent_id].add(child_id)
				indegree[child_id] += 1
			edges[key]["root_keys"].add(root_key)
			if is_producible and item not in path:
				add_structure(child_id, item, due_time, level + 1, root_key, path + (item,))

	for root in roots:
		root_key = str(root["key"])
		root_id = f"ROOT:{root_key}"
		quantity = max(flt(root.get("quantity")), 0)
		nodes[root_id] = {
			"id": root_id, "item_code": root["item_code"], "due_time": get_datetime(root["due_time"]), "level": 0,
			"is_root": True, "is_producible": True, "gross_qty": quantity, "production_qty": quantity,
			"gross_by_root": {root_key: quantity}, "production_by_root": {root_key: quantity},
			"demand_key": root_key, "commitment": root.get("commitment") or "",
			"service_priority": int(root.get("service_priority") or 0), "root_keys": {root_key},
		}
		indegree.setdefault(root_id, 0)
		add_structure(root_id, root["item_code"], root["due_time"], 1, root_key, (root["item_code"],))

	def root_sort_key(root_key: str):
		root = root_by_key[root_key]
		rank = {"P0": 0, "P1": 1, "P2": 2}.get(str(root.get("admission_class") or "P0"), 3)
		return (rank, get_datetime(root["due_time"]), -int(root.get("service_priority") or 0), root_key)

	queue = deque(sorted((node_id for node_id in nodes if indegree[node_id] == 0), key=lambda value: (nodes[value]["due_time"], value)))
	processed = []
	while queue:
		node_id = queue.popleft()
		node = nodes[node_id]
		processed.append(node_id)
		if not node["is_root"]:
			gross_by_root = {key: max(flt(qty), 0) for key, qty in node["gross_by_root"].items() if flt(qty) > QTY_TOLERANCE}
			node["gross_qty"] = sum(gross_by_root.values())
			stock_by_root = defaultdict(float)
			wip_by_root = defaultdict(float)
			production_by_root = defaultdict(float)
			if node["is_producible"]:
				for root_key in sorted(gross_by_root, key=root_sort_key):
					gross = gross_by_root[root_key]
					stock = min(gross, max(stock_remaining[node["item_code"]], 0))
					stock_remaining[node["item_code"]] -= stock
					after_stock = max(gross - stock, 0)
					wip = min(after_stock, max(wip_remaining[node["item_code"]], 0))
					wip_remaining[node["item_code"]] -= wip
					stock_by_root[root_key] = stock
					wip_by_root[root_key] = wip
					production_by_root[root_key] = max(after_stock - wip, 0)
				batch = max(flt(batch_by_item.get(node["item_code"])), 0)
				net = sum(production_by_root.values())
				production = math.ceil(net / batch) * batch if batch > QTY_TOLERANCE and net > QTY_TOLERANCE else net
				excess = max(production - net, 0)
				production_roots = [key for key in sorted(production_by_root, key=root_sort_key) if production_by_root[key] > QTY_TOLERANCE]
				if excess > QTY_TOLERANCE and production_roots:
					production_by_root[production_roots[-1]] += excess
				node.update({
					"stock_covered_qty": sum(stock_by_root.values()), "wip_covered_qty": sum(wip_by_root.values()),
					"production_qty": production, "batch_size": batch, "batch_excess_qty": excess,
					"stock_by_root": dict(stock_by_root), "wip_by_root": dict(wip_by_root),
					"production_by_root": dict(production_by_root),
				})
			else:
				node.update({
					"stock_covered_qty": 0.0, "wip_covered_qty": 0.0, "production_qty": 0.0,
					"batch_size": 0.0, "batch_excess_qty": 0.0, "stock_by_root": {},
					"wip_by_root": {}, "production_by_root": {},
				})
		for child_id in sorted(children.get(node_id) or ()):
			edge = edges[(node_id, child_id)]
			loss_factor = 1 - flt(edge["loss_percent"]) / 100
			for root_key, parent_production in sorted((node.get("production_by_root") or {}).items(), key=lambda row: root_sort_key(row[0])):
				required = max(flt(parent_production), 0) * flt(edge["component_qty"]) / flt(edge["bom_output_qty"]) / max(loss_factor, QTY_TOLERANCE)
				edge["required_by_root"][root_key] += required
				nodes[child_id]["gross_by_root"][root_key] += required
			edge["required_gross_qty"] = sum(edge["required_by_root"].values())
			nodes[child_id]["gross_qty"] = sum(nodes[child_id]["gross_by_root"].values())
			indegree[child_id] -= 1
			if indegree[child_id] == 0:
				queue.append(child_id)
		queue = deque(sorted(queue, key=lambda value: (nodes[value]["due_time"], value)))
	if len(processed) != len(nodes):
		raise BOMCycleError("BOM dependency graph contains a cycle.")

	# Coverage is consumed once at the merged node, then attributed exactly to every root/parent pair.
	peggings = []
	for child_id, child in sorted(nodes.items(), key=lambda value: (value[1]["due_time"], value[0])):
		incoming = sorted(
			(edge for edge in edges.values() if edge["child_id"] == child_id),
			key=lambda value: (nodes[value["parent_id"]]["due_time"], value["parent_id"]),
		)
		stock_left = defaultdict(float, child.get("stock_by_root") or {})
		wip_left = defaultdict(float, child.get("wip_by_root") or {})
		production_left = defaultdict(float, child.get("production_by_root") or {})
		child_rows = []
		for edge in incoming:
			for root_key, required in sorted(edge["required_by_root"].items(), key=lambda row: root_sort_key(row[0])):
				stock = min(required, stock_left[root_key]); stock_left[root_key] -= stock
				remaining = max(required - stock, 0)
				wip = min(remaining, wip_left[root_key]); wip_left[root_key] -= wip
				remaining = max(remaining - wip, 0)
				production = min(remaining, production_left[root_key]); production_left[root_key] -= production
				row = {
					**edge, "root_demand_key": root_key, "root_commitment": root_by_key[root_key].get("commitment") or "",
					"parent_demand_key": nodes[edge["parent_id"]].get("demand_key") or edge["parent_id"],
					"child_demand_key": child_id if child["is_producible"] and flt(child.get("production_qty")) > QTY_TOLERANCE else "",
					"required_gross_qty": required, "stock_covered_qty": stock, "wip_covered_qty": wip,
					"production_qty": production, "batch_size": flt(child.get("batch_size")),
					"batch_excess_qty": 0.0, "required_available_time": nodes[edge["parent_id"]]["due_time"],
					"is_raw_material_leaf": not child["is_producible"],
				}
				peggings.append(row); child_rows.append(row)
		for root_key, excess in sorted(production_left.items(), key=lambda row: root_sort_key(row[0])):
			if excess <= QTY_TOLERANCE:
				continue
			last = next((row for row in reversed(child_rows) if row["root_demand_key"] == root_key), None)
			if last:
				last["production_qty"] += excess
				last["batch_excess_qty"] += excess

	demands = []
	for node_id, node in sorted(nodes.items(), key=lambda value: (value[1]["due_time"], value[0])):
		if node["is_root"] or not node["is_producible"] or flt(node.get("production_qty")) <= QTY_TOLERANCE:
			continue
		node["demand_key"] = node_id
		production_roots = [key for key, qty in (node.get("production_by_root") or {}).items() if flt(qty) > QTY_TOLERANCE]
		best_root = min(production_roots or list(node["root_keys"]), key=root_sort_key)
		demands.append({
			"key": node_id, "result": "", "commitment": "", "item_code": node["item_code"],
			"admission_class": str(root_by_key[best_root].get("admission_class") or "P0"),
			"quantity": node["production_qty"], "due_time": node["due_time"], "original_due_time": node["due_time"],
			"service_priority": max((int(root_by_key[key].get("service_priority") or 0) for key in node["root_keys"]), default=0),
			"root_demand_keys": sorted(node["root_keys"], key=root_sort_key), "bom_generated": True,
		})

	peggings_by_edge = defaultdict(list)
	for row in peggings:
		peggings_by_edge[(row["parent_id"], row["child_id"])].append(row)
	precedences = []
	for edge_key, edge in sorted(edges.items(), key=lambda value: (value[1]["level"], value[0])):
		rows = peggings_by_edge[edge_key]
		child = nodes[edge["child_id"]]
		required = sum(flt(row["required_gross_qty"]) for row in rows)
		stock = sum(flt(row["stock_covered_qty"]) for row in rows)
		wip = sum(flt(row["wip_covered_qty"]) for row in rows)
		production = sum(flt(row["production_qty"]) for row in rows)
		root_keys = sorted({row["root_demand_key"] for row in rows}, key=root_sort_key)
		precedences.append({
			"predecessor_demand": edge["child_id"] if child["is_producible"] and production > QTY_TOLERANCE else "",
			"successor_demand": nodes[edge["parent_id"]].get("demand_key") or edge["parent_id"], "lag_minutes": 0,
			"parent_item": edge["parent_item"], "component_item": edge["component_item"], "bom": edge["bom"],
			"bom_fingerprint": edge["bom_fingerprint"], "selection_source": edge["selection_source"], "level": edge["level"],
			"qty_per_parent": edge["component_qty"], "bom_output_qty": edge["bom_output_qty"], "loss_percent": edge["loss_percent"],
			"component_uom": edge["component_uom"], "stock_uom": edge["stock_uom"], "conversion_factor": edge["conversion_factor"],
			"required_qty": required, "stock_covered_qty": stock, "wip_covered_qty": wip, "production_qty": production,
			"batch_size": flt(child.get("batch_size")), "batch_excess_qty": sum(flt(row["batch_excess_qty"]) for row in rows),
			"required_available_time": nodes[edge["parent_id"]]["due_time"],
			"root_demand_key": root_keys[0] if root_keys else "", "root_demand_keys": root_keys,
			"root_allocations": [{
				"root_demand_key": row["root_demand_key"], "required_qty": row["required_gross_qty"],
				"stock_covered_qty": row["stock_covered_qty"], "wip_covered_qty": row["wip_covered_qty"],
				"production_qty": row["production_qty"],
			} for row in rows],
			"is_raw_material_leaf": not child["is_producible"],
		})
	return {"demands": demands, "precedences": precedences, "peggings": peggings, "nodes": nodes}


def get_run_bom_selections(run_name: str) -> dict[str, Any]:
	"""Return the immutable-per-analysis BOM decision surface for a Run."""
	require_bom_feature()
	run = frappe.get_doc("APS Planning Run", run_name)
	settings = frappe.get_cached_doc("APS Settings")
	current = _run_bom_selection_snapshot(run)
	root_items = set(frappe.get_all("APS Schedule Result", filters={"planning_run": run.name}, pluck="item_code", limit_page_length=0))
	groups = configured_producible_groups(settings)
	eligible_items = set(root_items)
	try:
		masters, _item_groups, _batch = _load_master_graph(
			root_items,
			groups,
			bom_selections={row["item_code"]: row for row in current.get("selections") or []},
			bom_policy=current.get("policy") or settings.get("aps_bom_policy") or "Default BOM Only",
		)
		eligible_items.update(masters)
	except (BOMInputError, BOMCycleError):
		# The decision dialog must remain available to repair a bad selection or
		# missing root BOM. Analysis itself still fails closed with a formal blocker.
		pass
	options = []
	if eligible_items:
		for row in frappe.get_all(
			"BOM",
			filters={"item": ("in", sorted(eligible_items)), "is_active": 1, "docstatus": 1},
			fields=["name", "item", "is_default", "quantity", "modified"],
			order_by="item asc, is_default desc, name asc",
			limit_page_length=0,
		):
			options.append(dict(row))
	effective_policy = current.get("policy") if current.get("selections") else (settings.get("aps_bom_policy") or "Default BOM Only")
	return {
		"planning_run": run.name, "run_modified": str(run.modified),
		"policy": effective_policy,
		"selection_fingerprint": run.get("bom_selection_fingerprint") or fingerprint(current),
		"selections": current.get("selections") or [], "options": options,
	}


def set_run_bom_selections(
	run_name: str,
	selections: list[dict[str, Any]] | dict[str, str] | str | None,
	*,
	reason: str | None,
	expected_run_modified: str | None,
) -> dict[str, Any]:
	"""Approve explicit alternatives and invalidate every older solver projection."""
	require_bom_feature()
	frappe.db.sql("select name from `tabAPS Planning Run` where name=%s for update", run_name)
	run = frappe.get_doc("APS Planning Run", run_name)
	if expected_run_modified and str(run.modified) != str(expected_run_modified):
		frappe.throw(_("The Planning Run changed. Refresh BOM selections and try again.", context="Injection APS"), frappe.TimestampMismatchError)
	if run.status not in ("Draft", "Planned") or run.get("capacity_balance_applied_on"):
		frappe.throw(_("BOM selections can be changed only before the schedule is applied or released.", context="Injection APS"), frappe.ValidationError)
	if isinstance(selections, str):
		selections = json.loads(selections or "[]")
	if isinstance(selections, dict):
		selections = [{"item_code": item, "bom": bom} for item, bom in selections.items()]
	selections = selections or []
	policy = frappe.db.get_single_value("APS Settings", "aps_bom_policy") or "Default BOM Only"
	if selections and policy != "Explicit Approved Alternative":
		frappe.throw(_("APS BOM Policy must be Explicit Approved Alternative before selecting a non-default BOM.", context="Injection APS"), frappe.ValidationError)
	reason = str(reason or "").strip()
	if selections and not reason:
		frappe.throw(_("A reason is required when approving BOM alternatives.", context="Injection APS"), frappe.ValidationError)
	normalized = []
	seen = set()
	for source in selections:
		item = str(source.get("item_code") or "").strip()
		bom_name = str(source.get("bom") or "").strip()
		if not item or not bom_name or item in seen:
			frappe.throw(_("Each BOM selection requires one unique Item and BOM.", context="Injection APS"), frappe.ValidationError)
		bom = frappe.db.get_value("BOM", bom_name, ["name", "item", "is_active", "docstatus", "modified"], as_dict=True)
		if not bom or bom.item != item or not bom.is_active or int(bom.docstatus or 0) != 1:
			frappe.throw(_("BOM {0} is not an active submitted BOM for Item {1}.", context="Injection APS").format(bom_name, item), frappe.ValidationError)
		seen.add(item)
		normalized.append({
			"item_code": item, "bom": bom_name, "bom_modified": str(bom.modified),
			"approved_by": frappe.session.user, "approved_on": str(now_datetime()), "reason": reason,
		})
	normalized.sort(key=lambda row: (row["item_code"], row["bom"]))
	if normalized:
		root_items = set(frappe.get_all("APS Schedule Result", filters={"planning_run": run.name}, pluck="item_code", limit_page_length=0))
		masters, item_groups, _batch = _load_master_graph(
			root_items,
			configured_producible_groups(),
			bom_selections={row["item_code"]: row for row in normalized},
			bom_policy=policy,
		)
		unreachable = sorted(set(seen) - set(masters))
		if unreachable:
			frappe.throw(_("BOM selections are not reachable from this Run: {0}.", context="Injection APS").format(", ".join(unreachable)), frappe.ValidationError)
		_validate_master_cycles(root_items, masters, item_groups, configured_producible_groups())
	snapshot = {"version": 1, "policy": policy, "selections": normalized}
	selection_fingerprint = fingerprint(snapshot)
	values = {
		"bom_selection_json": canonical_json(snapshot), "bom_selection_fingerprint": selection_fingerprint,
		"bom_selection_by": frappe.session.user if normalized else None,
		"bom_selection_on": now_datetime() if normalized else None, "bom_selection_reason": reason or None,
		"solver_job": None, "solver_status": "Not Run", "solver_phase": None,
		"solver_input_fingerprint": None, "solver_solution_fingerprint": None, "selected_solver_scenario": None,
		"solver_acknowledged_by": None, "solver_acknowledged_on": None, "solver_acknowledgment_reason": None,
		"solver_acknowledgment_fingerprint": None, "capacity_balance_status": "Not Analyzed",
		"capacity_balance_analyzed_on": None, "capacity_balance_confirmed_by": None,
		"capacity_balance_confirmed_on": None, "capacity_balance_fingerprint": None,
		"capacity_balance_analysis_json": None,
	}
	frappe.db.set_value("APS Planning Run", run.name, values, update_modified=True)
	return get_run_bom_selections(run.name)


def expand_solver_demands(
	company: str,
	demands: list[dict[str, Any]],
	*,
	producible_groups: set[str],
	planning_run: str | None = None,
) -> dict[str, Any]:
	items = {row["item_code"] for row in demands}
	selection_snapshot = _run_bom_selection_snapshot(frappe.get_doc("APS Planning Run", planning_run)) if planning_run else {"selections": []}
	selections = {row["item_code"]: row for row in selection_snapshot.get("selections") or []}
	bom_by_item, item_groups, batch_by_item = _load_master_graph(
		items,
		producible_groups,
		bom_selections=selections,
		bom_policy=selection_snapshot.get("policy") or frappe.db.get_single_value("APS Settings", "aps_bom_policy") or "Default BOM Only",
	)
	component_items = set(item_groups) - items
	stock = _finished_or_semifinished_stock(company, component_items, exclude_run=planning_run)
	wip = _effective_wip(company, component_items)
	result = expand_bom_requirements(
		demands,
		bom_by_item=bom_by_item,
		item_group_by_item=item_groups,
		producible_groups=producible_groups,
		stock_by_item=stock,
		wip_by_item=wip,
		batch_by_item=batch_by_item,
	)
	result["bom_selection_snapshot"] = selection_snapshot
	result["bom_decisions"] = [
		{
			"item_code": item_code, "bom": bom["name"], "bom_fingerprint": bom["fingerprint"],
			"selection_source": bom["selection_source"], "output_qty": bom["output_qty"],
		}
		for item_code, bom in sorted(bom_by_item.items())
	]
	result["stock_snapshot"] = stock
	result["wip_snapshot"] = wip
	return result


def persist_solver_peggings(run, snapshot, solution) -> dict[str, Any]:
	if not snapshot.precedences:
		return {"pegging_count": 0}
	frappe.db.delete("APS BOM Pegging", {"planning_run": run.name, "status": ("not in", ["Completed"])})
	tasks_by_demand = defaultdict(list)
	for task in solution.tasks:
		tasks_by_demand[task.demand_key].append(task)
	outcomes = {row.demand_key: row for row in solution.outcomes}
	demands = {row.key: row for row in snapshot.demands}
	created = []
	origin = get_datetime(snapshot.horizon_start)
	for edge in snapshot.precedences:
		planned_minute = max((row.end_minute for row in tasks_by_demand.get(edge.predecessor_demand) or []), default=None)
		parent_start = min((row.occupied_start_minute for row in tasks_by_demand.get(edge.successor_demand) or []), default=edge.required_available_minute)
		required_minute = parent_start
		allocations = edge.root_allocations or ((
			edge.root_demand_key or edge.successor_demand,
			edge.required_units, edge.stock_covered_units, edge.wip_covered_units, edge.production_units,
		),)
		for root_key, required_units, stock_units, wip_units, production_units in allocations:
			root_demand = demands.get(root_key)
			root_commitment = root_demand.commitment if root_demand else (
				root_key if frappe.db.exists("APS Demand Commitment", root_key) else ""
			)
			parent_demand = demands.get(edge.successor_demand)
			child_demand = demands.get(edge.predecessor_demand)
			parent_result = _result_for_demand(run.name, edge.successor_demand, parent_demand)
			child_result = _result_for_demand(run.name, edge.predecessor_demand, child_demand) if edge.predecessor_demand else None
			predecessor_incomplete = bool(edge.predecessor_demand and outcomes.get(edge.predecessor_demand) and outcomes[edge.predecessor_demand].unscheduled_units)
			if edge.is_raw_material_leaf:
				status = "Informational"
			elif not edge.predecessor_demand:
				status = "Covered"
			elif planned_minute is None or predecessor_incomplete or planned_minute > required_minute:
				status = "Late"
			else:
				status = "Scheduled"
			batch_excess_units = max(stock_units + wip_units + production_units - required_units, 0)
			doc = frappe.get_doc({
				"doctype": "APS BOM Pegging", "planning_run": run.name, "company": run.company,
				"root_demand_key": root_key or edge.root_demand_key or edge.successor_demand,
				"root_commitment": root_commitment or None,
				"parent_demand_key": edge.successor_demand, "child_demand_key": edge.predecessor_demand,
				"parent_commitment": parent_demand.commitment if parent_demand and parent_demand.commitment else None,
				"child_commitment": child_demand.commitment if child_demand and child_demand.commitment else None,
				"parent_result": parent_result, "child_result": child_result,
				"parent_item": edge.parent_item, "component_item": edge.component_item, "bom": edge.bom or None,
				"bom_fingerprint": edge.bom_fingerprint or fingerprint({"bom": edge.bom, "parent": edge.parent_item, "component": edge.component_item}),
				"selection_source": edge.selection_source, "level": edge.level,
				"qty_per_parent": edge.qty_per_parent_units / snapshot.quantity_scale,
				"bom_output_qty": edge.bom_output_units / snapshot.quantity_scale,
				"loss_percent": edge.loss_percent_ppm / 10_000,
				"component_uom": edge.component_uom, "stock_uom": edge.stock_uom,
				"conversion_factor": edge.conversion_factor_ppm / 1_000_000,
				"batch_size": edge.batch_size_units / snapshot.quantity_scale,
				"batch_excess_qty": batch_excess_units / snapshot.quantity_scale,
				"required_gross_qty": required_units / snapshot.quantity_scale,
				"stock_covered_qty": stock_units / snapshot.quantity_scale,
				"wip_covered_qty": wip_units / snapshot.quantity_scale,
				"production_qty": production_units / snapshot.quantity_scale,
				"required_available_time": origin + __import__("datetime").timedelta(minutes=required_minute),
				"planned_available_time": origin + __import__("datetime").timedelta(minutes=planned_minute) if planned_minute is not None else None,
				"is_raw_material_leaf": int(edge.is_raw_material_leaf), "status": status,
				"source_snapshot_json": canonical_json({
					"input_fingerprint": solution.input_fingerprint, "solution_fingerprint": solution.solution_fingerprint,
					"bom_fingerprint": edge.bom_fingerprint, "selection_source": edge.selection_source,
					"root_allocation_units": [root_key, required_units, stock_units, wip_units, production_units],
				}),
			})
			doc.flags.aps_bom_transition = True
			doc.insert(ignore_permissions=True)
			created.append(doc.name)
	return {"pegging_count": len(created), "peggings": created}


def validate_derived_result_evidence(
	run_name: str,
	result_rows: list[dict[str, Any]],
) -> dict[str, Any]:
	"""Validate BOM Results against their frozen solver pegging evidence."""
	results = [dict(row) for row in result_rows or [] if row.get("bom_demand_key")]
	if not results:
		return {"valid": True, "checked": 0, "errors": []}
	peggings = [
		dict(row)
		for row in frappe.get_all(
			"APS BOM Pegging",
			filters={"planning_run": run_name, "status": ("!=", "Cancelled")},
			fields=[
				"name", "parent_demand_key", "child_demand_key", "parent_result",
				"child_result", "parent_item", "component_item", "bom",
				"bom_fingerprint", "required_gross_qty", "stock_covered_qty",
				"wip_covered_qty", "production_qty", "batch_excess_qty",
				"root_demand_key", "source_snapshot_json",
			],
			limit_page_length=0,
		)
	]
	errors = []
	keys: dict[str, list[str]] = defaultdict(list)
	for result in results:
		keys[str(result.get("bom_demand_key") or "")].append(result.get("name") or "<unnamed>")
	for demand_key, names in keys.items():
		if not demand_key or len(names) != 1:
			errors.append({
				"code": "bom_result_identity",
				"result": ", ".join(names),
				"message": f"BOM demand key {demand_key or '<missing>'} is not unique within the Run.",
			})

	for result in results:
		name = result.get("name") or "<unnamed>"
		key = result.get("bom_demand_key") or ""
		try:
			decision = json.loads(result.get("solver_decision_json") or "{}")
		except (TypeError, ValueError):
			decision = {}
		solution_fingerprint = str(
			decision.get("solution_fingerprint") or ""
			if isinstance(decision, dict)
			else ""
		)
		matching_peggings = [
			row for row in peggings
			if _pegging_solution_fingerprint(row) == solution_fingerprint
		]
		incoming = [
			row for row in matching_peggings
			if row.get("child_result") == name
			and row.get("child_demand_key") == key
			and row.get("component_item") == result.get("item_code")
		]
		outgoing = [
			row for row in matching_peggings
			if row.get("parent_result") == name
			and row.get("parent_demand_key") == key
			and row.get("parent_item") == result.get("item_code")
			and row.get("bom") == result.get("selected_bom")
			and row.get("bom_fingerprint") == result.get("selected_bom_fingerprint")
		]
		if (
			result.get("demand_source") != "BOM Component"
			or result.get("demand_commitment")
			or not result.get("selected_bom")
			or not result.get("selected_bom_fingerprint")
			or not solution_fingerprint
		):
			errors.append({
				"code": "bom_result_identity",
				"result": name,
				"message": "BOM Result identity or frozen solver/BOM decision is incomplete.",
			})
		if not incoming or any(not row.get("parent_result") or not row.get("root_demand_key") for row in incoming):
			errors.append({
				"code": "bom_result_lineage",
				"result": name,
				"message": "BOM Result has no complete incoming parent/root lineage.",
			})
		if not outgoing:
			errors.append({
				"code": "bom_result_lineage",
				"result": name,
				"message": "BOM Result has no outgoing pegging for its frozen selected BOM.",
			})
		planned_qty = flt(result.get("planned_qty"))
		production_qty = sum(flt(row.get("production_qty")) for row in incoming)
		quantity_invalid = planned_qty < -QTY_TOLERANCE or abs(planned_qty - production_qty) > QTY_TOLERANCE
		for row in incoming:
			quantities = [
				flt(row.get("required_gross_qty")),
				flt(row.get("stock_covered_qty")),
				flt(row.get("wip_covered_qty")),
				flt(row.get("production_qty")),
				flt(row.get("batch_excess_qty")),
			]
			if min(quantities) < -QTY_TOLERANCE or abs(
				quantities[1] + quantities[2] + quantities[3]
				- quantities[0] - quantities[4]
			) > QTY_TOLERANCE:
				quantity_invalid = True
		if quantity_invalid:
			errors.append({
				"code": "bom_result_quantity",
				"result": name,
				"message": f"BOM Result planned quantity {planned_qty:g} does not match pegged production {production_qty:g}.",
			})
	return {"valid": not errors, "checked": len(results), "errors": errors}


def _pegging_solution_fingerprint(row: dict[str, Any]) -> str:
	try:
		snapshot = json.loads(row.get("source_snapshot_json") or "{}")
	except (TypeError, ValueError):
		return ""
	return str(snapshot.get("solution_fingerprint") or "") if isinstance(snapshot, dict) else ""


def propagate_forecast_to_roots(run_name: str, forecast_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Propagate manufactured-child forecast completion to root customer Results."""
	if not frappe.db.exists("DocType", "APS BOM Pegging"):
		return []
	result_names = sorted({row.get("result") for row in forecast_rows if row.get("result")})
	bom_key_by_result = {
		row.name: row.bom_demand_key
		for row in frappe.get_all("APS Schedule Result", filters={"name": ("in", result_names)}, fields=["name", "bom_demand_key"], limit_page_length=0)
		if row.bom_demand_key
	} if result_names else {}
	completion_by_key = {}
	for row in forecast_rows:
		key = bom_key_by_result.get(row.get("result"))
		if key and row.get("forecast_end_time"):
			completion_by_key[key] = max(completion_by_key.get(key, get_datetime(row["forecast_end_time"])), get_datetime(row["forecast_end_time"]))
	peggings = frappe.get_all("APS BOM Pegging", filters={"planning_run": run_name, "child_demand_key": ("is", "set")}, fields=["name", "root_demand_key", "child_demand_key", "required_available_time"], limit_page_length=0)
	root_completion = {}
	for row in peggings:
		completion = completion_by_key.get(row.child_demand_key)
		if not completion:
			continue
		frappe.db.set_value("APS BOM Pegging", row.name, {"planned_available_time": completion, "status": "Late" if row.required_available_time and completion > get_datetime(row.required_available_time) else "Scheduled"}, update_modified=False)
		root_completion[row.root_demand_key] = max(root_completion.get(row.root_demand_key, completion), completion)
	impacts = []
	for root_key, completion in sorted(root_completion.items()):
		result_name = frappe.db.get_value("APS Schedule Result", {"planning_run": run_name, "demand_commitment": root_key}, "name") or (root_key if frappe.db.exists("APS Schedule Result", root_key) else None)
		if not result_name:
			continue
		result = frappe.get_doc("APS Schedule Result", result_name)
		late = bool(result.get("effective_due_time") and completion > get_datetime(result.effective_due_time))
		values = {
			"recovery_completion_time": completion,
			"risk_status": "Attention" if late and result.risk_status != "Critical" else result.risk_status,
		}
		if late:
			values["shortage_code"] = "BOM_CHILD_DELAY"
			values["solver_explanation"] = "Forecast risk propagated from a manufactured BOM child."
		elif result.get("shortage_code") == "BOM_CHILD_DELAY":
			values["shortage_code"] = None
		frappe.db.set_value("APS Schedule Result", result_name, values, update_modified=False)
		impacts.append({"root_demand_key": root_key, "result": result_name, "forecast_completion": completion, "late": late})
	return impacts


def get_bom_pegging_tree(run_name: str, *, root_demand_key: str | None = None) -> dict[str, Any]:
	filters: dict[str, Any] = {"planning_run": run_name, "status": ("!=", "Cancelled")}
	if root_demand_key:
		filters["root_demand_key"] = root_demand_key
	fields = [
		"name", "root_demand_key", "root_commitment", "parent_demand_key", "child_demand_key",
		"parent_result", "child_result", "parent_item", "component_item", "bom", "bom_fingerprint",
		"selection_source", "level", "qty_per_parent", "bom_output_qty", "component_uom", "stock_uom",
		"conversion_factor", "loss_percent", "batch_size", "batch_excess_qty", "required_gross_qty",
		"stock_covered_qty", "wip_covered_qty", "production_qty", "required_available_time",
		"planned_available_time", "is_raw_material_leaf", "status",
	]
	rows = [dict(row) for row in frappe.get_all("APS BOM Pegging", filters=filters, fields=fields, order_by="root_demand_key asc, level asc, parent_item asc, component_item asc, name asc", limit_page_length=0)]
	result_names = sorted({row.get("parent_result") for row in rows if row.get("parent_result")} | {row.get("child_result") for row in rows if row.get("child_result")})
	results = {
		row.name: dict(row)
		for row in frappe.get_all(
			"APS Schedule Result", filters={"name": ("in", result_names or [""])},
			fields=["name", "item_code", "customer", "demand_commitment", "bom_demand_key", "planned_qty", "produced_qty", "good_produced_qty", "actual_progress_qty", "actual_status", "risk_status", "effective_due_time", "recovery_completion_time"],
			limit_page_length=0,
		)
	}
	nodes = {}
	links = []
	for row in rows:
		root_key = row["root_demand_key"]
		parent_key = f"{root_key}|{row['parent_demand_key']}"
		child_identity = row.get("child_demand_key") or f"RAW:{row['component_item']}:{row['name']}"
		child_key = f"{root_key}|{child_identity}"
		parent_result = results.get(row.get("parent_result")) or {}
		child_result = results.get(row.get("child_result")) or {}
		nodes.setdefault(parent_key, {
			"key": parent_key, "demand_key": row["parent_demand_key"], "root_demand_key": root_key,
			"item_code": row["parent_item"], "result": row.get("parent_result"), "node_type": "Root" if row["parent_demand_key"] == root_key else "Manufactured",
			"planned_qty": parent_result.get("planned_qty"), "produced_qty": parent_result.get("good_produced_qty") or parent_result.get("produced_qty"),
			"actual_progress_qty": parent_result.get("actual_progress_qty"), "actual_status": parent_result.get("actual_status"),
			"risk_status": parent_result.get("risk_status"), "effective_due_time": parent_result.get("effective_due_time"),
		})
		nodes.setdefault(child_key, {
			"key": child_key, "demand_key": row.get("child_demand_key"), "root_demand_key": root_key,
			"item_code": row["component_item"], "result": row.get("child_result"),
			"node_type": "Raw Material Advisory" if row.get("is_raw_material_leaf") else "Manufactured",
			"planned_qty": child_result.get("planned_qty"), "produced_qty": child_result.get("good_produced_qty") or child_result.get("produced_qty"),
			"actual_progress_qty": child_result.get("actual_progress_qty"), "actual_status": child_result.get("actual_status"),
			"risk_status": child_result.get("risk_status"), "effective_due_time": child_result.get("effective_due_time"),
			"required_qty": row.get("required_gross_qty"), "stock_covered_qty": row.get("stock_covered_qty"),
			"wip_covered_qty": row.get("wip_covered_qty"), "production_qty": row.get("production_qty"),
		})
		links.append({
			"key": row["name"], "from": child_key, "to": parent_key, "root_demand_key": root_key,
			"bom": row.get("bom"), "level": row.get("level"), "required_qty": row.get("required_gross_qty"),
			"stock_covered_qty": row.get("stock_covered_qty"), "wip_covered_qty": row.get("wip_covered_qty"),
			"production_qty": row.get("production_qty"), "required_available_time": row.get("required_available_time"),
			"planned_available_time": row.get("planned_available_time"), "status": row.get("status"),
			"is_raw_material_leaf": row.get("is_raw_material_leaf"),
		})
	root_keys = sorted({row["root_demand_key"] for row in rows})
	return {
		"planning_run": run_name, "root_demand_keys": root_keys,
		"summary": {
			"root_count": len(root_keys), "node_count": len(nodes), "link_count": len(links),
			"required_qty": sum(flt(row.get("required_gross_qty")) for row in rows),
			"stock_covered_qty": sum(flt(row.get("stock_covered_qty")) for row in rows),
			"wip_covered_qty": sum(flt(row.get("wip_covered_qty")) for row in rows),
			"production_qty": sum(flt(row.get("production_qty")) for row in rows),
			"late_count": sum(1 for row in rows if row.get("status") == "Late"),
			"raw_material_advisory_count": sum(1 for row in rows if row.get("is_raw_material_leaf")),
		},
		"nodes": sorted(nodes.values(), key=lambda row: (row["root_demand_key"], row["node_type"], row["item_code"], row["key"])),
		"links": links, "rows": rows,
	}


def validate_run_precedence(run_name: str, *, raise_on_error: bool = True) -> dict[str, Any]:
	"""Validate live applied segments independently of both solver layers."""
	if not frappe.db.exists("DocType", "APS BOM Pegging"):
		return {"valid": True, "errors": []}
	rows = frappe.get_all(
		"APS BOM Pegging",
		filters={"planning_run": run_name, "status": ("not in", ["Cancelled", "Informational", "Covered"]), "child_result": ("is", "set"), "parent_result": ("is", "set")},
		fields=["name", "root_demand_key", "parent_result", "child_result", "parent_item", "component_item"],
		limit_page_length=0,
	)
	result_names = sorted({row.parent_result for row in rows} | {row.child_result for row in rows})
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": ("in", result_names or [""]), "parenttype": "APS Schedule Result", "segment_status": ("!=", "Cancelled")},
		fields=["parent", "start_time", "end_time", "segment_kind"],
		limit_page_length=0,
	)
	by_result = defaultdict(list)
	for segment in segments:
		if (segment.segment_kind or "Primary") != "Family Co-Product":
			by_result[segment.parent].append(segment)
	errors = []
	for row in rows:
		parent_start = min((get_datetime(segment.start_time) for segment in by_result[row.parent_result] if segment.start_time), default=None)
		child_end = max((get_datetime(segment.end_time) for segment in by_result[row.child_result] if segment.end_time), default=None)
		if parent_start and (not child_end or child_end > parent_start):
			errors.append({
				"code": "bom_precedence", "pegging": row.name, "root_demand_key": row.root_demand_key,
				"message": _("Parent item {0} starts before manufactured child {1} is available.", context="Injection APS").format(row.parent_item, row.component_item),
			})
	result = {"valid": not errors, "errors": errors, "checked": len(rows)}
	if errors and raise_on_error:
		frappe.throw("<br>".join(row["message"] for row in errors[:12]), frappe.ValidationError)
	return result


def preview_segment_precedence(
	run_name: str,
	result_name: str,
	segment_name: str,
	*,
	proposed_start,
	proposed_end,
) -> dict[str, Any]:
	"""Fail-closed dependency preview used by Gantt move/resize before mutation."""
	if not frappe.db.exists("DocType", "APS BOM Pegging"):
		return {"valid": True, "errors": []}
	rows = frappe.get_all(
		"APS BOM Pegging",
		filters={
			"planning_run": run_name, "status": ("not in", ["Cancelled", "Informational", "Covered"]),
			"parent_result": result_name,
		},
		fields=["name", "parent_result", "child_result", "parent_item", "component_item"],
		limit_page_length=0,
	)
	rows.extend(frappe.get_all(
		"APS BOM Pegging",
		filters={
			"planning_run": run_name, "status": ("not in", ["Cancelled", "Informational", "Covered"]),
			"child_result": result_name, "parent_result": ("!=", result_name),
		},
		fields=["name", "parent_result", "child_result", "parent_item", "component_item"],
		limit_page_length=0,
	))
	if not rows:
		return {"valid": True, "errors": []}
	result_names = sorted({row.parent_result for row in rows if row.parent_result} | {row.child_result for row in rows if row.child_result})
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": ("in", result_names), "parenttype": "APS Schedule Result", "segment_status": ("!=", "Cancelled")},
		fields=["name", "parent", "start_time", "end_time", "segment_kind"],
		limit_page_length=0,
	)
	by_result = defaultdict(list)
	for segment in segments:
		if (segment.segment_kind or "Primary") != "Family Co-Product" and segment.name != segment_name:
			by_result[segment.parent].append(segment)
	proposed_start = get_datetime(proposed_start)
	proposed_end = get_datetime(proposed_end)
	errors = []
	for row in rows:
		parent_starts = [get_datetime(segment.start_time) for segment in by_result[row.parent_result] if segment.start_time]
		child_ends = [get_datetime(segment.end_time) for segment in by_result[row.child_result] if segment.end_time]
		if row.parent_result == result_name:
			parent_starts.append(proposed_start)
		if row.child_result == result_name:
			child_ends.append(proposed_end)
		parent_start = min(parent_starts, default=None)
		child_end = max(child_ends, default=None)
		if parent_start and (not child_end or child_end > parent_start):
			errors.append({
				"code": "bom_precedence", "pegging": row.name,
				"message": _("BOM dependency blocks this change: child {0} must finish before parent {1} starts.", context="Injection APS").format(row.component_item, row.parent_item),
			})
	return {"valid": not errors, "errors": errors}


def _result_for_demand(run_name, demand_key, demand=None):
	if not demand_key:
		return None
	if demand and demand.result and frappe.db.exists("APS Schedule Result", demand.result):
		return demand.result
	return (
		frappe.db.get_value("APS Schedule Result", {"planning_run": run_name, "bom_demand_key": demand_key}, "name")
		or frappe.db.get_value("APS Schedule Result", {"planning_run": run_name, "demand_commitment": demand_key}, "name")
		or (demand_key if frappe.db.exists("APS Schedule Result", demand_key) else None)
	)


def _validate_master_cycles(root_items, bom_by_item, groups, producible_groups):
	visited = set()
	active = []
	def visit(item):
		if item in active:
			cycle = active[active.index(item):] + [item]
			raise BOMCycleError("BOM cycle: " + " -> ".join(cycle))
		if item in visited:
			return
		active.append(item)
		for row in (bom_by_item.get(item) or {}).get("components") or []:
			if groups.get(row["item_code"]) in producible_groups:
				visit(row["item_code"])
		active.pop(); visited.add(item)
	for item in root_items:
		visit(item)


def _load_master_graph(root_items, producible_groups, *, bom_selections=None, bom_policy="Default BOM Only"):
	bom_by_item = {}
	item_groups = {}
	batch = {}
	bom_selections = bom_selections or {}
	if bom_selections and bom_policy != "Explicit Approved Alternative":
		raise BOMInputError("Explicit BOM selections are not allowed by the current APS BOM Policy.")
	queue = deque(sorted(root_items))
	seen = set()
	while queue:
		item = queue.popleft()
		if item in seen:
			continue
		seen.add(item)
		item_row = frappe.db.get_value("Item", item, ["item_group", "min_order_qty", "default_bom", "stock_uom"], as_dict=True)
		if not item_row:
			raise BOMInputError(f"Item {item} was not found.")
		item_groups[item] = item_row.item_group
		batch[item] = flt(item_row.min_order_qty)
		selection = bom_selections.get(item) or {}
		bom_name = selection.get("bom") or item_row.default_bom or frappe.db.get_value("BOM", {"item": item, "is_default": 1, "is_active": 1, "docstatus": 1}, "name")
		if not bom_name:
			if item in root_items or item_row.item_group in producible_groups:
				raise BOMInputError(f"Manufactured item {item} has no active default BOM.")
			continue
		bom = frappe.db.get_value("BOM", bom_name, ["name", "item", "quantity", "modified", "is_active", "docstatus"], as_dict=True)
		if not bom or bom.item != item or not bom.is_active or int(bom.docstatus or 0) != 1:
			raise BOMInputError(f"BOM {bom_name} is not an active submitted BOM for manufactured item {item}.")
		if selection.get("bom_modified") and str(bom.modified) != str(selection.get("bom_modified")):
			raise BOMInputError(f"Approved BOM {bom_name} changed after approval; refresh and approve the BOM selection again.")
		meta = frappe.get_meta("BOM Item")
		fields = ["item_code", "qty", "stock_qty", "conversion_factor"]
		if meta.has_field("uom"):
			fields.append("uom")
		if meta.has_field("custom_loss_percent"):
			fields.append("custom_loss_percent")
		components = []
		for row in frappe.get_all("BOM Item", filters={"parent": bom_name, "parenttype": "BOM"}, fields=fields, order_by="idx asc", limit_page_length=0):
			component_item = row.item_code
			component = frappe.db.get_value("Item", component_item, ["item_group", "min_order_qty", "stock_uom"], as_dict=True)
			item_groups[component_item] = component.item_group if component else ""
			batch[component_item] = flt(component.min_order_qty) if component else 0
			qty = flt(row.stock_qty) or flt(row.qty) * max(flt(row.conversion_factor), 1)
			components.append({
				"item_code": component_item, "qty": qty, "loss_percent": flt(row.get("custom_loss_percent")),
				"component_uom": row.get("uom") or (component.stock_uom if component else ""),
				"stock_uom": component.stock_uom if component else "",
				"conversion_factor": flt(row.conversion_factor) or 1,
			})
			if item_groups[component_item] in producible_groups:
				queue.append(component_item)
		bom_snapshot = {
			"name": bom.name, "output_qty": flt(bom.quantity) or 1, "modified": str(bom.modified),
			"selection_source": "Explicit Approved Alternative" if selection else "Default BOM", "components": components,
		}
		bom_snapshot["fingerprint"] = fingerprint({
			"name": bom_snapshot["name"], "output_qty": bom_snapshot["output_qty"],
			"modified": bom_snapshot["modified"], "components": components,
		})
		bom_by_item[item] = bom_snapshot
	return bom_by_item, item_groups, batch


def _finished_or_semifinished_stock(company, items, *, exclude_run=None):
	if not items:
		return {}
	warehouses = _semifinished_warehouses(company)
	if not warehouses:
		return {}
	rows = frappe.db.sql(
		"""
		select b.item_code,
			coalesce(sum(greatest(
				ifnull(b.actual_qty, 0)
				- greatest(ifnull(b.reserved_qty, 0), ifnull(b.reserved_stock, 0))
				- ifnull(b.reserved_qty_for_production, 0)
				- ifnull(b.reserved_qty_for_sub_contract, 0)
				- ifnull(b.reserved_qty_for_production_plan, 0), 0
			)), 0) qty
		from `tabBin` b
		inner join `tabWarehouse` w on w.name=b.warehouse
		where w.company=%s and w.is_group=0 and w.disabled=0
			and b.warehouse in %s and b.item_code in %s
		group by b.item_code
		""",
		(company, tuple(warehouses), tuple(items)),
		as_dict=True,
	)
	result = {row.item_code: max(flt(row.qty), 0) for row in rows}
	if exclude_run and frappe.db.exists("DocType", "APS BOM Pegging") and frappe.get_meta("Work Order").has_field("custom_aps_result_reference"):
		claims = frappe.db.sql(
			"""
			select pegging.component_item as item_code, sum(ifnull(pegging.stock_covered_qty, 0)) as qty
			from `tabAPS BOM Pegging` pegging
			inner join `tabAPS Planning Run` run on run.name = pegging.planning_run
			left join `tabWork Order` wo
				on wo.custom_aps_result_reference = pegging.parent_result
				and wo.docstatus = 1
				and ifnull(wo.status, '') not in ('Stopped', 'Completed', 'Closed', 'Cancelled')
			where pegging.company = %(company)s
				and pegging.planning_run != %(exclude_run)s
				and pegging.component_item in %(items)s
				and pegging.status not in ('Cancelled', 'Completed')
				and run.status != 'Closed'
				and wo.name is null
			group by pegging.component_item
			""",
			{"company": company, "exclude_run": exclude_run, "items": tuple(items)},
			as_dict=True,
		)
		for row in claims:
			result[row.item_code] = max(flt(result.get(row.item_code)) - flt(row.qty), 0)
	return result


def _effective_wip(company, items):
	if not items:
		return {}
	fields = ["production_item", "qty", "produced_qty", "sales_order"]
	meta = frappe.get_meta("Work Order")
	for fieldname in ("custom_aps_result_reference", "custom_aps_run", "custom_aps_source"):
		if meta.has_field(fieldname):
			fields.append(fieldname)
	rows = frappe.get_all("Work Order", filters={"company": company, "production_item": ("in", list(items)), "docstatus": 1, "status": ("not in", ["Stopped", "Completed", "Closed", "Cancelled"])}, fields=fields, limit_page_length=0)
	result = defaultdict(float)
	for row in rows:
		# WIP already tied to a sales line, Result, or another APS Run is owned supply,
		# not a free semi-finished pool for this solver snapshot.
		if row.get("sales_order") or row.get("custom_aps_result_reference") or row.get("custom_aps_run"):
			continue
		result[row.production_item] += max(flt(row.qty) - flt(row.produced_qty), 0)
	return dict(result)


def _semifinished_warehouses(company: str) -> list[str]:
	warehouses = set(frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "disabled": 0, "warehouse_type": ("in", ["Finished Goods", "Work In Progress"])},
		pluck="name",
	))
	fieldname = frappe.db.get_single_value("APS Settings", "plant_floor_wip_warehouse_field")
	if fieldname and frappe.db.exists("DocType", "Plant Floor") and frappe.get_meta("Plant Floor").has_field(fieldname):
		configured = frappe.get_all("Plant Floor", filters={fieldname: ("is", "set")}, pluck=fieldname)
		if configured:
			warehouses.update(frappe.get_all(
				"Warehouse",
				filters={"name": ("in", list(set(configured))), "company": company, "is_group": 0, "disabled": 0},
				pluck="name",
			))
	return sorted(warehouses)


def _run_bom_selection_snapshot(run) -> dict[str, Any]:
	try:
		value = json.loads(run.get("bom_selection_json") or "{}")
	except (TypeError, ValueError):
		raise BOMInputError(f"Planning Run {run.name} has an invalid BOM selection snapshot.")
	if not isinstance(value, dict):
		raise BOMInputError(f"Planning Run {run.name} has an invalid BOM selection snapshot.")
	value.setdefault("version", 1)
	value.setdefault("policy", frappe.db.get_single_value("APS Settings", "aps_bom_policy") or "Default BOM Only")
	value.setdefault("selections", [])
	return value


def configured_producible_groups(settings=None):
	settings = settings or frappe.get_cached_doc("APS Settings")
	return {line.strip() for line in str(settings.get("aps_producible_item_groups") or "").splitlines() if line.strip()}


def current_bom_fingerprint(item_code: str, bom_name: str) -> str:
	masters, _groups, _batch = _load_master_graph(
		{item_code}, set(),
		bom_selections={item_code: {"bom": bom_name}},
		bom_policy="Explicit Approved Alternative",
	)
	return (masters.get(item_code) or {}).get("fingerprint") or ""


def require_bom_feature():
	settings = v2_flags.get_v2_settings()
	if not (settings["enable_aps_v2"] and settings["solver_engine"] == "CP-SAT" and settings["enable_multilevel_bom_planning"]):
		frappe.throw(_("Enable APS V2, CP-SAT, and Multilevel BOM Planning before expanding BOM demand.", context="Injection APS"), frappe.PermissionError)
