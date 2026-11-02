"""Node lifecycle: provisioning, bootstrap completion, health, draining and upgrades."""

import re
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now

from suite_cloud.mail.cluster import dns, plan
from suite_cloud.mail.stalwart import forget_sessions, get_client, has_credentials
from suite_cloud.mail.stalwart.credentials import Credential
from suite_cloud.mail.stalwart.errors import StalwartError, StalwartUnauthorizedError
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job
from suite_cloud.telemetry.agent.deploy import agent_variables, mark_token_installed
from suite_cloud.utils import get_config, log_error, log_exception

if TYPE_CHECKING:
    from frappe.model.document import Document

BOOTSTRAP_DEADLINE_MINUTES = 45
NODE_VARIABLES_BUILDER = "suite_cloud.mail.cluster.bootstrap.build_node_variables"
SECRET_VARIABLES = (
    "admin_password",
    "env_normal",
    "env_bootstrap",
    "env_recovery",
    "config_json",
    "bootstrap_ndjson",
    "cluster_ndjson",
    "agent_config_json",
)


# --- provisioning ---------------------------------------------------------------------------


def serves_clients(node: Document) -> bool:
    """Outbound-only nodes run no listeners: nothing to wait for, nothing to put in ingress DNS."""

    return (node.role or "full") != "outbound"


def provision_node(node: Document) -> Document:
    cluster = node.get_cluster()
    if (
        cluster.status == "Failed"
        or (cluster.status == "Pending" and not _has_other_bootstrap_node(cluster, node))
        or _holds_the_only_data_store(cluster, node)
    ):
        if not serves_clients(node):
            frappe.throw(_("The first node must serve clients; pick the full or frontend role."))
        playbook = "bootstrap-cluster.yml"
        node.db_set("is_bootstrap_node", 1, update_modified=False)
        cluster.db_set({"status": "Bootstrapping", "bootstrap_node": node.name}, update_modified=False)
        cluster.bump_config_version(plan.cluster_plan(cluster))
    elif cluster.status == "Bootstrapping" and node.is_bootstrap_node:
        playbook = "bootstrap-cluster.yml"
    elif cluster.status == "Active":
        playbook = "configure-node.yml"
    else:
        frappe.throw(_("The cluster is still bootstrapping; provision more nodes once it is active."))

    node.set_status("Provisioning")
    return create_server_job(
        node,
        playbook,
        title=f"Provision {node.hostname}",
        context={"node": node.name},
        variables_builder=NODE_VARIABLES_BUILDER,
        callback="after_provision",
    )


def _has_other_bootstrap_node(cluster: Document, node: Document) -> bool:
    return bool(cluster.bootstrap_node and cluster.bootstrap_node != node.name)


def _holds_the_only_data_store(cluster: Document, node: Document) -> bool:
    """A single-node cluster's embedded store lives on its node, so provisioning that node again
    may start from an empty store. Joining (configure-node.yml) would leave it unset; bootstrapping
    again sets it up and skips whatever is already in place."""

    return bool(cluster.single_node) and cluster.status == "Active" and cluster.bootstrap_node == node.name


def upgrade_node(node: Document) -> Document:
    if not node.enabled:
        frappe.throw(_("Enable the node first."))
    if node.status == "Active":
        drain_node(node)
    return create_server_job(
        node,
        "upgrade-stalwart.yml",
        title=f"Upgrade {node.hostname} to {node.get_cluster().stalwart_version}",
        context={"node": node.name},
        variables_builder=NODE_VARIABLES_BUILDER,
        callback="after_upgrade",
    )


def run_commands(server: Document, commands: list[str], title: str) -> Document:
    return create_server_job(
        server,
        "run-commands.yml",
        title=title,
        context={"commands": commands},
        variables_builder="suite_cloud.mail.cluster.bootstrap.build_command_variables",
    )


# The only ad-hoc commands a job may run on a server: opening a firewall port. The job's context
# is stored on a row operators can write, so the shape is checked here, not trusted.
ALLOWED_COMMANDS = (re.compile(r"^ufw allow \d{1,5}/tcp$"),)


def build_command_variables(context: dict) -> dict:
    commands = [str(c) for c in context.get("commands") or []]
    for command in commands:
        if not any(pattern.match(command) for pattern in ALLOWED_COMMANDS):
            frappe.throw(_("Command {0} is not one a Server Job may run.").format(command))
    return {"commands": commands}


def build_node_variables(context: dict) -> dict:
    """Everything the node playbooks need; secrets are listed so the job snapshot redacts them."""

    node = frappe.get_doc("Stalwart Node", context["node"])
    cluster = node.get_cluster()
    variables = {
        "node_hostname": node.hostname,
        "cluster_hostname": cluster.hostname,
        "ssh_port": node.ssh_port or cluster.ssh_port,
        "stalwart_version": cluster.stalwart_version or get_config("stalwart_version"),
        "stalwart_url_template": get_config("stalwart_download_url_template"),
        "stalwart_cli_version": get_config("stalwart_cli_version"),
        "stalwart_cli_url_template": get_config("stalwart_cli_download_url_template"),
        "systemd_unit": plan.systemd_unit(),
        "firewall_ports": list(plan.FIREWALL_PORTS),
        "wait_ports": [25, 443] if serves_clients(node) else [],
        "recovery_port": plan.BOOTSTRAP_PORT,
        "admin_user": cluster.admin_username,
        "admin_password": cluster.get_password("admin_password"),
        "config_version": cluster.config_version or 0,
        "plan_marker": plan.marker(recovery_plan := plan.recovery_plan(cluster)),
        "env_normal": plan.render_env(plan.node_env(node, "normal")),
        "env_bootstrap": plan.render_env(plan.node_env(node, "bootstrap")),
        "env_recovery": plan.render_env(plan.node_env(node, "recovery")),
        "config_json": frappe.as_json(plan.node_config(cluster)),
        "bootstrap_ndjson": plan.to_ndjson(bootstrap_plan := plan.bootstrap_plan(cluster)),
        "defaults_ndjson": plan.to_ndjson(plan.defaults_plan()),
        "cluster_ndjson": plan.to_ndjson(recovery_plan),
        "__secret_keys__": list(SECRET_VARIABLES),
        "__secret_values__": [
            cluster.get_password("admin_password"),
            plan.get_telemetry_token(node),
            *plan.secret_strings(bootstrap_plan),
            *plan.secret_strings(recovery_plan),
        ],
    }
    agent = agent_variables(node, context)
    variables["__secret_values__"] += agent.pop("__secret_values__")
    variables.update(agent)
    return variables


# --- callbacks --------------------------------------------------------------------------------


def after_provision(node: Document, job: Document) -> None:
    """The playbook finished: the node is installed and answering locally."""

    cluster = node.get_cluster()
    if node.is_bootstrap_node and cluster.status == "Failed" and cluster.bootstrap_node == node.name:
        cluster.db_set("status", "Bootstrapping", update_modified=False)  # a retried bootstrap succeeded
    mark_token_installed(node)  # while the node is not running yet, so the push follows no change
    node.db_set(
        {
            "status": "Provisioned",
            "provisioned_at": now(),
            "installed_version": node.get_cluster().stalwart_version,
        },
        update_modified=False,
    )
    if node.is_bootstrap_node:
        # The certificate check goes through the cluster hostname, so it must resolve to us.
        dns.sync_node_records(node, include_ingress=True)
    else:
        dns.sync_node_records(node, include_ingress=False)
    dns.sync_spf_record(node.get_cluster())
    check_node(node)


def after_upgrade(node: Document, job: Document) -> None:
    node.db_set(
        {"installed_version": node.get_cluster().stalwart_version, "provisioned_at": now()},
        update_modified=False,
    )
    node.set_status("Provisioned")
    check_node(node)
    mark_token_installed(node)  # the upgrade wrote stalwart.env


# --- health ------------------------------------------------------------------------------------


def check_node(node: Document) -> bool:
    """Promotes a Provisioned node to Active once Stalwart confirms it; fails it after a deadline."""

    cluster = node.get_cluster()
    if cluster.status == "Bootstrapping" and node.is_bootstrap_node:
        return finish_bootstrap(cluster)

    if cluster.status != "Active":
        return False

    try:
        registry = cluster.get_client().cluster_nodes.find_by_hostname(node.hostname)
    except StalwartError as e:
        return _not_ready(node, str(e))

    if problem := _registry_problem(cluster, registry):
        return _not_ready(node, problem)

    node.db_set(
        {
            "node_id": (registry or {}).get("nodeId") or 0,
            "last_health_at": now(),
            "last_error": None,
        },
        update_modified=False,
    )
    if node.status == "Provisioned":  # Draining stays put until an operator restores the node
        activate_node(node)
    return True


def activate_node(node: Document) -> None:
    if not node.enabled:
        frappe.throw(_("Enable the node first."))
    node.set_status("Active")
    dns.sync_node_records(node, include_ingress=serves_clients(node))
    dns.sync_spf_record(node.get_cluster())


def _push_initial_config(cluster: Document) -> None:
    """Objects the recovery stage cannot create (the disabled-accounts role, the quota backstop)
    land here."""

    try:
        cluster.push_config()
    except StalwartError as e:
        cluster.db_set("drift_report", frappe.as_json({"error": str(e)}), update_modified=False)
        log_error(f"Initial config push failed for {cluster.name}", message=str(e))


def _registry_problem(cluster: Document, registry: dict | None) -> str | None:
    """A single node without a coordinator may not hold a lease; the cluster answering is enough."""

    if registry is None and cluster.coordinator != "Disabled":
        return "Node registry entry absent"
    if registry and registry.get("status") != "active":
        return f"Node registry status: {registry.get('status')}"
    return None


def _not_ready(node: Document, detail: str) -> bool:
    """Records the problem; only a node still waiting to come up is failed after the deadline."""

    started = get_datetime(node.provisioned_at or now())
    expired = get_datetime(now()) > add_to_date(started, minutes=BOOTSTRAP_DEADLINE_MINUTES)
    if expired and node.status == "Provisioned":
        node.set_status("Failed", f"Not healthy after {BOOTSTRAP_DEADLINE_MINUTES} minutes: {detail}")
    else:
        node.db_set("last_error", detail[:1000], update_modified=False)
    return False


def finish_bootstrap(cluster: Document) -> bool:
    """Turns a bootstrapping cluster active once its first node serves a valid certificate.

    Reached repeatedly (button or cron) until it succeeds: the ACME DNS-01 issuance runs on the
    node after the playbook ends, so the HTTPS endpoint is not usable straight away.
    """

    node = frappe.get_doc("Stalwart Node", cluster.bootstrap_node) if cluster.bootstrap_node else None
    if not node or node.status != "Provisioned":
        return False

    try:
        admin = cluster.get_admin_client()
        ensure_api_key(cluster, admin)
        _set_default_certificate(cluster, admin)
        registry = admin.cluster_nodes.find_by_hostname(node.hostname)
        problem = _registry_problem(cluster, registry)
    except StalwartError as e:
        problem = str(e)

    if problem:
        _not_ready(node, problem)
        if node.status == "Failed":
            cluster.db_set("status", "Failed", update_modified=False)
        return False

    cluster.db_set({"status": "Active", "last_config_sync_at": now()}, update_modified=False)
    _push_initial_config(cluster)
    node.db_set(
        {
            "node_id": (registry or {}).get("nodeId") or 0,
            "last_health_at": now(),
            "last_error": None,
        },
        update_modified=False,
    )
    activate_node(node)
    return True


def ensure_api_key(target: Document, admin) -> None:
    """Mints the management key for a cluster or gateway unless the stored one still works.

    A stored key can be dead: a re-bootstrapped data store never saw it, and a re-run of the
    recovery-stage plan replaces the admin's credentials, keys included.

    No other key is deleted here. The cron, the job callback and the form buttons can run this
    at once, and one run could delete the key another has just minted and is about to store.
    Minting only adds keys, so whichever is stored works; the extras have secrets nobody kept.
    """

    if _api_key_works(target):
        return
    _, secret = admin.api_keys.create_secret(
        Credential(description=plan.API_KEY_DESCRIPTION, permissions=plan.api_key_permissions())
    )
    target.api_key = secret
    target.save(ignore_permissions=True)
    forget_sessions(target)


def _api_key_works(target: Document) -> bool:
    if not target.get_password("api_key", raise_exception=False):
        return False
    forget_sessions(target)  # a session cached for an earlier data store would hide a dead key
    try:
        get_client(target)
    except StalwartUnauthorizedError:
        return False
    return True


def _set_default_certificate(cluster: Document, client) -> None:
    """Non-SNI clients (SMTP) need a default certificate; pick the issued one for the hostname."""

    try:
        for certificate in client.objects("Certificate").get_all(
            properties=["id", "subjectAlternativeNames"]
        ):
            names = certificate.get("subjectAlternativeNames") or []
            names = list(names.keys()) if isinstance(names, dict) else list(names)
            if cluster.hostname in names:
                client.singleton("SystemSettings").write({"defaultCertificateId": certificate["id"]})
                return
    except StalwartError:
        log_exception(f"Could not pick a default certificate for {cluster.name}", cluster)


# --- draining / removal -----------------------------------------------------------------------


def drain_node(node: Document) -> None:
    """Takes the node out of the ingress round-robin; Stalwart keeps running on it."""

    dns.sync_node_records(node, include_ingress=False)
    node.set_status("Draining" if node.status == "Active" else "Disabled")
    dns.sync_spf_record(node.get_cluster())


def restore_node(node: Document) -> None:
    if not node.enabled:
        frappe.throw(_("Enable the node first."))
    node.db_set("provisioned_at", now(), update_modified=False)  # the health deadline starts afresh
    node.set_status("Provisioned")
    check_node(node)


def forget_node(node: Document) -> None:
    """Removes the node's registry lease when the cluster is reachable (best effort)."""

    cluster = node.get_cluster()
    if cluster.status != "Active" or not has_credentials(cluster):
        return
    try:
        client = cluster.get_client()
        if registry := client.cluster_nodes.find_by_hostname(node.hostname):
            client.cluster_nodes.delete(registry["id"])
    except StalwartError:
        log_exception(f"Could not remove {node.hostname} from the cluster registry", node)
