from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

from injection_aps.services import planning


class TestExceptionResolutionContext(TestCase):
	def test_historical_exception_without_diagnostics_gets_actionable_guidance(self):
		with patch("injection_aps.services.planning._", side_effect=lambda message, **_kwargs: message):
			context = planning._build_exception_resolution_context({
				"name": "EXC-1",
				"planning_run": "RUN-1",
				"severity": "Critical",
				"exception_type": "Machine Capacity Missing",
				"message": "No capacity is available on workstation M-1.",
				"workstation": "M-1",
				"source_doctype": "APS Schedule Segment",
				"source_name": "SEG-1",
			})

		self.assertEqual(context["name"], "EXC-1")
		self.assertEqual(context["root_cause_text"], "No capacity is available on workstation M-1.")
		self.assertGreaterEqual(len(context["suggested_actions"]), 2)
		self.assertIn("APS Machine Capability", context["suggested_actions"][0])
		self.assertEqual(context["related_routes"]["source"], "Form/APS Schedule Segment/SEG-1")

	def test_explicit_diagnostic_guidance_remains_authoritative(self):
		context = planning._build_exception_resolution_context(
			{
				"name": "EXC-2",
				"exception_type": "Custom Check",
				"message": "Custom failure.",
				"diagnostic_json": '{"root_cause_codes":["CUSTOM"],"root_cause_text":"Exact root cause.","suggested_actions":["Exact operator action."]}',
			}
		)

		self.assertEqual(context["root_cause_codes"], ["CUSTOM"])
		self.assertEqual(context["root_cause_text"], "Exact root cause.")
		self.assertEqual(context["suggested_actions"], ["Exact operator action."])

	def test_historical_delivery_hint_is_not_used_as_root_cause_and_defaults_are_added(self):
		with patch("injection_aps.services.planning._", side_effect=lambda message, **_kwargs: message):
			context = planning._build_exception_resolution_context({
				"name": "EXC-3",
				"exception_type": "Delivery Delay",
				"message": "Segment SEG-1 ends 897.8 minutes after requested delivery.",
				"resolution_hint": "Move the segment or confirm a revised customer delivery date.",
			})

		self.assertEqual(
			context["root_cause_text"],
			"Segment SEG-1 ends 897.8 minutes after requested delivery.",
		)
		self.assertEqual(
			context["suggested_actions"][0],
			"Move the segment or confirm a revised customer delivery date.",
		)
		self.assertGreaterEqual(len(context["suggested_actions"]), 4)
		self.assertIn("requested delivery date", context["suggested_actions"][1])

	def test_segment_exception_source_snapshot_includes_times_quantities_and_result(self):
		def get_value(doctype, name, fields, as_dict=False):
			if doctype == "APS Schedule Segment":
				return {
					"name": "SEG-1",
					"parent": "RESULT-1",
					"workstation": "M-1",
					"plant_floor": "PF-1",
					"current_start_time": "2026-08-21 08:00:00",
					"current_end_time": "2026-08-21 12:00:00",
					"planned_qty": 200,
					"schedule_delay_minutes": 30,
				}
			return {
				"name": "RESULT-1",
				"item_code": "ITEM-1",
				"customer": "CUSTOMER-1",
				"requested_date": "2026-08-21",
				"planned_qty": 250,
				"machine_scheduled_qty": 200,
				"unscheduled_qty": 50,
			}

		fake_frappe = MagicMock()
		fake_frappe.db.get_value.side_effect = get_value
		with patch.object(planning, "frappe", fake_frappe):
			snapshot = planning._get_exception_source_snapshot({
				"source_doctype": "APS Schedule Segment",
				"source_name": "SEG-1",
			})

		self.assertEqual(snapshot["result_name"], "RESULT-1")
		self.assertEqual(snapshot["item_code"], "ITEM-1")
		self.assertEqual(snapshot["end_time"], "2026-08-21 12:00:00")
		self.assertEqual(snapshot["segment_planned_qty"], 200)
		self.assertEqual(snapshot["unscheduled_qty"], 50)
