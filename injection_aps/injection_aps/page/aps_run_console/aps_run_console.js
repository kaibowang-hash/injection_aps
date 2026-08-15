frappe.pages["aps-run-console"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_ui_loader.js", () => {
		frappe.require("/assets/injection_aps/css/aps_run_console.css?v=20260815.1", () => injection_aps.ui_loader.start("20260815.2", () => {
			if (!wrapper.injection_aps_controller) {
				wrapper.injection_aps_controller = new InjectionAPSRunConsole(wrapper);
			}
			wrapper.injection_aps_controller.refresh();
		}));
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
			label: __("Company", null, "Injection APS"),
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.refresh(),
		});
		this.plantFloorField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "plant_floor",
			options: "Plant Floor",
			label: __("Plant Floor", null, "Injection APS"),
			change: () => this.refresh(),
		});
		if (injection_aps.ui.can_run_action("run_trial")) {
			this.page.set_primary_action(__("Recalculate"), () => this.openRunDialog());
		}

		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner ia-run-guide">
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

	getRunStatusTone(value) {
		if (["Approved", "Work Order Proposed", "Shift Proposed", "Applied"].includes(value)) {
			return "green";
		}
		return value === "Planned" ? "orange" : "blue";
	}

	formatRunQty(value) {
		return injection_aps.ui.format_number(value);
	}

	renderRunMetric(label, value, tone) {
		return `
			<div class="ia-run-metric${tone ? ` ${tone}` : ""}">
				<span class="ia-run-metric-label">${injection_aps.ui.escape(label)}</span>
				<strong class="ia-run-metric-value">${injection_aps.ui.escape(this.formatRunQty(value))}</strong>
			</div>
		`;
	}

	getExecutionHealthText(row) {
		const health = row.execution_health || {};
		return `${__("Running", null, "Injection APS")}:${health.running || 0} / ${__("Delayed", null, "Injection APS")}:${health.delayed || 0} / ${__("No Update")}:${health.no_recent_update || 0}`;
	}

	renderRunNavIcon(name) {
		const icons = {
			run: '<path d="M7 3h7l4 4v14H7zM14 3v5h5M10 12h6M10 16h6"/>',
			board: '<rect x="3" y="3" width="7" height="8" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="3" y="15" width="7" height="6" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/>',
			execution: '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
			admission: '<path d="M9 5h6M9 3h6v4H9zM7 5H5v16h14V5h-2M8 13l2 2 5-5"/>',
		};
		return `<svg class="ia-aps-icon ia-aps-icon-xs" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icons[name] || icons.run}</svg>`;
	}

	renderRunNavButton(icon, label, actionKey, runName) {
		return `
			<button
				type="button"
				class="ia-icon-btn ia-run-nav-button"
				data-run-action="${injection_aps.ui.escape(actionKey)}"
				data-run-name="${injection_aps.ui.escape(runName)}"
				title="${injection_aps.ui.escape(label)}"
				aria-label="${injection_aps.ui.escape(label)}"
			>${this.renderRunNavIcon(icon)}</button>
		`;
	}

	getPrimaryRouteAction(row) {
		const nextStep = injection_aps.ui.get_value(row, "next_actions.next_step", "");
		if (nextStep === "Analyze and Apply Capacity") {
			return { action_key: "open_run", label: __("Open Run") };
		}
		if (["Monitor Execution Drift", "Review Work Order Proposals", "Review Day/Night Shift Proposals"].includes(nextStep)) {
			return { action_key: "open_release", label: __("Execution", null, "Injection APS") };
		}
		return null;
	}

	renderRunActions(row) {
		const enabledActions = (injection_aps.ui.get_value(row, "next_actions.actions", []) || [])
			.filter((action) => !["open_gantt", "open_release_center"].includes(action.action_key))
			.filter((action) => injection_aps.ui.can_run_action(action))
			.filter((action) => Number(action.enabled || 0) === 1);
		let primaryRoute = this.getPrimaryRouteAction(row);
		const primaryAction = primaryRoute ? null : enabledActions[0];
		if (!primaryRoute && !primaryAction) {
			primaryRoute = { action_key: "open_run", label: __("Open Run") };
		}
		const secondaryActions = (primaryRoute ? enabledActions : enabledActions.slice(1)).slice(0, 1);
		const inlineButton = (action, primary) => `
			<button
				type="button"
				class="btn btn-xs ${primary ? "btn-primary" : "btn-default"}"
				data-inline-action='${injection_aps.ui.escape(encodeURIComponent(JSON.stringify(action)))}'
			>${injection_aps.ui.escape(injection_aps.ui.get_action_label(action))}</button>
		`;
		const primaryButton = primaryRoute
			? `<button type="button" class="btn btn-xs btn-primary" data-run-action="${injection_aps.ui.escape(primaryRoute.action_key)}" data-run-name="${injection_aps.ui.escape(row.name)}">${injection_aps.ui.escape(primaryRoute.label)}</button>`
			: inlineButton(primaryAction, true);
		return `
			<div class="ia-run-action-stack">
				<div class="ia-run-primary-actions">
					${primaryButton}
					${secondaryActions.map((action) => inlineButton(action, false)).join("")}
				</div>
				<div class="ia-run-nav-actions" aria-label="${injection_aps.ui.escape(__("Related Views", null, "Injection APS"))}">
					<span class="ia-run-related-label">${__("Related Views", null, "Injection APS")}</span>
					${primaryRoute && primaryRoute.action_key === "open_run" ? "" : this.renderRunNavButton("run", __("Open Run"), "open_run", row.name)}
					${this.renderRunNavButton("board", __("Board"), "open_gantt", row.name)}
					${primaryRoute && primaryRoute.action_key === "open_release" ? "" : this.renderRunNavButton("execution", __("Execution", null, "Injection APS"), "open_release", row.name)}
					${Number(row.v2_admission_available || 0) === 1 ? this.renderRunNavButton("admission", __("Demand Admission"), "open_admission", row.name) : ""}
				</div>
			</div>
		`;
	}

	renderRuns(rows) {
		if (!rows.length) {
			injection_aps.ui.render_table(this.table, [{ label: __("Info", null, "Injection APS"), fieldname: "message" }], []);
			return;
		}

		const columns = [
			{ label: __("Run / Scope", null, "Injection APS"), fieldname: "run_overview", className: "ia-run-col-overview" },
			{ label: __("Planning / Fulfillment", null, "Injection APS"), fieldname: "planning_fulfillment", className: "ia-run-col-quantities" },
			{ label: __("Risk / Execution", null, "Injection APS"), fieldname: "risk_execution", className: "ia-run-col-risk" },
			{ label: __("Next Step / Actions", null, "Injection APS"), fieldname: "next_actions", className: "ia-run-col-actions" },
		];
		const exportColumns = [
			{ label: __("Run", null, "Injection APS"), fieldname: "name" },
			{ label: __("Plant Floors"), fieldname: "selected_plant_floor_summary" },
			{ label: __("Planning Date"), fieldname: "planning_date" },
			{ label: __("Status", null, "Injection APS"), fieldname: "status" },
			{ label: __("Approval", null, "Injection APS"), fieldname: "approval_state" },
			{ label: __("Existing WO Policy"), fieldname: "existing_work_order_policy" },
			{ label: __("Plan Qty", null, "Injection APS"), fieldname: "total_net_requirement_qty", fieldtype: "Float" },
			{ label: __("Machine Scheduled", null, "Injection APS"), fieldname: "total_machine_scheduled_qty", fieldtype: "Float" },
			{ label: __("Demand Covered", null, "Injection APS"), fieldname: "total_demand_covered_qty", fieldtype: "Float" },
			{ label: __("Overproduction", null, "Injection APS"), fieldname: "total_overproduction_qty", fieldtype: "Float" },
			{ label: __("Unscheduled", null, "Injection APS"), fieldname: "total_unscheduled_qty", fieldtype: "Float" },
			{ label: __("Produced", null, "Injection APS"), fieldname: "total_produced_qty", fieldtype: "Float" },
			{ label: __("Delivered", null, "Injection APS"), fieldname: "total_delivered_qty", fieldtype: "Float" },
			{ label: __("Consistency", null, "Injection APS"), fieldname: "consistency_status" },
			{ label: __("Exceptions", null, "Injection APS"), fieldname: "exception_count", fieldtype: "Int" },
			{ label: __("Exec"), fieldname: "execution_health" },
			{ label: __("Next Step"), fieldname: "next_step" },
		];

		injection_aps.ui.render_table(
			this.table,
			columns,
			rows,
			(column, _value, row) => {
				if (column.fieldname === "run_overview") {
					const scope = row.selected_plant_floor_summary || row.plant_floor || "-";
					const sourceSummary = Number(row.v2_admission_available || 0) === 1
						? `<span>${__("Carry Forward", null, "Injection APS")} ${injection_aps.ui.escape(String(row.carried_commitment_count || 0))} · ${__("Source Runs")} ${injection_aps.ui.escape(String(row.source_run_count || 0))}</span>`
						: "";
					return `
						<div class="ia-run-overview">
							<div class="ia-run-heading">
								<div class="ia-run-name">${injection_aps.ui.route_link(row.name, `aps-planning-run/${encodeURIComponent(row.name)}`)}</div>
								<time class="ia-run-date">${injection_aps.ui.escape(injection_aps.ui.format_date(row.planning_date))}</time>
							</div>
							<div class="ia-run-pill-row">
								${injection_aps.ui.pill(injection_aps.ui.translate(row.status), this.getRunStatusTone(row.status))}
								${injection_aps.ui.pill(injection_aps.ui.translate(row.approval_state), row.approval_state === "Approved" ? "green" : "orange")}
							</div>
							<div class="ia-run-floor" title="${injection_aps.ui.escape(scope)}">${injection_aps.ui.escape(scope)}</div>
							<div class="ia-run-overview-meta">
								<span>${__("Existing WO", null, "Injection APS")}：${injection_aps.ui.escape(injection_aps.ui.get_existing_work_order_policy_label(row.existing_work_order_policy))}</span>
								${sourceSummary}
							</div>
						</div>
					`;
				}
				if (column.fieldname === "planning_fulfillment") {
					return `
						<div class="ia-run-quantity-sections">
							<div class="ia-run-quantity-group" role="group" aria-label="${injection_aps.ui.escape(__("Planning", null, "Injection APS"))}">
								<div class="ia-run-group-label">${__("Planning", null, "Injection APS")}</div>
								<div class="ia-run-metric-grid ia-run-metric-grid-3">
									${this.renderRunMetric(__("Plan Qty", null, "Injection APS"), row.total_net_requirement_qty)}
									${this.renderRunMetric(__("Machine Scheduled", null, "Injection APS"), row.total_machine_scheduled_qty)}
									${this.renderRunMetric(__("Demand Covered", null, "Injection APS"), row.total_demand_covered_qty)}
								</div>
							</div>
							<div class="ia-run-quantity-group" role="group" aria-label="${injection_aps.ui.escape(__("Fulfillment", null, "Injection APS"))}">
								<div class="ia-run-group-label">${__("Fulfillment", null, "Injection APS")}</div>
								<div class="ia-run-metric-grid ia-run-metric-grid-4">
									${this.renderRunMetric(__("Unscheduled", null, "Injection APS"), row.total_unscheduled_qty, Number(row.total_unscheduled_qty || 0) > 0 ? "warning" : "")}
									${this.renderRunMetric(__("Overproduction", null, "Injection APS"), row.total_overproduction_qty, Number(row.total_overproduction_qty || 0) > 0 ? "warning" : "")}
									${this.renderRunMetric(__("Produced", null, "Injection APS"), row.total_produced_qty)}
									${this.renderRunMetric(__("Delivered", null, "Injection APS"), row.total_delivered_qty)}
								</div>
							</div>
						</div>
					`;
				}
				if (column.fieldname === "risk_execution") {
					const health = row.execution_health || {};
					const consistencyValue = row.consistency_status || "Unchecked";
					const exceptionTone = Number(row.exception_count || 0) > 0 ? "red" : "blue";
					return `
						<div class="ia-run-risk-stack">
							<div class="ia-run-consistency">
								<span>${__("Consistency", null, "Injection APS")}</span>
								${injection_aps.ui.pill(injection_aps.ui.translate(consistencyValue), consistencyValue === "Valid" ? "green" : consistencyValue === "Invalid" ? "red" : "orange")}
							</div>
							<div class="ia-run-exception-line"><span>${__("Exceptions", null, "Injection APS")}</span>${injection_aps.ui.pill(String(row.exception_count || 0), exceptionTone)}</div>
							<div class="ia-run-execution-grid">
								${this.renderRunMetric(__("Running", null, "Injection APS"), health.running || 0)}
								${this.renderRunMetric(__("Delayed", null, "Injection APS"), health.delayed || 0, Number(health.delayed || 0) > 0 ? "warning" : "")}
								${this.renderRunMetric(__("No Update"), health.no_recent_update || 0, Number(health.no_recent_update || 0) > 0 ? "warning" : "")}
							</div>
						</div>
					`;
				}
				if (column.fieldname === "next_actions") {
					const nextStep = injection_aps.ui.translate(injection_aps.ui.get_value(row, "next_actions.next_step", ""));
					const blockingReason = injection_aps.ui.translate(injection_aps.ui.get_value(row, "next_actions.blocking_reason", ""));
					return `
						<div class="ia-run-next-action">
							<div class="ia-run-group-label">${__("Next Step")}</div>
							<div class="ia-run-next-step">${injection_aps.ui.escape(nextStep)}</div>
							${blockingReason ? `<div class="ia-run-blocking-reason" title="${injection_aps.ui.escape(blockingReason)}">${injection_aps.ui.escape(blockingReason)}</div>` : ""}
							${this.renderRunActions(row)}
						</div>
					`;
				}
				return "";
			},
			{
				exportable: true,
				export_title: __("Recalc Console"),
				export_sheet_name: __("APS Runs"),
				export_file_name: "aps_planning_runs",
				export_subtitle: __("APS run list with execution health and next actions."),
				export_columns: exportColumns,
				export_formatter: (column, value, row) => {
					if (["status", "approval_state", "consistency_status"].includes(column.fieldname)) {
						return injection_aps.ui.translate(value || (column.fieldname === "consistency_status" ? "Unchecked" : ""));
					}
					if (column.fieldname === "existing_work_order_policy") {
						return injection_aps.ui.get_existing_work_order_policy_label(value);
					}
					if (column.fieldname === "planning_date") {
						return injection_aps.ui.format_date(value);
					}
					if (column.fieldname === "selected_plant_floor_summary") {
						return value || row.plant_floor || "";
					}
					if (column.fieldname === "execution_health") {
						return this.getExecutionHealthText(row);
					}
					if (column.fieldname === "next_step") {
						return injection_aps.ui.translate(injection_aps.ui.get_value(row, "next_actions.next_step", ""));
					}
					return value;
				},
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
			.find("[data-run-action='open_run']")
			.each((_, node) => {
				node.addEventListener("click", () => {
					injection_aps.ui.go_to(`aps-planning-run/${encodeURIComponent(node.dataset.runName || "")}`);
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

		$(this.table)
			.find("[data-run-action='open_admission']")
			.each((_, node) => {
				node.addEventListener("click", () => {
					injection_aps.ui.go_to(`aps-demand-admission-workbench?run_name=${encodeURIComponent(node.dataset.runName || "")}`);
				});
			});
	}

	openRunDialog() {
		const dialog = new frappe.ui.Dialog({
			title: __("Create Recalc Run"),
			fields: [
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company", null, "Injection APS"), reqd: 1, default: this.companyField.get_value() || frappe.defaults.get_user_default("Company") },
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
							label: __("Plant Floor", null, "Injection APS"),
							in_list_view: 1,
							reqd: 1,
						},
					],
				},
				{ fieldname: "horizon_days", fieldtype: "Int", label: __("Horizon Days", null, "Injection APS"), default: 14, reqd: 1 },
				injection_aps.ui.get_existing_work_order_policy_field(),
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
							__("Existing work orders: {0}").replace(
								"{0}",
								injection_aps.ui.get_existing_work_order_policy_label(values.existing_work_order_policy)
							),
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
						existing_work_order_policy: values.existing_work_order_policy,
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
