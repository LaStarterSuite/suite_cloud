import frappe

SETTINGS = "Suite Cloud Settings"
FIELD = "telemetry_errors_per_hour"


def execute() -> None:
    """Stalwart pushes only the events Suite Cloud lists, so the Error Rate alert counts error events
    per hour instead of their share of all events. The old percentage has no meaning for it."""

    if not frappe.db.get_single_value(SETTINGS, FIELD):
        frappe.db.set_single_value(SETTINGS, FIELD, frappe.get_meta(SETTINGS).get_field(FIELD).default)
    frappe.db.delete("Singles", {"doctype": SETTINGS, "field": "telemetry_error_rate_percent"})
