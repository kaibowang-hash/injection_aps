frappe.pages["aps-customer-schedule-progress"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSCustomerScheduleProgress(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-customer-schedule-progress"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSCustomerScheduleProgress {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.suppressFilterRefresh = false;
		this.rows = [];
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Customer Schedule Progress"),
			single_column: true,
		});
		this.companyField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "company",
			options: "Company",
			label: __("Company"),
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.refreshFromFilter(),
		});
		this.customerField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "customer",
			options: "Customer",
			label: __("Customer"),
			change: () => this.refreshFromFilter(),
		});
		this.itemField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "item_code",
			options: "Item",
			label: __("Item"),
			change: () => this.refreshFromFilter(),
		});
		this.scopeField = this.page.add_field({
			fieldtype: "Data",
			fieldname: "schedule_scope",
			label: __("Schedule Scope"),
			change: () => this.refreshFromFilter(),
		});
		this.fromField = this.page.add_field({
			fieldtype: "Date",
			fieldname: "date_from",
			label: __("From", null, "Injection APS"),
			change: () => this.refreshFromFilter(),
		});
		this.toField = this.page.add_field({
			fieldtype: "Date",
			fieldname: "date_to",
			label: __("To", null, "Injection APS"),
			change: () => this.refreshFromFilter(),
		});
		this.statusField = this.page.add_field({
			fieldtype: "Select",
			fieldname: "status",
			label: __("Status"),
			options: ["", "Delivered", "Stock Covered", "On Track", "At Risk", "Late", "Uncovered"].join("\n"),
			change: () => this.refreshFromFilter(),
		});
		this.runField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "run_name",
			options: "APS Planning Run",
			label: __("APS Run"),
			default: injection_aps.ui.get_query_param("run_name") || undefined,
			change: () => this.refreshFromFilter(),
		});
		this.page.set_primary_action(__("Refresh"), () => this.refresh());
		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Customer Schedule Progress")}</h3>
					<p>${__("Customer delivery rows are matched against available stock, actual execution, and the selected APS run projection.")}</p>
				</div>
				<div class="ia-status-host"></div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-feedback"></div>
				<div class="ia-panel">
					<div class="ia-table-target"></div>
				</div>
			</div>
		`);
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.table = this.page.main.find(".ia-table-target")[0];
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading customer schedule progress..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_customer_schedule_progress_data", this.getFilters());
			this.data = data || {};
			this.rows = (this.data.rows || []).map((row, index) => Object.assign({ _row_no: index + 1 }, row));
			this.renderRunStatus(this.data.selected_run || null, this.data.truncated);
			this.renderSummary(this.data.summary || {});
			this.renderTable(this.rows);
			injection_aps.ui.set_feedback(this.feedback, __("Customer schedule progress refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load customer schedule progress."), "error");
		}
	}

	refreshFromFilter() {
		if (!this.suppressFilterRefresh) {
			this.refresh();
		}
	}

	getFilters() {
		return {
			company: this.companyField.get_value() || undefined,
			customer: this.customerField.get_value() || undefined,
			item_code: this.itemField.get_value() || undefined,
			schedule_scope: this.scopeField.get_value() || undefined,
			date_from: this.fromField.get_value() || undefined,
			date_to: this.toField.get_value() || undefined,
			status: this.statusField.get_value() || undefined,
			run_name: this.runField.get_value() || undefined,
			limit: 1000,
		};
	}

	renderRunStatus(selectedRun, truncated) {
		const runLabel = selectedRun && selectedRun.name ? selectedRun.name : __("None");
		const blockingReason = truncated ? __("Rows were truncated by the current page limit.") : "";
		injection_aps.ui.render_status_line(this.statusHost, {
			current_step: selectedRun && selectedRun.name ? __("Using APS Run {0}").replace("{0}", runLabel) : __("No APS Run"),
			next_step: selectedRun && selectedRun.status ? selectedRun.status : __("Select or create APS Run"),
			blocking_reason: blockingReason,
		});
	}

	renderSummary(summary) {
		injection_aps.ui.render_cards(this.summary, [
			{ label: __("Rows"), value: summary.rows || 0 },
			{ label: __("Schedule Qty"), value: injection_aps.ui.format_number(summary.required_qty || 0) },
			{ label: __("Stock Covered"), value: injection_aps.ui.format_number(summary.stock_covered_qty || 0) },
			{ label: __("Production Covered"), value: injection_aps.ui.format_number(summary.production_covered_qty || 0) },
			{ label: __("Uncovered"), value: injection_aps.ui.format_number(summary.uncovered_qty || 0) },
			{ label: __("Risk / Late"), value: `${summary.risk_rows || 0} / ${summary.late_rows || 0}` },
		]);
	}

	renderTable(rows) {
		const columns = [
			{ label: __("No."), fieldname: "_row_no", fieldtype: "Int", className: "ia-col-seq" },
			{ label: __("Customer"), fieldname: "customer" },
			{ label: __("Schedule", null, "Injection APS"), fieldname: "schedule" },
			{ label: __("Version"), fieldname: "version_no" },
			{ label: __("Item"), fieldname: "item_code" },
			{ label: __("Customer Part No"), fieldname: "customer_part_no" },
			{ label: __("Delivery Date"), fieldname: "schedule_date" },
			{ label: __("Qty"), fieldname: "required_qty", fieldtype: "Float" },
			{ label: __("Delivered", null, "Injection APS"), fieldname: "delivered_qty", fieldtype: "Float" },
			{ label: __("Stock"), fieldname: "stock_covered_qty", fieldtype: "Float" },
			{ label: __("Production"), fieldname: "production_covered_qty", fieldtype: "Float" },
			{ label: __("Uncovered"), fieldname: "uncovered_qty", fieldtype: "Float" },
			{ label: __("Projected Done"), fieldname: "projected_completion_time" },
			{ label: __("Variance Hrs"), fieldname: "variance_hours", fieldtype: "Float" },
			{ label: __("Status"), fieldname: "status" },
			{ label: __("Risk Reason"), fieldname: "risk_reason" },
			{ label: __("APS Run"), fieldname: "selected_run" },
			{ label: __("APS Result"), fieldname: "result_names" },
			{ label: __("Board"), fieldname: "gantt_link" },
		];
		injection_aps.ui.render_table(
			this.table,
			columns,
			rows,
			(column, value, row) => this.formatCell(column, value, row),
			{
				exportable: true,
				export_title: __("Customer Schedule Progress"),
				export_sheet_name: __("Schedule Progress"),
				export_file_name: "aps_customer_schedule_progress",
				export_subtitle: __("Customer schedule rows matched with stock, actual execution, and APS projection."),
				row_context_menu: (row) => this.getRowContextMenu(row),
			}
		);
	}

	formatCell(column, value, row) {
		if (column.fieldname === "_row_no") {
			return injection_aps.ui.escape(String(value || ""));
		}
		if (column.fieldname === "schedule") {
			return injection_aps.ui.doc_link("Customer Delivery Schedule", value, row.version_no || value);
		}
		if (column.fieldname === "item_code" && value) {
			return injection_aps.ui.doc_link("Item", value);
		}
		if (column.fieldname === "schedule_date") {
			return injection_aps.ui.format_date(value);
		}
		if (["required_qty", "delivered_qty", "stock_covered_qty", "production_covered_qty", "uncovered_qty"].includes(column.fieldname)) {
			return injection_aps.ui.escape(injection_aps.ui.format_number(value || 0));
		}
		if (column.fieldname === "projected_completion_time") {
			return injection_aps.ui.format_datetime(value);
		}
		if (column.fieldname === "variance_hours") {
			return value === null || value === undefined || value === "" ? "" : injection_aps.ui.escape(injection_aps.ui.format_number(value, 2));
		}
		if (column.fieldname === "status") {
			return injection_aps.ui.pill(injection_aps.ui.translate(value), this.getStatusTone(value));
		}
		if (column.fieldname === "risk_reason") {
			return `<span title="${injection_aps.ui.escape(injection_aps.ui.translate(value || ""))}">${injection_aps.ui.escape(injection_aps.ui.shorten(injection_aps.ui.translate(value || ""), 96))}</span>`;
		}
		if (column.fieldname === "selected_run") {
			return value ? injection_aps.ui.doc_link("APS Planning Run", value) : "";
		}
		if (column.fieldname === "result_names") {
			const names = value || [];
			if (!names.length) {
				return "";
			}
			const links = names.slice(0, 2).map((name) => injection_aps.ui.doc_link("APS Schedule Result", name));
			if (names.length > 2) {
				links.push(`<span class="ia-muted">+${names.length - 2}</span>`);
			}
			return links.join(" ");
		}
		if (column.fieldname === "gantt_link") {
			const route = row.routes && row.routes.gantt;
			return route ? injection_aps.ui.route_link(__("Board"), route) : "";
		}
		return injection_aps.ui.escape(value);
	}

	getStatusTone(status) {
		if (status === "Delivered" || status === "Stock Covered" || status === "On Track") {
			return "green";
		}
		if (status === "At Risk" || status === "Uncovered") {
			return "orange";
		}
		if (status === "Late") {
			return "red";
		}
		return "blue";
	}

	getRowContextMenu(row) {
		if (!row) {
			return [];
		}
		const items = [];
		if (row.schedule) {
			items.push({
				label: __("Open Schedule"),
				icon: "external-link",
				handler: () => frappe.set_route("Form", "Customer Delivery Schedule", row.schedule),
			});
		}
		if (row.selected_run) {
			items.push({
				label: __("Open APS Run"),
				icon: "external-link",
				handler: () => frappe.set_route("Form", "APS Planning Run", row.selected_run),
			});
		}
		(row.result_names || []).slice(0, 3).forEach((name) => {
			items.push({
				label: __("Open APS Result {0}").replace("{0}", name),
				icon: "external-link",
				handler: () => frappe.set_route("Form", "APS Schedule Result", name),
			});
		});
		if (row.routes && row.routes.gantt) {
			items.push({
				label: __("Open Board"),
				icon: "external-link",
				handler: () => injection_aps.ui.go_to(row.routes.gantt),
			});
		}
		return items;
	}
}
