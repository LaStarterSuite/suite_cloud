"""What Stalwart pushes to Suite Cloud on its own: events through two WebHooks and metrics over
OTLP, each request carrying the sending server's telemetry token.

Stalwart reads the token from ``SUITE_CLOUD_TELEMETRY_TOKEN`` in each server's ``stalwart.env``,
and a reload that names a variable a running server lacks fails. So the plan turns the push on
only once every running server of the cluster, or the gateway, has the token installed; until
then it writes both WebHooks switched off and OTLP disabled, which reload anywhere.
"""

from typing import TYPE_CHECKING

import frappe
from frappe.utils import cint, get_url

from suite_cloud.mail.cluster.plan import TELEMETRY_TOKEN_VARIABLE, as_set, secret_value
from suite_cloud.telemetry import ERROR_EVENTS, WEBHOOK_EVENTS
from suite_cloud.telemetry.agent.build import is_telemetry_enabled
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

RUNNING_STATUSES = ("Provisioned", "Active", "Draining")  # Stalwart runs normally, env file and all
API_METHOD = "/api/v2/method/suite_cloud.api.telemetry."
WEBHOOK_THROTTLE_MS = 5000
WEBHOOK_TIMEOUT_MS = 30000
OTLP_TIMEOUT_MS = 10000
DEFAULT_DISCARD_AFTER_MINUTES = 60
DEFAULT_PUSH_INTERVAL_SECONDS = 60
METRICS_USERNAME = "telemetry"  # the old agents' basic-auth user on /metrics/prometheus


def fill_token(server: Document) -> None:
    """A Stalwart Node or Egress Gateway gets its own key and secret when it is created."""

    server.telemetry_key = server.telemetry_key or frappe.generate_hash(length=20)
    if not server.telemetry_secret:
        server.telemetry_secret = frappe.generate_hash(length=40)


def get_owner(server: Document) -> Document:
    """Whose plan carries the server's WebHooks: its cluster's for a node, its own for a gateway."""

    return server if server.doctype == "Egress Gateway" else server.get_cluster()


def is_push_enabled(owner: Document) -> bool:
    """Telemetry is on and every server running the owner's plan has its token installed."""

    if not is_telemetry_enabled():
        return False
    if owner.doctype == "Egress Gateway":
        return owner.status not in RUNNING_STATUSES or bool(owner.telemetry_token_installed)
    return not frappe.db.exists(
        "Stalwart Node",
        {"cluster": owner.name, "status": ("in", RUNNING_STATUSES), "telemetry_token_installed": 0},
    )


def get_operations(owner: Document) -> list[dict]:
    """The two WebHooks and the Metrics exporters for a cluster or gateway plan.

    The Prometheus endpoint stays as it was until the push takes over, since the agents still
    scrape it until then.
    """

    enabled = is_push_enabled(owner)
    webhooks = {
        "telemetry-events": webhook("events", "info", WEBHOOK_EVENTS, enabled),
        "telemetry-errors": webhook("errors", "error", ERROR_EVENTS, enabled),
    }
    metrics = {
        "openTelemetry": otlp_exporter() if enabled else {"@type": "Disabled"},
        "prometheus": {"@type": "Disabled"} if enabled else prometheus_exporter(owner),
    }
    return [
        {"@type": "upsert", "object": "WebHook", "matchOn": ["url"], "value": webhooks},
        {"@type": "update", "object": "Metrics", "value": metrics},
    ]


def webhook(stream: str, level: str, events: tuple[str, ...], enabled: bool) -> dict:
    """``events`` sends the events the dashboards read; ``errors`` every other error. Stalwart
    ignores a WebHook's level, so only the list decides what it sends."""

    discard_after = cint(get_config("webhook_discard_after_minutes")) or DEFAULT_DISCARD_AFTER_MINUTES
    return {
        "enable": enabled,
        "url": f"{api_url('webhook')}?stream={stream}",
        "level": level,
        "events": as_set(events),
        "eventsPolicy": "include",
        "httpAuth": bearer_auth(),
        "httpHeaders": {},
        "signatureKey": {"@type": "None"},
        "throttle": WEBHOOK_THROTTLE_MS,
        "timeout": WEBHOOK_TIMEOUT_MS,
        "discardAfter": discard_after * 60_000,
        "lossy": False,
        "allowInvalidCerts": False,
    }


def otlp_exporter() -> dict:
    interval = cint(get_config("agent_metrics_interval_seconds")) or DEFAULT_PUSH_INTERVAL_SECONDS
    return {
        "@type": "Http",
        "endpoint": api_url("otlp"),
        "interval": interval * 1000,
        "timeout": OTLP_TIMEOUT_MS,
        "httpAuth": bearer_auth(),
        "httpHeaders": {},
    }


def prometheus_exporter(owner: Document) -> dict:
    """Behind a password: it sits on the public HTTPS listener, and without credentials Stalwart
    serves it to anyone."""

    cluster = owner.get_cluster() if owner.doctype == "Egress Gateway" else owner
    if not cluster.enable_metrics:
        return {"@type": "Disabled"}
    secret = owner.get_password("metrics_password", raise_exception=False)
    if not secret:
        raise ValueError("The metrics password is not set")
    return {"@type": "Enabled", "authUsername": METRICS_USERNAME, "authSecret": secret_value(secret)}


def bearer_auth() -> dict:
    """Each server sends its own token: the variable is read on the server that sends."""

    return {
        "@type": "Bearer",
        "bearerToken": {"@type": "EnvironmentVariable", "variableName": TELEMETRY_TOKEN_VARIABLE},
    }


def api_url(method: str) -> str:
    return f"{get_config('public_url') or get_url()}{API_METHOD}{method}"
