from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from injection_aps.patches.v0_0_2 import initialize_aps_v2_phase0_defaults
from injection_aps.services.v2_flags import DEFAULTS


class TestV2Phase0DefaultsPatch(unittest.TestCase):
	def test_patch_forces_flags_off_and_preserves_existing_numeric_settings(self):
		database = MagicMock()
		database.exists.return_value = True
		database.get_single_value.side_effect = lambda _doctype, fieldname: {
			"enable_aps_v2": 1,
			"solver_engine": "CP-SAT",
			"enable_shift_replan": 1,
			"enable_coproduct_campaign": 1,
			"enable_multilevel_bom_planning": 1,
			"delivery_legacy_match_tolerance_days": 7,
			"max_execution_staleness_minutes": 45,
			"solver_time_limit_seconds": 240,
			"shift_solver_time_limit_seconds": 60,
		}[fieldname]
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(initialize_aps_v2_phase0_defaults.frappe, "db", database),
			patch.object(initialize_aps_v2_phase0_defaults.frappe, "get_meta", return_value=meta),
		):
			initialize_aps_v2_phase0_defaults.execute()

		writes = {call.args[1]: call.args[2] for call in database.set_single_value.call_args_list}
		self.assertEqual(writes["enable_aps_v2"], 0)
		self.assertEqual(writes["solver_engine"], "Legacy")
		self.assertEqual(writes["enable_shift_replan"], 0)
		self.assertEqual(writes["enable_coproduct_campaign"], 0)
		self.assertEqual(writes["enable_multilevel_bom_planning"], 0)
		self.assertNotIn("solver_time_limit_seconds", writes)
		self.assertNotIn("delivery_legacy_match_tolerance_days", writes)

	def test_patch_populates_missing_numeric_defaults(self):
		database = MagicMock()
		database.exists.return_value = True
		database.get_single_value.return_value = None
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(initialize_aps_v2_phase0_defaults.frappe, "db", database),
			patch.object(initialize_aps_v2_phase0_defaults.frappe, "get_meta", return_value=meta),
		):
			initialize_aps_v2_phase0_defaults.execute()

		writes = {call.args[1]: call.args[2] for call in database.set_single_value.call_args_list}
		self.assertEqual(writes, dict(DEFAULTS))

	def test_patch_replaces_zero_numeric_values_with_contract_defaults(self):
		database = MagicMock()
		database.exists.return_value = True
		database.get_single_value.side_effect = lambda _doctype, fieldname: (
			"Legacy" if fieldname == "solver_engine" else 0
		)
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(initialize_aps_v2_phase0_defaults.frappe, "db", database),
			patch.object(initialize_aps_v2_phase0_defaults.frappe, "get_meta", return_value=meta),
		):
			initialize_aps_v2_phase0_defaults.execute()

		writes = {call.args[1]: call.args[2] for call in database.set_single_value.call_args_list}
		self.assertEqual(writes["delivery_legacy_match_tolerance_days"], 3)
		self.assertEqual(writes["max_execution_staleness_minutes"], 30)
		self.assertEqual(writes["solver_time_limit_seconds"], 120)
		self.assertEqual(writes["shift_solver_time_limit_seconds"], 30)


if __name__ == "__main__":
	unittest.main()
