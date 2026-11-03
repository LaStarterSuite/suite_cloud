"""The telemetry API: Stalwart pushes its events and metrics, and asks about sending limits.

Stalwart sends ``Authorization: Bearer <telemetry key>:<telemetry secret>``, the token of the one
node or gateway that sent the request, to ``webhook`` and ``otlp``. The sending-limit hook
(``suite_cloud.api.sending_limits``) sends its cluster's Quota Hook Token, the cluster named in the
URL. The ``authenticate`` auth hook checks the token and runs the request as the telemetry user,
which holds no role, so a token opens nothing but these methods. A batch is staged as one parquet
file and acknowledged; the warehouse loads it later. The cache notes which host sent a batch last,
for the status on the node forms.
"""

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password
from google.protobuf.message import DecodeError
from werkzeug.wrappers import Response

from suite_cloud.api.tenant import InvalidInputError, assert_arguments, throttle
from suite_cloud.mail import sending_limits
from suite_cloud.telemetry import EVENTS, METRICS, TELEMETRY_USER
from suite_cloud.telemetry.push import (
    STREAM_EVENTS,
    get_batch_id,
    get_event_rows,
    get_metric_rows,
    get_metrics_batch_id,
)
from suite_cloud.telemetry.spool import Spool
from suite_cloud.telemetry.status import record_contact
from suite_cloud.utils import get_config, log_error

API_PATHS = ("/api/v2/method/suite_cloud.api.telemetry.", "/api/method/suite_cloud.api.telemetry.")
HOOK_PATHS = ("/api/v2/method/suite_cloud.api.sending_limits.", "/api/method/suite_cloud.api.sending_limits.")
DEFAULT_REQUESTS_PER_MINUTE = 300  # the settings' defaults, used until the site has them
WEBHOOK_EVENT_LIMIT = 65536  # what one request may stage; Stalwart sends every waiting event at once
SOURCE = "stalwart"
SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")


def authenticate() -> None:
    """Frappe auth hook: a bearer token on the telemetry API is a server's telemetry token, and on
    the sending-limit hook the named cluster's Quota Hook Token.

    Elsewhere a bearer token is Frappe's own OAuth business, so the hook leaves it alone.
    """

    scheme, _space, token = frappe.get_request_header("Authorization", "").partition(" ")
    path = frappe.request.path
    if scheme.lower() != "bearer" or not path.startswith(API_PATHS + HOOK_PATHS):
        return

    is_hook = path.startswith(HOOK_PATHS)
    caller = get_hook_cluster(token.strip()) if is_hook else get_telemetry_server(token.strip())
    if not caller:
        request_ip = getattr(frappe.local, "request_ip", None)
        throttle(f"address:{request_ip or 'unknown'}")  # guessing secrets is paced like everything else
        raise frappe.AuthenticationError(_("Telemetry authentication failed."))

    form_dict = frappe.local.form_dict
    frappe.set_user(TELEMETRY_USER)
    frappe.local.form_dict = form_dict  # set_user clears the request body Frappe already parsed
    if is_hook:
        frappe.local.hook_cluster = caller
    else:
        frappe.local.telemetry_server = caller


def get_hook_cluster(token: str) -> str | None:
    """The cluster named in the URL's ``cluster``, when ``token`` is its Quota Hook Token; None for
    anything else. Telemetry being off does not matter: the sending limits hold regardless."""

    cluster = frappe.request.args.get("cluster")
    stored = cluster and sending_limits.get_token_hash(cluster)
    given = hashlib.sha256(token.encode()).hexdigest()
    if not stored or not secrets.compare_digest(stored, given):
        return None
    return cluster


def get_telemetry_server(token: str) -> frappe._dict | None:
    """The Stalwart Node or Egress Gateway whose telemetry key and secret make up
    ``<key>:<secret>``, while telemetry is on; None for anything else."""

    key, secret = split_token(token)
    if not key:
        return None
    for doctype in SERVER_DOCTYPES:
        fields = ["name", "ipv4_address", "hostname"]
        if server := frappe.db.get_value(doctype, {"telemetry_key": key}, fields, as_dict=True):
            stored = get_decrypted_password(doctype, server.name, "telemetry_secret", raise_exception=False)
            if stored and secrets.compare_digest(stored, secret):
                return frappe._dict(server, doctype=doctype)
            return None
    return None


def split_token(token: str) -> tuple[str | None, str | None]:
    key, separator, secret = token.partition(":")
    if not (separator and key and secret and cint(get_config("enable_telemetry"))):
        return None, None
    return key, secret


def hook_api(fn: Callable) -> Callable:
    """Only Stalwart, signed in with a cluster's Quota Hook Token, may call the method. It is not
    throttled: a refused check would be an accepted recipient."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if frappe.session.user != TELEMETRY_USER or not getattr(frappe.local, "hook_cluster", None):
            frappe.throw(_("Only a Stalwart cluster may call this method."), frappe.PermissionError)
        assert_arguments(fn, args, kwargs)
        return fn(*args, **kwargs)

    wrapper.is_telemetry_api = True  # the contract test checks every endpoint carries this
    return wrapper


def push_api(fn: Callable) -> Callable:
    """Only Stalwart, signed in with a server's telemetry token, may call the method. Each server
    is throttled on its own."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        server = getattr(frappe.local, "telemetry_server", None)
        if frappe.session.user != TELEMETRY_USER or not server:
            frappe.throw(_("Only a Stalwart server may call this method."), frappe.PermissionError)
        limit = get_limit("agent_requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE)
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
    server = frappe.local.telemetry_server
    rows, problems = get_event_rows(events, stream)
    if problems:
        log_error(
            f"Telemetry webhook from {server.hostname}: {len(problems)} events skipped",
            "\n".join(problems[:20]),
        )
    if rows:
        envelope = get_server_envelope(server, get_batch_id(server.name, events))
        Spool().write(EVENTS, EVENTS.get_batch(envelope, rows), envelope["batch_id"])
    record_contact(server.ipv4_address)
    return {"accepted": len(rows), "skipped": len(problems)}


@frappe.whitelist(methods=["POST"])
@push_api
def otlp() -> Response:
    """Stalwart's OTLP metrics push: an ``ExportMetricsServiceRequest`` in protobuf. Answers 200
    with an empty body, which Stalwart accepts."""

    server = frappe.local.telemetry_server
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
    record_contact(server.ipv4_address)
    return Response(status=200)  # an empty ExportMetricsServiceResponse


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
