"""What provisioning puts on a server for telemetry: the telemetry token, and the Server Jobs that
install it and switch Stalwart's push on."""

from typing import TYPE_CHECKING

import frappe
from frappe.utils import cint

from suite_cloud.mail.cluster import push, telemetry
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job
from suite_cloud.utils import get_config, log_exception

if TYPE_CHECKING:
    from frappe.model.document import Document

PLAYBOOK = "install-telemetry.yml"
ROLL_OUT_KEY = "suite_cloud:telemetry_roll_out"  # the settings change the last roll-out applied
ROLL_OUT_TIMEOUT = 600  # seconds a roll-out may hold, or wait for, the roll-out lock
NODE_BUILDER = "suite_cloud.mail.cluster.bootstrap.build_node_variables"
GATEWAY_BUILDER = "suite_cloud.mail.cluster.egress.build_gateway_variables"


def is_telemetry_enabled() -> bool:
    return bool(cint(get_config("enable_telemetry")))


def telemetry_variables(context: dict) -> dict:
    """What install-telemetry.yml needs beyond the node and gateway variables. The context's
    ``restart_stalwart`` restarts Stalwart even when its environment did not change, which starts
    OTLP."""

    return {"restart_stalwart": bool(context.get("restart_stalwart"))}


def install_telemetry(server: Document) -> Document:
    """A Server Job that installs the server's telemetry token. Stalwart restarts only when its
    environment changed."""

    return telemetry_job(server)


def telemetry_job(server: Document, restart_stalwart: bool = False) -> Document:
    is_gateway = server.doctype == "Egress Gateway"
    action = "Start telemetry push" if restart_stalwart else "Install telemetry token"
    context = {"gateway" if is_gateway else "node": server.name, "restart_stalwart": restart_stalwart}
    return create_server_job(
        server,
        PLAYBOOK,
        title=f"{action} on {server.hostname}",
        context=context,
        variables_builder=GATEWAY_BUILDER if is_gateway else NODE_BUILDER,
        callback="after_install_telemetry",
    )


def install_telemetry_tokens(cluster: Document) -> list[str]:
    """Rolls the telemetry tokens out to every server of the cluster: its nodes, then each of its
    gateways."""

    owners = [cluster, *(frappe.get_doc("Egress Gateway", name) for name in active_gateways(cluster))]
    return [name for owner in owners for name in roll_out(owner)]


def roll_out(owner: Document) -> list[str]:
    """Where every server of a cluster or gateway has its token, the push is switched on.
    Elsewhere each server gets its token."""

    if owner.status == "Active" and telemetry.is_push_enabled(owner):
        return start_push(owner)
    return [telemetry_job(server).name for server in owner_servers(owner)]


def start_push(owner: Document) -> list[str]:
    """Pushes the plan, which now holds the WebHooks and OTLP, then restarts Stalwart on each
    server to start OTLP."""

    if owner.status != "Active":
        return []  # its first push and start include the push already
    owner.push_config()
    return [telemetry_job(server, restart_stalwart=True).name for server in owner_servers(owner)]


def owner_servers(owner: Document) -> list[Document]:
    """The servers running a cluster's or gateway's plan that a job can reach."""

    if owner.doctype == "Egress Gateway":
        return [owner] if owner.status == "Active" else []
    nodes = frappe.get_all("Stalwart Node", filters_for(owner, ("Active", "Draining")), pluck="name")
    return [frappe.get_doc("Stalwart Node", name) for name in nodes]


def active_gateways(cluster: Document | None = None) -> list[str]:
    return frappe.get_all("Egress Gateway", filters_for(cluster, ("Active",)), pluck="name")


def mark_token_installed(server: Document) -> None:
    """A job wrote the token to the server's stalwart.env. When it was the last server of its
    cluster or gateway without one, the push is switched on, in a job of its own so a failed
    push never fails the job that installed the token."""

    owner = telemetry.get_owner(server)
    was_enabled = telemetry.is_push_enabled(owner)
    server.db_set("telemetry_token_installed", 1, update_modified=False)
    if was_enabled or not telemetry.is_push_enabled(owner):
        return
    if frappe.flags.do_not_enqueue:
        start_push_job(owner.doctype, owner.name)
    else:
        frappe.enqueue(
            start_push_job, doctype=owner.doctype, name=owner.name, queue="long", enqueue_after_commit=True
        )


def start_push_job(doctype: str, name: str) -> None:
    """A failure shows on the cluster's form; Install Telemetry Tokens tries again."""

    owner = frappe.get_doc(doctype, name)
    if doctype == "Stalwart Cluster":
        with push.recorded_failure(name):
            start_push(owner)
    else:
        start_push(owner)


def rotate_telemetry_token(server: Document) -> str | None:
    """A new secret, written to the server's stalwart.env by a job that restarts Stalwart. Until
    then Stalwart sends the old one, is refused and retries."""

    server.reload()  # the form sends its unsaved edits too; only the secret is saved
    server.telemetry_secret = frappe.generate_hash(length=40)
    server.save(ignore_permissions=True)
    if server.status not in ("Active", "Draining"):
        return None  # provisioning writes the new token
    return telemetry_job(server).name


def filters_for(cluster: Document | None, statuses: tuple[str, ...]) -> dict:
    filters = {"status": ("in", statuses)}
    if cluster:
        filters["cluster"] = cluster.name
    return filters


def switch_off_push() -> None:
    """With telemetry off each active cluster's and gateway's config is pushed again: its plan
    then holds the WebHooks and OTLP switched off."""

    for name in frappe.get_all("Stalwart Cluster", {"status": "Active"}, pluck="name"):
        push.resync_cluster_after_commit(name)
    for name in active_gateways():
        try:
            frappe.get_doc("Egress Gateway", name).push_config()
        except Exception:
            log_exception(f"Telemetry not switched off on {name}")


def enqueue_roll_out_telemetry() -> None:
    """Enable Telemetry changed. In the background, since the roll-out loads every server of every
    cluster and creates a job for each. Queued even where jobs otherwise run at once
    (``do_not_enqueue``): it reaches every server of the site, so a test runs it only by name."""

    frappe.enqueue(roll_out_telemetry, queue="long", enqueue_after_commit=True)


def roll_out_telemetry() -> list[str]:
    """Each turn of the setting queues a roll-out, and each reads the setting when it runs. They run
    one at a time, and one that finds the settings as the last roll-out left them does nothing, so
    quick turns end in the last one without a second job per server."""

    lock = f"{frappe.local.site}:suite_cloud:roll-out-telemetry"
    with frappe.cache.lock(lock, timeout=ROLL_OUT_TIMEOUT, blocking_timeout=ROLL_OUT_TIMEOUT):
        change = str(frappe.db.get_single_value("Suite Cloud Settings", "modified"))
        if frappe.cache.get_value(ROLL_OUT_KEY) == change:
            return []
        names = start_telemetry_jobs()
        if not frappe.in_test:
            frappe.db.commit()  # the jobs exist before the change is marked as rolled out
        frappe.cache.set_value(ROLL_OUT_KEY, change)
    return names


def start_telemetry_jobs() -> list[str]:
    """Every cluster and gateway gets the push, or loses it while telemetry is off. A failed push
    is an Error Log, not an exception: the setting is already saved."""

    if not is_telemetry_enabled():
        switch_off_push()
        return []
    names = []
    owners = [
        *(
            frappe.get_doc("Stalwart Cluster", name)
            for name in frappe.get_all("Stalwart Cluster", pluck="name")
        ),
        *(frappe.get_doc("Egress Gateway", name) for name in active_gateways()),
    ]
    for owner in owners:
        try:
            names += roll_out(owner)
        except Exception:
            log_exception(f"Telemetry not rolled out to {owner.name}")
    return names
