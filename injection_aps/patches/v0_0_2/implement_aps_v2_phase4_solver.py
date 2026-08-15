from __future__ import annotations

import frappe


def execute() -> None:
	"""Backfill read-only solver mirrors without changing legacy ownership or UI."""
	if frappe.db.exists("DocType", "APS Planning Run"):
		meta = frappe.get_meta("APS Planning Run")
		if meta.has_field("solver_status"):
			frappe.db.sql(
				"""
				update `tabAPS Planning Run`
				set solver_status = 'Not Started'
				where ifnull(solver_status, '') = ''
				"""
			)
		if meta.has_field("solver_engine_used"):
			frappe.db.sql(
				"""
				update `tabAPS Planning Run`
				set solver_engine_used = 'Legacy'
				where ifnull(solver_engine_used, '') = ''
				"""
			)
	if frappe.db.exists("DocType", "APS Schedule Segment"):
		meta = frappe.get_meta("APS Schedule Segment")
		for target, source in (
			("baseline_start_time", "start_time"),
			("baseline_end_time", "end_time"),
			("current_start_time", "start_time"),
			("current_end_time", "end_time"),
		):
			if meta.has_field(target):
				frappe.db.sql(
					f"update `tabAPS Schedule Segment` set `{target}` = `{source}` where `{target}` is null and `{source}` is not null"
				)
