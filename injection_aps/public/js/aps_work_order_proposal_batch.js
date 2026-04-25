frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Work Order Proposal Batch", {
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
	if (injection_aps.ui.can_run_action("apply_work_order_proposals") && ["Ready For Review", "Partially Reviewed", "Reviewed"].includes(frm.doc.status) && hasApprovedRows) {
		frm.add_custom_button(__("Apply Results"), async () => {
			const confirmed = await injection_aps.ui.confirm_action(
				{ action_key: "apply_work_order_proposals", confirm_required: 1 },
				{
					title: __("Confirm Apply Work Order Results"),
					summary_lines: [
						__("Work Order Proposal Batch: {0}").replace("{0}", frm.doc.name),
						__("Approved Rows: {0}").replace("{0}", String((frm.doc.items || []).filter((row) => row.review_status === "Approved").length)),
						__("This will formally create or bind work orders."),
					],
				}
			);
			if (!confirmed) {
				return;
			}
			const response = await injection_aps.ui.xcall(
				{
					message: __("Applying approved work-order proposals..."),
					success_message: __("Work-order results applied."),
					busy_key: `wo-proposal-apply:${frm.doc.name}`,
				},
				"injection_aps.api.app.apply_work_order_proposals",
				{ batch_name: frm.doc.name }
			);
			if (!response) {
				return;
			}
			await frm.reload_doc();
		});
	}

	if (injection_aps.ui.can_run_action("reject_work_order_proposals") && ["Ready For Review", "Partially Reviewed", "Reviewed"].includes(frm.doc.status) && reviewableRows.length) {
		frm.add_custom_button(__("Reject Results"), async () => {
			const reason = await injection_aps.ui.prompt_reason({
				title: __("Confirm Reject Work Order Results"),
				primary_action_label: __("Reject Results"),
				summary_lines: [
					__("Work Order Proposal Batch: {0}").replace("{0}", frm.doc.name),
					__("Reviewable Rows: {0}").replace("{0}", String(reviewableRows.length)),
					__("The selected reviewable rows will be marked Rejected."),
				],
			});
			if (!reason) {
				return;
			}
			const response = await injection_aps.ui.xcall(
				{
					message: __("Rejecting work-order proposal rows..."),
					success_message: __("Work-order proposal rows rejected."),
					busy_key: `wo-proposal-reject:${frm.doc.name}`,
				},
				"injection_aps.api.app.reject_work_order_proposals",
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
