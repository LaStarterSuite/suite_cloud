import frappe
from frappe.model.utils.rename_field import rename_field

SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")
FIELDS = {
    "telemetry_key": "suite_cloud_key",
    "telemetry_secret": "suite_cloud_secret",
    "telemetry_token_installed": "suite_cloud_token_installed",
}


def execute() -> None:
    """Each server's token is its Suite Cloud token now: Stalwart sends it on the sending-limit hook
    too, and the cluster's Quota Hook Token goes. Each server needs SUITE_CLOUD_TOKEN in its
    stalwart.env before its cluster's next Sync Config."""

    for doctype in SERVER_DOCTYPES:
        for old, new in FIELDS.items():
            rename_field(doctype, old, new)  # a Password field's __Auth rows move with it
    rename_field("Suite Cloud Settings", "agent_requests_per_minute", "server_requests_per_minute")
    frappe.db.set_value(
        "Server Job",
        {"callback": "after_install_telemetry"},
        "callback",
        "after_install_token",
        update_modified=False,
    )
    frappe.db.delete("__Auth", {"doctype": "Stalwart Cluster", "fieldname": "quota_hook_token"})
