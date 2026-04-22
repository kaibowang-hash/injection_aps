from __future__ import annotations

import json

import frappe

WORKSPACE_NAME = "Injection APS"
DASHBOARD_BLOCK_NAME = "Injection APS Dashboard"

DASHBOARD_HTML = """
<div class="ia-workspace">
	<div class="ia-head">
		<div>
			<h3 class="ia-title"></h3>
			<p class="ia-subtitle"></p>
		</div>
		<button type="button" class="ia-refresh"></button>
	</div>
	<div class="ia-feedback"></div>
	<div class="ia-grid ia-summary"></div>
	<div class="ia-grid ia-secondary"></div>
</div>
"""

DASHBOARD_STYLE = """
:host { display: block; }
.ia-workspace {
	border: 1px solid #d8dee9;
	border-radius: 18px;
	padding: 20px;
	background: linear-gradient(180deg, #fffef8 0%, #f8fafc 100%);
	color: #1f2937;
}
.ia-head {
	display: flex;
	align-items: flex-start;
	justify-content: space-between;
	gap: 12px;
	margin-bottom: 14px;
}
.ia-head h3 {
	margin: 0 0 4px;
	font-size: 20px;
}
.ia-head p,
.ia-feedback,
.ia-note {
	margin: 0;
	font-size: 12px;
	color: #64748b;
}
.ia-refresh {
	border: 1px solid #cbd5e1;
	border-radius: 999px;
	background: #fff;
	padding: 7px 14px;
	font-size: 12px;
	font-weight: 700;
	cursor: pointer;
}
.ia-grid {
	display: grid;
	grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
	gap: 12px;
	margin-top: 12px;
}
.ia-card {
	border: 1px solid #e2e8f0;
	border-radius: 14px;
	background: #fff;
	padding: 14px 15px;
}
.ia-label {
	display: block;
	font-size: 11px;
	text-transform: uppercase;
	letter-spacing: 0.05em;
	color: #64748b;
	margin-bottom: 6px;
}
.ia-value {
	font-size: 24px;
	line-height: 1.1;
	font-weight: 700;
	color: #0f172a;
}
.ia-note {
	margin-top: 6px;
}
"""

DASHBOARD_SCRIPT = """
const root = root_element;
const title = root.querySelector('.ia-title');
const subtitle = root.querySelector('.ia-subtitle');
const feedback = root.querySelector('.ia-feedback');
const summary = root.querySelector('.ia-summary');
const secondary = root.querySelector('.ia-secondary');
const refreshButton = root.querySelector('.ia-refresh');

title.textContent = __('Injection APS Overview');
subtitle.textContent = __('Demand, planning runs, release status and blocking exceptions stay isolated inside this app.');
refreshButton.textContent = __('Refresh');

function escapeHtml(value) {
	return frappe.utils.escape_html(value == null ? '' : String(value));
}

function renderCards(target, cards) {
	target.innerHTML = cards.map((card) => `
		<div class="ia-card">
			<span class="ia-label">${escapeHtml(card.label)}</span>
			<div class="ia-value">${escapeHtml(card.value)}</div>
			${card.note ? `<div class="ia-note">${escapeHtml(card.note)}</div>` : ''}
		</div>
	`).join('');
}

async function loadDashboard() {
	feedback.textContent = __('Loading APS snapshot...');
	try {
		const data = await frappe.xcall('injection_aps.api.app.get_workspace_dashboard_data');
		renderCards(summary, [
			{ label: __('Active Schedules'), value: data.active_schedules || 0, note: __('Current customer schedule versions') },
			{ label: __('Demand Rows'), value: data.open_demands || 0, note: __('Open APS demand pool rows') },
			{ label: __('Net Requirements'), value: data.open_net_requirements || 0, note: __('Current positive requirements') },
			{ label: __('Open Runs'), value: data.open_runs || 0, note: __('Draft / planned / approved runs') }
		]);
		renderCards(secondary, [
			{ label: __('Risk Exceptions'), value: data.blocking_exceptions || 0, note: __('Blocking and critical issues') },
			{ label: __('Applied Logs'), value: data.released_batches || 0, note: __('Formal work order / shift scheduling apply logs') },
			{ label: __('Formalized Results'), value: data.synced_results || 0, note: __('Results already reviewed into proposal / execution flow') },
			{ label: __('Machines Configured'), value: data.machine_capabilities || 0, note: __('APS machine capability rows') }
		]);
		feedback.textContent = __('APS workspace refreshed.');
	} catch (error) {
		console.error(error);
		feedback.textContent = __('Failed to load Injection APS workspace summary.');
	}
}

refreshButton?.addEventListener('click', loadDashboard);
loadDashboard();
"""


def ensure_workspace_resources():
	_ensure_dashboard_custom_block()
	_ensure_workspace_dashboard_layout()


def remove_workspace_resources():
	if frappe.db.exists("Workspace", WORKSPACE_NAME):
		workspace = frappe.get_doc("Workspace", WORKSPACE_NAME)
		content = _load_workspace_content(workspace)
		content = [
			block
			for block in content
			if not (
				block.get("type") == "custom_block"
				and (block.get("data") or {}).get("custom_block_name") == DASHBOARD_BLOCK_NAME
			)
		]
		workspace.content = json.dumps(content, separators=(",", ":"))
		workspace.set(
			"custom_blocks",
			[row for row in workspace.custom_blocks if row.custom_block_name != DASHBOARD_BLOCK_NAME],
		)
		workspace.save(ignore_permissions=True)

	if frappe.db.exists("Custom HTML Block", DASHBOARD_BLOCK_NAME):
		frappe.delete_doc("Custom HTML Block", DASHBOARD_BLOCK_NAME, force=1, ignore_permissions=True)


def _ensure_dashboard_custom_block():
	exists = frappe.db.exists("Custom HTML Block", DASHBOARD_BLOCK_NAME)
	if exists:
		doc = frappe.get_doc("Custom HTML Block", DASHBOARD_BLOCK_NAME)
	else:
		doc = frappe.new_doc("Custom HTML Block")
		doc.name = DASHBOARD_BLOCK_NAME
		doc.private = 0
	doc.html = DASHBOARD_HTML
	doc.style = DASHBOARD_STYLE
	doc.script = DASHBOARD_SCRIPT
	if exists:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)


def _ensure_workspace_dashboard_layout():
	if not frappe.db.exists("Workspace", WORKSPACE_NAME):
		return

	workspace = frappe.get_doc("Workspace", WORKSPACE_NAME)
	changed = False

	if not workspace.type:
		workspace.type = "Workspace"
		changed = True

	if not workspace.app:
		workspace.app = "injection_aps"
		changed = True

	if not workspace.icon:
		workspace.icon = "change-log"
		changed = True

	if not any(row.custom_block_name == DASHBOARD_BLOCK_NAME for row in workspace.custom_blocks):
		workspace.append(
			"custom_blocks",
			{
				"custom_block_name": DASHBOARD_BLOCK_NAME,
				"label": "Dashboard",
			},
		)
		changed = True

	content = _load_workspace_content(workspace)
	if not any(
		block.get("type") == "custom_block"
		and (block.get("data") or {}).get("custom_block_name") == DASHBOARD_BLOCK_NAME
		for block in content
	):
		insert_at = 2 if len(content) >= 2 else len(content)
		content.insert(
			insert_at,
			{
				"id": "custom-block-injection-aps-dashboard",
				"type": "custom_block",
				"data": {
					"custom_block_name": DASHBOARD_BLOCK_NAME,
					"col": 12,
				},
			},
		)
		changed = True

	if changed:
		workspace.content = json.dumps(content, separators=(",", ":"))
		workspace.save(ignore_permissions=True)


def _load_workspace_content(workspace) -> list[dict]:
	try:
		return json.loads(workspace.content or "[]")
	except Exception:
		return []
