"""The alert checker: a silent node and a noisy node open alerts, which close when the data says so."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_files_path, get_url_to_form, now_datetime

from suite_cloud.mail.tests.fixtures import error_log_count
from suite_cloud.telemetry import EVENTS, METRICS
from suite_cloud.telemetry.alerts import ERROR_RATE, SILENT_HOST, Alerts
from suite_cloud.telemetry.spool import Spool
from suite_cloud.telemetry.warehouse import Warehouse
from suite_cloud.tests.fixtures import configure_settings

NOW = datetime.now(UTC).replace(tzinfo=None)
QUIET = "203.0.113.5"
NOISY = "203.0.113.6"


@skipUnless("insights" in frappe.get_installed_apps(), "needs the insights app")
class TestAlerts(IntegrationTestCase):
    def setUp(self) -> None:
        configure_settings(
            telemetry_silence_minutes=30,
            telemetry_errors_per_hour=5,
            telemetry_alert_recipients="ops@example.test, oncall@example.test\n",
        )
        frappe.db.delete("Telemetry Alert")
        name = f"telemetry_test_{uuid7().hex[:8]}"
        self.spool = Spool(Path(get_files_path(name, is_private=1)))
        self.addCleanup(shutil.rmtree, self.spool.root, ignore_errors=True)
        self.addCleanup(Path(get_files_path(f"{name}.duckdb", is_private=1)).unlink, missing_ok=True)
        self.warehouse = Warehouse(name, self.spool)
        self.warehouse.ensure_data_source()
        self.warehouse.refresh_tables()
        self.alerts = Alerts(self.warehouse)
        self.mails = patch("suite_cloud.telemetry.alerts.frappe.sendmail").start()
        self.addCleanup(patch.stopall)

    def report(
        self, host_ip: str, received_at: datetime, rows: list[dict] | None = None, table=EVENTS
    ) -> None:
        envelope = {
            "batch_id": str(uuid7()),
            "received_at": received_at,
            "source": "stalwart",
            "host_ip": host_ip,
            "hostname": f"node-{host_ip[-1]}",
        }
        rows = rows or [
            {
                "timestamp": received_at,
                "name": "queue.count",
                "type": "gauge",
                "value": 1.0,
                "count": None,
                "sum": None,
                "labels": None,
            }
        ]
        self.spool.write(table, table.get_batch(envelope, rows), envelope["batch_id"])
        self.warehouse.load_pending()

    def events(self, errors: int, total: int = 100) -> list[dict]:
        return [
            {
                "timestamp": NOW - timedelta(minutes=5),
                "level": "error" if i < errors else "info",
                "event": "auth.error",
                "span_id": None,
                "data": None,
            }
            for i in range(total)
        ]

    def open_alerts(self) -> dict[tuple[str, str], str]:
        alerts = frappe.get_all("Telemetry Alert", {"status": "Open"}, ["alert_type", "host_ip", "details"])
        return {(a.alert_type, a.host_ip): a.details for a in alerts}

    def test_a_node_that_went_quiet_and_a_node_full_of_errors_open_alerts_and_mail(self) -> None:
        self.report(QUIET, NOW - timedelta(minutes=45), table=METRICS)
        self.report(NOISY, NOW, self.events(errors=6))
        self.report("203.0.113.7", NOW - timedelta(hours=25), table=METRICS)  # too old to be expected

        counts = self.alerts.check()

        self.assertEqual(counts, {"opened": 2, "resolved": 0})
        opened = self.open_alerts()
        self.assertEqual(set(opened), {(SILENT_HOST, QUIET), (ERROR_RATE, NOISY)})
        self.assertIn("No batch received since", opened[(SILENT_HOST, QUIET)])
        self.assertEqual(opened[(ERROR_RATE, NOISY)], "6 error events in the last hour.")
        alert = frappe.get_doc("Telemetry Alert", {"host_ip": QUIET})
        self.assertEqual(
            (alert.title, alert.hostname, alert.status), ("Silent Host: node-5", "node-5", "Open")
        )
        self.assertEqual(self.mails.call_count, 2)
        self.assertEqual(
            self.mails.call_args.kwargs["recipients"], ["ops@example.test", "oncall@example.test"]
        )
        self.assertIn("opened", self.mails.call_args.kwargs["subject"])
        noisy = frappe.get_doc("Telemetry Alert", {"host_ip": NOISY})
        message = self.mails.call_args.kwargs["message"]
        self.assertIn("node-6 is logging too many errors", message)
        self.assertIn("6 error events in the last hour", message)
        self.assertIn(get_url_to_form("Telemetry Alert", noisy.name), message)
        self.assertNotIn(">Resolved<", message)
        (icon,) = self.mails.call_args.kwargs["inline_images"]
        self.assertEqual(icon["filename"], "suite.png")
        silent = self.mails.call_args_list[0].kwargs["message"]
        self.assertIn("node-5 stopped reporting", silent)
        self.assertIn("resolve the alert by hand", silent)

    def test_a_second_check_updates_the_open_alert_and_resolves_it_when_the_node_recovers(self) -> None:
        self.report(NOISY, NOW, self.events(errors=6))
        self.alerts.check()
        self.report(NOISY, NOW, self.events(errors=8))
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})
        self.assertEqual(frappe.db.count("Telemetry Alert"), 1)
        self.assertIn("14 error events", self.open_alerts()[(ERROR_RATE, NOISY)])

        configure_settings(telemetry_errors_per_hour=20)  # more events of other levels change nothing
        self.report(NOISY, NOW, self.events(errors=0, total=1000))
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 1})

        alert = frappe.get_doc("Telemetry Alert", {"host_ip": NOISY})
        self.assertEqual(alert.status, "Resolved")
        self.assertIsNotNone(alert.resolved_at)
        self.assertIn("The condition cleared", alert.details)
        self.assertIn("resolved", self.mails.call_args.kwargs["subject"])
        message = self.mails.call_args.kwargs["message"]
        self.assertIn("Errors on node-6 are back under the threshold", message)
        self.assertIn(">Resolved<", message)

    def test_a_silent_node_keeps_its_alert_past_the_lookback_until_it_reports_again(self) -> None:
        self.report(QUIET, NOW - timedelta(hours=2), table=METRICS)
        self.alerts.check()
        with patch("suite_cloud.telemetry.alerts.LOOKBACK", timedelta(hours=1)):
            self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})

        self.report(QUIET, NOW, table=METRICS)
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 1})

    def test_a_check_reads_the_whole_history_only_for_a_watched_node_silent_past_the_lookback(self) -> None:
        self.report(QUIET, NOW - timedelta(hours=2), table=METRICS)
        self.alerts.check()

        with patch.object(self.warehouse, "query", wraps=self.warehouse.query) as query:
            self.alerts.check()
            self.assertFalse(any("list_contains" in call.args[0] for call in query.call_args_list))

            with patch("suite_cloud.telemetry.alerts.LOOKBACK", timedelta(hours=1)):
                self.alerts.check()
            self.assertEqual(
                [call.args[1]["hosts"] for call in query.call_args_list if "hosts" in call.args[1]], [[QUIET]]
            )

    def test_a_silent_node_whose_rows_the_retention_deleted_is_not_recovered(self) -> None:
        self.report(QUIET, NOW - timedelta(hours=2), table=METRICS)
        self.alerts.check()
        with self.warehouse.write_connection() as db:
            db.con.execute('DELETE FROM "metrics"')

        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})

        alert = frappe.get_doc("Telemetry Alert", {"host_ip": QUIET})
        self.assertEqual((alert.status, alert.details), ("Open", "No batch received within the retention."))

    def test_a_silent_host_alert_resolved_by_hand_stays_resolved_until_the_node_reports(self) -> None:
        self.report(QUIET, NOW - timedelta(minutes=45), table=METRICS)
        self.alerts.check()
        alert = frappe.get_doc("Telemetry Alert", {"host_ip": QUIET})
        alert.status = "Resolved"
        alert.save()

        self.assertEqual(alert.dismissed, 1)
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})
        self.assertEqual(frappe.db.count("Telemetry Alert"), 1)

        alert.db_set("resolved_at", now_datetime() - timedelta(minutes=50))  # dismissed before the last batch
        self.assertEqual(self.alerts.check(), {"opened": 1, "resolved": 0})
        self.assertEqual(frappe.db.count("Telemetry Alert", {"status": "Open"}), 1)

    def test_a_node_that_recovered_and_fell_silent_again_gets_a_new_alert(self) -> None:
        self.report(QUIET, NOW - timedelta(minutes=45), table=METRICS)
        self.alerts.check()
        self.report(QUIET, NOW - timedelta(minutes=20), table=METRICS)
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 1})
        self.assertEqual(frappe.db.get_value("Telemetry Alert", {"host_ip": QUIET}, "dismissed"), 0)

        configure_settings(telemetry_silence_minutes=10)  # the 20 minute old batch is now a silence
        self.assertEqual(self.alerts.check(), {"opened": 1, "resolved": 0})
        self.assertEqual(frappe.db.count("Telemetry Alert", {"host_ip": QUIET, "status": "Open"}), 1)

    def test_errors_under_the_threshold_or_older_than_an_hour_open_nothing(self) -> None:
        self.report(NOISY, NOW, self.events(errors=4, total=4))  # every event an error, but only 4
        old = [{**event, "timestamp": NOW - timedelta(minutes=61)} for event in self.events(errors=10)]
        self.report(QUIET, NOW, old)

        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})
        self.assertEqual(self.open_alerts(), {})
        self.mails.assert_not_called()

    def test_without_recipients_alerts_are_recorded_and_nobody_is_mailed(self) -> None:
        configure_settings(telemetry_alert_recipients="")
        self.report(QUIET, NOW - timedelta(hours=1), table=METRICS)

        self.alerts.check()

        self.assertEqual(set(self.open_alerts()), {(SILENT_HOST, QUIET)})
        self.mails.assert_not_called()

    def test_a_mail_that_cannot_go_out_is_logged_and_the_alert_stands(self) -> None:
        self.mails.side_effect = frappe.OutgoingEmailError("no account")
        self.report(QUIET, NOW - timedelta(hours=1), table=METRICS)
        errors = error_log_count()

        self.alerts.check()

        self.assertEqual(set(self.open_alerts()), {(SILENT_HOST, QUIET)})
        self.assertEqual(error_log_count(), errors + 1)
