import frappe


def execute() -> None:
    """Suite Cloud enforces the sending limits now, and the agent goes. A Server Job keeps its
    callback under the new name, so a retried job finds it. Each cluster is moved by hand: Sync
    Config swaps the hooks, and the agent is removed from each node."""

    frappe.db.set_value(
        "Server Job",
        {"callback": "after_install_agent"},
        "callback",
        "after_install_telemetry",
        update_modified=False,
    )
