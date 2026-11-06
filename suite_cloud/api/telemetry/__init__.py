"""The telemetry API: Stalwart pushes its events and metrics, and asks about sending limits; each
node's scan shipper pushes what its Rspamd scored.

Stalwart and the shipper send ``Authorization: Bearer <key>.<secret>``, the Suite Cloud token of the
one node or gateway that sent the request (``server_token``). ``webhook``, ``otlp`` and
``outbound_scans`` take any server's token
while telemetry is on; the sending-limit hook (``suite_cloud.api.sending_limits``) takes a Stalwart
Node's whatever telemetry is set to. The ``authenticate`` auth hook checks the token and runs the
request as the telemetry user, which holds no role, so a token opens nothing but these methods. A
batch is staged as one parquet file and acknowledged; the warehouse loads it later. The cache notes
when each host last sent each kind of telemetry, for the status on the node forms.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps

import frappe
from frappe import _
from frappe.utils import cint
from google.protobuf.message import DecodeError
from werkzeug.wrappers import Response

from suite_cloud.api.tenant import InvalidInputError, assert_arguments, throttle
from suite_cloud.mail.cluster import server_token
from suite_cloud.telemetry import (
    EVENTS,
    METRICS,
    OUTBOUND_SCAN_SYMBOLS,
    OUTBOUND_SCANS,
    TELEMETRY_USER,
    status,
)
from suite_cloud.telemetry import outbound_scans as scans_module
from suite_cloud.telemetry.push import (
    STREAM_EVENTS,
    get_batch_id,
    get_event_rows,
    get_metric_rows,
    get_metrics_batch_id,
)
from suite_cloud.telemetry.spool import Spool
from suite_cloud.utils import get_config, log_error

API_PATHS = ("/api/v2/method/suite_cloud.api.telemetry.", "/api/method/suite_cloud.api.telemetry.")
HOOK_PATHS = ("/api/v2/method/suite_cloud.api.sending_limits.", "/api/method/suite_cloud.api.sending_limits.")
DEFAULT_REQUESTS_PER_MINUTE = 300  # the settings' defaults, used until the site has them
WEBHOOK_EVENT_LIMIT = 65536  # what one request may stage; Stalwart sends every waiting event at once
SCAN_LIMIT = 5000  # the shipper sends 2000 records per request
SOURCE = "stalwart"


def authenticate() -> None:
    """Frappe auth hook: a bearer token on the telemetry API or the sending-limit hook is a
    server's Suite Cloud token.

    Elsewhere a bearer token is Frappe's own OAuth business, so the hook leaves it alone.
    """

    scheme, _space, token = frappe.get_request_header("Authorization", "").partition(" ")
    path = frappe.request.path
    if scheme.lower() != "bearer" or not path.startswith(API_PATHS + HOOK_PATHS):
        return

    server = get_caller(token.strip(), is_hook=path.startswith(HOOK_PATHS))
    if not server:
        request_ip = getattr(frappe.local, "request_ip", None)
        throttle(f"address:{request_ip or 'unknown'}")  # guessing secrets is paced like everything else
        raise frappe.AuthenticationError(_("Telemetry authentication failed."))

    form_dict = frappe.local.form_dict
    frappe.set_user(TELEMETRY_USER)
    frappe.local.form_dict = form_dict  # set_user clears the request body Frappe already parsed
    frappe.local.stalwart_server = server


def get_caller(token: str, is_hook: bool) -> frappe._dict | None:
    """The hook takes a Stalwart Node's token: the sending limits hold whether telemetry is on or
    not. The telemetry API takes any server's, while telemetry is on."""

    server = server_token.get_server(token)
    if not server:
        return None
    if is_hook:
        return server if server.doctype == "Stalwart Node" else None
    return server if cint(get_config("enable_telemetry")) else None


def hook_api(fn: Callable) -> Callable:
    """Only a Stalwart Node, signed in with its Suite Cloud token, may call the method. It is not
    throttled: a refused check would be an accepted recipient."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        server = getattr(frappe.local, "stalwart_server", None)
        if frappe.session.user != TELEMETRY_USER or not server or server.doctype != "Stalwart Node":
            frappe.throw(_("Only a Stalwart node may call this method."), frappe.PermissionError)
        assert_arguments(fn, args, kwargs)
        return fn(*args, **kwargs)

    wrapper.is_telemetry_api = True  # the contract test checks every endpoint carries this
    return wrapper


def push_api(fn: Callable) -> Callable:
    """Only Stalwart, signed in with a server's Suite Cloud token, may call the method. Each server
    is throttled on its own."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        server = getattr(frappe.local, "stalwart_server", None)
        if frappe.session.user != TELEMETRY_USER or not server:
            frappe.throw(_("Only a Stalwart server may call this method."), frappe.PermissionError)
        limit = get_limit("server_requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE)
        throttle(f"server:{server.ipv4_address}", limit)
        assert_arguments(fn, args, kwargs)
        return fn(*args, **kwargs)

    wrapper.is_telemetry_api = True  # the contract test checks every endpoint carries this
    return wrapper


@frappe.whitelist(methods=["POST"])
@push_api
def webhook(events: list) -> dict:
    """Stalwart's WebHook: ``{"events": [{id, createdAt, type, data}]}``, the stream in the URL.

    An event that does not parse is skipped and counted in one Error Log, since a refused request
    is sent again until Stalwart drops it."""

    stream = frappe.request.args.get("stream")
    if stream not in STREAM_EVENTS:
        frappe.throw(_("stream must be one of {0}.").format(", ".join(STREAM_EVENTS)), InvalidInputError)
    if len(events) > WEBHOOK_EVENT_LIMIT:
        frappe.throw(
            _("Send at most {0} events in one request.").format(WEBHOOK_EVENT_LIMIT), InvalidInputError
        )
    server = frappe.local.stalwart_server
    rows, problems = get_event_rows(events, stream)
    if problems:
        log_error(
            f"Telemetry webhook from {server.hostname}: {len(problems)} events skipped",
            "\n".join(problems[:20]),
        )
    if rows:
        envelope = get_server_envelope(server, get_batch_id(server.name, events))
        Spool().write(EVENTS, EVENTS.get_batch(envelope, rows), envelope["batch_id"])
    status.record_contact(server.ipv4_address, status.EVENTS)
    return {"accepted": len(rows), "skipped": len(problems)}


@frappe.whitelist(methods=["POST"])
@push_api
def otlp() -> Response:
    """Stalwart's OTLP metrics push: an ``ExportMetricsServiceRequest`` in protobuf. Answers 200
    with an empty body, which Stalwart accepts."""

    server = frappe.local.stalwart_server
    try:
        body = frappe.request.get_data()
        rows, skipped = get_metric_rows(body)
    except DecodeError:
        frappe.throw(_("The body is not an OTLP metrics request."), InvalidInputError)
    if skipped:
        log_error(f"Telemetry metrics from {server.hostname}: {skipped} data points skipped", "")
    if rows:
        envelope = get_server_envelope(server, get_metrics_batch_id(server.name, body))
        Spool().write(METRICS, METRICS.get_batch(envelope, rows), envelope["batch_id"])
    status.record_contact(server.ipv4_address, status.METRICS)
    return Response(status=200)  # an empty ExportMetricsServiceResponse


@frappe.whitelist(methods=["POST"])
@push_api
def outbound_scans(scans: list) -> dict:
    """The scan shipper of a node: ``{"scans": [record]}``, one record per message Rspamd scanned
    for a signed-in sender. A record that does not parse is skipped and counted in one Error Log."""

    if len(scans) > SCAN_LIMIT:
        frappe.throw(_("Send at most {0} scans in one request.").format(SCAN_LIMIT), InvalidInputError)
    server = frappe.local.stalwart_server
    rows, symbol_rows, problems = scans_module.get_scan_rows(scans)
    if problems:
        log_error(
            f"Outbound scans from {server.hostname}: {len(problems)} records skipped",
            "\n".join(problems[:20]),
        )
    envelope = get_server_envelope(server, scans_module.get_batch_id(server.name, scans))
    spool = Spool()
    for table, table_rows in ((OUTBOUND_SCANS, rows), (OUTBOUND_SCAN_SYMBOLS, symbol_rows)):
        if table_rows:
            spool.write(table, table.get_batch(envelope, table_rows), envelope["batch_id"])
    status.record_contact(server.ipv4_address, status.SCAN_CHECK_IN)
    if rows:
        status.record_contact(server.ipv4_address, status.SCAN_RECORDS)
    return {"accepted": len(rows), "skipped": len(problems)}


def get_server_envelope(server: frappe._dict, batch_id: str) -> dict:
    """What every row shares; the server record says who sent it, never the request."""

    return {
        "batch_id": batch_id,
        "received_at": datetime.now(UTC).replace(tzinfo=None),
        "source": SOURCE,
        "host_ip": server.ipv4_address,
        "hostname": server.hostname,
    }


def get_limit(key: str, default: int) -> int:
    """A limit from the settings; a site that has not filled the field yet gets the default."""

    return cint(get_config(key)) or default
