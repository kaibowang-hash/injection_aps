app_name = "injection_aps"
app_title = "Injection APS"
app_publisher = "JCE"
app_description = "Injection planning and scheduling for ERPNext"
app_email = "kaibo_wang@whjichen.cn"
app_license = "mit"

required_apps = ["erpnext", "zelin_pp", "light_mes", "mold_management"]

doctype_js = {
	"APS Planning Run": "public/js/aps_planning_run.js",
	"APS Work Order Proposal Batch": "public/js/aps_work_order_proposal_batch.js",
	"APS Shift Schedule Proposal Batch": "public/js/aps_shift_schedule_proposal_batch.js",
	"Customer Delivery Schedule": "public/js/customer_delivery_schedule.js",
	"APS Schedule Import Batch": "public/js/aps_schedule_import_batch.js",
	"APS Release Batch": "public/js/aps_release_batch.js",
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
