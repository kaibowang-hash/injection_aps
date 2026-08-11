from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class APSWorkOrderProposalBatch(Document):
	def validate(self):
		self._protect_system_review_statuses()
		items = list(self.get("items") or [])
		statuses = [row.review_status for row in items if row.review_status]
		self.proposal_count = len(items)
		self.applied_count = sum(1 for status in statuses if status == "Applied")
		skipped_count = sum(1 for status in statuses if status == "Skipped")
		if not items or not statuses or all(status == "Pending" for status in statuses):
			self.status = "Ready For Review" if items else (self.status or "Draft")
			self.approval_state = "Pending"
			return
		if self.applied_count:
			self.status = "Applied"
			self.approval_state = "Approved"
			return
		if skipped_count:
			self.status = "Reviewed"
			self.approval_state = "Rejected" if all(status in ("Rejected", "Skipped") for status in statuses) else "Approved"
			return
		if any(status == "Pending" for status in statuses):
			self.status = "Partially Reviewed"
			self.approval_state = "Pending"
			return
		self.status = "Reviewed"
		self.approval_state = "Rejected" if all(status == "Rejected" for status in statuses) else "Approved"

	def on_trash(self):
		if self.flags.get("allow_proposal_engine_delete") or getattr(frappe.flags, "in_uninstall", False):
			return
		self._protect_system_review_statuses()

	def _protect_system_review_statuses(self):
		if self.flags.get("proposal_engine_transition"):
			return
		# The batch and every child value are executable instructions, not user
		# master data.  Read-only field metadata is only a UI hint and can be
		# bypassed through REST, so all inserts/transitions must carry the private
		# service flag set by generation, review, reject, or Apply.
		frappe.throw(
			_(
				"Work Order proposal batches are maintained by APS review and release actions.",
				context="Injection APS",
			),
			frappe.PermissionError,
		)
