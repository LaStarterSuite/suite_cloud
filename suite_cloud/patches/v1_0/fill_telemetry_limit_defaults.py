import frappe

# A Single's field defaults only fill a site whose settings were never saved, so sites set up
# before the limit fields existed have them empty, which the now required fields refuse.
SETTINGS = "Suite Cloud Settings"
# The first was telemetry_*; rename_telemetry_limits_to_agent runs before this patch.
FIELDS = (
    "agent_requests_per_minute",
    "telemetry_silence_minutes",
)


def execute() -> None:
    meta = frappe.get_meta(SETTINGS)
    for field in FIELDS:
        if frappe.db.get_single_value(SETTINGS, field):
            continue
        frappe.db.set_single_value(SETTINGS, field, meta.get_field(field).default)
