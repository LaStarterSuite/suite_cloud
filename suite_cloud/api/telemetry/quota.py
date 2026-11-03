"""The sending-limit API: each node's Agent pulls its cluster's limits and pushes what it counted.

The auth hook resolves the caller's cluster from its credentials; ``host_ip`` must be one of that
cluster's Active or Draining nodes, and only that cluster's accounts are read or reported.
"""

import ipaddress
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from suite_cloud.api.telemetry import (
    DEFAULT_REQUESTS_PER_MINUTE,
    Record,
    get_limit,
    get_records,
    telemetry_api,
)
from suite_cloud.api.tenant import InvalidInputError, throttle
from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import reportable_periods, upsert_usage

PAGE_SIZE = 500
VERSION_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
CURSOR_SEPARATOR = "|"
# A save's modified time is set before its commit; rows this recent may still be joined by an
# older one, so the next pull returns them instead.
SETTLE_SECONDS = 5
NODE_STATUSES = ("Active", "Draining")


@frappe.whitelist(methods=["POST"])
@telemetry_api
def limits(host_ip: str, since_version: str | None = None) -> dict:
    """The caller's cluster's limits changed after ``since_version``, oldest first, a page at a
    time. ``next_version`` is an opaque cursor for the next call; empty starts over."""

    account = frappe.qb.DocType("Mail Account")
    query = (
        frappe.qb.from_(account)
        .select(account.name, account.daily_send_limit, account.monthly_send_limit, account.modified)
        .where(account.cluster == get_cluster_for_host(host_ip))
        .where(account.modified <= now_datetime() - timedelta(seconds=SETTLE_SECONDS))
        .orderby(account.modified)
        .orderby(account.name)
        .limit(PAGE_SIZE + 1)
    )
    if since_version:
        modified, name = parse_cursor(since_version)
        query = query.where(
            (account.modified > modified) | ((account.modified == modified) & (account.name > name))
        )
    rows = query.run(as_dict=True)
    page = rows[:PAGE_SIZE]
    return {
        "limits": [
            {
                "account": row.name,
                "daily": row.daily_send_limit,
                "monthly": row.monthly_send_limit,
                "version": row.modified.strftime(VERSION_FORMAT),
            }
            for row in page
        ],
        "next_version": cursor(page[-1]) if page else since_version or "",
        "has_more": len(rows) > PAGE_SIZE,
    }


@frappe.whitelist(methods=["POST"])
@telemetry_api
def usage(host_ip: str, records: list) -> dict:
    """Stores reported totals: ``{account, period, sent, refused}`` each, ``refused`` counting
    messages refused over a limit. Accounts of other clusters and periods other than today, this
    month and the ones before are dropped."""

    cluster = get_cluster_for_host(host_ip)
    rows = [parse_usage(record) for record in get_records(records)]
    periods = reportable_periods()
    return {"accepted": upsert_usage([row for row in rows if row["period"] in periods], cluster)}


def get_cluster_for_host(host_ip: str) -> str:
    """The caller's cluster, once ``host_ip`` proves to be one of its Active or Draining nodes; the
    host is throttled like any telemetry sender. The call is not noted as contact: the telemetry status
    reports Stalwart's last push, which a working limit pull must not stand in for."""

    try:
        address = str(ipaddress.ip_address(host_ip))
    except ValueError:
        frappe.throw(_("host_ip must be an IPv4 or IPv6 address."), InvalidInputError)
    throttle(f"agent:{address}", get_limit("agent_requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE))
    cluster = frappe.local.agent_cluster
    node = {"cluster": cluster, "ipv4_address": address, "status": ("in", NODE_STATUSES)}
    if not frappe.db.exists("Stalwart Node", node):
        frappe.throw(
            _("host_ip {0} is not an active Stalwart Node of the Agent's cluster.").format(address),
            frappe.PermissionError,
        )
    return cluster


def cursor(row) -> str:
    return f"{row.modified.strftime(VERSION_FORMAT)}{CURSOR_SEPARATOR}{row.name}"


def parse_cursor(value: str) -> tuple[datetime, str]:
    modified, _separator, name = str(value).partition(CURSOR_SEPARATOR)
    try:
        moment = datetime.strptime(modified, VERSION_FORMAT)
    except ValueError:
        moment = None
    if not moment or not name:
        frappe.throw(_("since_version must be a next_version this API returned."), InvalidInputError)
    return moment, name


def parse_usage(record: Record) -> dict:
    counts = {}
    for key in ("sent", "refused"):
        counts[key] = record.number(key, required=key == "sent", integer=True) or 0
        if counts[key] < 0:
            record.refuse(key, _("must not be negative"))
    return {
        "account": record.text("account", required=True, limit=320).lower(),
        "period": record.text("period", required=True, limit=10),
        **counts,
    }
