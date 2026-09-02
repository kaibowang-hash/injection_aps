from __future__ import annotations

import unittest

import frappe
from frappe.utils import getdate


class TestPhase3InstalledContracts(unittest.TestCase):
	def setUp(self):
		self.original_v2 = frappe.db.get_single_value("APS Settings", "enable_aps_v2") or 0
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", 1)
		frappe.clear_cache(doctype="APS Settings")

	def tearDown(self):
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", self.original_v2)
		frappe.clear_cache(doctype="APS Settings")
		frappe.db.rollback()

	def test_phase3_schema_is_installed(self):
		self.assertTrue(frappe.db.exists("DocType", "APS Constraint Resolution"))
		for fieldname in (
			"demand_horizon_end_date", "freeze_horizon_end_date",
			"restricted_horizon_end_date", "recovery_horizon_end_date",
		):
			self.assertTrue(frappe.get_meta("APS Planning Run").has_field(fieldname))

	def test_v2_run_validation_uses_inclusive_demand_dates_and_separate_recovery(self):
		run = frappe.get_doc({
			"doctype": "APS Planning Run", "company": "Fixture Company",
			"horizon_days": 14, "freeze_horizon_days": 2,
			"restricted_horizon_days": 7, "recovery_horizon_days": 5,
			"horizon_start": "2026-08-02 11:30:00",
		})
		run.flags.aps_run_transition = True
		run.validate()
		self.assertEqual(getdate(run.demand_horizon_end_date), getdate("2026-08-15"))
		self.assertEqual(getdate(run.recovery_horizon_start_date), getdate("2026-08-16"))
		self.assertEqual(getdate(run.recovery_horizon_end_date), getdate("2026-08-20"))

	def test_phase3_defaults_are_initialized_without_enabling_v2(self):
		self.assertEqual(frappe.db.get_single_value("APS Settings", "default_freeze_horizon_days"), 2)
		self.assertEqual(frappe.db.get_single_value("APS Settings", "default_restricted_horizon_days"), 7)
		self.assertEqual(frappe.db.get_single_value("APS Settings", "default_recovery_horizon_days"), 7)


if __name__ == "__main__":
	unittest.main()
