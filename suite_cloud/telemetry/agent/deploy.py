"""What provisioning puts on a server for telemetry: the agent's config.json and unit, the
telemetry token, and the Server Jobs that install them and switch Stalwart's push on."""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cint, get_url

from suite_cloud.mail.cluster import push, quota, telemetry
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job
from suite_cloud.telemetry.agent.build import (
    ARCHITECTURES,
    AgentBuild,
    is_quota_enabled,
    is_telemetry_enabled,
)
from suite_cloud.utils import get_config, log_error, log_exception, mask_secrets, secret_spellings

if TYPE_CHECKING:
    from frappe.model.document import Document

SOURCE = "stalwart"
CONFIG_PATH = "/etc/suite-cloud-agent/config.json"
STATE_DIR = "/var/lib/suite-cloud-agent"
PLAYBOOK = "install-telemetry.yml"
ROLL_OUT_KEY = "suite_cloud:agent_roll_out"  # the settings change the last roll-out applied
ROLL_OUT_TIMEOUT = 600  # seconds a roll-out may hold, or wait for, the roll-out lock
NODE_BUILDER = "suite_cloud.mail.cluster.bootstrap.build_node_variables"
GATEWAY_BUILDER = "suite_cloud.mail.cluster.egress.build_gateway_variables"


def agent_config(server: Document) -> dict:
    """``config.json`` for a Stalwart Node. The Go side reads exactly this shape
    (``agent/internal/config``), so the two change together. Stalwart pushes its own events and
    metrics, so the agent only enforces the sending limits."""

    config = {
        "suite_cloud": {
            "url": get_config("public_url") or get_url(),
            "secret": server.get_cluster().agent_token,
            "requests_per_minute": cint(get_config("agent_requests_per_minute")),
        },
        "node": {"host_ip": server.ipv4_address, "hostname": server.hostname},
        "state_dir": STATE_DIR,
    }
    if is_quota_hook_server(server):
        config["quota"] = quota_config(server.get_cluster())
    return config


def is_quota_hook_server(server: Document) -> bool:
    """Stalwart calls the hook on the node that took the message: a node with listeners. Gateways
    and outbound-only nodes only relay."""

    is_client_node = server.doctype == "Stalwart Node" and (server.role or "full") != "outbound"
    return is_client_node and is_quota_enabled()


def quota_config(cluster: Document) -> dict:
    """The sending-limit hook the Agent answers on loopback, and the store all of the cluster's
    Agents share."""

    return {
        "hook_port": quota.HOOK_PORT,
        "hook_token": cluster.get_password("quota_hook_token"),
        "store": quota.store_config(cluster),
    }


def systemd_unit() -> str:
    """Runs as the stalwart user, sandboxed away from Stalwart's own config and data."""

    return f"""[Unit]
Description=Suite Cloud Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stalwart
Group=stalwart
ExecStartPre=/usr/local/bin/suite-cloud-agent --config {CONFIG_PATH} --check
ExecStart=/usr/local/bin/suite-cloud-agent --config {CONFIG_PATH}
Restart=always
RestartSec=5
TimeoutStopSec=90
StateDirectory=suite-cloud-agent
StateDirectoryMode=0750
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
InaccessiblePaths=/etc/stalwart /var/lib/stalwart
MemoryMax=256M

[Install]
WantedBy=multi-user.target
"""


def agent_variables(server: Document, context: dict) -> dict:
    """What install-telemetry.yml and install-agent.yml need, merged into the node and gateway
    variables so the two never differ.

    The context's ``restart_stalwart`` restarts Stalwart even when its environment did not change,
    which starts OTLP, and ``agent_restart`` the agent. With telemetry off, without a build
    and on a gateway, which has no sending limits, the play stops the agent instead.
    """

    switches = {
        "agent_restart": bool(context.get("agent_restart")),
        "restart_stalwart": bool(context.get("restart_stalwart")),
    }
    disabled = {
        **switches,
        "agent_enabled": False,
        "agent_version": "",
        "agent_binaries": {},
        "agent_config_json": "",
        "agent_systemd_unit": "",
        "__secret_values__": [],
    }
    if not is_telemetry_enabled() or server.doctype == "Egress Gateway":
        return disabled
    build = AgentBuild.last_built()
    if not build or not build.is_built:
        log_error(
            "Agent not built",
            f"{server.hostname}: telemetry is on but no agent build exists; run Build Agent.",
        )
        return disabled

    config = agent_config(server)
    return {
        **switches,
        "agent_enabled": True,
        "agent_version": build.version,
        "agent_binaries": {arch: str(build.binary_path(arch)) for arch in ARCHITECTURES},
        "agent_config_json": frappe.as_json(config),
        "agent_systemd_unit": systemd_unit(),
        "__secret_values__": agent_secrets(config),
    }


def agent_secrets(config: dict) -> list[str]:
    """Every secret in an agent's config.json, so job output and previews can mask them."""

    secrets = [config["suite_cloud"]["secret"]]
    if "quota" in config:
        secrets += [config["quota"]["hook_token"], *quota.store_secrets(config["quota"]["store"])]
    return [secret for secret in secrets if secret]


def masked_agent_config(server: Document) -> dict:
    """config.json as the next Install Agent writes it to the server, every secret masked."""

    config = agent_config(server)
    return mask_config(config, secret_spellings(agent_secrets(config)))


def mask_config(value, spellings: list[str]):
    """Masks every secret wherever it appears, a Redis URL holding a password included."""

    if isinstance(value, dict):
        return {key: mask_config(item, spellings) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_config(item, spellings) for item in value]
    if isinstance(value, str):
        return mask_secrets(value, spellings)
    return value


def install_agent(server: Document) -> Document:
    """A Server Job that installs the server's telemetry token and its agent. Stalwart restarts
    only when its environment changed."""

    return agent_job(server, require_build())


def require_build() -> AgentBuild:
    if not is_telemetry_enabled():
        frappe.throw(_("Turn on Enable Telemetry in Suite Cloud Settings first."))
    build = AgentBuild.last_built()
    if not build or not build.is_built:
        frappe.throw(_("No agent build on this bench host; run Build Agent in Suite Cloud Settings first."))
    return build


def agent_job(
    server: Document,
    build: AgentBuild | None,
    restart_stalwart: bool = False,
) -> Document:
    """Without a build the job runs the play with the secret gone, which stops the agent."""

    is_gateway = server.doctype == "Egress Gateway"
    if restart_stalwart:
        title = f"Start telemetry push on {server.hostname}"
    elif build and not is_gateway:
        title = f"Install agent {build.version} on {server.hostname}"
    else:
        title = f"Stop agent on {server.hostname}"
    context = {
        "gateway" if is_gateway else "node": server.name,
        "agent_restart": True,
        "restart_stalwart": restart_stalwart,
    }
    return create_server_job(
        server,
        PLAYBOOK,
        title=title,
        context=context,
        variables_builder=GATEWAY_BUILDER if is_gateway else NODE_BUILDER,
        callback="after_install_agent",
    )


def upgrade_agents(cluster: Document) -> list[str]:
    """Rolls the built agent and the telemetry tokens out to every server of the cluster: its
    nodes, then each of its gateways."""

    build = require_build()
    owners = [cluster, *(frappe.get_doc("Egress Gateway", name) for name in active_gateways(cluster))]
    return [name for owner in owners for name in roll_out(owner, build)]


def roll_out(owner: Document, build: AgentBuild | None) -> list[str]:
    """Where every server of a cluster or gateway has its token, the push is switched on.
    Elsewhere each server gets its token and the agent that only enforces sending limits."""

    if owner.status == "Active" and telemetry.is_push_enabled(owner):
        return start_push(owner, build)
    return [agent_job(server, build).name for server in owner_servers(owner)]


def start_push(owner: Document, build: AgentBuild | None) -> list[str]:
    """Pushes the plan, which now holds the WebHooks and OTLP, then restarts Stalwart on each
    server to start OTLP, with the agent that no longer ships logs."""

    if owner.status != "Active":
        return []  # its first push and start include the push already
    owner.push_config()
    return [agent_job(server, build, restart_stalwart=True).name for server in owner_servers(owner)]


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
    """A failure shows on the cluster's form; Upgrade Agents tries again."""

    owner = frappe.get_doc(doctype, name)
    build = AgentBuild.last_built() if is_telemetry_enabled() else None
    if doctype == "Stalwart Cluster":
        with push.recorded_failure(name):
            start_push(owner, build)
    else:
        start_push(owner, build)


def rotate_telemetry_token(server: Document) -> str | None:
    """A new secret, written to the server's stalwart.env by a job that restarts Stalwart. Until
    then Stalwart sends the old one, is refused and retries."""

    server.reload()  # the form sends its unsaved edits too; only the secret is saved
    server.telemetry_secret = frappe.generate_hash(length=40)
    server.save(ignore_permissions=True)
    if server.status not in ("Active", "Draining"):
        return None  # provisioning writes the new token
    build = AgentBuild.last_built() if is_telemetry_enabled() else None
    return agent_job(server, build).name


def filters_for(cluster: Document | None, statuses: tuple[str, ...]) -> dict:
    filters = {"status": ("in", statuses)}
    if cluster:
        filters["cluster"] = cluster.name
    return filters


def switch_off_push() -> None:
    """With telemetry off each active cluster's and gateway's config is pushed again: its plan
    then holds the WebHooks and OTLP and the quota hook switched off."""

    for name in frappe.get_all("Stalwart Cluster", {"status": "Active"}, pluck="name"):
        push.resync_cluster_after_commit(name)
    for name in active_gateways():
        try:
            frappe.get_doc("Egress Gateway", name).push_config()
        except Exception:
            log_exception(f"Telemetry not switched off on {name}")


def enqueue_roll_out_agents() -> None:
    """Enable Telemetry changed. In the background, since the roll-out loads every server of every
    cluster and creates a job for each. Queued even where jobs otherwise run at once
    (``do_not_enqueue``): it reaches every server of the site, so a test runs it only by name."""

    frappe.enqueue(roll_out_agents, queue="long", enqueue_after_commit=True)


def roll_out_agents() -> list[str]:
    """Each turn of the setting queues a roll-out, and each reads the setting when it runs. They run
    one at a time, and one that finds the settings as the last roll-out left them does nothing, so
    quick turns end in the last one without a second job per server."""

    lock = f"{frappe.local.site}:suite_cloud:roll-out-agents"
    with frappe.cache.lock(lock, timeout=ROLL_OUT_TIMEOUT, blocking_timeout=ROLL_OUT_TIMEOUT):
        change = str(frappe.db.get_single_value("Suite Cloud Settings", "modified"))
        if frappe.cache.get_value(ROLL_OUT_KEY) == change:
            return []
        names = start_agent_jobs()
        if not frappe.in_test:
            frappe.db.commit()  # the jobs exist before the change is marked as rolled out
        frappe.cache.set_value(ROLL_OUT_KEY, change)
    return names


def start_agent_jobs() -> list[str]:
    """Every cluster and gateway gets the push and its agents, or stops both while telemetry is
    off. A missing build or a failed push is an Error Log, not an exception: the setting is
    already saved."""

    if not is_telemetry_enabled():
        switch_off_push()
        return [agent_job(server, None).name for server in telemetry.get_reporting_servers()]
    build = AgentBuild.last_built()
    if not build or not build.is_built:
        if telemetry.get_reporting_servers():
            log_error(
                "Agent not built",
                "Telemetry was turned on but no agent build exists; run Build Agent, then Upgrade Agents on every cluster.",
            )
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
            names += roll_out(owner, build)
        except Exception:
            log_exception(f"Telemetry not rolled out to {owner.name}")
    return names
