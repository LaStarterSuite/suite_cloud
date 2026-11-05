"""Whether servers report: the telemetry API notes when each host last sent each kind of telemetry,
and the node, gateway and cluster forms show one indicator per kind.

Metrics and the scan shipper send on a schedule, so a gap past the silence window is a fault.
Events come only when something happens, so a quiet server is normal.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime, pretty_date

from suite_cloud.mail.cluster import rspamd
from suite_cloud.mail.cluster.telemetry import get_reporting_servers
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

CACHE_KEY = "suite_cloud_telemetry_contact"  # one hash per stream, host IP to its last batch
METRICS = "metrics"
EVENTS = "events"
SCAN_CHECK_IN = "scan_check_in"  # each run of a node's scan shipper, records or not
SCAN_RECORDS = "scan_records"  # a shipper request that carried records
STREAMS = (METRICS, EVENTS, SCAN_CHECK_IN, SCAN_RECORDS)
SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")
DEFAULT_SILENCE_MINUTES = 30  # the settings' default, used until the site has it
STATES = ("silent", "outdated", "unknown", "quiet", "off", "ok")  # worst first
TELEMETRY = "telemetry"  # the one indicator while telemetry is off
COLORS = {
    "silent": "red",
    "outdated": "orange",
    "unknown": "blue",
    "quiet": "gray",
    "off": "gray",
    "ok": "green",
}


def record_contact(host_ip: str, stream: str) -> None:
    """Called by the API for every accepted batch; the cache costs one write, the database would
    cost a row update per request."""

    frappe.cache.hset(f"{CACHE_KEY}:{stream}", host_ip, {"seen_at": now_datetime().isoformat()})


def forget_contacts() -> None:
    for stream in STREAMS:
        frappe.cache.delete_value(f"{CACHE_KEY}:{stream}")


def get_seen_at(host_ip: str, stream: str) -> datetime | None:
    contact = frappe.cache.hget(f"{CACHE_KEY}:{stream}", host_ip) or {}
    return get_datetime(contact["seen_at"]) if contact else None


def get_silence() -> timedelta:
    """How long a server that reports on a schedule may stay quiet; the forms and the alerts share it."""

    return timedelta(minutes=cint(get_config("telemetry_silence_minutes")) or DEFAULT_SILENCE_MINUTES)


def is_recent(seen: datetime) -> bool:
    return now_datetime() - seen <= get_silence()


def get_label(stream: str) -> str:
    labels = {
        TELEMETRY: _("Telemetry"),
        METRICS: _("Metrics"),
        EVENTS: _("Events"),
        SCAN_CHECK_IN: _("Outbound scan"),
        SCAN_RECORDS: _("Scan records"),
    }
    return labels[stream]


def indicator(stream: str, state: str, detail: str) -> dict:
    """Keep ``detail`` short: Frappe squeezes an indicator's dot when its text wraps."""

    text = f"{get_label(stream)}: {detail}"
    return {"stream": stream, "state": state, "text": text, "color": COLORS[state]}


def server_status(server: Document) -> dict:
    """``indicators`` holds one ``{stream, state, text, color}`` per kind of telemetry the server
    sends: metrics and events, and for a node the outbound scan and its records."""

    if not cint(get_config("enable_telemetry")):
        return {"hostname": server.hostname, "indicators": [indicator(TELEMETRY, "off", _("off"))]}
    indicators = [metrics_indicator(server), events_indicator(server)]
    if server.doctype == "Stalwart Node":
        indicators.extend(scan_indicators(server))
    return {"hostname": server.hostname, "indicators": indicators}


def metrics_indicator(server: Document) -> dict:
    seen = get_seen_at(server.ipv4_address, METRICS)
    if not seen:
        return indicator(METRICS, "unknown", _("none received yet"))
    if is_recent(seen):
        return indicator(METRICS, "ok", pretty_date(seen))
    return indicator(METRICS, "silent", _("none since {0}").format(pretty_date(seen)))


def events_indicator(server: Document) -> dict:
    seen = get_seen_at(server.ipv4_address, EVENTS)
    if not seen:
        return indicator(EVENTS, "unknown", _("none received yet"))
    if is_recent(seen):
        return indicator(EVENTS, "ok", pretty_date(seen))
    return indicator(EVENTS, "quiet", _("last {0}").format(pretty_date(seen)))


def scan_indicators(node: Document) -> list[dict]:
    """The scanner's install state first: a shipper that checks in from an outdated scanner still
    needs an install. The records show beside a working shipper once some have arrived."""

    if get_config("outbound_scan_mode") == "Off":
        return [indicator(SCAN_CHECK_IN, "off", _("off"))]
    if not node.outbound_scanner_installed:
        return [indicator(SCAN_CHECK_IN, "off", _("not installed"))]
    if node.outbound_scanner_digest != rspamd.get_config_digest():
        return [indicator(SCAN_CHECK_IN, "outdated", _("scanner out of date"))]

    checked_in = get_seen_at(node.ipv4_address, SCAN_CHECK_IN)
    if not checked_in:
        return [indicator(SCAN_CHECK_IN, "unknown", _("not checked in yet"))]
    if not is_recent(checked_in):
        return [indicator(SCAN_CHECK_IN, "silent", _("silent since {0}").format(pretty_date(checked_in)))]
    indicators = [indicator(SCAN_CHECK_IN, "ok", pretty_date(checked_in))]
    if seen := get_seen_at(node.ipv4_address, SCAN_RECORDS):
        indicators.append(records_indicator(seen))
    return indicators


def records_indicator(seen: datetime) -> dict:
    if is_recent(seen):
        return indicator(SCAN_RECORDS, "ok", pretty_date(seen))
    return indicator(SCAN_RECORDS, "quiet", _("last {0}").format(pretty_date(seen)))


def cluster_summary(cluster: Document) -> dict:
    """One indicator per kind of telemetry over the servers that report; each takes the worst state
    of its servers and names the servers in a state that needs a look."""

    statuses = [server_status(server) for server in get_reporting_servers(cluster)]
    by_stream = {}
    for status in statuses:
        name = status["hostname"].removesuffix(f".{cluster.hostname}")  # n1, not n1.mail.example.com
        for item in status["indicators"]:
            by_stream.setdefault(item["stream"], []).append((name, item))
    indicators = [summarize(entries) for entries in by_stream.values()]
    return {"indicators": indicators, "statuses": statuses}


def summarize(entries: list[tuple[str, dict]]) -> dict:
    words = {
        "ok": _("{0} reporting"),
        "silent": _("{0} silent ({1})"),
        "outdated": _("{0} out of date ({1})"),
        "unknown": _("{0} not reported yet ({1})"),
        "quiet": _("{0} quiet"),
        "off": _("{0} off"),
    }
    parts = []
    for state in reversed(STATES):  # the healthy ones first, as a reader counts them
        hosts = [hostname for hostname, item in entries if item["state"] == state]
        if hosts:
            parts.append(words[state].format(len(hosts), ", ".join(hosts)))
    worst = next(state for state in STATES if any(item["state"] == state for _host, item in entries))
    return indicator(entries[0][1]["stream"], worst, ", ".join(parts))


@frappe.whitelist()
def get_server_status(doctype: str, name: str) -> dict:
    if doctype not in SERVER_DOCTYPES:
        frappe.throw(_("Not a server doctype: {0}").format(doctype), frappe.ValidationError)
    server = frappe.get_doc(doctype, name)
    server.check_permission("read")
    return server_status(server)


@frappe.whitelist()
def get_cluster_summary(cluster: str) -> dict:
    doc = frappe.get_doc("Stalwart Cluster", cluster)
    doc.check_permission("read")
    return cluster_summary(doc)
