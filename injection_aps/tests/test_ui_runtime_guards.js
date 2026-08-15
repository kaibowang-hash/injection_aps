"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_ROOT = path.resolve(__dirname, "..");

function loadPage(relativePath, pageName, className) {
	const context = {
		$: () => ({
			find() {
				return {
					each() {},
				};
			},
		}),
		console,
		document: {},
		injection_aps: {
			ui: {
				ensure_styles() {},
				escape(value) {
					return String(value == null ? "" : value);
				},
				set_feedback() {},
				translate(value) {
					return String(value == null ? "" : value);
				},
			},
		},
		__: (value) => String(value),
		frappe: {
			pages: { [pageName]: {} },
			require(_asset, callback) {
				if (callback) {
					callback();
				}
				return Promise.resolve();
			},
			ui: {},
		},
	};
	vm.createContext(context);
	const source = fs.readFileSync(path.join(APP_ROOT, relativePath), "utf8");
	vm.runInContext(`${source}\nglobalThis.__TestController = ${className};`, context, {
		filename: relativePath,
	});
	return { Controller: context.__TestController, context };
}

async function testGanttRiskValuesTranslateEachEnum() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js",
		"aps-schedule-gantt",
		"InjectionAPSScheduleGantt"
	);
	const controller = Object.create(Controller.prototype);
	const translations = {
		"Late Delivery": "交期延误",
		"Unscheduled Quantity": "未排数量",
		"Copy Mold Parallelized": "复制模并行",
	};
	context.injection_aps.ui.translate = (value) => translations[value] || String(value || "");

	assert.deepEqual(
		Array.from(controller.getRiskValues(["Late Delivery\nCopy Mold Parallelized", "Late Delivery", ""])),
		["Late Delivery", "Copy Mold Parallelized"]
	);
	assert.deepEqual(
		Array.from(controller.translateRiskValues(["Late Delivery", "Unscheduled Quantity"])),
		["交期延误", "未排数量"]
	);
	assert.equal(
		controller.translateRiskText("Late Delivery\nCopy Mold Parallelized"),
		"交期延误 / 复制模并行"
	);
	context.injection_aps.ui.translate = (value) => {
		const messages = {
			"There are still {0} APS exceptions waiting for review.": "当前仍有 {0} 条 APS 异常待处理。",
			"There is still unscheduled quantity: {0}.": "仍有未排数量：{0}。",
			"Plan consistency: {0}": "计划一致性：{0}",
			Invalid: "无效",
		};
		return messages[value] || String(value || "");
	};
	assert.equal(
		controller.translateRiskMessage("There are still 3 APS exceptions waiting for review."),
		"当前仍有 3 条 APS 异常待处理。"
	);
	assert.equal(
		controller.translateRiskMessage("There is still unscheduled quantity: 125.5."),
		"仍有未排数量：125.5。"
	);
	assert.equal(controller.translateRiskMessage("Plan consistency: Invalid"), "计划一致性：无效");
}

async function testRunConsoleRendersSevenStackedColumnsAndKeepsFullExport() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_run_console/aps_run_console.js",
		"aps-run-console",
		"InjectionAPSRunConsole"
	);
	const controller = Object.create(Controller.prototype);
	controller.table = {};
	let rendered;
	Object.assign(context.injection_aps.ui, {
		can_run_action() {
			return true;
		},
		format_date(value) {
			return String(value || "");
		},
		get_action_label(action) {
			return action.label || "";
		},
		get_existing_work_order_policy_label(value) {
			return value === "Include" ? "考虑现有工单" : "不考虑现有工单";
		},
		get_value(value, pathValue, fallback) {
			return String(pathValue || "")
				.split(".")
				.reduce((current, key) => (current && current[key] !== undefined ? current[key] : fallback), value);
		},
		pill(label, tone) {
			return `<span class="${tone}">${label}</span>`;
		},
		render_table(_target, columns, rows, formatter, options) {
			rendered = {
				columns,
				cells: columns.map((column) => formatter(column, rows[0][column.fieldname], rows[0])),
				options,
			};
		},
		route_link(label) {
			return `<a>${label}</a>`;
		},
	});
	context.frappe.format = (value) => String(value);
	controller.renderRuns([
		{
			name: "APS-RUN-00008",
			planning_date: "2026-08-12",
			selected_plant_floor_summary: "TH - Injection 1 / TH - Injection 2",
			existing_work_order_policy: "Exclude",
			status: "Planned",
			approval_state: "Pending",
			consistency_status: "Valid",
			total_net_requirement_qty: 100,
			total_machine_scheduled_qty: 90,
			total_demand_covered_qty: 90,
			total_unscheduled_qty: 10,
			total_overproduction_qty: 0,
			total_produced_qty: 2,
			total_delivered_qty: 1,
			exception_count: 3,
			execution_health: { running: 1, delayed: 2, no_recent_update: 3 },
			next_actions: { next_step: "Confirm Run", actions: [] },
		},
	]);

	assert.equal(rendered.columns.length, 7);
	assert.deepEqual(
		Array.from(rendered.columns, (column) => column.fieldname),
		["run_identity", "scope_policy", "state_summary", "schedule_summary", "fulfillment_summary", "risk_execution", "next_actions"]
	);
	assert.match(rendered.cells[0], /APS-RUN-00008/);
	assert.match(rendered.cells[0], /2026-08-12/);
	assert.match(rendered.cells[3], /100/);
	assert.match(rendered.cells[4], /10/);
	assert.match(rendered.cells[5], /3/);
	assert.equal(rendered.options.export_columns.length, 17);
	assert.ok(rendered.options.export_columns.some((column) => column.fieldname === "total_delivered_qty"));
	assert.equal(
		rendered.options.export_formatter(
			{ fieldname: "selected_plant_floor_summary" },
			"",
			{ plant_floor: "TH - Injection 1" }
		),
		"TH - Injection 1"
	);
}

function makeInspectionDialog(values) {
	const wrapper = { html() {} };
	return {
		values: Object.assign({}, values),
		iaApplyingInspection: false,
		iaInspectionGeneration: 0,
		iaInspectionResponse: null,
		iaInspectionSourceSignature: "",
		iaSourceNeedsAutoDetection: false,
		get_value(fieldname) {
			return this.values[fieldname];
		},
		async set_value(fieldname, value) {
			this.values[fieldname] = value;
		},
		set_df_property() {},
		get_field() {
			return { $wrapper: wrapper };
		},
	};
}

async function testSheetChangeReplacesOldMapping() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_schedule_console/aps_schedule_console.js",
		"aps-schedule-console",
		"InjectionAPSScheduleConsole"
	);
	const controller = Object.create(Controller.prototype);
	controller.syncImportDialogLayout = () => {};
	const dialog = makeInspectionDialog({
		file_url: "/private/files/schedule.xlsx",
		sheet_name: "Sheet B",
		header_row_no: 2,
		parser_mode: "matrix",
		item_reference_column: "A · Old Item",
		description_column: "C · Old Description",
	});
	let request;
	context.frappe.xcall = async (_method, payload) => {
		request = payload;
		return {
			selected_sheet: "Sheet B",
			sheet_names: ["Sheet A", "Sheet B"],
			column_options: [{ value: "B", label: "B · New Item" }],
			detected_mapping: {
				parser_mode: "matrix",
				header_row_no: 5,
				data_start_row_no: 6,
				item_reference_column: "B",
				date_columns_mode: "auto",
			},
			sample_rows: [],
		};
	};

	await controller.inspectImportSource(dialog, { forceSheet: 1 });

	assert.equal(request.sheet_name, "Sheet B");
	assert.equal(request.header_row_no, undefined);
	assert.equal(dialog.values.header_row_no, 5);
	assert.equal(dialog.values.item_reference_column, "B · New Item");
	assert.equal(dialog.values.description_column, "");
	assert.equal(
		dialog.iaInspectionSourceSignature,
		controller.getImportInspectionSourceSignature(dialog)
	);
}

async function testHeaderChangePreservesHeaderAndReplacesOldMapping() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_schedule_console/aps_schedule_console.js",
		"aps-schedule-console",
		"InjectionAPSScheduleConsole"
	);
	const controller = Object.create(Controller.prototype);
	controller.syncImportDialogLayout = () => {};
	const dialog = makeInspectionDialog({
		file_url: "/private/files/schedule.xlsx",
		sheet_name: "Sheet B",
		header_row_no: 9,
		parser_mode: "matrix",
		item_reference_column: "A · Old Item",
	});
	let request;
	context.frappe.xcall = async (_method, payload) => {
		request = payload;
		return {
			selected_sheet: "Sheet B",
			sheet_names: ["Sheet B"],
			column_options: [{ value: "D", label: "D · Item" }],
			detected_mapping: {
				parser_mode: "matrix",
				header_row_no: 9,
				data_start_row_no: 10,
				item_reference_column: "D",
				date_columns_mode: "range",
			},
			sample_rows: [],
		};
	};

	await controller.inspectImportSource(dialog, { forceHeader: 1 });

	assert.equal(request.sheet_name, "Sheet B");
	assert.equal(request.header_row_no, "9");
	assert.equal(dialog.values.header_row_no, 9);
	assert.equal(dialog.values.item_reference_column, "D · Item");
	assert.equal(dialog.values.date_columns_mode, "range");

	dialog.values.header_row_no = 10;
	let refreshed = 0;
	controller.refreshImportRecognition = async (_dialog, options) => {
		refreshed += 1;
		assert.equal(options.forceHeader, 1);
		return { detected_mapping: { header_row_no: 10 } };
	};
	await controller.ensureImportRecognitionCurrent(dialog);
	assert.equal(refreshed, 1);
}

async function testSourceChangeDuringMappingApplyRejectsResponse() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_schedule_console/aps_schedule_console.js",
		"aps-schedule-console",
		"InjectionAPSScheduleConsole"
	);
	const controller = Object.create(Controller.prototype);
	controller.syncImportDialogLayout = () => {};
	const dialog = makeInspectionDialog({
		file_url: "/private/files/schedule.xlsx",
		sheet_name: "Sheet A",
		header_row_no: 2,
		parser_mode: "matrix",
	});
	const originalSetValue = dialog.set_value.bind(dialog);
	dialog.set_value = async (fieldname, value) => {
		await originalSetValue(fieldname, value);
		if (fieldname === "item_reference_column") {
			dialog.values.sheet_name = "Sheet B";
		}
	};
	context.frappe.xcall = async () => ({
		selected_sheet: "Sheet A",
		sheet_names: ["Sheet A", "Sheet B"],
		column_options: [{ value: "A", label: "A · Item" }],
		detected_mapping: {
			parser_mode: "matrix",
			header_row_no: 2,
			data_start_row_no: 3,
			item_reference_column: "A",
			date_columns_mode: "auto",
		},
		sample_rows: [],
	});

	const response = await controller.inspectImportSource(dialog, { forceHeader: 1 });

	assert.equal(response, null);
	assert.equal(dialog.iaInspectionResponse, null);
	assert.equal(dialog.iaInspectionSourceSignature, "");
}

async function testCustomerProgressIgnoresOlderResponse() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
		"aps-customer-schedule-progress",
		"InjectionAPSCustomerScheduleProgress"
	);
	const controller = Object.create(Controller.prototype);
	controller.refreshGeneration = 0;
	controller.feedback = {};
	controller.rows = [];
	let filterGeneration = 0;
	controller.getFilters = () => ({ request: ++filterGeneration });
	const rendered = [];
	controller.renderRunStatus = () => {};
	controller.renderSummary = (summary) => rendered.push(summary.marker);
	controller.renderTable = () => {};
	const pending = [];
	context.frappe.xcall = (_method, filters) =>
		new Promise((resolve) => pending.push({ filters, resolve }));

	const olderRefresh = controller.refresh();
	const newerRefresh = controller.refresh();
	assert.equal(pending.length, 2);
	assert.deepEqual(pending.map((entry) => entry.filters.request), [1, 2]);

	pending[1].resolve({ rows: [{ name: "new" }], summary: { marker: "new" } });
	await newerRefresh;
	pending[0].resolve({ rows: [{ name: "old" }], summary: { marker: "old" } });
	await olderRefresh;

	assert.deepEqual(rendered, ["new"]);
	assert.equal(controller.rows[0].name, "new");
}

async function testCustomerProgressV2DispatchesDetailAndMatrixWithoutChangingLegacyCall() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
		"aps-customer-schedule-progress",
		"InjectionAPSCustomerScheduleProgress"
	);
	const controller = Object.create(Controller.prototype);
	controller.refreshGeneration = 0;
	controller.feedback = {};
	controller.rows = [];
	controller.offset = 100;
	controller.columnOffset = 14;
	controller.pageLength = 100;
	controller.getFilters = () => ({ company: "COMPANY-1" });
	controller.progressView = "Detail";
	const rendered = [];
	controller.renderProjectionStatus = () => rendered.push("projection");
	controller.renderV2Summary = () => rendered.push("summary");
	controller.renderV2Table = () => rendered.push("detail");
	controller.renderMatrix = () => rendered.push("matrix");
	controller.renderRunStatus = () => rendered.push("legacy-status");
	controller.renderSummary = () => rendered.push("legacy-summary");
	controller.renderTable = () => rendered.push("legacy-table");
	let calls = [];
	context.frappe.xcall = async (method, args) => {
		calls.push({ method, args });
		return {
			mode: "V2",
			projection: { type: args.progress_view === "Date Matrix" ? "Effective Cross-Run" : "Single Run" },
			rows: [{ demand_identity: "IDENTITY-1" }],
			summary: {},
			matrix: { dates: ["2026-08-20"] },
		};
	};

	await controller.refresh();
	assert.deepEqual(rendered, ["projection", "summary", "detail"]);
	assert.equal(calls[0].method, "injection_aps.api.app.get_customer_schedule_progress_data");
	assert.equal(calls[0].args.progress_view, "Detail");
	assert.equal(calls[0].args.offset, 100);
	assert.equal(calls[0].args.page_length, 100);

	controller.progressView = "Date Matrix";
	rendered.length = 0;
	await controller.refresh();
	assert.deepEqual(rendered, ["projection", "summary", "matrix"]);
	assert.equal(calls[1].method, "injection_aps.api.app.get_customer_schedule_progress_data");
	assert.equal(calls[1].args.progress_view, "Date Matrix");
	assert.equal(calls[1].args.column_offset, 14);
	assert.equal(calls[1].args.column_limit, 14);
}

async function testProgressToolbarUsesSharedIconControls() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
		"aps-customer-schedule-progress",
		"InjectionAPSCustomerScheduleProgress"
	);
	const controller = Object.create(Controller.prototype);
	controller.data = { pagination: { has_more: true }, matrix: {} };
	controller.offset = 100;
	controller.rows = [{}, {}];
	context.injection_aps.ui.icon_button = (iconName, title, attrs) => `<button class="ia-icon-btn" data-icon="${iconName}" title="${title}" ${attrs.disabled ? "disabled" : ""}></button>`;
	const html = controller.renderProgressToolbar(false);

	assert.match(html, /data-icon="chevron-left"/);
	assert.match(html, /data-icon="chevron-right"/);
	assert.match(html, /ia-progress-view-switch/);
	assert.doesNotMatch(html, />Previous Rows</);
	assert.doesNotMatch(html, />Next Rows</);
	assert.doesNotMatch(html, />Export Excel</);
}

async function testProgressMatrixCellShowsOperationalSummaryAndExactDrilldownKey() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
		"aps-customer-schedule-progress",
		"InjectionAPSCustomerScheduleProgress"
	);
	const controller = Object.create(Controller.prototype);
	context.injection_aps.ui.format_number = (value) => String(value);
	const html = controller.renderMatrixCell(
		{ demand_identity: "IDENTITY-1", schedule_item: "SCHEDULE-ROW-1" },
		"2026-08-20",
		{
			schedule_qty: 100,
			original_plan_qty: 90,
			current_plan_qty: 80,
			forecast_qty: 70,
			actual_good_qty: 30,
			actual_scrap_qty: 2,
			delivery_plan_qty: 60,
			delivered_qty: 20,
			stock_covered_qty: 10,
			shortage_qty: 10,
			recovery_qty: 10,
		}
	);
	for (const value of [80, 30, 20, 10]) {
		assert.match(html, new RegExp(` ${value}</span>`));
	}
	for (const hiddenLabel of ["Original Plan", "Forecast", "Scrap", "Delivery Plan", "Stock Covered", "Recovery"]) {
		assert.doesNotMatch(html, new RegExp(`>${hiddenLabel}<`));
	}
	assert.match(html, /data-progress-identity="IDENTITY-1"/);
	assert.match(html, /data-progress-schedule-item="SCHEDULE-ROW-1"/);
	assert.match(html, /data-progress-date="2026-08-20"/);
}

async function testUiLoaderReloadsSharedAssetsByVersionAndDeduplicatesRequests() {
	const source = fs.readFileSync(path.join(APP_ROOT, "public/js/injection_aps_ui_loader.js"), "utf8");
	const scripts = [];
	const context = {
		console,
		document: {
			createElement() {
				const listeners = {};
				return {
					addEventListener(name, callback) {
						listeners[name] = callback;
					},
					dataset: {},
					listeners,
				};
			},
			head: {
				appendChild(script) {
					scripts.push(script);
				},
			},
		},
		frappe: {
			msgprint() {},
			provide() {
				context.injection_aps = context.injection_aps || {};
				context.injection_aps.ui_loader = context.injection_aps.ui_loader || {};
			},
		},
		injection_aps: {},
		__: (value) => String(value),
	};
	vm.createContext(context);
	vm.runInContext(source, context, { filename: "injection_aps_ui_loader.js" });

	const first = context.injection_aps.ui_loader.load("20260815.2");
	const duplicate = context.injection_aps.ui_loader.load("20260815.2");
	assert.equal(first, duplicate);
	assert.equal(scripts.length, 1);
	assert.match(scripts[0].src, /injection_aps_shared\.js\?v=20260815\.2$/);

	let stylesEnsured = 0;
	context.injection_aps.ui = {
		__asset_version: "20260815.2",
		ensure_styles() {
			stylesEnsured += 1;
		},
	};
	scripts[0].listeners.load();
	await first;
	await context.injection_aps.ui_loader.load("20260815.2");
	assert.equal(scripts.length, 1);
	assert.equal(stylesEnsured, 2);
}

async function testGanttMachineViewCollapsesCampaignAndRendersFourPlanLayers() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js",
		"aps-schedule-gantt",
		"InjectionAPSScheduleGantt"
	);
	const controller = Object.create(Controller.prototype);
	let mode = "Machine";
	controller.viewField = { get_value: () => mode };
	context.injection_aps.ui.get_value = (value, pathValue, fallback) => String(pathValue || "")
		.split(".")
		.reduce((current, key) => (current && current[key] !== undefined ? current[key] : fallback), value);
	const tasks = [
		{ id: "OWNER", details: { production_campaign: "CAM-1", is_campaign_owner: 1 } },
		{ id: "OUTPUT", details: { production_campaign: "CAM-1", is_campaign_derived: 1 } },
	];
	assert.deepEqual(Array.from(controller.getFilteredTasks(tasks), (row) => row.id), ["OWNER"]);
	mode = "Mold";
	assert.deepEqual(Array.from(controller.getFilteredTasks(tasks), (row) => row.id), ["OWNER", "OUTPUT"]);

	context.frappe.datetime = {
		str_to_obj: (value) => vm.runInContext(`new Date(${JSON.stringify(value.replace(" ", "T") + "Z")})`, context),
	};
	context.injection_aps.ui.format_datetime = (value) => String(value || "");
	const task = {
		details: {
			original_start_time: "2026-08-20 08:00:00", original_end_time: "2026-08-20 09:00:00",
			current_start_time: "2026-08-20 09:00:00", current_end_time: "2026-08-20 10:00:00",
			forecast_start_time: "2026-08-20 10:00:00", forecast_end_time: "2026-08-20 11:00:00",
			actual_start_time: "2026-08-20 09:00:00", actual_end_time: "2026-08-20 09:30:00",
		},
	};
	const start = new Date("2026-08-20T00:00:00Z").getTime();
	const end = new Date("2026-08-21T00:00:00Z").getTime();
	for (const layer of ["original", "forecast", "actual"]) {
		const html = controller.renderTaskLayer(task, layer, 4, start, end, end - start);
		assert.match(html, new RegExp(`ia-gantt-plan-layer ${layer}`));
	}
	assert.equal(task.details.current_start_time, "2026-08-20 09:00:00");
}

async function main() {
	const tests = [
		testGanttRiskValuesTranslateEachEnum,
		testRunConsoleRendersSevenStackedColumnsAndKeepsFullExport,
		testSheetChangeReplacesOldMapping,
		testHeaderChangePreservesHeaderAndReplacesOldMapping,
		testSourceChangeDuringMappingApplyRejectsResponse,
		testCustomerProgressIgnoresOlderResponse,
		testCustomerProgressV2DispatchesDetailAndMatrixWithoutChangingLegacyCall,
		testProgressToolbarUsesSharedIconControls,
		testProgressMatrixCellShowsOperationalSummaryAndExactDrilldownKey,
		testUiLoaderReloadsSharedAssetsByVersionAndDeduplicatesRequests,
		testGanttMachineViewCollapsesCampaignAndRendersFourPlanLayers,
	];
	for (const test of tests) {
		await test();
	}
	process.stdout.write(`${tests.length} UI runtime guard tests passed.\n`);
}

main().catch((error) => {
	console.error(error);
	process.exitCode = 1;
});
