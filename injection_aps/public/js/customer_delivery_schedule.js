frappe.require("/assets/injection_aps/js/injection_aps_shared.js");

frappe.ui.form.on("Customer Delivery Schedule", {
	async refresh(frm) {
		injection_aps.ui.ensure_styles();
		await render_flow(frm);
		add_actions(frm);
	},
});

async function render_flow(frm) {
	if (frm.is_new()) {
		return;
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
}

function add_actions(frm) {
	frm.clear_custom_buttons();
	if (!frm.doc.customer || !frm.doc.company) {
		return;
	}

	if (injection_aps.ui.can_run_action("rebuild_demand_pool")) {
		frm.add_custom_button(__("Rebuild Demand"), async () => {
			const confirmed = await injection_aps.ui.confirm_action(
				{ action_key: "rebuild_demand_pool", confirm_required: 1 },
				{
					title: __("Confirm Rebuild Demand"),
					summary_lines: [
						__("Schedule: {0}").replace("{0}", frm.doc.name),
						__("Company: {0}").replace("{0}", frm.doc.company || "-"),
						__("This will rebuild the demand pool and recalculate net requirements."),
					],
				}
			);
			if (!confirmed) {
				return;
			}
			const result = await injection_aps.ui.xcall(
				{
					message: __("Rebuilding demand pool and net requirements..."),
					success_message: __("Demand pool and net requirement rebuilt."),
					busy_key: `schedule-rebuild:${frm.doc.name}`,
				},
				"injection_aps.api.app.promote_schedule_import_to_net_requirement",
				{
					schedule: frm.doc.name,
					company: frm.doc.company,
				}
			);
			if (!result) {
				return;
			}
			injection_aps.ui.show_warnings(result.demand_pool, __("Demand Pool Warnings"), "warning_count");
			injection_aps.ui.show_warnings(result.net_requirement, __("Net Requirement Warnings"), "warning_count");
		});
	}

	frm.add_custom_button(__("Net Requirement"), () => {
		injection_aps.ui.go_to("aps-net-requirement-workbench");
	});

	if (injection_aps.ui.can_run_action("preview_current_rows")) {
		frm.add_custom_button(__("View Version Diff"), () => {
			show_version_diff(frm);
		});
	}
}

async function show_version_diff(frm) {
	const preview = await injection_aps.ui.xcall(
		{
			message: __("Comparing schedule version..."),
			busy_key: `schedule-diff:${frm.doc.name}`,
		},
		"injection_aps.api.app.preview_customer_delivery_schedule",
		{
			customer: frm.doc.customer,
			company: frm.doc.company,
			version_no: frm.doc.version_no || frm.doc.name,
			rows_json: JSON.stringify(
				(frm.doc.items || []).map((row) => ({
					sales_order: row.sales_order,
					item_code: row.item_code,
					customer_part_no: row.customer_part_no,
					schedule_date: row.schedule_date,
					qty: row.qty,
					remark: row.remark,
				}))
			),
		}
	);
	if (!preview) {
		return;
	}
	const summary = Object.entries(preview.summary || {})
		.map(([label, value]) => `${label}: ${value}`)
		.join(" | ");
	frappe.msgprint({
		title: __("Version Diff"),
		message: `
			<div>${__("Rows")}: <b>${preview.row_count || 0}</b></div>
			<div style="margin-top:6px;">${frappe.utils.escape_html(summary || __("No difference summary"))}</div>
		`,
	});
}
