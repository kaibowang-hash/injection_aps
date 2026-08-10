from __future__ import annotations

from unittest import TestCase

from injection_aps.api.app import _build_exception_routes


class TestExceptionSourceRoutes(TestCase):
	def test_schedule_segment_source_targets_focused_gantt(self):
		routes = _build_exception_routes(
			{
				"planning_run": "APS RUN/7",
				"source_doctype": "APS Schedule Segment",
				"source_name": "segment & 1",
			}
		)

		expected = "aps-schedule-gantt?run_name=APS+RUN%2F7&segment_name=segment+%26+1"
		self.assertEqual(routes["gantt_route"], expected)
		self.assertEqual(routes["source_route"], expected)

	def test_regular_source_keeps_form_route(self):
		routes = _build_exception_routes(
			{
				"planning_run": "APS-RUN-00007",
				"source_doctype": "APS Schedule Result",
				"source_name": "APS-RES-00154",
			}
		)

		self.assertEqual(routes["gantt_route"], "aps-schedule-gantt?run_name=APS-RUN-00007")
		self.assertEqual(routes["source_route"], "Form/APS Schedule Result/APS-RES-00154")

	def test_schedule_segment_without_run_has_no_broken_form_route(self):
		routes = _build_exception_routes(
			{
				"source_doctype": "APS Schedule Segment",
				"source_name": "segment-1",
			}
		)

		self.assertEqual(routes, {"gantt_route": "", "source_route": ""})
