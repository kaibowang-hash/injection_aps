from __future__ import annotations

import unittest

from injection_aps.tests.v2_phase0_gate import validate_isolated_environment


SAFE_CONFIG = {
	"aps_v2_isolated_environment": 1,
	"aps_v2_fixture_environment": 1,
	"pause_scheduler": 1,
	"mute_emails": 1,
	"disable_async": 1,
}


class TestV2Phase0Gate(unittest.TestCase):
	def test_accepts_only_explicit_isolated_fixture_environment(self):
		errors = validate_isolated_environment(
			site="aps-opt-fixture.localhost",
			db_name="aps_v2_fixture_260814",
			db_host="127.0.0.1",
			db_port=13306,
			config=SAFE_CONFIG,
			require_fixture=True,
		)
		self.assertEqual(errors, [])

	def test_rejects_production_identity_even_if_flags_are_spoofed(self):
		errors = validate_isolated_environment(
			site="jce.1",
			db_name="_f486adafcc83f356",
			db_host="127.0.0.1",
			db_port=3306,
			config=SAFE_CONFIG,
			require_fixture=False,
		)
		self.assertIn("production site is forbidden", errors)
		self.assertIn("production database port is forbidden", errors)
		self.assertIn("database name is outside the APS V2 isolated namespace", errors)

	def test_rejects_missing_side_effect_guards(self):
		errors = validate_isolated_environment(
			site="aps-opt-test.localhost",
			db_name="aps_v2_t_260814",
			db_host="127.0.0.1",
			db_port=13306,
			config={},
			require_fixture=False,
		)
		self.assertIn("pause_scheduler must be enabled", errors)
		self.assertIn("mute_emails must be enabled", errors)
		self.assertIn("disable_async must be enabled", errors)


if __name__ == "__main__":
	unittest.main()
