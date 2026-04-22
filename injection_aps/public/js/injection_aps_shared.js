frappe.provide("injection_aps.ui");

(function () {
	if (injection_aps.ui.__initialized) {
		return;
	}

	injection_aps.ui.__initialized = true;
	injection_aps.ui.__busy_keys = new Set();
	injection_aps.ui.__freeze_depth = 0;

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
			>${frappe.utils.icon(iconName || "download", "xs")}</button>
		`;
	};

	injection_aps.ui.parse_download_filename = function (headerValue, fallback) {
		const source = String(headerValue || "");
		const utf8Match = source.match(/filename\*=UTF-8''([^;]+)/i);
		if (utf8Match?.[1]) {
			return decodeURIComponent(utf8Match[1]);
		}
		const simpleMatch = source.match(/filename=\"?([^\";]+)\"?/i);
		return simpleMatch?.[1] || fallback;
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
		const settings = { ...(options || {}) };
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
		const settings = { ...(options || {}) };
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
				async () => {
					const body = new URLSearchParams();
					body.append("payload_json", JSON.stringify(payload));
					const response = await fetch("/api/method/injection_aps.api.app.export_table_xlsx", {
						method: "POST",
						credentials: "same-origin",
						headers: {
							"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
							"X-Frappe-CSRF-Token": frappe.csrf_token || "",
						},
						body: body.toString(),
					});
					if (!response.ok) {
						const message = await response.text();
						throw new Error(message || __("Failed to export Excel."));
					}
					const blob = await response.blob();
					const filename = injection_aps.ui.parse_download_filename(
						response.headers.get("Content-Disposition"),
						payload.file_name
					);
					injection_aps.ui.download_blob(blob, filename);
				}
			);
		} catch (error) {
			console.error(error);
			frappe.msgprint({
				title: __("Excel Export Failed"),
				message: injection_aps.ui.escape(error?.message || __("Failed to export Excel.")),
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
		const settings = typeof options === "string" ? { message: options } : { ...(options || {}) };
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
					<div class="ia-card-value">${injection_aps.ui.escape(injection_aps.ui.to_plain_text(card.value ?? ""))}</div>
					${card.note ? `<div class="ia-muted ia-card-note">${injection_aps.ui.escape(card.note)}</div>` : ""}
				</div>
			`
			)
			.join("");
	};

	injection_aps.ui.render_table = function (target, columns, rows, formatter, options) {
		const settings = { ...(options || {}) };
		if (!rows || !rows.length) {
			target.innerHTML = `<div class="ia-table-shell"><div class="ia-muted ia-empty">${__("No rows found.")}</div></div>`;
			return;
		}

		const body = rows
			.map((row) => {
				const cells = columns
					.map((column) => {
						const rawValue = row[column.fieldname];
						const value = formatter
							? formatter(column, rawValue, row)
							: injection_aps.ui.escape(rawValue == null ? "" : String(rawValue));
						return `<td>${value}</td>`;
					})
					.join("");
				return `<tr>${cells}</tr>`;
			})
			.join("");

		const toolbar = settings.exportable
			? `
				<div class="ia-table-toolbar">
					${injection_aps.ui.icon_button("download", __("Export Excel"), { "data-ia-export-table": "1" })}
				</div>
			`
			: "";

		target.innerHTML = `
			${toolbar}
			<div class="ia-table-shell">
				<table class="ia-table">
					<thead>
						<tr>${columns.map((column) => `<th>${injection_aps.ui.escape(column.label)}</th>`).join("")}</tr>
					</thead>
					<tbody>${body}</tbody>
				</table>
			</div>
		`;

		if (settings.exportable) {
			target.querySelector("[data-ia-export-table='1']")?.addEventListener("click", () => {
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
					<div class="ia-status-value">${injection_aps.ui.escape(context.current_step || "-")}</div>
				</div>
				<div class="ia-status-cell">
					<span class="ia-status-label">${__("Next Step")}</span>
					<div class="ia-status-value">${injection_aps.ui.escape(context.next_step || "-")}</div>
				</div>
				<div class="ia-status-cell ia-status-cell-wide">
					<span class="ia-status-label">${__("Blocking Reason")}</span>
					<div class="ia-status-value ${context.blocking_reason ? "ia-risk-text" : "ia-muted"}">${injection_aps.ui.escape(context.blocking_reason || __("None"))}</div>
				</div>
			</div>
		`;
	};

	injection_aps.ui.render_actions = function (target, actions, handler) {
		if (!target) {
			return;
		}
		const safeActions = actions || [];
		if (!safeActions.length) {
			target.innerHTML = "";
			return;
		}
		target.innerHTML = `
			<div class="ia-action-strip">
				${safeActions
					.map((action, index) => {
						const enabled = Number(action.enabled || 0) === 1;
						return `
							<button
								type="button"
								class="btn btn-xs ${index === 0 ? "btn-primary" : "btn-default"} ia-action-btn"
								data-action-index="${index}"
								${enabled ? "" : "disabled"}
								title="${injection_aps.ui.escape(action.reason || "")}"
							>${injection_aps.ui.escape(action.label || "")}</button>
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
					if (!action || Number(action.enabled || 0) !== 1) {
						return;
					}
					await handler(action);
				});
			});
	};

	injection_aps.ui.run_action = async function (action, afterCall) {
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
		const response = await injection_aps.ui.xcall(
			{
				message: action.busy_message || __("APS is processing {0}...").replace("{0}", action.label || __("request")),
				success_message: action.success_message,
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
		const count = Number(result?.[warningKey] || 0);
		if (!count) {
			return;
		}
		const rowsKey = warningKey === "preflight_warning_count" ? "preflight_warnings" : "warnings";
		const warnings = result?.[rowsKey] || [];
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
					<button type="button" class="btn btn-sm btn-default" data-ia-close="1">${__("Close")}</button>
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
})();
