"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_ROOT = path.resolve(__dirname, "..");

function loadPage(relativePath, pageName, className) {
	const context = {
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

async function main() {
	const tests = [
		testSheetChangeReplacesOldMapping,
		testHeaderChangePreservesHeaderAndReplacesOldMapping,
		testSourceChangeDuringMappingApplyRejectsResponse,
		testCustomerProgressIgnoresOlderResponse,
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
