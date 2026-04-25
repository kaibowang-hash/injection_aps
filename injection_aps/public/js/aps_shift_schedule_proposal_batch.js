frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Shift Schedule Proposal Batch", {
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

	frm.add_custom_button(__("APS Run"), () => {
		if (frm.doc.planning_run) {
			frappe.set_route("Form", "APS Planning Run", frm.doc.planning_run);
		}
	});

	const hasApprovedRows = (frm.doc.items || []).some((row) => row.review_status === "Approved");
	const reviewableRows = (frm.doc.items || []).filter((row) => ["Pending", "Approved"].includes(row.review_status));
	if (injection_aps.ui.can_run_action("apply_shift_schedule_proposals") && ["Ready For Review", "Partially Reviewed", "Reviewed"].includes(frm.doc.status) && hasApprovedRows) {
		frm.add_custom_button(__("Apply Results"), async () => {
			const confirmed = await injection_aps.ui.confirm_action(
				{ action_key: "apply_shift_schedule_proposals", confirm_required: 1 },
				{
					title: __("Confirm Apply Day/Night Shift Results"),
					summary_lines: [
						__("Shift Proposal Batch: {0}").replace("{0}", frm.doc.name),
						__("Approved Rows: {0}").replace("{0}", String((frm.doc.items || []).filter((row) => row.review_status === "Approved").length)),
						__("This will formally write day/night shift scheduling rows."),
					],
				}
			);
			if (!confirmed) {
				return;
			}
			const response = await injection_aps.ui.xcall(
				{
					message: __("Applying approved day/night shift proposals..."),
					success_message: __("Day/night shift results applied."),
					busy_key: `shift-proposal-apply:${frm.doc.name}`,
				},
				"injection_aps.api.app.apply_shift_schedule_proposals",
				{ batch_name: frm.doc.name }
			);
			if (!response) {
				return;
			}
			await frm.reload_doc();
		});
	}

	if (injection_aps.ui.can_run_action("reject_shift_schedule_proposals") && ["Ready For Review", "Partially Reviewed", "Reviewed"].includes(frm.doc.status) && reviewableRows.length) {
		frm.add_custom_button(__("Reject Results"), async () => {
			const reason = await injection_aps.ui.prompt_reason({
				title: __("Confirm Reject Day/Night Results"),
				primary_action_label: __("Reject Results"),
				summary_lines: [
					__("Shift Proposal Batch: {0}").replace("{0}", frm.doc.name),
					__("Reviewable Rows: {0}").replace("{0}", String(reviewableRows.length)),
					__("The selected reviewable rows will be marked Rejected."),
				],
			});
			if (!reason) {
				return;
			}
			const response = await injection_aps.ui.xcall(
				{
					message: __("Rejecting day/night shift proposal rows..."),
					success_message: __("Day/night shift proposal rows rejected."),
					busy_key: `shift-proposal-reject:${frm.doc.name}`,
				},
				"injection_aps.api.app.reject_shift_schedule_proposals",
				{ batch_name: frm.doc.name, reason }
			);
			if (!response) {
				return;
			}
			await frm.reload_doc();
		});
	}

	frm.add_custom_button(__("Execution"), () => {
		injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(frm.doc.planning_run || "")}`);
	});
}
