import csv
import importlib.util
import json
import re
import unittest
from collections import Counter
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
CONFLICT_KEYS = {
	"Impact Summary",
	"Downtime",
	"Current Qty",
	"New",
	"Target Qty",
	"Release Date",
	"View Mode",
	"Horizon Days",
	"Horizon Start",
	"Horizon End",
	"APS Run",
	"Exception Count",
	"Net Qty",
	"Try changing the filters or refreshing the data.",
	"No rows available to export.",
	"Invalid export payload.",
	"Missing BOM",
	"Delay",
	"Open WO",
	"Manual Override",
	"Superseded",
	"Warnings",
}


def _read_translation_rows(path):
	with path.open(encoding="utf-8-sig", newline="") as handle:
		return [row for row in csv.reader(handle) if len(row) >= 2 and row[0]]


PLACEHOLDER_PATTERN = re.compile(
	r"(?<!\{)\{(?:\d+|[A-Za-z_][A-Za-z0-9_]*)(?:![^{}]+|:[^{}]+)?\}(?!\})"
	r"|%\([^)]+\)[#0+\- ]*[diouxXeEfFgGcrs%]"
	r"|(?<!%)%[#0+\- ]*(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs]"
)

OFFICIAL_CONTEXTLESS_FALLBACKS = {
	"Company": "公司",
	"Customer": "客户",
	"Item": "物料",
}


class TestUIStaticContracts(unittest.TestCase):
	def test_plain_text_conversion_uses_inert_template_content(self):
		source = (APP_ROOT / "public/js/injection_aps_shared.js").read_text(encoding="utf-8")
		plain_text = source[
			source.index("injection_aps.ui.to_plain_text") : source.index("injection_aps.ui.format_number")
		]
		self.assertIn('document.createElement("template")', plain_text)
		self.assertIn("template.content.textContent", plain_text)
		self.assertNotIn('document.createElement("div")', plain_text)

	def test_run_console_distills_primary_decisions_into_three_columns_and_drawer(self):
		source = (
			APP_ROOT
			/ "injection_aps/page/aps_run_console/aps_run_console.js"
		).read_text(encoding="utf-8")
		visible_columns = source[
			source.index("\t\tconst columns = [") : source.index("\n\t\tconst exportColumns = [")
		]
		self.assertEqual(visible_columns.count("fieldname:"), 3)
		for marker in (
			'fieldname: "run_overview"',
			'fieldname: "key_results"',
			'fieldname: "next_action"',
			'class="ia-run-overview"',
			'class="ia-run-metric-grid ia-run-key-metrics"',
			'class="ia-status-line"',
			'class="ia-page ia-drawer-stack ia-run-drawer"',
			'class="ia-kv ia-run-drawer-metrics"',
			'data-run-details=',
			'injection_aps.ui.open_drawer(',
			'action_key: "open_run"',
			'aps_run_console.css?v=20260901.1',
			"export_columns: exportColumns",
			"return injection_aps.ui.format_number(value);",
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)
		self.assertNotIn("frappe.format(", source)

		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		translations = {(row[0], row[2] if len(row) > 2 else ""): row[1] for row in rows}
		self.assertEqual(translations.get(("Analyze and Apply Capacity", "")), "分析并应用产能方案")
		self.assertEqual(translations.get(("APS Run Details", "Injection APS")), "运算详情")
		self.assertEqual(translations.get(("Key Results", "Injection APS")), "关键结果")

		for original_field in (
			"total_net_requirement_qty",
			"total_machine_scheduled_qty",
			"total_demand_covered_qty",
			"total_overproduction_qty",
			"total_unscheduled_qty",
			"total_produced_qty",
			"total_delivered_qty",
			"execution_health",
		):
			with self.subTest(export_field=original_field):
				self.assertIn(f'fieldname: "{original_field}"', source)

		css = (APP_ROOT / "public/css/aps_run_console.css").read_text(encoding="utf-8")
		for marker in (
			".ia-run-table .ia-table",
			"min-width: 900px",
			".ia-run-key-metrics",
			".ia-run-drawer-number",
			".ia-run-nav-actions",
			"@media (max-width: 960px)",
			"@media (max-width: 640px)",
		):
			with self.subTest(css_marker=marker):
				self.assertIn(marker, css)
		self.assertNotIn(":has(.ia-run-drawer)", css)
		self.assertNotIn(".ia-run-drawer-section", css)

	def test_reused_pages_resync_and_clear_route_run_state(self):
		constraint = (
			APP_ROOT
			/ "injection_aps/page/aps_constraint_resolution_center/aps_constraint_resolution_center.js"
		).read_text(encoding="utf-8")
		run_console = (
			APP_ROOT / "injection_aps/page/aps_run_console/aps_run_console.js"
		).read_text(encoding="utf-8")
		progress = (
			APP_ROOT / "injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js"
		).read_text(encoding="utf-8")

		self.assertIn("if (nextRun !== state.run)", constraint)
		self.assertNotIn("if (nextRun && nextRun !== state.run)", constraint)
		self.assertIn("wrapper.injection_aps_controller.syncRouteState();", run_console)
		self.assertIn("wrapper.injection_aps_controller.syncRouteState();", progress)
		self.assertIn('const runName = injection_aps.ui.get_query_param("run_name") || "";', progress)

	def test_execution_exception_drawer_loads_authoritative_context_and_guidance(self):
		source = (
			APP_ROOT
			/ "injection_aps/page/aps_release_center/aps_release_center.js"
		).read_text(encoding="utf-8")
		for marker in (
			'"injection_aps.api.app.get_exception_resolution_context"',
			"renderExceptionResolution(detail, loadError)",
			'class="ia-page ia-drawer-stack ia-exception-drawer"',
			'class="ia-panel ia-resolution-panel"',
			"renderExceptionSourceFacts(detail)",
			"getExceptionSuggestedActions(detail)",
			"injection_aps.ui.item_identity(row)",
			'fieldname: "root_cause_text"',
			'fieldname: "suggested_actions"',
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)

	def test_gantt_manual_changes_return_and_render_authoritative_segment_state(self):
		gantt = (
			APP_ROOT
			/ "injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js"
		).read_text(encoding="utf-8")
		planning = (APP_ROOT / "services/planning.py").read_text(encoding="utf-8")
		for marker in (
			"this.refreshGeneration = 0",
			"const refreshGeneration = ++this.refreshGeneration",
			"applyManualAdjustmentLocally(response)",
			"details.segment_planned_qty = Number(segment.planned_qty || 0)",
			"this.renderGantt(this.data.tasks)",
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, gantt)
		for marker in (
			'"updated_segment": {',
			'"start_time": updated_segment.get("current_start_time")',
			'"planned_qty": updated_segment.get("planned_qty")',
			'"updated_result": {',
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, planning)

	def test_gantt_uses_version_safe_shared_ui_loader(self):
		gantt = (
			APP_ROOT
			/ "injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js"
		).read_text(encoding="utf-8")
		self.assertNotIn(
			'frappe.require("/assets/injection_aps/js/injection_aps_ui_loader.js"',
			gantt,
		)
		self.assertIn('injection_aps.ui_loader.start("20260901.1"', gantt)
		self.assertNotIn(
			'frappe.require("/assets/injection_aps/js/injection_aps_shared.js?v=',
			gantt,
		)

	def test_gantt_avoids_duplicate_fetches_and_scroll_reflows(self):
		gantt = (
			APP_ROOT
			/ "injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js"
		).read_text(encoding="utf-8")
		for marker in (
			'this.loadingKey = ""',
			'this.dataRunName = ""',
			"if (this.loadingKey === loadingKey)",
			"if (this.data && this.dataRunName !== runName)",
			"scheduleDependencyRender()",
			"if (this.dependencyRaf !== null)",
			"this.renderGantt(this.data.tasks || [])",
			'role="button"',
			'tabindex="0"',
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, gantt)
		api = (APP_ROOT / "api/app.py").read_text(encoding="utf-8")
		self.assertNotIn('"fulfillment_timeline":', api)
		self.assertNotIn('"fulfillment_results":', api)

	def test_gantt_risk_values_are_translated_per_enum_across_all_render_paths(self):
		source = (
			APP_ROOT
			/ "injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js"
		).read_text(encoding="utf-8")
		for marker in (
			"getRiskValues(value)",
			"translateRiskValues(value)",
			"translateRiskText(value, separator)",
			"translateRiskMessage(value)",
			"this.translateRiskValues(row.exception_types || [])",
			"this.translateRiskText(flag)",
			"risk_status: this.translateRiskText(row.risk_status || \"\")",
			"blocking_reason: this.translateRiskMessage(row.blocking_reason || \"\")",
			"exception_summary: this.translateRiskText(row.exception_types || [], \", \")",
			"risk_flags: this.translateRiskText(row.risk_flags || \"\")",
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)
		self.assertNotIn(
			'visibleRiskFlags.map((flag) => `<span class="ia-gantt-flag ${String(flag).includes("FDA") ? "red" : "orange"}">${injection_aps.ui.escape(flag)}</span>`)',
			source,
		)

		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		keys = {(row[0], row[2] if len(row) > 2 else "") for row in rows}
		for value in (
			"Late Delivery",
			"Unscheduled Quantity",
			"Copy Mold Parallelized",
			"Plan Consistency Error",
			"Demand Lineage Changed",
			"Frozen / Locked",
			"Execution: Delayed",
			"Execution: Slow Progress",
			"Execution: No Recent Update",
			"Execution: Overproduced",
			"Urgent Order",
			"Mold Master Missing",
			"Primary Segment Missing",
			"Mold Reference Empty",
			"Mold Product Missing",
			"Mold Status Blocked",
			"Mold Cycle Missing",
			"Slow Progress",
			"Delayed Execution",
			"No Recent Update",
			"Actual Output Mismatch",
			"There are still {0} APS exceptions waiting for review.",
			"Plan consistency is {0}. Recalculate and resolve consistency errors before release.",
			"There is still unscheduled quantity: {0}.",
			"Plan consistency: {0}",
		):
			with self.subTest(value=value):
				self.assertIn((value, "Injection APS"), keys)

	def test_schedule_preview_preserves_all_p1_policy_fields(self):
		source = (
			APP_ROOT
			/ "injection_aps/page/aps_schedule_console/aps_schedule_console.js"
		).read_text(encoding="utf-8")
		for fieldname in (
			"production_strategy",
			"demand_confidence",
			"cancellation_risk_percent",
			"prebuild_allowed",
			"max_prebuild_days",
		):
			with self.subTest(fieldname=fieldname):
				self.assertGreaterEqual(source.count(fieldname), 4)
		self.assertIn('action_key: "refresh_preview"', source)
		self.assertIn('title: __("Schedule Import Failed")', source)

	def test_schedule_file_recognition_discards_stale_async_responses(self):
		source = (
			APP_ROOT
			/ "injection_aps/page/aps_schedule_console/aps_schedule_console.js"
		).read_text(encoding="utf-8")
		for marker in (
			"iaInspectionGeneration",
			"getImportInspectionSnapshot",
			"isImportInspectionRequestCurrent",
			'requestSnapshot.file_url === String(dialog.get_value("file_url") || "")',
			'requestSnapshot.sheet_name === String(dialog.get_value("sheet_name") || "")',
			'requestSnapshot.header_row_no === String(dialog.get_value("header_row_no") || "")',
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)

		inspection_source = source[
			source.index("\tasync inspectImportSource") : source.index("\n\tasync previewImport")
		]
		guard = "if (!this.isImportInspectionRequestCurrent(dialog, requestSnapshot))"
		self.assertGreaterEqual(inspection_source.count(guard), 2)
		self.assertLess(
			inspection_source.index(guard),
			inspection_source.index("dialog.iaInspectionResponse = response"),
		)
		self.assertIn("dialog.iaInspectionGeneration = requestSnapshot.generation", inspection_source)
		self.assertGreaterEqual(source.count("await this.refreshImportRecognition(dialog"), 3)
		self.assertIn(
			"dialog.iaInspectionGeneration = (dialog.iaInspectionGeneration || 0) + 1",
			source,
		)

	def test_schedule_sheet_and_header_changes_replace_stale_mapping_before_preview(self):
		source = (
			APP_ROOT
			/ "injection_aps/page/aps_schedule_console/aps_schedule_console.js"
		).read_text(encoding="utf-8")
		for marker in (
			"getImportInspectionSourceSignature",
			"ensureImportRecognitionCurrent",
			"dialog.iaInspectionSourceSignature",
			"responseSourceSignature",
			"const response = await this.ensureImportRecognitionCurrent(dialog)",
			"values = dialog.get_values()",
			'if (settings.forceHeader && fieldname === "header_row_no")',
			'await dialog.set_value(fieldname, "")',
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)
		self.assertNotIn("if (!settings.forceSheet && !settings.forceHeader)", source)
		self.assertIn(
			"responseSourceSignature !== this.getImportInspectionSourceSignature(dialog)",
			source,
		)
		confirm_source = source[
			source.index('\t\tdialog.set_primary_action(__("Confirm Recognition and View Differences"') :
			source.index("\n\tresetImportRecognition")
		]
		self.assertLess(
			confirm_source.index("ensureImportRecognitionCurrent"),
			confirm_source.index("this.previewImport(values)"),
		)

	def test_form_scripts_wait_for_shared_ui_before_using_it(self):
		contracts = {
			"aps_change_request.js": "CHANGE_REQUEST_SHARED_READY",
			"aps_planning_run.js": "PLANNING_RUN_SHARED_READY",
			"aps_shift_schedule_proposal_batch.js": "SHIFT_PROPOSAL_SHARED_READY",
			"customer_delivery_schedule.js": "CUSTOMER_SCHEDULE_SHARED_READY",
			"aps_schedule_import_batch.js": "SCHEDULE_IMPORT_BATCH_SHARED_READY",
			"aps_release_batch.js": "RELEASE_BATCH_SHARED_READY",
			"aps_work_order_proposal_batch.js": "WORK_ORDER_PROPOSAL_SHARED_READY",
		}
		for filename, ready_name in contracts.items():
			source = (APP_ROOT / "public/js" / filename).read_text(encoding="utf-8")
			with self.subTest(filename=filename):
				declaration = "let" if filename == "aps_planning_run.js" else "const"
				self.assertIn(
					f'{declaration} {ready_name} = injection_aps.ui_loader.load("20260901.1")',
					source,
				)
				self.assertIn(f"await {ready_name};", source)
				self.assertLess(
					source.index(f"await {ready_name};"),
					source.index("injection_aps.ui.ensure_styles()"),
				)

	def test_non_gantt_pages_use_hot_reload_safe_ui_assets(self):
		page_root = APP_ROOT / "injection_aps/page"
		page_sources = sorted(
			path for path in page_root.glob("*/*.js")
			if path.parent.name != "aps_schedule_gantt"
		)
		self.assertEqual(len(page_sources), 10)
		for path in page_sources:
			source = path.read_text(encoding="utf-8")
			with self.subTest(page=path.parent.name):
				self.assertNotIn(
					'frappe.require("/assets/injection_aps/js/injection_aps_ui_loader.js"',
					source,
				)
				self.assertIn('injection_aps.ui_loader.start("20260901.1"', source)

		loader = (APP_ROOT / "public/js/injection_aps_ui_loader.js").read_text(encoding="utf-8")
		shared = (APP_ROOT / "public/js/injection_aps_shared.js").read_text(encoding="utf-8")
		hooks = (APP_ROOT / "hooks.py").read_text(encoding="utf-8")
		self.assertLess(
			hooks.index('"/assets/injection_aps/js/injection_aps_ui_loader.js"'),
			hooks.index('"/assets/injection_aps/js/injection_aps_shared.js"'),
		)
		self.assertIn('injection_aps_shared.js?v=${encodeURIComponent(version)}', loader)
		self.assertIn('const UI_ASSET_VERSION = "20260901.1"', shared)
		self.assertIn('existingStyle.setAttribute("href", styleHref)', shared)
		self.assertIn('aps-icons.svg?v=${UI_ASSET_VERSION}', shared)

	def test_customer_progress_ignores_stale_refresh_results(self):
		source = (
			APP_ROOT
			/ "injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js"
		).read_text(encoding="utf-8")
		for marker in (
			"this.refreshGeneration = 0",
			"const refreshGeneration = ++this.refreshGeneration",
			"const filters = this.getFilters()",
			"if (refreshGeneration !== this.refreshGeneration)",
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)
		refresh_source = source[source.index("\tasync refresh()") : source.index("\n\trefreshFromFilter()")]
		self.assertGreaterEqual(
			refresh_source.count("if (refreshGeneration !== this.refreshGeneration)"),
			2,
		)
		self.assertLess(
			refresh_source.index("if (refreshGeneration !== this.refreshGeneration)"),
			refresh_source.index("this.data = data || {}"),
		)

	def test_dynamic_select_options_use_app_translation_context(self):
		contracts = {
			"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js": (
				'["", "Delivered", "Stock Covered", "On Track", "At Risk", "Late", "Uncovered", "No Formal Plan"].join("\\n")',
			),
			"injection_aps/page/aps_change_impact_center/aps_change_impact_center.js": (
				'options: "\\nDraft\\nAnalyzed\\nPMC Confirmed\\nApproved\\nApplied\\nRejected\\nCancelled"',
			),
			"injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js": (
				'["Machine", "Mold", "Risk", "Locked"].join("\\n")',
				'["Time", "Qty", "Downtime Window"].join("\\n")',
				'["Plant Floor", "Workstation", "Company"].join("\\n")',
			),
			"injection_aps/page/aps_schedule_console/aps_schedule_console.js": (
				'["Replace Scope", "Partial Update", "Append"].join("\\n")',
				'["Block", "Sum"].join("\\n")',
				'["rows", "matrix"].join("\\n")',
				'["auto", "range"].join("\\n")',
			),
		}
		for relative_path, option_markers in contracts.items():
			source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
			with self.subTest(relative_path=relative_path):
				for marker in option_markers:
					marker_index = source.index(marker)
					self.assertIn(
						'context: "Injection APS"',
						source[marker_index : marker_index + len(marker) + 100],
					)

		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		keys = {(row[0], row[2] if len(row) > 2 else "") for row in rows}
		for value in (
			"Stock Covered",
			"No Formal Plan",
			"At Risk",
			"Uncovered",
			"Draft",
			"PMC Confirmed",
			"Rejected",
			"Cancelled",
			"Mold",
			"Locked",
			"Qty",
			"Downtime Window",
			"Plant Floor",
			"Replace Scope",
			"Partial Update",
			"Append",
			"Block",
			"Sum",
			"rows",
			"matrix",
			"auto",
			"range",
		):
			with self.subTest(value=value):
				self.assertIn((value, "Injection APS"), keys)

	def test_current_changed_ui_has_exact_official_frappe_translations(self):
		from frappe.gettext.extractors.utils import extract_messages_from_code
		from frappe.translate import extract_messages_from_javascript_code

		targets = (
			"injection_aps/page/aps_change_impact_center/aps_change_impact_center.js",
			"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
			"injection_aps/page/aps_net_requirement_workbench/aps_net_requirement_workbench.js",
			"injection_aps/page/aps_release_center/aps_release_center.js",
			"injection_aps/page/aps_run_console/aps_run_console.js",
			"injection_aps/page/aps_schedule_console/aps_schedule_console.js",
			"injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js",
			"public/js/aps_change_request.js",
			"public/js/aps_change_request_list.js",
			"public/js/aps_planning_run.js",
			"public/js/aps_release_batch.js",
			"public/js/aps_schedule_import_batch.js",
			"public/js/aps_shift_schedule_proposal_batch.js",
			"public/js/aps_work_order_proposal_batch.js",
			"public/js/customer_delivery_schedule.js",
			"public/js/injection_aps_shared.js",
		)
		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		keys = {
			(row[0], row[2] if len(row) > 2 and row[2] else None)
			for row in rows
		}
		missing = []
		for relative_path in targets:
			path = APP_ROOT / relative_path
			code = path.read_text(encoding="utf-8")
			for extractor in (extract_messages_from_code, extract_messages_from_javascript_code):
				for line, message, context in extractor(code):
					if (message, context) not in keys:
						missing.append(f"{relative_path}:{line}: {message!r} [{context!r}]")
		self.assertEqual(missing, [])

	def test_all_app_code_and_metadata_have_runtime_chinese_translations(self):
		from frappe.gettext.extractors.utils import extract_messages_from_code
		from frappe.translate import (
			extract_messages_from_javascript_code,
			extract_messages_from_python_code,
		)

		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		translations = {
			(row[0], row[2] if len(row) > 2 and row[2] else None): row[1]
			for row in rows
		}

		installed_fallbacks = {}
		frappe_spec = importlib.util.find_spec("frappe")
		if frappe_spec and frappe_spec.origin:
			apps_root = Path(frappe_spec.origin).resolve().parents[2]
			for path in apps_root.glob("*/*/translations/zh.csv"):
				if path.parents[2].name == "injection_aps":
					continue
				for row in _read_translation_rows(path):
					if len(row) < 3 or not row[2]:
						installed_fallbacks.setdefault(row[0], set()).add(row[1])

		missing = []
		for path in APP_ROOT.rglob("*"):
			if (
				not path.is_file()
				or path.suffix not in {".py", ".js", ".html", ".vue"}
				or "tests" in path.parts
				or "__pycache__" in path.parts
			):
				continue
			code = path.read_text(encoding="utf-8")
			if path.suffix == ".py":
				messages = extract_messages_from_python_code(code)
			else:
				messages = extract_messages_from_code(code)
				messages += extract_messages_from_javascript_code(code)
			for line, message, context in messages:
				if translations.get((message, context)):
					continue
				expected_fallback = OFFICIAL_CONTEXTLESS_FALLBACKS.get(message)
				if (
					context is None
					and expected_fallback
					and expected_fallback in installed_fallbacks.get(message, set())
				):
					continue
				missing.append(
					f"{path.relative_to(APP_ROOT)}:{line}: {message!r} [{context!r}]"
				)

		metadata_keys = set()
		for path in APP_ROOT.glob("injection_aps/doctype/*/*.json"):
			definition = json.loads(path.read_text(encoding="utf-8"))
			if definition.get("doctype") != "DocType":
				continue
			doctype = definition.get("name")
			metadata_keys.add((doctype, None, path))
			if definition.get("description"):
				metadata_keys.add((definition["description"], doctype, path))
			for field in definition.get("fields") or []:
				for property_name in ("label", "description"):
					if field.get(property_name):
						metadata_keys.add((field[property_name], doctype, path))
				if field.get("fieldtype") == "Select" and field.get("options"):
					for option in field["options"].split("\n"):
						if option and not option.startswith("icon"):
							metadata_keys.add((option, doctype, path))

		for path in APP_ROOT.glob("injection_aps/page/*/*.json"):
			definition = json.loads(path.read_text(encoding="utf-8"))
			if definition.get("doctype") == "Page":
				metadata_keys.add((definition.get("title") or definition.get("name"), None, path))

		for message, context, path in metadata_keys:
			if not translations.get((message, context)):
				missing.append(
					f"{path.relative_to(APP_ROOT)}: metadata {message!r} [{context!r}]"
				)

		self.assertEqual(missing, [])

	def test_chinese_translation_csv_has_unique_keys_and_matching_placeholders(self):
		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		self.assertTrue(all(len(row) in {2, 3} for row in rows))
		keys = [(row[0], row[2] if len(row) > 2 else "") for row in rows]
		self.assertEqual(len(keys), len(set(keys)))

		placeholder_mismatches = []
		for row in rows:
			source_placeholders = Counter(PLACEHOLDER_PATTERN.findall(row[0]))
			target_placeholders = Counter(PLACEHOLDER_PATTERN.findall(row[1]))
			if source_placeholders != target_placeholders:
				placeholder_mismatches.append((row, source_placeholders, target_placeholders))
		self.assertEqual(placeholder_mismatches, [])

	def test_chinese_translations_do_not_expose_internal_english_workflow_terms(self):
		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		forbidden = re.compile(
			r"(?<![A-Za-z])(?:Demand Identity|Planning Run|Solver Job|Current Plan|"
			r"Forecast|Commitments?|Formal|Trial|Legacy|Apply|Runs?|Phase 1)(?![A-Za-z])",
			re.IGNORECASE,
		)
		mixed = [
			f"{source!r} => {translation!r}"
			for source, translation, *_ in rows
			if forbidden.search(translation)
		]
		self.assertEqual(mixed, [])

	def test_allocation_helpers_are_not_searchable_or_mutable_by_roles(self):
		for doctype in ("aps_delivery_allocation", "aps_production_allocation"):
			path = APP_ROOT / "injection_aps/doctype" / doctype / f"{doctype}.json"
			definition = json.loads(path.read_text(encoding="utf-8"))
			with self.subTest(doctype=definition["name"]):
				self.assertEqual(definition.get("read_only"), 1)
				for permission in definition.get("permissions", []):
					self.assertFalse(permission.get("create"))
					self.assertFalse(permission.get("write"))
					self.assertFalse(permission.get("delete"))

	def test_auxiliary_doctypes_are_not_exposed_in_workspace_json(self):
		workspace_files = list(APP_ROOT.glob("**/workspace/**/*.json"))
		combined = "\n".join(path.read_text(encoding="utf-8") for path in workspace_files)
		for auxiliary_doctype in (
			"APS Delivery Allocation",
			"APS Production Allocation",
			"APS Demand Pool",
			"APS Net Requirement",
			"APS Exception Log",
		):
			with self.subTest(auxiliary_doctype=auxiliary_doctype):
				self.assertNotIn(auxiliary_doctype, combined)

		workspace = json.loads(
			(
				APP_ROOT
				/ "injection_aps/workspace/injection_aps/injection_aps.json"
			).read_text(encoding="utf-8")
		)
		self.assertTrue(
			any(
				row.get("link_to") == "aps-change-impact-center"
				for row in workspace.get("links", [])
			)
		)
		self.assertTrue(
			any(
				row.get("link_to") == "aps-change-impact-center"
				for row in workspace.get("shortcuts", [])
			)
		)

	def test_fulfillment_warnings_are_visible_on_pmc_pages(self):
		api_source = (APP_ROOT / "api/app.py").read_text(encoding="utf-8")
		self.assertGreaterEqual(api_source.count('"fulfillment_warnings"'), 2)
		self.assertGreaterEqual(
			api_source.count('len(fulfillment.get("warnings") or [])')
			+ api_source.count('len((fulfillment or {}).get("warnings") or [])'),
			2,
		)
		availability_source = (APP_ROOT / "services/availability.py").read_text(encoding="utf-8")
		self.assertGreaterEqual(availability_source.count('"warning_count": len(warnings)'), 2)
		for page in ("aps_schedule_gantt", "aps_release_center"):
			source = (
				APP_ROOT / "injection_aps/page" / page / f"{page}.js"
			).read_text(encoding="utf-8")
			with self.subTest(page=page):
				self.assertIn("fulfillment_warning_count", source)
				self.assertIn("fulfillment_warnings", source)
				self.assertIn("render_warnings", source)
		planning_run_source = (APP_ROOT / "public/js/aps_planning_run.js").read_text(
			encoding="utf-8"
		)
		self.assertIn("response.fulfillment", planning_run_source)
		self.assertIn('"Execution Sync Warnings"', planning_run_source)

	def test_shared_detail_drawer_supports_keyboard_and_focus_recovery(self):
		source = (APP_ROOT / "public/js/injection_aps_shared.js").read_text(
			encoding="utf-8"
		)
		for marker in (
			'role="dialog"',
			'aria-modal="true"',
			'drawer.setAttribute("aria-hidden", "false")',
			'event.key === "Escape" && drawer.classList.contains("open")',
			'drawer.iaReturnFocus = document.activeElement',
			'returnFocus.focus()',
		):
			with self.subTest(marker=marker):
				self.assertIn(marker, source)

	def test_contextless_aps_translations_do_not_conflict_with_installed_apps(self):
		translation_file = APP_ROOT / "translations/zh.csv"
		aps_rows = _read_translation_rows(translation_file)
		contextless = [row for row in aps_rows if len(row) < 3 or not row[2]]
		self.assertFalse(CONFLICT_KEYS & {row[0] for row in contextless})

		frappe_spec = importlib.util.find_spec("frappe")
		if not frappe_spec or not frappe_spec.origin:
			self.skipTest("Installed app translations are unavailable")
		apps_root = Path(frappe_spec.origin).resolve().parents[2]
		other_translations = {}
		for path in apps_root.glob("*/*/translations/zh.csv"):
			if path.parents[2].name == "injection_aps":
				continue
			for row in _read_translation_rows(path):
				if len(row) < 3 or not row[2]:
					other_translations.setdefault(row[0], set()).add(row[1])
		conflicts = {
			row[0]
			for row in contextless
			if row[0] in other_translations
			and any(value != row[1] for value in other_translations[row[0]])
		}
		self.assertEqual(conflicts, set())

	def test_current_sync_and_proposal_guard_messages_have_chinese_translations(self):
		rows = _read_translation_rows(APP_ROOT / "translations/zh.csv")
		by_source = {row[0]: row[1] for row in rows}
		for source in (
			"Customer schedule rows are no longer Active or do not belong to this scope: {0}.",
			"Net delivered quantity for APS schedule item {0} exceeds its active quantity by {1}.",
			"Direct APS delivery source row {0} has no replacement target; only a fully returned zero-net chain may be settled without one.",
			"Direct return source row {0} has no replacement target for its APS delivery lineage.",
			"Return source row {0} has no replacement target for its original APS delivery lineage.",
			"APS segment {0} has multiple execution items; select APS Scheduling Item explicitly.",
			"Linked Work Order Scheduling is not in Manufacture status.",
			"Linked Work Order Scheduling does not have valid APS approval.",
			"APS delivery lineage scope could not be resolved; the schedule replacement was not applied.",
			"APS result {0} or its machine schedule changed after proposal review. Regenerate the proposal batch.",
			"APS result {0} covers only part of schedule item {1}, which already has delivery or allocation history. The historical coverage offset is not persisted, so fulfillment quantities for this result cannot be attributed precisely.",
		):
			with self.subTest(source=source):
				self.assertTrue(by_source.get(source))
		self.assertIn(
			["Result State Token", "排程结果状态令牌", "APS Work Order Proposal Item"],
			rows,
		)


if __name__ == "__main__":
	unittest.main()
