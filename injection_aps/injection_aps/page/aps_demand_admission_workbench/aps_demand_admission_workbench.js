frappe.pages["aps-demand-admission-workbench"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_ui_loader.js", () => injection_aps.ui_loader.start("20260815.2", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSDemandAdmissionWorkbench(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	}));
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
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Demand Admission Workbench")}</h3>
					<p>${__("P0 customer commitments are mandatory. P1 framework demand and P2 safety-stock demand remain unselected until PMC explicitly saves a decision.")}</p>
				</div>
				<div class="ia-status-host"></div>
				<div class="ia-action-host"></div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-feedback"></div>
				<div class="ia-panel"><div class="ia-source-summary"></div></div>
				<div class="ia-panel"><div class="ia-table-target"></div></div>
			</div>
		`);
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.actionHost = this.page.main.find(".ia-action-host")[0];
		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.sourceSummary = this.page.main.find(".ia-source-summary")[0];
		this.table = this.page.main.find(".ia-table-target")[0];
		this.data = null;
		this.applyRouteRun();
	}

	applyRouteRun() {
		const routeRun = frappe.utils.get_url_arg("run_name");
		if (routeRun && this.runField.get_value() !== routeRun) {
			this.runField.set_value(routeRun);
		}
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		const runName = this.runField.get_value();
		if (!runName) {
			this.renderEmpty(__("Select a Planning Run to prepare and review its demand baseline."));
			return;
		}
		injection_aps.ui.set_feedback(this.feedback, __("Loading demand admission..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_demand_admission_candidates", {
				planning_run: runName,
			});
			this.data = data;
			this.render(data);
		} catch (error) {
			console.error(error);
			this.renderEmpty(__("Demand admission could not be loaded. Review the error and refresh."), "error");
		}
	}

	render(data) {
		const available = data.available !== false;
		const summary = data.summary || {};
		injection_aps.ui.render_status_line(this.statusHost, {
			current_step: available ? __("Demand Baseline / Admission") : __("Legacy Planning Active"),
			next_step: available ? __("Prepare Baseline, then confirm P1/P2", null, "Injection APS") : __("Enable APS V2 in a controlled rollout"),
			blocking_reason: available ? "" : (data.reason || __("APS V2 is disabled.")),
		});
		const canPlan = injection_aps.ui.can_run_action("prepare_demand_baseline");
		injection_aps.ui.render_actions(this.actionHost, [
			{ label: __("Prepare Demand Baseline"), action_key: "prepare_demand_baseline", enabled: available && canPlan ? 1 : 0 },
			{ label: __("Save P1/P2 Selection"), action_key: "save_demand_admission", enabled: available && canPlan && (data.rows || []).length ? 1 : 0 },
			{ label: __("Recalc Console"), action_key: "run_console", enabled: 1, route: "aps-run-console" },
		], async (action) => {
			if (action.action_key === "prepare_demand_baseline") {
				await this.prepareBaseline();
				return;
			}
			if (action.action_key === "save_demand_admission") {
				this.openSaveDialog();
				return;
			}
			await injection_aps.ui.run_action(action);
		});
		injection_aps.ui.render_cards(this.summary, [
			{ label: __("P0 Mandatory"), value: injection_aps.ui.format_number(summary.p0_qty || 0) },
			{ label: __("Stock Covered"), value: injection_aps.ui.format_number(summary.stock_covered_qty || 0) },
			{ label: __("Carried Supply"), value: injection_aps.ui.format_number(summary.carried_qty || 0) },
			{ label: __("New Plan Qty"), value: injection_aps.ui.format_number(summary.new_plan_qty || 0) },
			{ label: __("P1 Candidate"), value: injection_aps.ui.format_number(summary.p1_candidate_qty || 0) },
			{ label: __("P2 Candidate"), value: injection_aps.ui.format_number(summary.p2_candidate_qty || 0) },
		]);
		this.sourceSummary.innerHTML = `
			<div class="ia-run-cell-stack">
				<strong>${__("Run Demand Ownership")}</strong>
				<div class="ia-run-cell-note">${__("Baseline fingerprint")}: ${injection_aps.ui.escape(data.demand_baseline_fingerprint || __("Not prepared"))}</div>
				<div class="ia-run-cell-note">${__("Admission fingerprint")}: ${injection_aps.ui.escape(data.admission_fingerprint || __("Not prepared"))}</div>
				<div class="ia-run-cell-note">${__("Source Runs")}: ${injection_aps.ui.escape((data.source_runs || []).join(", ") || __("None", null, "Injection APS"))}</div>
				<div class="ia-run-cell-note">${__("P0 is locked. P1/P2 changes invalidate any prior capacity analysis and require recalculation.")}</div>
			</div>
		`;
		this.renderRows(data.rows || []);
		injection_aps.ui.set_feedback(
			this.feedback,
			available ? __("Demand admission loaded. Disabled actions show that a baseline or role is still required.") : (data.reason || __("APS V2 is disabled.")),
			available ? "" : "warning"
		);
	}

	renderRows(rows) {
		injection_aps.ui.render_table(
			this.table,
			[
				{ label: __("Class", null, "Injection APS"), fieldname: "admission_class" },
				{ label: __("Customer"), fieldname: "customer" },
				{ label: __("Item"), fieldname: "item_code" },
				{ label: __("Candidate Qty"), fieldname: "candidate_qty" },
				{ label: __("Recommended Qty", null, "Injection APS"), fieldname: "recommended_qty" },
				{ label: __("Selected Qty"), fieldname: "selected_qty" },
				{ label: __("Recommendation"), fieldname: "recommendation_reason" },
			],
			rows,
			(column, value, row) => {
				if (column.fieldname === "admission_class") {
					const tone = value === "P0" ? "red" : value === "P1" ? "orange" : "blue";
					return injection_aps.ui.pill(value, tone);
				}
				if (column.fieldname === "item_code") {
					return injection_aps.ui.route_link(value, `item/${encodeURIComponent(value)}`);
				}
				if (["candidate_qty", "recommended_qty"].includes(column.fieldname)) {
					return injection_aps.ui.escape(injection_aps.ui.format_number(value || 0));
				}
				if (column.fieldname === "selected_qty") {
					if (row.admission_class === "P0") {
						return `<span title="${injection_aps.ui.escape(__("P0 is mandatory and locked."))}">${injection_aps.ui.escape(injection_aps.ui.format_number(value || 0))} 🔒</span>`;
					}
					return `<input class="form-control input-xs" type="number" min="0" max="${Number(row.candidate_qty || 0)}" step="any" value="${Number(value || 0)}" data-admission-name="${injection_aps.ui.escape(row.name)}">`;
				}
				return injection_aps.ui.escape(value || "-");
			},
			{ exportable: true, export_title: __("Demand Admission Workbench"), export_sheet_name: __("Admission", null, "Injection APS") }
		);
	}

	async prepareBaseline() {
		const runName = this.runField.get_value();
		if (!runName) {
			frappe.msgprint(__("Select a Planning Run first."));
			return;
		}
		const result = await injection_aps.ui.xcall(
			{
				message: __("Preparing demand ownership and stock coverage..."),
				success_message: __("Demand baseline prepared."),
				busy_key: `prepare-demand:${runName}`,
				feedback_target: this.feedback,
			},
			"injection_aps.api.app.prepare_run_demand_baseline",
			{ planning_run: runName, input_fingerprint: (this.data || {}).demand_baseline_fingerprint || undefined }
		);
		if (result) {
			await this.refresh();
		}
	}

	collectDecisions() {
		const values = new Map((this.data.rows || []).map((row) => [row.name, Number(row.selected_qty || 0)]));
		this.table.querySelectorAll("[data-admission-name]").forEach((node) => {
			values.set(node.dataset.admissionName, Number(node.value || 0));
		});
		return Array.from(values.entries()).map(([name, selected_qty]) => ({ name, selected_qty }));
	}

	openSaveDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Confirm P1/P2 Admission"),
			fields: [
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Decision Reason"), reqd: 1, description: __("Explain why the optional demand is selected or left out.") },
			],
			primary_action_label: __("Save Selection"),
			primary_action: async (values) => {
				const result = await injection_aps.ui.xcall(
					{
						message: __("Saving admission decisions..."),
						success_message: __("Admission decisions saved; prior capacity analysis was invalidated."),
						busy_key: `save-admission:${this.runField.get_value()}`,
						feedback_target: this.feedback,
					},
					"injection_aps.api.app.save_demand_admission_decisions",
					{
						planning_run: this.runField.get_value(),
						decisions: this.collectDecisions(),
						input_fingerprint: this.data.admission_fingerprint,
						reason: values.reason,
					}
				);
				if (result) {
					dialog.hide();
					await this.refresh();
				}
			},
		});
		dialog.show();
	}

	renderEmpty(message, tone) {
		this.data = null;
		injection_aps.ui.render_status_line(this.statusHost, { current_step: __("Demand Admission"), next_step: __("Select a Run"), blocking_reason: message });
		injection_aps.ui.render_actions(this.actionHost, [{ label: __("Prepare Demand Baseline"), action_key: "prepare_demand_baseline", enabled: 0 }], () => {});
		injection_aps.ui.render_cards(this.summary, []);
		this.sourceSummary.innerHTML = "";
		injection_aps.ui.render_table(this.table, [{ label: __("Info"), fieldname: "message" }], []);
		injection_aps.ui.set_feedback(this.feedback, message, tone || "warning");
	}
}
