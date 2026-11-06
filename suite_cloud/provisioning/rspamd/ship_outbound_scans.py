"""Sends the outbound scan records Rspamd wrote to Suite Cloud; suite-cloud-scans.timer runs it each
minute. Installed by install-rspamd.yml with the Python standard library only.

A run with nothing to send posts an empty batch, so the node form shows that the shipper works
while nobody sends mail. A file that fails to send stays for the next run. The same batch sent
again carries the same records in the same order, so Suite Cloud gives it the same id and stores
it once.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SPOOL = Path("/var/lib/rspamd/suite-cloud-scans.jsonl")  # scan_recorder.lua appends here
PENDING = Path("/var/lib/rspamd/suite-cloud-scans.pending")  # same file system, so a move is a rename
BATCH_SIZE = 2000  # records per request; Suite Cloud takes at most 5000
MAX_BACKLOG_BYTES = 100 * 1024 * 1024  # past this the oldest files go, while Suite Cloud refuses
WRITER_GRACE_SECONDS = 2  # a process that opened the spool before the move finishes its line
REQUEST_TIMEOUT_SECONDS = 30


def main() -> int:
    url, token = os.environ.get("SUITE_CLOUD_SCANS_URL"), os.environ.get("SUITE_CLOUD_TOKEN")
    if not (url and token):
        print("SUITE_CLOUD_SCANS_URL or SUITE_CLOUD_TOKEN is not set; nothing sent", file=sys.stderr)
        return 0
    PENDING.mkdir(mode=0o700, exist_ok=True)
    take_spool()
    trim_backlog()
    files = sorted(PENDING.glob("*.jsonl"))
    if not files:
        return 0 if post(url, token, []) else 1
    for path in files:
        if not send_file(path, url, token):
            return 1
        path.unlink()
    return 0


def take_spool() -> None:
    if SPOOL.exists() and SPOOL.stat().st_size:
        os.replace(SPOOL, PENDING / f"{time.time_ns()}.jsonl")
        time.sleep(WRITER_GRACE_SECONDS)


def trim_backlog() -> None:
    files = sorted(PENDING.glob("*.jsonl"))
    total = sum(path.stat().st_size for path in files)
    while files and total > MAX_BACKLOG_BYTES:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink()
        print(f"Backlog over {MAX_BACKLOG_BYTES} bytes: dropped {oldest.name}", file=sys.stderr)


def send_file(path: Path, url: str, token: str) -> bool:
    records = get_records(path)
    for start in range(0, len(records), BATCH_SIZE):
        if not post(url, token, records[start : start + BATCH_SIZE]):
            return False
    return True


def get_records(path: Path) -> list[dict]:
    """The file's records; a line cut short by a crash is skipped."""

    records = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def post(url: str, token: str, records: list[dict]) -> bool:
    body = json.dumps({"scans": records}, separators=(",", ":"), sort_keys=True).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
            return True
    except (urllib.error.URLError, OSError) as error:
        print(f"Suite Cloud did not take {len(records)} records: {error}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(main())
