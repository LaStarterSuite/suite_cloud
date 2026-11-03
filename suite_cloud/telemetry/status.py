"""Whether servers report: the telemetry API notes the last batch each host sent, and the node,
gateway and cluster forms show it."""

from datetime import timedelta
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime, pretty_date

from suite_cloud.mail.cluster.telemetry import get_reporting_servers
from suite_cloud.telemetry.alerts import DEFAULT_SILENCE_MINUTES
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

CACHE_KEY = "suite_cloud_telemetry_contact"
SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")
STATES = ("silent", "unknown", "ok")  # worst first


def record_contact(host_ip: str) -> None:
    """Called by the API for every accepted batch; the cache costs one write, the database would
    cost a row update per request."""

    frappe.cache.hset(CACHE_KEY, host_ip, {"seen_at": now_datetime().isoformat()})


def forget_contacts() -> None:
    frappe.cache.delete_value(CACHE_KEY)


def server_status(server: Document) -> dict:
    """``state`` is ``ok``, ``silent`` (no batch within the silence window of the alerts) or
    ``unknown`` (no batch recorded); ``message`` says so."""

    contact = frappe.cache.hget(CACHE_KEY, server.ipv4_address) or {}
    status = {"hostname": server.hostname}
    if not contact:
        status["state"] = "unknown"
        status["message"] = _("No telemetry received from this server yet.")
        return status

    seen = get_datetime(contact["seen_at"])
    status["seen_at"] = seen
    silence = timedelta(minutes=cint(get_config("telemetry_silence_minutes")) or DEFAULT_SILENCE_MINUTES)
    if now_datetime() - seen > silence:
        status["state"] = "silent"
        status["message"] = _(
            "No telemetry since {0}. Check the WebHook and OTLP errors in Stalwart's log on the server."
        ).format(pretty_date(seen))
    else:
        status["state"] = "ok"
        status["message"] = _("Telemetry received {0}.").format(pretty_date(seen))
    return status


def cluster_summary(cluster: Document) -> dict:
    """One line for the cluster form over the servers that report; ``state`` is the worst."""

    statuses = [server_status(server) for server in get_reporting_servers(cluster)]
    if not statuses:
        return {"state": "ok", "message": "", "statuses": []}

    by_state = {state: [s["hostname"] for s in statuses if s["state"] == state] for state in STATES}
    parts = []
    if by_state["ok"]:
        parts.append(_("{0} reporting").format(len(by_state["ok"])))
    for state, label in (
        ("silent", _("{0} silent ({1})")),
        ("unknown", _("{0} without a batch yet ({1})")),
    ):
        if by_state[state]:
            parts.append(label.format(len(by_state[state]), ", ".join(by_state[state])))
    worst = next(state for state in STATES if by_state[state])
    return {"state": worst, "message": _("Telemetry: {0}.").format(", ".join(parts)), "statuses": statuses}


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
