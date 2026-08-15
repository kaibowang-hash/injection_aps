frappe.pages["aps-constraint-resolution-center"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Constraint Resolution Center", null, "Injection APS"), single_column: true });
	const state = { run: frappe.utils.get_url_arg("run_name") || "", data: null };
	const runField = page.add_field({
		label: __("Planning Run", null, "Injection APS"),
		fieldname: "planning_run",
		fieldtype: "Link",
		options: "APS Planning Run",
		default: state.run,
		change: () => { state.run = runField.get_value(); load(); },
	});
	page.set_primary_action(__("Recompute", null, "Injection APS"), async () => {
		if (!state.run || !state.data) return;
		await frappe.xcall("injection_aps.api.app.recompute_after_resolution", {
			planning_run: state.run,
			expected_fingerprint: state.data.input_fingerprint,
		});
		await load();
	});

	async function load() {
		if (!state.run) {
			page.main.html(`<div class="text-muted">${__("Select a Planning Run.", null, "Injection APS")}</div>`);
			return;
		}
		state.data = await frappe.xcall("injection_aps.api.app.get_constraint_resolutions", { planning_run: state.run });
		render();
	}

	function render() {
		const groups = [
			["must_fix", __("Must Fix", null, "Injection APS")],
			["temporary_override", __("Temporary Override", null, "Injection APS")],
			["exclude", __("Exclude For Partial Release", null, "Injection APS")],
			["acknowledgment", __("Acknowledgment", null, "Injection APS")],
		];
		page.main.html(`
			<div class="mb-3"><b>${__("Readiness", null, "Injection APS")}: ${frappe.utils.escape_html(state.data.readiness_status || "-")}</b>
			<div class="text-muted">${__("There is no Force Apply All. Resolve each illegal input or exclude only the affected commitment.", null, "Injection APS")}</div></div>
			${groups.map(([key, label]) => renderGroup(label, (state.data.groups || {})[key] || [])).join("")}
		`);
		page.main.find("[data-open]").on("click", function () { frappe.set_route("Form", "APS Constraint Resolution", this.dataset.open); });
		page.main.find("[data-exclude]").on("click", async function () {
			const reason = await promptReason(__("Exclude Commitment From This Release", null, "Injection APS"));
			if (!reason) return;
			await frappe.xcall("injection_aps.api.app.exclude_commitment_from_release", {
				resolution: this.dataset.exclude, reason, expected_fingerprint: state.data.input_fingerprint,
			});
			await load();
		});
	}

	function renderGroup(label, rows) {
		const body = rows.length ? rows.map((row) => `<tr>
			<td>${frappe.utils.escape_html(row.affected_customer || "-")}</td><td>${frappe.utils.escape_html(row.affected_item || "-")}</td>
			<td>${format_currency(row.affected_qty || 0, null, 2)}</td><td>${frappe.utils.escape_html(row.message || "-")}</td>
			<td>${frappe.utils.escape_html(row.status || "-")}</td><td>
			<button class="btn btn-xs btn-default" data-open="${row.name}">${__("Details", null, "Injection APS")}</button>
			${["Open", "Requested"].includes(row.status) ? `<button class="btn btn-xs btn-warning" data-exclude="${row.name}">${__("Exclude", null, "Injection APS")}</button>` : ""}
			</td></tr>`).join("") : `<tr><td colspan="6" class="text-muted">${__("None", null, "Injection APS")}</td></tr>`;
		return `<section class="mb-4"><h4>${label} (${rows.length})</h4><div class="table-responsive"><table class="table table-bordered table-sm">
			<thead><tr><th>${__("Customer", null, "Injection APS")}</th><th>${__("Item", null, "Injection APS")}</th><th>${__("Qty", null, "Injection APS")}</th><th>${__("Reason", null, "Injection APS")}</th><th>${__("Status", null, "Injection APS")}</th><th>${__("Action", null, "Injection APS")}</th></tr></thead>
			<tbody>${body}</tbody></table></div></section>`;
	}

	function promptReason(title) {
		return new Promise((resolve) => {
			const dialog = new frappe.ui.Dialog({ title, fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason", null, "Injection APS"), reqd: 1 }],
				primary_action_label: __("Confirm", null, "Injection APS"), primary_action: (values) => { dialog.hide(); resolve(values.reason); } });
			dialog.show();
		});
	}

	load();
};
