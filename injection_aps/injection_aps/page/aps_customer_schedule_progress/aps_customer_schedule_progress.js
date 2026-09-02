frappe.pages["aps-customer-schedule-progress"].on_page_load = function (wrapper) {
	injection_aps.ui_loader.start("20260901.1", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSCustomerScheduleProgress(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-customer-schedule-progress"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.syncRouteState();
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
		this.offset = 0;
		this.columnOffset = 0;
		this.pageLength = 25;
		this.loadingKey = "";
		this.progressView = "Date Matrix";
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
				<div class="ia-projection-banner"></div>
				<div class="ia-status-host"></div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-feedback"></div>
				<div class="ia-panel ia-progress-panel">
					<div class="ia-table-target"></div>
				</div>
			</div>
		`);
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.projectionBanner = this.page.main.find(".ia-projection-banner")[0];
		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.table = this.page.main.find(".ia-table-target")[0];
	}

	async refresh() {
		const filters = this.getFilters();
		const requestedView = this.progressView || "Date Matrix";
		const loadingKey = JSON.stringify({ ...filters, requestedView, offset: this.offset, columnOffset: this.columnOffset });
		if (this.loadingKey === loadingKey) return;
		const refreshGeneration = ++this.refreshGeneration;
		this.loadingKey = loadingKey;
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading customer schedule progress..."));
		this.table.innerHTML = `<div class="text-muted">${__("Loading customer schedule progress...", null, "Injection APS")}</div>`;
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_customer_schedule_progress_data", Object.assign({}, filters, {
				limit: 1000,
				progress_view: requestedView,
				offset: this.offset || 0,
				page_length: this.pageLength || 25,
				column_offset: requestedView === "Date Matrix" ? (this.columnOffset || 0) : undefined,
				column_limit: requestedView === "Date Matrix" ? 14 : undefined,
			}));
			if (refreshGeneration !== this.refreshGeneration) {
				return;
			}
			this.data = data || {};
			this.v2Enabled = this.data.mode === "V2";
			const matrixMode = this.v2Enabled && requestedView === "Date Matrix";
			this.rows = (this.data.rows || []).map((row, index) => Object.assign({ _row_no: index + 1 }, row));
			if (this.v2Enabled) {
				this.renderProjectionStatus(this.data.projection || {});
				this.renderV2Summary(this.data.summary || {});
				if (matrixMode) {
					this.renderMatrix(this.rows, this.data.matrix || {});
				} else {
					this.renderV2Table(this.rows);
				}
			} else {
				if (this.projectionBanner) this.projectionBanner.innerHTML = "";
				if (this.summary && this.summary.classList) this.summary.classList.remove("ia-progress-summary");
				this.renderRunStatus(this.data.selected_run || null, this.data.truncated);
				this.renderSummary(this.data.summary || {});
				this.renderTable(this.rows);
			}
			injection_aps.ui.set_feedback(this.feedback, __("Customer schedule progress refreshed."));
		} catch (error) {
			if (refreshGeneration !== this.refreshGeneration) {
				return;
			}
			console.error(error);
			this.data = {};
			this.rows = [];
			this.projectionBanner.innerHTML = "";
			this.statusHost.innerHTML = "";
			this.summary.innerHTML = "";
			this.table.innerHTML = `<div class="alert alert-danger">${__("No progress data is shown because the selected scope could not be loaded.", null, "Injection APS")}</div>`;
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load customer schedule progress."), "error");
		} finally {
			if (refreshGeneration === this.refreshGeneration) this.loadingKey = "";
		}
	}

	refreshFromFilter() {
		if (!this.suppressFilterRefresh) {
			this.offset = 0;
			this.columnOffset = 0;
			this.refresh();
		}
	}

	syncRouteState() {
		const runName = injection_aps.ui.get_query_param("run_name") || "";
		if ((this.runField.get_value() || "") === runName) return false;
		this.suppressFilterRefresh = true;
		this.runField.set_value(runName);
		this.suppressFilterRefresh = false;
		this.offset = 0;
		this.columnOffset = 0;
		this.loadingKey = "";
		this.refreshGeneration += 1;
		return true;
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

	renderProjectionStatus(projection) {
		const singleRun = Number(projection.single_run_view || 0) === 1;
		const toneClass = singleRun ? "warning" : "success";
		const runNames = projection.run_names || [];
		const visibleRunNames = runNames.slice(0, 3);
		const runSummary = visibleRunNames.length
			? `${visibleRunNames.join(", ")}${runNames.length > visibleRunNames.length ? ` +${runNames.length - visibleRunNames.length}` : ""}`
			: "";
		const guidance = singleRun
			? __("Clear APS Run to return to the effective cross-Run view.", null, "Injection APS")
			: __("Select a Run only when an explicit single-Run audit is required.", null, "Injection APS");
		this.projectionBanner.innerHTML = `
			<div class="ia-progress-projection ${toneClass}" role="status">
				<div class="ia-progress-projection-copy">
					<strong>${injection_aps.ui.escape(injection_aps.ui.translate(projection.label || ""))}</strong>
					<span>${injection_aps.ui.escape(injection_aps.ui.translate(projection.reason || ""))}</span>
				</div>
				<div class="ia-progress-projection-meta">
					<span>${injection_aps.ui.escape(guidance)}</span>
					${runSummary ? `<span title="${injection_aps.ui.escape(runNames.join(", "))}">${__("APS Runs", null, "Injection APS")}: ${injection_aps.ui.escape(runSummary)}</span>` : ""}
				</div>
			</div>
		`;
		this.statusHost.innerHTML = "";
	}

	renderV2Summary(summary) {
		const statusCounts = summary.status_counts || {};
		const healthyRows = Number(statusCounts.Delivered || 0) + Number(statusCounts["Stock Covered"] || 0) + Number(statusCounts["On Track"] || 0);
		const riskRows = Number(statusCounts["At Risk"] || 0);
		const lateRows = Number(statusCounts.Late || 0) + Number(statusCounts.Uncovered || 0);
		this.summary.classList.add("ia-progress-summary");
		this.summary.innerHTML = [
			this.progressSummaryGroup(__("Current Page Demand", null, "Injection APS"), [
				[__("Rows"), summary.rows || 0],
				[__("Schedule Qty"), summary.schedule_qty || 0],
			]),
			this.progressSummaryGroup(__("Plan", null, "Injection APS"), [
				[__("Original Plan", null, "Injection APS"), summary.original_plan_qty || 0],
				[__("Current Plan", null, "Injection APS"), summary.current_plan_qty || 0, "strong"],
				[__("Forecast", null, "Injection APS"), summary.forecast_qty || 0],
			]),
			this.progressSummaryGroup(__("Execution", null, "Injection APS"), [
				[__("Actual Good", null, "Injection APS"), summary.actual_good_qty || 0, "strong"],
				[__("Scrap", null, "Injection APS"), summary.actual_scrap_qty || 0, Number(summary.actual_scrap_qty || 0) > 0 ? "warning" : ""],
			]),
			this.progressSummaryGroup(__("Delivery", null, "Injection APS"), [
				[__("Delivery Plan", null, "Injection APS"), summary.delivery_plan_qty || 0],
				[__("Delivered", null, "Injection APS"), summary.delivered_qty || 0, "strong"],
			]),
			this.progressSummaryGroup(__("Coverage", null, "Injection APS"), [
				[__("Stock Covered"), summary.stock_covered_qty || 0],
				[__("Shortage", null, "Injection APS"), summary.shortage_qty || 0, Number(summary.shortage_qty || 0) > 0 ? "danger" : ""],
				[__("Recovery", null, "Injection APS"), summary.recovery_qty || 0],
			]),
			this.progressSummaryGroup(__("Current Page Status", null, "Injection APS"), [
				[__("On Track", null, "Injection APS"), healthyRows, "success"],
				[__("At Risk", null, "Injection APS"), riskRows, riskRows > 0 ? "warning" : ""],
				[__("Late", null, "Injection APS"), lateRows, lateRows > 0 ? "danger" : ""],
				[__("Conservation Issues", null, "Injection APS"), summary.conservation_issue_rows || 0, Number(summary.conservation_issue_rows || 0) > 0 ? "danger" : ""],
			]),
		].join("");
	}

	progressSummaryGroup(title, metrics) {
		return `
			<section class="ia-progress-summary-group">
				<h4>${injection_aps.ui.escape(title)}</h4>
				<div class="ia-progress-summary-metrics">
					${metrics.map(([label, value, tone]) => `<div class="ia-progress-summary-metric ${injection_aps.ui.escape(tone || "")}"><span>${injection_aps.ui.escape(label)}</span><strong>${injection_aps.ui.escape(injection_aps.ui.format_number(value || 0))}</strong></div>`).join("")}
				</div>
			</section>
		`;
	}

	renderProgressToolbar(matrixMode) {
		const pagination = this.data.pagination || {};
		const matrix = this.data.matrix || {};
		const previousRowsDisabled = this.offset <= 0;
		const nextRowsDisabled = !pagination.has_more;
		const previousColumnsDisabled = !matrixMode || !matrix.has_previous_columns;
		const nextColumnsDisabled = !matrixMode || !matrix.has_more_columns;
		return `
			<div class="ia-progress-view-switch" role="group" aria-label="${injection_aps.ui.escape(__("Progress View", null, "Injection APS"))}">
				<button type="button" class="ia-progress-view-option ${matrixMode ? "" : "active"}" data-progress-view="Detail" aria-pressed="${matrixMode ? "false" : "true"}">${__("Detail", null, "Injection APS")}</button>
				<button type="button" class="ia-progress-view-option ${matrixMode ? "active" : ""}" data-progress-view="Date Matrix" aria-pressed="${matrixMode ? "true" : "false"}">${__("Date Matrix", null, "Injection APS")}</button>
			</div>
			<div class="ia-progress-toolbar-group" aria-label="${injection_aps.ui.escape(__("Rows"))}">
				<span class="ia-progress-toolbar-label">${__("Rows")}</span>
				${injection_aps.ui.icon_button("chevron-left", previousRowsDisabled ? __("This is the first result page.", null, "Injection APS") : __("Previous result page", null, "Injection APS"), { "data-progress-page": "previous", disabled: previousRowsDisabled ? "disabled" : null })}
				${injection_aps.ui.icon_button("chevron-right", nextRowsDisabled ? __("No more result rows are available.", null, "Injection APS") : __("Next result page", null, "Injection APS"), { "data-progress-page": "next", disabled: nextRowsDisabled ? "disabled" : null })}
			</div>
			${matrixMode ? `<div class="ia-progress-toolbar-group" aria-label="${injection_aps.ui.escape(__("Date Matrix", null, "Injection APS"))}">
				<span class="ia-progress-toolbar-label">${injection_aps.ui.escape((matrix.dates || [])[0] || "")} – ${injection_aps.ui.escape((matrix.dates || []).slice(-1)[0] || "")}</span>
				${injection_aps.ui.icon_button("chevron-left", previousColumnsDisabled ? __("This is the first date window.", null, "Injection APS") : __("Previous date window", null, "Injection APS"), { "data-progress-column": "previous", disabled: previousColumnsDisabled ? "disabled" : null })}
				${injection_aps.ui.icon_button("chevron-right", nextColumnsDisabled ? __("No later dates are available in this filter range.", null, "Injection APS") : __("Next date window", null, "Injection APS"), { "data-progress-column": "next", disabled: nextColumnsDisabled ? "disabled" : null })}
			</div>` : ""}
		`;
	}

	bindProgressToolbar() {
		this.table.querySelectorAll("[data-progress-view]").forEach((button) => button.addEventListener("click", () => {
			const nextView = button.dataset.progressView || "Detail";
			if (nextView === this.progressView) return;
			this.progressView = nextView;
			this.offset = 0;
			this.columnOffset = 0;
			this.refresh();
		}));
		this.table.querySelectorAll("[data-progress-page]").forEach((button) => button.addEventListener("click", () => {
			this.offset = Math.max(this.offset + (button.dataset.progressPage === "next" ? this.pageLength : -this.pageLength), 0);
			this.refresh();
		}));
		this.table.querySelectorAll("[data-progress-column]").forEach((button) => button.addEventListener("click", () => {
			this.columnOffset = Math.max(this.columnOffset + (button.dataset.progressColumn === "next" ? 14 : -14), 0);
			this.refresh();
		}));
		const exportButton = this.table.querySelector("[data-progress-export]");
		if (exportButton) exportButton.addEventListener("click", () => this.exportV2CurrentView());
	}

	getProgressRowRangeLabel() {
		return __("Rows {0} - {1}", null, "Injection APS")
			.replace("{0}", this.offset + (this.rows.length ? 1 : 0))
			.replace("{1}", this.offset + this.rows.length);
	}

	renderV2Table(rows) {
		const columns = [
			{ label: __("Customer / Item", null, "Injection APS"), fieldname: "identity_summary", className: "ia-progress-col-identity" },
			{ label: __("Delivery Date", null, "Injection APS"), fieldname: "schedule_date", className: "ia-progress-col-date" },
			{ label: __("Schedule Qty"), fieldname: "schedule_qty", fieldtype: "Float", className: "ia-progress-col-qty" },
			{ label: __("Plan", null, "Injection APS"), fieldname: "plan_layers", className: "ia-progress-col-metrics" },
			{ label: __("Execution", null, "Injection APS"), fieldname: "actual_layers", className: "ia-progress-col-metrics" },
			{ label: __("Delivery", null, "Injection APS"), fieldname: "delivery_layers", className: "ia-progress-col-metrics" },
			{ label: __("Coverage", null, "Injection APS"), fieldname: "coverage_layers", className: "ia-progress-col-metrics" },
			{ label: __("Status", null, "Injection APS"), fieldname: "v2_status", className: "ia-progress-col-status" },
			{ label: __("Actions", null, "Injection APS"), fieldname: "v2_actions", exportable: false, className: "ia-progress-col-actions" },
		];
		injection_aps.ui.render_table(this.table, columns, rows, (column, value, row) => this.formatV2Cell(column, value, row), {
			exportable: true,
			count_label: this.getProgressRowRangeLabel(),
			toolbar_html: this.renderProgressToolbar(false),
			export_title: __("Customer Schedule Progress V2", null, "Injection APS"),
			export_sheet_name: __("Progress Detail", null, "Injection APS"),
			export_file_name: "aps_customer_schedule_progress_v2",
			export_subtitle: (this.data.projection || {}).label || "",
			export_columns: this.getV2ExportColumns(),
			after_render: () => {
				this.bindProgressToolbar();
				this.bindV2Actions();
			},
		});
	}

	progressMetricList(metrics) {
		return `<div class="ia-progress-metric-list">${metrics.map(([label, value, tone]) => `<div class="${injection_aps.ui.escape(tone || "")}"><span>${injection_aps.ui.escape(label)}</span><strong>${injection_aps.ui.escape(injection_aps.ui.format_number(value || 0))}</strong></div>`).join("")}</div>`;
	}

	formatV2Cell(column, value, row) {
		const number = (candidate) => injection_aps.ui.escape(injection_aps.ui.format_number(candidate || 0));
		if (column.fieldname === "identity_summary") {
			return `<div><div>${this.safeDocLink("Customer", row.customer)}</div>${injection_aps.ui.item_identity(row)}<div class="ia-muted">${row.demand_identity ? this.safeDocLink("APS Demand Identity", row.demand_identity) : __("No Demand Identity", null, "Injection APS")}</div></div>`;
		}
		if (column.fieldname === "schedule_date") return injection_aps.ui.escape(injection_aps.ui.format_date(value));
		if (column.fieldname === "schedule_qty") return number(value);
		if (column.fieldname === "plan_layers") return this.progressMetricList([[__("Current Plan", null, "Injection APS"), row.current_plan_qty, "strong"], [__("Forecast", null, "Injection APS"), row.forecast_qty], [__("Original Plan", null, "Injection APS"), row.original_plan_qty]]);
		if (column.fieldname === "actual_layers") return this.progressMetricList([[__("Actual Good", null, "Injection APS"), row.actual_good_qty, "strong"], [__("Scrap", null, "Injection APS"), row.actual_scrap_qty, Number(row.actual_scrap_qty || 0) > 0 ? "warning" : ""]]);
		if (column.fieldname === "delivery_layers") return this.progressMetricList([[__("Delivery Plan", null, "Injection APS"), row.delivery_plan_qty], [__("Delivered", null, "Injection APS"), row.delivered_qty, "strong"]]);
		if (column.fieldname === "coverage_layers") return `${this.progressMetricList([[__("Stock Covered"), row.stock_covered_qty], [__("Shortage", null, "Injection APS"), row.shortage_qty, Number(row.shortage_qty || 0) > 0 ? "danger" : ""], [__("Recovery", null, "Injection APS"), row.recovery_qty]])}${row.recovery_completion_time ? `<div class="ia-progress-recovery-time">${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.recovery_completion_time))}</div>` : ""}`;
		if (column.fieldname === "v2_status") return `${injection_aps.ui.pill(injection_aps.ui.translate(row.status || ""), row.status_tone || "gray")}<div class="ia-muted" title="${injection_aps.ui.escape(injection_aps.ui.translate(row.reason || ""))}">${injection_aps.ui.escape(injection_aps.ui.shorten(injection_aps.ui.translate(row.reason || ""), 64))}</div>`;
		if (column.fieldname === "v2_actions") return `<button type="button" class="btn btn-xs btn-default" data-v2-progress-details="${Number(row._row_no || 0)}">${__("View Details", null, "Injection APS")}</button>`;
		return injection_aps.ui.escape(value == null ? "" : value);
	}

	getV2ExportColumns() {
		return [
			{ label: __("Demand Identity", null, "Injection APS"), fieldname: "demand_identity" },
			{ label: __("Company", null, "Injection APS"), fieldname: "company" },
			{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
			{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
			{ label: __("Delivery Date", null, "Injection APS"), fieldname: "schedule_date" },
			{ label: __("Schedule Qty"), fieldname: "schedule_qty", fieldtype: "Float" },
			{ label: __("Original Plan", null, "Injection APS"), fieldname: "original_plan_qty", fieldtype: "Float" },
			{ label: __("Current Plan", null, "Injection APS"), fieldname: "current_plan_qty", fieldtype: "Float" },
			{ label: __("Forecast", null, "Injection APS"), fieldname: "forecast_qty", fieldtype: "Float" },
			{ label: __("Actual Good", null, "Injection APS"), fieldname: "actual_good_qty", fieldtype: "Float" },
			{ label: __("Scrap", null, "Injection APS"), fieldname: "actual_scrap_qty", fieldtype: "Float" },
			{ label: __("Delivery Plan", null, "Injection APS"), fieldname: "delivery_plan_qty", fieldtype: "Float" },
			{ label: __("Delivered", null, "Injection APS"), fieldname: "delivered_qty", fieldtype: "Float" },
			{ label: __("Stock Covered"), fieldname: "stock_covered_qty", fieldtype: "Float" },
			{ label: __("Shortage", null, "Injection APS"), fieldname: "shortage_qty", fieldtype: "Float" },
			{ label: __("Recovery", null, "Injection APS"), fieldname: "recovery_qty", fieldtype: "Float" },
			{ label: __("Status", null, "Injection APS"), fieldname: "status" },
			{ label: __("Reason", null, "Injection APS"), fieldname: "reason" },
			{ label: __("Conservation Status", null, "Injection APS"), fieldname: "conservation_status" },
		];
	}

	bindV2Actions() {
		this.table.querySelectorAll("[data-v2-progress-details]").forEach((button) => button.addEventListener("click", () => {
			const row = this.rows.find((candidate) => Number(candidate._row_no || 0) === Number(button.dataset.v2ProgressDetails || 0));
			if (row) this.openV2Details(row);
		}));
	}

	renderMatrix(rows, matrix) {
		const dates = matrix.dates || [];
		const layers = this.getMatrixLayers();
		const headers = dates.map((dateValue) => `<th class="ia-progress-date-header">${injection_aps.ui.escape(injection_aps.ui.format_date(dateValue))}</th>`).join("");
		const body = rows.map((row) => {
			return layers.map((layer, layerIndex) => {
				const cells = dates.map((dateValue) => this.renderMatrixLayerCell(row, dateValue, (row.cells || {})[dateValue], layer)).join("");
				const identity = layerIndex === 0 ? `<th class="ia-progress-row-header" rowspan="${layers.length}">
					<div class="ia-progress-row-customer">${this.safeDocLink("Customer", row.customer)}</div>
					${injection_aps.ui.item_identity(row)}
					<div class="ia-progress-row-meta">
						${injection_aps.ui.pill(injection_aps.ui.translate(row.status || ""), row.status_tone || "gray")}
						<span>${injection_aps.ui.escape(injection_aps.ui.format_number(row.schedule_qty || 0))}</span>
						<span>${Number(row.schedule_count || 0)} ${__("Rows")}</span>
					</div>
				</th>` : "";
				return `<tr class="ia-progress-layer-row ${layerIndex === 0 ? "group-start" : ""}">${identity}<th class="ia-progress-layer-header">${injection_aps.ui.escape(layer.label)}</th>${cells}</tr>`;
			}).join("");
		}).join("");
		this.table.innerHTML = `
			<div class="ia-table-toolbar ia-progress-matrix-toolbar">
				<div class="ia-table-count">${injection_aps.ui.escape(this.getProgressRowRangeLabel())}</div>
				<div class="ia-progress-legend" aria-label="${injection_aps.ui.escape(__("Cell color legend", null, "Injection APS"))}">
					<span class="red">${__("Short / Late", null, "Injection APS")}</span>
					<span class="yellow">${__("Progress Risk", null, "Injection APS")}</span>
					<span class="green">${__("Actual / Delivered", null, "Injection APS")}</span>
				</div>
				<div class="ia-table-actions">
					${this.renderProgressToolbar(true)}
					${injection_aps.ui.icon_button("download", __("Export Excel", null, "Injection APS"), { "data-progress-export": "1" })}
				</div>
			</div>
			<div class="ia-progress-matrix-shell"><table class="ia-progress-matrix"><thead><tr><th class="ia-progress-corner">${__("Customer / Item", null, "Injection APS")}</th><th class="ia-progress-layer-corner">${__("Progress Layer", null, "Injection APS")}</th>${headers}</tr></thead><tbody>${body}</tbody></table></div>
		`;
		this.bindProgressToolbar();
		this.table.querySelectorAll("[data-progress-cell]").forEach((button) => button.addEventListener("click", () => this.openMatrixCell(Number(button.dataset.progressRow || 0), button.dataset.progressDate, button.dataset.progressLayer)));
	}

	getMatrixLayers() {
		return [
			{ key: "schedule", label: __("Customer Schedule", null, "Injection APS"), fields: [["schedule_qty", __("Schedule Qty")], ["stock_covered_qty", __("Stock Covered")], ["shortage_qty", __("Shortage", null, "Injection APS"), "danger"]] },
			{ key: "plan", label: __("APS Plan", null, "Injection APS"), fields: [["current_plan_qty", __("Current Plan", null, "Injection APS")], ["original_plan_qty", __("Original Plan", null, "Injection APS")], ["forecast_qty", __("Forecast", null, "Injection APS")], ["recovery_qty", __("Recovery", null, "Injection APS")]] },
			{ key: "actual", label: __("Actual Inbound", null, "Injection APS"), fields: [["actual_good_qty", __("Actual Good", null, "Injection APS")], ["actual_scrap_qty", __("Scrap", null, "Injection APS"), "warning"]] },
			{ key: "delivery", label: __("Delivery", null, "Injection APS"), fields: [["delivered_qty", __("Delivered", null, "Injection APS")], ["delivery_plan_qty", __("Delivery Plan", null, "Injection APS")]] },
		];
	}

	renderMatrixLayerCell(row, dateValue, cell, layer) {
		if (!cell) return `<td class="ia-progress-cell empty"></td>`;
		const visibleFields = layer.fields.filter(([fieldname]) => Math.abs(Number(cell[fieldname] || 0)) > 1e-9);
		const alerts = (cell.alerts || []).filter((alertRow) => alertRow.layer === layer.key);
		const reasons = [
			...(row.events || []).filter((event) => event.date === dateValue && layer.fields.some(([fieldname]) => fieldname === event.layer) && event.reason).map((event) => event.reason),
			...alerts.map((alertRow) => alertRow.reason),
		];
		if (!visibleFields.length && !reasons.length) return `<td class="ia-progress-cell empty"></td>`;
		const lines = visibleFields.map(([fieldname, label, tone]) => `<span class="${injection_aps.ui.escape(tone || "")}"><b>${injection_aps.ui.escape(label)}</b><strong>${injection_aps.ui.escape(injection_aps.ui.format_number(cell[fieldname] || 0))}</strong></span>`).join("");
		const reason = [...new Set(reasons)].join(" ");
		const alert = reason ? `<span class="ia-progress-cell-alert" aria-hidden="true">!</span>` : "";
		const title = reason || __("Open source documents for this date cell.", null, "Injection APS");
		const tone = this.getMatrixCellTone(cell, layer, alerts);
		const quantityLabel = visibleFields.length
			? visibleFields.map(([fieldname, label]) => `${label} ${injection_aps.ui.format_number(cell[fieldname] || 0)}`).join(", ")
			: __("No quantity", null, "Injection APS");
		const ariaLabel = [row.customer, row.item_code, layer.label, dateValue, quantityLabel, reason].filter(Boolean).join(" · ");
		return `<td class="ia-progress-cell ${injection_aps.ui.escape(tone)}"><button type="button" data-progress-cell="1" data-progress-row="${Number(row._row_no || 0)}" data-progress-date="${injection_aps.ui.escape(dateValue)}" data-progress-layer="${injection_aps.ui.escape(layer.key)}" title="${injection_aps.ui.escape(title)}" aria-label="${injection_aps.ui.escape(ariaLabel)}">${alert}${lines}</button></td>`;
	}

	getMatrixCellTone(cell, layer, alerts) {
		if (alerts.some((row) => row.tone === "red")) return "red";
		if (alerts.length || (layer.key === "actual" && Number(cell.actual_scrap_qty || 0) > 0)) return "yellow";
		if (layer.key === "delivery" && Number(cell.delivered_qty || 0) > 0) return "green";
		if (layer.key === "actual" && Number(cell.actual_good_qty || 0) > 0) return "green";
		if (layer.key === "plan") return "blue";
		return "gray";
	}

	openMatrixCell(rowNumber, dateValue, layerKey) {
		const row = this.rows.find((candidate) => Number(candidate._row_no || 0) === rowNumber);
		const layer = this.getMatrixLayers().find((candidate) => candidate.key === layerKey);
		if (!row || !layer) return;
		const events = (row.events || []).filter((event) => event.date === dateValue && layer.fields.some(([fieldname]) => fieldname === event.layer));
		const cell = (row.cells || {})[dateValue] || {};
		const quantities = layer.fields
			.filter(([fieldname]) => Math.abs(Number(cell[fieldname] || 0)) > 1e-9)
			.map(([fieldname, label]) => [label, injection_aps.ui.escape(injection_aps.ui.format_number(cell[fieldname] || 0))]);
		const reasons = [...new Set([
			...events.map((event) => event.reason),
			...(cell.alerts || []).filter((alertRow) => alertRow.layer === layerKey).map((alertRow) => alertRow.reason),
		].filter(Boolean))];
		const sources = [];
		const seen = new Set();
		events.flatMap((event) => event.sources || []).forEach((source) => {
			const key = `${source.doctype}|${source.name}`;
			if (!seen.has(key)) {
				seen.add(key);
				sources.push(source);
			}
		});
		const entries = [[__("Date", null, "Injection APS"), injection_aps.ui.escape(injection_aps.ui.format_date(dateValue))], ...quantities];
		if (reasons.length) entries.push([__("Reason", null, "Injection APS"), injection_aps.ui.escape(reasons.join(" "))]);
		const html = `<div class="ia-progress-cell-detail">${this.detailSection(layer.label, entries)}${this.detailSection(__("Source Documents", null, "Injection APS"), [[__("Documents", null, "Injection APS"), this.sourceDocumentsHtml(sources)]])}</div>`;
		injection_aps.ui.open_drawer(__("Progress Cell Drilldown", null, "Injection APS"), [row.customer, row.item_code, dateValue].filter(Boolean).join(" · "), html);
	}

	sourceDocumentsHtml(sources) {
		if (!(sources || []).length) return `<span class="ia-muted">${__("No readable source document is available.", null, "Injection APS")}</span>`;
		const childDoctypes = new Set(["Customer Delivery Schedule Item", "APS Schedule Segment", "Scheduling Item", "Delivery Plan Item Qty"]);
		return sources.map((source) => childDoctypes.has(source.doctype) ? `<div>${injection_aps.ui.escape(source.doctype)}: ${injection_aps.ui.escape(source.name)}</div>` : `<div>${this.safeDocLink(source.doctype, source.name, `${source.doctype}: ${source.name}`)}</div>`).join("");
	}

	openV2Details(row) {
		const number = (value) => injection_aps.ui.escape(injection_aps.ui.format_number(value || 0));
		const html = `<div style="display:grid;gap:10px;">
			${this.detailSection(__("Demand Identity", null, "Injection APS"), [[__("Demand Identity", null, "Injection APS"), this.safeDocLink("APS Demand Identity", row.demand_identity)], [__("Customer", null, "Injection APS"), this.safeDocLink("Customer", row.customer)], [__("Item", null, "Injection APS"), injection_aps.ui.item_identity(row)], [__("Delivery Date", null, "Injection APS"), injection_aps.ui.escape(injection_aps.ui.format_date(row.schedule_date))], [__("Schedule Qty"), number(row.schedule_qty)]])}
			${this.detailSection(__("Original / Current / Forecast / Actual", null, "Injection APS"), [[__("Original Plan", null, "Injection APS"), `${number(row.original_plan_qty)} / ${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.original_completion_time))}`], [__("Current Plan", null, "Injection APS"), `${number(row.current_plan_qty)} / ${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.current_completion_time))}`], [__("Forecast", null, "Injection APS"), `${number(row.forecast_qty)} / ${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.forecast_completion_time))}`], [__("Actual Good / Scrap", null, "Injection APS"), `${number(row.actual_good_qty)} / ${number(row.actual_scrap_qty)}`]])}
			${this.detailSection(__("Delivery and Recovery", null, "Injection APS"), [[__("Delivery Plan / Delivered", null, "Injection APS"), `${number(row.delivery_plan_qty)} / ${number(row.delivered_qty)}`], [__("Stock Covered"), number(row.stock_covered_qty)], [__("Shortage / Recovery", null, "Injection APS"), `${number(row.shortage_qty)} / ${number(row.recovery_qty)}`], [__("Recovery Completion", null, "Injection APS"), injection_aps.ui.escape(injection_aps.ui.format_datetime(row.recovery_completion_time))]])}
			${this.detailSection(__("Status and Conservation", null, "Injection APS"), [[__("Status", null, "Injection APS"), injection_aps.ui.pill(injection_aps.ui.translate(row.status || ""), row.status_tone || "gray")], [__("Reason", null, "Injection APS"), injection_aps.ui.escape(injection_aps.ui.translate(row.reason || ""))], [__("Conservation Status", null, "Injection APS"), injection_aps.ui.escape(row.conservation_status || "")], [__("Demand / Solver Delta", null, "Injection APS"), `${number(row.demand_conservation_delta)} / ${number(row.solver_partition_delta)}`]])}
			${this.detailSection(__("Source Documents", null, "Injection APS"), [[__("Documents", null, "Injection APS"), this.sourceDocumentsHtml(row.source_documents || [])]])}
		</div>`;
		injection_aps.ui.open_drawer(__("Customer Schedule Progress V2", null, "Injection APS"), [row.customer, row.schedule_date].filter(Boolean).join(" · "), html);
	}

	exportV2CurrentView() {
		const dates = injection_aps.ui.get_value(this.data, "matrix.dates", []) || [];
		const columns = [{ label: __("Customer", null, "Injection APS"), fieldname: "customer" }, { label: __("Item", null, "Injection APS"), fieldname: "item_code" }, ...dates.map((dateValue) => ({ label: dateValue, fieldname: dateValue }))];
		const rows = this.rows.map((row) => {
			const output = { customer: row.customer, item_code: row.item_code };
			dates.forEach((dateValue) => {
				const cell = (row.cells || {})[dateValue] || {};
				output[dateValue] = Object.entries(cell).filter(([key, value]) => key.endsWith("_qty") && Math.abs(Number(value || 0)) > 1e-9).map(([key, value]) => `${key}=${value}`).join("; ");
			});
			return output;
		});
		injection_aps.ui.export_rows_to_excel({ title: __("Customer Schedule Progress Matrix", null, "Injection APS"), sheet_name: __("Date Matrix", null, "Injection APS"), file_name: "aps_customer_schedule_progress_matrix", subtitle: (this.data.projection || {}).label || "", columns, rows });
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
			return `<div><div>${customer}</div>${injection_aps.ui.item_identity(row)}</div>`;
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
					[__("Item", null, "Injection APS"), injection_aps.ui.item_identity(row)],
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
		const subtitle = [row.customer, row.schedule_date ? injection_aps.ui.format_date(row.schedule_date) : ""]
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
