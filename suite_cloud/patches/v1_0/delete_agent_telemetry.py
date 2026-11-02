import frappe

from suite_cloud.telemetry import TABLES
from suite_cloud.telemetry.spool import Spool
from suite_cloud.telemetry.warehouse import DATA_SOURCE, Warehouse

AGENT_BATCH = "substr(batch_id, 15, 1) = '7'"  # the agents named batches with UUIDv7, Stalwart's are UUIDv5


def execute() -> None:
    """Stalwart pushes its own events and metrics now. The agents' rows came from its log files and
    the machine, so they would mix with the pushed ones in every chart: they go, staged or loaded."""

    spool = Spool()
    for table in TABLES:
        for path in spool.get_directory(table).glob("*.parquet"):
            if path.stem[14:15] == "7":
                path.unlink()
    if not frappe.db.exists("Insights Data Source v3", DATA_SOURCE):
        return
    with Warehouse().write_connection() as db:
        for table in TABLES:
            db.raw_sql(table.create_statement)
            db.con.execute(f'DELETE FROM "{table.name}" WHERE {AGENT_BATCH}')
        db.raw_sql("CHECKPOINT")
