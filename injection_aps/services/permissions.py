from __future__ import annotations

from typing import Iterable

import frappe
from frappe import _
from frappe.permissions import setup_custom_perms


ROLE_PMC = "PMC"
ROLE_GMC = "GMC"

CORE_APS_ROLES = (ROLE_PMC, ROLE_GMC)

APS_READ_ROLES = {
	"System Manager",
	ROLE_GMC,
	ROLE_PMC,
	"Sales Manager",
	"Sales User",
	"Purchase Manager",
	"Purchase User",
	"Manufacturing Manager",
	"Manufacturing User",
	"Stock Manager",
	"Stock User",
}
APS_DEMAND_ROLES = {
	"System Manager",
	ROLE_GMC,
	ROLE_PMC,
	"Sales Manager",
	"Sales User",
	"Manufacturing Manager",
}
APS_PLAN_ROLES = {
	"System Manager",
	ROLE_GMC,
	ROLE_PMC,
	"Manufacturing Manager",
}
APS_APPROVE_ROLES = {
	"System Manager",
	ROLE_GMC,
	"Manufacturing Manager",
}
APS_RELEASE_ROLES = {
	"System Manager",
	ROLE_GMC,
	"Manufacturing Manager",
}
APS_EXECUTION_ROLES = {
	"System Manager",
	ROLE_GMC,
	ROLE_PMC,
	"Manufacturing Manager",
	"Manufacturing User",
}
APS_MRP_ROLES = {
	"System Manager",
	ROLE_GMC,
	ROLE_PMC,
	"Purchase Manager",
	"Purchase User",
	"Manufacturing Manager",
	"Stock Manager",
	"Stock User",
}
APS_ADMIN_ROLES = {
	"System Manager",
	ROLE_GMC,
	"Manufacturing Manager",
}

READ_FLAGS = {"read", "select", "report", "export", "print", "email", "share"}
READ_SELECT_FLAGS = {"read", "select"}
READ_NO_EXPORT_FLAGS = {"read", "select", "report", "print", "email"}
WRITE_FLAGS = {"read", "select", "write", "create", "report", "export", "import", "print", "email", "share"}
FULL_FLAGS = {
	"read",
	"select",
	"write",
	"create",
	"delete",
	"report",
	"export",
	"import",
	"print",
	"email",
	"share",
}
PERMISSION_FLAGS = FULL_FLAGS | {"submit", "cancel", "amend"}
CONFIG_WRITE_FLAGS = {"read", "select", "write", "create", "delete", "report", "export", "print", "email", "share"}

APS_PAGE_NAMES = (
	"aps-schedule-console",
	"aps-customer-schedule-progress",
	"aps-net-requirement-workbench",
	"aps-run-console",
	"aps-schedule-gantt",
	"aps-release-center",
	"aps-change-impact-center",
)
APS_PAGE_ROLE_MAP = {
	"aps-change-impact-center": {
		"System Manager",
		ROLE_GMC,
		ROLE_PMC,
		"Sales Manager",
		"Sales User",
		"Purchase Manager",
		"Purchase User",
		"Manufacturing Manager",
		"Manufacturing User",
		"Stock Manager",
		"Stock User",
	},
}
APS_WORKSPACE_NAMES = ("Injection APS",)

APS_DOCTYPE_PERMISSIONS = {
	"Customer Delivery Schedule": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Schedule Import Batch": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Demand Delta": {role: READ_FLAGS for role in APS_READ_ROLES},
	"APS Demand Pool": {
		ROLE_GMC: FULL_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
	},
	"APS Net Requirement": {
		ROLE_GMC: FULL_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
	},
	"APS Planning Run": {
		ROLE_GMC: FULL_FLAGS,
		ROLE_PMC: WRITE_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Schedule Result": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Downtime Window": {
		ROLE_GMC: FULL_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Segment Adjustment": {
		ROLE_GMC: FULL_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Exception Log": {
		ROLE_GMC: FULL_FLAGS,
		ROLE_PMC: {"read", "select", "write", "report", "export", "print", "email", "share"},
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Change Request": {
		ROLE_GMC: FULL_FLAGS,
		ROLE_PMC: WRITE_FLAGS,
		"Sales Manager": WRITE_FLAGS,
		"Sales User": WRITE_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Change Application Log": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
	},
	"APS Production Allocation": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Delivery Allocation": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Sales Manager": READ_FLAGS,
		"Sales User": READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Work Order Proposal Batch": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Shift Schedule Proposal Batch": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Release Batch": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Machine Capability": {
		ROLE_GMC: CONFIG_WRITE_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": CONFIG_WRITE_FLAGS,
		"Manufacturing User": READ_FLAGS,
		"Stock Manager": READ_FLAGS,
		"Stock User": READ_FLAGS,
	},
	"APS Mould-Machine Rule": {
		ROLE_GMC: CONFIG_WRITE_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Purchase Manager": READ_FLAGS,
		"Purchase User": READ_FLAGS,
		"Manufacturing Manager": CONFIG_WRITE_FLAGS,
		"Manufacturing User": READ_FLAGS,
	},
	"APS Color Transition Rule": {
		ROLE_GMC: CONFIG_WRITE_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Manufacturing Manager": CONFIG_WRITE_FLAGS,
		"Manufacturing User": READ_FLAGS,
	},
	"APS Freeze Rule": {
		ROLE_GMC: CONFIG_WRITE_FLAGS,
		ROLE_PMC: READ_FLAGS,
		"Manufacturing Manager": CONFIG_WRITE_FLAGS,
		"Manufacturing User": READ_FLAGS,
	},
	"APS Settings": {
		ROLE_GMC: READ_FLAGS,
		ROLE_PMC: READ_NO_EXPORT_FLAGS,
		"Manufacturing Manager": READ_FLAGS,
	},
}

DEPENDENCY_DOCTYPE_ROLE_PERMISSIONS = {
	"Work Order Scheduling": {
		"System Manager": FULL_FLAGS,
		ROLE_GMC: FULL_FLAGS,
		"GMC (Production Material Control - Manager)": FULL_FLAGS,
		"Manufacturing Manager": FULL_FLAGS,
	},
}

# Do not manufacture read access to ERPNext master or transaction records. APS
# APIs must honor the permissions and User Permissions supplied by ERPNext for
# Company, Customer, Item, Sales Order, Supplier, Employee, User, and similar
# records. Only the companion scheduling DocType has an explicit cross-app role
# contract required by the controlled release workflow.
DEPENDENCY_READ_DOCTYPES = tuple(DEPENDENCY_DOCTYPE_ROLE_PERMISSIONS)
LEGACY_DEPENDENCY_READ_DOCTYPES = (
	"Company",
	"Item",
	"Customer",
	"Sales Order",
	"Work Order",
	"BOM",
	"Warehouse",
	"Workstation",
	"Plant Floor",
	"Mold",
	"Mold Product",
	"Mold Default Material",
	"Work Order Scheduling",
	"Scheduling Item",
	"Delivery Plan",
	"Material Request",
	"Purchase Order",
	"Supplier",
	"Bin",
	"UOM",
	"Address",
	"Location",
	"Asset",
	"Employee",
	"User",
)


def ensure_roles_and_permissions():
	ensure_roles()
	ensure_aps_doctype_permissions()
	ensure_dependency_link_permissions()
	ensure_page_and_workspace_roles()
	frappe.clear_cache()


def ensure_roles():
	for role_name in CORE_APS_ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		role = frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
			}
		)
		role.insert(ignore_permissions=True)


def ensure_aps_doctype_permissions():
	for doctype, role_map in APS_DOCTYPE_PERMISSIONS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for role, flags in role_map.items():
			ensure_custom_docperm(doctype=doctype, role=role, flags=flags)


def ensure_dependency_link_permissions():
	_restore_legacy_dependency_link_permissions()
	for doctype, role_permissions in DEPENDENCY_DOCTYPE_ROLE_PERMISSIONS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for role, flags in role_permissions.items():
			ensure_custom_docperm(
				doctype=doctype,
				role=role,
				flags=flags,
				replace=False,
				preserve_standard=True,
			)


def _restore_legacy_dependency_link_permissions():
	"""Remove only the broad dependency grants previously managed by this app.

	When ERPNext already defines a permission row for the same role, restore that
	standard row rather than deleting it from a DocType using Custom DocPerms.
	"""
	legacy_roles = APS_READ_ROLES | APS_MRP_ROLES
	for doctype in LEGACY_DEPENDENCY_READ_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		retained_roles = set(DEPENDENCY_DOCTYPE_ROLE_PERMISSIONS.get(doctype) or {})
		for row in frappe.get_all(
			"Custom DocPerm",
			filters={
				"parent": doctype,
				"role": ("in", sorted(legacy_roles - retained_roles)),
				"permlevel": 0,
				"if_owner": 0,
			},
			fields=["name", "role"],
			limit_page_length=0,
		):
			standard_values = frappe.db.get_value(
				"DocPerm",
				{
					"parent": doctype,
					"role": row.get("role"),
					"permlevel": 0,
					"if_owner": 0,
				},
				fieldname=list(PERMISSION_FLAGS),
				as_dict=True,
			)
			if not standard_values:
				frappe.delete_doc("Custom DocPerm", row.get("name"), ignore_permissions=True)
				continue
			docperm = frappe.get_doc("Custom DocPerm", row.get("name"))
			changed = False
			for flag in PERMISSION_FLAGS:
				value = 1 if standard_values.get(flag) else 0
				if docperm.get(flag) != value:
					docperm.set(flag, value)
					changed = True
			if changed:
				docperm.save(ignore_permissions=True)


def ensure_page_and_workspace_roles():
	for page_name in APS_PAGE_NAMES:
		if frappe.db.exists("Page", page_name):
			_add_roles_to_child_table(
				"Page",
				page_name,
				"roles",
				APS_PAGE_ROLE_MAP.get(page_name, APS_READ_ROLES),
			)

	for workspace_name in APS_WORKSPACE_NAMES:
		if frappe.db.exists("Workspace", workspace_name):
			_add_roles_to_child_table("Workspace", workspace_name, "roles", APS_READ_ROLES)


def ensure_custom_docperm(
	doctype: str,
	role: str,
	flags: Iterable[str],
	permlevel: int = 0,
	replace: bool = True,
	preserve_standard: bool = False,
):
	if not role or not frappe.db.exists("Role", role):
		return
	if frappe.get_meta(doctype).istable:
		return
	setup_custom_perms(doctype)
	filters = {
		"parent": doctype,
		"role": role,
		"permlevel": permlevel,
		"if_owner": 0,
	}
	name = frappe.db.get_value("Custom DocPerm", filters)
	target_flags = set(flags or [])
	if preserve_standard:
		standard_values = frappe.db.get_value(
			"DocPerm",
			filters,
			fieldname=list(PERMISSION_FLAGS),
			as_dict=True,
		)
		if standard_values:
			target_flags.update(flag for flag in PERMISSION_FLAGS if standard_values.get(flag))

	values = {flag: 1 if flag in target_flags else 0 for flag in PERMISSION_FLAGS}
	if name:
		docperm = frappe.get_doc("Custom DocPerm", name)
		if not replace:
			for flag in PERMISSION_FLAGS:
				if docperm.get(flag):
					values[flag] = 1
		changed = False
		for fieldname, value in values.items():
			if docperm.get(fieldname) != value:
				docperm.set(fieldname, value)
				changed = True
		if changed:
			docperm.save(ignore_permissions=True)
		return

	docperm = frappe.get_doc(
		{
			"doctype": "Custom DocPerm",
			"parent": doctype,
			"parenttype": "DocType",
			"parentfield": "permissions",
			"role": role,
			"permlevel": permlevel,
			"if_owner": 0,
			**values,
		}
	)
	docperm.insert(ignore_permissions=True)


def _add_roles_to_child_table(doctype: str, docname: str, child_table_field: str, roles: Iterable[str]):
	doc = frappe.get_doc(doctype, docname)
	existing = {row.role for row in doc.get(child_table_field) or []}
	changed = False
	for role in sorted(roles):
		if not role or role in existing or not frappe.db.exists("Role", role):
			continue
		doc.append(child_table_field, {"role": role})
		changed = True
	if changed:
		doc.save(ignore_permissions=True)


def require_any_role(allowed_roles: Iterable[str], message: str | None = None):
	if frappe.session.user == "Administrator":
		return
	allowed = set(allowed_roles or [])
	if allowed.intersection(set(frappe.get_roles())):
		return
	frappe.throw(
		message or _("You do not have permission to perform this APS action."),
		frappe.PermissionError,
	)
