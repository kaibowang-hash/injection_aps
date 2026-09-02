frappe.pages["aps-solver-scenario-comparison"].on_page_load = function (wrapper) {
	injection_aps.ui_loader.start("20260901.1", () => initializeSolverComparison(wrapper));
};

function initializeSolverComparison(wrapper) {
	wrapper.classList.add("ia-app-page");
	injection_aps.ui.ensure_styles();
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("APS Solver Scenario Comparison", null, "Injection APS"), single_column: true });
	const state = { run: frappe.utils.get_url_arg("run_name") || "", data: null, trialComparison: null };
	const runField = page.add_field({ label: __("Planning Run", null, "Injection APS"), fieldname: "planning_run", fieldtype: "Link", options: "APS Planning Run", default: state.run,
		change: () => { state.run = runField.get_value(); load(); } });

	async function load() {
		if (!state.run) {
			page.main.html(`<div class="text-muted">${__("Select a Planning Run.", null, "Injection APS")}</div>`);
			return;
		}
		[state.data, state.trialComparison] = await Promise.all([
			frappe.xcall("injection_aps.api.app.get_solver_scenarios", { planning_run: state.run }),
			frappe.xcall("injection_aps.api.app.get_legacy_v2_comparison", { planning_run: state.run }),
		]);
		render();
	}

	function number(value) { return frappe.format(value || 0, { fieldtype: "Float", precision: 2 }); }
	function optionalNumber(value) { return value == null ? "-" : number(value); }
	function dateTime(value) { return value ? frappe.utils.escape_html(frappe.datetime.str_to_user(value)) : "-"; }
	function renderScenarioDetails(row) {
		const messages = []
			.concat(row.explanation || [])
			.concat(row.warnings || []);
		const taskRows = (row.tasks || []).map((task) => `<tr>
			<td>${frappe.utils.escape_html(task.result || task.demand_key || "-")}</td>
			<td>${frappe.utils.escape_html(task.workstation || "-")}</td>
			<td>${frappe.utils.escape_html(task.mold || "-")}</td>
			<td>${number(task.qty)}</td>
			<td>${number(task.cycles)}</td>
			<td>${dateTime(task.occupied_start)}</td>
			<td>${dateTime(task.production_start)}</td>
			<td>${dateTime(task.end)}</td>
			<td>${number(task.setup_minutes)} / ${number(task.changeover_minutes)}</td>
			<td>${frappe.utils.escape_html(__(task.horizon_zone || "-", null, "Injection APS"))}</td>
		</tr>`).join("");
		return `<details class="card mb-3" ${row.scenario_key === state.data.selected_scenario ? "open" : ""}>
			<summary class="card-header"><b>${frappe.utils.escape_html(__(row.scenario_label || row.scenario_key, null, "Injection APS"))}</b> · ${__("Proposed tasks before Apply", null, "Injection APS")} (${(row.tasks || []).length})</summary>
			<div class="card-body">
				${messages.length ? `<div class="alert ${row.valid === false ? "alert-danger" : "alert-secondary"}">${messages.map((message) => frappe.utils.escape_html(__(message, null, "Injection APS"))).join("<br>")}</div>` : ""}
				<div class="small text-muted mb-2">${__("These rows are a preview only. No production segment is changed until a Formal run is applied by an authorized approver.", null, "Injection APS")}</div>
				<div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr>
					<th>${__("Result", null, "Injection APS")}</th><th>${__("Workstation", null, "Injection APS")}</th><th>${__("Mold", null, "Injection APS")}</th>
					<th>${__("Qty", null, "Injection APS")}</th><th>${__("Cycles", null, "Injection APS")}</th><th>${__("Occupied Start", null, "Injection APS")}</th>
					<th>${__("Production Start", null, "Injection APS")}</th><th>${__("End", null, "Injection APS")}</th><th>${__("Setup / Changeover", null, "Injection APS")}</th><th>${__("Zone", null, "Injection APS")}</th>
				</tr></thead><tbody>${taskRows || `<tr><td colspan="10" class="text-muted">${__("No proposed task is available for this scenario.", null, "Injection APS")}</td></tr>`}</tbody></table></div>
			</div>
		</details>`;
	}
	function renderTrialComparison() {
		const comparison = state.trialComparison || {};
		if (comparison.status !== "Ready") {
			return `<div class="alert alert-secondary"><b>${__("Legacy / V2 Trial Comparison", null, "Injection APS")}</b><br>${frappe.utils.escape_html(__(comparison.message || "Trial comparison is not available for this run.", null, "Injection APS"))}</div>`;
		}
		const legacy = (comparison.legacy || {}).metrics || {};
		const v2 = (comparison.v2 || {}).metrics || {};
		const delta = comparison.delta || {};
		const metrics = [
			[__("Scheduled Qty", null, "Injection APS"), "scheduled_qty"],
			[__("P0 On Time Qty", null, "Injection APS"), "on_time_qty"],
			[__("Late Qty", null, "Injection APS"), "late_qty"],
			[__("Critical Unplanned", null, "Injection APS"), "critical_unplanned_qty"],
			[__("Changeovers", null, "Injection APS"), "change_count"],
			[__("Setup Minutes", null, "Injection APS"), "setup_minutes"],
		];
		const rows = metrics.map(([label, key]) => `<tr><td>${label}</td><td>${optionalNumber(legacy[key])}</td><td>${optionalNumber(v2[key])}</td><td>${optionalNumber(delta[key])}</td></tr>`).join("");
		const readOnlyNotice = comparison.run_type === "Trial"
			? `<div class="alert alert-warning">${__("Trial comparison is read-only; only a Formal run can be applied.", null, "Injection APS")}</div>`
			: "";
		return `${readOnlyNotice}<div class="card mb-3"><div class="card-body"><h5>${__("Legacy / V2 Trial Comparison", null, "Injection APS")}</h5>
			<div class="small text-muted mb-2">${__("The Legacy fingerprint was captured before V2 projection and is retained in the Solver Job audit record.", null, "Injection APS")}</div>
			<div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Metric", null, "Injection APS")}</th><th>Legacy</th><th>V2</th><th>${__("Delta (V2 - Legacy)", null, "Injection APS")}</th></tr></thead><tbody>${rows}</tbody></table></div>
		</div></div>`;
	}
	function render() {
		const scale = Number(state.data.quantity_scale || 1000);
		const rows = (state.data.scenarios || []).map((row) => {
			const metrics = row.metrics || {};
			const selected = row.scenario_key === state.data.selected_scenario;
			const selectable = !state.data.selection_locked && row.valid === true && row.status !== "Failed" && injection_aps.ui.can_run_action("select_solver_scenario");
			const invalidReason = state.data.selection_locked
				? state.data.selection_locked_reason
				: row.valid === false || row.status === "Failed"
					? __("Independent validation failed. Review the warnings; this scenario cannot be selected.", null, "Injection APS")
					: __("Your role cannot select solver scenarios.", null, "Injection APS");
			return `<tr class="${selected ? "bg-light" : ""}">
				<td><b>${frappe.utils.escape_html(__(row.scenario_label || row.scenario_key, null, "Injection APS"))}</b><div class="small text-muted">${frappe.utils.escape_html(row.engine || "-")} / ${frappe.utils.escape_html(row.status || "-")}</div>${row.valid === false ? `<span class="indicator-pill red">${__("Invalid", null, "Injection APS")}</span>` : ""}</td>
				<td>${number((metrics.p0_on_time_units || 0) / scale)}</td>
				<td>${number((metrics.total_late_units || 0) / scale)} / ${number((metrics.max_lateness_minutes || 0) / 60)}h</td>
				<td>${number((metrics.p0_critical_unplanned_units || 0) / scale)}</td>
				<td>${number(metrics.change_count)} / ${number(metrics.setup_minutes)}</td>
				<td>${number(metrics.utilization_spread_minutes)}</td>
				<td>${number((metrics.p1_p2_completed_units || 0) / scale)}</td>
				<td>${row.gap_percent == null ? "-" : `${number(row.gap_percent)}%`}</td>
				<td>${selected ? `<span class="indicator-pill green">${__("Selected", null, "Injection APS")}</span>` : `<button type="button" class="btn btn-xs btn-primary" data-select="${row.scenario_key}" ${selectable ? "" : "disabled"} title="${frappe.utils.escape_html(invalidReason)}">${__("Select", null, "Injection APS")}</button>`}</td>
			</tr>`;
		}).join("");
		const details = (state.data.scenarios || []).map(renderScenarioDetails).join("");
		const selectionNotice = state.data.selection_locked
			? `<div class="alert alert-warning">${frappe.utils.escape_html(state.data.selection_locked_reason)}</div>`
			: "";
		page.main.html(`${renderTrialComparison()}${selectionNotice}<div class="alert alert-info">${__("Delivery objectives are solved first. Efficiency objectives may only break delivery-equivalent ties.", null, "Injection APS")}</div>
			<div class="table-responsive"><table class="table table-bordered"><thead><tr>
			<th>${__("Scenario", null, "Injection APS")}</th><th>${__("P0 On Time Qty", null, "Injection APS")}</th><th>${__("Late Qty / Max Delay", null, "Injection APS")}</th>
			<th>${__("Critical Unplanned", null, "Injection APS")}</th><th>${__("Changeovers / Minutes", null, "Injection APS")}</th><th>${__("Utilization Spread", null, "Injection APS")}</th>
			<th>${__("P1/P2 Completed", null, "Injection APS")}</th><th>${__("Gap", null, "Injection APS")}</th><th>${__("Action", null, "Injection APS")}</th>
			</tr></thead><tbody>${rows || `<tr><td colspan="9" class="text-muted">${__("No validated solver scenario is available.", null, "Injection APS")}</td></tr>`}</tbody></table></div>${details}`);
		page.main.find("[data-select]").on("click", async function () {
			const scenario = this.dataset.select;
			let reason = "";
			if (scenario !== "recommended") reason = await promptReason();
			if (scenario !== "recommended" && !reason) return;
			await frappe.xcall("injection_aps.api.app.select_solver_scenario", { planning_run: state.run, scenario_key: scenario, reason, expected_fingerprint: state.data.input_fingerprint });
			await load();
		});
	}

	function promptReason() {
		return new Promise((resolve) => {
			const dialog = new frappe.ui.Dialog({ title: __("Reason for Non-Recommended Scenario", null, "Injection APS"), fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason", null, "Injection APS"), reqd: 1 }],
				primary_action_label: __("Select Scenario", null, "Injection APS"), primary_action: (values) => { dialog.hide(); resolve(values.reason); } });
			dialog.show();
		});
	}
	load();
}
