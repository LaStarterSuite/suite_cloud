"""The outbound scan records: the node's shipper sends what its Rspamd wrote, and the telemetry API
stages them as rows of the outbound_scans and outbound_scan_symbols tables."""

import importlib.util
import json
import tempfile
import urllib.error
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as parquet

from suite_cloud.api.telemetry import outbound_scans
from suite_cloud.api.tenant import InvalidInputError
from suite_cloud.mail.cluster import rspamd
from suite_cloud.telemetry import OUTBOUND_SCAN_SYMBOLS, OUTBOUND_SCANS
from suite_cloud.telemetry.tests.test_push import PushTestCase
from suite_cloud.tests.fixtures import configure_settings

SCANS = "/api/v2/method/suite_cloud.api.telemetry.outbound_scans"
SHIPPER = rspamd.SOURCE_DIRECTORY / "ship_outbound_scans.py"


def scan_record(message_id: str = "<1@acme.test>", score: float = 3.5, **values) -> dict:
    """A record as scan_recorder.lua writes it."""

    return {
        "time": 1792490340.25,  # 2026-10-20 09:59:00.25 UTC
        "user": "Jane@Acme.test",
        "envelope_from": "jane@acme.test",
        "header_from": "jane@acme.test",
        "message_id": message_id,
        "queue_id": "Q1",
        "client_ip": "198.51.100.7",
        "recipients": 2,
        "size": 2048,
        "action": "no action",
        "score": score,
        "scan_time": 87,
        "symbols": {"MIME_GOOD": -0.1, "R_SUSPICIOUS_URL": 3.6},
        **values,
    }


def load_shipper():
    spec = importlib.util.spec_from_file_location("ship_outbound_scans", SHIPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OutboundScanTestCase(PushTestCase):
    def setUp(self) -> None:
        configure_settings(outbound_scan_mode="Observe", outbound_scan_reject_score=15)
        super().setUp()  # signs the node in

    def push(self, scans: list) -> dict:
        self.request(self.token, SCANS)
        return outbound_scans(scans=scans)

    def rows(self, table=OUTBOUND_SCANS) -> list[dict]:
        return [row for path in self.staged(table) for row in parquet.read_table(path).to_pylist()]


class TestOutboundScanApi(OutboundScanTestCase):
    def test_a_record_becomes_a_scan_and_its_rules(self) -> None:
        self.assertEqual(self.push([scan_record()]), {"accepted": 1, "skipped": 0})

        [scan] = self.rows()
        self.assertEqual(scan["timestamp"], datetime(2026, 10, 20, 9, 59, 0, 250000))
        self.assertEqual(
            {key: scan[key] for key in ("account", "message_id", "action", "score", "scan_time")},
            {
                "account": "jane@acme.test",
                "message_id": "<1@acme.test>",
                "action": "no action",
                "score": 3.5,
                "scan_time": 87.0,
            },
        )
        # The settings when the batch arrived, so Observe shows what Enforce would refuse.
        self.assertEqual((scan["mode"], scan["reject_score"]), ("Observe", 15.0))
        self.assertEqual((scan["host_ip"], scan["hostname"]), (self.node.ipv4_address, self.node.hostname))
        self.assertEqual(json.loads(scan["symbols"]), {"MIME_GOOD": -0.1, "R_SUSPICIOUS_URL": 3.6})
        rules = sorted(self.rows(OUTBOUND_SCAN_SYMBOLS), key=lambda row: row["symbol"])
        self.assertEqual(
            [(row["symbol"], row["score"], row["message_score"], row["message_id"]) for row in rules],
            [("MIME_GOOD", -0.1, 3.5, "<1@acme.test>"), ("R_SUSPICIOUS_URL", 3.6, 3.5, "<1@acme.test>")],
        )

    def test_a_bad_record_is_skipped_and_logged_once(self) -> None:
        bad = [
            "not an object",
            scan_record(user=None),
            scan_record(score="high"),
            scan_record(time=None),
            scan_record(symbols="x"),
        ]
        with patch("suite_cloud.api.telemetry.log_error") as log_error:
            response = self.push([scan_record(symbols=[], recipients=None), *bad])
        self.assertEqual(response, {"accepted": 1, "skipped": 5})
        log_error.assert_called_once()
        self.assertIn("5 records skipped", log_error.call_args.args[0])
        [scan] = self.rows()
        self.assertEqual((scan["symbols"], scan["recipients"]), ("{}", None))
        self.assertEqual(self.rows(OUTBOUND_SCAN_SYMBOLS), [])

    def test_a_resent_batch_keeps_its_id(self) -> None:
        batch = [scan_record("<1@acme.test>"), scan_record("<2@acme.test>")]
        self.push(batch)
        self.push(batch)
        self.assertEqual(len(self.staged(OUTBOUND_SCANS)), 1)
        self.push([*batch, scan_record("<3@acme.test>")])
        self.assertEqual(len(self.staged(OUTBOUND_SCANS)), 2)

    def test_a_request_holds_at_most_the_limit(self) -> None:
        with self.assertRaisesRegex(InvalidInputError, "at most 5000 scans"):
            self.push([scan_record()] * 5001)


class TestShipper(OutboundScanTestCase):
    """The script runs on the node with the standard library only; here it posts to the API."""

    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.shipper = load_shipper()
        self.shipper.SPOOL = Path(directory.name) / "suite-cloud-scans.jsonl"
        self.shipper.PENDING = Path(directory.name) / "suite-cloud-scans.pending"
        self.shipper.WRITER_GRACE_SECONDS = 0
        self.environment = self.enterContext(
            patch.dict(
                "os.environ",
                {"SUITE_CLOUD_SCANS_URL": "https://suite.test/scans", "SUITE_CLOUD_TOKEN": self.token},
            )
        )
        self.sent = []

    def write_spool(self, *records: dict | str) -> None:
        with self.shipper.SPOOL.open("a") as spool:
            for record in records:
                spool.write((record if isinstance(record, str) else json.dumps(record)) + "\n")

    def deliver(self, request, timeout):
        """Hands the request body to the API, as Suite Cloud would."""

        self.assertEqual(request.get_header("Authorization"), f"Bearer {self.token}")
        self.sent.append(request.data)
        self.push(json.loads(request.data)["scans"])
        return nullcontext()

    def test_the_spool_reaches_suite_cloud_in_batches(self) -> None:
        self.shipper.BATCH_SIZE = 2
        self.write_spool(scan_record("<1@acme.test>"), '{"cut short', scan_record("<2@acme.test>"))
        self.write_spool(scan_record("<3@acme.test>"))
        with patch("urllib.request.urlopen", side_effect=self.deliver):
            self.assertEqual(self.shipper.main(), 0)

        self.assertEqual(len(self.sent), 2)
        self.assertEqual(
            sorted(row["message_id"] for row in self.rows()),
            ["<1@acme.test>", "<2@acme.test>", "<3@acme.test>"],
        )
        self.assertFalse(self.shipper.SPOOL.exists())
        self.assertEqual(list(self.shipper.PENDING.iterdir()), [])

    def test_a_failed_send_keeps_the_file_and_sends_the_same_batch_again(self) -> None:
        self.write_spool(scan_record())
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            self.assertEqual(self.shipper.main(), 1)
        self.assertEqual(len(list(self.shipper.PENDING.glob("*.jsonl"))), 1)

        self.write_spool(scan_record("<2@acme.test>"))  # arrived meanwhile: a file of its own
        with patch("urllib.request.urlopen", side_effect=self.deliver):
            self.assertEqual(self.shipper.main(), 0)
            self.write_spool(scan_record())
            self.shipper.main()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[0], self.sent[2])  # the same records give the same body, so one batch
        self.assertEqual(len(self.staged(OUTBOUND_SCANS)), 2)

    def test_the_backlog_drops_its_oldest_files(self) -> None:
        self.shipper.PENDING.mkdir()
        for name in ("1.jsonl", "2.jsonl", "3.jsonl"):
            (self.shipper.PENDING / name).write_text(json.dumps(scan_record()) + "\n")
        self.shipper.MAX_BACKLOG_BYTES = 2 * (self.shipper.PENDING / "1.jsonl").stat().st_size
        self.shipper.trim_backlog()
        self.assertEqual(sorted(path.name for path in self.shipper.PENDING.iterdir()), ["2.jsonl", "3.jsonl"])

    def test_without_a_token_nothing_is_sent(self) -> None:
        self.write_spool(scan_record())
        del self.environment["SUITE_CLOUD_TOKEN"]
        with patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(self.shipper.main(), 0)
        urlopen.assert_not_called()
        self.assertTrue(self.shipper.SPOOL.exists())
