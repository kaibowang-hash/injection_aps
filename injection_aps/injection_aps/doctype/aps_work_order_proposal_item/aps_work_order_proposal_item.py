from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class APSWorkOrderProposalItem(Document):
	def validate(self):
		self._block_direct_mutation()

	def on_trash(self):
		self._block_direct_mutation()

	def _block_direct_mutation(self):
		if self.flags.get("proposal_engine_transition"):
			return
		frappe.throw(
			_(
				"Work Order proposal batches are maintained by APS review and release actions.",
				context="Injection APS",
			),
			frappe.PermissionError,
		)
