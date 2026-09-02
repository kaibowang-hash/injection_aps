frappe.pages["aps-change-impact-center"].on_page_load = function (wrapper) {
	injection_aps.ui_loader.start("20260902.1", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSChangeImpactCenter(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-change-impact-center"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSChangeImpactCenter {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.selected = new Set();
		this.rows = [];
		this.refreshSequence = 0;
		this.canAnalyze = injection_aps.ui.can_run_action("batch_analyze_change_requests");
		this.canConfirm = injection_aps.ui.can_run_action("confirm_change_request");
		this.canApprove = injection_aps.ui.can_run_action("approve_change_request");
		this.canReject = injection_aps.ui.can_run_action("reject_change_request");
		this.canApply = injection_aps.ui.can_run_action("apply_change_request");
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Change Impact Center", null, "Injection APS"),
			single_column: true,
		});
		this.addFilters();
		this.page.add_inner_button(__("Refresh", null, "Injection APS"), () => this.refresh());
		if (this.canAnalyze) {
			this.page.set_primary_action(
				__("Batch Analyze", null, "Injection APS"),
				() => this.batchAnalyzeSelected()
			);
			this.page.add_inner_button(
				__("New Change Request", null, "Injection APS"),
				() => frappe.new_doc("APS Change Request")
			);
		}

		this.page.main.html(`
			<div class="ia-page ia-change-impact-center">
				<div class="ia-banner">
					<h3>${__("Change Impact Center", null, "Injection APS")}</h3>
					<p>${__("Select draft requests, analyze them together, and review customer, quantity, delay, and execution impacts before entering the approval workflow.", null, "Injection APS")}</p>
				</div>
				<div class="ia-feedback"></div>
				<div class="ia-panel">
					<div class="ia-change-impact-scope ia-muted"></div>
					<div class="ia-change-impact-cards ia-card-grid"></div>
				</div>
				<div class="ia-panel">
					<div class="ia-change-impact-table"></div>
				</div>
			</div>
		`);
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.scopeNote = this.page.main.find(".ia-change-impact-scope")[0];
		this.cards = this.page.main.find(".ia-change-impact-cards")[0];
		this.table = this.page.main.find(".ia-change-impact-table")[0];
	}

	addFilters() {
		const refresh = () => this.refresh();
		this.companyField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "company",
			options: "Company",
			label: __("Company", null, "Injection APS"),
			default: frappe.defaults.get_user_default("Company"),
			change: refresh,
		});
		this.runField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "planning_run",
			options: "APS Planning Run",
			label: __("Planning Run", null, "Injection APS"),
			change: refresh,
		});
		this.statusField = this.page.add_field({
			fieldtype: "Select",
			fieldname: "status",
			label: __("Status", null, "Injection APS"),
			options: "\nDraft\nAnalyzed\nPMC Confirmed\nApproved\nApplied\nRejected\nCancelled",
			context: "Injection APS",
			change: refresh,
		});
		this.customerField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "customer",
			options: "Customer",
			label: __("Customer", null, "Injection APS"),
			change: refresh,
		});
		this.itemField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "item_code",
			options: "Item",
			label: __("Item", null, "Injection APS"),
			change: refresh,
		});
		this.limitField = this.page.add_field({
			fieldtype: "Select",
			fieldname: "limit",
			label: __("Rows", null, "Injection APS"),
			options: "20\n50\n100",
			default: "50",
			change: refresh,
		});
	}

	async refresh() {
		const sequence = ++this.refreshSequence;
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading Change Requests...", null, "Injection APS"));
		try {
			const data = await frappe.xcall("injection_aps.api.app.get_change_impact_center_data", {
				company: this.companyField.get_value() || undefined,
				planning_run: this.runField.get_value() || undefined,
				status: this.statusField.get_value() || undefined,
				customer: this.customerField.get_value() || undefined,
				item_code: this.itemField.get_value() || undefined,
				limit: Number(this.limitField.get_value() || 50),
			});
			if (sequence !== this.refreshSequence) {
				return;
			}
			this.rows = data.rows || [];
			this.serverSummary = data.summary || {};
			this.mayHaveMore = Boolean(data.may_have_more);
			const visibleNames = new Set(this.rows.map((row) => row.name));
			this.selected = new Set(Array.from(this.selected).filter((name) => visibleNames.has(name)));
			this.renderSummary();
			this.renderRows();
			injection_aps.ui.set_feedback(this.feedback, __("Change impact list refreshed.", null, "Injection APS"));
		} catch (error) {
			console.error(error);
			if (sequence === this.refreshSequence) {
				injection_aps.ui.set_feedback(this.feedback, __("Failed to load Change Requests.", null, "Injection APS"), "error");
			}
		}
	}

	isBatchAnalyzable(row) {
		return this.canAnalyze && ["Draft", "Analyzed"].includes(row.status || "Draft");
	}

	getSummaryRows() {
		const selectedRows = this.rows.filter((row) => this.selected.has(row.name));
		return selectedRows.length ? selectedRows : this.rows;
	}

	buildSummary(rows) {
		const customers = new Set();
		(rows || []).forEach((row) => (row.affected_customers || []).forEach((customer) => customers.add(customer)));
		return {
			visible_count: (rows || []).length,
			blocking_count: (rows || []).filter((row) => Number(row.blocking || 0) === 1).length,
			affected_order_count: (rows || []).reduce((total, row) => total + Number(row.affected_order_count || 0), 0),
			affected_customer_count: customers.size,
			delayed_qty: (rows || []).reduce((total, row) => total + Number(row.delayed_qty || 0), 0),
			retained_excess_qty: (rows || []).reduce((total, row) => total + Number(row.retained_excess_qty || 0), 0),
		};
	}

	renderSummary() {
		const rows = this.getSummaryRows();
		const summary = this.selected.size ? this.buildSummary(rows) : Object.assign({}, this.serverSummary || {});
		this.scopeNote.textContent = this.selected.size
			? __("Impact summary for {0} selected request(s).", null, "Injection APS").replace("{0}", String(this.selected.size))
			: this.mayHaveMore
				? __("Impact summary for the visible rows. More matching requests may exist; narrow the filters to review them.", null, "Injection APS")
				: __("Impact summary for all visible requests.", null, "Injection APS");
		injection_aps.ui.render_cards(this.cards, [
			{ label: __("Requests", null, "Injection APS"), value: injection_aps.ui.format_number(summary.visible_count || 0) },
			{ label: __("Selected", null, "Injection APS"), value: injection_aps.ui.format_number(this.selected.size) },
			{ label: __("Blocking", null, "Injection APS"), value: injection_aps.ui.format_number(summary.blocking_count || 0) },
			{ label: __("Affected Orders", null, "Injection APS"), value: injection_aps.ui.format_number(summary.affected_order_count || 0) },
			{ label: __("Affected Customers", null, "Injection APS"), value: injection_aps.ui.format_number(summary.affected_customer_count || 0) },
			{ label: __("Delayed Qty", null, "Injection APS"), value: injection_aps.ui.format_number(summary.delayed_qty || 0) },
			{ label: __("Retained Excess", null, "Injection APS"), value: injection_aps.ui.format_number(summary.retained_excess_qty || 0) },
		]);
	}

	renderRows() {
		const columns = [
			{ label: "", fieldname: "selection" },
			{ label: __("Change Request", null, "Injection APS"), fieldname: "name" },
			{ label: __("Status", null, "Injection APS"), fieldname: "status" },
			{ label: __("Change Type", null, "Injection APS"), fieldname: "change_type" },
			{ label: __("Planning Run", null, "Injection APS"), fieldname: "planning_run" },
			{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
			{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
			{ label: __("Required Date", null, "Injection APS"), fieldname: "required_date" },
			{ label: __("Plan Qty", null, "Injection APS"), fieldname: "target_planned_qty", fieldtype: "Float" },
			{ label: __("Impact", null, "Injection APS"), fieldname: "impact_state" },
			{ label: __("Actions", null, "Injection APS"), fieldname: "actions" },
		];
		const toolbar = this.canAnalyze
			? `<label class="checkbox" style="margin:0;"><input type="checkbox" data-select-analyzable="1"> ${__("Select analyzable rows", null, "Injection APS")}</label>`
			: "";
		injection_aps.ui.render_table(
			this.table,
			columns,
			this.rows,
			(column, value, row) => this.formatCell(column, value, row),
			{
				toolbar_html: toolbar,
				empty_message: __("No Change Requests match the current filters.", null, "Injection APS"),
				after_render: () => this.bindRowActions(),
			}
		);
	}

	formatCell(column, value, row) {
		if (column.fieldname === "selection") {
			if (!this.isBatchAnalyzable(row)) {
				return "";
			}
			return `<input type="checkbox" data-change-select="${injection_aps.ui.escape(row.name)}" ${this.selected.has(row.name) ? "checked" : ""}>`;
		}
		if (column.fieldname === "name") {
			return `<button type="button" class="btn btn-link btn-xs" data-open-change="${injection_aps.ui.escape(row.name)}">${injection_aps.ui.escape(row.name)}</button>`;
		}
		if (column.fieldname === "status") {
			const tone = row.status === "Applied" ? "green" : row.status === "Rejected" || row.status === "Cancelled" ? "red" : row.status === "Draft" ? "blue" : "orange";
			return injection_aps.ui.pill(injection_aps.ui.translate(row.status || "Draft"), tone);
		}
		if (column.fieldname === "required_date") {
			return injection_aps.ui.escape(injection_aps.ui.format_date(row.required_date || row.current_required_date || ""));
		}
		if (column.fieldname === "target_planned_qty") {
			return injection_aps.ui.escape(
				`${injection_aps.ui.format_number(row.current_planned_qty || 0)} → ${injection_aps.ui.format_number(row.target_planned_qty || 0)}`
			);
		}
		if (column.fieldname === "impact_state") {
			if (!row.analysis_fingerprint) {
				return injection_aps.ui.pill(__("Not Analyzed", null, "Injection APS"), "blue");
			}
			if (Number(row.blocking || 0) === 1) {
				return injection_aps.ui.pill(__("Blocked", null, "Injection APS"), "red");
			}
			if (Number(row.retained_excess_qty || 0) > 0) {
				return injection_aps.ui.pill(__("Retained Qty", null, "Injection APS"), "orange");
			}
			return injection_aps.ui.pill(__("Analyzed", null, "Injection APS"), "green");
		}
		if (column.fieldname === "actions") {
			const actions = [
				injection_aps.ui.icon_button("search", __("View Impact", null, "Injection APS"), { "data-view-impact": row.name }),
				injection_aps.ui.icon_button("external-link", __("Open Change Request", null, "Injection APS"), { "data-open-change": row.name }),
			];
			if (this.canAnalyze && ["Draft", "Analyzed"].includes(row.status || "Draft")) {
				actions.push(this.workflowButton("analyze", __("Analyze", null, "Injection APS"), row.name));
			}
			if (this.canConfirm && row.status === "Analyzed" && Number(row.blocking || 0) !== 1) {
				actions.push(this.workflowButton("confirm", __("PMC Confirm", null, "Injection APS"), row.name));
			}
			if (this.canApprove && row.status === "PMC Confirmed") {
				actions.push(this.workflowButton("approve", __("Approve", null, "Injection APS"), row.name));
			}
			if (this.canApply && row.status === "Approved") {
				actions.push(this.workflowButton("apply", __("Apply", null, "Injection APS"), row.name, true));
			}
			if (this.canReject && ["Analyzed", "PMC Confirmed", "Approved"].includes(row.status)) {
				actions.push(this.workflowButton("reject", __("Reject", null, "Injection APS"), row.name));
			}
			return actions.join(" ");
		}
		return injection_aps.ui.escape(injection_aps.ui.translate(value || ""));
	}

	workflowButton(action, label, name, primary = false) {
		return `<button type="button" class="btn ${primary ? "btn-primary" : "btn-default"} btn-xs" data-change-workflow="${injection_aps.ui.escape(action)}" data-change-name="${injection_aps.ui.escape(name)}">${injection_aps.ui.escape(label)}</button>`;
	}

	bindRowActions() {
		this.table.querySelectorAll("[data-change-select]").forEach((node) => {
			node.addEventListener("change", () => {
				if (node.checked) {
					this.selected.add(node.dataset.changeSelect);
				} else {
					this.selected.delete(node.dataset.changeSelect);
				}
				this.renderSummary();
				this.syncSelectAllState();
			});
		});
		const selectAll = this.table.querySelector("[data-select-analyzable='1']");
		if (selectAll) {
			selectAll.addEventListener("change", () => {
				this.rows.filter((row) => this.isBatchAnalyzable(row)).forEach((row) => {
					if (selectAll.checked) {
						this.selected.add(row.name);
					} else {
						this.selected.delete(row.name);
					}
				});
				this.renderRows();
				this.renderSummary();
			});
		}
		this.syncSelectAllState();
		this.table.querySelectorAll("[data-view-impact]").forEach((node) => {
			node.addEventListener("click", () => this.openImpact(node.dataset.viewImpact));
		});
		this.table.querySelectorAll("[data-open-change]").forEach((node) => {
			node.addEventListener("click", () => this.openChangeRequest(node.dataset.openChange));
		});
		this.table.querySelectorAll("[data-change-workflow]").forEach((node) => {
			node.addEventListener("click", () => this.runWorkflowAction(node.dataset.changeWorkflow, node.dataset.changeName));
		});
	}

	async runWorkflowAction(action, name) {
		const row = this.rows.find((entry) => entry.name === name);
		if (!row) {
			return;
		}
		const definitions = {
			analyze: {
				method: "injection_aps.api.app.analyze_change_request_impact",
				title: __("Confirm Impact Analysis", null, "Injection APS"),
				message: __("Analyzing Change Request...", null, "Injection APS"),
				success: __("Change Request analyzed.", null, "Injection APS"),
			},
			confirm: {
				method: "injection_aps.api.app.confirm_change_request",
				title: __("Confirm PMC Review", null, "Injection APS"),
				message: __("Confirming Change Request...", null, "Injection APS"),
				success: __("Change Request confirmed by PMC.", null, "Injection APS"),
			},
			approve: {
				method: "injection_aps.api.app.approve_change_request",
				title: __("Confirm Change Approval", null, "Injection APS"),
				message: __("Approving Change Request...", null, "Injection APS"),
				success: __("Change Request approved.", null, "Injection APS"),
			},
			apply: {
				method: "injection_aps.api.app.apply_change_request",
				title: __("Confirm Change Apply", null, "Injection APS"),
				message: __("Applying Change Request...", null, "Injection APS"),
				success: __("Change Request applied.", null, "Injection APS"),
			},
		};
		if (action === "reject") {
			const reason = await injection_aps.ui.prompt_reason({
				title: __("Confirm Change Rejection", null, "Injection APS"),
				primary_action_label: __("Reject", null, "Injection APS"),
				summary_lines: [name, row.impact_summary || __("No impact summary is available.", null, "Injection APS")],
			});
			if (!reason) {
				return;
			}
			const response = await injection_aps.ui.xcall(
				{
					message: __("Rejecting Change Request...", null, "Injection APS"),
					success_message: __("Change Request rejected.", null, "Injection APS"),
					busy_key: `change-workflow:${name}`,
					feedback_target: this.feedback,
				},
				"injection_aps.api.app.reject_change_request",
				{ change_request: name, reason }
			);
			if (response) {
				await this.refresh();
			}
			return;
		}
		const definition = definitions[action];
		if (!definition) {
			return;
		}
		const confirmed = await injection_aps.ui.confirm_action(
			{ action_key: `${action}_change_request`, confirm_required: 1 },
			{
				title: definition.title,
				summary_lines: [
					name,
					row.impact_summary || __("No impact summary is available.", null, "Injection APS"),
				],
			}
		);
		if (!confirmed) {
			return;
		}
		const response = await injection_aps.ui.xcall(
			{
				message: definition.message,
				success_message: definition.success,
				busy_key: `change-workflow:${name}`,
				feedback_target: this.feedback,
			},
			definition.method,
			{ change_request: name }
		);
		if (response) {
			await this.refresh();
		}
	}

	syncSelectAllState() {
		const selectAll = this.table.querySelector("[data-select-analyzable='1']");
		if (!selectAll) {
			return;
		}
		const analyzable = this.rows.filter((row) => this.isBatchAnalyzable(row));
		const selectedCount = analyzable.filter((row) => this.selected.has(row.name)).length;
		selectAll.checked = Boolean(analyzable.length) && selectedCount === analyzable.length;
		selectAll.indeterminate = selectedCount > 0 && selectedCount < analyzable.length;
	}

	openChangeRequest(name) {
		if (!name) {
			return;
		}
		frappe.set_route("Form", "APS Change Request", name);
	}

	openImpact(name) {
		const row = this.rows.find((entry) => entry.name === name);
		if (!row) {
			return;
		}
		const root = document.createElement("div");
		root.innerHTML = `
			<div class="ia-impact-drawer-cards ia-card-grid"></div>
			<div class="ia-impact-drawer-summary ia-alert" style="margin-top:12px;"></div>
			<div class="ia-impact-drawer-orders" style="margin-top:12px;"></div>
			<div style="margin-top:12px;"><button type="button" class="btn btn-primary btn-sm" data-open-full-change="1">${__("Open Full Change Request", null, "Injection APS")}</button></div>
		`;
		injection_aps.ui.render_cards(root.querySelector(".ia-impact-drawer-cards"), [
			{ label: __("Status", null, "Injection APS"), value: injection_aps.ui.translate(row.status || "Draft") },
			{ label: __("Affected Orders", null, "Injection APS"), value: injection_aps.ui.format_number(row.affected_order_count || 0) },
			{ label: __("Affected Customers", null, "Injection APS"), value: injection_aps.ui.format_number(row.affected_customer_count || 0) },
			{ label: __("Delayed Qty", null, "Injection APS"), value: injection_aps.ui.format_number(row.delayed_qty || 0) },
			{ label: __("Minimum Retained", null, "Injection APS"), value: injection_aps.ui.format_number(row.minimum_retained_qty || 0) },
			{ label: __("Retained Excess", null, "Injection APS"), value: injection_aps.ui.format_number(row.retained_excess_qty || 0) },
		]);
		root.querySelector(".ia-impact-drawer-summary").textContent = row.impact_summary || __("This request has not been analyzed yet.", null, "Injection APS");
		injection_aps.ui.render_table(
			root.querySelector(".ia-impact-drawer-orders"),
			[
				{ label: __("Affected Order", null, "Injection APS"), fieldname: "affected_order" },
				{ label: __("Customer", null, "Injection APS"), fieldname: "customer" },
				{ label: __("Item", null, "Injection APS"), fieldname: "item_code" },
				{ label: __("Due Date", null, "Injection APS"), fieldname: "due_date" },
				{ label: __("New Completion", null, "Injection APS"), fieldname: "new_completion_time" },
				{ label: __("Delayed Qty", null, "Injection APS"), fieldname: "delayed_qty", fieldtype: "Float" },
			],
			row.affected_orders_preview || [],
			(column, value) => {
				if (["due_date", "new_completion_time"].includes(column.fieldname)) {
					return injection_aps.ui.escape(column.fieldname === "due_date" ? injection_aps.ui.format_date(value) : injection_aps.ui.format_datetime(value));
				}
				return injection_aps.ui.escape(value == null ? "" : String(value));
			},
			{
				empty_message: __("No affected order preview is available.", null, "Injection APS"),
			}
		);
		if (Number(row.affected_orders_truncated || 0) > 0) {
			const note = document.createElement("div");
			note.className = "ia-muted";
			note.textContent = __("{0} more affected order(s) are available in the full request.", null, "Injection APS").replace(
				"{0}",
				String(row.affected_orders_truncated)
			);
			root.querySelector(".ia-impact-drawer-orders").appendChild(note);
		}
		injection_aps.ui.open_drawer(
			__("Change Impact", null, "Injection APS"),
			`${row.name} · ${injection_aps.ui.translate(row.change_type || "")}`,
			root.innerHTML
		);
		const drawer = injection_aps.ui.ensure_drawer();
		const openButton = drawer.querySelector("[data-open-full-change='1']");
		if (openButton) {
			openButton.addEventListener("click", () => {
				injection_aps.ui.close_drawer();
				this.openChangeRequest(row.name);
			});
		}
	}

	async batchAnalyzeSelected() {
		const rows = this.rows.filter((row) => this.selected.has(row.name) && this.isBatchAnalyzable(row));
		if (!rows.length) {
			frappe.msgprint(__("Select at least one Draft or Analyzed Change Request.", null, "Injection APS"));
			return;
		}
		if (rows.length > 50) {
			frappe.msgprint(__("Select no more than 50 Change Requests for one batch analysis.", null, "Injection APS"));
			return;
		}
		const confirmed = await injection_aps.ui.confirm_action(
			{ action_key: "batch_analyze_change_requests", confirm_required: 1 },
			{
				title: __("Confirm Batch Impact Analysis", null, "Injection APS"),
				summary_lines: [
					__("Selected requests: {0}", null, "Injection APS").replace("{0}", String(rows.length)),
					__("Only impact analysis will run. No request will be confirmed, approved, or applied.", null, "Injection APS"),
					__("If any request fails, all analyses in this batch will be rolled back.", null, "Injection APS"),
				],
			}
		);
		if (!confirmed) {
			return;
		}
		try {
			const result = await injection_aps.ui.xcall(
				{
					message: __("Analyzing selected Change Requests...", null, "Injection APS"),
					success_message: __("Batch impact analysis completed.", null, "Injection APS"),
					busy_key: "change-impact-batch-analysis",
					feedback_target: this.feedback,
					success_feedback: __("Batch analysis completed. Review the refreshed impact summary.", null, "Injection APS"),
				},
				"injection_aps.api.app.batch_analyze_change_requests",
				{ change_requests: JSON.stringify(rows.map((row) => row.name)) }
			);
			if (!result) {
				return;
			}
			this.selected.clear();
			await this.refresh();
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(
				this.feedback,
				__("Batch analysis failed. No request in this batch was changed.", null, "Injection APS"),
				"error"
			);
		}
	}
}
