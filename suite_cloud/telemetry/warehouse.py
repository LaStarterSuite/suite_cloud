"""Insights' DuckDB: the loader copies staged batches in, and the retention prunes old rows.

Suite Cloud owns one local DuckDB data source in Insights, with one table per kind of telemetry.
Writes go through Insights' write connection, which holds its file lock, so dashboards reading the
file and this loader never collide: files are checked before the lock is taken, and the lock is
held only for the insert.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import frappe
import pyarrow.parquet as parquet
from frappe import _
from frappe.utils import cint

from suite_cloud.telemetry import (
    CURRENT_METRICS,
    DIMENSIONS,
    MAIL_DOMAINS,
    METRICS,
    NODES,
    TABLES,
    TelemetryTable,
)
from suite_cloud.telemetry.spool import Spool
from suite_cloud.utils import get_config, log_exception

DATA_SOURCE = "suite_cloud_telemetry"  # the Insights data source and its file under private files
BATCH_FILES = 200  # files loaded under one lock: with 5000 rows each, seconds of hold at most
DEFAULT_RETENTION_DAYS = 90
NODE_FIELDS = ("hostname", "cluster", "status", "ipv4_address")
CURRENT_AFTER = timedelta(minutes=10)  # a node silent for longer drops out of current_metrics
# The previous point of a metric's series, which the delta columns subtract.
PREVIOUS_POINT = (
    "lag(value) OVER series AS previous_value, lag(count) OVER series AS previous_count, "
    "lag(sum) OVER series AS previous_sum"
)
SERIES_WINDOW = "WINDOW series AS (PARTITION BY host_ip, name, labels ORDER BY timestamp)"
# The host's push before each of its pushes, from one row per push (its host_ip and timestamp).
PREVIOUS_PUSH = "lag(timestamp) OVER (PARTITION BY host_ip ORDER BY timestamp) AS previous_push"


def load_staged_batches() -> None:
    """Scheduled every five minutes."""

    Warehouse().load_pending()


def prune_expired_rows() -> None:
    """Scheduled daily."""

    Warehouse().prune_expired()


class Warehouse:
    """The data source Suite Cloud's telemetry lives in, and the only writer of its file."""

    def __init__(self, data_source: str = DATA_SOURCE, spool: Spool | None = None):
        self.data_source = data_source
        self.spool = spool or Spool()

    @property
    def retention_days(self) -> int:
        """The configured retention; anything under a day falls back to the default, since a
        negative number would move the cutoff into the future and delete the whole history."""

        days = cint(get_config("telemetry_retention_days"))
        return days if days > 0 else DEFAULT_RETENTION_DAYS

    def ensure_data_source(self) -> None:
        """Install and migrate: the Insights data source and its table registration.

        The file itself is left to the loader: a dashboard reading it can hold it longer than a
        migrate should wait.
        """

        if "insights" not in frappe.get_installed_apps():
            frappe.throw(_("Install the insights app before migrating Suite Cloud: it holds the telemetry."))
        if not frappe.db.exists("Insights Data Source v3", self.data_source):
            frappe.get_doc(
                {
                    "doctype": "Insights Data Source v3",
                    "title": frappe.unscrub(self.data_source),  # Insights names the source after it
                    "type": "Database",
                    "database_type": "DuckDB",
                    "database_name": self.data_source,
                }
            ).insert(ignore_permissions=True)
        self.register_tables()

    def add_derived_columns(self, db, table: TelemetryTable) -> None:
        """Adds the derived and delta columns a table created before them lacks, filled from its rows.

        One transaction: a failed backfill leaves the table without the columns, so the next
        load tries again.
        """

        existing = {
            name
            for (name,) in db.con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE table_name = $table", {"table": table.name}
            ).fetchall()
        }
        missing = [column for column in table.derived if column.name not in existing]
        missing_deltas = [column for column in table.deltas if column.name not in existing]
        if not missing + missing_deltas:
            return
        db.con.begin()
        for column in missing + missing_deltas:
            db.con.execute(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type}')
        if missing:
            assignments = ", ".join(f'"{column.name}" = {column.expression}' for column in missing)
            db.con.execute(f'UPDATE "{table.name}" SET {assignments}')
        if missing_deltas:
            self.fill_deltas(db, table, missing_deltas)
        db.con.commit()

    def fill_deltas(self, db, table: TelemetryTable, columns: list) -> None:
        """Computes delta columns over the whole table, as the loader computes them per chunk."""

        assignments = ", ".join(f'"{column.name}" = point."{column.name}"' for column in columns)
        deltas = ", ".join(f'{column.expression} AS "{column.name}"' for column in columns)
        db.con.execute(f"""
            WITH pushes AS (SELECT host_ip, timestamp, {PREVIOUS_PUSH} FROM (
                SELECT DISTINCT host_ip, timestamp FROM "{table.name}"
            ))
            UPDATE "{table.name}" SET {assignments}
            FROM (SELECT row_id, {deltas} FROM (
                SELECT rowid AS row_id, rows.*, previous_push, {PREVIOUS_POINT}
                FROM "{table.name}" AS rows JOIN pushes USING (host_ip, timestamp) {SERIES_WINDOW}
            )) AS point
            WHERE "{table.name}".rowid = point.row_id
        """)

    def load_pending(self) -> dict[str, int]:
        """Loads every staged file of every table, oldest first; returns the rows added per table.

        The tables are refreshed first, so a load never meets a table without its columns. Files
        go in chunks of BATCH_FILES, each under its own lock, so a dashboard query gets its turn
        between chunks and a backlog drains in one run. A table whose load fails (the lock timed
        out, the disk is full) keeps its files for the next run and is logged; the other tables
        still load.
        """

        try:
            self.refresh_tables()
        except Exception:
            log_exception("Telemetry tables not refreshed")
            return {}

        added = {}
        for table in TABLES:
            while files := self.get_loadable(table):
                try:
                    added[table.name] = added.get(table.name, 0) + self.load(table, files)
                except Exception:
                    log_exception(f"Telemetry load of {table.name} deferred")
                    break
                if len(files) < BATCH_FILES:
                    break
        try:
            self.refresh_current_metrics()
        except Exception:
            log_exception("Current metrics not refreshed")
        if added:
            self.register_tables()
        return added

    def refresh_tables(self) -> None:
        """Creates the telemetry tables, adds the derived columns an older one lacks, and rewrites
        the node and mail domain tables from Suite Cloud's records.

        The records are read before the lock is taken. Past a one-time backfill, the file is held
        only for the table checks and two small table swaps.
        """

        batches = {
            NODES: NODES.get_batch({}, get_node_rows()),
            MAIL_DOMAINS: MAIL_DOMAINS.get_batch({}, get_mail_domain_rows()),
        }
        with self.write_connection() as db:
            for table in (*TABLES, CURRENT_METRICS):
                db.raw_sql(table.create_statement)
                self.add_derived_columns(db, table)
            for table, batch in batches.items():
                db.con.register("dimension", batch)
                db.con.execute(f'CREATE OR REPLACE TABLE "{table.name}" AS SELECT * FROM dimension')
                db.con.unregister("dimension")

    def get_loadable(self, table: TelemetryTable) -> list[Path]:
        """The next chunk of staged files that read whole and carry the table's schema.

        Each file is read in full here, outside the lock: a damaged page found inside the insert
        would fail the whole chunk on every run, so it is moved aside instead.
        """

        files = []
        for path in self.spool.get_pending(table, BATCH_FILES):
            try:
                schema = parquet.read_table(path).schema
            except Exception as e:
                self.spool.reject(path, str(e))
                continue
            if not schema.equals(table.schema):
                self.spool.reject(path, f"Schema differs from {table.name}:\n{schema}")
                continue
            files.append(path)
        return files

    def load(self, table: TelemetryTable, files: list[Path]) -> int:
        """Inserts the files' rows in one statement and deletes the files once it is committed.

        The derived columns are computed here, once per row. A batch already in the table is
        skipped, so a run that died between the commit and the delete adds nothing twice. The
        lookup is bounded to the ids being loaded.
        """

        names = [*table.columns, *(column.name for column in table.derived)]
        values = [f'"{column}"' for column in table.columns]
        values += [f'{column.expression} AS "{column.name}"' for column in table.derived]
        incoming = (
            f"SELECT {', '.join(values)} FROM read_parquet($files) WHERE batch_id NOT IN "
            f'(SELECT batch_id FROM "{table.name}" WHERE batch_id IN (SELECT unnest($ids)))'
        )
        columns = ", ".join(f'"{name}"' for name in names)
        statement = (
            self.get_metrics_insert(names, incoming)
            if table is METRICS
            else f'INSERT INTO "{table.name}" ({columns}) {incoming}'
        )
        with self.write_connection() as db:
            result = db.con.execute(
                statement, {"files": [str(path) for path in files], "ids": [path.stem for path in files]}
            ).fetchone()
        self.spool.delete(files)
        return result[0]

    def get_metrics_insert(self, names: list[str], incoming: str) -> str:
        """The insert of new metric rows with their change since the previous point of the series.

        The stored rows a change can be taken against are the series' newest one before its first
        incoming row, looked up within a day, and any between its incoming rows, which only a late
        file has. The host's previous push is its incoming push before, or for its first incoming
        push its newest stored one within a day. Only the incoming rows are inserted.
        """

        columns = ", ".join(f'"{name}"' for name in names)
        stored_columns = ", ".join(f'stored."{name}"' for name in names)
        delta_names = ", ".join(f'"{column.name}"' for column in METRICS.deltas)
        deltas = ", ".join(f'{column.expression} AS "{column.name}"' for column in METRICS.deltas)
        return f"""
            WITH incoming AS ({incoming}),
            bounds AS (
                SELECT host_ip, name, labels, min(timestamp) AS first, max(timestamp) AS last
                FROM incoming GROUP BY ALL
            ),
            stored AS (
                SELECT {stored_columns} FROM "{METRICS.name}" AS stored JOIN bounds
                ON stored.host_ip = bounds.host_ip AND stored.name = bounds.name
                AND stored.labels IS NOT DISTINCT FROM bounds.labels
                WHERE stored.timestamp < bounds.last
                AND stored.timestamp >= (SELECT min(first) FROM bounds) - INTERVAL 1 DAY
                QUALIFY stored.timestamp > bounds.first OR row_number() OVER (
                    PARTITION BY stored.host_ip, stored.name, stored.labels, stored.timestamp > bounds.first
                    ORDER BY stored.timestamp DESC
                ) = 1
            ),
            points AS (SELECT *, true AS is_incoming FROM incoming UNION ALL SELECT *, false FROM stored),
            hosts AS (SELECT host_ip, min(first) AS first FROM bounds GROUP BY ALL),
            pushes AS (SELECT host_ip, timestamp, {PREVIOUS_PUSH} FROM (
                SELECT DISTINCT host_ip, timestamp FROM incoming
                UNION SELECT stored.host_ip, max(stored.timestamp) FROM "{METRICS.name}" AS stored
                JOIN hosts ON stored.host_ip = hosts.host_ip
                WHERE stored.timestamp < hosts.first
                AND stored.timestamp >= (SELECT min(first) FROM hosts) - INTERVAL 1 DAY
                GROUP BY ALL
            )),
            changes AS (
                SELECT points.*, previous_push, {PREVIOUS_POINT}
                FROM points LEFT JOIN pushes USING (host_ip, timestamp) {SERIES_WINDOW}
            )
            INSERT INTO "{METRICS.name}" ({columns}, {delta_names})
            SELECT {columns}, {deltas} FROM changes WHERE is_incoming
        """

    def refresh_current_metrics(self) -> None:
        """Rewrites current_metrics from the newest gauge readings of the nodes still pushing."""

        since = datetime.now(UTC).replace(tzinfo=None) - CURRENT_AFTER
        columns = ", ".join(f'"{column}"' for column in CURRENT_METRICS.columns)
        with self.write_connection() as db:
            db.con.begin()
            db.con.execute(f'DELETE FROM "{CURRENT_METRICS.name}"')
            db.con.execute(
                f'INSERT INTO "{CURRENT_METRICS.name}" ({columns}) '
                "SELECT host_ip, hostname, name, labels, arg_max(value, timestamp), max(timestamp) "
                f"FROM \"{METRICS.name}\" WHERE type = 'gauge' AND timestamp >= $since GROUP BY ALL",
                {"since": since},
            )
            db.con.commit()

    def prune_expired(self) -> None:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=self.retention_days)
        with self.write_connection() as db:
            for table in TABLES:
                db.raw_sql(table.create_statement)
                db.con.execute(f'DELETE FROM "{table.name}" WHERE timestamp < $cutoff', {"cutoff": cutoff})
            db.raw_sql("CHECKPOINT")

    def query(self, statement: str, parameters: dict) -> list[tuple]:
        """A read under the writer's lock: a job must leave no reader open, since one open in a
        worker would block the next load."""

        with self.write_connection() as db:
            return db.con.execute(statement, parameters).fetchall()

    def write_connection(self):
        return frappe.get_doc("Insights Data Source v3", self.data_source).write_connection()

    def register_tables(self) -> None:
        """Lists the tables in Insights so the query builder offers them; duplicates are ignored."""

        from insights.insights.doctype.insights_table_v3.insights_table_v3 import InsightsTablev3

        tables = (*TABLES, *DIMENSIONS, CURRENT_METRICS)
        InsightsTablev3.bulk_create(self.data_source, [table.name for table in tables])


def get_node_rows() -> list[dict]:
    """One row per address of every Stalwart Node and Egress Gateway, since a batch names its host
    by address. A gateway runs a Stalwart of its own that sends the cluster's mail, so the
    dashboards see it as a node of the cluster with the role ``gateway``. Both run in their
    cluster's region, which the rows carry as ``location``."""

    regions = dict(frappe.get_all("Stalwart Cluster", fields=["name", "region"], as_list=True))
    rows = []
    for node in frappe.get_all("Stalwart Node", fields=[*NODE_FIELDS, "role", "ipv6_address"]):
        rows += get_host_rows(node, node.role, regions, (node.ipv4_address, node.ipv6_address))
    for gateway in frappe.get_all("Egress Gateway", fields=NODE_FIELDS):
        rows += get_host_rows(gateway, "gateway", regions, (gateway.ipv4_address,))
    return rows


def get_host_rows(
    record: dict, role: str, regions: dict[str, str], addresses: tuple[str | None, ...]
) -> list[dict]:
    return [
        {
            "host_ip": address,
            "hostname": record.hostname,
            "cluster": record.cluster,
            "role": role,
            "location": regions.get(record.cluster),
            "status": record.status,
        }
        for address in addresses
        if address
    ]


def get_mail_domain_rows() -> list[dict]:
    titles = dict(frappe.get_all("Suite Tenant", fields=["name", "title"], as_list=True))
    return [
        {"domain": domain.name, "tenant": domain.tenant, "tenant_title": titles.get(domain.tenant)}
        for domain in frappe.get_all("Mail Domain", fields=["name", "tenant"])
    ]
