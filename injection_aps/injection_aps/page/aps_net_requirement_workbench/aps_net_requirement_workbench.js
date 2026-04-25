frappe.pages["aps-net-requirement-workbench"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSNetRequirementWorkbench(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-net-requirement-workbench"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSNetRequirementWorkbench {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.suppressFilterRefresh = false;
		this.tableFilterState = {
			search_text: "",
		};
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Net Requirement Workbench"),
			single_column: true,
		});
		this.companyField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "company",
			options: "Company",
			label: __("Company"),
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.refreshFromFilter(),
		});
		this.customerField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "customer",
			options: "Customer",
			label: __("Customer"),
			change: () => this.refreshFromFilter(),
		});
		this.itemField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "item_code",
			options: "Item",
			label: __("Item"),
			change: () => this.refreshFromFilter(),
		});
		this.plantFloorField = this.page.add_field({
			fieldtype: "Link",
			fieldname: "plant_floor",
			options: "Plant Floor",
			label: __("Plant Floor"),
		});
		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Net Requirement Workbench")}</h3>
					<p>${__("This page concentrates the demand pool, stock, WIP, safety stock, and minimum batch uplift results. Rebuild first, then push the filtered context directly into recalculation.")}</p>
				</div>
				<div class="ia-status-host"></div>
				<div class="ia-action-host"></div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-feedback"></div>
				<div class="ia-panel">
					<div class="ia-table-target"></div>
				</div>
			</div>
		`);
		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.actionHost = this.page.main.find(".ia-action-host")[0];
		this.table = this.page.main.find(".ia-table-target")[0];
		this.rows = [];
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		injection_aps.ui.set_feedback(this.feedback, __("Loading net requirements..."));
		try {
			const filters = this.getFilters();
			const data = await frappe.xcall("injection_aps.api.app.get_net_requirement_page_data", filters);
			injection_aps.ui.render_status_line(this.statusHost, {
				current_step: __("Net Requirements Ready"),
				next_step: __("Recalculate"),
				blocking_reason: "",
			});
			injection_aps.ui.render_actions(this.actionHost, [
				{ label: __("Rebuild Demand"), action_key: "rebuild", enabled: 1 },
				{ label: __("Recalculate"), action_key: "trial", enabled: 1 },
				{ label: __("Recalc Console"), action_key: "run_console", enabled: 1, route: "aps-run-console" },
			], async (action) => {
				if (action.action_key === "rebuild") {
					await this.rebuildDemandPool();
					return;
				}
				if (action.action_key === "trial") {
					await this.createTrialRun();
					return;
				}
				await injection_aps.ui.run_action(action);
			});
			injection_aps.ui.render_cards(this.summary, [
				{ label: __("Rows"), value: data.summary.rows || 0 },
				{ label: __("Net Qty"), value: injection_aps.ui.format_number(data.summary.net_requirement_qty || 0) },
				{ label: __("Planning Qty"), value: injection_aps.ui.format_number(data.summary.planning_qty || 0), note: __("Includes minimum batch uplift") },
			]);
			const rows = (data.rows || []).map((row, index) => Object.assign({ _row_no: index + 1 }, row));
			this.rows = rows;
			injection_aps.ui.render_table(
				this.table,
				[
					{ label: __("No."), fieldname: "_row_no", fieldtype: "Int", className: "ia-col-seq" },
					{ label: __("Item"), fieldname: "item_code" },
					{ label: __("Customer"), fieldname: "customer" },
					{ label: __("Demand Date"), fieldname: "demand_date" },
					{ label: __("Demand"), fieldname: "demand_qty" },
					{ label: __("Stock"), fieldname: "available_stock_qty" },
					{ label: __("Open WO"), fieldname: "open_work_order_qty" },
					{ label: __("Safety Gap"), fieldname: "safety_stock_gap_qty" },
					{ label: __("Min Batch"), fieldname: "minimum_batch_qty" },
					{ label: __("Planning Qty"), fieldname: "planning_qty" },
					{ label: __("Net Qty"), fieldname: "net_requirement_qty" },
					{ label: __("Reason"), fieldname: "reason_text" },
				],
				rows,
				(column, value, row) => {
					if (column.fieldname === "_row_no") {
						return injection_aps.ui.escape(String(value || ""));
					}
					if (column.fieldname === "item_code") {
						return injection_aps.ui.route_link(value, `item/${encodeURIComponent(value)}`);
					}
					if (["demand_qty", "available_stock_qty", "open_work_order_qty", "safety_stock_gap_qty", "minimum_batch_qty", "planning_qty", "net_requirement_qty"].includes(column.fieldname)) {
						return injection_aps.ui.escape(injection_aps.ui.format_number(value || 0));
					}
					if (column.fieldname === "demand_date") {
						return injection_aps.ui.format_date(value);
					}
					if (column.fieldname === "reason_text") {
						const translatedValue = injection_aps.ui.translate(value || "");
						return `<span title="${injection_aps.ui.escape(translatedValue)}">${injection_aps.ui.escape(translatedValue)}</span>`;
					}
					return injection_aps.ui.escape(value);
				},
				{
					exportable: true,
					export_title: __("APS Net Requirement Workbench"),
					export_sheet_name: __("Net Requirements"),
					export_file_name: "aps_net_requirements",
					export_subtitle: __("Net requirement rows for manual planning analysis."),
					toolbar_html: this.getTableFilterHtml(),
					row_context_menu: (row) => this.getRowContextMenu(row),
					after_render: (target) => this.bindTableFilters(target),
				}
			);
			injection_aps.ui.set_feedback(this.feedback, __("Net requirement workbench refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load net requirements."), "error");
		}
	}

	refreshFromFilter() {
		if (!this.suppressFilterRefresh) {
			this.refresh();
		}
	}

	getFilters() {
		return {
			company: this.companyField.get_value() || undefined,
			item_code: this.itemField.get_value() || undefined,
			customer: this.customerField.get_value() || undefined,
			search_text: this.tableFilterState.search_text || undefined,
		};
	}

	clearFilters() {
		this.suppressFilterRefresh = true;
		this.customerField.set_value("");
		this.itemField.set_value("");
		this.tableFilterState = {
			search_text: "",
		};
		this.suppressFilterRefresh = false;
		this.refresh();
	}

	getTableFilterHtml() {
		const state = this.tableFilterState || {};
		return `
			<form class="ia-table-search-strip" data-ia-net-search="1">
				<label class="ia-table-search-field">
					<input
						type="search"
						class="form-control input-sm ia-table-search-input"
						value="${injection_aps.ui.escape(state.search_text || "")}"
						placeholder="${injection_aps.ui.escape(__("Search Item Code"))}"
						data-ia-net-search-input="1"
					>
				</label>
				<button type="submit" class="ia-table-search-button">${__("Find")}</button>
			</form>
		`;
	}

	bindTableFilters(target) {
		const searchForm = target.querySelector("[data-ia-net-search='1']");
		if (!searchForm) {
			return;
		}
		searchForm.addEventListener("submit", (event) => {
			event.preventDefault();
			const input = searchForm.querySelector("[data-ia-net-search-input='1']");
			this.tableFilterState.search_text = (input && input.value ? input.value : "").trim();
			this.refresh();
		});
	}

	getRowContextMenu(row) {
		if (!row || !row.name) {
			return [];
		}
		const items = [
			{
				label: __("View Detail"),
				icon: "external-link",
				handler: () => injection_aps.ui.go_to(`Form/APS Net Requirement/${encodeURIComponent(row.name)}`),
			},
		];
		if (injection_aps.ui.can_run_action("edit_net_requirement")) {
			items.push({
				label: __("Edit Row"),
				icon: "edit",
				handler: () => this.editRow(row),
			});
		}
		if (injection_aps.ui.can_run_action("delete_net_requirement")) {
			items.push({
				label: __("Delete Row"),
				icon: "trash-2",
				handler: () => this.deleteRow(row),
			});
		}
		return items;
	}

	editRow(row) {
		const dialog = new frappe.ui.Dialog({
			title: __("Edit Row"),
			fields: [
				{ fieldname: "demand_date", fieldtype: "Date", label: __("Demand Date"), default: row.demand_date },
				{ fieldname: "demand_qty", fieldtype: "Float", label: __("Demand"), default: row.demand_qty },
				{ fieldname: "available_stock_qty", fieldtype: "Float", label: __("Stock"), default: row.available_stock_qty },
				{ fieldname: "open_work_order_qty", fieldtype: "Float", label: __("Open WO"), default: row.open_work_order_qty },
				{ fieldname: "safety_stock_gap_qty", fieldtype: "Float", label: __("Safety Gap"), default: row.safety_stock_gap_qty },
				{ fieldname: "minimum_batch_qty", fieldtype: "Float", label: __("Min Batch"), default: row.minimum_batch_qty },
				{ fieldname: "net_requirement_qty", fieldtype: "Float", label: __("Net Qty"), default: row.net_requirement_qty },
				{ fieldname: "planning_qty", fieldtype: "Float", label: __("Planning Qty"), default: row.planning_qty },
				{ fieldname: "reason_text", fieldtype: "Small Text", label: __("Reason"), default: row.reason_text || "" },
			],
			primary_action_label: __("Save Changes"),
			primary_action: async (values) => {
				const response = await injection_aps.ui.xcall(
					{
						message: __("Saving row..."),
						success_message: __("Row updated."),
						busy_key: `net-row-edit:${row.name}`,
						feedback_target: this.feedback,
						success_feedback: __("Row updated."),
					},
					"injection_aps.api.app.update_net_requirement_row",
					{
						name: row.name,
						values,
					}
				);
				if (!response) {
					return;
				}
				dialog.hide();
				await this.refresh();
			},
		});
		dialog.show();
	}

	async deleteRow(row) {
		const confirmed = await injection_aps.ui.confirm_action(
			{ action_key: "delete_net_requirement", confirm_required: 1 },
			{
				title: __("Confirm Delete"),
				summary_lines: [
					__("Row: {0}").replace("{0}", row.name || "-"),
					__("Item: {0}").replace("{0}", row.item_code || "-"),
					__("Demand Date: {0}").replace("{0}", injection_aps.ui.format_date(row.demand_date) || "-"),
				],
			}
		);
		if (!confirmed) {
			return;
		}
		const response = await injection_aps.ui.xcall(
			{
				message: __("Deleting row..."),
				success_message: __("Row deleted."),
				busy_key: `net-row-delete:${row.name}`,
				feedback_target: this.feedback,
				success_feedback: __("Row deleted."),
			},
			"injection_aps.api.app.delete_net_requirement_row",
			{ name: row.name }
		);
		if (response) {
			await this.refresh();
		}
	}

	async rebuildDemandPool() {
		const filters = this.getFilters();
		const confirmed = await injection_aps.ui.confirm_action(
			{ action_key: "rebuild_demand_pool", confirm_required: 1 },
			{
				title: __("Confirm Demand Rebuild"),
				summary_lines: [
					__("Company: {0}").replace("{0}", filters.company || "-"),
					__("Customer: {0}").replace("{0}", filters.customer || __("All")),
					__("Item: {0}").replace("{0}", filters.item_code || __("All")),
					__("This action will rebuild the demand pool and recalculate net requirements."),
				],
			}
		);
		if (!confirmed) {
			return;
		}
		const result = await injection_aps.ui.xcall(
			{
				message: __("Rebuilding demand and net requirements..."),
				success_message: __("Demand and net requirements were rebuilt."),
				busy_key: `net-rebuild:${filters.company || "all"}`,
				feedback_target: this.feedback,
				success_feedback: __("Demand and net requirements were rebuilt."),
			},
			"injection_aps.api.app.promote_schedule_import_to_net_requirement",
			{
				company: filters.company,
			}
		);
		if (!result) {
			return;
		}
		injection_aps.ui.show_warnings(result.demand_pool, __("Demand Pool Warnings"), "warning_count");
		injection_aps.ui.show_warnings(result.net_requirement, __("Net Requirement Warnings"), "warning_count");
		await this.refresh();
	}

	async createTrialRun() {
		const dialog = new frappe.ui.Dialog({
			title: __("Create Recalc Run"),
			fields: [
				{
					fieldname: "plant_floor_rows",
					fieldtype: "Table",
					label: __("Selected Plant Floors"),
					reqd: 1,
					in_place_edit: true,
					data: this.plantFloorField.get_value() ? [{ plant_floor: this.plantFloorField.get_value() }] : [],
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
				const plantFloors = [];
				(values.plant_floor_rows || []).forEach((row) => {
					const value = row && row.plant_floor ? String(row.plant_floor).trim() : "";
					if (value && !plantFloors.includes(value)) {
						plantFloors.push(value);
					}
				});
				if (!plantFloors.length) {
					frappe.msgprint(__("Select at least one Plant Floor before APS planning."));
					return;
				}
				const confirmed = await injection_aps.ui.confirm_action(
					{ action_key: "run_trial", confirm_required: 1 },
					{
						title: __("Confirm Recalculate"),
						summary_lines: [
							__("Company: {0}").replace("{0}", this.companyField.get_value() || "-"),
							__("Plant Floors: {0}").replace("{0}", plantFloors.join(", ") || "-"),
							__("Customer: {0}").replace("{0}", this.customerField.get_value() || __("All")),
							__("Item: {0}").replace("{0}", this.itemField.get_value() || __("All")),
							__("Horizon: {0} days").replace("{0}", String(values.horizon_days || 14)),
						],
					}
				);
				if (!confirmed) {
					return;
				}
				const response = await injection_aps.ui.xcall(
					{
						message: __("Creating recalculation..."),
						success_message: __("Recalculation created."),
						busy_key: `net-trial:${this.companyField.get_value() || "all"}:${plantFloors.join("|") || "all"}`,
						feedback_target: this.feedback,
						success_feedback: __("Recalculation created. Redirecting to the APS run..."),
					},
					"injection_aps.api.app.create_trial_run_from_net_requirement_context",
					{
						company: this.companyField.get_value() || undefined,
						plant_floor: plantFloors[0],
						plant_floors: plantFloors,
						item_code: this.itemField.get_value() || undefined,
						customer: this.customerField.get_value() || undefined,
						horizon_days: values.horizon_days || undefined,
					}
				);
				if (!response) {
					return;
				}
				injection_aps.ui.show_warnings(response, __("Planning Precheck Warnings"), "preflight_warning_count");
				dialog.hide();
				injection_aps.ui.go_to(`aps-planning-run/${encodeURIComponent(response.run)}`);
			},
		});
		dialog.show();
	}
}
