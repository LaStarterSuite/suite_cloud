"""Egress gateways: outbound-only Stalwart instances that deliver from an IP pool.

The cluster keeps signing and queueing mail; everything it sends leaves through the cluster's
default pool, and domains assigned to another pool through that one. Each hop hands messages to
the pool's gateways over authenticated SMTP (an MtaRoute of type Relay), and the gateway binds
the outbound connection to one of the pool's addresses. Stalwart has no forward-proxy support,
so a relay hop is the only way to change the source address.
"""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now

from suite_cloud.mail.cluster import dns, plan
from suite_cloud.mail.cluster.push import recorded_failure, resync_cluster
from suite_cloud.mail.stalwart import has_credentials
from suite_cloud.mail.stalwart.directory import dkim_management_payload
from suite_cloud.mail.stalwart.errors import StalwartError
from suite_cloud.utils import dkim_algorithms

if TYPE_CHECKING:
    from frappe.model.document import Document

GATEWAY_ROLE = "egress"
GATEWAY_DEADLINE_MINUTES = 45
GATEWAY_VARIABLES_BUILDER = "suite_cloud.mail.cluster.egress.build_gateway_variables"
SUITE_RULE_PREFIX = "'egress-"
# Mail between domains of the cluster never leaves it: without this rule first, a relay fallback
# would send it out through a gateway only to come back through the cluster's own MX.
LOCAL_RULE = {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}
LOCAL_ADDRESS_RULE = {"if": "is_local_address(rcpt)", "then": LOCAL_RULE["then"]}


# --- pool resolution ------------------------------------------------------------------------


def pool_for_domain(domain: Document, site_pool: str | None, cluster_pool: str | None) -> str | None:
    return domain.egress_pool or site_pool or cluster_pool


def populated_pools(cluster: Document) -> set[str]:
    """Pools of the cluster that hold at least one address; only those can relay."""

    return {
        name
        for name in frappe.get_all("Egress IP Pool", {"cluster": cluster.name}, pluck="name")
        if frappe.db.exists("Egress IP Pool Address", {"parent": name, "parenttype": "Egress IP Pool"})
    }


def default_pool(cluster: Document) -> str | None:
    """The cluster's default pool when it can relay, else None (mail goes direct)."""

    pool = cluster.default_egress_pool
    return pool if pool and pool in populated_pools(cluster) else None


def domains_by_pool(cluster: Document) -> dict[str, list[str]]:
    """Enabled domains grouped by the pool they leave through (pools without addresses are skipped)."""

    populated = populated_pools(cluster)
    tenant_pools = dict(
        frappe.get_all(
            "Suite Tenant", {"stalwart_cluster": cluster.name}, ["name", "egress_pool"], as_list=True
        )
    )
    grouped: dict[str, list[str]] = {}
    domains = frappe.get_all(
        "Mail Domain",
        {"cluster": cluster.name, "enabled": 1},
        ["domain_name", "tenant", "egress_pool"],
        order_by="domain_name",
    )
    for domain in domains:
        pool = pool_for_domain(domain, tenant_pools.get(domain.tenant), cluster.default_egress_pool)
        if pool in populated:
            grouped.setdefault(pool, []).append(domain.domain_name)
    return grouped


def relaying_domains(cluster: Document) -> list[str]:
    """Enabled domains that keep some of their mailboxes on another server (split delivery)."""

    return frappe.get_all(
        "Mail Domain",
        {"cluster": cluster.name, "enabled": 1, "allow_relaying": 1},
        pluck="domain_name",
        order_by="domain_name",
    )


# --- cluster side ------------------------------------------------------------------------------


def cluster_operations(cluster: Document) -> list[dict]:
    """Relay routes plus the routing rules that send mail through them.

    The default pool is the ``else`` of the route expression, so it carries every sender the
    cluster has (bounces and reports included); only domains on another pool need a rule.
    """

    grouped = domains_by_pool(cluster)
    default = default_pool(cluster)
    overrides = {pool: domains for pool, domains in grouped.items() if pool != default}
    pools = set(grouped) | ({default} if default else set())
    if not pools:
        return [
            {
                "@type": "update",
                "object": "MtaOutboundStrategy",
                "value": {"route": route_expression(cluster, {}, None)},
            }
        ]

    routes = {}
    for pool_name in sorted(pools):
        pool = frappe.get_cached_doc("Egress IP Pool", pool_name)
        routes[f"egress-{pool.pool_name}"] = {
            "@type": "Relay",
            "name": f"egress-{pool.pool_name}",
            "description": f"Egress pool {pool.pool_name}",
            "address": pool.hostname,
            "port": pool.relay_port,
            "protocol": "smtp",
            "authUsername": cluster.relay_username or "relay",
            "authSecret": plan.secret_value(cluster.get_password("relay_password")),
            "implicitTls": False,
            "allowInvalidCerts": False,
        }

    return [
        {"@type": "upsert", "object": "MtaRoute", "matchOn": ["name"], "value": routes},
        {
            "@type": "update",
            "object": "MtaOutboundStrategy",
            "value": {"route": route_expression(cluster, overrides, default)},
        },
    ]


def route_name(pool_name: str) -> str:
    return f"'egress-{frappe.get_cached_value('Egress IP Pool', pool_name, 'pool_name')}'"


def route_expression(cluster: Document, grouped: dict[str, list[str]], default: str | None) -> dict:
    """Local delivery first, then Suite Cloud's pool rules, with its default pool as the fallback;
    anything else the strategy holds (foreign rules and a foreign ``else``) is preserved.

    Every rule Suite Cloud writes routes to ``'local'`` or to an egress pool, which is how its
    former rules are told from foreign ones and dropped."""

    current = current_route_expression(cluster)
    kept = [
        rule
        for rule in expression_rules(current)
        if not str(rule.get("then", "")).startswith(SUITE_RULE_PREFIX)
        and rule.get("then") != LOCAL_RULE["then"]
    ]
    rules = local_rules(relaying_domains(cluster))
    for pool_name, domain_names in grouped.items():
        condition = " || ".join(f"sender_domain == '{d}'" for d in domain_names)
        rules.append({"if": condition, "then": route_name(pool_name)})
    fallback = current.get("else") or "'mx'"
    if default:
        fallback = route_name(default)
    elif fallback.startswith(SUITE_RULE_PREFIX):
        fallback = "'mx'"  # our former default pool is gone; back to direct delivery
    return {"match": plan.as_list(rules + kept), "else": fallback}


def local_rules(relaying: list[str]) -> list[dict]:
    """The rules that keep mail on the cluster, given the domains that relay.

    Stalwart accepts every address of a relaying domain, held here or not. One it does not hold
    must miss these rules, so that it leaves like any other outbound mail (pool rules, then the
    fallback) and reaches the domain's MX; as local mail it would bounce with no mailbox to take it.
    """

    if not relaying:
        return [LOCAL_RULE]
    held_elsewhere = " && ".join(f"rcpt_domain != '{d}'" for d in relaying)
    return [
        LOCAL_ADDRESS_RULE,
        {"if": f"{LOCAL_RULE['if']} && {held_elsewhere}", "then": LOCAL_RULE["then"]},
    ]


def expression_rules(expression: dict) -> list[dict]:
    """The match rules of an expression in order; Stalwart sends List<T> as an index-keyed object."""

    match = expression.get("match") or {}
    if isinstance(match, dict):
        return [match[key] for key in sorted(match, key=int)]
    return list(match)


def current_route_expression(cluster: Document) -> dict:
    if cluster.status != "Active" or not has_credentials(cluster):
        return {}
    try:
        return (
            cluster.get_client().singleton("MtaOutboundStrategy").read(properties=["route"]).get("route")
            or {}
        )
    except StalwartError:
        return {}


def apply_pool_changes(pool: Document) -> None:
    """A pool changed: in a job after commit, so a remote failure never fails the save. The
    gateways that newly host its relay port are worked out now, while the pool before the save is
    known."""

    before = pool.get_doc_before_save()
    port_is_new = before is None or before.relay_port != pool.relay_port
    hosted_before = set(before.gateway_names()) if before else set()
    new_port_on = [
        name
        for name in pool.gateway_names()
        if pool.relay_port and (port_is_new or name not in hosted_before)
    ]
    if frappe.flags.do_not_enqueue:
        apply_pool_changes_job(pool.name, new_port_on)
        return
    frappe.msgprint(_("The pool's configuration is pushed in the background."), alert=True)
    frappe.enqueue(
        apply_pool_changes_job,
        pool=pool.name,
        new_port_on=new_port_on,
        queue="long",
        enqueue_after_commit=True,
    )


def apply_pool_changes_job(pool: str, new_port_on: list[str]) -> None:
    """The cluster's routes first, then every hosting gateway's listeners: a pool the cluster stops
    routing to is closed on the gateways only after. A cluster that refuses the push stops the job
    before any gateway changes."""

    if not frappe.db.exists("Egress IP Pool", pool):
        return  # deleted meanwhile; its delete resyncs the cluster
    doc = frappe.get_doc("Egress IP Pool", pool)
    with recorded_failure(doc.cluster):
        resync_cluster(doc.get_cluster())
    for gateway_name in doc.gateway_names():
        gateway = frappe.get_doc("Egress Gateway", gateway_name)
        if gateway.status != "Active":
            continue  # provisioning opens every port of every pool it hosts
        gateway.push_config()
        if gateway_name in new_port_on:
            open_relay_port(gateway, doc.relay_port)


def open_relay_port(gateway: Document, port: int) -> None:
    """The listener is live once the config is pushed, but ufw on the gateway only learned the
    ports it had at provisioning; a job opens the new one so mail does not queue silently."""

    from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

    create_server_job(
        gateway,
        "run-commands.yml",
        title=f"Open relay port {port} on {gateway.name}",
        context={"commands": [f"ufw allow {int(port)}/tcp"]},
        variables_builder="suite_cloud.mail.cluster.bootstrap.build_command_variables",
    )


# --- gateway side --------------------------------------------------------------------------------


def gateway_plan(gateway: Document) -> list[dict]:
    """The gateway's whole configuration: relay listeners per pool, source IPs, its own certificate."""

    cluster = gateway.get_cluster()
    zone = dns.egress_zone(cluster)
    pools = gateway.pools()
    listeners, strategies, rules, sender_rules = {}, {}, [], []
    for pool in pools:
        addresses = pool.addresses_on(gateway.name)
        if not addresses:
            continue
        listener = f"relay-{pool.pool_name}"
        listeners[listener] = {
            "name": listener,
            "protocol": "smtp",
            "bind": {f"0.0.0.0:{pool.relay_port}": True},
            "useTls": True,
            "tlsImplicit": False,
        }
        strategies[pool.pool_name] = {
            "name": pool.pool_name,
            "description": f"Egress pool {pool.pool_name}",
            "ehloHostname": gateway.hostname,
            "sourceIps": plan.as_list(
                [{"sourceIp": a.ip_address, "ehloHostname": a.ehlo_hostname} for a in addresses]
            ),
        }
        # The connection strategy is chosen at delivery time, where the listener is no longer
        # known but the port the message arrived on is.
        rules.append({"if": f"received_via_port == {pool.relay_port}", "then": f"'{pool.pool_name}'"})
        # The cluster logs in as the relay user of the egress zone but sends as its customers'
        # addresses, so Stalwart's default "sender must match the login" check is lifted here.
        sender_rules.append({"if": f"local_port == {pool.relay_port}", "then": "false"})

    operations: list[dict] = [
        {"@type": "update", "object": "Coordinator", "value": {"@type": "Disabled"}},
    ]
    dns_server = plan.dns_server_object(cluster)
    if dns_server:
        operations.append(
            {
                "@type": "upsert",
                "object": "DnsServer",
                "matchOn": ["description"],
                "value": {"dns": dns_server},
            }
        )
    operations.append(
        {
            "@type": "upsert",
            "object": "AcmeProvider",
            "matchOn": ["directory"],
            "value": {"acme": plan.acme_provider(cluster)},
        }
    )
    # Each gateway signs its own mail (delivery status notifications) from a domain named after
    # itself. Gateways run separate Stalwarts with separate keys, so a domain shared between them
    # would publish clashing DKIM selectors. The certificate also covers the egress sub-zone, which
    # every pool hostname sits under, so the relay listeners present it for any pool.
    domain = {
        "name": gateway.hostname,
        "description": "Gateway domain",
        "isEnabled": True,
        "certificateManagement": {
            "@type": "Automatic",
            "acmeProviderId": "#acme",
            "subjectAlternativeNames": plan.as_set([f"*.{zone}"]),
        },
        "dkimManagement": dkim_management_payload(dkim_algorithms()),
        "dnsManagement": {
            "@type": "Automatic",
            "dnsServerId": "#dns",
            "origin": cluster.dns_zone,
            "publishRecords": plan.PUBLISHED_RECORD_TYPES,
        }
        if dns_server
        else {"@type": "Manual"},
        "subAddressing": {"@type": "Disabled"},
    }
    operations.append(
        {"@type": "upsert", "object": "Domain", "matchOn": ["name"], "value": {"gateway": domain}}
    )
    operations.append(
        {
            "@type": "update",
            "object": "SystemSettings",
            "value": {
                "defaultHostname": gateway.hostname,
                "defaultDomainId": "#gateway",
                # Published as the zone's MX: replies to notifications reach the cluster, whose
                # port 25 is open, rather than a gateway's, which is firewalled.
                "mailExchangers": plan.as_list([{"hostname": cluster.hostname, "priority": 10}]),
            },
        }
    )
    operations.append(
        {
            "@type": "upsert",
            "object": "Account",
            "matchOn": ["name", "domainId"],
            "value": {
                "relay": {
                    "@type": "User",
                    "name": cluster.relay_username or "relay",
                    "domainId": "#gateway",
                    "description": "Cluster relay login",
                    "credentials": {
                        "0": {"@type": "Password", "secret": cluster.get_password("relay_password")}
                    },
                    "roles": {"@type": "User"},
                    "permissions": {"@type": "Inherit"},
                    "quotas": {},
                    "aliases": {},
                    "memberGroupIds": {},
                    "locale": "en-US",
                    "encryptionAtRest": {"@type": "Disabled"},
                }
            },
        }
    )
    if listeners:
        operations.append(
            {"@type": "upsert", "object": "NetworkListener", "matchOn": ["name"], "value": listeners}
        )
        operations.append(
            {"@type": "upsert", "object": "MtaConnectionStrategy", "matchOn": ["name"], "value": strategies}
        )
    operations.extend(gateway_defaults_plan())
    operations.append(plan.tracer_operation())
    operations.extend(plan.telemetry_operations(gateway))
    operations.append(
        {
            "@type": "update",
            "object": "MtaStageAuth",
            "value": {"mustMatchSender": {"match": plan.as_list(sender_rules), "else": "true"}},
        }
    )
    operations.append(
        {
            "@type": "update",
            "object": "MtaOutboundStrategy",
            "value": {"connection": {"match": plan.as_list(rules), "else": "'default'"}},
        }
    )
    return operations


def gateway_recovery_plan(gateway: Document) -> list[dict]:
    return [*gateway_plan(gateway), plan.admin_account_operation(gateway)]


def gateway_defaults_plan() -> list[dict]:
    """The gateway's cluster role, resolver and spam rules pin, in place before its first normal start."""

    role = {
        "name": GATEWAY_ROLE,
        "description": "Outbound relay only",
        "tasks": {
            "@type": "EnableSome",
            "taskTypes": plan.as_set(["outboundMta", "taskQueueProcessing", "taskScheduler", "metricsPush"]),
        },
        # Every listener stays on so management HTTPS keeps working; the firewall only opens 443
        # and the relay ports.
        "listeners": {"@type": "EnableAll"},
    }
    # Plan labels are one namespace: "egress" already names the domain.
    return [
        {"@type": "upsert", "object": "ClusterRole", "matchOn": ["name"], "value": {"gateway-role": role}},
        plan.dns_resolver_operation(),
        *plan.spam_settings_operations(),
    ]


def gateway_bootstrap_plan(gateway: Document) -> list[dict]:
    value = {
        "serverHostname": gateway.hostname,
        "defaultDomain": gateway.hostname,
        "requestTlsCertificate": False,
        "generateDkimKeys": False,
        "dataStore": gateway.get_store("data_store").config,
        "blobStore": {"@type": "Default"},
        "searchStore": {"@type": "Default"},
        "inMemoryStore": {"@type": "Default"},
        "directory": {"@type": "Internal"},
        "tracer": plan.log_tracer(),
        "dnsServer": {"@type": "Manual"},
    }
    return [{"@type": "update", "object": "Bootstrap", "value": value}]


def gateway_env(gateway: Document, mode: str = "normal") -> dict[str, str]:
    env = {"STALWART_HOSTNAME": gateway.hostname, "STALWART_PUBLIC_URL": gateway.base_url}
    if mode == "normal":
        env["STALWART_ROLE"] = GATEWAY_ROLE
        env[plan.TELEMETRY_TOKEN_VARIABLE] = plan.get_telemetry_token(gateway)
        return env
    env["STALWART_RECOVERY_ADMIN"] = f"{gateway.admin_username}:{gateway.get_password('admin_password')}"
    env["STALWART_RECOVERY_MODE_PORT"] = str(plan.BOOTSTRAP_PORT)
    if mode == "recovery":
        env["STALWART_RECOVERY_MODE"] = "1"
    return env


# --- provisioning ----------------------------------------------------------------------------------


def provision_gateway(gateway: Document) -> Document:
    from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

    gateway.bump_config_version(gateway_plan(gateway))
    gateway.set_status("Provisioning")
    return create_server_job(
        gateway,
        "bootstrap-cluster.yml",
        title=f"Provision gateway {gateway.hostname}",
        context={"gateway": gateway.name},
        variables_builder=GATEWAY_VARIABLES_BUILDER,
        callback="after_provision",
    )


def upgrade_gateway(gateway: Document) -> Document:
    from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

    return create_server_job(
        gateway,
        "upgrade-stalwart.yml",
        title=f"Upgrade gateway {gateway.hostname}",
        context={"gateway": gateway.name},
        variables_builder=GATEWAY_VARIABLES_BUILDER,
        callback="after_upgrade",
    )


def build_gateway_variables(context: dict) -> dict:
    from suite_cloud.mail.cluster.bootstrap import SECRET_VARIABLES
    from suite_cloud.telemetry.install import telemetry_variables
    from suite_cloud.utils import get_config

    gateway = frappe.get_doc("Egress Gateway", context["gateway"])
    relay_ports = sorted({p.relay_port for p in gateway.pools()})
    variables = {
        "node_hostname": gateway.hostname,
        "cluster_hostname": gateway.get_cluster().hostname,
        "ssh_port": gateway.ssh_port or gateway.get_cluster().ssh_port,
        "stalwart_version": gateway.stalwart_version or get_config("stalwart_version"),
        "stalwart_url_template": get_config("stalwart_download_url_template"),
        "stalwart_cli_version": get_config("stalwart_cli_version"),
        "stalwart_cli_url_template": get_config("stalwart_cli_download_url_template"),
        "systemd_unit": plan.systemd_unit(),
        "firewall_ports": [443],
        "relay_ports": relay_ports,
        "relay_sources": node_addresses(gateway.get_cluster()),
        "wait_ports": [443, *relay_ports],
        "recovery_port": plan.BOOTSTRAP_PORT,
        "admin_user": gateway.admin_username,
        "admin_password": gateway.get_password("admin_password"),
        "config_version": gateway.config_version or 0,
        "plan_marker": plan.marker(recovery_plan := gateway_recovery_plan(gateway)),
        "env_normal": plan.render_env(gateway_env(gateway, "normal")),
        "env_bootstrap": plan.render_env(gateway_env(gateway, "bootstrap")),
        "env_recovery": plan.render_env(gateway_env(gateway, "recovery")),
        "config_json": frappe.as_json(gateway.get_store("data_store").config),
        "bootstrap_ndjson": plan.to_ndjson(bootstrap_plan := gateway_bootstrap_plan(gateway)),
        "defaults_ndjson": plan.to_ndjson(gateway_defaults_plan()),
        "cluster_ndjson": plan.to_ndjson(recovery_plan),
        "__secret_keys__": list(SECRET_VARIABLES),
        "__secret_values__": [
            gateway.get_password("admin_password"),
            plan.get_telemetry_token(gateway),
            *plan.secret_strings(bootstrap_plan),
            *plan.secret_strings(recovery_plan),
        ],
    }
    variables.update(telemetry_variables(context))
    return variables


def after_gateway_provision(gateway: Document, job: Document) -> None:
    from suite_cloud.telemetry.install import mark_token_installed

    mark_token_installed(gateway)  # while the gateway is not running yet, so the push follows no change
    gateway.db_set(
        {"status": "Provisioned", "provisioned_at": now(), "installed_version": gateway.stalwart_version},
        update_modified=False,
    )
    check_gateway(gateway)


def check_gateway(gateway: Document) -> bool:
    """Activates a provisioned gateway once its certificate is live; then wires the cluster to it."""

    from suite_cloud.mail.cluster.bootstrap import ensure_api_key

    if gateway.status not in ("Provisioned", "Active"):
        return False
    try:
        ensure_api_key(gateway, gateway.get_admin_client())
    except StalwartError as e:
        started = get_datetime(gateway.provisioned_at or now())
        expired = get_datetime(now()) > add_to_date(started, minutes=GATEWAY_DEADLINE_MINUTES)
        if expired and gateway.status == "Provisioned":
            gateway.set_status("Failed", f"Not reachable after {GATEWAY_DEADLINE_MINUTES} minutes: {e}")
        else:
            gateway.db_set("last_error", str(e)[:1000], update_modified=False)
        return False

    if gateway.status != "Active":
        gateway.db_set(
            {"status": "Active", "last_error": None, "last_config_sync_at": now()}, update_modified=False
        )
        resync_cluster(gateway.get_cluster())
    return True


def node_addresses(cluster: Document) -> list[str]:
    """Every address a node of the cluster may send from: what a gateway's firewall admits."""

    nodes = frappe.get_all(
        "Stalwart Node",
        {"cluster": cluster.name, "status": ["!=", "Disabled"]},
        ["ipv4_address", "ipv6_address"],
    )
    return sorted({ip for node in nodes for ip in (node.ipv4_address, node.ipv6_address) if ip})
