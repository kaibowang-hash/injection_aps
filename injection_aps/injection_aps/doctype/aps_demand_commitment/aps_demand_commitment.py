from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, now_datetime


ACTIVE_OWNER_STATUSES = {"Draft", "Proposed", "Approved", "Released", "In Progress"}
QTY_TOLERANCE = 0.000001


class APSDemandCommitment(Document):
	def validate(self):
		self.admission_class = self.admission_class or "P0"
		self.owner_state = self.owner_state or "Owned"
		self.execution_state = self.execution_state or "Reschedulable"
		self.status = self.status or "Draft"
		for fieldname in (
			"requested_qty", "stock_covered_qty", "carried_qty", "newly_planned_qty",
			"on_time_qty", "late_qty", "unscheduled_qty", "produced_qty", "delivered_qty",
			"remaining_qty", "executed_floor_qty", "excess_qty",
		):
			if flt(self.get(fieldname)) < -QTY_TOLERANCE:
				frappe.throw(_("Commitment quantity {0} cannot be negative.").format(fieldname), frappe.ValidationError)
		self._validate_scope()
		self._set_active_owner_key()
		self.transitioned_on = self.transitioned_on or now_datetime()

	def on_update(self):
		self._protect_engine_managed_record()

	def on_trash(self):
		self._protect_engine_managed_record()

	def _validate_scope(self):
		if self.admission_class == "P0" and not self.demand_identity:
			frappe.throw(_("P0 Commitment requires a Demand Identity."), frappe.ValidationError)
		if self.demand_identity:
			identity = frappe.db.get_value(
				"APS Demand Identity", self.demand_identity, ["company", "customer", "item_code"], as_dict=True
			) or {}
			if not identity or any(
				(self.get(fieldname) or "") != (identity.get(fieldname) or "")
				for fieldname in ("company", "customer", "item_code")
			):
				frappe.throw(_("Commitment scope does not match its Demand Identity."), frappe.ValidationError)

	def _set_active_owner_key(self):
		is_active_owner = bool(
			self.demand_identity
			and self.owner_state == "Owned"
			and self.status in ACTIVE_OWNER_STATUSES
			and cint(self.formal_owner)
		)
		self.active_owner_key = (
			hashlib.sha256(f"{self.company}|{self.demand_identity}".encode()).hexdigest()
			if is_active_owner else None
		)

	def _protect_engine_managed_record(self):
		if self.flags.get("ignore_permissions") or self.flags.get("aps_phase2_transition"):
			return
		frappe.throw(_("Demand Commitments are maintained by the controlled APS demand ledger."), frappe.PermissionError)
