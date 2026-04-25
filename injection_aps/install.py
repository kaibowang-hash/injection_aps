import frappe

from injection_aps.services.customizations import (
	ensure_default_settings,
	ensure_seed_records,
	ensure_standard_customizations,
)
from injection_aps.services.permissions import ensure_roles_and_permissions
from injection_aps.services.workspace import ensure_workspace_resources


def after_install():
	ensure_standard_customizations()
	ensure_default_settings()
	ensure_seed_records()
	ensure_workspace_resources()
	ensure_roles_and_permissions()
	frappe.clear_cache()


def after_migrate():
	ensure_standard_customizations()
	ensure_default_settings()
	ensure_seed_records()
	ensure_workspace_resources()
	ensure_roles_and_permissions()
	frappe.clear_cache()
