frappe.pages["aps-schedule-console"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSScheduleConsole(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-schedule-console"].on_page_show = function (wrapper) {
	wrapper.injection_aps_controller?.refresh();
};

class InjectionAPSScheduleConsole {
	constructor(wrapper) {
		this.wrapper = wrapper;
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
					<p>${__("Preview -> formal import -> rebuild demand pool / net requirement. Keep only one active version for each customer and push the planner directly to the next step.")}</p>
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
				{ label: __("Active Versions"), value: data.summary.active_versions || 0, note: __("One active version per customer / company") },
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
					{ label: __("正式导入并重建需求池"), action_key: "import_and_promote", enabled: 1 },
					{ label: __("仅正式导入"), action_key: "import_only", enabled: 1 },
					{ label: __("进入净需求工作台"), action_key: "open_net_requirement", enabled: this.lastImported ? 1 : 0, route: "aps-net-requirement-workbench" },
				],
			}
			: {
				current_step: this.lastImported ? __("Imported") : __("Waiting For Preview"),
				next_step: this.lastImported ? __("Open Net Requirement Workbench") : __("Preview Import"),
				blocking_reason: "",
				actions: [
					{ label: __("预览导入"), action_key: "preview", enabled: 1 },
					{ label: __("进入净需求工作台"), action_key: "open_net_requirement", enabled: 1, route: "aps-net-requirement-workbench" },
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
				{ label: __("Version"), fieldname: "version_no" },
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
				{ label: __("Version"), fieldname: "version_no" },
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
				if (column.fieldname === "next_step") {
					return injection_aps.ui.escape(nextActions?.[row.name]?.next_step || "");
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
		const preview = this.pendingImport?.preview;
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
		injection_aps.ui.render_cards(this.previewSummary, [
			{ label: __("Customer"), value: preview.customer || "-" },
			{ label: __("Version"), value: preview.version_no || "-" },
			{ label: __("Rows"), value: preview.row_count || 0 },
			{ label: __("Changes"), value: summaryRows.length || 0, note: summaryRows.map((row) => `${row.label}:${row.value}`).join(" | ") },
		]);
		injection_aps.ui.render_table(
			this.previewTable,
			[
				{ label: __("Sales Order"), fieldname: "sales_order" },
				{ label: __("Item"), fieldname: "item_code" },
				{ label: __("Part No"), fieldname: "customer_part_no" },
				{ label: __("Schedule Date"), fieldname: "schedule_date" },
				{ label: __("Qty"), fieldname: "qty" },
				{ label: __("Prev Qty"), fieldname: "previous_qty" },
				{ label: __("Change"), fieldname: "change_type" },
			],
			preview.rows || [],
			(column, value) => {
				if (column.fieldname === "change_type") {
					const tone = ["Cancelled", "Reduced", "Delayed"].includes(value)
						? "red"
						: ["Advanced", "Added", "Increased"].includes(value)
							? "orange"
							: "green";
					return injection_aps.ui.pill(injection_aps.ui.translate(value), tone);
				}
				if (["qty", "previous_qty"].includes(column.fieldname)) {
					return frappe.format(value || 0, { fieldtype: "Float" });
				}
				if (column.fieldname === "schedule_date") {
					return injection_aps.ui.format_date(value);
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Schedule Import Preview"),
				export_sheet_name: __("Preview Diff"),
				export_file_name: "aps_schedule_preview",
				export_subtitle: __("Preview rows before formally importing customer schedule data."),
			}
		);
	}

	openPreviewDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Preview Customer Schedule Import"),
			fields: [
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer"), reqd: 1 },
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company"), reqd: 1, default: frappe.defaults.get_user_default("Company") },
				{ fieldname: "version_no", fieldtype: "Data", label: __("Version No"), reqd: 1 },
				{ fieldname: "file_url", fieldtype: "Attach", label: __("Excel File") },
				{ fieldname: "rows_json", fieldtype: "Small Text", label: __("Rows JSON"), description: __("Optional. Paste JSON rows when no Excel file is available.") },
			],
			primary_action_label: __("Preview"),
			primary_action: async (values) => {
				await this.previewImport(values);
				dialog.hide();
			},
		});
		dialog.show();
	}

	async previewImport(values) {
		const payload = {
			customer: values.customer,
			company: values.company,
			version_no: values.version_no,
			file_url: values.file_url || undefined,
			rows_json: values.rows_json || undefined,
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
		this.pendingImport = { payload, preview };
		this.renderPreview();
		this.renderFlow();
		injection_aps.ui.set_feedback(this.feedback, __("Preview completed. Review changes, then import and rebuild demand."), "warning");
	}

	async importPending(rebuildNextStep) {
		if (!this.pendingImport) {
			frappe.show_alert({ message: __("No pending preview to import."), indicator: "orange" });
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
}
