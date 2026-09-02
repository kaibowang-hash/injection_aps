from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime


USER_DERIVED_READ_ONLY_FIELDS = {
	"selected_plant_floor_summary",
	"horizon_start",
	"horizon_end",
	"demand_horizon_start_date",
	"demand_horizon_end_date",
	"freeze_horizon_end_date",
	"restricted_horizon_end_date",
	"recovery_horizon_start_date",
	"recovery_horizon_end_date",
	"due_time_policy",
}
NEW_RUN_ENGINE_DEFAULTS = {
	"status": "Draft",
	"approval_state": "Pending",
	"capacity_balance_status": "Not Analyzed",
	"solver_status": "Not Started",
	"consistency_status": "Unchecked",
}


class APSPlanningRun(Document):
	def validate(self):
		self._protect_new_run_submission()
		self.status = self.status or "Draft"
		self.approval_state = self.approval_state or "Pending"
		self.run_type = self.run_type or "Trial"
		self.horizon_days = cint(self.horizon_days or 14)
		self.planning_date = self.planning_date or getdate()

		if not self.horizon_start:
			self.horizon_start = get_datetime(now_datetime())
		from injection_aps.services.v2_flags import is_v2_enabled

		if is_v2_enabled():
			from injection_aps.services import horizon_status, planning

			for fieldname, value in horizon_status.planning_run_window_fields(
				self, planning.get_settings_dict()
			).items():
				self.set(fieldname, value)
			self.due_time_policy = self.due_time_policy or planning.get_settings_dict().get("due_time_policy")
		elif not self.horizon_end:
			self.horizon_end = get_datetime(add_days(self.horizon_start, self.horizon_days))
		if get_datetime(self.horizon_end) < get_datetime(self.horizon_start):
			frappe.throw(_("Horizon End cannot be earlier than Horizon Start."))
		if self.plant_floor and not any(row.get("plant_floor") == self.plant_floor for row in self.get("selected_plant_floors") or []):
			self.append("selected_plant_floors", {"plant_floor": self.plant_floor})
		plant_floors = []
		for row in self.get("selected_plant_floors") or []:
			plant_floor = row.get("plant_floor")
			if plant_floor and plant_floor not in plant_floors:
				plant_floors.append(plant_floor)
		if self.company and plant_floors and frappe.db.exists("DocType", "Plant Floor"):
			rows = frappe.get_all("Plant Floor", filters={"name": ("in", plant_floors)}, fields=["name", "company"])
			row_map = {row.name: row.company for row in rows}
			invalid = [row for row in plant_floors if row_map.get(row) not in ("", None, self.company)]
			if invalid:
				frappe.throw(_("Plant Floor {0} does not belong to company {1}.").format(", ".join(invalid), self.company))
		self._sync_selected_plant_floor_summary()
		self._protect_engine_managed_fields()

	def _protect_new_run_submission(self):
		if not self.is_new() or self.flags.get("aps_run_transition"):
			return
		injected = []
		for field in self.meta.fields:
			if not cint(field.read_only):
				continue
			value = self.get(field.fieldname)
			allowed_default = NEW_RUN_ENGINE_DEFAULTS.get(field.fieldname)
			if field.fieldname == "due_time_policy":
				allowed_default = field.get("default")
			if allowed_default is not None:
				if value not in (None, "", allowed_default):
					injected.append(field.fieldname)
			elif value not in (None, "", 0, 0.0):
				injected.append(field.fieldname)
		if injected:
			frappe.throw(
				_(
					"New Planning Runs must start without a precomputed planning window or engine results: {0}.",
					context="Injection APS",
				).format(", ".join(sorted(injected))),
				frappe.PermissionError,
			)

	def _protect_engine_managed_fields(self):
		if self.flags.get("aps_run_transition"):
			return
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			frappe.throw(
				_("Planning Run changes must use the controlled APS planning service."),
				frappe.PermissionError,
			)
		protected = {
			field.fieldname
			for field in self.meta.fields
			if cint(field.read_only) and field.fieldname not in USER_DERIVED_READ_ONLY_FIELDS
		}
		# Run type is chosen when a Run is created; changing Trial/Formal ownership
		# afterwards is an engine transition even though the creation field is editable.
		protected.add("run_type")
		if before.get("demand_baseline_fingerprint") or (before.get("status") or "Draft") != "Draft":
			protected.update(field.fieldname for field in self.meta.fields if field.fieldname != "notes")
		changed = sorted(
			fieldname
			for fieldname in protected
			if APSPlanningRun._guard_value(self, fieldname)
			!= APSPlanningRun._guard_value(self, fieldname, before)
		)
		if changed:
			frappe.throw(
				_(
					"Planning Run engine and approval fields are maintained by controlled APS actions: {0}.",
					context="Injection APS",
				).format(", ".join(changed)),
				frappe.PermissionError,
			)

	def _guard_value(self, fieldname, doc=None):
		doc = self if doc is None else doc
		if fieldname == "selected_plant_floors":
			return tuple(row.get("plant_floor") for row in doc.get(fieldname) or [])
		return doc.get(fieldname)

	def _sync_selected_plant_floor_summary(self):
		rows = []
		for row in self.get("selected_plant_floors") or []:
			plant_floor = row.get("plant_floor")
			if plant_floor and plant_floor not in rows:
				rows.append(plant_floor)
		if self.plant_floor and self.plant_floor not in rows:
			rows.insert(0, self.plant_floor)
		if self.plant_floor and not rows:
			rows = [self.plant_floor]
		self.selected_plant_floor_summary = ", ".join(rows)
