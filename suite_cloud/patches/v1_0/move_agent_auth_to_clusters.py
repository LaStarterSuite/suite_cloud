import frappe
from frappe.utils.password import get_decrypted_password

SETTINGS = "Suite Cloud Settings"
OLD_FIELDS = ("telemetry_secret", "telemetry_previous_secret", "telemetry_previous_secret_until")


def execute() -> None:
    """The Agents signed in with one Telemetry Secret shared by every cluster. Enable Telemetry took
    over the secret's other job: turning telemetry on and off. The clusters' own agent credentials
    that followed went with the agent."""

    if get_decrypted_password(SETTINGS, SETTINGS, "telemetry_secret", raise_exception=False):
        frappe.db.set_single_value(SETTINGS, "enable_telemetry", 1)
    frappe.db.delete("__Auth", {"doctype": SETTINGS, "fieldname": ("in", OLD_FIELDS)})
    frappe.db.delete("Singles", {"doctype": SETTINGS, "field": ("in", OLD_FIELDS)})
