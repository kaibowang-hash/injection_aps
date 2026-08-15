frappe.pages["aps-shift-replan-center"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_ui_loader.js", () => injection_aps.ui_loader.start("20260815.2", () => initializeShiftReplanCenter(wrapper)));
};

function initializeShiftReplanCenter(wrapper) {
	wrapper.classList.add("ia-app-page");
	injection_aps.ui.ensure_styles();
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("APS Shift Replan Center", null, "Injection APS"), single_column: true });
	const state = { cycle: null };
	const $body = $("<div class='ia-page ia-replan-center'></div>").appendTo(page.body);
	page.add_field({ label: __("Planning Run", null, "Injection APS"), fieldname: "planning_run", fieldtype: "Link", options: "APS Planning Run", reqd: 1 });
	page.add_field({ label: __("Shift Date", null, "Injection APS"), fieldname: "shift_date", fieldtype: "Date", default: frappe.datetime.get_today(), reqd: 1 });
	page.add_field({ label: __("Shift Type", null, "Injection APS"), fieldname: "shift_type", fieldtype: "Select", options: [__("Day Shift", null, "Injection APS"), __("Night Shift", null, "Injection APS")].join("\n"), default: __("Day Shift", null, "Injection APS") });
	page.add_field({ label: __("Cycle Type", null, "Injection APS"), fieldname: "cycle_type", fieldtype: "Select", options: [__("Scheduled Shift", null, "Injection APS"), __("Emergency Manual", null, "Injection APS")].join("\n"), default: __("Scheduled Shift", null, "Injection APS") });
	page.add_field({ label: __("Reason", null, "Injection APS"), fieldname: "reason", fieldtype: "Small Text" });

	if (injection_aps.ui.can_run_action("create_replan_cycle")) {
		page.set_primary_action(__("Create Replan Proposal", null, "Injection APS"), createCycle);
	}
	if (injection_aps.ui.can_run_action("refresh_shift_actuals")) {
		page.add_inner_button(__("Refresh Actuals", null, "Injection APS"), refreshActuals);
	}

	async function refreshActuals() {
		const planningRun = page.fields_dict.planning_run.get_value();
		if (!planningRun) {
			frappe.msgprint(__("Select a Planning Run first.", null, "Injection APS"));
			return;
		}
		await frappe.call({ method: "injection_aps.api.app.refresh_shift_actuals", type: "POST", args: { baseline_run: planningRun }, freeze: true, freeze_message: __("Refreshing formal production actuals...", null, "Injection APS") });
		frappe.show_alert({ message: __("Actual production evidence refreshed.", null, "Injection APS"), indicator: "green" });
	}

	async function createCycle() {
		const planningRun = page.fields_dict.planning_run.get_value();
		if (!planningRun) {
			frappe.msgprint(__("Select a Planning Run first.", null, "Injection APS"));
			return;
		}
		const cycleType = page.fields_dict.cycle_type.get_value();
		const reason = page.fields_dict.reason.get_value();
		if (["Emergency Manual", __("Emergency Manual", null, "Injection APS")].includes(cycleType) && !reason) {
			frappe.msgprint(__("Enter a reason for Emergency Manual replan.", null, "Injection APS"));
			return;
		}
		const response = await frappe.call({
			method: "injection_aps.api.app.create_replan_cycle",
			type: "POST",
			args: {
				baseline_run: planningRun,
				shift_date: page.fields_dict.shift_date.get_value(),
				shift_type: page.fields_dict.shift_type.get_value(),
				cycle_type: cycleType,
				reason,
			},
			freeze: true,
			freeze_message: __("Calculating forecast and shift differences...", null, "Injection APS"),
		});
		state.cycle = response.message || {};
		render();
	}

	async function reasonAction(method, title) {
		const values = await new Promise((resolve) => {
			frappe.prompt([{ label: __("Reason", null, "Injection APS"), fieldname: "reason", fieldtype: "Small Text", reqd: 1 }], resolve, title);
		});
		return callCycle(method, { reason: values.reason });
	}

	async function callCycle(method, extraArgs = {}) {
		const response = await frappe.call({
			method,
			type: "POST",
			args: {
				replan_cycle: state.cycle.name,
				expected_fingerprint: state.cycle.solution_fingerprint,
				...extraArgs,
			},
			freeze: true,
		});
		state.cycle = response.message || state.cycle;
		render();
	}

	function actionButton(label, action, { disabled = false, reason = "", primary = false, actionKey = "" } = {}) {
		if (actionKey && !injection_aps.ui.can_run_action(actionKey)) {
			disabled = true;
			reason = __("Your role cannot perform this action.", null, "Injection APS");
		}
		const $button = $(`<button type="button" class="btn ${primary ? "btn-primary" : "btn-default"} btn-sm" ${disabled ? "disabled" : ""} title="${frappe.utils.escape_html(reason || "")}">${label}</button>`);
		if (!disabled) $button.on("click", action);
		return $button;
	}

	function renderActions($target) {
		const cycle = state.cycle;
		const hasShiftProposal = Boolean(cycle.shift_schedule_proposal_batch);
		$target.append(actionButton(__("Acknowledge Freshness / Fallback", null, "Injection APS"), () => reasonAction("injection_aps.api.app.acknowledge_replan_fallback", __("Acknowledge Replan Risk", null, "Injection APS")), {
			actionKey: "acknowledge_replan_fallback",
			disabled: cycle.status !== "Acknowledgment Required",
			reason: cycle.status !== "Acknowledgment Required" ? __("No freshness or fallback acknowledgment is currently required.", null, "Injection APS") : "",
		}));
		$target.append(actionButton(__("Generate Review Proposals", null, "Injection APS"), () => callCycle("injection_aps.api.app.generate_replan_proposals"), {
			actionKey: "generate_replan_proposals",
			disabled: !["Proposal Ready", "Approved"].includes(cycle.status) || hasShiftProposal,
			reason: hasShiftProposal ? __("The Shift Schedule proposal already exists.", null, "Injection APS") : cycle.status === "Acknowledgment Required" ? __("Acknowledge freshness or fallback risk first.", null, "Injection APS") : "",
		}));
		$target.append(actionButton(__("Approve Replan", null, "Injection APS"), () => reasonAction("injection_aps.api.app.approve_replan_cycle", __("Approve Replan", null, "Injection APS")), {
			actionKey: "approve_replan_cycle",
			disabled: cycle.status !== "Proposal Ready" || !hasShiftProposal,
			reason: !hasShiftProposal ? __("Generate and review the Shift Schedule proposal first.", null, "Injection APS") : "",
		}));
		$target.append(actionButton(__("Apply Reviewed Proposal", null, "Injection APS"), () => callCycle("injection_aps.api.app.apply_replan_cycle"), {
			actionKey: "apply_replan_cycle",
			disabled: cycle.status !== "Approved",
			reason: cycle.status !== "Approved" ? __("The replan must be approved by GMC before Apply.", null, "Injection APS") : "",
			primary: true,
		}));
		if (hasShiftProposal) {
			$target.append(actionButton(__("Open Shift Proposal", null, "Injection APS"), () => frappe.set_route("Form", "APS Shift Schedule Proposal Batch", cycle.shift_schedule_proposal_batch)));
		}
	}

	function render() {
		if (!state.cycle) {
			$body.html(`<div class="text-muted">${__("Create a proposal to compare Current Plan, Forecast, and the next-shift recommendation.", null, "Injection APS")}</div>`);
			return;
		}
		const rows = state.cycle.diffs || [];
		$body.html(`
			<div class="ia-card"><h4>${frappe.utils.escape_html(state.cycle.name || "")}</h4>
			<p>${__("Status", null, "Injection APS")}: ${__(state.cycle.status || "", null, "Injection APS")} · ${__("Freshness", null, "Injection APS")}: ${__(state.cycle.freshness_status || "", null, "Injection APS")} (${state.cycle.freshness_minutes || 0} ${__("minutes", null, "Injection APS")})</p>
			<div class="ia-replan-actions d-flex flex-wrap" style="gap: 8px;"></div></div>
			<div class="table-responsive"><table class="table table-bordered"><thead><tr>
			<th>${__("Segment", null, "Injection APS")}</th><th>${__("Difference", null, "Injection APS")}</th><th>${__("Current Start", null, "Injection APS")}</th><th>${__("Current End", null, "Injection APS")}</th><th>${__("Forecast End", null, "Injection APS")}</th><th>${__("Proposed Start", null, "Injection APS")}</th><th>${__("Proposed End", null, "Injection APS")}</th><th>${__("Reason", null, "Injection APS")}</th>
			</tr></thead><tbody>${rows.map((row) => `<tr class="${row.is_frozen ? "text-muted" : ""}"><td>${frappe.utils.escape_html(row.segment || "")}</td><td>${__(row.diff_type || "", null, "Injection APS")}</td><td>${frappe.datetime.str_to_user(row.current_start_time)}</td><td>${frappe.datetime.str_to_user(row.current_end_time)}</td><td>${frappe.datetime.str_to_user(row.forecast_end_time)}</td><td>${frappe.datetime.str_to_user(row.proposed_start_time)}</td><td>${frappe.datetime.str_to_user(row.proposed_end_time)}</td><td>${frappe.utils.escape_html(row.reason || "")}</td></tr>`).join("")}</tbody></table></div>`);
		renderActions($body.find(".ia-replan-actions"));
	}

	render();
}
