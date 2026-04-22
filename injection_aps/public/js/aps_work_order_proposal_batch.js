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
	frm.add_custom_button(__("Open Run"), () => {
		if (frm.doc.planning_run) {
			frappe.set_route("Form", "APS Planning Run", frm.doc.planning_run);
		}
	});

	if (["Ready For Review", "Partially Reviewed", "Reviewed"].includes(frm.doc.status)) {
		frm.add_custom_button(__("Apply Work Order Proposals"), async () => {
			const response = await injection_aps.ui.xcall(
				{
					message: __("Applying reviewed work order proposals..."),
					success_message: __("Formal work orders created."),
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

	frm.add_custom_button(__("Open Execution Center"), () => {
		injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(frm.doc.planning_run || "")}`);
	});
}

