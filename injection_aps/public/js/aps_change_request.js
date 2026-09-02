const CHANGE_REQUEST_SHARED_READY = injection_aps.ui_loader.load("20260902.1");

const CHANGE_REQUEST_FIELDS = [
	"planning_run",
	"company",
	"plant_floor",
	"source_demand_delta",
	"change_type",
	"target_result",
	"item_code",
	"customer",
	"required_date",
	"qty",
	"target_planned_qty",
	"machine_exception_mode",
	"workstation",
	"exception_start_time",
	"exception_end_time",
	"available_capacity_percent",
	"retained_disposition",
	"notes",
];

frappe.ui.form.on("APS Change Request", {
	setup(frm) {
		frm.set_query("target_result", () => ({
			filters: {
				planning_run: frm.doc.planning_run || "",
				...(frm.doc.item_code ? { item_code: frm.doc.item_code } : {}),
				...(frm.doc.customer ? { customer: frm.doc.customer } : {}),
			},
		}));
	},

	async refresh(frm) {
		await CHANGE_REQUEST_SHARED_READY;
		injection_aps.ui.ensure_styles();
		set_change_request_field_state(frm);
		render_change_status(frm);
		await injection_aps.ui.load_item_display_details([
			frm.doc.item_code,
			...collect_item_codes(parse_json(frm.doc.impact_json)),
		]);
		render_change_request(frm);
		if (!frm.is_new()) {
			add_change_request_actions(frm);
		}
	},

	async planning_run(frm) {
		if (!frm.doc.planning_run) {
			return;
		}
		const values = await frappe.db.get_value("APS Planning Run", frm.doc.planning_run, ["company", "plant_floor"]);
		const row = values && values.message;
		if (row) {
			await frm.set_value("company", row.company || frm.doc.company);
			await frm.set_value("plant_floor", row.plant_floor || frm.doc.plant_floor);
		}
	},

	async target_result(frm) {
		if (!frm.doc.target_result) {
			return;
		}
		const values = await frappe.db.get_value(
			"APS Schedule Result",
			frm.doc.target_result,
			["item_code", "customer", "requested_date", "planned_qty", "plant_floor"]
		);
		const row = values && values.message;
		if (!row) {
			return;
		}
		await frm.set_value("item_code", row.item_code || "");
		await frm.set_value("customer", row.customer || "");
		await frm.set_value("plant_floor", row.plant_floor || frm.doc.plant_floor);
	},

	async source_demand_delta(frm) {
		if (!frm.doc.source_demand_delta) {
			return;
		}
		const values = await frappe.db.get_value(
			"APS Demand Delta",
			frm.doc.source_demand_delta,
			[
				"company",
				"customer",
				"item_code",
				"previous_schedule_date",
				"current_schedule_date",
				"previous_qty",
				"current_qty",
				"delta_qty",
				"change_type",
			]
		);
		const row = values && values.message;
		if (!row) {
			return;
		}
		const typeMap = {
			Added: "Urgent Order",
			Appended: "Increase Qty",
			Increased: "Increase Qty",
			Reduced: "Decrease Qty",
			Cancelled: "Cancel",
			Advanced: "Pull In",
			Delayed: "Push Out",
		};
		await frm.set_value("company", row.company || frm.doc.company);
		await frm.set_value("customer", row.customer || "");
		await frm.set_value("item_code", row.item_code || "");
		await frm.set_value("change_type", typeMap[row.change_type] || frm.doc.change_type);
		await frm.set_value("required_date", row.current_schedule_date || "");
		await frm.set_value("target_planned_qty", row.current_qty || 0);
		await frm.set_value("qty", Math.abs(Number(row.delta_qty || 0)));
	},

	machine_exception_mode(frm) {
		if (frm.doc.machine_exception_mode === "Downtime") {
			frm.set_value("available_capacity_percent", 0);
		}
	},

	change_type(frm) {
		set_change_request_field_state(frm);
	},
});

function render_change_status(frm) {
	const nextStep = {
		Draft: "Analyze",
		Analyzed: "PMC Confirm",
		"PMC Confirmed": "Approve",
		Approved: "Apply",
		Applied: "Complete",
		Rejected: "None",
		Cancelled: "None",
	};
	const proposal = parse_json(frm.doc.proposal_json);
	const status = frm.doc.status || "Draft";
	const node = document.createElement("div");
	injection_aps.ui.render_status_line(node, {
		current_step: status,
		next_step: nextStep[status] || "-",
		blocking_reason: status === "Analyzed" && Number(proposal.allowed || 0) !== 1
			? __("Blocking impact requires reconciliation")
			: "",
	});
	frm.dashboard.set_headline(node.outerHTML);
}

function set_change_request_field_state(frm) {
	const locked = (frm.doc.status || "Draft") !== "Draft";
	CHANGE_REQUEST_FIELDS.forEach((fieldname) => frm.set_df_property(fieldname, "read_only", locked ? 1 : 0));
	frm.toggle_display("target_result", !["Urgent Order", "Machine Exception"].includes(frm.doc.change_type));
	frm.toggle_display("item_code", frm.doc.change_type !== "Machine Exception");
	frm.toggle_display("customer", frm.doc.change_type !== "Machine Exception");
	frm.toggle_display("required_date", ["Pull In", "Push Out", "Urgent Order"].includes(frm.doc.change_type));
	frm.toggle_display("qty", ["Increase Qty", "Decrease Qty", "Urgent Order"].includes(frm.doc.change_type));
	frm.toggle_display("target_planned_qty", ["Increase Qty", "Decrease Qty", "Cancel", "Urgent Order"].includes(frm.doc.change_type));
	frm.toggle_display("protection_section", ["Increase Qty", "Decrease Qty", "Cancel"].includes(frm.doc.change_type));
	frm.toggle_display("audit_section", Boolean(frm.doc.analysis_fingerprint));
	frm.set_df_property(
		"available_capacity_percent",
		"read_only",
		locked || frm.doc.machine_exception_mode === "Downtime" ? 1 : 0
	);
}

function add_change_request_actions(frm) {
	frm.clear_custom_buttons();
	const status = frm.doc.status || "Draft";
	if (["Draft", "Analyzed", "PMC Confirmed", "Approved"].includes(status) && injection_aps.ui.can_run_action("analyze_change_request")) {
		frm.add_custom_button(__("Analyze"), () => run_change_action(frm, {
			method: "injection_aps.api.app.analyze_change_request_impact",
			message: __("Analyzing plan impact..."),
			success: __("Impact analysis saved."),
			busyKey: `change-analyze:${frm.doc.name}`,
		}), null, "primary");
	}
	if (status === "Analyzed" && injection_aps.ui.can_run_action("confirm_change_request")) {
		frm.add_custom_button(__("PMC Confirm", null, "Injection APS"), () => confirm_change_action(frm, {
			method: "injection_aps.api.app.confirm_change_request",
			title: __("Confirm Impact Proposal"),
			message: __("Confirming PMC review..."),
			success: __("PMC confirmation recorded."),
			busyKey: `change-confirm:${frm.doc.name}`,
		}), null, "primary");
	}
	if (status === "PMC Confirmed" && injection_aps.ui.can_run_action("approve_change_request")) {
		frm.add_custom_button(__("Approve", null, "Injection APS"), () => confirm_change_action(frm, {
			method: "injection_aps.api.app.approve_change_request",
			title: __("Approve Plan Change"),
			message: __("Approving plan change..."),
			success: __("Plan change approved."),
			busyKey: `change-approve:${frm.doc.name}`,
		}), null, "primary");
	}
	if (status === "Approved" && injection_aps.ui.can_run_action("apply_change_request")) {
		frm.add_custom_button(__("Apply", null, "Injection APS"), () => confirm_change_action(frm, {
			method: "injection_aps.api.app.apply_change_request",
			title: __("Apply Plan Change"),
			message: __("Applying plan change..."),
			success: __("Plan change applied and recalculated."),
			busyKey: `change-apply:${frm.doc.name}`,
		}), null, "primary");
	}
	if (["Analyzed", "PMC Confirmed", "Approved"].includes(status) && injection_aps.ui.can_run_action("reject_change_request")) {
		frm.add_custom_button(__("Reject", null, "Injection APS"), () => reject_change_action(frm), __("Review", null, "Injection APS"));
	}
	if (frm.doc.planning_run) {
		frm.add_custom_button(__("Gantt", null, "Injection APS"), () => injection_aps.ui.go_to(`aps-schedule-gantt?run_name=${encodeURIComponent(frm.doc.planning_run)}`), __("Open", null, "Injection APS"));
	}
	if (frm.doc.application_log) {
		frm.add_custom_button(__("Audit Log", null, "Injection APS"), () => frappe.set_route("Form", "APS Change Application Log", frm.doc.application_log), __("Open", null, "Injection APS"));
	}
	frm.add_custom_button(
		__("Change Impact Center", null, "Injection APS"),
		() => frappe.set_route("aps-change-impact-center"),
		__("Open", null, "Injection APS")
	);
}

async function run_change_action(frm, options) {
	await injection_aps.ui.xcall(
		{
			message: options.message,
			success_message: options.success,
			busy_key: options.busyKey,
		},
		options.method,
		{ change_request: frm.doc.name }
	);
	await frm.reload_doc();
}

async function confirm_change_action(frm, options) {
	const confirmed = await injection_aps.ui.confirm_action(
		{ confirm_required: 1 },
		{
			title: options.title,
			summary_lines: build_confirmation_summary(frm),
		}
	);
	if (!confirmed) {
		return;
	}
	await run_change_action(frm, options);
}

function reject_change_action(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Reject Plan Change"),
		fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason", null, "Injection APS"), reqd: 1 }],
		primary_action_label: __("Reject", null, "Injection APS"),
		primary_action: async (values) => {
			dialog.hide();
			await injection_aps.ui.xcall(
				{
					message: __("Rejecting plan change..."),
					success_message: __("Plan change rejected."),
					busy_key: `change-reject:${frm.doc.name}`,
				},
				"injection_aps.api.app.reject_change_request",
				{ change_request: frm.doc.name, reason: values.reason }
			);
			await frm.reload_doc();
		},
	});
	dialog.show();
}

function build_confirmation_summary(frm) {
	return [
		__("Change Request: {0}").replace("{0}", frm.doc.name),
		__("Type: {0}").replace("{0}", injection_aps.ui.translate(frm.doc.change_type || "-")),
		__("Planning Run: {0}").replace("{0}", frm.doc.planning_run || "-"),
		__("Target Qty: {0}").replace("{0}", injection_aps.ui.format_number(frm.doc.target_planned_qty || 0)),
		__("Retained Qty: {0}").replace("{0}", injection_aps.ui.format_number(frm.doc.retained_excess_qty || 0)),
	];
}

function render_change_request(frm) {
	const wrapper = frm.fields_dict.impact_preview_html && frm.fields_dict.impact_preview_html.$wrapper;
	if (!wrapper) {
		return;
	}
	const impact = parse_json(frm.doc.impact_json);
	const proposal = parse_json(frm.doc.proposal_json);
	if (!frm.doc.analysis_fingerprint) {
		wrapper.empty();
		return;
	}
	const root = document.createElement("div");
	root.className = "ia-change-engine";
	root.innerHTML = `
		<div class="ia-change-metrics"></div>
		<div class="ia-change-impact-orders"></div>
		<div class="ia-change-secondary"></div>
	`;
	wrapper.empty().append(root);
	injection_aps.ui.render_cards(root.querySelector(".ia-change-metrics"), [
		{ label: __("Current Plan", null, "Injection APS"), value: injection_aps.ui.format_number(frm.doc.current_planned_qty || 0) },
		{ label: __("Target Plan", null, "Injection APS"), value: injection_aps.ui.format_number(frm.doc.target_planned_qty || 0) },
		{ label: __("Machine Scheduled", null, "Injection APS"), value: injection_aps.ui.format_number((proposal.quantity_protection || {}).machine_scheduled_qty || proposal.projected_machine_scheduled_qty || 0) },
		{ label: __("Minimum Retained", null, "Injection APS"), value: injection_aps.ui.format_number(frm.doc.minimum_retained_qty || 0) },
		{ label: __("Retained Excess", null, "Injection APS"), value: injection_aps.ui.format_number(frm.doc.retained_excess_qty || 0) },
		{ label: __("Segment Actions", null, "Injection APS"), value: (proposal.segment_actions || []).length },
	]);
	const columns = [
		{ label: __("Affected Order", null, "Injection APS"), fieldname: "affected_order" },
		{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
		{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
		{ label: __("Due Date", null, "Injection APS"), fieldname: "due_date", fieldtype: "Date" },
		{ label: __("Old Completion", null, "Injection APS"), fieldname: "old_completion_time", fieldtype: "Datetime" },
		{ label: __("New Completion", null, "Injection APS"), fieldname: "new_completion_time", fieldtype: "Datetime" },
		{ label: __("Delayed Qty", null, "Injection APS"), fieldname: "delayed_qty", fieldtype: "Float" },
		{ label: __("Delay Minutes"), fieldname: "delay_minutes", fieldtype: "Float" },
	];
	injection_aps.ui.render_table(root.querySelector(".ia-change-impact-orders"), columns, impact.affected_orders || [], null, {
		exportable: true,
		export_title: __("Plan Change Impact", null, "Injection APS"),
		export_file_name: `aps_change_${frm.doc.name}`,
	});
	render_secondary_impact(root.querySelector(".ia-change-secondary"), impact);
}

function collect_item_codes(value, output = new Set()) {
	if (Array.isArray(value)) {
		value.forEach((entry) => collect_item_codes(entry, output));
	} else if (value && typeof value === "object") {
		if (value.item_code) {
			output.add(value.item_code);
		}
		Object.values(value).forEach((entry) => collect_item_codes(entry, output));
	}
	return Array.from(output);
}

function render_secondary_impact(target, impact) {
	const options = impact.alternate_machine_options || [];
	const conflicts = impact.freeze_conflicts || [];
	const suggestions = []
		.concat(impact.suggestions || [])
		.concat([impact.overtime_suggestion, impact.subcontract_suggestion].filter(Boolean));
	target.innerHTML = `
		<div class="ia-change-secondary-band">
			<div><strong>${__("Affected Customers", null, "Injection APS")}</strong><span>${injection_aps.ui.escape((impact.affected_customers || []).join(", ") || "-")}</span></div>
			<div><strong>${__("Cascading Delays", null, "Injection APS")}</strong><span>${Number(impact.cascading_delay_count || 0)}</span></div>
			<div><strong>${__("Added Mold Changes", null, "Injection APS")}</strong><span>${Number(impact.additional_mold_changes || 0)}</span></div>
			<div><strong>${__("Freeze Conflicts", null, "Injection APS")}</strong><span>${conflicts.length}</span></div>
		</div>
		${options.length ? `<div class="ia-change-option-list">${options.map((row) => `
			<div class="ia-change-option-row">
				<span>${row.selected ? injection_aps.ui.pill(__("Selected", null, "Injection APS"), "green") : injection_aps.ui.pill(__("Alternative", null, "Injection APS"), "blue")}</span>
				<strong>${injection_aps.ui.escape(row.workstation || "-")}</strong>
				<span>${injection_aps.ui.escape(row.mould_reference || "-")}</span>
				<span>${injection_aps.ui.format_datetime(row.completion_time)}</span>
				<span>${injection_aps.ui.format_number(row.delayed_qty || 0)}</span>
			</div>`).join("")}</div>` : ""}
		${suggestions.length ? `<ul class="ia-change-suggestions">${suggestions.map((row) => `<li>${injection_aps.ui.escape(row)}</li>`).join("")}</ul>` : ""}
	`;
}

function parse_json(value) {
	if (!value) {
		return {};
	}
	try {
		return JSON.parse(value);
	} catch (error) {
		console.error(error);
		return {};
	}
}
