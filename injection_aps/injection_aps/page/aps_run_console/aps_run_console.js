frappe.pages["aps-run-console"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSRunConsole(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-run-console"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSRunConsole {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Recalc Console"),
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
		if (injection_aps.ui.can_run_action("run_trial")) {
			this.page.set_primary_action(__("Recalculate"), () => this.openRunDialog());
		}

		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Recalc Console")}</h3>
					<p>${__("Recalculate -> Confirm Run -> Review Work Order Proposals -> Review Day/Night Shift Proposals -> Formal Scheduling -> Execution Feedback. This console centralizes each APS run and its next action.")}</p>
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
		injection_aps.ui.set_feedback(this.feedback, __("Loading APS runs..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_run_console_data", {
				company: this.companyField.get_value() || undefined,
				plant_floor: this.plantFloorField.get_value() || undefined,
			});
			this.renderRuns(data.runs || []);
			injection_aps.ui.set_feedback(this.feedback, __("Recalc Console refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load APS runs."), "error");
		}
	}

	renderRuns(rows) {
		if (!rows.length) {
			injection_aps.ui.render_table(this.table, [{ label: __("Info"), fieldname: "message" }], []);
			return;
		}

		const columns = [
			{ label: __("Run"), fieldname: "name" },
			{ label: __("Plant Floors"), fieldname: "selected_plant_floor_summary" },
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
				if (column.fieldname === "selected_plant_floor_summary") {
					return injection_aps.ui.escape(value || row.plant_floor || "");
				}
				if (["total_net_requirement_qty", "total_scheduled_qty", "total_unscheduled_qty"].includes(column.fieldname)) {
					return frappe.format(value || 0, { fieldtype: "Float" });
				}
				if (column.fieldname === "next_step") {
					return injection_aps.ui.escape(
						injection_aps.ui.translate(injection_aps.ui.get_value(row, "next_actions.next_step", ""))
					);
				}
				if (column.fieldname === "execution_health") {
					const health = row.execution_health || {};
					return `${__("Run")}:${health.running || 0} / ${__("Delay")}:${health.delayed || 0} / ${__("No Update")}:${health.no_recent_update || 0}`;
				}
					if (column.fieldname === "actions_html") {
						const displayActions = (injection_aps.ui.get_value(row, "next_actions.actions", []) || [])
							.filter((action) => !["open_gantt", "open_release_center"].includes(action.action_key))
							.filter((action) => injection_aps.ui.can_run_action(action))
							.sort((left, right) => Number(right.enabled || 0) - Number(left.enabled || 0))
							.slice(0, 2);
					return `
						<div class="ia-chip-row">
							<button class="btn btn-xs btn-default" data-run-action="open_gantt" data-run-name="${injection_aps.ui.escape(row.name)}">${__("Board")}</button>
							<button class="btn btn-xs btn-default" data-run-action="open_release" data-run-name="${injection_aps.ui.escape(row.name)}">${__("Execution")}</button>
							${displayActions
								.map(
									(action, index) => `
										<button
											class="btn btn-xs ${index === 0 ? "btn-primary" : "btn-default"}"
											data-inline-action='${encodeURIComponent(JSON.stringify(action))}'
											${Number(action.enabled || 0) === 1 ? "" : "disabled"}
										>${injection_aps.ui.escape(injection_aps.ui.get_action_label(action))}</button>
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
				export_title: __("Recalc Console"),
				export_sheet_name: __("APS Runs"),
				export_file_name: "aps_planning_runs",
				export_subtitle: __("APS run list with execution health and next actions."),
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
			title: __("Create Recalc Run"),
			fields: [
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company"), reqd: 1, default: this.companyField.get_value() || frappe.defaults.get_user_default("Company") },
				{
					fieldname: "plant_floor_rows",
					fieldtype: "Table",
					label: __("Selected Plant Floors"),
					reqd: 1,
					in_place_edit: true,
					data: this.getDefaultPlantFloorRows(),
					fields: [
						{
							fieldname: "plant_floor",
							fieldtype: "Link",
							options: "Plant Floor",
							label: __("Plant Floor"),
							in_list_view: 1,
							reqd: 1,
						},
					],
				},
				{ fieldname: "horizon_days", fieldtype: "Int", label: __("Horizon Days"), default: 14, reqd: 1 },
			],
			primary_action_label: __("Recalculate"),
			primary_action: async (values) => {
				const plantFloors = this.extractPlantFloors(values.plant_floor_rows);
				if (!plantFloors.length) {
					frappe.msgprint(__("Select at least one Plant Floor before APS planning."));
					return;
				}
				const confirmed = await injection_aps.ui.confirm_action(
					{ action_key: "run_trial", confirm_required: 1 },
					{
						title: __("Confirm Recalculate"),
						summary_lines: [
							__("Company: {0}").replace("{0}", values.company || "-"),
							__("Plant Floors: {0}").replace("{0}", plantFloors.join(", ") || "-"),
							__("Horizon: {0} days").replace("{0}", String(values.horizon_days || 14)),
						],
					}
				);
				if (!confirmed) {
					return;
				}
				const result = await injection_aps.ui.xcall(
					{
						message: __("Running recalculation..."),
						success_message: __("Recalculation completed."),
						busy_key: `run-console-trial:${values.company || "all"}:${plantFloors.join("|") || "all"}`,
						feedback_target: this.feedback,
						success_feedback: __("Recalculation completed. Refreshing console..."),
					},
					"injection_aps.api.app.run_planning_run",
					{
						company: values.company,
						plant_floor: plantFloors[0],
						plant_floors: plantFloors,
						horizon_days: values.horizon_days,
					}
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

	getDefaultPlantFloorRows() {
		const value = this.plantFloorField.get_value();
		return value ? [{ plant_floor: value }] : [];
	}

	extractPlantFloors(rows) {
		const values = [];
		(rows || []).forEach((row) => {
			const value = row && row.plant_floor ? String(row.plant_floor).trim() : "";
			if (value && !values.includes(value)) {
				values.push(value);
			}
		});
		return values;
	}
}
