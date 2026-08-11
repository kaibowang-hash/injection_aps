from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import frappe

from injection_aps import install
from injection_aps.api import app
from injection_aps.services import planning


class TestImportAndTransactionGuards(unittest.TestCase):
	def test_roles_are_created_before_workspace_on_install_and_migrate(self):
		for hook in (install.after_install, install.after_migrate):
			calls = []
			with (
				patch.object(install, "ensure_standard_customizations", side_effect=lambda: calls.append("customizations")),
				patch.object(install, "ensure_default_settings", side_effect=lambda: calls.append("settings")),
				patch.object(install, "ensure_seed_records", side_effect=lambda: calls.append("seeds")),
				patch.object(install, "ensure_roles", side_effect=lambda: calls.append("roles")),
				patch.object(install, "ensure_roles_and_permissions", side_effect=lambda: calls.append("permissions")),
				patch.object(install, "ensure_workspace_resources", side_effect=lambda: calls.append("workspace")),
				patch.object(install.frappe, "clear_cache", side_effect=lambda: calls.append("cache")),
			):
				hook()
			self.assertLess(calls.index("roles"), calls.index("workspace"))

	def test_before_install_creates_roles_before_frappe_schema_sync(self):
		with patch.object(install, "ensure_roles") as ensure_roles:
			install.before_install()
		ensure_roles.assert_called_once_with()

	def test_atomic_batch_operation_rolls_back_and_reraises(self):
		database = MagicMock()
		failure = RuntimeError("second approved row failed")
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="atomic1234"),
		):
			with self.assertRaisesRegex(RuntimeError, "second approved row failed"):
				planning._run_atomic_batch_operation("aps_batch", lambda: (_ for _ in ()).throw(failure))
		database.savepoint.assert_called_once_with("aps_batch_atomic1234")
		database.rollback.assert_called_once_with(save_point="aps_batch_atomic1234")
		database.release_savepoint.assert_not_called()

	def test_atomic_batch_operation_releases_only_after_success(self):
		database = MagicMock()
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="atomic5678"),
		):
			result = planning._run_atomic_batch_operation("aps_batch", lambda: {"applied": 2})
		self.assertEqual(result, {"applied": 2})
		database.release_savepoint.assert_called_once_with("aps_batch_atomic5678")
		database.rollback.assert_not_called()

	def test_proposal_apply_locks_batch_then_planning_run(self):
		for apply_function, batch_doctype, batch_name in (
			(planning._apply_work_order_proposals, "APS Work Order Proposal Batch", "WOP-1"),
			(planning._apply_shift_schedule_proposals, "APS Shift Schedule Proposal Batch", "SSP-1"),
		):
			database = MagicMock()
			batch = frappe._dict(planning_run="RUN-1", items=[])
			run_doc = frappe._dict(name="RUN-1")
			with (
				patch.object(planning.frappe, "db", database),
				patch.object(planning.frappe, "get_doc", side_effect=[batch, run_doc]),
				patch.object(planning.consistency, "assert_plan_consistent"),
				patch.object(planning, "_assert_work_order_proposal_batch_fingerprint_current"),
				patch.object(planning, "_assert_shift_schedule_proposal_batch_fingerprint_current"),
				patch.object(planning, "_assert_release_capacity_current"),
				patch.object(
					planning,
					"validate_run_mold_readiness",
					side_effect=RuntimeError("stop after fixed lock prefix"),
				),
			):
				with self.subTest(batch_doctype=batch_doctype):
					with self.assertRaisesRegex(RuntimeError, "fixed lock prefix"):
						apply_function(batch_name)
			sql = [call.args[0].lower() for call in database.sql.call_args_list]
			self.assertIn(f"tab{batch_doctype.lower()}", sql[0])
			self.assertIn("tabaps planning run", sql[1])
			self.assertIn("for update", sql[0])
			self.assertIn("for update", sql[1])

	def test_work_order_and_shift_apply_block_unreviewed_rows(self):
		for apply_function, needs_run_doc in (
			(planning._apply_work_order_proposals, True),
			(planning._apply_shift_schedule_proposals, False),
		):
			database = MagicMock()
			batch = MagicMock()
			batch.name = "BATCH-1"
			batch.planning_run = "RUN-1"
			batch.items = [
				frappe._dict(idx=1, review_status="Approved"),
				frappe._dict(idx=2, review_status="Pending"),
			]
			documents = [batch, frappe._dict(name="RUN-1", company="COMPANY-1")] if needs_run_doc else [batch]
			with (
				patch.object(planning.frappe, "db", database),
				patch.object(planning.frappe, "get_doc", side_effect=documents),
				patch.object(planning, "_", side_effect=lambda message: message),
				patch.object(
					planning.frappe,
					"throw",
					side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
						frappe.ValidationError(message)
					),
				),
				patch.object(planning.consistency, "assert_plan_consistent") as consistency_gate,
			):
				with self.subTest(apply_function=apply_function.__name__):
					with self.assertRaisesRegex(frappe.ValidationError, "rows 2 still require"):
						apply_function("BATCH-1")
			consistency_gate.assert_not_called()

	def test_proposal_reject_locks_batch_then_run_and_refuses_applied_state(self):
		for reject_function, batch_doctype, batch_name in (
			(planning._reject_work_order_proposals, "APS Work Order Proposal Batch", "WOP-1"),
			(planning._reject_shift_schedule_proposals, "APS Shift Schedule Proposal Batch", "SSP-1"),
		):
			database = MagicMock()
			batch = frappe._dict(
				name=batch_name,
				planning_run="RUN-1",
				status="Applied",
				items=[frappe._dict(idx=1, review_status="Applied")],
			)
			with (
				patch.object(planning.frappe, "db", database),
				patch.object(planning.frappe, "get_doc", return_value=batch),
				patch.object(planning, "_", side_effect=lambda message: message),
				patch.object(
					planning.frappe,
					"throw",
					side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
						frappe.ValidationError(message)
					),
				),
			):
				with self.subTest(batch_doctype=batch_doctype):
					with self.assertRaisesRegex(frappe.ValidationError, "cannot be rejected"):
						reject_function(batch_name, "stale rejection")
			sql = [call.args[0].lower() for call in database.sql.call_args_list]
			self.assertIn(f"tab{batch_doctype.lower()}", sql[0])
			self.assertIn("tabaps planning run", sql[1])
			self.assertIn("for update", sql[0])
			self.assertIn("for update", sql[1])

	def test_proposal_rejects_mark_private_engine_transition_before_save(self):
		for reject_function in (
			planning._reject_work_order_proposals,
			planning._reject_shift_schedule_proposals,
		):
			database = MagicMock()
			row = frappe._dict(review_status="Pending", review_note="")
			batch = MagicMock()
			batch.name = "BATCH-1"
			batch.planning_run = "RUN-1"
			batch.status = "Ready For Review"
			batch.items = [row]
			batch.flags = frappe._dict()
			batch.notes = ""
			with (
				patch.object(planning.frappe, "db", database),
				patch.object(planning.frappe, "get_doc", return_value=batch),
			):
				with self.subTest(reject_function=reject_function.__name__):
					result = reject_function("BATCH-1", "customer schedule changed")

			self.assertEqual(result["rejected_rows"], 1)
			self.assertEqual(row.review_status, "Rejected")
			self.assertTrue(batch.flags.proposal_engine_transition)
			batch.save.assert_called_once_with(ignore_permissions=True)

	def test_public_proposal_rejects_use_atomic_boundaries(self):
		for reject_function, label in (
			(planning.reject_work_order_proposals, "aps_reject_work_order_proposals"),
			(planning.reject_shift_schedule_proposals, "aps_reject_shift_schedule_proposals"),
		):
			with patch.object(planning, "_run_atomic_batch_operation", return_value={"rejected": 1}) as atomic:
				self.assertEqual(reject_function("BATCH-1", "reason"), {"rejected": 1})
			self.assertEqual(atomic.call_args.args[0], label)

	def test_shift_proposal_fingerprint_is_stable_and_includes_source_state(self):
		base_item = {
			"result_reference": "RESULT-1",
			"segment_reference": "SEG-1",
			"item_code": "ITEM-1",
			"action": "New",
			"posting_date": "2026-08-12",
			"shift_type": "Day",
			"plant_floor": "PF-1",
			"workstation": "MC-1",
			"work_order": "WO-1",
			"planned_start_time": "2026-08-12 08:00:00",
			"planned_end_time": "2026-08-12 10:00:00",
			"planned_qty": 100,
			"work_order_state_token": "WO-STATE-1",
			"segment_state_token": "SEG-STATE-1",
		}
		kwargs = {
			"run_name": "RUN-1",
			"work_order_proposal_batch": "WOP-1",
			"release_from": "2026-08-12",
			"release_to": "2026-08-12",
			"shift_type": "Day",
		}
		first = planning._shift_schedule_proposal_fingerprint(items=[base_item], **kwargs)
		replay = planning._shift_schedule_proposal_fingerprint(items=[dict(base_item)], **kwargs)
		changed = planning._shift_schedule_proposal_fingerprint(
			items=[{**base_item, "segment_state_token": "SEG-STATE-2"}],
			**kwargs,
		)
		self.assertEqual(first, replay)
		self.assertNotEqual(first, changed)

	def test_work_order_proposal_fingerprint_covers_all_apply_quantity_and_date_inputs(self):
		item = {
			"result_reference": "RESULT-1",
			"item_code": "ITEM-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"required_delivery_date": "2026-08-12",
			"action": "Create Delta",
			"proposed_qty": 100,
			"result_state_token": "RESULT-STATE-1",
			"existing_work_order": "WO-1",
			"existing_qty": 40,
			"existing_state_token": "WO-STATE-1",
			"target_start_time": "2026-08-11 08:00:00",
			"target_end_time": "2026-08-11 10:00:00",
			"review_status": "Pending",
		}
		base = planning._work_order_proposal_fingerprint("RUN-1", [item])
		self.assertNotEqual(
			base,
			planning._work_order_proposal_fingerprint("RUN-1", [{**item, "existing_qty": 41}]),
		)
		self.assertNotEqual(
			base,
			planning._work_order_proposal_fingerprint(
				"RUN-1",
				[{**item, "required_delivery_date": "2026-08-13"}],
			),
		)
		self.assertEqual(
			base,
			planning._work_order_proposal_fingerprint("RUN-1", [{**item, "review_status": "Approved"}]),
		)

	def test_apply_rejects_tampered_proposal_rows_before_capacity_or_execution(self):
		work_item = {
			"result_reference": "RESULT-1",
			"item_code": "ITEM-1",
			"customer": "CUSTOMER-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"required_delivery_date": "2026-08-12",
			"action": "Create Delta",
			"proposed_qty": 100,
			"result_state_token": "RESULT-STATE-1",
			"existing_work_order": "WO-1",
			"existing_qty": 40,
			"existing_state_token": "WO-STATE-1",
			"target_start_time": "2026-08-11 08:00:00",
			"target_end_time": "2026-08-11 10:00:00",
		}
		shift_item = {
			"result_reference": "RESULT-1",
			"segment_reference": "SEG-1",
			"item_code": "ITEM-1",
			"action": "New",
			"posting_date": "2026-08-12",
			"shift_type": "Day",
			"plant_floor": "PF-1",
			"workstation": "MC-1",
			"work_order": "WO-1",
			"planned_start_time": "2026-08-12 08:00:00",
			"planned_end_time": "2026-08-12 10:00:00",
			"planned_qty": 100,
			"work_order_state_token": "WO-STATE-1",
			"segment_state_token": "SEG-STATE-1",
			"scheduling_state_token": "",
		}
		work_batch = frappe._dict(
			name="WOP-1",
			status="Ready For Review",
			planning_run="RUN-1",
			proposal_fingerprint=planning._work_order_proposal_fingerprint("RUN-1", [work_item]),
			items=[frappe._dict({**work_item, "existing_qty": 41, "review_status": "Approved"})],
		)
		shift_batch = frappe._dict(
			name="SSP-1",
			status="Ready For Review",
			planning_run="RUN-1",
			work_order_proposal_batch="WOP-1",
			proposal_fingerprint=planning._shift_schedule_proposal_fingerprint(
				run_name="RUN-1",
				work_order_proposal_batch="WOP-1",
				release_from="2026-08-12",
				release_to="2026-08-12",
				shift_type="Day",
				items=[shift_item],
			),
			items=[frappe._dict({**shift_item, "planned_qty": 101, "review_status": "Approved"})],
		)

		for apply_function, batch in (
			(planning._apply_work_order_proposals, work_batch),
			(planning._apply_shift_schedule_proposals, shift_batch),
		):
			database = MagicMock()
			with (
				self.subTest(apply_function=apply_function.__name__),
				patch.object(planning.frappe, "db", database),
				patch.object(planning.frappe, "get_doc", return_value=batch),
				patch.object(planning.consistency, "assert_plan_consistent") as consistency_gate,
				patch.object(planning, "_assert_release_capacity_current") as capacity_gate,
				patch.object(planning, "validate_run_mold_readiness") as mold_gate,
				patch.object(planning, "_", side_effect=lambda message, **_kwargs: message),
				patch.object(
					planning.frappe,
					"throw",
					side_effect=lambda message, exc=frappe.ValidationError, **_kwargs: (_ for _ in ()).throw(
						exc(message)
					),
				),
				self.assertRaisesRegex(frappe.ValidationError, "changed after generation"),
			):
				apply_function(batch.name)
			consistency_gate.assert_called_once()
			capacity_gate.assert_not_called()
			mold_gate.assert_not_called()

	def test_shift_generate_reuses_same_pending_fingerprint(self):
		context = {
			"run_doc": frappe._dict(name="RUN-1", company="COMPANY-1", plant_floor="PF-1"),
			"work_order_proposal_batch_doc": frappe._dict(name="WOP-1"),
			"release_from": "2026-08-12",
			"release_to": "2026-08-12",
			"shift_type": "Day",
			"items": [],
		}
		with (
			patch.object(planning, "_build_shift_schedule_release_context", return_value=context),
			patch.object(planning.consistency, "assert_plan_consistent"),
			patch.object(planning, "_assert_release_capacity_current"),
			patch.object(planning.frappe.db, "get_value", return_value="SSP-EXISTING"),
			patch.object(
				planning,
				"_format_shift_schedule_proposal_batch",
				return_value={"shift_schedule_proposal_batch": "SSP-EXISTING", "idempotent_replay": 1},
			) as formatter,
			patch.object(planning.frappe, "get_doc") as get_doc,
		):
			result = planning.generate_shift_schedule_proposals(run_name="RUN-1")
		self.assertEqual(result["idempotent_replay"], 1)
		formatter.assert_called_once_with("SSP-EXISTING", idempotent_replay=True)
		get_doc.assert_not_called()

	def test_applied_proposal_retries_return_original_results_without_reapplying(self):
		for apply_function, batch_doctype, formatter_name, batch in (
			(
				planning._apply_work_order_proposals,
				"APS Work Order Proposal Batch",
				"_format_work_order_proposal_batch",
				frappe._dict(name="WOP-1", status="Applied", planning_run="RUN-1", proposal_count=1, items=[]),
			),
			(
				planning._apply_shift_schedule_proposals,
				"APS Shift Schedule Proposal Batch",
				"_format_shift_schedule_proposal_batch",
				frappe._dict(name="SSP-1", status="Applied", planning_run="RUN-1", proposal_count=1, items=[]),
			),
		):
			database = MagicMock()
			with (
				patch.object(planning.frappe, "db", database),
				patch.object(planning.frappe, "get_doc", return_value=batch),
				patch.object(planning, formatter_name, return_value={"idempotent_replay": 1}) as formatter,
			):
				with self.subTest(batch_doctype=batch_doctype):
					result = apply_function(batch.name)
			self.assertEqual(result["idempotent_replay"], 1)
			formatter.assert_called_once_with(batch.name, idempotent_replay=True)
			self.assertEqual(database.sql.call_count, 1)

	def test_other_active_run_work_order_is_not_treated_as_orphan(self):
		snapshot = {
			"name": "WO-OLD-ACTIVE",
			"custom_aps_run": "RUN-OLD",
			"custom_aps_result_reference": "RESULT-OLD",
		}
		old_result = frappe._dict(
			name="RESULT-OLD",
			planning_run="RUN-OLD",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json={
				"version": 2,
				"targets": [{"customer_schedule_item": "TARGET-ACTIVE"}],
			},
		)
		with (
			patch.object(planning.frappe, "get_all", return_value=[old_result]),
			patch.object(planning.frappe.db, "sql", return_value=[[1]]),
		):
			self.assertFalse(planning._work_order_is_orphan_candidate_for_run(snapshot, "RUN-NEW"))

	def test_cross_run_work_order_requires_explicitly_retired_demand_before_orphan_proposal(self):
		snapshot = {
			"name": "WO-OLD-RETIRED",
			"custom_aps_run": "RUN-OLD",
			"custom_aps_result_reference": "RESULT-OLD",
		}
		old_result = frappe._dict(
			name="RESULT-OLD",
			planning_run="RUN-OLD",
			demand_source="Customer Delivery Schedule",
			fulfillment_baseline_json={
				"version": 2,
				"targets": [{"customer_schedule_item": "TARGET-OLD", "retired": 1}],
			},
		)
		with (
			patch.object(planning.frappe, "get_all", return_value=[old_result]),
			patch.object(planning.frappe.db, "sql") as sql,
		):
			self.assertTrue(planning._work_order_is_orphan_candidate_for_run(snapshot, "RUN-NEW"))
		sql.assert_not_called()

	def test_second_work_order_batch_is_blocked_when_first_created_open_work_order(self):
		result_doc = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			machine_scheduled_qty=100,
		)
		row = frappe._dict(
			action="New",
			result_reference="RESULT-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			proposed_qty=100,
			result_state_token=planning._work_order_result_proposal_state_token(result_doc, []),
		)
		with (
			patch.object(planning, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "already has an open Work Order"):
				planning._validate_work_order_proposal_row_current(
					row=row,
					batch=frappe._dict(name="BATCH-2", planning_run="RUN-1"),
					run_doc=frappe._dict(name="RUN-1", company="COMPANY-1"),
					result_doc=result_doc,
					primary_segments=[],
					apply_state={
						"open_by_result": {"RESULT-1": ["WO-FROM-BATCH-1"]},
						"work_order_snapshots": {},
					},
				)

	def test_work_order_apply_blocks_state_changed_after_review(self):
		result_doc = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			machine_scheduled_qty=120,
		)
		current_snapshot = {
			"name": "WO-1",
			"company": "COMPANY-1",
			"production_item": "ITEM-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"qty": 100,
			"produced_qty": 10,
			"material_transferred_for_manufacturing": 10,
			"docstatus": 1,
			"status": "In Process",
			"modified": "2026-08-11 11:00:00",
			"has_execution": True,
			"scheduling_rows": [],
		}
		row = frappe._dict(
			action="Update Existing",
			result_reference="RESULT-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			proposed_qty=120,
			existing_qty=100,
			existing_work_order="WO-1",
			existing_state_token="token-before-material-transfer",
			result_state_token=planning._work_order_result_proposal_state_token(result_doc, []),
		)
		with (
			patch.object(planning, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "changed after proposal review"):
				planning._validate_work_order_proposal_row_current(
					row=row,
					batch=frappe._dict(name="BATCH-2", planning_run="RUN-1"),
					run_doc=frappe._dict(name="RUN-1", company="COMPANY-1"),
					result_doc=result_doc,
					primary_segments=[],
					apply_state={
						"open_by_result": {"RESULT-1": ["WO-1"]},
						"work_order_snapshots": {"WO-1": current_snapshot},
					},
				)

	def test_work_order_apply_blocks_same_quantity_with_changed_machine_times(self):
		result_doc = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			item_code="ITEM-1",
			demand_source="Stock Production",
			machine_scheduled_qty=100,
			requested_date="2026-08-12",
			modified="2026-08-11 08:00:00",
		)
		before_segment = {
			"name": "SEG-1",
			"parent": "RESULT-1",
			"workstation": "MACHINE-1",
			"start_time": "2026-08-11 08:00:00",
			"end_time": "2026-08-11 10:00:00",
			"planned_qty": 100,
			"segment_status": "Work Order Proposed",
			"modified": "2026-08-11 08:00:00",
		}
		after_segment = {
			**before_segment,
			"start_time": "2026-08-11 09:00:00",
			"end_time": "2026-08-11 11:00:00",
		}
		row = frappe._dict(
			action="New",
			result_reference="RESULT-1",
			item_code="ITEM-1",
			proposed_qty=100,
			result_state_token=planning._work_order_result_proposal_state_token(
				result_doc,
				[before_segment],
			),
		)
		with (
			patch.object(planning, "_", side_effect=lambda message, **_kwargs: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "machine schedule changed"):
				planning._validate_work_order_proposal_row_current(
					row=row,
					batch=frappe._dict(name="BATCH-1", planning_run="RUN-1"),
					run_doc=frappe._dict(name="RUN-1", company="COMPANY-1"),
					result_doc=result_doc,
					primary_segments=[after_segment],
					apply_state={"open_by_result": {}, "work_order_snapshots": {}},
				)

	def test_shift_segment_token_ignores_only_expected_workflow_status_change(self):
		segment = {
			"name": "SEG-1",
			"parent": "RESULT-1",
			"workstation": "MACHINE-1",
			"plant_floor": "FLOOR-1",
			"start_time": "2026-08-11 08:00:00",
			"end_time": "2026-08-11 10:00:00",
			"planned_qty": 100,
			"segment_status": "Work Order Proposed",
			"modified": "2026-08-11 08:00:00",
		}
		before = planning._segment_proposal_state_token(segment)
		self.assertEqual(
			before,
			planning._segment_proposal_state_token({**segment, "segment_status": "Shift Proposed"}),
		)
		self.assertNotEqual(
			before,
			planning._segment_proposal_state_token({**segment, "planned_qty": 101}),
		)

	def test_create_delta_blocks_when_current_open_quantity_changed(self):
		result_doc = frappe._dict(
			name="RESULT-1",
			planning_run="RUN-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			machine_scheduled_qty=120,
		)
		existing = {
			"name": "WO-BASE",
			"company": "COMPANY-1",
			"production_item": "ITEM-1",
			"sales_order": "SO-1",
			"sales_order_item": "SOI-1",
			"qty": 100,
			"produced_qty": 10,
			"material_transferred_for_manufacturing": 10,
			"docstatus": 1,
			"status": "In Process",
			"modified": "2026-08-11 10:00:00",
			"has_execution": True,
			"scheduling_rows": [],
		}
		other_delta = {
			"name": "WO-FIRST-DELTA",
			"qty": 10,
			"docstatus": 1,
			"status": "Not Started",
			"scheduling_rows": [],
		}
		row = frappe._dict(
			action="Create Delta",
			result_reference="RESULT-1",
			item_code="ITEM-1",
			customer="CUSTOMER-1",
			sales_order="SO-1",
			sales_order_item="SOI-1",
			proposed_qty=120,
			existing_qty=100,
			existing_work_order="WO-BASE",
			existing_state_token=planning._work_order_proposal_state_token(existing),
			result_state_token=planning._work_order_result_proposal_state_token(result_doc, []),
		)
		with (
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "Delta quantity.*changed"):
				planning._validate_work_order_proposal_row_current(
					row=row,
					batch=frappe._dict(name="BATCH-2", planning_run="RUN-1"),
					run_doc=frappe._dict(name="RUN-1", company="COMPANY-1"),
					result_doc=result_doc,
					primary_segments=[],
					apply_state={
						"open_by_result": {"RESULT-1": ["WO-BASE", "WO-FIRST-DELTA"]},
						"work_order_snapshots": {
							"WO-BASE": existing,
							"WO-FIRST-DELTA": other_delta,
						},
					},
				)

	def test_shift_apply_blocks_work_order_changed_by_first_batch(self):
		current_snapshot = {
			"name": "WO-1",
			"company": "COMPANY-1",
			"production_item": "ITEM-1",
			"qty": 100,
			"docstatus": 1,
			"status": "Not Started",
			"modified": "2026-08-11 11:00:00",
			"scheduling_rows": [
				{
					"name": "SI-FROM-BATCH-1",
					"work_order_scheduling": "WOS-1",
					"custom_aps_segment_reference": "SEG-1",
					"planned_start_date": "2026-08-12 08:00:00",
					"planned_end_date": "2026-08-12 10:00:00",
				}
			],
		}
		row = frappe._dict(
			action="New",
			result_reference="RESULT-1",
			segment_reference="SEG-1",
			item_code="ITEM-1",
			work_order="WO-1",
			work_order_state_token="token-before-first-batch",
		)
		with (
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "changed after shift proposal review"):
				planning._validate_shift_proposal_row_current(
					row=row,
					batch=frappe._dict(name="SHIFT-BATCH-2", company="COMPANY-1", plant_floor="PF-1"),
					apply_state={"work_order_snapshots": {"WO-1": current_snapshot}},
				)

	def test_shift_apply_scope_locks_targets_in_deterministic_order(self):
		database = MagicMock()
		row = frappe._dict(
			result_reference="RESULT-1",
			segment_reference="SEG-1",
			work_order="WO-1",
			existing_scheduling="WOS-OLD",
			existing_scheduling_item="SI-1",
		)
		snapshot = {
			"scheduling_rows": [
				{"name": "SI-1", "work_order_scheduling": "WOS-OLD"},
			]
		}
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning, "_get_shift_target_wos_names", return_value={"WOS-TARGET"}),
			patch.object(planning, "_get_work_order_reconciliation_snapshot", return_value=snapshot),
			patch.object(planning, "_get_segment_proposal_snapshot", return_value={"name": "SEG-1"}),
		):
			planning._prepare_shift_apply_state(
				frappe._dict(company="COMPANY-1", plant_floor="PF-1"),
				[row],
			)
		locked_tables = [
			call.args[0].split("`")[1]
			for call in database.sql.call_args_list
			if "for update" in call.args[0].lower()
		]
		self.assertEqual(
			locked_tables,
			[
				"tabAPS Schedule Result",
				"tabAPS Schedule Segment",
				"tabWork Order",
				"tabWork Order Scheduling",
				"tabScheduling Item",
			],
		)

	def test_import_locks_customer_and_blocks_changed_active_state(self):
		database = MagicMock()
		database.sql.return_value = [("CUSTOMER-1",)]
		initial_preview = {"active_state_token": "state-before"}
		locked_preview = {
			"active_state_token": "state-after",
			"is_idempotent_replay": 0,
			"can_import": 1,
		}
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="importlock1"),
			patch.object(planning, "preview_customer_delivery_schedule", side_effect=[initial_preview, locked_preview]),
			patch.object(planning, "_apply_customer_delivery_schedule_import") as apply_import,
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "changed after preview"):
				planning.import_customer_delivery_schedule(
					customer="CUSTOMER-1",
					company="COMPANY-1",
					version_no="V1",
					active_state_token="state-before",
				)
		lock_sql = database.sql.call_args.args[0].lower()
		self.assertIn("tabcustomer", lock_sql)
		self.assertIn("for update", lock_sql)
		apply_import.assert_not_called()
		database.rollback.assert_called_once_with(save_point="aps_schedule_import_importlock1")

	def test_active_state_token_tracks_execution_qty_and_normalizes_blank_scope(self):
		live_delivered_qty = {"value": 2}
		audit_fields = {
			"remark": None,
			"source_origin": None,
			"source_excel_row": None,
			"source_excel_rows": None,
			"manual_override": None,
			"manual_change_reason": None,
		}
		def get_all(doctype, **kwargs):
			if doctype == "Customer Delivery Schedule":
				self.assertEqual(kwargs["filters"]["schedule_scope"], "Default Scope")
				return [frappe._dict(name="SCHED-1", modified="2026-08-11 10:00:00")]
			return [
				frappe._dict(
					name="ROW-1",
					parent="SCHED-1",
					idx=1,
					sales_order="SO-1",
					item_code="ITEM-1",
					customer_part_no="PART-1",
					schedule_date="2026-08-12",
					qty=10,
					allocated_qty=5,
					produced_qty=4,
					# Deliberately stale async cache: the physical-source helper below
					# must be authoritative in both rows and concurrency token.
					delivered_qty=99,
					balance_qty=0,
					status="Open",
					**audit_fields,
				)
			]
		with (
			patch.object(planning.frappe, "get_all", side_effect=get_all),
			patch(
				"injection_aps.services.delivery_sync.get_schedule_delivery_lower_bounds",
				side_effect=lambda **_kwargs: {"ROW-1": live_delivered_qty["value"]},
			),
		):
			before = planning._get_active_schedule_snapshot("CUSTOMER-1", "COMPANY-1", None)
			live_delivered_qty["value"] = 3
			after = planning._get_active_schedule_snapshot("CUSTOMER-1", "COMPANY-1", "")
		self.assertNotEqual(before["token"], after["token"])
		self.assertEqual(before["rows"][0]["delivered_qty"], 2)
		self.assertEqual(after["rows"][0]["delivered_qty"], 3)

		# None and blank are canonicalized to the same state, while every audit
		# field that can be replaced by an import participates in the token.
		with (
			patch.object(planning.frappe, "get_all", side_effect=get_all),
			patch(
				"injection_aps.services.delivery_sync.get_schedule_delivery_lower_bounds",
				side_effect=lambda **_kwargs: {"ROW-1": live_delivered_qty["value"]},
			),
		):
			live_delivered_qty["value"] = 2
			empty_baseline = planning._get_active_schedule_snapshot("CUSTOMER-1", "COMPANY-1", "")
			for fieldname in audit_fields:
				audit_fields[fieldname] = "" if fieldname not in {"source_excel_row", "manual_override"} else 0
			blank_baseline = planning._get_active_schedule_snapshot("CUSTOMER-1", "COMPANY-1", "")
			self.assertEqual(empty_baseline["token"], blank_baseline["token"])
			changed_values = {
				"remark": "urgent",
				"source_origin": "manual",
				"source_excel_row": 7,
				"source_excel_rows": "7, 8",
				"manual_override": 1,
				"manual_change_reason": "customer changed plan",
			}
			for fieldname, changed_value in changed_values.items():
				original = audit_fields[fieldname]
				audit_fields[fieldname] = changed_value
				changed = planning._get_active_schedule_snapshot("CUSTOMER-1", "COMPANY-1", "")
				self.assertNotEqual(blank_baseline["token"], changed["token"], fieldname)
				audit_fields[fieldname] = original

	def test_import_replay_wins_after_lock_even_if_active_state_changed(self):
		database = MagicMock()
		database.sql.return_value = [("CUSTOMER-1",)]
		initial_preview = {"active_state_token": "state-before"}
		locked_preview = {
			"active_state_token": "state-after",
			"is_idempotent_replay": 1,
			"existing_import": {"import_batch": "IMP-1", "schedule": "SCHED-1"},
			"summary": {"Unchanged": 1},
		}
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="importlock2"),
			patch.object(planning, "preview_customer_delivery_schedule", side_effect=[initial_preview, locked_preview]),
			patch.object(planning, "_apply_customer_delivery_schedule_import") as apply_import,
		):
			result = planning.import_customer_delivery_schedule(
				customer="CUSTOMER-1",
				company="COMPANY-1",
				version_no="V1",
				active_state_token="state-before",
			)
		self.assertEqual(result["idempotent_replay"], 1)
		self.assertEqual(result["schedule"], "SCHED-1")
		apply_import.assert_not_called()
		database.release_savepoint.assert_called_once_with("aps_schedule_import_importlock2")
		database.rollback.assert_not_called()

	def test_import_blocks_source_rows_changed_after_user_preview(self):
		database = MagicMock()
		database.sql.return_value = [("CUSTOMER-1",)]
		initial_preview = {
			"active_state_token": "state-same",
			"import_fingerprint": "fingerprint-new-content",
		}
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="sourcechange"),
			patch.object(planning, "preview_customer_delivery_schedule", return_value=initial_preview) as preview,
			patch.object(planning, "_apply_customer_delivery_schedule_import") as apply_import,
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "source changed after preview"):
				planning.import_customer_delivery_schedule(
					customer="CUSTOMER-1",
					company="COMPANY-1",
					version_no="V1",
					active_state_token="state-same",
					expected_import_fingerprint="fingerprint-user-confirmed",
				)
		apply_import.assert_not_called()
		database.rollback.assert_called_once_with(save_point="aps_schedule_import_sourcechange")
		preview.assert_called_once()

	def test_strict_console_import_previews_only_after_customer_lock(self):
		database = MagicMock()
		database.sql.return_value = [("CUSTOMER-1",)]
		def locked_preview(**_kwargs):
			self.assertTrue(database.sql.called)
			return {
				"active_state_token": "state-confirmed",
				"import_fingerprint": "fingerprint-confirmed",
				"is_idempotent_replay": 0,
				"can_import": 1,
				"schedule_scope": "DAILY",
				"import_strategy": "Replace Scope",
				"duplicate_policy": "Block",
				"version_no": "V1",
			}
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="strictlock"),
			patch.object(planning, "preview_customer_delivery_schedule", side_effect=locked_preview) as preview,
			patch.object(
				planning,
				"_apply_customer_delivery_schedule_import",
				return_value={"schedule": "SCHED-1"},
			) as apply_import,
		):
			result = planning.import_customer_delivery_schedule(
				customer="CUSTOMER-1",
				company="COMPANY-1",
				version_no="V1",
				active_state_token="state-confirmed",
				expected_import_fingerprint="fingerprint-confirmed",
			)
		self.assertEqual(result["schedule"], "SCHED-1")
		preview.assert_called_once()
		apply_import.assert_called_once()

	def test_schedule_console_import_sends_confirmed_rows_snapshot(self):
		console_path = os.path.join(
			os.path.dirname(os.path.dirname(__file__)),
			"injection_aps",
			"page",
			"aps_schedule_console",
			"aps_schedule_console.js",
		)
		with open(console_path, encoding="utf-8") as source_file:
			source = source_file.read()
		self.assertIn("rows_json: JSON.stringify(confirmedRows)", source)
		self.assertIn("expected_import_fingerprint: this.pendingImport.preview.import_fingerprint", source)

	def test_preview_applies_payload_limit_to_expanded_console_snapshot(self):
		raw_row = {"item_code": "ITEM-1", "schedule_date": "2026-08-12", "qty": 1}
		snapshot_rows = planning._build_schedule_source_snapshot_rows([raw_row])
		raw_size = 2 + planning._schedule_row_payload_size(raw_row, index=1)
		snapshot_size = 2 + planning._schedule_row_payload_size(snapshot_rows[0], index=1)
		self.assertGreater(snapshot_size, raw_size)
		with (
			patch.object(planning, "MAX_SCHEDULE_ROWS_JSON_BYTES", raw_size),
			patch.object(planning, "_normalize_schedule_rows", return_value=([raw_row], {})),
			patch.object(planning, "_prepare_schedule_rows_for_import", return_value=[raw_row]),
			patch.object(
				planning,
				"_get_active_schedule_snapshot",
				return_value={"rows": [], "token": "active-token"},
			),
			patch.object(planning, "_validate_schedule_import_rows", return_value=[]),
			patch.object(planning, "_resolve_schedule_row_duplicates", return_value=([raw_row], [])),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(
				planning.frappe,
				"throw",
				side_effect=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
					frappe.ValidationError(message)
				),
			),
		):
			# The lean normalized row fits, but Preview must reject the larger exact
			# row shape that the browser would send back on formal Import.
			planning._validate_schedule_rows_payload([raw_row])
			with self.assertRaisesRegex(frappe.ValidationError, "10 MB safety limit"):
				planning.preview_customer_delivery_schedule(
					customer="CUSTOMER-1",
					company="COMPANY-1",
					version_no="V1",
				)

	def test_schedule_item_remap_handles_exact_and_explicit_date_move(self):
		previous = [
			{
				"name": "OLD-EXACT",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			},
			{
				"name": "OLD-MOVED",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-2",
				"customer_part_no": "PART-2",
				"schedule_date": "2026-08-13",
			},
		]
		current = [
			{**previous[0], "name": "NEW-EXACT", "parent": "SCHED-NEW"},
			{
				**previous[1],
				"name": "NEW-MOVED",
				"parent": "SCHED-NEW",
				"schedule_date": "2026-08-15",
			},
		]
		diff = [
			{**current[0], "previous_schedule_date": "2026-08-12"},
			{**current[1], "previous_schedule_date": "2026-08-13"},
		]
		remap = planning._build_schedule_item_remap(previous, current, diff)
		self.assertEqual(remap["OLD-EXACT"]["customer_schedule_item"], "NEW-EXACT")
		self.assertEqual(remap["OLD-MOVED"]["customer_schedule_item"], "NEW-MOVED")
		self.assertEqual(remap["OLD-MOVED"]["schedule_date"], "2026-08-15")

	def test_explicit_previous_dates_control_crossing_moves_and_delivery_lower_bound(self):
		previous = [
			{
				"name": "OLD-12",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
				"qty": 10,
				"delivered_qty": 9,
			},
			{
				"name": "OLD-13",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-13",
				"qty": 20,
				"delivered_qty": 0,
			},
		]
		incoming = [
			{
				**previous[0],
				"name": None,
				"parent": None,
				"previous_schedule_date": "2026-08-12",
				"schedule_date": "2026-08-13",
				"qty": 5,
			},
			{
				**previous[1],
				"name": None,
				"parent": None,
				"previous_schedule_date": "2026-08-13",
				"schedule_date": "2026-08-12",
				"qty": 20,
			},
		]
		with (
			patch.object(planning, "_prime_item_resolution_cache"),
			patch.object(planning, "_resolve_item_name", side_effect=lambda item: item),
		):
			plan = planning._build_schedule_import_plan(
				previous_rows=previous,
				incoming_rows=incoming,
				import_strategy="Partial Update",
			)
		diff_by_previous_date = {
			str(row.get("previous_schedule_date")): row for row in plan["diff_rows"]
		}
		self.assertEqual(str(diff_by_previous_date["2026-08-12"]["schedule_date"]), "2026-08-13")
		self.assertEqual(diff_by_previous_date["2026-08-12"]["delivered_qty"], 9)
		self.assertEqual(diff_by_previous_date["2026-08-12"]["new_qty"], 5)
		lower_bound_rows = [
			row
			for row in plan["diff_rows"]
			if planning.flt(row.get("new_qty")) < planning.flt(row.get("delivered_qty"))
		]
		self.assertEqual([str(row.get("previous_schedule_date")) for row in lower_bound_rows], ["2026-08-12"])

		current = [
			{**row, "name": f"NEW-{row['schedule_date']}", "parent": "SCHED-NEW"}
			for row in plan["effective_schedule_rows"]
		]
		remap = planning._build_schedule_item_remap(previous, current, plan["diff_rows"])
		self.assertEqual(remap["OLD-12"]["customer_schedule_item"], "NEW-2026-08-13")
		self.assertEqual(remap["OLD-13"]["customer_schedule_item"], "NEW-2026-08-12")

	def test_partial_date_move_cannot_overwrite_an_unmoved_destination(self):
		previous = [
			{
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			},
			{
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-13",
			},
		]
		incoming = [
			{
				**previous[0],
				"previous_schedule_date": "2026-08-12",
				"schedule_date": "2026-08-13",
				"source_excel_row": 8,
			}
		]
		ambiguities = planning._find_partial_update_ambiguities(previous, incoming)
		self.assertEqual(len(ambiguities), 1)
		self.assertEqual(ambiguities[0]["excel_rows"], [8])

	def test_sales_order_must_match_customer_company_and_item(self):
		rows = [
			{
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"source_excel_row": 7,
			}
		]
		database = MagicMock()
		database.exists.return_value = True
		def get_all(doctype, **_kwargs):
			if doctype == "Sales Order":
				return [
					frappe._dict(
						name="SO-1", customer="OTHER-CUSTOMER", company="COMPANY-1", docstatus=1
					)
				]
			if doctype == "Sales Order Item":
				return [frappe._dict(parent="SO-1", item_code="ITEM-2")]
			return []
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_all", side_effect=get_all) as get_all_mock,
			patch.object(planning, "_", side_effect=lambda message: message),
		):
			issues = planning._validate_schedule_sales_order_ownership(
				rows, customer="CUSTOMER-1", company="COMPANY-1"
			)
		self.assertEqual(len(issues), 2)
		self.assertTrue(all(issue["excel_rows"] == [7] for issue in issues))
		self.assertEqual(get_all_mock.call_count, 2)
		database.get_value.assert_not_called()

	def test_item_validation_batches_duplicate_rows_without_per_row_exists(self):
		rows = [
			{"item_code": "ITEM-1", "schedule_date": "2026-08-12", "qty": 1},
			{"item_code": "ITEM-1", "schedule_date": "2026-08-13", "qty": 2},
			{"item_code": "ITEM-2", "schedule_date": "2026-08-14", "qty": 3},
		]
		database = MagicMock()
		database.exists.return_value = True
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_all", return_value=["ITEM-1", "ITEM-2"]) as get_all_mock,
		):
			issues = planning._validate_schedule_import_rows(rows, customer="CUSTOMER-1", company="COMPANY-1")
		self.assertEqual(issues, [])
		self.assertEqual(get_all_mock.call_count, 1)
		self.assertEqual(database.exists.call_count, 1)

	def test_item_resolution_primes_unique_references_in_batches(self):
		database = MagicMock()
		database.exists.return_value = True
		def get_all(_doctype, **kwargs):
			if kwargs.get("pluck") == "name":
				return ["ITEM-1"]
			fields = kwargs.get("fields") or []
			if "item_code" in fields:
				return [frappe._dict(name="ITEM-2", item_code="ALIAS-2")]
			return []
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_all", side_effect=get_all) as get_all_mock,
			patch.object(
				planning.frappe.local,
				"injection_aps_item_resolution_cache",
				{},
				create=True,
			),
		):
			planning._prime_item_resolution_cache(["ITEM-1"] * 50 + ["ALIAS-2"] * 50)
			self.assertEqual(planning._resolve_item_name("ITEM-1"), "ITEM-1")
			self.assertEqual(planning._resolve_item_name("ALIAS-2"), "ITEM-2")
		self.assertEqual(database.exists.call_count, 1)
		self.assertEqual(get_all_mock.call_count, 2)

	def test_sales_order_validation_batches_multiple_orders(self):
		rows = [
			{"sales_order": "SO-1", "item_code": "ITEM-1", "source_excel_row": 4},
			{"sales_order": "SO-1", "item_code": "ITEM-1", "source_excel_row": 5},
			{"sales_order": "SO-2", "item_code": "ITEM-2", "source_excel_row": 6},
		]
		database = MagicMock()
		database.exists.return_value = True
		def get_all(doctype, **_kwargs):
			if doctype == "Sales Order":
				return [
					frappe._dict(name="SO-1", customer="CUSTOMER-1", company="COMPANY-1", docstatus=1),
					frappe._dict(name="SO-2", customer="CUSTOMER-1", company="COMPANY-1", docstatus=1),
				]
			if doctype == "Sales Order Item":
				return [
					frappe._dict(parent="SO-1", item_code="ITEM-1"),
					frappe._dict(parent="SO-2", item_code="ITEM-2"),
				]
			return []
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_all", side_effect=get_all) as get_all_mock,
		):
			issues = planning._validate_schedule_sales_order_ownership(
				rows, customer="CUSTOMER-1", company="COMPANY-1"
			)
		self.assertEqual(issues, [])
		self.assertEqual(get_all_mock.call_count, 2)
		database.get_value.assert_not_called()

	def test_partial_update_preserves_existing_production_policy_when_omitted(self):
		previous = [
			{
				"sales_order": "",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
				"qty": 100,
				"production_strategy": "Force JIT",
				"demand_confidence": "Forecast",
				"cancellation_risk_percent": 80,
				"prebuild_allowed": 0,
				"max_prebuild_days": 0,
			}
		]
		incoming = [{**previous[0], "qty": 120}]
		for fieldname in planning.SCHEDULE_POLICY_FIELDS:
			incoming[0].pop(fieldname, None)
		with (
			patch.object(planning, "_prime_item_resolution_cache"),
			patch.object(planning, "_resolve_item_name", return_value="ITEM-1"),
		):
			plan = planning._build_schedule_import_plan(
				previous_rows=previous,
				incoming_rows=incoming,
				import_strategy="Partial Update",
			)
		row = plan["effective_schedule_rows"][0]
		self.assertEqual(row["production_strategy"], "Force JIT")
		self.assertEqual(row["demand_confidence"], "Forecast")
		self.assertEqual(row["cancellation_risk_percent"], 80)
		self.assertEqual(row["prebuild_allowed"], 0)

	def test_production_policy_changes_are_part_of_import_fingerprint(self):
		base = {
			"item_code": "ITEM-1",
			"schedule_date": "2026-08-12",
			"qty": 100,
		}
		common = {
			"customer": "CUSTOMER-1",
			"company": "COMPANY-1",
			"version_no": "V1",
			"schedule_scope": "DAILY",
			"import_strategy": "Replace Scope",
			"source_type": "Customer Delivery Schedule",
		}
		auto = planning._build_schedule_import_fingerprint(
			**common, rows=[{**base, "production_strategy": "Auto Balance"}]
		)
		jit = planning._build_schedule_import_fingerprint(
			**common, rows=[{**base, "production_strategy": "Force JIT"}]
		)
		self.assertNotEqual(auto, jit)

	def test_import_fingerprint_is_idempotent_per_normalized_version(self):
		common = {
			"customer": "CUSTOMER-1",
			"company": "COMPANY-1",
			"schedule_scope": "DAILY",
			"import_strategy": "Replace Scope",
			"source_type": "Customer Delivery Schedule",
			"rows": [{"item_code": "ITEM-1", "schedule_date": "2026-08-12", "qty": 100}],
		}
		v1 = planning._build_schedule_import_fingerprint(**common, version_no=" V1 ")
		v1_replay = planning._build_schedule_import_fingerprint(**common, version_no="V1")
		v2 = planning._build_schedule_import_fingerprint(**common, version_no="V2")
		empty = planning._build_schedule_import_fingerprint(**common, version_no=None)
		empty_again = planning._build_schedule_import_fingerprint(**common, version_no=" ")
		self.assertEqual(v1, v1_replay)
		self.assertNotEqual(v1, v2)
		self.assertEqual(empty, empty_again)

	def test_string_rows_json_is_subject_to_row_limit(self):
		payload = json.dumps([{"item_code": "ITEM-1"}, {"item_code": "ITEM-2"}])
		with (
			patch.object(planning, "MAX_SCHEDULE_WORKSHEET_ROWS", 1),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				planning._normalize_schedule_rows(rows_json=payload)

	def test_parsed_and_string_rows_share_payload_limit(self):
		rows = [{"item_code": "ITEM-1", "remark": "界" * 20}]
		with (
			patch.object(planning, "MAX_SCHEDULE_ROWS_JSON_BYTES", 48),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			for payload in (rows, json.dumps(rows, ensure_ascii=False)):
				with self.subTest(payload_type=type(payload).__name__):
					with self.assertRaises(frappe.ValidationError):
						planning._normalize_schedule_rows(rows_json=payload)

	def test_schedule_rows_reject_nested_or_non_scalar_values(self):
		payloads = (
			[{"item_code": "ITEM-1", "remark": {"nested": "value"}}],
			json.dumps([{"item_code": "ITEM-1", "remark": {"nested": "value"}}]),
		)
		with (
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			for payload in payloads:
				with self.subTest(payload_type=type(payload).__name__):
					with self.assertRaises(frappe.ValidationError):
						planning._normalize_schedule_rows(rows_json=payload)

	def test_mapping_json_has_an_independent_payload_limit(self):
		with (
			patch.object(planning, "MAX_SCHEDULE_MAPPING_JSON_BYTES", 16),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				planning._normalize_schedule_mapping(json.dumps({"sheet_name": "X" * 32}))

	def test_schedule_mapping_rejects_nested_values_and_excess_fields(self):
		with (
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			for payload in (
				{"sheet_name": {"nested": "value"}},
				json.dumps({"sheet_name": {"nested": {"too": "deep"}}}),
			):
				with self.subTest(payload_type=type(payload).__name__):
					with self.assertRaises(frappe.ValidationError):
						planning._normalize_schedule_mapping(payload)
			with patch.object(planning, "MAX_SCHEDULE_MAPPING_FIELDS", 1):
				with self.assertRaises(frappe.ValidationError):
					planning._normalize_schedule_mapping({"parser_mode": "matrix", "sheet_name": "Sheet1"})

	def test_schedule_mapping_keeps_only_recognized_scalar_fields(self):
		mapping = planning._normalize_schedule_mapping(
			{
				"parser_mode": "matrix",
				"sheet_name": "Sheet1",
				"item_reference_column": "A",
				"unused_client_metadata": "ignored",
			}
		)
		self.assertEqual(
			mapping,
			{"parser_mode": "matrix", "sheet_name": "Sheet1", "item_reference_column": "A"},
		)

	def test_schedule_reduction_below_delivered_quantity_is_blocking(self):
		row = {
			"item_code": "ITEM-1",
			"schedule_date": "2026-08-12",
			"new_qty": 0,
			"delivered_qty": 25,
			"source_excel_row": 9,
		}
		with patch.object(planning, "_", side_effect=lambda message, **_kwargs: message):
			checks = planning._build_schedule_import_checks(
				import_strategy="Partial Update",
				duplicate_policy="Block",
				duplicate_groups=[],
				row_issues=[],
				append_zero_rows=[],
				partial_ambiguities=[],
				replay=None,
				delivery_lower_bound_rows=[row],
			)
		check = next(check for check in checks if check["title"] == "Delivered quantity lower bound")
		self.assertEqual(check["status"], "failed")
		self.assertEqual(check["blocking"], 1)
		self.assertIn("Excel rows 9", check["details"][0])

	def test_cancelled_omitted_row_is_not_silently_remapped(self):
		previous = [
			{
				"name": "OLD-CANCELLED",
				"parent": "SCHED-OLD",
				"sales_order": "",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			}
		]
		remap = planning._build_schedule_item_remap(previous, [], [])
		self.assertIn("OLD-CANCELLED", remap)
		self.assertIsNone(remap["OLD-CANCELLED"]["customer_schedule_item"])

	def test_zero_quantity_partial_cancellation_is_not_an_execution_target(self):
		previous = [
			{
				"name": "OLD-CANCELLED",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
				"qty": 10,
			}
		]
		current = [
			{
				**previous[0],
				"name": "NEW-CANCELLED",
				"parent": "SCHED-NEW",
				"qty": 0,
				"status": "Cancelled",
			}
		]
		remap = planning._build_schedule_item_remap(
			previous,
			current,
			[{**current[0], "previous_schedule_date": "2026-08-12"}],
		)
		self.assertEqual(
			remap["OLD-CANCELLED"],
			{
				"customer_schedule_item": None,
				"customer_schedule": None,
				"schedule_date": None,
			},
		)

	def test_all_affected_runs_are_rebuilt_and_sync_failure_propagates(self):
		previous = [
			{
				"name": "OLD-1",
				"parent": "SCHED-OLD",
				"sales_order": "",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			}
		]
		current = [{**previous[0], "name": "NEW-1", "parent": "SCHED-NEW"}]
		database = MagicMock()
		database.exists.side_effect = (
			lambda doctype, name=None, *_args, **_kwargs: doctype == "DocType" and name == "APS Production Allocation"
		)
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(
				planning.frappe,
				"get_all",
				return_value=[
					{"name": "ALLOC-1", "planning_run": "RUN-1"},
					{"name": "ALLOC-2", "planning_run": "RUN-2"},
				],
			),
			patch(
				"injection_aps.services.execution_sync.sync_production_for_run",
				side_effect=[{"run": "RUN-1"}, RuntimeError("RUN-2 sync failed")],
			) as production_sync,
		):
			with self.assertRaisesRegex(RuntimeError, "RUN-2 sync failed"):
				planning._rebuild_schedule_execution_allocations(
					previous_item_rows=previous,
					new_item_rows=current,
					diff_rows=[{**current[0], "previous_schedule_date": "2026-08-12"}],
				)
		self.assertEqual(production_sync.call_count, 2)

	def test_result_baseline_adds_run_to_production_resync_before_ledger_exists(self):
		result = frappe._dict(
			name="RESULT-BASELINE",
			planning_run="RUN-BASELINE",
			fulfillment_baseline_json={
				"version": 2,
				"targets": [
					{
						"customer_schedule": "SCHED-OLD",
						"customer_schedule_item": "OLD-1",
						"schedule_date": "2026-08-12",
						"opening_required_qty": 100,
					}
				],
			},
		)
		with (
			patch.object(planning.frappe, "get_all", return_value=[result]),
			patch.object(planning.frappe.db, "set_value") as set_value,
		):
			runs = planning._remap_result_fulfillment_baselines(
				{
					"OLD-1": {
						"customer_schedule_item": "NEW-1",
						"customer_schedule": "SCHED-NEW",
						"schedule_date": "2026-08-12",
					}
				},
				new_item_rows=[{"name": "NEW-1", "qty": 100}],
				company="COMPANY-1",
				customer="CUSTOMER-1",
				item_codes=["ITEM-1"],
			)
		self.assertEqual(runs, ["RUN-BASELINE"])
		self.assertEqual(set_value.call_args.args[:3], ("APS Schedule Result", "RESULT-BASELINE", "fulfillment_baseline_json"))

	def test_full_cancellation_clears_direct_links_and_uses_previous_parent_scope(self):
		previous = [
			{
				"name": "OLD-1",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			}
		]
		database = MagicMock()
		database.exists.side_effect = lambda doctype, name=None, *_args, **_kwargs: (
			(doctype == "DocType" and name in {"APS Delivery Allocation", "Delivery Note Item"})
		)
		database.get_value.return_value = frappe._dict(company="COMPANY-1", customer="CUSTOMER-1")
		def get_all(doctype, **_kwargs):
			if doctype == "APS Delivery Allocation":
				return [{"name": "DEL-ALLOC-1", "source_delivery_note_item": "DNI-1"}]
			if doctype == "Delivery Note Item":
				return [{"name": "DNI-1", "custom_aps_customer_schedule_item": "OLD-1"}]
			return []
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_all", side_effect=get_all),
			patch.object(planning.frappe, "get_meta", return_value=meta),
			patch("injection_aps.services.delivery_sync.sync_delivery_allocations", return_value={}) as delivery_sync,
		):
			planning._rebuild_schedule_execution_allocations(
				previous_item_rows=previous,
				new_item_rows=[],
				diff_rows=[],
			)
		database.set_value.assert_called_once_with(
			"Delivery Note Item",
			"DNI-1",
			"custom_aps_customer_schedule_item",
			None,
			update_modified=False,
		)
		delivery_sync.assert_called_once_with(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_codes=["ITEM-1"],
			target_remap={"OLD-1": None},
		)

	def test_schedule_replacement_moves_an_existing_direct_delivery_link(self):
		previous = [
			{
				"name": "OLD-1",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			}
		]
		current = [{**previous[0], "name": "NEW-1", "parent": "SCHED-NEW", "schedule_date": "2026-08-13"}]
		database = MagicMock()
		database.exists.side_effect = lambda doctype, name=None, *_args, **_kwargs: (
			doctype == "DocType" and name == "Delivery Note Item"
		)
		database.get_value.return_value = frappe._dict(company="COMPANY-1", customer="CUSTOMER-1")

		def get_all(doctype, **_kwargs):
			if doctype == "Delivery Note Item":
				return [{"name": "DNI-DIRECT", "custom_aps_customer_schedule_item": "OLD-1"}]
			return []

		meta = MagicMock()
		meta.has_field.return_value = True
		events = []
		database.set_value.side_effect = lambda *_args, **_kwargs: events.append("direct-link-updated")
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_all", side_effect=get_all),
			patch.object(planning.frappe, "get_meta", return_value=meta),
			patch(
				"injection_aps.services.delivery_sync.sync_delivery_allocations",
				side_effect=lambda **_kwargs: events.append("delivery-sync") or {},
			) as delivery_sync,
		):
			planning._rebuild_schedule_execution_allocations(
				previous_item_rows=previous,
				new_item_rows=current,
				diff_rows=[{**current[0], "previous_schedule_date": "2026-08-12"}],
			)

		database.set_value.assert_called_once_with(
			"Delivery Note Item",
			"DNI-DIRECT",
			"custom_aps_customer_schedule_item",
			"NEW-1",
			update_modified=False,
		)
		self.assertEqual(events, ["delivery-sync", "direct-link-updated"])
		delivery_sync.assert_called_once_with(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_codes=["ITEM-1"],
			target_remap={
				"OLD-1": {
					"customer_schedule_item": "NEW-1",
					"customer_schedule": "SCHED-NEW",
					"schedule_date": "2026-08-13",
				}
			},
		)

	def test_fifo_split_delivery_uses_ledger_remap_without_writing_a_direct_link(self):
		previous = [
			{
				"name": "OLD-1",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			}
		]
		current = [{**previous[0], "name": "NEW-1", "parent": "SCHED-NEW"}]
		database = MagicMock()
		database.exists.side_effect = lambda doctype, name=None, *_args, **_kwargs: (
			doctype == "DocType" and name in {"APS Delivery Allocation", "Delivery Note Item"}
		)
		database.get_value.return_value = frappe._dict(company="COMPANY-1", customer="CUSTOMER-1")

		def get_all(doctype, **_kwargs):
			if doctype == "APS Delivery Allocation":
				return [{"name": "ALLOC-FIFO", "source_delivery_note_item": "DNI-SPLIT"}]
			if doctype == "Delivery Note Item":
				return []
			return []

		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "get_all", side_effect=get_all),
			patch.object(planning.frappe, "get_meta", return_value=meta),
			patch("injection_aps.services.delivery_sync.sync_delivery_allocations", return_value={}) as delivery_sync,
		):
			planning._rebuild_schedule_execution_allocations(
				previous_item_rows=previous,
				new_item_rows=current,
				diff_rows=[],
			)

		database.set_value.assert_not_called()
		delivery_sync.assert_called_once_with(
			company="COMPANY-1",
			customer="CUSTOMER-1",
			item_codes=["ITEM-1"],
			target_remap={
				"OLD-1": {
					"customer_schedule_item": "NEW-1",
					"customer_schedule": "SCHED-NEW",
					"schedule_date": "2026-08-12",
				}
			},
		)

	def test_delivery_sync_failure_keeps_an_unsynced_submitted_direct_link(self):
		previous = [
			{
				"name": "OLD-1",
				"parent": "SCHED-OLD",
				"sales_order": "SO-1",
				"item_code": "ITEM-1",
				"customer_part_no": "PART-1",
				"schedule_date": "2026-08-12",
			}
		]
		database = MagicMock()
		database.exists.side_effect = lambda doctype, name=None, *_args, **_kwargs: (
			doctype == "DocType" and name == "Delivery Note Item"
		)
		database.get_value.return_value = frappe._dict(company="COMPANY-1", customer="CUSTOMER-1")
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(
				planning.frappe,
				"get_all",
				side_effect=lambda doctype, **_kwargs: (
					[{"name": "DNI-UNSYNCED", "custom_aps_customer_schedule_item": "OLD-1"}]
					if doctype == "Delivery Note Item"
					else []
				),
			),
			patch.object(planning.frappe, "get_meta", return_value=meta),
			patch(
				"injection_aps.services.delivery_sync.sync_delivery_allocations",
				side_effect=frappe.ValidationError("submitted direct DN still has non-zero delivery"),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "non-zero delivery"):
				planning._rebuild_schedule_execution_allocations(
					previous_item_rows=previous,
					new_item_rows=[],
					diff_rows=[],
				)
		# The old direct value remains the discovery trace; the enclosing import
		# savepoint will also roll back every other schedule write.
		database.set_value.assert_not_called()

	def test_schedule_allocation_rebuild_failure_rolls_back_its_savepoint(self):
		database = MagicMock()
		with (
			patch.object(planning.frappe, "db", database),
			patch.object(planning.frappe, "generate_hash", return_value="remap1234"),
			patch.object(
				planning,
				"_rebuild_schedule_execution_allocations",
				side_effect=RuntimeError("ledger rebuild failed"),
			),
		):
			with self.assertRaisesRegex(RuntimeError, "ledger rebuild failed"):
				planning._remap_schedule_execution_allocations(
					previous_item_rows=[],
					new_item_rows=[],
					diff_rows=[],
				)
		database.rollback.assert_called_once_with(save_point="aps_remap_schedule_allocations_remap1234")
		database.release_savepoint.assert_not_called()

	def test_xlsx_archive_limit_is_checked_before_parsing(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			path = os.path.join(temp_dir, "large.xlsx")
			with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
				archive.writestr("xl/worksheets/sheet1.xml", b"x" * 2048)
			with (
				patch.object(planning, "MAX_XLSX_UNCOMPRESSED_BYTES", 1024),
				patch.object(planning, "_", side_effect=lambda message: message),
				patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
			):
				with self.assertRaises(frappe.ValidationError):
					planning._validate_schedule_workbook_archive(path)

	def test_matrix_expansion_has_an_independent_normalized_row_limit(self):
		raw_rows = [
			["Item", "2026-08-12", "2026-08-13"],
			["ITEM-1", 1, 2],
			["ITEM-2", 3, 4],
		]
		with (
			patch.object(
				planning,
				"_read_schedule_workbook_rows",
				return_value=(raw_rows, {"sheet_name": "Schedule", "sheet_names": ["Schedule"]}),
			),
			patch.object(planning, "_resolve_item_name", side_effect=lambda item: item),
			patch.object(planning, "today", return_value="2026-08-11"),
			patch.object(planning, "MAX_SCHEDULE_NORMALIZED_ROWS", 3),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				planning._normalize_schedule_rows_from_matrix(
					file_url="/private/files/matrix.xlsx",
					mapping={"parser_mode": "matrix", "item_reference_column": "A"},
				)

	def test_matrix_expansion_has_a_streamed_total_payload_limit(self):
		raw_rows = [
			["Item", "2026-08-12", "2026-08-13", "Remark"],
			["ITEM-1", 1, 2, "R" * 80],
		]
		with (
			patch.object(
				planning,
				"_read_schedule_workbook_rows",
				return_value=(raw_rows, {"sheet_name": "Schedule", "sheet_names": ["Schedule"]}),
			),
			patch.object(planning, "today", return_value="2026-08-11"),
			patch.object(planning, "MAX_SCHEDULE_ROWS_JSON_BYTES", 180),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				planning._normalize_schedule_rows_from_matrix(
					file_url="/private/files/matrix.xlsx",
					mapping={
						"parser_mode": "matrix",
						"item_reference_column": "A",
						"remark_column": "D",
					},
				)

	def test_private_file_permission_is_checked_before_path_or_openpyxl(self):
		file_doc = MagicMock()
		file_doc.check_permission.side_effect = frappe.PermissionError("not permitted")
		with (
			patch.object(planning.frappe, "get_doc", return_value=file_doc),
			patch.object(planning, "load_workbook") as load_workbook,
		):
			with self.assertRaises(frappe.PermissionError):
				planning._read_schedule_workbook_rows("/private/files/customer.xlsx")
		file_doc.get_full_path.assert_not_called()
		load_workbook.assert_not_called()

	def test_workbook_cell_length_is_blocked_before_rows_are_returned(self):
		file_doc = MagicMock()
		file_doc.get_full_path.return_value = "/tmp/schedule.xlsx"
		worksheet = MagicMock()
		worksheet.max_row = 1
		worksheet.max_column = 1
		worksheet.title = "Schedule"
		worksheet.iter_rows.return_value = [[MagicMock(value="X" * 33)]]
		workbook = MagicMock()
		workbook.sheetnames = ["Schedule"]
		workbook.active = worksheet
		with (
			patch.object(planning.frappe, "get_doc", return_value=file_doc),
			patch.object(planning, "_validate_schedule_workbook_archive"),
			patch.object(planning, "load_workbook", return_value=workbook),
			patch.object(planning, "MAX_SCHEDULE_CELL_CHARACTERS", 32),
			patch.object(planning, "_", side_effect=lambda message: message),
			patch.object(planning.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				planning._read_schedule_workbook_rows("/private/files/schedule.xlsx")
		workbook.close.assert_called_once_with()

	def test_export_shape_rejects_excess_rows_and_columns(self):
		with (
			patch.object(app, "MAX_EXPORT_ROWS", 2),
			patch.object(app, "_", side_effect=lambda message: message),
			patch.object(app.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				app._validate_export_shape([{"fieldname": "item"}], [{}, {}, {}])
		with (
			patch.object(app, "MAX_EXPORT_COLUMNS", 1),
			patch.object(app, "_", side_effect=lambda message: message),
			patch.object(app.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				app._validate_export_shape([{"fieldname": "item"}, {"fieldname": "qty"}], [{}])
		with (
			patch.object(app, "MAX_EXPORT_CELLS", 3),
			patch.object(app, "_", side_effect=lambda message: message),
			patch.object(app.frappe, "throw", side_effect=frappe.ValidationError),
		):
			with self.assertRaises(frappe.ValidationError):
				app._validate_export_shape(
					[{"fieldname": "item"}, {"fieldname": "qty"}],
					[{}, {}],
				)

	def test_export_text_neutralizes_excel_formula_prefixes(self):
		for prefix in app.EXCEL_FORMULA_PREFIXES:
			value = f"{prefix}HYPERLINK(\"https://example.invalid\")"
			self.assertEqual(app._coerce_export_text(value), f"'{value}")
			self.assertEqual(app._coerce_export_value(value), f"'{value}")
		self.assertEqual(app._coerce_export_value("-12.5", "Float"), -12.5)

	def test_export_filename_and_sheet_name_are_safe(self):
		self.assertEqual(
			app._sanitize_export_filename("../../folder\\evil\r\nHeader: value?.xlsx"),
			"evilHeader_ value.xlsx",
		)
		self.assertEqual(app._sanitize_export_filename("\r\n"), "aps_export.xlsx")
		sheet_name = app._sanitize_excel_sheet_name("[APS]:计划/甲?*\\\r\n")
		self.assertEqual(sheet_name, "_APS__计划_甲___")
		self.assertLessEqual(len(app._sanitize_excel_sheet_name("A" * 100)), 31)
		self.assertTrue(set(sheet_name).isdisjoint(app.EXCEL_SHEET_FORBIDDEN_CHARACTERS))

	def test_matrix_remark_is_capped_at_excel_cell_limit(self):
		with (
			patch.object(planning, "MAX_SCHEDULE_CELL_CHARACTERS", 32),
			patch.object(planning, "_", side_effect=lambda message: message),
		):
			remark = planning._build_matrix_row_remark(
				base_remark="R" * 40,
				description="D" * 40,
				cell_ref="B2",
			)
		self.assertEqual(len(remark), 32)


if __name__ == "__main__":
	unittest.main()
