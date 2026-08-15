const PLANNING_RUN_SHARED_READY = frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Planning Run", {
	async refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		await PLANNING_RUN_SHARED_READY;
		injection_aps.ui.ensure_styles();
		try {
			const capabilities = await frappe.xcall("injection_aps.api.app.get_v2_capabilities");
			frm.__aps_v2_enabled = Number(((capabilities || {}).settings || {}).enable_aps_v2 || 0) === 1;
			frm.__aps_solver_engine = ((capabilities || {}).settings || {}).solver_engine || "Legacy";
			frm.__aps_multilevel_bom = Number(((capabilities || {}).settings || {}).enable_multilevel_bom_planning || 0) === 1;
		} catch (error) {
			frm.__aps_v2_enabled = false;
			frm.__aps_solver_engine = "Legacy";
			frm.__aps_multilevel_bom = false;
		}
		await render_flow(frm);
		render_quantity_indicators(frm);
		render_v2_horizons(frm);
		render_capacity_analysis(frm);
		add_actions(frm);
	},
});

function render_quantity_indicators(frm) {
	frm.dashboard.add_indicator(`${__("Prebuild", null, "Injection APS")}: ${injection_aps.ui.format_number(frm.doc.total_prebuild_qty || 0)}`, "orange");
	frm.dashboard.add_indicator(`${__("JIT", null, "Injection APS")}: ${injection_aps.ui.format_number(frm.doc.total_jit_qty || 0)}`, "blue");
	frm.dashboard.add_indicator(`${__("Current Deliverable", null, "Injection APS")}: ${injection_aps.ui.format_number(frm.doc.total_current_deliverable_qty || 0)}`, "green");
	frm.dashboard.add_indicator(`${__("Actual / Scrap", null, "Injection APS")}: ${injection_aps.ui.format_number(frm.doc.total_produced_qty || 0)} / ${injection_aps.ui.format_number(frm.doc.total_scrap_qty || 0)}`, "gray");
	if (Number(frm.doc.total_cancellation_inventory_risk_qty || 0) > 0) {
		frm.dashboard.add_indicator(`${__("Cancel Stock Risk", null, "Injection APS")}: ${injection_aps.ui.format_number(frm.doc.total_cancellation_inventory_risk_qty || 0)}`, "red");
	}
}

function render_v2_horizons(frm) {
	frm.dashboard.parent.find(".ia-v2-horizon-section").remove();
	if (!frm.__aps_v2_enabled) return;
	const cells = [
		[__("Overdue", null, "Injection APS"), `${frm.doc.demand_horizon_start_date || "-"} ${__("and earlier open P0", null, "Injection APS")}`],
		[__("Demand", null, "Injection APS"), `${frm.doc.demand_horizon_start_date || "-"} → ${frm.doc.demand_horizon_end_date || "-"}`],
		[__("Freeze", null, "Injection APS"), `${frm.doc.demand_horizon_start_date || "-"} → ${frm.doc.freeze_horizon_end_date || "-"}`],
		[__("Restricted", null, "Injection APS"), `${frm.doc.freeze_horizon_end_date || "-"} → ${frm.doc.restricted_horizon_end_date || "-"}`],
		[__("Recovery", null, "Injection APS"), `${frm.doc.recovery_horizon_start_date || "-"} → ${frm.doc.recovery_horizon_end_date || "-"}`],
	];
	const html = `<div class="row">${cells.map(([label, value]) => `<div class="col-sm-4 mb-2"><b>${label}</b><div class="text-muted">${injection_aps.ui.escape(value)}</div></div>`).join("")}</div>`;
	frm.dashboard.add_section(html, __("APS V2 Horizons", null, "Injection APS"), "custom ia-v2-horizon-section");
	frm.dashboard.show();
}

function get_capacity_analysis(frm) {
	if (!frm.doc.capacity_balance_analysis_json) {
		return null;
	}
	try {
		return JSON.parse(frm.doc.capacity_balance_analysis_json);
	} catch (error) {
		console.error(error);
		return null;
	}
}

function get_capacity_evidence_rows(frm) {
	const analysis = get_capacity_analysis(frm);
	if (!analysis) {
		return [];
	}
	return (analysis.demands || []).flatMap((row) => {
		const importantChecks = (row.checks || []).filter((check) => ["warning", "blocked", "failed"].includes(check.status));
		const reasons = []
			.concat(row.confirmation_reasons || [])
			.concat(importantChecks.map((check) => check.message))
			.filter(Boolean)
			.map((reason) => injection_aps.ui.translate(reason));
		const base = Object.assign({}, row, { display_reasons: [...new Set(reasons)] });
		if (!(row.allocations || []).length) {
			return [base];
		}
		return row.allocations.map((allocation, index) => Object.assign({}, base, {
			workstation: allocation.workstation,
			mold: allocation.mold,
			planned_qty: allocation.qty,
			late_qty: allocation.delivery_status === "Late" ? allocation.qty : 0,
			unscheduled_qty: index === 0 ? row.unscheduled_qty : 0,
			proposed_start: allocation.production_start,
			proposed_end: allocation.end,
			setup_minutes: allocation.setup_minutes,
			changeover_minutes: allocation.changeover_minutes,
			horizon_zone: allocation.horizon_zone,
			provisional: 1,
		}));
	});
}

function get_impacted_capacity_rows(frm) {
	return get_capacity_evidence_rows(frm).filter(
		(row) =>
			Number(row.requires_confirmation || 0) === 1 ||
			row.status === "Blocked" ||
			Number(row.late_qty || 0) > 0 ||
			Number(row.unscheduled_qty || 0) > 0 ||
			(row.display_reasons || []).length
	);
}

function build_capacity_confirmation_lines(frm) {
	const analysis = get_capacity_analysis(frm) || {};
	const summary = analysis.summary || {};
	const impactedRows = get_impacted_capacity_rows(frm);
	const lines = [
		__("Prebuild Qty: {0}").replace("{0}", injection_aps.ui.format_number(summary.prebuild_qty || frm.doc.total_prebuild_qty || 0)),
		__("JIT Qty: {0}").replace("{0}", injection_aps.ui.format_number(summary.jit_qty || frm.doc.total_jit_qty || 0)),
		__("Late / Unscheduled Qty: {0} / {1}")
			.replace("{0}", injection_aps.ui.format_number(summary.late_qty || 0))
			.replace("{1}", injection_aps.ui.format_number(summary.unscheduled_qty || 0)),
	];
	impactedRows.slice(0, 12).forEach((row) => {
		lines.push(
			__("{0} @ {1}: Plan {2}; Prebuild {3}; JIT {4}; Late {5}; Unscheduled {6}; {7}")
				.replace("{0}", row.result || row.segment || "-")
				.replace("{1}", row.workstation || "-")
				.replace("{2}", injection_aps.ui.format_number(row.planned_qty || 0))
				.replace("{3}", injection_aps.ui.format_number(row.prebuild_qty || 0))
				.replace("{4}", injection_aps.ui.format_number(row.jit_qty || 0))
				.replace("{5}", injection_aps.ui.format_number(row.late_qty || 0))
				.replace("{6}", injection_aps.ui.format_number(row.unscheduled_qty || 0))
				.replace("{7}", (row.display_reasons || []).join("；") || injection_aps.ui.translate(row.status || "-"))
		);
	});
	if (impactedRows.length > 12) {
		lines.push(__("{0} additional affected demand rows are shown in Capacity Analysis.").replace("{0}", String(impactedRows.length - 12)));
	}
	return lines;
}

function render_capacity_analysis(frm) {
	frm.dashboard.parent.find(".ia-capacity-analysis-section").remove();
	const analysis = get_capacity_analysis(frm);
	if (!analysis) {
		return;
	}
	const summary = analysis.summary || {};
	const readiness = analysis.readiness_status || frm.doc.capacity_balance_status || "-";
	const allRows = get_capacity_evidence_rows(frm);
	const impactedRows = get_impacted_capacity_rows(frm);
	const rows = (impactedRows.length ? impactedRows : allRows).slice(0, 50);
	const body = rows.length
		? rows
				.map(
					(row) => `
						<tr>
							<td>${injection_aps.ui.escape(row.result || row.segment || "-")}</td>
							<td>${injection_aps.ui.escape(row.workstation || "-")}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.planned_qty || 0))}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.prebuild_qty || 0))}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.jit_qty || 0))}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.late_qty || 0))}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.unscheduled_qty || 0))}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.proposed_start || ""))}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.proposed_end || ""))}</td>
							<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.status || "-"))}</td>
							<td>${injection_aps.ui.escape((row.display_reasons || []).join("；") || "-")}</td>
						</tr>
					`
				)
				.join("")
		: `<tr><td colspan="11" class="text-muted">${__("No affected capacity demand rows.")}</td></tr>`;
	const html = `
		<div class="alert ${readiness === "Hard Blocked" ? "alert-danger" : readiness === "Acknowledgment Required" ? "alert-warning" : "alert-success"}">
			<b>${__("Readiness", null, "Injection APS")}: ${injection_aps.ui.escape(injection_aps.ui.translate(readiness))}</b><br>
			${injection_aps.ui.escape(injection_aps.ui.translate(analysis.next_action || ""))}
		</div>
		<div class="small text-muted mb-2">
			${Number(analysis.solver_v2 || 0) === 1 ? `<b>${__("Proposed tasks before Apply", null, "Injection APS")}</b> · ${__("These rows are read-only until Apply.", null, "Injection APS")}<br>` : ""}
			${__("Prebuild", null, "Injection APS")}: ${injection_aps.ui.escape(injection_aps.ui.format_number(summary.prebuild_qty || 0))} ·
			${__("JIT", null, "Injection APS")}: ${injection_aps.ui.escape(injection_aps.ui.format_number(summary.jit_qty || 0))} ·
			${__("Late", null, "Injection APS")}: ${injection_aps.ui.escape(injection_aps.ui.format_number(summary.late_qty || 0))} ·
			${__("Unscheduled", null, "Injection APS")}: ${injection_aps.ui.escape(injection_aps.ui.format_number(summary.unscheduled_qty || 0))} ·
			${__("Confirmation Required", null, "Injection APS")}: ${injection_aps.ui.escape(String(summary.requires_confirmation || 0))}
		</div>
		<div class="table-responsive">
			<table class="table table-bordered table-sm">
				<thead><tr>
					<th>${__("Result", null, "Injection APS")}</th>
					<th>${__("Workstation", null, "Injection APS")}</th>
					<th>${__("Plan", null, "Injection APS")}</th>
					<th>${__("Prebuild", null, "Injection APS")}</th>
					<th>${__("JIT", null, "Injection APS")}</th>
					<th>${__("Late", null, "Injection APS")}</th>
					<th>${__("Unscheduled", null, "Injection APS")}</th>
					<th>${__("Proposed Start", null, "Injection APS")}</th>
					<th>${__("Proposed End", null, "Injection APS")}</th>
					<th>${__("Status", null, "Injection APS")}</th>
					<th>${__("Reason", null, "Injection APS")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table>
		</div>
		${(impactedRows.length || allRows.length) > 50 ? `<div class="text-muted small">${__("Only the first 50 capacity demand rows are shown.")}</div>` : ""}
	`;
	frm.dashboard.add_section(html, __("Capacity Analysis", null, "Injection APS"), "custom ia-capacity-analysis-section");
	frm.dashboard.show();
}

async function render_flow(frm) {
	try {
		const context = await frappe.xcall("injection_aps.api.app.get_next_actions_for_context", {
			doctype: frm.doctype,
			docname: frm.doc.name,
		});
		const status = document.createElement("div");
		injection_aps.ui.render_status_line(status, context);
		frm.dashboard.set_headline(status.outerHTML);
	} catch (error) {
		console.error(error);
	}
}

function add_actions(frm) {
	frm.clear_custom_buttons();

	const addButton = (label, fn, group, type, actionKey) => {
		if (actionKey && !injection_aps.ui.can_run_action(actionKey)) {
			return null;
		}
		const button = frm.add_custom_button(__(label, null, "Injection APS"), fn, group ? __(group, null, "Injection APS") : undefined);
		if (type) {
			frm.change_custom_button_type(
				__(label, null, "Injection APS"),
				group ? __(group, null, "Injection APS") : undefined,
				type
			);
		}
		return button;
	};

	const confirmAndCall = async (action, options, method, args) => {
		let existingWorkOrderPolicy = null;
		if (Number(action.requires_existing_work_order_policy || 0) === 1) {
			existingWorkOrderPolicy = await injection_aps.ui.confirm_net_requirement_calculation(action, options);
			if (!existingWorkOrderPolicy) {
				return null;
			}
		} else {
			const confirmed = await injection_aps.ui.confirm_action(action, options);
			if (!confirmed) {
				return null;
			}
		}
		const callArgs = Object.assign({}, args || {});
		if (existingWorkOrderPolicy) {
			callArgs.existing_work_order_policy = existingWorkOrderPolicy;
		}
		return injection_aps.ui.xcall(options || {}, method, callArgs);
	};

	if (["Draft", "Planned", "Risk"].includes(frm.doc.status || "Draft")) {
		addButton("Recalculate", async () => {
			const result = await confirmAndCall(
				{ action_key: "run_trial", confirm_required: 1, requires_existing_work_order_policy: 1 },
				{
					title: __("Confirm Recalculate"),
					summary_lines: [
						__("APS Run: {0}").replace("{0}", frm.doc.name),
						__("The current results will be recalculated from the latest demand."),
					],
					message: __("Running APS planning..."),
					success_message: __("Planning run completed."),
					busy_key: `planning-run:${frm.doc.name}`,
				},
				"injection_aps.api.app.run_planning_run",
				{
					run_name: frm.doc.name,
				}
			);
			if (!result) {
				return;
			}
			injection_aps.ui.show_warnings(result, __("Planning Warnings"), "preflight_warning_count");
			await frm.reload_doc();
		}, null, "primary", "run_trial");
	}

	if (["Draft", "Planned", "Risk"].includes(frm.doc.status || "Draft") && !["Applied", "Applied with Exceptions"].includes(frm.doc.capacity_balance_status)) {
		addButton("Analyze Capacity", async () => {
			const response = await injection_aps.ui.xcall(
				{
					message: __("Analyzing shift capacity..."),
					success_message: __("Capacity proposal refreshed."),
					busy_key: `planning-capacity-analyze:${frm.doc.name}`,
				},
				frm.__aps_v2_enabled && frm.__aps_solver_engine === "CP-SAT"
					? "injection_aps.api.app.analyze_v2_schedule"
					: "injection_aps.api.app.analyze_capacity_balance",
				frm.__aps_v2_enabled && frm.__aps_solver_engine === "CP-SAT"
					? { run_name: frm.doc.name, run_in_background: 1 }
					: { run_name: frm.doc.name }
			);
			if (response) {
				await frm.reload_doc();
			}
		}, "Capacity", null, "analyze_capacity_balance");
	}

	if (frm.__aps_v2_enabled && frm.__aps_solver_engine === "CP-SAT" && frm.doc.solver_job) {
		addButton("Compare Solver Scenarios", () => {
			frappe.set_route("aps-solver-scenario-comparison", { run_name: frm.doc.name });
		}, "APS V2", null);
	}

	if (frm.__aps_v2_enabled && frm.__aps_multilevel_bom) {
		addButton("BOM Tree", () => show_bom_tree(frm), "APS V2", null);
		if (["Draft", "Planned"].includes(frm.doc.status || "Draft") && injection_aps.ui.can_run_action("analyze_capacity_balance")) {
			addButton("BOM Decisions", () => show_bom_decisions(frm), "APS V2", null);
		}
	}

	if (!frm.__aps_v2_enabled && frm.doc.capacity_balance_status === "Confirmation Required" && !frm.doc.capacity_balance_confirmed_by) {
		addButton("PMC Confirm", async () => {
			const response = await confirmAndCall(
				{ action_key: "confirm_capacity_balance", confirm_required: 1 },
				{
					title: __("Confirm Capacity Suggestion"),
					summary_lines: build_capacity_confirmation_lines(frm),
					message: __("Recording PMC confirmation..."),
					success_message: __("Capacity proposal confirmed."),
					busy_key: `planning-capacity-confirm:${frm.doc.name}`,
				},
				"injection_aps.api.app.confirm_capacity_balance",
				{ run_name: frm.doc.name }
			);
			if (response) {
				await frm.reload_doc();
			}
		}, "Capacity", null, "confirm_capacity_balance");
	}

	if (frm.__aps_v2_enabled && frm.doc.capacity_balance_status === "Acknowledgment Required" && !frm.doc.capacity_balance_confirmed_by) {
		addButton("Review and Acknowledge", async () => {
			if (frm.__aps_solver_engine === "CP-SAT") {
				const reason = await prompt_solver_reason(__("Acknowledge APS Schedule Risks", null, "Injection APS"));
				if (!reason) return;
				const response = await injection_aps.ui.xcall(
					{ message: __("Recording risk acknowledgment...", null, "Injection APS"), success_message: __("Schedule risks acknowledged.", null, "Injection APS"), busy_key: `planning-solver-acknowledge:${frm.doc.name}` },
					"injection_aps.api.app.acknowledge_schedule_risks",
					{ planning_run: frm.doc.name, reason, expected_fingerprint: frm.doc.solver_input_fingerprint }
				);
				if (response) await frm.reload_doc();
				return;
			}
			const response = await confirmAndCall(
				{ action_key: "confirm_capacity_balance", confirm_required: 1 },
				{
					title: __("Acknowledge APS Schedule Risks", null, "Injection APS"),
					summary_lines: build_capacity_confirmation_lines(frm),
					message: __("Recording risk acknowledgment...", null, "Injection APS"),
					success_message: __("Schedule risks acknowledged.", null, "Injection APS"),
					busy_key: `planning-capacity-acknowledge:${frm.doc.name}`,
				},
				"injection_aps.api.app.confirm_capacity_balance",
				{ run_name: frm.doc.name }
			);
			if (response) await frm.reload_doc();
		}, "APS V2", null, frm.__aps_solver_engine === "CP-SAT" ? "acknowledge_schedule_risks" : "confirm_capacity_balance");
	}

	if (frm.__aps_v2_enabled && frm.doc.capacity_balance_status === "Hard Blocked") {
		addButton("Open Resolution Center", () => {
			frappe.set_route("aps-constraint-resolution-center", { run_name: frm.doc.name });
		}, "APS V2", "primary");
	}

	const capacityReadyToApply = (
		frm.doc.capacity_balance_status === "Suggestion Ready" ||
		(frm.doc.capacity_balance_status === "Confirmation Required" && frm.doc.capacity_balance_confirmed_by) ||
		(frm.__aps_v2_enabled && frm.doc.capacity_balance_status === "Ready") ||
		(frm.__aps_v2_enabled && frm.doc.capacity_balance_status === "Acknowledgment Required" && frm.doc.capacity_balance_confirmed_by)
	);
	const v2TrialIsReadOnly = frm.__aps_v2_enabled && frm.__aps_solver_engine === "CP-SAT" && frm.doc.run_type !== "Formal";
	if (capacityReadyToApply && v2TrialIsReadOnly) {
		const reason = __("Trial runs are read-only. Create or approve a Formal run before applying the V2 schedule.", null, "Injection APS");
		const button = addButton("Apply Balance", () => {}, "Capacity", "primary", "apply_v2_schedule");
		if (button) {
			button.prop("disabled", true).attr("title", reason).attr("aria-label", reason);
		}
	}

	if (capacityReadyToApply && !v2TrialIsReadOnly) {
		addButton("Apply Balance", async () => {
			const response = await confirmAndCall(
				{ action_key: "apply_capacity_balance", confirm_required: 1 },
				{
					title: __("Apply Capacity Balance"),
					summary_lines: [
						__("Prebuild Qty: {0}").replace("{0}", injection_aps.ui.format_number(frm.doc.total_prebuild_qty || 0)),
						__("JIT Qty: {0}").replace("{0}", injection_aps.ui.format_number(frm.doc.total_jit_qty || 0)),
					],
					message: __("Applying capacity balance..."),
					success_message: __("Capacity balance applied."),
					busy_key: `planning-capacity-apply:${frm.doc.name}`,
				},
				frm.__aps_v2_enabled && frm.__aps_solver_engine === "CP-SAT"
					? "injection_aps.api.app.apply_v2_schedule"
					: "injection_aps.api.app.apply_capacity_balance",
				frm.__aps_v2_enabled && frm.__aps_solver_engine === "CP-SAT"
					? { planning_run: frm.doc.name, expected_fingerprint: frm.doc.solver_input_fingerprint }
					: { run_name: frm.doc.name }
			);
			if (response) {
				await frm.reload_doc();
			}
		}, "Capacity", "primary", frm.__aps_v2_enabled && frm.__aps_solver_engine === "CP-SAT" ? "apply_v2_schedule" : "apply_capacity_balance");
	}

	if (frm.doc.approval_state !== "Approved") {
		const analysis = get_capacity_analysis(frm) || {};
		const capacityReadyForConfirmation = ["Applied", "Applied with Exceptions"].includes(frm.doc.capacity_balance_status)
			&& Boolean(analysis.applied_plan_fingerprint)
			&& Boolean(analysis.applied_resource_fingerprint);
		const confirmRunButton = addButton("Confirm Run", async () => {
			const response = await confirmAndCall(
				{ action_key: "approve", confirm_required: 1 },
				{
					title: __("Confirm APS Run"),
					summary_lines: [
						__("APS Run: {0}").replace("{0}", frm.doc.name),
						__("Exceptions: {0}").replace("{0}", String(frm.doc.exception_count || 0)),
						__("After confirmation, the run will move into proposal review."),
					],
					message: __("Approving planning run..."),
					success_message: __("Planning run approved."),
					busy_key: `planning-approve:${frm.doc.name}`,
				},
				"injection_aps.api.app.approve_planning_run",
				{
					run_name: frm.doc.name,
				}
			);
			if (!response) {
				return;
			}
			if (response.work_order_proposal_batch) {
				frappe.set_route("Form", "APS Work Order Proposal Batch", response.work_order_proposal_batch);
				return;
			}
			await frm.reload_doc();
		}, null, "primary", "approve");
		if (confirmRunButton && !capacityReadyForConfirmation) {
			const reason = __("Apply the analyzed capacity plan before confirming this run.", null, "Injection APS");
			confirmRunButton.prop("disabled", true).attr("title", reason).attr("aria-label", reason);
		}
	}

	if (frm.doc.approval_state === "Approved" && frm.doc.status === "Approved") {
		addButton("WO Proposal", async () => {
			const response = await confirmAndCall(
				{ action_key: "generate_work_order_proposals", confirm_required: 1 },
				{
					title: __("Confirm Generate Work Order Proposals"),
					summary_lines: [
						__("APS Run: {0}").replace("{0}", frm.doc.name),
						__("This will generate a work-order proposal batch for review."),
					],
					message: __("Generating work order proposal batch..."),
					success_message: __("Work order proposal batch generated."),
					busy_key: `planning-wo-proposal:${frm.doc.name}`,
				},
				"injection_aps.api.app.generate_work_order_proposals",
				{
					run_name: frm.doc.name,
				}
			);
			if (!response) {
				return;
			}
			if (response.shift_schedule_proposal_batch) {
				frappe.set_route("Form", "APS Shift Schedule Proposal Batch", response.shift_schedule_proposal_batch);
				return;
			}
			await frm.reload_doc();
		}, null, "default", "generate_work_order_proposals");
	}

	if (frm.doc.status === "Work Order Proposed") {
		addButton("Shift Proposal", async () => {
			const response = await confirmAndCall(
				{ action_key: "generate_shift_schedule_proposals", confirm_required: 1 },
				{
					title: __("Confirm Generate Day/Night Shift Proposals"),
					summary_lines: [
						__("APS Run: {0}").replace("{0}", frm.doc.name),
						__("This will generate day/night shift proposals for review."),
					],
					message: __("Generating day/night shift proposal batch..."),
					success_message: __("Shift proposal batch generated."),
					busy_key: `planning-shift-proposal:${frm.doc.name}`,
				},
				"injection_aps.api.app.generate_shift_schedule_proposals",
				{
					run_name: frm.doc.name,
				}
			);
			if (!response) {
				return;
			}
			await frm.reload_doc();
		}, null, "default", "generate_shift_schedule_proposals");
	}

	addButton("Board", () => {
		injection_aps.ui.go_to(`aps-schedule-gantt?run_name=${encodeURIComponent(frm.doc.name)}`);
	}, "Open");

	addButton("Execution", () => {
		injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(frm.doc.name)}`);
	}, "Open");

	if (["Applied", "Shift Proposed"].includes(frm.doc.status)) {
		addButton("Sync", async () => {
			const response = await injection_aps.ui.xcall(
				{
					message: __("Syncing execution feedback..."),
					success_message: __("Execution feedback synced."),
					busy_key: `planning-execution-sync:${frm.doc.name}`,
				},
				"injection_aps.api.app.sync_execution_feedback_to_aps",
				{
					run_name: frm.doc.name,
				}
			);
			if (!response) {
				return;
			}
			injection_aps.ui.show_warnings(
				response.fulfillment,
				__("Execution Sync Warnings"),
				"warning_count"
			);
			await frm.reload_doc();
		}, "Tools", null, "sync_execution");
	}

	addButton("Rebuild Exceptions", async () => {
		const response = await injection_aps.ui.xcall(
			{
				message: __("Rebuilding exceptions..."),
				success_message: __("Exceptions rebuilt."),
				busy_key: `planning-exceptions:${frm.doc.name}`,
			},
			"injection_aps.api.app.rebuild_exceptions",
			{
				run_name: frm.doc.name,
			}
		);
		if (!response) {
			return;
		}
	}, "Tools", null, "rebuild_exceptions");
}

function prompt_solver_reason(title) {
	return new Promise((resolve) => {
		const dialog = new frappe.ui.Dialog({
			title,
			fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason", null, "Injection APS"), reqd: 1 }],
			primary_action_label: __("Confirm", null, "Injection APS"),
			primary_action: (values) => { dialog.hide(); resolve(values.reason); },
		});
		dialog.show();
	});
}

async function show_bom_decisions(frm) {
	const data = await frappe.xcall("injection_aps.api.app.get_run_bom_selections", { planning_run: frm.doc.name });
	if (!data) return;
	const byItem = {};
	(data.options || []).forEach((row) => {
		if (!byItem[row.item]) byItem[row.item] = [];
		byItem[row.item].push(row);
	});
	const selected = {};
	(data.selections || []).forEach((row) => { selected[row.item_code] = row.bom; });
	const rows = Object.keys(byItem).sort().map((item) => {
		const options = byItem[item];
		const defaultBom = (options.find((row) => Number(row.is_default || 0) === 1) || options[0] || {}).name || "";
		return `
			<tr>
				<td>${injection_aps.ui.escape(item)}</td>
				<td>${injection_aps.ui.escape(defaultBom || "-")}</td>
				<td><select class="form-control input-sm ia-bom-choice" data-item="${injection_aps.ui.escape(item)}" ${data.policy === "Explicit Approved Alternative" ? "" : "disabled"}>
					<option value="">${injection_aps.ui.escape(__("Use Default BOM", null, "Injection APS"))}</option>
					${options.map((row) => `<option value="${injection_aps.ui.escape(row.name)}" ${selected[item] === row.name ? "selected" : ""}>${injection_aps.ui.escape(row.name)}${Number(row.is_default || 0) ? ` (${injection_aps.ui.escape(__("Default", null, "Injection APS"))})` : ""}</option>`).join("")}
				</select></td>
			</tr>`;
	}).join("");
	const dialog = new frappe.ui.Dialog({
		title: __("BOM Decisions", null, "Injection APS"),
		fields: [
			{ fieldname: "policy_info", fieldtype: "HTML", options: `<div class="alert alert-info"><b>${injection_aps.ui.escape(__("Policy", null, "Injection APS"))}:</b> ${injection_aps.ui.escape(data.policy)}<br>${injection_aps.ui.escape(__("Only explicitly approved alternatives replace the submitted default BOM. Any BOM master change requires approval again.", null, "Injection APS"))}</div><div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Item", null, "Injection APS")}</th><th>${__("Default BOM", null, "Injection APS")}</th><th>${__("Approved Selection", null, "Injection APS")}</th></tr></thead><tbody>${rows || `<tr><td colspan="3" class="text-muted">${__("No eligible manufactured items were found.", null, "Injection APS")}</td></tr>`}</tbody></table></div>` },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Approval Reason", null, "Injection APS"), depends_on: `eval:${JSON.stringify(data.policy)}==\"Explicit Approved Alternative\"` },
		],
		primary_action_label: __("Save BOM Decisions", null, "Injection APS"),
		primary_action: async (values) => {
			const selections = [];
			dialog.$wrapper.find(".ia-bom-choice").each(function () {
				if (this.value) selections.push({ item_code: this.dataset.item, bom: this.value });
			});
			const response = await injection_aps.ui.xcall(
				{ message: __("Saving BOM decisions...", null, "Injection APS"), success_message: __("BOM decisions saved; analyze the schedule again.", null, "Injection APS"), busy_key: `bom-decisions:${frm.doc.name}` },
				"injection_aps.api.app.set_run_bom_selections",
				{ planning_run: frm.doc.name, selections, reason: values.reason || "", expected_run_modified: data.run_modified }
			);
			if (response) { dialog.hide(); await frm.reload_doc(); }
		},
	});
	if (data.policy !== "Explicit Approved Alternative") dialog.get_primary_btn().hide();
	dialog.show();
}

async function show_bom_tree(frm) {
	const data = await frappe.xcall("injection_aps.api.app.get_bom_pegging_tree", { planning_run: frm.doc.name });
	if (!data) return;
	const summary = data.summary || {};
	const rows = (data.links || []).map((row) => `<tr>
		<td>${injection_aps.ui.escape(row.root_demand_key || "-")}</td>
		<td>${injection_aps.ui.escape(((data.nodes || []).find((node) => node.key === row.from) || {}).item_code || "-")}</td>
		<td>→</td>
		<td>${injection_aps.ui.escape(((data.nodes || []).find((node) => node.key === row.to) || {}).item_code || "-")}</td>
		<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.required_qty || 0))}</td>
		<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.stock_covered_qty || 0))}</td>
		<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.wip_covered_qty || 0))}</td>
		<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.production_qty || 0))}</td>
		<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.status || "-"))}</td>
	</tr>`).join("");
	frappe.msgprint({
		title: __("BOM Tree", null, "Injection APS"), wide: true,
		message: `<div class="mb-2"><b>${__("Roots", null, "Injection APS")}:</b> ${summary.root_count || 0} · <b>${__("Late Dependencies", null, "Injection APS")}:</b> ${summary.late_count || 0} · <b>${__("Raw Material Advisory", null, "Injection APS")}:</b> ${summary.raw_material_advisory_count || 0}</div><div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Root Demand", null, "Injection APS")}</th><th>${__("Child", null, "Injection APS")}</th><th></th><th>${__("Parent", null, "Injection APS")}</th><th>${__("Required", null, "Injection APS")}</th><th>${__("Stock", null, "Injection APS")}</th><th>${__("WIP", null, "Injection APS")}</th><th>${__("Produce", null, "Injection APS")}</th><th>${__("Status", null, "Injection APS")}</th></tr></thead><tbody>${rows || `<tr><td colspan="9" class="text-muted">${__("Apply a validated V2 schedule to materialize BOM pegging.", null, "Injection APS")}</td></tr>`}</tbody></table></div>`,
	});
}
