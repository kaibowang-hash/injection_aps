from __future__ import annotations

from frappe.model.document import Document


class APSWorkOrderProposalBatch(Document):
	def validate(self):
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
