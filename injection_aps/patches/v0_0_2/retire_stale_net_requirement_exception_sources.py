from __future__ import annotations

import frappe

from injection_aps.services import planning


def execute():
	"""Detach historical exception links from deleted or recycled NR names."""
	if not all(
		frappe.db.exists("DocType", doctype)
		for doctype in ("APS Exception Log", "APS Net Requirement", "APS Schedule Result")
	):
		return

	# APS-NET uses a reversible naming series, so existence alone is insufficient:
	# an old exception can silently resolve to a newer, unrelated requirement with
	# the same name.  A valid source must predate its exception and be claimed by a
	# Result from that same Planning Run.
	stale_names = frappe.db.sql(
		"""
		select distinct exception_log.name
		from `tabAPS Exception Log` exception_log
		left join `tabAPS Net Requirement` net_requirement
			on net_requirement.name = exception_log.source_name
		where exception_log.source_doctype = 'APS Net Requirement'
			and ifnull(exception_log.source_name, '') != ''
			and (
				net_requirement.name is null
				or exception_log.creation < net_requirement.creation
				or not exists (
					select 1
					from `tabAPS Schedule Result` schedule_result
					where schedule_result.planning_run = exception_log.planning_run
						and schedule_result.net_requirement = exception_log.source_name
				)
			)
		order by exception_log.name
		""",
		pluck=True,
	)
	if not stale_names:
		return
	planning._retire_net_requirement_exception_sources(
		exception_names=stale_names,
		reason="Historical APS Net Requirement source retired during upgrade",
	)
