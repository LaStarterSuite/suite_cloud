"""Rows from what Stalwart pushes on its own: WebHook events and OTLP metrics.

A value that does not parse skips its event or data point, never the request: Stalwart resends a
refused request until it gives up on it, so one bad event would cost the whole batch.
"""

import hashlib
import math
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import orjson
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

from suite_cloud.telemetry import ERROR_EVENTS, EVENT_LEVELS, WARN_EVENTS

EVENT_NAME_LIMIT = 128
SPAN_ID_LIMIT = 64
# What each WebHook lists. Right after a start Stalwart also hands every WebHook the events it
# logged before the WebHooks existed, which would land on a stream that does not list them.
STREAM_EVENTS = {
    "events": frozenset(EVENT_LEVELS),
    "errors": frozenset(ERROR_EVENTS),
    "warnings": frozenset(WARN_EVENTS),
}
LEVELS = {**dict.fromkeys(ERROR_EVENTS, "error"), **dict.fromkeys(WARN_EVENTS, "warn"), **EVENT_LEVELS}


class Skipped(ValueError):
    """An event or data point left out of its batch."""


def get_batch_id(server: str, events: list) -> str:
    """The same request sent again gets the same id, so its staged file is replaced and the
    warehouse loads it once."""

    ids = [event.get("id") if isinstance(event, dict) else None for event in (events[0], events[-1])]
    return str(uuid5(NAMESPACE_URL, f"{server}:{ids[0]}:{ids[1]}:{len(events)}"))


def get_metrics_batch_id(server: str, body: bytes) -> str:
    """A push sent again gets the same id, as a WebHook batch does."""

    return str(uuid5(NAMESPACE_URL, f"{server}:{hashlib.sha256(body).hexdigest()}"))


def get_event_rows(events: list, stream: str) -> tuple[list[dict], list[str]]:
    """``{id, createdAt, type, data}`` each. The events stream carries the listed events at the
    level Stalwart logs them; the errors and warnings streams every other event logged at error
    or warn level. An event the stream does not list is left out."""

    rows, problems = [], []
    for index, event in enumerate(events):
        try:
            row = parse_event(event)
        except Skipped as problem:
            problems.append(f"events[{index}]: {problem}")
            continue
        if row["event"] in STREAM_EVENTS[stream]:
            rows.append(row)
    return rows, problems


def parse_event(event: object) -> dict:
    if not isinstance(event, dict):
        raise Skipped("not an object")
    name = event.get("type")
    if not isinstance(name, str) or not 0 < len(name) <= EVENT_NAME_LIMIT:
        raise Skipped(f"type must be 1 to {EVENT_NAME_LIMIT} characters")
    data = event.get("data") or {}
    if not isinstance(data, dict):
        raise Skipped("data must be an object")
    data = dict(data)
    span_id = data.pop("spanId", None)
    span_id = None if span_id is None else str(span_id)[:SPAN_ID_LIMIT]
    return {
        "timestamp": parse_timestamp(event.get("createdAt")),
        "level": LEVELS.get(name, "error"),
        "event": name,
        "span_id": span_id,
        "data": orjson.dumps(data).decode(),
    }


def parse_timestamp(value: object) -> datetime:
    """An RFC 3339 instant with its offset, stored as naive UTC."""

    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise Skipped("createdAt must be an RFC 3339 timestamp") from None
    if moment.tzinfo is None:
        raise Skipped("createdAt must carry a UTC offset")
    return moment.astimezone(UTC).replace(tzinfo=None)


def get_metric_rows(body: bytes) -> tuple[list[dict], int]:
    """Rows from an OTLP ``ExportMetricsServiceRequest`` and the number of data points skipped.

    Stalwart sends cumulative sums and histograms, so a sum's value is its running total and a
    histogram's value is its mean since Stalwart started, as the agent sent them.
    """

    request = ExportMetricsServiceRequest()
    request.ParseFromString(body)
    rows, skipped = [], 0
    for resource in request.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points, row_type = get_points(metric)
                for point in points:
                    try:
                        rows.append({"name": metric.name, **parse_point(point, row_type)})
                    except Skipped:
                        skipped += 1
    return rows, skipped


def get_points(metric) -> tuple[list, str | None]:
    """The metric's data points and the row type they map to; None for the kinds Suite Cloud does
    not store (exponential histograms, summaries)."""

    kind = metric.WhichOneof("data")
    points = list(getattr(metric, kind).data_points) if kind else []
    return points, {"sum": "counter", "gauge": "gauge", "histogram": "histogram"}.get(kind)


def parse_point(point, row_type: str | None) -> dict:
    if row_type is None:
        raise Skipped("unsupported point type")
    row = {
        "timestamp": datetime.fromtimestamp(point.time_unix_nano / 1e9, UTC).replace(tzinfo=None),
        "type": row_type,
        "count": None,
        "sum": None,
        "labels": get_labels(point.attributes),
    }
    if row_type == "histogram":
        row["count"] = point.count
        row["sum"] = point.sum
        row["value"] = point.sum / point.count if point.count else 0.0
    else:
        row["value"] = point.as_double if point.WhichOneof("value") == "as_double" else float(point.as_int)
    if not math.isfinite(row["value"]):
        raise Skipped("value must be finite")
    return row


def get_labels(attributes) -> str | None:
    labels = {attribute.key: get_value(attribute.value) for attribute in attributes}
    return orjson.dumps(labels).decode() if labels else None


def get_value(value: AnyValue):
    kind = value.WhichOneof("value")
    if kind in ("string_value", "bool_value", "int_value", "double_value"):
        return getattr(value, kind)
    return str(getattr(value, kind)) if kind else None
