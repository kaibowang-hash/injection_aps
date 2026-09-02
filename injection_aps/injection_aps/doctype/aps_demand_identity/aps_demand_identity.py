from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


IMMUTABLE_FIELDS = (
	"company",
	"customer",
	"schedule_scope",
	"item_code",
	"external_line_reference",
)


class APSDemandIdentity(Document):
	def validate(self):
		self.schedule_scope = str(self.schedule_scope or "").strip()
		self.customer_part_no = str(self.customer_part_no or "").strip()
		self.external_line_reference = str(self.external_line_reference or "").strip()
		self.status = self.status or "Active"
		self.first_seen_on = self.first_seen_on or now_datetime()
		self.last_revised_on = self.last_revised_on or self.first_seen_on
		self.external_identity_key = _build_external_identity_key(self)
		self._validate_immutable_identity()

	def _validate_immutable_identity(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		changed = [fieldname for fieldname in IMMUTABLE_FIELDS if (before.get(fieldname) or "") != (self.get(fieldname) or "")]
		if changed:
			frappe.throw(
				_("Demand Identity business keys are immutable: {0}.").format(", ".join(changed)),
				frappe.ValidationError,
			)


def _build_external_identity_key(doc) -> str | None:
	external_reference = str(doc.get("external_line_reference") or "").strip()
	if not external_reference:
		return None
	payload = "|".join(
		str(doc.get(fieldname) or "").strip()
		for fieldname in ("company", "customer", "schedule_scope", "item_code", "external_line_reference")
	)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()
