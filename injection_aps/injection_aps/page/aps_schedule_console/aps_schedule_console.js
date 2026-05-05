frappe.pages["aps-schedule-console"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSScheduleConsole(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-schedule-console"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSScheduleConsole {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.pendingImport = null;
		this.lastImported = null;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Schedule Import & Diff"),
			single_column: true,
		});
		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Customer Schedule Versions")}</h3>
					<p>${__("Preview -> formal import -> rebuild demand pool / net requirement. Keep active schedule versions by customer, company, and scope, then push the planner directly to the next step.")}</p>
				</div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-feedback"></div>
				<div class="ia-status-host"></div>
				<div class="ia-action-host"></div>
				<div class="ia-grid-2">
					<div class="ia-panel">
						<h4>${__("Pending Preview")}</h4>
						<div class="ia-preview-summary ia-card-grid" style="margin-top: 8px;"></div>
						<div class="ia-preview-table" style="margin-top: 8px;"></div>
					</div>
					<div class="ia-page">
						<div class="ia-panel">
							<h4>${__("Active Versions")}</h4>
							<div class="ia-active-table" style="margin-top: 8px;"></div>
						</div>
						<div class="ia-panel">
							<h4>${__("Recent Import Batches")}</h4>
							<div class="ia-batch-table" style="margin-top: 8px;"></div>
						</div>
					</div>
				</div>
			</div>
		`);

		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.actionHost = this.page.main.find(".ia-action-host")[0];
		this.previewSummary = this.page.main.find(".ia-preview-summary")[0];
		this.previewTable = this.page.main.find(".ia-preview-table")[0];
		this.activeTable = this.page.main.find(".ia-active-table")[0];
		this.batchTable = this.page.main.find(".ia-batch-table")[0];
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading schedule versions..."));

		try {
			const data = await frappe.xcall("injection_aps.api.app.get_schedule_console_data");
			injection_aps.ui.render_cards(this.summary, [
				{ label: __("Active Versions"), value: data.summary.active_versions || 0, note: __("One active version per customer / company / scope") },
				{ label: __("Recent Batches"), value: data.summary.recent_batches || 0, note: __("Latest imports") },
				{ label: __("Active Qty"), value: injection_aps.ui.format_number(data.summary.active_qty || 0), note: __("Current live version volume") },
			]);
			this.renderScheduleTable(data.active_schedules || []);
			this.renderBatchTable(data.import_batches || [], data.next_actions || {});
			this.renderPreview();
			this.renderFlow();
			injection_aps.ui.set_feedback(
				this.feedback,
				this.pendingImport ? __("Preview ready. Primary action now imports and rebuilds demand / net requirement.") : __("Schedule console refreshed.")
			);
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load schedule versions."), "error");
		}
	}

	renderFlow() {
		const context = this.pendingImport
			? {
				current_step: __("Preview Completed"),
				next_step: __("Formal Import + Demand Rebuild"),
				blocking_reason: "",
				actions: [
					{ label: __("Import and Rebuild"), action_key: "import_and_promote", enabled: 1 },
					{ label: __("Import", null, "Injection APS"), action_key: "import_only", enabled: 1 },
					{ label: __("Net Requirements"), action_key: "open_net_requirement", enabled: this.lastImported ? 1 : 0, route: "aps-net-requirement-workbench" },
				],
			}
			: {
				current_step: this.lastImported ? __("Imported") : __("Waiting For Preview"),
				next_step: this.lastImported ? __("Open Net Requirement Workbench") : __("Preview Import"),
				blocking_reason: "",
				actions: [
					{ label: __("Preview Import"), action_key: "preview", enabled: 1 },
					{ label: __("Net Requirements"), action_key: "open_net_requirement", enabled: 1, route: "aps-net-requirement-workbench" },
				],
			};
		injection_aps.ui.render_status_line(this.statusHost, context);
		injection_aps.ui.render_actions(this.actionHost, context.actions, async (action) => {
			if (action.action_key === "preview") {
				this.openPreviewDialog();
				return;
			}
			if (action.action_key === "import_and_promote") {
				await this.importPending(true);
				return;
			}
			if (action.action_key === "import_only") {
				await this.importPending(false);
				return;
			}
			await injection_aps.ui.run_action(action);
		});
	}

	renderScheduleTable(rows) {
		injection_aps.ui.render_table(
			this.activeTable,
			[
				{ label: __("Name"), fieldname: "name" },
				{ label: __("Customer"), fieldname: "customer" },
				{ label: __("Company"), fieldname: "company" },
				{ label: __("Schedule Scope"), fieldname: "schedule_scope" },
				{ label: __("Version"), fieldname: "version_no" },
				{ label: __("Import Strategy"), fieldname: "import_strategy" },
				{ label: __("Source"), fieldname: "source_type" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("Qty"), fieldname: "schedule_total_qty" },
				{ label: __("Modified"), fieldname: "modified" },
			],
			rows,
			(column, value) => {
				if (column.fieldname === "name") {
					return injection_aps.ui.route_link(value, `customer-delivery-schedule/${encodeURIComponent(value)}`);
				}
				if (column.fieldname === "status") {
					return injection_aps.ui.pill(injection_aps.ui.translate(value), value === "Active" ? "green" : "blue");
				}
				if (column.fieldname === "modified") {
					return injection_aps.ui.format_datetime(value);
				}
				if (column.fieldname === "import_strategy") {
					return injection_aps.ui.escape(injection_aps.ui.translate(value));
				}
				if (column.fieldname === "schedule_total_qty") {
					return frappe.format(value || 0, { fieldtype: "Float" });
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Customer Delivery Schedule Overview"),
				export_sheet_name: __("Active Schedules"),
				export_file_name: "aps_customer_delivery_schedules",
				export_subtitle: __("Active customer schedule versions currently driving APS."),
			}
		);
	}

	renderBatchTable(rows, nextActions) {
		injection_aps.ui.render_table(
			this.batchTable,
			[
				{ label: __("Batch"), fieldname: "name" },
				{ label: __("Customer"), fieldname: "customer" },
				{ label: __("Schedule Scope"), fieldname: "schedule_scope" },
				{ label: __("Version"), fieldname: "version_no" },
				{ label: __("Import Strategy"), fieldname: "import_strategy" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("Imported"), fieldname: "imported_rows" },
				{ label: __("Effective"), fieldname: "effective_rows" },
				{ label: __("Next"), fieldname: "next_step" },
			],
			rows,
			(column, value, row) => {
				if (column.fieldname === "status") {
					return injection_aps.ui.pill(injection_aps.ui.translate(value), value === "Imported" ? "green" : "orange");
				}
				if (column.fieldname === "import_strategy") {
					return injection_aps.ui.escape(injection_aps.ui.translate(value));
				}
				if (column.fieldname === "next_step") {
					return injection_aps.ui.escape(
						injection_aps.ui.translate((nextActions && nextActions[row.name] && nextActions[row.name].next_step) || "")
					);
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Schedule Import Batch Review"),
				export_sheet_name: __("Import Batches"),
				export_file_name: "aps_schedule_import_batches",
				export_subtitle: __("Imported customer schedule batches and next recommended steps."),
			}
		);
	}

	renderPreview() {
		const preview = this.pendingImport ? this.pendingImport.preview : null;
		if (!preview) {
			injection_aps.ui.render_cards(this.previewSummary, [
				{ label: __("Preview"), value: __("None"), note: __("Run preview before import.") },
			]);
			injection_aps.ui.render_table(this.previewTable, [{ label: __("Info"), fieldname: "message" }], []);
			return;
		}

		const summaryRows = Object.entries(preview.summary || {}).map(([label, value]) => ({
			label: injection_aps.ui.translate(label),
			value,
		}));
		const parseContext = preview.parse_context || {};
		injection_aps.ui.render_cards(this.previewSummary, [
			{ label: __("Customer"), value: preview.customer || "-" },
			{ label: __("Schedule Scope"), value: preview.schedule_scope || "-" },
			{ label: __("Version"), value: preview.version_no || "-" },
			{ label: __("Import Strategy"), value: injection_aps.ui.translate(preview.import_strategy || "-") },
			{ label: __("Rows"), value: preview.row_count || 0 },
			{
				label: __("Changes", null, "Injection APS"),
				value: summaryRows.length || 0,
				note: [
					parseContext.parser_mode ? `${__("Mode")}:${injection_aps.ui.translate(parseContext.parser_mode)}` : "",
					parseContext.sheet_name ? `${__("Sheet")}:${parseContext.sheet_name}` : "",
					summaryRows.map((row) => `${row.label}:${row.value}`).join(" | "),
				]
					.filter(Boolean)
					.join(" | "),
			},
		]);
		this.renderPreviewEditor(preview.rows || []);
	}

	openPreviewDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Preview Customer Schedule Import"),
			fields: [
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer"), reqd: 1 },
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company"), reqd: 1, default: frappe.defaults.get_user_default("Company") },
				{ fieldname: "version_no", fieldtype: "Data", label: __("Version No"), reqd: 1 },
				{ fieldname: "schedule_scope", fieldtype: "Data", label: __("Schedule Scope"), reqd: 1 },
				{
					fieldname: "import_strategy",
					fieldtype: "Select",
					label: __("Import Strategy"),
					options: ["Replace Scope", "Partial Item Update", "Append"].join("\n"),
					default: "Replace Scope",
					reqd: 1,
				},
				{
					fieldname: "file_url",
					fieldtype: "Attach",
					label: __("Excel File"),
					change: () => this.inspectImportSource(dialog),
				},
				{ fieldname: "rows_json", fieldtype: "Small Text", label: __("Rows JSON"), description: __("Optional. Paste JSON rows when no Excel file is available.") },
				{
					fieldname: "parser_mode",
					fieldtype: "Select",
					label: __("Parser Mode"),
					options: ["rows", "matrix"].join("\n"),
					default: "matrix",
					change: () => this.syncImportDialogLayout(dialog),
				},
				{
					fieldname: "sheet_name",
					fieldtype: "Select",
					label: __("Sheet"),
					change: () => this.inspectImportSource(dialog, { forceSheet: 1 }),
				},
				{
					fieldname: "header_row_no",
					fieldtype: "Int",
					label: __("Header Row No"),
					change: () => this.inspectImportSource(dialog, { forceHeader: 1 }),
				},
				{ fieldname: "data_start_row_no", fieldtype: "Int", label: __("Data Start Row No"), default: 2 },
				{ fieldname: "item_reference_column", fieldtype: "Select", label: __("Item Reference Column") },
				{ fieldname: "customer_part_no_column", fieldtype: "Select", label: __("Customer Part No Column") },
				{ fieldname: "description_column", fieldtype: "Select", label: __("Description Column") },
				{ fieldname: "sales_order_column", fieldtype: "Select", label: __("Sales Order Column") },
				{ fieldname: "row_type_column", fieldtype: "Select", label: __("Row Type Column") },
				{ fieldname: "demand_row_type_value", fieldtype: "Data", label: __("Demand Row Type Value") },
				{ fieldname: "po_qty_column", fieldtype: "Select", label: __("PO Qty Column") },
				{ fieldname: "plan_qty_column", fieldtype: "Select", label: __("Plan Qty Column") },
				{ fieldname: "remark_column", fieldtype: "Select", label: __("Remark Column") },
				{
					fieldname: "date_columns_mode",
					fieldtype: "Select",
					label: __("Date Columns Mode"),
					options: ["auto", "range"].join("\n"),
					default: "auto",
					change: () => this.syncImportDialogLayout(dialog),
				},
				{ fieldname: "date_start_column", fieldtype: "Select", label: __("Date Start Column") },
				{ fieldname: "date_end_column", fieldtype: "Select", label: __("Date End Column") },
				{ fieldname: "skip_zero_qty", fieldtype: "Check", label: __("Skip Zero Qty"), default: 1 },
				{ fieldname: "inspection_html", fieldtype: "HTML", label: __("Detected Layout") },
			],
			primary_action_label: __("Preview"),
			primary_action: async (values) => {
				await this.previewImport(values);
				dialog.hide();
			},
		});
		this.syncImportDialogLayout(dialog);
		dialog.show();
	}

	syncImportDialogLayout(dialog) {
		const parserMode = dialog.get_value("parser_mode") || "matrix";
		const dateMode = dialog.get_value("date_columns_mode") || "auto";
		const showFileMapping = !!dialog.get_value("file_url");
		const matrixFields = [
			"item_reference_column",
			"customer_part_no_column",
			"description_column",
			"sales_order_column",
			"row_type_column",
			"demand_row_type_value",
			"po_qty_column",
			"plan_qty_column",
			"remark_column",
			"date_columns_mode",
			"date_start_column",
			"date_end_column",
			"skip_zero_qty",
		];
		dialog.set_df_property("sheet_name", "hidden", showFileMapping ? 0 : 1);
		dialog.set_df_property("header_row_no", "hidden", showFileMapping ? 0 : 1);
		dialog.set_df_property("data_start_row_no", "hidden", showFileMapping ? 0 : 1);
		dialog.set_df_property("inspection_html", "hidden", showFileMapping ? 0 : 1);
		matrixFields.forEach((fieldname) => {
			let hidden = !showFileMapping || parserMode !== "matrix";
			if (["date_start_column", "date_end_column"].includes(fieldname)) {
				hidden = hidden || dateMode !== "range";
			}
			dialog.set_df_property(fieldname, "hidden", hidden ? 1 : 0);
		});
	}

	getImportMapping(values) {
		if (!values.file_url) {
			return null;
		}
		const mapping = {
			parser_mode: values.parser_mode || "matrix",
			sheet_name: values.sheet_name || undefined,
			header_row_no: values.header_row_no || undefined,
			data_start_row_no: values.data_start_row_no || undefined,
		};
		if (mapping.parser_mode === "matrix") {
			Object.assign(mapping, {
				item_reference_column: values.item_reference_column || undefined,
				customer_part_no_column: values.customer_part_no_column || undefined,
				description_column: values.description_column || undefined,
				sales_order_column: values.sales_order_column || undefined,
				row_type_column: values.row_type_column || undefined,
				demand_row_type_value: values.demand_row_type_value || undefined,
				po_qty_column: values.po_qty_column || undefined,
				plan_qty_column: values.plan_qty_column || undefined,
				remark_column: values.remark_column || undefined,
				date_columns_mode: values.date_columns_mode || "auto",
				date_start_column: values.date_start_column || undefined,
				date_end_column: values.date_end_column || undefined,
				skip_zero_qty: values.skip_zero_qty ? 1 : 0,
			});
		}
		return mapping;
	}

	async inspectImportSource(dialog, options) {
		const settings = Object.assign({}, options || {});
		const fileUrl = dialog.get_value("file_url");
		if (!fileUrl) {
			dialog.get_field("inspection_html").$wrapper.html("");
			return;
		}
		const response = await frappe.xcall("injection_aps.api.app.inspect_customer_delivery_schedule_file", {
			file_url: fileUrl,
			sheet_name: dialog.get_value("sheet_name") || undefined,
			header_row_no: dialog.get_value("header_row_no") || undefined,
		});
		if (!response) {
			return;
		}
		const columnOptions = response.column_options || [];
		const selectOptions = ["", ...columnOptions.map((row) => row.label)].join("\n");
		const labelByValue = Object.fromEntries(columnOptions.map((row) => [row.value, row.label]));
		[
			"item_reference_column",
			"customer_part_no_column",
			"description_column",
			"sales_order_column",
			"row_type_column",
			"po_qty_column",
			"plan_qty_column",
			"remark_column",
			"date_start_column",
			"date_end_column",
		].forEach((fieldname) => dialog.set_df_property(fieldname, "options", selectOptions));
		dialog.set_df_property("sheet_name", "options", ["", ...(response.sheet_names || [])].join("\n"));
		if (!settings.forceSheet || !dialog.get_value("sheet_name")) {
			dialog.set_value("sheet_name", response.selected_sheet || "");
		}
		const detected = response.detected_mapping || {};
		if (!settings.forceSheet && !settings.forceHeader) {
			[
				"parser_mode",
				"header_row_no",
				"data_start_row_no",
				"item_reference_column",
				"customer_part_no_column",
				"description_column",
				"row_type_column",
				"demand_row_type_value",
				"po_qty_column",
				"plan_qty_column",
				"date_columns_mode",
				"date_start_column",
				"date_end_column",
			].forEach((fieldname) => {
				if (detected[fieldname] !== undefined && detected[fieldname] !== null && detected[fieldname] !== "") {
					dialog.set_value(fieldname, labelByValue[detected[fieldname]] || detected[fieldname]);
				}
			});
		}
		const sampleRows = response.sample_rows || [];
		const htmlRows = sampleRows
			.map(
				(row) =>
					`<tr>${(row || [])
						.slice(0, 12)
						.map((cell) => `<td>${injection_aps.ui.escape(cell == null ? "" : String(cell))}</td>`)
						.join("")}</tr>`
			)
			.join("");
		dialog.get_field("inspection_html").$wrapper.html(`
			<div class="ia-muted" style="margin-bottom:8px;">
				${__("Detected parser mode")}: ${injection_aps.ui.escape(detected.parser_mode || "-")} |
				${__("Date Columns")}: ${injection_aps.ui.escape(String((detected.date_column_letters || []).length || 0))}
			</div>
			<div class="ia-table-wrap">
				<table class="ia-table">
					<tbody>${htmlRows || `<tr><td>${__("No sample rows found.")}</td></tr>`}</tbody>
				</table>
			</div>
		`);
		this.syncImportDialogLayout(dialog);
	}

	async previewImport(values) {
		const scheduleScope = values.schedule_scope || values.version_no;
		const payload = {
			customer: values.customer,
			company: values.company,
			version_no: values.version_no,
			schedule_scope: scheduleScope,
			import_strategy: values.import_strategy || "Replace Scope",
			file_url: values.file_url || undefined,
			rows_json: values.rows_json || undefined,
			mapping_json: this.getImportMapping(values) ? JSON.stringify(this.getImportMapping(values)) : undefined,
		};
		injection_aps.ui.set_feedback(this.feedback, __("Running import preview..."));
		const preview = await injection_aps.ui.xcall(
			{
				message: __("Previewing customer schedule import..."),
				success_feedback: __("Preview completed. Review changes, then import and rebuild demand."),
				busy_key: `schedule-preview:${payload.customer || ""}:${payload.version_no || ""}`,
				feedback_target: this.feedback,
			},
			"injection_aps.api.app.preview_customer_delivery_schedule",
			payload
		);
		if (!preview) {
			return;
		}
		const editableRows = this.buildEditablePreviewRows(preview.rows || []);
		payload.rows_json = JSON.stringify(editableRows);
		this.pendingImport = { payload, preview, editableRows };
		this.renderPreview();
		this.renderFlow();
		injection_aps.ui.set_feedback(this.feedback, __("Preview completed. Review changes, then import and rebuild demand."), "warning");
	}

	async importPending(rebuildNextStep) {
		if (!this.pendingImport) {
			frappe.show_alert({ message: __("No pending preview to import."), indicator: "orange" });
			return;
		}
		const confirmed = await injection_aps.ui.confirm_action(
			{ action_key: rebuildNextStep ? "import_and_promote" : "import_only", confirm_required: 1 },
			{
				title: rebuildNextStep ? __("Confirm Import and Rebuild") : __("Confirm Import"),
				summary_lines: [
					__("Customer: {0}").replace("{0}", this.pendingImport.payload.customer || "-"),
					__("Company: {0}").replace("{0}", this.pendingImport.payload.company || "-"),
					__("Schedule Scope: {0}").replace("{0}", this.pendingImport.payload.schedule_scope || "-"),
					__("Version: {0}").replace("{0}", this.pendingImport.payload.version_no || "-"),
					__("Import Strategy: {0}").replace("{0}", injection_aps.ui.translate(this.pendingImport.payload.import_strategy || "-")),
					rebuildNextStep ? __("This will formally import the schedule and rebuild demand / net requirements.") : __("This will formally import the current schedule version."),
				],
			}
		);
		if (!confirmed) {
			return;
		}
		const response = await injection_aps.ui.with_busy(
			{
				message: rebuildNextStep
					? __("Importing schedule and rebuilding demand / net requirements...")
					: __("Importing customer schedule..."),
				success_feedback: rebuildNextStep
					? __("Schedule imported. Demand pool and net requirements were rebuilt.")
					: __("Schedule imported successfully."),
				busy_key: `schedule-import:${this.pendingImport.payload.customer || ""}:${this.pendingImport.payload.version_no || ""}`,
				feedback_target: this.feedback,
			},
			async () => {
				const imported = await frappe.xcall(
					"injection_aps.api.app.import_customer_delivery_schedule",
					this.pendingImport.payload
				);
				if (rebuildNextStep) {
					const promotion = await frappe.xcall(
						"injection_aps.api.app.promote_schedule_import_to_net_requirement",
						{
							import_batch: imported.import_batch,
						}
					);
					injection_aps.ui.show_warnings(promotion.demand_pool, __("Demand Pool Warnings"), "warning_count");
					injection_aps.ui.show_warnings(promotion.net_requirement, __("Net Requirement Warnings"), "warning_count");
				}
				return imported;
			}
		);
		if (!response) {
			return;
		}
		this.lastImported = response.schedule;
		this.pendingImport = null;
		frappe.show_alert({ message: __("Imported schedule {0}.").replace("{0}", response.schedule), indicator: "green" });
		await this.refresh();
	}

	buildEditablePreviewRows(rows) {
		return (rows || []).map((row) => ({
			sales_order: row.sales_order || "",
			item_code: row.item_code || "",
			customer_part_no: row.customer_part_no || "",
			schedule_date: row.schedule_date || "",
			qty: Number(row.qty || 0),
			remark: row.remark || "",
			source_origin: row.source_origin || "imported",
			source_excel_row: row.source_excel_row || "",
			manual_override: row.manual_override ? 1 : 0,
			manual_change_reason: row.manual_change_reason || "",
		}));
	}

	getEditablePreviewRows() {
		return this.pendingImport ? this.buildEditablePreviewRows(this.pendingImport.editableRows || this.pendingImport.preview.rows || []) : [];
	}

	async refreshPendingPreviewFromRows(rows, feedbackMessage) {
		if (!this.pendingImport) {
			return;
		}
		const nextRows = this.buildEditablePreviewRows(rows);
		this.pendingImport.payload.rows_json = JSON.stringify(nextRows);
		const preview = await injection_aps.ui.xcall(
			{
				message: __("Refreshing import preview..."),
				success_feedback: feedbackMessage || __("Preview updated."),
				busy_key: `schedule-preview-refresh:${this.pendingImport.payload.customer || ""}:${this.pendingImport.payload.version_no || ""}`,
				feedback_target: this.feedback,
			},
			"injection_aps.api.app.preview_customer_delivery_schedule",
			this.pendingImport.payload
		);
		if (!preview) {
			return;
		}
		this.pendingImport.preview = preview;
		this.pendingImport.editableRows = this.buildEditablePreviewRows(preview.rows || []);
		this.pendingImport.payload.schedule_scope = preview.schedule_scope || this.pendingImport.payload.schedule_scope;
		this.pendingImport.payload.import_strategy = preview.import_strategy || this.pendingImport.payload.import_strategy;
		this.pendingImport.payload.rows_json = JSON.stringify(this.pendingImport.editableRows);
		this.renderPreview();
	}

	renderPreviewEditor(rows) {
		const previewRows = rows || [];
		const columns = [
			{ label: __("Seq"), fieldname: "line_idx" },
			{ label: __("Excel Row"), fieldname: "source_excel_row" },
			{ label: __("Sales Order"), fieldname: "sales_order" },
			{ label: __("Item"), fieldname: "item_code" },
			{ label: __("Part No"), fieldname: "customer_part_no" },
			{ label: __("Schedule Date"), fieldname: "schedule_date" },
			{ label: __("Qty"), fieldname: "qty" },
			{ label: __("Prev Qty"), fieldname: "previous_qty" },
			{ label: __("Change", null, "Injection APS"), fieldname: "change_type" },
			{ label: __("Source"), fieldname: "source_origin" },
		];
		if (!previewRows.length) {
			this.previewTable.innerHTML = `
				<div class="ia-table-toolbar">
					${injection_aps.ui.icon_button("download", __("Export Excel"), { "data-ia-preview-export": "1" })}
					${injection_aps.ui.icon_button("plus", __("Add Row"), { "data-ia-preview-add": "1" })}
				</div>
				<div class="ia-table-shell"><div class="ia-muted ia-empty">${__("No rows found.")}</div></div>
			`;
			this.bindPreviewToolbar(previewRows, columns);
			return;
		}
		const body = previewRows
			.map((row, index) => {
				const tone = ["Cancelled", "Reduced", "Delayed"].includes(row.change_type)
					? "red"
					: ["Advanced", "Added", "Increased"].includes(row.change_type)
						? "orange"
						: "green";
				const displayLineIndex = row.line_idx || index + 1;
				const displayExcelRow = row.source_excel_row || "";
				return `
					<tr data-preview-index="${index}">
						<td class="ia-col-seq">${injection_aps.ui.escape(String(displayLineIndex))}</td>
						<td class="ia-col-excel-row">${injection_aps.ui.escape(String(displayExcelRow))}</td>
						<td>${injection_aps.ui.escape(row.sales_order || "")}</td>
						<td>${injection_aps.ui.escape(row.item_code || "")}</td>
						<td>${injection_aps.ui.escape(row.customer_part_no || "")}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.format_date(row.schedule_date))}</td>
						<td>${frappe.format(row.qty || 0, { fieldtype: "Float" })}</td>
						<td>${frappe.format(row.previous_qty || 0, { fieldtype: "Float" })}</td>
						<td>${injection_aps.ui.pill(injection_aps.ui.translate(row.change_type), tone)}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.source_origin || "imported"))}</td>
					</tr>
				`;
			})
			.join("");
		this.previewTable.innerHTML = `
			<div class="ia-table-toolbar">
				${injection_aps.ui.icon_button("download", __("Export Excel"), { "data-ia-preview-export": "1" })}
				${injection_aps.ui.icon_button("plus", __("Add Row"), { "data-ia-preview-add": "1" })}
			</div>
			<div class="ia-table-shell">
				<table class="ia-table">
					<thead>
						<tr>
							${columns
								.map((column) => {
									const className = column.fieldname === "line_idx"
										? "ia-col-seq"
										: column.fieldname === "source_excel_row"
											? "ia-col-excel-row"
											: "";
									return `<th${className ? ` class="${className}"` : ""}>${injection_aps.ui.escape(column.label)}</th>`;
								})
								.join("")}
						</tr>
					</thead>
					<tbody>${body}</tbody>
				</table>
			</div>
		`;
		this.bindPreviewToolbar(previewRows, columns);
		this.previewTable.querySelectorAll("[data-preview-index]").forEach((rowNode) => {
			rowNode.addEventListener("contextmenu", (event) => {
				event.preventDefault();
				const rowIndex = Number(rowNode.dataset.previewIndex || 0);
				injection_aps.ui.open_context_menu(
					[
						{
							label: __("Edit Row"),
							icon: "edit",
							handler: async () => this.openPreviewRowDialog(rowIndex),
						},
						{
							label: __("Delete Row"),
							icon: "delete",
							handler: async () => this.deletePreviewRow(rowIndex),
						},
					],
					{ x: event.clientX, y: event.clientY }
				);
			});
		});
	}

	bindPreviewToolbar(rows, columns) {
		const exportButton = this.previewTable.querySelector("[data-ia-preview-export='1']");
		if (exportButton) {
			exportButton.addEventListener("click", () => {
				injection_aps.ui.export_rows_to_excel({
					title: __("Schedule Import Preview"),
					subtitle: __("Preview rows before formally importing customer schedule data."),
					sheet_name: __("Preview Diff"),
					file_name: "aps_schedule_preview",
					columns,
					rows,
					formatter: (column, value) => {
						if (column.fieldname === "change_type" || column.fieldname === "source_origin") {
							return injection_aps.ui.translate(value);
						}
						if (column.fieldname === "schedule_date") {
							return injection_aps.ui.format_date(value);
						}
						return value;
					},
				});
			});
		}
		const addButton = this.previewTable.querySelector("[data-ia-preview-add='1']");
		if (addButton) {
			addButton.addEventListener("click", async () => {
				await this.openPreviewRowDialog(null);
			});
		}
	}

	async openPreviewRowDialog(rowIndex) {
		const rows = this.getEditablePreviewRows();
		const isNew = rowIndex == null || rowIndex < 0 || rowIndex >= rows.length;
		const row = isNew ? {} : rows[rowIndex];
		const dialog = new frappe.ui.Dialog({
			title: isNew ? __("Add Preview Row") : __("Edit Preview Row"),
			fields: [
				{ fieldname: "sales_order", fieldtype: "Data", label: __("Sales Order"), default: row.sales_order || "" },
				{ fieldname: "item_code", fieldtype: "Data", label: __("Item"), reqd: 1, default: row.item_code || "" },
				{ fieldname: "customer_part_no", fieldtype: "Data", label: __("Part No"), default: row.customer_part_no || "" },
				{ fieldname: "schedule_date", fieldtype: "Date", label: __("Schedule Date"), reqd: 1, default: row.schedule_date || "" },
				{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), reqd: 1, default: row.qty || 0 },
				{ fieldname: "remark", fieldtype: "Small Text", label: __("Remark"), default: row.remark || "" },
				{ fieldname: "manual_change_reason", fieldtype: "Small Text", label: __("Manual Change Reason"), reqd: 1, default: row.manual_change_reason || "" },
			],
			primary_action_label: isNew ? __("Add Row") : __("Update Row"),
			primary_action: async (values) => {
				const updated = Object.assign({}, row, {
					sales_order: values.sales_order || "",
					item_code: values.item_code || "",
					customer_part_no: values.customer_part_no || "",
					schedule_date: values.schedule_date,
					qty: Number(values.qty || 0),
					remark: values.remark || "",
					source_origin: isNew ? "manual_added" : "manual_adjusted",
					source_excel_row: row.source_excel_row || "",
					manual_override: 1,
					manual_change_reason: values.manual_change_reason || "",
				});
				const nextRows = rows.slice();
				if (isNew) {
					nextRows.push(updated);
				} else {
					nextRows[rowIndex] = updated;
				}
				dialog.hide();
				await this.refreshPendingPreviewFromRows(
					nextRows,
					isNew ? __("Preview updated after adding one row.") : __("Preview updated after editing one row.")
				);
			},
		});
		dialog.show();
	}

	async deletePreviewRow(rowIndex) {
		const rows = this.getEditablePreviewRows();
		if (rowIndex < 0 || rowIndex >= rows.length) {
			return;
		}
		const row = Object.assign({}, rows[rowIndex]);
		const reason = await injection_aps.ui.prompt_reason({
			title: __("Delete Preview Row"),
			summary_lines: [
				__("Item: {0}").replace("{0}", row.item_code || "-"),
				__("Schedule Date: {0}").replace("{0}", injection_aps.ui.format_date(row.schedule_date || "")),
				__("Qty: {0}").replace("{0}", frappe.format(row.qty || 0, { fieldtype: "Float" })),
			],
			label: __("Manual Change Reason"),
			primary_action_label: __("Delete Row"),
		});
		if (!reason) {
			return;
		}
		let nextRows = rows.slice();
		if (Number(row.previous_qty || 0) > 0 || row.source_origin === "retained_existing") {
			nextRows[rowIndex] = Object.assign({}, row, {
				qty: 0,
				source_origin: "manual_adjusted",
				manual_override: 1,
				manual_change_reason: reason,
			});
		} else {
			nextRows.splice(rowIndex, 1);
		}
		await this.refreshPendingPreviewFromRows(nextRows, __("Preview updated after removing one row."));
	}
}
