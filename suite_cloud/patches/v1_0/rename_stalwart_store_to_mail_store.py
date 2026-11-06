import frappe

OLD = "Stalwart Store"
NEW = "Mail Store"


def execute() -> None:
    """Before the model sync, so the new DocType file finds its table. The outbound scanner now
    uses these stores too, not only Stalwart."""

    if frappe.db.exists("DocType", OLD) and not frappe.db.exists("DocType", NEW):
        frappe.rename_doc("DocType", OLD, NEW, force=True)
