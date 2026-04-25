frappe.provide("injection_aps.ui");

(function () {
	if (injection_aps.ui.__initialized) {
		return;
	}

	injection_aps.ui.__initialized = true;
	injection_aps.ui.__busy_keys = new Set();
	injection_aps.ui.__freeze_depth = 0;
	injection_aps.ui.__action_role_map = {
		preview: ["System Manager", "GMC", "PMC", "Sales Manager", "Sales User", "Manufacturing Manager"],
		preview_current_rows: ["System Manager", "GMC", "PMC", "Sales Manager", "Sales User", "Manufacturing Manager"],
		import_only: ["System Manager", "GMC", "PMC", "Sales Manager", "Sales User", "Manufacturing Manager"],
		import_and_promote: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		promote_import: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		rebuild: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		rebuild_demand_pool: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		rebuild_net_requirements: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		trial: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		run_trial: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		approve: ["System Manager", "GMC", "Manufacturing Manager"],
		generate_work_order_proposals: ["System Manager", "GMC", "Manufacturing Manager"],
		generate_shift_schedule_proposals: ["System Manager", "GMC", "Manufacturing Manager"],
		apply_work_order_proposals: ["System Manager", "GMC", "Manufacturing Manager"],
		apply_shift_schedule_proposals: ["System Manager", "GMC", "Manufacturing Manager"],
		reject_work_order_proposals: ["System Manager", "GMC", "Manufacturing Manager"],
		reject_shift_schedule_proposals: ["System Manager", "GMC", "Manufacturing Manager"],
		preview_manual_schedule_adjustment: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		apply_manual_schedule_adjustment: ["System Manager", "GMC", "Manufacturing Manager"],
		update_schedule_notes: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		sync_execution: ["System Manager", "GMC", "PMC", "Manufacturing Manager", "Manufacturing User"],
		rebuild_exceptions: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		edit_net_requirement: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
		delete_net_requirement: ["System Manager", "GMC", "PMC", "Manufacturing Manager"],
	};

	injection_aps.ui.ensure_styles = function () {
		if (document.getElementById("injection-aps-page-style")) {
			return;
		}
		const link = document.createElement("link");
		link.id = "injection-aps-page-style";
		link.rel = "stylesheet";
		link.href = "/assets/injection_aps/css/injection_aps.css";
		document.head.appendChild(link);
	};

	injection_aps.ui.escape = function (value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	};

	injection_aps.ui.__local_icons = new Set([
		"download",
		"edit",
		"external-link",
		"filter",
		"search",
		"trash-2",
		"x",
	]);

	injection_aps.ui.icon = function (iconName, size) {
		const name = iconName || "download";
		const sizeClass = size ? ` ia-aps-icon-${injection_aps.ui.escape(size)}` : "";
		if (injection_aps.ui.__local_icons.has(name)) {
			return `<svg class="ia-aps-icon${sizeClass}" aria-hidden="true"><use href="/assets/injection_aps/icons/aps-icons.svg#${injection_aps.ui.escape(name)}"></use></svg>`;
		}
		if (frappe.utils && frappe.utils.icon) {
			try {
				const icon = frappe.utils.icon(name, size || "xs");
				if (icon) {
					return icon;
				}
			} catch (error) {
				// Fall through to a stable local icon if the ERPNext build does not ship this symbol.
			}
		}
		return `<svg class="ia-aps-icon${sizeClass}" aria-hidden="true"><use href="/assets/injection_aps/icons/aps-icons.svg#filter"></use></svg>`;
	};

	injection_aps.ui.translate = function (value) {
		if (value == null || value === "") {
			return "";
		}
		return __(String(value));
	};

	injection_aps.ui.pill = function (label, tone) {
		return `<span class="ia-pill ${tone || "blue"}">${injection_aps.ui.escape(label || "")}</span>`;
	};

	injection_aps.ui.route_link = function (label, route) {
		return `<a href="/app/${route}" class="ia-link">${injection_aps.ui.escape(label || "")}</a>`;
	};

	injection_aps.ui.doc_route = function (doctype, name) {
		if (!doctype || !name) {
			return "";
		}
		return `Form/${doctype}/${name}`;
	};

	injection_aps.ui.doc_link = function (doctype, name, label) {
		const route = injection_aps.ui.doc_route(doctype, name);
		return route ? injection_aps.ui.route_link(label || name, route) : injection_aps.ui.escape(label || name || "");
	};

	injection_aps.ui.shorten = function (value, length) {
		const text = value == null ? "" : String(value);
		const maxLength = length || 24;
		if (text.length <= maxLength) {
			return text;
		}
		return `${text.slice(0, Math.max(0, maxLength - 1))}…`;
	};

	injection_aps.ui.to_plain_text = function (value) {
		if (value == null) {
			return "";
		}
		if (typeof value === "number") {
			return String(value);
		}
		const text = String(value);
		if (!/[<>]/.test(text)) {
			return text;
		}
		const container = document.createElement("div");
		container.innerHTML = text;
		return container.textContent || container.innerText || "";
	};

	injection_aps.ui.format_number = function (value, maximumFractionDigits) {
		const numericValue = Number(value || 0);
		if (!Number.isFinite(numericValue)) {
			return "0";
		}
		const inferredDigits = Math.abs(numericValue - Math.round(numericValue)) > 0.000001 ? 3 : 0;
		return new Intl.NumberFormat(undefined, {
			minimumFractionDigits: 0,
			maximumFractionDigits:
				Number.isInteger(maximumFractionDigits) && maximumFractionDigits >= 0
					? maximumFractionDigits
					: inferredDigits,
		}).format(numericValue);
	};

	injection_aps.ui.go_to = function (route) {
		if (!route) {
			return;
		}
		if (route.startsWith("http")) {
			window.location.href = route;
			return;
		}
		window.location.href = `/app/${route}`;
	};

	injection_aps.ui.format_datetime = function (value) {
		if (!value) {
			return "";
		}
		return frappe.datetime.str_to_user(value);
	};

	injection_aps.ui.format_date = function (value) {
		if (!value) {
			return "";
		}
		return frappe.datetime.str_to_user(value);
	};

	injection_aps.ui.make_file_name = function (value, fallback) {
		const safe = String(value || fallback || "aps_export")
			.trim()
			.replace(/[\\/:*?"<>|]+/g, "_")
			.replace(/\s+/g, "_")
			.replace(/_+/g, "_")
			.replace(/^_+|_+$/g, "");
		return safe || fallback || "aps_export";
	};

	injection_aps.ui.get_user_roles = function () {
		const roles = []
			.concat((frappe.boot && frappe.boot.user && frappe.boot.user.roles) || [])
			.concat(frappe.user_roles || []);
		return Array.from(new Set(roles.filter(Boolean)));
	};

	injection_aps.ui.has_any_role = function (roles) {
		if (frappe.session && frappe.session.user === "Administrator") {
			return true;
		}
		const requiredRoles = roles || [];
		if (!requiredRoles.length) {
			return true;
		}
		const userRoles = new Set(injection_aps.ui.get_user_roles());
		return requiredRoles.some((role) => userRoles.has(role));
	};

	injection_aps.ui.can_run_action = function (action) {
		const actionKey = typeof action === "string" ? action : action && action.action_key;
		if (!actionKey) {
			return true;
		}
		const requiredRoles = injection_aps.ui.__action_role_map[actionKey];
		return !requiredRoles || injection_aps.ui.has_any_role(requiredRoles);
	};

	injection_aps.ui.is_numeric_like = function (value, fieldtype) {
		if (["Float", "Currency", "Percent", "Int", "Check"].includes(fieldtype || "")) {
			return true;
		}
		if (typeof value === "number") {
			return Number.isFinite(value);
		}
		if (typeof value !== "string") {
			return false;
		}
		return /^-?\d+(?:\.\d+)?$/.test(value.trim());
	};

	injection_aps.ui.export_cell_text = function (value) {
		const text = injection_aps.ui.to_plain_text(value);
		return String(text == null ? "" : text).replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
	};

	injection_aps.ui.escape_excel = function (value) {
		return injection_aps.ui.escape(String(value == null ? "" : value)).replace(/\n/g, "<br>");
	};

	injection_aps.ui.icon_button = function (iconName, title, attrs, extraClass) {
		const buttonClass = ["ia-icon-btn", extraClass || ""].filter(Boolean).join(" ");
		const safeAttrs = Object.entries(attrs || {})
			.filter(([, value]) => value !== null && value !== undefined && value !== false)
			.map(([key, value]) => `${key}="${injection_aps.ui.escape(value == null ? "" : String(value))}"`)
			.join(" ");
		return `
			<button
				type="button"
				class="${injection_aps.ui.escape(buttonClass)}"
				title="${injection_aps.ui.escape(title || "")}"
				aria-label="${injection_aps.ui.escape(title || "")}"
				${safeAttrs}
			>${injection_aps.ui.icon(iconName || "download", "xs")}</button>
		`;
	};

	injection_aps.ui.get_action_label = function (action) {
		if (!action) {
			return "";
		}
		return injection_aps.ui.translate(action.short_label || action.label || "");
	};

	injection_aps.ui.should_confirm_action = function (action, options) {
		if (!action) {
			return false;
		}
		if (options && options.confirm === false) {
			return false;
		}
		if (action.confirm_required === 1 || action.confirm_required === true) {
			return true;
		}
		return [
			"run_trial",
			"approve",
			"generate_work_order_proposals",
			"generate_shift_schedule_proposals",
			"apply_work_order_proposals",
			"apply_shift_schedule_proposals",
			"promote_import",
			"rebuild_demand_pool",
			"import_and_promote",
			"import_only",
		].includes(action.action_key || "");
	};

	injection_aps.ui.build_confirm_summary = function (action, options) {
		const settings = Object.assign({}, options || {});
		if (settings.html) {
			return settings.html;
		}
		const lines = []
			.concat(action && action.confirm_summary ? action.confirm_summary : [])
			.concat(settings.summary_lines || [])
			.filter(Boolean);
		if (!lines.length) {
			return `<div class="ia-muted">${__("Please confirm before continuing.")}</div>`;
		}
		return `
			<div class="ia-confirm-summary">
				${lines.map((row) => `<div class="ia-confirm-row">${injection_aps.ui.escape(injection_aps.ui.translate(row))}</div>`).join("")}
			</div>
		`;
	};

	injection_aps.ui.confirm_action = function (action, options) {
		const settings = Object.assign({}, options || {});
		if (!injection_aps.ui.should_confirm_action(action, settings)) {
			return Promise.resolve(true);
		}
		return new Promise((resolve) => {
			let settled = false;
			const finish = (value) => {
				if (settled) {
					return;
				}
				settled = true;
				resolve(value);
			};
			const dialog = new frappe.ui.Dialog({
				title: injection_aps.ui.translate(settings.title || (action && action.confirm_title) || __("Confirm Action")),
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "summary_html",
					},
				],
				primary_action_label: injection_aps.ui.translate(settings.primary_action_label || (action && action.confirm_label) || __("Confirm")),
				primary_action() {
					dialog.hide();
					finish(true);
				},
				secondary_action_label: __("Cancel"),
				secondary_action() {
					dialog.hide();
					finish(false);
				},
			});
			dialog.get_field("summary_html").$wrapper.html(injection_aps.ui.build_confirm_summary(action, settings));
			dialog.$wrapper.on("hidden.bs.modal", () => finish(false));
			dialog.show();
		});
	};

	injection_aps.ui.prompt_reason = function (options) {
		const settings = Object.assign({}, options || {});
		return new Promise((resolve) => {
			let settled = false;
			const finish = (value) => {
				if (settled) {
					return;
				}
				settled = true;
				resolve(value);
			};
			const dialog = new frappe.ui.Dialog({
				title: injection_aps.ui.translate(settings.title || __("Enter Rejection Reason")),
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "summary_html",
					},
					{
						fieldtype: "Small Text",
						fieldname: "reason",
						label: injection_aps.ui.translate(settings.label || __("Rejection Reason")),
						reqd: 1,
					},
				],
				primary_action_label: injection_aps.ui.translate(settings.primary_action_label || __("Confirm Reject")),
				primary_action() {
					const reason = String(dialog.get_value("reason") || "").trim();
					if (!reason) {
						frappe.show_alert({ message: __("Please enter a rejection reason."), indicator: "orange" });
						return;
					}
					dialog.hide();
					finish(reason);
				},
				secondary_action_label: __("Cancel"),
				secondary_action() {
					dialog.hide();
					finish(null);
				},
			});
			dialog.get_field("summary_html").$wrapper.html(
				injection_aps.ui.build_confirm_summary(null, {
					summary_lines: settings.summary_lines || [],
					html: settings.html,
				})
			);
			dialog.$wrapper.on("hidden.bs.modal", () => finish(null));
			dialog.show();
			const field = dialog.get_field("reason");
			if (field && field.$input) {
				field.$input.trigger("focus");
			}
		});
	};

	injection_aps.ui.get_value = function (object, path, fallback) {
		if (!object || !path) {
			return fallback;
		}
		const segments = Array.isArray(path) ? path : String(path).split(".");
		let cursor = object;
		for (let index = 0; index < segments.length; index += 1) {
			if (cursor == null) {
				return fallback;
			}
			cursor = cursor[segments[index]];
		}
		return cursor == null ? fallback : cursor;
	};

	injection_aps.ui.get_query_param = function (key) {
		const search = window.location && window.location.search ? window.location.search.replace(/^\?/, "") : "";
		if (!search || !key) {
			return null;
		}
		const pairs = search.split("&");
		for (let index = 0; index < pairs.length; index += 1) {
			const pair = pairs[index];
			if (!pair) {
				continue;
			}
			const chunks = pair.split("=");
			const name = decodeURIComponent(chunks[0] || "");
			if (name === key) {
				return decodeURIComponent((chunks.slice(1).join("=") || "").replace(/\+/g, " "));
			}
		}
		return null;
	};

	injection_aps.ui.add_click_listener = function (elementOrId, handler) {
		const element = typeof elementOrId === "string" ? document.getElementById(elementOrId) : elementOrId;
		if (element) {
			element.addEventListener("click", handler);
		}
	};

	injection_aps.ui.parse_download_filename = function (headerValue, fallback) {
		const source = String(headerValue || "");
		const utf8Match = source.match(/filename\*=UTF-8''([^;]+)/i);
		if (utf8Match && utf8Match[1]) {
			return decodeURIComponent(utf8Match[1]);
		}
		const simpleMatch = source.match(/filename=\"?([^\";]+)\"?/i);
		return simpleMatch && simpleMatch[1] ? simpleMatch[1] : fallback;
	};

	injection_aps.ui.download_blob = function (blob, filename) {
		const url = window.URL.createObjectURL(blob);
		const anchor = document.createElement("a");
		anchor.href = url;
		anchor.download = filename;
		document.body.appendChild(anchor);
		anchor.click();
		document.body.removeChild(anchor);
		window.URL.revokeObjectURL(url);
	};

	injection_aps.ui.build_export_payload = function (options) {
		const settings = Object.assign({}, options || {});
		const rows = settings.rows || [];
		const visibleColumns = settings.columns || [];
		const columns = (settings.export_columns || visibleColumns).filter(
			(column) => column && column.exportable !== false && column.fieldname !== "actions_html"
		);

		return {
			title: settings.title || settings.sheet_name || __("Export Excel"),
			subtitle: settings.subtitle || "",
			sheet_name: injection_aps.ui.make_file_name(settings.sheet_name || settings.title || "APS").slice(0, 28),
			file_name: `${injection_aps.ui.make_file_name(settings.file_name || settings.title || "aps_export")}.xlsx`,
			columns: columns.map((column) => ({
				label: column.export_label || column.label || column.fieldname || "",
				fieldname: column.fieldname,
				fieldtype: column.export_fieldtype || column.fieldtype || "",
			})),
			rows: rows.map((row, rowIndex) => {
				const exportRow = {};
				columns.forEach((column) => {
					const rawValue = row[column.fieldname];
					const formattedValue = settings.formatter
						? settings.formatter(column, rawValue, row, { mode: "export", row_index: rowIndex })
						: rawValue;
					const isNumeric = injection_aps.ui.is_numeric_like(rawValue, column.export_fieldtype || column.fieldtype);
					exportRow[column.fieldname] =
						isNumeric && rawValue !== null && rawValue !== undefined && rawValue !== ""
							? Number(rawValue)
							: injection_aps.ui.export_cell_text(formattedValue != null ? formattedValue : rawValue);
				});
				return exportRow;
			}),
		};
	};

	injection_aps.ui.export_rows_to_excel = async function (options) {
		const settings = Object.assign({}, options || {});
		const rows = settings.rows || [];
		if (!rows.length) {
			frappe.show_alert({ message: __("No rows available to export."), indicator: "orange" });
			return;
		}

		const payload = injection_aps.ui.build_export_payload(settings);
		if (!payload.columns.length) {
			frappe.show_alert({ message: __("No rows available to export."), indicator: "orange" });
			return;
		}

		try {
			await injection_aps.ui.with_busy(
				{
					message: __("Preparing Excel export..."),
					success_message: __("Excel export ready."),
					busy_key: `xlsx-export:${payload.file_name}`,
				},
				() =>
					new Promise((resolve, reject) => {
						const xhr = new XMLHttpRequest();
						xhr.open("POST", "/api/method/injection_aps.api.app.export_table_xlsx", true);
						xhr.responseType = "blob";
						xhr.withCredentials = true;
						xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
						xhr.setRequestHeader("X-Frappe-CSRF-Token", frappe.csrf_token || "");
						xhr.onload = function () {
							if (xhr.status < 200 || xhr.status >= 300) {
								reject(new Error(xhr.responseText || __("Failed to export Excel.")));
								return;
							}
							const filename = injection_aps.ui.parse_download_filename(
								xhr.getResponseHeader("Content-Disposition"),
								payload.file_name
							);
							injection_aps.ui.download_blob(xhr.response, filename);
							resolve(xhr.response);
						};
						xhr.onerror = function () {
							reject(new Error(__("Failed to export Excel.")));
						};
						xhr.send(`payload_json=${encodeURIComponent(JSON.stringify(payload))}`);
					})
			);
		} catch (error) {
			console.error(error);
			frappe.msgprint({
				title: __("Excel Export Failed"),
				message: injection_aps.ui.escape((error && error.message) || __("Failed to export Excel.")),
				indicator: "red",
			});
		}
	};

	injection_aps.ui.set_feedback = function (target, message, tone) {
		if (!target) {
			return;
		}
		target.className = `ia-feedback ${tone || ""}`.trim();
		target.textContent = message || "";
	};

	injection_aps.ui.with_busy = async function (options, task) {
		const settings = typeof options === "string" ? { message: options } : Object.assign({}, options || {});
		const busyKey = settings.busy_key || "aps-global";
		if (injection_aps.ui.__busy_keys.has(busyKey)) {
			if (settings.feedback_target) {
				injection_aps.ui.set_feedback(
					settings.feedback_target,
					settings.duplicate_feedback || __("APS is still processing the previous request. Please wait."),
					"warning"
				);
			}
			frappe.show_alert({
				message: settings.duplicate_message || __("APS is still processing the last request. Please wait."),
				indicator: "orange",
			});
			return null;
		}

		const busyMessage = settings.message || __("APS is processing...");
		const feedbackTarget = settings.feedback_target;
		injection_aps.ui.__busy_keys.add(busyKey);
		frappe.show_alert({ message: busyMessage, indicator: "blue" });
		if (feedbackTarget) {
			injection_aps.ui.set_feedback(feedbackTarget, busyMessage, "warning");
		}
		if (settings.freeze !== false) {
			injection_aps.ui.__freeze_depth += 1;
			if (injection_aps.ui.__freeze_depth === 1) {
				frappe.dom.freeze(busyMessage);
			}
		}

		try {
			const response = await task();
			if (feedbackTarget && settings.success_feedback) {
				injection_aps.ui.set_feedback(feedbackTarget, settings.success_feedback);
			}
			if (settings.success_message) {
				frappe.show_alert({ message: settings.success_message, indicator: "green" });
			}
			return response;
		} catch (error) {
			if (feedbackTarget) {
				injection_aps.ui.set_feedback(
					feedbackTarget,
					settings.error_feedback || __("APS processing failed. Please review the error and try again."),
					"error"
				);
			}
			throw error;
		} finally {
			if (settings.freeze !== false) {
				injection_aps.ui.__freeze_depth = Math.max(0, injection_aps.ui.__freeze_depth - 1);
				if (injection_aps.ui.__freeze_depth === 0) {
					frappe.dom.unfreeze();
				}
			}
			injection_aps.ui.__busy_keys.delete(busyKey);
		}
	};

	injection_aps.ui.xcall = async function (options, method, args) {
		return injection_aps.ui.with_busy(options, () => frappe.xcall(method, args || {}));
	};

	injection_aps.ui.render_cards = function (target, cards) {
		target.innerHTML = (cards || [])
			.map(
				(card) => `
				<div class="ia-card">
					<span class="ia-card-label">${injection_aps.ui.escape(card.label || "")}</span>
					<div class="ia-card-value">${injection_aps.ui.escape(injection_aps.ui.to_plain_text(card && card.value != null ? card.value : ""))}</div>
					${card.note ? `<div class="ia-muted ia-card-note">${injection_aps.ui.escape(card.note)}</div>` : ""}
				</div>
			`
			)
			.join("");
	};

	injection_aps.ui.render_table = function (target, columns, rows, formatter, options) {
		const settings = Object.assign({}, options || {});
		const toolbar = settings.exportable || settings.show_count !== false || settings.toolbar_html
			? `
				<div class="ia-table-toolbar">
					<div class="ia-table-count">${__("{0} rows").replace("{0}", injection_aps.ui.format_number((rows || []).length))}</div>
					<div class="ia-table-actions">
						${settings.toolbar_html || ""}
						${
							settings.exportable && rows && rows.length
								? injection_aps.ui.icon_button("download", __("Export Excel"), { "data-ia-export-table": "1" })
								: ""
						}
					</div>
				</div>
			`
			: "";
		if (!rows || !rows.length) {
			const emptyToolbar = settings.toolbar_html || settings.exportable || settings.show_count === true ? toolbar : "";
			target.innerHTML = `
				${emptyToolbar}
				<div class="ia-table-empty">
					<div class="ia-empty-title">${__("No rows found")}</div>
					<div class="ia-muted">${settings.empty_message || __("Try changing the filters or refreshing the data.")}</div>
				</div>
			`;
			if (settings.after_render) {
				settings.after_render(target, { columns, rows: rows || [] });
			}
			return;
		}

		const body = rows
			.map((row, rowIndex) => {
				const cells = columns
					.map((column) => {
						const rawValue = row[column.fieldname];
						const value = formatter
							? formatter(column, rawValue, row, rowIndex)
							: injection_aps.ui.escape(rawValue == null ? "" : String(rawValue));
						const classNames = [
							injection_aps.ui.is_numeric_like(rawValue, column.fieldtype) ? "ia-cell-number" : "",
							column.className || "",
						]
							.filter(Boolean)
							.join(" ");
						return `<td class="${injection_aps.ui.escape(classNames)}">${value}</td>`;
					})
					.join("");
				return `<tr data-row-index="${rowIndex}">${cells}</tr>`;
			})
			.join("");

		target.innerHTML = `
			${toolbar}
			<div class="ia-table-shell">
				<table class="ia-table">
					<thead>
						<tr>${columns.map((column) => `<th class="${injection_aps.ui.escape(column.className || "")}">${injection_aps.ui.escape(column.label)}</th>`).join("")}</tr>
					</thead>
					<tbody>${body}</tbody>
				</table>
			</div>
		`;

		if (settings.exportable) {
			const exportButton = target.querySelector("[data-ia-export-table='1']");
			if (exportButton) {
				exportButton.addEventListener("click", () => {
					injection_aps.ui.export_rows_to_excel({
						title: settings.export_title || settings.export_sheet_name || __("Export Excel"),
						subtitle: settings.export_subtitle || "",
						sheet_name: settings.export_sheet_name || settings.export_title || __("Export Excel"),
						file_name: settings.export_file_name || settings.export_title || "aps_export",
						columns,
						rows,
						formatter: settings.export_formatter || formatter,
						export_columns: settings.export_columns,
					});
				});
			}
		}
		if (settings.row_context_menu) {
			target.querySelectorAll("tbody tr[data-row-index]").forEach((node) => {
				node.addEventListener("contextmenu", (event) => {
					const row = rows[Number(node.dataset.rowIndex || 0)];
					const items = settings.row_context_menu(row, Number(node.dataset.rowIndex || 0), event) || [];
					if (!items.length) {
						return;
					}
					event.preventDefault();
					injection_aps.ui.open_context_menu(items, { x: event.clientX, y: event.clientY });
				});
			});
		}
		if (settings.after_render) {
			settings.after_render(target, { columns, rows });
		}
	};

	injection_aps.ui.render_status_line = function (target, context) {
		if (!target) {
			return;
		}
		if (!context) {
			target.innerHTML = "";
			return;
		}
		target.innerHTML = `
			<div class="ia-status-line">
				<div class="ia-status-cell">
					<span class="ia-status-label">${__("Current Step")}</span>
					<div class="ia-status-value">${injection_aps.ui.escape(injection_aps.ui.translate(context.current_step || "-"))}</div>
				</div>
				<div class="ia-status-cell">
					<span class="ia-status-label">${__("Next Step")}</span>
					<div class="ia-status-value">${injection_aps.ui.escape(injection_aps.ui.translate(context.next_step || "-"))}</div>
				</div>
				<div class="ia-status-cell ia-status-cell-wide">
					<span class="ia-status-label">${__("Blocking Reason")}</span>
					<div class="ia-status-value ${context.blocking_reason ? "ia-risk-text" : "ia-muted"}">${injection_aps.ui.escape(injection_aps.ui.translate(context.blocking_reason || __("None")))}</div>
				</div>
			</div>
		`;
	};

	injection_aps.ui.render_run_context = function (target, context) {
		if (!target) {
			return;
		}
		if (!context || !context.docname) {
			target.innerHTML = "";
			return;
		}
		const plantFloors = (context.selected_plant_floors || []).join(", ");
		target.innerHTML = `
			<div class="ia-run-context">
				<div class="ia-run-context-title">${__("Current APS Run")}</div>
				<div class="ia-run-context-grid">
					<div class="ia-run-context-cell"><span class="ia-status-label">${__("ID")}</span><div class="ia-run-context-value">${injection_aps.ui.escape(context.docname || "")}</div></div>
					<div class="ia-run-context-cell ia-run-context-cell-wide"><span class="ia-status-label">${__("Company")}</span><div class="ia-run-context-value">${injection_aps.ui.escape(context.company || "-")}</div></div>
					<div class="ia-run-context-cell ia-run-context-cell-wide"><span class="ia-status-label">${__("Plant Floors")}</span><div class="ia-run-context-value">${injection_aps.ui.escape(plantFloors || "-")}</div></div>
					<div class="ia-run-context-cell"><span class="ia-status-label">${__("Horizon")}</span><div class="ia-run-context-value">${injection_aps.ui.escape((context.horizon_days || 0) ? `${context.horizon_days} ${__("Days")}` : "-")}</div></div>
					<div class="ia-run-context-cell"><span class="ia-status-label">${__("Status")}</span><div class="ia-run-context-value">${injection_aps.ui.escape(injection_aps.ui.translate(context.status_label || context.current_step || "-"))}</div></div>
					<div class="ia-run-context-cell"><span class="ia-status-label">${__("Approval")}</span><div class="ia-run-context-value">${injection_aps.ui.escape(injection_aps.ui.translate(context.approval_state_label || "-"))}</div></div>
					<div class="ia-run-context-cell"><span class="ia-status-label">${__("Exceptions")}</span><div class="ia-run-context-value">${injection_aps.ui.escape(String(context.exception_count || 0))}</div></div>
					<div class="ia-run-context-cell ia-run-context-cell-wide"><span class="ia-status-label">${__("Updated On")}</span><div class="ia-run-context-value">${injection_aps.ui.escape(injection_aps.ui.format_datetime(context.modified || ""))}</div></div>
				</div>
			</div>
		`;
	};

	injection_aps.ui.render_run_empty_state = function (target, options) {
		if (!target) {
			return;
		}
		const settings = Object.assign({}, options || {});
		const runs = settings.recent_runs || [];
		target.innerHTML = `
			<div class="ia-empty ia-run-empty">
				<div class="ia-run-empty-title">${injection_aps.ui.escape(settings.title || __("No APS Run Selected"))}</div>
				<div class="ia-muted">${injection_aps.ui.escape(settings.description || __("Select an APS run before entering this page."))}</div>
				<div class="ia-run-empty-actions">
					${settings.console_route ? `<a class="btn btn-xs btn-primary" href="/app/${injection_aps.ui.escape(settings.console_route)}">${__("Recalc Console")}</a>` : ""}
				</div>
				<div class="ia-run-empty-list">
					<div class="ia-run-empty-subtitle">${__("Recent Open APS Runs")}</div>
					${runs.length
						? runs
								.map(
									(row) => `
										<a class="ia-run-empty-row" href="/app/${injection_aps.ui.escape(row.route || "")}">
											<span class="ia-run-empty-main">
												<span class="ia-run-empty-name">${injection_aps.ui.escape(row.name || "")}</span>
												<span class="ia-run-empty-meta">${injection_aps.ui.escape((row.selected_plant_floors || []).join(", ") || row.company || "-")}</span>
											</span>
											<span class="ia-run-empty-side">
												<span>${injection_aps.ui.escape(injection_aps.ui.translate(row.status_label || row.status || ""))}</span>
												<span>${__("Exceptions")} ${injection_aps.ui.escape(String(row.exception_count || 0))}</span>
											</span>
										</a>
									`
								)
								.join("")
						: `<div class="ia-muted">${__("No open APS runs are available.")}</div>`}
				</div>
			</div>
		`;
	};

	injection_aps.ui.render_actions = function (target, actions, handler) {
		if (!target) {
			return;
		}
		const safeActions = (actions || []).filter((action) => Number((action && action.enabled) || 0) === 1 && injection_aps.ui.can_run_action(action));
		if (!safeActions.length) {
			target.innerHTML = "";
			return;
		}
		target.innerHTML = `
			<div class="ia-action-strip">
				${safeActions
					.map((action, index) => {
						return `
							<button
								type="button"
								class="btn btn-xs ${index === 0 ? "btn-primary" : "btn-default"} ia-action-btn"
								data-action-index="${index}"
								title="${injection_aps.ui.escape(injection_aps.ui.translate(action.label || action.reason || ""))}"
							>${injection_aps.ui.escape(injection_aps.ui.get_action_label(action))}</button>
						`;
					})
					.join("")}
			</div>
		`;
		$(target)
			.find("[data-action-index]")
			.each(function () {
				const index = Number(this.dataset.actionIndex);
				this.addEventListener("click", async () => {
					const action = safeActions[index];
					if (!action) {
						return;
					}
					await handler(action);
				});
			});
	};

	injection_aps.ui.run_action = async function (action, afterCall, options) {
		if (!action) {
			return null;
		}
		if (action.route) {
			injection_aps.ui.go_to(action.route);
			return null;
		}
		if (!action.method) {
			return null;
		}
		const confirmed = await injection_aps.ui.confirm_action(action, options);
		if (!confirmed) {
			return null;
		}
		const response = await injection_aps.ui.xcall(
			{
				message: injection_aps.ui.translate(action.busy_message || __("APS is processing {0}...").replace("{0}", injection_aps.ui.translate(action.label || __("request")))),
				success_message: injection_aps.ui.translate(action.success_message),
				busy_key: action.busy_key || `action:${action.action_key || action.method}`,
				duplicate_message: __("This APS action is already running."),
			},
			action.method,
			action.kwargs || {}
		);
		if (response == null) {
			return null;
		}
		if (afterCall) {
			await afterCall(response, action);
		}
		return response;
	};

	injection_aps.ui.show_warnings = function (result, title, warningKey) {
		const count = Number(result && warningKey ? result[warningKey] || 0 : 0);
		if (!count) {
			return;
		}
		const rowsKey = warningKey === "preflight_warning_count" ? "preflight_warnings" : "warnings";
		const warnings = result && rowsKey ? result[rowsKey] || [] : [];
		const extraCount = Math.max(count - warnings.length, 0);
		const rows = warnings
			.map((row) => `<li>${injection_aps.ui.escape(row.message || "")}</li>`)
			.join("");

		frappe.msgprint({
			title: title || __("APS Warnings"),
			message: `
				<div>${__("Warnings")}: <b>${count}</b></div>
				<ul style="margin-top:8px; padding-left:18px;">${rows}</ul>
				${extraCount ? `<div class="text-muted" style="margin-top:8px;">${__("Additional warnings")}: ${extraCount}</div>` : ""}
			`,
			wide: true,
		});
	};

	injection_aps.ui.ensure_drawer = function () {
		let drawer = document.getElementById("injection-aps-drawer");
		if (drawer) {
			return drawer;
		}
		drawer = document.createElement("div");
		drawer.id = "injection-aps-drawer";
		drawer.className = "ia-drawer";
		drawer.innerHTML = `
			<div class="ia-drawer-mask" data-ia-close="1"></div>
			<div class="ia-drawer-panel">
				<div class="ia-drawer-header">
					<div>
						<div class="ia-drawer-title"></div>
						<div class="ia-drawer-subtitle"></div>
					</div>
					${injection_aps.ui.icon_button("x", __("Close"), { "data-ia-close": "1" })}
				</div>
				<div class="ia-drawer-body"></div>
			</div>
		`;
		document.body.appendChild(drawer);
		drawer.querySelectorAll("[data-ia-close='1']").forEach((node) => {
			node.addEventListener("click", () => injection_aps.ui.close_drawer());
		});
		return drawer;
	};

	injection_aps.ui.open_drawer = function (title, subtitle, html) {
		const drawer = injection_aps.ui.ensure_drawer();
		drawer.querySelector(".ia-drawer-title").textContent = title || "";
		drawer.querySelector(".ia-drawer-subtitle").textContent = subtitle || "";
		drawer.querySelector(".ia-drawer-body").innerHTML = html || "";
		drawer.classList.add("open");
	};

	injection_aps.ui.close_drawer = function () {
		const drawer = document.getElementById("injection-aps-drawer");
		if (drawer) {
			drawer.classList.remove("open");
		}
	};

	injection_aps.ui.ensure_context_menu = function () {
		let menu = document.getElementById("injection-aps-context-menu");
		if (menu) {
			return menu;
		}
		menu = document.createElement("div");
		menu.id = "injection-aps-context-menu";
		menu.className = "ia-context-menu";
		menu.innerHTML = `<div class="ia-context-menu-body"></div>`;
		document.body.appendChild(menu);
		document.addEventListener("click", () => injection_aps.ui.close_context_menu());
		document.addEventListener("scroll", () => injection_aps.ui.close_context_menu(), true);
		document.addEventListener("keydown", (event) => {
			if (event.key === "Escape") {
				injection_aps.ui.close_context_menu();
			}
		});
		return menu;
	};

	injection_aps.ui.open_context_menu = function (items, point) {
		const rows = (items || []).filter((row) => row && row.label);
		if (!rows.length) {
			return;
		}
		const menu = injection_aps.ui.ensure_context_menu();
		const body = menu.querySelector(".ia-context-menu-body");
		body.innerHTML = rows
			.map((row, index) => `
				<button type="button" class="ia-context-menu-item" data-context-index="${index}">
					${row.icon ? `<span class="ia-context-menu-icon">${injection_aps.ui.icon(row.icon, "xs")}</span>` : ""}
					<span>${injection_aps.ui.escape(row.label)}</span>
				</button>
			`)
			.join("");
		body.querySelectorAll("[data-context-index]").forEach((node) => {
			node.addEventListener("click", async (event) => {
				event.stopPropagation();
				const item = rows[Number(node.dataset.contextIndex || 0)];
				injection_aps.ui.close_context_menu();
				if (item && item.handler) {
					await item.handler();
				}
			});
		});
		menu.style.display = "block";
		const maxLeft = window.innerWidth - 220;
		const maxTop = window.innerHeight - 240;
		menu.style.left = `${Math.max(8, Math.min(point.x || 0, maxLeft))}px`;
		menu.style.top = `${Math.max(8, Math.min(point.y || 0, maxTop))}px`;
	};

	injection_aps.ui.close_context_menu = function () {
		const menu = document.getElementById("injection-aps-context-menu");
		if (!menu) {
			return;
		}
		menu.style.display = "none";
	};
})();
