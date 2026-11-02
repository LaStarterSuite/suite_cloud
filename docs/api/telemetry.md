# Telemetry

Module `suite_cloud.api.telemetry`. Each Stalwart node and egress gateway pushes its own events through a WebHook and its own metrics over OTLP. Suite Cloud stages every accepted batch as a parquet file, copies the files into Frappe Insights' DuckDB every five minutes and deletes rows older than the retention in **Suite Cloud Settings > Agent & Telemetry** daily. Operators read the data in the Insights dashboards Suite Cloud ships. The loader copies the event data keys the dashboards read (`remoteIp`, `listenerId`, `domain`, `hostname`, `details` or `reason`, `code`, `accountName` or `accountId`, `from`, `to`, `elapsed`) into columns of the `events` table, with the SMTP code and the sender and recipient domains, so a chart does not parse the JSON of every row. Each dashboard opens on yesterday and today. The DMARC and TLS dashboards, which read daily reports, open on the last week.

| Method | Verb | Caller | Purpose |
| --- | --- | --- | --- |
| `telemetry.webhook` | POST | Stalwart | Stages a batch of events |
| `telemetry.otlp` | POST | Stalwart | Stages one OTLP metrics push |
| `telemetry.quota.limits` | POST | Agent | A page of the caller's cluster's sending limits |
| `telemetry.quota.usage` | POST | Agent | Stores the recipients the caller counted per account |

## Authentication

Every Stalwart Node and Egress Gateway has its own **Telemetry Key** and **Telemetry Secret**, made when the record is created. Provisioning writes them to the server's `/etc/stalwart/stalwart.env`:

```sh
SUITE_CLOUD_TELEMETRY_TOKEN=<telemetry key>:<telemetry secret>
```

The WebHooks and the OTLP push read the variable on the server that sends, so each request names its one server:

```http
Authorization: Bearer <telemetry key>:<telemetry secret>
```

The token opens only `webhook` and `otlp`. The Agent sends its cluster's **Agent Key** and **Agent Secret** in the same header, and that token opens only the sending-limit methods. Both run as a user without any role. The server record gives every row its `source` (`stalwart`), `host_ip` (the server's IPv4 address) and `hostname`. The request carries none of them. With **Enable Telemetry** off, every request gets a 401. **Rotate Telemetry Token** on a node or gateway form replaces the secret at once and queues a job that writes it to the server and restarts Stalwart. Until the job ends, Stalwart sends the old token, gets a 401 and tries again.

## WebHook

Stalwart sends events in batches of up to 65,536, at most one request every 5 seconds per WebHook:

```http
POST /api/v2/method/suite_cloud.api.telemetry.webhook?stream=events
Authorization: Bearer <telemetry key>:<telemetry secret>
Content-Type: application/json

{"events": [{"id": "...", "createdAt": "2026-10-20T09:59:00Z", "type": "delivery.delivered", "data": {"queueId": 1234, "remoteIp": "198.51.100.7", "spanId": 42}}]}
```

Each server has two WebHooks:

| `stream` | Sends | Row level |
| --- | --- | --- |
| `events` | The events the Stalwart workbook and the alerts read: `WEBHOOK_EVENTS` in `suite_cloud/telemetry/__init__.py` | The level Stalwart logs the event at, from `EVENT_LEVELS` beside the list |
| `errors` | Every other event Stalwart logs at error level: `ERROR_EVENTS`, taken from `EventType::level()` in Stalwart's `trc` crate | `error` |

Stalwart ignores the `level` of a WebHook. Only its event list decides what it sends, so both WebHooks use `eventsPolicy: include`. A new Stalwart release can add error events, so check `ERROR_EVENTS` when you upgrade. Right after a start, Stalwart also hands every WebHook the events it logged before the WebHooks existed. Suite Cloud keeps only the events on the stream's own list.

An event becomes one row: `timestamp` from `createdAt`, `event` from `type`, `span_id` from `data.spanId` and `data` without `spanId`. The event names and the `data` keys are the ones Stalwart writes in its log file, so the dashboards read them as before. An event that does not parse is skipped. The request still succeeds, and one Error Log per request counts the skipped events. A refused request would make Stalwart send the whole batch again.

The answer is `{"data": {"accepted": 2, "skipped": 0}}`. The batch id is a UUIDv5 derived from the server, the first and last event ids and the event count. A batch sent again replaces its staged file, and the warehouse loads a batch once.

## OTLP

Stalwart pushes its metrics every **Metrics Push Interval (Seconds)**, 60 by default, as a protobuf `ExportMetricsServiceRequest` (`Content-Type: application/x-protobuf`, no compression). Suite Cloud answers 200 with an empty body.

| OTLP point | Row |
| --- | --- |
| Sum | `type` `counter`, `value` the running total since Stalwart started |
| Gauge | `type` `gauge`, `value` the reading |
| Histogram | `type` `histogram`, `value` the mean (`sum / count`), with `count` and `sum` |
| Exponential histogram, summary | Skipped and counted in an Error Log |

`timestamp` comes from the point's `time_unix_nano` and `labels` from its attributes. The batch id is a UUIDv5 of the server and the body, so a push sent again is loaded once. The metric names are Stalwart's own, such as `server.memory`, `queue.count` and `http.request-time`. Suite Cloud keeps no percentiles.

## Errors

| Status | Type | Meaning |
| --- | --- | --- |
| 400 | `InvalidInputError` | A `stream` other than `events` or `errors`, more than 65,536 events, or a body that is not an OTLP metrics request |
| 401 | `AuthenticationError` | The key or secret is wrong, or telemetry is off |
| 403 | `PermissionError` | A verb other than POST, or a token for the other kind of method |
| 413 | | The body is larger than the site's `max_file_size`, 25 MB by default |
| 429 | `TooManyRequestsError` | The server sent more requests in the current minute than **Agent Requests per Minute** allows |
| 500 | | The batch could not be written, for example because the disk is full. Nothing was staged |

**Agent Requests per Minute** (300 by default) counts every request of one server in fixed one-minute windows: WebHooks, OTLP and the Agent's sending-limit calls together. Two WebHooks and the metrics push make about 25 requests a minute.

## Agent data

The agents shipped Stalwart's log lines and the machine's metrics until Stalwart pushed its own. Their batch ids were UUIDv7. The patch `delete_agent_telemetry` deletes those rows, loaded or staged, so they do not mix with the pushed ones. The `host.*` metrics and the `.p50`, `.p95` and `.p99` gauges went with them.

## Losses

Stalwart keeps undelivered events in memory and tries again until **Webhook Discard After (Minutes)**, 60 by default, which covers a Suite Cloud deploy. Events are lost when Stalwart restarts, when Suite Cloud is down for longer than that, or when more than 65,536 events wait. A change to the metrics interval, the URL or the token takes effect when Stalwart restarts. The log files on each server, kept for 14 days, stay the source of truth for debugging.

## Sending limits

The Agent on a Stalwart node uses two methods to enforce the [sending limits](../agent.md#sending-limits). The caller only reads and reports the accounts of its own cluster, and `host_ip` must be one of that cluster's Active or Draining Stalwart Nodes. Any other address is a 403. Both calls count against **Agent Requests per Minute**.

`telemetry.quota.limits` takes `host_ip` and `since_version`, and answers the limits changed after it, oldest first, at most 500 per page:

```json
{"data": {"limits": [{"account": "jane@acme.example", "daily": 500, "monthly": 10000, "version": "2026-10-26 10:15:02.123456"}], "next_version": "<cursor>", "has_more": false}}
```

`daily` and `monthly` are recipients per UTC day and month. 0 means no limit. `version` is the account's modified time, so an older entry never replaces a newer one. Send `next_version` as the next `since_version`, and an empty one to read everything again. A save is served once it is 5 seconds old.

`telemetry.quota.usage` takes `host_ip` and `records`, at most 5,000, each `{"account", "period", "sent", "refused"}`. `period` is `d:YYYYMMDD` or `m:YYYYMM`, `sent` the recipients counted so far and `refused` the messages refused over a limit, 0 when left out. Suite Cloud keeps the higher of the stored and the reported count, because several nodes report the same counter and it only grows within its period. Accounts of other clusters, and periods other than today, yesterday and their months, are dropped. It answers `{"data": {"accepted": n}}`.

## Alerts

Every five minutes Suite Cloud checks the loaded data and keeps a **Telemetry Alert** record per condition, open until it clears:

| Alert | Condition |
| --- | --- |
| Silent Host | A server that reported in the last day sent nothing for **Silent Host After (Minutes)**, 30 by default. OTLP pushes every interval even on an idle server. The alert stays open until a newer batch proves the server is back, even once the retention has deleted its last rows. Resolve it by hand when the server was taken out of service. It then stays resolved until the server reports again |
| Error Rate | A server logged at least **Errors per Hour Alert** error events in the last hour, 100 by default. Stalwart pushes only the listed events, so a share of all events would mean nothing |

**Telemetry Alert Recipients** get a mail when an alert opens and when it resolves. With no recipients, the records are kept and nobody is mailed.
