from __future__ import annotations

from frappe.model.document import Document


class APSWorkOrderProposalBatch(Document):
	def validate(self):
		self.status = self.status or "Draft"
		self.approval_state = self.approval_state or "Pending"

