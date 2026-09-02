frappe.pages["aps-demand-admission-workbench"].on_page_load = function (wrapper) {
	injection_aps.ui_loader.start("20260902.1", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSDemandAdmissionWorkbench(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-demand-admission-workbench"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.applyRouteRun();
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSDemandAdmissionWorkbench {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Demand Admission Workbench"),
			single_column: true,
		});
		this.runField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "planning_run",
			options: "APS Planning Run",
			label: __("Planning Run"),
			change: () => this.refresh(),
		});
		this.page.main.html(`
			<div class="ia-page ia-admission-page">
				<div class="ia-workflow-host"></div>
				<div class="ia-banner">
					<h3>${__("Decide optional demand in one batch", null, "Injection APS")}</h3>
					<p>${__("P0 is included automatically. Choose one policy for P1 and P2, review the impact once, then continue to APS calculation.", null, "Injection APS")}</p>
				</div>
				<div class="ia-status-host"></div>
				<div class="ia-feedback"></div>
				<section class="ia-admission-guide" aria-label="${injection_aps.ui.escape(__("Admission class guide", null, "Injection APS"))}">
					<div><strong>P0</strong><span>${__("Confirmed customer demand; included and locked automatically.", null, "Injection APS")}</span></div>
					<div><strong>P1</strong><span>${__("Framework order remainder; PMC decides whether to produce early.", null, "Injection APS")}</span></div>
					<div><strong>P2</strong><span>${__("Safety-stock replenishment; PMC decides whether to include it.", null, "Injection APS")}</span></div>
				</section>
				<section class="ia-panel ia-admission-strategy-panel">
					<div class="ia-panel-head"><div><h4>${__("Choose a batch policy", null, "Injection APS")}</h4><p>${__("The policy applies to all optional rows. You can still fine-tune individual exceptions below.", null, "Injection APS")}</p></div></div>
					<div class="ia-admission-strategies"></div>
				</section>
				<section class="ia-panel ia-admission-table-panel">
					<div class="ia-admission-filter-host"></div>
					<div class="ia-admission-selection-host"></div>
					<div class="ia-table-target"></div>
					<div class="ia-admission-pagination"></div>
				</section>
				<div class="ia-admission-savebar">
					<div class="ia-admission-change-summary"></div>
					<button type="button" class="btn btn-primary" data-admission-preview="1">${__("Preview and confirm", null, "Injection APS")}</button>
				</div>
			</div>
		`);
		this.workflowHost = this.page.main.find(".ia-workflow-host")[0];
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.strategiesHost = this.page.main.find(".ia-admission-strategies")[0];
		this.filterHost = this.page.main.find(".ia-admission-filter-host")[0];
		this.selectionHost = this.page.main.find(".ia-admission-selection-host")[0];
		this.table = this.page.main.find(".ia-table-target")[0];
		this.pagination = this.page.main.find(".ia-admission-pagination")[0];
		this.changeSummary = this.page.main.find(".ia-admission-change-summary")[0];
		this.previewButton = this.page.main.find("[data-admission-preview='1']")[0];
		this.data = null;
		this.decisionMap = new Map();
		this.savedDecisionMap = new Map();
		this.selectedRows = new Set();
		this.filters = { admission_class: "ALL", search_text: "" };
		this.pageNumber = 1;
		this.pageLength = 100;
		this.strategy = "custom";
		this.previewButton.addEventListener("click", () => this.previewAndConfirm());
		this.applyRouteRun();
	}

	applyRouteRun() {
		const routeRun = frappe.utils.get_url_arg("run_name");
		if (routeRun && this.runField.get_value() !== routeRun) {
			this.runField.set_value(routeRun);
		}
	}

	renderWorkflow(currentStep) {
		injection_aps.ui.render_workflow_steps(this.workflowHost, [
			{ label: __("Demand baseline", null, "Injection APS"), status: "complete", route: "aps-net-requirement-workbench" },
			{ label: __("Batch admission", null, "Injection APS"), status: currentStep === "admission" ? "current" : "complete" },
			{ label: __("Impact confirmation", null, "Injection APS"), status: currentStep === "impact" ? "current" : "upcoming" },
			{ label: __("APS calculation", null, "Injection APS"), status: "upcoming" },
		]);
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		this.renderWorkflow("admission");
		const runName = this.runField.get_value();
		if (!runName) {
			this.renderEmpty(__("Select the draft Planning Run created from Net Requirements."));
			return;
		}
		injection_aps.ui.set_feedback(this.feedback, __("Loading demand admission..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_demand_admission_candidates", { planning_run: runName });
			this.data = data;
			this.initializeDecisions(data.rows || []);
			this.render();
		} catch (error) {
			console.error(error);
			this.renderEmpty(__("Demand admission could not be loaded. Review the error and refresh."), "error");
		}
	}

	initializeDecisions(rows) {
		this.decisionMap = new Map();
		this.savedDecisionMap = new Map();
		this.selectedRows.clear();
		rows.forEach((row) => {
			const value = Number(row.selected_qty || 0);
			this.decisionMap.set(row.name, value);
			this.savedDecisionMap.set(row.name, value);
		});
		const state = this.data.admission_state || {};
		this.strategy = state.strategy || (Number(state.confirmed || 0) ? "custom" : "standard");
		if (!Number(state.confirmed || 0) && Number(state.optional_row_count || 0)) {
			rows.forEach((row) => {
				if (row.admission_class === "P1") {
					this.decisionMap.set(row.name, Number(row.recommended_qty || 0));
				}
				if (row.admission_class === "P2") {
					this.decisionMap.set(row.name, 0);
				}
			});
		}
		this.pageNumber = 1;
	}

	render() {
		const state = this.data.admission_state || {};
		const baselineReady = Number(state.baseline_ready || 0) === 1;
		const optionalCount = Number(state.optional_row_count || 0);
		const canSave = injection_aps.ui.can_run_action("save_demand_admission");
		const permissionBlock = canSave ? "" : __("You do not have permission to confirm demand admission.", null, "Injection APS");
		injection_aps.ui.render_status_line(this.statusHost, {
			current_step: baselineReady ? __("Batch admission", null, "Injection APS") : __("Demand baseline", null, "Injection APS"),
			next_step: baselineReady ? (optionalCount ? __("Preview admission impact", null, "Injection APS") : __("APS calculation", null, "Injection APS")) : __("Prepare demand baseline", null, "Injection APS"),
			blocking_reason: permissionBlock || (baselineReady
				? state.blocking_reason
				: __("The demand baseline is missing. Prepare it before making admission decisions.", null, "Injection APS")),
		});
		this.renderStrategies();
		this.renderFilters();
		this.renderRows();
		this.renderChangeSummary();
		this.previewButton.disabled = !baselineReady || !optionalCount || !Number(state.editable || 0) || !canSave;
		if (!baselineReady) {
			this.renderPrepareBaselineAction();
			return;
		}
		if (!optionalCount) {
			this.renderP0OnlyState();
			return;
		}
		injection_aps.ui.set_feedback(this.feedback, __("Choose the recommended standard policy for a quick decision, then preview the total impact before saving.", null, "Injection APS"));
	}

	renderPrepareBaselineAction() {
		const disabled = injection_aps.ui.can_run_action("prepare_demand_baseline") ? "" : "disabled aria-disabled=\"true\"";
		this.selectionHost.innerHTML = `<div class="ia-admission-blocker"><span>${__("This Run has no demand baseline yet.", null, "Injection APS")}</span><button type="button" class="btn btn-primary btn-xs" data-prepare-baseline="1" ${disabled}>${__("Prepare demand baseline", null, "Injection APS")}</button></div>`;
		const button = this.selectionHost.querySelector("[data-prepare-baseline='1']");
		if (button) {
			button.addEventListener("click", () => this.prepareBaseline());
		}
	}

	renderP0OnlyState() {
		this.selectionHost.innerHTML = `<div class="ia-admission-complete"><div><strong>${__("Only mandatory P0 demand was found", null, "Injection APS")}</strong><span>${__("The system confirmed it automatically; no optional rows require PMC input.", null, "Injection APS")}</span></div><button type="button" class="btn btn-primary btn-xs" data-continue-calculation="1">${__("Continue to calculation", null, "Injection APS")}</button></div>`;
		this.selectionHost.querySelector("[data-continue-calculation='1']").addEventListener("click", () => {
			injection_aps.ui.go_to(`aps-run-console?run_name=${encodeURIComponent(this.runField.get_value())}&from_admission=auto`);
		});
	}

	renderStrategies() {
		const strategies = [
			{ key: "standard", title: __("Standard policy", null, "Injection APS"), badge: __("Recommended", null, "Injection APS"), description: __("Use the suggested P1 quantity and exclude P2.", null, "Injection APS") },
			{ key: "conservative", title: __("Conservative policy", null, "Injection APS"), description: __("Exclude all P1 and P2 demand.", null, "Injection APS") },
			{ key: "all_recommended", title: __("All suggestions", null, "Injection APS"), description: __("Use the suggested quantity for both P1 and P2.", null, "Injection APS") },
		];
		this.strategiesHost.innerHTML = strategies.map((row) => `<button type="button" class="ia-admission-strategy ${this.strategy === row.key ? "selected" : ""}" data-strategy="${row.key}" aria-pressed="${this.strategy === row.key ? "true" : "false"}"><span class="ia-admission-strategy-title">${injection_aps.ui.escape(row.title)}${row.badge ? `<em>${injection_aps.ui.escape(row.badge)}</em>` : ""}</span><span>${injection_aps.ui.escape(row.description)}</span></button>`).join("");
		this.strategiesHost.querySelectorAll("[data-strategy]").forEach((button) => button.addEventListener("click", () => this.applyStrategy(button.dataset.strategy)));
	}

	applyStrategy(strategy) {
		(this.data.rows || []).forEach((row) => {
			if (row.admission_class === "P0") {
				return;
			}
			let value = Number(row.recommended_qty || 0);
			if (strategy === "conservative" || (strategy === "standard" && row.admission_class === "P2")) {
				value = 0;
			}
			this.decisionMap.set(row.name, value);
		});
		this.strategy = strategy;
		this.selectedRows.clear();
		this.render();
	}

	renderFilters() {
		this.filterHost.innerHTML = `<div class="ia-admission-filters"><div class="ia-admission-class-tabs" role="group" aria-label="${injection_aps.ui.escape(__("Filter by admission class", null, "Injection APS"))}">${["ALL", "P0", "P1", "P2"].map((value) => `<button type="button" class="btn btn-xs ${this.filters.admission_class === value ? "btn-primary" : "btn-default"}" data-class-filter="${value}">${value === "ALL" ? __("All", null, "Injection APS") : value}</button>`).join("")}</div><label class="ia-admission-search"><span class="sr-only">${__("Search demand admission", null, "Injection APS")}</span><input type="search" class="form-control input-sm" value="${injection_aps.ui.escape(this.filters.search_text)}" placeholder="${injection_aps.ui.escape(__("Search customer, internal item, customer item or item name", null, "Injection APS"))}" data-admission-search="1"></label></div>`;
		this.filterHost.querySelectorAll("[data-class-filter]").forEach((button) => {
			button.addEventListener("click", () => {
				this.filters.admission_class = button.dataset.classFilter;
				this.pageNumber = 1;
				this.renderFilters();
				this.renderRows();
			});
		});
		const search = this.filterHost.querySelector("[data-admission-search='1']");
		search.addEventListener("input", () => {
			this.filters.search_text = search.value.trim();
			this.pageNumber = 1;
			this.renderRows();
		});
	}

	getFilteredRows() {
		const query = this.filters.search_text.toLowerCase();
		return (this.data.rows || []).filter((row) => {
			if (this.filters.admission_class !== "ALL" && row.admission_class !== this.filters.admission_class) {
				return false;
			}
			return !query || [row.customer, row.item_code, row.customer_code, row.item_name].some((value) => String(value || "").toLowerCase().includes(query));
		});
	}

	getPageRows() {
		const filtered = this.getFilteredRows();
		const pageCount = Math.max(1, Math.ceil(filtered.length / this.pageLength));
		this.pageNumber = Math.min(this.pageNumber, pageCount);
		const start = (this.pageNumber - 1) * this.pageLength;
		return filtered.slice(start, start + this.pageLength);
	}

	renderSelectionToolbar(pageRows, filteredRows) {
		const editablePageRows = pageRows.filter((row) => row.admission_class !== "P0");
		const editableFilteredRows = filteredRows.filter((row) => row.admission_class !== "P0");
		this.selectionHost.innerHTML = `<div class="ia-admission-selection-bar"><div><button type="button" class="btn btn-xs btn-default" data-select-page="1">${__("Select current page", null, "Injection APS")}</button><button type="button" class="btn btn-xs btn-default" data-select-filtered="1">${__("Select all filtered", null, "Injection APS")} (${injection_aps.ui.format_number(editableFilteredRows.length)})</button><button type="button" class="btn btn-xs btn-default" data-clear-selection="1">${__("Clear selection", null, "Injection APS")}</button></div><div class="ia-admission-bulk-actions"><span>${__("Selected", null, "Injection APS")} ${injection_aps.ui.format_number(this.selectedRows.size)}</span><button type="button" class="btn btn-xs btn-default" data-bulk-action="recommended" ${this.selectedRows.size ? "" : "disabled"}>${__("Use suggestion", null, "Injection APS")}</button><button type="button" class="btn btn-xs btn-default" data-bulk-action="exclude" ${this.selectedRows.size ? "" : "disabled"}>${__("Exclude", null, "Injection APS")}</button><button type="button" class="btn btn-xs btn-default" data-bulk-action="restore" ${this.selectedRows.size ? "" : "disabled"}>${__("Restore saved value", null, "Injection APS")}</button></div></div>`;
		this.selectionHost.querySelector("[data-select-page='1']").addEventListener("click", () => {
			this.selectedRows.clear();
			editablePageRows.forEach((row) => this.selectedRows.add(row.name));
			this.renderRows();
		});
		this.selectionHost.querySelector("[data-select-filtered='1']").addEventListener("click", () => {
			this.selectedRows.clear();
			editableFilteredRows.forEach((row) => this.selectedRows.add(row.name));
			this.renderRows();
		});
		this.selectionHost.querySelector("[data-clear-selection='1']").addEventListener("click", () => {
			this.selectedRows.clear();
			this.renderRows();
		});
		this.selectionHost.querySelectorAll("[data-bulk-action]").forEach((button) => button.addEventListener("click", () => this.applyBulkAction(button.dataset.bulkAction)));
	}

	applyBulkAction(action) {
		const byName = new Map((this.data.rows || []).map((row) => [row.name, row]));
		this.selectedRows.forEach((name) => {
			const row = byName.get(name);
			if (!row || row.admission_class === "P0") {
				return;
			}
			const value = action === "recommended" ? Number(row.recommended_qty || 0) : action === "restore" ? Number(this.savedDecisionMap.get(name) || 0) : 0;
			this.decisionMap.set(name, value);
		});
		this.strategy = "custom";
		this.render();
	}

	renderRows() {
		if (!this.data) {
			return;
		}
		const filteredRows = this.getFilteredRows();
		const pageRows = this.getPageRows();
		const optionalCount = Number((this.data.admission_state || {}).optional_row_count || 0);
		if (optionalCount) {
			this.renderSelectionToolbar(pageRows, filteredRows);
		}
		injection_aps.ui.render_table(
			this.table,
			[
				{ label: "", fieldname: "selection", className: "ia-admission-select-col" },
				{ label: __("Class", null, "Injection APS"), fieldname: "admission_class" },
				{ label: __("Item", null, "Injection APS"), fieldname: "item_code", className: "ia-admission-item-col" },
				{ label: __("Customer"), fieldname: "customer" },
				{ label: __("Candidate Qty"), fieldname: "candidate_qty", fieldtype: "Float" },
				{ label: __("Suggested Qty", null, "Injection APS"), fieldname: "recommended_qty", fieldtype: "Float" },
				{ label: __("Included Qty", null, "Injection APS"), fieldname: "selected_qty", fieldtype: "Float" },
				{ label: __("Decision", null, "Injection APS"), fieldname: "decision" },
			],
			pageRows.map((row) => ({ ...row, selected_qty: this.decisionMap.get(row.name) || 0 })),
			(column, value, row) => {
				if (column.fieldname === "selection") {
					return row.admission_class === "P0" ? "" : `<input type="checkbox" aria-label="${injection_aps.ui.escape(__("Select row", null, "Injection APS"))}" data-select-admission="${injection_aps.ui.escape(row.name)}" ${this.selectedRows.has(row.name) ? "checked" : ""}>`;
				}
				if (column.fieldname === "admission_class") {
					return injection_aps.ui.pill(value, value === "P0" ? "red" : value === "P1" ? "orange" : "blue");
				}
				if (column.fieldname === "item_code") {
					return injection_aps.ui.doc_link("Item", value, value);
				}
				if (["candidate_qty", "recommended_qty"].includes(column.fieldname)) {
					return injection_aps.ui.escape(injection_aps.ui.format_number(value || 0));
				}
				if (column.fieldname === "selected_qty") {
					if (row.admission_class === "P0") {
						return `<span class="ia-admission-locked" title="${injection_aps.ui.escape(__("P0 is mandatory and locked."))}">${injection_aps.ui.escape(injection_aps.ui.format_number(value || 0))}<small>${__("Locked", null, "Injection APS")}</small></span>`;
					}
					return `<input class="form-control input-sm ia-admission-qty" type="number" min="0" max="${Number(row.candidate_qty || 0)}" step="any" value="${Number(value || 0)}" data-admission-qty="${injection_aps.ui.escape(row.name)}">`;
				}
				if (column.fieldname === "decision") {
					const selected = Number(this.decisionMap.get(row.name) || 0);
					const candidate = Number(row.candidate_qty || 0);
					const status = row.admission_class === "P0" || selected >= candidate ? __("Included", null, "Injection APS") : selected > 0 ? __("Partially included", null, "Injection APS") : __("Excluded", null, "Injection APS");
					return `<div class="ia-admission-decision"><strong>${injection_aps.ui.escape(status)}</strong><span title="${injection_aps.ui.escape(injection_aps.ui.translate(row.recommendation_reason || ""))}">${injection_aps.ui.escape(injection_aps.ui.translate(row.recommendation_reason || ""))}</span></div>`;
				}
				return injection_aps.ui.escape(value || "-");
			},
			{
				exportable: true,
				export_title: __("Demand Admission Workbench"),
				export_sheet_name: __("Admission", null, "Injection APS"),
				count_label: __("{0} filtered rows", null, "Injection APS").replace("{0}", injection_aps.ui.format_number(filteredRows.length)),
				empty_message: __("No admission rows match the current filters.", null, "Injection APS"),
				after_render: (target) => this.bindRowInputs(target, pageRows),
			}
		);
		this.renderPagination(filteredRows.length);
		this.renderChangeSummary();
	}

	bindRowInputs(target, pageRows) {
		const rowByName = new Map(pageRows.map((row) => [row.name, row]));
		target.querySelectorAll("[data-select-admission]").forEach((node) => {
			node.addEventListener("change", () => {
				if (node.checked) {
					this.selectedRows.add(node.dataset.selectAdmission);
				} else {
					this.selectedRows.delete(node.dataset.selectAdmission);
				}
				this.renderRows();
			});
		});
		target.querySelectorAll("[data-admission-qty]").forEach((node) => {
			const row = rowByName.get(node.dataset.admissionQty);
			node.addEventListener("change", () => {
				const value = Number(node.value);
				if (!Number.isFinite(value) || value < 0 || value > Number(row.candidate_qty || 0)) {
					frappe.show_alert({ message: __("Included quantity must be between zero and the candidate quantity.", null, "Injection APS"), indicator: "orange" });
					node.value = Number(this.decisionMap.get(row.name) || 0);
					return;
				}
				this.decisionMap.set(row.name, value);
				this.strategy = "custom";
				this.render();
			});
		});
		target.querySelectorAll("tbody tr[data-row-index]").forEach((node) => {
			const row = pageRows[Number(node.dataset.rowIndex || 0)];
			if (row && this.isRowModified(row)) {
				node.classList.add("ia-admission-row-modified");
			}
		});
	}

	renderPagination(totalRows) {
		const pageCount = Math.max(1, Math.ceil(totalRows / this.pageLength));
		this.pagination.innerHTML = `<span>${__("Page {0} of {1}", null, "Injection APS").replace("{0}", this.pageNumber).replace("{1}", pageCount)}</span><div>${injection_aps.ui.icon_button("chevron-left", __("Previous Page"), { "data-admission-page": "previous", disabled: this.pageNumber <= 1 ? "disabled" : null })}${injection_aps.ui.icon_button("chevron-right", __("Next Page"), { "data-admission-page": "next", disabled: this.pageNumber >= pageCount ? "disabled" : null })}</div>`;
		this.pagination.querySelectorAll("[data-admission-page]").forEach((button) => {
			button.addEventListener("click", () => {
				this.pageNumber += button.dataset.admissionPage === "next" ? 1 : -1;
				this.renderRows();
			});
		});
	}

	isRowModified(row) {
		return Math.abs(Number(this.decisionMap.get(row.name) || 0) - Number(this.savedDecisionMap.get(row.name) || 0)) > 0.000001;
	}

	getDecisionSummary() {
		const result = { modified: 0, p1: 0, p2: 0, originalOptional: 0 };
		(this.data.rows || []).forEach((row) => {
			const selected = Number(this.decisionMap.get(row.name) || 0);
			const saved = Number(this.savedDecisionMap.get(row.name) || 0);
			if (this.isRowModified(row)) {
				result.modified += 1;
			}
			if (row.admission_class !== "P0") {
				result.originalOptional += saved;
			}
			if (row.admission_class === "P1") {
				result.p1 += selected;
			}
			if (row.admission_class === "P2") {
				result.p2 += selected;
			}
		});
		result.delta = result.p1 + result.p2 - result.originalOptional;
		return result;
	}

	renderChangeSummary() {
		if (!this.data) {
			this.changeSummary.innerHTML = "";
			return;
		}
		const summary = this.getDecisionSummary();
		this.changeSummary.innerHTML = `<span><strong>${injection_aps.ui.format_number(summary.modified)}</strong>${__("Modified rows", null, "Injection APS")}</span><span><strong>${injection_aps.ui.format_number(summary.p1)}</strong>${__("P1 included", null, "Injection APS")}</span><span><strong>${injection_aps.ui.format_number(summary.p2)}</strong>${__("P2 included", null, "Injection APS")}</span><span><strong>${summary.delta > 0 ? "+" : ""}${injection_aps.ui.format_number(summary.delta)}</strong>${__("Total change", null, "Injection APS")}</span>`;
	}

	collectDecisions() {
		return (this.data.rows || []).map((row) => ({ name: row.name, selected_qty: Number(this.decisionMap.get(row.name) || 0) }));
	}

	async previewAndConfirm() {
		if (!this.data || !(this.data.rows || []).length) {
			return;
		}
		const preview = await injection_aps.ui.xcall(
			{ message: __("Calculating admission impact...", null, "Injection APS"), busy_key: `preview-admission:${this.runField.get_value()}`, feedback_target: this.feedback },
			"injection_aps.api.app.preview_admission_impact",
			{ planning_run: this.runField.get_value(), decisions: this.collectDecisions(), input_fingerprint: this.data.admission_fingerprint, strategy: this.strategy }
		);
		if (preview) {
			this.renderWorkflow("impact");
			this.openImpactDrawer(preview);
		}
	}

	getStrategyLabel(strategy) {
		return { standard: __("Standard policy", null, "Injection APS"), conservative: __("Conservative policy", null, "Injection APS"), all_recommended: __("All suggestions", null, "Injection APS"), custom: __("Custom adjustment", null, "Injection APS") }[strategy] || __("Custom adjustment", null, "Injection APS");
	}

	openImpactDrawer(preview) {
		const before = preview.before_summary || {};
		const after = preview.summary || {};
		const html = `<div class="ia-page ia-drawer-stack ia-admission-impact-drawer"><div class="ia-admission-impact-lead"><strong>${injection_aps.ui.escape(this.getStrategyLabel(preview.strategy || this.strategy))}</strong><span>${__("Review the complete batch impact. Saving will return this Run to pending calculation.", null, "Injection APS")}</span></div><div class="ia-kv"><div class="ia-kv-row"><div class="ia-kv-key">${__("Modified rows", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.format_number(preview.changed_row_count || 0)}</div></div><div class="ia-kv-row"><div class="ia-kv-key">${__("P1 included", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.format_number(before.selected_p1_qty || 0)} → ${injection_aps.ui.format_number(after.selected_p1_qty || 0)}</div></div><div class="ia-kv-row"><div class="ia-kv-key">${__("P2 included", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.format_number(before.selected_p2_qty || 0)} → ${injection_aps.ui.format_number(after.selected_p2_qty || 0)}</div></div><div class="ia-kv-row"><div class="ia-kv-key">${__("Total plan quantity", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.format_number(before.selected_total_qty || 0)} → ${injection_aps.ui.format_number(after.selected_total_qty || 0)}</div></div><div class="ia-kv-row"><div class="ia-kv-key">${__("Excluded / partial rows", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.format_number(preview.excluded_row_count || 0)} / ${injection_aps.ui.format_number(preview.partial_row_count || 0)}</div></div></div>${preview.invalidates_previous_analysis ? `<div class="ia-alert warning">${__("The existing calculation and capacity analysis will become invalid. Recalculate this Run after saving.", null, "Injection APS")}</div>` : ""}<label class="ia-admission-reason"><span>${__("Batch decision reason", null, "Injection APS")}</span><textarea class="form-control" rows="4" data-admission-reason="1" placeholder="${injection_aps.ui.escape(__("Record why this batch policy is appropriate.", null, "Injection APS"))}"></textarea></label><div class="ia-drawer-footer-actions"><button type="button" class="btn btn-default" data-admission-back="1">${__("Back to adjust", null, "Injection APS")}</button><button type="button" class="btn btn-primary" data-admission-save="1">${__("Confirm admission and continue", null, "Injection APS")}</button></div></div>`;
		injection_aps.ui.open_drawer(__("Admission impact", null, "Injection APS"), this.runField.get_value(), html);
		const drawer = injection_aps.ui.ensure_drawer();
		drawer.querySelector("[data-admission-back='1']").addEventListener("click", () => {
			injection_aps.ui.close_drawer();
			this.renderWorkflow("admission");
		});
		drawer.querySelector("[data-admission-save='1']").addEventListener("click", () => this.saveAdmission(drawer));
	}

	async saveAdmission(drawer) {
		const reasonNode = drawer.querySelector("[data-admission-reason='1']");
		const reason = String(reasonNode ? reasonNode.value : "").trim();
		if (!reason) {
			frappe.show_alert({ message: __("Enter one reason for this batch decision.", null, "Injection APS"), indicator: "orange" });
			if (reasonNode) {
				reasonNode.focus();
			}
			return;
		}
		const result = await injection_aps.ui.xcall(
			{ message: __("Saving admission decisions..."), success_message: __("Admission decisions saved. Recalculate this Run before continuing.", null, "Injection APS"), busy_key: `save-admission:${this.runField.get_value()}`, feedback_target: this.feedback },
			"injection_aps.api.app.save_demand_admission_decisions",
			{ planning_run: this.runField.get_value(), decisions: this.collectDecisions(), input_fingerprint: this.data.admission_fingerprint, reason, strategy: this.strategy }
		);
		if (result) {
			injection_aps.ui.close_drawer();
			injection_aps.ui.go_to(result.next_route || `aps-run-console?run_name=${encodeURIComponent(this.runField.get_value())}&from_admission=1`);
		}
	}

	async prepareBaseline() {
		const runName = this.runField.get_value();
		if (!runName) {
			return;
		}
		const result = await injection_aps.ui.xcall(
			{ message: __("Preparing demand ownership and stock coverage..."), success_message: __("Demand baseline prepared."), busy_key: `prepare-demand:${runName}`, feedback_target: this.feedback },
			"injection_aps.api.app.prepare_run_demand_baseline",
			{ planning_run: runName, input_fingerprint: (this.data || {}).demand_baseline_fingerprint || undefined }
		);
		if (result) {
			await this.refresh();
		}
	}

	renderEmpty(message, tone) {
		this.data = null;
		this.decisionMap.clear();
		this.savedDecisionMap.clear();
		injection_aps.ui.render_status_line(this.statusHost, { current_step: __("Demand admission", null, "Injection APS"), next_step: __("Select a Run", null, "Injection APS"), blocking_reason: message });
		this.strategiesHost.innerHTML = "";
		this.filterHost.innerHTML = "";
		this.selectionHost.innerHTML = "";
		this.pagination.innerHTML = "";
		this.changeSummary.innerHTML = "";
		this.previewButton.disabled = true;
		injection_aps.ui.render_table(this.table, [{ label: __("Info"), fieldname: "message" }], []);
		injection_aps.ui.set_feedback(this.feedback, message, tone || "warning");
	}
}
