import frappe
from frappe.model.utils.rename_field import rename_field

SETTINGS = "Suite Cloud Settings"
INTERVAL = "metrics_push_interval_seconds"


def execute() -> None:
    """The agents no longer scrape Stalwart's Prometheus endpoint or retry batches, so the metrics
    passwords and Agent Max Attempts go. The metrics interval is Stalwart's own push now."""

    rename_field(SETTINGS, "agent_metrics_interval_seconds", INTERVAL)
    if not frappe.db.get_single_value(SETTINGS, INTERVAL):
        frappe.db.set_single_value(SETTINGS, INTERVAL, frappe.get_meta(SETTINGS).get_field(INTERVAL).default)
    frappe.db.delete("Singles", {"doctype": SETTINGS, "field": "agent_max_attempts"})
    frappe.db.delete(
        "__Auth", {"doctype": ("in", ("Stalwart Cluster", "Egress Gateway")), "fieldname": "metrics_password"}
    )
