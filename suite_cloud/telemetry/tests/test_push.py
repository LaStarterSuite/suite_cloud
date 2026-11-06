"""The telemetry API: a server's telemetry token signs Stalwart in, and WebHook events and OTLP
metrics become rows of the events and metrics tables."""

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import frappe
import pyarrow.parquet as parquet
from frappe.tests import IntegrationTestCase
from frappe.utils import get_test_client
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    Gauge,
    Histogram,
    HistogramDataPoint,
    Metric,
    NumberDataPoint,
    Sum,
    Summary,
    SummaryDataPoint,
)

from suite_cloud.api.telemetry import authenticate, otlp, webhook
from suite_cloud.api.tenant import InvalidInputError
from suite_cloud.mail.cluster import server_token
from suite_cloud.mail.tests.fixtures import make_cluster, make_node
from suite_cloud.telemetry import (
    ERROR_EVENTS,
    EVENT_LEVELS,
    EVENTS,
    METRIC_NAMES,
    METRICS,
    TELEMETRY_USER,
    WARN_EVENTS,
    WEBHOOK_EVENTS,
)
from suite_cloud.telemetry.spool import Spool
from suite_cloud.telemetry.status import EVENTS as EVENTS_STREAM
from suite_cloud.telemetry.status import forget_contacts, get_seen_at
from suite_cloud.tests.fixtures import configure_settings, production_logging

NODE_IPS = ("203.0.113.5", "198.51.100.77")
WEBHOOK = "/api/v2/method/suite_cloud.api.telemetry.webhook"
OTLP = "/api/v2/method/suite_cloud.api.telemetry.otlp"
NANOS = 1_792_490_340_000_000_000  # 2026-10-20 09:59:00 UTC


def webhook_event(event_id: str, name: str = "delivery.delivered", **data) -> dict:
    return {
        "id": event_id,
        "createdAt": "2026-10-20T09:59:00Z",
        "type": name,
        "data": {"remoteIp": "198.51.100.7", "spanId": 42, **data},
    }


def attribute(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def metrics_request() -> bytes:
    request = ExportMetricsServiceRequest()
    scope = request.resource_metrics.add().scope_metrics.add()
    scope.metrics.extend(
        [
            Metric(
                name="delivery.attempts",
                sum=Sum(data_points=[NumberDataPoint(time_unix_nano=NANOS, as_int=12)], is_monotonic=True),
            ),
            Metric(
                name="server.memory",
                gauge=Gauge(
                    data_points=[
                        NumberDataPoint(
                            time_unix_nano=NANOS, as_double=1024.5, attributes=[attribute("kind", "rss")]
                        )
                    ]
                ),
            ),
            Metric(
                name="http.request-time",
                histogram=Histogram(
                    data_points=[HistogramDataPoint(time_unix_nano=NANOS, count=4, sum=10.0)]
                ),
            ),
            Metric(
                name="unknown.summary",
                summary=Summary(data_points=[SummaryDataPoint(time_unix_nano=NANOS, count=1, sum=1.0)]),
            ),
        ]
    )
    return request.SerializeToString()


class TelemetryApiTestCase(IntegrationTestCase):
    def setUp(self) -> None:
        configure_settings(enable_telemetry=1)
        self.cluster = make_cluster()
        for ip in NODE_IPS:
            make_node(self.cluster, ipv4=ip)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = Spool(Path(self.directory.name))
        patcher = patch("suite_cloud.api.telemetry.Spool", return_value=self.spool)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        frappe.local.request = None
        frappe.set_user("Administrator")

    def request(self, secret: str | None, path: str = WEBHOOK, scheme: str = "Bearer") -> None:
        """A request as the auth hook sees it: signed in only when the bearer is a valid token."""

        headers = {}
        if secret is not None:
            headers["Authorization"] = f"{scheme} {secret}"
        frappe.set_user("Guest")
        frappe.utils.set_request(method="POST", path=path, headers=headers)
        frappe.local.form_dict = frappe._dict()
        authenticate()

    def staged(self, table=EVENTS) -> list[Path]:
        return self.spool.get_pending(table, 10)


class PushTestCase(TelemetryApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.node = frappe.get_doc(
            "Stalwart Node", {"ipv4_address": NODE_IPS[0], "cluster": self.cluster.name}
        )
        self.token = server_token.get_token(self.node)
        self.request(self.token, f"{WEBHOOK}?stream=events")

    def tearDown(self) -> None:
        frappe.local.stalwart_server = None
        super().tearDown()

    def request(self, secret: str | None, path: str = WEBHOOK, scheme: str = "Bearer") -> None:
        frappe.local.stalwart_server = None
        super().request(secret, path, scheme)


class TestServerToken(PushTestCase):
    def test_each_server_signs_in_with_its_own_token(self) -> None:
        self.assertEqual(frappe.session.user, TELEMETRY_USER)
        server = frappe.local.stalwart_server
        self.assertEqual(
            (server.doctype, server.name, server.ipv4_address), ("Stalwart Node", self.node.name, NODE_IPS[0])
        )
        self.assertRegex(self.token, r"^[0-9a-f]{20}\.[0-9a-f]{40}$")

        frappe.set_user("Administrator")
        gateway = frappe.get_doc(
            {"doctype": "Egress Gateway", "cluster": self.cluster.name, "ipv4_address": "203.0.113.50"}
        ).insert()
        self.request(server_token.get_token(gateway), OTLP)
        self.assertEqual(frappe.local.stalwart_server.doctype, "Egress Gateway")

    def test_a_wrong_or_misplaced_token_is_refused(self) -> None:
        key, secret = self.node.suite_cloud_key, self.node.get_password("suite_cloud_secret")
        cases = (
            (f"{key}.wrong", WEBHOOK),
            (f"unknown.{secret}", WEBHOOK),
            (key, OTLP),
            (f"{key}:{secret}", WEBHOOK),  # the old telemetry token's form
        )
        for secret, path in cases:
            with self.subTest(path=path), self.assertRaisesRegex(frappe.AuthenticationError, "failed"):
                self.request(secret, path)

    def test_telemetry_off_refuses_the_servers(self) -> None:
        frappe.set_user("Administrator")
        configure_settings(enable_telemetry=0)
        with self.assertRaisesRegex(frappe.AuthenticationError, "failed"):
            self.request(self.token, WEBHOOK)

    def test_only_a_signed_in_server_may_push(self) -> None:
        frappe.local.stalwart_server = None
        self.assertRaisesRegex(frappe.PermissionError, "Stalwart server", webhook, events=[])
        frappe.set_user("Administrator")
        self.assertRaisesRegex(frappe.PermissionError, "Stalwart server", otlp)

    def test_other_requests_pass_through_untouched(self) -> None:
        for secret, path, scheme in (
            (None, WEBHOOK, "Bearer"),
            (self.token, WEBHOOK, "Tenant"),
            (self.token, "/api/method/frappe.ping", "Bearer"),  # Frappe's own OAuth bearer tokens
        ):
            with self.subTest(f"{scheme} on {path}"):
                self.request(secret, path, scheme)
                self.assertEqual(frappe.session.user, "Guest")


class TestWebhook(PushTestCase):
    def push(self, stream: str, batch: list) -> dict:
        self.request(self.token, f"{WEBHOOK}?stream={stream}")
        return webhook(events=batch)

    def rows(self) -> list[dict]:
        return [row for path in self.staged() for row in parquet.read_table(path).to_pylist()]

    def test_events_become_rows_at_the_level_stalwart_logs_them(self) -> None:
        response = self.push(
            "events", [webhook_event("1"), webhook_event("2", "auth.error", accountName="jane@acme.test")]
        )
        self.push("errors", [webhook_event("3", "store.data-corruption")])
        warnings = [webhook_event("7", "queue.back-pressure"), webhook_event("8", "store.data-corruption")]
        self.assertEqual(self.push("warnings", warnings)["accepted"], 1)
        # What Stalwart logged before its WebHooks existed reaches each of them after a start.
        startup = [webhook_event("4", "network.listen-start"), webhook_event("5", "delivery.delivered")]
        self.assertEqual(self.push("errors", startup)["accepted"], 0)
        self.assertEqual(self.push("events", [webhook_event("6", "network.listen-start")])["accepted"], 0)

        self.assertEqual(response, {"accepted": 2, "skipped": 0})
        # The server's last contact, for the status on its form.
        self.assertIsNotNone(get_seen_at(self.node.ipv4_address, EVENTS_STREAM))
        forget_contacts()
        self.assertIsNone(get_seen_at(self.node.ipv4_address, EVENTS_STREAM))
        rows = sorted(self.rows(), key=lambda row: row["event"])
        self.assertEqual(
            [(row["event"], row["level"]) for row in rows],
            [
                ("auth.error", "error"),
                ("delivery.delivered", "info"),
                ("queue.back-pressure", "warn"),
                ("store.data-corruption", "error"),
            ],
        )
        delivered = rows[1]
        self.assertEqual(delivered["timestamp"], datetime(2026, 10, 20, 9, 59))
        self.assertEqual(
            (delivered["span_id"], json.loads(delivered["data"])), ("42", {"remoteIp": "198.51.100.7"})
        )
        self.assertEqual(
            (delivered["source"], delivered["host_ip"], delivered["hostname"]),
            ("stalwart", self.node.ipv4_address, self.node.hostname),
        )

    def test_a_bad_event_is_skipped_and_logged_once(self) -> None:
        bad = [
            "not an object",
            {**webhook_event("2"), "createdAt": "yesterday"},
            {**webhook_event("3"), "createdAt": "2026-10-20T09:59:00"},
            {**webhook_event("4"), "type": None},
            {**webhook_event("5"), "data": "x"},
        ]
        with patch("suite_cloud.api.telemetry.log_error") as log_error:
            response = self.push("events", [webhook_event("1"), *bad])
        self.assertEqual(response, {"accepted": 1, "skipped": 5})
        log_error.assert_called_once()
        self.assertIn("5 events skipped", log_error.call_args.args[0])
        self.assertEqual(len(self.rows()), 1)

    def test_the_outbound_scan_events_arrive_on_the_events_stream(self) -> None:
        names = (
            "milter.action-accept",
            "milter.action-reply-code",
            "milter.action-reject",
            "milter.action-discard",
            "milter.action-temp-fail",
        )
        batch = [webhook_event(str(index), name, elapsed=12) for index, name in enumerate(names)]
        self.assertEqual(self.push("events", batch), {"accepted": len(names), "skipped": 0})
        self.assertEqual({(row["event"], row["level"]) for row in self.rows()}, {(n, "info") for n in names})

    def test_a_resent_batch_keeps_its_id(self) -> None:
        batch = [webhook_event("1"), webhook_event("2")]
        self.push("events", batch)
        self.push("events", batch)
        self.assertEqual(len(self.staged()), 1)
        self.push("events", [*batch, webhook_event("3")])
        self.assertEqual(len(self.staged()), 2)

    def test_the_stream_must_be_named(self) -> None:
        for stream in ("", "logs"):
            with self.subTest(stream), self.assertRaisesRegex(InvalidInputError, "stream must be one of"):
                self.push(stream, [webhook_event("1")])
        self.assertEqual(self.push("events", []), {"accepted": 0, "skipped": 0})
        self.assertEqual(self.staged(), [])


class TestOtlp(PushTestCase):
    def test_sums_gauges_and_histograms_become_metric_rows(self) -> None:
        self.request(self.token, OTLP)
        with (
            patch("frappe.request.get_data", return_value=metrics_request()),
            patch("suite_cloud.api.telemetry.log_error") as log_error,
        ):
            response = otlp()

        self.assertEqual((response.status_code, response.get_data()), (200, b""))
        self.assertIn("1 data points skipped", log_error.call_args.args[0])  # the summary
        rows = parquet.read_table(self.staged(METRICS)[0]).to_pylist()
        found = {row["name"]: row for row in rows}
        self.assertEqual(set(found), {"delivery.attempts", "server.memory", "http.request-time"})
        self.assertEqual(
            (found["delivery.attempts"]["type"], found["delivery.attempts"]["value"]), ("counter", 12.0)
        )
        memory = found["server.memory"]
        self.assertEqual(
            (memory["type"], memory["value"], memory["labels"]), ("gauge", 1024.5, '{"kind":"rss"}')
        )
        histogram = found["http.request-time"]
        self.assertEqual(
            (histogram["type"], histogram["value"], histogram["count"], histogram["sum"]),
            ("histogram", 2.5, 4, 10.0),
        )
        self.assertEqual(memory["timestamp"], datetime(2026, 10, 20, 9, 59))
        self.assertEqual(memory["host_ip"], self.node.ipv4_address)

    def test_a_body_that_is_not_otlp_is_a_400(self) -> None:
        self.request(self.token, OTLP)
        with patch("frappe.request.get_data", return_value=b"\xff\xff\xff"):
            self.assertRaises(InvalidInputError, otlp)
        self.assertEqual(self.staged(METRICS), [])

    def test_a_resent_push_keeps_its_id(self) -> None:
        self.request(self.token, OTLP)
        with (
            patch("frappe.request.get_data", return_value=metrics_request()),
            patch("suite_cloud.api.telemetry.log_error"),
        ):
            otlp()
            otlp()
        self.assertEqual(len(self.staged(METRICS)), 1)

    def test_stalwart_gets_an_empty_answer_over_http(self) -> None:
        db = frappe.local.db
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Frappe-Site-Name": frappe.local.site,
            "Content-Type": "application/x-protobuf",
        }
        with (
            patch("frappe.database.get_db", return_value=db),
            patch.object(db, "commit"),
            patch.object(db, "rollback"),
            patch("frappe.destroy"),
            production_logging(),
            patch("suite_cloud.api.telemetry.log_error"),
        ):
            response = get_test_client().open(
                OTLP, method="POST", headers=headers, base_url="http://localhost/", data=metrics_request()
            )
        self.request(self.token, OTLP)
        self.assertEqual((response.status_code, response.get_data()), (200, b""))
        self.assertEqual(len(self.staged(METRICS)), 1)


class TestEventList(PushTestCase):
    def test_every_event_the_workbook_reads_is_sent_with_its_level(self) -> None:
        self.assertEqual(set(WEBHOOK_EVENTS), set(EVENT_LEVELS))
        self.assertTrue(set(EVENT_LEVELS.values()) <= {"debug", "info", "warn", "error"})
        self.assertIn("server.startup-error", WEBHOOK_EVENTS)
        # Each event is on one WebHook only, so none is sent twice.
        lists = (set(WEBHOOK_EVENTS), set(ERROR_EVENTS), set(WARN_EVENTS))
        self.assertEqual(sum(map(len, lists)), len(set.union(*lists)))

        workbook = Path(__file__).resolve().parents[1] / "insights_workbook" / "stalwart" / "stalwart.json"
        text = workbook.read_text()
        metrics = set(METRIC_NAMES)
        for name in (
            set(
                re.findall(
                    r"(?<![\w.@/-])((?:auth|dane|delivery|message-ingest|mta-sts|queue|security|server|smtp|tls-rpt)\.[a-z0-9-]+)",
                    text,
                )
            )
            - metrics
        ):
            if name.endswith("-"):
                continue  # a prefix the ratios match on: its -pass and -fail events are listed
            with self.subTest(name):
                self.assertIn(name, WEBHOOK_EVENTS)
        self.assertFalse(metrics & set(WEBHOOK_EVENTS))
