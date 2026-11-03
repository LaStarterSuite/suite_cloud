import frappe

from suite_cloud.telemetry import TELEMETRY_USER
from suite_cloud.telemetry.warehouse import Warehouse

# The role every tenant owner holds: no desk, and on each DocType only what a tenant owns
# (see suite_cloud.mail.tenancy.permissions). Operators are System Managers.
ROLES = (("Suite Tenant", 0),)


def after_install() -> None:
    setup()


def after_migrate() -> None:
    setup()


def setup() -> None:
    """Creates the roles, the telemetry user and data source, and fills new settings defaults; safe
    to run on every migrate."""

    for role_name, desk_access in ROLES:
        ensure_role(role_name, desk_access)

    ensure_user(TELEMETRY_USER, "Telemetry Agent")
    ensure_setting_defaults()
    Warehouse().ensure_data_source()


def ensure_role(role_name: str, desk_access: int) -> None:
    if not frappe.db.exists("Role", role_name):
        frappe.get_doc(
            {"doctype": "Role", "role_name": role_name, "desk_access": desk_access, "is_custom": 0}
        ).insert(ignore_permissions=True)
    elif frappe.db.get_value("Role", role_name, "desk_access") != desk_access:
        frappe.db.set_value("Role", role_name, "desk_access", desk_access)


def ensure_user(email: str, first_name: str) -> None:
    """A Website User with no roles: it can reach nothing but the API its credential is for."""

    if frappe.db.exists("User", email):
        return
    frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "first_name": first_name,
            "user_type": "Website User",
            "send_welcome_email": 0,
        }
    ).insert(ignore_permissions=True)


def ensure_setting_defaults() -> None:
    """Fills settings fields added after the singleton was first saved with their defaults.

    Field defaults only apply to new documents, so an existing site would otherwise fail the
    mandatory check the next time anyone saves Suite Cloud Settings.
    """

    settings = frappe.get_single("Suite Cloud Settings")
    changed = False
    for field in settings.meta.fields:
        if field.default and settings.get(field.fieldname) in (None, ""):
            settings.set(field.fieldname, field.default)
            changed = True

    if changed:
        settings.flags.ignore_mandatory = True
        settings.save(ignore_permissions=True)
        frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")
