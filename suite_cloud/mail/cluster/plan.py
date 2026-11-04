"""Generates the Stalwart configuration Suite Cloud owns for a cluster.

Two plans exist. The bootstrap plan is applied once on the first node in bootstrap mode and
only names the stores and hostnames (Stalwart provisions everything else with defaults).
The cluster plan holds the objects Suite Cloud manages afterwards (roles, coordinator, ACME,
DNS provider, DNS resolver, default domain, system settings, spam rules source, licences) and is
re-applied on every sync. Its cluster roles, DNS resolver and SpamSettings are also the defaults
plan, applied before the first normal start.
"""

import hashlib
import json
from typing import TYPE_CHECKING

import frappe

from suite_cloud.mail.cluster import server_token
from suite_cloud.mail.stalwart.client import is_write_only
from suite_cloud.mail.stalwart.directory import dkim_management_payload
from suite_cloud.utils import dkim_algorithms, get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

BOOTSTRAP_PORT = 8080
API_KEY_DESCRIPTION = "suite-cloud"
DISABLED_ROLE_DESCRIPTION = "suite-disabled"
FIREWALL_PORTS = (25, 465, 587, 143, 993, 110, 995, 443, 4190)
SECRET_MARKER = "***"
SPAM_RULES_URL = (
    "https://github.com/stalwartlabs/spam-filter/releases/download/{version}/spam-filter-rules.json.gz"
)


def as_set(values) -> dict:
    """Stalwart encodes Set<T> as ``{value: true}``; an empty set is ``{}``."""

    return {value: True for value in values}


def as_list(items) -> dict:
    """Stalwart encodes List<T> as an index-keyed object, ``{"0": item, "1": item}``."""

    return {str(index): item for index, item in enumerate(items)}


# What Stalwart publishes for the cluster zone itself: its MX, the DKIM keys it rotates and a
# DMARC policy. The zone's SPF is a DNS Record of the cluster (it references the include target).
PUBLISHED_RECORD_TYPES = {"mx": True, "dkim": True, "dmarc": True}

CLUSTER_ROLES = {
    "full": {
        "name": "full",
        "description": "All tasks and listeners",
        "tasks": {"@type": "EnableAll"},
        "listeners": {"@type": "EnableAll"},
    },
    "frontend": {
        "name": "frontend",
        "description": "Serves clients, never delivers the outbound queue",
        "tasks": {"@type": "DisableSome", "taskTypes": as_set(["outboundMta"])},
        "listeners": {"@type": "EnableAll"},
    },
    "outbound": {
        "name": "outbound",
        "description": "Delivers the outbound queue only",
        "tasks": {
            "@type": "EnableSome",
            "taskTypes": as_set(["outboundMta", "taskQueueProcessing", "metricsPush"]),
        },
        "listeners": {"@type": "DisableAll"},
    },
}


# --- telemetry ------------------------------------------------------------------------

LOG_DIRECTORY = "/var/log/stalwart"  # created by install-stalwart.yml, pruned by its tmpfiles rule


def log_tracer(level: str = "info") -> dict:
    """A plain-text log rotated daily: stalwart.<date> under LOG_DIRECTORY."""

    return {
        "@type": "Log",
        "path": LOG_DIRECTORY,
        "prefix": "stalwart",
        "rotate": "daily",
        "ansi": False,
        "multiline": False,
        "enable": True,
        "level": level,
        "lossy": False,
        "events": {},
        "eventsPolicy": "exclude",
    }


def tracer_operation() -> dict:
    """Full detail goes to the log file; the journal keeps warnings so journalctl still shows trouble.

    Tracers carry no name, so they are matched by kind: the Journal tracer bootstrap created is
    turned down rather than duplicated, and the Log tracer is added once.
    """

    journal = {
        "@type": "Journal",
        "enable": True,
        "level": "warn",
        "lossy": False,
        "events": {},
        "eventsPolicy": "exclude",
    }
    return {
        "@type": "upsert",
        "object": "Tracer",
        "matchOn": ["@type"],
        "value": {"log": log_tracer(), "journal": journal},
    }


# --- DNS resolution -------------------------------------------------------------------

LOCAL_RESOLVER = "127.0.0.1"  # Unbound, installed and checked by install-stalwart.yml


def dns_resolver_operation() -> dict:
    """Every lookup goes to the node's own validating resolver.

    DNSSEC answers let Stalwart enforce DANE, and blocklists (Spamhaus, URIBL) see the node's
    address rather than a shared public resolver they refuse. No public fallback is listed:
    Stalwart queries all its servers by measured speed, so a fallback would answer too.
    Stalwart only trusts DNSSEC over a non-UDP connection, hence TCP as well.
    """

    servers = [{"address": LOCAL_RESOLVER, "port": 53, "protocol": p} for p in ("udp", "tcp")]
    return {
        "@type": "update",
        "object": "DnsResolver",
        "value": {"@type": "Custom", "servers": as_list(servers)},
    }


# --- bootstrap ------------------------------------------------------------------------


def bootstrap_plan(cluster: Document) -> list[dict]:
    """The single Bootstrap update applied while the first node runs in bootstrap mode."""

    store = cluster.get_store
    value = {
        "serverHostname": cluster.hostname,
        "defaultDomain": cluster.default_domain,
        "requestTlsCertificate": False,
        "generateDkimKeys": False,
        "dataStore": store("data_store").config,
        "blobStore": store("blob_store").config if cluster.blob_store else {"@type": "Default"},
        "searchStore": store("search_store").config if cluster.search_store else {"@type": "Default"},
        "inMemoryStore": store("in_memory_store").config if cluster.in_memory_store else {"@type": "Default"},
        "directory": {"@type": "Internal"},
        "tracer": log_tracer(),
        "dnsServer": {"@type": "Manual"},
    }
    return [{"@type": "update", "object": "Bootstrap", "value": value}]


# --- cluster --------------------------------------------------------------------------


def cluster_plan(cluster: Document) -> list[dict]:
    plan: list[dict] = [
        {"@type": "update", "object": "Coordinator", "value": {"@type": cluster.coordinator or "Disabled"}},
        *defaults_plan(),
        tracer_operation(),
        *telemetry_operations(cluster),
    ]

    dns_server = dns_server_object(cluster)
    if dns_server:
        plan.append(
            {
                "@type": "upsert",
                "object": "DnsServer",
                "matchOn": ["description"],
                "value": {"dns": dns_server},
            }
        )

    plan.append(
        {
            "@type": "upsert",
            "object": "AcmeProvider",
            "matchOn": ["directory"],
            "value": {"acme": acme_provider(cluster)},
        }
    )
    plan.append(
        {
            "@type": "upsert",
            "object": "Domain",
            "matchOn": ["name"],
            "value": {"default": default_domain(cluster, with_dns=bool(dns_server))},
        }
    )
    plan.append(
        {
            "@type": "update",
            "object": "SystemSettings",
            "value": {
                "defaultHostname": cluster.hostname,
                "defaultDomainId": "#default",
                "mailExchangers": as_list([{"hostname": cluster.hostname, "priority": 10}]),
            },
        }
    )
    plan.append(
        {
            "@type": "upsert",
            "object": "Role",
            "matchOn": ["description"],
            "value": {
                "disabled": {
                    "description": DISABLED_ROLE_DESCRIPTION,
                    "roleIds": {},
                    "enabledPermissions": {"emailReceive": True},
                    "disabledPermissions": {},
                }
            },
        }
    )

    plan.extend(egress_operations(cluster))
    plan.extend(quota_operations(cluster))
    return plan


def acme_provider(cluster: Document) -> dict:
    contact = cluster.acme_contact_email or get_config("acme_contact_email")
    provider = {
        "directory": cluster.acme_directory_url or get_config("acme_directory_url"),
        "challengeType": "Dns01",
    }
    if contact:
        provider["contact"] = {contact: True}
    return provider


def default_domain(cluster: Document, with_dns: bool) -> dict:
    """The cluster zone: carries the wildcard certificate and signs the cluster's own mail.

    Reports, alarms and notifications leave from this domain, so it gets DKIM keys like any
    customer domain, chosen by the same setting.
    """

    domain = {
        "name": cluster.default_domain,
        "description": "Cluster default domain",
        "isEnabled": True,
        "certificateManagement": {
            "@type": "Automatic",
            "acmeProviderId": "#acme",
            # The default domain is the ingress hostname, which its own wildcard does not cover.
            # Nodes stay out: Let's Encrypt rejects a name the wildcard already covers.
            "subjectAlternativeNames": as_set([cluster.default_domain, f"*.{cluster.default_domain}"]),
        },
        "dkimManagement": dkim_management_payload(dkim_algorithms()),
        "subAddressing": {"@type": "Disabled"},
    }
    if with_dns:
        # Automatic DNS management is what gives DNS-01 its provider; it also publishes the
        # zone's MX, DKIM and DMARC records. Node, ingress and SPF records stay with Suite Cloud.
        domain["dnsManagement"] = {
            "@type": "Automatic",
            "dnsServerId": "#dns",
            "origin": cluster.dns_zone,
            "publishRecords": PUBLISHED_RECORD_TYPES,
        }
    else:
        domain["dnsManagement"] = {"@type": "Manual"}
    return domain


def dns_server_object(cluster: Document) -> dict | None:
    """The cluster zone's provider as a Stalwart DnsServer, or None when records are published by hand."""

    return frappe.get_cached_doc("DNS Zone", cluster.dns_zone).stalwart_dns_server()


def admin_account_operation(server: Document) -> dict:
    """Pins the permanent administrator's password.

    Bootstrap creates the account with a password nobody is told (the recovery credential is a
    virtual login that bypasses the directory), so the recovery-stage plan sets it. Only that
    plan may carry it: credentials are a whole list, and once the management API key exists it
    lives in the same list and would be wiped by a later push. The account always exists, so
    the operation only ever updates it and names nothing else: sending a domain id would try to
    move the account when bootstrap's default domain differs from the plan's.
    """

    username = server.admin_username or "admin"
    return {
        "@type": "upsert",
        "object": "Account",
        "matchOn": ["name"],
        "value": {
            "admin": {
                "@type": "User",
                "name": username,
                "credentials": {"0": {"@type": "Password", "secret": server.get_password("admin_password")}},
            }
        },
    }


# Stalwart inserts its defaults of these only on a normal start that finds none, so the recovery
# plan must not create any; they are pushed once the cluster is active.
DEFAULTS_PROVISIONED_BY_STALWART = ("Role", "MtaInboundThrottle")


def recovery_plan(cluster: Document) -> list[dict]:
    """The cluster plan as the first node applies it in recovery mode.

    Stalwart provisions its built-in roles and inbound throttles only on a normal start that
    finds none, so the plan must not create any: the disabled-accounts role and the quota
    backstop are pushed once the cluster is active. The admin password is pinned here and only
    here (see the helper).
    """

    operations = [op for op in cluster_plan(cluster) if op["object"] not in DEFAULTS_PROVISIONED_BY_STALWART]
    operations.append(admin_account_operation(cluster))
    return operations


def defaults_plan() -> list[dict]:
    """Applied in recovery mode before the first normal start, which needs all of it.

    That start names the node's cluster role (Stalwart fails the start when it is missing),
    checks its resolver for DNSSEC (and turns DANE off when it cannot validate) and imports the
    spam rules. None is an object Stalwart counts before inserting its own defaults, so creating
    them early suppresses nothing.
    """

    return [
        {"@type": "upsert", "object": "ClusterRole", "matchOn": ["name"], "value": CLUSTER_ROLES},
        dns_resolver_operation(),
        *spam_settings_operations(),
    ]


def spam_settings_operations() -> list[dict]:
    """Pins the spam rules release; empty leaves Stalwart's default, the latest release.

    The latest release may call expression functions the pinned server lacks: those objects are
    stored, then skipped at every start. The import runs once and never updates a stored rule,
    so the pin must be in place before it.
    """

    version = get_config("spam_filter_rules_version")
    if not version:
        return []
    return [
        {
            "@type": "update",
            "object": "SpamSettings",
            "value": {"spamFilterRulesUrl": SPAM_RULES_URL.format(version=version)},
        }
    ]


def egress_operations(cluster: Document) -> list[dict]:
    """Relay routes and routing rules for egress pools; filled in by suite_cloud.mail.cluster.egress."""

    try:
        from suite_cloud.mail.cluster.egress import cluster_operations
    except ImportError:
        return []
    return cluster_operations(cluster)


def telemetry_operations(target: Document) -> list[dict]:
    """The WebHooks and metrics push of a cluster or gateway; filled in by
    suite_cloud.mail.cluster.telemetry."""

    from suite_cloud.mail.cluster.telemetry import get_operations  # it builds on this module

    return get_operations(target)


def quota_operations(cluster: Document) -> list[dict]:
    """The sending-limit hook and its backstop; filled in by suite_cloud.mail.cluster.quota."""

    from suite_cloud.mail.cluster.quota import cluster_operations  # it builds on this module

    return cluster_operations(cluster)


def api_key_permissions() -> dict:
    """Permissions of Suite Cloud's own management key.

    Inherit (the admin account's full set) for now; a Replace list scoped to the object types
    Suite Cloud manages is the follow-up once the identifiers are confirmed on a live server.
    """

    return {"@type": "Inherit"}


# --- per node -----------------------------------------------------------------------------


def node_config(cluster: Document) -> dict:
    """``/etc/stalwart/config.json``: only the data store; everything else lives in it."""

    return cluster.get_store("data_store").config


def node_env(node: Document, mode: str = "normal") -> dict[str, str]:
    """``stalwart.env`` for a node in ``normal``, ``bootstrap`` or ``recovery`` mode."""

    cluster = node.get_cluster()
    env = {
        "STALWART_HOSTNAME": node.hostname,
        "STALWART_PUBLIC_URL": cluster.base_url,
    }
    if mode == "normal":
        env["STALWART_ROLE"] = node.role or "full"
        env[server_token.TOKEN_VARIABLE] = server_token.get_token(node)
        return env

    env["STALWART_RECOVERY_ADMIN"] = f"{cluster.admin_username}:{cluster.get_password('admin_password')}"
    env["STALWART_RECOVERY_MODE_PORT"] = str(BOOTSTRAP_PORT)
    if mode == "recovery":
        env["STALWART_RECOVERY_MODE"] = "1"
    return env


def systemd_unit() -> str:
    return """[Unit]
Description=Stalwart Mail and Collaboration Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stalwart
Group=stalwart
EnvironmentFile=/etc/stalwart/stalwart.env
ExecStart=/usr/local/bin/stalwart --config /etc/stalwart/config.json
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
LimitNOFILE=65536
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


# --- rendering ------------------------------------------------------------------------------


def render_env(env: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in env.items())


def to_ndjson(plan: list[dict]) -> str:
    assert_unique_labels(plan)
    return "".join(json.dumps(operation, separators=(",", ":")) + "\n" for operation in plan)


def assert_unique_labels(plan: list[dict]) -> None:
    """``#label`` references share one namespace across the whole plan, whatever the object type.

    A later operation reusing a label silently rebinds every reference to it (the CLI resolved
    ``#egress`` to a ClusterRole once), so duplicates are a programming error.
    """

    seen: dict[str, str] = {}
    for operation in plan:
        for label in (operation.get("value") or {}) if operation["@type"] in ("upsert", "create") else ():
            if label in seen:
                raise ValueError(f"Plan label #{label} used for {seen[label]} and {operation['object']}")
            seen[label] = operation["object"]


def secret_value(secret: str | None) -> dict | None:
    return {"@type": "Value", "secret": secret} if secret else None


def secret_strings(plan: list[dict]) -> list[str]:
    """Every literal secret in a plan, so job output that echoes the plan can be masked."""

    found: list[str] = []
    _collect_secrets(plan, found)
    return list(dict.fromkeys(found))


def _collect_secrets(value, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SECRET_KEYS and isinstance(item, str) and item:
                found.append(item)
            else:
                _collect_secrets(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_secrets(item, found)


def marker(plan: list[dict]) -> str:
    """Name of the file a node keeps once it applied this exact plan.

    The digest covers the redacted plan and a hash of its secrets, so a rotated password changes
    the marker and gets applied, while the secrets themselves never appear on the node's disk.
    """

    secrets = hashlib.sha256("\0".join(secret_strings(plan)).encode()).hexdigest()
    digest = hashlib.sha1(f"{redacted(plan)}\n{secrets}".encode()).hexdigest()[:12]
    return f".suite-cloud-plan-{digest}"


def redacted(plan: list[dict]) -> str:
    """The plan for display: every secret-carrying value replaced by a marker."""

    return json.dumps([_redact(op) for op in plan], indent=2)


SECRET_KEYS = {
    "secret",
    "secretKey",
    "secretAccessKey",
    "apiKey",
    "authSecret",
    "sentinelSecret",
    "bearerToken",
}


def _redact(value):
    if isinstance(value, dict):
        if value.get("@type") == "Value" and "secret" in value:
            return {"@type": "Value", "secret": SECRET_MARKER}
        return {
            k: (SECRET_MARKER if k in SECRET_KEYS and isinstance(v, str) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


# --- drift ------------------------------------------------------------------------------------


def drift_report(cluster: Document) -> dict:
    """Compares the generated plan with what the cluster currently holds (never mutates)."""

    client = cluster.get_client()
    differences: list[dict] = []
    for operation in cluster_plan(cluster):
        object_type = operation["object"]
        if operation["@type"] == "upsert":
            match_on = operation.get("matchOn") or ["name"]
            existing = client.objects(object_type).get_all()
            for ref, value in operation["value"].items():
                live = next((o for o in existing if all(o.get(k) == value.get(k) for k in match_on)), None)
                if live is None:
                    differences.append({"object": object_type, "ref": ref, "missing": True})
                    continue
                differences += _drifted(object_type, live, value, ref)
        elif operation["@type"] == "update" and not operation.get("id"):
            live = client.singleton(object_type).read()
            differences += _drifted(object_type, live, operation["value"])

    return {"checked_at": frappe.utils.now(), "differences": differences}


def _drifted(object_type: str, live: dict, wanted: dict, ref: str | None = None) -> list[dict]:
    found = []
    for key, value in wanted.items():
        if key == "@type" or is_write_only(key, value):
            continue  # secrets are never echoed back
        if not same_value(live.get(key), value):
            entry = {"object": object_type, "property": key}
            if ref:
                entry["ref"] = ref
            found.append(entry)
    return found


def same_value(live, wanted) -> bool:
    """Deep equality where a ``#ref`` in the plan stands for whatever id the server assigned."""

    if isinstance(wanted, str) and wanted.startswith("#"):
        return isinstance(live, str) and bool(live)
    if isinstance(wanted, dict):
        if not isinstance(live, dict):
            return False
        return all(
            (not is_write_only(k, v) and same_value(live.get(k), v)) or is_write_only(k, v)
            for k, v in wanted.items()
        )
    if isinstance(wanted, list):
        return isinstance(live, list) and len(live) == len(wanted) and all(map(same_value, live, wanted))
    return live == wanted
