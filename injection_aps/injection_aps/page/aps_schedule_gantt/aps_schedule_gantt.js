frappe.pages["aps-schedule-gantt"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSScheduleGantt(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-schedule-gantt"].on_page_show = function (wrapper) {
	wrapper.injection_aps_controller?.refresh();
};

class InjectionAPSScheduleGantt {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.draggingSegment = null;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Machine Schedule Gantt"),
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
		this.viewField = this.page.add_field({
			fieldtype: "Select",
			fieldname: "view_mode",
			label: __("View"),
			options: ["Machine", "Mold", "Risk", "Locked"].join("\n"),
			default: "Machine",
			change: () => this.refresh(),
		});
		this.page.set_primary_action(__("Refresh Gantt"), () => this.refresh());

		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-banner">
					<h3>${__("Machine / Mold Schedule Board")}</h3>
					<p>${__("Drag a segment to another qualified machine lane or in front of another segment. APS will preview the move against tonnage, FDA, mold usage, family co-production and frozen constraints before saving.")}</p>
				</div>
				<div class="ia-status-host"></div>
				<div class="ia-card-grid ia-summary"></div>
				<div class="ia-feedback"></div>
				<div class="ia-chip-row ia-legend">
					<span class="ia-chip">${__("Blue")}: ${__("Normal")}</span>
					<span class="ia-chip">${__("Yellow")}: ${__("Attention")}</span>
					<span class="ia-chip">${__("Red")}: ${__("Critical / Blocked")}</span>
					<span class="ia-chip">${__("B")}: ${__("Copy Mold Parallel")}</span>
					<span class="ia-chip">${__("F")}: ${__("Family Mold")}</span>
					<span class="ia-chip">${__("L")}: ${__("Locked")}</span>
					<span class="ia-chip red">${__("FDA")}: ${__("Risk / Override")}</span>
				</div>
				<div class="ia-risk-board"></div>
				<div class="ia-gantt-shell">
					<div class="ia-gantt-timeline"></div>
					<div class="ia-gantt-grid"></div>
				</div>
			</div>
		`);
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.riskBoard = this.page.main.find(".ia-risk-board")[0];
		this.timeline = this.page.main.find(".ia-gantt-timeline")[0];
		this.grid = this.page.main.find(".ia-gantt-grid")[0];
	}

	makeExportId(prefix) {
		return `ia-export-${prefix}-${Math.random().toString(36).slice(2, 8)}`;
	}

	exportRows(title, fileName, columns, rows, subtitle) {
		injection_aps.ui.export_rows_to_excel({
			title,
			sheet_name: title,
			file_name: fileName,
			subtitle,
			columns,
			rows,
		});
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		const runName = this.runField.get_value();
		if (!runName) {
			injection_aps.ui.render_status_line(this.statusHost, {
				current_step: __("Run Not Selected"),
				next_step: __("Choose Planning Run"),
				blocking_reason: "",
			});
			injection_aps.ui.render_cards(this.summary, [
				{ label: __("Planning Run"), value: __("Not Selected"), note: __("Choose a run first.") },
			]);
			this.grid.innerHTML = `<div class="ia-muted">${__("Choose a planning run to load the schedule board.")}</div>`;
			return;
		}

		injection_aps.ui.set_feedback(this.feedback, __("Loading Gantt data..."));
		try {
			this.data = await frappe.xcall("injection_aps.api.app.get_schedule_gantt_data", {
				run_name: runName,
			});
			injection_aps.ui.render_status_line(this.statusHost, this.data.run || null);
			this.renderBlockedResults(this.data.blocked_results || []);
			this.renderGantt(this.data.tasks || []);
			injection_aps.ui.set_feedback(this.feedback, __("Gantt refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load Gantt data."), "error");
		}
	}

	renderBlockedResults(rows) {
		if (!rows.length) {
			this.riskBoard.innerHTML = "";
			return;
		}
		const exportId = this.makeExportId("risk");
		this.riskBoard.innerHTML = `
			<div class="ia-panel ia-risk-panel">
				<div class="ia-risk-head">
					<div class="ia-risk-head-main">
						<div class="ia-risk-title">${__("Blocked / Risk Demands")}</div>
						<div class="ia-risk-count">${rows.length}</div>
					</div>
					<div class="ia-panel-tools">
						${injection_aps.ui.icon_button("download", __("Export Excel"), { id: exportId })}
					</div>
				</div>
				<div class="ia-risk-list">
					${rows
						.map(
							(row) => `
								<button
									type="button"
									class="ia-risk-row"
									data-risk-result="${injection_aps.ui.escape(row.name)}"
								>
									<span class="ia-risk-main">
										<span class="ia-risk-item">${injection_aps.ui.escape(row.item_code || "")}</span>
										<span class="ia-risk-name">${injection_aps.ui.escape(injection_aps.ui.shorten(row.item_name || "", 28))}</span>
									</span>
									<span class="ia-risk-side">
										${row.requested_date ? `<span>${injection_aps.ui.escape(injection_aps.ui.format_date(row.requested_date))}</span>` : ""}
										${row.unscheduled_qty ? `<span>${injection_aps.ui.escape(injection_aps.ui.format_number(row.unscheduled_qty))}</span>` : ""}
										${(row.exception_types || []).slice(0, 1).map((flag) => `<span class="ia-risk-badge">${injection_aps.ui.escape(flag)}</span>`).join("")}
									</span>
								</button>
							`
						)
						.join("")}
				</div>
			</div>
		`;
		$(this.riskBoard)
			.find("[data-risk-result]")
			.each((_, node) => {
				node.addEventListener("click", () => this.openResultDrawer(node.dataset.riskResult, null));
			});
		document.getElementById(exportId)?.addEventListener("click", () => {
			this.exportRows(
				__("Blocked / Risk Demands"),
				"aps_blocked_risk_demands",
				[
					{ label: __("Result"), fieldname: "name" },
					{ label: __("Item"), fieldname: "item_code" },
					{ label: __("Item Name"), fieldname: "item_name" },
					{ label: __("Customer"), fieldname: "customer" },
					{ label: __("Requested Date"), fieldname: "requested_date" },
					{ label: __("Demand Source"), fieldname: "demand_source" },
					{ label: __("Status"), fieldname: "status" },
					{ label: __("Risk Status"), fieldname: "risk_status" },
					{ label: __("Unscheduled Qty"), fieldname: "unscheduled_qty", fieldtype: "Float" },
					{ label: __("Blocking Reason"), fieldname: "blocking_reason" },
					{ label: __("Exception Types"), fieldname: "exception_summary" },
				],
				rows.map((row) => ({
					...row,
					exception_summary: (row.exception_types || []).join(", "),
				})),
				__("Rows currently blocked or carrying high scheduling risk.")
			);
		});
	}

	getFilteredTasks(tasks) {
		const mode = this.viewField.get_value() || "Machine";
		if (mode === "Risk") {
			return tasks.filter((task) => {
				const risk = (task.custom_class || "").replace("ia-risk-", "");
				return ["attention", "critical", "blocked"].includes(risk) || !!task.details?.risk_flags;
			});
		}
		if (mode === "Locked") {
			return tasks.filter((task) => Number(task.details?.is_locked || 0) === 1);
		}
		return tasks;
	}

	getLaneKey(task) {
		const mode = this.viewField.get_value() || "Machine";
		if (mode === "Mold") {
			return task.details?.mould_reference || __("No Mold");
		}
		return task.details?.workstation || __("Unknown");
	}

	renderGantt(tasks) {
		const filtered = this.getFilteredTasks(tasks);
		if (!filtered.length) {
			injection_aps.ui.render_cards(this.summary, [
				{ label: __("Tasks"), value: 0, note: __("No scheduled segments for this view.") },
			]);
			this.timeline.innerHTML = "";
			this.grid.innerHTML = `<div class="ia-muted">${__("No schedule segments were generated for this planning run.")}</div>`;
			return;
		}

		const parsedTasks = filtered.map((task) => ({
			...task,
			startDate: frappe.datetime.str_to_obj(task.start),
			endDate: frappe.datetime.str_to_obj(task.end),
		}));
		const minTime = Math.min(...parsedTasks.map((task) => task.startDate.getTime()));
		const maxTime = Math.max(...parsedTasks.map((task) => task.endDate.getTime()));
		const timelineStart = this.floorToDay(minTime);
		const timelineEnd = this.ceilToDay(maxTime);
		const span = Math.max(timelineEnd - timelineStart, 1);
		const lanes = {};
		const blockedCount = (this.data?.blocked_results || []).length;

		parsedTasks.forEach((task) => {
			const lane = this.getLaneKey(task);
			if (!lanes[lane]) {
				lanes[lane] = [];
			}
			lanes[lane].push(task);
		});

		injection_aps.ui.render_cards(this.summary, [
			{ label: __("Tasks"), value: parsedTasks.length },
			{ label: __("Lanes"), value: Object.keys(lanes).length },
			{ label: __("Days"), value: Math.max(1, Math.round((timelineEnd - timelineStart) / 86400000)) },
			{ label: __("Blocked"), value: blockedCount },
		]);
		this.renderTimeline(timelineStart, timelineEnd, span);

		this.grid.innerHTML = Object.entries(lanes)
			.map(([lane, rows]) => {
				const dividers = this.buildDividers(timelineStart, timelineEnd, span);
				const bars = rows
					.map((task) => {
						const left = ((task.startDate.getTime() - timelineStart) / span) * 100;
						const width = Math.max(((task.endDate.getTime() - task.startDate.getTime()) / span) * 100, 4);
						const compactBar = width < 12;
						const tone = (task.custom_class || "").replace("ia-risk-", "");
						const markers = [
							task.details?.copy_mold_parallel ? "B" : "",
							task.details?.family_mold_result ? "F" : "",
							task.details?.is_locked ? "L" : "",
						].filter(Boolean);
						const riskFlags = [...new Set([...(task.details?.risk_badges || []), ...String(task.details?.risk_flags || "").split("\n").filter(Boolean)])];
						const barClass = ["ia-gantt-bar", tone, task.details?.is_locked ? "locked" : "", compactBar ? "compact" : ""].filter(Boolean).join(" ");
						const isDragLocked =
							task.details?.segment_kind === "Family Co-Product" ||
							Number(task.details?.is_locked || 0) === 1 ||
							["Applied", "Completed"].includes(task.details?.segment_status);
						const title = task.details?.item_name || task.details?.item_code || "";
						const metaParts = [
							injection_aps.ui.format_number(task.details?.planned_qty || 0),
							task.details?.mould_reference || "-",
							task.details?.customer_reference || "",
						].filter(Boolean);
						const visibleRiskFlags = riskFlags.slice(0, compactBar ? 1 : 2);
						return `
							<div
								class="${barClass}"
								style="left:${left}%; width:${width}%;"
								data-segment-name="${injection_aps.ui.escape(task.details?.segment_name || "")}"
								data-result-name="${injection_aps.ui.escape(task.details?.result_name || "")}"
								data-workstation="${injection_aps.ui.escape(task.details?.workstation || "")}"
								draggable="${isDragLocked ? "false" : "true"}"
							>
								<div class="ia-gantt-title">
									<span class="ia-gantt-code">${injection_aps.ui.escape(task.details?.item_code || "")}</span>
									${compactBar ? "" : `<span class="ia-gantt-name">${injection_aps.ui.escape(injection_aps.ui.shorten(title, 24))}</span>`}
								</div>
								<div class="ia-gantt-meta-line">
									<span class="ia-gantt-meta-text">${injection_aps.ui.escape(metaParts.join(" | "))}</span>
									<span class="ia-gantt-inline-flags">
										${markers.map((flag) => `<span class="ia-gantt-flag blue">${flag}</span>`).join("")}
										${visibleRiskFlags.map((flag) => `<span class="ia-gantt-flag ${String(flag).includes("FDA") ? "red" : "orange"}">${injection_aps.ui.escape(flag)}</span>`).join("")}
									</span>
								</div>
							</div>
						`;
					})
					.join("");

				return `
					<div class="ia-gantt-row">
						<div class="ia-gantt-label">
							<div>${injection_aps.ui.escape(lane)}</div>
							<div class="ia-muted">${rows.length} ${__("segments")}</div>
						</div>
						<div class="ia-gantt-track" data-lane="${injection_aps.ui.escape(lane)}" data-workstation="${this.viewField.get_value() === "Mold" ? "" : injection_aps.ui.escape(lane)}">
							${dividers}
							${bars}
						</div>
					</div>
				`;
			})
			.join("");

		this.bindGanttInteractions();
	}

	floorToDay(value) {
		const date = new Date(value);
		date.setHours(0, 0, 0, 0);
		return date.getTime();
	}

	ceilToDay(value) {
		const date = new Date(value);
		date.setHours(0, 0, 0, 0);
		date.setDate(date.getDate() + 1);
		return date.getTime();
	}

	renderTimeline(timelineStart, timelineEnd, span) {
		const cells = [];
		let cursor = timelineStart;
		while (cursor < timelineEnd) {
			const next = Math.min(cursor + 86400000, timelineEnd);
			const left = ((cursor - timelineStart) / span) * 100;
			const width = ((next - cursor) / span) * 100;
			const date = new Date(cursor);
			cells.push(`
				<div class="ia-gantt-timeline-cell" style="left:${left}%; width:${width}%;">
					<div class="ia-gantt-timeline-date">${date.toLocaleDateString()}</div>
					<div>${__("00:00")} - ${__("24:00")}</div>
				</div>
			`);
			cursor = next;
		}
		this.timeline.innerHTML = `
			<div class="ia-gantt-timeline-spacer"></div>
			<div class="ia-gantt-timeline-track">${cells.join("")}</div>
		`;
	}

	buildDividers(timelineStart, timelineEnd, span) {
		const dividers = [];
		let cursor = timelineStart + 86400000;
		while (cursor < timelineEnd) {
			const left = ((cursor - timelineStart) / span) * 100;
			dividers.push(`<div class="ia-gantt-divider" style="left:${left}%;"></div>`);
			cursor += 86400000;
		}
		return dividers.join("");
	}

	bindGanttInteractions() {
		$(this.grid)
			.find(".ia-gantt-bar")
			.each((_, node) => {
				node.addEventListener("click", () => this.openResultDrawer(node.dataset.resultName, node.dataset.segmentName));
				node.addEventListener("dragstart", (event) => {
					this.draggingSegment = node.dataset.segmentName;
					node.classList.add("dragging");
					event.dataTransfer.setData("text/plain", this.draggingSegment);
				});
				node.addEventListener("dragend", () => {
					node.classList.remove("dragging");
				});
				node.addEventListener("dragover", (event) => event.preventDefault());
				node.addEventListener("drop", async (event) => {
					event.preventDefault();
					if (this.viewField.get_value() !== "Machine") {
						frappe.show_alert({ message: __("Switch to Machine view before drag re-sequencing."), indicator: "orange" });
						return;
					}
					const segmentName = event.dataTransfer.getData("text/plain");
					await this.handleDrop(segmentName, node.dataset.workstation, node.dataset.segmentName);
				});
			});

		$(this.grid)
			.find(".ia-gantt-track")
			.each((_, node) => {
				node.addEventListener("dragover", (event) => event.preventDefault());
				node.addEventListener("drop", async (event) => {
					event.preventDefault();
					if (this.viewField.get_value() !== "Machine") {
						frappe.show_alert({ message: __("Switch to Machine view before drag re-sequencing."), indicator: "orange" });
						return;
					}
					const segmentName = event.dataTransfer.getData("text/plain");
					await this.handleDrop(segmentName, node.dataset.workstation || "", null);
				});
			});
	}

	async handleDrop(segmentName, targetWorkstation, beforeSegmentName) {
		if (!segmentName || !targetWorkstation) {
			return;
		}
		const preview = await injection_aps.ui.xcall(
			{
				message: __("Previewing manual schedule adjustment..."),
				busy_key: `gantt-preview:${segmentName}`,
				feedback_target: this.feedback,
				success_feedback: __("Manual move preview is ready."),
			},
			"injection_aps.api.app.preview_manual_schedule_adjustment",
			{
				segment_name: segmentName,
				target_workstation: targetWorkstation,
				before_segment_name: beforeSegmentName || undefined,
			}
		);
		if (!preview) {
			return;
		}
		if (!preview.allowed) {
			if (preview.override_available) {
				frappe.confirm(
					`${preview.override_reason || __("This move requires risk override.")}<br><br>${__("Continue with manual override?")}`,
					async () => {
						const response = await injection_aps.ui.xcall(
							{
								message: __("Applying manual override..."),
								success_message: __("Manual override applied."),
								busy_key: `gantt-override:${segmentName}`,
								feedback_target: this.feedback,
								success_feedback: __("Manual override applied. Refreshing Gantt..."),
							},
							"injection_aps.api.app.apply_manual_schedule_adjustment",
							{
								segment_name: segmentName,
								target_workstation: targetWorkstation,
								before_segment_name: beforeSegmentName || undefined,
								allow_risk_override: 1,
							}
						);
						if (!response) {
							return;
						}
						await this.refresh();
					}
				);
				return;
			}
			frappe.msgprint({
				title: __("Manual Move Blocked"),
				message: (preview.blocking_reasons || []).map((row) => `<div>${injection_aps.ui.escape(row)}</div>`).join(""),
			});
			return;
		}
		frappe.confirm(
			__("Move segment to {0} with mold {1}?").replace("{0}", targetWorkstation).replace("{1}", preview.target_mould_reference || "-"),
			async () => {
				const response = await injection_aps.ui.xcall(
					{
						message: __("Applying manual schedule adjustment..."),
						success_message: __("Manual adjustment applied."),
						busy_key: `gantt-apply:${segmentName}`,
						feedback_target: this.feedback,
						success_feedback: __("Manual adjustment applied. Refreshing Gantt..."),
					},
					"injection_aps.api.app.apply_manual_schedule_adjustment",
					{
						segment_name: segmentName,
						target_workstation: targetWorkstation,
						before_segment_name: beforeSegmentName || undefined,
					}
				);
				if (!response) {
					return;
				}
				await this.refresh();
			}
		);
	}

	async openResultDrawer(resultName, segmentName) {
		const detail = await injection_aps.ui.xcall(
			{
				message: __("Loading segment detail..."),
				busy_key: `result-detail:${resultName}`,
				feedback_target: this.feedback,
				success_feedback: __("Segment detail loaded."),
			},
			"injection_aps.api.app.get_schedule_result_detail",
			{
				result_name: resultName,
			}
		);
		if (!detail) {
			return;
		}
		const result = detail.result || {};
		const segments = detail.segments || [];
		const itemDetail = detail.item_detail || {};
		const sourceRows = detail.source_rows || [];
		const exceptionRows = detail.exception_rows || [];
		const moldRows = detail.mold_rows || [];
		const selectedSegment = segments.find((row) => row.name === segmentName) || segments[0] || {};
		const actionHostId = `ia-drawer-actions-${Math.random().toString(36).slice(2, 8)}`;
		const resultNoteId = `ia-result-note-${Math.random().toString(36).slice(2, 8)}`;
		const segmentNoteId = `ia-segment-note-${Math.random().toString(36).slice(2, 8)}`;
		const saveResultNoteId = `ia-save-result-note-${Math.random().toString(36).slice(2, 8)}`;
		const saveSegmentNoteId = `ia-save-segment-note-${Math.random().toString(36).slice(2, 8)}`;
		const sourceExportId = this.makeExportId("sources");
		const moldExportId = this.makeExportId("molds");
		const exceptionExportId = this.makeExportId("exceptions");
		const segmentExportId = this.makeExportId("segments");
		const routeLinks = detail.routes || {};
		const link = (route, label) => (route ? injection_aps.ui.route_link(label, route) : injection_aps.ui.escape(label || ""));
		const sourceTable = sourceRows.length
			? `
				<div class="ia-table-shell">
					<table class="ia-table">
						<thead>
							<tr>
								<th>${__("Source")}</th>
								<th>${__("Demand")}</th>
								<th>${__("Qty")}</th>
								<th>${__("Customer Ref")}</th>
								<th>${__("Remark")}</th>
							</tr>
						</thead>
						<tbody>
							${sourceRows
								.map(
									(row) => `
										<tr>
											<td>${link(row.source_route, row.source_name || row.source_doctype || "-")}</td>
											<td>${injection_aps.ui.escape(row.demand_source || "")}</td>
											<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.qty || 0))}</td>
											<td>${injection_aps.ui.escape(row.customer_part_no || row.sales_order || "")}</td>
											<td>${injection_aps.ui.escape(row.remark || "")}</td>
										</tr>
									`
								)
								.join("")}
						</tbody>
					</table>
				</div>
			`
			: `<div class="ia-muted">${__("No demand source rows were linked back for this result.")}</div>`;
		const moldTable = moldRows.length
			? `
				<div class="ia-table-shell">
					<table class="ia-table">
						<thead>
							<tr>
								<th>${__("Mold")}</th>
								<th>${__("Status")}</th>
								<th>${__("Tonnage")}</th>
								<th>${__("Cavity")}</th>
								<th>${__("Output/Cycle")}</th>
							</tr>
						</thead>
						<tbody>
							${moldRows
								.map(
									(row) => `
										<tr>
											<td>${link(row.mold_route, row.mold || "-")} / ${injection_aps.ui.escape(row.mold_name || "")}</td>
											<td>${injection_aps.ui.escape(row.mold_status || "")}${row.is_family_mold ? ` / ${__("Family")}` : ""}</td>
											<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.machine_tonnage || 0))}</td>
											<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.cavity_count || 0))}</td>
											<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.effective_output_qty || 0))} / ${injection_aps.ui.escape(injection_aps.ui.format_number(row.cycle_time_seconds || 0))}s</td>
										</tr>
									`
								)
								.join("")}
						</tbody>
					</table>
				</div>
			`
			: `<div class="ia-muted">${__("No mold detail was available for this result.")}</div>`;
		const exceptionTable = exceptionRows.length
			? `
				<div class="ia-table-shell">
					<table class="ia-table">
						<thead>
							<tr>
								<th>${__("Severity")}</th>
								<th>${__("Type")}</th>
								<th>${__("Message")}</th>
							</tr>
						</thead>
						<tbody>
							${exceptionRows
								.map(
									(row) => `
										<tr>
											<td>${injection_aps.ui.pill(row.severity || "", row.is_blocking ? "red" : "orange")}</td>
											<td>${injection_aps.ui.escape(row.exception_type || "")}</td>
											<td>${injection_aps.ui.escape(row.message || "")}</td>
										</tr>
									`
								)
								.join("")}
						</tbody>
					</table>
				</div>
			`
			: `<div class="ia-muted">${__("No open exception rows were found for this result.")}</div>`;
		const html = `
			<div class="ia-page">
				<div class="ia-mini-grid">
					<div class="ia-panel">
						<h4>${__("Item")}</h4>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Code")}</div><div class="ia-kv-value">${link(itemDetail.item_route, itemDetail.item_code || result.item_code || "-")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Name")}</div><div class="ia-kv-value">${injection_aps.ui.escape(itemDetail.item_name || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Customer Ref")}</div><div class="ia-kv-value">${injection_aps.ui.escape(itemDetail.customer_reference || sourceRows[0]?.customer_part_no || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Drawing")}</div><div class="ia-kv-value">${injection_aps.ui.escape(itemDetail.drawing_file || "")}</div></div>
						</div>
					</div>
					<div class="ia-panel">
						<h4>${__("Planning")}</h4>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Requested")}</div><div class="ia-kv-value">${injection_aps.ui.escape(result.requested_date || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Source")}</div><div class="ia-kv-value">${injection_aps.ui.escape(result.demand_source || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Run")}</div><div class="ia-kv-value">${link(routeLinks.planning_run, result.planning_run || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Net Req")}</div><div class="ia-kv-value">${link(routeLinks.net_requirement, result.net_requirement || "")}</div></div>
						</div>
					</div>
				</div>
				<div class="ia-mini-grid">
					<div class="ia-panel">
						<h4>${__("Execution")}</h4>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Actual Status")}</div><div class="ia-kv-value">${injection_aps.ui.escape(selectedSegment.actual_status || result.actual_status || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Actual Qty")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.format_number(selectedSegment.actual_completed_qty || result.actual_progress_qty || 0))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Work Order")}</div><div class="ia-kv-value">${link(selectedSegment.work_order_route, selectedSegment.linked_work_order || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Scheduling")}</div><div class="ia-kv-value">${link(selectedSegment.work_order_scheduling_route, selectedSegment.linked_work_order_scheduling || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Manufacture Entry")}</div><div class="ia-kv-value">${link(selectedSegment.latest_stock_entry_route, selectedSegment.latest_stock_entry || "")}</div></div>
						</div>
					</div>
						<div class="ia-panel">
						<h4>${__("Notes")}</h4>
						<div class="ia-kv-row" style="display:block; margin-bottom:8px;">
							<div class="ia-kv-key">${__("Result Note")}</div>
							<textarea id="${resultNoteId}" class="form-control" rows="3">${injection_aps.ui.escape(result.notes || "")}</textarea>
							<div class="ia-note-save-row">
								${injection_aps.ui.icon_button("check", __("Save Result Note"), { id: saveResultNoteId }, "ia-note-save-btn")}
							</div>
						</div>
						<div class="ia-kv-row" style="display:block;">
							<div class="ia-kv-key">${__("Segment Note")}</div>
							<textarea id="${segmentNoteId}" class="form-control" rows="3">${injection_aps.ui.escape(selectedSegment.segment_note || "")}</textarea>
							<div class="ia-note-save-row">
								${injection_aps.ui.icon_button("check", __("Save Segment Note"), { id: saveSegmentNoteId, disabled: selectedSegment.name ? null : "disabled" }, "ia-note-save-btn")}
							</div>
						</div>
					</div>
				</div>
				<div class="ia-status-line">
					<div class="ia-status-cell"><span class="ia-status-label">${__("Flow")}</span><div class="ia-status-value">${injection_aps.ui.escape(result.flow_step || "")}</div></div>
					<div class="ia-status-cell"><span class="ia-status-label">${__("Next")}</span><div class="ia-status-value">${injection_aps.ui.escape(result.next_step_hint || "")}</div></div>
					<div class="ia-status-cell"><span class="ia-status-label">${__("Blocking")}</span><div class="ia-status-value">${injection_aps.ui.escape(result.blocking_reason || __("None"))}</div></div>
				</div>
				<div class="ia-panel">
					<h4>${__("Explanation")}</h4>
					<div class="ia-muted">${injection_aps.ui.escape(result.schedule_explanation || result.family_output_summary || "")}</div>
				</div>
				<div id="${actionHostId}"></div>
				<div class="ia-panel">
					<div class="ia-panel-head">
						<h4>${__("Demand Sources")}</h4>
						<div class="ia-panel-tools">
							${injection_aps.ui.icon_button("download", __("Export Excel"), { id: sourceExportId })}
						</div>
					</div>
					${sourceTable}
				</div>
				<div class="ia-panel">
					<div class="ia-panel-head">
						<h4>${__("Mold Basis")}</h4>
						<div class="ia-panel-tools">
							${injection_aps.ui.icon_button("download", __("Export Excel"), { id: moldExportId })}
						</div>
					</div>
					${moldTable}
				</div>
				<div class="ia-panel">
					<div class="ia-panel-head">
						<h4>${__("Risk Flags")}</h4>
						<div class="ia-panel-tools">
							${injection_aps.ui.icon_button("download", __("Export Excel"), { id: exceptionExportId })}
						</div>
					</div>
					${exceptionTable}
				</div>
				<div class="ia-panel">
					<div class="ia-panel-head">
						<h4>${__("Segments")}</h4>
						<div class="ia-panel-tools">
							${injection_aps.ui.icon_button("download", __("Export Excel"), { id: segmentExportId })}
						</div>
					</div>
					<div class="ia-table-shell">
						<table class="ia-table">
							<thead>
								<tr>
									<th>${__("Segment")}</th>
									<th>${__("Workstation")}</th>
									<th>${__("Mold")}</th>
									<th>${__("Qty")}</th>
									<th>${__("Actual")}</th>
									<th>${__("Risk")}</th>
									<th>${__("Window")}</th>
								</tr>
							</thead>
							<tbody>
								${segments
									.map(
										(row) => `
											<tr ${row.name === segmentName ? 'style="background:#f8fbff;"' : ""}>
												<td>${injection_aps.ui.escape(row.segment_kind || "")}</td>
												<td>${injection_aps.ui.escape(row.workstation || "")}</td>
												<td>${injection_aps.ui.escape(row.mould_reference || "")}</td>
												<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.planned_qty || 0))}</td>
												<td>${injection_aps.ui.escape(row.actual_status || "")} / ${injection_aps.ui.escape(injection_aps.ui.format_number(row.actual_completed_qty || 0))}</td>
												<td>${injection_aps.ui.escape(row.risk_flags || "")}</td>
												<td>${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.start_time))} - ${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.end_time))}</td>
											</tr>
										`
									)
									.join("")}
							</tbody>
						</table>
					</div>
				</div>
			</div>
		`;
		injection_aps.ui.open_drawer(resultName, `${result.item_code || ""} / ${result.customer || ""}`, html);
		const actionHost = document.getElementById(actionHostId);
		injection_aps.ui.render_actions(actionHost, detail.next_actions?.actions || [], async (action) => {
			await injection_aps.ui.run_action(action);
		});
		document.getElementById(saveResultNoteId)?.addEventListener("click", async () => {
			await injection_aps.ui.xcall(
				{
					message: __("Saving result note..."),
					success_message: __("Result note saved."),
					busy_key: `save-result-note:${resultName}`,
					feedback_target: this.feedback,
				},
				"injection_aps.api.app.update_schedule_notes",
				{
					result_name: resultName,
					result_note: document.getElementById(resultNoteId)?.value || "",
				}
			);
		});
		document.getElementById(saveSegmentNoteId)?.addEventListener("click", async () => {
			if (!selectedSegment.name) {
				return;
			}
			await injection_aps.ui.xcall(
				{
					message: __("Saving segment note..."),
					success_message: __("Segment note saved."),
					busy_key: `save-segment-note:${selectedSegment.name}`,
					feedback_target: this.feedback,
				},
				"injection_aps.api.app.update_schedule_notes",
				{
					result_name: resultName,
					segment_name: selectedSegment.name,
					segment_note: document.getElementById(segmentNoteId)?.value || "",
				}
			);
		});
		document.getElementById(sourceExportId)?.addEventListener("click", () => {
			this.exportRows(
				__("Demand Sources"),
				`aps_demand_sources_${resultName}`,
				[
					{ label: __("Source Doctype"), fieldname: "source_doctype" },
					{ label: __("Source Name"), fieldname: "source_name" },
					{ label: __("Demand Source"), fieldname: "demand_source" },
					{ label: __("Demand Date"), fieldname: "demand_date" },
					{ label: __("Qty"), fieldname: "qty", fieldtype: "Float" },
					{ label: __("Customer Ref"), fieldname: "customer_part_no" },
					{ label: __("Sales Order"), fieldname: "sales_order" },
					{ label: __("Remark"), fieldname: "remark" },
				],
				sourceRows,
				`${result.item_code || ""} / ${result.customer || ""}`
			);
		});
		document.getElementById(moldExportId)?.addEventListener("click", () => {
			this.exportRows(
				__("Mold Basis"),
				`aps_mold_basis_${resultName}`,
				[
					{ label: __("Mold"), fieldname: "mold" },
					{ label: __("Mold Name"), fieldname: "mold_name" },
					{ label: __("Status"), fieldname: "mold_status" },
					{ label: __("Machine Tonnage"), fieldname: "machine_tonnage", fieldtype: "Float" },
					{ label: __("Cavity"), fieldname: "cavity_count", fieldtype: "Float" },
					{ label: __("Effective Output Qty"), fieldname: "effective_output_qty", fieldtype: "Float" },
					{ label: __("Cycle Time Seconds"), fieldname: "cycle_time_seconds", fieldtype: "Float" },
					{ label: __("Family Mold"), fieldname: "is_family_mold", fieldtype: "Check" },
				],
				moldRows,
				`${result.item_code || ""} / ${result.customer || ""}`
			);
		});
		document.getElementById(exceptionExportId)?.addEventListener("click", () => {
			this.exportRows(
				__("Risk Flags"),
				`aps_risk_flags_${resultName}`,
				[
					{ label: __("Severity"), fieldname: "severity" },
					{ label: __("Type"), fieldname: "exception_type" },
					{ label: __("Message"), fieldname: "message" },
					{ label: __("Blocking"), fieldname: "is_blocking", fieldtype: "Check" },
					{ label: __("Machine"), fieldname: "workstation" },
					{ label: __("Source Doctype"), fieldname: "source_doctype" },
					{ label: __("Source Name"), fieldname: "source_name" },
				],
				exceptionRows,
				`${result.item_code || ""} / ${result.customer || ""}`
			);
		});
		document.getElementById(segmentExportId)?.addEventListener("click", () => {
			this.exportRows(
				__("Segments"),
				`aps_segments_${resultName}`,
				[
					{ label: __("Segment"), fieldname: "segment_kind" },
					{ label: __("Workstation"), fieldname: "workstation" },
					{ label: __("Mold"), fieldname: "mould_reference" },
					{ label: __("Qty"), fieldname: "planned_qty", fieldtype: "Float" },
					{ label: __("Actual Status"), fieldname: "actual_status" },
					{ label: __("Actual Qty"), fieldname: "actual_completed_qty", fieldtype: "Float" },
					{ label: __("Risk"), fieldname: "risk_flags" },
					{ label: __("Start"), fieldname: "start_time" },
					{ label: __("End"), fieldname: "end_time" },
					{ label: __("Work Order"), fieldname: "linked_work_order" },
					{ label: __("Scheduling"), fieldname: "linked_work_order_scheduling" },
				],
				segments,
				`${result.item_code || ""} / ${result.customer || ""}`
			);
		});
	}
}
