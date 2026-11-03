import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password


def execute() -> None:
    """Clusters made before the sending limits have no token for the hook; Sync Config on each
    cluster sends the new one to Stalwart."""

    for name in frappe.get_all("Stalwart Cluster", pluck="name"):
        if get_decrypted_password("Stalwart Cluster", name, "quota_hook_token", raise_exception=False):
            continue
        set_encrypted_password("Stalwart Cluster", name, frappe.generate_hash(length=32), "quota_hook_token")
