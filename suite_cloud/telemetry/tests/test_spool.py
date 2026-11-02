"""The staging directory: whole files only, oldest first, bad ones moved aside."""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.tests.fixtures import error_log_count
from suite_cloud.telemetry import EVENTS, METRICS
from suite_cloud.telemetry.spool import Spool

ENVELOPE = {
    "batch_id": "",
    "received_at": datetime(2026, 10, 20, 10, 0),
    "source": "stalwart",
    "host_ip": "203.0.113.5",
    "hostname": "n1.c1.example.test",
}


def event_row(**fields) -> dict:
    return {
        "timestamp": datetime(2026, 10, 20, 9, 59),
        "level": "info",
        "event": "delivery.delivered",
        "span_id": None,
        "data": None,
        **fields,
    }


class TestSpool(IntegrationTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = Spool(Path(self.directory.name))

    def write(self, batch_id: str | None = None, table=EVENTS) -> Path:
        batch_id = batch_id or str(uuid7())
        batch = table.get_batch({**ENVELOPE, "batch_id": batch_id}, [event_row()])
        return self.spool.write(table, batch, batch_id)

    def test_a_batch_lands_as_one_parquet_file_and_leaves_no_temporary_file(self) -> None:
        path = self.write("b1")

        self.assertEqual(path, self.spool.get_directory(EVENTS) / "b1.parquet")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["b1.parquet"])

    def test_the_same_batch_id_replaces_the_earlier_file(self) -> None:
        self.write("b1")
        self.write("b1")

        self.assertEqual(len(self.spool.get_pending(EVENTS, 10)), 1)

    def test_a_failed_write_leaves_nothing_behind(self) -> None:
        with (
            patch("suite_cloud.telemetry.spool.parquet.write_table", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.write("b1")

        self.assertEqual(list(self.spool.get_directory(EVENTS).iterdir()), [])

    def test_pending_files_come_oldest_first_up_to_the_limit(self) -> None:
        names = [str(uuid7()) for _ in range(3)]
        for age, name in enumerate(reversed(names)):  # a WebHook batch id does not sort by arrival
            os.utime(self.write(name), (1_000_000 + age, 1_000_000 + age))

        pending = self.spool.get_pending(EVENTS, 2)
        self.assertEqual([p.stem for p in pending], list(reversed(names))[:2])
        self.assertEqual(self.spool.get_pending(METRICS, 2), [])

    def test_a_rejected_file_is_moved_aside_and_logged(self) -> None:
        path = self.write("b1")
        before = error_log_count()

        self.spool.reject(path, "not a parquet file")

        self.assertFalse(path.exists())
        self.assertTrue((self.spool.root / "rejected" / "events" / "b1.parquet").exists())
        self.assertEqual(error_log_count(), before + 1)

    def test_delete_tolerates_a_file_another_run_removed(self) -> None:
        path = self.write("b1")

        self.spool.delete([path, path.parent / "gone.parquet"])

        self.assertEqual(self.spool.get_pending(EVENTS, 10), [])
