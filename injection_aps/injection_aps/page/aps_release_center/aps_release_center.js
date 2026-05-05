frappe.pages["aps-release-center"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
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
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Execution"),
			single_column: true,
		});
		this.runField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "run_name",
			options: "APS Planning Run",
			label: __("APS Run"),
			default: injection_aps.ui.get_query_param("run_name") || undefined,
			change: () => this.refresh(),
		});
		if (injection_aps.ui.can_run_action("sync_execution")) {
			this.page.set_primary_action(__("Sync"), () => this.syncExecution());
		}
		this.page.set_secondary_action(__("Insert Order Impact"), () => this.openImpactDialog());

		this.page.main.html(`
				<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Execution")}</h3>
					<p>${__("This page handles proposal review, formal apply, execution feedback, and exception handling after an APS run. Select an APS run first before working here.")}</p>
				</div>
				<div class="ia-empty-state-host"></div>
				<div class="ia-run-body">
					<div class="ia-run-context-host"></div>
				<div class="ia-status-host"></div>
				<div class="ia-action-host"></div>
				<div class="ia-card-grid ia-summary"></div>
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
				<div class="ia-grid-2">
					<div class="ia-panel">
						<h4>${__("Formal Apply Logs")}</h4>
						<div class="ia-release-table" style="margin-top: 8px;"></div>
					</div>
					<div class="ia-panel">
						<h4>${__("Open Exceptions")}</h4>
						<div class="ia-exception-table" style="margin-top: 8px;"></div>
					</div>
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
			${rows.length > settings.previewCount ? `<button type="button" class="ia-inline-toggle" id="${toggleId}">${__("Expand All")} (+${rows.length - settings.previewCount})</button>` : ""}
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
				toggleNode.textContent = collapsed ? __("Collapse") : `${__("Expand All")} (+${rows.length - settings.previewCount})`;
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
					["generate_work_order_proposals", "generate_shift_schedule_proposals", "open_gantt"].includes(row.action_key)
				),
				async (action) => {
					const response = await injection_aps.ui.run_action(action);
					injection_aps.ui.show_warnings(response, __("APS Warnings"), "preflight_warning_count");
					await this.refresh();
				}
			);

			const executionHealth = data.execution_health || {};
			const exceptions = data.exceptions || [];
			const blocking = exceptions.filter((row) => Number(row.is_blocking || 0)).length;
			injection_aps.ui.render_cards(this.summary, [
				{ label: __("WO Proposal Batches"), value: (data.work_order_proposal_batches || []).length },
				{ label: __("Day/Night Proposal Batches"), value: (data.shift_schedule_proposal_batches || []).length },
				{ label: __("Delayed", null, "Injection APS"), value: executionHealth.delayed_segments || 0 },
				{ label: __("No Update"), value: executionHealth.no_recent_update_segments || 0 },
				{ label: __("Today Manufacture"), value: executionHealth.today_completed_entries || 0 },
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

	renderWorkOrderProposalTable(rows) {
		injection_aps.ui.render_table(
			this.woProposalTable,
			[
				{ label: __("Batch"), fieldname: "name" },
				{ label: __("APS Run"), fieldname: "planning_run" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("Approval"), fieldname: "approval_state" },
				{ label: __("Rows"), fieldname: "proposal_count" },
				{ label: __("Applied", null, "Injection APS"), fieldname: "applied_count" },
				{ label: __("Actions"), fieldname: "actions_html" },
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
				{ label: __("Batch"), fieldname: "name" },
				{ label: __("APS Run"), fieldname: "planning_run" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("Approval"), fieldname: "approval_state" },
				{ label: __("WO Proposal Batch"), fieldname: "work_order_proposal_batch" },
				{ label: __("Rows"), fieldname: "proposal_count" },
				{ label: __("Applied", null, "Injection APS"), fieldname: "applied_count" },
				{ label: __("Actions"), fieldname: "actions_html" },
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
				{ label: __("Batch"), fieldname: "name" },
				{ label: __("APS Run"), fieldname: "planning_run" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("From", null, "Injection APS"), fieldname: "release_from_date" },
				{ label: __("To", null, "Injection APS"), fieldname: "release_to_date" },
				{ label: __("Work Orders", null, "Injection APS"), fieldname: "generated_work_orders" },
				{ label: __("Scheduling", null, "Injection APS"), fieldname: "work_order_scheduling" },
			],
			rows,
			(column, value) => {
				if (column.fieldname === "planning_run" && value) {
					return injection_aps.ui.doc_link("APS Planning Run", value);
				}
				if (column.fieldname === "status") {
					return injection_aps.ui.pill(injection_aps.ui.translate(value), value === "Released" ? "green" : "orange");
				}
				if (column.fieldname === "work_order_scheduling" && value) {
					return injection_aps.ui.doc_link("Work Order Scheduling", value);
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
			}
		);
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
				{ label: __("Severity"), fieldname: "severity" },
				{ label: __("Type"), fieldname: "exception_type" },
				{ label: __("Item"), fieldname: "item_code" },
				{ label: __("Machine", null, "Injection APS"), fieldname: "workstation" },
				{ label: __("Message", null, "Injection APS"), fieldname: "message" },
				{ label: __("Actions"), fieldname: "actions_html" },
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
				if (column.fieldname === "message") {
					const text = row.root_cause_text || row.resolution_hint || value || "";
					return injection_aps.ui.escape(injection_aps.ui.translate(text));
				}
				if (column.fieldname === "actions_html") {
					return `
						<div class="ia-row-actions">
							${injection_aps.ui.icon_button("external-link", __("Open Source"), { "data-open-source": row.source_name || "", "data-source-doctype": row.source_doctype || "" })}
							${injection_aps.ui.icon_button("search", __("Resolution Guidance"), { "data-open-resolution": row.name || "" })}
						</div>
					`;
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("APS Exception Review"),
				export_sheet_name: __("Exceptions"),
				export_file_name: "aps_exceptions",
				export_subtitle: __("Blocking and warning exceptions waiting for manual review."),
			}
		);

		$(this.exceptionTable)
			.find("[data-open-source]")
			.each((_, node) => {
				node.addEventListener("click", () => {
					const doctype = node.dataset.sourceDoctype;
					const name = node.dataset.openSource;
					if (doctype && name) {
						frappe.set_route("Form", doctype, name);
					}
				});
			});

		$(this.exceptionTable)
			.find("[data-open-resolution]")
			.each((_, node) => {
				node.addEventListener("click", () => this.openExceptionResolution(this.exceptionRowsByName[node.dataset.openResolution || ""] || null));
			});
	}

	async openExceptionResolution(row) {
		if (!row) {
			return;
		}
		let detail = {
			name: row.name,
			planning_run: row.planning_run,
			severity: row.severity,
			exception_type: row.exception_type,
			item_code: row.item_code,
			customer: row.customer,
			workstation: row.workstation,
			message: row.message,
			resolution_hint: row.resolution_hint,
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
		const routes = detail.related_routes || {};
		const translatedExceptionType = injection_aps.ui.translate(detail.exception_type || "");
		const translatedMessage = injection_aps.ui.translate(detail.message || "");
		const translatedRootCause = injection_aps.ui.translate(detail.root_cause_text || detail.resolution_hint || detail.message || "-");
		const suggestedActions = (detail.suggested_actions || [])
			.map((row) => `<li>${injection_aps.ui.escape(injection_aps.ui.translate(row))}</li>`)
			.join("");
		const candidateMoldList = injection_aps.ui.get_value(detail, "diagnostic.candidate_molds", []) || [];
		const candidateWorkstationList = injection_aps.ui.get_value(detail, "diagnostic.candidate_workstations", []) || [];
		const candidateMolds = this.renderCollapsedChipList(candidateMoldList, { previewCount: 4 });
		const candidateWorkstations = this.renderCollapsedChipList(candidateWorkstationList, { previewCount: 5 });
		const selectedPlantFloors = injection_aps.ui.escape((injection_aps.ui.get_value(detail, "diagnostic.selected_plant_floors", []) || []).join(", ") || "-");
		const html = `
			<div class="ia-page">
				<div class="ia-status-line">
					<div class="ia-status-cell"><span class="ia-status-label">${__("Severity")}</span><div class="ia-status-value">${injection_aps.ui.escape(detail.severity || "-")}</div></div>
					<div class="ia-status-cell"><span class="ia-status-label">${__("Type")}</span><div class="ia-status-value">${injection_aps.ui.escape(translatedExceptionType || "-")}</div></div>
					<div class="ia-status-cell ia-status-cell-wide"><span class="ia-status-label">${__("Message", null, "Injection APS")}</span><div class="ia-status-value">${injection_aps.ui.escape(translatedMessage || "-")}</div></div>
				</div>
				<div class="ia-mini-grid">
					<div class="ia-panel">
						<h4>${__("Root Cause")}</h4>
						<div class="ia-muted">${injection_aps.ui.escape(translatedRootCause)}</div>
					</div>
					<div class="ia-panel">
						<h4>${__("Resource Scope")}</h4>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Plant Floors")}</div><div class="ia-kv-value">${selectedPlantFloors}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Candidate Molds")}</div><div class="ia-kv-value">${candidateMolds.html}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Candidate Machines")}</div><div class="ia-kv-value">${candidateWorkstations.html}</div></div>
						</div>
					</div>
				</div>
				<div class="ia-panel">
					<h4>${__("Resolution Guidance")}</h4>
					${suggestedActions ? `<ul style="margin:0; padding-left:18px;">${suggestedActions}</ul>` : `<div class="ia-muted">${__("No explicit resolution guidance is available.")}</div>`}
				</div>
				<div class="ia-chip-row">
					${routes.source ? `<a class="btn btn-xs btn-default" href="/app/${injection_aps.ui.escape(routes.source)}">${__("Open Source")}</a>` : ""}
					${routes.gantt ? `<a class="btn btn-xs btn-default" href="/app/${injection_aps.ui.escape(routes.gantt)}">${__("Board")}</a>` : ""}
					${routes.item ? `<a class="btn btn-xs btn-default" href="/app/${injection_aps.ui.escape(routes.item)}">${__("Open Item")}</a>` : ""}
					${routes.workstation ? `<a class="btn btn-xs btn-default" href="/app/${injection_aps.ui.escape(routes.workstation)}">${__("Open Machine")}</a>` : ""}
				</div>
			</div>
		`;
		injection_aps.ui.open_drawer(translatedExceptionType || __("Resolution Guidance"), detail.item_code || detail.name || "", html);
		if (candidateMolds.bind) {
			candidateMolds.bind();
		}
		if (candidateWorkstations.bind) {
			candidateWorkstations.bind();
		}
	}

	renderImpact() {
		if (!this.lastImpact) {
			injection_aps.ui.render_cards(this.impactSummary, [
				{ label: __("Insert Order Impact"), value: __("None"), note: __("Use the page-level insert order impact tool when needed. It is no longer mixed into exception handling.") },
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
				{ label: __("Mold"), fieldname: "mould_reference" },
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
				{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company"), reqd: 1, default: frappe.defaults.get_user_default("Company") },
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
							label: __("Plant Floor"),
							in_list_view: 1,
							reqd: 1,
						},
					],
				},
				{ fieldname: "item_code", fieldtype: "Link", options: "Item", label: __("Item"), reqd: 1, default: prefillItemCode || undefined },
				{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), reqd: 1 },
				{ fieldname: "required_date", fieldtype: "Date", label: __("Required Date"), reqd: 1 },
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer") },
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
