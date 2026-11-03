import frappe

from suite_cloud.mail.cluster import push
from suite_cloud.telemetry.install import active_gateways, owner_servers, telemetry_job


def execute() -> None:
    """Suite Cloud enforces the sending limits now, and the agent goes. A Server Job keeps its
    callback under the new name, so a retried job finds it. Each Active cluster gets the new hook
    and loses the agent in a job of its own."""

    frappe.db.set_value(
        "Server Job",
        {"callback": "after_install_agent"},
        "callback",
        "after_install_telemetry",
        update_modified=False,
    )
    for name in frappe.get_all("Stalwart Cluster", {"status": "Active"}, pluck="name"):
        frappe.enqueue(move_cluster, cluster=name, queue="long", enqueue_after_commit=True)


def move_cluster(cluster: str) -> list[str]:
    """Pushes the plan, which swaps the agent's hook for Suite Cloud's, then removes the agent from
    every server with a telemetry install job. A failed push shows on the cluster's form; Sync
    Config and Install Telemetry Tokens run the same steps again."""

    doc = frappe.get_doc("Stalwart Cluster", cluster)
    with push.recorded_failure(cluster):
        doc.push_config()
    owners = [doc, *(frappe.get_doc("Egress Gateway", name) for name in active_gateways(doc))]
    return [telemetry_job(server).name for owner in owners for server in owner_servers(owner)]
