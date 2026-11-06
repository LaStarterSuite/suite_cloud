import frappe

# A Single's field defaults only fill a site whose settings were never saved, so a site set up
# before the outbound scan fields has them empty, which the required mode refuses.
SETTINGS = "Suite Cloud Settings"
FIELDS = ("outbound_scan_mode", "outbound_scan_reject_score")


def execute() -> None:
    meta = frappe.get_meta(SETTINGS)
    for field in FIELDS:
        if not frappe.db.get_single_value(SETTINGS, field):
            frappe.db.set_single_value(SETTINGS, field, meta.get_field(field).default)
