import frappe
from frappe.model.utils.rename_field import rename_field

SETTINGS = "Suite Cloud Settings"
INTERVAL = "metrics_push_interval_seconds"
DISCARD_AFTER = "webhook_discard_after_minutes"


def execute() -> None:
    """The agents no longer scrape Stalwart's Prometheus endpoint or retry batches, so the metrics
    passwords and Agent Max Attempts go. The metrics interval is Stalwart's own push now. A
    Single's defaults fill only settings never saved, so the push fields get theirs here."""

    rename_field(SETTINGS, "agent_metrics_interval_seconds", INTERVAL)
    meta = frappe.get_meta(SETTINGS)
    for field in (INTERVAL, DISCARD_AFTER):
        if not frappe.db.get_single_value(SETTINGS, field):
            frappe.db.set_single_value(SETTINGS, field, meta.get_field(field).default)
    frappe.db.delete("Singles", {"doctype": SETTINGS, "field": "agent_max_attempts"})
    frappe.db.delete(
        "__Auth", {"doctype": ("in", ("Stalwart Cluster", "Egress Gateway")), "fieldname": "metrics_password"}
    )
