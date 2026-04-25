frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Planning Run", {
	async refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		injection_aps.ui.ensure_styles();
		await render_flow(frm);
		add_actions(frm);
	},
});

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
		frm.add_custom_button(__(label), fn, group ? __(group) : undefined);
		if (type) {
			frm.change_custom_button_type(__(label), group ? __(group) : undefined, type);
		}
	};

	const confirmAndCall = async (action, options, method, args) => {
		const confirmed = await injection_aps.ui.confirm_action(action, options);
		if (!confirmed) {
			return null;
		}
		return injection_aps.ui.xcall(options || {}, method, args || {});
	};

	if (["Draft", "Planned", "Risk"].includes(frm.doc.status || "Draft")) {
		addButton("Recalculate", async () => {
			const result = await confirmAndCall(
				{ action_key: "run_trial", confirm_required: 1 },
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
