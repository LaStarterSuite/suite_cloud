"""Events and metrics Stalwart nodes and gateways push to Suite Cloud.

A batch arrives through the telemetry API, is staged as a parquet file by the ``Spool`` and is copied into Frappe
Insights' DuckDB by the ``Warehouse`` loader, where the shipped dashboards read it. The tables
here are the one definition of the columns: the parquet schema, the DuckDB DDL and the insert
column list all come from them.
"""

from dataclasses import dataclass

import pyarrow

TELEMETRY_USER = "stalwart@suite-cloud.internal"  # a server's telemetry token signs this user in


# The events the Stalwart workbook and the alerts read, at the level Stalwart logs each. The events
# webhook sends only these.
EVENT_LEVELS = {
    **dict.fromkeys(
        (
            "auth.success",
            "dane.authentication-failure",
            "dane.tlsa-record-fetch-error",
            "delivery.connect",
            "delivery.connect-error",
            "delivery.delivered",
            "delivery.double-bounce",
            "delivery.dsn-perm-fail",
            "delivery.dsn-temp-fail",
            "delivery.ehlo-rejected",
            "delivery.failed",
            "delivery.greeting-failed",
            "delivery.ip-lookup-failed",
            "delivery.mail-from-rejected",
            "delivery.message-rejected",
            "delivery.mx-lookup-failed",
            "delivery.null-mx",
            "delivery.rcpt-to-rejected",
            "delivery.start-tls",
            "delivery.start-tls-error",
            "delivery.start-tls-unavailable",
            "message-ingest.ham",
            "message-ingest.spam",
            "mta-sts.invalid-policy",
            "mta-sts.not-authorized",
            "mta-sts.policy-fetch-error",
            "queue.rescheduled",
            "security.abuse-ban",
            "security.authentication-ban",
            "security.ip-blocked",
            "security.loiter-ban",
            "security.scan-ban",
            "server.startup",
            "smtp.auth-mechanism-not-supported",
            "smtp.auth-not-allowed",
            "smtp.dkim-fail",
            "smtp.dkim-pass",
            "smtp.dmarc-fail",
            "smtp.dmarc-pass",
            "smtp.ehlo",
            "smtp.invalid-ehlo",
            "smtp.iprev-fail",
            "smtp.iprev-pass",
            "smtp.mail-from",
            "smtp.mailbox-does-not-exist",
            "smtp.rate-limit-exceeded",
            "smtp.relay-not-allowed",
            "smtp.spf-ehlo-fail",
            "smtp.spf-ehlo-pass",
            "smtp.spf-from-fail",
            "smtp.spf-from-pass",
            "smtp.too-many-messages",
            "smtp.too-many-recipients",
            "tls-rpt.record-fetch-error",
        ),
        "info",
    ),
    # Debug events that carry the DNS times the Community edition leaves out of OTLP.
    **dict.fromkeys(("delivery.ip-lookup", "delivery.mx-lookup"), "debug"),
    **dict.fromkeys(("auth.too-many-attempts", "smtp.id-not-found"), "warn"),
    **dict.fromkeys(("auth.error", "server.startup-error"), "error"),
}
WEBHOOK_EVENTS = tuple(EVENT_LEVELS)

# Every other event Stalwart 0.16 logs at error level (``EventType::level()`` in its ``trc`` crate).
# A WebHook ignores its level and sends whatever its list allows, so the errors webhook names them.
ERROR_EVENTS = (
    "acme.error",
    "cluster.message-invalid",
    "cluster.publisher-error",
    "cluster.subscriber-error",
    "dkim.build-error",
    "dns.build-error",
    "message-ingest.error",
    "network.bind-error",
    "network.listen-error",
    "network.set-opt-error",
    "network.split-error",
    "registry.build-error",
    "registry.deserialization-error",
    "registry.local-parse-error",
    "registry.local-read-error",
    "registry.local-write-error",
    "registry.read-error",
    "registry.validation-error",
    "registry.write-error",
    "resource.bad-parameters",
    "resource.error",
    "server.thread-error",
    "sieve.unexpected-error",
    "store.assert-value-failed",
    "store.azure-error",
    "store.crypto-error",
    "store.data-corruption",
    "store.decompress-error",
    "store.deserialize-error",
    "store.elasticsearch-error",
    "store.filesystem-error",
    "store.foundationdb-error",
    "store.ldap-error",
    "store.meilisearch-error",
    "store.mysql-error",
    "store.not-configured",
    "store.not-supported",
    "store.pool-error",
    "store.postgresql-error",
    "store.redis-error",
    "store.rocksdb-error",
    "store.s3-error",
    "store.sqlite-error",
    "store.unexpected-error",
    "tls.not-configured",
)

# Every other event Stalwart 0.16 logs at warn level, for the warnings webhook. Stalwart never hands
# ``telemetry.webhook-error`` to a WebHook, so it is left out.
WARN_EVENTS = (
    "acme.auth-error",
    "acme.auth-too-many-attempts",
    "acme.order-invalid",
    "acme.tls-alpn-error",
    "acme.token-not-found",
    "ai.api-error",
    "arc.sealer-not-found",
    "calendar.alarm-failed",
    "cluster.subscriber-disconnected",
    "delivery.concurrency-limit-exceeded",
    "delivery.missing-outbound-hostname",
    "delivery.rate-limit-exceeded",
    "dkim.signer-not-found",
    "dns.record-creation-failed",
    "dns.record-propagation-timeout",
    "eval.directory-not-found",
    "http.x-forwarded-missing",
    "incoming-report.dmarc-report-with-warnings",
    "incoming-report.tls-report-with-warnings",
    "limit.concurrent-connection",
    "limit.too-many-requests",
    "milter.disconnected",
    "milter.frame-invalid",
    "milter.frame-too-large",
    "milter.io-error",
    "milter.parse-error",
    "milter.timeout",
    "milter.tls-invalid-name",
    "milter.unexpected-response",
    "mta-hook.error",
    "network.proxy-error",
    "queue.back-pressure",
    "registry.build-warning",
    "server.bootstrap-mode",
    "server.recovery-mode",
    "sieve.list-not-found",
    "sieve.message-too-large",
    "sieve.not-supported",
    "sieve.quota-exceeded",
    "sieve.script-not-found",
    "smtp.missing-local-hostname",
    "spam.train-sample-not-found",
    "store.blob-missing-marker",
    "store.cache-entry-too-large",
    "store.http-store-error",
    "task-manager.task-failed",
    "telemetry.alert-event",
    "telemetry.journal-error",
    "telemetry.log-error",
    "telemetry.otel-exporter-error",
    "telemetry.otel-metrics-exporter-error",
    "telemetry.prometheus-exporter-error",
    "tls.multiple-certificates-available",
    "tls.no-certificates-available",
)

# The metrics the Stalwart workbook reads, as Stalwart 0.16 names them (``MetricType`` in its ``trc``
# crate). Only metrics the Community edition pushes: it leaves out ``queue.count`` and the ingest,
# indexing, DNS and store timings (``collect_gauges`` and ``collect_histograms``).
METRIC_NAMES = (
    "delivery.active-connections",
    "delivery.attempt-start",
    "delivery.attempt-time",
    "delivery.total-time",
    "domain.count",
    "http.active-connections",
    "http.connection-start",
    "imap.active-connections",
    "imap.connection-start",
    "manage-sieve.connection-start",
    "message.size",
    "pop3.active-connections",
    "pop3.connection-start",
    "queue.authenticated-message-queued",
    "queue.dsn-queued",
    "queue.message-queued",
    "queue.report-queued",
    "server.memory",
    "sieve.active-connections",
    "smtp.active-connections",
    "smtp.connection-start",
    "user.count",
)

DUCKDB_TYPES = {
    pyarrow.string(): "VARCHAR",
    pyarrow.timestamp("us"): "TIMESTAMP",
    pyarrow.float64(): "DOUBLE",
    pyarrow.int64(): "BIGINT",
}

# What every row of one batch shares. Timestamps are naive UTC by contract.
ENVELOPE_FIELDS = (
    pyarrow.field("batch_id", pyarrow.string(), nullable=False),
    pyarrow.field("received_at", pyarrow.timestamp("us"), nullable=False),
    pyarrow.field("source", pyarrow.string(), nullable=False),
    pyarrow.field("host_ip", pyarrow.string(), nullable=False),
    pyarrow.field("hostname", pyarrow.string()),
)


@dataclass(frozen=True)
class DerivedColumn:
    """A column the loader computes from a row's own columns, so a dashboard reads it instead of
    parsing the row on every query."""

    name: str
    type: str
    expression: str


@dataclass(frozen=True)
class TelemetryTable:
    """One kind of telemetry: its DuckDB table, its spool folder and its columns.

    The schema is what a staged batch carries; the derived and delta columns exist only in DuckDB.
    """

    name: str
    schema: pyarrow.Schema
    derived: tuple[DerivedColumn, ...] = ()
    deltas: tuple[DerivedColumn, ...] = ()

    @property
    def columns(self) -> list[str]:
        return self.schema.names

    @property
    def create_statement(self) -> str:
        columns = [
            f'"{field.name}" {DUCKDB_TYPES[field.type]}{"" if field.nullable else " NOT NULL"}'
            for field in self.schema
        ]
        columns += [f'"{column.name}" {column.type}' for column in self.derived + self.deltas]
        return f'CREATE TABLE IF NOT EXISTS "{self.name}" ({", ".join(columns)})'

    def get_batch(self, envelope: dict, rows: list[dict]) -> pyarrow.Table:
        """The rows of one request as a parquet-ready table, each carrying the envelope."""

        return pyarrow.Table.from_pylist([{**envelope, **row} for row in rows], schema=self.schema)


def data_text(*keys: str) -> str:
    """The first of the event data's keys that is set, as text; ``data`` is always valid JSON."""

    values = [f"(data ->> '$.{key}')" for key in keys]
    return values[0] if len(values) == 1 else f"coalesce({', '.join(values)})"


EMAIL_DOMAIN = "'@([A-Za-z0-9.-]+)'"
SMTP_CODE = "'(^|[^0-9])([245][0-9][0-9])([^0-9]|$)'"

# The event data fields the shipped dashboards read, under Stalwart's key names.
EVENT_DATA_COLUMNS = (
    DerivedColumn("remote_ip", "VARCHAR", data_text("remoteIp")),
    DerivedColumn("listener", "VARCHAR", data_text("listenerId")),
    DerivedColumn("domain", "VARCHAR", data_text("domain")),
    DerivedColumn("mx", "VARCHAR", data_text("hostname")),
    DerivedColumn("reason", "VARCHAR", data_text("details", "reason")),
    DerivedColumn(
        "code",
        "VARCHAR",
        f"coalesce({data_text('code')}, regexp_extract({data_text('details', 'reason')}, {SMTP_CODE}, 2))",
    ),
    DerivedColumn("account", "VARCHAR", data_text("accountName", "accountId")),
    DerivedColumn("sender", "VARCHAR", data_text("from")),
    DerivedColumn("recipient", "VARCHAR", data_text("to")),
    DerivedColumn("sender_domain", "VARCHAR", f"regexp_extract({data_text('from')}, {EMAIL_DOMAIN}, 1)"),
    DerivedColumn("recipient_domain", "VARCHAR", f"regexp_extract({data_text('to')}, {EMAIL_DOMAIN}, 1)"),
    DerivedColumn("elapsed", "DOUBLE", f"try_cast({data_text('elapsed')} AS DOUBLE)"),
)

EVENTS = TelemetryTable(
    "events",
    pyarrow.schema(
        [
            *ENVELOPE_FIELDS,
            pyarrow.field("timestamp", pyarrow.timestamp("us"), nullable=False),
            pyarrow.field("level", pyarrow.string(), nullable=False),
            pyarrow.field("event", pyarrow.string(), nullable=False),
            pyarrow.field("span_id", pyarrow.string()),
            pyarrow.field("data", pyarrow.string()),
        ]
    ),
    EVENT_DATA_COLUMNS,
)


def get_change(total: str, previous: str, reset: str) -> str:
    """A running total's change since the previous point of its series.

    The loader reads the previous point beside each row as ``previous_value``, ``previous_count``
    and ``previous_sum``, and the host's previous push as ``previous_push``. Stalwart pushes a
    counter or histogram once it is above zero and then on every push, so a series the host's
    previous push lacked was zero then, and its first total is all new. On a host's first push
    there is nothing to subtract. A total that fell means Stalwart restarted, and the point's own
    total is the change.
    """

    return (
        f"CASE WHEN previous_value IS NULL AND previous_push IS NULL THEN NULL "
        f"WHEN previous_value IS NULL OR {reset} THEN {total} ELSE {total} - {previous} END"
    )


HISTOGRAM_RESET = '("count" < previous_count OR "sum" < previous_sum)'
# Charts sum these instead of the running totals; a gauge gets NULL.
METRIC_DELTA_COLUMNS = (
    DerivedColumn(
        "increase",
        "DOUBLE",
        f"CASE type WHEN 'counter' THEN {get_change('value', 'previous_value', 'value < previous_value')} "
        f"WHEN 'histogram' THEN {get_change('"sum"', 'previous_sum', HISTOGRAM_RESET)} END",
    ),
    DerivedColumn(
        "samples_added",
        "BIGINT",
        f"CASE type WHEN 'histogram' THEN {get_change('"count"', 'previous_count', HISTOGRAM_RESET)} END",
    ),
)

METRICS = TelemetryTable(
    "metrics",
    pyarrow.schema(
        [
            *ENVELOPE_FIELDS,
            pyarrow.field("timestamp", pyarrow.timestamp("us"), nullable=False),
            pyarrow.field("name", pyarrow.string(), nullable=False),
            pyarrow.field("type", pyarrow.string(), nullable=False),
            pyarrow.field("value", pyarrow.float64(), nullable=False),
            pyarrow.field("count", pyarrow.int64()),
            pyarrow.field("sum", pyarrow.float64()),
            pyarrow.field("labels", pyarrow.string()),
        ]
    ),
    deltas=METRIC_DELTA_COLUMNS,
)

TABLES = (EVENTS, METRICS)

# The newest reading of every gauge of every node that pushed in the last few minutes, rebuilt after
# each load, so a card sums the nodes' current values instead of every reading in its period.
CURRENT_METRICS = TelemetryTable(
    "current_metrics",
    pyarrow.schema(
        [
            pyarrow.field("host_ip", pyarrow.string(), nullable=False),
            pyarrow.field("hostname", pyarrow.string()),
            pyarrow.field("name", pyarrow.string(), nullable=False),
            pyarrow.field("labels", pyarrow.string()),
            pyarrow.field("value", pyarrow.float64(), nullable=False),
            pyarrow.field("timestamp", pyarrow.timestamp("us"), nullable=False),
        ]
    ),
)

# Copies of Suite Cloud's own records beside the telemetry, so a dashboard joins on the cluster
# or the tenant inside one DuckDB instead of across data sources.
NODES = TelemetryTable(
    "nodes",
    pyarrow.schema(
        [
            pyarrow.field("host_ip", pyarrow.string(), nullable=False),
            pyarrow.field("hostname", pyarrow.string(), nullable=False),
            pyarrow.field("cluster", pyarrow.string(), nullable=False),
            pyarrow.field("role", pyarrow.string()),
            pyarrow.field("location", pyarrow.string()),
            pyarrow.field("status", pyarrow.string()),
        ]
    ),
)

MAIL_DOMAINS = TelemetryTable(
    "mail_domains",
    pyarrow.schema(
        [
            pyarrow.field("domain", pyarrow.string(), nullable=False),
            pyarrow.field("tenant", pyarrow.string(), nullable=False),
            pyarrow.field("tenant_title", pyarrow.string()),
        ]
    ),
)

DIMENSIONS = (NODES, MAIL_DOMAINS)
