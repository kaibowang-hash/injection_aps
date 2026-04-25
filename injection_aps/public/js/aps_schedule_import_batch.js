frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("APS Schedule Import Batch", {
	async refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			injection_aps.ui.ensure_styles();
			frm.clear_custom_buttons();
			if (frm.doc.status === "Imported" && injection_aps.ui.can_run_action("promote_import")) {
			frm.add_custom_button(__("Promote"), async () => {
				const response = await injection_aps.ui.xcall(
					{
						message: __("Rebuilding demand pool and net requirements..."),
						success_message: __("Demand / net requirement rebuilt."),
						busy_key: `import-batch-promote:${frm.doc.name}`,
					},
					"injection_aps.api.app.promote_schedule_import_to_net_requirement",
					{
						import_batch: frm.doc.name,
					}
				);
				if (!response) {
					return;
				}
				injection_aps.ui.show_warnings(response && response.demand_pool, __("Demand Pool Warnings"), "warning_count");
				injection_aps.ui.show_warnings(response && response.net_requirement, __("Net Requirement Warnings"), "warning_count");
			});
		}
		frm.add_custom_button(__("Net Workbench"), () => injection_aps.ui.go_to("aps-net-requirement-workbench"));
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
