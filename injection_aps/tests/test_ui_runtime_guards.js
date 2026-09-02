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
				format_number(value) {
					return Number(value || 0).toLocaleString("en-US");
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

async function testRunConsoleRendersFourDecisionColumnsAndKeepsFullExport() {
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
		format_number(value) {
			return new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(Number(value || 0));
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
	context.frappe.format = (value) => `<div style='text-align: right'>${value}</div>`;
	const row = {
			name: "APS-RUN-00008",
			company: "Jichen (Thailand) Co., Ltd",
			planning_date: "2026-08-12",
			selected_plant_floor_summary: "TH - Injection 1 / TH - Injection 2",
			existing_work_order_policy: "Exclude",
			status: "Planned",
			approval_state: "Pending",
			consistency_status: "Valid",
			total_net_requirement_qty: 394684,
			total_machine_scheduled_qty: 478962,
			total_demand_covered_qty: 390534,
			total_unscheduled_qty: 88428,
			total_overproduction_qty: 0,
			total_produced_qty: 127628,
			total_delivered_qty: 0,
			exception_count: 3,
			execution_health: { running: 1, delayed: 2, no_recent_update: 3 },
			v2_admission_available: 1,
			next_actions: {
				next_step: "Analyze and Apply Capacity",
				blocking_reason: "Apply the analyzed capacity plan before confirming this run.",
				actions: [
					{ action_key: "run_trial", enabled: 1, label: "Recalculate" },
					{ action_key: "approve", enabled: 0, label: "Confirm Run" },
				],
			},
		};
	controller.renderRuns([row]);

	assert.equal(rendered.columns.length, 3);
	assert.deepEqual(
		Array.from(rendered.columns, (column) => column.fieldname),
		["run_overview", "key_results", "next_action"]
	);
	assert.match(rendered.cells[0], /APS-RUN-00008/);
	assert.match(rendered.cells[0], /2026-08-12/);
	assert.match(rendered.cells[1], /394,684/);
	assert.match(rendered.cells[1], /478,962/);
	assert.match(rendered.cells[1], /88,428/);
	assert.match(rendered.cells[1], />3</);
	assert.match(rendered.cells[2], /Open Run/);
	assert.match(rendered.cells[2], /View Details/);
	assert.doesNotMatch(rendered.cells[2], /Recalculate|open_gantt|open_release|open_admission|Confirm Run|disabled/);
	assert.doesNotMatch(rendered.cells[1], /text-align|&lt;div/i);

	const drawerHtml = controller.renderRunDrawer(row);
	assert.match(drawerHtml, /ia-page ia-drawer-stack ia-run-drawer/);
	assert.match(drawerHtml, /ia-status-line/);
	assert.match(drawerHtml, /ia-run-drawer-metrics/);
	assert.doesNotMatch(drawerHtml, /ia-card-grid/);
	assert.match(drawerHtml, /390,534/);
	assert.match(drawerHtml, /127,628/);
	assert.match(drawerHtml, /Apply the analyzed capacity plan/);
	assert.match(drawerHtml, /Recalculate/);
	assert.match(drawerHtml, /data-run-action="open_gantt"/);
	assert.match(drawerHtml, /data-run-action="open_release"/);
	assert.match(drawerHtml, /data-run-action="open_admission"/);
	assert.match(drawerHtml, /Confirm Run/);
	assert.match(drawerHtml, /disabled aria-disabled="true"/);
	assert.match(drawerHtml, /Apply the analyzed capacity plan before confirming this run/);
	let openedDrawer;
	context.injection_aps.ui.open_drawer = (title, subtitle, html) => {
		openedDrawer = { title, subtitle, html };
	};
	context.injection_aps.ui.ensure_drawer = () => ({});
	controller.bindRunActionHandlers = () => {};
	controller.openRunDetails(row);
	assert.equal(openedDrawer.title, "APS Run Details");
	assert.match(openedDrawer.subtitle, /APS-RUN-00008/);
	assert.match(openedDrawer.html, /Planning and Fulfillment/);
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

async function testRunConsoleStylesDoNotBlockPageInitialization() {
	const { context } = loadPage(
		"injection_aps/page/aps_run_console/aps_run_console.js",
		"aps-run-console",
		"InjectionAPSRunConsole"
	);
	const styles = new Map();
	const appended = [];
	context.document = {
		getElementById(id) {
			return styles.get(id) || null;
		},
		createElement(tagName) {
			const attributes = {};
			return {
				tagName,
				getAttribute(name) {
					return attributes[name] || null;
				},
				setAttribute(name, value) {
					attributes[name] = value;
				},
			};
		},
		head: {
			appendChild(style) {
				appended.push(style);
				styles.set(style.id, style);
			},
		},
	};
	const requiredAssets = [];
	context.frappe.require = (asset, callback) => {
		requiredAssets.push(asset);
		callback();
		return Promise.resolve();
	};
	let started = 0;
	context.injection_aps.ui_loader = {
		start(version, callback) {
			assert.equal(version, "20260902.1");
			started += 1;
			callback();
		},
	};
	let refreshed = 0;
	const wrapper = {
		injection_aps_controller: {
			refresh() {
				refreshed += 1;
			},
		},
	};

	context.frappe.pages["aps-run-console"].on_page_load(wrapper);
	context.frappe.pages["aps-run-console"].on_page_load(wrapper);

	assert.deepEqual(requiredAssets, []);
	assert.equal(started, 2);
	assert.equal(refreshed, 2);
	assert.equal(appended.length, 1);
	assert.equal(appended[0].rel, "stylesheet");
	assert.equal(
		appended[0].getAttribute("href"),
		"/assets/injection_aps/css/aps_run_console.css?v=20260902.1"
	);
}

async function testRunConsoleResyncsRouteWhenSpaReusesThePage() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_run_console/aps_run_console.js",
		"aps-run-console",
		"InjectionAPSRunConsole"
	);
	const controller = Object.create(Controller.prototype);
	Object.assign(controller, {
		targetRun: "RUN-A",
		fromAdmission: "1",
		targetRunOpened: true,
		loadingKey: "old",
		refreshGeneration: 4,
	});
	const route = { run_name: "RUN-B", from_admission: "" };
	context.frappe.utils = { get_url_arg: (key) => route[key] || "" };

	assert.equal(controller.syncRouteState(), true);
	assert.equal(controller.targetRun, "RUN-B");
	assert.equal(controller.fromAdmission, "");
	assert.equal(controller.targetRunOpened, false);
	assert.equal(controller.loadingKey, "");
	assert.equal(controller.refreshGeneration, 5);
	assert.equal(controller.syncRouteState(), false);
}

async function testAdmissionBatchPolicyHandlesOneHundredRowsWithoutPerRowCalls() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_demand_admission_workbench/aps_demand_admission_workbench.js",
		"aps-demand-admission-workbench",
		"InjectionAPSDemandAdmissionWorkbench"
	);
	const controller = Object.create(Controller.prototype);
	const rows = [
		{
			name: "P0-1",
			admission_class: "P0",
			candidate_qty: 10,
			recommended_qty: 10,
			selected_qty: 10,
			customer: "CUST-A",
			item_code: "ITEM-P0",
			customer_code: "CP-P0",
			item_name: "Mandatory item",
		},
	];
	for (let index = 1; index <= 60; index += 1) {
		rows.push({
			name: `P1-${index}`,
			admission_class: "P1",
			candidate_qty: 20,
			recommended_qty: 12,
			selected_qty: 0,
			customer: index <= 30 ? "CUST-A" : "CUST-B",
			item_code: `ITEM-P1-${index}`,
			customer_code: `CP-P1-${index}`,
			item_name: `Framework item ${index}`,
		});
	}
	for (let index = 1; index <= 39; index += 1) {
		rows.push({
			name: `P2-${index}`,
			admission_class: "P2",
			candidate_qty: 8,
			recommended_qty: 5,
			selected_qty: 0,
			customer: "CUST-C",
			item_code: `ITEM-P2-${index}`,
			customer_code: `CP-P2-${index}`,
			item_name: `Safety item ${index}`,
		});
	}
	controller.data = {
		rows,
		admission_state: { confirmed: 0, optional_row_count: 99 },
	};
	controller.decisionMap = new Map();
	controller.savedDecisionMap = new Map();
	controller.selectedRows = new Set();
	controller.filters = { admission_class: "ALL", search_text: "" };
	controller.pageNumber = 1;
	controller.pageLength = 100;
	controller.render = () => {};
	let serverCalls = 0;
	context.frappe.xcall = async () => {
		serverCalls += 1;
	};

	controller.initializeDecisions(rows);
	assert.equal(controller.decisionMap.get("P0-1"), 10);
	assert.equal(controller.decisionMap.get("P1-1"), 12);
	assert.equal(controller.decisionMap.get("P2-1"), 0);
	assert.equal(controller.collectDecisions().length, 100);
	assert.equal(controller.getDecisionSummary().p1, 720);
	assert.equal(controller.getDecisionSummary().p2, 0);
	assert.equal(serverCalls, 0);

	controller.filters = { admission_class: "P1", search_text: "CUST-A" };
	const filtered = controller.getFilteredRows();
	assert.equal(filtered.length, 30);
	filtered.forEach((row) => controller.selectedRows.add(row.name));
	controller.applyBulkAction("exclude");
	assert.equal(controller.decisionMap.get("P1-1"), 0);
	assert.equal(controller.decisionMap.get("P1-31"), 12);
	assert.equal(controller.decisionMap.get("P0-1"), 10);

	controller.applyStrategy("all_recommended");
	assert.equal(controller.decisionMap.get("P0-1"), 10);
	assert.equal(controller.decisionMap.get("P1-60"), 12);
	assert.equal(controller.decisionMap.get("P2-39"), 5);
	assert.equal(serverCalls, 0);
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
	controller.table = { innerHTML: "" };
	controller.projectionBanner = { innerHTML: "" };
	controller.statusHost = { innerHTML: "" };
	controller.summary = { innerHTML: "" };
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
	controller.table = { innerHTML: "" };
	controller.projectionBanner = { innerHTML: "" };
	controller.statusHost = { innerHTML: "" };
	controller.summary = { innerHTML: "" };
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

async function testCustomerProgressResyncsAndClearsRouteRunOnPageReuse() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
		"aps-customer-schedule-progress",
		"InjectionAPSCustomerScheduleProgress"
	);
	const controller = Object.create(Controller.prototype);
	let fieldValue = "RUN-A";
	Object.assign(controller, {
		runField: {
			get_value: () => fieldValue,
			set_value: (value) => { fieldValue = value; },
		},
		suppressFilterRefresh: false,
		offset: 25,
		columnOffset: 14,
		loadingKey: "old",
		refreshGeneration: 2,
	});
	context.injection_aps.ui.get_query_param = () => "";

	assert.equal(controller.syncRouteState(), true);
	assert.equal(fieldValue, "");
	assert.equal(controller.offset, 0);
	assert.equal(controller.columnOffset, 0);
	assert.equal(controller.loadingKey, "");
	assert.equal(controller.refreshGeneration, 3);
	assert.equal(controller.syncRouteState(), false);
}

async function testCustomerProgressExplainsMissingFormalProjection() {
	const { Controller } = loadPage(
		"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
		"aps-customer-schedule-progress",
		"InjectionAPSCustomerScheduleProgress"
	);
	const controller = Object.create(Controller.prototype);
	controller.projectionBanner = { innerHTML: "" };
	controller.statusHost = { innerHTML: "stale" };
	controller.renderProjectionStatus(
		{
			available: 0,
			single_run_view: 0,
			label: "No Formal APS Projection",
			reason: "Trial Run stock and plan quantities are intentionally excluded.",
			run_names: [],
		},
		{ unprojected_open_qty: 72643 }
	);

	assert.match(controller.projectionBanner.innerHTML, /No Formal APS Projection/);
	assert.match(controller.projectionBanner.innerHTML, /Select a Trial Run/);
	assert.match(controller.projectionBanner.innerHTML, /72,643/);
	assert.equal(controller.statusHost.innerHTML, "");
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

async function testProgressMatrixCellSeparatesFourOperationalLayersAndUsesGroupedRowKey() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_customer_schedule_progress/aps_customer_schedule_progress.js",
		"aps-customer-schedule-progress",
		"InjectionAPSCustomerScheduleProgress"
	);
	const controller = Object.create(Controller.prototype);
	context.injection_aps.ui.format_number = (value) => String(value);
	const row = {
		_row_no: 7,
		status_tone: "yellow",
		events: [{ date: "2026-08-20", layer: "current_plan_qty", reason: "Plan moved" }],
	};
	const cell = {
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
	};
	const htmlByLayer = Object.fromEntries(controller.getMatrixLayers().map((layer) => [
		layer.key,
		controller.renderMatrixLayerCell(row, "2026-08-20", cell, layer),
	]));
	assert.match(htmlByLayer.schedule, />Schedule Qty<.*>100</s);
	assert.match(htmlByLayer.plan, />Current Plan<.*>80</s);
	assert.match(htmlByLayer.actual, />Actual Good<.*>30</s);
	assert.match(htmlByLayer.delivery, />Delivered<.*>20</s);
	assert.match(htmlByLayer.plan, /ia-progress-cell-alert/);
	assert.doesNotMatch(htmlByLayer.actual, />Delivered</);
	for (const [layer, html] of Object.entries(htmlByLayer)) {
		assert.match(html, /data-progress-row="7"/);
		assert.match(html, new RegExp(`data-progress-layer="${layer}"`));
		assert.match(html, /data-progress-date="2026-08-20"/);
	}
}

async function testUiLoaderReloadsSharedAssetsByVersionAndDeduplicatesRequests() {
	const source = fs.readFileSync(path.join(APP_ROOT, "public/js/injection_aps_ui_loader.js"), "utf8");
	const scripts = [];
	const context = {
		console,
		setTimeout,
		clearTimeout,
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

async function testSharedItemIdentityUsesThreeUnlabelledLines() {
	const source = fs.readFileSync(path.join(APP_ROOT, "public/js/injection_aps_shared.js"), "utf8");
	let detailRequests = 0;
	const context = {
		console,
		document: {},
		window: {},
		injection_aps: { ui: {} },
		__: (value) => String(value),
		frappe: {
			boot: {},
			session: {},
			provide() {},
			async xcall(method, args) {
				detailRequests += 1;
				assert.equal(method, "frappe.client.get_list");
				assert.deepEqual(Array.from(args.filters.name[1]), ["ITEM-CACHED"]);
				return [{ name: "ITEM-CACHED", customer_code: "CACHED-CODE", item_name: "Cached Name" }];
			},
			utils: {
				escape_html(value) {
					return String(value == null ? "" : value)
						.replaceAll("&", "&amp;")
						.replaceAll("<", "&lt;")
						.replaceAll(">", "&gt;")
						.replaceAll('"', "&quot;");
				},
			},
		},
	};
	vm.createContext(context);
	vm.runInContext(source, context, { filename: "injection_aps_shared.js" });
	const ui = context.injection_aps.ui;
	const html = ui.item_identity({
		item_code: "31000058",
		customer_code: "L-1225L",
		item_name: "Panlite Resin",
	});
	assert.ok(html.indexOf("31000058") < html.indexOf("L-1225L"));
	assert.ok(html.indexOf("L-1225L") < html.indexOf("Panlite Resin"));
	assert.doesNotMatch(html, /Customer Item|Customer Code|Item Name|客户物料号|物料名称/);

	const withoutCustomerCode = ui.item_identity({ item_code: "ITEM-2", item_name: "Name 2" });
	assert.doesNotMatch(withoutCustomerCode, /ia-item-customer-code/);

	const target = { innerHTML: "" };
	ui.render_table(
		target,
		[{ label: "Item", fieldname: "item_code" }],
		[{ item_code: "ITEM-3", customer_code: "C-3", item_name: "Name 3" }],
		() => "<a>ITEM-3</a>",
		{ show_count: false }
	);
	assert.match(target.innerHTML, /ia-item-identity/);
	assert.match(target.innerHTML, /C-3/);
	assert.match(target.innerHTML, /Name 3/);

	await ui.load_item_display_details(["ITEM-CACHED", "ITEM-CACHED"]);
	await ui.load_item_display_details(["ITEM-CACHED"]);
	const cachedHtml = ui.item_identity({ item_code: "ITEM-CACHED" });
	assert.equal(detailRequests, 1);
	assert.match(cachedHtml, /CACHED-CODE/);
	assert.match(cachedHtml, /Cached Name/);
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

async function testGanttManualAdjustmentUpdatesVisibleSegmentImmediately() {
	const { Controller, context } = loadPage(
		"injection_aps/page/aps_schedule_gantt/aps_schedule_gantt.js",
		"aps-schedule-gantt",
		"InjectionAPSScheduleGantt"
	);
	const controller = Object.create(Controller.prototype);
	controller.data = {
		tasks: [{
			id: "SEG-1",
			start: "2026-08-20 08:00:00",
			end: "2026-08-20 10:00:00",
			details: {
				segment_planned_qty: 100,
				workstation: "M-1",
				current_start_time: "2026-08-20 08:00:00",
				current_end_time: "2026-08-20 10:00:00",
			},
		}],
	};
	controller.focusWindow = {
		start: new Date("2026-08-20T00:00:00Z").getTime(),
		end: new Date("2026-08-20T12:00:00Z").getTime(),
	};
	controller.ganttShell = { scrollLeft: 144 };
	controller.feedback = {};
	let renderedTasks;
	controller.renderGantt = (tasks) => {
		renderedTasks = tasks;
	};
	context.window = { requestAnimationFrame: (callback) => callback() };
	context.frappe.datetime = {
		str_to_obj: (value) => vm.runInContext(`new Date(${JSON.stringify(value.replace(" ", "T") + "Z")})`, context),
	};
	const changed = controller.applyManualAdjustmentLocally({
		updated_segment: {
			name: "SEG-1",
			start_time: "2026-08-20 08:00:00",
			end_time: "2026-08-20 14:00:00",
			planned_qty: 300,
			workstation: "M-2",
			mould_reference: "MOLD-2",
			is_manual: 1,
		},
		updated_result: {
			machine_scheduled_qty: 300,
			demand_covered_qty: 250,
			overproduction_qty: 50,
			unscheduled_qty: 0,
		},
	});

	assert.equal(changed, true);
	assert.equal(renderedTasks, controller.data.tasks);
	assert.equal(controller.data.tasks[0].end, "2026-08-20 14:00:00");
	assert.equal(controller.data.tasks[0].details.segment_planned_qty, 300);
	assert.equal(controller.data.tasks[0].details.workstation, "M-2");
	assert.equal(controller.data.tasks[0].details.machine_scheduled_qty, 300);
	assert.equal(controller.data.tasks[0].details.overproduction_qty, 50);
	assert.equal(controller.focusWindow, null);
	assert.equal(controller.ganttShell.scrollLeft, 144);

	let refreshCount = 0;
	controller.refresh = async () => {
		refreshCount += 1;
	};
	const fallbackChanged = await controller.applyManualAdjustmentResponse(
		{ status: "Applied" },
		{
			start_time: "2026-08-20 08:00:00",
			end_time: "2026-08-20 16:00:00",
			target_qty: 400,
			target_workstation: "M-2",
			target_mould_reference: "MOLD-2",
			projected_result_qty: 400,
			overproduction_qty: 150,
			unscheduled_qty: 0,
		},
		"SEG-1"
	);

	assert.equal(fallbackChanged, true);
	assert.equal(refreshCount, 0);
	assert.equal(controller.data.tasks[0].end, "2026-08-20 16:00:00");
	assert.equal(controller.data.tasks[0].details.segment_planned_qty, 400);
	assert.equal(controller.data.tasks[0].details.overproduction_qty, 150);
}

async function main() {
	const tests = [
		testGanttRiskValuesTranslateEachEnum,
		testRunConsoleRendersFourDecisionColumnsAndKeepsFullExport,
		testRunConsoleStylesDoNotBlockPageInitialization,
		testRunConsoleResyncsRouteWhenSpaReusesThePage,
		testAdmissionBatchPolicyHandlesOneHundredRowsWithoutPerRowCalls,
		testSheetChangeReplacesOldMapping,
		testHeaderChangePreservesHeaderAndReplacesOldMapping,
		testSourceChangeDuringMappingApplyRejectsResponse,
		testCustomerProgressIgnoresOlderResponse,
		testCustomerProgressV2DispatchesDetailAndMatrixWithoutChangingLegacyCall,
		testCustomerProgressResyncsAndClearsRouteRunOnPageReuse,
		testCustomerProgressExplainsMissingFormalProjection,
		testProgressToolbarUsesSharedIconControls,
		testProgressMatrixCellSeparatesFourOperationalLayersAndUsesGroupedRowKey,
		testUiLoaderReloadsSharedAssetsByVersionAndDeduplicatesRequests,
		testSharedItemIdentityUsesThreeUnlabelledLines,
		testGanttMachineViewCollapsesCampaignAndRendersFourPlanLayers,
		testGanttManualAdjustmentUpdatesVisibleSegmentImmediately,
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
