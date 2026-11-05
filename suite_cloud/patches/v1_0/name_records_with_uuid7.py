from uuid import uuid7

import frappe

from suite_cloud.patches.v1_0.tenant_id_and_secret import is_uuid

DOCTYPES = (
    "DNS Record",
    "Mailing List Recipient",
    "Server Job",
    "Stalwart Store",
    "Server Alert",
)  # renamed before this runs


def execute() -> None:
    for doctype in DOCTYPES:
        # A queued or running Server Job loads itself by its current name.
        filters = {"status": ["not in", ["Pending", "Running"]]} if doctype == "Server Job" else {}
        for name in frappe.get_all(doctype, filters, pluck="name"):
            if not is_uuid(name):
                frappe.rename_doc(
                    doctype, name, str(uuid7()), force=True, show_alert=False, rebuild_search=False
                )
