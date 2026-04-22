frappe.pages["aps-release-center"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSReleaseCenter(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-release-center"].on_page_show = function (wrapper) {
	wrapper.injection_aps_controller?.refresh();
};

class InjectionAPSReleaseCenter {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.lastImpact = null;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Proposal & Execution Center"),
			single_column: true,
		});
		this.runField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "run_name",
			options: "APS Planning Run",
			label: __("Planning Run"),
			default: new URLSearchParams(window.location.search).get("run_name") || undefined,
			change: () => this.refresh(),
		});
		this.page.set_primary_action(__("Sync Execution Feedback"), () => this.syncExecution());
		this.page.set_secondary_action(__("Insert Order Impact"), () => this.openImpactDialog());

		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Proposal & Execution Center")}</h3>
					<p>${__("APS stays as a planning layer. Formal documents are created only after proposal review: work orders first, then white / night shift scheduling, then execution feedback rolls back into APS.")}</p>
				</div>
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
						<h4>${__("Shift Schedule Proposal Batches")}</h4>
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
					<h4>${__("Latest Impact Analysis")}</h4>
					<div class="ia-impact-summary ia-card-grid" style="margin-top: 8px;"></div>
					<div class="ia-impact-table" style="margin-top: 8px;"></div>
				</div>
			</div>
		`);
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
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading proposal / execution center..."));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_release_center_data", {
				run_name: this.runField.get_value() || undefined,
			});
			this.data = data;
			injection_aps.ui.render_status_line(this.statusHost, data.run_context || {
				current_step: __("Run Not Selected"),
				next_step: __("Choose Run"),
				blocking_reason: "",
			});
			injection_aps.ui.render_actions(
				this.actionHost,
				(data.run_context?.actions || []).filter((row) =>
					["generate_work_order_proposals", "generate_shift_schedule_proposals", "open_release_center", "open_gantt"].includes(row.action_key)
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
				{ label: __("WO Proposal"), value: (data.work_order_proposal_batches || []).length },
				{ label: __("Shift Proposal"), value: (data.shift_schedule_proposal_batches || []).length },
				{ label: __("Delayed"), value: executionHealth.delayed_segments || 0 },
				{ label: __("No Update"), value: executionHealth.no_recent_update_segments || 0 },
				{ label: __("Today Manufacture"), value: executionHealth.today_completed_entries || 0 },
				{ label: __("Blocking"), value: blocking, note: __("Need manual review before formal changes") },
			]);
			this.renderWorkOrderProposalTable(data.work_order_proposal_batches || []);
			this.renderShiftProposalTable(data.shift_schedule_proposal_batches || []);
			this.renderReleaseTable(data.release_batches || []);
			this.renderExceptionTable(exceptions);
			this.renderImpact();
			injection_aps.ui.set_feedback(this.feedback, __("Proposal / execution center refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load proposal / execution center."), "error");
		}
	}

	renderWorkOrderProposalTable(rows) {
		injection_aps.ui.render_table(
			this.woProposalTable,
			[
				{ label: __("Batch"), fieldname: "name" },
				{ label: __("Run"), fieldname: "planning_run" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("Approval"), fieldname: "approval_state" },
				{ label: __("Rows"), fieldname: "proposal_count" },
				{ label: __("Applied"), fieldname: "applied_count" },
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
					return `
						<div class="ia-chip-row">
							<button class="btn btn-xs btn-default" data-open-wo-batch="${injection_aps.ui.escape(row.name)}">${__("Open")}</button>
							<button
								class="btn btn-xs btn-primary"
								data-apply-wo-batch="${injection_aps.ui.escape(row.name)}"
								${["Ready For Review", "Partially Reviewed", "Reviewed"].includes(row.status) ? "" : "disabled"}
							>${__("Apply")}</button>
						</div>
					`;
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Work Order Proposal Review"),
				export_sheet_name: __("WO Proposal"),
				export_file_name: "aps_work_order_proposals",
				export_subtitle: __("Formal work-order suggestions pending manual review."),
			}
		);
		$(this.woProposalTable)
			.find("[data-open-wo-batch]")
			.each((_, node) => {
				node.addEventListener("click", () => frappe.set_route("Form", "APS Work Order Proposal Batch", node.dataset.openWoBatch));
			});
		$(this.woProposalTable)
			.find("[data-apply-wo-batch]")
			.each((_, node) => {
				node.addEventListener("click", async () => {
					const response = await injection_aps.ui.xcall(
						{
							message: __("Applying reviewed work order proposals..."),
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
				{ label: __("Run"), fieldname: "planning_run" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("Approval"), fieldname: "approval_state" },
				{ label: __("WO Batch"), fieldname: "work_order_proposal_batch" },
				{ label: __("Rows"), fieldname: "proposal_count" },
				{ label: __("Applied"), fieldname: "applied_count" },
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
					return `
						<div class="ia-chip-row">
							<button class="btn btn-xs btn-default" data-open-shift-batch="${injection_aps.ui.escape(row.name)}">${__("Open")}</button>
							<button
								class="btn btn-xs btn-primary"
								data-apply-shift-batch="${injection_aps.ui.escape(row.name)}"
								${["Ready For Review", "Partially Reviewed", "Reviewed"].includes(row.status) ? "" : "disabled"}
							>${__("Apply")}</button>
						</div>
					`;
				}
				return injection_aps.ui.escape(value);
			},
			{
				exportable: true,
				export_title: __("Shift Schedule Proposal Review"),
				export_sheet_name: __("Shift Proposal"),
				export_file_name: "aps_shift_schedule_proposals",
				export_subtitle: __("White/night shift scheduling suggestions pending review."),
			}
		);
		$(this.shiftProposalTable)
			.find("[data-open-shift-batch]")
			.each((_, node) => {
				node.addEventListener("click", () => frappe.set_route("Form", "APS Shift Schedule Proposal Batch", node.dataset.openShiftBatch));
			});
		$(this.shiftProposalTable)
			.find("[data-apply-shift-batch]")
			.each((_, node) => {
				node.addEventListener("click", async () => {
					const response = await injection_aps.ui.xcall(
						{
							message: __("Applying reviewed white / night shift proposals..."),
							success_message: __("Formal Work Order Scheduling updated."),
							busy_key: `release-center-shift-apply:${node.dataset.applyShiftBatch}`,
							feedback_target: this.feedback,
							success_feedback: __("Shift schedule proposals applied."),
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
				{ label: __("Run"), fieldname: "planning_run" },
				{ label: __("Status"), fieldname: "status" },
				{ label: __("From"), fieldname: "release_from_date" },
				{ label: __("To"), fieldname: "release_to_date" },
				{ label: __("Work Orders"), fieldname: "generated_work_orders" },
				{ label: __("Scheduling"), fieldname: "work_order_scheduling" },
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
				export_sheet_name: __("Release Log"),
				export_file_name: "aps_release_batches",
				export_subtitle: __("Applied work orders and scheduling downstream logs."),
			}
		);
	}

	renderExceptionTable(rows) {
		injection_aps.ui.render_table(
			this.exceptionTable,
			[
				{ label: __("Severity"), fieldname: "severity" },
				{ label: __("Type"), fieldname: "exception_type" },
				{ label: __("Item"), fieldname: "item_code" },
				{ label: __("Machine"), fieldname: "workstation" },
				{ label: __("Message"), fieldname: "message" },
				{ label: __("Actions"), fieldname: "actions_html" },
			],
			rows,
			(column, value, row) => {
				if (column.fieldname === "severity") {
					const tone = row.is_blocking ? "red" : value === "Critical" ? "orange" : "blue";
					return injection_aps.ui.pill(injection_aps.ui.translate(value), tone);
				}
				if (column.fieldname === "actions_html") {
					return `
						<div class="ia-chip-row">
							<button class="btn btn-xs btn-default" data-open-source="${injection_aps.ui.escape(row.source_name || "")}" data-source-doctype="${injection_aps.ui.escape(row.source_doctype || "")}">${__("Source")}</button>
							<button class="btn btn-xs btn-default" data-impact-item="${injection_aps.ui.escape(row.item_code || "")}">${__("Impact")}</button>
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
				export_subtitle: __("Blocking and warning rows for manual analysis."),
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
			.find("[data-impact-item]")
			.each((_, node) => {
				node.addEventListener("click", () => this.openImpactDialog(node.dataset.impactItem || ""));
			});
	}

	renderImpact() {
		if (!this.lastImpact) {
			injection_aps.ui.render_cards(this.impactSummary, [
				{ label: __("Impact"), value: __("None"), note: __("Run insert-order analysis to see displaced segments, parallelization and family side outputs.") },
			]);
			injection_aps.ui.render_table(this.impactTable, [{ label: __("Info"), fieldname: "message" }], []);
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
				{ label: __("Workstation"), fieldname: "workstation" },
				{ label: __("Qty"), fieldname: "planned_qty" },
				{ label: __("Start"), fieldname: "start_time" },
				{ label: __("End"), fieldname: "end_time" },
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
			frappe.show_alert({ message: __("Choose a planning run first."), indicator: "orange" });
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
				{ fieldname: "plant_floor", fieldtype: "Link", options: "Plant Floor", label: __("Plant Floor"), reqd: 1 },
				{ fieldname: "item_code", fieldtype: "Link", options: "Item", label: __("Item"), reqd: 1, default: prefillItemCode || undefined },
				{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), reqd: 1 },
				{ fieldname: "required_date", fieldtype: "Date", label: __("Required Date"), reqd: 1 },
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer") },
			],
			primary_action_label: __("Analyze"),
			primary_action: async (values) => {
				this.lastImpact = await injection_aps.ui.xcall(
					{
						message: __("Analyzing insert order impact..."),
						success_message: __("Impact analysis completed."),
						busy_key: `impact-analysis:${values.company || "all"}:${values.item_code || "item"}`,
						feedback_target: this.feedback,
						success_feedback: __("Impact analysis completed."),
					},
					"injection_aps.api.app.analyze_insert_order_impact",
					values
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
