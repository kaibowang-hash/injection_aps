const PLANNING_RUN_SHARED_READY = frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Planning Run", {
	async refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		await PLANNING_RUN_SHARED_READY;
		injection_aps.ui.ensure_styles();
		await render_flow(frm);
		render_quantity_indicators(frm);
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
	return (analysis.demands || []).map((row) => {
		const importantChecks = (row.checks || []).filter((check) => ["warning", "blocked", "failed"].includes(check.status));
		const reasons = []
			.concat(row.confirmation_reasons || [])
			.concat(importantChecks.map((check) => check.message))
			.filter(Boolean)
			.map((reason) => injection_aps.ui.translate(reason));
		return Object.assign({}, row, { display_reasons: [...new Set(reasons)] });
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
							<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.status || "-"))}</td>
							<td>${injection_aps.ui.escape((row.display_reasons || []).join("；") || "-")}</td>
						</tr>
					`
				)
				.join("")
		: `<tr><td colspan="9" class="text-muted">${__("No affected capacity demand rows.")}</td></tr>`;
	const html = `
		<div class="small text-muted mb-2">
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
			return;
		}
		frm.add_custom_button(__(label, null, "Injection APS"), fn, group ? __(group, null, "Injection APS") : undefined);
		if (type) {
			frm.change_custom_button_type(
				__(label, null, "Injection APS"),
				group ? __(group, null, "Injection APS") : undefined,
				type
			);
		}
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

	if (["Draft", "Planned", "Risk"].includes(frm.doc.status || "Draft") && frm.doc.capacity_balance_status !== "Applied") {
		addButton("Analyze Capacity", async () => {
			const response = await injection_aps.ui.xcall(
				{
					message: __("Analyzing shift capacity..."),
					success_message: __("Capacity proposal refreshed."),
					busy_key: `planning-capacity-analyze:${frm.doc.name}`,
				},
				"injection_aps.api.app.analyze_capacity_balance",
				{ run_name: frm.doc.name }
			);
			if (response) {
				await frm.reload_doc();
			}
		}, "Capacity", null, "analyze_capacity_balance");
	}

	if (frm.doc.capacity_balance_status === "Confirmation Required" && !frm.doc.capacity_balance_confirmed_by) {
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

	if (
		frm.doc.capacity_balance_status === "Suggestion Ready" ||
		(frm.doc.capacity_balance_status === "Confirmation Required" && frm.doc.capacity_balance_confirmed_by)
	) {
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
				"injection_aps.api.app.apply_capacity_balance",
				{ run_name: frm.doc.name }
			);
			if (response) {
				await frm.reload_doc();
			}
		}, "Capacity", "primary", "apply_capacity_balance");
	}

	if (frm.doc.approval_state !== "Approved") {
		addButton("Confirm Run", async () => {
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
