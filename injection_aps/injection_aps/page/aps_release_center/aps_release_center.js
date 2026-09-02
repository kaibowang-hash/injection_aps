frappe.pages["aps-release-center"].on_page_load = function (wrapper) {
	injection_aps.ui_loader.start("20260901.1", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSReleaseCenter(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-release-center"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSReleaseCenter {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.lastImpact = null;
		this.releaseDrawerState = null;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Execution", null, "Injection APS"),
			single_column: true,
		});
		this.runField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "run_name",
			options: "APS Planning Run",
			label: __("APS Run", null, "Injection APS"),
			default: injection_aps.ui.get_query_param("run_name") || undefined,
			change: () => this.refresh(),
		});
		if (injection_aps.ui.can_run_action("sync_execution")) {
			this.page.set_primary_action(__("Sync", null, "Injection APS"), () => this.syncExecution());
		}
		this.page.set_secondary_action(__("Insert Order Impact"), () => this.openImpactDialog());

		this.page.main.html(`
				<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Execution", null, "Injection APS")}</h3>
					<p>${__("This page handles proposal review, formal apply, execution feedback, and exception handling after an APS run. Select an APS run first before working here.")}</p>
				</div>
				<div class="ia-empty-state-host"></div>
				<div class="ia-run-body">
					<div class="ia-run-context-host"></div>
				<div class="ia-status-host"></div>
				<div class="ia-action-host"></div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-fulfillment-warnings"></div>
				<div class="ia-feedback"></div>
				<div class="ia-grid-2">
					<div class="ia-panel">
						<h4>${__("Work Order Proposal Batches")}</h4>
						<div class="ia-wo-proposal-table" style="margin-top: 8px;"></div>
					</div>
					<div class="ia-panel">
						<h4>${__("Day/Night Shift Proposal Batches")}</h4>
						<div class="ia-shift-proposal-table" style="margin-top: 8px;"></div>
					</div>
				</div>
				<div class="ia-panel">
					<h4>${__("Formal Apply Logs")}</h4>
					<div class="ia-release-table" style="margin-top: 8px;"></div>
				</div>
				<div class="ia-panel">
					<h4>${__("Open Exceptions")}</h4>
					<div class="ia-exception-table" style="margin-top: 8px;"></div>
				</div>
				<div class="ia-panel">
					<h4>${__("Insert Order Impact Analysis")}</h4>
					<div class="ia-impact-summary ia-card-grid" style="margin-top: 8px;"></div>
					<div class="ia-impact-table" style="margin-top: 8px;"></div>
				</div>
				</div>
			</div>
		`);
		this.emptyStateHost = this.page.main.find(".ia-empty-state-host")[0];
		this.runBody = this.page.main.find(".ia-run-body")[0];
		this.runContextHost = this.page.main.find(".ia-run-context-host")[0];
		this.summary = this.page.main.find(".ia-summary")[0];
		this.fulfillmentWarnings = this.page.main.find(".ia-fulfillment-warnings")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.actionHost = this.page.main.find(".ia-action-host")[0];
		this.woProposalTable = this.page.main.find(".ia-wo-proposal-table")[0];
		this.shiftProposalTable = this.page.main.find(".ia-shift-proposal-table")[0];
		this.releaseTable = this.page.main.find(".ia-release-table")[0];
		this.exceptionTable = this.page.main.find(".ia-exception-table")[0];
		this.impactSummary = this.page.main.find(".ia-impact-summary")[0];
		this.impactTable = this.page.main.find(".ia-impact-table")[0];
		this.exceptionRowsByName = {};
	}

	renderCollapsedChipList(values, options) {
		const settings = Object.assign({ previewCount: 5 }, options || {});
		const rows = (values || []).filter(Boolean);
		if (!rows.length) {
			return { html: `<div class="ia-muted">-</div>`, bind: null };
		}
		const listId = `ia-collapsible-${Math.random().toString(36).slice(2, 8)}`;
		const toggleId = `ia-collapsible-toggle-${Math.random().toString(36).slice(2, 8)}`;
		const renderPreview = (collapsed) => {
			const visible = collapsed ? rows.slice(0, settings.previewCount) : rows;
			return visible.map((row) => `<span class="ia-chip">${injection_aps.ui.escape(row)}</span>`).join("");
		};
		const html = `
			<div class="ia-list-preview" id="${listId}" data-collapsed="1">${renderPreview(true)}</div>
			${rows.length > settings.previewCount ? `<button type="button" class="ia-inline-toggle" id="${toggleId}">${__("Expand All", null, "Injection APS")} (+${rows.length - settings.previewCount})</button>` : ""}
		`;
		const bind = () => {
			if (rows.length <= settings.previewCount) {
				return;
			}
			const listNode = document.getElementById(listId);
			const toggleNode = document.getElementById(toggleId);
			if (!listNode || !toggleNode) {
				return;
			}
			toggleNode.addEventListener("click", () => {
				const collapsed = listNode.dataset.collapsed !== "0";
				listNode.innerHTML = renderPreview(!collapsed);
				listNode.dataset.collapsed = collapsed ? "0" : "1";
				toggleNode.textContent = collapsed ? __("Collapse", null, "Injection APS") : `${__("Expand All", null, "Injection APS")} (+${rows.length - settings.previewCount})`;
			});
		};
		return { html, bind };
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		const runName = this.runField.get_value();
		injection_aps.ui.set_feedback(this.feedback, __("Loading execution center..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_release_center_data", {
				run_name: runName || undefined,
			});
			this.data = data;
			if (!runName) {
				this.runBody.style.display = "none";
				injection_aps.ui.render_run_empty_state(this.emptyStateHost, {
					title: __("No APS Run Selected"),
					description: __("Execution center depends on a single APS run context. Select an APS run first to review proposals, formal apply logs, and exceptions."),
					recent_runs: (data.recent_runs || []).map((row) => Object.assign({}, row, { route: row.execution_route || row.route })),
					console_route: "aps-run-console",
				});
				injection_aps.ui.set_feedback(this.feedback, "");
				return;
			}
			this.runBody.style.display = "";
			this.emptyStateHost.innerHTML = "";
			injection_aps.ui.render_run_context(this.runContextHost, data.run_context || null);
			injection_aps.ui.render_status_line(this.statusHost, data.run_context || null);
			injection_aps.ui.render_actions(
				this.actionHost,
				(injection_aps.ui.get_value(data, "run_context.actions", []) || []).filter((row) =>
					["generate_work_order_proposals", "open_gantt"].includes(row.action_key)
				),
				async (action) => {
					const response = await injection_aps.ui.run_action(action);
					injection_aps.ui.show_warnings(response, __("APS Warnings"), "preflight_warning_count");
					await this.refresh();
				}
			);
			this.renderWOSReleaseAction(data.run_context || null);

			const executionHealth = data.execution_health || {};
			const quantities = data.quantity_summary || {};
			const fulfillment = data.fulfillment_summary || {};
			injection_aps.ui.render_warnings(
				this.fulfillmentWarnings,
				{
					warning_count: data.fulfillment_warning_count || 0,
					warnings: data.fulfillment_warnings || [],
				},
				__("Fulfillment Data Warnings")
			);
			const exceptions = data.exceptions || [];
			const blocking = exceptions.filter((row) => Number(row.is_blocking || 0)).length;
			injection_aps.ui.render_cards(this.summary, [
				{ label: __("Planned Qty", null, "Injection APS"), value: injection_aps.ui.format_number(quantities.planned_qty || 0) },
				{ label: __("Machine Scheduled Qty", null, "Injection APS"), value: injection_aps.ui.format_number(quantities.machine_scheduled_qty || 0) },
				{ label: __("Demand Covered Qty", null, "Injection APS"), value: injection_aps.ui.format_number(quantities.demand_covered_qty || 0) },
				{ label: __("Overproduction Qty", null, "Injection APS"), value: injection_aps.ui.format_number(quantities.overproduction_qty || 0) },
				{ label: __("Unscheduled Qty"), value: injection_aps.ui.format_number(quantities.unscheduled_qty || 0) },
				{ label: __("Produced Qty", null, "Injection APS"), value: injection_aps.ui.format_number(quantities.produced_qty || 0) },
				{ label: __("Delivered Qty", null, "Injection APS"), value: injection_aps.ui.format_number(quantities.delivered_qty || 0) },
				{ label: __("Prebuild / JIT", null, "Injection APS"), value: `${injection_aps.ui.format_number(fulfillment.prebuild_qty || 0)} / ${injection_aps.ui.format_number(fulfillment.jit_qty || 0)}` },
				{ label: __("Current Deliverable", null, "Injection APS"), value: injection_aps.ui.format_number(fulfillment.current_deliverable_qty || 0) },
				{ label: __("Prebuild Inventory", null, "Injection APS"), value: injection_aps.ui.format_number(fulfillment.prebuild_inventory_qty || 0) },
				{ label: __("Cancel Stock Risk", null, "Injection APS"), value: injection_aps.ui.format_number(fulfillment.cancellation_inventory_risk_qty || 0) },
				{ label: __("Consistency", null, "Injection APS"), value: injection_aps.ui.translate(quantities.consistency_status || "Unchecked") },
				{ label: __("Delayed", null, "Injection APS"), value: executionHealth.delayed_segments || 0 },
				{ label: __("Blocking"), value: blocking, note: __("Manual handling is required before formal apply.") },
			]);
			this.renderWorkOrderProposalTable(data.work_order_proposal_batches || []);
			this.renderShiftProposalTable(data.shift_schedule_proposal_batches || []);
			this.renderReleaseTable(data.release_batches || []);
			this.renderExceptionTable(exceptions);
			this.renderImpact();
			injection_aps.ui.set_feedback(this.feedback, __("Execution center refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load execution center."), "error");
		}
	}

	renderWOSReleaseAction(runContext) {
		const actions = (runContext && runContext.actions) || [];
		const action = actions.find((row) => row.action_key === "generate_shift_schedule_proposals");
		if (!action || !Number(action.enabled || 0) || !injection_aps.ui.can_run_action(action)) {
			return;
		}
		let strip = this.actionHost.querySelector(".ia-action-strip");
		if (!strip) {
			this.actionHost.innerHTML = `<div class="ia-action-strip"></div>`;
			strip = this.actionHost.querySelector(".ia-action-strip");
		}
		const button = document.createElement("button");
		button.type = "button";
		button.className = `btn btn-xs ${strip.children.length ? "btn-default" : "btn-primary"} ia-action-btn`;
		button.textContent = __("Release WOS");
		button.title = __("Preview and generate day/night WOS proposals by date and shift.");
		button.addEventListener("click", () => this.openWOSReleaseDrawer());
		strip.appendChild(button);
	}

	getDefaultWOSReleaseDate() {
		const today = frappe.datetime && frappe.datetime.get_today ? frappe.datetime.get_today() : new Date().toISOString().slice(0, 10);
		const dates = [];
		(this.data && this.data.release_batches ? this.data.release_batches : []).forEach((batch) => {
			(batch.work_order_schedulings || []).forEach((row) => {
				if (row.posting_date) {
					dates.push(String(row.posting_date));
				}
			});
		});
		if (!dates.length) {
			return today;
		}
		const uniqueDates = Array.from(new Set(dates)).sort();
		return uniqueDates.find((dateValue) => dateValue >= today) || uniqueDates[0];
	}

	openWOSReleaseDrawer() {
		const runName = this.getSelectedRun();
		if (!runName) {
			return;
		}
		this.releaseDrawerState = null;
		const defaultDate = this.getDefaultWOSReleaseDate();
		const html = `
			<div class="ia-confirm-summary">
				<div class="ia-panel">
					<div class="ia-panel-head">
						<h4>${__("Release Scope")}</h4>
					</div>
					<div class="row">
						<div class="col-sm-6">
							<label class="control-label">${__("Release Date", null, "Injection APS Execution")}</label>
							<input type="date" class="form-control input-sm" data-wos-release-date value="${injection_aps.ui.escape(defaultDate)}">
						</div>
						<div class="col-sm-6">
							<label class="control-label">${__("Shift Type", null, "Injection APS")}</label>
							<select class="form-control input-sm" data-wos-shift-type>
								<option value="All">${__("Both Shifts")}</option>
								<option value="白班">${__("白班", null, "Injection APS")}</option>
								<option value="晚班">${__("晚班", null, "Injection APS")}</option>
							</select>
						</div>
					</div>
					<div class="ia-toolbar" style="margin-top: 10px;">
						<button type="button" class="btn btn-sm btn-default" data-preview-wos-release>${__("Preview", null, "Injection APS")}</button>
						<button type="button" class="btn btn-sm btn-primary" data-generate-wos-release>${__("Generate Proposal Batch")}</button>
					</div>
				</div>
				<div class="ia-feedback" data-wos-release-feedback></div>
				<div class="ia-card-grid" data-wos-release-summary></div>
				<div data-wos-release-warning></div>
				<div data-wos-release-table></div>
			</div>
		`;
		injection_aps.ui.open_drawer(__("Release WOS"), runName, html);
		const drawer = injection_aps.ui.ensure_drawer();
		drawer.querySelector("[data-preview-wos-release]").addEventListener("click", () => this.previewWOSRelease(drawer));
		drawer.querySelector("[data-generate-wos-release]").addEventListener("click", () => this.generateWOSRelease(drawer));
		this.previewWOSRelease(drawer);
	}

	getWOSReleaseDrawerValues(drawer) {
		return {
			run_name: this.getSelectedRun(),
			release_from_date: drawer.querySelector("[data-wos-release-date]").value,
			release_horizon_days: 0,
			shift_type: drawer.querySelector("[data-wos-shift-type]").value || "All",
		};
	}

	async previewWOSRelease(drawer) {
		const values = this.getWOSReleaseDrawerValues(drawer);
		const feedback = drawer.querySelector("[data-wos-release-feedback]");
		if (!values.run_name || !values.release_from_date) {
			injection_aps.ui.set_feedback(feedback, __("Select a release date first.", null, "Injection APS"), "warning");
			return null;
		}
		const preview = await injection_aps.ui.xcall(
			{
				message: __("Previewing WOS release..."),
				success_message: __("WOS release preview is ready."),
				busy_key: `wos-release-preview:${values.run_name}:${values.release_from_date}:${values.shift_type}`,
				feedback_target: feedback,
				success_feedback: __("WOS release preview is ready."),
			},
			"injection_aps.api.app.preview_shift_schedule_release",
			values
		);
		if (!preview) {
			return null;
		}
		this.releaseDrawerState = { values, preview };
		this.renderWOSReleasePreview(drawer, preview);
		return preview;
	}

	renderWOSReleasePreview(drawer, preview) {
		const summaryTarget = drawer.querySelector("[data-wos-release-summary]");
		const warningTarget = drawer.querySelector("[data-wos-release-warning]");
		const tableTarget = drawer.querySelector("[data-wos-release-table]");
		const actionCounts = preview.action_counts || {};
		injection_aps.ui.render_cards(summaryTarget, [
			{ label: __("Rows"), value: preview.proposal_count || 0 },
			{ label: __("New", null, "Injection APS Execution"), value: actionCounts.New || 0 },
			{ label: __("Update", null, "Injection APS"), value: (actionCounts["Update Existing"] || 0) + (actionCounts["Move Existing"] || 0) },
			{ label: __("Cancel"), value: actionCounts["Cancel Existing"] || 0 },
			{ label: __("Qty"), value: injection_aps.ui.format_number(preview.total_planned_qty || 0) },
			{ label: __("Shift Type", null, "Injection APS"), value: preview.shift_type || "All" },
		]);
		const pending = preview.pending_batches || [];
		warningTarget.innerHTML = pending.length
			? `<div class="ia-alert warning">${__("There are existing un-applied proposal batches for this date/shift.", null, "Injection APS")} ${pending
					.map((row) => injection_aps.ui.doc_link("APS Shift Schedule Proposal Batch", row.name, `${row.name} (${row.matching_count})`))
					.join(" ")}</div>`
			: "";
		injection_aps.ui.render_table(
			tableTarget,
			[
				{ label: __("Action", null, "Injection APS"), fieldname: "action" },
				{ label: __("WOS Date"), fieldname: "posting_date" },
				{ label: __("Shift Type", null, "Injection APS"), fieldname: "shift_type" },
				{ label: __("Work Order", null, "Injection APS"), fieldname: "work_order" },
				{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
				{ label: __("Workstation", null, "Injection APS"), fieldname: "workstation" },
				{ label: __("Qty"), fieldname: "planned_qty", fieldtype: "Float" },
				{ label: __("Existing WOS"), fieldname: "existing_scheduling" },
			],
			preview.preview_rows || [],
			(column, value) => {
				if (column.fieldname === "action") {
					const tone = value === "New" ? "blue" : value === "Cancel Existing" ? "red" : value === "Keep Existing" ? "green" : "orange";
					return injection_aps.ui.pill(injection_aps.ui.translate(value || ""), tone);
				}
				if (column.fieldname === "work_order" && value) {
					return injection_aps.ui.doc_link("Work Order", value);
				}
				if (column.fieldname === "existing_scheduling" && value) {
					return injection_aps.ui.doc_link("Work Order Scheduling", value);
				}
				if (column.fieldname === "planned_qty") {
					return injection_aps.ui.format_number(value || 0);
				}
				return injection_aps.ui.escape(value == null ? "" : String(value));
			},
			{
				show_count: true,
				empty_message: __("No WOS proposal rows are needed for this date and shift."),
			}
		);
		if (preview.truncated) {
			tableTarget.insertAdjacentHTML("beforeend", `<div class="ia-muted" style="margin-top: 6px;">${__("Preview is truncated. Open the generated proposal batch to review all rows.")}</div>`);
		}
	}

	async generateWOSRelease(drawer) {
		const values = this.getWOSReleaseDrawerValues(drawer);
		const current = this.releaseDrawerState;
		const preview =
			current &&
			current.values &&
			current.values.release_from_date === values.release_from_date &&
			current.values.shift_type === values.shift_type
				? current.preview
				: await this.previewWOSRelease(drawer);
		if (!preview) {
			return;
		}
		if (!Number(preview.proposal_count || 0)) {
			frappe.show_alert({ message: __("No WOS proposal rows are needed for this date and shift."), indicator: "orange" });
			return;
		}
		const confirmed = await injection_aps.ui.confirm_action(
			{ action_key: "generate_shift_schedule_proposals", confirm_required: 1 },
			{
				title: __("Confirm Generate WOS Proposal"),
				summary_lines: [
					__("APS Run: {0}").replace("{0}", values.run_name),
					__("Date: {0}").replace("{0}", values.release_from_date),
					__("Shift Type: {0}").replace("{0}", preview.shift_type || "All"),
					__("Rows: {0}").replace("{0}", String(preview.proposal_count || 0)),
					__("The proposal batch will still require manual review before formal WOS apply."),
				],
			}
		);
		if (!confirmed) {
			return;
		}
		const response = await injection_aps.ui.xcall(
			{
				message: __("Generating WOS proposal batch...", null, "Injection APS"),
				success_message: __("WOS proposal batch generated.", null, "Injection APS"),
				busy_key: `wos-release-generate:${values.run_name}:${values.release_from_date}:${values.shift_type}`,
				feedback_target: drawer.querySelector("[data-wos-release-feedback]"),
				success_feedback: __("WOS proposal batch generated.", null, "Injection APS"),
			},
			"injection_aps.api.app.generate_shift_schedule_proposals",
			values
		);
		if (!response) {
			return;
		}
		injection_aps.ui.close_drawer();
		await this.refresh();
		frappe.set_route("Form", "APS Shift Schedule Proposal Batch", response.shift_schedule_proposal_batch);
	}

	renderWorkOrderProposalTable(rows) {
		injection_aps.ui.render_table(
			this.woProposalTable,
			[
				{ label: __("Batch", null, "Injection APS"), fieldname: "name" },
				{ label: __("APS Run", null, "Injection APS"), fieldname: "planning_run" },
				{ label: __("Status", null, "Injection APS"), fieldname: "status" },
				{ label: __("Approval", null, "Injection APS"), fieldname: "approval_state" },
				{ label: __("Rows"), fieldname: "proposal_count" },
				{ label: __("Applied", null, "Injection APS"), fieldname: "applied_count" },
				{ label: __("Actions", null, "Injection APS"), fieldname: "actions_html" },
			],
			rows,
			(column, value, row) => {
				if (column.fieldname === "name") {
					return injection_aps.ui.doc_link("APS Work Order Proposal Batch", value);
				}
				if (column.fieldname === "planning_run" && value) {
					return injection_aps.ui.doc_link("APS Planning Run", value);
				}
				if (["status", "approval_state"].includes(column.fieldname)) {
					const tone = value === "Applied" || value === "Approved" ? "green" : value === "Rejected" ? "red" : "orange";
					return injection_aps.ui.pill(injection_aps.ui.translate(value), tone);
				}
				if (column.fieldname === "actions_html") {
					const canApply = ["Ready For Review", "Partially Reviewed", "Reviewed"].includes(row.status) && Number(row.approved_count || 0) > 0;
					const reviewableCount = Number(row.pending_count || 0) + Number(row.approved_count || 0);
					const canRejectAction = injection_aps.ui.can_run_action("reject_work_order_proposals");
					const canApplyAction = injection_aps.ui.can_run_action("apply_work_order_proposals");
					return `
						<div class="ia-row-actions">
							${injection_aps.ui.icon_button("external-link", __("Open Work Order Proposal Batch"), { "data-open-wo-batch": row.name })}
							${reviewableCount && canRejectAction ? injection_aps.ui.icon_button("close", __("Reject Results"), { "data-reject-wo-batch": row.name, "data-batch-name": row.name, "data-reviewable-count": reviewableCount }) : ""}
							${canApply && canApplyAction ? injection_aps.ui.icon_button("check", __("Apply Results"), { "data-apply-wo-batch": row.name, "data-batch-name": row.name, "data-approved-count": row.approved_count || 0 }) : ""}
						</div>
					`;
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Work Order Proposal Review"),
				export_sheet_name: __("Work Order Proposals"),
				export_file_name: "aps_work_order_proposals",
				export_subtitle: __("Formal work order proposals waiting for manual review."),
			}
		);
		$(this.woProposalTable)
			.find("[data-open-wo-batch]")
			.each((_, node) => {
				node.addEventListener("click", () => frappe.set_route("Form", "APS Work Order Proposal Batch", node.dataset.openWoBatch));
			});
		$(this.woProposalTable)
			.find("[data-reject-wo-batch]")
			.each((_, node) => {
				node.addEventListener("click", async () => {
					const batchName = node.dataset.batchName || node.dataset.rejectWoBatch || "-";
					const reviewableCount = node.dataset.reviewableCount || "0";
					const reason = await injection_aps.ui.prompt_reason({
						title: __("Confirm Reject Work Order Results"),
						primary_action_label: __("Reject Results"),
						summary_lines: [
							__("Batch: {0}").replace("{0}", batchName),
							__("Reviewable Rows: {0}").replace("{0}", String(reviewableCount || 0)),
							__("The selected reviewable rows will be marked Rejected."),
						],
					});
					if (!reason) {
						return;
					}
					const response = await injection_aps.ui.xcall(
						{
							message: __("Rejecting work-order proposal rows..."),
							success_message: __("Work-order proposal rows rejected."),
							busy_key: `release-center-wo-reject:${node.dataset.rejectWoBatch}`,
							feedback_target: this.feedback,
							success_feedback: __("Work order proposals rejected."),
						},
						"injection_aps.api.app.reject_work_order_proposals",
						{ batch_name: node.dataset.rejectWoBatch, reason }
					);
					if (!response) {
						return;
					}
					await this.refresh();
				});
			});
		$(this.woProposalTable)
			.find("[data-apply-wo-batch]")
			.each((_, node) => {
				node.addEventListener("click", async () => {
					const batchName = node.dataset.batchName || node.dataset.applyWoBatch || "-";
					const approvedCount = node.dataset.approvedCount || "0";
					const confirmed = await injection_aps.ui.confirm_action(
						{ action_key: "apply_work_order_proposals", confirm_required: 1 },
						{
							title: __("Confirm Work Order Apply"),
							summary_lines: [
								__("Batch: {0}").replace("{0}", batchName),
								__("Approved: {0}").replace("{0}", String(approvedCount || 0)),
								__("This action will formally create or bind work orders."),
							],
						}
					);
					if (!confirmed) {
						return;
					}
					const response = await injection_aps.ui.xcall(
						{
							message: __("Applying approved work order proposals..."),
							success_message: __("Formal work orders created."),
							busy_key: `release-center-wo-apply:${node.dataset.applyWoBatch}`,
							feedback_target: this.feedback,
							success_feedback: __("Work order proposals applied."),
						},
						"injection_aps.api.app.apply_work_order_proposals",
						{ batch_name: node.dataset.applyWoBatch }
					);
					if (!response) {
						return;
					}
					await this.refresh();
				});
			});
	}

	renderShiftProposalTable(rows) {
		injection_aps.ui.render_table(
			this.shiftProposalTable,
			[
				{ label: __("Batch", null, "Injection APS"), fieldname: "name" },
				{ label: __("APS Run", null, "Injection APS"), fieldname: "planning_run" },
				{ label: __("Status", null, "Injection APS"), fieldname: "status" },
				{ label: __("Approval", null, "Injection APS"), fieldname: "approval_state" },
				{ label: __("WO Proposal Batch"), fieldname: "work_order_proposal_batch" },
				{ label: __("Rows"), fieldname: "proposal_count" },
				{ label: __("Applied", null, "Injection APS"), fieldname: "applied_count" },
				{ label: __("Actions", null, "Injection APS"), fieldname: "actions_html" },
			],
			rows,
			(column, value, row) => {
				if (column.fieldname === "name") {
					return injection_aps.ui.doc_link("APS Shift Schedule Proposal Batch", value);
				}
				if (column.fieldname === "planning_run" && value) {
					return injection_aps.ui.doc_link("APS Planning Run", value);
				}
				if (column.fieldname === "work_order_proposal_batch" && value) {
					return injection_aps.ui.doc_link("APS Work Order Proposal Batch", value);
				}
				if (["status", "approval_state"].includes(column.fieldname)) {
					const tone = value === "Applied" || value === "Approved" ? "green" : value === "Rejected" ? "red" : "orange";
					return injection_aps.ui.pill(injection_aps.ui.translate(value), tone);
				}
				if (column.fieldname === "actions_html") {
					const canApply = ["Ready For Review", "Partially Reviewed", "Reviewed"].includes(row.status) && Number(row.approved_count || 0) > 0;
					const reviewableCount = Number(row.pending_count || 0) + Number(row.approved_count || 0);
					const canRejectAction = injection_aps.ui.can_run_action("reject_shift_schedule_proposals");
					const canApplyAction = injection_aps.ui.can_run_action("apply_shift_schedule_proposals");
					return `
						<div class="ia-row-actions">
							${injection_aps.ui.icon_button("external-link", __("Open Day/Night Proposal Batch"), { "data-open-shift-batch": row.name })}
							${reviewableCount && canRejectAction ? injection_aps.ui.icon_button("close", __("Reject Results"), { "data-reject-shift-batch": row.name, "data-batch-name": row.name, "data-reviewable-count": reviewableCount }) : ""}
							${canApply && canApplyAction ? injection_aps.ui.icon_button("check", __("Apply Results"), { "data-apply-shift-batch": row.name, "data-batch-name": row.name, "data-approved-count": row.approved_count || 0 }) : ""}
						</div>
					`;
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Day/Night Shift Proposal Review"),
				export_sheet_name: __("Day/Night Proposals"),
				export_file_name: "aps_shift_schedule_proposals",
				export_subtitle: __("Day/night shift proposals waiting for manual review."),
			}
		);
		$(this.shiftProposalTable)
			.find("[data-open-shift-batch]")
			.each((_, node) => {
				node.addEventListener("click", () => frappe.set_route("Form", "APS Shift Schedule Proposal Batch", node.dataset.openShiftBatch));
			});
		$(this.shiftProposalTable)
			.find("[data-reject-shift-batch]")
			.each((_, node) => {
				node.addEventListener("click", async () => {
					const batchName = node.dataset.batchName || node.dataset.rejectShiftBatch || "-";
					const reviewableCount = node.dataset.reviewableCount || "0";
					const reason = await injection_aps.ui.prompt_reason({
						title: __("Confirm Reject Day/Night Results"),
						primary_action_label: __("Reject Results"),
						summary_lines: [
							__("Batch: {0}").replace("{0}", batchName),
							__("Reviewable Rows: {0}").replace("{0}", String(reviewableCount || 0)),
							__("The selected reviewable rows will be marked Rejected."),
						],
					});
					if (!reason) {
						return;
					}
					const response = await injection_aps.ui.xcall(
						{
							message: __("Rejecting day/night shift proposal rows..."),
							success_message: __("Day/night shift proposal rows rejected."),
							busy_key: `release-center-shift-reject:${node.dataset.rejectShiftBatch}`,
							feedback_target: this.feedback,
							success_feedback: __("Day/night shift proposals rejected."),
						},
						"injection_aps.api.app.reject_shift_schedule_proposals",
						{ batch_name: node.dataset.rejectShiftBatch, reason }
					);
					if (!response) {
						return;
					}
					await this.refresh();
				});
			});
		$(this.shiftProposalTable)
			.find("[data-apply-shift-batch]")
			.each((_, node) => {
				node.addEventListener("click", async () => {
					const batchName = node.dataset.batchName || node.dataset.applyShiftBatch || "-";
					const approvedCount = node.dataset.approvedCount || "0";
					const confirmed = await injection_aps.ui.confirm_action(
						{ action_key: "apply_shift_schedule_proposals", confirm_required: 1 },
						{
							title: __("Confirm Day/Night Apply"),
							summary_lines: [
								__("Batch: {0}").replace("{0}", batchName),
								__("Approved: {0}").replace("{0}", String(approvedCount || 0)),
								__("This action will formally write day/night scheduling."),
							],
						}
					);
					if (!confirmed) {
						return;
					}
					const response = await injection_aps.ui.xcall(
						{
							message: __("Applying approved day/night shift proposals..."),
							success_message: __("Formal scheduling updated."),
							busy_key: `release-center-shift-apply:${node.dataset.applyShiftBatch}`,
							feedback_target: this.feedback,
							success_feedback: __("Day/night shift proposals applied."),
						},
						"injection_aps.api.app.apply_shift_schedule_proposals",
						{ batch_name: node.dataset.applyShiftBatch }
					);
					if (!response) {
						return;
					}
					await this.refresh();
				});
			});
	}

	renderReleaseTable(rows) {
		injection_aps.ui.render_table(
			this.releaseTable,
			[
				{ label: __("Batch", null, "Injection APS"), fieldname: "name" },
				{ label: __("APS Run", null, "Injection APS"), fieldname: "planning_run" },
				{ label: __("Status", null, "Injection APS"), fieldname: "status" },
				{ label: __("From", null, "Injection APS"), fieldname: "release_from_date" },
				{ label: __("To", null, "Injection APS"), fieldname: "release_to_date" },
				{ label: __("Work Orders", null, "Injection APS"), fieldname: "generated_work_orders" },
				{ label: __("Released WOS", null, "Injection APS"), fieldname: "work_order_schedulings", export_fieldtype: "Data" },
			],
			rows,
			(column, value) => {
				if (column.fieldname === "planning_run" && value) {
					return injection_aps.ui.doc_link("APS Planning Run", value);
				}
				if (column.fieldname === "status") {
					return injection_aps.ui.pill(injection_aps.ui.translate(value), value === "Released" ? "green" : "orange");
				}
				if (column.fieldname === "work_order_schedulings") {
					return this.renderReleasedWOSList(value || []);
				}
				if (["release_from_date", "release_to_date"].includes(column.fieldname)) {
					return injection_aps.ui.format_date(value);
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("APS Formal Apply Log"),
				export_sheet_name: __("Apply Logs"),
				export_file_name: "aps_release_batches",
				export_subtitle: __("Formally applied work order and scheduling logs."),
				export_formatter: (column, value, row) => {
					if (column.fieldname === "work_order_schedulings") {
						return (row.work_order_schedulings || []).map((wos) => wos.display_name || wos.work_order_scheduling || "").join(", ");
					}
					if (["release_from_date", "release_to_date"].includes(column.fieldname)) {
						return injection_aps.ui.format_date(value);
					}
					return value;
				},
			}
		);
	}

	renderReleasedWOSList(rows) {
		const wosRows = (rows || []).filter((row) => row && row.work_order_scheduling);
		if (!wosRows.length) {
			return `<div class="ia-muted">-</div>`;
		}
		const previewCount = 5;
		const renderLinks = (items) =>
			items
			.map((row) => {
				const label = row.display_name || row.work_order_scheduling;
				return injection_aps.ui.doc_link("Work Order Scheduling", row.work_order_scheduling, label);
			})
			.join("");
		if (wosRows.length <= previewCount) {
			return `<div class="ia-list-preview">${renderLinks(wosRows)}</div>`;
		}
		return `
			<div class="ia-list-preview">${renderLinks(wosRows.slice(0, previewCount))}<span class="ia-chip">+${wosRows.length - previewCount}</span></div>
			<details class="ia-list-preview">
				<summary>${__("Expand All", null, "Injection APS")} (${wosRows.length})</summary>
				<div class="ia-list-preview">${renderLinks(wosRows.slice(previewCount))}</div>
			</details>
	`;
	}

	renderExceptionTable(rows) {
		this.exceptionRowsByName = {};
		(rows || []).forEach((row) => {
			if (row && row.name) {
				this.exceptionRowsByName[row.name] = row;
			}
		});
		injection_aps.ui.render_table(
			this.exceptionTable,
			[
				{ label: __("Severity", null, "Injection APS"), fieldname: "severity" },
				{ label: __("Type", null, "Injection APS"), fieldname: "exception_type" },
				{ label: __("Affected Object", null, "Injection APS"), fieldname: "item_code" },
				{ label: __("Diagnosis / Recommendation", null, "Injection APS"), fieldname: "message" },
				{ label: __("Actions", null, "Injection APS"), fieldname: "actions_html" },
			],
			rows,
			(column, value, row) => {
				if (column.fieldname === "severity") {
					const tone = row.is_blocking ? "red" : value === "Critical" ? "orange" : "blue";
					return injection_aps.ui.pill(injection_aps.ui.translate(value), tone);
				}
				if (column.fieldname === "exception_type") {
					return injection_aps.ui.escape(injection_aps.ui.translate(value || ""));
				}
				if (column.fieldname === "item_code") {
					const source = [row.source_doctype, row.source_name].filter(Boolean).join(" / ");
					return `
						<div class="ia-exception-subject">
							${row.item_code ? injection_aps.ui.item_identity(row) : ""}
							<div class="ia-muted">${[row.customer, row.workstation].filter(Boolean).map((item) => injection_aps.ui.escape(item)).join(" · ") || "-"}</div>
							${source ? `<div class="ia-muted">${injection_aps.ui.escape(source)}</div>` : ""}
						</div>
					`;
				}
				if (column.fieldname === "message") {
					const text = row.root_cause_text || value || row.resolution_hint || "";
					const firstAction = this.getExceptionSuggestedActions(row)[0] || "";
					return `
						<div class="ia-exception-diagnosis">
							<div>${injection_aps.ui.escape(injection_aps.ui.translate(text))}</div>
							${firstAction && firstAction !== text ? `<div class="ia-exception-advice">${injection_aps.ui.escape(injection_aps.ui.translate(firstAction))}</div>` : ""}
						</div>
					`;
				}
				if (column.fieldname === "actions_html") {
					return `
						<div class="ia-row-actions">
							${injection_aps.ui.icon_button("external-link", __("Open Source"), { "data-source-route": row.source_route || "", disabled: row.source_route ? null : "disabled" })}
							${injection_aps.ui.icon_button("search", __("Resolution Guidance"), { "data-open-resolution": row.name || "" })}
						</div>
					`;
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("APS Exception Review"),
				export_sheet_name: __("Exceptions", null, "Injection APS"),
				export_file_name: "aps_exceptions",
				export_subtitle: __("Blocking and warning exceptions waiting for manual review."),
				export_columns: [
					{ label: __("Severity", null, "Injection APS"), fieldname: "severity" },
					{ label: __("Type", null, "Injection APS"), fieldname: "exception_type" },
					{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
					{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
					{ label: __("Machine", null, "Injection APS"), fieldname: "workstation" },
					{ label: __("Message", null, "Injection APS"), fieldname: "message" },
					{ label: __("Root Cause", null, "Injection APS"), fieldname: "root_cause_text" },
					{ label: __("Resolution Guidance"), fieldname: "suggested_actions" },
					{ label: __("Source Doctype", null, "Injection APS"), fieldname: "source_doctype" },
					{ label: __("Source Name", null, "Injection APS"), fieldname: "source_name" },
				],
			}
		);

		$(this.exceptionTable)
			.find("[data-source-route]")
			.each((_, node) => {
				node.addEventListener("click", () => {
					const route = node.dataset.sourceRoute;
					if (route && route.startsWith("Form/")) {
						const [, doctype, ...nameParts] = route.split("/");
						frappe.set_route("Form", doctype, nameParts.join("/"));
					} else if (route) {
						injection_aps.ui.go_to(route);
					}
				});
			});

		$(this.exceptionTable)
			.find("[data-open-resolution]")
			.each((_, node) => {
				node.addEventListener("click", () => this.openExceptionResolution(this.exceptionRowsByName[node.dataset.openResolution || ""] || null));
			});
	}

	formatDiagnosticValue(value) {
		if (value == null || value === "") {
			return "";
		}
		if (Array.isArray(value)) {
			return value
				.slice(0, 12)
				.map((entry) => (entry && typeof entry === "object" ? JSON.stringify(entry) : String(entry)))
				.join("; ");
		}
		if (typeof value === "object") {
			return JSON.stringify(value);
		}
		return String(value);
	}

	getDefaultExceptionActions(detail) {
		const searchText = `${detail.exception_type || ""} ${detail.message || ""}`.toLowerCase();
		if (["mold", "mould", "模具"].some((token) => searchText.includes(token))) {
			return [
				__("Check the linked Mold and Mold Product records for status, cycle time, cavity output, and machine-tonnage compatibility."),
				__("Correct the mold assignment or move the segment to a compatible machine, then recalculate and confirm that the exception is cleared."),
			];
		}
		if (["delivery delay", "late delivery", "delivery", "late", "交期", "交付", "延期"].some((token) => searchText.includes(token))) {
			return [
				__("Compare the requested delivery date with the segment end time and the displayed delay minutes."),
				__("If a feasible earlier window exists, move, resize, or split the segment on the Board and run validation again."),
				__("If capacity cannot meet the date, adjust machine capacity or downtime first; otherwise confirm the revised customer delivery date in the source demand and recalculate APS."),
			];
		}
		if (["machine", "workstation", "capacity", "机台", "产能"].some((token) => searchText.includes(token))) {
			return [
				__("Check APS Machine Capability, the machine calendar, and active downtime for the affected workstation."),
				__("Restore capacity or move/resize the affected segment in the Board, then rerun validation."),
			];
		}
		if (["material", "stock", "bom", "warehouse", "原料", "库存"].some((token) => searchText.includes(token))) {
			return [
				__("Verify the BOM, source warehouse, available stock, and expected supply date for the affected item."),
				__("Correct the material or supply data, then recalculate APS and confirm that the shortage no longer blocks the plan."),
			];
		}
		return [
			__("Open the source record and correct the condition described in the exception message."),
			__("Recalculate or rebuild exceptions for this APS run, then confirm that the exception no longer appears before release."),
		];
	}

	getExceptionSuggestedActions(detail) {
		const diagnosticActions = injection_aps.ui.get_value(detail, "diagnostic.suggested_actions", []) || [];
		const actions = diagnosticActions.length
			? diagnosticActions
			: [detail.resolution_hint, ...(detail.suggested_actions || []), ...this.getDefaultExceptionActions(detail)];
		return Array.from(new Set(actions.map((row) => String(row || "").trim()).filter(Boolean)));
	}

	renderExceptionSourceFacts(detail) {
		const snapshot = detail.source_snapshot || {};
		const definitions = [
			["segment_name", __("Segment", null, "Injection APS"), (value) => injection_aps.ui.escape(value)],
			["result_name", __("Schedule Result", null, "Injection APS"), (value) => injection_aps.ui.escape(value)],
			["requested_date", __("Requested Delivery", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_date(value))],
			["start_time", __("Segment Start", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_datetime(value))],
			["end_time", __("Segment End", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_datetime(value))],
			["projected_completion_time", __("Projected Completion", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_datetime(value))],
			["delay_minutes", __("Delay Minutes", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_number(value, 1))],
			["segment_planned_qty", __("Segment Qty", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_number(value))],
			["result_planned_qty", __("Plan Qty", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_number(value))],
			["machine_scheduled_qty", __("Machine Scheduled", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_number(value))],
			["unscheduled_qty", __("Unscheduled", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.format_number(value))],
			["plant_floor", __("Plant Floor", null, "Injection APS"), (value) => injection_aps.ui.escape(value)],
			["mould_reference", __("Mold", null, "Injection APS"), (value) => injection_aps.ui.escape(value)],
			["segment_status", __("Segment Status", null, "Injection APS"), (value) => injection_aps.ui.escape(injection_aps.ui.translate(value))],
		];
		const rows = definitions
			.filter(([fieldname]) => snapshot[fieldname] !== null && snapshot[fieldname] !== undefined && snapshot[fieldname] !== "")
			.map(([fieldname, label, formatter]) => `<div class="ia-kv-row"><div class="ia-kv-key">${injection_aps.ui.escape(label)}</div><div class="ia-kv-value">${formatter(snapshot[fieldname])}</div></div>`)
			.join("");
		return rows ? `<section class="ia-panel"><div class="ia-panel-head"><h4>${__("Operational Facts", null, "Injection APS")}</h4></div><div class="ia-kv">${rows}</div></section>` : "";
	}

	renderExceptionResolution(detail, loadError) {
		const routes = detail.related_routes || {};
		const translatedExceptionType = injection_aps.ui.translate(detail.exception_type || "");
		const translatedMessage = injection_aps.ui.translate(detail.message || "");
		const translatedRootCause = injection_aps.ui.translate(detail.root_cause_text || detail.resolution_hint || detail.message || "-");
		const suggestedActions = this.getExceptionSuggestedActions(detail)
			.map((row) => `<li>${injection_aps.ui.escape(injection_aps.ui.translate(row))}</li>`)
			.join("");
		const candidateMoldList = injection_aps.ui.get_value(detail, "diagnostic.candidate_molds", []) || [];
		const candidateWorkstationList = injection_aps.ui.get_value(detail, "diagnostic.candidate_workstations", []) || [];
		const candidateMolds = this.renderCollapsedChipList(candidateMoldList, { previewCount: 4 });
		const candidateWorkstations = this.renderCollapsedChipList(candidateWorkstationList, { previewCount: 5 });
		const selectedPlantFloorList = injection_aps.ui.get_value(detail, "diagnostic.selected_plant_floors", []) || [];
		const selectedPlantFloors = injection_aps.ui.escape(selectedPlantFloorList.join(", ") || "-");
		const hasResourceScope = Boolean(selectedPlantFloorList.length || candidateMoldList.length || candidateWorkstationList.length);
		const diagnosticKeysToSkip = new Set(["root_cause_codes", "root_cause_text", "suggested_actions", "candidate_molds", "candidate_workstations", "selected_plant_floors"]);
		const diagnosticRows = Object.entries(detail.diagnostic || {})
			.filter(([key, value]) => !diagnosticKeysToSkip.has(key) && value != null && value !== "")
			.slice(0, 12)
			.map(([key, value]) => `<div class="ia-kv-row"><div class="ia-kv-key">${injection_aps.ui.escape(injection_aps.ui.translate(key.replaceAll("_", " ")))}</div><div class="ia-kv-value">${injection_aps.ui.escape(this.formatDiagnosticValue(value))}</div></div>`)
			.join("");
		const sourceLabel = [detail.source_doctype, detail.source_name].filter(Boolean).join(" / ") || "-";
		const rootCauseCodes = (detail.root_cause_codes || []).map((code) => `<span class="ia-chip">${injection_aps.ui.escape(code)}</span>`).join("");
		return {
			title: translatedExceptionType || __("Resolution Guidance"),
			subtitle: detail.name || "",
			html: `
				<div class="ia-page ia-drawer-stack ia-exception-drawer">
					${loadError ? `<div class="ia-alert warning"><strong>${__("Latest details could not be loaded.", null, "Injection APS")}</strong><div>${__("The list summary is shown below. Refresh the page and try again.", null, "Injection APS")}</div></div>` : ""}
					<div class="ia-status-line">
						<div class="ia-status-cell"><span class="ia-status-label">${__("Severity", null, "Injection APS")}</span><div class="ia-status-value">${injection_aps.ui.pill(injection_aps.ui.translate(detail.severity || "-"), detail.is_blocking ? "red" : detail.severity === "Critical" ? "orange" : "blue")}</div></div>
						<div class="ia-status-cell"><span class="ia-status-label">${__("Blocking")}</span><div class="ia-status-value">${detail.is_blocking ? __("Yes", null, "Injection APS") : __("No", null, "Injection APS")}</div></div>
						<div class="ia-status-cell"><span class="ia-status-label">${__("Exception ID", null, "Injection APS")}</span><div class="ia-status-value">${injection_aps.ui.escape(detail.name || "-")}</div></div>
					</div>
					<section class="ia-panel">
						<div class="ia-panel-head"><h4>${__("Affected Context", null, "Injection APS")}</h4></div>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("APS Run", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.escape(detail.planning_run || "-")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Item", null, "Injection APS")}</div><div class="ia-kv-value">${detail.item_code ? injection_aps.ui.item_identity(detail) : "-"}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Customer", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.escape(detail.customer || "-")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Machine", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.escape(detail.workstation || "-")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Source", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.escape(sourceLabel)}</div></div>
						</div>
					</section>
					<section class="ia-panel">
						<div class="ia-panel-head"><h4>${__("Exception Message", null, "Injection APS")}</h4></div>
						<div class="ia-exception-message">${injection_aps.ui.escape(translatedMessage || "-")}</div>
					</section>
					${this.renderExceptionSourceFacts(detail)}
					<div class="${hasResourceScope ? "ia-mini-grid" : ""}">
						<section class="ia-panel">
							<div class="ia-panel-head"><h4>${__("Root Cause", null, "Injection APS")}</h4></div>
							<div class="ia-exception-message">${injection_aps.ui.escape(translatedRootCause)}</div>
							${rootCauseCodes ? `<div class="ia-chip-row ia-exception-code-row">${rootCauseCodes}</div>` : ""}
						</section>
						${hasResourceScope ? `<section class="ia-panel">
							<div class="ia-panel-head"><h4>${__("Resource Scope")}</h4></div>
							<div class="ia-kv">
								<div class="ia-kv-row"><div class="ia-kv-key">${__("Plant Floors")}</div><div class="ia-kv-value">${selectedPlantFloors}</div></div>
								<div class="ia-kv-row"><div class="ia-kv-key">${__("Candidate Molds")}</div><div class="ia-kv-value">${candidateMolds.html}</div></div>
								<div class="ia-kv-row"><div class="ia-kv-key">${__("Candidate Machines")}</div><div class="ia-kv-value">${candidateWorkstations.html}</div></div>
							</div>
						</section>` : ""}
					</div>
					${diagnosticRows ? `<section class="ia-panel"><div class="ia-panel-head"><h4>${__("Diagnostic Details", null, "Injection APS")}</h4></div><div class="ia-kv">${diagnosticRows}</div></section>` : ""}
					<section class="ia-panel ia-resolution-panel">
						<div class="ia-panel-head"><h4>${__("Resolution Guidance")}</h4></div>
						${suggestedActions ? `<ol class="ia-resolution-list">${suggestedActions}</ol>` : `<div class="ia-muted">${__("No explicit resolution guidance is available.")}</div>`}
					</section>
					<div class="ia-toolbar">
						${routes.source ? `<button type="button" class="btn btn-xs btn-default" data-exception-route="${injection_aps.ui.escape(routes.source)}">${__("Open Source")}</button>` : ""}
						${routes.gantt ? `<button type="button" class="btn btn-xs btn-default" data-exception-route="${injection_aps.ui.escape(routes.gantt)}">${__("Board")}</button>` : ""}
						${routes.item ? `<button type="button" class="btn btn-xs btn-default" data-exception-route="${injection_aps.ui.escape(routes.item)}">${__("Open Item")}</button>` : ""}
						${routes.workstation ? `<button type="button" class="btn btn-xs btn-default" data-exception-route="${injection_aps.ui.escape(routes.workstation)}">${__("Open Machine")}</button>` : ""}
					</div>
				</div>
			`,
			bind: () => {
				if (candidateMolds.bind) {
					candidateMolds.bind();
				}
				if (candidateWorkstations.bind) {
					candidateWorkstations.bind();
				}
				const drawer = injection_aps.ui.ensure_drawer();
				drawer.querySelectorAll("[data-exception-route]").forEach((node) => {
					node.addEventListener("click", () => {
						const route = node.dataset.exceptionRoute || "";
						injection_aps.ui.close_drawer();
						if (route.startsWith("Form/")) {
							const [, doctype, ...nameParts] = route.split("/");
							frappe.set_route("Form", doctype, nameParts.join("/"));
						} else if (route) {
							injection_aps.ui.go_to(route);
						}
					});
				});
			},
		};
	}

	async openExceptionResolution(row) {
		if (!row) {
			return;
		}
		const requestKey = `${row.name || "exception"}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
		const initialTitle = injection_aps.ui.translate(row.exception_type || "") || __("Resolution Guidance");
		injection_aps.ui.open_drawer(
			initialTitle,
			row.name || "",
			`<div class="ia-page" data-exception-request="${injection_aps.ui.escape(requestKey)}"><div class="ia-panel"><div class="ia-muted">${__("Loading exception diagnosis and resolution guidance...", null, "Injection APS")}</div></div></div>`
		);
		let detail = {
			name: row.name,
			planning_run: row.planning_run,
			severity: row.severity,
			exception_type: row.exception_type,
			item_code: row.item_code,
			customer_code: row.customer_code,
			item_name: row.item_name,
			customer: row.customer,
			workstation: row.workstation,
			message: row.message,
			resolution_hint: row.resolution_hint,
			is_blocking: row.is_blocking,
			source_doctype: row.source_doctype,
			source_name: row.source_name,
			diagnostic: row.diagnostic || {},
			root_cause_codes: row.root_cause_codes || [],
			root_cause_text: row.root_cause_text,
			suggested_actions: row.suggested_actions || [],
			related_routes: {
				source: row.source_route || "",
				item: row.item_route || "",
				workstation: row.workstation_route || "",
				gantt: row.gantt_route || "",
				execution: row.execution_route || "",
			},
		};
		let loadError = null;
		try {
			const loadedDetail = await injection_aps.ui.xcall(
				{
					message: __("Loading exception details...", null, "Injection APS"),
					busy_key: `exception-resolution:${row.name || "unknown"}`,
					feedback_target: this.feedback,
					success_feedback: __("Exception details loaded.", null, "Injection APS"),
				},
				"injection_aps.api.app.get_exception_resolution_context",
				{ exception_name: row.name }
			);
			if (loadedDetail) {
				detail = loadedDetail;
			}
		} catch (error) {
			console.error(error);
			loadError = error;
		}
		const drawer = injection_aps.ui.ensure_drawer();
		if (!drawer.querySelector(`[data-exception-request="${requestKey}"]`)) {
			return;
		}
		const rendered = this.renderExceptionResolution(detail, loadError);
		drawer.querySelector(".ia-drawer-title").textContent = rendered.title;
		drawer.querySelector(".ia-drawer-subtitle").textContent = rendered.subtitle;
		drawer.querySelector(".ia-drawer-body").innerHTML = rendered.html;
		rendered.bind();
	}

	renderImpact() {
		if (!this.lastImpact) {
			injection_aps.ui.render_cards(this.impactSummary, [
				{ label: __("Insert Order Impact"), value: __("None", null, "Injection APS"), note: __("Use the page-level insert order impact tool when needed. It is no longer mixed into exception handling.") },
			]);
			injection_aps.ui.render_table(this.impactTable, [{ label: __("Message", null, "Injection APS"), fieldname: "message" }], []);
			return;
		}

		injection_aps.ui.render_cards(this.impactSummary, [
				{ label: __("Scheduled Qty"), value: frappe.format(this.lastImpact.scheduled_qty || 0, { fieldtype: "Float" }) },
				{ label: __("Unscheduled Qty"), value: frappe.format(this.lastImpact.unscheduled_qty || 0, { fieldtype: "Float" }) },
			{ label: __("Changeover Minutes"), value: frappe.format(this.lastImpact.changeover_minutes || 0, { fieldtype: "Float" }) },
			{ label: __("Future Batch Hint"), value: this.lastImpact.future_batch_hint || "-" },
		]);
		injection_aps.ui.render_table(
			this.impactTable,
			[
				{ label: __("Lane"), fieldname: "lane_key" },
				{ label: __("Mold", null, "Injection APS"), fieldname: "mould_reference" },
				{ label: __("Machine", null, "Injection APS"), fieldname: "workstation" },
				{ label: __("Qty"), fieldname: "planned_qty" },
				{ label: __("Start", null, "Injection APS"), fieldname: "start_time" },
				{ label: __("End", null, "Injection APS"), fieldname: "end_time" },
			],
			this.lastImpact.parallelization_plan || [],
			(column, value) => {
				if (["start_time", "end_time"].includes(column.fieldname)) {
					return injection_aps.ui.format_datetime(value);
				}
				if (column.fieldname === "planned_qty") {
					return frappe.format(value || 0, { fieldtype: "Float" });
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Insert Order Impact Analysis"),
				export_sheet_name: __("Impact Plan"),
				export_file_name: "aps_insert_order_impact",
				export_subtitle: __("Parallelization plan generated from insert-order analysis."),
			}
		);
	}

	getSelectedRun() {
		const runName = this.runField.get_value();
		if (!runName) {
			frappe.show_alert({ message: __("Select an APS run first."), indicator: "orange" });
			return null;
		}
		return runName;
	}

	async syncExecution() {
		const runName = this.getSelectedRun();
		if (!runName) {
			return;
		}
		const response = await injection_aps.ui.xcall(
			{
				message: __("Syncing execution feedback back to APS..."),
				success_message: __("Execution feedback synced."),
				busy_key: `execution-sync:${runName}`,
				feedback_target: this.feedback,
				success_feedback: __("Execution feedback synced. Refreshing center..."),
			},
			"injection_aps.api.app.sync_execution_feedback_to_aps",
			{ run_name: runName }
		);
		if (!response) {
			return;
		}
		await this.refresh();
	}

	openImpactDialog(prefillItemCode) {
		const dialog = new frappe.ui.Dialog({
			title: __("Insert Order Impact Analysis"),
			fields: [
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company", null, "Injection APS"), reqd: 1, default: frappe.defaults.get_user_default("Company") },
				{
					fieldname: "plant_floor_rows",
					fieldtype: "Table",
					label: __("Selected Plant Floors"),
					reqd: 1,
					in_place_edit: true,
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
				{ fieldname: "item_code", fieldtype: "Link", options: "Item", label: __("Item", null, "Injection APS"), reqd: 1, default: prefillItemCode || undefined },
				{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), reqd: 1 },
				{ fieldname: "required_date", fieldtype: "Date", label: __("Required Date", null, "Injection APS"), reqd: 1 },
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer", null, "Injection APS") },
			],
			primary_action_label: __("Analyze"),
			primary_action: async (values) => {
				const plantFloors = [];
				(values.plant_floor_rows || []).forEach((row) => {
					const value = row && row.plant_floor ? String(row.plant_floor).trim() : "";
					if (value && !plantFloors.includes(value)) {
						plantFloors.push(value);
					}
				});
				if (!plantFloors.length) {
					frappe.msgprint(__("Select at least one Plant Floor before running insert order impact analysis."));
					return;
				}
				this.lastImpact = await injection_aps.ui.xcall(
					{
						message: __("Analyzing insert order impact..."),
						success_message: __("Insert order impact analysis completed."),
						busy_key: `impact-analysis:${values.company || "all"}:${values.item_code || "item"}`,
						feedback_target: this.feedback,
						success_feedback: __("Insert order impact analysis completed."),
					},
					"injection_aps.api.app.analyze_insert_order_impact",
					{
						company: values.company,
						plant_floor: plantFloors[0],
						plant_floors: plantFloors,
						item_code: values.item_code,
						qty: values.qty,
						required_date: values.required_date,
						customer: values.customer,
					}
				);
				if (!this.lastImpact) {
					return;
				}
				dialog.hide();
				this.renderImpact();
			},
		});
		dialog.show();
	}
}
