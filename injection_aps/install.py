import frappe

from injection_aps.services.customizations import (
	ensure_default_settings,
	ensure_seed_records,
	ensure_standard_customizations,
)
from injection_aps.services.permissions import ensure_roles, ensure_roles_and_permissions
from injection_aps.services.workspace import ensure_workspace_resources


def before_install():
	# Frappe imports Workspace records during schema sync, before after_install.
	# Create link-target roles first so a fresh site can validate those records.
	ensure_roles()


def after_install():
	ensure_standard_customizations()
	ensure_default_settings()
	ensure_seed_records()
	# Workspace shortcuts can reference APS/GMC roles.  Those roles must exist
	# before Frappe validates and saves the workspace during a fresh install.
	ensure_roles()
	ensure_workspace_resources()
	# Apply DocType/Page/Workspace permissions after every referenced resource
	# exists; this also reruns the idempotent role creation guard.
	ensure_roles_and_permissions()
	frappe.clear_cache()


def after_migrate():
	ensure_standard_customizations()
	ensure_default_settings()
	ensure_seed_records()
	ensure_roles()
	ensure_workspace_resources()
	ensure_roles_and_permissions()
	frappe.clear_cache()
