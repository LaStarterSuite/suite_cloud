"""Rows from the outbound scan records each node's Rspamd writes (``scan_recorder.lua``) and its
shipper sends (``ship_outbound_scans.py``).

A record that does not parse is skipped, never the request: the shipper sends a refused batch
again on every run until its backlog drops it.
"""

import hashlib
import math
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import orjson
from frappe.utils import flt

from suite_cloud.mail.cluster.rspamd import SCAN_FIELDS
from suite_cloud.telemetry.push import Skipped
from suite_cloud.utils import get_config

ADDRESS_LIMIT = 320
TEXT_LIMIT = 998  # a header line's limit; Message-IDs are shorter in practice
SHORT_LIMIT = 64
SYMBOL_LIMIT = 200  # rules per message; Rspamd matches a few dozen


def get_batch_id(server: str, scans: list) -> str:
    """The shipper sends a failed file again in the same order, so it gets the same id."""

    digest = hashlib.sha256(orjson.dumps(scans, option=orjson.OPT_SORT_KEYS)).hexdigest()
    return str(uuid5(NAMESPACE_URL, f"{server}:outbound-scans:{digest}"))


def get_scan_rows(scans: list) -> tuple[list[dict], list[dict], list[str]]:
    """The scan rows, the matched-rule rows and a problem per skipped record."""

    mode, score = get_config(SCAN_FIELDS)
    settings = {"mode": mode, "reject_score": flt(score) if mode != "Off" else None}
    rows, symbol_rows, problems = [], [], []
    for index, scan in enumerate(scans):
        try:
            row, symbols = parse_scan(scan)
        except Skipped as problem:
            problems.append(f"scans[{index}]: {problem}")
            continue
        rows.append({**row, **settings, "symbols": orjson.dumps(symbols).decode()})
        symbol_rows.extend(get_symbol_rows(row, symbols))
    return rows, symbol_rows, problems


def parse_scan(scan: object) -> tuple[dict, dict[str, float]]:
    if not isinstance(scan, dict):
        raise Skipped("not an object")
    account = get_text(scan, "user", ADDRESS_LIMIT)
    action = get_text(scan, "action", SHORT_LIMIT)
    if not (account and action):
        raise Skipped("user and action must be set")
    row = {
        "timestamp": parse_time(scan.get("time")),
        "account": account.lower(),
        "envelope_from": get_text(scan, "envelope_from", ADDRESS_LIMIT),
        "header_from": get_text(scan, "header_from", ADDRESS_LIMIT),
        "message_id": get_text(scan, "message_id", TEXT_LIMIT),
        "queue_id": get_text(scan, "queue_id", SHORT_LIMIT),
        "client_ip": get_text(scan, "client_ip", SHORT_LIMIT),
        "recipients": get_count(scan.get("recipients")),
        "size": get_count(scan.get("size")),
        "action": action,
        "score": get_number(scan.get("score"), "score"),
        "scan_time": get_number(scan.get("scan_time"), "scan_time", required=False),
    }
    return row, get_symbols(scan.get("symbols"))


def get_symbol_rows(row: dict, symbols: dict[str, float]) -> list[dict]:
    shared = {key: row[key] for key in ("timestamp", "account", "message_id", "action")}
    return [
        {**shared, "message_score": row["score"], "symbol": symbol, "score": score}
        for symbol, score in symbols.items()
    ]


def parse_time(value: object) -> datetime:
    """Unix seconds, stored as naive UTC."""

    seconds = get_number(value, "time")
    try:
        return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        raise Skipped("time is out of range") from None


def get_text(scan: dict, key: str, limit: int) -> str | None:
    value = scan.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise Skipped(f"{key} must be a string")
    return value[:limit]


def get_number(value: object, key: str, required: bool = True) -> float | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Skipped(f"{key} must be a finite number")
    return float(value)


def get_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return max(int(value), 0)


def get_symbols(value: object) -> dict[str, float]:
    """``{rule: score}``; Rspamd's JSON writes an empty table as a list."""

    if value in (None, []):
        return {}
    if not isinstance(value, dict):
        raise Skipped("symbols must be an object")
    symbols = {}
    for name, score in list(value.items())[:SYMBOL_LIMIT]:
        if isinstance(score, int | float) and not isinstance(score, bool) and math.isfinite(score):
            symbols[str(name)[: SHORT_LIMIT * 2]] = flt(score, 4)
    return symbols
