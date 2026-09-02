from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch

import frappe

from injection_aps.api import app
from injection_aps.services import item_display


class TestItemDisplay(unittest.TestCase):
	def test_display_endpoint_uses_permission_filtered_item_list(self):
		meta = MagicMock()
		meta.has_field.return_value = True
		endpoint = inspect.unwrap(app.get_item_display_details)
		with (
			patch.object(app, "_require_read_access") as require_read,
			patch.object(app.frappe, "get_meta", return_value=meta),
			patch.object(
				app.frappe,
				"get_list",
				return_value=[
					frappe._dict(name="ITEM-1", customer_code="CUST-1", item_name="Name 1")
				],
			) as get_list,
		):
			result = endpoint('["ITEM-1", "ITEM-1"]')

		require_read.assert_called_once_with()
		get_list.assert_called_once_with(
			"Item",
			filters={"name": ("in", ["ITEM-1"])},
			fields=["name", "item_name", "customer_code"],
			limit_page_length=0,
		)
		self.assertEqual(
			result["items"],
			[{"item_code": "ITEM-1", "customer_code": "CUST-1", "item_name": "Name 1"}],
		)

	def test_nested_rows_are_enriched_with_one_item_query(self):
		meta = MagicMock()
		meta.has_field.return_value = True
		payload = {
			"rows": [
				{"item_code": "ITEM-2"},
				{"item_code": "ITEM-1", "item_name": "stale"},
			],
			"nested": {"preview": [{"item_code": "ITEM-1"}]},
		}
		with (
			patch.object(item_display.frappe, "get_meta", return_value=meta),
			patch.object(
				item_display.frappe,
				"get_list",
				return_value=[
					frappe._dict(name="ITEM-1", customer_code="CUST-1", item_name="Name 1"),
					frappe._dict(name="ITEM-2", customer_code="CUST-2", item_name="Name 2"),
				],
			) as get_list,
		):
			result = item_display.attach_item_display_fields(payload)

		self.assertIs(result, payload)
		self.assertEqual(payload["rows"][0]["customer_code"], "CUST-2")
		self.assertEqual(payload["rows"][0]["item_name"], "Name 2")
		self.assertEqual(payload["rows"][1]["item_name"], "Name 1")
		self.assertEqual(payload["nested"]["preview"][0]["customer_code"], "CUST-1")
		get_list.assert_called_once_with(
			"Item",
			filters={"name": ("in", ["ITEM-1", "ITEM-2"])},
			fields=["name", "item_name", "customer_code"],
			limit_page_length=0,
		)

	def test_custom_item_identity_field_and_missing_customer_code_are_supported(self):
		meta = MagicMock()
		meta.has_field.return_value = False
		payload = {"options": [{"item": "ITEM-1"}]}
		with (
			patch.object(item_display.frappe, "get_meta", return_value=meta),
			patch.object(
				item_display.frappe,
				"get_list",
				return_value=[frappe._dict(name="ITEM-1", item_name="Name 1")],
			),
		):
			item_display.attach_item_display_fields(payload, item_fields=("item_code", "item"))

		self.assertEqual(payload["options"][0]["customer_code"], "")
		self.assertEqual(payload["options"][0]["item_name"], "Name 1")


if __name__ == "__main__":
	unittest.main()
