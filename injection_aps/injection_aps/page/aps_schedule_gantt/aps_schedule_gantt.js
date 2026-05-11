frappe.pages["aps-schedule-gantt"].on_page_load = function (wrapper) {
	frappe.require("/assets/injection_aps/js/injection_aps_shared.js", () => {
		if (!wrapper.injection_aps_controller) {
			wrapper.injection_aps_controller = new InjectionAPSScheduleGantt(wrapper);
		}
		wrapper.injection_aps_controller.refresh();
	});
};

frappe.pages["aps-schedule-gantt"].on_page_show = function (wrapper) {
	if (wrapper.injection_aps_controller) {
		wrapper.injection_aps_controller.refresh();
	}
};

class InjectionAPSScheduleGantt {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.wrapper.classList.add("ia-app-page");
		this.draggingSegment = null;
		this.focusWindow = null;
		this.timelineMeta = null;
		this.dragState = null;
		this.splitState = null;
		this.splitHoverHandle = null;
		this.splitHoverBar = null;
		this.splitHoverHideTimer = null;
		this.isChartFullscreen = false;
		this.zoomFactor = 1;
		this.segmentSearchTerm = "";
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Board"),
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
		this.viewField = this.page.add_field({
			fieldtype: "Select",
			fieldname: "view_mode",
			label: __("View Mode"),
			options: ["Machine", "Mold", "Risk", "Locked"].join("\n"),
			default: "Machine",
			change: () => this.refresh(),
		});

		this.page.main.html(`
			<div class="ia-page">
				<div class="ia-empty-state-host"></div>
				<div class="ia-run-body">
					<div class="ia-run-context-host"></div>
					<div class="ia-status-host"></div>
					<div class="ia-card-grid ia-summary"></div>
					<div class="ia-feedback"></div>
					<div class="ia-risk-board"></div>
					<div class="ia-gantt-frame">
						<div class="ia-gantt-frame-close"></div>
						<div class="ia-gantt-tools"></div>
						<div class="ia-gantt-overlay"></div>
						<div class="ia-gantt-shell">
							<div class="ia-gantt-timeline"></div>
							<div class="ia-gantt-grid"></div>
						</div>
					</div>
				</div>
			</div>
		`);
		this.emptyStateHost = this.page.main.find(".ia-empty-state-host")[0];
		this.runBody = this.page.main.find(".ia-run-body")[0];
		this.runContextHost = this.page.main.find(".ia-run-context-host")[0];
		this.statusHost = this.page.main.find(".ia-status-host")[0];
		this.summary = this.page.main.find(".ia-summary")[0];
		this.feedback = this.page.main.find(".ia-feedback")[0];
		this.tools = this.page.main.find(".ia-gantt-tools")[0];
		this.riskBoard = this.page.main.find(".ia-risk-board")[0];
		this.ganttFrame = this.page.main.find(".ia-gantt-frame")[0];
		this.ganttFrameClose = this.page.main.find(".ia-gantt-frame-close")[0];
		this.ganttOverlay = this.page.main.find(".ia-gantt-overlay")[0];
		this.ganttShell = this.page.main.find(".ia-gantt-shell")[0];
		this.timeline = this.page.main.find(".ia-gantt-timeline")[0];
		this.grid = this.page.main.find(".ia-gantt-grid")[0];
	}

	makeExportId(prefix) {
		return `ia-export-${prefix}-${Math.random().toString(36).slice(2, 8)}`;
	}

	canEditManualSchedule() {
		return injection_aps.ui.can_run_action("apply_manual_schedule_adjustment");
	}

	canPreviewSegmentSplit() {
		return injection_aps.ui.can_run_action("preview_segment_split") || injection_aps.ui.can_run_action("apply_segment_split");
	}

	canApplySegmentSplit() {
		return injection_aps.ui.can_run_action("apply_segment_split");
	}

	canEditScheduleFlex() {
		return this.canPreviewSegmentSplit() || injection_aps.ui.can_run_action("create_or_update_downtime_window");
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

	showManualAdjustmentBlocked(preview, fallbackTitle) {
		const summary = preview && preview.blocking_summary ? `<div class="ia-confirm-row"><strong>${injection_aps.ui.escape(preview.blocking_summary)}</strong></div>` : "";
		const contextRows = ((preview && preview.blocking_context_rows) || [])
			.map((row) => {
				return `<div class="ia-confirm-row"><span class="ia-muted">${injection_aps.ui.escape(row.label || "")}</span> ${injection_aps.ui.escape(row.value || "")}</div>`;
			})
			.join("");
		const suggestions = ((preview && preview.resolution_suggestions) || [])
			.map((row) => `<li>${injection_aps.ui.escape(row || "")}</li>`)
			.join("");
		const rawReasons = ((preview && preview.blocking_reasons) || [])
			.map((row) => `<li>${injection_aps.ui.escape(row || "")}</li>`)
			.join("");
		const message = `
			<div class="ia-confirm-summary">
				${summary}
				${contextRows ? `<div>${contextRows}</div>` : ""}
				${suggestions ? `<div><div class="ia-muted">${__("Suggested Actions")}</div><ul>${suggestions}</ul></div>` : ""}
				${rawReasons ? `<details><summary>${__("Technical Blocking Details")}</summary><ul>${rawReasons}</ul></details>` : ""}
			</div>
		`;
		frappe.msgprint({
			title: (preview && preview.blocking_title) || fallbackTitle || __("Manual Move Blocked"),
			message,
			wide: true,
		});
	}

	renderImpactHtml(preview) {
		const updates = (preview && preview.proposed_updates) || [];
		const blockers = (preview && preview.blockers) || [];
		const risks = (preview && preview.delivery_risks) || [];
		const rows = updates
			.slice(0, 12)
			.map(
				(row) => `
					<tr>
						<td>${injection_aps.ui.escape(row.segment_name || "")}</td>
						<td>${injection_aps.ui.escape(row.item_code || "")}</td>
						<td>${injection_aps.ui.escape(row.workstation || "")}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.old_start_time))}</td>
						<td>${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.new_start_time))}</td>
						<td>${injection_aps.ui.escape(row.wos_action || "")}</td>
						<td>${row.delivery_risk ? injection_aps.ui.pill(row.delivery_risk, "orange") : ""}</td>
					</tr>
				`
			)
			.join("");
		const blockerRows = blockers
			.map((row) => `<li>${injection_aps.ui.escape(row.segment_name || "")}: ${injection_aps.ui.escape(row.reason || "")}</li>`)
			.join("");
		return `
			<div class="ia-confirm-summary">
				<div class="ia-confirm-row"><strong>${__("Affected Segments")}: ${injection_aps.ui.format_number(updates.length)}</strong></div>
				<div class="ia-confirm-row">${__("Delivery Risks")}: ${injection_aps.ui.format_number(risks.length)}</div>
				${blockerRows ? `<div class="ia-confirm-row"><strong>${__("Blocked")}</strong><ul>${blockerRows}</ul></div>` : ""}
				${
					rows
						? `<div class="ia-table-shell"><table class="ia-table"><thead><tr><th>${__("Segment")}</th><th>${__("Item")}</th><th>${__("Workstation")}</th><th>${__("Old Start")}</th><th>${__("New Start")}</th><th>${__("WOS")}</th><th>${__("Risk")}</th></tr></thead><tbody>${rows}</tbody></table></div>`
						: `<div class="ia-muted">${__("No downstream segment needs to move.")}</div>`
				}
			</div>
		`;
	}

	async showImpactPreview(options) {
		const settings = Object.assign({}, options || {});
		const runName = this.runField.get_value() || injection_aps.ui.get_value(this.data, "run.name", "");
		const preview =
			settings.preview ||
			(await injection_aps.ui.xcall(
				{
					message: __("Previewing schedule impact..."),
					busy_key: `impact-preview:${runName || settings.segmentName || ""}`,
					feedback_target: this.feedback,
					success_feedback: __("Impact preview is ready."),
				},
				"injection_aps.api.app.preview_schedule_impact",
				{
					run_name: runName,
					downtime_window: settings.downtimeWindow || undefined,
					segment_name: settings.segmentName || undefined,
				}
			));
		if (!preview) {
			return;
		}
		const canApply = preview.allowed && injection_aps.ui.can_run_action("apply_schedule_impact");
		const dialog = new frappe.ui.Dialog({
			title: __("Schedule Impact Preview"),
			fields: [{ fieldtype: "HTML", fieldname: "impact_html" }],
			primary_action_label: canApply ? __("Apply Impact") : __("Close"),
			primary_action: async () => {
				if (!canApply) {
					dialog.hide();
					return;
				}
				const response = await injection_aps.ui.xcall(
					{
						message: __("Applying schedule impact..."),
						success_message: __("Schedule impact applied."),
						busy_key: `impact-apply:${runName}`,
						feedback_target: this.feedback,
						success_feedback: __("Impact applied. Refreshing Gantt..."),
					},
					"injection_aps.api.app.apply_schedule_impact",
					{
						run_name: runName,
						downtime_window: settings.downtimeWindow || preview.downtime_window || undefined,
					}
				);
				dialog.hide();
				if (response) {
					await this.refresh();
				}
			},
		});
		dialog.get_field("impact_html").$wrapper.html(this.renderImpactHtml(preview));
		dialog.show();
	}

	openSegmentSplitDialog(segmentName) {
		if (!segmentName) {
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Split Segment"),
			fields: [
				{ fieldtype: "Select", fieldname: "split_mode", label: __("Split Mode"), options: ["Time", "Qty", "Downtime Window"].join("\n"), default: "Time", reqd: 1 },
				{ fieldtype: "Datetime", fieldname: "split_time", label: __("Split Time"), depends_on: "eval:doc.split_mode=='Time'" },
				{ fieldtype: "Float", fieldname: "split_qty", label: __("First Segment Qty"), depends_on: "eval:doc.split_mode=='Qty'" },
				{ fieldtype: "Link", fieldname: "downtime_window", label: __("Downtime Window"), options: "APS Downtime Window", depends_on: "eval:doc.split_mode=='Downtime Window'" },
				{ fieldtype: "Data", fieldname: "split_reason", label: __("Reason") },
				{ fieldtype: "HTML", fieldname: "preview_html" },
			],
			primary_action_label: __("Preview"),
			primary_action: async () => {
				const values = dialog.get_values();
				if (!values) {
					return;
				}
				const args = {
					segment_name: segmentName,
					split_reason: values.split_reason || undefined,
				};
				if (values.split_mode === "Time") {
					args.split_time = values.split_time;
				} else if (values.split_mode === "Qty") {
					args.split_qty = values.split_qty;
				} else {
					args.downtime_window = values.downtime_window;
				}
				const preview = await injection_aps.ui.xcall(
					{
						message: __("Previewing segment split..."),
						busy_key: `split-preview:${segmentName}`,
						feedback_target: this.feedback,
						success_feedback: __("Split preview is ready."),
					},
					"injection_aps.api.app.preview_segment_split",
					args
				);
				if (!preview || !preview.allowed) {
					this.showManualAdjustmentBlocked(preview, __("Split Blocked"));
					return;
				}
				const rows = (preview.proposed_segments || [])
					.map(
						(row) => `<div class="ia-confirm-row">${__("Split Part")} ${row.split_index}: ${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.start_time))} - ${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.end_time))} / ${injection_aps.ui.escape(injection_aps.ui.format_number(row.planned_qty || 0))}</div>`
					)
					.join("");
				dialog.get_field("preview_html").$wrapper.html(`<div class="ia-confirm-summary">${rows}</div>`);
				if (!this.canApplySegmentSplit()) {
					injection_aps.ui.set_feedback(this.feedback, __("Split preview is ready, but this user cannot apply the split."), "warning");
					return;
				}
				frappe.confirm(__("Apply this segment split?"), async () => {
					const response = await injection_aps.ui.xcall(
						{
							message: __("Applying segment split..."),
							success_message: __("Segment split applied."),
							busy_key: `split-apply:${segmentName}`,
							feedback_target: this.feedback,
							success_feedback: __("Segment split applied. Refreshing Gantt..."),
						},
						"injection_aps.api.app.apply_segment_split",
						args
					);
					dialog.hide();
					if (response) {
						await this.refresh();
					}
				});
			},
		});
		dialog.show();
	}

	canVisualSplitSegment(barNode) {
		return !this.getVisualSplitBlockReason(barNode);
	}

	canShowSplitHoverHandle(barNode) {
		if (!barNode || !this.canPreviewSegmentSplit()) {
			return false;
		}
		if (!barNode.dataset.segmentName) {
			return false;
		}
		if ((this.viewField.get_value() || "Machine") !== "Machine") {
			return false;
		}
		const startMs = Number(barNode.dataset.startMs || 0);
		const endMs = Number(barNode.dataset.endMs || 0);
		return Boolean(startMs && endMs && endMs > startMs);
	}

	getVisualSplitBlockReason(barNode) {
		if (!barNode) {
			return __("No segment is selected.");
		}
		if (!this.canPreviewSegmentSplit()) {
			return __("You do not have permission to split segments.");
		}
		if (!barNode.dataset.segmentName) {
			return __("No segment is linked to this bar.");
		}
		if ((this.viewField.get_value() || "Machine") !== "Machine") {
			return __("Switch to Machine view to split segments.");
		}
		const startMs = Number(barNode.dataset.startMs || 0);
		const endMs = Number(barNode.dataset.endMs || 0);
		if (!startMs || !endMs || endMs <= startMs) {
			return __("This segment has no valid time range.");
		}
		if (barNode.dataset.segmentKind === "Family Co-Product") {
			return __("Family co-product segments follow the primary segment and cannot be split directly.");
		}
		if (Number(barNode.dataset.isLocked || 0) === 1) {
			return __("Locked segments cannot be split.");
		}
		if (["Applied", "Completed"].includes(barNode.dataset.segmentStatus || "")) {
			return __("Released or completed segments cannot be split from Gantt.");
		}
		return "";
	}

	beginVisualSplit(barNode) {
		const blockReason = this.getVisualSplitBlockReason(barNode);
		if (blockReason) {
			injection_aps.ui.set_feedback(this.feedback, blockReason, "warning");
			return;
		}
		this.hideSplitHoverHandle();
		this.cleanupInteraction();
		this.cleanupVisualSplit();
		const startMs = Number(barNode.dataset.startMs || 0);
		const endMs = Number(barNode.dataset.endMs || 0);
		const cutNode = document.createElement("span");
		const tooltipNode = document.createElement("span");
		cutNode.className = "ia-gantt-cut-line";
		tooltipNode.className = "ia-gantt-cut-tooltip";
		barNode.appendChild(cutNode);
		barNode.appendChild(tooltipNode);
		this.splitState = {
			barNode,
			cutNode,
			tooltipNode,
			segmentName: barNode.dataset.segmentName || "",
			startMs,
			endMs,
			plannedQty: Number(barNode.dataset.plannedQty || 0),
			cutMs: null,
		};
		this.ganttFrame.classList.add("ia-gantt-split-mode");
		barNode.classList.add("split-target");
		const onMove = (event) => this.updateVisualSplit(event);
		const onClick = (event) => this.confirmVisualSplit(event);
		const onKeyDown = (event) => {
			if (event.key === "Escape") {
				event.preventDefault();
				this.cleanupVisualSplit();
				injection_aps.ui.set_feedback(this.feedback, __("Visual split cancelled."));
			}
		};
		barNode.addEventListener("pointermove", onMove);
		barNode.addEventListener("mousemove", onMove);
		barNode.addEventListener("click", onClick, true);
		document.addEventListener("keydown", onKeyDown);
		this.visualSplitCleanup = () => {
			barNode.removeEventListener("pointermove", onMove);
			barNode.removeEventListener("mousemove", onMove);
			barNode.removeEventListener("click", onClick, true);
			document.removeEventListener("keydown", onKeyDown);
			this.visualSplitCleanup = null;
		};
		this.updateVisualSplitAtClientX((barNode.getBoundingClientRect().left + barNode.getBoundingClientRect().right) / 2);
		this.scrollBarIntoView(barNode);
		injection_aps.ui.set_feedback(this.feedback, __("Visual cut mode: move over the segment and click where to cut. Press Esc to cancel."));
	}

	cleanupVisualSplit() {
		if (this.visualSplitCleanup) {
			this.visualSplitCleanup();
		}
		const state = this.splitState;
		if (state && state.barNode) {
			state.barNode.classList.remove("split-target");
		}
		if (state && state.cutNode && state.cutNode.parentNode) {
			state.cutNode.parentNode.removeChild(state.cutNode);
		}
		if (state && state.tooltipNode && state.tooltipNode.parentNode) {
			state.tooltipNode.parentNode.removeChild(state.tooltipNode);
		}
		if (this.ganttFrame) {
			this.ganttFrame.classList.remove("ia-gantt-split-mode");
		}
		this.splitState = null;
	}

	getVisualSplitSnapMs(durationMs) {
		if (durationMs <= 0) {
			return 60 * 60 * 1000;
		}
		const coarseSnap = this.getSnapMs();
		const visualSnap = Math.max(5 * 60 * 1000, Math.floor(durationMs / 12));
		return Math.min(coarseSnap, visualSnap);
	}

	snapVisualSplitMs(value, startMs, endMs) {
		const durationMs = Math.max(endMs - startMs, 1);
		const snapMs = this.getVisualSplitSnapMs(durationMs);
		const minGapMs = Math.max(5 * 60 * 1000, Math.min(snapMs, Math.floor(durationMs / 4)));
		const minimumValue = startMs + minGapMs;
		const maximumValue = endMs - minGapMs;
		if (maximumValue <= minimumValue) {
			return startMs + durationMs / 2;
		}
		const rounded = Math.round(value / snapMs) * snapMs;
		return Math.min(Math.max(rounded, minimumValue), maximumValue);
	}

	updateVisualSplit(event) {
		if (!this.splitState) {
			return;
		}
		event.preventDefault();
		event.stopPropagation();
		this.updateVisualSplitAtClientX(event.clientX);
	}

	updateVisualSplitAtClientX(clientX) {
		const state = this.splitState;
		if (!state || !state.barNode) {
			return;
		}
		const rect = state.barNode.getBoundingClientRect();
		const ratio = Math.min(Math.max((clientX - rect.left) / Math.max(rect.width, 1), 0), 1);
		const rawCutMs = state.startMs + (state.endMs - state.startMs) * ratio;
		const cutMs = this.snapVisualSplitMs(rawCutMs, state.startMs, state.endMs);
		const cutRatio = Math.min(Math.max((cutMs - state.startMs) / Math.max(state.endMs - state.startMs, 1), 0), 1);
		const firstQty = state.plannedQty ? state.plannedQty * cutRatio : 0;
		const secondQty = Math.max((state.plannedQty || 0) - firstQty, 0);
		state.cutMs = cutMs;
		state.cutNode.style.left = `${cutRatio * 100}%`;
		state.tooltipNode.style.left = `${cutRatio * 100}%`;
		const cutLabel = frappe.datetime.str_to_user(this.formatServerDatetime(new Date(cutMs)));
		const qtyLabel = state.plannedQty
			? `${injection_aps.ui.format_number(firstQty)} / ${injection_aps.ui.format_number(secondQty)}`
			: __("Click to cut");
		state.tooltipNode.innerHTML = `
			<span>${injection_aps.ui.escape(cutLabel)}</span>
			<span>${injection_aps.ui.escape(qtyLabel)}</span>
		`;
	}

	renderSegmentSplitPreview(preview) {
		const rows = (preview && preview.proposed_segments ? preview.proposed_segments : [])
			.map(
				(row) => `
					<div class="ia-confirm-row">
						${__("Split Part")} ${row.split_index}:
						${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.start_time))}
						-
						${injection_aps.ui.escape(injection_aps.ui.format_datetime(row.end_time))}
						/
						${injection_aps.ui.escape(injection_aps.ui.format_number(row.planned_qty || 0))}
					</div>
				`
			)
			.join("");
		return `<div class="ia-confirm-summary">${rows || `<div class="ia-muted">${__("No split preview was generated.")}</div>`}</div>`;
	}

	async confirmVisualSplit(event) {
		if (!this.splitState || !this.splitState.cutMs) {
			return;
		}
		event.preventDefault();
		event.stopPropagation();
		this.__suppressBarClickUntil = Date.now() + 500;
		const state = this.splitState;
		const cutTime = this.formatServerDatetime(new Date(state.cutMs));
		const cutLabel = frappe.datetime.str_to_user(cutTime);
		const args = {
			segment_name: state.segmentName,
			split_time: cutTime,
			split_reason: "Visual Cut",
		};
		this.cleanupVisualSplit();
		const preview = await injection_aps.ui.xcall(
			{
				message: __("Previewing visual segment split..."),
				busy_key: `visual-split-preview:${state.segmentName}`,
				feedback_target: this.feedback,
				success_feedback: __("Visual split preview is ready."),
			},
			"injection_aps.api.app.preview_segment_split",
			args
		);
		if (!preview || !preview.allowed) {
			this.showManualAdjustmentBlocked(preview, __("Split Blocked"));
			return;
		}
		if (!this.canApplySegmentSplit()) {
			frappe.msgprint({
				title: __("Split Preview"),
				message: this.renderSegmentSplitPreview(preview),
				wide: true,
			});
			injection_aps.ui.set_feedback(this.feedback, __("Split preview is ready, but this user cannot apply the split."), "warning");
			return;
		}
		frappe.confirm(
			`${__("Cut segment at {0}?").replace("{0}", injection_aps.ui.escape(cutLabel))}${this.renderSegmentSplitPreview(preview)}`,
			async () => {
				const response = await injection_aps.ui.xcall(
					{
						message: __("Applying visual segment split..."),
						success_message: __("Visual segment split applied."),
						busy_key: `visual-split-apply:${state.segmentName}`,
						feedback_target: this.feedback,
						success_feedback: __("Visual segment split applied. Refreshing Gantt..."),
					},
					"injection_aps.api.app.apply_segment_split",
					args
				);
				if (response) {
					await this.refresh();
				}
			}
		);
	}

	openDowntimeDialog(defaultWorkstation) {
		const runName = this.runField.get_value();
		const lane = ((this.data && this.data.lanes) || []).find((row) => row.workstation === defaultWorkstation) || {};
		const defaultPlantFloor = lane.plant_floor || ((this.data && this.data.selected_plant_floors && this.data.selected_plant_floors[0]) || "");
		const dialog = new frappe.ui.Dialog({
			title: __("Add Downtime Window"),
			fields: [
				{ fieldtype: "Select", fieldname: "scope", label: __("Scope"), options: ["Plant Floor", "Workstation", "Company"].join("\n"), default: defaultWorkstation ? "Workstation" : "Plant Floor", reqd: 1 },
				{ fieldtype: "Link", fieldname: "plant_floor", label: __("Plant Floor"), options: "Plant Floor", default: defaultPlantFloor },
				{ fieldtype: "Link", fieldname: "workstation", label: __("Workstation"), options: "Workstation", default: defaultWorkstation || "" },
				{ fieldtype: "Datetime", fieldname: "start_time", label: __("Start Time"), reqd: 1 },
				{ fieldtype: "Datetime", fieldname: "end_time", label: __("End Time"), reqd: 1 },
				{ fieldtype: "Data", fieldname: "reason", label: __("Reason") },
			],
			primary_action_label: __("Create and Preview"),
			primary_action: async () => {
				const values = dialog.get_values();
				if (!values) {
					return;
				}
				const response = await injection_aps.ui.xcall(
					{
						message: __("Creating downtime window..."),
						success_message: __("Downtime window created."),
						busy_key: "downtime-create",
						feedback_target: this.feedback,
						success_feedback: __("Downtime window saved."),
					},
					"injection_aps.api.app.create_or_update_downtime_window",
					{
						scope: values.scope,
						plant_floor: values.plant_floor,
						workstation: values.workstation,
						start_time: values.start_time,
						end_time: values.end_time,
						reason: values.reason,
						planning_run: runName,
					}
				);
				dialog.hide();
				if (response && response.impact_preview) {
					await this.showImpactPreview({
						downtimeWindow: response.downtime_window,
						preview: response.impact_preview,
					});
				}
				await this.refresh();
			},
		});
		dialog.show();
	}

	async refresh() {
		injection_aps.ui.ensure_styles();
		const runName = this.runField.get_value();
		if (!runName) {
			const emptyData = await frappe.xcall("injection_aps.api.app.get_release_center_data", {});
			this.runBody.style.display = "none";
			injection_aps.ui.render_run_empty_state(this.emptyStateHost, {
				title: __("No APS Run Selected"),
				description: __("Board must be bound to a single APS run. Select an APS run first before viewing the Gantt schedule and risk segments."),
				recent_runs: ((emptyData && emptyData.recent_runs) || []).map((row) => Object.assign({}, row, { route: row.gantt_route || row.route })),
				console_route: "aps-run-console",
			});
			return;
		}
		this.runBody.style.display = "";
		this.emptyStateHost.innerHTML = "";

		injection_aps.ui.set_feedback(this.feedback, __("Loading board..."));
		try {
			this.data = await frappe.xcall("injection_aps.api.app.get_schedule_gantt_data", {
				run_name: runName,
			});
			injection_aps.ui.render_run_context(this.runContextHost, this.data.run_context || this.data.run || null);
			injection_aps.ui.render_status_line(this.statusHost, this.data.run_context || this.data.run || null);
			this.renderBlockedResults(this.data.blocked_results || []);
			this.renderGantt(this.data.tasks || []);
			this.bindShellZoom();
			injection_aps.ui.set_feedback(this.feedback, __("Board refreshed."));
		} catch (error) {
			console.error(error);
			injection_aps.ui.set_feedback(this.feedback, __("Failed to load board."), "error");
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
										${row.diagnostic_summary ? `<span class="ia-muted">${injection_aps.ui.escape(injection_aps.ui.shorten(row.diagnostic_summary || "", 72))}</span>` : ""}
									</span>
									<span class="ia-risk-side">
										${row.requested_date ? `<span>${injection_aps.ui.escape(injection_aps.ui.format_date(row.requested_date))}</span>` : ""}
										${row.unscheduled_qty ? `<span>${injection_aps.ui.escape(injection_aps.ui.format_number(row.unscheduled_qty))}</span>` : ""}
										${(row.exception_types || []).slice(0, 1).map((flag) => `<span class="ia-risk-badge">${injection_aps.ui.escape(injection_aps.ui.translate(flag))}</span>`).join("")}
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
		injection_aps.ui.add_click_listener(exportId, () => {
			this.exportRows(
				__("Blocked / Risk Demands"),
				"aps_blocked_risk_demands",
				[
					{ label: __("Result", null, "Injection APS"), fieldname: "name" },
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
				rows.map((row) => {
					return Object.assign({}, row, {
						exception_summary: (row.exception_types || []).join(", "),
					});
				}),
				__("Demands that are still blocked or high-risk in the current APS run.")
			);
		});
	}

	getFilteredTasks(tasks) {
		const mode = this.viewField.get_value() || "Machine";
		if (mode === "Risk") {
			return tasks.filter((task) => {
				const risk = (task.custom_class || "").replace("ia-risk-", "");
				return ["attention", "critical", "blocked"].includes(risk) || !!injection_aps.ui.get_value(task, "details.risk_flags", "");
			});
		}
		if (mode === "Locked") {
			return tasks.filter((task) => Number(injection_aps.ui.get_value(task, "details.is_locked", 0) || 0) === 1);
		}
		return tasks;
	}

	getLaneKey(task) {
		const mode = this.viewField.get_value() || "Machine";
		if (mode === "Mold") {
			return injection_aps.ui.get_value(task, "details.mould_reference", "") || __("No Mold");
		}
		return injection_aps.ui.get_value(task, "details.workstation", "") || __("Unknown");
	}

	renderGantt(tasks) {
		this.cleanupVisualSplit();
		const filtered = this.getFilteredTasks(tasks);
		const parsedTasks = filtered
			.map((task) => {
				const startDate = frappe.datetime.str_to_obj(task.start);
				const endDate = frappe.datetime.str_to_obj(task.end);
				if (!(startDate instanceof Date) || Number.isNaN(startDate.getTime()) || !(endDate instanceof Date) || Number.isNaN(endDate.getTime())) {
					return null;
				}
				return Object.assign({}, task, {
					startDate,
					endDate,
				});
			})
			.filter(Boolean);
		const laneRows = this.buildLaneRows(parsedTasks);
		if (!parsedTasks.length && !laneRows.length) {
			injection_aps.ui.render_cards(this.summary, [
				{ label: __("Segments"), value: 0, note: __("No schedule segments are available in the current view.") },
			]);
			this.tools.innerHTML = "";
			this.timeline.innerHTML = "";
			this.grid.innerHTML = `<div class="ia-muted">${__("No visible schedule segments were generated for the current APS run.")}</div>`;
			return;
		}
		const laneTaskList = [];
		laneRows.forEach((lane) => {
			(lane.tasks || []).forEach((task) => laneTaskList.push(task));
		});
		const taskStartTimes = laneTaskList.map((task) => task.startDate.getTime());
		const taskEndTimes = laneTaskList.map((task) => task.endDate.getTime());
		const fallbackStart = new Date().getTime();
		const minTime = taskStartTimes.length ? Math.min.apply(Math, taskStartTimes) : fallbackStart;
		const maxTime = taskEndTimes.length ? Math.max.apply(Math, taskEndTimes) : fallbackStart + 86400000;
		const fullTimelineStart = this.floorToDay(minTime);
		const fullTimelineEnd = this.ceilToDay(maxTime);
		let timelineStart = fullTimelineStart;
		let timelineEnd = fullTimelineEnd;
		if (this.focusWindow && this.focusWindow.start < this.focusWindow.end) {
			timelineStart = Math.max(fullTimelineStart, this.floorToDay(this.focusWindow.start));
			timelineEnd = Math.min(fullTimelineEnd, this.ceilToDay(this.focusWindow.end));
			if (timelineEnd <= timelineStart) {
				timelineStart = fullTimelineStart;
				timelineEnd = fullTimelineEnd;
				this.focusWindow = null;
			}
		}
		const span = Math.max(timelineEnd - timelineStart, 1);
		const blockedCount = (this.data && this.data.blocked_results ? this.data.blocked_results : []).length;
		const timelineWidth = Math.max(960, Math.ceil(((timelineEnd - timelineStart) / 86400000) * 180 * this.zoomFactor));
		this.timelineMeta = { start: timelineStart, end: timelineEnd, span, fullStart: fullTimelineStart, fullEnd: fullTimelineEnd, width: timelineWidth };

		injection_aps.ui.render_cards(this.summary, [
			{ label: __("Segments"), value: parsedTasks.length },
			{ label: __("Machines / Lanes"), value: laneRows.length },
			{ label: __("Days"), value: Math.max(1, Math.round((timelineEnd - timelineStart) / 86400000)) },
			{ label: __("Blocking"), value: blockedCount },
		]);
		this.renderTimeline(timelineStart, timelineEnd, span, timelineWidth);
		this.renderGanttTools();

		let lastPlantFloor = null;
		this.grid.innerHTML = laneRows
			.map((lane) => {
				const parts = [];
				if ((this.viewField.get_value() || "Machine") === "Machine" && lane.plant_floor !== lastPlantFloor) {
					lastPlantFloor = lane.plant_floor;
					parts.push(`<div class="ia-gantt-group">${injection_aps.ui.escape(lane.plant_floor || __("Unknown Plant Floor"))}</div>`);
				}
				const dividers = this.buildDividers(timelineStart, timelineEnd, span);
				const downtimeBlocks = this.buildDowntimeBlocks(lane, timelineStart, timelineEnd, span);
				const stacks = this.buildSubrows(lane.tasks || []);
				const trackHeight = Math.max(50, 8 + stacks.length * 46);
				const bars = [];
				stacks.forEach((stack, stackIndex) => {
					stack.forEach((task) => {
						const clipped = this.clipTaskToWindow(task, timelineStart, timelineEnd);
						if (!clipped) {
							return;
						}
						const details = task.details || {};
						const compactBar = clipped.width < 12;
						const tone = (task.custom_class || "").replace("ia-risk-", "");
						const markers = [
							details.copy_mold_parallel ? "B" : "",
							details.family_mold_result ? "F" : "",
							details.is_locked ? "L" : "",
						].filter(Boolean);
						const riskFlagList = (details.risk_badges || []).concat(String(details.risk_flags || "").split("\n").filter(Boolean));
						const riskFlags = Array.from(new Set(riskFlagList));
						const barClass = ["ia-gantt-bar", tone, details.is_locked ? "locked" : "", compactBar ? "compact" : ""].filter(Boolean).join(" ");
						const isDragLocked =
							!this.canEditManualSchedule() ||
							(this.viewField.get_value() || "Machine") !== "Machine" ||
							details.segment_kind === "Family Co-Product" ||
							Number(details.is_locked || 0) === 1 ||
							["Applied", "Completed"].includes(details.segment_status);
						const isSplitLocked =
							!this.canPreviewSegmentSplit() ||
							(this.viewField.get_value() || "Machine") !== "Machine" ||
							!details.segment_name ||
							details.segment_kind === "Family Co-Product" ||
							Number(details.is_locked || 0) === 1 ||
							["Applied", "Completed"].includes(details.segment_status);
						const title = details.item_name || details.item_code || "";
						const segmentLabel = details.segment_name || "";
						const metaParts = [
							injection_aps.ui.format_number(details.planned_qty || 0),
							details.mould_reference || "-",
							details.customer_reference || "",
						].filter(Boolean);
						const visibleRiskFlags = riskFlags.slice(0, compactBar ? 1 : 2);
						bars.push(`
							<div
								class="${barClass}"
								style="left:${clipped.left}%; width:${clipped.width}%; top:${4 + stackIndex * 44}px;"
								data-segment-name="${injection_aps.ui.escape(details.segment_name || "")}"
								data-result-name="${injection_aps.ui.escape(details.result_name || "")}"
								data-workstation="${injection_aps.ui.escape(details.workstation || "")}"
								data-start-ms="${task.startDate.getTime()}"
								data-end-ms="${task.endDate.getTime()}"
								data-planned-qty="${Number(details.planned_qty || 0)}"
								data-segment-kind="${injection_aps.ui.escape(details.segment_kind || "")}"
								data-segment-status="${injection_aps.ui.escape(details.segment_status || "")}"
								data-is-locked="${Number(details.is_locked || 0)}"
								data-draggable="${isDragLocked ? "0" : "1"}"
								data-can-split="${isSplitLocked ? "0" : "1"}"
								draggable="false"
							>
								<div class="ia-gantt-title">
									<span class="ia-gantt-code">${injection_aps.ui.escape(details.item_code || "")}</span>
									${compactBar ? "" : `<span class="ia-gantt-name">${injection_aps.ui.escape(injection_aps.ui.shorten(title, 24))}</span>`}
									${segmentLabel ? `<span class="ia-gantt-segment-tag" title="${injection_aps.ui.escape(segmentLabel)}">${injection_aps.ui.escape(segmentLabel)}</span>` : ""}
								</div>
								<div class="ia-gantt-meta-line">
									<span class="ia-gantt-meta-text">${injection_aps.ui.escape(metaParts.join(" | "))}</span>
									<span class="ia-gantt-inline-flags">
										${markers.map((flag) => `<span class="ia-gantt-flag blue">${flag}</span>`).join("")}
										${visibleRiskFlags.map((flag) => `<span class="ia-gantt-flag ${String(flag).includes("FDA") ? "red" : "orange"}">${injection_aps.ui.escape(flag)}</span>`).join("")}
									</span>
								</div>
								${isDragLocked ? "" : `<span class="ia-gantt-resize-handle" data-resize-segment="${injection_aps.ui.escape(details.segment_name || "")}" title="${injection_aps.ui.escape(__("Resize Segment Duration"))}"></span>`}
							</div>
						`);
					});
				});

				parts.push(`
					<div class="ia-gantt-row">
						<div class="ia-gantt-label">
							<div>${injection_aps.ui.escape(lane.label)}</div>
							<div class="ia-muted">${lane.tasks.length} ${__("segments")}</div>
						</div>
						<div class="ia-gantt-track" style="min-height:${trackHeight}px; min-width:${timelineWidth}px;" data-lane="${injection_aps.ui.escape(lane.key)}" data-workstation="${this.viewField.get_value() === "Mold" ? "" : injection_aps.ui.escape(lane.key)}">
							${dividers}
							${downtimeBlocks}
							${bars.join("")}
						</div>
					</div>
				`);
				return parts.join("");
			})
			.join("");

		this.bindGanttInteractions();
		this.applySegmentSearchHighlight();
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

	renderTimeline(timelineStart, timelineEnd, span, timelineWidth) {
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
			<div class="ia-gantt-timeline-track" style="min-width:${timelineWidth}px;">${cells.join("")}</div>
		`;
		this.bindTimelineZoom();
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

	buildDowntimeBlocks(lane, timelineStart, timelineEnd, span) {
		if ((this.viewField.get_value() || "Machine") !== "Machine") {
			return "";
		}
		return ((this.data && this.data.downtime_windows) || [])
			.filter((row) => this.downtimeAppliesToLane(row, lane))
			.map((row) => {
				const startDate = frappe.datetime.str_to_obj(row.start_time);
				const endDate = frappe.datetime.str_to_obj(row.end_time);
				if (!(startDate instanceof Date) || !(endDate instanceof Date)) {
					return "";
				}
				const startMs = Math.max(startDate.getTime(), timelineStart);
				const endMs = Math.min(endDate.getTime(), timelineEnd);
				if (endMs <= startMs) {
					return "";
				}
				const left = ((startMs - timelineStart) / span) * 100;
				const width = ((endMs - startMs) / span) * 100;
				const title = [row.reason || __("Downtime"), injection_aps.ui.format_datetime(row.start_time), injection_aps.ui.format_datetime(row.end_time)]
					.filter(Boolean)
					.join(" | ");
				return `<div class="ia-gantt-downtime" style="left:${left}%; width:${width}%;" title="${injection_aps.ui.escape(title)}"></div>`;
			})
			.join("");
	}

	downtimeAppliesToLane(row, lane) {
		const scope = row.scope || "Plant Floor";
		if (scope === "Company") {
			return true;
		}
		if (scope === "Workstation") {
			return row.workstation === lane.key;
		}
		return !row.plant_floor || row.plant_floor === lane.plant_floor;
	}

	buildLaneRows(tasks) {
		const mode = this.viewField.get_value() || "Machine";
		if (mode !== "Machine") {
			const grouped = {};
			tasks.forEach((task) => {
				const key = this.getLaneKey(task);
				if (!grouped[key]) {
					grouped[key] = [];
				}
				grouped[key].push(task);
			});
			return Object.keys(grouped).map((key) => ({ key, label: key, plant_floor: "", tasks: grouped[key] }));
		}
		const taskMap = {};
		(tasks || []).forEach((task) => {
			const key = injection_aps.ui.get_value(task, "details.workstation", "") || __("Unknown");
			if (!taskMap[key]) {
				taskMap[key] = [];
			}
			taskMap[key].push(task);
		});
		const rows = [];
		(this.data.lanes || []).forEach((lane) => {
			if (!lane.workstation) {
				return;
			}
			rows.push({
				key: lane.workstation,
				label: lane.workstation,
				plant_floor: lane.plant_floor || "",
				tasks: taskMap[lane.workstation] || [],
			});
			delete taskMap[lane.workstation];
		});
		Object.keys(taskMap).forEach((workstation) => {
			rows.push({
				key: workstation,
				label: workstation,
				plant_floor: injection_aps.ui.get_value(taskMap[workstation][0], "details.plant_floor", ""),
				tasks: taskMap[workstation],
			});
		});
		return rows.sort((left, right) => {
			if ((left.plant_floor || "") !== (right.plant_floor || "")) {
				return (left.plant_floor || "").localeCompare(right.plant_floor || "");
			}
			return (left.label || "").localeCompare(right.label || "");
		});
	}

	buildSubrows(tasks) {
		const stacks = [];
		const sorted = (tasks || []).slice().sort((left, right) => left.startDate - right.startDate || left.endDate - right.endDate);
		sorted.forEach((task) => {
			let placed = false;
			for (let index = 0; index < stacks.length; index += 1) {
				const stack = stacks[index];
				const last = stack[stack.length - 1];
				if (task.startDate.getTime() >= last.endDate.getTime()) {
					stack.push(task);
					placed = true;
					break;
				}
			}
			if (!placed) {
				stacks.push([task]);
			}
		});
		return stacks.length ? stacks : [[]];
	}

	clipTaskToWindow(task, timelineStart, timelineEnd) {
		const start = Math.max(task.startDate.getTime(), timelineStart);
		const end = Math.min(task.endDate.getTime(), timelineEnd);
		if (end <= start) {
			return null;
		}
		return {
			left: ((start - timelineStart) / (timelineEnd - timelineStart)) * 100,
			width: Math.max(((end - start) / (timelineEnd - timelineStart)) * 100, 2),
		};
	}

	renderGanttTools() {
		const zoomPercent = Math.round(this.zoomFactor * 100);
		this.tools.innerHTML = `
			<div class="ia-gantt-tools-main">
				<div class="ia-chip-row ia-legend">
					<span class="ia-chip">${__("Blue")}: ${__("Normal")}</span>
					<span class="ia-chip">${__("Yellow")}: ${__("Attention")}</span>
					<span class="ia-chip">${__("Red")}: ${__("Critical / Blocking")}</span>
					<span class="ia-chip">${__("B")}: ${__("Copy Mold Parallelized")}</span>
					<span class="ia-chip">${__("F")}: ${__("Family Mold Co-Production")}</span>
					<span class="ia-chip">${__("L")}: ${__("Locked")}</span>
					<span class="ia-chip red">${__("FDA")}: ${__("Risk / Override")}</span>
					<span class="ia-chip">${__("Drag on the timeline to focus a day or date range")}</span>
				</div>
			</div>
			<div class="ia-gantt-tool-actions">
				<div class="ia-gantt-search">
					<input type="text" class="form-control input-xs" data-segment-search="1" placeholder="${injection_aps.ui.escape(__("Search Segment"))}" value="${injection_aps.ui.escape(this.segmentSearchTerm || "")}">
					<button class="btn btn-xs btn-default" data-run-segment-search="1">${__("Find")}</button>
					${this.segmentSearchTerm ? `<button class="btn btn-xs btn-default" data-clear-segment-search="1">${__("Clear")}</button>` : ""}
				</div>
				<button class="btn btn-xs btn-default" data-zoom-out="1">-</button>
				<span class="ia-chip blue">${zoomPercent}%</span>
				<button class="btn btn-xs btn-default" data-zoom-in="1">+</button>
				${injection_aps.ui.can_run_action("create_or_update_downtime_window") ? injection_aps.ui.icon_button("calendar", __("Add Downtime Window"), { "data-add-downtime": "1" }) : ""}
				${injection_aps.ui.can_run_action("preview_schedule_impact") ? injection_aps.ui.icon_button("search", __("Preview Downtime Impact"), { "data-preview-impact": "1" }) : ""}
				${injection_aps.ui.icon_button(this.isChartFullscreen ? "collapse" : "expand", this.isChartFullscreen ? __("Exit Chart Fullscreen") : __("Chart Fullscreen"), { "data-toggle-gantt-fullscreen": "1" })}
				<button class="ia-icon-btn" type="button" title="${injection_aps.ui.escape(__("Refresh Board"))}" aria-label="${injection_aps.ui.escape(__("Refresh Board"))}" data-refresh-gantt="1">↻</button>
				${this.focusWindow ? `<button class="btn btn-xs btn-default" data-reset-gantt-focus="1">${__("Reset Date Focus")}</button>` : ""}
			</div>
		`;
		if (this.ganttFrameClose) {
			this.ganttFrameClose.innerHTML = this.isChartFullscreen
				? injection_aps.ui.icon_button("x", __("Close Chart Fullscreen"), { "data-close-gantt-fullscreen": "1" })
				: "";
			const closeButton = this.ganttFrameClose.querySelector("[data-close-gantt-fullscreen='1']");
			if (closeButton) {
				closeButton.addEventListener("click", () => {
					this.isChartFullscreen = false;
					if (this.ganttFrame) {
						this.ganttFrame.classList.remove("ia-gantt-frame-fullscreen");
					}
					this.renderGanttTools();
				});
			}
		}
		const fullscreenButton = this.tools.querySelector("[data-toggle-gantt-fullscreen='1']");
		if (fullscreenButton) {
			fullscreenButton.addEventListener("click", () => this.toggleChartFullscreen());
		}
		const refreshButton = this.tools.querySelector("[data-refresh-gantt='1']");
		if (refreshButton) {
			refreshButton.addEventListener("click", () => this.refresh());
		}
		const addDowntimeButton = this.tools.querySelector("[data-add-downtime='1']");
		if (addDowntimeButton) {
			addDowntimeButton.addEventListener("click", () => this.openDowntimeDialog(""));
		}
		const previewImpactButton = this.tools.querySelector("[data-preview-impact='1']");
		if (previewImpactButton) {
			previewImpactButton.addEventListener("click", () => this.showImpactPreview({}));
		}
		const zoomOutButton = this.tools.querySelector("[data-zoom-out='1']");
		if (zoomOutButton) {
			zoomOutButton.addEventListener("click", () => this.adjustZoom(-0.1));
		}
		const zoomInButton = this.tools.querySelector("[data-zoom-in='1']");
		if (zoomInButton) {
			zoomInButton.addEventListener("click", () => this.adjustZoom(0.1));
		}
		const searchInput = this.tools.querySelector("[data-segment-search='1']");
		const runSearchButton = this.tools.querySelector("[data-run-segment-search='1']");
		const clearSearchButton = this.tools.querySelector("[data-clear-segment-search='1']");
		const runSearch = () => {
			this.segmentSearchTerm = ((searchInput && searchInput.value) || "").trim();
			this.renderGanttTools();
			this.applySegmentSearchHighlight();
		};
		if (searchInput) {
			searchInput.addEventListener("keydown", (event) => {
				if (event.key === "Enter") {
					event.preventDefault();
					runSearch();
				}
			});
		}
		if (runSearchButton) {
			runSearchButton.addEventListener("click", runSearch);
		}
		if (clearSearchButton) {
			clearSearchButton.addEventListener("click", () => {
				this.segmentSearchTerm = "";
				if (searchInput) {
					searchInput.value = "";
				}
				this.applySegmentSearchHighlight();
				this.renderGanttTools();
			});
		}
		const resetButton = this.tools.querySelector("[data-reset-gantt-focus='1']");
		if (resetButton) {
			resetButton.addEventListener("click", () => {
				this.focusWindow = null;
				this.refresh();
			});
		}
	}

	toggleChartFullscreen() {
		this.isChartFullscreen = !this.isChartFullscreen;
		if (this.ganttFrame) {
			this.ganttFrame.classList.toggle("ia-gantt-frame-fullscreen", this.isChartFullscreen);
		}
		this.renderGanttTools();
	}

	adjustZoom(delta) {
		const next = Math.max(0.1, Math.min(3, Number((this.zoomFactor + delta).toFixed(2))));
		if (next === this.zoomFactor) {
			return;
		}
		this.zoomFactor = next;
		if (this.data) {
			this.renderGantt(this.data.tasks || []);
		}
	}

	applySegmentSearchHighlight() {
		if (!this.grid) {
			return;
		}
		const term = (this.segmentSearchTerm || "").trim().toLowerCase();
		const bars = Array.from(this.grid.querySelectorAll(".ia-gantt-bar"));
		let firstMatch = null;
		bars.forEach((node) => {
			const segmentName = String(node.dataset.segmentName || "").toLowerCase();
			const matched = !!term && segmentName.includes(term);
			node.classList.toggle("segment-match", matched);
			if (matched && !firstMatch) {
				firstMatch = node;
			}
		});
		if (!term) {
			injection_aps.ui.set_feedback(this.feedback, __("Board refreshed."));
			return;
		}
		if (!firstMatch) {
			injection_aps.ui.set_feedback(this.feedback, __("No matching segment was found."), "warning");
			return;
		}
		injection_aps.ui.set_feedback(this.feedback, __("Matching segment highlighted."));
		this.scrollBarIntoView(firstMatch);
	}

	scrollBarIntoView(barNode) {
		if (!barNode || !this.ganttShell) {
			return;
		}
		const shellRect = this.ganttShell.getBoundingClientRect();
		const barRect = barNode.getBoundingClientRect();
		const horizontalPadding = 96;
		const verticalPadding = 48;
		if (barRect.left < shellRect.left + horizontalPadding) {
			this.ganttShell.scrollLeft += barRect.left - shellRect.left - horizontalPadding;
		} else if (barRect.right > shellRect.right - horizontalPadding) {
			this.ganttShell.scrollLeft += barRect.right - shellRect.right + horizontalPadding;
		}
		if (barRect.top < shellRect.top + verticalPadding) {
			this.ganttShell.scrollTop += barRect.top - shellRect.top - verticalPadding;
		} else if (barRect.bottom > shellRect.bottom - verticalPadding) {
			this.ganttShell.scrollTop += barRect.bottom - shellRect.bottom + verticalPadding;
		}
	}

	bindShellZoom() {
		if (!this.ganttShell || this.__boundShellZoom) {
			return;
		}
		this.__boundShellZoom = true;
		this.ganttShell.addEventListener(
			"wheel",
			(event) => {
				if (!event.ctrlKey) {
					return;
				}
				event.preventDefault();
				this.adjustZoom(event.deltaY > 0 ? -0.1 : 0.1);
			},
			{ passive: false }
		);
	}

	bindTimelineZoom() {
		const track = this.timeline.querySelector(".ia-gantt-timeline-track");
		if (!track || !this.timelineMeta) {
			return;
		}
		let anchorX = null;
		track.addEventListener("mousedown", (event) => {
			if (event.target.closest(".ia-gantt-timeline-cell")) {
				anchorX = event.clientX;
			}
		});
		track.addEventListener("mouseup", (event) => {
			if (anchorX == null) {
				return;
			}
			const rect = track.getBoundingClientRect();
			const startRatio = Math.min(Math.max((Math.min(anchorX, event.clientX) - rect.left) / rect.width, 0), 1);
			const endRatio = Math.min(Math.max((Math.max(anchorX, event.clientX) - rect.left) / rect.width, 0), 1);
			anchorX = null;
			if (Math.abs(endRatio - startRatio) < 0.06) {
				return;
			}
			this.focusWindow = {
				start: this.timelineMeta.start + this.timelineMeta.span * startRatio,
				end: this.timelineMeta.start + this.timelineMeta.span * endRatio,
			};
			this.refresh();
		});
	}

	getSplitHoverHandle() {
		if (this.splitHoverHandle) {
			return this.splitHoverHandle;
		}
		const handle = document.createElement("button");
		handle.type = "button";
		handle.className = "ia-gantt-split-handle";
		handle.setAttribute("aria-label", __("Cut Segment"));
		handle.setAttribute("title", __("Cut Segment"));
		handle.dataset.splitSegment = "";
		handle.innerHTML = injection_aps.ui.icon("scissors", "xs");
		["pointerdown", "mousedown"].forEach((eventName) => {
			handle.addEventListener(eventName, (event) => {
				event.preventDefault();
				event.stopPropagation();
			});
		});
		handle.addEventListener("mouseenter", () => {
			if (this.splitHoverHideTimer) {
				clearTimeout(this.splitHoverHideTimer);
				this.splitHoverHideTimer = null;
			}
		});
		handle.addEventListener("mouseleave", () => this.scheduleHideSplitHoverHandle());
		handle.addEventListener("click", (event) => {
			event.preventDefault();
			event.stopPropagation();
			this.beginVisualSplit(this.splitHoverBar);
		});
		handle.addEventListener("keydown", (event) => {
			if (!["Enter", " "].includes(event.key)) {
				return;
			}
			event.preventDefault();
			event.stopPropagation();
			this.beginVisualSplit(this.splitHoverBar);
		});
		this.splitHoverHandle = handle;
		return handle;
	}

	showSplitHoverHandle(barNode) {
		if (!this.canShowSplitHoverHandle(barNode)) {
			this.hideSplitHoverHandle();
			return;
		}
		if (this.splitHoverHideTimer) {
			clearTimeout(this.splitHoverHideTimer);
			this.splitHoverHideTimer = null;
		}
		const trackNode = barNode.closest(".ia-gantt-track");
		if (!trackNode) {
			this.hideSplitHoverHandle();
			return;
		}
		const handle = this.getSplitHoverHandle();
		this.splitHoverBar = barNode;
		handle.dataset.splitSegment = barNode.dataset.segmentName || "";
		const blockReason = this.getVisualSplitBlockReason(barNode);
		const canSplit = !blockReason;
		handle.classList.toggle("disabled", !canSplit);
		handle.setAttribute("aria-disabled", canSplit ? "false" : "true");
		handle.setAttribute("title", canSplit ? __("Cut Segment") : blockReason);
		handle.setAttribute("aria-label", canSplit ? __("Cut Segment") : blockReason);
		if (handle.parentNode !== trackNode) {
			trackNode.appendChild(handle);
		}
		const left = Math.max(4, Math.min(barNode.offsetLeft + 5, barNode.offsetLeft + Math.max(barNode.offsetWidth - 22, 4)));
		handle.style.left = `${left}px`;
		handle.style.top = `${barNode.offsetTop + 5}px`;
		handle.classList.add("visible");
		handle.hidden = false;
	}

	scheduleHideSplitHoverHandle(delay = 90) {
		if (this.splitHoverHideTimer) {
			clearTimeout(this.splitHoverHideTimer);
		}
		this.splitHoverHideTimer = setTimeout(() => this.hideSplitHoverHandle(), delay);
	}

	hideSplitHoverHandle() {
		if (this.splitHoverHideTimer) {
			clearTimeout(this.splitHoverHideTimer);
			this.splitHoverHideTimer = null;
		}
		if (this.splitHoverHandle) {
			this.splitHoverHandle.classList.remove("visible");
			this.splitHoverHandle.classList.remove("disabled");
			this.splitHoverHandle.setAttribute("aria-disabled", "false");
			this.splitHoverHandle.hidden = true;
			this.splitHoverHandle.dataset.splitSegment = "";
		}
		this.splitHoverBar = null;
	}

	bindGanttInteractions() {
		$(this.grid)
			.find(".ia-gantt-bar")
			.each((_, node) => {
				node.addEventListener("mouseenter", () => this.showSplitHoverHandle(node));
				node.addEventListener("mouseleave", (event) => {
					if (this.splitHoverHandle && this.splitHoverHandle.contains(event.relatedTarget)) {
						return;
					}
					this.scheduleHideSplitHoverHandle();
				});
				node.addEventListener("focusin", () => this.showSplitHoverHandle(node));
				node.addEventListener("focusout", (event) => {
					if (this.splitHoverHandle && this.splitHoverHandle.contains(event.relatedTarget)) {
						return;
					}
					this.scheduleHideSplitHoverHandle();
				});
				node.addEventListener("click", () => {
					if (this.splitState || (this.__suppressBarClickUntil && Date.now() < this.__suppressBarClickUntil)) {
						return;
					}
					this.openResultDrawer(node.dataset.resultName, node.dataset.segmentName);
				});
				node.addEventListener("contextmenu", (event) => this.openSegmentContextMenu(event, node));
				if (node.dataset.draggable === "1") {
					let lastPointerDownAt = 0;
					node.addEventListener("pointerdown", (event) => {
						if (this.splitState) {
							return;
						}
						if (event.target.closest("[data-split-segment]")) {
							return;
						}
						lastPointerDownAt = Date.now();
						if (event.target.closest("[data-resize-segment]")) {
							return;
						}
						this.beginPointerDrag(event, node);
					});
					node.addEventListener("mousedown", (event) => {
						if (this.splitState) {
							return;
						}
						if (event.target.closest("[data-split-segment]")) {
							return;
						}
						if (Date.now() - lastPointerDownAt < 80) {
							return;
						}
						if (event.target.closest("[data-resize-segment]")) {
							return;
						}
						this.beginPointerDrag(event, node);
					});
				}
			});
		$(this.grid)
			.find("[data-resize-segment]")
			.each((_, node) => {
				let lastPointerDownAt = 0;
				node.addEventListener("pointerdown", (event) => {
					lastPointerDownAt = Date.now();
					this.beginResize(event, node.dataset.resizeSegment);
				});
				node.addEventListener("mousedown", (event) => {
					if (Date.now() - lastPointerDownAt < 80) {
						return;
					}
					this.beginResize(event, node.dataset.resizeSegment);
				});
			});
	}

	attachInteractionListeners(startEvent, onMove, onUp, onKeyDown) {
		const usesMouseFallback = startEvent && startEvent.type === "mousedown";
		const moveType = usesMouseFallback ? "mousemove" : "pointermove";
		const upType = usesMouseFallback ? "mouseup" : "pointerup";
		document.addEventListener(moveType, onMove);
		document.addEventListener(upType, onUp);
		document.addEventListener("keydown", onKeyDown);
		this.interactionListenerCleanup = () => {
			document.removeEventListener(moveType, onMove);
			document.removeEventListener(upType, onUp);
			document.removeEventListener("keydown", onKeyDown);
			this.interactionListenerCleanup = null;
		};
	}

	detachInteractionListeners() {
		if (this.interactionListenerCleanup) {
			this.interactionListenerCleanup();
		}
	}

	openSegmentContextMenu(event, barNode) {
		event.preventDefault();
		event.stopPropagation();
		if (!barNode) {
			return;
		}
		const resultName = barNode.dataset.resultName || "";
		const segmentName = barNode.dataset.segmentName || "";
		const runName = this.runField.get_value() || injection_aps.ui.get_value(this.data, "run.name", "");
		injection_aps.ui.open_context_menu(
			[
				{
					label: __("View Detail"),
					icon: "file",
					handler: async () => this.openResultDrawer(resultName, segmentName),
				},
				{
					label: __("Split Segment"),
					icon: "scissors",
					disabled: !this.canVisualSplitSegment(barNode),
					handler: async () => this.beginVisualSplit(barNode),
				},
				{
					label: __("Split by Time / Qty"),
					icon: "edit",
					disabled: !this.canPreviewSegmentSplit(),
					handler: async () => this.openSegmentSplitDialog(segmentName),
				},
				{
					label: __("Preview Impact"),
					icon: "search",
					handler: async () => this.showImpactPreview({ segmentName }),
				},
				{
					label: __("Add Downtime Window"),
					icon: "calendar",
					disabled: !injection_aps.ui.can_run_action("create_or_update_downtime_window"),
					handler: async () => this.openDowntimeDialog(barNode.dataset.workstation || ""),
				},
				{
					label: __("Result", null, "Injection APS"),
					icon: "external-link",
					handler: async () => frappe.set_route("Form", "APS Schedule Result", resultName),
				},
				{
					label: __("Run", null, "Injection APS"),
					icon: "branch",
					handler: async () => {
						if (runName) {
							frappe.set_route("Form", "APS Planning Run", runName);
						}
					},
				},
				{
					label: __("Execution"),
					icon: "settings",
					handler: async () => {
						if (runName) {
							injection_aps.ui.go_to(`aps-release-center?run_name=${encodeURIComponent(runName)}`);
						}
					},
				},
			],
			{ x: event.clientX, y: event.clientY }
		);
	}

	beginPointerDrag(event, barNode) {
		if (!this.canEditManualSchedule()) {
			return;
		}
		if ((this.viewField.get_value() || "Machine") !== "Machine") {
			return;
		}
		if (event.button !== 0) {
			return;
		}
		event.preventDefault();
		event.stopPropagation();
		const trackNode = barNode.closest(".ia-gantt-track");
		if (!barNode || !trackNode || !this.timelineMeta) {
			return;
		}
		this.cleanupInteraction();
		const rect = barNode.getBoundingClientRect();
		const ghostNode = this.createGhostNode(barNode, trackNode);
		this.dragState = {
			mode: "move",
			segmentName: barNode.dataset.segmentName,
			barNode,
			ghostNode,
			trackNode,
			targetTrackNode: trackNode,
			initialClientX: event.clientX,
			initialClientY: event.clientY,
			initialOffsetX: event.clientX - rect.left,
			initialWidthPx: barNode.offsetWidth,
			trackWidthPx: trackNode.clientWidth,
			startMs: Number(barNode.dataset.startMs || 0),
			endMs: Number(barNode.dataset.endMs || 0),
			durationMs: Math.max(Number(barNode.dataset.endMs || 0) - Number(barNode.dataset.startMs || 0), this.getSnapMs()),
			targetWorkstation: barNode.dataset.workstation || "",
			targetBeforeSegmentName: null,
			moved: false,
		};
		barNode.classList.add("origin-dim");
		barNode.classList.add("dragging");
		if (typeof barNode.setPointerCapture === "function" && event.pointerId != null) {
			try {
				barNode.setPointerCapture(event.pointerId);
				this.dragState.pointerId = event.pointerId;
			} catch (captureError) {
				/* no-op */
			}
		}
		this.updateMovePreview(event);
		const onMove = (moveEvent) => {
			if (!this.dragState) {
				return;
			}
			this.updateMovePreview(moveEvent);
		};
		const onUp = async (upEvent) => {
			this.detachInteractionListeners();
			const state = this.dragState;
			if (!state) {
				return;
			}
			this.__suppressBarClickUntil = Date.now() + 300;
			const moved = state.moved;
			const segmentName = state.segmentName;
			const targetWorkstation = state.targetWorkstation;
			const beforeSegmentName = state.targetBeforeSegmentName;
			const targetStartMs = state.targetStartMs;
			this.cleanupInteraction();
			if (!moved) {
				await this.openResultDrawer(state.barNode.dataset.resultName, segmentName);
				return;
			}
			await this.handleDrop(segmentName, targetWorkstation, beforeSegmentName, targetStartMs);
		};
		const onKeyDown = (keyEvent) => {
			if (keyEvent.key === "Escape") {
				this.cleanupInteraction();
				this.detachInteractionListeners();
			}
		};
		this.attachInteractionListeners(event, onMove, onUp, onKeyDown);
	}

	beginResize(event, segmentName) {
		if (!this.canEditManualSchedule()) {
			return;
		}
		if ((this.viewField.get_value() || "Machine") !== "Machine") {
			return;
		}
		if (event.button !== 0) {
			return;
		}
		event.preventDefault();
		event.stopPropagation();
		const barNode = event.target.closest(".ia-gantt-bar");
		const trackNode = barNode ? barNode.closest(".ia-gantt-track") : null;
		if (!barNode || !trackNode || !this.timelineMeta) {
			return;
		}
		this.cleanupInteraction();
		const ghostNode = this.createGhostNode(barNode, trackNode);
		this.dragState = {
			mode: "resize",
			segmentName,
			barNode,
			ghostNode,
			trackNode,
			targetTrackNode: trackNode,
			initialClientX: event.clientX,
			initialWidthPx: barNode.offsetWidth,
			trackWidthPx: trackNode.clientWidth,
			startMs: Number(barNode.dataset.startMs || 0),
			endMs: Number(barNode.dataset.endMs || 0),
			targetWorkstation: barNode.dataset.workstation || "",
			moved: false,
		};
		barNode.classList.add("origin-dim");
		barNode.classList.add("dragging");
		if (typeof barNode.setPointerCapture === "function" && event.pointerId != null) {
			try {
				barNode.setPointerCapture(event.pointerId);
				this.dragState.pointerId = event.pointerId;
			} catch (captureError) {
				/* no-op */
			}
		}
		this.updateResizePreview(event);
		const onMove = (moveEvent) => {
			if (!this.dragState) {
				return;
			}
			this.updateResizePreview(moveEvent);
		};
		const onUp = async () => {
			this.detachInteractionListeners();
			const state = this.dragState;
			if (!state) {
				return;
			}
			const { segmentName: resizeSegmentName, targetWorkstation, targetEndMs } = state;
			this.cleanupInteraction();
			if (!state.moved || !targetEndMs) {
				return;
			}
			const targetEndTime = this.formatServerDatetime(new Date(targetEndMs));
			const preview = await injection_aps.ui.xcall(
				{
					message: __("Previewing segment resize..."),
					busy_key: `gantt-resize-preview:${resizeSegmentName}`,
					feedback_target: this.feedback,
					success_feedback: __("Resize preview is ready."),
				},
				"injection_aps.api.app.preview_manual_schedule_adjustment",
				{
					segment_name: resizeSegmentName,
					target_workstation: targetWorkstation,
					target_end_time: targetEndTime,
				}
			);
			if (!preview || !preview.allowed) {
				if (preview) {
					this.showManualAdjustmentBlocked(preview, __("Resize Blocked"));
				}
				await this.refresh();
				return;
			}
			const response = await injection_aps.ui.xcall(
				{
					message: __("Applying segment resize..."),
					success_message: __("Segment resized."),
					busy_key: `gantt-resize-apply:${resizeSegmentName}`,
					feedback_target: this.feedback,
					success_feedback: __("Segment resized. Refreshing Gantt..."),
				},
				"injection_aps.api.app.apply_manual_schedule_adjustment",
				{
					segment_name: resizeSegmentName,
					target_workstation: targetWorkstation,
					target_end_time: targetEndTime,
				}
			);
			if (!response) {
				await this.refresh();
				return;
			}
			await this.refresh();
		};
		const onKeyDown = (keyEvent) => {
			if (keyEvent.key === "Escape") {
				this.cleanupInteraction();
				this.detachInteractionListeners();
			}
		};
		this.attachInteractionListeners(event, onMove, onUp, onKeyDown);
	}

	createGhostNode(barNode, trackNode) {
		const ghostNode = document.createElement("div");
		ghostNode.className = "ia-gantt-ghost";
		ghostNode.style.top = barNode.style.top || "4px";
		ghostNode.style.left = barNode.style.left || "0%";
		ghostNode.style.width = barNode.style.width || "2%";
		trackNode.appendChild(ghostNode);
		return ghostNode;
	}

	cleanupInteraction() {
		this.detachInteractionListeners();
		const state = this.dragState;
		if (!state) {
			this.hideGuide();
			return;
		}
		if (state.barNode) {
			state.barNode.classList.remove("origin-dim");
			state.barNode.classList.remove("dragging");
			if (typeof state.barNode.releasePointerCapture === "function" && state.pointerId != null) {
				try {
					state.barNode.releasePointerCapture(state.pointerId);
				} catch (releaseError) {
					/* no-op */
				}
			}
		}
		if (state.targetTrackNode) {
			state.targetTrackNode.classList.remove("drag-target", "invalid-target");
		}
		if (state.ghostNode && state.ghostNode.parentNode) {
			state.ghostNode.parentNode.removeChild(state.ghostNode);
		}
		this.dragState = null;
		this.hideGuide();
	}

	getSnapMs() {
		if (this.zoomFactor <= 0.2) {
			return 24 * 60 * 60 * 1000;
		}
		if (this.zoomFactor <= 0.45) {
			return 6 * 60 * 60 * 1000;
		}
		return 60 * 60 * 1000;
	}

	snapMs(value, minimumValue, maximumValue) {
		const snapMs = this.getSnapMs();
		const rounded = Math.round(value / snapMs) * snapMs;
		return Math.min(Math.max(rounded, minimumValue), maximumValue);
	}

	findTrackByPoint(clientX, clientY) {
		const node = document.elementFromPoint(clientX, clientY);
		return node ? node.closest(".ia-gantt-track") : null;
	}

	findBeforeSegment(trackNode, targetStartMs, movingSegmentName) {
		if (!trackNode) {
			return null;
		}
		const bars = Array.from(trackNode.querySelectorAll(".ia-gantt-bar"))
			.filter((node) => node.dataset.segmentName && node.dataset.segmentName !== movingSegmentName)
			.map((node) => ({
				segmentName: node.dataset.segmentName,
				startMs: Number(node.dataset.startMs || 0),
			}))
			.sort((left, right) => left.startMs - right.startMs);
		const next = bars.find((row) => row.startMs >= targetStartMs);
		return next ? next.segmentName : null;
	}

	autoScrollShell(clientX) {
		if (!this.ganttShell) {
			return;
		}
		const rect = this.ganttShell.getBoundingClientRect();
		const threshold = 56;
		const maxStep = 28;
		if (clientX < rect.left + threshold) {
			const ratio = 1 - Math.max((clientX - rect.left) / threshold, 0);
			this.ganttShell.scrollLeft -= Math.ceil(maxStep * ratio);
		} else if (clientX > rect.right - threshold) {
			const ratio = 1 - Math.max((rect.right - clientX) / threshold, 0);
			this.ganttShell.scrollLeft += Math.ceil(maxStep * ratio);
		}
	}

	ensureGuideElements() {
		if (!this.ganttOverlay) {
			return {};
		}
		let line = this.ganttOverlay.querySelector(".ia-gantt-guide-line");
		let tooltip = this.ganttOverlay.querySelector(".ia-gantt-guide-tooltip");
		if (!line) {
			line = document.createElement("div");
			line.className = "ia-gantt-guide-line";
			this.ganttOverlay.appendChild(line);
		}
		if (!tooltip) {
			tooltip = document.createElement("div");
			tooltip.className = "ia-gantt-guide-tooltip";
			this.ganttOverlay.appendChild(tooltip);
		}
		return { line, tooltip };
	}

	updateGuide(trackNode, snappedMs, endMs, invalid) {
		if (!trackNode || !this.timelineMeta) {
			return;
		}
		const elements = this.ensureGuideElements();
		const line = elements.line;
		const tooltip = elements.tooltip;
		if (!line || !tooltip) {
			return;
		}
		const frameRect = this.ganttFrame.getBoundingClientRect();
		const trackRect = trackNode.getBoundingClientRect();
		const timelineRect = this.timeline.getBoundingClientRect();
		const gridRect = this.grid.getBoundingClientRect();
		const ratio = (snappedMs - this.timelineMeta.start) / Math.max(this.timelineMeta.span, 1);
		const left = trackRect.left - frameRect.left + trackRect.width * ratio;
		line.style.left = `${left}px`;
		line.style.top = `${Math.max(timelineRect.top - frameRect.top, 0)}px`;
		line.style.height = `${Math.max(gridRect.bottom - timelineRect.top, trackRect.height)}px`;
		line.style.display = "block";
		line.classList.toggle("invalid", !!invalid);
		const startLabel = frappe.datetime.str_to_user(this.formatServerDatetime(new Date(snappedMs)));
		const endLabel = endMs ? frappe.datetime.str_to_user(this.formatServerDatetime(new Date(endMs))) : "";
		tooltip.textContent = endLabel ? `${startLabel} → ${endLabel}` : startLabel;
		tooltip.style.display = "block";
		tooltip.style.left = `${left}px`;
		tooltip.style.top = `${Math.max(trackRect.top - frameRect.top - 28, 6)}px`;
	}

	hideGuide() {
		if (!this.ganttOverlay) {
			return;
		}
		this.ganttOverlay.querySelectorAll(".ia-gantt-guide-line, .ia-gantt-guide-tooltip").forEach((node) => {
			node.style.display = "none";
			node.classList.remove("invalid");
		});
	}

	updateMovePreview(event) {
		const state = this.dragState;
		if (!state || state.mode !== "move" || !this.timelineMeta) {
			return;
		}
		state.moved =
			state.moved ||
			Math.abs(event.clientX - state.initialClientX) > 4 ||
			Math.abs(event.clientY - state.initialClientY) > 4;
		this.autoScrollShell(event.clientX);
		const trackNode = this.findTrackByPoint(event.clientX, event.clientY) || state.targetTrackNode || state.trackNode;
		if (!trackNode) {
			return;
		}
		if (state.targetTrackNode && state.targetTrackNode !== trackNode) {
			state.targetTrackNode.classList.remove("drag-target", "invalid-target");
		}
		state.targetTrackNode = trackNode;
		state.targetTrackNode.classList.add("drag-target");
		const trackRect = trackNode.getBoundingClientRect();
		const rawStartMs = this.timelineMeta.start + (((event.clientX - trackRect.left) - state.initialOffsetX) / Math.max(trackRect.width, 1)) * this.timelineMeta.span;
		const snappedStartMs = this.snapMs(
			rawStartMs,
			this.timelineMeta.start,
			Math.max(this.timelineMeta.start, this.timelineMeta.end - state.durationMs)
		);
		const snappedEndMs = snappedStartMs + state.durationMs;
		const leftPct = ((snappedStartMs - this.timelineMeta.start) / this.timelineMeta.span) * 100;
		const widthPct = Math.max((state.durationMs / this.timelineMeta.span) * 100, 2);
		if (state.ghostNode.parentNode !== trackNode) {
			trackNode.appendChild(state.ghostNode);
		}
		state.ghostNode.style.left = `${leftPct}%`;
		state.ghostNode.style.width = `${widthPct}%`;
		state.targetWorkstation = trackNode.dataset.workstation || "";
		state.targetBeforeSegmentName = this.findBeforeSegment(trackNode, snappedStartMs, state.segmentName);
		state.targetStartMs = snappedStartMs;
		state.targetEndMs = snappedEndMs;
		this.updateGuide(trackNode, snappedStartMs, snappedEndMs, false);
	}

	updateResizePreview(event) {
		const state = this.dragState;
		if (!state || state.mode !== "resize" || !this.timelineMeta) {
			return;
		}
		state.moved = state.moved || Math.abs(event.clientX - state.initialClientX) > 3;
		this.autoScrollShell(event.clientX);
		const trackRect = state.trackNode.getBoundingClientRect();
		const rawEndMs = this.timelineMeta.start + ((event.clientX - trackRect.left) / Math.max(trackRect.width, 1)) * this.timelineMeta.span;
		const snappedEndMs = this.snapMs(rawEndMs, state.startMs + this.getSnapMs(), this.timelineMeta.end);
		const widthPct = Math.max(((snappedEndMs - state.startMs) / this.timelineMeta.span) * 100, 2);
		state.ghostNode.style.left = state.barNode.style.left || "0%";
		state.ghostNode.style.width = `${widthPct}%`;
		state.targetEndMs = snappedEndMs;
		this.updateGuide(state.trackNode, snappedEndMs, null, false);
	}

	formatServerDatetime(date) {
		const year = date.getFullYear();
		const month = String(date.getMonth() + 1).padStart(2, "0");
		const day = String(date.getDate()).padStart(2, "0");
		const hours = String(date.getHours()).padStart(2, "0");
		const minutes = String(date.getMinutes()).padStart(2, "0");
		const seconds = String(date.getSeconds()).padStart(2, "0");
		return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
	}

	async handleDrop(segmentName, targetWorkstation, beforeSegmentName, targetStartMs) {
		if (!this.canEditManualSchedule()) {
			return;
		}
		if (!segmentName || !targetWorkstation) {
			return;
		}
		const targetStartTime = targetStartMs ? this.formatServerDatetime(new Date(targetStartMs)) : null;
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
				target_start_time: targetStartTime || undefined,
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
								target_start_time: targetStartTime || undefined,
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
			this.showManualAdjustmentBlocked(preview, __("Manual Move Blocked"));
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
						target_start_time: targetStartTime || undefined,
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
				message: __("Loading schedule detail..."),
				busy_key: `result-detail:${resultName}`,
				feedback_target: this.feedback,
				success_feedback: __("Schedule detail loaded."),
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
		const canEditNotes = injection_aps.ui.can_run_action("update_schedule_notes");
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
								<th>${__("Demand", null, "Injection APS")}</th>
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
											<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.demand_source || ""))}</td>
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
											<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.mold_status || ""))}${row.is_family_mold ? ` / ${__("Family", null, "Injection APS")}` : ""}</td>
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
								<th>${__("Reason / Message")}</th>
							</tr>
						</thead>
						<tbody>
							${exceptionRows
								.map(
									(row) => `
										<tr>
											<td>${injection_aps.ui.pill(row.severity || "", row.is_blocking ? "red" : "orange")}</td>
											<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.exception_type || ""))}</td>
											<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.root_cause_text || row.message || ""))}</td>
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
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Customer Ref")}</div><div class="ia-kv-value">${injection_aps.ui.escape(itemDetail.customer_reference || ((sourceRows[0] && sourceRows[0].customer_part_no) || ""))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Drawing")}</div><div class="ia-kv-value">${injection_aps.ui.escape(itemDetail.drawing_file || "")}</div></div>
						</div>
					</div>
					<div class="ia-panel">
						<h4>${__("Planning")}</h4>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Requested", null, "Injection APS")}</div><div class="ia-kv-value">${injection_aps.ui.escape(result.requested_date || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Source")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.translate(result.demand_source || ""))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Run", null, "Injection APS")}</div><div class="ia-kv-value">${link(routeLinks.planning_run, result.planning_run || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Net Req")}</div><div class="ia-kv-value">${link(routeLinks.net_requirement, result.net_requirement || "")}</div></div>
						</div>
					</div>
				</div>
				<div class="ia-mini-grid">
					<div class="ia-panel">
						<h4>${__("Execution")}</h4>
						<div class="ia-kv">
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Actual Status")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.translate(selectedSegment.actual_status || result.actual_status || ""))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Actual Qty")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.format_number(selectedSegment.actual_completed_qty || result.actual_progress_qty || 0))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Hourly Capacity")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.format_number(selectedSegment.hourly_capacity_qty || 0))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Daily Capacity")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.format_number(selectedSegment.daily_capacity_qty || 0))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Capacity Source")}</div><div class="ia-kv-value">${injection_aps.ui.escape(injection_aps.ui.translate(selectedSegment.capacity_source_label || selectedSegment.capacity_source || ""))}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Work Order")}</div><div class="ia-kv-value">${link(selectedSegment.work_order_route, selectedSegment.linked_work_order || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Scheduling", null, "Injection APS")}</div><div class="ia-kv-value">${link(selectedSegment.work_order_scheduling_route, selectedSegment.linked_work_order_scheduling || "")}</div></div>
							<div class="ia-kv-row"><div class="ia-kv-key">${__("Manufacture Entry")}</div><div class="ia-kv-value">${link(selectedSegment.latest_stock_entry_route, selectedSegment.latest_stock_entry || "")}</div></div>
						</div>
					</div>
						<div class="ia-panel">
						<h4>${__("Notes")}</h4>
						<div class="ia-kv-row" style="display:block; margin-bottom:8px;">
							<div class="ia-kv-key">${__("Result Note")}</div>
							<textarea id="${resultNoteId}" class="form-control" rows="3" ${canEditNotes ? "" : "readonly"}>${injection_aps.ui.escape(result.notes || "")}</textarea>
							<div class="ia-note-save-row">
								${canEditNotes ? injection_aps.ui.icon_button("check", __("Save Result Note"), { id: saveResultNoteId }, "ia-note-save-btn") : ""}
							</div>
						</div>
						<div class="ia-kv-row" style="display:block;">
							<div class="ia-kv-key">${__("Segment Note")}</div>
							<textarea id="${segmentNoteId}" class="form-control" rows="3" ${canEditNotes ? "" : "readonly"}>${injection_aps.ui.escape(selectedSegment.segment_note || "")}</textarea>
							<div class="ia-note-save-row">
								${canEditNotes ? injection_aps.ui.icon_button("check", __("Save Segment Note"), { id: saveSegmentNoteId, disabled: selectedSegment.name ? null : "disabled" }, "ia-note-save-btn") : ""}
							</div>
						</div>
					</div>
				</div>
				<div class="ia-status-line">
					<div class="ia-status-cell"><span class="ia-status-label">${__("Flow")}</span><div class="ia-status-value">${injection_aps.ui.escape(injection_aps.ui.translate(result.flow_step || ""))}</div></div>
					<div class="ia-status-cell"><span class="ia-status-label">${__("Next")}</span><div class="ia-status-value">${injection_aps.ui.escape(injection_aps.ui.translate(result.next_step_hint || ""))}</div></div>
					<div class="ia-status-cell"><span class="ia-status-label">${__("Blocking")}</span><div class="ia-status-value">${injection_aps.ui.escape(injection_aps.ui.translate(result.blocking_reason || __("None")))}</div></div>
				</div>
				<div class="ia-panel">
					<h4>${__("Explanation")}</h4>
					<div class="ia-muted">${injection_aps.ui.escape(injection_aps.ui.translate(result.schedule_explanation || result.family_output_summary || ""))}</div>
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
												<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.segment_kind || ""))}</td>
												<td>${injection_aps.ui.escape(row.workstation || "")}</td>
												<td>${injection_aps.ui.escape(row.mould_reference || "")}</td>
												<td>${injection_aps.ui.escape(injection_aps.ui.format_number(row.planned_qty || 0))}</td>
												<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.actual_status || ""))} / ${injection_aps.ui.escape(injection_aps.ui.format_number(row.actual_completed_qty || 0))}</td>
												<td>${injection_aps.ui.escape(injection_aps.ui.translate(row.risk_flags || ""))}</td>
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
		injection_aps.ui.open_drawer(resultName, `${__("Current APS Run")} ${result.planning_run || ""}`, html);
		const actionHost = document.getElementById(actionHostId);
		injection_aps.ui.render_actions(actionHost, injection_aps.ui.get_value(detail, "next_actions.actions", []) || [], async (action) => {
			await injection_aps.ui.run_action(action);
		});
		if (canEditNotes) {
			injection_aps.ui.add_click_listener(saveResultNoteId, async () => {
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
						result_note: (document.getElementById(resultNoteId) && document.getElementById(resultNoteId).value) || "",
					}
				);
			});
			injection_aps.ui.add_click_listener(saveSegmentNoteId, async () => {
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
						segment_note: (document.getElementById(segmentNoteId) && document.getElementById(segmentNoteId).value) || "",
					}
				);
			});
		}
		injection_aps.ui.add_click_listener(sourceExportId, () => {
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
		injection_aps.ui.add_click_listener(moldExportId, () => {
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
		injection_aps.ui.add_click_listener(exceptionExportId, () => {
			this.exportRows(
				__("Risk Flags"),
				`aps_risk_flags_${resultName}`,
				[
					{ label: __("Severity"), fieldname: "severity" },
					{ label: __("Type"), fieldname: "exception_type" },
					{ label: __("Message", null, "Injection APS"), fieldname: "message" },
					{ label: __("Blocking"), fieldname: "is_blocking", fieldtype: "Check" },
					{ label: __("Machine", null, "Injection APS"), fieldname: "workstation" },
					{ label: __("Source Doctype"), fieldname: "source_doctype" },
					{ label: __("Source Name"), fieldname: "source_name" },
				],
				exceptionRows,
				`${result.item_code || ""} / ${result.customer || ""}`
			);
		});
		injection_aps.ui.add_click_listener(segmentExportId, () => {
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
					{ label: __("Start", null, "Injection APS"), fieldname: "start_time" },
					{ label: __("End", null, "Injection APS"), fieldname: "end_time" },
					{ label: __("Work Order"), fieldname: "linked_work_order" },
					{ label: __("Scheduling", null, "Injection APS"), fieldname: "linked_work_order_scheduling" },
				],
				segments,
				`${result.item_code || ""} / ${result.customer || ""}`
			);
		});
	}
}
