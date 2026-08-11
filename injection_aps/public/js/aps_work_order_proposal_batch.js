const WORK_ORDER_PROPOSAL_SHARED_READY = frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Work Order Proposal Batch", {
	async refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		await WORK_ORDER_PROPOSAL_SHARED_READY;
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

	frm.add_custom_button(__("APS Run", null, "Injection APS"), () => {
		if (frm.doc.planning_run) {
			frappe.set_route("Form", "APS Planning Run", frm.doc.planning_run);
		}
	});

	const hasApprovedRows = (frm.doc.items || []).some((row) => row.review_status === "Approved");
	const pendingRows = (frm.doc.items || []).filter((row) => row.review_status === "Pending");
	const reviewableRows = (frm.doc.items || []).filter((row) => ["Pending", "Approved"].includes(row.review_status));
	if (injection_aps.ui.can_run_action("review_work_order_proposals") && pendingRows.length) {
		frm.add_custom_button(__("Approve Pending Rows", null, "Injection APS"), async () => {
			const selectedNames = new Set(((frm.get_selected && frm.get_selected().items) || []));
			const targetRows = pendingRows.filter((row) => selectedNames.has(row.name));
			const rowsToApprove = targetRows.length ? targetRows : pendingRows;
			const confirmed = await injection_aps.ui.confirm_action(
				{ action_key: "review_work_order_proposals", confirm_required: 1 },
				{
					title: __("Confirm Work Order Proposal Review", null, "Injection APS"),
					summary_lines: [
						__("Pending Rows: {0}", null, "Injection APS").replace("{0}", String(rowsToApprove.length)),
						__("Only reviewed rows can be formally applied.", null, "Injection APS"),
					],
				}
			);
			if (!confirmed) {
				return;
			}
			const response = await injection_aps.ui.xcall(
				{
					message: __("Approving pending work-order proposal rows...", null, "Injection APS"),
					success_message: __("Pending work-order proposal rows approved.", null, "Injection APS"),
					busy_key: `wo-proposal-review:${frm.doc.name}`,
				},
				"injection_aps.api.app.review_work_order_proposals",
				{
					batch_name: frm.doc.name,
					review_status: "Approved",
					row_names: JSON.stringify(rowsToApprove.map((row) => row.name)),
				}
			);
			if (response) {
				await frm.reload_doc();
			}
		});
	}
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
			const selectedNames = new Set(((frm.get_selected && frm.get_selected().items) || []));
			const selectedRows = reviewableRows.filter((row) => selectedNames.has(row.name));
			const rowsToReject = selectedRows.length ? selectedRows : reviewableRows;
			const reason = await injection_aps.ui.prompt_reason({
				title: __("Confirm Reject Work Order Results"),
				primary_action_label: __("Reject Results"),
				summary_lines: [
					__("Work Order Proposal Batch: {0}").replace("{0}", frm.doc.name),
					__("Reviewable Rows: {0}").replace("{0}", String(rowsToReject.length)),
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
				"injection_aps.api.app.review_work_order_proposals",
				{
					batch_name: frm.doc.name,
					review_status: "Rejected",
					row_names: JSON.stringify(rowsToReject.map((row) => row.name)),
					note: reason,
				}
			);
			if (!response) {
				return;
			}
			await frm.reload_doc();
		});
	}

	frm.add_custom_button(__("Execution", null, "Injection APS"), () => {
		injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(frm.doc.planning_run || "")}`);
	});
}
