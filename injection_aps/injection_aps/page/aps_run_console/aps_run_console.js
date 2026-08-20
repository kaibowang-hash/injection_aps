frappe.pages["aps-run-console"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_ui_loader.js", () => {
		ensureInjectionAPSRunConsoleStyles();
		injection_aps.ui_loader.start("20260821.2", () => {
			if (!wrapper.injection_aps_controller) {
				wrapper.injection_aps_controller = new InjectionAPSRunConsole(wrapper);
			}
			wrapper.injection_aps_controller.refresh();
		});
	});
};

function ensureInjectionAPSRunConsoleStyles() {
	const styleId = "injection-aps-run-console-style";
	const styleHref = "/assets/injection_aps/css/aps_run_console.css?v=20260821.2";
	let style = document.getElementById(styleId);
	if (!style) {
		style = document.createElement("link");
		style.id = styleId;
		style.rel = "stylesheet";
		document.head.appendChild(style);
	}
	if (style.getAttribute("href") !== styleHref) {
		style.setAttribute("href", styleHref);
	}
}

frappe.pages["aps-run-console"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSRunConsole {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.targetRun = frappe.utils.get_url_arg("run_name") || "";
		this.fromAdmission = frappe.utils.get_url_arg("from_admission") || "";
		this.targetRunOpened = false;
		this.v2Enabled = false;
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
				<div class="ia-workflow-host"></div>
				<div class="ia-banner ia-run-guide">
					<p>${__("Recalculate -> Confirm Run -> Review Work Order Proposals -> Review Day/Night Shift Proposals -> Formal Scheduling -> Execution Feedback. This console centralizes each APS run and its next action.")}</p>
				</div>
				<div class="ia-feedback"></div>
				<div class="ia-panel">
					<div class="ia-run-table"></div>
				</div>
			</div>
		`);
		this.workflowHost = this.page.main.find(".ia-workflow-host")[0];
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
				run_name: this.targetRun || undefined,
			});
			this.v2Enabled = Number(data.v2_enabled || 0) === 1;
			if (injection_aps.ui.can_run_action("run_trial")) {
				this.page.set_primary_action(
					this.v2Enabled ? __("Create admission draft", null, "Injection APS") : __("Recalculate"),
					() => this.openRunDialog()
				);
			}
			const rows = data.runs || [];
			this.renderRunWorkflow(rows.find((row) => row.name === this.targetRun));
			this.renderRuns(rows);
			this.focusTargetRun(rows);
			injection_aps.ui.set_feedback(
				this.feedback,
				this.fromAdmission
					? (this.fromAdmission === "auto" ? __("Only P0 demand was found and confirmed automatically. Run the first APS calculation now.", null, "Injection APS") : __("Demand admission is complete. Recalculate this Run before capacity analysis.", null, "Injection APS"))
					: __("Recalc Console refreshed."),
				this.fromAdmission ? "warning" : ""
			);
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load APS runs."), "error");
		}
	}

	renderRunWorkflow(row) {
		const state = injection_aps.ui.get_value(row, "next_actions.admission_state", {}) || {};
		const admissionPending = Number(state.baseline_ready || 0) === 1 && Number(state.confirmed || 0) !== 1;
		const calculationCurrent = Number(state.recalculation_required || 0) === 1 || (row && row.status === "Draft" && !admissionPending);
		injection_aps.ui.render_workflow_steps(this.workflowHost, [
			{ label: __("Demand baseline", null, "Injection APS"), status: row ? "complete" : "upcoming", route: "aps-net-requirement-workbench" },
			{ label: __("Batch admission", null, "Injection APS"), status: admissionPending ? "current" : row ? "complete" : "upcoming", route: row ? `aps-demand-admission-workbench?run_name=${encodeURIComponent(row.name)}` : "" },
			{ label: __("Impact confirmation", null, "Injection APS"), status: admissionPending ? "upcoming" : row ? "complete" : "upcoming" },
			{ label: __("APS calculation", null, "Injection APS"), status: calculationCurrent ? "current" : "upcoming" },
		]);
	}

	focusTargetRun(rows) {
		if (!this.targetRun || this.targetRunOpened) {
			return;
		}
		const row = rows.find((item) => item.name === this.targetRun);
		if (!row) {
			return;
		}
		this.targetRunOpened = true;
		this.openRunDetails(row);
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

	renderRunDrawerMetricRow(label, value, tone) {
		return `
			<div class="ia-kv-row${tone ? ` ia-run-drawer-row-${tone}` : ""}">
				<div class="ia-kv-key">${injection_aps.ui.escape(label)}</div>
				<div class="ia-kv-value ia-run-drawer-number">${injection_aps.ui.escape(this.formatRunQty(value))}</div>
			</div>
		`;
	}

	getExecutionHealthText(row) {
		const health = row.execution_health || {};
		return `${__("Running", null, "Injection APS")}:${health.running || 0} / ${__("Delayed", null, "Injection APS")}:${health.delayed || 0} / ${__("No Update")}:${health.no_recent_update || 0}`;
	}

	getVisibleRunActions(row) {
		return (injection_aps.ui.get_value(row, "next_actions.actions", []) || [])
			.filter((action) => !["open_gantt", "open_release_center"].includes(action.action_key))
			.filter((action) => injection_aps.ui.can_run_action(action));
	}

	getEnabledRunActions(row) {
		return this.getVisibleRunActions(row)
			.filter((action) => Number(action.enabled || 0) === 1);
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
				class="btn btn-default btn-sm ia-run-nav-button"
				data-run-action="${injection_aps.ui.escape(actionKey)}"
				data-run-name="${injection_aps.ui.escape(runName)}"
				title="${injection_aps.ui.escape(label)}"
				aria-label="${injection_aps.ui.escape(label)}"
			>${this.renderRunNavIcon(icon)}<span>${injection_aps.ui.escape(label)}</span></button>
		`;
	}

	getPrimaryRouteAction(row) {
		const nextStep = injection_aps.ui.get_value(row, "next_actions.next_step", "");
		if (nextStep === "Demand Admission") {
			return { action_key: "open_admission", label: __("Open demand admission", null, "Injection APS"), is_route: 1 };
		}
		if (nextStep === "Analyze and Apply Capacity") {
			return { action_key: "open_run", label: __("Open Run"), is_route: 1 };
		}
		if (["Monitor Execution Drift", "Review Work Order Proposals", "Review Day/Night Shift Proposals"].includes(nextStep)) {
			return { action_key: "open_release", label: __("Execution", null, "Injection APS"), is_route: 1 };
		}
		return null;
	}

	getPrimaryRunAction(row) {
		return this.getPrimaryRouteAction(row)
			|| this.getEnabledRunActions(row)[0]
			|| { action_key: "open_run", label: __("Open Run"), is_route: 1 };
	}

	renderRunActionButton(action, primary) {
		if (action.is_route) {
			return `<button type="button" class="btn btn-xs ${primary ? "btn-primary" : "btn-default"}" data-run-action="${injection_aps.ui.escape(action.action_key)}" data-run-name="${injection_aps.ui.escape(action.run_name || "")}">${injection_aps.ui.escape(action.label || "")}</button>`;
		}
		return `
			<button
				type="button"
				class="btn btn-xs ${primary ? "btn-primary" : "btn-default"}"
				data-inline-action='${injection_aps.ui.escape(encodeURIComponent(JSON.stringify(action)))}'
			>${injection_aps.ui.escape(injection_aps.ui.get_action_label(action))}</button>
		`;
	}

	renderRunPrimaryAction(row) {
		const action = Object.assign({}, this.getPrimaryRunAction(row), { run_name: row.name });
		return this.renderRunActionButton(action, true);
	}

	renderRunDrawerOperation(action) {
		const enabled = Number(action.enabled || 0) === 1;
		const disabledReason = injection_aps.ui.translate(action.disabled_reason || "");
		return `
			<div class="ia-run-drawer-operation">
				<button
					type="button"
					class="btn btn-xs btn-default"
					data-inline-action='${injection_aps.ui.escape(encodeURIComponent(JSON.stringify(action)))}'
					${enabled ? "" : 'disabled aria-disabled="true"'}
				>${injection_aps.ui.escape(injection_aps.ui.get_action_label(action))}</button>
				${!enabled && disabledReason ? `<span>${injection_aps.ui.escape(disabledReason)}</span>` : ""}
			</div>
		`;
	}

	renderRunDrawerActions(row) {
		const primaryAction = this.getPrimaryRunAction(row);
		const secondaryActions = this.getVisibleRunActions(row)
			.filter((action) => action.action_key !== primaryAction.action_key);
		return `
			${secondaryActions.length ? `
				<div class="ia-panel ia-run-drawer-action-group">
					<div class="ia-panel-head"><h4>${__("Other Actions", null, "Injection APS")}</h4></div>
					<div class="ia-run-drawer-operation-list">${secondaryActions.map((action) => this.renderRunDrawerOperation(action)).join("")}</div>
				</div>
			` : ""}
			<div class="ia-panel ia-run-drawer-action-group">
				<div class="ia-panel-head"><h4>${__("Related Views", null, "Injection APS")}</h4></div>
				<div class="ia-run-nav-actions" aria-label="${injection_aps.ui.escape(__("Related Views", null, "Injection APS"))}">
					${this.renderRunNavButton("run", __("Open Run"), "open_run", row.name)}
					${this.renderRunNavButton("board", __("Board"), "open_gantt", row.name)}
					${this.renderRunNavButton("execution", __("Execution", null, "Injection APS"), "open_release", row.name)}
					${Number(row.v2_admission_available || 0) === 1 ? this.renderRunNavButton("admission", __("Demand Admission"), "open_admission", row.name) : ""}
				</div>
			</div>
		`;
	}

	renderRunDrawer(row) {
		const health = row.execution_health || {};
		const consistencyValue = row.consistency_status || "Unchecked";
		const scope = row.selected_plant_floor_summary || row.plant_floor || "-";
		const nextStep = injection_aps.ui.translate(injection_aps.ui.get_value(row, "next_actions.next_step", ""));
		const blockingReason = injection_aps.ui.translate(injection_aps.ui.get_value(row, "next_actions.blocking_reason", ""));
		const admissionState = injection_aps.ui.get_value(row, "next_actions.admission_state", {}) || {};
		return `
			<div class="ia-page ia-drawer-stack ia-run-drawer">
				<div class="ia-status-line">
					<div class="ia-status-cell"><span class="ia-status-label">${__("Status", null, "Injection APS")}</span><div class="ia-status-value">${injection_aps.ui.pill(injection_aps.ui.translate(row.status), this.getRunStatusTone(row.status))}</div></div>
					<div class="ia-status-cell"><span class="ia-status-label">${__("Approval", null, "Injection APS")}</span><div class="ia-status-value">${injection_aps.ui.pill(injection_aps.ui.translate(row.approval_state), row.approval_state === "Approved" ? "green" : "orange")}</div></div>
					<div class="ia-status-cell"><span class="ia-status-label">${__("Exceptions", null, "Injection APS")}</span><div class="ia-status-value">${injection_aps.ui.pill(String(row.exception_count || 0), Number(row.exception_count || 0) > 0 ? "red" : "blue")}</div></div>
				</div>
				<section class="ia-panel">
						<div class="ia-panel-head"><h4>${__("Overview", null, "Injection APS")}</h4></div>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Company", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.escape(row.company || "-")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Plant Floors")}</div><div class="ia-kv-value">${injection_aps.ui.escape(scope)}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Planning Date")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.format_date(row.planning_date))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Existing WO", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.get_existing_work_order_policy_label(row.existing_work_order_policy))}</div></div>
							${Number(row.v2_admission_available || 0) === 1 ? `<div class="ia-kv-row"><div class="ia-kv-key">${__("Demand Ownership", null, "Injection APS")}</div><div class="ia-kv-value">${__("Carry Forward", null, "Injection APS")} ${injection_aps.ui.escape(String(row.carried_commitment_count || 0))} · ${__("Source Runs")} ${injection_aps.ui.escape(String(row.source_run_count || 0))}</div></div>` : ""}
							${this.v2Enabled && Number(admissionState.legacy_unchecked || 0) === 1 ? `<div class="ia-kv-row"><div class="ia-kv-key">${__("Demand admission", null, "Injection APS")}</div><div class="ia-kv-value">${__("Admission status pending review", null, "Injection APS")}</div></div>` : ""}
						</div>
				</section>
				<section class="ia-panel">
						<div class="ia-panel-head"><h4>${__("Workflow", null, "Injection APS")}</h4></div>
						<div class="ia-run-next-step">${injection_aps.ui.escape(nextStep || "-")}</div>
						${blockingReason ? `<div class="ia-alert warning"><strong>${__("Blocking Reason")}</strong><div>${injection_aps.ui.escape(blockingReason)}</div></div>` : `<div class="ia-muted">${__("No blocking reason is currently recorded.", null, "Injection APS")}</div>`}
				</section>
				<section class="ia-panel">
					<div class="ia-panel-head"><h4>${__("Planning and Fulfillment", null, "Injection APS")}</h4></div>
					<div class="ia-kv ia-run-drawer-metrics">
						${this.renderRunDrawerMetricRow(__("Plan Qty", null, "Injection APS"), row.total_net_requirement_qty)}
						${this.renderRunDrawerMetricRow(__("Machine Scheduled", null, "Injection APS"), row.total_machine_scheduled_qty)}
						${this.renderRunDrawerMetricRow(__("Demand Covered", null, "Injection APS"), row.total_demand_covered_qty)}
						${this.renderRunDrawerMetricRow(__("Unscheduled", null, "Injection APS"), row.total_unscheduled_qty, Number(row.total_unscheduled_qty || 0) > 0 ? "warning" : "")}
						${this.renderRunDrawerMetricRow(__("Overproduction", null, "Injection APS"), row.total_overproduction_qty, Number(row.total_overproduction_qty || 0) > 0 ? "warning" : "")}
						${this.renderRunDrawerMetricRow(__("Produced", null, "Injection APS"), row.total_produced_qty)}
						${this.renderRunDrawerMetricRow(__("Delivered", null, "Injection APS"), row.total_delivered_qty)}
					</div>
				</section>
				<section class="ia-panel">
					<div class="ia-panel-head"><h4>${__("Risk and Execution", null, "Injection APS")}</h4></div>
					<div class="ia-kv ia-run-drawer-metrics">
						<div class="ia-kv-row"><div class="ia-kv-key">${__("Consistency", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.pill(injection_aps.ui.translate(consistencyValue), consistencyValue === "Valid" ? "green" : consistencyValue === "Invalid" ? "red" : "orange")}</div></div>
						${this.renderRunDrawerMetricRow(__("Running", null, "Injection APS"), health.running || 0)}
						${this.renderRunDrawerMetricRow(__("Delayed", null, "Injection APS"), health.delayed || 0, Number(health.delayed || 0) > 0 ? "warning" : "")}
						${this.renderRunDrawerMetricRow(__("No Update"), health.no_recent_update || 0, Number(health.no_recent_update || 0) > 0 ? "warning" : "")}
					</div>
				</section>
				${this.renderRunDrawerActions(row)}
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
			{ label: __("Key Results", null, "Injection APS"), fieldname: "key_results", className: "ia-run-col-results" },
			{ label: __("Next Step / Actions", null, "Injection APS"), fieldname: "next_action", className: "ia-run-col-actions" },
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
						</div>
					`;
				}
				if (column.fieldname === "key_results") {
					return `
						<div class="ia-run-metric-grid ia-run-key-metrics">
							${this.renderRunMetric(__("Plan Qty", null, "Injection APS"), row.total_net_requirement_qty)}
							${this.renderRunMetric(__("Machine Scheduled", null, "Injection APS"), row.total_machine_scheduled_qty)}
							${this.renderRunMetric(__("Unscheduled", null, "Injection APS"), row.total_unscheduled_qty, Number(row.total_unscheduled_qty || 0) > 0 ? "warning" : "")}
							${this.renderRunMetric(__("Exceptions", null, "Injection APS"), row.exception_count, Number(row.exception_count || 0) > 0 ? "danger" : "")}
						</div>
					`;
				}
				if (column.fieldname === "next_action") {
					const nextStep = injection_aps.ui.translate(injection_aps.ui.get_value(row, "next_actions.next_step", ""));
					return `
						<div class="ia-run-next-action">
							<div class="ia-run-group-label">${__("Next Step")}</div>
							<div class="ia-run-next-step">${injection_aps.ui.escape(nextStep)}</div>
							<div class="ia-run-primary-actions">
								${this.renderRunPrimaryAction(row)}
								<button type="button" class="btn btn-xs btn-default" data-run-details="${injection_aps.ui.escape(row.name)}">${__("View Details", null, "Injection APS")}</button>
							</div>
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

		const rowsByName = new Map(rows.map((row) => [row.name, row]));
		$(this.table).find("[data-run-details]").each((_, node) => {
			node.addEventListener("click", () => {
				const row = rowsByName.get(node.dataset.runDetails || "");
				if (row) {
					this.openRunDetails(row);
				}
			});
		});
		this.bindRunActionHandlers(this.table);
	}

	openRunDetails(row) {
		const subtitle = [row.name, injection_aps.ui.format_date(row.planning_date)].filter(Boolean).join(" · ");
		injection_aps.ui.open_drawer(
			__("APS Run Details", null, "Injection APS"),
			subtitle,
			this.renderRunDrawer(row)
		);
		this.bindRunActionHandlers(injection_aps.ui.ensure_drawer(), true);
	}

	bindRunActionHandlers(root, closeDrawerAfterAction) {
		$(root).find("[data-inline-action]").each((_, node) => {
			node.addEventListener("click", async () => {
				const action = JSON.parse(decodeURIComponent(node.dataset.inlineAction || ""));
				const response = await injection_aps.ui.run_action(action);
				injection_aps.ui.show_warnings(response, __("Planning Warnings"), "preflight_warning_count");
				if (closeDrawerAfterAction && response != null) {
					injection_aps.ui.close_drawer();
				}
				await this.refresh();
			});
		});

		const routes = {
			open_run: (runName) => `aps-planning-run/${encodeURIComponent(runName)}`,
			open_gantt: (runName) => `aps-schedule-gantt?run_name=${encodeURIComponent(runName)}`,
			open_release: (runName) => `aps-release-center?run_name=${encodeURIComponent(runName)}`,
			open_admission: (runName) => `aps-demand-admission-workbench?run_name=${encodeURIComponent(runName)}`,
		};
		$(root).find("[data-run-action]").each((_, node) => {
			node.addEventListener("click", () => {
				const route = routes[node.dataset.runAction];
				if (!route) {
					return;
				}
				if (closeDrawerAfterAction) {
					injection_aps.ui.close_drawer();
				}
				injection_aps.ui.go_to(route(node.dataset.runName || ""));
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
			primary_action_label: this.v2Enabled ? __("Create admission draft", null, "Injection APS") : __("Recalculate"),
			primary_action: async (values) => {
				const plantFloors = this.extractPlantFloors(values.plant_floor_rows);
				if (!plantFloors.length) {
					frappe.msgprint(__("Select at least one Plant Floor before APS planning."));
					return;
				}
				const confirmed = await injection_aps.ui.confirm_action(
					{ action_key: "run_trial", confirm_required: 1 },
					{
						title: this.v2Enabled ? __("Confirm admission draft", null, "Injection APS") : __("Confirm Recalculate"),
						summary_lines: [
							__("Company: {0}").replace("{0}", values.company || "-"),
							__("Plant Floors: {0}").replace("{0}", plantFloors.join(", ") || "-"),
							__("Horizon: {0} days").replace("{0}", String(values.horizon_days || 14)),
							__("Existing work orders: {0}").replace(
								"{0}",
								injection_aps.ui.get_existing_work_order_policy_label(values.existing_work_order_policy)
							),
							...(this.v2Enabled ? [__("This creates the demand baseline first; APS calculation starts only after demand admission.", null, "Injection APS")] : []),
						],
					}
				);
				if (!confirmed) {
					return;
				}
				const result = await injection_aps.ui.xcall(
					{
						message: this.v2Enabled ? __("Preparing demand baseline...", null, "Injection APS") : __("Running recalculation..."),
						success_message: this.v2Enabled ? __("Admission draft created.", null, "Injection APS") : __("Recalculation completed."),
						busy_key: `run-console-trial:${values.company || "all"}:${plantFloors.join("|") || "all"}`,
						feedback_target: this.feedback,
						success_feedback: this.v2Enabled ? __("Demand baseline is ready. Opening the next step.", null, "Injection APS") : __("Recalculation completed. Refreshing console..."),
					},
					this.v2Enabled ? "injection_aps.api.app.create_trial_run_for_admission" : "injection_aps.api.app.run_planning_run",
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
				if (this.v2Enabled && result.next_route) {
					injection_aps.ui.go_to(result.next_route);
					return;
				}
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
