"""The staging directory: one parquet file per accepted batch, until the warehouse loads it."""

import os
from pathlib import Path
from uuid import uuid7

import pyarrow
import pyarrow.parquet as parquet
from frappe.utils import get_files_path

from suite_cloud.telemetry import TelemetryTable
from suite_cloud.utils import log_error


class Spool:
    """Files land under ``private/files/telemetry/<table>/<batch id>.parquet``.

    Every request writes its own file, so web workers never share a file or a lock; the loader
    reads whole files only, since a batch is visible only once its temporary file is renamed.
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root or get_files_path("telemetry", is_private=1))

    def get_directory(self, table: TelemetryTable) -> Path:
        return self.root / table.name

    def write(self, table: TelemetryTable, batch: pyarrow.Table, batch_id: str) -> Path:
        """Stages one batch; the same batch id written again replaces the earlier file."""

        directory = self.get_directory(table)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.parquet"
        temporary = directory / f"{batch_id}.{uuid7().hex}.tmp"
        try:
            parquet.write_table(batch, temporary, compression="zstd")
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return path

    def get_pending(self, table: TelemetryTable, limit: int) -> list[Path]:
        """The oldest staged files first. A WebHook batch id is derived from its events, so names
        do not sort by arrival; the write time does."""

        staged = self.get_directory(table).glob("*.parquet")
        return sorted(staged, key=lambda path: path.stat().st_mtime)[:limit]

    def reject(self, path: Path, reason: str) -> None:
        """Moves a file the loader cannot read out of the way, so one bad batch blocks nothing."""

        rejected = self.root / "rejected" / path.parent.name
        rejected.mkdir(parents=True, exist_ok=True)
        path.replace(rejected / path.name)
        log_error(title=f"Telemetry batch {path.stem} rejected", message=f"{reason}\nMoved to {rejected}")

    def delete(self, paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)
