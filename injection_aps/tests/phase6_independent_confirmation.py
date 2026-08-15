from __future__ import annotations

import base64
import hashlib
import json
import ipaddress
import os
import re
import secrets
import stat
import subprocess
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

import frappe
from frappe.utils import add_days, cint, flt, get_bench_path, get_datetime, getdate, now_datetime, nowtime, today

from injection_aps.api import app
from injection_aps.services import availability, capacity_balance, consistency, delivery_sync, execution_sync, planning


FORBIDDEN_PHASE6_SITES = {"jce.1"}
PHASE6_ATTESTATION_PATH = Path("/etc/injection_aps/phase6-environment-attestation.json")
PHASE6_ATTESTATION_PUBLIC_KEY_PATH = Path("/etc/injection_aps/phase6-attestation-ed25519-public.pem")
REQUIRED_CODE_APPS = ("injection_aps", "frappe", "erpnext", "zelin_pp", "light_mes")
REQUIRED_ISOLATION_FLAGS = (
	"allow_tests",
	"aps_phase6_isolated_site",
	"aps_phase6_isolated_bench",
	"aps_phase6_isolated_database",
	"aps_phase6_backup_verified",
	"aps_phase6_restore_drill_verified",
)
REQUIRED_ISOLATION_VALUES = (
	"db_name",
	"db_host",
)
REQUIRED_EXTERNAL_SERVICES = {
	"redis_cache": ("redis_cache", "aps_phase6_redis_cache_resource_id"),
	"redis_queue": ("redis_queue", "aps_phase6_redis_queue_resource_id"),
	"redis_socketio": ("redis_socketio", "aps_phase6_redis_socketio_resource_id"),
}
PREFIX = "PHASE6-"
MARKER = "PHASE6_INDEPENDENT_CONFIRMATION"
QTY_TOLERANCE = 0.000001
TRANSACTION_CALLBACK_QUEUES = (
	"before_commit",
	"after_commit",
	"before_rollback",
	"after_rollback",
)
LOCAL_SIDE_EFFECT_QUEUES = (
	"_realtime_log",
	"_webhook_queue",
	"_phase6_deferred_enqueue_log",
)
SUCCESS_ARTIFACT_NAME = "phase6-server-confirmation.json"


def run_phase6_gate(output_dir: str | None = None) -> dict:
	"""Run rollback-only server checks on a fully isolated test environment."""
	isolation_evidence = _assert_test_site()
	attestation = isolation_evidence["attestation"]
	gate_run_id = secrets.token_hex(16)
	artifact_dir = Path(
		output_dir
		or frappe.get_site_path("private", "files", "aps_phase6_confirmation")
	)
	artifact_dir.mkdir(parents=True, exist_ok=True)
	artifact_path = artifact_dir / SUCCESS_ARTIFACT_NAME
	# Remove the canonical name from any previous success before exercising new
	# code.  A failed or interrupted rerun must never leave an older success at
	# the path consumed by release tooling.
	_revoke_previous_success_artifact(artifact_path, gate_run_id=gate_run_id)
	_write_json_atomic(
		artifact_path,
		{
			"status": "running",
			"gate_run_id": gate_run_id,
			"server_gate_passed": False,
			"release_ready": False,
			"database_changes_rolled_back": False,
			"attestation_sha256": isolation_evidence.get("attestation_sha256"),
			"expected_commits": attestation.get("expected_commits"),
			"started_on": str(now_datetime()),
		},
	)
	original_user = frappe.session.user
	original_callbacks: dict[str, tuple] | None = None
	original_local_queues: dict[str, tuple] | None = None
	save_point = "aps_phase6_independent_confirmation"
	savepoint_created = False
	payload: dict = {
		"gate_run_id": gate_run_id,
		"site": frappe.local.site,
		"environment_evidence": _get_phase6_environment_evidence(isolation_evidence),
		"code_evidence": {},
		"started_on": str(now_datetime()),
		"isolated_test_site": True,
		"database_changes_rolled_back": False,
		"transaction_state_restored": False,
		"external_side_effects_contained": False,
		"server_gate_passed": False,
		"release_ready": False,
		"status": "running",
		"full_chain": {},
		"scenarios": [],
		"audits": {},
		"ui_checks": {},
	}
	result: dict | None = None
	external_guard_log: list[dict] = []
	try:
		code_evidence = _assert_exact_code_commit(attestation)
		payload["code_evidence"] = code_evidence
		original_callbacks = _snapshot_transaction_callbacks()
		original_local_queues = _snapshot_local_side_effect_queues()
		_detach_local_side_effect_queues()
		# Establish the rollback boundary before changing the user, creating
		# fixtures, mutating settings or writing any ERP document.
		frappe.db.savepoint(save_point)
		savepoint_created = True
		frappe.set_user("Administrator")

		# A Phase 6 server check must never make fixture data durable. If a
		# service attempts to commit internally, fail instead of losing the
		# savepoint and leaking test records.
		with _phase6_external_side_effect_guard() as external_guard_log, patch.object(
				frappe.db,
				"commit",
				side_effect=frappe.ValidationError(
					"Phase 6 confirmation blocked an internal commit; the gate must remain rollback-only."
				),
			):
			context = _ensure_master_data(attestation)
			payload["master_data"] = _jsonable(context)
			full_chain = _run_full_business_chain(context)
			payload["full_chain"] = _jsonable(full_chain)
			payload["audits"]["full_chain"] = _jsonable(full_chain["quantity_audit"])
			payload["scenarios"] = _jsonable(_run_pmc_scenarios(context, full_chain))
			payload["ui_checks"] = _jsonable(_run_ui_checks(context, full_chain))
		payload["external_side_effect_guard"] = {
			"immediate_queue_socketio_and_email_blocked": True,
			"outbound_http_notifications_and_webhooks_blocked": True,
			"isolated_cache_mutations_allowed": True,
			"deferred_call_count": len(external_guard_log),
			"deferred_calls": external_guard_log,
		}
		payload["external_side_effects_contained"] = True

		payload["automated_test_coverage"] = {
			"unit": {
				"executed_by_this_gate": False,
				"required_external_suite": (
					"Run all no-site unit and static suites from the exact commit before accepting this artifact."
				),
			},
			"integration": {
				"executed_by_this_gate": True,
				"coverage": "import -> net requirement -> schedule -> release -> production feedback -> delivery",
			},
			"idempotency": {
				"executed_by_this_gate": True,
				"coverage": ["same schedule import", "production sync replay", "delivery sync replay"],
			},
			"transaction": {
				"executed_by_this_gate": True,
				"coverage": "schedule import plus rebuild rollback probe",
			},
			"permission": {
				"executed_by_this_gate": False,
				"required_external_suite": (
					"Run the app permission suite for PMC, GMC/planning supervisor and production operator."
				),
			},
			"ui": {
				"browser_executed_by_this_gate": False,
				"coverage": "Server payload and static checks only; execute the browser scenario suite separately.",
			},
		}
		payload["release_blockers"] = [
			"Exact-commit no-site unit/static suite evidence is not attached by this server runner.",
			"Role and record-scope permission suite is not executed by this server runner.",
			"Browser UI scenario replay is not executed by this server runner.",
			"Independent backup existence, checksum and restore-drill evidence must be verified outside this process.",
			"Real scrap, Manufacture cancellation, released-Work-Order schedule cancellation and positive-delay scenarios require the isolated database suite.",
			"MariaDB concurrency, locking and query-plan tests require two isolated database connections.",
		]
		payload["server_gate_passed"] = True
		payload["release_ready"] = False
		payload["status"] = "server_checks_passed"
		payload["finished_on"] = str(now_datetime())
		payload["artifact_binding"] = _build_artifact_binding(
			gate_run_id=gate_run_id,
			planning_run=full_chain["run"],
			isolation_evidence=isolation_evidence,
			code_evidence=code_evidence,
		)
		result = {
			"gate_run_id": gate_run_id,
			"server_gate_passed": True,
			"release_ready": False,
			"release_blockers": list(payload["release_blockers"]),
			"artifact": str(artifact_path),
			"full_chain_run": full_chain["run"],
			"release_batch": full_chain["release"].get("release_batch"),
			"scenario_count": len(payload["scenarios"]),
			"audit_difference_count": full_chain["quantity_audit"]["difference_count"],
		}
	except Exception as exc:
		payload["status"] = "failed"
		payload["failed_on"] = str(now_datetime())
		payload["error"] = str(exc)
		_write_failure_artifacts_best_effort(artifact_dir, artifact_path, payload)
		raise
	finally:
		# A savepoint rollback does not reset Frappe's transaction callback
		# queues or frappe.local realtime/webhook queues. Submitted fixtures may
		# have registered work which must not survive after their rows disappear.
		restoration_errors = []
		if savepoint_created:
			try:
				frappe.db.rollback(save_point=save_point)
				payload["database_changes_rolled_back"] = True
			except Exception as exc:
				restoration_errors.append(("database rollback", exc))
		if savepoint_created:
			try:
				frappe.clear_cache(doctype="APS Settings")
			except Exception as exc:
				restoration_errors.append(("APS Settings cache clear", exc))
		try:
			frappe.set_user(original_user or "Guest")
		except Exception as exc:
			restoration_errors.append(("session user restore", exc))
		if original_local_queues is not None:
			try:
				_restore_local_side_effect_queues(original_local_queues)
			except Exception as exc:
				restoration_errors.append(("frappe.local side-effect queue restore", exc))
		if original_callbacks is not None:
			try:
				_restore_transaction_callbacks(original_callbacks)
			except Exception as exc:
				restoration_errors.append(("transaction callback restore", exc))
		if restoration_errors:
			payload["status"] = "failed"
			payload["server_gate_passed"] = False
			payload["release_ready"] = False
			payload["transaction_state_restored"] = False
			payload["restoration_errors"] = [
				{"stage": stage, "error": str(error)} for stage, error in restoration_errors
			]
			_write_failure_artifacts_best_effort(artifact_dir, artifact_path, payload)
			raise frappe.ValidationError(
				"Phase 6 failed to restore isolated runner state: {0}.".format(
					"; ".join("{0}: {1}".format(stage, error) for stage, error in restoration_errors)
				)
			) from restoration_errors[0][1]
		payload["transaction_state_restored"] = True
		if payload.get("status") == "failed":
			_write_failure_artifacts_best_effort(artifact_dir, artifact_path, payload)
	if result is None:
		frappe.throw("Phase 6 server confirmation ended without a result.")
	if not payload.get("database_changes_rolled_back") or not payload.get("transaction_state_restored"):
		frappe.throw("Phase 6 server confirmation did not restore its transaction boundary.")
	result["database_changes_rolled_back"] = True
	# Publish a successful server artifact only after rollback, local queue and
	# callback restoration succeeded. It is not an overall release approval.
	_write_json_atomic(artifact_path, payload)
	return result


def _snapshot_transaction_callbacks(database=None) -> dict[str, tuple]:
	"""Capture Frappe callback queues without executing or discarding them."""
	database = database or frappe.db
	snapshot: dict[str, tuple] = {}
	for queue_name in TRANSACTION_CALLBACK_QUEUES:
		manager = getattr(database, queue_name, None)
		functions = getattr(manager, "_functions", None)
		if functions is None:
			frappe.throw(
				"Phase 6 confirmation cannot prove transaction callback isolation for queue {0}.".format(
					queue_name
				)
			)
		snapshot[queue_name] = tuple(functions)
	return snapshot


def _restore_transaction_callbacks(snapshot: dict[str, tuple], database=None) -> None:
	"""Discard gate callbacks and restore the caller's original queues."""
	database = database or frappe.db
	for queue_name in TRANSACTION_CALLBACK_QUEUES:
		manager = getattr(database, queue_name, None)
		if manager is None or not callable(getattr(manager, "reset", None)) or not callable(
			getattr(manager, "add", None)
		):
			frappe.throw(
				"Phase 6 confirmation cannot restore transaction callback queue {0}.".format(queue_name)
			)
		manager.reset()
		for callback in snapshot.get(queue_name, ()):
			manager.add(callback)


def _snapshot_local_side_effect_queues(local=None) -> dict[str, tuple]:
	"""Snapshot request-local queues which a savepoint rollback cannot see."""
	local = local or frappe.local
	snapshot = {}
	for queue_name in LOCAL_SIDE_EFFECT_QUEUES:
		if not hasattr(local, queue_name):
			snapshot[queue_name] = (False, "missing", (), None, None)
			continue
		value = getattr(local, queue_name)
		if value is None:
			snapshot[queue_name] = (True, "none", (), None, None)
		elif isinstance(value, list):
			snapshot[queue_name] = (True, "list", tuple(value), None, value)
		elif isinstance(value, deque):
			snapshot[queue_name] = (True, "deque", tuple(value), value.maxlen, value)
		elif isinstance(value, tuple):
			snapshot[queue_name] = (True, "tuple", tuple(value), None, value)
		else:
			frappe.throw(
				"Phase 6 cannot snapshot unsupported frappe.local queue {0} ({1}).".format(
					queue_name, type(value).__name__
				)
			)
	return snapshot


def _restore_local_side_effect_queues(snapshot: dict[str, tuple], local=None) -> None:
	"""Discard gate-created queue entries and recreate the exact caller state."""
	local = local or frappe.local
	for queue_name in LOCAL_SIDE_EFFECT_QUEUES:
		if hasattr(local, queue_name):
			delattr(local, queue_name)
		existed, container_type, values, _maxlen, original_container = snapshot.get(
			queue_name, (False, "missing", (), None, None)
		)
		if not existed:
			continue
		if container_type == "none":
			restored = None
		elif container_type == "list":
			original_container.clear()
			original_container.extend(values)
			restored = original_container
		elif container_type == "deque":
			original_container.clear()
			original_container.extend(values)
			restored = original_container
		elif container_type == "tuple":
			restored = original_container
		else:
			frappe.throw(
				"Phase 6 cannot restore frappe.local queue {0} ({1}).".format(
					queue_name, container_type
				)
			)
		setattr(local, queue_name, restored)


def _detach_local_side_effect_queues(local=None) -> None:
	"""Give the gate empty request-local queue namespaces of its own."""
	local = local or frappe.local
	for queue_name in LOCAL_SIDE_EFFECT_QUEUES:
		if hasattr(local, queue_name):
			delattr(local, queue_name)


@contextmanager
def _phase6_external_side_effect_guard():
	"""Block transaction-external queue/socket/email writes during the gate.

	Database-backed delayed mail remains inside the savepoint. Background jobs
	are recorded but deliberately not handed to Redis; only after-commit jobs are
	accepted. Realtime messages may register after-commit local callbacks, while
	any attempt to emit immediately to Socket.IO Redis fails closed.
	"""
	import frappe.realtime
	import requests.sessions

	deferred_calls = []
	original_sendmail = frappe.sendmail
	local = frappe.local
	queue_name = "_phase6_deferred_enqueue_log"
	if not hasattr(local, queue_name):
		deferred_queue_snapshot = (False, "missing", (), None)
	else:
		original_queue = getattr(local, queue_name)
		if original_queue is None:
			deferred_queue_snapshot = (True, "none", (), None)
		elif isinstance(original_queue, list):
			deferred_queue_snapshot = (True, "list", tuple(original_queue), original_queue)
		elif isinstance(original_queue, deque):
			deferred_queue_snapshot = (True, "deque", tuple(original_queue), original_queue)
		elif isinstance(original_queue, tuple):
			deferred_queue_snapshot = (True, "tuple", tuple(original_queue), original_queue)
		else:
			frappe.throw(
				"Phase 6 cannot snapshot unsupported frappe.local queue {0} ({1}).".format(
					queue_name, type(original_queue).__name__
				)
			)
		delattr(local, queue_name)

	def defer_enqueue(*args, **kwargs):
		if kwargs.get("enqueue_after_commit") is not True:
			raise frappe.ValidationError(
				"Phase 6 blocked an immediate Redis queue write; only enqueue_after_commit is allowed."
			)
		method = args[0] if args else kwargs.get("method")
		deferred_calls.append(
			{
				"kind": "background_job",
				"method": getattr(method, "__qualname__", None) or str(method or "-"),
				"queue": str(kwargs.get("queue") or "default"),
				"job_id": str(kwargs.get("job_id") or ""),
			}
		)
		if not hasattr(local, queue_name):
			setattr(local, queue_name, [])
		getattr(local, queue_name).append(deferred_calls[-1])
		return None

	def block_immediate_realtime(*_args, **_kwargs):
		raise frappe.ValidationError(
			"Phase 6 blocked an immediate Socket.IO/Redis realtime publish."
		)

	def block_outbound_http(*_args, **_kwargs):
		raise frappe.ValidationError("Phase 6 blocked an outbound HTTP notification/webhook.")

	def guarded_sendmail(*args, **kwargs):
		delayed = kwargs.get("delayed", args[5] if len(args) > 5 else True)
		if not delayed or kwargs.get("now"):
			raise frappe.ValidationError("Phase 6 blocked an immediate outbound email.")
		deferred_calls.append({"kind": "database_backed_email", "delayed": True})
		return original_sendmail(*args, **kwargs)

	try:
		with (
			patch.object(frappe, "enqueue", side_effect=defer_enqueue),
			patch.object(frappe.realtime, "emit_via_redis", side_effect=block_immediate_realtime),
			patch.object(frappe, "sendmail", side_effect=guarded_sendmail),
			patch.object(requests.sessions.Session, "request", side_effect=block_outbound_http),
		):
			yield deferred_calls
	finally:
		if hasattr(local, queue_name):
			delattr(local, queue_name)
		existed, container_type, values, original_queue = deferred_queue_snapshot
		if existed:
			if container_type == "none":
				restored_queue = None
			elif container_type in {"list", "deque"}:
				original_queue.clear()
				original_queue.extend(values)
				restored_queue = original_queue
			elif container_type == "tuple":
				restored_queue = original_queue
			else:  # pragma: no cover - snapshot construction is exhaustive
				frappe.throw(
					"Phase 6 cannot restore frappe.local queue {0} ({1}).".format(
						queue_name, container_type
					)
				)
			setattr(local, queue_name, restored_queue)


def cleanup(
	delete_master_data: bool = False,
	confirmation_token: str | None = None,
) -> dict:
	"""Refuse in-place fixture deletion; use a disposable isolated database.

	Standard ERP documents cannot be made consistent by raw table deletion: Stock
	Ledger, General Ledger and controller side effects would be left behind.  The
	Phase 6 gate is rollback-only, so it never needs cleanup.  Old test databases
	must be discarded or restored from their independently verified backup.
	"""
	_assert_test_site()
	frappe.throw(
		"Automatic Phase 6 cleanup is disabled. Restore or replace the disposable isolated test database instead.",
		frappe.PermissionError,
	)


def _run_full_business_chain(context: dict) -> dict:
	# The main chain is due on the posting date so its first real Manufacture
	# entry proves the natural-day JIT path rather than accidentally exercising
	# only Prebuild while labelling the snapshot "JIT".
	due_date = getdate(today())
	rows = [
		{
			"sales_order": context["sales_order"],
			"item_code": context["flow_item"],
			"customer_part_no": "FLOW",
			"schedule_date": due_date,
			"qty": 120,
			"production_strategy": "Force JIT",
			"demand_confidence": "Confirmed",
			"prebuild_allowed": 1,
			"max_prebuild_days": 2,
			"source_excel_row": 2,
		}
	]
	import_result = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no=f"{PREFIX}FLOW-V1",
		schedule_scope=f"{PREFIX}FLOW-{context['suffix']}",
		import_strategy="Replace Scope",
		duplicate_policy="Block",
		rows_json=rows,
		rebuild=1,
		existing_work_order_policy="Exclude",
	)
	run_result = planning.run_planning_run(
		company=context["company"],
		plant_floor=context["plant_floor"],
		plant_floors=[context["plant_floor"]],
		horizon_days=5,
		customer=context["customer"],
		item_code=context["flow_item"],
		existing_work_order_policy="Exclude",
		run_type="Trial",
	)
	run_name = run_result["run"]
	frappe.db.set_value(
		"APS Planning Run",
		run_name,
		"notes",
		f"{MARKER}: full import-net-schedule-release-report-delivery",
		update_modified=False,
	)
	initial_capacity = _ensure_capacity_applied(run_name)
	planned_jit = _assert_planned_jit_capacity(run_name, due_date=due_date, expected_qty=120)
	projected_jit_availability = _assert_projected_jit_availability(
		run_name,
		due_date=due_date,
		expected_qty=120,
		expected_results=planned_jit["results"],
	)
	approve_result = planning.approve_planning_run(run_name)
	capacity_after_approval = _ensure_capacity_current(run_name, reason="after approval")
	work_order_proposal = planning.generate_work_order_proposals(run_name)
	_assert_positive(
		"work_order_proposal",
		"APS Work Order Proposal Batch",
		work_order_proposal.get("work_order_proposal_batch"),
		work_order_proposal.get("proposal_count"),
	)
	_approve_proposal_batch("APS Work Order Proposal Batch", work_order_proposal["work_order_proposal_batch"])
	work_order_apply = planning.apply_work_order_proposals(work_order_proposal["work_order_proposal_batch"])
	_assert_positive("work_order_apply", "Work Order", run_name, len(work_order_apply.get("applied_work_orders") or []))
	capacity_after_work_order = _ensure_capacity_current(run_name, reason="after work order proposal apply")
	planned_jit_after_work_order = _assert_planned_jit_capacity(
		run_name,
		due_date=due_date,
		expected_qty=120,
	)
	shift_proposal = planning.generate_shift_schedule_proposals(
		run_name=run_name,
		work_order_proposal_batch=work_order_proposal["work_order_proposal_batch"],
		release_horizon_days=7,
		release_from_date=today(),
	)
	_assert_positive(
		"shift_schedule_proposal",
		"APS Shift Schedule Proposal Batch",
		shift_proposal.get("shift_schedule_proposal_batch"),
		shift_proposal.get("proposal_count"),
	)
	_approve_proposal_batch("APS Shift Schedule Proposal Batch", shift_proposal["shift_schedule_proposal_batch"])
	release_apply = planning.apply_shift_schedule_proposals(shift_proposal["shift_schedule_proposal_batch"])
	_assert_positive("release_apply", "APS Release Batch", release_apply.get("release_batch"), release_apply.get("applied_rows"))
	capacity_after_release = _ensure_capacity_current(
		run_name,
		reason="after shift schedule proposal apply",
	)
	planned_jit_after_release = _assert_planned_jit_capacity(
		run_name,
		due_date=due_date,
		expected_qty=120,
	)
	projected_jit_availability_after_release = _assert_projected_jit_availability(
		run_name,
		due_date=due_date,
		expected_qty=120,
		expected_results=planned_jit_after_release["results"],
	)

	schedule_item = _single_value(
		"Customer Delivery Schedule Item",
		{"parent": import_result["schedule"], "item_code": context["flow_item"]},
		"name",
	)
	page_before_execution = _page_snapshot(run_name, context, import_result["schedule"])
	scheduling_rows = _get_released_scheduling_rows(run_name)
	_assert_positive("released_scheduling_rows", "Scheduling Item", run_name, len(scheduling_rows))
	_assert_qty(
		"released_scheduling_qty",
		"APS Planning Run",
		run_name,
		120,
		sum(flt(row.scheduling_qty) for row in scheduling_rows),
	)
	shift_boundary_evidence = _assert_released_shift_boundaries(run_name, scheduling_rows)
	persisted_release_chain = _assert_persisted_segment_proposal_wos_chain(
		run_name,
		shift_proposal["shift_schedule_proposal_batch"],
		require_cross_midnight=False,
	)
	started_schedulings = _start_formal_scheduling_for_production(run_name, scheduling_rows)
	distinct_workstations = sorted({row.workstation for row in scheduling_rows if row.workstation})

	first_row = scheduling_rows[0]
	first_qty = flt(first_row.scheduling_qty)
	_create_manufacture_entry(context, run_name, first_row, first_qty, sequence=1)
	first_production_sync = execution_sync.sync_production_for_run(run_name)
	first_delivery = _create_delivery_note(
		context,
		schedule_item=schedule_item,
		qty=50,
		sequence=1,
	)
	first_delivery_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[context["flow_item"]],
	)
	partial_fulfillment = availability.recalculate_run_fulfillment(run_name)
	partial_results = partial_fulfillment.get("results") or []
	partial_actual_good = sum(flt(row.get("actual_good_qty")) for row in partial_results)
	partial_jit_good = sum(flt(row.get("jit_actual_good_qty")) for row in partial_results)
	partial_prebuild_good = sum(flt(row.get("prebuild_actual_good_qty")) for row in partial_results)
	partial_late_good = sum(flt(row.get("late_actual_good_qty")) for row in partial_results)
	_assert_qty("jit_actual_good", "APS Planning Run", run_name, first_qty, partial_jit_good)
	_assert_qty("jit_actual_total", "APS Planning Run", run_name, first_qty, partial_actual_good)
	_assert_qty("jit_actual_not_prebuild", "APS Planning Run", run_name, 0, partial_prebuild_good)
	_assert_qty("jit_actual_not_late", "APS Planning Run", run_name, 0, partial_late_good)
	jit_snapshot = _page_snapshot(run_name, context, import_result["schedule"])
	_assert_qty(
		"jit_partial_production",
		"APS Planning Run",
		run_name,
		first_qty,
		jit_snapshot["gantt_quantity_summary"].get("produced_qty"),
	)
	_assert_qty(
		"jit_partial_delivery",
		"APS Planning Run",
		run_name,
		50,
		jit_snapshot["gantt_quantity_summary"].get("delivered_qty"),
	)

	for index, row in enumerate(scheduling_rows[1:], start=2):
		_create_manufacture_entry(context, run_name, row, flt(row.scheduling_qty), sequence=index)
	second_delivery = _create_delivery_note(
		context,
		schedule_item=schedule_item,
		qty=70,
		sequence=2,
	)
	final_production_sync = execution_sync.sync_production_for_run(run_name)
	final_delivery_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[context["flow_item"]],
	)
	availability_result = availability.recalculate_run_fulfillment(run_name)
	consistency_result = consistency.recalculate_plan_consistency(
		run_name,
		reason="Phase 6 independent confirmation final reconciliation",
	)
	page_after_execution = _page_snapshot(run_name, context, import_result["schedule"])
	audit = consistency.audit_run_quantity_consistency(run_name)
	if not audit.get("valid"):
		_raise_audit_failure("quantity_consistency_audit", audit)
	production_replay = execution_sync.sync_production_for_run(run_name)
	delivery_replay = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[context["flow_item"]],
	)
	for label, replay in (("production", production_replay), ("delivery", delivery_replay)):
		for fieldname in ("created", "updated", "reversed"):
			_assert_qty(
				f"{label}_sync_replay_{fieldname}",
				"APS Planning Run",
				run_name,
				0,
				(replay.get("ledger") or {}).get(fieldname),
			)
	replay_audit = consistency.audit_run_quantity_consistency(run_name)
	if not replay_audit.get("valid"):
		_raise_audit_failure("quantity_consistency_after_idempotent_replay", replay_audit)
	if replay_audit.get("totals") != audit.get("totals"):
		_phase6_fail(
			"idempotent_replay_totals",
			"APS Planning Run",
			run_name,
			audit.get("totals"),
			replay_audit.get("totals"),
			"",
		)
	_assert_qty(
		"final_produced_qty",
		"APS Planning Run",
		run_name,
		120,
		page_after_execution["gantt_quantity_summary"].get("produced_qty"),
	)
	_assert_qty(
		"final_delivered_qty",
		"APS Planning Run",
		run_name,
		120,
		page_after_execution["gantt_quantity_summary"].get("delivered_qty"),
	)
	return {
		"run": run_name,
		"import": import_result,
		"planning": run_result,
		"capacity": {
			"initial": initial_capacity,
			"after_approval": capacity_after_approval,
			"after_work_order_apply": capacity_after_work_order,
			"after_shift_schedule_apply": capacity_after_release,
		},
		"planned_jit": planned_jit,
		"planned_jit_after_work_order": planned_jit_after_work_order,
		"planned_jit_after_release": planned_jit_after_release,
		"projected_jit_availability": projected_jit_availability,
		"projected_jit_availability_after_release": projected_jit_availability_after_release,
		"shift_boundary_evidence": shift_boundary_evidence,
		"persisted_release_chain": persisted_release_chain,
		"approval": approve_result,
		"work_order_proposal": work_order_proposal,
		"work_order_apply": work_order_apply,
		"shift_proposal": shift_proposal,
		"release": release_apply,
		"schedule_item": schedule_item,
		"released_scheduling_rows": [dict(row) for row in scheduling_rows],
		"started_work_order_schedulings": started_schedulings,
		"distinct_workstations": distinct_workstations,
		"production_sync": {
			"partial": first_production_sync,
			"final": final_production_sync,
			"idempotent_replay": production_replay,
		},
		"delivery_sync": {
			"partial": first_delivery_sync,
			"final": final_delivery_sync,
			"idempotent_replay": delivery_replay,
			"delivery_notes": [first_delivery, second_delivery],
		},
		"fulfillment": availability_result,
		"partial_jit_fulfillment": partial_fulfillment,
		"consistency": consistency_result,
		"page_comparison": {
			"before_execution": page_before_execution,
			"partial_jit": jit_snapshot,
			"after_execution": page_after_execution,
		},
		"quantity_audit": audit,
		"quantity_audit_after_replay": replay_audit,
	}


def _run_pmc_scenarios(context: dict, full_chain: dict) -> list[dict]:
	return [
		_scenario_cancel_tomorrow(context),
		_scenario_temporary_increase(context),
		_scenario_decrease_after_start(context, full_chain),
		_scenario_split_order_across_two_machines(full_chain),
		_scenario_urgent_insert_displaces(context),
		_scenario_jit_produce_and_deliver(full_chain),
		_scenario_day_night_shift_slicing(context),
		_scenario_machine_downtime(context, full_chain),
		_scenario_duplicate_rows(context),
		_scenario_repeat_import(context),
		_scenario_delivery_note_cancel_return(context),
		_scenario_transaction_rollback(context),
	]


def _scenario_cancel_tomorrow(context: dict) -> dict:
	scope = f"{PREFIX}CANCEL-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 1))
	_import_scope(context, scope, item, 30, due_date, version="V1")
	result = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 0, due_date, source_excel_row=3)],
	)
	row = _active_schedule_row(scope, item)
	_assert_qty("cancel_tomorrow_qty", "Customer Delivery Schedule Item", row.name, 0, row.qty)
	_assert_equal("cancel_tomorrow_status", "Customer Delivery Schedule Item", row.name, "Cancelled", row.status)
	return {"scenario": "cancel_tomorrow_delivery", "passed": True, "import": result, "schedule_item": row.name}


def _scenario_temporary_increase(context: dict) -> dict:
	scope = f"{PREFIX}INCREASE-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 1))
	_import_scope(context, scope, item, 40, due_date, version="V1")
	preview = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 70, due_date, source_excel_row=4)],
	)
	row_preview = next(row for row in preview["rows"] if row["item_code"] == item)
	_assert_equal("temporary_increase_type", "Customer Delivery Schedule", scope, "Increased", row_preview["change_type"])
	result = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, 70, due_date, source_excel_row=4)],
	)
	row = _active_schedule_row(scope, item)
	_assert_qty("temporary_increase_qty", "Customer Delivery Schedule Item", row.name, 70, row.qty)
	return {"scenario": "temporary_increase_tomorrow", "passed": True, "preview": preview, "import": result}


def _scenario_decrease_after_start(context: dict, full_chain: dict) -> dict:
	schedule_name = full_chain["import"]["schedule"]
	schedule = frappe.db.get_value(
		"Customer Delivery Schedule",
		schedule_name,
		["schedule_scope", "version_no"],
		as_dict=True,
	)
	row = frappe.db.get_value(
		"Customer Delivery Schedule Item",
		{"parent": schedule_name, "item_code": context["flow_item"]},
		["name", "customer_part_no", "schedule_date", "qty", "produced_qty", "delivered_qty"],
		as_dict=True,
	)
	if not schedule or not row:
		_phase6_fail("decrease_after_start_fixture", "Customer Delivery Schedule", schedule_name, "active row", row, "")
	target_qty = max(min(flt(row.qty) - 1, flt(row.produced_qty) - 1), 0)
	preview = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no=f"{schedule.version_no}-DECREASE-CHECK",
		schedule_scope=schedule.schedule_scope,
		import_strategy="Partial Update",
		duplicate_policy="Block",
		rows_json=[
			{
				**_schedule_row(context["flow_item"], target_qty, row.schedule_date, source_excel_row=5),
				"sales_order": context["sales_order"],
				"customer_part_no": row.customer_part_no,
				"previous_schedule_date": row.schedule_date,
			}
		],
	)
	row_preview = next(item for item in preview["rows"] if item["item_code"] == context["flow_item"])
	if "affects_produced" not in row_preview:
		_phase6_fail(
			"decrease_after_start_preview_shape",
			"Customer Delivery Schedule Item",
			row.name,
			"an evaluated diff row with execution-impact fields",
			row_preview,
			"",
			details=preview,
		)
	_assert_equal(
		"decrease_after_start_is_blocked",
		"Customer Delivery Schedule",
		schedule.schedule_scope,
		False,
		bool(preview.get("can_import")),
	)
	_assert_qty(
		"decrease_after_start_produced_impact",
		"Customer Delivery Schedule Item",
		row.name,
		1,
		row_preview.get("affects_produced"),
	)
	return {
		"scenario": "decrease_after_start_is_safely_blocked",
		"passed": True,
		"preview": preview,
		"current_produced_qty": row.produced_qty,
		"current_delivered_qty": row.delivered_qty,
		"requested_reduction_qty": target_qty,
	}


def _scenario_split_order_across_two_machines(full_chain: dict) -> dict:
	workstations = full_chain.get("distinct_workstations") or []
	_assert_qty("split_order_machine_count", "APS Planning Run", full_chain["run"], 2, len(workstations))
	total_scheduled = sum(flt(row.get("scheduling_qty")) for row in full_chain.get("released_scheduling_rows") or [])
	_assert_qty("split_order_total_qty", "APS Planning Run", full_chain["run"], 120, total_scheduled)
	return {
		"scenario": "one_order_split_to_two_machines",
		"passed": True,
		"workstations": workstations,
		"scheduled_qty": total_scheduled,
	}


def _scenario_urgent_insert_displaces(context: dict) -> dict:
	required_date = getdate(add_days(today(), 1))
	probe = _create_open_overlap_probe(context, required_date=required_date)
	analysis = planning.analyze_insert_order_impact(
		company=context["company"],
		plant_floor=context["plant_floor"],
		plant_floors=[context["plant_floor"]],
		item_code=context["flow_item"],
		qty=30,
		required_date=required_date,
		customer=context["customer"],
	)
	displaced_segments = analysis.get("displaced_segments") or []
	if not displaced_segments:
		_phase6_fail(
			"urgent_insert_displaced_segments",
			"APS Planning Run",
			probe["run"],
			"> 0",
			0,
			0,
			details=[
				{
					"probe": probe,
					"parallelization_plan": analysis.get("parallelization_plan") or [],
					"candidate_workstations": analysis.get("candidate_workstations") or [],
					"selected_plant_floors": analysis.get("selected_plant_floors") or [],
					"scheduled_qty": analysis.get("scheduled_qty"),
					"unscheduled_qty": analysis.get("unscheduled_qty"),
					"exceptions": analysis.get("exceptions") or [],
				}
			],
		)
	return {
		"scenario": "urgent_order_insert_displaces_original",
		"passed": True,
		"probe": probe,
		"displaced_segments": displaced_segments,
		"scheduled_qty": analysis.get("scheduled_qty"),
	}


def _scenario_jit_produce_and_deliver(full_chain: dict) -> dict:
	partial = full_chain["page_comparison"]["partial_jit"]["gantt_quantity_summary"]
	final = full_chain["page_comparison"]["after_execution"]["gantt_quantity_summary"]
	partial_projection = full_chain.get("partial_jit_fulfillment") or {}
	partial_results = partial_projection.get("results") or []
	jit_actual_good_qty = sum(flt(row.get("jit_actual_good_qty")) for row in partial_results)
	prebuild_actual_good_qty = sum(flt(row.get("prebuild_actual_good_qty")) for row in partial_results)
	late_actual_good_qty = sum(flt(row.get("late_actual_good_qty")) for row in partial_results)
	_assert_positive("jit_partial_produced", "APS Planning Run", full_chain["run"], partial.get("produced_qty"))
	_assert_positive("jit_partial_delivered", "APS Planning Run", full_chain["run"], partial.get("delivered_qty"))
	_assert_positive("jit_actual_classification", "APS Planning Run", full_chain["run"], jit_actual_good_qty)
	_assert_qty("jit_has_no_prebuild_actual", "APS Planning Run", full_chain["run"], 0, prebuild_actual_good_qty)
	_assert_qty("jit_has_no_late_actual", "APS Planning Run", full_chain["run"], 0, late_actual_good_qty)
	_assert_qty("jit_final_delivered", "APS Planning Run", full_chain["run"], final.get("planned_qty"), final.get("delivered_qty"))
	return {
		"scenario": "jit_produce_while_delivering",
		"passed": True,
		"partial": partial,
		"final": final,
		"jit_actual_good_qty": jit_actual_good_qty,
		"prebuild_actual_good_qty": prebuild_actual_good_qty,
		"late_actual_good_qty": late_actual_good_qty,
	}


def _scenario_day_night_shift_slicing(context: dict) -> dict:
	"""Exercise the real A/B shift slicer across both boundaries and midnight."""
	work_date = getdate(today())
	start = get_datetime(f"{work_date} 19:00:00")
	end = get_datetime(f"{add_days(work_date, 1)} 09:00:00")
	slices = planning._split_segment_into_shift_slices(
		{
			"name": f"{PREFIX}SHIFT-BOUNDARY",
			"primary_item_code": context["flow_item"],
			"start_time": start,
			"end_time": end,
			"planned_qty": 140,
		}
	)
	_assert_qty("shift_boundary_slice_count", "APS Planning Run", "shift-boundary", 3, len(slices))
	_assert_qty(
		"shift_boundary_quantity_conservation",
		"APS Planning Run",
		"shift-boundary",
		140,
		sum(flt(row.get("planned_qty")) for row in slices),
	)
	_assert_equal(
		"shift_boundary_types",
		"APS Planning Run",
		"shift-boundary",
		["白班", "晚班", "白班"],
		[row.get("shift_type") for row in slices],
	)
	_assert_equal(
		"shift_boundary_quantities",
		"APS Planning Run",
		"shift-boundary",
		[10.0, 120.0, 10.0],
		[flt(row.get("planned_qty")) for row in slices],
	)
	expected_windows = [
		(start, get_datetime(f"{work_date} 20:00:00"), work_date),
		(
			get_datetime(f"{work_date} 20:00:00"),
			get_datetime(f"{add_days(work_date, 1)} 08:00:00"),
			work_date,
		),
		(
			get_datetime(f"{add_days(work_date, 1)} 08:00:00"),
			end,
			getdate(add_days(work_date, 1)),
		),
	]
	actual_windows = [
		(
			get_datetime(row.get("start_time")),
			get_datetime(row.get("end_time")),
			getdate(row.get("posting_date")),
		)
		for row in slices
	]
	_assert_equal(
		"shift_boundary_exact_windows",
		"APS Planning Run",
		"shift-boundary",
		expected_windows,
		actual_windows,
	)
	for previous, current in zip(slices, slices[1:]):
		if get_datetime(previous.get("end_time")) != get_datetime(current.get("start_time")):
			_phase6_fail(
				"shift_boundary_continuity",
				"APS Planning Run",
				"shift-boundary",
				get_datetime(previous.get("end_time")),
				get_datetime(current.get("start_time")),
				"",
			)
	persisted_chain = _create_persisted_cross_midnight_release_probe(
		context,
		start=start,
		end=end,
		slices=slices,
	)
	return {
		"scenario": "day_night_shift_cross_midnight_slicing",
		"passed": True,
		"slices": _jsonable(slices),
		"persisted_chain": persisted_chain,
	}


def _create_persisted_cross_midnight_release_probe(
	context: dict,
	*,
	start,
	end,
	slices: list[dict],
) -> dict:
	"""Persist a quantity-valid cross-midnight Segment/proposal/WOS probe."""
	probe_suffix = "{0}-SHIFT".format(context["suffix"])
	plant_floor = _create_plant_floor(
		context["company"],
		context["fg_warehouse"],
		context["raw_warehouse"],
		context["fg_warehouse"],
		probe_suffix,
	)
	workstation = _create_workstation(
		context["company"],
		plant_floor,
		context["fg_warehouse"],
		probe_suffix,
		"N",
	)
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": context["company"],
			"plant_floor": plant_floor,
			"planning_date": today(),
			"horizon_start": start,
			"horizon_end": end,
			"horizon_days": 2,
			"run_type": "Trial",
			"existing_work_order_policy": "Exclude",
			"status": "Planned",
			"approval_state": "Pending",
			"notes": f"{MARKER}: persisted cross-midnight release probe",
		}
	).insert(ignore_permissions=True)
	result = frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": run.name,
			"company": context["company"],
			"plant_floor": plant_floor,
			"item_code": context["flow_item"],
			"requested_date": getdate(end),
			"demand_source": "Safety Stock",
			"production_strategy": "Force Prebuild",
			"planned_qty": 140,
			"prebuild_qty": 140,
			"machine_scheduled_qty": 140,
			"scheduled_qty": 140,
			"unscheduled_qty": 0,
			"status": "Planned",
			"risk_status": "Normal",
			"segments": [
				{
					"workstation": workstation,
					"plant_floor": plant_floor,
					"start_time": start,
					"end_time": end,
					"planned_qty": 140,
					"production_mode": "Prebuild",
					"sequence_no": 1,
					"segment_kind": "Primary",
					"segment_status": "Planned",
				}
			],
		}
	).insert(ignore_permissions=True)
	segment_name = _single_value(
		"APS Schedule Segment",
		{"parent": result.name, "parenttype": "APS Schedule Result"},
		"name",
	)
	wo_batch = frappe.get_doc(
		{
			"doctype": "APS Work Order Proposal Batch",
			"planning_run": run.name,
			"company": context["company"],
			"plant_floor": plant_floor,
			"proposal_date": today(),
			"proposal_fingerprint": hashlib.sha256(
				("{0}|cross-midnight-wo".format(run.name)).encode("utf-8")
			).hexdigest(),
			"items": [
				{
					"result_reference": result.name,
					"item_code": context["flow_item"],
					"required_delivery_date": getdate(end),
					"action": "New",
					"proposed_qty": 140,
					"target_start_time": start,
					"target_end_time": end,
					"review_status": "Pending",
				}
			],
		}
	)
	wo_batch.flags.proposal_engine_transition = True
	wo_batch.insert(ignore_permissions=True)
	work_order = planning._create_formal_work_order(
		run,
		result,
		140,
		start,
		end,
		planning.get_settings_dict(),
		wo_batch.name,
		sales_order=None,
		sales_order_item=None,
	)
	wo_batch.items[0].target_work_order = work_order
	wo_batch.items[0].review_status = "Applied"
	wo_batch.flags.proposal_engine_transition = True
	wo_batch.save(ignore_permissions=True)

	shift_batch = frappe.get_doc(
		{
			"doctype": "APS Shift Schedule Proposal Batch",
			"planning_run": run.name,
			"company": context["company"],
			"plant_floor": plant_floor,
			"work_order_proposal_batch": wo_batch.name,
			"proposal_date": today(),
			"proposal_fingerprint": hashlib.sha256(
				("{0}|cross-midnight-shift".format(run.name)).encode("utf-8")
			).hexdigest(),
			"items": [
				{
					"result_reference": result.name,
					"segment_reference": segment_name,
					"action": "New",
					"item_code": context["flow_item"],
					"work_order": work_order,
					"plant_floor": plant_floor,
					"posting_date": row.get("posting_date"),
					"shift_type": row.get("shift_type"),
					"workstation": workstation,
					"planned_start_time": row.get("start_time"),
					"planned_end_time": row.get("end_time"),
					"planned_qty": row.get("planned_qty"),
					"review_status": "Approved",
				}
				for row in slices
			],
		}
	)
	shift_batch.flags.proposal_engine_transition = True
	shift_batch.insert(ignore_permissions=True)
	for row in shift_batch.items:
		applied = planning._upsert_formal_shift_scheduling(shift_batch, row)
		row.target_scheduling = applied.get("docname")
		row.review_status = "Applied"
		row.review_note = applied.get("message")
	shift_batch.flags.proposal_engine_transition = True
	shift_batch.save(ignore_permissions=True)
	chain = _assert_persisted_segment_proposal_wos_chain(
		run.name,
		shift_batch.name,
		require_cross_midnight=True,
	)
	return {
		"run": run.name,
		"result": result.name,
		"segment": segment_name,
		"work_order_proposal_batch": wo_batch.name,
		"work_order": work_order,
		"shift_schedule_proposal_batch": shift_batch.name,
		"lineage": chain,
	}


def _scenario_machine_downtime(context: dict, full_chain: dict) -> dict:
	segment = frappe.get_doc("APS Schedule Segment", full_chain["released_scheduling_rows"][0]["custom_aps_segment_reference"])
	window = frappe.get_doc(
		{
			"doctype": "APS Downtime Window",
			"company": context["company"],
			"scope": "Workstation",
			"plant_floor": context["plant_floor"],
			"workstation": segment.workstation,
			"start_time": segment.start_time,
			"end_time": min(get_datetime(segment.end_time), get_datetime(segment.start_time) + timedelta(minutes=20)),
			"available_capacity_percent": 0,
			"reason": "Phase 6 machine stop replay",
			"status": "Active",
			"planning_run": full_chain["run"],
			"notes": f"{MARKER}: machine downtime replay",
		}
	).insert(ignore_permissions=True)
	impact = planning.preview_schedule_impact(run_name=full_chain["run"], downtime_window=window.name)
	if not impact.get("blockers") and not impact.get("affected_count"):
		_phase6_fail("machine_downtime", "APS Downtime Window", window.name, "blockers or affected segments", "no impact", "")
	window.status = "Cancelled"
	window.save(ignore_permissions=True)
	return {
		"scenario": "machine_stops_suddenly",
		"passed": True,
		"downtime_window": window.name,
		"impact": impact,
	}


def _scenario_duplicate_rows(context: dict) -> dict:
	scope = f"{PREFIX}DUP-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 2))
	rows = [
		_schedule_row(item, 40, due_date, source_excel_row=7),
		_schedule_row(item, 60, due_date, source_excel_row=8),
	]
	blocked = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V1",
		schedule_scope=scope,
		import_strategy="Replace Scope",
		duplicate_policy="Block",
		rows_json=rows,
	)
	_assert_equal("duplicate_block_can_import", "APS Schedule Import Batch", scope, False, bool(blocked["can_import"]))
	summed = planning.preview_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V1",
		schedule_scope=scope,
		import_strategy="Replace Scope",
		duplicate_policy="Sum",
		rows_json=rows,
	)
	_assert_qty("duplicate_sum_qty", "APS Schedule Import Batch", scope, 100, summed["effective_schedule_rows"][0]["qty"])
	return {"scenario": "duplicate_rows_in_schedule_file", "passed": True, "blocked": blocked, "summed": summed}


def _scenario_repeat_import(context: dict) -> dict:
	scope = f"{PREFIX}REPEAT-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 3))
	rows = [_schedule_row(item, 25, due_date, source_excel_row=9)]
	first = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V1",
		schedule_scope=scope,
		import_strategy="Append",
		duplicate_policy="Block",
		rows_json=rows,
	)
	second = planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no="V2",
		schedule_scope=scope,
		import_strategy="Append",
		duplicate_policy="Block",
		rows_json=rows,
	)
	_assert_qty("repeat_import_idempotent", "APS Schedule Import Batch", second.get("import_batch"), 1, second.get("idempotent_replay"))
	_assert_equal("repeat_import_batch", "APS Schedule Import Batch", second.get("import_batch"), first.get("import_batch"), second.get("import_batch"))
	return {"scenario": "same_file_repeated_import", "passed": True, "first": first, "second": second}


def _scenario_delivery_note_cancel_return(context: dict) -> dict:
	scope = f"{PREFIX}DN-CANCEL-{context['suffix']}"
	item = context["return_item"]
	due_date = getdate(add_days(today(), 2))
	import_result = _import_scope(context, scope, item, 30, due_date, version="V1")
	schedule_item = _single_value(
		"Customer Delivery Schedule Item",
		{"parent": import_result["schedule"], "item_code": item},
		"name",
	)
	delivery_note = _create_delivery_note(
		{**context, "flow_item": item},
		schedule_item=schedule_item,
		qty=30,
		sequence=31,
	)
	first_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[item],
	)
	_assert_qty("delivery_note_initial_qty", "Delivery Note", delivery_note, 30, first_sync["rollup"]["delivered_qty"])
	frappe.get_doc("Delivery Note", delivery_note).cancel()
	cancel_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[item],
	)
	_assert_qty("delivery_note_cancel_qty", "Delivery Note", delivery_note, 0, cancel_sync["rollup"]["delivered_qty"])
	replacement_delivery = _create_delivery_note(
		{**context, "flow_item": item},
		schedule_item=schedule_item,
		qty=20,
		sequence=32,
	)
	replacement_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[item],
	)
	_assert_qty(
		"delivery_note_replacement_qty",
		"Delivery Note",
		replacement_delivery,
		20,
		replacement_sync["rollup"]["delivered_qty"],
	)
	return_note = _create_delivery_return(replacement_delivery, schedule_item=schedule_item)
	return_sync = delivery_sync.sync_delivery_allocations(
		company=context["company"],
		customer=context["customer"],
		item_codes=[item],
	)
	_assert_qty("delivery_note_return_qty", "Delivery Note", return_note, 0, return_sync["rollup"]["delivered_qty"])
	return {
		"scenario": "delivery_note_cancel_or_return",
		"passed": True,
		"delivery_note": delivery_note,
		"first_sync": first_sync,
		"cancel_sync": cancel_sync,
		"replacement_delivery_note": replacement_delivery,
		"replacement_sync": replacement_sync,
		"return_delivery_note": return_note,
		"return_sync": return_sync,
	}


def _scenario_transaction_rollback(context: dict) -> dict:
	scope = f"{PREFIX}TXN-{context['suffix']}"
	item = context["scenario_item"]
	due_date = getdate(add_days(today(), 4))
	_import_scope(context, scope, item, 20, due_date, version="V1")
	before = _transaction_scope_snapshot(context["company"], item, scope)
	with patch(
		"injection_aps.services.planning.rebuild_demand_pool",
		side_effect=RuntimeError("PHASE6 forced rollback"),
	):
		try:
			planning.import_customer_delivery_schedule(
				customer=context["customer"],
				company=context["company"],
				version_no="V2",
				schedule_scope=scope,
				import_strategy="Replace Scope",
				duplicate_policy="Block",
				rows_json=[_schedule_row(item, 35, due_date, source_excel_row=12)],
				rebuild=1,
				existing_work_order_policy="Exclude",
			)
		except RuntimeError:
			pass
		else:
			_phase6_fail("transaction_rollback", "APS Schedule Import Batch", scope, "forced rollback exception", "success", "")
	after = _transaction_scope_snapshot(context["company"], item, scope)
	_assert_equal("transaction_scope_fully_rolled_back", "APS Schedule Import Batch", scope, before, after)
	return {"scenario": "mid_import_failure_rolls_back", "passed": True, "before": before, "after": after}


def _transaction_scope_snapshot(company: str, item_code: str, schedule_scope: str) -> dict:
	batch_rows = frappe.get_all(
		"APS Schedule Import Batch",
		filters={"company": company, "schedule_scope": schedule_scope},
		fields=[
			"name",
			"status",
			"import_fingerprint",
			"schedule_reference",
			"imported_rows",
			"effective_rows",
			"post_import_total_qty",
		],
		order_by="name",
		limit_page_length=0,
	)
	schedule_rows = frappe.get_all(
		"Customer Delivery Schedule",
		filters={"company": company, "schedule_scope": schedule_scope},
		fields=["name", "status", "version_no", "schedule_total_qty", "import_batch"],
		order_by="name",
		limit_page_length=0,
	)
	schedule_names = [row.name for row in schedule_rows]
	batch_names = [row.name for row in batch_rows]
	return _jsonable(
		{
			"batches": batch_rows,
			"schedules": schedule_rows,
			"schedule_items": (
				frappe.get_all(
					"Customer Delivery Schedule Item",
					filters={"parent": ("in", schedule_names)},
					fields=[
						"name",
						"parent",
						"item_code",
						"sales_order",
						"schedule_date",
						"qty",
						"produced_qty",
						"delivered_qty",
						"status",
					],
					order_by="parent, idx, name",
					limit_page_length=0,
				)
				if schedule_names
				else []
			),
			"demand_deltas": (
				frappe.get_all(
					"APS Demand Delta",
					filters={"import_batch": ("in", batch_names)},
					fields=[
						"name",
						"import_batch",
						"change_type",
						"item_code",
						"previous_qty",
						"current_qty",
						"delta_qty",
					],
					order_by="name",
					limit_page_length=0,
				)
				if batch_names
				else []
			),
			"demand_pool": frappe.get_all(
				"APS Demand Pool",
				filters={"company": company, "item_code": item_code},
				fields=["name", "demand_date", "qty", "status", "source_doctype", "source_name"],
				order_by="name",
				limit_page_length=0,
			),
			"net_requirements": frappe.get_all(
				"APS Net Requirement",
				filters={"company": company, "item_code": item_code},
				fields=[
					"name",
					"demand_date",
					"demand_qty",
					"available_stock_qty",
					"open_work_order_qty",
					"net_requirement_qty",
					"planning_qty",
				],
				order_by="name",
				limit_page_length=0,
			),
		}
	)


def _run_ui_checks(context: dict, full_chain: dict) -> dict:
	run_name = full_chain["run"]
	gantt = app.get_schedule_gantt_data(run_name)
	release = app.get_release_center_data(run_name)
	progress = planning.get_customer_schedule_progress_data(
		company=context["company"],
		customer=context["customer"],
		item_code=context["flow_item"],
		schedule_scope=f"{PREFIX}FLOW-{context['suffix']}",
		run_name=run_name,
	)
	_assert_positive("ui_gantt_tasks", "APS Planning Run", run_name, len(gantt.get("tasks") or []))
	_assert_positive("ui_release_batches", "APS Planning Run", run_name, len(release.get("release_batches") or []))
	_assert_qty("ui_gantt_delivered", "APS Planning Run", run_name, 120, gantt["quantity_summary"].get("delivered_qty"))
	_assert_qty("ui_release_delivered", "APS Planning Run", run_name, 120, release["quantity_summary"].get("delivered_qty"))
	_assert_qty("ui_progress_delivered", "APS Planning Run", run_name, 120, progress["summary"].get("delivered_qty"))
	translation_path = Path(__file__).resolve().parents[1] / "translations" / "zh.csv"
	translation_text = translation_path.read_text(encoding="utf-8")
	required_terms = [
		"机台排程看板",
		"释放批次",
		"排程段",
		"客户排期目标",
	]
	missing = [term for term in required_terms if term not in translation_text]
	if missing:
		_phase6_fail("zh_translation_check", "File", str(translation_path), "all required Chinese terms", ", ".join(missing), "")
	return {
		"passed": True,
		"gantt_task_count": len(gantt.get("tasks") or []),
		"release_batch_count": len(release.get("release_batches") or []),
		"progress_rows": progress["summary"].get("rows"),
		"page_quantity_comparison": full_chain["page_comparison"],
		"translation_file": str(translation_path),
		"required_terms": required_terms,
	}


def _ensure_master_data(attestation: dict) -> dict:
	suffix = frappe.generate_hash(length=8).upper()
	configured_company = str(attestation.get("test_company") or "").strip()
	company = (
		frappe.db.get_value("Company", configured_company, "name")
		if configured_company
		else None
	)
	if not company:
		_phase6_fail(
			"master_data",
			"Company",
			"signed test_company",
			"the dedicated company from the signed environment attestation",
			configured_company or "missing",
			"",
		)
	parent_warehouse = _root_warehouse(company)
	if not parent_warehouse:
		_phase6_fail(
			"master_data",
			"Warehouse",
			company,
			"a root warehouse for the dedicated test company",
			"missing",
			"",
		)
	item_group = _ensure_item_group()
	stock_uom = frappe.db.get_value("UOM", "Nos", "name") or frappe.db.get_value("UOM", {}, "name")
	customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	if not all((parent_warehouse, item_group, stock_uom, customer_group, territory)):
		_phase6_fail("master_data", "Company", company, "warehouse/item group/uom/customer group/territory", "missing", "")

	customer = _create_customer(customer_group, territory, suffix)
	raw_warehouse = _create_warehouse(company, parent_warehouse, suffix, "RM")
	fg_warehouse = _create_warehouse(
		company,
		parent_warehouse,
		suffix,
		"FG",
		capacity_qty=10000,
	)
	plant_floor = _create_plant_floor(company, fg_warehouse, raw_warehouse, fg_warehouse, suffix)
	workstations = [
		_create_workstation(company, plant_floor, fg_warehouse, suffix, "A"),
		_create_workstation(company, plant_floor, fg_warehouse, suffix, "B"),
	]
	for index, workstation in enumerate(workstations, start=1):
		_create_machine_capability(workstation, plant_floor, index)
	raw_item = _create_item(f"{PREFIX}RM-{suffix}", item_group, stock_uom, raw_warehouse, is_fg=False)
	flow_item = _create_item(f"{PREFIX}FLOW-{suffix}", item_group, stock_uom, fg_warehouse)
	scenario_item = _create_item(f"{PREFIX}SCENARIO-{suffix}", item_group, stock_uom, fg_warehouse)
	return_item = _create_item(f"{PREFIX}RETURN-{suffix}", item_group, stock_uom, fg_warehouse)
	initial_stock_entries = [_create_initial_stock(
		company,
		raw_item,
		raw_warehouse,
		stock_uom,
		qty=10000,
		suffix=suffix,
	)]
	for index, item in enumerate((scenario_item, return_item), start=1):
		initial_stock_entries.append(
			_create_initial_stock(
				company,
				item,
				fg_warehouse,
				stock_uom,
				qty=100,
				suffix=f"{suffix}-FG-{index}",
			)
		)
	for item in (flow_item, scenario_item, return_item):
		_create_bom(item, raw_item, company, raw_warehouse)
		_create_mold(item, company, fg_warehouse, suffix, "A")
		_create_mold(item, company, fg_warehouse, suffix, "B")
	sales_order, sales_order_item = _create_sales_order(
		company=company,
		customer=customer,
		item_code=flow_item,
		warehouse=fg_warehouse,
		stock_uom=stock_uom,
		qty=120,
		delivery_date=getdate(add_days(today(), 1)),
		suffix=suffix,
	)
	settings = frappe.get_single("APS Settings")
	settings.default_company = company
	settings.default_plant_floor = plant_floor
	settings.planning_horizon_days = 7
	settings.release_horizon_days = 7
	settings.default_hourly_capacity_qty = 100
	settings.minimum_parallel_split_qty = 1
	settings.default_setup_minutes = 0
	settings.save(ignore_permissions=True)
	return {
		"suffix": suffix,
		"company": company,
		"warehouse": fg_warehouse,
		"raw_warehouse": raw_warehouse,
		"fg_warehouse": fg_warehouse,
		"item_group": item_group,
		"stock_uom": stock_uom,
		"customer": customer,
		"plant_floor": plant_floor,
		"workstations": workstations,
		"raw_item": raw_item,
		"initial_stock_entries": initial_stock_entries,
		"flow_item": flow_item,
		"scenario_item": scenario_item,
		"return_item": return_item,
		"sales_order": sales_order,
		"sales_order_item": sales_order_item,
	}


def _ensure_item_group() -> str:
	if frappe.db.exists("Item Group", "Plastic Part"):
		return "Plastic Part"
	parent = frappe.db.get_value("Item Group", {"is_group": 1}, "name") or "All Item Groups"
	frappe.get_doc(
		{
			"doctype": "Item Group",
			"item_group_name": "Plastic Part",
			"parent_item_group": parent,
			"is_group": 0,
		}
	).insert(ignore_permissions=True)
	return "Plastic Part"


def _root_warehouse(company: str) -> str | None:
	return (
		frappe.db.get_value("Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ["is", "not set"]}, "name")
		or frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
	)


def _create_customer(customer_group: str, territory: str, suffix: str) -> str:
	name = f"{PREFIX}CUSTOMER-{suffix}"
	doc = frappe.new_doc("Customer")
	doc.name = name
	doc.customer_name = name
	doc.customer_type = "Company"
	doc.customer_group = customer_group
	doc.territory = territory
	if frappe.get_meta("Customer").has_field("custom_customer_abbreviation"):
		doc.custom_customer_abbreviation = f"P6-{suffix[:6]}"
	doc.insert(ignore_permissions=True)
	return doc.name


def _create_warehouse(
	company: str,
	parent_warehouse: str | None,
	suffix: str,
	code: str,
	*,
	capacity_qty: float = 0,
) -> str:
	name = f"{PREFIX}{code}-{suffix}"
	doc = frappe.new_doc("Warehouse")
	doc.warehouse_name = name
	doc.company = company
	doc.parent_warehouse = parent_warehouse
	doc.is_group = 0
	if frappe.get_meta("Warehouse").has_field("custom_aps_capacity_qty"):
		doc.custom_aps_capacity_qty = capacity_qty
	doc.insert(ignore_permissions=True)
	return doc.name


def _create_plant_floor(company: str, warehouse: str, source_warehouse: str, fg_warehouse: str, suffix: str) -> str:
	name = f"{PREFIX}FLOOR-{suffix}"
	doc = frappe.get_doc(
		{
			"doctype": "Plant Floor",
			"floor_name": name,
			"company": company,
			"warehouse": warehouse,
		}
	)
	meta = frappe.get_meta("Plant Floor")
	if meta.has_field("custom_default_source_warehouse"):
		doc.custom_default_source_warehouse = source_warehouse
	if meta.has_field("custom_default_finished_goods_warehouse"):
		doc.custom_default_finished_goods_warehouse = fg_warehouse
	if meta.has_field("custom_default_scrap_warehouse"):
		doc.custom_default_scrap_warehouse = source_warehouse
	doc.insert(ignore_permissions=True)
	return doc.name


def _create_workstation(company: str, plant_floor: str, warehouse: str, suffix: str, code: str) -> str:
	name = f"{PREFIX}MC-{code}-{suffix}"
	doc = frappe.get_doc(
		{
			"doctype": "Workstation",
			"workstation_name": name,
			"plant_floor": plant_floor,
			"warehouse": warehouse,
			"production_capacity": 1,
			"status": "Idle",
		}
	).insert(ignore_permissions=True)
	return doc.name


def _create_machine_capability(workstation: str, plant_floor: str, sequence: int) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "APS Machine Capability",
			"workstation": workstation,
			"plant_floor": plant_floor,
			"machine_tonnage": 120,
			"risk_category": "",
			"hourly_capacity_qty": 100,
			"daily_capacity_qty": 800,
			"queue_sequence": sequence,
			"machine_status": "Available",
			"max_run_hours": 1,
			"is_active": 1,
			"sync_source": MARKER,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _create_item(item_code: str, item_group: str, stock_uom: str, warehouse: str, is_fg: bool = True) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"description": f"{MARKER} test item",
			"item_group": item_group,
			"stock_uom": stock_uom,
			"is_stock_item": 1,
			"include_item_in_manufacturing": 1,
			"valuation_rate": 1,
			"custom_aps_prebuild_allowed": 1 if is_fg else 0,
			"custom_aps_max_prebuild_days": 2 if is_fg else 0,
			"custom_aps_cancellation_risk_percent": 0,
			"custom_aps_max_stock_qty": 10000,
			"item_defaults": [
				{
					"company": frappe.db.get_value("Warehouse", warehouse, "company"),
					"default_warehouse": warehouse,
				}
			],
		}
	).insert(ignore_permissions=True)
	return doc.name


def _create_bom(item_code: str, raw_item: str, company: str, warehouse: str) -> str:
	currency = frappe.db.get_value("Company", company, "default_currency") or "USD"
	raw_uom = frappe.db.get_value("Item", raw_item, "stock_uom")
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": item_code,
			"quantity": 1,
			"company": company,
			"currency": currency,
			"is_active": 1,
			"is_default": 1,
			"items": [
				{
					"item_code": raw_item,
					"qty": 1,
					"uom": raw_uom,
					"stock_uom": raw_uom,
					"rate": 1,
					"source_warehouse": warehouse,
				}
			],
		}
	)
	if frappe.get_meta("BOM").has_field("custom_temporary_bom"):
		bom.custom_temporary_bom = "No"
	bom.insert(ignore_permissions=True)
	bom.submit()
	frappe.db.set_value("Item", item_code, "default_bom", bom.name, update_modified=False)
	return bom.name


def _create_initial_stock(
	company: str,
	item_code: str,
	warehouse: str,
	stock_uom: str,
	*,
	qty: float,
	suffix: str,
) -> str:
	"""Create test stock through ERPNext's submitted Stock Entry lifecycle."""
	doc = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"purpose": "Material Receipt",
			"company": company,
			"posting_date": today(),
			"posting_time": nowtime(),
			"remarks": f"{MARKER}: initial isolated stock {suffix}",
			"items": [
				{
					"item_code": item_code,
					"t_warehouse": warehouse,
					"qty": qty,
					"transfer_qty": qty,
					"uom": stock_uom,
					"stock_uom": stock_uom,
					"conversion_factor": 1,
					"basic_rate": 1,
				}
			],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _create_sales_order(
	*,
	company: str,
	customer: str,
	item_code: str,
	warehouse: str,
	stock_uom: str,
	qty: float,
	delivery_date,
	suffix: str,
) -> tuple[str, str]:
	currency = frappe.db.get_value("Company", company, "default_currency") or "USD"
	so_name = f"{PREFIX}SO-{suffix}"
	doc = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": company,
			"customer": customer,
			"transaction_date": today(),
			"delivery_date": delivery_date,
			"currency": currency,
			"conversion_rate": 1,
			"plc_conversion_rate": 1,
			"selling_price_list": frappe.db.get_value("Price List", {"selling": 1}, "name") or "",
			"items": [
				{
					"item_code": item_code,
					"delivery_date": delivery_date,
					"qty": qty,
					"uom": stock_uom,
					"stock_uom": stock_uom,
					"conversion_factor": 1,
					"warehouse": warehouse,
					"rate": 1,
				}
			],
		}
	)
	doc.insert(ignore_permissions=True, set_name=so_name)
	doc.submit()
	return doc.name, doc.items[0].name


def _create_mold(item_code: str, company: str, warehouse: str, suffix: str, code: str) -> str:
	mold = frappe.get_doc(
		{
			"doctype": "Mold",
			"mold_name": f"{PREFIX}MOLD-{code}-{item_code}-{suffix}",
			"company": company,
			"ownership_type": "Company",
			"default_warehouse": warehouse,
			"current_warehouse": warehouse,
			"mold_type": "INJ",
			"cavity_count": 1,
			"is_family_mold": 0,
			"standard_cycle_seconds": 60,
			"machine_tonnage": 80,
			"status": "Active",
			"mold_products": [
				{
					"item_code": item_code,
					"output_group": "Default",
					"configuration_label": code,
					"priority": 1 if code == "A" else 2,
					"is_default_product": 1 if code == "A" else 0,
					"output_qty": 1,
					"cavity_output_qty": 1,
					"cycle_time_seconds": 60,
				}
			],
		}
	)
	mold.insert(ignore_permissions=True)
	mold.submit()
	frappe.db.set_value("Mold", mold.name, "status", "Active", update_modified=False)
	return mold.name


def _ensure_capacity_applied(run_name: str) -> dict:
	status = frappe.db.get_value("APS Planning Run", run_name, "capacity_balance_status")
	if status == "Applied":
		return {"status": "Applied", "idempotent_replay": 1}
	analysis = capacity_balance.analyze_capacity_balance(run_name, persist=True)
	summary = analysis.get("summary") or {}
	if summary.get("blocked_demands") or summary.get("unscheduled_qty"):
		_raise_capacity_failure(run_name, analysis)
	status = frappe.db.get_value("APS Planning Run", run_name, "capacity_balance_status")
	if status == "Applied":
		return {"status": "Applied", "analysis": analysis, "idempotent_replay": 1}
	if status not in ("Suggestion Ready", "Confirmation Required"):
		_phase6_fail(
			"capacity_balance_status",
			"APS Planning Run",
			run_name,
			"Suggestion Ready, Confirmation Required, or Applied",
			status,
			"",
			details={"summary": summary, "demands": analysis.get("demands") or []},
		)
	if summary.get("requires_confirmation"):
		capacity_balance.confirm_capacity_balance(run_name)
	try:
		return capacity_balance.apply_capacity_balance(run_name, pmc_confirmed=1)
	except Exception:
		latest_status = frappe.db.get_value("APS Planning Run", run_name, "capacity_balance_status")
		_phase6_fail(
			"capacity_balance_apply",
			"APS Planning Run",
			run_name,
			"Applied",
			latest_status,
			"",
			details={"summary": summary, "demands": analysis.get("demands") or []},
		)


def _ensure_capacity_current(run_name: str, *, reason: str) -> dict:
	try:
		analysis = capacity_balance.assert_applied_capacity_current(run_name, lock_rows=True)
		return {"status": "Applied", "current": 1, "reason": reason, "summary": analysis.get("summary") or {}}
	except frappe.ValidationError as exc:
		capacity_balance.invalidate_capacity_balance(run_name)
		application = _ensure_capacity_applied(run_name)
		analysis = capacity_balance.assert_applied_capacity_current(run_name, lock_rows=True)
		return {
			"status": "Applied",
			"current": 0,
			"reason": reason,
			"previous_error": str(exc),
			"application": application,
			"summary": analysis.get("summary") or {},
		}


def _assert_planned_jit_capacity(run_name: str, *, due_date, expected_qty: float) -> dict:
	"""Verify persisted Result and Segment plan quantities use the natural due day."""
	results = frappe.get_all(
		"APS Schedule Result",
		filters={"planning_run": run_name},
			fields=[
				"name",
				"requested_date",
				"production_strategy",
			"planned_qty",
			"prebuild_qty",
			"jit_qty",
			"late_qty_after_balance",
			"unscheduled_qty",
		],
		order_by="requested_date asc, name asc",
		limit_page_length=0,
	)
	if not results:
		_phase6_fail("planned_jit_results", "APS Planning Run", run_name, "> 0", 0, 0)
	result_names = [row.name for row in results]
	if len(result_names) != len(set(result_names)):
		_phase6_fail(
			"planned_jit_result_collection",
			"APS Planning Run",
			run_name,
			"unique Result names",
			result_names,
			"",
		)
	segments = frappe.get_all(
		"APS Schedule Segment",
		filters={"parent": ("in", result_names), "parenttype": "APS Schedule Result"},
		fields=[
			"name",
			"parent",
			"workstation",
			"start_time",
			"end_time",
			"planned_qty",
			"production_mode",
			"segment_kind",
			"segment_status",
		],
		order_by="start_time asc, idx asc",
		limit_page_length=0,
	)
	segment_names = [row.name for row in segments]
	if len(segment_names) != len(set(segment_names)):
		_phase6_fail(
			"planned_jit_segment_collection",
			"APS Planning Run",
			run_name,
			"unique Segment names",
			segment_names,
			"",
		)
	for segment in segments:
		if segment.parent not in set(result_names):
			_phase6_fail(
				"planned_jit_segment_parent",
				"APS Schedule Segment",
				segment.name,
				result_names,
				segment.parent,
				"",
			)
	effective_segments = [row for row in segments if consistency.is_effective_primary_segment(row)]
	due_start = get_datetime(f"{getdate(due_date)} 00:00:00")
	due_end = due_start + timedelta(days=1)
	_assert_qty(
		"planned_jit_result_total",
		"APS Planning Run",
		run_name,
		expected_qty,
		sum(flt(row.planned_qty) for row in results),
	)
	result_evidence = {}
	for result in results:
		actual_requested_date = getdate(result.requested_date) if result.requested_date else None
		if actual_requested_date != getdate(due_date) or result.production_strategy != "Force JIT":
			_phase6_fail(
				"planned_jit_result_identity",
				"APS Schedule Result",
				result.name,
				{"requested_date": getdate(due_date), "production_strategy": "Force JIT"},
				{
					"requested_date": actual_requested_date,
					"production_strategy": result.production_strategy,
				},
				"",
			)
		result_segments = [row for row in effective_segments if row.parent == result.name]
		result_segment_qty = sum(flt(row.planned_qty) for row in result_segments)
		_assert_qty(
			"planned_jit_result_planned_equals_jit",
			"APS Schedule Result",
			result.name,
			result.planned_qty,
			result.jit_qty,
		)
		_assert_qty(
			"planned_jit_result_segment_quantity",
			"APS Schedule Result",
			result.name,
			result.planned_qty,
			result_segment_qty,
		)
		if flt(result.planned_qty) > QTY_TOLERANCE and not result_segments:
			_phase6_fail(
				"planned_jit_result_segment_collection",
				"APS Schedule Result",
				result.name,
				"at least one effective primary Segment",
				0,
				"",
			)
		result_evidence[result.name] = {
			"requested_date": actual_requested_date,
			"planned_qty": flt(result.planned_qty),
			"jit_qty": flt(result.jit_qty),
			"segment_names": [row.name for row in result_segments],
			"segment_qty": result_segment_qty,
			"last_segment_end": max(
				(get_datetime(row.end_time) for row in result_segments),
				default=None,
			),
		}
	_assert_qty(
		"planned_jit_quantity",
		"APS Planning Run",
		run_name,
		expected_qty,
		sum(flt(row.jit_qty) for row in results),
	)
	for fieldname in ("prebuild_qty", "late_qty_after_balance", "unscheduled_qty"):
		_assert_qty(
			f"planned_jit_zero_{fieldname}",
			"APS Planning Run",
			run_name,
			0,
			sum(flt(row.get(fieldname)) for row in results),
		)
	_assert_qty(
		"planned_jit_segment_total",
		"APS Planning Run",
		run_name,
		expected_qty,
		sum(flt(row.planned_qty) for row in effective_segments),
	)
	for segment in effective_segments:
		start = get_datetime(segment.start_time)
		end = get_datetime(segment.end_time)
		if segment.production_mode != "JIT" or start < due_start or end > due_end:
			_phase6_fail(
				"planned_jit_segment_mode",
				"APS Schedule Segment",
				segment.name,
				{"mode": "JIT", "start_gte": due_start, "end_lte": due_end},
				{"mode": segment.production_mode, "start": start, "end": end},
				"",
			)
	return {
		"due_start": due_start,
		"due_end": due_end,
		"result_count": len(results),
		"segment_count": len(effective_segments),
		"planned_qty": sum(flt(row.planned_qty) for row in results),
		"jit_qty": sum(flt(row.jit_qty) for row in results),
		"results": result_evidence,
	}


def _assert_projected_jit_availability(
	run_name: str,
	*,
	due_date,
	expected_qty: float,
	expected_results: dict,
) -> dict:
	"""Verify the due-day projected ATP curve releases the planned JIT output."""
	due_start = get_datetime(f"{getdate(due_date)} 00:00:00")
	due_end = due_start + timedelta(days=1)
	projection = availability.get_run_fulfillment_projection(run_name, persist=False, as_of=due_start)
	projected_by_due = 0.0
	expected_by_result = 0.0
	point_count = 0
	projection_results = projection.get("results") or []
	projection_names = [str(row.get("result") or "") for row in projection_results]
	expected_names = sorted(str(name) for name in (expected_results or {}))
	if len(projection_names) != len(set(projection_names)) or sorted(projection_names) != expected_names:
		_phase6_fail(
			"planned_jit_availability_result_collection",
			"APS Planning Run",
			run_name,
			expected_names,
			projection_names,
			"",
		)
	result_cutoffs = {}
	for result in projection_results:
		result_name = str(result.get("result") or "")
		expected_result = expected_results[result_name]
		expected_segment_end = expected_result.get("last_segment_end")
		include_due_end = bool(
			expected_segment_end
			and get_datetime(expected_segment_end) == due_end
		)
		points = [
			row
			for row in result.get("timeline") or []
			if row.get("time")
			and due_start <= get_datetime(row.get("time"))
			and (
				get_datetime(row.get("time")) < due_end
				or (include_due_end and get_datetime(row.get("time")) == due_end)
			)
		]
		point_count += len(points)
		if not points:
			_phase6_fail(
				"planned_jit_availability_result_timeline",
				"APS Schedule Result",
				result.get("result") or "-",
				"at least one point inside the natural due day",
				0,
				"",
			)
		point_times = [get_datetime(row.get("time")) for row in points]
		if len(point_times) != len(set(point_times)):
			_phase6_fail(
				"planned_jit_availability_unique_cutoff_points",
				"APS Schedule Result",
				result_name,
				"unique natural-day timeline timestamps",
				point_times,
				"",
			)
		last_point = max(points, key=lambda row: get_datetime(row.get("time")))
		last_point_time = get_datetime(last_point.get("time"))
		if expected_segment_end and last_point_time < get_datetime(expected_segment_end):
			_phase6_fail(
				"planned_jit_availability_cutoff",
				"APS Schedule Result",
				result_name,
				{"at_or_after_last_segment_end": get_datetime(expected_segment_end), "before": due_end},
				last_point_time,
				"",
			)
		actual_result_atp = flt(last_point.get("projected_available_to_promise_qty"))
		expected_result_atp = flt(expected_result.get("planned_qty"))
		_assert_qty(
			"planned_jit_result_fulfillment_demand",
			"APS Schedule Result",
			result_name,
			expected_result_atp,
			result.get("fulfillment_demand_qty"),
		)
		_assert_qty(
			"planned_jit_result_projected_atp_by_due",
			"APS Schedule Result",
			result_name or "-",
			expected_result_atp,
			actual_result_atp,
		)
		projected_by_due += actual_result_atp
		expected_by_result += expected_result_atp
		result_cutoffs[result_name] = last_point_time
	_assert_positive("planned_jit_availability_points", "APS Planning Run", run_name, point_count)
	_assert_qty(
		"planned_jit_fulfillment_demand_by_due",
		"APS Planning Run",
		run_name,
		expected_qty,
		expected_by_result,
	)
	_assert_qty(
		"planned_jit_projected_atp_by_due",
		"APS Planning Run",
		run_name,
		expected_qty,
		projected_by_due,
	)
	return {
		"as_of": due_start,
		"due_end": due_end,
		"timeline_point_count": point_count,
		"projected_available_to_promise_qty": projected_by_due,
		"fulfillment_demand_qty": expected_by_result,
		"result_names": expected_names,
		"result_cutoffs": result_cutoffs,
	}


def _assert_released_shift_boundaries(run_name: str, scheduling_rows: list) -> dict:
	"""Verify formal WOS headers and rows preserve the fixed A/B shift windows."""
	wos_names = sorted({row.parent for row in scheduling_rows if row.get("parent")})
	headers = {
		row.name: row
		for row in frappe.get_all(
			"Work Order Scheduling",
			filters={"name": ("in", wos_names)},
			fields=["name", "posting_date", "shift_type", "custom_aps_run"],
			limit_page_length=0,
		)
	}
	for row in scheduling_rows:
		start = get_datetime(row.planned_start_date)
		end = get_datetime(row.planned_end_date)
		work_date = getdate(start)
		if 8 <= start.hour < 20:
			shift_start = get_datetime(f"{work_date} 08:00:00")
			shift_end = get_datetime(f"{work_date} 20:00:00")
			expected_shift = "白班"
		elif start.hour < 8:
			shift_start = get_datetime(f"{add_days(work_date, -1)} 20:00:00")
			shift_end = get_datetime(f"{work_date} 08:00:00")
			expected_shift = "晚班"
		else:
			shift_start = get_datetime(f"{work_date} 20:00:00")
			shift_end = get_datetime(f"{add_days(work_date, 1)} 08:00:00")
			expected_shift = "晚班"
		header = headers.get(row.parent)
		actual = {
			"start": start,
			"end": end,
			"shift_type": header.get("shift_type") if header else None,
			"posting_date": getdate(header.get("posting_date")) if header and header.get("posting_date") else None,
			"run": header.get("custom_aps_run") if header else None,
		}
		expected = {
			"start_gte": shift_start,
			"end_lte": shift_end,
			"shift_type": expected_shift,
			"posting_date": getdate(shift_start),
			"run": run_name,
		}
		if (
			not header
			or start < shift_start
			or end > shift_end
			or actual["shift_type"] != expected_shift
			or actual["posting_date"] != getdate(shift_start)
			or actual["run"] != run_name
		):
			_phase6_fail(
				"released_shift_boundary",
				"Scheduling Item",
				row.name,
				expected,
				actual,
				"",
			)
	return {
		"header_count": len(headers),
		"row_count": len(scheduling_rows),
		"shift_types": sorted({row.shift_type for row in headers.values()}),
	}


def _assert_persisted_segment_proposal_wos_chain(
	run_name: str,
	proposal_batch: str,
	*,
	require_cross_midnight: bool,
) -> dict:
	"""Prove the persisted Segment -> proposal item -> WOS row lineage."""
	proposal_rows = frappe.get_all(
		"APS Shift Schedule Proposal Item",
		filters={
			"parent": proposal_batch,
			"parenttype": "APS Shift Schedule Proposal Batch",
		},
		fields=[
			"name",
			"result_reference",
			"segment_reference",
			"action",
			"work_order",
			"plant_floor",
			"posting_date",
			"shift_type",
			"workstation",
			"planned_start_time",
			"planned_end_time",
			"planned_qty",
			"target_scheduling",
			"review_status",
		],
		order_by="idx asc",
		limit_page_length=0,
	)
	applied_rows = [
		row
		for row in proposal_rows
		if row.get("review_status") == "Applied" and row.get("action") != "Cancel Existing"
	]
	if not applied_rows:
		_phase6_fail(
			"persisted_shift_proposal_rows",
			"APS Shift Schedule Proposal Batch",
			proposal_batch,
			"> 0 applied non-cancel rows",
			0,
			0,
		)
	if len(applied_rows) != len([row for row in proposal_rows if row.get("action") != "Cancel Existing"]):
		_phase6_fail(
			"persisted_shift_proposal_review_state",
			"APS Shift Schedule Proposal Batch",
			proposal_batch,
			"all non-cancel rows Applied",
			[row.get("review_status") for row in proposal_rows],
			"",
		)

	segment_names = sorted({str(row.get("segment_reference") or "") for row in applied_rows})
	if "" in segment_names:
		_phase6_fail(
			"persisted_shift_proposal_segment_identity",
			"APS Shift Schedule Proposal Batch",
			proposal_batch,
			"non-empty segment references",
			segment_names,
			"",
		)
	segments = {
		row.name: row
		for row in frappe.get_all(
			"APS Schedule Segment",
			filters={"name": ("in", segment_names), "parenttype": "APS Schedule Result"},
			fields=[
				"name",
				"parent",
				"start_time",
				"end_time",
				"planned_qty",
				"linked_work_order",
				"linked_work_order_scheduling",
				"linked_scheduling_item",
			],
			limit_page_length=0,
		)
	}
	if set(segments) != set(segment_names):
		_phase6_fail(
			"persisted_shift_proposal_segment_set",
			"APS Shift Schedule Proposal Batch",
			proposal_batch,
			segment_names,
			sorted(segments),
			"",
		)

	scheduling_rows = frappe.get_all(
		"Scheduling Item",
		filters={"custom_aps_shift_proposal": proposal_batch, "custom_aps_run": run_name},
		fields=[
			"name",
			"parent",
			"work_order",
			"workstation",
			"scheduling_qty",
			"planned_start_date",
			"planned_end_date",
			"custom_aps_run",
			"custom_aps_result_reference",
			"custom_aps_segment_reference",
			"custom_aps_shift_proposal",
		],
		order_by="planned_start_date asc, name asc",
		limit_page_length=0,
	)
	wos_names = sorted({str(row.get("parent") or "") for row in scheduling_rows})
	headers = {
		row.name: row
		for row in frappe.get_all(
			"Work Order Scheduling",
			filters={"name": ("in", wos_names)},
			fields=["name", "posting_date", "shift_type", "custom_aps_run"],
			limit_page_length=0,
		)
	}
	matched_scheduling_names = set()
	proposal_qty_by_segment = {}
	scheduling_qty_by_segment = {}
	window_sets = {}
	cross_midnight_segments = set()
	for proposal in applied_rows:
		segment_name = str(proposal.get("segment_reference") or "")
		segment = segments[segment_name]
		start = get_datetime(proposal.get("planned_start_time"))
		end = get_datetime(proposal.get("planned_end_time"))
		segment_start = get_datetime(segment.get("start_time"))
		segment_end = get_datetime(segment.get("end_time"))
		if (
			proposal.get("result_reference") != segment.get("parent")
			or start < segment_start
			or end > segment_end
			or end <= start
		):
			_phase6_fail(
				"persisted_shift_proposal_segment_lineage",
				"APS Shift Schedule Proposal Item",
				proposal.name,
				{
					"result": segment.get("parent"),
					"start_gte": segment_start,
					"end_lte": segment_end,
				},
				{
					"result": proposal.get("result_reference"),
					"start": start,
					"end": end,
				},
				"",
			)
		matches = [
			row
			for row in scheduling_rows
			if row.get("custom_aps_segment_reference") == segment_name
			and row.get("custom_aps_result_reference") == proposal.get("result_reference")
			and row.get("work_order") == proposal.get("work_order")
			and row.get("workstation") == proposal.get("workstation")
			and get_datetime(row.get("planned_start_date")) == start
			and get_datetime(row.get("planned_end_date")) == end
			and abs(flt(row.get("scheduling_qty")) - flt(proposal.get("planned_qty"))) <= QTY_TOLERANCE
		]
		if len(matches) != 1 or matches[0].name in matched_scheduling_names:
			_phase6_fail(
				"persisted_shift_proposal_wos_mapping",
				"APS Shift Schedule Proposal Item",
				proposal.name,
				"exactly one unused Scheduling Item",
				[row.get("name") for row in matches],
				"",
			)
		matched = matches[0]
		matched_scheduling_names.add(matched.name)
		header = headers.get(matched.get("parent"))
		expected_posting_date = getdate(proposal.get("posting_date"))
		if (
			not header
			or proposal.get("target_scheduling") != matched.get("parent")
			or header.get("custom_aps_run") != run_name
			or getdate(header.get("posting_date")) != expected_posting_date
			or header.get("shift_type") != proposal.get("shift_type")
		):
			_phase6_fail(
				"persisted_shift_proposal_wos_header",
				"Scheduling Item",
				matched.name,
				{
					"target_scheduling": matched.get("parent"),
					"run": run_name,
					"posting_date": expected_posting_date,
					"shift_type": proposal.get("shift_type"),
				},
				{
					"target_scheduling": proposal.get("target_scheduling"),
					"run": header.get("custom_aps_run") if header else None,
					"posting_date": getdate(header.get("posting_date")) if header else None,
					"shift_type": header.get("shift_type") if header else None,
				},
				"",
			)
		proposal_qty_by_segment[segment_name] = proposal_qty_by_segment.get(segment_name, 0.0) + flt(
			proposal.get("planned_qty")
		)
		scheduling_qty_by_segment[segment_name] = scheduling_qty_by_segment.get(segment_name, 0.0) + flt(
			matched.get("scheduling_qty")
		)
		window_sets.setdefault(segment_name, set()).add(
			(start, end, expected_posting_date, proposal.get("shift_type"))
		)
		if start.date() < end.date():
			cross_midnight_segments.add(segment_name)
			shift_start, shift_end, expected_shift_type = planning._get_shift_window_for_time(start)
			if not (
				proposal.get("shift_type") == expected_shift_type
				and start >= shift_start
				and end <= shift_end
				and expected_posting_date == getdate(shift_start)
			):
				_phase6_fail(
					"persisted_cross_midnight_shift_window",
					"APS Shift Schedule Proposal Item",
					proposal.name,
					{
						"start_gte": shift_start,
						"end_lte": shift_end,
						"shift_type": expected_shift_type,
						"posting_date": getdate(shift_start),
					},
					{
						"start": start,
						"end": end,
						"shift_type": proposal.get("shift_type"),
						"posting_date": expected_posting_date,
					},
					"",
				)

	if matched_scheduling_names != {row.name for row in scheduling_rows}:
		_phase6_fail(
			"persisted_shift_proposal_wos_set",
			"APS Shift Schedule Proposal Batch",
			proposal_batch,
			sorted(matched_scheduling_names),
			sorted(row.name for row in scheduling_rows),
			"",
		)
	for segment_name, segment in segments.items():
		_assert_qty(
			"persisted_shift_proposal_segment_quantity",
			"APS Schedule Segment",
			segment_name,
			segment.get("planned_qty"),
			proposal_qty_by_segment.get(segment_name),
		)
		_assert_qty(
			"persisted_wos_segment_quantity",
			"APS Schedule Segment",
			segment_name,
			segment.get("planned_qty"),
			scheduling_qty_by_segment.get(segment_name),
		)
		segment_scheduling_rows = [
			row for row in scheduling_rows if row.get("custom_aps_segment_reference") == segment_name
		]
		linked_item = next(
			(
				row
				for row in segment_scheduling_rows
				if row.get("name") == segment.get("linked_scheduling_item")
			),
			None,
		)
		if (
			not linked_item
			or segment.get("linked_work_order") != linked_item.get("work_order")
			or segment.get("linked_work_order_scheduling") != linked_item.get("parent")
		):
			_phase6_fail(
				"persisted_segment_wos_backlink",
				"APS Schedule Segment",
				segment_name,
				"one exact linked Scheduling Item/Work Order/WOS from the proposal chain",
				{
					"linked_work_order": segment.get("linked_work_order"),
					"linked_work_order_scheduling": segment.get("linked_work_order_scheduling"),
					"linked_scheduling_item": segment.get("linked_scheduling_item"),
				},
				"",
			)
		windows = sorted(window_sets.get(segment_name) or set(), key=lambda row: (row[0], row[1]))
		if (
			not windows
			or windows[0][0] != get_datetime(segment.get("start_time"))
			or windows[-1][1] != get_datetime(segment.get("end_time"))
			or any(left[1] != right[0] for left, right in zip(windows, windows[1:]))
		):
			_phase6_fail(
				"persisted_shift_proposal_window_continuity",
				"APS Schedule Segment",
				segment_name,
				{"start": segment.get("start_time"), "end": segment.get("end_time"), "continuous": True},
				windows,
				"",
			)
	if require_cross_midnight and not cross_midnight_segments:
		_phase6_fail(
			"persisted_cross_midnight_segment_chain",
			"APS Shift Schedule Proposal Batch",
			proposal_batch,
			"> 0 cross-midnight Segment chains",
			0,
			0,
		)
	return {
		"proposal_batch": proposal_batch,
		"segment_count": len(segments),
		"proposal_row_count": len(applied_rows),
		"scheduling_row_count": len(scheduling_rows),
		"wos_count": len(headers),
		"cross_midnight_segments": sorted(cross_midnight_segments),
	}


def _raise_capacity_failure(run_name: str, analysis: dict) -> None:
	summary = analysis.get("summary") or {}
	demands = analysis.get("demands") or []
	first_blocked = next(
		(row for row in demands if row.get("status") == "Blocked" or flt(row.get("unscheduled_qty")) > QTY_TOLERANCE),
		{},
	)
	_phase6_fail(
		"capacity_balance",
		"APS Planning Run",
		run_name,
		"blocked_demands=0 and unscheduled_qty=0",
		"blocked_demands={0}, unscheduled_qty={1}".format(
			summary.get("blocked_demands"),
			summary.get("unscheduled_qty"),
		),
		flt(summary.get("unscheduled_qty")),
		details={
			"summary": summary,
			"first_blocked_demand": first_blocked,
		},
	)


def _approve_proposal_batch(doctype: str, name: str) -> None:
	doc = frappe.get_doc(doctype, name)
	for row in doc.get("items") or []:
		if row.review_status == "Pending":
			row.review_status = "Approved"
	doc.flags.proposal_engine_transition = True
	doc.save(ignore_permissions=True)


def _get_released_scheduling_rows(run_name: str) -> list:
	return frappe.get_all(
		"Scheduling Item",
		filters={"custom_aps_run": run_name},
		fields=[
			"name",
			"parent",
			"work_order",
			"workstation",
			"scheduling_qty",
			"planned_start_date",
			"planned_end_date",
			"custom_aps_result_reference",
			"custom_aps_segment_reference",
		],
		order_by="planned_start_date asc, name asc",
		limit_page_length=0,
	)


def _start_formal_scheduling_for_production(run_name: str, scheduling_rows: list) -> list[str]:
	wos_names = sorted({row.parent for row in scheduling_rows if row.get("parent")})
	for wos_name in wos_names:
		wos = frappe.db.get_value(
			"Work Order Scheduling",
			wos_name,
			["custom_aps_run", "custom_aps_approval_state", "status"],
			as_dict=True,
		)
		if not wos:
			_phase6_fail("start_formal_scheduling", "Work Order Scheduling", wos_name, "exists", None, "")
		if wos.custom_aps_run != run_name:
			_phase6_fail(
				"start_formal_scheduling", "Work Order Scheduling", wos_name, run_name, wos.custom_aps_run, ""
			)
		if wos.custom_aps_approval_state != "Approved":
			_phase6_fail(
				"start_formal_scheduling",
				"Work Order Scheduling",
				wos_name,
				"Approved",
				wos.custom_aps_approval_state,
				"",
			)
		wos_doc = frappe.get_doc("Work Order Scheduling", wos_name)
		wos_doc.status = "Manufacture"
		wos_doc.save(ignore_permissions=True)
	return wos_names


def _create_manufacture_entry(context: dict, run_name: str, scheduling_row, qty: float, sequence: int) -> str:
	from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

	values = make_stock_entry(scheduling_row.work_order, "Manufacture", qty=qty)
	doc = frappe.get_doc(values)
	doc.work_order_scheduling = scheduling_row.parent
	doc.posting_date = today()
	doc.posting_time = nowtime()
	doc.custom_aps_scheduling_item = scheduling_row.name
	doc.custom_aps_segment_reference = scheduling_row.custom_aps_segment_reference
	doc.custom_aps_output_type = "Good"
	doc.remarks = f"{MARKER}: submitted manufacture {context['suffix']}-{sequence}"
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _create_delivery_note(context: dict, *, schedule_item: str, qty: float, sequence: int) -> str:
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	target = frappe.db.get_value(
		"Customer Delivery Schedule Item",
		schedule_item,
		["item_code", "sales_order"],
		as_dict=True,
	)
	if not target:
		_phase6_fail("create_delivery_note", "Customer Delivery Schedule Item", schedule_item, "exists", None, "")
	if target.sales_order:
		doc = make_delivery_note(target.sales_order)
		if not getattr(doc, "doctype", None):
			doc = frappe.get_doc(doc)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Delivery Note",
				"company": context["company"],
				"customer": context["customer"],
				"items": [
					{
						"item_code": target.item_code,
						"warehouse": context["warehouse"],
						"qty": qty,
						"conversion_factor": 1,
					}
				],
			}
		)
	doc.posting_date = today()
	doc.posting_time = nowtime()
	matching_rows = [row for row in doc.items if row.item_code == target.item_code]
	if len(matching_rows) != 1:
		_phase6_fail(
			"create_delivery_note",
			"Delivery Note Item",
			target.sales_order or "-",
			f"one mapped line for {target.item_code}",
			len(matching_rows),
			"",
		)
	row = matching_rows[0]
	row.qty = qty
	row.stock_qty = qty
	row.custom_aps_customer_schedule_item = schedule_item
	doc.remarks = f"{MARKER}: submitted delivery {context['suffix']}-{sequence}"
	doc.run_method("set_missing_values")
	doc.run_method("calculate_taxes_and_totals")
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _create_delivery_return(source_delivery_note: str, *, schedule_item: str) -> str:
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

	doc = make_sales_return(source_delivery_note)
	if not getattr(doc, "doctype", None):
		doc = frappe.get_doc(doc)
	doc.posting_date = today()
	doc.posting_time = nowtime()
	doc.remarks = f"{MARKER}: submitted delivery return for {source_delivery_note}"
	for row in doc.items:
		row.custom_aps_customer_schedule_item = schedule_item
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _create_open_overlap_probe(context: dict, *, required_date) -> dict:
	probe_date = getdate(required_date)
	start = get_datetime(f"{probe_date} 00:20:00")
	end = start + timedelta(hours=1)
	run = frappe.get_doc(
		{
			"doctype": "APS Planning Run",
			"company": context["company"],
			"plant_floor": context["plant_floor"],
			"planning_date": today(),
			"horizon_start": start,
			"horizon_end": start + timedelta(days=2),
			"horizon_days": 2,
			"run_type": "Trial",
			"existing_work_order_policy": "Exclude",
			"status": "Planned",
			"approval_state": "Pending",
			"notes": f"{MARKER}: urgent displacement probe",
		}
	).insert(ignore_permissions=True)
	segments = [
		{
			"workstation": workstation,
			"plant_floor": context["plant_floor"],
			"start_time": start,
			"end_time": end,
			"planned_qty": 40,
			"sequence_no": index,
			"segment_kind": "Primary",
			"segment_status": "Planned",
		}
		for index, workstation in enumerate(context["workstations"], start=1)
	]
	result = frappe.get_doc(
		{
			"doctype": "APS Schedule Result",
			"planning_run": run.name,
			"company": context["company"],
			"plant_floor": context["plant_floor"],
			"customer": context["customer"],
			"item_code": context["flow_item"],
			"requested_date": getdate(required_date),
			"demand_source": "Customer Delivery Schedule",
			"planned_qty": sum(row["planned_qty"] for row in segments),
			"machine_scheduled_qty": sum(row["planned_qty"] for row in segments),
			"scheduled_qty": sum(row["planned_qty"] for row in segments),
			"unscheduled_qty": 0,
			"status": "Planned",
			"risk_status": "Normal",
			"segments": segments,
		}
	).insert(ignore_permissions=True)
	segments = frappe.get_all("APS Schedule Segment", filters={"parent": result.name}, pluck="name")
	return {"run": run.name, "result": result.name, "segments": segments}


def _page_snapshot(run_name: str, context: dict, schedule_name: str) -> dict:
	gantt = app.get_schedule_gantt_data(run_name)
	release = app.get_release_center_data(run_name)
	progress = planning.get_customer_schedule_progress_data(
		company=context["company"],
		customer=context["customer"],
		item_code=context["flow_item"],
		schedule_scope=frappe.db.get_value("Customer Delivery Schedule", schedule_name, "schedule_scope"),
		run_name=run_name,
	)
	return {
		"gantt_quantity_summary": gantt.get("quantity_summary") or {},
		"gantt_fulfillment_summary": gantt.get("fulfillment_summary") or {},
		"gantt_task_count": len(gantt.get("tasks") or []),
		"release_quantity_summary": release.get("quantity_summary") or {},
		"release_fulfillment_summary": release.get("fulfillment_summary") or {},
		"release_batch_count": len(release.get("release_batches") or []),
		"progress_summary": progress.get("summary") or {},
	}


def _import_scope(context: dict, scope: str, item: str, qty: float, due_date, *, version: str) -> dict:
	return planning.import_customer_delivery_schedule(
		customer=context["customer"],
		company=context["company"],
		version_no=version,
		schedule_scope=scope,
		import_strategy="Replace Scope",
		duplicate_policy="Block",
		rows_json=[_schedule_row(item, qty, due_date, source_excel_row=2)],
	)


def _schedule_row(item: str, qty: float, due_date, *, source_excel_row: int = 2) -> dict:
	return {
		"sales_order": "",
		"item_code": item,
		"customer_part_no": item,
		"schedule_date": due_date,
		"qty": qty,
		"production_strategy": "Auto Balance",
		"demand_confidence": "Confirmed",
		"prebuild_allowed": 1,
		"max_prebuild_days": 2,
		"source_excel_row": source_excel_row,
	}


def _active_schedule_row(scope: str, item: str):
	return frappe.db.sql(
		"""
		select i.name, i.parent, i.item_code, i.qty, i.produced_qty, i.delivered_qty, i.balance_qty, i.status
		from `tabCustomer Delivery Schedule Item` i
		inner join `tabCustomer Delivery Schedule` s on s.name = i.parent
		where s.schedule_scope = %s and s.status = 'Active' and i.item_code = %s
		order by s.creation desc, i.idx asc
		limit 1
		""",
		(scope, item),
		as_dict=True,
	)[0]


def _single_value(doctype: str, filters: dict, fieldname: str):
	value = frappe.db.get_value(doctype, filters, fieldname)
	if not value:
		_phase6_fail("missing_document", doctype, json.dumps(filters, default=str), fieldname, value, "")
	return value


def _assert_positive(stage: str, doctype: str, name: str | None, value) -> None:
	if flt(value) <= 0:
		_phase6_fail(stage, doctype, name or "-", "> 0", value, flt(value))


def _assert_qty(stage: str, doctype: str, name: str | None, expected, actual) -> None:
	difference = flt(actual) - flt(expected)
	if abs(difference) > QTY_TOLERANCE:
		_phase6_fail(stage, doctype, name or "-", expected, actual, difference)


def _assert_equal(stage: str, doctype: str, name: str | None, expected, actual) -> None:
	if expected != actual:
		_phase6_fail(stage, doctype, name or "-", expected, actual, "")


def _raise_audit_failure(stage: str, audit: dict) -> None:
	first = (audit.get("differences") or [{}])[0]
	_phase6_fail(
		stage,
		first.get("doctype") or "APS Planning Run",
		first.get("name") or audit.get("run") or "-",
		first.get("expected_qty"),
		first.get("actual_qty"),
		first.get("difference_qty"),
		details=audit.get("differences") or [],
	)


def _phase6_fail(stage: str, doctype: str, name: str, expected, actual, difference, details=None) -> None:
	payload = {
		"stage": stage,
		"doctype": doctype,
		"document": name,
		"expected": expected,
		"actual": actual,
		"difference": difference,
		"details": details or [],
	}
	frappe.throw("Phase 6 failure: {0}".format(json.dumps(_jsonable(payload), ensure_ascii=False, default=str)), frappe.ValidationError)


def _jsonable(value):
	return json.loads(frappe.as_json(value))


def _build_artifact_binding(
	*,
	gate_run_id: str,
	planning_run: str,
	isolation_evidence: dict,
	code_evidence: dict,
) -> dict:
	"""Bind one artifact to one gate run, planning run, signature and code set."""
	attestation = isolation_evidence.get("attestation") or {}
	actual_commits = {
		app_name: str((code_evidence.get(app_name) or {}).get("commit") or "").lower()
		for app_name in REQUIRED_CODE_APPS
	}
	binding = {
		"gate_run_id": gate_run_id,
		"planning_run": planning_run,
		"environment_id": attestation.get("environment_id"),
		"site": attestation.get("site"),
		"attestation_sha256": isolation_evidence.get("attestation_sha256"),
		"attestation_expires_at": attestation.get("expires_at"),
		"expected_commits": attestation.get("expected_commits"),
		"actual_commits": actual_commits,
	}
	canonical = json.dumps(binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
	return {**binding, "binding_sha256": hashlib.sha256(canonical).hexdigest()}


def _fsync_directory(path: Path) -> None:
	flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
	directory_fd = os.open(path, flags)
	try:
		os.fsync(directory_fd)
	finally:
		os.close(directory_fd)


def _write_json_atomic(path: Path, payload) -> None:
	"""Durably publish JSON without exposing a partial success artifact."""
	path.parent.mkdir(parents=True, exist_ok=True)
	encoded = (
		json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
	).encode("utf-8")
	temporary_path = path.with_name(".{0}.{1}.tmp".format(path.name, secrets.token_hex(8)))
	flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
	file_descriptor = os.open(temporary_path, flags, 0o600)
	try:
		view = memoryview(encoded)
		while view:
			written = os.write(file_descriptor, view)
			if written <= 0:
				raise OSError("atomic artifact write made no progress")
			view = view[written:]
		os.fsync(file_descriptor)
	finally:
		os.close(file_descriptor)
	try:
		os.replace(temporary_path, path)
		_fsync_directory(path.parent)
	finally:
		try:
			temporary_path.unlink()
		except FileNotFoundError:
			pass


def _revoke_previous_success_artifact(path: Path, *, gate_run_id: str) -> Path | None:
	"""Atomically remove an old success from the canonical consumer path."""
	if not path.exists() and not path.is_symlink():
		return None
	revoked_path = path.with_name(
		"{0}.{1}.revoked.json".format(path.stem, gate_run_id)
	)
	os.replace(path, revoked_path)
	_fsync_directory(path.parent)
	try:
		_write_json_atomic(
			revoked_path,
			{
				"status": "revoked",
				"server_gate_passed": False,
				"release_ready": False,
				"superseded_by_gate_run_id": gate_run_id,
				"revoked_on": str(now_datetime()),
			},
		)
	except Exception:
		# If even the revocation marker cannot be published, remove the renamed
		# success entirely so no stale positive payload remains reusable.
		try:
			revoked_path.unlink()
			_fsync_directory(path.parent)
		except FileNotFoundError:
			pass
		raise
	return revoked_path


def _write_failure_artifacts_best_effort(artifact_dir: Path, artifact_path: Path, payload: dict) -> None:
	"""Keep the canonical artifact visibly non-successful without hiding errors."""
	for path in (
		artifact_path,
		artifact_dir / "phase6-server-confirmation.failed.json",
	):
		try:
			_write_json_atomic(path, payload)
		except Exception:
			# The gate's original validation/rollback error remains authoritative.
			pass


def _write_json(path: Path, payload) -> None:
	"""Compatibility wrapper for callers which require atomic JSON output."""
	_write_json_atomic(path, payload)


def _read_secure_root_owned_file(path: Path, *, maximum_size: int) -> bytes:
	"""Read immutable attestation material without a symlink/replace race."""
	no_follow = getattr(os, "O_NOFOLLOW", None)
	if no_follow is None:
		raise frappe.PermissionError("Phase 6 requires O_NOFOLLOW support for attestation files.")
	flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
	try:
		file_descriptor = os.open(path, flags)
	except OSError as exc:
		raise frappe.PermissionError("Phase 6 attestation file is unavailable: {0}.".format(exc)) from exc
	try:
		file_stat = os.fstat(file_descriptor)
		if not stat.S_ISREG(file_stat.st_mode):
			raise frappe.PermissionError("Phase 6 attestation material must be a regular non-symlink file.")
		if file_stat.st_uid != 0 or file_stat.st_mode & 0o022:
			raise frappe.PermissionError(
				"Phase 6 attestation material must be root-owned and not writable by group or other users."
			)
		if file_stat.st_size <= 0 or file_stat.st_size > maximum_size:
			raise frappe.PermissionError("Phase 6 attestation material has an invalid size.")
		remaining = file_stat.st_size
		chunks = []
		while remaining:
			chunk = os.read(file_descriptor, min(remaining, 64 * 1024))
			if not chunk:
				break
			chunks.append(chunk)
			remaining -= len(chunk)
		content = b"".join(chunks)
		if len(content) != file_stat.st_size:
			raise frappe.PermissionError("Phase 6 attestation material changed while it was being read.")
		return content
	finally:
		os.close(file_descriptor)


def _canonical_attestation_payload(payload: dict) -> bytes:
	return json.dumps(
		payload,
		ensure_ascii=True,
		sort_keys=True,
		separators=(",", ":"),
	).encode("utf-8")


def _parse_signed_utc_timestamp(value, fieldname: str) -> datetime:
	text = str(value or "").strip()
	try:
		parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError as exc:
		raise frappe.ValidationError("Phase 6 attestation {0} is not valid ISO-8601.".format(fieldname)) from exc
	if parsed.tzinfo is None:
		raise frappe.ValidationError("Phase 6 attestation {0} must include a timezone.".format(fieldname))
	return parsed.astimezone(timezone.utc)


def _verify_phase6_attestation_document(document: dict, public_key_pem: bytes, *, current_time=None) -> dict:
	"""Verify an externally signed, short-lived environment attestation."""
	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

	if not isinstance(document, dict) or not isinstance(document.get("payload"), dict):
		raise frappe.ValidationError("Phase 6 attestation payload is missing or invalid.")
	try:
		signature = base64.b64decode(str(document.get("signature") or ""), validate=True)
	except (ValueError, TypeError) as exc:
		raise frappe.ValidationError("Phase 6 attestation signature is not valid base64.") from exc
	try:
		public_key = serialization.load_pem_public_key(public_key_pem)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Phase 6 attestation public key is invalid.") from exc
	if not isinstance(public_key, Ed25519PublicKey):
		raise frappe.ValidationError("Phase 6 attestation requires an Ed25519 public key.")
	try:
		public_key.verify(signature, _canonical_attestation_payload(document["payload"]))
	except InvalidSignature as exc:
		raise frappe.PermissionError("Phase 6 environment attestation signature is invalid.") from exc

	payload = dict(document["payload"])
	now_utc = current_time or datetime.now(timezone.utc)
	if now_utc.tzinfo is None:
		now_utc = now_utc.replace(tzinfo=timezone.utc)
	now_utc = now_utc.astimezone(timezone.utc)
	issued_at = _parse_signed_utc_timestamp(payload.get("issued_at"), "issued_at")
	expires_at = _parse_signed_utc_timestamp(payload.get("expires_at"), "expires_at")
	if issued_at > now_utc or expires_at <= now_utc or expires_at - issued_at > timedelta(days=7):
		raise frappe.PermissionError("Phase 6 environment attestation is not currently valid.")
	return payload


def _load_verified_phase6_attestation(
	attestation_path: Path = PHASE6_ATTESTATION_PATH,
	public_key_path: Path = PHASE6_ATTESTATION_PUBLIC_KEY_PATH,
) -> dict:
	document_bytes = _read_secure_root_owned_file(attestation_path, maximum_size=128 * 1024)
	public_key_bytes = _read_secure_root_owned_file(public_key_path, maximum_size=16 * 1024)
	try:
		document = json.loads(document_bytes.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError) as exc:
		raise frappe.ValidationError("Phase 6 environment attestation is not valid JSON.") from exc
	payload = _verify_phase6_attestation_document(document, public_key_bytes)
	key_id = str(document.get("key_id") or "").strip()
	if not key_id:
		raise frappe.ValidationError("Phase 6 environment attestation key_id is required.")
	return {
		"attestation": payload,
		"key_id": key_id,
		"attestation_sha256": hashlib.sha256(document_bytes).hexdigest(),
	}


def _get_runtime_database_identity() -> dict:
	rows = frappe.db.sql(
		"select database() as database_name, @@hostname as server_hostname, @@port as server_port, @@server_id as server_id",
		as_dict=True,
	)
	if len(rows) != 1:
		raise frappe.PermissionError("Phase 6 could not read one runtime database identity.")
	row = dict(rows[0])
	return {
		"database_name": str(row.get("database_name") or ""),
		"server_hostname": str(row.get("server_hostname") or ""),
		"server_port": str(row.get("server_port") or ""),
		"server_id": str(row.get("server_id") or ""),
	}


def _assert_test_site() -> dict:
	site = frappe.local.site or ""
	conf = getattr(frappe, "conf", None) or frappe._dict()
	verified = _load_verified_phase6_attestation()
	runtime_database = _get_runtime_database_identity()
	errors = _get_phase6_isolation_errors(
		site,
		conf,
		attestation=verified["attestation"],
		runtime_database=runtime_database,
	)
	if errors:
		frappe.throw(
			(
				"Phase 6 independent confirmation is blocked for site {0}. "
				"It requires an externally signed isolated-environment attestation. "
				"Isolation errors: {1}."
			).format(site or "-", "; ".join(errors)),
			frappe.PermissionError,
		)
	return {**verified, "runtime_database": runtime_database}


def _get_code_evidence(repo_root: str | Path) -> dict:
	"""Return the exact immutable source revision exercised by this gate."""
	root = Path(repo_root).resolve()
	try:
		commit = subprocess.run(
			["git", "-C", str(root), "rev-parse", "HEAD"],
			check=True,
			capture_output=True,
			text=True,
			timeout=10,
		).stdout.strip()
		status = subprocess.run(
			["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
			check=True,
			capture_output=True,
			text=True,
			timeout=10,
		).stdout.strip()
	except (OSError, subprocess.SubprocessError) as exc:
		raise frappe.ValidationError(
			"Phase 6 confirmation cannot prove the tested Git revision: {0}.".format(exc)
		) from exc
	if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
		raise frappe.ValidationError("Phase 6 confirmation received an invalid Git commit identifier.")
	return {
		"commit": commit.lower(),
		"worktree_clean": not bool(status),
		"dirty_path_count": len(status.splitlines()) if status else 0,
	}


def _assert_exact_code_commit(attestation: dict) -> dict:
	expected_commits = attestation.get("expected_commits") or {}
	if set(expected_commits) != set(REQUIRED_CODE_APPS):
		raise frappe.PermissionError(
			"Phase 6 attestation must pin exactly these app commits: {0}.".format(
				", ".join(REQUIRED_CODE_APPS)
			)
		)
	bench_path = Path(get_bench_path()).resolve()
	repo_roots = {
		"injection_aps": Path(__file__).resolve().parents[2],
		**{app_name: bench_path / "apps" / app_name for app_name in REQUIRED_CODE_APPS if app_name != "injection_aps"},
	}
	result = {}
	for app_name in REQUIRED_CODE_APPS:
		evidence = _get_code_evidence(repo_roots[app_name])
		expected = str(expected_commits.get(app_name) or "").strip().lower()
		if not re.fullmatch(r"[0-9a-fA-F]{40,64}", expected) or evidence["commit"] != expected:
			raise frappe.PermissionError(
				"Phase 6 confirmation app {0} is running commit {1}, but signed evidence requires {2}.".format(
					app_name, evidence["commit"], expected or "-"
				)
			)
		if not evidence["worktree_clean"]:
			raise frappe.PermissionError(
				"Phase 6 confirmation requires a clean {0} worktree.".format(app_name)
			)
		result[app_name] = evidence
	return result


def _get_phase6_environment_evidence(isolation_evidence: dict) -> dict:
	"""Return signed, non-secret isolation evidence for the immutable artifact."""
	attestation = isolation_evidence["attestation"]
	return {
		"key_id": isolation_evidence.get("key_id"),
		"attestation_sha256": isolation_evidence.get("attestation_sha256"),
		"environment_id": attestation.get("environment_id"),
		"site": attestation.get("site"),
		"bench_path": attestation.get("bench_path"),
		"test_company": attestation.get("test_company"),
		"database": attestation.get("database"),
		"production": attestation.get("production"),
		"backup": attestation.get("backup"),
		"restore_drill": attestation.get("restore_drill"),
		"services": attestation.get("services"),
		"issued_at": attestation.get("issued_at"),
		"expires_at": attestation.get("expires_at"),
		"expected_commits": attestation.get("expected_commits"),
		"runtime_database": isolation_evidence.get("runtime_database"),
	}


def _normalise_host_identity(value: str | None) -> str:
	raw = str(value or "").strip().lower()
	if not raw:
		return ""
	parsed = urlparse(raw if "://" in raw else "//{0}".format(raw))
	host = (parsed.hostname or raw).rstrip(".")
	if host == "localhost":
		return "loopback"
	try:
		if ipaddress.ip_address(host).is_loopback:
			return "loopback"
	except ValueError:
		pass
	return host


def _reference_uses_local_storage(reference: str | None) -> bool:
	parsed = urlparse(str(reference or "").strip())
	if parsed.scheme.lower() not in {"s3", "gs", "azure", "https"}:
		return True
	if not parsed.netloc or not parsed.path or parsed.path == "/":
		return True
	host = _normalise_host_identity(parsed.hostname)
	if host == "loopback":
		return True
	try:
		address = ipaddress.ip_address(host)
		if address.is_private or address.is_link_local or address.is_unspecified:
			return True
	except ValueError:
		pass
	return False


def _parse_runtime_redis_identity(value: str | None) -> dict:
	"""Return non-secret Redis endpoint identity from a Frappe config URL."""
	raw = str(value or "").strip()
	if not raw:
		return {}
	try:
		parsed = urlparse(raw)
		if parsed.scheme.lower() not in {"redis", "rediss"} or not parsed.hostname:
			return {}
		port = parsed.port or 6379
	except ValueError:
		return {}
	database = (parsed.path or "").strip("/") or "0"
	if "/" in database or not database.isdigit():
		return {}
	return {
		"server_hostname": str(parsed.hostname).rstrip(".").casefold(),
		"server_port": str(port),
		"database": database,
	}


def _get_external_service_isolation_errors(conf, attestation: dict) -> list[str]:
	"""Compare signed cache/queue/socket infrastructure to live config."""
	conf = conf or {}
	services = attestation.get("services") or {}
	production_services = (attestation.get("production") or {}).get("services") or {}
	errors = []
	production_resource_ids = {
		str(row.get("resource_id") or "").casefold()
		for row in production_services.values()
		if str(row.get("resource_id") or "").strip()
	}
	production_server_ids = {
		(
			str(row.get("server_hostname") or "").casefold(),
			str(row.get("server_port") or ""),
		)
		for row in production_services.values()
		if str(row.get("server_hostname") or "").strip() and str(row.get("server_port") or "").strip()
	}
	for service_name, (runtime_url_field, runtime_resource_field) in REQUIRED_EXTERNAL_SERVICES.items():
		runtime = _parse_runtime_redis_identity(conf.get(runtime_url_field))
		signed = services.get(service_name) or {}
		production = production_services.get(service_name) or {}
		if not runtime:
			errors.append("runtime {0} Redis endpoint is missing or invalid".format(service_name))
		resource_id = str(conf.get(runtime_resource_field) or "").strip()
		if not resource_id:
			errors.append("runtime {0} resource id is missing".format(service_name))
		for fieldname in ("server_hostname", "server_port", "database", "resource_id"):
			if not str(signed.get(fieldname) or "").strip():
				errors.append("signed test {0} {1} is missing".format(service_name, fieldname))
			if not str(production.get(fieldname) or "").strip():
				errors.append("signed production {0} {1} is missing".format(service_name, fieldname))
		for fieldname in ("server_hostname", "server_port", "database"):
			if str(runtime.get(fieldname) or "").casefold() != str(signed.get(fieldname) or "").casefold():
				errors.append(
					"runtime {0} {1} does not match signed evidence".format(service_name, fieldname)
				)
		if resource_id.casefold() != str(signed.get("resource_id") or "").casefold():
			errors.append("runtime {0} resource id does not match signed evidence".format(service_name))
		if str(signed.get("resource_id") or "").casefold() == str(
			production.get("resource_id") or ""
		).casefold():
			errors.append("test {0} resource identity matches production".format(service_name))
		if str(signed.get("resource_id") or "").casefold() in production_resource_ids:
			errors.append("test {0} resource identity matches a production service".format(service_name))
		test_server = (
			str(signed.get("server_hostname") or "").casefold(),
			str(signed.get("server_port") or ""),
		)
		production_server = (
			str(production.get("server_hostname") or "").casefold(),
			str(production.get("server_port") or ""),
		)
		if all(test_server) and test_server == production_server:
			errors.append("test {0} Redis server is not independent from production".format(service_name))
		if all(test_server) and test_server in production_server_ids:
			errors.append("test {0} Redis server matches a production service".format(service_name))
	return errors


def _get_phase6_isolation_errors(
	site: str,
	conf,
	*,
	attestation: dict | None,
	runtime_database: dict | None,
	bench_path: str | None = None,
	current_time=None,
) -> list[str]:
	"""Compare signed authority evidence with the live process and DB server."""
	conf = conf or {}
	attestation = attestation or {}
	runtime_database = runtime_database or {}
	errors = []
	if not site:
		errors.append("site name is missing")
	if not attestation:
		errors.append("signed environment attestation is missing")
		return errors
	production = attestation.get("production") or {}
	database = attestation.get("database") or {}
	backup = attestation.get("backup") or {}
	restore_drill = attestation.get("restore_drill") or {}
	errors.extend(_get_external_service_isolation_errors(conf, attestation))
	if site in FORBIDDEN_PHASE6_SITES or site == production.get("site"):
		errors.append("site name is explicitly forbidden")
	if not str(production.get("site") or "").strip():
		errors.append("signed production site identity is missing")
	if site != attestation.get("site"):
		errors.append("runtime site does not match the signed test site")
	if attestation.get("environment_kind") != "isolated_test":
		errors.append("signed environment kind is not isolated_test")
	if not str(attestation.get("environment_id") or "").strip():
		errors.append("signed environment id is missing")
	if not str(attestation.get("test_company") or "").strip():
		errors.append("signed dedicated test company is missing")

	missing_flags = [fieldname for fieldname in REQUIRED_ISOLATION_FLAGS if not cint(conf.get(fieldname))]
	if missing_flags:
		errors.append("missing true site-config flags: {0}".format(", ".join(missing_flags)))
	missing_values = [fieldname for fieldname in REQUIRED_ISOLATION_VALUES if not str(conf.get(fieldname) or "").strip()]
	if missing_values:
		errors.append("missing runtime database config: {0}".format(", ".join(missing_values)))

	current_bench = Path(bench_path or get_bench_path()).resolve()
	signed_bench = str(attestation.get("bench_path") or "").strip()
	production_bench = str(production.get("bench_path") or "").strip()
	if not signed_bench or not Path(signed_bench).is_absolute() or current_bench != Path(signed_bench).resolve():
		errors.append("runtime bench path does not match signed test bench")
	if not production_bench or not Path(production_bench).is_absolute():
		errors.append("signed production bench path is missing or not absolute")
	elif current_bench == Path(production_bench).resolve():
		errors.append("test bench path matches the production bench path")

	identity_fields = ("database_name", "server_hostname", "server_port", "server_id")
	for fieldname in identity_fields:
		if not str(runtime_database.get(fieldname) or "").strip():
			errors.append("runtime database {0} is missing".format(fieldname))
		if not str(database.get(fieldname) or "").strip():
			errors.append("signed test database {0} is missing".format(fieldname))
		if str(runtime_database.get(fieldname) or "").casefold() != str(database.get(fieldname) or "").casefold():
			errors.append("runtime database {0} does not match signed evidence".format(fieldname))
	if str(conf.get("db_name") or "").casefold() != str(runtime_database.get("database_name") or "").casefold():
		errors.append("site-config database name does not match the connected database")
	if not str(database.get("resource_id") or "").strip():
		errors.append("signed test database resource id is missing")
	if not str(production.get("database_resource_id") or "").strip():
		errors.append("signed production database resource id is missing")
	if str(database.get("resource_id") or "").casefold() == str(
		production.get("database_resource_id") or ""
	).casefold():
		errors.append("test database resource identity matches the production database")
	production_server_identity = (
		str(production.get("database_server_hostname") or "").casefold(),
		str(production.get("database_server_port") or ""),
		str(production.get("database_server_id") or ""),
	)
	test_server_identity = (
		str(database.get("server_hostname") or "").casefold(),
		str(database.get("server_port") or ""),
		str(database.get("server_id") or ""),
	)
	production_server_id = str(production.get("database_server_id") or "").strip().casefold()
	test_server_id = str(database.get("server_id") or "").strip().casefold()
	matching_nonempty_server_id = bool(
		production_server_id and test_server_id and production_server_id == test_server_id
	)
	if (
		not all(production_server_identity)
		or test_server_identity == production_server_identity
		or matching_nonempty_server_id
	):
		errors.append("test database server identity is not independent from production")

	backup_reference = str(backup.get("reference") or "").strip()
	if _reference_uses_local_storage(backup_reference):
		errors.append("signed backup reference must identify independent non-local storage")
	if not str(backup.get("object_version") or "").strip():
		errors.append("signed backup object version is missing")
	backup_sha256 = str(backup.get("sha256") or "").strip()
	if not re.fullmatch(r"[0-9a-fA-F]{64}", backup_sha256):
		errors.append("signed backup SHA-256 is invalid")
	restore_reference = str(restore_drill.get("reference") or "").strip()
	if _reference_uses_local_storage(restore_reference):
		errors.append("signed restore-drill reference must identify independent non-local evidence")
	if restore_drill.get("result") != "passed":
		errors.append("signed restore drill did not pass")

	now_utc = current_time or datetime.now(timezone.utc)
	if now_utc.tzinfo is None:
		now_utc = now_utc.replace(tzinfo=timezone.utc)
	now_utc = now_utc.astimezone(timezone.utc)
	for fieldname, value, maximum_age in (
		("backup.verified_at", backup.get("verified_at"), timedelta(days=7)),
		("restore_drill.verified_at", restore_drill.get("verified_at"), timedelta(days=90)),
	):
		try:
			verified_at = _parse_signed_utc_timestamp(value, fieldname)
		except frappe.ValidationError as exc:
			errors.append(str(exc))
			continue
		if verified_at > now_utc or now_utc - verified_at > maximum_age:
			errors.append("signed {0} evidence is stale or from the future".format(fieldname))
	return errors
