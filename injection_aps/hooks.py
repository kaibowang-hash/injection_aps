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
}

doc_events = {
	"Delivery Note": {
		"on_submit": "injection_aps.services.delivery_sync.queue_delivery_sync",
		"on_cancel": "injection_aps.services.delivery_sync.queue_delivery_sync",
	},
	"Stock Entry": {
		"on_submit": "injection_aps.services.execution_sync.queue_production_sync",
		"on_cancel": "injection_aps.services.execution_sync.queue_production_sync",
	},
}

after_install = "injection_aps.install.after_install"
after_migrate = "injection_aps.install.after_migrate"
before_uninstall = "injection_aps.uninstall.before_uninstall"

scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"injection_aps.services.customizations.sync_machine_capabilities_from_workstations",
		]
	}
}
