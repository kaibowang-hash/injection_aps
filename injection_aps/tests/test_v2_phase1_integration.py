from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, getdate, now_datetime, nowtime, today

from injection_aps.services import delivery_fulfillment, planning, schedule_revision


class TestPhase1RevisionIntegration(FrappeTestCase):
	def setUp(self):
		# Some legacy transaction tests intentionally replace this request-local
		# flag and do not restore the normal list when the full app suite continues.
		# Each integration fixture must establish its own valid Frappe save context.
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		for doctype in ("APS Demand Identity", "APS Unallocated Delivery", "Customer Delivery Schedule"):
			if not frappe.db.exists("DocType", doctype):
				self.skipTest("Phase 1 DocTypes are not synced.")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		self.items = frappe.get_all("Item", filters={"disabled": 0}, pluck="name", limit=2)
		if not self.company or not self.customer or len(self.items) < 2:
			self.skipTest("Phase 1 integration needs a Company, Customer, and two Items.")
		self.original_v2 = frappe.db.get_single_value("APS Settings", "enable_aps_v2") or 0
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", 1)
		frappe.clear_cache(doctype="APS Settings")
		self.scope = f"APS-V2-P1-{frappe.generate_hash(length=10)}"
		self.date_1 = getdate(add_days(today(), 30))
		self.date_2 = getdate(add_days(today(), 31))

	def tearDown(self):
		if hasattr(self, "original_v2"):
			frappe.db.set_single_value("APS Settings", "enable_aps_v2", self.original_v2)
			frappe.clear_cache(doctype="APS Settings")

	def test_full_partial_date_move_and_excess_keep_stable_identity(self):
		first = self._apply(
			"V1",
			"Full Replacement",
			[
				self._row(self.items[0], self.date_1, 100, "LINE-1"),
				self._row(self.items[-1], self.date_2, 40, "LINE-2"),
			],
		)
		first_rows = frappe.get_all(
			"Customer Delivery Schedule Item",
			filters={"parent": first["schedule"]},
			fields=["name", "item_code", "demand_identity"],
		)
		identity_by_item = {row.item_code: row.demand_identity for row in first_rows}
		first_item = next(row.name for row in first_rows if row.item_code == self.items[0])
		frappe.db.set_value(
			"Customer Delivery Schedule Item",
			first_item,
			{"produced_qty": 70, "executed_floor_qty": 70},
			update_modified=False,
		)

		recommendation = schedule_revision.recommend_schedule_revision_mode(
			customer=self.customer,
			company=self.company,
			schedule_scope=self.scope,
			rows_json=[self._row(self.items[0], self.date_2, 30, "LINE-1")],
		)
		self.assertEqual(recommendation["recommended_mode"], "Partial Revision")

		second = self._apply(
			"V2",
			"Partial Revision",
			[self._row(self.items[0], self.date_2, 30, "LINE-1")],
		)
		second_rows = frappe.get_all(
			"Customer Delivery Schedule Item",
			filters={"parent": second["schedule"]},
			fields=[
				"item_code",
				"demand_identity",
				"effective_qty",
				"executed_floor_qty",
				"excess_qty",
				"revision_action",
				"schedule_date",
			],
		)
		by_item = {row.item_code: row for row in second_rows}
		self.assertEqual(by_item[self.items[0]].demand_identity, identity_by_item[self.items[0]])
		self.assertEqual(by_item[self.items[0]].revision_action, "Date Moved")
		self.assertEqual(by_item[self.items[0]].effective_qty, 30)
		self.assertEqual(by_item[self.items[0]].executed_floor_qty, 70)
		self.assertEqual(by_item[self.items[0]].excess_qty, 40)
		self.assertEqual(getdate(by_item[self.items[0]].schedule_date), self.date_2)
		self.assertEqual(by_item[self.items[-1]].revision_action, "Retained")
		self.assertEqual(by_item[self.items[-1]].effective_qty, 40)
		self.assertEqual(
			frappe.db.get_value("APS Demand Identity", identity_by_item[self.items[0]], "current_schedule"),
			second["schedule"],
		)

	def test_incremental_overlap_is_additive_and_idempotent(self):
		self._apply(
			"BASE",
			"Full Replacement",
			[self._row(self.items[0], self.date_1, 100, "BASE-LINE")],
		)
		rows = [self._row(self.items[0], self.date_1, 25, "INCREMENT-LINE")]
		preview = self._preview("INC", "Incremental Demand", rows)
		first = schedule_revision.apply_revision(
			customer=self.customer,
			company=self.company,
			version_no="INC",
			schedule_scope=self.scope,
			confirmed_revision_mode="Incremental Demand",
			duplicate_policy="Block",
			rows_json=rows,
			mode_confirmation_reason="Customer confirmed this is additional demand.",
			expected_active_state_token=preview["active_state_token"],
			expected_revision_fingerprint=preview["revision_fingerprint"],
		)
		second = schedule_revision.apply_revision(
			customer=self.customer,
			company=self.company,
			version_no="INC",
			schedule_scope=self.scope,
			confirmed_revision_mode="Incremental Demand",
			duplicate_policy="Block",
			rows_json=rows,
			mode_confirmation_reason="Customer confirmed this is additional demand.",
		)
		self.assertFalse(first["idempotent_replay"])
		self.assertTrue(second["idempotent_replay"])
		self.assertEqual(second["schedule"], first["schedule"])
		active_total = frappe.db.sql(
			"""
			select coalesce(sum(i.effective_qty), 0)
			from `tabCustomer Delivery Schedule Item` i
			inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
			where s.company = %s and s.customer = %s and s.schedule_scope = %s and s.status = 'Active'
			""",
			(self.company, self.customer, self.scope),
		)[0][0]
		self.assertEqual(active_total, 125)

	def test_ambiguous_identity_blocks_preview(self):
		self._apply(
			"BASE",
			"Full Replacement",
			[
				self._row(self.items[0], self.date_1, 40, "LINE-A"),
				self._row(self.items[0], self.date_2, 60, "LINE-B"),
			],
		)
		preview = self._preview(
			"AMBIG",
			"Partial Revision",
			[{"item_code": self.items[0], "schedule_date": add_days(self.date_2, 1), "qty": 50}],
		)
		self.assertFalse(preview["can_apply"])
		self.assertEqual(preview["identity_issues"][0]["reason_code"], "AMBIGUOUS_IDENTITY")

	def test_legacy_backfill_reports_duplicate_external_line_once_and_never_guesses(self):
		schedule_name = f"APS-V2-P1-LEGACY-{frappe.generate_hash(length=10)}"
		schedule = frappe.new_doc("Customer Delivery Schedule")
		schedule.name = schedule_name
		schedule.customer = self.customer
		schedule.company = self.company
		schedule.schedule_scope = f"{self.scope}-LEGACY"
		schedule.version_no = "LEGACY"
		schedule.status = "Active"
		schedule.import_strategy = "Replace Scope"
		schedule.db_insert()
		row_names = []
		for index, qty in enumerate((40, 60), start=1):
			row = frappe.new_doc("Customer Delivery Schedule Item")
			row.name = f"APS-V2-P1-LEGACY-ROW-{frappe.generate_hash(length=10)}"
			row.parent = schedule.name
			row.parenttype = "Customer Delivery Schedule"
			row.parentfield = "items"
			row.idx = index
			row.item_code = self.items[0]
			row.schedule_date = self.date_1
			row.qty = qty
			row.external_line_reference = "DUPLICATE-CUSTOMER-LINE"
			row.db_insert()
			row_names.append(row.name)

		first = schedule_revision.backfill_active_demand_identities()
		second = schedule_revision.backfill_active_demand_identities()
		self.assertGreaterEqual(first["ambiguities"], 1)
		self.assertGreaterEqual(second["ambiguities"], 1)
		self.assertEqual(
			frappe.get_all(
				"Customer Delivery Schedule Item",
				filters={"name": ("in", row_names), "demand_identity": ("is", "set")},
				pluck="name",
			),
			[],
		)
		exceptions = frappe.get_all(
			"APS Exception Log",
			filters={
				"source_doctype": "Customer Delivery Schedule",
				"source_name": schedule.name,
				"exception_type": ("like", "Phase1 Identity Backfill%"),
			},
			pluck="name",
		)
		self.assertEqual(len(exceptions), 1)

	def _preview(self, version, mode, rows):
		return schedule_revision.preview_revision(
			customer=self.customer,
			company=self.company,
			version_no=version,
			schedule_scope=self.scope,
			revision_mode=mode,
			duplicate_policy="Block",
			rows_json=rows,
		)

	def _apply(self, version, mode, rows):
		preview = self._preview(version, mode, rows)
		self.assertTrue(preview["can_apply"], preview.get("checks"))
		return schedule_revision.apply_revision(
			customer=self.customer,
			company=self.company,
			version_no=version,
			schedule_scope=self.scope,
			confirmed_revision_mode=mode,
			duplicate_policy="Block",
			rows_json=rows,
			mode_confirmation_reason="Integration-test confirmation" if mode != preview["recommended_revision_mode"] else None,
			expected_active_state_token=preview["active_state_token"],
			expected_revision_fingerprint=preview["revision_fingerprint"],
		)

	@staticmethod
	def _row(item, schedule_date, qty, external_reference):
		return {
			"item_code": item,
			"schedule_date": schedule_date,
			"qty": qty,
			"external_line_reference": external_reference,
		}


class TestPhase1DeliveryIntegration(FrappeTestCase):
	def setUp(self):
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		for doctype in ("APS Demand Identity", "APS Unallocated Delivery", "APS Delivery Allocation"):
			if not frappe.db.exists("DocType", doctype):
				self.skipTest("Phase 1 delivery DocTypes are not synced.")
		self.company = frappe.db.get_value("Company", {})
		self.customer = frappe.db.get_value("Customer", {})
		if not self.company or not self.customer:
			self.skipTest("Phase 1 delivery integration needs Company and Customer.")
		self.original_v2 = frappe.db.get_single_value("APS Settings", "enable_aps_v2") or 0
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", 1)
		frappe.clear_cache(doctype="APS Settings")
		self.item = self._create_item("APS-V2-P1-DELIVERY")
		self.other_item = self._create_item("APS-V2-P1-UNALLOCATED")
		self.due_date = getdate(add_days(today(), 45))
		self.identity, self.schedule_item = self._create_identity_schedule()

	def tearDown(self):
		if hasattr(self, "original_v2"):
			frappe.db.set_single_value("APS Settings", "enable_aps_v2", self.original_v2)
			frappe.clear_cache(doctype="APS Settings")

	def test_explicit_delivery_and_return_follow_original_identity(self):
		delivery_note, delivery_item = self._create_delivery_note(
			self.item,
			80,
			demand_identity=self.identity,
			schedule_item=self.schedule_item,
		)
		first = delivery_fulfillment.sync_delivery_allocations(
			company=self.company,
			customer=self.customer,
			source_delivery_note=delivery_note,
		)
		self.assertEqual(first["unallocated_count"], 0)
		self.assertEqual(first["rollup"]["delivered_qty"], 80)

		return_note, _return_item = self._create_delivery_note(
			self.item,
			30,
			is_return=True,
			return_against=delivery_note,
			original_delivery_note_item=delivery_item,
		)
		returned = delivery_fulfillment.sync_delivery_allocations(
			company=self.company,
			customer=self.customer,
			source_delivery_note=return_note,
		)
		self.assertEqual(returned["unallocated_count"], 0)
		self.assertEqual(
			frappe.db.get_value("Customer Delivery Schedule Item", self.schedule_item, "delivered_qty"),
			50,
		)
		allocation = frappe.db.get_value(
			"APS Delivery Allocation",
			{"source_delivery_note": return_note},
			["demand_identity", "allocation_method", "effective_qty", "original_delivery_note_item"],
			as_dict=True,
		)
		self.assertEqual(allocation.demand_identity, self.identity)
		self.assertEqual(allocation.allocation_method, "Return Trace")
		self.assertEqual(allocation.effective_qty, -30)
		self.assertEqual(allocation.original_delivery_note_item, delivery_item)

	def test_delivery_plan_lineage_reaches_delivery_note_and_schedule_rollup(self):
		delivery_plan = frappe.new_doc("Delivery Plan")
		delivery_plan.name = f"APS-V2-P1-DP-{frappe.generate_hash(length=10)}"
		delivery_plan.customer = self.customer
		delivery_plan.company = self.company
		delivery_plan.arrival_date = self.due_date
		delivery_plan.delivery_date = self.due_date
		delivery_plan.db_insert()
		plan_item = frappe.new_doc("Delivery Plan Item")
		plan_item.name = f"APS-V2-P1-DPI-{frappe.generate_hash(length=10)}"
		plan_item.parent = delivery_plan.name
		plan_item.parenttype = "Delivery Plan"
		plan_item.parentfield = "items"
		plan_item.idx = 1
		plan_item.item_code = self.item
		plan_item.planned_delivery_qty = 40
		plan_item.custom_aps_demand_identity = self.identity
		plan_item.custom_aps_customer_schedule_item = self.schedule_item
		plan_item.custom_aps_required_delivery_date = self.due_date
		plan_item.custom_aps_match_method = "Delivery Plan FIFO"
		plan_item.db_insert()

		draft = frappe.new_doc("Delivery Note")
		draft.company = self.company
		draft.customer = self.customer
		draft.delivery_plan = delivery_plan.name
		draft_item = draft.append("items", {"item_code": self.item, "qty": 40, "stock_qty": 40})
		delivery_fulfillment.inherit_delivery_note_lineage(draft)
		self.assertEqual(draft_item.custom_aps_demand_identity, self.identity)
		self.assertEqual(draft_item.custom_aps_customer_schedule_item, self.schedule_item)
		self.assertEqual(draft_item.custom_aps_delivery_plan_detail, plan_item.name)
		self.assertEqual(draft_item.custom_aps_match_method, "Delivery Plan")

		delivery_note, delivery_item = self._create_delivery_note(
			self.item,
			40,
			demand_identity=draft_item.custom_aps_demand_identity,
			schedule_item=draft_item.custom_aps_customer_schedule_item,
			delivery_plan=delivery_plan.name,
			delivery_plan_detail=draft_item.custom_aps_delivery_plan_detail,
		)
		result = delivery_fulfillment.sync_delivery_allocations(
			company=self.company,
			customer=self.customer,
			source_delivery_note=delivery_note,
		)
		allocation = frappe.db.get_value(
			"APS Delivery Allocation",
			{"source_delivery_note_item": delivery_item},
			["demand_identity", "customer_schedule_item", "effective_qty"],
			as_dict=True,
		)
		self.assertEqual(allocation.demand_identity, self.identity)
		self.assertEqual(allocation.customer_schedule_item, self.schedule_item)
		self.assertEqual(allocation.effective_qty, 40)
		self.assertEqual(result["rollup"]["delivered_qty"], 40)
		self.assertEqual(
			frappe.db.get_value("Customer Delivery Schedule Item", self.schedule_item, "delivered_qty"),
			40,
		)

	def test_unlinked_delivery_creates_queue_without_raising(self):
		delivery_note, delivery_item = self._create_delivery_note(self.other_item, 20)
		result = delivery_fulfillment.sync_delivery_allocations(
			company=self.company,
			customer=self.customer,
			source_delivery_note=delivery_note,
		)
		self.assertEqual(result["unallocated_count"], 1)
		queue = frappe.db.get_value(
			"APS Unallocated Delivery",
			{"source_delivery_note_item": delivery_item},
			["status", "unallocated_qty", "reason_code"],
			as_dict=True,
		)
		self.assertEqual(queue.status, "Open")
		self.assertEqual(queue.unallocated_qty, 20)
		self.assertEqual(queue.reason_code, "DELIVERY_LINEAGE_UNRESOLVED")

	def _create_identity_schedule(self):
		scope = f"APS-V2-P1-DEL-{frappe.generate_hash(length=10)}"
		preview = schedule_revision.preview_revision(
			customer=self.customer,
			company=self.company,
			version_no="BASE",
			schedule_scope=scope,
			revision_mode="Full Replacement",
			rows_json=[{"item_code": self.item, "schedule_date": self.due_date, "qty": 100, "external_line_reference": "DELIVERY-LINE"}],
		)
		result = schedule_revision.apply_revision(
			customer=self.customer,
			company=self.company,
			version_no="BASE",
			schedule_scope=scope,
			confirmed_revision_mode="Full Replacement",
			rows_json=[{"item_code": self.item, "schedule_date": self.due_date, "qty": 100, "external_line_reference": "DELIVERY-LINE"}],
			expected_active_state_token=preview["active_state_token"],
			expected_revision_fingerprint=preview["revision_fingerprint"],
		)
		row = frappe.db.get_value(
			"Customer Delivery Schedule Item",
			{"parent": result["schedule"]},
			["name", "demand_identity"],
			as_dict=True,
		)
		return row.demand_identity, row.name

	def _create_delivery_note(
		self,
		item_code,
		qty,
		*,
		demand_identity=None,
		schedule_item=None,
		is_return=False,
		return_against=None,
		original_delivery_note_item=None,
		delivery_plan=None,
		delivery_plan_detail=None,
	):
		doc = frappe.new_doc("Delivery Note")
		doc.name = f"APS-V2-P1-DN-{frappe.generate_hash(length=10)}"
		doc.docstatus = 1
		doc.company = self.company
		doc.customer = self.customer
		doc.posting_date = self.due_date
		doc.posting_time = nowtime()
		doc.is_return = 1 if is_return else 0
		doc.return_against = return_against
		doc.delivery_plan = delivery_plan
		doc.db_insert()
		item = frappe.new_doc("Delivery Note Item")
		item.name = frappe.generate_hash(length=10)
		item.parent = doc.name
		item.parenttype = "Delivery Note"
		item.parentfield = "items"
		item.idx = 1
		item.item_code = item_code
		item.qty = -qty if is_return else qty
		item.stock_qty = -qty if is_return else qty
		item.conversion_factor = 1
		item.dn_detail = original_delivery_note_item
		item.custom_aps_demand_identity = demand_identity
		item.custom_aps_customer_schedule_item = schedule_item
		item.custom_aps_delivery_plan_detail = delivery_plan_detail
		item.db_insert()
		return doc.name, item.name

	@staticmethod
	def _create_item(prefix):
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or frappe.db.get_value("Item Group", {})
		stock_uom = frappe.db.get_value("UOM", {})
		name = f"{prefix}-{frappe.generate_hash(length=8)}"
		item = frappe.new_doc("Item")
		item.name = name
		item.item_code = name
		item.item_name = name
		item.item_group = item_group
		item.stock_uom = stock_uom
		item.is_stock_item = 1
		item.disabled = 0
		item.db_insert()
		return item.name


class TestPhase1NoSalesOrderWorkOrderIntegration(FrappeTestCase):
	def setUp(self):
		if not isinstance(frappe.flags.get("currently_saving"), list):
			frappe.flags.currently_saving = []
		company_row = frappe.db.sql(
			"""
			select company, count(*) as warehouse_count
			from `tabWarehouse`
			where is_group = 0 and ifnull(disabled, 0) = 0 and ifnull(company, '') != ''
			group by company
			having count(*) >= 3
			order by count(*) desc
			limit 1
			""",
			as_dict=True,
		)
		self.workstation = frappe.db.get_value("Workstation", {})
		if not company_row or not self.workstation:
			self.skipTest("No-SO Work Order test needs one Company with three warehouses and a Workstation.")
		self.company = company_row[0].company
		self.warehouses = frappe.get_all(
			"Warehouse",
			filters={"company": self.company, "is_group": 0, "disabled": 0},
			pluck="name",
			order_by="name",
			limit=3,
		)
		self.original_v2 = frappe.db.get_single_value("APS Settings", "enable_aps_v2") or 0
		frappe.db.set_single_value("APS Settings", "enable_aps_v2", 1)
		frappe.clear_cache(doctype="APS Settings")

	def tearDown(self):
		if hasattr(self, "original_v2"):
			frappe.db.set_single_value("APS Settings", "enable_aps_v2", self.original_v2)
			frappe.clear_cache(doctype="APS Settings")

	def test_no_sales_order_work_order_submits_and_receives_finished_goods(self):
		from unittest.mock import patch

		from erpnext.manufacturing.doctype.production_plan.test_production_plan import make_bom
		from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry
		from erpnext.stock.doctype.stock_entry import test_stock_entry

		suffix = frappe.generate_hash(length=10)
		source_warehouse, wip_warehouse, fg_warehouse = self.warehouses
		item_group = frappe.db.get_value("Item Group", {"is_group": 0})
		stock_uom = frappe.db.get_value("UOM", {})

		def create_manufacturing_item(item_code, warehouse, valuation_rate):
			return frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"description": "APS V2 Phase 1 isolated Work Order fixture",
					"item_group": item_group,
					"stock_uom": stock_uom,
					"is_stock_item": 1,
					"include_item_in_manufacturing": 1,
					"valuation_rate": valuation_rate,
					"item_defaults": [{"company": self.company, "default_warehouse": warehouse}],
				}
			).insert(ignore_permissions=True)

		fg_item = create_manufacturing_item(f"APS-V2-P1-FG-{suffix}", fg_warehouse, 100)
		raw_item = create_manufacturing_item(f"APS-V2-P1-RM-{suffix}", source_warehouse, 10)
		currency = frappe.db.get_value("Company", self.company, "default_currency")
		bom = make_bom(
			item=fg_item.name,
			raw_materials=[raw_item.name],
			rm_qty=1,
			rate=10,
			company=self.company,
			currency=currency,
			do_not_save=True,
		)
		bom.custom_temporary_bom = 0
		bom.insert(ignore_permissions=True)
		bom.submit()
		run = frappe.new_doc("APS Planning Run")
		run.name = f"APS-V2-P1-RUN-{suffix}"
		run.company = self.company
		run.run_type = "Formal"
		run.status = "Approved"
		run.horizon_start = getdate(today())
		run.horizon_end = getdate(add_days(today(), 7))
		run.db_insert()
		result = frappe.new_doc("APS Schedule Result")
		result.name = f"APS-V2-P1-RES-{suffix}"
		result.planning_run = run.name
		result.company = self.company
		result.item_code = fg_item.name
		result.requested_date = getdate(add_days(today(), 2))
		result.demand_source = "Customer Delivery Schedule"
		result.planned_qty = 1
		result.machine_scheduled_qty = 1
		result.status = "Scheduled"
		result.db_insert()
		segment = frappe.new_doc("APS Schedule Segment")
		segment.name = f"APS-V2-P1-SEG-{suffix}"
		segment.parent = result.name
		segment.parenttype = "APS Schedule Result"
		segment.parentfield = "segments"
		segment.idx = 1
		segment.workstation = self.workstation
		segment.start_time = now_datetime()
		segment.end_time = add_to_date(segment.start_time, hours=1)
		segment.planned_qty = 1
		segment.sequence_no = 1
		segment.segment_kind = "Primary"
		segment.segment_status = "Planned"
		segment.db_insert()

		lineage = planning._get_result_sales_order_lineage(result.as_dict())
		self.assertIsNone(lineage["blocking_reason"])
		with patch.object(
			planning,
			"_get_work_order_warehouse_values",
			return_value={
				"source_warehouse": source_warehouse,
				"wip_warehouse": wip_warehouse,
				"fg_warehouse": fg_warehouse,
				"scrap_warehouse": fg_warehouse,
			},
		):
			work_order = planning._create_formal_work_order(
				run,
				result,
				1,
				segment.start_time,
				segment.end_time,
				{},
				None,
				lineage["sales_order"],
				lineage["sales_order_item"],
			)
		wo = frappe.get_doc("Work Order", work_order)
		self.assertEqual(wo.docstatus, 1)
		self.assertFalse(wo.sales_order)
		self.assertFalse(wo.sales_order_item)
		self.assertEqual(wo.custom_aps_run, run.name)
		self.assertEqual(wo.custom_aps_result_reference, result.name)
		self.assertEqual(wo.bom_no, bom.name)

		test_stock_entry.make_stock_entry(
			item_code=raw_item.name,
			target=source_warehouse,
			qty=2,
			basic_rate=10,
		)
		transfer = frappe.get_doc(make_stock_entry(wo.name, "Material Transfer for Manufacture", 1))
		for row in transfer.items:
			row.s_warehouse = row.s_warehouse or source_warehouse
			row.t_warehouse = row.t_warehouse or wip_warehouse
		transfer.insert()
		transfer.submit()
		manufacture = frappe.get_doc(make_stock_entry(wo.name, "Manufacture", 1))
		for row in manufacture.items:
			if row.is_finished_item:
				row.t_warehouse = row.t_warehouse or fg_warehouse
			else:
				row.s_warehouse = row.s_warehouse or wip_warehouse
		manufacture.insert()
		manufacture.submit()
		wo.reload()
		self.assertEqual(wo.produced_qty, 1)
		self.assertEqual(manufacture.docstatus, 1)
