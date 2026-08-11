from __future__ import annotations

import ast
import base64
import copy
import json
import tempfile
import unittest
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from injection_aps.tests import phase6_independent_confirmation as phase6


class TestPhase6RunnerSafety(unittest.TestCase):
	NOW = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)

	class _CallbackQueue:
		def __init__(self, *callbacks):
			self._functions = deque(callbacks)

		def add(self, callback):
			self._functions.append(callback)

		def reset(self):
			self._functions.clear()

	@classmethod
	def _fake_database(cls):
		return SimpleNamespace(
			savepoint=MagicMock(),
			rollback=MagicMock(),
			commit=MagicMock(),
			**{
				queue_name: cls._CallbackQueue(lambda: queue_name)
				for queue_name in phase6.TRANSACTION_CALLBACK_QUEUES
			},
		)

	@classmethod
	def _attestation_payload(cls, *, now=None, **overrides):
		now = now or cls.NOW
		payload = {
			"issued_at": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
			"expires_at": (now + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
			"environment_kind": "isolated_test",
			"environment_id": "aps-uat-2026-08",
			"site": "aps-test.example",
			"bench_path": "/srv/aps-test-bench",
			"test_company": "APS Isolated Test Company",
			"database": {
				"database_name": "isolated-test-db",
				"server_hostname": "test-db.internal",
				"server_port": "3306",
				"server_id": "2001",
				"resource_id": "db-resource-test-001",
			},
			"services": {
				"redis_cache": {
					"server_hostname": "test-cache.internal",
					"server_port": "6379",
					"database": "0",
					"resource_id": "redis-cache-test-001",
				},
				"redis_queue": {
					"server_hostname": "test-queue.internal",
					"server_port": "6379",
					"database": "0",
					"resource_id": "redis-queue-test-001",
				},
				"redis_socketio": {
					"server_hostname": "test-socketio.internal",
					"server_port": "6379",
					"database": "0",
					"resource_id": "redis-socketio-test-001",
				},
			},
			"production": {
				"site": "jce.1",
				"bench_path": "/srv/production-bench",
				"database_resource_id": "db-resource-production-001",
				"database_server_hostname": "production-db.internal",
				"database_server_port": "3306",
				"database_server_id": "1001",
				"services": {
					"redis_cache": {
						"server_hostname": "production-cache.internal",
						"server_port": "6379",
						"database": "0",
						"resource_id": "redis-cache-production-001",
					},
					"redis_queue": {
						"server_hostname": "production-queue.internal",
						"server_port": "6379",
						"database": "0",
						"resource_id": "redis-queue-production-001",
					},
					"redis_socketio": {
						"server_hostname": "production-socketio.internal",
						"server_port": "6379",
						"database": "0",
						"resource_id": "redis-socketio-production-001",
					},
				},
			},
			"backup": {
				"reference": "s3://aps-test-backups/verified/backup.sql.gz",
				"sha256": "a" * 64,
				"object_version": "version-2026-08-11T180000Z",
				"verified_at": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
			},
			"restore_drill": {
				"reference": "https://evidence.example/restore-drills/APS-2026-08-11",
				"result": "passed",
				"verified_at": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
			},
			"expected_commits": {
				app_name: f"{index + 1:040x}"
				for index, app_name in enumerate(phase6.REQUIRED_CODE_APPS)
			},
		}
		payload.update(overrides)
		return payload

	@staticmethod
	def _runtime_database():
		return {
			"database_name": "isolated-test-db",
			"server_hostname": "test-db.internal",
			"server_port": "3306",
			"server_id": "2001",
		}

	@staticmethod
	def _isolated_conf(**overrides):
		values = {
			"db_name": "isolated-test-db",
			"db_host": "test-db.internal",
			"redis_cache": "redis://test-cache.internal:6379/0",
			"redis_queue": "redis://test-queue.internal:6379/0",
			"redis_socketio": "redis://test-socketio.internal:6379/0",
			"aps_phase6_redis_cache_resource_id": "redis-cache-test-001",
			"aps_phase6_redis_queue_resource_id": "redis-queue-test-001",
			"aps_phase6_redis_socketio_resource_id": "redis-socketio-test-001",
			**{fieldname: 1 for fieldname in phase6.REQUIRED_ISOLATION_FLAGS},
		}
		values.update(overrides)
		return values

	@classmethod
	def _isolation_evidence(cls):
		return {
			"attestation": cls._attestation_payload(),
			"key_id": "unit-ed25519-key",
			"attestation_sha256": "d" * 64,
			"runtime_database": cls._runtime_database(),
		}

	@staticmethod
	def _sign_attestation(payload):
		private_key = Ed25519PrivateKey.generate()
		public_key_pem = private_key.public_key().public_bytes(
			encoding=serialization.Encoding.PEM,
			format=serialization.PublicFormat.SubjectPublicKeyInfo,
		)
		signature = private_key.sign(phase6._canonical_attestation_payload(payload))
		document = {
			"key_id": "temporary-unit-key",
			"payload": copy.deepcopy(payload),
			"signature": base64.b64encode(signature).decode("ascii"),
		}
		return document, public_key_pem

	@staticmethod
	@contextmanager
	def _bound_frappe_local():
		initialized_here = False
		temporary_sites = None
		try:
			phase6.frappe.local.site
		except (AttributeError, RuntimeError):
			temporary_sites = tempfile.TemporaryDirectory()
			test_site = Path(temporary_sites.name, "unit-no-site")
			test_site.mkdir()
			(test_site / "site_config.json").write_text("{}\n", encoding="utf-8")
			Path(temporary_sites.name, "apps.txt").write_text(
				"frappe\nerpnext\ninjection_aps\n",
				encoding="utf-8",
			)
			phase6.frappe.init("unit-no-site", sites_path=temporary_sites.name)
			phase6.frappe.local.session = phase6.frappe._dict(user="Administrator")
			phase6.frappe.local.db = MagicMock()
			phase6.frappe.local.db.get_global.return_value = "[]"
			cache = MagicMock()
			cache.hget.return_value = {}
			phase6.frappe.cache = cache
			phase6.frappe.local.system_settings = phase6.frappe._dict(time_zone="UTC")
			initialized_here = True
		try:
			yield
		finally:
			if initialized_here:
				phase6.frappe.destroy()
			if temporary_sites:
				temporary_sites.cleanup()

	def test_real_ed25519_attestation_is_accepted(self):
		payload = self._attestation_payload()
		document, public_key_pem = self._sign_attestation(payload)

		verified = phase6._verify_phase6_attestation_document(
			document,
			public_key_pem,
			current_time=self.NOW,
		)

		self.assertEqual(verified, payload)

	def test_tampered_ed25519_attestation_is_rejected(self):
		payload = self._attestation_payload()
		document, public_key_pem = self._sign_attestation(payload)
		document["payload"]["database"]["database_name"] = "production-db"

		with self.assertRaisesRegex(phase6.frappe.PermissionError, "signature is invalid"):
			phase6._verify_phase6_attestation_document(
				document,
				public_key_pem,
				current_time=self.NOW,
			)

	def test_expired_but_correctly_signed_attestation_is_rejected(self):
		payload = self._attestation_payload(
			issued_at=(self.NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
			expires_at=(self.NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
		)
		document, public_key_pem = self._sign_attestation(payload)

		with self.assertRaisesRegex(phase6.frappe.PermissionError, "not currently valid"):
			phase6._verify_phase6_attestation_document(
				document,
				public_key_pem,
				current_time=self.NOW,
			)

	def test_external_attestation_loader_uses_verified_document_and_key(self):
		payload = self._attestation_payload(now=datetime.now(timezone.utc))
		document, public_key_pem = self._sign_attestation(payload)
		document_bytes = json.dumps(document, sort_keys=True).encode("utf-8")
		with tempfile.TemporaryDirectory() as temp_dir:
			attestation_path = Path(temp_dir, "attestation.json")
			public_key_path = Path(temp_dir, "public.pem")
			attestation_path.write_bytes(document_bytes)
			public_key_path.write_bytes(public_key_pem)

			with patch.object(
				phase6,
				"_read_secure_root_owned_file",
				side_effect=lambda path, **_kwargs: Path(path).read_bytes(),
			) as secure_read:
				verified = phase6._load_verified_phase6_attestation(attestation_path, public_key_path)

		self.assertEqual(verified["attestation"], payload)
		self.assertEqual(verified["key_id"], "temporary-unit-key")
		self.assertEqual(len(verified["attestation_sha256"]), 64)
		self.assertEqual(
			secure_read.call_args_list,
			[
				call(attestation_path, maximum_size=128 * 1024),
				call(public_key_path, maximum_size=16 * 1024),
			],
		)

	def test_artifact_writer_is_atomic_and_private(self):
		with self._bound_frappe_local():
			with tempfile.TemporaryDirectory() as output_dir:
				path = Path(output_dir, "artifact.json")
				phase6._write_json_atomic(path, {"status": "server_checks_passed", "run": "RUN-1"})
				payload = json.loads(path.read_text(encoding="utf-8"))
				mode = path.stat().st_mode & 0o777
				temporary_files = [path for path in Path(output_dir).iterdir() if path.name.endswith(".tmp")]

		self.assertEqual(payload["run"], "RUN-1")
		self.assertEqual(mode, 0o600)
		self.assertEqual(temporary_files, [])

	def test_production_site_is_rejected_even_with_valid_signed_evidence(self):
		payload = self._attestation_payload(site="jce.1")
		errors = phase6._get_phase6_isolation_errors(
			"jce.1",
			self._isolated_conf(),
			attestation=payload,
			runtime_database=self._runtime_database(),
			bench_path="/srv/aps-test-bench",
			current_time=self.NOW,
		)
		self.assertIn("site name is explicitly forbidden", errors)

	def test_isolation_requires_all_runtime_flags_and_database_identity(self):
		errors = phase6._get_phase6_isolation_errors(
			"aps-test.example",
			{"allow_tests": 1},
			attestation=self._attestation_payload(),
			runtime_database={},
			bench_path="/srv/aps-test-bench",
			current_time=self.NOW,
		)
		self.assertTrue(any("aps_phase6_isolated_site" in row for row in errors))
		self.assertTrue(any("db_name" in row for row in errors))
		self.assertTrue(any("runtime database database_name is missing" in row for row in errors))

	def test_complete_signed_isolation_contract_is_accepted(self):
		self.assertEqual(
			phase6._get_phase6_isolation_errors(
				"aps-test.example",
				self._isolated_conf(),
				attestation=self._attestation_payload(),
				runtime_database=self._runtime_database(),
				bench_path="/srv/aps-test-bench",
				current_time=self.NOW,
			),
			[],
		)

	def test_database_server_id_alias_cannot_bypass_isolation(self):
		payload = self._attestation_payload()
		payload["database"]["server_hostname"] = "test-alias-for-production-db.internal"
		payload["database"]["server_port"] = payload["production"]["database_server_port"]
		payload["database"]["server_id"] = payload["production"]["database_server_id"]
		runtime_database = self._runtime_database()
		runtime_database.update(
			{
				"server_hostname": payload["database"]["server_hostname"],
				"server_port": payload["database"]["server_port"],
				"server_id": payload["database"]["server_id"],
			}
		)

		errors = phase6._get_phase6_isolation_errors(
			"aps-test.example",
			self._isolated_conf(db_host=payload["database"]["server_hostname"]),
			attestation=payload,
			runtime_database=runtime_database,
			bench_path="/srv/aps-test-bench",
			current_time=self.NOW,
		)

		self.assertNotEqual(
			payload["database"]["server_hostname"],
			payload["production"]["database_server_hostname"],
		)
		self.assertIn("test database server identity is not independent from production", errors)

	def test_cache_queue_and_socketio_runtime_identity_fail_closed(self):
		payload = self._attestation_payload()
		missing_runtime = self._isolated_conf(redis_queue="", aps_phase6_redis_socketio_resource_id="")
		errors = phase6._get_phase6_isolation_errors(
			"aps-test.example",
			missing_runtime,
			attestation=payload,
			runtime_database=self._runtime_database(),
			bench_path="/srv/aps-test-bench",
			current_time=self.NOW,
		)
		self.assertIn("runtime redis_queue Redis endpoint is missing or invalid", errors)
		self.assertIn("runtime redis_socketio resource id is missing", errors)

		shared = copy.deepcopy(payload)
		shared["services"]["redis_cache"] = copy.deepcopy(
			shared["production"]["services"]["redis_cache"]
		)
		conf = self._isolated_conf(
			redis_cache="redis://production-cache.internal:6379/0",
			aps_phase6_redis_cache_resource_id="redis-cache-production-001",
		)
		errors = phase6._get_phase6_isolation_errors(
			"aps-test.example",
			conf,
			attestation=shared,
			runtime_database=self._runtime_database(),
			bench_path="/srv/aps-test-bench",
			current_time=self.NOW,
		)
		self.assertIn("test redis_cache resource identity matches production", errors)
		self.assertIn("test redis_cache Redis server is not independent from production", errors)

		cross_role = copy.deepcopy(payload)
		cross_role["services"]["redis_cache"] = copy.deepcopy(
			cross_role["production"]["services"]["redis_queue"]
		)
		conf = self._isolated_conf(
			redis_cache="redis://production-queue.internal:6379/0",
			aps_phase6_redis_cache_resource_id="redis-queue-production-001",
		)
		errors = phase6._get_phase6_isolation_errors(
			"aps-test.example",
			conf,
			attestation=cross_role,
			runtime_database=self._runtime_database(),
			bench_path="/srv/aps-test-bench",
			current_time=self.NOW,
		)
		self.assertIn("test redis_cache resource identity matches a production service", errors)
		self.assertIn("test redis_cache Redis server matches a production service", errors)

	def test_localhost_fake_urls_and_shared_production_identity_are_rejected(self):
		payload = self._attestation_payload()
		payload["bench_path"] = "/srv/production-bench"
		payload["database"]["resource_id"] = payload["production"]["database_resource_id"]
		payload["database"]["server_hostname"] = payload["production"]["database_server_hostname"]
		payload["database"]["server_port"] = payload["production"]["database_server_port"]
		payload["database"]["server_id"] = payload["production"]["database_server_id"]
		payload["backup"]["reference"] = "https://localhost/not-a-backup"
		payload["restore_drill"]["reference"] = "https://127.0.0.1/not-a-drill"
		runtime_database = {
			"database_name": "isolated-test-db",
			"server_hostname": "production-db.internal",
			"server_port": "3306",
			"server_id": "1001",
		}

		errors = phase6._get_phase6_isolation_errors(
			"aps-test.example",
			self._isolated_conf(),
			attestation=payload,
			runtime_database=runtime_database,
			bench_path="/srv/production-bench",
			current_time=self.NOW,
		)

		self.assertIn("test bench path matches the production bench path", errors)
		self.assertIn("test database resource identity matches the production database", errors)
		self.assertIn("test database server identity is not independent from production", errors)
		self.assertIn("signed backup reference must identify independent non-local storage", errors)
		self.assertIn("signed restore-drill reference must identify independent non-local evidence", errors)

	def test_malformed_or_local_evidence_references_cannot_look_remote(self):
		for reference in (
			"file:///srv/backup.sql.gz",
			"http://evidence.example/backup.sql.gz",
			"https://localhost/backup.sql.gz",
			"https://127.0.0.1/backup.sql.gz",
			"https://10.0.0.8/backup.sql.gz",
			"https://evidence.example",
		):
			with self.subTest(reference=reference):
				self.assertTrue(phase6._reference_uses_local_storage(reference))

	def test_exact_five_app_commits_and_clean_worktrees_are_required(self):
		attestation = self._attestation_payload()
		expected = attestation["expected_commits"]
		clean_rows = iter(
			{
				"commit": expected[app_name],
				"worktree_clean": True,
				"dirty_path_count": 0,
			}
			for app_name in phase6.REQUIRED_CODE_APPS
		)
		with (
			patch.object(phase6, "get_bench_path", return_value="/srv/aps-test-bench"),
			patch.object(phase6, "_get_code_evidence", side_effect=lambda _root: next(clean_rows)) as evidence_getter,
		):
			evidence = phase6._assert_exact_code_commit(attestation)

		self.assertEqual(tuple(evidence), phase6.REQUIRED_CODE_APPS)
		self.assertEqual(evidence_getter.call_count, 5)

		missing_app = copy.deepcopy(attestation)
		missing_app["expected_commits"].pop("light_mes")
		with self.assertRaisesRegex(phase6.frappe.PermissionError, "pin exactly"):
			phase6._assert_exact_code_commit(missing_app)

		mismatch_rows = iter(
			{
				"commit": ("f" * 40 if app_name == "erpnext" else expected[app_name]),
				"worktree_clean": True,
				"dirty_path_count": 0,
			}
			for app_name in phase6.REQUIRED_CODE_APPS
		)
		with (
			patch.object(phase6, "get_bench_path", return_value="/srv/aps-test-bench"),
			patch.object(phase6, "_get_code_evidence", side_effect=lambda _root: next(mismatch_rows)),
			self.assertRaisesRegex(phase6.frappe.PermissionError, "erpnext is running commit"),
		):
			phase6._assert_exact_code_commit(attestation)

		dirty_rows = iter(
			{
				"commit": expected[app_name],
				"worktree_clean": app_name != "zelin_pp",
				"dirty_path_count": 1 if app_name == "zelin_pp" else 0,
			}
			for app_name in phase6.REQUIRED_CODE_APPS
		)
		with (
			patch.object(phase6, "get_bench_path", return_value="/srv/aps-test-bench"),
			patch.object(phase6, "_get_code_evidence", side_effect=lambda _root: next(dirty_rows)),
			self.assertRaisesRegex(phase6.frappe.PermissionError, "clean zelin_pp worktree"),
		):
			phase6._assert_exact_code_commit(attestation)

	def test_gate_is_rollback_only_and_blocks_internal_commit(self):
		source = Path(phase6.__file__).read_text(encoding="utf-8")
		tree = ast.parse(source)
		gate = next(
			node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_phase6_gate"
		)
		calls = [node for node in ast.walk(gate) if isinstance(node, ast.Call)]
		attributes = {node.func.attr for node in calls if isinstance(node.func, ast.Attribute)}
		self.assertIn("savepoint", attributes)
		self.assertIn("rollback", attributes)
		self.assertIn("object", attributes)
		gate_source = ast.get_source_segment(source, gate) or ""
		self.assertNotIn('"passed": True', gate_source)
		self.assertIn('"release_ready": False', gate_source)

	def test_transaction_callbacks_added_by_gate_are_discarded(self):
		database = self._fake_database()
		snapshot = phase6._snapshot_transaction_callbacks(database)
		original = {
			queue_name: tuple(getattr(database, queue_name)._functions)
			for queue_name in phase6.TRANSACTION_CALLBACK_QUEUES
		}
		for queue_name in phase6.TRANSACTION_CALLBACK_QUEUES:
			getattr(database, queue_name).add(lambda: "gate callback")

		phase6._restore_transaction_callbacks(snapshot, database)

		for queue_name in phase6.TRANSACTION_CALLBACK_QUEUES:
			self.assertEqual(tuple(getattr(database, queue_name)._functions), original[queue_name])

	def test_local_realtime_webhook_and_deferred_job_queues_are_fully_restored(self):
		local = SimpleNamespace(
			_realtime_log=[["existing", {}, "room"]],
			_webhook_queue=["existing-webhook"],
		)
		original_realtime_queue = local._realtime_log
		original_webhook_queue = local._webhook_queue
		snapshot = phase6._snapshot_local_side_effect_queues(local)
		phase6._detach_local_side_effect_queues(local)
		self.assertFalse(hasattr(local, "_realtime_log"))
		self.assertFalse(hasattr(local, "_webhook_queue"))
		local._realtime_log = [["gate", {}, "room"]]
		local._webhook_queue = ["gate-webhook"]
		local._phase6_deferred_enqueue_log = [{"job_id": "gate-job"}]

		phase6._restore_local_side_effect_queues(snapshot, local)

		self.assertEqual(local._realtime_log, [["existing", {}, "room"]])
		self.assertEqual(local._webhook_queue, ["existing-webhook"])
		self.assertIs(local._realtime_log, original_realtime_queue)
		self.assertIs(local._webhook_queue, original_webhook_queue)
		self.assertFalse(hasattr(local, "_phase6_deferred_enqueue_log"))

	def test_external_side_effect_guard_defers_only_after_commit_and_blocks_socketio(self):
		with self._bound_frappe_local():
			self.assertFalse(hasattr(phase6.frappe.local, "_phase6_deferred_enqueue_log"))
			with phase6._phase6_external_side_effect_guard() as calls:
				phase6.frappe.enqueue(
					"unit.job",
					enqueue_after_commit=True,
					queue="short",
					job_id="unit-job",
				)
				with self.assertRaisesRegex(phase6.frappe.ValidationError, "immediate Redis queue"):
					phase6.frappe.enqueue("unit.immediate")
				with self.assertRaisesRegex(phase6.frappe.ValidationError, "Socket.IO"):
					phase6.frappe.realtime.emit_via_redis("event", {}, "room")
				with self.assertRaisesRegex(phase6.frappe.ValidationError, "outbound HTTP"):
					requests.get("https://should-not-leave.invalid/phase6", timeout=1)
			self.assertFalse(hasattr(phase6.frappe.local, "_phase6_deferred_enqueue_log"))

			caller_queue = [{"job_id": "caller-job"}]
			phase6.frappe.local._phase6_deferred_enqueue_log = caller_queue
			with phase6._phase6_external_side_effect_guard():
				phase6.frappe.enqueue("unit.second", enqueue_after_commit=True, job_id="guard-job")
			self.assertIs(phase6.frappe.local._phase6_deferred_enqueue_log, caller_queue)
			self.assertEqual(caller_queue, [{"job_id": "caller-job"}])
			del phase6.frappe.local._phase6_deferred_enqueue_log

		self.assertEqual(calls[0]["kind"], "background_job")
		self.assertEqual(calls[0]["job_id"], "unit-job")

	def test_gate_rolls_back_and_restores_callbacks_before_success_artifact(self):
		with self._bound_frappe_local():
			# Reproduce the historical order dependency: a standalone guard call
			# immediately before the full gate must not become caller-owned state.
			with phase6._phase6_external_side_effect_guard():
				phase6.frappe.enqueue(
					"unit.before-gate",
					enqueue_after_commit=True,
					job_id="before-gate-job",
				)
			self.assertFalse(hasattr(phase6.frappe.local, "_phase6_deferred_enqueue_log"))
			database = self._fake_database()
			original_user = phase6.frappe.session.user
			original_callbacks = tuple(database.after_commit._functions)
			pretest_local_queues = phase6._snapshot_local_side_effect_queues()
			phase6.frappe.local._realtime_log = [["caller-event", {}, "caller-room"]]
			phase6.frappe.local._webhook_queue = ["caller-webhook"]
			isolation_evidence = self._isolation_evidence()
			code_evidence = {
				app_name: {
					"commit": isolation_evidence["attestation"]["expected_commits"][app_name],
					"worktree_clean": True,
					"dirty_path_count": 0,
				}
				for app_name in phase6.REQUIRED_CODE_APPS
			}

			def set_user(user):
				phase6.frappe.local.session.user = user

			def run_chain(_context):
				database.after_commit.add(lambda: "must be discarded")
				phase6.frappe.local._realtime_log = [["gate-event", {}, "gate-room"]]
				phase6.frappe.local._webhook_queue = ["gate-webhook"]
				return {
					"run": "RUN-1",
					"release": {},
					"quantity_audit": {"difference_count": 0},
				}

			old_database = phase6.frappe.local.db
			try:
				phase6.frappe.local.db = database
				with tempfile.TemporaryDirectory() as output_dir:
					canonical_artifact = Path(output_dir, phase6.SUCCESS_ARTIFACT_NAME)
					canonical_artifact.write_text('{"status":"server_checks_passed","stale":true}\n', encoding="utf-8")
					with (
						patch.object(phase6, "_assert_test_site", return_value=isolation_evidence),
						patch.object(phase6, "_assert_exact_code_commit", return_value=code_evidence) as commit_gate,
						patch.object(phase6.frappe, "set_user", side_effect=set_user),
						patch.object(phase6.frappe, "clear_cache"),
						patch.object(phase6, "_ensure_master_data", return_value={}) as master_data,
						patch.object(phase6, "_run_full_business_chain", side_effect=run_chain),
						patch.object(phase6, "_run_pmc_scenarios", return_value=[]),
						patch.object(phase6, "_run_ui_checks", return_value={}),
					):
						result = phase6.run_phase6_gate(output_dir)
						artifact = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
						revoked_artifacts = list(Path(output_dir).glob("*.revoked.json"))
						revoked_artifact_count = len(revoked_artifacts)
						revoked_artifact = json.loads(revoked_artifacts[0].read_text(encoding="utf-8"))
						local_queue_state = (
							list(phase6.frappe.local._realtime_log),
							list(phase6.frappe.local._webhook_queue),
							hasattr(phase6.frappe.local, "_phase6_deferred_enqueue_log"),
						)
			finally:
				phase6.frappe.local.db = old_database
				phase6.frappe.local.session.user = original_user
				phase6._restore_local_side_effect_queues(pretest_local_queues)

		commit_gate.assert_called_once_with(isolation_evidence["attestation"])
		master_data.assert_called_once_with(isolation_evidence["attestation"])
		database.savepoint.assert_called_once_with("aps_phase6_independent_confirmation")
		database.rollback.assert_called_once_with(save_point="aps_phase6_independent_confirmation")
		self.assertEqual(tuple(database.after_commit._functions), original_callbacks)
		self.assertEqual(local_queue_state[0], [["caller-event", {}, "caller-room"]])
		self.assertEqual(local_queue_state[1], ["caller-webhook"])
		self.assertFalse(local_queue_state[2])
		self.assertTrue(result["server_gate_passed"])
		self.assertFalse(result["release_ready"])
		self.assertTrue(result["database_changes_rolled_back"])
		self.assertEqual(artifact["status"], "server_checks_passed")
		self.assertTrue(artifact["database_changes_rolled_back"])
		self.assertEqual(
			artifact["code_evidence"]["injection_aps"]["commit"],
			isolation_evidence["attestation"]["expected_commits"]["injection_aps"],
		)
		self.assertEqual(artifact["environment_evidence"]["key_id"], "unit-ed25519-key")
		self.assertEqual(artifact["gate_run_id"], result["gate_run_id"])
		self.assertEqual(artifact["artifact_binding"]["gate_run_id"], result["gate_run_id"])
		self.assertEqual(artifact["artifact_binding"]["planning_run"], "RUN-1")
		self.assertEqual(
			artifact["artifact_binding"]["attestation_sha256"],
			isolation_evidence["attestation_sha256"],
		)
		self.assertEqual(len(artifact["artifact_binding"]["binding_sha256"]), 64)
		self.assertFalse(artifact.get("stale"))
		self.assertEqual(revoked_artifact_count, 1)
		self.assertEqual(revoked_artifact["status"], "revoked")
		self.assertFalse(revoked_artifact["server_gate_passed"])

	def test_failed_rerun_revokes_old_success_and_cannot_reuse_it(self):
		with self._bound_frappe_local():
			with tempfile.TemporaryDirectory() as output_dir:
				artifact_path = Path(output_dir, phase6.SUCCESS_ARTIFACT_NAME)
				artifact_path.write_text(
					'{"status":"server_checks_passed","server_gate_passed":true}\n',
					encoding="utf-8",
				)
				with (
					patch.object(phase6, "_assert_test_site", return_value=self._isolation_evidence()),
					patch.object(
						phase6,
						"_assert_exact_code_commit",
						side_effect=phase6.frappe.PermissionError("dirty commit"),
					),
					patch.object(phase6.frappe, "clear_cache"),
					self.assertRaisesRegex(phase6.frappe.PermissionError, "dirty commit"),
				):
					phase6.run_phase6_gate(output_dir)

				failed = json.loads(artifact_path.read_text(encoding="utf-8"))
				self.assertEqual(failed["status"], "failed")
				self.assertFalse(failed["server_gate_passed"])
				self.assertEqual(len(list(Path(output_dir).glob("*.revoked.json"))), 1)
				self.assertFalse(any(path.name.endswith(".tmp") for path in Path(output_dir).iterdir()))

	def test_gate_rolls_back_and_restores_user_when_user_switch_fails(self):
		with self._bound_frappe_local():
			database = self._fake_database()
			original_user = phase6.frappe.session.user
			phase6.frappe.local.session.user = "pmc@example.test"
			gate_user = phase6.frappe.session.user
			set_user_calls = []

			def set_user(user):
				set_user_calls.append(user)
				if user == "Administrator":
					raise RuntimeError("user switch failed")
				phase6.frappe.local.session.user = user

			old_database = phase6.frappe.local.db
			try:
				phase6.frappe.local.db = database
				with tempfile.TemporaryDirectory() as output_dir:
					with (
						patch.object(phase6, "_assert_test_site", return_value=self._isolation_evidence()),
						patch.object(phase6, "_assert_exact_code_commit", return_value={}),
						patch.object(phase6.frappe, "set_user", side_effect=set_user),
						patch.object(phase6.frappe, "clear_cache"),
						self.assertRaisesRegex(RuntimeError, "user switch failed"),
					):
						phase6.run_phase6_gate(output_dir)
			finally:
				phase6.frappe.local.db = old_database
				phase6.frappe.local.session.user = original_user

		database.rollback.assert_called_once_with(save_point="aps_phase6_independent_confirmation")
		self.assertEqual(set_user_calls, ["Administrator", gate_user])

	def test_rollback_failure_downgrades_canonical_artifact_to_failed(self):
		with self._bound_frappe_local():
			database = self._fake_database()
			database.rollback.side_effect = RuntimeError("rollback unavailable")
			original_user = phase6.frappe.session.user
			isolation_evidence = self._isolation_evidence()
			code_evidence = {
				app_name: {
					"commit": isolation_evidence["attestation"]["expected_commits"][app_name],
					"worktree_clean": True,
					"dirty_path_count": 0,
				}
				for app_name in phase6.REQUIRED_CODE_APPS
			}

			def set_user(user):
				phase6.frappe.local.session.user = user

			old_database = phase6.frappe.local.db
			try:
				phase6.frappe.local.db = database
				with tempfile.TemporaryDirectory() as output_dir:
					with (
						patch.object(phase6, "_assert_test_site", return_value=isolation_evidence),
						patch.object(phase6, "_assert_exact_code_commit", return_value=code_evidence),
						patch.object(phase6.frappe, "set_user", side_effect=set_user),
						patch.object(phase6.frappe, "clear_cache"),
						patch.object(phase6, "_ensure_master_data", return_value={}),
						patch.object(
							phase6,
							"_run_full_business_chain",
							return_value={
								"run": "RUN-ROLLBACK",
								"release": {},
								"quantity_audit": {"difference_count": 0},
							},
						),
						patch.object(phase6, "_run_pmc_scenarios", return_value=[]),
						patch.object(phase6, "_run_ui_checks", return_value={}),
						self.assertRaisesRegex(phase6.frappe.ValidationError, "failed to restore"),
					):
						phase6.run_phase6_gate(output_dir)
					artifact = json.loads(
						Path(output_dir, phase6.SUCCESS_ARTIFACT_NAME).read_text(encoding="utf-8")
					)
			finally:
				phase6.frappe.local.db = old_database
				phase6.frappe.local.session.user = original_user

		self.assertEqual(artifact["status"], "failed")
		self.assertFalse(artifact["server_gate_passed"])
		self.assertFalse(artifact["database_changes_rolled_back"])
		self.assertIn("database rollback", artifact["restoration_errors"][0]["stage"])

	def test_cleanup_is_disabled_instead_of_raw_deleting_erp_rows(self):
		source = Path(phase6.__file__).read_text(encoding="utf-8")
		tree = ast.parse(source)
		cleanup = next(
			node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "cleanup"
		)
		cleanup_source = ast.get_source_segment(source, cleanup) or ""
		self.assertNotIn("db.delete", cleanup_source)
		with (
			patch.object(phase6, "_assert_test_site"),
			patch.object(phase6.frappe, "throw", side_effect=phase6.frappe.PermissionError),
		):
			with self.assertRaises(phase6.frappe.PermissionError):
				phase6.cleanup(confirmation_token=phase6.MARKER)

	def test_cross_midnight_slicer_has_exact_windows_dates_and_continuity(self):
		work_date = date(2026, 8, 11)
		start = datetime(2026, 8, 11, 19, 0)
		end = datetime(2026, 8, 12, 9, 0)
		segment = {
			"name": "SEG-CROSS-MIDNIGHT",
			"primary_item_code": "FG-001",
			"start_time": start,
			"end_time": end,
			"planned_qty": 140,
		}

		with (
			patch.object(phase6.planning, "_get_shift_scheduling_qty_precision", return_value=6),
			patch.object(phase6.planning, "_item_quantity_requires_integer", return_value=False),
		):
			slices = phase6.planning._split_segment_into_shift_slices(segment)

		self.assertEqual(
			[(row["start_time"], row["end_time"]) for row in slices],
			[
				(datetime(2026, 8, 11, 19, 0), datetime(2026, 8, 11, 20, 0)),
				(datetime(2026, 8, 11, 20, 0), datetime(2026, 8, 12, 8, 0)),
				(datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 9, 0)),
			],
		)
		self.assertEqual([row["posting_date"] for row in slices], [work_date, work_date, date(2026, 8, 12)])
		self.assertEqual([row["shift_type"] for row in slices], ["白班", "晚班", "白班"])
		self.assertEqual([row["planned_qty"] for row in slices], [10.0, 120.0, 10.0])
		self.assertEqual([row["slice_index"] for row in slices], [1, 2, 3])
		self.assertEqual([row["slice_count"] for row in slices], [3, 3, 3])
		self.assertEqual(slices[0]["start_time"], start)
		self.assertEqual(slices[-1]["end_time"], end)
		self.assertTrue(all(left["end_time"] == right["start_time"] for left, right in zip(slices, slices[1:])))
		self.assertAlmostEqual(sum(row["planned_qty"] for row in slices), 140.0)

	@staticmethod
	def _persisted_cross_midnight_rows():
		windows = [
			("P1", "SI-1", "WOS-1", datetime(2026, 8, 11, 19, 0), datetime(2026, 8, 11, 20, 0), 10, date(2026, 8, 11), "白班"),
			("P2", "SI-2", "WOS-2", datetime(2026, 8, 11, 20, 0), datetime(2026, 8, 12, 8, 0), 120, date(2026, 8, 11), "晚班"),
			("P3", "SI-3", "WOS-3", datetime(2026, 8, 12, 8, 0), datetime(2026, 8, 12, 9, 0), 10, date(2026, 8, 12), "白班"),
		]
		proposals = []
		scheduling = []
		headers = []
		for proposal_name, item_name, wos_name, start, end, qty, posting_date, shift_type in windows:
			proposals.append(
				phase6.frappe._dict(
					name=proposal_name,
					result_reference="RESULT-1",
					segment_reference="SEGMENT-1",
					action="New",
					work_order="WO-1",
					plant_floor="FLOOR-1",
					posting_date=posting_date,
					shift_type=shift_type,
					workstation="MACHINE-1",
					planned_start_time=start,
					planned_end_time=end,
					planned_qty=qty,
					target_scheduling=wos_name,
					review_status="Applied",
				)
			)
			scheduling.append(
				phase6.frappe._dict(
					name=item_name,
					parent=wos_name,
					work_order="WO-1",
					workstation="MACHINE-1",
					scheduling_qty=qty,
					planned_start_date=start,
					planned_end_date=end,
					custom_aps_run="RUN-1",
					custom_aps_result_reference="RESULT-1",
					custom_aps_segment_reference="SEGMENT-1",
					custom_aps_shift_proposal="SHIFT-BATCH-1",
				)
			)
			headers.append(
				phase6.frappe._dict(
					name=wos_name,
					posting_date=posting_date,
					shift_type=shift_type,
					custom_aps_run="RUN-1",
				)
			)
		segments = [
			phase6.frappe._dict(
				name="SEGMENT-1",
				parent="RESULT-1",
				start_time=datetime(2026, 8, 11, 19, 0),
				end_time=datetime(2026, 8, 12, 9, 0),
				planned_qty=140,
				linked_work_order="WO-1",
				linked_work_order_scheduling="WOS-3",
				linked_scheduling_item="SI-3",
			)
		]
		return proposals, segments, scheduling, headers

	def test_persisted_cross_midnight_segment_proposal_wos_chain_is_exact(self):
		proposals, segments, scheduling, headers = self._persisted_cross_midnight_rows()
		rows = {
			"APS Shift Schedule Proposal Item": proposals,
			"APS Schedule Segment": segments,
			"Scheduling Item": scheduling,
			"Work Order Scheduling": headers,
		}
		with patch.object(phase6.frappe, "get_all", side_effect=lambda doctype, **_kwargs: rows[doctype]):
			evidence = phase6._assert_persisted_segment_proposal_wos_chain(
				"RUN-1",
				"SHIFT-BATCH-1",
				require_cross_midnight=True,
			)

		self.assertEqual(evidence["proposal_row_count"], 3)
		self.assertEqual(evidence["scheduling_row_count"], 3)
		self.assertEqual(evidence["cross_midnight_segments"], ["SEGMENT-1"])

	def test_persisted_cross_midnight_chain_rejects_silent_result_reassignment(self):
		proposals, segments, scheduling, headers = self._persisted_cross_midnight_rows()
		scheduling[1].custom_aps_result_reference = "RESULT-OTHER"
		rows = {
			"APS Shift Schedule Proposal Item": proposals,
			"APS Schedule Segment": segments,
			"Scheduling Item": scheduling,
			"Work Order Scheduling": headers,
		}
		with (
			patch.object(phase6.frappe, "get_all", side_effect=lambda doctype, **_kwargs: rows[doctype]),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("broken persistent lineage"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "broken persistent lineage"),
		):
			phase6._assert_persisted_segment_proposal_wos_chain(
				"RUN-1",
				"SHIFT-BATCH-1",
				require_cross_midnight=True,
			)
		self.assertEqual(fail.call_args.args[0], "persisted_shift_proposal_wos_mapping")

	def test_persisted_cross_midnight_chain_rejects_broken_segment_backlink(self):
		proposals, segments, scheduling, headers = self._persisted_cross_midnight_rows()
		segments[0].linked_scheduling_item = "SI-MISSING"
		rows = {
			"APS Shift Schedule Proposal Item": proposals,
			"APS Schedule Segment": segments,
			"Scheduling Item": scheduling,
			"Work Order Scheduling": headers,
		}
		with (
			patch.object(phase6.frappe, "get_all", side_effect=lambda doctype, **_kwargs: rows[doctype]),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("broken segment backlink"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "broken segment backlink"),
		):
			phase6._assert_persisted_segment_proposal_wos_chain(
				"RUN-1",
				"SHIFT-BATCH-1",
				require_cross_midnight=True,
			)
		self.assertEqual(fail.call_args.args[0], "persisted_segment_wos_backlink")

	@staticmethod
	def _jit_result(*, due_date, strategy="Force JIT", name="RESULT-1", qty=100):
		return phase6.frappe._dict(
			name=name,
			requested_date=due_date,
			production_strategy=strategy,
			planned_qty=qty,
			prebuild_qty=0,
			jit_qty=qty,
			late_qty_after_balance=0,
			unscheduled_qty=0,
		)

	@staticmethod
	def _jit_segment(*, start, end, mode="JIT", name="SEGMENT-1", parent="RESULT-1", qty=100):
		return phase6.frappe._dict(
			name=name,
			parent=parent,
			workstation="MACHINE-1",
			start_time=start,
			end_time=end,
			planned_qty=qty,
			production_mode=mode,
			segment_kind="Primary",
			segment_status="Planned",
		)

	def test_jit_plan_helper_executes_natural_due_day_and_force_jit_assertions(self):
		due_date = date(2026, 8, 12)
		result = self._jit_result(due_date=due_date)
		segment = self._jit_segment(
			start=datetime(2026, 8, 12, 8, 0),
			end=datetime(2026, 8, 12, 18, 0),
		)

		with patch.object(
			phase6.frappe,
			"get_all",
			side_effect=lambda doctype, **_kwargs: [result] if doctype == "APS Schedule Result" else [segment],
		):
			verified = phase6._assert_planned_jit_capacity("RUN-1", due_date=due_date, expected_qty=100)

		self.assertEqual(verified["planned_qty"], 100)
		self.assertEqual(verified["jit_qty"], 100)
		self.assertEqual(verified["segment_count"], 1)
		self.assertEqual(verified["results"]["RESULT-1"]["segment_names"], ["SEGMENT-1"])

	def test_jit_plan_helper_reconciles_segments_per_result_not_only_in_total(self):
		due_date = date(2026, 8, 12)
		results = [
			self._jit_result(due_date=due_date, name="RESULT-1", qty=40),
			self._jit_result(due_date=due_date, name="RESULT-2", qty=60),
		]
		# The aggregate is still 100, but attribution is swapped 50/50.
		segments = [
			self._jit_segment(
				start=datetime(2026, 8, 12, 8, 0),
				end=datetime(2026, 8, 12, 12, 0),
				name="SEGMENT-1",
				parent="RESULT-1",
				qty=50,
			),
			self._jit_segment(
				start=datetime(2026, 8, 12, 12, 0),
				end=datetime(2026, 8, 12, 18, 0),
				name="SEGMENT-2",
				parent="RESULT-2",
				qty=50,
			),
		]
		with (
			patch.object(
				phase6.frappe,
				"get_all",
				side_effect=lambda doctype, **_kwargs: results if doctype == "APS Schedule Result" else segments,
			),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("per-result mismatch"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "per-result mismatch"),
		):
			phase6._assert_planned_jit_capacity("RUN-1", due_date=due_date, expected_qty=100)
		self.assertEqual(fail.call_args.args[0], "planned_jit_result_segment_quantity")

	def test_jit_plan_helper_rejects_wrong_result_identity_and_prebuild_segment(self):
		due_date = date(2026, 8, 12)
		wrong_result = self._jit_result(due_date=date(2026, 8, 13), strategy="Auto")
		jit_segment = self._jit_segment(
			start=datetime(2026, 8, 12, 8, 0),
			end=datetime(2026, 8, 12, 18, 0),
		)
		with (
			patch.object(
				phase6.frappe,
				"get_all",
				side_effect=lambda doctype, **_kwargs: [wrong_result] if doctype == "APS Schedule Result" else [jit_segment],
			),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("wrong JIT result identity"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "wrong JIT result identity"),
		):
			phase6._assert_planned_jit_capacity("RUN-1", due_date=due_date, expected_qty=100)
		self.assertEqual(fail.call_args.args[0], "planned_jit_result_identity")

		valid_result = self._jit_result(due_date=due_date)
		prebuild_segment = self._jit_segment(
			start=datetime(2026, 8, 12, 8, 0),
			end=datetime(2026, 8, 12, 18, 0),
			mode="Prebuild",
		)
		with (
			patch.object(
				phase6.frappe,
				"get_all",
				side_effect=lambda doctype, **_kwargs: [valid_result] if doctype == "APS Schedule Result" else [prebuild_segment],
			),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("prebuild segment rejected"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "prebuild segment rejected"),
		):
			phase6._assert_planned_jit_capacity("RUN-1", due_date=due_date, expected_qty=100)
		self.assertEqual(fail.call_args.args[0], "planned_jit_segment_mode")

	def test_jit_atp_helper_uses_last_due_day_point_not_transient_maximum(self):
		due_date = date(2026, 8, 12)
		valid_projection = {
			"results": [
				{
					"result": "RESULT-1",
					"fulfillment_demand_qty": 100,
					"timeline": [
						{"time": "2026-08-11 23:59:59", "projected_available_to_promise_qty": 999},
						{"time": "2026-08-12 08:00:00", "projected_available_to_promise_qty": 40},
						{"time": "2026-08-12 18:00:00", "projected_available_to_promise_qty": 100},
						{"time": "2026-08-13 00:00:00", "projected_available_to_promise_qty": 999},
					],
				}
			]
		}
		with patch.object(
			phase6.availability,
			"get_run_fulfillment_projection",
			return_value=valid_projection,
		):
			verified = phase6._assert_projected_jit_availability(
				"RUN-1",
				due_date=due_date,
				expected_qty=100,
				expected_results={
					"RESULT-1": {
						"planned_qty": 100,
						"last_segment_end": datetime(2026, 8, 12, 18, 0),
					}
				},
			)
		self.assertEqual(verified["projected_available_to_promise_qty"], 100)
		self.assertEqual(verified["fulfillment_demand_qty"], 100)

		transient_only_projection = copy.deepcopy(valid_projection)
		transient_only_projection["results"][0]["timeline"][1]["projected_available_to_promise_qty"] = 100
		transient_only_projection["results"][0]["timeline"][2]["projected_available_to_promise_qty"] = 40
		with (
			patch.object(
				phase6.availability,
				"get_run_fulfillment_projection",
				return_value=transient_only_projection,
			),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("transient ATP rejected"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "transient ATP rejected"),
		):
			phase6._assert_projected_jit_availability(
				"RUN-1",
				due_date=due_date,
				expected_qty=100,
				expected_results={
					"RESULT-1": {
						"planned_qty": 100,
						"last_segment_end": datetime(2026, 8, 12, 18, 0),
					}
				},
			)
		self.assertEqual(fail.call_args.args[0], "planned_jit_result_projected_atp_by_due")

	def test_jit_atp_helper_rejects_timeline_only_at_next_day_boundary(self):
		projection = {
			"results": [
				{
					"result": "RESULT-1",
					"fulfillment_demand_qty": 100,
					"timeline": [
						{"time": "2026-08-13 00:00:00", "projected_available_to_promise_qty": 100},
					],
				}
			]
		}
		with (
			patch.object(phase6.availability, "get_run_fulfillment_projection", return_value=projection),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("next day point rejected"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "next day point rejected"),
		):
			phase6._assert_projected_jit_availability(
				"RUN-1",
				due_date=date(2026, 8, 12),
				expected_qty=100,
				expected_results={
					"RESULT-1": {
						"planned_qty": 100,
						"last_segment_end": datetime(2026, 8, 12, 18, 0),
					}
				},
			)
		self.assertEqual(fail.call_args.args[0], "planned_jit_availability_result_timeline")

	def test_jit_atp_helper_requires_exact_unique_result_set_and_post_segment_cutoff(self):
		due_date = date(2026, 8, 12)
		expected = {
			"RESULT-1": {"planned_qty": 40, "last_segment_end": datetime(2026, 8, 12, 12, 0)},
			"RESULT-2": {"planned_qty": 60, "last_segment_end": datetime(2026, 8, 12, 18, 0)},
		}
		duplicate_projection = {
			"results": [
				{
					"result": "RESULT-1",
					"fulfillment_demand_qty": 40,
					"timeline": [{"time": "2026-08-12 12:00:00", "projected_available_to_promise_qty": 40}],
				},
				{
					"result": "RESULT-1",
					"fulfillment_demand_qty": 60,
					"timeline": [{"time": "2026-08-12 18:00:00", "projected_available_to_promise_qty": 60}],
				},
			]
		}
		with (
			patch.object(phase6.availability, "get_run_fulfillment_projection", return_value=duplicate_projection),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("duplicate result rejected"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "duplicate result rejected"),
		):
			phase6._assert_projected_jit_availability(
				"RUN-1",
				due_date=due_date,
				expected_qty=100,
				expected_results=expected,
			)
		self.assertEqual(fail.call_args.args[0], "planned_jit_availability_result_collection")

		stale_cutoff_projection = {
			"results": [
				{
					"result": "RESULT-1",
					"fulfillment_demand_qty": 40,
					"timeline": [{"time": "2026-08-12 12:00:00", "projected_available_to_promise_qty": 40}],
				},
				{
					"result": "RESULT-2",
					"fulfillment_demand_qty": 60,
					"timeline": [{"time": "2026-08-12 17:59:59", "projected_available_to_promise_qty": 60}],
				},
			]
		}
		with (
			patch.object(phase6.availability, "get_run_fulfillment_projection", return_value=stale_cutoff_projection),
			patch.object(
				phase6,
				"_phase6_fail",
				side_effect=phase6.frappe.ValidationError("stale cutoff rejected"),
			) as fail,
			self.assertRaisesRegex(phase6.frappe.ValidationError, "stale cutoff rejected"),
		):
			phase6._assert_projected_jit_availability(
				"RUN-1",
				due_date=due_date,
				expected_qty=100,
				expected_results=expected,
			)
		self.assertEqual(fail.call_args.args[0], "planned_jit_availability_cutoff")


if __name__ == "__main__":
	unittest.main()
