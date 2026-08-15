from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestPhase6Contracts(unittest.TestCase):
	def test_campaign_schema_has_one_owner_and_explicit_outputs(self):
		campaign = json.loads((ROOT / "injection_aps/doctype/aps_production_campaign/aps_production_campaign.json").read_text())
		fields = {row["fieldname"] for row in campaign["fields"]}
		self.assertTrue({"planned_cycles", "capacity_owner_segment", "outputs", "input_fingerprint"} <= fields)
		output = json.loads((ROOT / "injection_aps/doctype/aps_campaign_output/aps_campaign_output.json").read_text())
		output_fields = {row["fieldname"] for row in output["fields"]}
		self.assertTrue({"output_per_cycle", "planned_qty", "demand_covered_qty", "excess_qty", "work_order", "actual_good_qty", "actual_scrap_qty"} <= output_fields)

	def test_work_order_lineage_fields_are_custom_fields(self):
		source = (ROOT / "setup/resources.py").read_text()
		for fieldname in ("custom_aps_campaign", "custom_aps_output_role", "custom_aps_capacity_owner", "custom_aps_source_reason"):
			self.assertIn(fieldname, source)

	def test_campaign_lineage_is_visible_in_both_proposal_layers(self):
		for filename in (
			"injection_aps/doctype/aps_work_order_proposal_item/aps_work_order_proposal_item.json",
			"injection_aps/doctype/aps_shift_schedule_proposal_item/aps_shift_schedule_proposal_item.json",
		):
			definition = json.loads((ROOT / filename).read_text())
			fields = {row["fieldname"] for row in definition["fields"]}
			self.assertTrue({"production_campaign", "output_role", "capacity_owner"} <= fields)
		client = (ROOT / "public/js/aps_work_order_proposal_batch.js").read_text()
		self.assertIn("render_campaign_summary", client)
		self.assertIn("atomically", client)

	def test_result_and_segments_have_exact_campaign_lineage(self):
		result = json.loads((ROOT / "injection_aps/doctype/aps_schedule_result/aps_schedule_result.json").read_text())
		segment = json.loads((ROOT / "injection_aps/doctype/aps_schedule_segment/aps_schedule_segment.json").read_text())
		self.assertTrue({"production_campaign", "campaign_group_key", "campaign_output_role"} <= {row["fieldname"] for row in result["fields"]})
		self.assertTrue({"production_campaign", "capacity_owner"} <= {row["fieldname"] for row in segment["fields"]})


if __name__ == "__main__":
	unittest.main()
