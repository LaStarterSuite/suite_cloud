"""Server alerts: a server whose metrics stopped, a node whose outbound scan shipper stopped, and a
server that logs too many errors.

Every five minutes the checker opens a Server Alert for each condition it finds, resolves the open
alerts whose condition cleared, and mails the configured recipients about both. Metrics and the
shipper report every minute even on an idle server, so their silence is a fault; events come only
when something happens, so they raise an alert only by their error count. A silence alert stays
open until the server reports again, so the alert of a server taken out of service is resolved by
hand.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import (
    cint,
    convert_utc_to_system_timezone,
    format_datetime,
    get_system_timezone,
    get_url_to_form,
    now_datetime,
)

from suite_cloud.mail.cluster import rspamd
from suite_cloud.suite_cloud.doctype.tenant_email.email_types import get_inline_images
from suite_cloud.telemetry import status
from suite_cloud.telemetry.warehouse import Warehouse
from suite_cloud.utils import get_config, log_exception

ALERT = "Server Alert"
METRICS_SILENT = "Metrics Silent"
SCAN_SILENT = "Outbound Scan Silent"
ERROR_RATE = "Error Rate"
SILENCES = (METRICS_SILENT, SCAN_SILENT)  # stay open until the server reports again
LOOKBACK = timedelta(hours=24)  # a server that reported this recently is expected to keep reporting
SCANNING_STATUSES = ("Active", "Draining")  # the nodes that report, as on the forms
DEFAULT_ERRORS_PER_HOUR = 100  # the settings' default, used until the site has it


def check_server_alerts() -> None:
    """Scheduled every five minutes."""

    Alerts().check()


@dataclass(frozen=True)
class Finding:
    alert_type: str
    host_ip: str
    hostname: str | None
    details: str
    seen_at: datetime | None = None  # a silence's last report, naive in the system timezone

    @property
    def key(self) -> tuple[str, str]:
        return (self.alert_type, self.host_ip)


class Alerts:
    """The checker: what the telemetry shows now, against the alerts that are open."""

    def __init__(self, warehouse: Warehouse | None = None):
        self.warehouse = warehouse or Warehouse()

    @property
    def silence(self) -> timedelta:
        return status.get_silence()

    @property
    def errors_per_hour(self) -> int:
        return cint(get_config("telemetry_errors_per_hour")) or DEFAULT_ERRORS_PER_HOUR

    @property
    def recipients(self) -> list[str]:
        text = (get_config("telemetry_alert_recipients") or "").replace("\n", ",")
        return [address.strip() for address in text.split(",") if address.strip()]

    def check(self) -> dict[str, int]:
        """Opens an alert per new finding, resolves the open alerts found no more; returns the counts."""

        open_alerts = {
            (alert.alert_type, alert.host_ip): alert
            for alert in frappe.get_all(ALERT, {"status": "Open"}, ["name", "alert_type", "host_ip"])
        }
        watched = {
            alert_type: [ip for kind, ip in open_alerts if kind == alert_type] for alert_type in SILENCES
        }
        findings = (
            self.find_silent_metrics(watched[METRICS_SILENT])
            + self.find_silent_scanners(watched[SCAN_SILENT])
            + self.find_error_rates()
        )

        opened = 0
        for finding in findings:
            if alert := open_alerts.pop(finding.key, None):
                frappe.db.set_value(ALERT, alert.name, "details", finding.details)
            elif not self.is_dismissed(finding):
                self.open(finding)
                opened += 1
        for alert in open_alerts.values():
            self.resolve(alert.name)
        return {"opened": opened, "resolved": len(open_alerts)}

    def is_dismissed(self, finding: Finding) -> bool:
        """A silence alert resolved by hand stays resolved until the server reports again. One the
        checker resolved does not count: the server was back, and a new silence is a new alert."""

        if finding.alert_type not in SILENCES or not finding.seen_at:
            return False
        return bool(
            frappe.db.exists(
                ALERT,
                {
                    "alert_type": finding.alert_type,
                    "host_ip": finding.host_ip,
                    "dismissed": 1,
                    "resolved_at": (">=", finding.seen_at),
                },
            )
        )

    def find_silent_metrics(self, watched: list[str]) -> list[Finding]:
        """Servers that pushed metrics within the lookback, or hold an open alert, but not within the
        silence. Stalwart pushes metrics every interval even on an idle server. A server with an
        open alert stays silent until a newer push proves it is back: past the lookback it keeps
        its alert, and once the retention has deleted its last rows it is still not recovered."""

        now = datetime.now(UTC).replace(tzinfo=None)
        rows = self.get_last_pushes("received_at >= $since", {"since": now - LOOKBACK})
        recent = {host_ip for host_ip, _hostname, _last in rows}
        if stale := [host_ip for host_ip in watched if host_ip not in recent]:
            rows += self.get_last_pushes("list_contains($hosts, host_ip)", {"hosts": stale})
        silent = now - self.silence
        findings = [
            Finding(
                METRICS_SILENT,
                host_ip,
                hostname,
                _("No metrics received since {0} UTC.").format(last.strftime("%Y-%m-%d %H:%M")),
                seen_at=convert_utc_to_system_timezone(last).replace(tzinfo=None),
            )
            for host_ip, hostname, last in rows
            if last < silent
        ]
        seen = {host_ip for host_ip, _hostname, _last in rows}
        findings += [
            Finding(METRICS_SILENT, host_ip, None, _("No metrics received within the retention."))
            for host_ip in watched
            if host_ip not in seen
        ]
        return findings

    def get_last_pushes(self, condition: str, parameters: dict) -> list[tuple]:
        """``(host_ip, hostname, received_at)`` of each host's last metrics push among the rows
        matching the condition. A condition on ``received_at`` alone lets DuckDB skip the older row
        groups; only a host silent past the lookback needs the whole history read."""

        return self.warehouse.query(
            'SELECT host_ip, arg_max(hostname, received_at), max(received_at) FROM "metrics" '
            f"WHERE {condition} GROUP BY host_ip",
            parameters,
        )

    def find_silent_scanners(self, watched: list[str]) -> list[Finding]:
        """Nodes with the current scanner whose shipper, which checks in every minute, did not
        within the silence. The check-ins live in the cache the forms read: a node the cache lost
        opens nothing, and keeps an open alert until it checks in again. While telemetry is off
        the API refuses the shipper, and while the scan is off the node scans nothing."""

        if not cint(get_config("enable_telemetry")) or get_config("outbound_scan_mode") == "Off":
            return []
        nodes = frappe.get_all(
            "Stalwart Node",
            {"status": ("in", SCANNING_STATUSES), "outbound_scanner_installed": 1},
            ["ipv4_address", "hostname", "cluster", "outbound_scanner_digest"],
        )
        digests = {
            cluster: rspamd.get_config_digest(frappe.get_cached_doc("Stalwart Cluster", cluster))
            for cluster in {node.cluster for node in nodes}
        }
        findings = []
        for node in nodes:
            if node.outbound_scanner_digest != digests[node.cluster]:
                continue  # an older shipper may not check in
            seen = status.get_seen_at(node.ipv4_address, status.SCAN_CHECK_IN)
            if seen and not status.is_recent(seen):
                findings.append(
                    Finding(
                        SCAN_SILENT,
                        node.ipv4_address,
                        node.hostname,
                        _("No check-in from the scan shipper since {0} UTC.").format(
                            to_utc(seen).strftime("%Y-%m-%d %H:%M")
                        ),
                        seen_at=seen,
                    )
                )
            elif not seen and node.ipv4_address in watched:
                findings.append(
                    Finding(
                        SCAN_SILENT,
                        node.ipv4_address,
                        node.hostname,
                        _("No check-in recorded since Suite Cloud's cache was cleared."),
                    )
                )
        return findings

    def find_error_rates(self) -> list[Finding]:
        """Servers that logged at least the threshold of error events in the last hour. Stalwart
        pushes only the events Suite Cloud lists, so a share of all events means nothing."""

        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        rows = self.warehouse.query(
            'SELECT host_ip, arg_max(hostname, "timestamp"), count(*) FROM "events" '
            "WHERE \"timestamp\" >= $since AND level = 'error' GROUP BY host_ip HAVING count(*) >= $limit",
            {"since": since, "limit": self.errors_per_hour},
        )
        return [
            Finding(ERROR_RATE, host_ip, hostname, _("{0} error events in the last hour.").format(errors))
            for host_ip, hostname, errors in rows
        ]

    def open(self, finding: Finding) -> None:
        alert = frappe.get_doc(
            {
                "doctype": ALERT,
                "alert_type": finding.alert_type,
                "host_ip": finding.host_ip,
                "hostname": finding.hostname,
                "status": "Open",
                "opened_at": now_datetime(),
                "details": finding.details,
            }
        ).insert(ignore_permissions=True)
        self.notify(alert)

    def resolve(self, name: str) -> None:
        alert = frappe.get_doc(ALERT, name)
        alert.status = "Resolved"
        alert.details = _("{0} The condition cleared.").format(alert.details)
        alert.flags.resolved_by_check = True
        alert.save(ignore_permissions=True)
        self.notify(alert)

    def notify(self, alert) -> None:
        """Mails the recipients; a mail that cannot go out is logged, the alert record stands."""

        if not self.recipients:
            return
        state = _("resolved") if alert.status == "Resolved" else _("opened")
        try:
            message = self.get_message(alert)
            frappe.sendmail(
                recipients=self.recipients,
                subject=f"[Suite Cloud] {alert.title} {state}",
                message=message,
                inline_images=get_inline_images(message),
                reference_doctype=ALERT,
                reference_name=alert.name,
            )
        except Exception:
            log_exception(f"Server alert mail not sent for {alert.name}", alert)

    def get_message(self, alert) -> str:
        """The HTML body, with times in the system timezone."""

        timezone = get_system_timezone()
        return frappe.render_template(
            "templates/operator_emails/server_alert.html",
            {
                "alert_type": alert.alert_type,
                "status": alert.status,
                "server": alert.hostname or alert.host_ip,
                "host_ip": alert.host_ip,
                "details": alert.details,
                "opened_at": f"{format_datetime(alert.opened_at)} ({timezone})",
                "resolved_at": alert.resolved_at and f"{format_datetime(alert.resolved_at)} ({timezone})",
                "link": get_url_to_form(ALERT, alert.name),
            },
        )


def to_utc(moment: datetime) -> datetime:
    """A naive system-timezone time as naive UTC, as the other alert details write their times."""

    return moment.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(UTC).replace(tzinfo=None)
