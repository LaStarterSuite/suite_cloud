import functools
import json
import re
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.caching import request_cache

CONFIG_KEYS = (
    "default_dns_ttl",
    "public_url",
    "stalwart_version",
    "stalwart_cli_version",
    "stalwart_download_url_template",
    "stalwart_cli_download_url_template",
    "spam_filter_rules_version",
    "acme_directory_url",
    "acme_contact_email",
    "server_job_timeout",
    "sign_with_ed25519",
    "skip_domain_verification",
    "verify_stalwart_tls",
    "dmarc_report_retention_days",
    "tls_report_retention_days",
    "send_usage_retention_days",
    "enable_telemetry",
    "telemetry_retention_days",
    "agent_requests_per_minute",
    "telemetry_alert_recipients",
    "telemetry_silence_minutes",
    "telemetry_errors_per_hour",
    "metrics_push_interval_seconds",
    "webhook_discard_after_minutes",
    "agent_built_version",
    "central_url",
    "central_team",
    "central_api_key",
    "default_region",
    "central_image_offering",
    "central_plan",
    "max_sites_per_vm",
    "reserved_memory_mb",
    "memory_per_site_mb",
    "reserved_disk_gb",
    "disk_per_site_gb",
    "min_free_site_slots",
    "auto_create_vms",
    "suite_app_repo",
    "suite_app_ref",
    "ready_sites_per_region",
    "allow_signup",
    "allow_subaddressed_signups",
    "site_dns_zone",
    "tenant_mail_zone",
)


@request_cache
def get_config(key: str | tuple[str, ...] | None = None) -> dict[str, Any] | tuple | Any:
    """Fetches configuration values from Suite Cloud Settings.

    Cached per request: the returned dict is shared, so callers must treat it as read-only.
    """

    settings = frappe.get_cached_doc("Suite Cloud Settings")
    config = {field: settings.get(field) for field in CONFIG_KEYS}

    if not key:
        return config

    keys = (key,) if isinstance(key, str) else key
    for k in keys:
        if k not in config:
            frappe.throw(_("Suite Cloud config key '{0}' not found").format(k))

    return tuple(config[k] for k in keys) if len(keys) > 1 else config[keys[0]]


def clear_config_cache() -> None:
    """Forgets what get_config has read, so a change to Suite Cloud Settings is seen within the
    request that saved it: Frappe clears the document's own cache only once its hooks have run,
    and get_config keeps its answer for the rest of the request besides."""

    frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")
    cache = getattr(frappe.local, "request_cache", None)
    if cache is not None:
        cache.pop(get_config.__wrapped__, None)


def dkim_algorithms() -> tuple[str, ...]:
    """The key types Stalwart generates for a domain registered now: RSA always, Ed25519 by choice.

    Read at registration time only; Stalwart keeps the algorithms a domain was created with.
    """

    from suite_cloud.mail.stalwart.directory import DKIM_ED25519, DKIM_RSA

    return (DKIM_ED25519, DKIM_RSA) if cint(get_config("sign_with_ed25519")) else (DKIM_RSA,)


def utc_iso(value) -> str | None:
    """A stored datetime as an aware UTC string (``2026-09-09T10:00:00Z``) for API payloads.

    Frappe stores naive system-time values; handing them out naive lets a tenant in another time
    zone read them as its own local time and show them hours off.
    """

    if not value:
        return None
    from datetime import UTC
    from zoneinfo import ZoneInfo

    moment = frappe.utils.get_datetime(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone()))
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_datetime(value):
    """A UTC timestamp from Stalwart as the naive system-time value Frappe stores."""

    if not value:
        return None
    from datetime import UTC
    from zoneinfo import ZoneInfo

    moment = frappe.utils.get_datetime(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(frappe.utils.get_system_timezone())).replace(tzinfo=None)


def password_or_none(doc, field: str) -> str | None:
    """Returns the decrypted password if the field is set, otherwise None."""

    return doc.get_password(field) if doc.get(field) else None


def log_error(title: str | None = None, message: str | None = None, **kwargs) -> None:
    """Logs an error, prefixing the title with "[Suite Cloud]" so these errors can be filtered out."""

    prefix = "[Suite Cloud] "
    if title and not title.startswith(prefix):
        title = f"{prefix}{title}"

    frappe.log_error(title=title, message=message, **kwargs)


def log_exception(title: str, doc=None) -> None:
    """The current exception's traceback, without local variables.

    Frappe's default adds every frame's locals and masks only names that look like secrets; a
    connection or plan object holding a password would go in whole. The plain traceback carries
    what is needed to find the fault.
    """

    prefix = "[Suite Cloud] "
    if not title.startswith(prefix):
        title = f"{prefix}{title}"
    frappe.log_error(
        title=title,
        message=frappe.get_traceback(),
        reference_doctype=doc.doctype if doc is not None else None,
        reference_name=doc.name if doc is not None else None,
    )


def enqueue_job(
    method: str | Callable, job_id: str | None = None, deduplicate: bool = False, **kwargs
) -> None:
    """Enqueues a background job, deriving a stable job id when deduplicating."""

    if deduplicate and not job_id:
        job_id = method.split(".")[-1] if isinstance(method, str) else method.__name__

    frappe.enqueue(method, job_id=job_id, deduplicate=deduplicate, **kwargs)


@contextmanager
def user_context(user: str) -> Generator[None]:
    """Temporarily switches the session user."""

    session_user = frappe.session.user
    session_sid = frappe.session.sid
    session_data = frappe.session.data.copy()
    form_dict = frappe.local.form_dict

    if session_user == user:
        yield
        return

    try:
        frappe.set_user(user)
        yield
    finally:
        # frappe.set_user() overwrites session.sid with the username and wipes session.data and
        # form_dict, so restore all three alongside the user to avoid corrupting the original
        # session. form_dict matters beyond tidiness: rate limiting keys its counter off
        # form_dict.cmd, so leaving it emptied silently unlimits the rest of the request.
        frappe.set_user(session_user)
        frappe.session.sid = session_sid
        frappe.session.data = session_data
        frappe.local.form_dict = form_dict


def reconnect_on_failure(max_retries: int = 3) -> Callable:
    """Decorator that reconnects to the database and retries when the connection drops.

    Ansible plays run for minutes and report progress through the ORM, long enough for the
    server to close an idle connection in between events.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0

            while True:
                try:
                    return func(*args, **kwargs)

                except Exception as e:
                    if not is_connection_error(e) or retries >= max_retries:
                        raise type(e)(f"{e!s} | Retries attempted: {retries}/{max_retries}") from e

                    retries += 1
                    frappe.db.connect()

        return wrapper

    return decorator


def is_connection_error(exception: Exception) -> bool:
    operational_error = getattr(frappe.db, "OperationalError", ())
    return frappe.db.is_interface_error(exception) or isinstance(exception, operational_error)


def child_rows(doctype: str, parenttype: str, parents: list[str], fields: list[str]) -> dict[str, list]:
    """``{parent: [rows]}`` for a child table of many parents at once, rows in their saved order.

    A page of documents would otherwise cost one query per document per child table.
    """

    rows: dict[str, list] = {parent: [] for parent in parents}
    if not parents:
        return rows
    for row in frappe.get_all(
        doctype,
        filters={"parenttype": parenttype, "parent": ["in", parents]},
        fields=["parent", *fields],
        order_by="parent asc, idx asc",
    ):
        rows[row.parent].append(row)
    return rows


def alias_payloads(rows: list) -> list[dict]:
    return [{"email": r.alias_email, "enabled": bool(r.enabled), "description": r.description} for r in rows]


VERSION = re.compile(r"^v?\d+\.\d+\.\d+$")


def validate_version(value: str | None, label: str) -> str | None:
    """A release tag such as v0.16.20; it is interpolated into a download URL, so nothing else."""

    value = (value or "").strip()
    if not value:
        return None
    if not VERSION.match(value):
        frappe.throw(_("{0} must be a version such as v0.16.20.").format(label))
    return value


def secret_spellings(secrets: Iterable[str | None]) -> list[str]:
    """Each secret as it is, and as JSON and shell output would quote it (``\"``, ``\\``, ``é``),
    longest first so a secret containing another is masked whole."""

    spellings = set()
    for secret in secrets:
        if secret:
            spellings |= {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}
    return sorted(spellings, key=len, reverse=True)


def mask_secrets(text: str, spellings: list[str]) -> str:
    """``text`` with every spelling from ``secret_spellings`` replaced by ``***``."""

    for spelling in spellings:
        text = text.replace(spelling, "***")
    return text


def assert_test_host(host: str | None) -> None:
    """While tests run, only a host under the reserved ``.test`` domain may be reached: the test
    site is often a working site whose real clusters a test must never touch."""

    if frappe.in_test and not (host or "").rstrip(".").endswith(".test"):
        frappe.throw(_("Tests may only reach .test hosts, not {0}.").format(host))
