app_name = "injection_aps"
app_title = "Injection APS"
app_publisher = "JCE"
app_description = "Injection planning and scheduling for ERPNext"
app_email = "kaibo_wang@whjichen.cn"
app_license = "mit"

import pathlib as _pathlib

_apps_dir = _pathlib.Path(__file__).resolve().parents[2]


def _local_app_or_name(app_name):
	app_path = _apps_dir / app_name
	return str(app_path) if app_path.exists() else app_name


required_apps = [
	"erpnext",
	_local_app_or_name("zelin_pp"),
	_local_app_or_name("light_mes"),
	_local_app_or_name("mold_management"),
]

doctype_js = {
	"APS Planning Run": "public/js/aps_planning_run.js",
	"APS Work Order Proposal Batch": "public/js/aps_work_order_proposal_batch.js",
	"APS Shift Schedule Proposal Batch": "public/js/aps_shift_schedule_proposal_batch.js",
	"Customer Delivery Schedule": "public/js/customer_delivery_schedule.js",
	"APS Schedule Import Batch": "public/js/aps_schedule_import_batch.js",
	"APS Change Request": "public/js/aps_change_request.js",
	"APS Release Batch": "public/js/aps_release_batch.js",
	"APS Unallocated Delivery": "public/js/aps_unallocated_delivery.js",
}

doctype_list_js = {
	"APS Change Request": "public/js/aps_change_request_list.js",
}

doc_events = {
	"Delivery Plan": {
		"validate": "injection_aps.services.delivery_fulfillment.sync_delivery_plan_lineage",
	},
	"Delivery Note": {
		"before_validate": "injection_aps.services.delivery_fulfillment.inherit_delivery_note_lineage",
		"before_submit": "injection_aps.services.delivery_sync.validate_delivery_before_submit",
		"on_submit": "injection_aps.services.delivery_sync.queue_delivery_sync",
		"on_cancel": [
			"injection_aps.services.delivery_sync.retire_delivery_artifacts",
			"injection_aps.services.delivery_sync.queue_delivery_sync",
		],
		"on_trash": "injection_aps.services.delivery_sync.delete_delivery_artifacts",
	},
	"Stock Entry": {
		"before_submit": "injection_aps.services.execution_sync.validate_manufacture_before_submit",
		"on_submit": "injection_aps.services.execution_sync.queue_production_sync",
		"on_cancel": "injection_aps.services.execution_sync.queue_production_sync",
	},
}

before_install = "injection_aps.install.before_install"
after_install = "injection_aps.install.after_install"
after_migrate = "injection_aps.install.after_migrate"
before_uninstall = "injection_aps.uninstall.before_uninstall"

scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"injection_aps.services.customizations.sync_machine_capabilities_from_workstations",
		]
	},
	"hourly": [
		"injection_aps.services.shift_replan.scheduled_shift_replan",
	]
}
