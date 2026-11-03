# Telemetry

Module `suite_cloud.api.telemetry`. Each Stalwart node and egress gateway pushes its own events through a WebHook and its own metrics over OTLP. Suite Cloud stages every accepted batch as a parquet file, copies the files into Frappe Insights' DuckDB every five minutes and deletes rows older than the retention in **Suite Cloud Settings > Telemetry** daily. Operators read the data in the Insights dashboards Suite Cloud ships. The loader copies the event data keys the dashboards read (`remoteIp`, `listenerId`, `domain`, `hostname`, `details` or `reason`, `code`, `accountName` or `accountId`, `from`, `to`, `elapsed`) into columns of the `events` table, with the SMTP code and the sender and recipient domains, so a chart does not parse the JSON of every row. Each dashboard opens on yesterday and today. The DMARC and TLS dashboards, which read daily reports, open on the last week.

| Method | Verb | Caller | Purpose |
| --- | --- | --- | --- |
| `telemetry.webhook` | POST | Stalwart | Stages a batch of events |
| `telemetry.otlp` | POST | Stalwart | Stages one OTLP metrics push |

Stalwart's sending-limit hook, `suite_cloud.api.sending_limits.check`, goes through the same auth hook with its cluster's Quota Hook Token. See [Sending limits](../sending-limits.md).

## Authentication

Every Stalwart Node and Egress Gateway has its own **Telemetry Key** and **Telemetry Secret**, made when the record is created. Provisioning writes them to the server's `/etc/stalwart/stalwart.env`:

```sh
SUITE_CLOUD_TELEMETRY_TOKEN=<telemetry key>:<telemetry secret>
```

The WebHooks and the OTLP push read the variable on the server that sends, so each request names its one server:

```http
Authorization: Bearer <telemetry key>:<telemetry secret>
```

The token opens only `webhook` and `otlp`, and runs as a user without any role. The server record gives every row its `source` (`stalwart`), `host_ip` (the server's IPv4 address) and `hostname`. The request carries none of them. With **Enable Telemetry** off, every request gets a 401. **Rotate Telemetry Token** on a node or gateway form replaces the secret at once and queues a job that writes it to the server and restarts Stalwart. Until the job ends, Stalwart sends the old token, gets a 401 and tries again.

## WebHook

Stalwart sends every event waiting for a WebHook in one request, at most one request every 30 seconds per WebHook. The warehouse loads every five minutes, so more frequent requests would only stage more small files:

```http
POST /api/v2/method/suite_cloud.api.telemetry.webhook?stream=events
Authorization: Bearer <telemetry key>:<telemetry secret>
Content-Type: application/json

{"events": [{"id": "...", "createdAt": "2026-10-20T09:59:00Z", "type": "delivery.delivered", "data": {"queueId": 1234, "remoteIp": "198.51.100.7", "spanId": 42}}]}
```

Each server has three WebHooks:

| `stream` | Sends | Row level |
| --- | --- | --- |
| `events` | The events the Stalwart workbook and the alerts read: `WEBHOOK_EVENTS` in `suite_cloud/telemetry/__init__.py` | The level Stalwart logs the event at, from `EVENT_LEVELS` beside the list |
| `errors` | Every other event Stalwart logs at error level: `ERROR_EVENTS`, taken from `EventType::level()` in Stalwart's `trc` crate | `error` |
| `warnings` | Every other event Stalwart logs at warn level: `WARN_EVENTS`, from the same table | `warn` |

Stalwart ignores the `level` of a WebHook. Only its event list decides what it sends, so every WebHook uses `eventsPolicy: include`. Each WebHook has its own queue, so a burst of warnings, such as rate-limit events during an attack, does not hold back the errors. Stalwart never hands `telemetry.webhook-error` to a WebHook, so `WARN_EVENTS` leaves it out. A new Stalwart release can add error or warn events, so check `ERROR_EVENTS` and `WARN_EVENTS` when you upgrade. Right after a start, Stalwart also hands every WebHook the events it logged before the WebHooks existed. Suite Cloud keeps only the events on the stream's own list.

An event becomes one row: `timestamp` from `createdAt`, `event` from `type`, `span_id` from `data.spanId` and `data` without `spanId`. The event names and the `data` keys are the ones Stalwart writes in its log file, so the dashboards read them as before. An event that does not parse is skipped. The request still succeeds, and one Error Log per request counts the skipped events. A refused request would make Stalwart send the whole batch again.

The answer is `{"data": {"accepted": 2, "skipped": 0}}`. The batch id is a UUIDv5 derived from the server, the first and last event ids and the event count. The same request sent again replaces its staged file, and the warehouse loads a batch once. Stalwart does not send a failed batch again on its own, though: it adds the newer events, so the retry gets a new id. A request that times out after Suite Cloud staged it therefore stores its events twice. Suite Cloud answers in milliseconds, far inside the 30-second timeout, so this takes a stalled request.

## OTLP

Stalwart pushes its metrics every **Metrics Push Interval (Seconds)**, 60 by default, as a protobuf `ExportMetricsServiceRequest` (`Content-Type: application/x-protobuf`, no compression). Suite Cloud answers 200 with an empty body.

| OTLP point | Row |
| --- | --- |
| Sum | `type` `counter`, `value` the running total since Stalwart started |
| Gauge | `type` `gauge`, `value` the reading |
| Histogram | `type` `histogram`, `value` the mean (`sum / count`), with `count` and `sum` |
| Exponential histogram, summary | Skipped and counted in an Error Log |

`timestamp` comes from the point's `time_unix_nano` and `labels` from its attributes. The batch id is a UUIDv5 of the server and the body, so a push sent again is loaded once. The metric names are Stalwart's own, such as `server.memory`, `queue.count` and `http.request-time`. Suite Cloud keeps no percentiles.

Counters and histograms are running totals, so the loader adds two columns to each `metrics` row: `increase`, the change of a counter's `value` or a histogram's `sum` since the previous point of its series, and `samples_added`, the change of a histogram's `count`. A series is one `host_ip`, `name` and `labels`. The previous point is the newest stored or loaded row of the series with an earlier `timestamp`. Stalwart pushes a counter or histogram once it is above zero and then on every push, so a series the server's previous push lacked was zero then, and its first point's totals are its change. Only a server's first push gets NULL, as does every gauge. A total that fell means Stalwart restarted, so the point's own totals are its change. A restart whose first total is above the old one is missed, which is rare at one push a minute. A late file gets its change against the stored row before it, and the row after it keeps its own. A chart sums `increase` per period, and a histogram's mean over a period is `sum(increase) / sum(samples_added)`. Stalwart's timings are in milliseconds; it counts `delivery.total-time` in whole seconds. The Community edition, which the clusters run, leaves `queue.count` and the ingest, indexing, DNS and store timings out of its push. The Performance dashboard reads the ingest and DNS timings from the `elapsed` of events instead: `message-ingest.ham` and `message-ingest.spam`, and `delivery.mx-lookup` and `delivery.ip-lookup`, which Stalwart logs at debug level. The queue size, the indexing time and the store timings have no event in 0.16.27, so the workbook leaves them out. A table loaded before these columns existed gets them filled on the next load.

After each load, `current_metrics` holds the newest value of every gauge of each server that pushed in the last 10 minutes. The current-value cards read it and take the Host filter but not the Date Range. They lag Stalwart by up to about six minutes: a 60-second push plus the five-minute load. `METRIC_NAMES` in `suite_cloud/telemetry/__init__.py` lists the metrics the workbook reads.

## Errors

| Status | Type | Meaning |
| --- | --- | --- |
| 400 | `InvalidInputError` | A `stream` other than `events`, `errors` or `warnings`, more than 65,536 events, or a body that is not an OTLP metrics request |
| 401 | `AuthenticationError` | The key or secret is wrong, or telemetry is off |
| 403 | `PermissionError` | A verb other than POST |
| 413 | | The body is larger than the site's `max_file_size`, 25 MB by default |
| 429 | `TooManyRequestsError` | The server sent more requests in the current minute than **Server Requests per Minute** allows |
| 500 | | The batch could not be written, for example because the disk is full. Nothing was staged |

**Server Requests per Minute** (300 by default) counts every request of one server in fixed one-minute windows: WebHooks and OTLP together. Three WebHooks and the metrics push make at most 7 requests a minute. The sending-limit hook is not throttled.

## Agent data

The agents shipped Stalwart's log lines and the machine's metrics until Stalwart pushed its own. Their batch ids were UUIDv7. The patch `delete_agent_telemetry` deletes those rows, loaded or staged, so they do not mix with the pushed ones. The `host.*` metrics and the `.p50`, `.p95` and `.p99` gauges went with them.

## Losses

Stalwart keeps undelivered events in memory and tries again every 30 seconds until **Webhook Discard After (Minutes)**, 10 by default, which covers a Suite Cloud deploy. A retry is never split: Stalwart sends the failed events again together with every newer one, so the request keeps growing until events pass the discard time. A batch of more than 65,536 events, or a body over the site's `max_file_size`, is refused, and so is every retry after it until enough events are discarded. A short discard time keeps that backlog small. Events are lost when Stalwart restarts (up to 30 seconds of them), when Suite Cloud is down for longer than the discard time, or while the backlog is too large to accept. A change to the metrics interval, the URL or the token takes effect when Stalwart restarts. The log files on each server, kept for 14 days, stay the source of truth for debugging.

## Alerts

Every five minutes Suite Cloud checks the loaded data and keeps a **Telemetry Alert** record per condition, open until it clears:

| Alert | Condition |
| --- | --- |
| Silent Host | A server that reported in the last day sent nothing for **Silent Host After (Minutes)**, 30 by default. OTLP pushes every interval even on an idle server. The alert stays open until a newer batch proves the server is back, even once the retention has deleted its last rows. Resolve it by hand when the server was taken out of service. It then stays resolved until the server reports again |
| Error Rate | A server logged at least **Errors per Hour Alert** error events in the last hour, 100 by default. Stalwart pushes only the listed events, so a share of all events would mean nothing |

**Telemetry Alert Recipients** get a mail when an alert opens and when it resolves. With no recipients, the records are kept and nobody is mailed.
