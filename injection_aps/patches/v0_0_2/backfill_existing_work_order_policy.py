from __future__ import annotations

import frappe


def execute():
	if frappe.db.exists("DocType", "APS Net Requirement") and frappe.get_meta("APS Net Requirement").has_field(
		"existing_work_order_policy"
	):
		frappe.db.sql(
			"""
			update `tabAPS Net Requirement`
			set existing_work_order_policy = 'Include'
			where ifnull(existing_work_order_policy, '') = ''
				and ifnull(is_system_generated, 0) = 1
			"""
		)

	if frappe.db.exists("DocType", "APS Planning Run") and frappe.get_meta("APS Planning Run").has_field(
		"existing_work_order_policy"
	):
		frappe.db.sql(
			"""
			update `tabAPS Planning Run` run
			set run.existing_work_order_policy = 'Include'
			where ifnull(run.existing_work_order_policy, '') = ''
				and (
					ifnull(run.status, 'Draft') != 'Draft'
					or exists (
						select result.name
						from `tabAPS Schedule Result` result
						where result.planning_run = run.name
					)
				)
			"""
		)
