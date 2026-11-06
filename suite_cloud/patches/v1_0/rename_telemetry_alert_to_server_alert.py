import frappe

OLD = "Telemetry Alert"
NEW = "Server Alert"
OLD_TYPE = "Silent Host"
NEW_TYPE = "Metrics Silent"  # OTLP was what kept a Silent Host alert closed on an idle server


def execute() -> None:
    """Before the model sync, so the new DocType file finds its table. Silent Host is now Metrics
    Silent: the outbound scan shipper gets an alert of its own."""

    if frappe.db.exists("DocType", OLD) and not frappe.db.exists("DocType", NEW):
        frappe.rename_doc("DocType", OLD, NEW, force=True)
    if not frappe.db.table_exists(NEW):
        return
    frappe.db.sql(
        f"UPDATE `tab{NEW}` SET alert_type = %(new)s, title = REPLACE(title, %(old_title)s, %(new_title)s) "
        "WHERE alert_type = %(old)s",
        {"new": NEW_TYPE, "old": OLD_TYPE, "old_title": f"{OLD_TYPE}:", "new_title": f"{NEW_TYPE}:"},
    )
