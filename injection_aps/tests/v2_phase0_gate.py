from __future__ import annotations

import hashlib
import json
import platform
from importlib import metadata
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

import frappe
from frappe.utils import cint, now_datetime

from injection_aps.services import v2_baseline, v2_flags


EVIDENCE_SCHEMA_VERSION = 1
PRODUCTION_SITE = "jce.1"
PRODUCTION_DB_PORT = 3306
SELECTED_ORTOOLS_VERSION = "9.4.1874"
REQUIRED_PROTOBUF_VERSION = "3.20.3"
PERMISSION_READER_USER = "aps-v2-phase0-reader@example.invalid"
PERMISSION_DENIED_USER = "aps-v2-phase0-denied@example.invalid"
FLAG_OFF_COUNT_DOCTYPES = (
	"APS Planning Run",
	"APS Schedule Result",
	"APS Schedule Segment",
	"APS Work Order Proposal Batch",
	"APS Shift Schedule Proposal Batch",
	"APS Release Batch",
	"Work Order",
	"Work Order Scheduling",
	"Delivery Plan",
)


def validate_isolated_environment(
	*,
	site: str,
	db_name: str,
	db_host: str,
	db_port: int,
	config: dict[str, Any],
	require_fixture: bool = False,
) -> list[str]:
	"""Return every failed isolation guard without touching Frappe state."""
	errors = []
	if site == PRODUCTION_SITE:
		errors.append("production site is forbidden")
	if not site or "test" not in site and "fixture" not in site:
		errors.append("site name is not explicitly test/fixture scoped")
	if not db_name.startswith("aps_v2_"):
		errors.append("database name is outside the APS V2 isolated namespace")
	if str(db_host) not in {"127.0.0.1", "localhost"}:
		errors.append("database host is not loopback")
	if int(db_port or 0) == PRODUCTION_DB_PORT:
		errors.append("production database port is forbidden")
	if int(db_port or 0) != 13306:
		errors.append("unexpected isolated database port")
	for fieldname in ("aps_v2_isolated_environment", "pause_scheduler", "mute_emails", "disable_async"):
		if cint(config.get(fieldname)) != 1:
			errors.append(f"{fieldname} must be enabled")
	if require_fixture and cint(config.get("aps_v2_fixture_environment")) != 1:
		errors.append("aps_v2_fixture_environment must be enabled")
	return errors


def assert_isolated_environment(*, require_fixture: bool = False) -> None:
	errors = validate_isolated_environment(
		site=str(frappe.local.site or ""),
		db_name=str(frappe.conf.db_name or ""),
		db_host=str(frappe.conf.db_host or ""),
		db_port=int(frappe.conf.db_port or 0),
		config=dict(frappe.conf),
		require_fixture=require_fixture,
	)
	if errors:
		raise RuntimeError("Phase 0 isolation guard failed: " + "; ".join(errors))


def capture_legacy_baseline_evidence(
	planning_runs: Iterable[str] | str | None = None,
	output_dir: str | None = None,
) -> dict[str, Any]:
	"""Capture two read-only Legacy snapshots per run and prove repeatability."""
	assert_isolated_environment()
	frappe.set_user("Administrator")
	run_names = _normalize_run_names(planning_runs)
	if not run_names:
		run_names = frappe.get_all(
			"APS Planning Run",
			pluck="name",
			order_by="planning_date asc, name asc",
			limit_page_length=0,
		)
	if not run_names:
		raise RuntimeError("No APS Planning Run exists for Legacy baseline capture")

	artifact_dir = Path(
		output_dir
		or frappe.get_site_path("private", "files", "aps_v2_phase0", "legacy_baseline")
	).resolve()
	artifact_dir.mkdir(parents=True, exist_ok=True)

	started = perf_counter()
	run_records = []
	for run_name in run_names:
		first = v2_baseline.capture_legacy_baseline(run_name)
		second = v2_baseline.capture_legacy_baseline(run_name)
		first_path = artifact_dir / f"legacy-{_safe_filename(run_name)}.json"
		_write_json(first_path, first)
		run_records.append(
			{
				"planning_run": run_name,
				"repeatable": first["content_fingerprint"] == second["content_fingerprint"],
				"content_fingerprint": first["content_fingerprint"],
				"counts": first["counts"],
				"snapshot": str(first_path),
				"snapshot_sha256": _sha256_file(first_path),
			}
		)

	capabilities = v2_flags.get_v2_capabilities()
	manifest = {
		"schema_version": EVIDENCE_SCHEMA_VERSION,
		"site": frappe.local.site,
		"database": frappe.conf.db_name,
		"database_port": int(frappe.conf.db_port),
		"captured_on": str(now_datetime()),
		"capture_mode": "READ_ONLY",
		"elapsed_seconds": round(perf_counter() - started, 6),
		"planning_run_count": len(run_records),
		"all_runs_repeatable": all(row["repeatable"] for row in run_records),
		"legacy_mode_confirmed": capabilities["mode"] == "Legacy",
		"formal_v2_writes_enabled": capabilities["formal_v2_writes_enabled"],
		"v2_settings": capabilities["settings"],
		"solver_runtime": capabilities["solver_runtime"],
		"runs": run_records,
	}
	manifest["passed"] = bool(
		manifest["all_runs_repeatable"]
		and manifest["legacy_mode_confirmed"]
		and not manifest["formal_v2_writes_enabled"]
	)
	manifest_path = artifact_dir / "manifest.json"
	_write_json(manifest_path, manifest)
	if not manifest["passed"]:
		raise RuntimeError(f"Legacy baseline gate failed; see {manifest_path}")
	return {
		"passed": True,
		"manifest": str(manifest_path),
		"planning_run_count": len(run_records),
		"elapsed_seconds": manifest["elapsed_seconds"],
		"fingerprints": {
			row["planning_run"]: row["content_fingerprint"] for row in run_records
		},
	}


def run_phase0_permission_flag_gate(output_dir: str | None = None) -> dict[str, Any]:
	"""Exercise the public Phase 0 APIs with real roles while V2 is disabled."""
	assert_isolated_environment(require_fixture=True)
	if frappe.local.site != "aps-opt-fixture.localhost":
		raise RuntimeError("Permission/Flag-Off integration is restricted to aps-opt-fixture.localhost")
	from injection_aps.api import app

	frappe.set_user("Administrator")
	_ensure_gate_user(PERMISSION_READER_USER, ("Manufacturing Manager",))
	_ensure_gate_user(PERMISSION_DENIED_USER, ("Employee",))
	run_name = frappe.db.get_value(
		"APS Planning Run",
		{"notes": ("like", "APS-V2-FIXTURE-SCENARIO|%")},
		"name",
		order_by="planning_date asc, name asc",
	)
	if not run_name:
		raise RuntimeError("Run the Phase 0 fixture gate before the permission gate")

	settings = v2_flags.get_v2_settings()
	counts_before = _flag_off_document_counts()
	reader_payload = {}
	denied_payload = {}
	try:
		frappe.set_user(PERMISSION_READER_USER)
		capabilities = app.get_v2_capabilities()
		baseline = app.capture_legacy_baseline(run_name)
		comparison = app.get_legacy_v2_comparison(
			run_name,
			legacy_fingerprint=baseline["content_fingerprint"],
		)
		reader_payload = {
			"user": PERMISSION_READER_USER,
			"roles": frappe.get_roles(),
			"capability_mode": capabilities["mode"],
			"formal_v2_writes_enabled": capabilities["formal_v2_writes_enabled"],
			"baseline_fingerprint": baseline["content_fingerprint"],
			"baseline_counts": baseline["counts"],
			"comparison_status": comparison["status"],
			"comparison_reason_code": comparison["reason_code"],
		}

		frappe.set_user(PERMISSION_DENIED_USER)
		for method_name, method in (
			("get_v2_capabilities", app.get_v2_capabilities),
			("capture_legacy_baseline", lambda: app.capture_legacy_baseline(run_name)),
			("get_legacy_v2_comparison", lambda: app.get_legacy_v2_comparison(run_name)),
		):
			try:
				method()
			except frappe.PermissionError as exc:
				denied_payload[method_name] = {"denied": True, "exception": type(exc).__name__}
			else:
				denied_payload[method_name] = {"denied": False, "exception": None}
	finally:
		frappe.set_user("Administrator")

	counts_after = _flag_off_document_counts()
	boolean_flags_off = all(not settings[fieldname] for fieldname in v2_flags.BOOLEAN_FLAGS)
	gates = {
		"all_v2_boolean_flags_off": boolean_flags_off,
		"legacy_solver_selected": settings["solver_engine"] == "Legacy",
		"business_reader_can_use_read_only_apis": bool(reader_payload.get("baseline_fingerprint")),
		"business_reader_sees_legacy_mode": reader_payload.get("capability_mode") == "Legacy",
		"formal_v2_writes_disabled": reader_payload.get("formal_v2_writes_enabled") is False,
		"comparison_is_phase0_stub": reader_payload.get("comparison_status") == "Not Available",
		"unauthorized_user_denied_every_api": bool(denied_payload)
		and all(row["denied"] for row in denied_payload.values()),
		"aps_and_formal_document_counts_unchanged": counts_before == counts_after,
	}
	payload = {
		"schema_version": EVIDENCE_SCHEMA_VERSION,
		"site": frappe.local.site,
		"database": frappe.conf.db_name,
		"captured_on": str(now_datetime()),
		"planning_run": run_name,
		"settings": settings,
		"reader": reader_payload,
		"denied_user": {"user": PERMISSION_DENIED_USER, "api_results": denied_payload},
		"document_counts_before": counts_before,
		"document_counts_after": counts_after,
		"acceptance_gates": gates,
		"passed": all(gates.values()),
	}
	artifact_dir = Path(
		output_dir
		or frappe.get_site_path("private", "files", "aps_v2_phase0", "permission_flag_off")
	).resolve()
	artifact_dir.mkdir(parents=True, exist_ok=True)
	manifest_path = artifact_dir / "manifest.json"
	_write_json(manifest_path, payload)
	if not payload["passed"]:
		raise RuntimeError(f"Permission/Flag-Off gate failed; see {manifest_path}")
	return {"passed": True, "manifest": str(manifest_path), "planning_run": run_name}


def run_phase0_ortools_compatibility_gate(output_dir: str | None = None) -> dict[str, Any]:
	"""Prove CP-SAT works without replacing Frappe's protobuf dependency."""
	assert_isolated_environment()
	import google.api_core  # noqa: F401 - the import is an explicit compatibility assertion
	from google.protobuf import __version__ as protobuf_version
	from ortools.sat.python import cp_model

	model = cp_model.CpModel()
	x = model.NewIntVar(0, 10, "x")
	model.Maximize(x)
	solver = cp_model.CpSolver()
	status = solver.Solve(model)
	ortools_version = metadata.version("ortools")
	capabilities = v2_flags.get_v2_capabilities()
	gates = {
		"selected_ortools_version_imported": ortools_version == SELECTED_ORTOOLS_VERSION,
		"existing_protobuf_version_preserved": protobuf_version == REQUIRED_PROTOBUF_VERSION,
		"frappe_imported": bool(frappe.__version__),
		"google_api_core_imported": True,
		"cp_sat_reaches_optimal": solver.StatusName(status) == "OPTIMAL",
		"cp_sat_objective_is_10": solver.ObjectiveValue() == 10,
		"capability_reader_detects_solver": bool(capabilities["solver_runtime"]["available"]),
		"solver_remains_disabled_for_formal_writes": capabilities["formal_v2_writes_enabled"] is False,
	}
	payload = {
		"schema_version": EVIDENCE_SCHEMA_VERSION,
		"site": frappe.local.site,
		"database": frappe.conf.db_name,
		"captured_on": str(now_datetime()),
		"python": platform.python_version(),
		"ortools": ortools_version,
		"protobuf": protobuf_version,
		"cp_sat": {"status": solver.StatusName(status), "objective": solver.ObjectiveValue()},
		"capabilities": capabilities,
		"compatibility_boundary": {
			"embedded_solver": f"ortools=={SELECTED_ORTOOLS_VERSION}",
			"reason": (
				"OR-Tools 9.5+ requires protobuf 4 or newer, while this Frappe environment "
				"requires protobuf below 4 through google-api-core."
			),
			"upgrade_path": "Run a newer solver in a dependency-isolated process; do not upgrade protobuf in the Frappe env.",
		},
		"acceptance_gates": gates,
		"passed": all(gates.values()),
	}
	artifact_dir = Path(
		output_dir
		or frappe.get_site_path("private", "files", "aps_v2_phase0", "ortools_compatibility")
	).resolve()
	artifact_dir.mkdir(parents=True, exist_ok=True)
	manifest_path = artifact_dir / "manifest.json"
	_write_json(manifest_path, payload)
	if not payload["passed"]:
		raise RuntimeError(f"OR-Tools compatibility gate failed; see {manifest_path}")
	return {"passed": True, "manifest": str(manifest_path), "ortools": ortools_version}


def _ensure_gate_user(user: str, roles: tuple[str, ...]) -> None:
	if not frappe.db.exists("User", user):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": user,
				"first_name": "APS V2 Phase 0 Gate",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
	else:
		doc = frappe.get_doc("User", user)
		doc.enabled = 1
		existing_roles = {row.role for row in doc.roles}
		for role in roles:
			if role not in existing_roles:
				doc.append("roles", {"role": role})
		doc.save(ignore_permissions=True)
	frappe.clear_cache(user=user)


def _flag_off_document_counts() -> dict[str, int | None]:
	return {
		doctype: frappe.db.count(doctype) if frappe.db.exists("DocType", doctype) else None
		for doctype in FLAG_OFF_COUNT_DOCTYPES
	}


def _normalize_run_names(planning_runs: Iterable[str] | str | None) -> list[str]:
	if not planning_runs:
		return []
	if isinstance(planning_runs, str):
		try:
			decoded = json.loads(planning_runs)
		except json.JSONDecodeError:
			decoded = [planning_runs]
		planning_runs = decoded if isinstance(decoded, list) else [decoded]
	return sorted({str(name).strip() for name in planning_runs if str(name).strip()})


def _safe_filename(value: str) -> str:
	return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)


def _write_json(path: Path, payload: Any) -> None:
	path.write_text(
		json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)


def _sha256_file(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()
