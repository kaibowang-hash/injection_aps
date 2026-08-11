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
		this.refreshGeneration = 0;
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
			label: __("Company", null, "Injection APS"),
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.refreshFromFilter(),
		});
		this.customerField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "customer",
			options: "Customer",
			label: __("Customer", null, "Injection APS"),
			change: () => this.refreshFromFilter(),
		});
		this.itemField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "item_code",
			options: "Item",
			label: __("Item", null, "Injection APS"),
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
			label: __("Status", null, "Injection APS"),
			options: ["", "Delivered", "Stock Covered", "On Track", "At Risk", "Late", "Uncovered"].join("\n"),
			context: "Injection APS",
			change: () => this.refreshFromFilter(),
		});
		this.runField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "run_name",
			options: "APS Planning Run",
			label: __("APS Run", null, "Injection APS"),
			default: injection_aps.ui.get_query_param("run_name") || undefined,
			change: () => this.refreshFromFilter(),
		});
		this.page.set_primary_action(__("Refresh", null, "Injection APS"), () => this.refresh());
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
		const refreshGeneration = ++this.refreshGeneration;
		const filters = this.getFilters();
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading customer schedule progress..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_customer_schedule_progress_data", filters);
			if (refreshGeneration !== this.refreshGeneration) {
				return;
			}
			this.data = data || {};
			this.rows = (this.data.rows || []).map((row, index) => Object.assign({ _row_no: index + 1 }, row));
			this.renderRunStatus(this.data.selected_run || null, this.data.truncated);
			this.renderSummary(this.data.summary || {});
			this.renderTable(this.rows);
			injection_aps.ui.set_feedback(this.feedback, __("Customer schedule progress refreshed."));
		} catch (error) {
			if (refreshGeneration !== this.refreshGeneration) {
				return;
			}
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
		const runLabel = selectedRun && selectedRun.name ? selectedRun.name : __("None", null, "Injection APS");
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
			{ label: __("Actual Good", null, "Injection APS"), value: injection_aps.ui.format_number(summary.actual_good_qty || 0) },
			{ label: __("Current Deliverable", null, "Injection APS"), value: injection_aps.ui.format_number(summary.current_deliverable_qty || 0) },
			{ label: __("Delivered", null, "Injection APS"), value: injection_aps.ui.format_number(summary.delivered_qty || 0) },
			{ label: __("Stock Covered"), value: injection_aps.ui.format_number(summary.stock_covered_qty || 0) },
			{ label: __("Production Covered"), value: injection_aps.ui.format_number(summary.production_covered_qty || 0) },
			{ label: __("Prebuild / JIT", null, "Injection APS"), value: `${injection_aps.ui.format_number(summary.prebuild_qty || 0)} / ${injection_aps.ui.format_number(summary.jit_qty || 0)}` },
			{ label: __("Cancel Stock Risk", null, "Injection APS"), value: injection_aps.ui.format_number(summary.cancellation_inventory_risk_qty || 0) },
			{ label: __("Uncovered"), value: injection_aps.ui.format_number(summary.uncovered_qty || 0) },
			{ label: __("Risk / Late"), value: `${summary.risk_rows || 0} / ${summary.late_rows || 0}` },
		]);
	}

	renderTable(rows) {
		const columns = [
			{ label: __("Customer / Item", null, "Injection APS"), fieldname: "identity_summary" },
			{ label: __("Delivery Date", null, "Injection APS"), fieldname: "schedule_date" },
			{ label: __("Demand", null, "Injection APS"), fieldname: "required_qty", fieldtype: "Float" },
			{ label: __("Actual Good", null, "Injection APS"), fieldname: "actual_good_qty", fieldtype: "Float" },
			{ label: __("Current Deliverable", null, "Injection APS"), fieldname: "current_deliverable_qty", fieldtype: "Float" },
			{ label: __("Delivered", null, "Injection APS"), fieldname: "delivered_qty", fieldtype: "Float" },
			{ label: __("Uncovered"), fieldname: "uncovered_qty", fieldtype: "Float" },
			{ label: __("Risk", null, "Injection APS"), fieldname: "risk_summary" },
			{ label: __("Actions", null, "Injection APS"), fieldname: "actions_html", exportable: false },
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
				export_columns: this.getExportColumns(),
				row_context_menu: (row) => this.getRowContextMenu(row),
				after_render: () => this.bindRowActions(),
			}
		);
	}

	getExportColumns() {
		return [
			{ label: __("No.", null, "Injection APS"), fieldname: "_row_no", fieldtype: "Int" },
			{ label: __("Company", null, "Injection APS"), fieldname: "company" },
			{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
			{ label: __("Schedule", null, "Injection APS"), fieldname: "schedule" },
			{ label: __("Schedule Item", null, "Injection APS"), fieldname: "schedule_item" },
			{ label: __("Version", null, "Injection APS"), fieldname: "version_no" },
			{ label: __("Schedule Scope"), fieldname: "schedule_scope" },
			{ label: __("Source Type", null, "Injection APS"), fieldname: "source_type" },
			{ label: __("Sales Order", null, "Injection APS"), fieldname: "sales_order" },
			{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
			{ label: __("Customer Part No"), fieldname: "customer_part_no" },
			{ label: __("Delivery Date", null, "Injection APS"), fieldname: "schedule_date" },
			{ label: __("Strategy", null, "Injection APS"), fieldname: "production_strategy" },
			{ label: __("Demand Confidence", null, "Injection APS"), fieldname: "demand_confidence" },
			{ label: __("Cancellation Risk %", null, "Injection APS"), fieldname: "cancellation_risk_percent", fieldtype: "Percent" },
			{ label: __("Prebuild Allowed", null, "Injection APS"), fieldname: "prebuild_allowed", fieldtype: "Check" },
			{ label: __("Max Prebuild Days", null, "Injection APS"), fieldname: "max_prebuild_days", fieldtype: "Int" },
			{ label: __("Demand", null, "Injection APS"), fieldname: "required_qty", fieldtype: "Float" },
			{ label: __("Allocated", null, "Injection APS"), fieldname: "allocated_qty", fieldtype: "Float" },
			{ label: __("Prebuild", null, "Injection APS"), fieldname: "prebuild_qty", fieldtype: "Float" },
			{ label: __("JIT", null, "Injection APS"), fieldname: "jit_qty", fieldtype: "Float" },
			{ label: __("Early Days", null, "Injection APS"), fieldname: "early_days", fieldtype: "Float" },
			{ label: __("Actual Good", null, "Injection APS"), fieldname: "actual_good_qty", fieldtype: "Float" },
			{ label: __("Scrap", null, "Injection APS"), fieldname: "scrap_qty", fieldtype: "Float" },
			{ label: __("Current Deliverable", null, "Injection APS"), fieldname: "current_deliverable_qty", fieldtype: "Float" },
			{ label: __("Delivered", null, "Injection APS"), fieldname: "delivered_qty", fieldtype: "Float" },
			{ label: __("Peak Inventory", null, "Injection APS"), fieldname: "projected_peak_inventory_qty", fieldtype: "Float" },
			{ label: __("Prebuild Inventory", null, "Injection APS"), fieldname: "prebuild_inventory_qty", fieldtype: "Float" },
			{ label: __("Late Before / After", null, "Injection APS"), fieldname: "late_balance" },
			{ label: __("Cancel Stock Risk", null, "Injection APS"), fieldname: "cancellation_inventory_risk_qty", fieldtype: "Float" },
			{ label: __("Last Report", null, "Injection APS"), fieldname: "last_actual_report_time" },
			{ label: __("Stock", null, "Injection APS"), fieldname: "stock_covered_qty", fieldtype: "Float" },
			{ label: __("Production", null, "Injection APS"), fieldname: "production_covered_qty", fieldtype: "Float" },
			{ label: __("Uncovered"), fieldname: "uncovered_qty", fieldtype: "Float" },
			{ label: __("Projected Done"), fieldname: "projected_completion_time" },
			{ label: __("Variance Hrs"), fieldname: "variance_hours", fieldtype: "Float" },
			{ label: __("Status", null, "Injection APS"), fieldname: "status" },
			{ label: __("Risk Reason"), fieldname: "risk_reason" },
			{ label: __("APS Run", null, "Injection APS"), fieldname: "selected_run" },
			{ label: __("APS Result"), fieldname: "result_names" },
			{ label: __("Production Sources", null, "Injection APS"), fieldname: "production_source_documents" },
			{ label: __("Delivery Sources", null, "Injection APS"), fieldname: "delivery_source_documents" },
			{ label: __("Remark", null, "Injection APS"), fieldname: "remark" },
			{ label: __("Board"), fieldname: "gantt_link" },
		];
	}

	formatCell(column, value, row) {
		if (column.fieldname === "_row_no") {
			return injection_aps.ui.escape(String(value || ""));
		}
		if (column.fieldname === "identity_summary") {
			const customer = this.safeDocLink("Customer", row.customer);
			const item = this.safeDocLink("Item", row.item_code);
			const customerPart = row.customer_part_no
				? `<span class="ia-muted"> · ${injection_aps.ui.escape(row.customer_part_no)}</span>`
				: "";
			return `<div><div>${customer}</div><div>${item}${customerPart}</div></div>`;
		}
		if (column.fieldname === "schedule") {
			return this.safeDocLink("Customer Delivery Schedule", value, row.version_no || value);
		}
		if (column.fieldname === "item_code" && value) {
			return this.safeDocLink("Item", value);
		}
		if (column.fieldname === "sales_order" && value) {
			return this.safeDocLink("Sales Order", value);
		}
		if (column.fieldname === "schedule_date") {
			return injection_aps.ui.escape(injection_aps.ui.format_date(value));
		}
		if (["required_qty", "allocated_qty", "prebuild_qty", "jit_qty", "early_days", "actual_good_qty", "scrap_qty", "current_deliverable_qty", "delivered_qty", "projected_peak_inventory_qty", "prebuild_inventory_qty", "cancellation_inventory_risk_qty", "stock_covered_qty", "production_covered_qty", "uncovered_qty"].includes(column.fieldname)) {
			return injection_aps.ui.escape(injection_aps.ui.format_number(value || 0));
		}
		if (column.fieldname === "cancellation_risk_percent") {
			return `${injection_aps.ui.escape(injection_aps.ui.format_number(value || 0, 2))}%`;
		}
		if (column.fieldname === "prebuild_allowed") {
			return Number(value || 0) ? __("Yes", null, "Injection APS") : __("No", null, "Injection APS");
		}
		if (column.fieldname === "production_strategy") {
			return injection_aps.ui.pill(injection_aps.ui.translate(value || "Auto Balance"), value === "Force JIT" ? "blue" : value === "Force Prebuild" ? "orange" : "green");
		}
		if (column.fieldname === "late_balance") {
			return `${injection_aps.ui.escape(injection_aps.ui.format_number(row.late_qty_before_balance || 0))} / ${injection_aps.ui.escape(injection_aps.ui.format_number(row.late_qty_after_balance || 0))}`;
		}
		if (column.fieldname === "last_actual_report_time") {
			return injection_aps.ui.escape(injection_aps.ui.format_datetime(value));
		}
		if (column.fieldname === "projected_completion_time") {
			return injection_aps.ui.escape(injection_aps.ui.format_datetime(value));
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
		if (column.fieldname === "risk_summary") {
			const reason = injection_aps.ui.translate(row.risk_reason || "");
			const reasonHtml = reason
				? `<div class="ia-muted" title="${injection_aps.ui.escape(reason)}">${injection_aps.ui.escape(injection_aps.ui.shorten(reason, 52))}</div>`
				: "";
			return `${injection_aps.ui.pill(injection_aps.ui.translate(row.status || ""), this.getStatusTone(row.status))}${reasonHtml}`;
		}
		if (column.fieldname === "selected_run") {
			return value ? this.safeDocLink("APS Planning Run", value) : "";
		}
		if (column.fieldname === "result_names") {
			const names = value || [];
			if (!names.length) {
				return "";
			}
			const links = names.slice(0, 2).map((name) => this.safeDocLink("APS Schedule Result", name));
			if (names.length > 2) {
				links.push(`<span class="ia-muted">+${names.length - 2}</span>`);
			}
			return links.join(" ");
		}
		if (["production_source_documents", "delivery_source_documents"].includes(column.fieldname)) {
			return injection_aps.ui.escape((value || []).join(", "));
		}
		if (column.fieldname === "gantt_link") {
			const route = row.routes && row.routes.gantt;
			return route ? this.safeRouteLink(__("Board"), route) : "";
		}
		if (column.fieldname === "actions_html") {
			const label = __("View Details", null, "Injection APS");
			return `<button type="button" class="btn btn-xs btn-default" data-progress-details="${Number(row._row_no || 0)}" aria-label="${injection_aps.ui.escape(label)}">${injection_aps.ui.escape(label)}</button>`;
		}
		return injection_aps.ui.escape(value);
	}

	bindRowActions() {
		this.table.querySelectorAll("[data-progress-details]").forEach((button) => {
			button.addEventListener("click", () => {
				const rowNo = Number(button.dataset.progressDetails || 0);
				const row = this.rows.find((candidate) => Number(candidate._row_no || 0) === rowNo);
				if (row) {
					this.openDetails(row);
				}
			});
		});
	}

	openDetails(row) {
		const number = (value, digits) => injection_aps.ui.escape(injection_aps.ui.format_number(value || 0, digits));
		const text = (value) => injection_aps.ui.escape(value == null || value === "" ? "-" : value);
		const translated = (value) => text(injection_aps.ui.translate(value || ""));
		const date = (value) => text(value ? injection_aps.ui.format_date(value) : "-");
		const datetime = (value) => text(value ? injection_aps.ui.format_datetime(value) : "-");
		const yesNo = (value) => text(
			Number(value || 0) ? __("Yes", null, "Injection APS") : __("No", null, "Injection APS")
		);
		const sourceLinks = (doctype, names) => {
			const links = (names || []).filter(Boolean).map((name) => this.safeDocLink(doctype, name));
			return links.length ? links.join("<br>") : text("-");
		};
		const resultLinks = (row.result_names || [])
			.filter(Boolean)
			.map((name) => this.safeDocLink("APS Schedule Result", name));
		const boardRoute = row.routes && row.routes.gantt;

		const html = `
			<div style="display:grid; gap:10px;">
				${this.detailSection(__("Demand Identity", null, "Injection APS"), [
					[__("Customer", null, "Injection APS"), this.safeDocLink("Customer", row.customer)],
					[__("Item", null, "Injection APS"), this.safeDocLink("Item", row.item_code)],
					[__("Customer Part No"), text(row.customer_part_no)],
					[__("Delivery Date", null, "Injection APS"), date(row.schedule_date)],
					[__("Schedule", null, "Injection APS"), this.safeDocLink("Customer Delivery Schedule", row.schedule)],
					[__("Version", null, "Injection APS"), text(row.version_no)],
					[__("Schedule Item", null, "Injection APS"), text(row.schedule_item)],
					[__("Sales Order", null, "Injection APS"), this.safeDocLink("Sales Order", row.sales_order)],
					[__("Company", null, "Injection APS"), text(row.company)],
					[__("Schedule Scope"), text(row.schedule_scope)],
					[__("Source Type", null, "Injection APS"), translated(row.source_type)],
					[__("Remark", null, "Injection APS"), text(row.remark)],
				])}
				${this.detailSection(__("Quantity Progress", null, "Injection APS"), [
					[__("Demand", null, "Injection APS"), number(row.required_qty)],
					[__("Allocated", null, "Injection APS"), number(row.allocated_qty)],
					[__("Actual Good", null, "Injection APS"), number(row.actual_good_qty)],
					[__("Scrap", null, "Injection APS"), number(row.scrap_qty)],
					[__("Current Deliverable", null, "Injection APS"), number(row.current_deliverable_qty)],
					[__("Delivered", null, "Injection APS"), number(row.delivered_qty)],
					[__("Stock Covered"), number(row.stock_covered_qty)],
					[__("Production Covered"), number(row.production_covered_qty)],
					[__("Uncovered"), number(row.uncovered_qty)],
				])}
				${this.detailSection(__("Planning and Risk", null, "Injection APS"), [
					[__("Status", null, "Injection APS"), injection_aps.ui.pill(injection_aps.ui.translate(row.status || ""), this.getStatusTone(row.status))],
					[__("Risk Reason"), translated(row.risk_reason)],
					[__("Strategy", null, "Injection APS"), translated(row.production_strategy || "Auto Balance")],
					[__("Demand Confidence", null, "Injection APS"), translated(row.demand_confidence)],
					[__("Cancellation Risk %", null, "Injection APS"), `${number(row.cancellation_risk_percent, 2)}%`],
					[__("Prebuild Allowed", null, "Injection APS"), yesNo(row.prebuild_allowed)],
					[__("Max Prebuild Days", null, "Injection APS"), number(row.max_prebuild_days)],
					[__("Prebuild / JIT", null, "Injection APS"), `${number(row.prebuild_qty)} / ${number(row.jit_qty)}`],
					[__("Early Days", null, "Injection APS"), number(row.early_days, 2)],
					[__("Peak Inventory", null, "Injection APS"), number(row.projected_peak_inventory_qty)],
					[__("Prebuild Inventory", null, "Injection APS"), number(row.prebuild_inventory_qty)],
					[__("Late Before / After", null, "Injection APS"), `${number(row.late_qty_before_balance)} / ${number(row.late_qty_after_balance)}`],
					[__("Cancel Stock Risk", null, "Injection APS"), number(row.cancellation_inventory_risk_qty)],
					[__("Projected Done"), datetime(row.projected_completion_time)],
					[__("Variance Hrs"), row.variance_hours === null || row.variance_hours === undefined ? text("-") : number(row.variance_hours, 2)],
					[__("Last Report", null, "Injection APS"), datetime(row.last_actual_report_time)],
				])}
				${this.detailSection(__("Documents and Navigation", null, "Injection APS"), [
					[__("APS Run", null, "Injection APS"), this.safeDocLink("APS Planning Run", row.selected_run)],
					[__("APS Result"), resultLinks.length ? resultLinks.join("<br>") : text("-")],
					[__("Board"), boardRoute ? this.safeRouteLink(__("Open Board", null, "Injection APS"), boardRoute) : text("-")],
					[__("Production Sources", null, "Injection APS"), sourceLinks("Stock Entry", row.production_source_documents)],
					[__("Delivery Sources", null, "Injection APS"), sourceLinks("Delivery Note", row.delivery_source_documents)],
				])}
			</div>
		`;
		const subtitle = [row.customer, row.item_code, row.schedule_date ? injection_aps.ui.format_date(row.schedule_date) : ""]
			.filter(Boolean)
			.join(" · ");
		injection_aps.ui.open_drawer(__("Customer Schedule Details", null, "Injection APS"), subtitle, html);
	}

	detailSection(title, entries) {
		const rows = (entries || [])
			.map(
				([label, value]) => `
					<div class="ia-kv-row">
						<div class="ia-kv-key">${injection_aps.ui.escape(label || "")}</div>
						<div class="ia-kv-value">${value || "-"}</div>
					</div>
				`
			)
			.join("");
		return `<section class="ia-panel"><h4>${injection_aps.ui.escape(title || "")}</h4><div class="ia-kv">${rows}</div></section>`;
	}

	safeDocLink(doctype, name, label) {
		const linkLabel = injection_aps.ui.escape(label || name || "-");
		if (!doctype || !name) {
			return linkLabel;
		}
		const route = ["Form", doctype, name].map((part) => encodeURIComponent(String(part))).join("/");
		return `<a href="/app/${route}" class="ia-link">${linkLabel}</a>`;
	}

	safeRouteLink(label, route) {
		if (!route) {
			return injection_aps.ui.escape(label || "");
		}
		const normalizedRoute = String(route).replace(/^\/?app\//, "");
		return `<a href="/app/${injection_aps.ui.escape(normalizedRoute)}" class="ia-link">${injection_aps.ui.escape(label || "")}</a>`;
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
		const items = [
			{
				label: __("View Details", null, "Injection APS"),
				icon: "search",
				handler: () => this.openDetails(row),
			},
		];
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
				label: __("Open Board", null, "Injection APS"),
				icon: "external-link",
				handler: () => injection_aps.ui.go_to(row.routes.gantt),
			});
		}
		return items;
	}
}
