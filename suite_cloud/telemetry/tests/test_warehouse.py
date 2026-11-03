"""The loader: whole batches into Insights' DuckDB once, bad files aside, old rows out."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5, uuid7

import frappe
from duckdb import IOException
from frappe.tests import IntegrationTestCase
from frappe.utils import get_files_path

from suite_cloud.mail.tests.fixtures import error_log_count, make_cluster, make_node
from suite_cloud.telemetry import CURRENT_METRICS, EVENT_DATA_COLUMNS, EVENTS, METRICS, TelemetryTable
from suite_cloud.telemetry.spool import Spool
from suite_cloud.telemetry.warehouse import Warehouse
from suite_cloud.tests.fixtures import configure_settings

NOW = datetime.now(UTC).replace(tzinfo=None)
ENVELOPE = {"received_at": NOW, "source": "stalwart", "host_ip": "203.0.113.5", "hostname": "n1"}
STALWART_DATA = (
    '{"accountName": "ana", "remoteIp": "198.51.100.7", "listenerId": "smtp", "domain": "example.com", '
    '"hostname": "mx.example.com", "details": "Remote said: 550 5.1.1 no such user", '
    '"from": "ana@acme.test", "to": "bo@example.com", "elapsed": 412}'
)
# remote_ip, listener, domain, mx, reason, code, account, sender, recipient, sender_domain,
# recipient_domain, elapsed
STALWART_COLUMNS = (
    "198.51.100.7",
    "smtp",
    "example.com",
    "mx.example.com",
    "Remote said: 550 5.1.1 no such user",
    "550",
    "ana",
    "ana@acme.test",
    "bo@example.com",
    "acme.test",
    "example.com",
    412.0,
)
CODE_ONLY_COLUMNS = (None, None, None, None, "550 rejected", "421", None, None, None, None, None, None)
NO_DATA_COLUMNS = (None,) * len(EVENT_DATA_COLUMNS)
TABLE_NAMES = {"events", "metrics", "nodes", "mail_domains", "current_metrics"}


def event(**fields) -> dict:
    return {
        "timestamp": NOW,
        "level": "info",
        "event": "delivery.delivered",
        "span_id": None,
        "data": None,
        **fields,
    }


def minutes(count: int) -> datetime:
    return NOW - timedelta(minutes=60 - count)


def counter(at: int, value: float, **fields) -> dict:
    return metric(
        **{"timestamp": minutes(at), "name": "auth.success", "type": "counter", "value": value, **fields}
    )


def histogram(at: int, count: int, total: float) -> dict:
    return metric(
        timestamp=minutes(at),
        name="delivery.total-time",
        type="histogram",
        value=total / count,
        count=count,
        sum=total,
    )


def metric(**fields) -> dict:
    return {
        "timestamp": NOW,
        "name": "server.memory",
        "type": "gauge",
        "value": 1.0,
        "count": None,
        "sum": None,
        "labels": None,
        **fields,
    }


@skipUnless("insights" in frappe.get_installed_apps(), "needs the insights app")
class TestWarehouse(IntegrationTestCase):
    def setUp(self) -> None:
        configure_settings(telemetry_retention_days=30)
        self.name = f"telemetry_test_{uuid7().hex[:8]}"
        self.addCleanup(self.remove_store)
        self.spool = Spool(Path(get_files_path(self.name, is_private=1)))
        self.warehouse = Warehouse(self.name, self.spool)
        self.warehouse.ensure_data_source()
        self.warehouse.refresh_tables()

    def remove_store(self) -> None:
        shutil.rmtree(self.spool.root, ignore_errors=True)
        Path(get_files_path(f"{self.name}.duckdb", is_private=1)).unlink(missing_ok=True)

    def stage(self, table: TelemetryTable, rows: list[dict], batch_id: str | None = None) -> Path:
        batch_id = batch_id or str(uuid7())
        return self.spool.write(table, table.get_batch({**ENVELOPE, "batch_id": batch_id}, rows), batch_id)

    def count(self, table: TelemetryTable) -> int:
        with self.warehouse.write_connection() as db:
            return db.con.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]

    def registered(self) -> set[str]:
        return set(frappe.get_all("Insights Table v3", {"data_source": self.name}, pluck="table"))

    def test_the_store_is_set_up_once_and_its_tables_are_listed_in_insights(self) -> None:
        self.warehouse.ensure_data_source()  # a second migrate changes nothing

        self.assertTrue(frappe.db.exists("Insights Data Source v3", self.name))
        self.assertEqual(self.registered(), TABLE_NAMES)
        self.assertEqual((self.count(EVENTS), self.count(METRICS)), (0, 0))

    def test_without_insights_the_setup_fails_in_plain_words(self) -> None:
        with (
            patch("suite_cloud.telemetry.warehouse.frappe.get_installed_apps", return_value=["frappe"]),
            self.assertRaisesRegex(frappe.ValidationError, "Install the insights app"),
        ):
            Warehouse("telemetry_missing").ensure_data_source()

    def test_a_migrate_does_not_wait_for_the_store_file(self) -> None:
        with patch.object(Warehouse, "write_connection", side_effect=IOException("Could not set lock")):
            self.warehouse.ensure_data_source()

        self.assertEqual(self.registered(), TABLE_NAMES)

    def test_a_load_inserts_every_staged_file_and_deletes_it(self) -> None:
        self.stage(EVENTS, [event(), event(level="warn"), event()])
        self.stage(EVENTS, [event(span_id="7", data='{"a":1}')])
        self.stage(METRICS, [metric(type="histogram", count=3, sum=9.5)])

        added = self.warehouse.load_pending()

        self.assertEqual(added, {"events": 4, "metrics": 1})
        self.assertEqual((self.count(EVENTS), self.count(METRICS)), (4, 1))
        self.assertEqual(self.spool.get_pending(EVENTS, 10) + self.spool.get_pending(METRICS, 10), [])

    def test_the_first_load_into_a_new_store_creates_its_tables(self) -> None:
        Path(get_files_path(f"{self.name}.duckdb", is_private=1)).unlink()
        self.warehouse.ensure_data_source()
        self.stage(EVENTS, [event()])

        added = self.warehouse.load_pending()

        self.assertEqual(added, {"events": 1})
        self.assertEqual((self.count(EVENTS), self.count(METRICS)), (1, 0))

    def test_a_load_extracts_the_event_data_the_dashboards_read(self) -> None:
        self.stage(
            EVENTS,
            [
                event(data=STALWART_DATA),
                event(data='{"code": "421", "details": "550 rejected", "elapsed": "soon"}'),
                event(),
            ],
        )

        self.warehouse.load_pending()

        self.assertEqual(self.event_data_columns(), [STALWART_COLUMNS, CODE_ONLY_COLUMNS, NO_DATA_COLUMNS])

    def test_a_table_from_before_the_extracted_columns_gets_them_filled_on_the_next_load(self) -> None:
        legacy = TelemetryTable(EVENTS.name, EVENTS.schema)
        batch = legacy.get_batch({**ENVELOPE, "batch_id": "old"}, [event(data=STALWART_DATA)])
        with self.warehouse.write_connection() as db:
            db.con.execute(f'DROP TABLE "{EVENTS.name}"')
            db.raw_sql(legacy.create_statement)
            db.con.register("batch", batch)
            db.con.execute(f'INSERT INTO "{EVENTS.name}" SELECT * FROM batch')

        self.warehouse.load_pending()

        self.assertEqual(self.event_data_columns(), [STALWART_COLUMNS])

    def event_data_columns(self) -> list[tuple]:
        names = ", ".join(f'"{column.name}"' for column in EVENT_DATA_COLUMNS)
        with self.warehouse.write_connection() as db:
            return db.con.execute(
                f'SELECT {names} FROM "{EVENTS.name}" ORDER BY "data" NULLS LAST'
            ).fetchall()

    def deltas(self, name: str, labels: str | None = None) -> list[tuple]:
        with self.warehouse.write_connection() as db:
            return db.con.execute(
                'SELECT increase, samples_added FROM "metrics" WHERE name = $name '
                "AND labels IS NOT DISTINCT FROM $labels ORDER BY timestamp, host_ip",
                {"name": name, "labels": labels},
            ).fetchall()

    def test_a_counter_gets_its_increase_per_point_and_its_own_total_after_a_restart(self) -> None:
        self.stage(METRICS, [counter(0, 4), counter(1, 6), counter(2, 6), counter(3, 2), counter(4, 5)])

        self.warehouse.load_pending()

        self.assertEqual(
            self.deltas("auth.success"), [(None, None), (2, None), (0, None), (2, None), (3, None)]
        )

    def test_a_histogram_gets_its_added_sum_and_samples_and_a_restart_its_own_totals(self) -> None:
        # The restart at minute 2 kept the count at 3, but the sum fell.
        points = [histogram(0, 2, 100), histogram(1, 3, 160), histogram(2, 3, 30), histogram(3, 1, 10)]
        self.stage(METRICS, points)

        self.warehouse.load_pending()

        self.assertEqual(self.deltas("delivery.total-time"), [(None, None), (60, 1), (30, 3), (10, 1)])

    def test_a_series_the_hosts_previous_push_lacked_counts_its_whole_total(self) -> None:
        """Stalwart pushes a counter or histogram only once it is above zero."""

        self.stage(METRICS, [counter(0, 4)])
        self.warehouse.load_pending()
        self.stage(METRICS, [counter(1, 4), histogram(1, 1, 30)])
        self.stage(METRICS, [counter(2, 4), histogram(2, 1, 30), counter(2, 2, name="queue.message-queued")])

        self.warehouse.load_pending()

        self.assertEqual(self.deltas("delivery.total-time"), [(30, 1), (0, 0)])
        self.assertEqual(self.deltas("queue.message-queued"), [(2, None)])

    def test_a_gauge_has_no_increase(self) -> None:
        self.stage(METRICS, [metric(timestamp=minutes(0), value=5), metric(timestamp=minutes(1), value=3)])

        self.warehouse.load_pending()

        self.assertEqual(self.deltas("server.memory"), [(None, None), (None, None)])

    def test_a_series_continues_from_its_stored_points_across_loads(self) -> None:
        self.stage(METRICS, [counter(0, 4), counter(5, 10)])
        self.warehouse.load_pending()
        self.stage(METRICS, [counter(6, 12), counter(7, 15)])
        self.stage(METRICS, [counter(2, 7)])  # a late file: against minute 0, not minute 7

        self.warehouse.load_pending()

        self.assertEqual([increase for increase, _ in self.deltas("auth.success")], [None, 3, 6, 2, 3])

    def test_series_are_told_apart_by_host_and_labels_even_when_none_are_set(self) -> None:
        labelled = '{"protocol": "smtp"}'
        self.stage(METRICS, [counter(0, 1), counter(0, 50, labels=labelled), counter(1, 3)])
        self.stage(METRICS, [counter(1, 52, labels=labelled), counter(2, 4)])
        self.spool.write(
            METRICS,
            METRICS.get_batch({**ENVELOPE, "host_ip": "203.0.113.6", "batch_id": "other"}, [counter(2, 100)]),
            "other",
        )

        self.warehouse.load_pending()

        self.assertEqual([increase for increase, _ in self.deltas("auth.success")], [None, 2, 1, None])
        self.assertEqual([increase for increase, _ in self.deltas("auth.success", labelled)], [None, 2])

    def test_a_table_from_before_the_delta_columns_gets_them_filled_on_the_next_load(self) -> None:
        legacy = TelemetryTable(METRICS.name, METRICS.schema)
        rows = [counter(0, 4), counter(1, 6), histogram(1, 3, 160), histogram(2, 4, 200)]
        batch = legacy.get_batch({**ENVELOPE, "batch_id": "old"}, rows)
        with self.warehouse.write_connection() as db:
            db.con.execute(f'DROP TABLE "{METRICS.name}"')
            db.raw_sql(legacy.create_statement)
            db.con.register("batch", batch)
            db.con.execute(f'INSERT INTO "{METRICS.name}" SELECT * FROM batch')
        self.stage(METRICS, [counter(2, 9)])

        self.warehouse.load_pending()

        self.assertEqual(self.deltas("auth.success"), [(None, None), (2, None), (3, None)])
        self.assertEqual(self.deltas("delivery.total-time"), [(160, 3), (40, 1)])

    def test_current_metrics_hold_the_newest_gauge_of_every_node_still_pushing(self) -> None:
        silent = {**ENVELOPE, "host_ip": "203.0.113.6", "hostname": "n2", "batch_id": "silent"}
        self.stage(METRICS, [metric(timestamp=minutes(50), value=5), metric(timestamp=minutes(58), value=7)])
        self.stage(METRICS, [counter(58, 3), metric(timestamp=minutes(58), name="queue.count", value=2)])
        self.spool.write(
            METRICS, METRICS.get_batch(silent, [metric(timestamp=minutes(45), value=9)]), "silent"
        )

        self.warehouse.load_pending()

        with self.warehouse.write_connection() as db:
            current = db.con.execute(
                f'SELECT host_ip, hostname, name, value FROM "{CURRENT_METRICS.name}" ORDER BY name'
            ).fetchall()
        self.assertEqual(
            current, [("203.0.113.5", "n1", "queue.count", 2), ("203.0.113.5", "n1", "server.memory", 7)]
        )

    def test_a_file_left_behind_after_its_commit_is_not_loaded_twice(self) -> None:
        self.stage(EVENTS, [event(), event()], batch_id="b1")
        self.warehouse.load_pending()
        self.stage(EVENTS, [event(), event()], batch_id="b1")  # as if the run died before the delete
        self.stage(EVENTS, [event()], batch_id="b2")

        added = self.warehouse.load_pending()

        self.assertEqual(added, {"events": 1})
        self.assertEqual(self.count(EVENTS), 3)
        self.assertEqual(self.spool.get_pending(EVENTS, 10), [])

    def test_a_backlog_drains_in_one_run_chunk_by_chunk(self) -> None:
        for _ in range(5):
            self.stage(EVENTS, [event()])

        with (
            patch("suite_cloud.telemetry.warehouse.BATCH_FILES", 2),
            patch.object(Warehouse, "load", wraps=self.warehouse.load) as load,
        ):
            added = self.warehouse.load_pending()

        self.assertEqual((added, load.call_count), ({"events": 5}, 3))
        self.assertEqual(self.spool.get_pending(EVENTS, 10), [])

    def test_a_file_the_loader_cannot_read_is_moved_aside_and_the_rest_load(self) -> None:
        directory = self.spool.get_directory(EVENTS)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "0-garbage.parquet").write_bytes(b"not parquet")
        damaged = self.stage(EVENTS, [event(data='{"k": "%s"}' % ("x" * 2000))], batch_id="0-damaged")
        content = bytearray(damaged.read_bytes())
        content[8:40] = b"\xff" * 32  # inside the first data page; the footer and its schema stay intact
        damaged.write_bytes(content)
        self.spool.write(
            EVENTS, METRICS.get_batch({**ENVELOPE, "batch_id": "0-wrong"}, [metric()]), "0-wrong"
        )
        self.stage(EVENTS, [event()])
        errors = error_log_count()

        added = self.warehouse.load_pending()

        self.assertEqual(added, {"events": 1})
        rejected = self.spool.root / "rejected" / "events"
        self.assertEqual(
            sorted(p.name for p in rejected.iterdir()),
            ["0-damaged.parquet", "0-garbage.parquet", "0-wrong.parquet"],
        )
        self.assertEqual(error_log_count(), errors + 3)

    def test_a_lock_that_cannot_be_taken_keeps_the_files_for_the_next_run(self) -> None:
        path = self.stage(EVENTS, [event()])
        errors = error_log_count()

        with patch.object(Warehouse, "write_connection", side_effect=IOException("Could not set lock")):
            added = self.warehouse.load_pending()

        self.assertEqual(added, {})
        self.assertTrue(path.exists())
        self.assertEqual(error_log_count(), errors + 1)  # the table refresh fails, so no load is tried

    def test_the_prune_removes_only_rows_past_the_retention(self) -> None:
        self.stage(
            EVENTS, [event(timestamp=NOW - timedelta(days=31)), event(timestamp=NOW - timedelta(days=29))]
        )
        self.stage(METRICS, [metric(timestamp=NOW - timedelta(days=31))])
        self.warehouse.load_pending()

        self.warehouse.prune_expired()

        self.assertEqual((self.count(EVENTS), self.count(METRICS)), (1, 0))

    def test_the_agents_rows_go_staged_or_loaded_and_stalwarts_stay(self) -> None:
        from suite_cloud.patches.v1_0 import delete_agent_telemetry as cleanup

        pushed = str(uuid5(NAMESPACE_URL, "n1:batch"))
        self.stage(EVENTS, [event()])
        self.stage(METRICS, [metric(name="host.cpu.percent")])
        self.stage(EVENTS, [event(), event()], pushed)
        self.warehouse.load_pending()
        staged = self.stage(EVENTS, [event()])  # an agent batch not loaded yet
        kept = self.stage(METRICS, [metric()], str(uuid5(NAMESPACE_URL, "n1:otlp")))

        with (
            patch.object(cleanup, "DATA_SOURCE", self.name),
            patch.object(cleanup, "Warehouse", return_value=self.warehouse),
            patch.object(cleanup, "Spool", return_value=self.spool),
        ):
            cleanup.execute()

        self.assertEqual((self.count(EVENTS), self.count(METRICS)), (2, 0))
        self.assertEqual((staged.exists(), kept.exists()), (False, True))

    def test_node_and_domain_records_are_copied_beside_the_telemetry(self) -> None:
        cluster = make_cluster()
        node = make_node(cluster, ipv4="203.0.113.77", ipv6_address="2001:db8::77")
        # Inserted as rows: the documents' hooks would need an active cluster to push to.
        tenant = frappe.get_doc(
            {
                "doctype": "Suite Tenant",
                "name": str(uuid7()),
                "title": "Acme",
                "tenant_type": "Suite Site",
                "user": "Administrator",
            }
        )
        tenant.db_insert()
        frappe.get_doc(
            {
                "doctype": "Mail Domain",
                "domain_name": "acme.frappe.test",
                "tenant": tenant.name,
                "cluster": cluster.name,
            }
        ).db_insert()
        frappe.get_doc(
            {
                "doctype": "Egress Gateway",
                "name": "g1.blr.frappe.test",
                "hostname": "g1.blr.frappe.test",
                "cluster": cluster.name,
                "ipv4_address": "203.0.113.88",
                "status": "Active",
            }
        ).db_insert()

        self.warehouse.load_pending()

        # Only what this test made: the site may hold clusters and tenants of its own.
        with self.warehouse.write_connection() as db:
            nodes = db.con.execute(
                'SELECT host_ip, hostname, cluster, role FROM "nodes" WHERE cluster = ? ORDER BY host_ip',
                [cluster.name],
            ).fetchall()
            domains = db.con.execute(
                'SELECT domain, tenant, tenant_title FROM "mail_domains" WHERE tenant = ?', [tenant.name]
            ).fetchall()
        self.assertEqual(
            nodes,
            [
                ("2001:db8::77", node.hostname, cluster.name, "full"),
                ("203.0.113.77", node.hostname, cluster.name, "full"),
                ("203.0.113.88", "g1.blr.frappe.test", cluster.name, "gateway"),
            ],
        )
        self.assertEqual(domains, [("acme.frappe.test", tenant.name, "Acme")])
        self.assertEqual(self.registered(), TABLE_NAMES)

    def test_a_retention_under_a_day_falls_back_to_the_default(self) -> None:
        with patch("suite_cloud.telemetry.warehouse.get_config", return_value=-5):
            self.assertEqual(self.warehouse.retention_days, 90)
