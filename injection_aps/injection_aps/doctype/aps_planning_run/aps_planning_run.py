from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime


class APSPlanningRun(Document):
	def validate(self):
		self.status = self.status or "Draft"
		self.approval_state = self.approval_state or "Pending"
		self.run_type = self.run_type or "Trial"
		self.horizon_days = cint(self.horizon_days or 14)
		self.planning_date = self.planning_date or getdate()

		if not self.horizon_start:
			self.horizon_start = get_datetime(now_datetime())
		if not self.horizon_end:
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
