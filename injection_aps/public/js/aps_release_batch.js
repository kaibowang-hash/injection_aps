frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Release Batch", {
	async refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		injection_aps.ui.ensure_styles();
		if (frm.doc.planning_run) {
			frm.add_custom_button(__("Open Run"), () => frappe.set_route("Form", "APS Planning Run", frm.doc.planning_run));
			frm.add_custom_button(__("Open Release Center"), () => injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(frm.doc.planning_run)}`));
		}
		if (frm.doc.work_order_scheduling) {
			frm.add_custom_button(__("Open Work Order Scheduling"), () => frappe.set_route("Form", "Work Order Scheduling", frm.doc.work_order_scheduling));
		}
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
	},
});
