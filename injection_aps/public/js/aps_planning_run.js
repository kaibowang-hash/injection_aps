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

	const addButton = (label, fn, group, type) => {
		frm.add_custom_button(__(label), fn, group ? __(group) : undefined);
		if (type) {
			frm.change_custom_button_type(__(label), group ? __(group) : undefined, type);
		}
	};

	addButton("Run Planning", async () => {
		const result = await injection_aps.ui.xcall(
			{
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
	}, null, "primary");

	if (frm.doc.approval_state !== "Approved") {
		addButton("Approve", async () => {
			const response = await injection_aps.ui.xcall(
				{
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
		}, null, "primary");
	}

	if (frm.doc.approval_state === "Approved") {
		addButton("Generate WO Proposals", async () => {
			const response = await injection_aps.ui.xcall(
				{
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
		}, null, "default");
	}

	if (["Approved", "Work Order Proposed"].includes(frm.doc.status)) {
		addButton("Generate Shift Proposals", async () => {
			const response = await injection_aps.ui.xcall(
				{
					message: __("Generating white / night shift proposal batch..."),
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
		}, null, "default");
	}

	addButton("Open Gantt", () => {
		injection_aps.ui.go_to(`aps-schedule-gantt?run_name=${encodeURIComponent(frm.doc.name)}`);
	}, "Open");

	addButton("Open Execution Center", () => {
		injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(frm.doc.name)}`);
	}, "Open");

	if (["Applied", "Shift Proposed", "Work Order Proposed"].includes(frm.doc.status)) {
		addButton("Sync Execution Feedback", async () => {
			const response = await injection_aps.ui.xcall(
				{
					message: __("Syncing execution feedback back to APS..."),
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
		}, "Tools");
	}

	addButton("Rebuild Exceptions", async () => {
		const response = await injection_aps.ui.xcall(
			{
				message: __("Rebuilding APS exceptions..."),
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
	}, "Tools");
}
