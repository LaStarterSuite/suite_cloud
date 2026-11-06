"""The alert checker: a node whose metrics or scan shipper went silent, and a noisy node, open
alerts, which close when the telemetry says so."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_files_path, get_url_to_form, now_datetime

from suite_cloud.mail.cluster import rspamd
from suite_cloud.mail.tests.fixtures import error_log_count, make_cluster, make_node
from suite_cloud.telemetry import EVENTS, METRICS, status
from suite_cloud.telemetry.alerts import ERROR_RATE, METRICS_SILENT, SCAN_SILENT, Alerts
from suite_cloud.telemetry.spool import Spool
from suite_cloud.telemetry.warehouse import Warehouse
from suite_cloud.tests.fixtures import configure_settings

NOW = datetime.now(UTC).replace(tzinfo=None)
QUIET = "203.0.113.5"
NOISY = "203.0.113.6"
SCANNER = "203.0.113.8"


@skipUnless("insights" in frappe.get_installed_apps(), "needs the insights app")
class TestAlerts(IntegrationTestCase):
    def setUp(self) -> None:
        configure_settings(
            telemetry_silence_minutes=30,
            telemetry_errors_per_hour=5,
            telemetry_alert_recipients="ops@example.test, oncall@example.test\n",
        )
        frappe.db.delete("Server Alert")
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
        alerts = frappe.get_all("Server Alert", {"status": "Open"}, ["alert_type", "host_ip", "details"])
        return {(a.alert_type, a.host_ip): a.details for a in alerts}

    def test_a_node_that_went_quiet_and_a_node_full_of_errors_open_alerts_and_mail(self) -> None:
        self.report(QUIET, NOW - timedelta(minutes=45), table=METRICS)
        self.report(NOISY, NOW, self.events(errors=6))
        self.report("203.0.113.7", NOW - timedelta(hours=25), table=METRICS)  # too old to be expected

        counts = self.alerts.check()

        self.assertEqual(counts, {"opened": 2, "resolved": 0})
        opened = self.open_alerts()
        self.assertEqual(set(opened), {(METRICS_SILENT, QUIET), (ERROR_RATE, NOISY)})
        self.assertIn("No metrics received since", opened[(METRICS_SILENT, QUIET)])
        self.assertEqual(opened[(ERROR_RATE, NOISY)], "6 error events in the last hour.")
        alert = frappe.get_doc("Server Alert", {"host_ip": QUIET})
        self.assertEqual(
            (alert.title, alert.hostname, alert.status), ("Metrics Silent: node-5", "node-5", "Open")
        )
        self.assertEqual(self.mails.call_count, 2)
        self.assertEqual(
            self.mails.call_args.kwargs["recipients"], ["ops@example.test", "oncall@example.test"]
        )
        self.assertIn("opened", self.mails.call_args.kwargs["subject"])
        noisy = frappe.get_doc("Server Alert", {"host_ip": NOISY})
        message = self.mails.call_args.kwargs["message"]
        self.assertIn("node-6 is logging too many errors", message)
        self.assertIn("6 error events in the last hour", message)
        self.assertIn(get_url_to_form("Server Alert", noisy.name), message)
        self.assertNotIn(">Resolved<", message)
        (icon,) = self.mails.call_args.kwargs["inline_images"]
        self.assertEqual(icon["filename"], "suite.png")
        silent = self.mails.call_args_list[0].kwargs["message"]
        self.assertIn("node-5 stopped sending metrics", silent)
        self.assertIn("resolve the alert by hand", silent)

    def test_a_second_check_updates_the_open_alert_and_resolves_it_when_the_node_recovers(self) -> None:
        self.report(NOISY, NOW, self.events(errors=6))
        self.alerts.check()
        self.report(NOISY, NOW, self.events(errors=8))
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})
        self.assertEqual(frappe.db.count("Server Alert"), 1)
        self.assertIn("14 error events", self.open_alerts()[(ERROR_RATE, NOISY)])

        configure_settings(telemetry_errors_per_hour=20)  # more events of other levels change nothing
        self.report(NOISY, NOW, self.events(errors=0, total=1000))
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 1})

        alert = frappe.get_doc("Server Alert", {"host_ip": NOISY})
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

        alert = frappe.get_doc("Server Alert", {"host_ip": QUIET})
        self.assertEqual((alert.status, alert.details), ("Open", "No metrics received within the retention."))

    def test_a_metrics_alert_resolved_by_hand_stays_resolved_until_the_node_reports(self) -> None:
        self.report(QUIET, NOW - timedelta(minutes=45), table=METRICS)
        self.alerts.check()
        alert = frappe.get_doc("Server Alert", {"host_ip": QUIET})
        alert.status = "Resolved"
        alert.save()

        self.assertEqual(alert.dismissed, 1)
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})
        self.assertEqual(frappe.db.count("Server Alert"), 1)

        alert.db_set("resolved_at", now_datetime() - timedelta(minutes=50))  # dismissed before the last batch
        self.assertEqual(self.alerts.check(), {"opened": 1, "resolved": 0})
        self.assertEqual(frappe.db.count("Server Alert", {"status": "Open"}), 1)

    def test_a_node_that_recovered_and_fell_silent_again_gets_a_new_alert(self) -> None:
        self.report(QUIET, NOW - timedelta(minutes=45), table=METRICS)
        self.alerts.check()
        self.report(QUIET, NOW - timedelta(minutes=20), table=METRICS)
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 1})
        self.assertEqual(frappe.db.get_value("Server Alert", {"host_ip": QUIET}, "dismissed"), 0)

        configure_settings(telemetry_silence_minutes=10)  # the 20 minute old batch is now a silence
        self.assertEqual(self.alerts.check(), {"opened": 1, "resolved": 0})
        self.assertEqual(frappe.db.count("Server Alert", {"host_ip": QUIET, "status": "Open"}), 1)

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

        self.assertEqual(set(self.open_alerts()), {(METRICS_SILENT, QUIET)})
        self.mails.assert_not_called()

    def test_a_mail_that_cannot_go_out_is_logged_and_the_alert_stands(self) -> None:
        self.mails.side_effect = frappe.OutgoingEmailError("no account")
        self.report(QUIET, NOW - timedelta(hours=1), table=METRICS)
        errors = error_log_count()

        self.alerts.check()

        self.assertEqual(set(self.open_alerts()), {(METRICS_SILENT, QUIET)})
        self.assertEqual(error_log_count(), errors + 1)


class TestScanAlerts(IntegrationTestCase):
    """The scan shipper checks in every minute into the cache the forms read; no warehouse needed."""

    def setUp(self) -> None:
        configure_settings(
            enable_telemetry=1,
            outbound_scan_mode="Enforce",
            outbound_scan_reject_score=15,
            telemetry_silence_minutes=30,
            telemetry_alert_recipients="ops@example.test",
        )
        frappe.db.delete("Server Alert")
        status.forget_contacts()
        self.addCleanup(status.forget_contacts)
        self.node = make_node(make_cluster(), ipv4=SCANNER)
        self.node.db_set(
            {
                "status": "Active",
                "outbound_scanner_installed": 1,
                "outbound_scanner_digest": rspamd.get_config_digest(self.node.get_cluster()),
            }
        )
        self.alerts = Alerts(warehouse=None)
        self.enterContext(patch.object(Alerts, "find_silent_metrics", return_value=[]))
        self.enterContext(patch.object(Alerts, "find_error_rates", return_value=[]))
        self.mails = self.enterContext(patch("suite_cloud.telemetry.alerts.frappe.sendmail"))

    def check_in(self, minutes_ago: int) -> None:
        seen = now_datetime() - timedelta(minutes=minutes_ago)
        frappe.cache.hset(
            f"{status.CACHE_KEY}:{status.SCAN_CHECK_IN}", SCANNER, {"seen_at": seen.isoformat()}
        )

    def open_alert(self) -> frappe._dict | None:
        return frappe.db.get_value(
            "Server Alert", {"alert_type": SCAN_SILENT, "status": "Open"}, ["name", "details"], as_dict=True
        )

    def test_a_shipper_that_stopped_checking_in_opens_an_alert_until_it_checks_in(self) -> None:
        self.check_in(minutes_ago=5)
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})

        self.check_in(minutes_ago=45)
        self.assertEqual(self.alerts.check(), {"opened": 1, "resolved": 0})
        self.assertIn("No check-in from the scan shipper since", self.open_alert().details)
        message = self.mails.call_args.kwargs["message"]
        self.assertIn(f"The outbound scan on {self.node.hostname} stopped reporting", message)
        self.assertEqual(
            self.mails.call_args.kwargs["subject"],
            f"[Suite Cloud] Outbound Scan Silent: {self.node.hostname} opened",
        )

        status.forget_contacts()  # a cleared cache proves nothing either way
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})
        self.assertIn("cache was cleared", self.open_alert().details)

        self.check_in(minutes_ago=0)
        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 1})
        self.assertIn("is reporting again", self.mails.call_args.kwargs["message"])

    def test_only_a_running_node_with_the_current_scanner_is_watched(self) -> None:
        self.check_in(minutes_ago=45)
        watched = {
            "status": "Active",
            "outbound_scanner_digest": rspamd.get_config_digest(self.node.get_cluster()),
        }
        cases = (
            ("an older scanner, whose shipper may not check in", {"outbound_scanner_digest": "older"}, {}),
            ("a node out of service", {"status": "Provisioned"}, {}),
            ("the scan switched off", {}, {"outbound_scan_mode": "Off"}),
            ("telemetry off, which refuses the shipper", {}, {"enable_telemetry": 0}),
        )
        for case, node_fields, settings in cases:
            with self.subTest(case):
                frappe.db.set_value("Stalwart Node", self.node.name, {**watched, **node_fields})
                configure_settings(**{"enable_telemetry": 1, "outbound_scan_mode": "Enforce", **settings})
                self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})

        frappe.db.set_value("Stalwart Node", self.node.name, watched)
        configure_settings(enable_telemetry=1, outbound_scan_mode="Enforce")
        self.assertEqual(self.alerts.check(), {"opened": 1, "resolved": 0})

    def test_an_alert_resolved_by_hand_stays_resolved_until_the_next_check_in(self) -> None:
        self.check_in(minutes_ago=45)
        self.alerts.check()
        alert = frappe.get_doc("Server Alert", self.open_alert().name)
        alert.status = "Resolved"
        alert.save()

        self.assertEqual(self.alerts.check(), {"opened": 0, "resolved": 0})
        alert.db_set("resolved_at", now_datetime() - timedelta(minutes=50))  # dismissed before the check-in
        self.check_in(minutes_ago=40)  # checked in after the dismissal, then went silent again
        self.assertEqual(self.alerts.check(), {"opened": 1, "resolved": 0})


class TestRenamePatch(IntegrationTestCase):
    def test_a_silent_host_alert_becomes_a_metrics_silent_one(self) -> None:
        from suite_cloud.patches.v1_0 import rename_telemetry_alert_to_server_alert as patch_module

        frappe.db.delete("Server Alert")
        alert = frappe.get_doc(
            {"doctype": "Server Alert", "alert_type": METRICS_SILENT, "status": "Open", "host_ip": QUIET}
        ).insert()
        frappe.db.set_value(
            "Server Alert", alert.name, {"alert_type": "Silent Host", "title": "Silent Host: n1"}
        )

        patch_module.execute()
        patch_module.execute()  # a second run changes nothing

        self.assertEqual(
            frappe.db.get_value("Server Alert", alert.name, ["alert_type", "title"]),
            (METRICS_SILENT, "Metrics Silent: n1"),
        )
