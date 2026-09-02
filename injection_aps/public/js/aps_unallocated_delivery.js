const UNALLOCATED_DELIVERY_SHARED_READY = injection_aps.ui_loader.load("20260902.1");

frappe.ui.form.on("APS Unallocated Delivery", {
	async refresh(frm) {
		await UNALLOCATED_DELIVERY_SHARED_READY;
		injection_aps.ui.ensure_styles();
		const executionRoles = ["System Manager", "GMC", "PMC", "Manufacturing Manager", "Manufacturing User"];
		if (frm.doc.status !== "Open" || !frappe.user_roles.some((role) => executionRoles.includes(role))) {
			return;
		}
		await injection_aps.ui.load_item_display_details([frm.doc.item_code]);
		frm.add_custom_button(__("Resolve Delivery Lineage", null, "Injection APS"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Resolve Unallocated Delivery", null, "Injection APS"),
				fields: [
					{
						fieldname: "context_html",
						fieldtype: "HTML",
						options: `<div class="text-muted">
							${__("Delivery Note", null, "Injection APS")}: ${frappe.utils.escape_html(frm.doc.source_delivery_note || "-")}<br>
							${injection_aps.ui.item_identity({ item_code: frm.doc.item_code })}<br>
							${__("Unallocated Qty", null, "Injection APS")}: ${frappe.format(frm.doc.unallocated_qty || 0, { fieldtype: "Float" })}
						</div>`,
					},
					{
						fieldname: "demand_identity",
						fieldtype: "Link",
						options: "APS Demand Identity",
						label: __("Demand Identity", null, "Injection APS"),
						reqd: 1,
						get_query: () => ({
							filters: {
								company: frm.doc.company,
								customer: frm.doc.customer,
								item_code: frm.doc.item_code,
								status: ["in", ["Active", "Cancelled"]],
							},
						}),
					},
					{
						fieldname: "reason",
						fieldtype: "Small Text",
						label: __("Resolution Reason", null, "Injection APS"),
						reqd: 1,
					},
				],
				primary_action_label: __("Apply Resolution", null, "Injection APS"),
				primary_action: async (values) => {
					await frappe.xcall("injection_aps.api.app.resolve_unallocated_delivery", {
						name: frm.doc.name,
						demand_identity: values.demand_identity,
						reason: values.reason,
					});
					dialog.hide();
					frappe.show_alert({ message: __("Delivery lineage resolved.", null, "Injection APS"), indicator: "green" });
					await frm.reload_doc();
				},
			});
			dialog.show();
		});
	},
});
