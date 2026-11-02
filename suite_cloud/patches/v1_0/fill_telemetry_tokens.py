import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")


def execute() -> None:
    """Stalwart sends each server's own telemetry token. Upgrade Agents on each cluster installs
    the tokens and switches Stalwart's push on."""

    for doctype in SERVER_DOCTYPES:
        for name in frappe.get_all(doctype, filters={"telemetry_key": ("is", "not set")}, pluck="name"):
            frappe.db.set_value(doctype, name, "telemetry_key", frappe.generate_hash(length=20))
        for name in frappe.get_all(doctype, pluck="name"):
            if not get_decrypted_password(doctype, name, "telemetry_secret", raise_exception=False):
                set_encrypted_password(doctype, name, frappe.generate_hash(length=40), "telemetry_secret")
