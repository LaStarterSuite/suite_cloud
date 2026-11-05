import frappe

from suite_cloud.telemetry import TELEMETRY_USER

OLD = "telemetry@suite-cloud.internal"


def execute() -> None:
    if frappe.db.exists("User", OLD) and not frappe.db.exists("User", TELEMETRY_USER):
        frappe.rename_doc("User", OLD, TELEMETRY_USER, force=True, show_alert=False)
