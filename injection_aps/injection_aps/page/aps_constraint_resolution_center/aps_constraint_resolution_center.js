frappe.pages["aps-constraint-resolution-center"].on_page_load = function (wrapper) {
	injection_aps.ui_loader.start("20260901.1", () => initializeConstraintResolutionCenter(wrapper));
};

frappe.pages["aps-constraint-resolution-center"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) wrapper.injection_aps_controller.show();
};

function initializeConstraintResolutionCenter(wrapper) {
	wrapper.classList.add("ia-app-page");
	injection_aps.ui.ensure_styles();
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Constraint Resolution Center", null, "Injection APS"), single_column: true });
	const routeRun = () => frappe.utils.get_url_arg("run_name") || ((frappe.route_options || {}).run_name || "");
	const state = { run: routeRun(), data: null, loadingRun: "", generation: 0 };
	const runField = page.add_field({
		label: __("Planning Run", null, "Injection APS"),
		fieldname: "planning_run",
		fieldtype: "Link",
		options: "APS Planning Run",
		default: state.run,
		change: () => { state.run = runField.get_value() || ""; state.data = null; load(); },
	});
	if (injection_aps.ui.can_run_action("recompute_constraint_resolution")) {
		page.set_primary_action(__("Recompute", null, "Injection APS"), async () => {
			if (!state.run || !state.data) return;
			await mutate("injection_aps.api.app.recompute_after_resolution", {
				planning_run: state.run,
				expected_fingerprint: state.data.input_fingerprint,
			}, __("Recomputing APS constraints...", null, "Injection APS"));
		});
	}

	async function load() {
		if (!state.run) {
			page.main.html(`<div class="text-muted">${__("Select a Planning Run.", null, "Injection APS")}</div>`);
			return;
		}
		if (state.loadingRun === state.run) return;
		const run = state.run;
		const generation = ++state.generation;
		state.loadingRun = run;
		page.main.html(`<div class="text-muted">${__("Loading constraint resolutions...", null, "Injection APS")}</div>`);
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_constraint_resolutions", { planning_run: run });
			if (generation !== state.generation || run !== state.run) return;
			state.data = data || {};
			render();
		} catch (error) {
			if (generation !== state.generation) return;
			console.error(error);
			state.data = null;
			page.main.html(`<div class="alert alert-danger">${__("Constraint resolutions could not be loaded. Check access and try again.", null, "Injection APS")}</div><button type="button" class="btn btn-default" data-retry-load="1">${__("Retry", null, "Injection APS")}</button>`);
			page.main.find("[data-retry-load]").on("click", load);
		} finally {
			if (state.loadingRun === run) state.loadingRun = "";
		}
	}

	async function mutate(method, args, message) {
		try {
			const response = await injection_aps.ui.xcall({ message, busy_key: `constraint-resolution:${state.run}` }, method, args);
			if (response == null) return;
			state.data = null;
			await load();
		} catch (error) {
			console.error(error);
		}
	}

	function render() {
		const overrideSupported = Boolean(state.data.temporary_override_supported);
		const overrideNotice = overrideSupported ? "" : `<div class="alert alert-warning">${frappe.utils.escape_html(
			state.data.temporary_override_reason || __("The current solver does not support temporary constraint overrides. Correct the master data or exclude the affected demand.", null, "Injection APS")
		)}</div>`;
		const groups = [
			["must_fix", __("Must Fix", null, "Injection APS")],
			["temporary_override", __("Temporary Risk Exception", null, "Injection APS")],
			["exclude", __("Exclude For Partial Release", null, "Injection APS")],
			["acknowledgment", __("Acknowledgment", null, "Injection APS")],
		];
		page.main.html(`
			<div class="mb-3"><b>${__("Readiness", null, "Injection APS")}: ${frappe.utils.escape_html(injection_aps.ui.translate(state.data.readiness_status || "-"))}</b>
			<div class="text-muted">${__("There is no Force Apply All. Resolve each illegal input or exclude only the affected commitment.", null, "Injection APS")}</div></div>
			${overrideNotice}
			${groups.map(([key, label]) => renderGroup(label, (state.data.groups || {})[key] || [])).join("")}
		`);
		page.main.find("[data-open]").on("click", function () { frappe.set_route("Form", "APS Constraint Resolution", this.dataset.open); });
		page.main.find("[data-exclude]").on("click", async function () {
			const reason = await promptReason(__("Exclude Commitment From This Release", null, "Injection APS"));
			if (!reason) return;
			await mutate("injection_aps.api.app.exclude_commitment_from_release", {
				resolution: this.dataset.exclude, reason, expected_fingerprint: state.data.input_fingerprint,
			}, __("Excluding the affected commitment...", null, "Injection APS"));
		});
		page.main.find("[data-request-override]").on("click", async function () {
			const values = await promptOverride();
			if (!values) return;
			await mutate("injection_aps.api.app.request_temporary_override", {
				resolution: this.dataset.requestOverride,
				resolution_type: values.resolution_type,
				proposed_value: {},
				expires_on: values.expires_on,
				reason: values.reason,
				expected_fingerprint: state.data.input_fingerprint,
			}, __("Requesting temporary override...", null, "Injection APS"));
		});
		page.main.find("[data-approve-override]").on("click", async function () {
			const reason = await promptReason(__("Approve Temporary Risk Exception", null, "Injection APS"));
			if (!reason) return;
			await mutate("injection_aps.api.app.approve_temporary_override", {
				resolution: this.dataset.approveOverride,
				reason,
				expected_fingerprint: state.data.input_fingerprint,
		}, __("Approving temporary risk exception...", null, "Injection APS"));
		});
	}

	function renderGroup(label, rows) {
		const overrideSupported = Boolean(state.data.temporary_override_supported);
		const canRequest = overrideSupported && injection_aps.ui.can_run_action("request_temporary_override");
		const canApprove = overrideSupported && injection_aps.ui.can_run_action("approve_temporary_override");
		const canExclude = injection_aps.ui.can_run_action("exclude_commitment_from_release");
		const body = rows.length ? rows.map((row) => `<tr>
			<td>${frappe.utils.escape_html(row.affected_customer || "-")}</td><td>${frappe.utils.escape_html(row.affected_item || "-")}</td>
			<td>${frappe.utils.escape_html(injection_aps.ui.format_number(row.affected_qty || 0))}</td><td>${frappe.utils.escape_html(row.message || "-")}</td>
			<td>${frappe.utils.escape_html(injection_aps.ui.translate(row.status || "-"))}</td><td>
			<button type="button" class="btn btn-xs btn-default" data-open="${frappe.utils.escape_html(row.name)}">${__("Details", null, "Injection APS")}</button>
			${canExclude && row.blocker_policy === "Exclude Only" && ["Open", "Requested"].includes(row.status) ? `<button type="button" class="btn btn-xs btn-warning" data-exclude="${frappe.utils.escape_html(row.name)}">${__("Exclude", null, "Injection APS")}</button>` : ""}
			${canRequest && row.blocker_policy === "Temporary Override" && ["Open", "Rejected", "Expired"].includes(row.status) ? `<button type="button" class="btn btn-xs btn-default" data-request-override="${frappe.utils.escape_html(row.name)}">${__("Request Exception", null, "Injection APS")}</button>` : ""}
			${canApprove && row.blocker_policy === "Temporary Override" && row.status === "Requested" ? `<button type="button" class="btn btn-xs btn-primary" data-approve-override="${frappe.utils.escape_html(row.name)}">${__("Approve", null, "Injection APS")}</button>` : ""}
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

	function promptOverride() {
		return new Promise((resolve) => {
			const dialog = new frappe.ui.Dialog({
				title: __("Request Temporary Risk Exception", null, "Injection APS"),
				fields: [
					{ fieldname: "notice", fieldtype: "HTML", options: `<div class="alert alert-warning">${__("This exception only waives the named blocker until expiry. It does not change cycle time, capacity, compatibility, or other master data.", null, "Injection APS")}</div>` },
					{ fieldname: "resolution_type", fieldtype: "Select", label: __("Exception Type", null, "Injection APS"), options: ["Temporary Cycle Override", "Temporary Capacity Override", "Temporary Compatibility Override"].join("\n"), reqd: 1 },
					{ fieldname: "expires_on", fieldtype: "Datetime", label: __("Expires On", null, "Injection APS"), reqd: 1 },
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason", null, "Injection APS"), reqd: 1 },
				],
				primary_action_label: __("Submit", null, "Injection APS"),
				primary_action: (values) => { dialog.hide(); resolve(values); },
			});
			dialog.show();
		});
	}

	wrapper.injection_aps_controller = {
		show() {
			const nextRun = routeRun();
			if (nextRun !== state.run) {
				state.run = nextRun;
				state.data = null;
				state.generation += 1;
				state.loadingRun = "";
				runField.set_value(nextRun);
			}
			load();
		},
	};
	wrapper.injection_aps_controller.show();
}
