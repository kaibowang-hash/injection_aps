frappe.pages["aps-run-console"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSRunConsole(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-run-console"].on_page_show = function (wrapper) {
	wrapper.injection_aps_controller?.refresh();
};

class InjectionAPSRunConsole {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("APS Run Console"),
			single_column: true,
		});
		this.companyField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "company",
			options: "Company",
			label: __("Company"),
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.refresh(),
		});
		this.plantFloorField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "plant_floor",
			options: "Plant Floor",
			label: __("Plant Floor"),
			change: () => this.refresh(),
		});
		this.page.set_primary_action(__("Run Trial"), () => this.openRunDialog());

		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("APS Run Console")}</h3>
					<p>${__("Run Trial -> Approve -> Work Order Proposal Review -> Shift Proposal Review -> Formal Scheduling -> Execution Feedback. The console keeps each run in a dense list with the next action visible instead of forcing extra clicks.")}</p>
				</div>
				<div class="ia-feedback"></div>
				<div class="ia-panel">
					<div class="ia-run-table"></div>
				</div>
			</div>
		`);
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.table = this.page.main.find(".ia-run-table")[0];
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading planning runs..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_run_console_data", {
				company: this.companyField.get_value() || undefined,
				plant_floor: this.plantFloorField.get_value() || undefined,
			});
			this.renderRuns(data.runs || []);
			injection_aps.ui.set_feedback(this.feedback, __("Run console refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load planning runs."), "error");
		}
	}

	renderRuns(rows) {
		if (!rows.length) {
			injection_aps.ui.render_table(this.table, [{ label: __("Info"), fieldname: "message" }], []);
			return;
		}

		const columns = [
			{ label: __("Run"), fieldname: "name" },
			{ label: __("Plant Floor"), fieldname: "plant_floor" },
			{ label: __("Planning Date"), fieldname: "planning_date" },
			{ label: __("Status"), fieldname: "status" },
			{ label: __("Approval"), fieldname: "approval_state" },
			{ label: __("Plan Qty"), fieldname: "total_net_requirement_qty" },
			{ label: __("Scheduled"), fieldname: "total_scheduled_qty" },
			{ label: __("Unscheduled"), fieldname: "total_unscheduled_qty" },
			{ label: __("Exceptions"), fieldname: "exception_count" },
			{ label: __("Exec"), fieldname: "execution_health" },
			{ label: __("Next Step"), fieldname: "next_step" },
			{ label: __("Actions"), fieldname: "actions_html" },
		];

		injection_aps.ui.render_table(
			this.table,
			columns,
			rows,
			(column, value, row) => {
				if (column.fieldname === "name") {
					return injection_aps.ui.route_link(value, `aps-planning-run/${encodeURIComponent(value)}`);
				}
				if (column.fieldname === "status") {
					const tone = ["Approved", "Work Order Proposed", "Shift Proposed", "Applied"].includes(value)
						? "green"
						: value === "Planned"
							? "orange"
							: "blue";
					return injection_aps.ui.pill(injection_aps.ui.translate(value), tone);
				}
				if (column.fieldname === "approval_state") {
					return injection_aps.ui.pill(injection_aps.ui.translate(value), value === "Approved" ? "green" : "orange");
				}
				if (column.fieldname === "planning_date") {
					return injection_aps.ui.format_date(value);
				}
				if (["total_net_requirement_qty", "total_scheduled_qty", "total_unscheduled_qty"].includes(column.fieldname)) {
					return frappe.format(value || 0, { fieldtype: "Float" });
				}
				if (column.fieldname === "next_step") {
					return injection_aps.ui.escape(row.next_actions?.next_step || "");
				}
				if (column.fieldname === "execution_health") {
					const health = row.execution_health || {};
					return `${__("Run")}:${health.running || 0} / ${__("Delay")}:${health.delayed || 0} / ${__("No Update")}:${health.no_recent_update || 0}`;
				}
				if (column.fieldname === "actions_html") {
					const displayActions = (row.next_actions?.actions || [])
						.filter((action) => !["open_gantt", "open_release_center"].includes(action.action_key))
						.sort((left, right) => Number(right.enabled || 0) - Number(left.enabled || 0))
						.slice(0, 2);
					return `
						<div class="ia-chip-row">
							<button class="btn btn-xs btn-default" data-run-action="open_gantt" data-run-name="${injection_aps.ui.escape(row.name)}">${__("Gantt")}</button>
							<button class="btn btn-xs btn-default" data-run-action="open_release" data-run-name="${injection_aps.ui.escape(row.name)}">${__("Execution")}</button>
							${displayActions
								.map(
									(action, index) => `
										<button
											class="btn btn-xs ${index === 0 ? "btn-primary" : "btn-default"}"
											data-inline-action='${encodeURIComponent(JSON.stringify(action))}'
											${Number(action.enabled || 0) === 1 ? "" : "disabled"}
										>${injection_aps.ui.escape(action.label || "")}</button>
									`
								)
								.join("")}
						</div>
					`;
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("APS Planning Run Console"),
				export_sheet_name: __("Planning Runs"),
				export_file_name: "aps_planning_runs",
				export_subtitle: __("Planning runs with execution health and next-step summary."),
			}
		);

		$(this.table)
			.find("[data-inline-action]")
			.each((_, node) => {
				node.addEventListener("click", async () => {
					const action = JSON.parse(decodeURIComponent(node.dataset.inlineAction || ""));
					const response = await injection_aps.ui.run_action(action);
					injection_aps.ui.show_warnings(response, __("Planning Warnings"), "preflight_warning_count");
					await this.refresh();
				});
			});

		$(this.table)
			.find("[data-run-action='open_gantt']")
			.each((_, node) => {
				node.addEventListener("click", () => {
					injection_aps.ui.go_to(`aps-schedule-gantt?run_name=${encodeURIComponent(node.dataset.runName || "")}`);
				});
			});

		$(this.table)
			.find("[data-run-action='open_release']")
			.each((_, node) => {
				node.addEventListener("click", () => {
					injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(node.dataset.runName || "")}`);
				});
			});
	}

	openRunDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Create Trial Planning Run"),
			fields: [
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company"), reqd: 1, default: this.companyField.get_value() || frappe.defaults.get_user_default("Company") },
				{ fieldname: "plant_floor", fieldtype: "Link", options: "Plant Floor", label: __("Plant Floor"), default: this.plantFloorField.get_value() || undefined },
				{ fieldname: "horizon_days", fieldtype: "Int", label: __("Horizon Days"), default: 14, reqd: 1 },
			],
			primary_action_label: __("Run Trial"),
			primary_action: async (values) => {
				const result = await injection_aps.ui.xcall(
					{
						message: __("Running APS trial planning..."),
						success_message: __("Planning run completed."),
						busy_key: `run-console-trial:${values.company || "all"}:${values.plant_floor || "all"}`,
						feedback_target: this.feedback,
						success_feedback: __("Planning run completed. Refreshing the run console..."),
					},
					"injection_aps.api.app.run_planning_run",
					values
				);
				if (!result) {
					return;
				}
				injection_aps.ui.show_warnings(result, __("Planning Precheck Warnings"), "preflight_warning_count");
				dialog.hide();
				await this.refresh();
			},
		});
			dialog.show();
	}
}
