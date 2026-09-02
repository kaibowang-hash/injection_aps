from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.injection_aps.doctype.aps_planning_run import aps_planning_run
from injection_aps.injection_aps.doctype.aps_planning_run.aps_planning_run import APSPlanningRun


class TestPlanningRunEngineGuard(unittest.TestCase):
	def setUp(self):
		translation = patch.object(aps_planning_run, "_", side_effect=lambda message, **_: message)
		throw = patch.object(aps_planning_run.frappe, "throw", side_effect=self._raise_frappe_error)
		translation.start()
		throw.start()
		self.addCleanup(translation.stop)
		self.addCleanup(throw.stop)

	@staticmethod
	def _raise_frappe_error(message, exc=frappe.ValidationError, **_):
		raise exc(message)

	def _run(
		self, current, previous=None, *, fields=(), read_only_fields=(), read_only_defaults=None,
		flags=None, is_new=False,
	):
		doc = MagicMock()
		doc.is_new.return_value = is_new
		doc.flags = frappe._dict(flags or {})
		doc.meta = frappe._dict(
			fields=[
				frappe._dict(
					fieldname=fieldname,
					read_only=int(fieldname in read_only_fields),
					default=(read_only_defaults or {}).get(fieldname),
				)
				for fieldname in dict.fromkeys((*fields, *read_only_fields))
			]
		)
		doc.get.side_effect = current.get
		doc.get_doc_before_save.return_value = frappe._dict(previous or {})
		return doc

	def test_new_run_rejects_injected_state_and_engine_results(self):
		doc = self._run(
			{
				"status": "Applied",
				"approval_state": "Approved",
				"demand_baseline_fingerprint": "forged",
				"total_scheduled_qty": 999,
			},
			read_only_fields=(
				"status", "approval_state", "demand_baseline_fingerprint", "total_scheduled_qty",
			),
			is_new=True,
		)
		with self.assertRaises(frappe.PermissionError):
			APSPlanningRun._protect_new_run_submission(doc)

	def test_new_run_rejects_injected_horizon_and_nondefault_due_policy(self):
		doc = self._run(
			{
				"horizon_start": "2030-01-01 00:00:00",
				"due_time_policy": "Injected Policy",
			},
			read_only_fields=("horizon_start", "due_time_policy"),
			read_only_defaults={"due_time_policy": "Delivery Date End Of Day"},
			is_new=True,
		)
		with self.assertRaises(frappe.PermissionError):
			APSPlanningRun._protect_new_run_submission(doc)

	def test_new_run_allows_only_empty_engine_fields_and_normal_defaults(self):
		doc = self._run(
			{
				"status": "Draft",
				"approval_state": "Pending",
				"capacity_balance_status": "Not Analyzed",
				"solver_status": "Not Started",
				"consistency_status": "Unchecked",
				"demand_baseline_fingerprint": None,
				"total_scheduled_qty": 0,
				"horizon_end": None,
				"due_time_policy": "Delivery Date End Of Day",
			},
			read_only_fields=(
				"status", "approval_state", "capacity_balance_status", "solver_status",
				"consistency_status", "demand_baseline_fingerprint", "total_scheduled_qty",
				"horizon_end", "due_time_policy",
			),
			read_only_defaults={"due_time_policy": "Delivery Date End Of Day"},
			is_new=True,
		)
		APSPlanningRun._protect_new_run_submission(doc)

	def test_controlled_service_flag_allows_initialized_new_run(self):
		doc = self._run(
			{"status": "Applied", "demand_baseline_fingerprint": "engine"},
			read_only_fields=("status", "demand_baseline_fingerprint"),
			flags={"aps_run_transition": True},
			is_new=True,
		)
		APSPlanningRun._protect_new_run_submission(doc)

	def test_direct_engine_state_change_is_blocked(self):
		doc = self._run(
			{"status": "Applied", "approval_state": "Approved", "run_type": "Trial"},
			{"status": "Draft", "approval_state": "Pending", "run_type": "Trial"},
			read_only_fields=("status", "approval_state"),
		)
		with self.assertRaises(frappe.PermissionError):
			APSPlanningRun._protect_engine_managed_fields(doc)

	def test_existing_run_type_change_is_blocked(self):
		doc = self._run(
			{"run_type": "Formal"},
			{"run_type": "Trial"},
		)
		with self.assertRaises(frappe.PermissionError):
			APSPlanningRun._protect_engine_managed_fields(doc)

	def test_ignore_permissions_does_not_bypass_existing_run_guard(self):
		doc = self._run(
			{"status": "Applied"},
			{"status": "Draft"},
			read_only_fields=("status",),
			flags={"ignore_permissions": True},
		)
		with self.assertRaises(frappe.PermissionError):
			APSPlanningRun._protect_engine_managed_fields(doc)

	def test_user_input_and_derived_horizon_changes_remain_allowed(self):
		doc = self._run(
			{
				"status": "Draft",
				"company": "New Company",
				"horizon_end": "2026-09-30",
				"notes": "updated",
			},
			{
				"status": "Draft",
				"company": "Old Company",
				"horizon_end": "2026-09-15",
				"notes": "old",
			},
			fields=("company",),
			read_only_fields=("status", "horizon_end"),
		)
		APSPlanningRun._protect_engine_managed_fields(doc)

	def test_baseline_freezes_inputs_and_selected_plant_floors(self):
		doc = self._run(
			{
				"status": "Draft",
				"demand_baseline_fingerprint": "baseline-1",
				"selected_plant_floors": [{"plant_floor": "Floor B"}],
			},
			{
				"status": "Draft",
				"demand_baseline_fingerprint": "baseline-1",
				"selected_plant_floors": [{"plant_floor": "Floor A"}],
			},
			fields=("selected_plant_floors",),
			read_only_fields=("status", "demand_baseline_fingerprint"),
		)
		with self.assertRaises(frappe.PermissionError):
			APSPlanningRun._protect_engine_managed_fields(doc)

	def test_non_draft_run_allows_notes_only(self):
		doc = self._run(
			{"status": "Planned", "horizon_days": 30, "notes": "updated"},
			{"status": "Planned", "horizon_days": 14, "notes": "old"},
			fields=("horizon_days", "notes"),
			read_only_fields=("status",),
		)
		with self.assertRaises(frappe.PermissionError):
			APSPlanningRun._protect_engine_managed_fields(doc)

	def test_non_draft_run_note_change_remains_allowed(self):
		doc = self._run(
			{"status": "Planned", "horizon_days": 14, "notes": "updated"},
			{"status": "Planned", "horizon_days": 14, "notes": "old"},
			fields=("horizon_days", "notes"),
			read_only_fields=("status",),
		)
		APSPlanningRun._protect_engine_managed_fields(doc)

	def test_controlled_service_flag_allows_engine_transition(self):
		doc = self._run(
			{"status": "Planned", "run_type": "Trial"},
			{"status": "Draft", "run_type": "Trial"},
			read_only_fields=("status",),
			flags={"aps_run_transition": True},
		)
		APSPlanningRun._protect_engine_managed_fields(doc)


if __name__ == "__main__":
	unittest.main()
