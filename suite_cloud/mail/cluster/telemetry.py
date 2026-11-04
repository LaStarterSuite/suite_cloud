"""What Stalwart pushes to Suite Cloud on its own: events through three WebHooks and metrics over
OTLP, each request carrying the sending server's Suite Cloud token (``server_token``). The plan
turns the push on only while telemetry is on and every running server of the cluster, or the
gateway, has its token installed; until then it writes the WebHooks switched off and OTLP disabled.
"""

from typing import TYPE_CHECKING

import frappe
from frappe.utils import cint, get_url

from suite_cloud.mail.cluster import server_token
from suite_cloud.mail.cluster.plan import as_set
from suite_cloud.telemetry import ERROR_EVENTS, WARN_EVENTS, WEBHOOK_EVENTS
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

API_METHOD = "/api/v2/method/suite_cloud.api.telemetry."
WEBHOOK_THROTTLE_MS = 30_000  # the warehouse loads every 5 minutes, so sooner buys nothing
WEBHOOK_TIMEOUT_MS = 30000
OTLP_TIMEOUT_MS = 10000
DEFAULT_DISCARD_AFTER_MINUTES = 10
DEFAULT_PUSH_INTERVAL_SECONDS = 60
# WebHook has no description, and its URL follows the Public URL, so this header names the stream.
STREAM_HEADER = "X-Suite-Cloud-Stream"


def get_reporting_servers(cluster: Document | None = None) -> list[Document]:
    """The servers that report, in one cluster or in every cluster: Active and Draining nodes and
    Active gateways."""

    filters = {"cluster": cluster.name} if cluster else {}
    nodes = frappe.get_all(
        "Stalwart Node", {**filters, "status": ("in", ("Active", "Draining"))}, pluck="name"
    )
    gateways = frappe.get_all("Egress Gateway", {**filters, "status": "Active"}, pluck="name")
    return [
        *(frappe.get_doc("Stalwart Node", name) for name in nodes),
        *(frappe.get_doc("Egress Gateway", name) for name in gateways),
    ]


def is_push_enabled(owner: Document) -> bool:
    """Telemetry is on and every server running the owner's plan has its token installed."""

    from suite_cloud.telemetry.install import is_telemetry_enabled  # it builds on this module

    return is_telemetry_enabled() and server_token.has_every_token(owner)


def get_operations(owner: Document) -> list[dict]:
    """The three WebHooks and the metrics push of a cluster or gateway plan. Nothing reads the
    Prometheus endpoint, which sits on the public listener, so it stays off."""

    enabled = is_push_enabled(owner)
    webhooks = {
        "telemetry-events": webhook("events", "info", WEBHOOK_EVENTS, enabled),
        "telemetry-errors": webhook("errors", "error", ERROR_EVENTS, enabled),
        "telemetry-warnings": webhook("warnings", "warn", WARN_EVENTS, enabled),
    }
    metrics = {
        "openTelemetry": otlp_exporter() if enabled else {"@type": "Disabled"},
        "prometheus": {"@type": "Disabled"},
    }
    return [
        {"@type": "upsert", "object": "WebHook", "matchOn": ["httpHeaders"], "value": webhooks},
        {"@type": "update", "object": "Metrics", "value": metrics},
    ]


def webhook(stream: str, level: str, events: tuple[str, ...], enabled: bool) -> dict:
    """``events`` sends the events the dashboards read; ``errors`` and ``warnings`` every other
    error and warning, each in its own queue. Stalwart ignores a WebHook's level, so only the
    list decides what it sends."""

    discard_after = cint(get_config("webhook_discard_after_minutes")) or DEFAULT_DISCARD_AFTER_MINUTES
    return {
        "enable": enabled,
        "url": f"{api_url('webhook')}?stream={stream}",
        "level": level,
        "events": as_set(events),
        "eventsPolicy": "include",
        "httpAuth": server_token.bearer_auth(),
        "httpHeaders": {STREAM_HEADER: stream},
        "signatureKey": {"@type": "None"},
        "throttle": WEBHOOK_THROTTLE_MS,
        "timeout": WEBHOOK_TIMEOUT_MS,
        "discardAfter": discard_after * 60_000,
        "lossy": False,
        "allowInvalidCerts": False,
    }


def otlp_exporter() -> dict:
    interval = cint(get_config("metrics_push_interval_seconds")) or DEFAULT_PUSH_INTERVAL_SECONDS
    return {
        "@type": "Http",
        "endpoint": api_url("otlp"),
        "interval": interval * 1000,
        "timeout": OTLP_TIMEOUT_MS,
        "httpAuth": server_token.bearer_auth(),
        "httpHeaders": {},
    }


def api_url(method: str) -> str:
    return f"{public_url()}{API_METHOD}{method}"


def public_url() -> str:
    """Where Stalwart reaches Suite Cloud."""

    return get_config("public_url") or get_url()
