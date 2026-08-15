from __future__ import annotations

import frappe


INDEXES = {
	"Customer Delivery Schedule Item": [
		("iaps_p8_sched_identity_date", ["demand_identity", "effective_schedule_date"]),
	],
	"APS Demand Commitment": [
		("iaps_p8_commit_owner", ["demand_identity", "formal_owner", "owner_state", "status"]),
		("iaps_p8_commit_run_identity", ["planning_run", "demand_identity"]),
	],
	"APS Schedule Result": [
		("iaps_p8_result_commit_run", ["demand_commitment", "planning_run"]),
	],
	"APS Stock Coverage Allocation": [
		("iaps_p8_stock_identity_status", ["demand_identity", "status", "owner_run"]),
	],
	"APS Delivery Allocation": [
		("iaps_p8_delivery_identity", ["demand_identity", "is_effective", "source_posting_time"]),
	],
}


def execute() -> None:
	"""Add only query indexes; Progress V2 derives facts and performs no backfill."""
	for doctype, indexes in INDEXES.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		table = f"tab{doctype}"
		columns = {row.Field for row in frappe.db.sql(f"show columns from `{table}`", as_dict=True)}
		for index_name, fields in indexes:
			if all(field in columns for field in fields):
				frappe.db.add_index(doctype, fields, index_name)
