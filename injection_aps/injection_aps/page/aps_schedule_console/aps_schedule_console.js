frappe.pages["aps-schedule-console"].on_page_load = function (wrapper) {
	injection_aps.ui_loader.start("20260901.1", () => {
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
		this.v2Enabled = false;
		this.unallocatedDeliveryCount = 0;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Schedule Import & Diff"),
			single_column: true,
		});
		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-workflow-host"></div>
				<div class="ia-banner">
					<h3>${__("Customer Schedule Versions")}</h3>
					<p>${__("Preview -> formal import -> rebuild demand pool / net requirement. Keep active schedule versions by customer, company, and scope, then push the planner directly to the next step.")}</p>
				</div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-feedback"></div>
				<div class="ia-status-host"></div>
				<div class="ia-action-host"></div>
				<div class="ia-import-continuation-host"></div>
				<div class="ia-import-checks-host"></div>
				<div class="ia-panel ia-pending-preview-panel">
					<h4>${__("Pending Preview")}</h4>
					<div class="ia-preview-summary ia-card-grid" style="margin-top: 8px;"></div>
					<div class="ia-preview-table" style="margin-top: 8px;"></div>
				</div>
				<div class="ia-grid-2">
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
		`);

		this.workflowHost = this.page.main.find(".ia-workflow-host")[0];
		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.actionHost = this.page.main.find(".ia-action-host")[0];
		this.continuationHost = this.page.main.find(".ia-import-continuation-host")[0];
		this.importChecksHost = this.page.main.find(".ia-import-checks-host")[0];
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
			this.v2Enabled = Boolean(data.v2 && data.v2.settings && Number(data.v2.settings.enable_aps_v2));
			this.unallocatedDeliveryCount = Number((data.summary || {}).unallocated_delivery_count || 0);
			const summaryCards = [
				{ label: __("Active Versions"), value: data.summary.active_versions || 0, note: __("One active version per customer / company / scope") },
				{ label: __("Recent Batches"), value: data.summary.recent_batches || 0, note: __("Latest imports") },
				{ label: __("Active Qty"), value: injection_aps.ui.format_number(data.summary.active_qty || 0), note: __("Current live version volume") },
			];
			if (this.v2Enabled) {
				summaryCards.push({
					label: __("Unallocated Deliveries", null, "Injection APS"),
					value: this.unallocatedDeliveryCount,
					note: __("Review unresolved delivery lineage without blocking shipment.", null, "Injection APS"),
				});
			}
			injection_aps.ui.render_cards(this.summary, summaryCards);
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
		injection_aps.ui.render_workflow_steps(this.workflowHost, [
			{ label: __("Demand baseline", null, "Injection APS"), status: "current" },
			{ label: __("Batch admission", null, "Injection APS"), status: "upcoming" },
			{ label: __("Impact confirmation", null, "Injection APS"), status: "upcoming" },
			{ label: __("APS calculation", null, "Injection APS"), status: "upcoming" },
		]);
		const previewReady = Boolean(
			this.pendingImport &&
				this.pendingImport.preview &&
				(this.v2Enabled ? this.pendingImport.preview.can_apply : this.pendingImport.preview.can_import)
		);
		const previewBlocked = Boolean(this.pendingImport && !previewReady);
		const blockingReason = previewBlocked
			? this.pendingImport.preview.is_idempotent_replay
				? __("This file was already imported. No additional demand will be created.")
				: __("Import checks contain blocking findings.")
			: "";
		const context = this.pendingImport
			? {
				current_step: __("3 View Differences", null, "Injection APS"),
				next_step: __("Formal Import + Demand Rebuild"),
				blocking_reason: blockingReason,
				actions: [
					{ label: __("Import and Rebuild"), action_key: "import_and_promote", enabled: previewReady ? 1 : 0 },
					{ label: __("Import", null, "Injection APS"), action_key: "import_only", enabled: previewReady ? 1 : 0 },
					{ label: __("Refresh Preview", null, "Injection APS"), action_key: "refresh_preview", enabled: 1 },
					{ label: __("Net Requirements"), action_key: "open_net_requirement", enabled: this.lastImported ? 1 : 0, route: "aps-net-requirement-workbench" },
				].concat(
					this.v2Enabled
						? [{ label: __("Unallocated Deliveries", null, "Injection APS"), action_key: "open_unallocated_delivery", enabled: 1 }]
						: []
				),
			}
			: {
				current_step: this.lastImported ? __("Imported") : __("1 Upload", null, "Injection APS"),
				next_step: this.lastImported ? __("Review net requirements", null, "Injection APS") : __("2 Confirm Recognition", null, "Injection APS"),
				blocking_reason: "",
				actions: (this.lastImported
					? [
						{ label: __("Continue to net requirement review", null, "Injection APS"), action_key: "open_net_requirement", enabled: 1, route: "aps-net-requirement-workbench" },
						{ label: __("Preview another import", null, "Injection APS"), action_key: "preview", enabled: 1 },
					]
					: [
						{ label: __("Preview Import"), action_key: "preview", enabled: 1 },
						{ label: __("Net Requirements"), action_key: "open_net_requirement", enabled: 1, route: "aps-net-requirement-workbench" },
					]).concat(
					this.v2Enabled
						? [{ label: __("Unallocated Deliveries", null, "Injection APS"), action_key: "open_unallocated_delivery", enabled: 1 }]
						: []
				),
			};
		injection_aps.ui.render_status_line(this.statusHost, context);
		this.renderImportContinuation();
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
			if (action.action_key === "refresh_preview") {
				await this.refreshPendingPreviewFromRows(
					this.getEditablePreviewRows(),
					__("Preview refreshed against the latest schedule state.")
				);
				this.renderFlow();
				return;
			}
			if (action.action_key === "open_unallocated_delivery") {
				const doctype = "APS Unallocated Delivery";
				// A permission change only reaches frappe.boot.user.can_read after a
				// fresh boot. Register the route explicitly so an already-open Desk
				// session does not misclassify the DocType slug as a missing Page.
				frappe.router.routes[frappe.router.slug(doctype)] = { doctype };
				frappe.set_route("List", doctype, "List");
				return;
			}
			await injection_aps.ui.run_action(action);
		});
	}

	renderImportContinuation() {
		if (!this.lastImported || this.pendingImport) {
			this.continuationHost.innerHTML = "";
			return;
		}
		this.continuationHost.innerHTML = `
			<div class="ia-import-continuation">
				<div><strong>${__("Schedule import completed", null, "Injection APS")}</strong><span>${__("Review warnings if any, then verify the net demand before creating the APS draft Run.", null, "Injection APS")}</span></div>
				<button type="button" class="btn btn-primary btn-sm" data-continue-net="1">${__("Continue to net requirement review", null, "Injection APS")}</button>
			</div>`;
		this.continuationHost.querySelector("[data-continue-net='1']").addEventListener("click", () => injection_aps.ui.go_to("aps-net-requirement-workbench"));
	}

	renderScheduleTable(rows) {
		injection_aps.ui.render_table(
			this.activeTable,
			[
				{ label: __("Name", null, "Injection APS"), fieldname: "name" },
				{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
				{ label: __("Company", null, "Injection APS"), fieldname: "company" },
				{ label: __("Schedule Scope"), fieldname: "schedule_scope" },
				{ label: __("Version", null, "Injection APS"), fieldname: "version_no" },
				{ label: __("Import Strategy"), fieldname: "import_strategy" },
				{ label: __("Source", null, "Injection APS"), fieldname: "source_type" },
				{ label: __("Status", null, "Injection APS"), fieldname: "status" },
				{ label: __("Qty"), fieldname: "schedule_total_qty" },
				{ label: __("Modified", null, "Injection APS"), fieldname: "modified" },
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
				{ label: __("Batch", null, "Injection APS"), fieldname: "name" },
				{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
				{ label: __("Schedule Scope"), fieldname: "schedule_scope" },
				{ label: __("Version", null, "Injection APS"), fieldname: "version_no" },
				{ label: __("Import Strategy"), fieldname: "import_strategy" },
				{ label: __("Status", null, "Injection APS"), fieldname: "status" },
				{ label: __("Imported"), fieldname: "imported_rows" },
				{ label: __("Effective", null, "Injection APS"), fieldname: "effective_rows" },
				{ label: __("Next", null, "Injection APS"), fieldname: "next_step" },
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
			this.renderImportChecks([]);
			injection_aps.ui.render_cards(this.previewSummary, [
				{ label: __("Preview", null, "Injection APS"), value: __("None", null, "Injection APS"), note: __("Run preview before import.") },
			]);
			injection_aps.ui.render_table(this.previewTable, [{ label: __("Info", null, "Injection APS"), fieldname: "message" }], []);
			return;
		}

		const summaryRows = Object.entries(preview.summary || {}).map(([label, value]) => ({
			label: injection_aps.ui.translate(label),
			value,
		}));
		const parseContext = preview.parse_context || {};
		const selectedMode = preview.revision_mode || preview.import_strategy || "-";
		const postImportQty = this.v2Enabled ? preview.post_revision_total_qty : preview.post_import_total_qty;
		const totalDeltaQty = this.v2Enabled
			? Number(preview.post_revision_total_qty || 0) - Number(preview.previous_total_qty || 0)
			: preview.total_delta_qty;
		injection_aps.ui.render_cards(this.previewSummary, [
			{ label: __("Customer", null, "Injection APS"), value: preview.customer || "-" },
			{ label: __("Schedule Scope"), value: preview.schedule_scope || "-" },
			{ label: __("Version", null, "Injection APS"), value: preview.version_no || "-" },
			{
				label: this.v2Enabled ? __("Confirmed Revision Mode", null, "Injection APS") : __("Import Strategy"),
				value: injection_aps.ui.translate(selectedMode),
				note: this.v2Enabled
					? `${__("APS Recommendation", null, "Injection APS")}: ${injection_aps.ui.translate(preview.recommended_revision_mode || "-")}`
					: "",
			},
			{ label: __("Source Rows", null, "Injection APS"), value: preview.source_row_count || 0 },
			{ label: __("Previous Total", null, "Injection APS"), value: frappe.format(preview.previous_total_qty || 0, { fieldtype: "Float" }) },
			{ label: __("Post Import Total", null, "Injection APS"), value: frappe.format(postImportQty || 0, { fieldtype: "Float" }) },
			{ label: __("Total Delta", null, "Injection APS"), value: frappe.format(totalDeltaQty || 0, { fieldtype: "Float" }) },
			{
				label: __("Changes", null, "Injection APS"),
				value: summaryRows.length || 0,
				note: [
					parseContext.parser_mode ? `${__("Mode", null, "Injection APS")}:${injection_aps.ui.translate(parseContext.parser_mode)}` : "",
					parseContext.sheet_name ? `${__("Sheet")}:${parseContext.sheet_name}` : "",
					summaryRows.map((row) => `${row.label}:${row.value}`).join(" | "),
				]
					.filter(Boolean)
					.join(" | "),
			},
		]);
		this.renderImportChecks(preview.checks || []);
		this.renderPreviewEditor(preview.rows || []);
	}

	renderImportChecks(checks) {
		if (!checks.length) {
			this.importChecksHost.innerHTML = "";
			return;
		}
		const rows = checks
			.map((check) => {
				const status = check.status || "notice";
				const iconName = status === "passed" ? "check" : status === "failed" ? "x" : "alert-triangle";
				const details = (check.details || [])
					.map((detail) => `<li>${injection_aps.ui.escape(detail || "")}</li>`)
					.join("");
				return `
					<div class="ia-import-check ia-import-check-${injection_aps.ui.escape(status)}">
						<span class="ia-import-check-icon">${injection_aps.ui.icon(iconName, "sm")}</span>
						<div class="ia-import-check-body">
							<div class="ia-import-check-title">${injection_aps.ui.escape(check.title || "")}</div>
							<div class="ia-import-check-summary">${injection_aps.ui.escape(check.summary || "")}</div>
							${details ? `<ul>${details}</ul>` : ""}
						</div>
					</div>
				`;
			})
			.join("");
		this.importChecksHost.innerHTML = `
			<div class="ia-panel ia-import-checks">
				<div class="ia-panel-head"><h4>${__("Import Checks", null, "Injection APS")}</h4></div>
				${rows}
			</div>
		`;
	}

	openPreviewDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Import Customer Schedule", null, "Injection APS"),
			fields: [
				{ fieldname: "wizard_progress", fieldtype: "HTML" },
				{
					fieldname: "upload_hint",
					fieldtype: "HTML",
					options: `<div class="ia-muted">${__(
						"Upload the schedule and fill in its business scope. APS will recognize the workbook before showing any differences.",
						null,
						"Injection APS"
					)}</div>`,
				},
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer", null, "Injection APS"), reqd: 1 },
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company", null, "Injection APS"), reqd: 1, default: frappe.defaults.get_user_default("Company") },
				{ fieldname: "version_no", fieldtype: "Data", label: __("Version No"), reqd: 1 },
				{ fieldname: "schedule_scope", fieldtype: "Data", label: __("Schedule Scope"), reqd: 1 },
				{
					fieldname: "import_strategy",
					fieldtype: "Select",
					label: __("Import Strategy"),
					options: ["Replace Scope", "Partial Update", "Append"].join("\n"),
					context: "Injection APS",
					default: "Replace Scope",
					reqd: 1,
				},
				{
					fieldname: "duplicate_policy",
					fieldtype: "Select",
					label: __("Duplicate Policy", null, "Injection APS"),
					options: ["Block", "Sum"].join("\n"),
					context: "Injection APS",
					default: "Block",
					reqd: 1,
					description: __("Duplicates are blocked unless Sum is explicitly selected."),
				},
				{
					fieldname: "file_url",
					fieldtype: "Attach",
					label: __("Excel File"),
					description: __("Upload an Excel file. APS will recognize the sheet, header and date columns in the next step.", null, "Injection APS"),
					change: () => this.resetImportRecognition(dialog),
				},
				{ fieldname: "advanced_source_controls", fieldtype: "HTML" },
				{
					fieldname: "rows_json",
					fieldtype: "Small Text",
					label: __("Rows JSON"),
					description: __("Advanced fallback only. Paste JSON rows when no Excel file is available.", null, "Injection APS"),
				},
				{ fieldname: "recognition_hint", fieldtype: "HTML" },
				{ fieldname: "inspection_html", fieldtype: "HTML", label: __("Detected Layout") },
				{ fieldname: "advanced_mapping_controls", fieldtype: "HTML" },
				{
					fieldname: "parser_mode",
					fieldtype: "Select",
					label: __("Parser Mode"),
					options: ["rows", "matrix"].join("\n"),
					context: "Injection APS",
					default: "matrix",
					change: () => this.syncImportDialogLayout(dialog),
				},
				{
					fieldname: "sheet_name",
					fieldtype: "Select",
					label: __("Sheet"),
					change: async () => {
						await this.refreshImportRecognition(dialog, { forceSheet: 1 });
					},
				},
				{
					fieldname: "header_row_no",
					fieldtype: "Int",
					label: __("Header Row No"),
					change: async () => {
						await this.refreshImportRecognition(dialog, { forceHeader: 1 });
					},
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
					context: "Injection APS",
					default: "auto",
					change: () => this.syncImportDialogLayout(dialog),
				},
				{ fieldname: "date_start_column", fieldtype: "Select", label: __("Date Start Column") },
				{ fieldname: "date_end_column", fieldtype: "Select", label: __("Date End Column") },
			],
			primary_action_label: __("Recognize File", null, "Injection APS"),
			primary_action: async () => this.advanceImportWizardToRecognition(dialog),
		});
		dialog.iaWizardStep = 1;
		dialog.iaShowAdvancedSource = false;
		dialog.iaShowAdvancedMapping = false;
		dialog.iaInspectionResponse = null;
		dialog.iaInspectionSourceSignature = "";
		dialog.iaInspectionGeneration = 0;
		dialog.iaSourceNeedsAutoDetection = true;
		this.showImportWizardStep(dialog, 1);
		dialog.show();
	}

	syncImportDialogLayout(dialog) {
		const step = dialog.iaWizardStep || 1;
		const parserMode = dialog.get_value("parser_mode") || "matrix";
		const dateMode = dialog.get_value("date_columns_mode") || "auto";
		const showFileMapping = !!dialog.get_value("file_url");
		const uploadFields = [
			"upload_hint",
			"customer",
			"company",
			"version_no",
			"schedule_scope",
			"import_strategy",
			"duplicate_policy",
			"file_url",
			"advanced_source_controls",
		];
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
		];
		uploadFields.forEach((fieldname) => dialog.set_df_property(fieldname, "hidden", step === 1 ? 0 : 1));
		if (this.v2Enabled) {
			dialog.set_df_property("import_strategy", "hidden", 1);
			dialog.set_df_property("import_strategy", "reqd", 0);
		}
		dialog.set_df_property("rows_json", "hidden", step === 1 && dialog.iaShowAdvancedSource ? 0 : 1);
		dialog.set_df_property("recognition_hint", "hidden", step === 2 ? 0 : 1);
		dialog.set_df_property("inspection_html", "hidden", step === 2 ? 0 : 1);
		dialog.set_df_property("advanced_mapping_controls", "hidden", step === 2 ? 0 : 1);
		["parser_mode", "sheet_name", "header_row_no", "data_start_row_no"].forEach((fieldname) => {
			dialog.set_df_property(
				fieldname,
				"hidden",
				step === 2 && showFileMapping && dialog.iaShowAdvancedMapping ? 0 : 1
			);
		});
		matrixFields.forEach((fieldname) => {
			let hidden = step !== 2 || !showFileMapping || !dialog.iaShowAdvancedMapping || parserMode !== "matrix";
			if (["date_start_column", "date_end_column"].includes(fieldname)) {
				hidden = hidden || dateMode !== "range";
			}
			dialog.set_df_property(fieldname, "hidden", hidden ? 1 : 0);
		});
		this.renderImportWizardProgress(dialog);
		this.renderImportWizardControls(dialog);
	}

	renderImportWizardProgress(dialog) {
		const host = dialog.get_field("wizard_progress").$wrapper;
		const currentStep = dialog.iaWizardStep || 1;
		const steps = [
			__("1 Upload", null, "Injection APS"),
			__("2 Confirm Recognition", null, "Injection APS"),
			__("3 View Differences", null, "Injection APS"),
		];
		host.html(`
			<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;">
				${steps
					.map((label, index) => {
						const active = index + 1 === currentStep;
						const complete = index + 1 < currentStep;
						const style = active
							? "background:var(--primary);color:var(--fg-color);border-color:var(--primary);"
							: complete
								? "background:var(--green-100);color:var(--green-700);border-color:var(--green-300);"
								: "background:var(--control-bg);color:var(--text-muted);border-color:var(--border-color);";
						return `<span style="padding:5px 10px;border:1px solid;border-radius:999px;${style}">${injection_aps.ui.escape(label)}</span>`;
					})
					.join("")}
			</div>
		`);
	}

	renderImportWizardControls(dialog) {
		const sourceHost = dialog.get_field("advanced_source_controls").$wrapper;
		const sourceLabel = dialog.iaShowAdvancedSource
			? __("Hide Manual JSON", null, "Injection APS")
			: __("Advanced: Enter Rows JSON", null, "Injection APS");
		sourceHost.html(`
			<button type="button" class="btn btn-xs btn-default ia-toggle-json-source">${injection_aps.ui.escape(sourceLabel)}</button>
			<span class="ia-muted" style="margin-left:8px;">${__("Most PMC imports only need the Excel upload above.", null, "Injection APS")}</span>
		`);
		sourceHost.find(".ia-toggle-json-source").on("click", () => {
			dialog.iaShowAdvancedSource = !dialog.iaShowAdvancedSource;
			this.syncImportDialogLayout(dialog);
		});

		const mappingHost = dialog.get_field("advanced_mapping_controls").$wrapper;
		const hasFile = !!dialog.get_value("file_url");
		const mappingLabel = dialog.iaShowAdvancedMapping
			? __("Hide Advanced Mapping", null, "Injection APS")
			: __("Advanced Field Mapping", null, "Injection APS");
		mappingHost.html(`
			<button type="button" class="btn btn-xs btn-default ia-import-back">${__("Back to Upload", null, "Injection APS")}</button>
			${
				hasFile
					? `<button type="button" class="btn btn-xs btn-default ia-toggle-import-mapping" style="margin-left:8px;">${injection_aps.ui.escape(mappingLabel)}</button>`
					: ""
			}
			<span class="ia-muted" style="margin-left:8px;">${
				hasFile
					? __("Use the detected mapping unless this workbook needs a manual correction.", null, "Injection APS")
					: __("Manual JSON will be validated when the differences are generated.", null, "Injection APS")
			}</span>
		`);
		mappingHost.find(".ia-import-back").on("click", () => this.showImportWizardStep(dialog, 1));
		mappingHost.find(".ia-toggle-import-mapping").on("click", () => {
			dialog.iaShowAdvancedMapping = !dialog.iaShowAdvancedMapping;
			this.syncImportDialogLayout(dialog);
		});
	}

	showImportWizardStep(dialog, step) {
		dialog.iaWizardStep = step;
		this.syncImportDialogLayout(dialog);
		if (step === 1) {
			dialog.set_primary_action(__("Recognize Source", null, "Injection APS"), async () => {
				await this.advanceImportWizardToRecognition(dialog);
			});
			return;
		}
		dialog.set_primary_action(__("Confirm Recognition and View Differences", null, "Injection APS"), async () => {
			let values = dialog.get_values();
			if (!values) {
				return;
			}
			if (values.file_url) {
				const response = await this.ensureImportRecognitionCurrent(dialog);
				if (!response) {
					return;
				}
				// Recognition can replace stale mapping controls. Read the submitted
				// values only after the latest sheet/header response has been applied.
				values = dialog.get_values();
				if (!values) {
					return;
				}
			}
			const preview = await this.previewImport(values);
			if (preview) {
				dialog.hide();
				const previewPanel = this.page.main.find(".ia-pending-preview-panel")[0];
				if (previewPanel && previewPanel.scrollIntoView) {
					previewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
				}
			}
		});
	}

	resetImportRecognition(dialog) {
		dialog.iaInspectionGeneration = (dialog.iaInspectionGeneration || 0) + 1;
		dialog.iaInspectionResponse = null;
		dialog.iaInspectionSourceSignature = "";
		dialog.iaShowAdvancedMapping = false;
		dialog.iaSourceNeedsAutoDetection = true;
		const inspectionField = dialog.get_field("inspection_html");
		if (inspectionField) {
			inspectionField.$wrapper.html("");
		}
	}

	async refreshImportRecognition(dialog, options) {
		if (dialog.iaApplyingInspection) {
			return null;
		}
		try {
			return await this.inspectImportSource(dialog, options);
		} catch (error) {
			const message = (error && error.message) || __("The workbook could not be recognized.", null, "Injection APS");
			frappe.msgprint({
				title: __("Recognition Failed", null, "Injection APS"),
				message: injection_aps.ui.escape(message),
				indicator: "red",
			});
			return null;
		}
	}

	async advanceImportWizardToRecognition(dialog) {
		const values = dialog.get_values();
		if (!values) {
			return;
		}
		const rowsJson = String(values.rows_json || "").trim();
		if (!values.file_url && !rowsJson) {
			frappe.msgprint(__("Upload an Excel file, or use the advanced option to enter Rows JSON.", null, "Injection APS"));
			return;
		}
		if (values.file_url) {
			const response = await this.refreshImportRecognition(dialog);
			if (!response) {
				return;
			}
		} else {
			let manualRows;
			try {
				manualRows = JSON.parse(rowsJson);
			} catch (error) {
				frappe.msgprint(__("Rows JSON must be valid JSON before it can be previewed.", null, "Injection APS"));
				return;
			}
			if (!Array.isArray(manualRows) || !manualRows.length) {
				frappe.msgprint(__("Rows JSON must contain at least one row.", null, "Injection APS"));
				return;
			}
			dialog.get_field("inspection_html").$wrapper.html(`
				<div class="ia-import-check ia-import-check-passed">
					<div class="ia-import-check-body">
						<div class="ia-import-check-title">${__("Manual JSON source recognized", null, "Injection APS")}</div>
						<div class="ia-import-check-summary">${__("{0} rows are ready for server validation.", null, "Injection APS").replace(
							"{0}",
							injection_aps.ui.escape(String(manualRows.length))
						)}</div>
					</div>
				</div>
			`);
		}
		dialog.get_field("recognition_hint").$wrapper.html(`
			<div class="ia-muted" style="margin-bottom:8px;">${__(
				"Confirm the recognized source below. Open advanced field mapping only when the detected layout is incorrect.",
				null,
				"Injection APS"
			)}</div>
		`);
		this.showImportWizardStep(dialog, 2);
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
			});
		}
		return mapping;
	}

	getImportInspectionSnapshot(dialog, settings) {
		const sourceChanged = !!dialog.iaSourceNeedsAutoDetection;
		const sheetName = String(dialog.get_value("sheet_name") || "");
		const headerRowNo = String(dialog.get_value("header_row_no") || "");
		return {
			generation: (dialog.iaInspectionGeneration || 0) + 1,
			file_url: String(dialog.get_value("file_url") || ""),
			sheet_name: sheetName,
			header_row_no: headerRowNo,
			auto_detect: sourceChanged,
			request_sheet_name: sourceChanged ? "" : sheetName,
			request_header_row_no: sourceChanged || settings.forceSheet ? "" : headerRowNo,
		};
	}

	getImportInspectionSourceSignature(dialog) {
		return JSON.stringify([
			String(dialog.get_value("file_url") || ""),
			String(dialog.get_value("sheet_name") || ""),
			String(dialog.get_value("header_row_no") || ""),
		]);
	}

	async ensureImportRecognitionCurrent(dialog) {
		const currentSignature = this.getImportInspectionSourceSignature(dialog);
		if (
			dialog.iaInspectionResponse &&
			dialog.iaInspectionSourceSignature === currentSignature
		) {
			return dialog.iaInspectionResponse;
		}
		// Preserve the user's current sheet/header choice, but redetect every
		// dependent mapping field before formal preview submission.
		return await this.refreshImportRecognition(dialog, { forceHeader: 1 });
	}

	isImportInspectionRequestCurrent(dialog, requestSnapshot) {
		return (
			requestSnapshot.generation === (dialog.iaInspectionGeneration || 0) &&
			requestSnapshot.file_url === String(dialog.get_value("file_url") || "") &&
			requestSnapshot.sheet_name === String(dialog.get_value("sheet_name") || "") &&
			requestSnapshot.header_row_no === String(dialog.get_value("header_row_no") || "")
		);
	}

	async inspectImportSource(dialog, options) {
		const settings = Object.assign({}, options || {});
		const requestSnapshot = this.getImportInspectionSnapshot(dialog, settings);
		dialog.iaInspectionGeneration = requestSnapshot.generation;
		if (!requestSnapshot.file_url) {
			dialog.get_field("inspection_html").$wrapper.html("");
			return null;
		}
		let response;
		try {
			response = await frappe.xcall("injection_aps.api.app.inspect_customer_delivery_schedule_file", {
				file_url: requestSnapshot.file_url,
				sheet_name: requestSnapshot.request_sheet_name || undefined,
				header_row_no: requestSnapshot.request_header_row_no || undefined,
			});
		} catch (error) {
			if (!this.isImportInspectionRequestCurrent(dialog, requestSnapshot)) {
				return null;
			}
			throw error;
		}
		if (!this.isImportInspectionRequestCurrent(dialog, requestSnapshot)) {
			return null;
		}
		if (!response) {
			return null;
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
		const detected = response.detected_mapping || {};
		const responseSourceSignature = JSON.stringify([
			requestSnapshot.file_url,
			settings.forceSheet && requestSnapshot.sheet_name
				? requestSnapshot.sheet_name
				: String(response.selected_sheet || ""),
			settings.forceHeader
				? requestSnapshot.header_row_no
				: String(detected.header_row_no || ""),
		]);
		dialog.iaApplyingInspection = true;
		try {
			dialog.set_df_property("sheet_name", "options", ["", ...(response.sheet_names || [])].join("\n"));
			if (!settings.forceSheet || !dialog.get_value("sheet_name")) {
				await dialog.set_value("sheet_name", response.selected_sheet || "");
			}
			const detectedFields = [
				"parser_mode",
				"header_row_no",
				"data_start_row_no",
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
			];
			for (const fieldname of detectedFields) {
				// A forced header is an explicit user choice. All other fields must
				// come from this response so values from the previous layout cannot leak.
				if (settings.forceHeader && fieldname === "header_row_no") {
					continue;
				}
				if (detected[fieldname] !== undefined && detected[fieldname] !== null && detected[fieldname] !== "") {
					await dialog.set_value(fieldname, labelByValue[detected[fieldname]] || detected[fieldname]);
				} else {
					await dialog.set_value(fieldname, "");
				}
			}
		} finally {
			dialog.iaApplyingInspection = false;
		}
		if (
			requestSnapshot.generation !== (dialog.iaInspectionGeneration || 0) ||
			responseSourceSignature !== this.getImportInspectionSourceSignature(dialog)
		) {
			// The file/sheet/header changed while response fields were being applied.
			// Never mark that response as current or allow its mapping into preview.
			dialog.iaInspectionResponse = null;
			dialog.iaInspectionSourceSignature = "";
			return null;
		}
		dialog.iaInspectionResponse = response;
		dialog.iaSourceNeedsAutoDetection = false;
		dialog.iaInspectionSourceSignature = responseSourceSignature;
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
			<div class="ia-import-check ia-import-check-passed" style="margin-bottom:8px;">
				<div class="ia-import-check-body">
					<div class="ia-import-check-title">${__("Workbook layout recognized", null, "Injection APS")}</div>
					<div class="ia-import-check-summary">
						${__("Sheet")}: ${injection_aps.ui.escape(response.selected_sheet || "-")} |
						${__("Parser Mode")}: ${injection_aps.ui.escape(injection_aps.ui.translate(detected.parser_mode || "-"))} |
						${__("Header Row No")}: ${injection_aps.ui.escape(String(detected.header_row_no || "-"))} |
						${__("Date Columns")}: ${injection_aps.ui.escape(String((detected.date_column_letters || []).length || 0))}
					</div>
				</div>
			</div>
			<div class="ia-table-wrap">
				<table class="ia-table">
					<tbody>${htmlRows || `<tr><td>${__("No sample rows found.")}</td></tr>`}</tbody>
				</table>
			</div>
		`);
		this.syncImportDialogLayout(dialog);
		return response;
	}

	async previewImport(values) {
		const scheduleScope = values.schedule_scope || values.version_no;
		const payload = {
			customer: values.customer,
			company: values.company,
			version_no: values.version_no,
			schedule_scope: scheduleScope,
			import_strategy: values.import_strategy || "Replace Scope",
			duplicate_policy: values.duplicate_policy || "Block",
			file_url: values.file_url || undefined,
			rows_json: values.file_url ? undefined : values.rows_json || undefined,
			mapping_json: this.getImportMapping(values) ? JSON.stringify(this.getImportMapping(values)) : undefined,
		};
		let endpoint = "injection_aps.api.app.preview_customer_delivery_schedule";
		if (this.v2Enabled) {
			// V2 uses revision_mode instead of the Legacy import_strategy. Remove
			// it from the canonical pending payload so neither Preview nor Apply
			// sends an argument that the revision APIs do not accept.
			delete payload.import_strategy;
			injection_aps.ui.set_feedback(this.feedback, __("Analyzing schedule revision intent...", null, "Injection APS"));
			const recommendation = await frappe.xcall(
				"injection_aps.api.app.recommend_schedule_revision_mode",
				{
					customer: payload.customer,
					company: payload.company,
					schedule_scope: payload.schedule_scope,
					file_url: payload.file_url,
					rows_json: payload.rows_json,
					mapping_json: payload.mapping_json,
				}
			);
			const confirmation = await this.confirmRevisionMode(recommendation);
			if (!confirmation) {
				injection_aps.ui.set_feedback(this.feedback, __("Revision preview was cancelled before mode confirmation.", null, "Injection APS"), "warning");
				return;
			}
			payload.revision_mode = confirmation.revision_mode;
			payload.mode_confirmation_reason = confirmation.mode_confirmation_reason;
			endpoint = "injection_aps.api.app.preview_schedule_revision";
		}
		const previewPayload = Object.assign({}, payload);
		delete previewPayload.mode_confirmation_reason;
		injection_aps.ui.set_feedback(this.feedback, __("Running import preview..."));
		const preview = await injection_aps.ui.xcall(
			{
				message: __("Previewing customer schedule import..."),
				success_feedback: __("Preview completed. Review changes, then import and rebuild demand."),
				busy_key: `schedule-preview:${payload.customer || ""}:${payload.version_no || ""}`,
				feedback_target: this.feedback,
			},
			endpoint,
			previewPayload
		);
		if (!preview) {
			return;
		}
		const editableRows = this.buildEditablePreviewRows(preview.source_rows || []);
		this.pendingImport = { payload, preview, editableRows };
		this.renderPreview();
		this.renderFlow();
		injection_aps.ui.set_feedback(
			this.feedback,
			__("Step 3 of 3: review the recognized differences, then import when the checks pass.", null, "Injection APS"),
			"warning"
		);
		return preview;
	}

	confirmRevisionMode(recommendation) {
		return new Promise((resolve) => {
			let resolved = false;
			const recommendedMode = recommendation.recommended_mode || "Full Replacement";
			const dialog = new frappe.ui.Dialog({
				title: __("Confirm Schedule Revision Mode", null, "Injection APS"),
				fields: [
					{
						fieldname: "recommendation_html",
						fieldtype: "HTML",
						options: `
							<div class="ia-import-check ia-import-check-notice">
								<div class="ia-import-check-body">
									<div class="ia-import-check-title">${__("APS Recommendation", null, "Injection APS")}: ${injection_aps.ui.escape(injection_aps.ui.translate(recommendedMode))}</div>
									<div class="ia-import-check-summary">${injection_aps.ui.escape(recommendation.reason || "")}</div>
									<div class="ia-muted">${__("Confidence", null, "Injection APS")}: ${injection_aps.ui.escape(injection_aps.ui.translate(recommendation.confidence || "-"))}</div>
								</div>
							</div>`,
					},
					{
						fieldname: "revision_mode",
						fieldtype: "Select",
						label: __("Revision Mode", null, "Injection APS"),
						options: ["Full Replacement", "Partial Revision", "Incremental Demand"].join("\n"),
						default: recommendedMode,
						reqd: 1,
						change: () => {
							const differs = dialog.get_value("revision_mode") !== recommendedMode;
							dialog.set_df_property("mode_confirmation_reason", "reqd", differs ? 1 : 0);
							dialog.set_df_property("mode_confirmation_reason", "description", differs
								? __("Required because your selection differs from the APS recommendation.", null, "Injection APS")
								: __("Optional note for the audit trail.", null, "Injection APS"));
						},
					},
					{
						fieldname: "mode_confirmation_reason",
						fieldtype: "Small Text",
						label: __("Confirmation Reason", null, "Injection APS"),
						description: __("Optional note for the audit trail.", null, "Injection APS"),
					},
				],
				primary_action_label: __("Confirm Mode and Preview", null, "Injection APS"),
				primary_action: (values) => {
					const mode = values.revision_mode;
					const reason = String(values.mode_confirmation_reason || "").trim();
					if (mode !== recommendedMode && !reason) {
						frappe.msgprint(__("Explain why the selected revision mode differs from the APS recommendation.", null, "Injection APS"));
						return;
					}
					resolved = true;
					dialog.hide();
					resolve({ revision_mode: mode, mode_confirmation_reason: reason });
				},
			});
			dialog.$wrapper.on("hidden.bs.modal", () => {
				if (!resolved) {
					resolved = true;
					resolve(null);
				}
			});
			dialog.show();
		});
	}

	async importPending(rebuildNextStep) {
		if (!this.pendingImport) {
			frappe.show_alert({ message: __("No pending preview to import."), indicator: "orange" });
			return;
		}
		const canImport = this.v2Enabled ? this.pendingImport.preview.can_apply : this.pendingImport.preview.can_import;
		if (!canImport) {
			frappe.show_alert({ message: __("Resolve all import checks before importing."), indicator: "red" });
			return;
		}
		const confirmationAction = { action_key: rebuildNextStep ? "import_and_promote" : "import_only", confirm_required: 1 };
		const confirmationOptions = {
			title: rebuildNextStep ? __("Confirm Import and Rebuild") : __("Confirm Import"),
			summary_lines: [
				__("Customer: {0}").replace("{0}", this.pendingImport.payload.customer || "-"),
				__("Company: {0}").replace("{0}", this.pendingImport.payload.company || "-"),
				__("Schedule Scope: {0}").replace("{0}", this.pendingImport.payload.schedule_scope || "-"),
				__("Version: {0}").replace("{0}", this.pendingImport.payload.version_no || "-"),
				this.v2Enabled
					? __("Revision Mode: {0}", null, "Injection APS").replace("{0}", injection_aps.ui.translate(this.pendingImport.payload.revision_mode || "-"))
					: __("Import Strategy: {0}").replace("{0}", injection_aps.ui.translate(this.pendingImport.payload.import_strategy || "-")),
				rebuildNextStep ? __("This will formally import the schedule and rebuild demand / net requirements.") : __("This will formally import the current schedule version."),
			],
		};
		let existingWorkOrderPolicy = null;
		let confirmed = false;
		if (rebuildNextStep) {
			existingWorkOrderPolicy = await injection_aps.ui.confirm_net_requirement_calculation(
				confirmationAction,
				confirmationOptions
			);
			confirmed = Boolean(existingWorkOrderPolicy);
		} else {
			confirmed = await injection_aps.ui.confirm_action(confirmationAction, confirmationOptions);
		}
		if (!confirmed) {
			return;
		}
		let response;
		try {
			response = await injection_aps.ui.with_busy(
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
						const confirmedRows = this.buildEditablePreviewRows(
							this.pendingImport.editableRows || this.pendingImport.preview.source_rows || []
						);
						const importPayload = Object.assign({}, this.pendingImport.payload, {
							// Import the exact editable rows the user confirmed.  Keep file_url in
							// the payload only as audit provenance so the customer lock is not held
							// while the server re-opens and expands a large workbook.
							rows_json: JSON.stringify(confirmedRows),
							active_state_token: this.pendingImport.preview.active_state_token || undefined,
							expected_import_fingerprint: this.pendingImport.preview.import_fingerprint || undefined,
						});
					let imported;
					if (this.v2Enabled) {
						delete importPayload.active_state_token;
						delete importPayload.expected_import_fingerprint;
						importPayload.confirmed_revision_mode = importPayload.revision_mode;
						importPayload.expected_active_state_token = this.pendingImport.preview.active_state_token || undefined;
						importPayload.expected_revision_fingerprint = this.pendingImport.preview.revision_fingerprint || undefined;
						importPayload.rebuild = rebuildNextStep ? 1 : 0;
						importPayload.existing_work_order_policy = existingWorkOrderPolicy || undefined;
						delete importPayload.revision_mode;
						imported = await frappe.xcall("injection_aps.api.app.apply_schedule_revision", importPayload);
					} else {
						importPayload.rebuild = rebuildNextStep ? 1 : 0;
						importPayload.existing_work_order_policy = existingWorkOrderPolicy || undefined;
						imported = await frappe.xcall(
							"injection_aps.api.app.import_customer_delivery_schedule",
							importPayload
						);
					}
					if (imported.promotion) {
						const promotion = imported.promotion;
						injection_aps.ui.show_warnings(promotion.demand_pool, __("Demand Pool Warnings"), "warning_count");
						injection_aps.ui.show_warnings(promotion.net_requirement, __("Net Requirement Warnings"), "warning_count");
					}
					return imported;
				}
			);
		} catch (error) {
			const message = (error && error.message) || __("Schedule import failed. Refresh Preview and try again.");
			injection_aps.ui.set_feedback(this.feedback, message, "error");
			frappe.msgprint({
				title: __("Schedule Import Failed"),
				message: injection_aps.ui.escape(message),
				indicator: "red",
			});
			this.renderFlow();
			return;
		}
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
			customer_code: row.customer_code || "",
			item_name: row.item_name || "",
			customer_part_no: row.customer_part_no || "",
			external_line_reference: row.external_line_reference || "",
			demand_identity: row.demand_identity || "",
			identity_resolution_reason: row.identity_resolution_reason || "",
			schedule_date: row.schedule_date || "",
			previous_schedule_date: row.previous_schedule_date || "",
			qty: Number(row.import_qty != null ? row.import_qty : row.qty || 0),
			production_strategy: row.production_strategy || "Auto Balance",
			demand_confidence: row.demand_confidence || "Confirmed",
			cancellation_risk_percent: Number(row.cancellation_risk_percent || 0),
			prebuild_allowed: row.prebuild_allowed == null ? 1 : (row.prebuild_allowed ? 1 : 0),
			max_prebuild_days: Number(row.max_prebuild_days || 0),
			remark: row.remark || "",
			source_origin: row.source_origin || "imported",
			source_excel_row: row.source_excel_row || "",
			source_excel_rows: row.source_excel_rows || "",
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
		const endpoint = this.v2Enabled
			? "injection_aps.api.app.preview_schedule_revision"
			: "injection_aps.api.app.preview_customer_delivery_schedule";
		const previewPayload = Object.assign({}, this.pendingImport.payload);
		delete previewPayload.mode_confirmation_reason;
		const preview = await injection_aps.ui.xcall(
			{
				message: __("Refreshing import preview..."),
				success_feedback: feedbackMessage || __("Preview updated."),
				busy_key: `schedule-preview-refresh:${this.pendingImport.payload.customer || ""}:${this.pendingImport.payload.version_no || ""}`,
				feedback_target: this.feedback,
			},
			endpoint,
			previewPayload
		);
		if (!preview) {
			return;
		}
		this.pendingImport.preview = preview;
		this.pendingImport.editableRows = this.buildEditablePreviewRows(preview.source_rows || []);
		this.pendingImport.payload.schedule_scope = preview.schedule_scope || this.pendingImport.payload.schedule_scope;
		if (this.v2Enabled) {
			this.pendingImport.payload.revision_mode = preview.revision_mode || this.pendingImport.payload.revision_mode;
		} else {
			this.pendingImport.payload.import_strategy = preview.import_strategy || this.pendingImport.payload.import_strategy;
		}
		this.pendingImport.payload.rows_json = JSON.stringify(
			this.pendingImport.editableRows.map(({ customer_code, item_name, ...row }) => row)
		);
		this.renderPreview();
	}

	renderPreviewEditor(rows) {
		const previewRows = rows || [];
		const columns = [
			{ label: __("Seq"), fieldname: "line_idx" },
			{ label: __("Excel Rows", null, "Injection APS"), fieldname: "source_excel_rows" },
			{ label: __("Sales Order", null, "Injection APS"), fieldname: "sales_order" },
			{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
			{ label: __("Part No"), fieldname: "customer_part_no" },
			{ label: __("Strategy", null, "Injection APS"), fieldname: "production_strategy" },
			{ label: __("Demand Confidence", null, "Injection APS"), fieldname: "demand_confidence" },
			{ label: __("Cancellation Risk", null, "Injection APS"), fieldname: "cancellation_risk_percent" },
			{ label: __("Prebuild Allowed", null, "Injection APS"), fieldname: "prebuild_allowed" },
			{ label: __("Max Prebuild Days", null, "Injection APS"), fieldname: "max_prebuild_days" },
			{ label: __("Previous Date", null, "Injection APS"), fieldname: "previous_schedule_date" },
			{ label: __("New Date", null, "Injection APS"), fieldname: "schedule_date" },
			{ label: __("Previous Qty"), fieldname: "previous_qty" },
			{ label: __("New Qty", null, "Injection APS"), fieldname: "new_qty" },
			{ label: __("Delta", null, "Injection APS"), fieldname: "delta_qty" },
			{ label: __("Change", null, "Injection APS"), fieldname: "change_type" },
			{ label: __("Execution Impact", null, "Injection APS"), fieldname: "execution_impact" },
		];
		if (!previewRows.length) {
			this.previewTable.innerHTML = `
				<div class="ia-table-toolbar">
					${injection_aps.ui.icon_button("download", __("Export Excel", null, "Injection APS"), { "data-ia-preview-export": "1" })}
					${injection_aps.ui.icon_button("plus", __("Add Row", null, "Injection APS"), { "data-ia-preview-add": "1" })}
				</div>
				<div class="ia-table-shell"><div class="ia-muted ia-empty">${__("No rows found.")}</div></div>
			`;
			this.bindPreviewToolbar(previewRows, columns);
			return;
		}
		const body = previewRows
			.map((row, index) => {
				const tone = ["Cancelled", "Reduced", "Delayed", "Duplicate Blocked", "Validation Blocked"].includes(row.change_type)
					? "red"
					: ["Advanced", "Added", "Appended", "Increased"].includes(row.change_type)
						? "orange"
						: "green";
				const displayLineIndex = row.line_idx || index + 1;
				const displayExcelRows = row.source_excel_rows || row.source_excel_row || "";
				const sourceIndex = this.findEditableSourceIndex(row);
				return `
					<tr data-preview-index="${index}" data-source-index="${sourceIndex}">
						<td class="ia-col-seq">${injection_aps.ui.escape(String(displayLineIndex))}</td>
						<td class="ia-col-excel-row">${injection_aps.ui.escape(String(displayExcelRows))}</td>
						<td>${injection_aps.ui.escape(row.sales_order || "")}</td>
						<td>${injection_aps.ui.item_identity(row)}</td>
						<td>${injection_aps.ui.escape(row.customer_part_no || "")}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.production_strategy || "Auto Balance"))}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.demand_confidence || "Confirmed"))}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.cancellation_risk_percent || 0, 2))}%</td>
						<td>${row.prebuild_allowed ? __("Yes", null, "Injection APS") : __("No", null, "Injection APS")}</td>
						<td>${injection_aps.ui.escape(String(row.max_prebuild_days || 0))}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.format_date(row.previous_schedule_date))}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.format_date(row.schedule_date))}</td>
						<td>${frappe.format(row.previous_qty || 0, { fieldtype: "Float" })}</td>
						<td>${frappe.format(row.new_qty || 0, { fieldtype: "Float" })}</td>
						<td>${frappe.format(row.delta_qty || 0, { fieldtype: "Float" })}</td>
						<td>${injection_aps.ui.pill(injection_aps.ui.translate(row.change_type), tone)}</td>
						<td>${injection_aps.ui.escape(row.execution_impact || __("None", null, "Injection APS"))}</td>
					</tr>
				`;
			})
			.join("");
		this.previewTable.innerHTML = `
			<div class="ia-table-toolbar">
				${injection_aps.ui.icon_button("download", __("Export Excel", null, "Injection APS"), { "data-ia-preview-export": "1" })}
				${injection_aps.ui.icon_button("plus", __("Add Row", null, "Injection APS"), { "data-ia-preview-add": "1" })}
			</div>
			<div class="ia-table-shell">
				<table class="ia-table">
					<thead>
						<tr>
							${columns
								.map((column) => {
									const className = column.fieldname === "line_idx"
										? "ia-col-seq"
										: column.fieldname === "source_excel_rows"
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
				const rowIndex = Number(rowNode.dataset.sourceIndex);
				if (rowIndex < 0) {
					return;
				}
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

	findEditableSourceIndex(row) {
		const rows = this.getEditablePreviewRows();
		const excelRows = String(row.source_excel_rows || row.source_excel_row || "");
		return rows.findIndex((source) => {
			if (excelRows && String(source.source_excel_rows || source.source_excel_row || "") === excelRows) {
				return true;
			}
			return (
				(source.sales_order || "") === (row.sales_order || "") &&
				(source.item_code || "") === (row.item_code || "") &&
				(source.customer_part_no || "") === (row.customer_part_no || "") &&
				(source.schedule_date || "") === (row.schedule_date || "")
			);
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
						if (["schedule_date", "previous_schedule_date"].includes(column.fieldname)) {
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
				{ fieldname: "sales_order", fieldtype: "Data", label: __("Sales Order", null, "Injection APS"), default: row.sales_order || "" },
				{ fieldname: "item_code", fieldtype: "Data", label: __("Item", null, "Injection APS"), reqd: 1, default: row.item_code || "" },
				{ fieldname: "customer_part_no", fieldtype: "Data", label: __("Part No"), default: row.customer_part_no || "" },
				{
					fieldname: "production_strategy",
					fieldtype: "Select",
					label: __("Production Strategy", null, "Injection APS"),
					options: ["Auto Balance", "Force Prebuild", "Force JIT"].join("\n"),
					context: "Injection APS",
					default: row.production_strategy || "Auto Balance",
					reqd: 1,
				},
				{
					fieldname: "demand_confidence",
					fieldtype: "Select",
					label: __("Demand Confidence", null, "Injection APS"),
					options: ["Confirmed", "Forecast"].join("\n"),
					context: "Injection APS",
					default: row.demand_confidence || "Confirmed",
					reqd: 1,
				},
				{
					fieldname: "cancellation_risk_percent",
					fieldtype: "Percent",
					label: __("Cancellation Risk Percent", null, "Injection APS"),
					default: Number(row.cancellation_risk_percent || 0),
				},
				{
					fieldname: "prebuild_allowed",
					fieldtype: "Check",
					label: __("Prebuild Allowed", null, "Injection APS"),
					default: row.prebuild_allowed == null ? 1 : Number(row.prebuild_allowed),
				},
				{
					fieldname: "max_prebuild_days",
					fieldtype: "Int",
					label: __("Max Prebuild Days", null, "Injection APS"),
					default: Number(row.max_prebuild_days || 0),
				},
				{ fieldname: "schedule_date", fieldtype: "Date", label: __("Schedule Date", null, "Injection APS"), reqd: 1, default: row.schedule_date || "" },
				{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), reqd: 1, default: row.qty || 0 },
				{ fieldname: "remark", fieldtype: "Small Text", label: __("Remark", null, "Injection APS"), default: row.remark || "" },
				{ fieldname: "manual_change_reason", fieldtype: "Small Text", label: __("Manual Change Reason"), reqd: 1, default: row.manual_change_reason || "" },
			],
			primary_action_label: isNew ? __("Add Row", null, "Injection APS") : __("Update Row"),
			primary_action: async (values) => {
				const updated = Object.assign({}, row, {
					sales_order: values.sales_order || "",
					item_code: values.item_code || "",
					customer_part_no: values.customer_part_no || "",
					production_strategy: values.production_strategy || "Auto Balance",
					demand_confidence: values.demand_confidence || "Confirmed",
					cancellation_risk_percent: Number(values.cancellation_risk_percent || 0),
					prebuild_allowed: values.prebuild_allowed ? 1 : 0,
					max_prebuild_days: Number(values.max_prebuild_days || 0),
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
