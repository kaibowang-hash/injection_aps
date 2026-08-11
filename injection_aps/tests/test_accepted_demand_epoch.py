from __future__ import annotations

import json
import unittest
from datetime import date

import frappe

from injection_aps.services import availability, execution_sync, planning


class TestAcceptedDemandEpoch(unittest.TestCase):
	def setUp(self):
		self.target = {
			"customer_schedule_item": "TARGET-1",
			"sales_order": "SO-1",
			"item_code": "FG-1",
			"schedule_date": "2026-08-11",
			"opening_required_qty": 100,
			"opening_delivered_qty": 20,
			"source_open_qty": 80,
			"accepted_required_qty": 70,
			"accepted_delivered_qty": 30,
			"accepted_source_open_qty": 50,
			"accepted_current_open_qty": 40,
			"accepted_schedule_date": "2026-08-12",
		}

	def test_fulfillment_and_execution_use_accepted_lifetime_cap(self):
		result = frappe._dict(
			name="RESULT-1",
			item_code="FG-1",
			sales_order="SO-1",
			fulfillment_baseline_json=json.dumps(
				{"version": 4, "targets": [self.target]},
				sort_keys=True,
			),
		)

		self.assertEqual(availability._get_result_fulfillment_demand_qty(result), 50)
		self.assertEqual(availability._get_frozen_target_fulfillment_qty(self.target), 50)
		self.assertEqual(execution_sync._get_frozen_target_fulfillment_qty(self.target), 50)

	def test_customer_progress_uses_accepted_date_and_cap_without_erasing_originals(self):
		result = frappe._dict(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			item_code="FG-1",
			requested_date=date(2026, 8, 12),
			machine_scheduled_qty=70,
			fulfillment_baseline_json=json.dumps(
				{"version": 4, "targets": [self.target]},
				sort_keys=True,
			),
		)

		rows = planning._get_customer_schedule_progress_result_targets(result, "FG-1")

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["schedule_date"], date(2026, 8, 12))
		self.assertEqual(rows[0]["remaining_qty"], 50)
		self.assertEqual(self.target["schedule_date"], "2026-08-11")
		self.assertEqual(self.target["source_open_qty"], 80)

	def test_legacy_baseline_still_uses_original_source_open(self):
		legacy = dict(self.target)
		for fieldname in (
			"accepted_required_qty",
			"accepted_delivered_qty",
			"accepted_source_open_qty",
			"accepted_current_open_qty",
			"accepted_schedule_date",
		):
			legacy.pop(fieldname)

		self.assertEqual(availability._get_frozen_target_fulfillment_qty(legacy), 80)
		self.assertEqual(execution_sync._get_frozen_target_fulfillment_qty(legacy), 80)
		self.assertEqual(planning._get_frozen_target_fulfillment_qty(legacy), 80)


if __name__ == "__main__":
	unittest.main()
