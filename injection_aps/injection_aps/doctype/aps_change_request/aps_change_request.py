from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


CHANGE_TYPE_ALIASES = {
	"Insert Order": "Urgent Order",
	"Advance": "Pull In",
	"Delay": "Push Out",
}
CHANGE_TYPES = (
	"Increase Qty",
	"Decrease Qty",
	"Cancel",
	"Pull In",
	"Push Out",
	"Urgent Order",
	"Machine Exception",
)
WORKFLOW_STATUSES = (
	"Draft",
	"Analyzed",
	"PMC Confirmed",
	"Approved",
	"Applied",
	"Rejected",
	"Cancelled",
)
REQUEST_FIELDS = (
	"planning_run",
	"company",
	"plant_floor",
	"source_demand_delta",
	"change_type",
	"target_result",
	"item_code",
	"customer",
	"required_date",
	"qty",
	"target_planned_qty",
	"machine_exception_mode",
	"workstation",
	"exception_start_time",
	"exception_end_time",
	"available_capacity_percent",
	"retained_disposition",
	"notes",
)
ENGINE_FIELDS = (
	"status",
	"approval_state",
	"current_required_date",
	"current_planned_qty",
	"current_machine_scheduled_qty",
	"delivered_qty",
	"produced_qty",
	"started_locked_qty",
	"frozen_qty",
	"minimum_retained_qty",
	"cancellable_qty",
	"retained_excess_qty",
	"analysis_revision",
	"analyzed_by",
	"analyzed_on",
	"pmc_confirmed_by",
	"pmc_confirmed_on",
	"approved_by",
	"approved_on",
	"applied_by",
	"applied_on",
	"impact_summary",
	"analysis_fingerprint",
	"source_snapshot_hash",
	"application_fingerprint",
	"application_log",
	"apply_count",
	"impact_json",
	"proposal_json",
	"before_snapshot_json",
	"after_snapshot_json",
	"application_result_json",
	"downtime_window",
)


def normalize_change_type(value: str | None) -> str:
	return CHANGE_TYPE_ALIASES.get(value or "", value or "")


class APSChangeRequest(Document):
	def validate(self):
		self.status = self.status or "Draft"
		self.approval_state = self.approval_state or "Pending"
		self.change_type = normalize_change_type(self.change_type)
		self.machine_exception_mode = self.machine_exception_mode or "Downtime"
		self.retained_disposition = self.retained_disposition or "Pending Negotiation"
		self.available_capacity_percent = flt(self.available_capacity_percent)

		if self.change_type and self.change_type not in CHANGE_TYPES:
			frappe.throw(_("Unsupported APS change type: {0}.").format(self.change_type), frappe.ValidationError)
		if self.status not in WORKFLOW_STATUSES:
			frappe.throw(_("Unsupported APS change-request status: {0}.").format(self.status), frappe.ValidationError)
		if self.available_capacity_percent < 0 or self.available_capacity_percent > 100:
			frappe.throw(_("Available Capacity Percent must be between 0 and 100."), frappe.ValidationError)

		self._validate_scope_links()
		self._protect_engine_fields()

	def on_trash(self):
		if self.status == "Applied" and not self.flags.get("allow_change_engine_delete"):
			frappe.throw(_("An applied APS Change Request cannot be deleted."), frappe.ValidationError)

	def _validate_scope_links(self):
		if self.planning_run:
			run_company = frappe.db.get_value("APS Planning Run", self.planning_run, "company")
			if run_company and self.company and run_company != self.company:
				frappe.throw(_("Planning Run {0} belongs to company {1}.").format(self.planning_run, run_company))
			if run_company and not self.company:
				self.company = run_company
		if self.target_result:
			result_scope = frappe.db.get_value(
				"APS Schedule Result",
				self.target_result,
				["planning_run", "company"],
				as_dict=True,
			)
			if not result_scope:
				frappe.throw(_("Target Schedule Result {0} was not found.").format(self.target_result))
			if self.planning_run and result_scope.planning_run != self.planning_run:
				frappe.throw(_("Target Schedule Result must belong to Planning Run {0}.").format(self.planning_run))
			if self.company and result_scope.company and result_scope.company != self.company:
				frappe.throw(_("Target Schedule Result must belong to company {0}.").format(self.company))

	def _protect_engine_fields(self):
		if self.flags.get("change_engine_transition"):
			return
		before = self.get_doc_before_save()
		if self.is_new():
			if self.status != "Draft" or self.approval_state != "Pending":
				frappe.throw(_("New APS Change Requests must start in Draft / Pending."), frappe.ValidationError)
			if any(self.get(fieldname) not in (None, "", 0, 0.0) for fieldname in ENGINE_FIELDS if fieldname not in ("status", "approval_state")):
				frappe.throw(_("Workflow and audit fields are maintained by the APS change engine."), frappe.ValidationError)
			return
		if not before:
			return
		changed_engine_fields = [fieldname for fieldname in ENGINE_FIELDS if self.get(fieldname) != before.get(fieldname)]
		if changed_engine_fields:
			frappe.throw(
				_("Workflow and audit fields can only be changed by the APS change engine: {0}.").format(
					", ".join(changed_engine_fields)
				),
				frappe.ValidationError,
			)
		if before.status != "Draft":
			changed_request_fields = [fieldname for fieldname in REQUEST_FIELDS if self.get(fieldname) != before.get(fieldname)]
			if changed_request_fields:
				frappe.throw(
					_("Analyzed APS Change Requests are immutable. Cancel this request and create a new revision: {0}.").format(
						", ".join(changed_request_fields)
					),
					frappe.ValidationError,
				)
