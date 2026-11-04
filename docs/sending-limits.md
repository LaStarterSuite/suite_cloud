# Sending limits

A Mail Account's **Daily Send Limit** and **Monthly Send Limit** count recipients per UTC day and month. Each is at least 1 and at most the cluster's **Max Daily Send Limit** and **Max Monthly Send Limit**. A new account starts at its tenant's **Default Daily Send Limit** and **Default Monthly Send Limit**, 2,000 and 10,000 unless an operator changes them. A tenant sets an account's limits through the API only while **Tenant May Set Send Limits** is on.

Suite Cloud enforces the limits. The code is in `suite_cloud/mail/sending_limits.py` (cache, check, flush), `suite_cloud/api/sending_limits.py` (the endpoint) and `suite_cloud/mail/cluster/quota.py` (the Stalwart config).

## Request flow

Stalwart calls an MTA Hook at the RCPT stage, once for each recipient of a signed-in sender. This covers SMTP submission and JMAP sends.

```http
POST /api/v2/method/suite_cloud.api.sending_limits.check
Authorization: Bearer <key>.<secret>
Content-Type: application/json

{"context": {"stage": "rcpt", "sasl": {"login": "jane@acme.example"}, ...}, "envelope": {"from": {...}, "to": [{"address": "..."}]}}
```

1. The `authenticate` auth hook takes the bearer as the calling node's Suite Cloud token (`suite_cloud/mail/cluster/server_token.py`). It reads the server of the key from the cache, `suite_cloud:server_token:<key>` for 5 minutes, and compares the SHA-256 of the secret. The caller's cluster is the node's. A gateway's token, an unknown key or a wrong secret is a 401, and the address is throttled. **Enable Telemetry** does not matter. A saved or deleted node or gateway clears its cache key after commit.
2. The endpoint reads the sender from `context.sasl.login`, stripped and in lowercase. The current recipient is the last entry of `envelope.to`.
3. It reads the account's cluster and limits from the cache, or from Mail Account on a miss.
4. One Lua script checks the day and month counters and counts the recipient as sent or refused.
5. It answers in Stalwart's own JSON, without Frappe's `{"data": ...}` envelope.

| Case | Answer |
| --- | --- |
| Under both limits, no login, no recipient, an address that is no Mail Account, or an account of another cluster | `{"action": "accept"}`. Only the first case is counted |
| Over the daily limit | `{"action": "reject", "response": {"status": 550, "enhanced_status": "5.4.5", "message": "Daily sending limit of 2,000 recipients reached for jane@acme.example. It resets at 2026-11-04 00:00 UTC."}}` |
| Over the monthly limit | The same, with the monthly limit and the 1st of the next month |
| A fault, such as Redis being unreachable | 500, logged to the `sending_limits` log, not to an Error Log. Stalwart accepts the recipient |

A recipient over a limit is refused. The other recipients of the same message that fit are accepted, so a message can go to part of its recipients. An SMTP client gets `550 5.4.5 <message>` for each refused recipient. A JMAP `EmailSubmission/set` succeeds even when every recipient is refused: Stalwart writes the refusal only to `EmailSubmission.deliveryStatus.<address>` (`delivered: "no"`, `smtpReply: "550 5.4.5 ..."`) and sends no bounce. A JMAP client must read `deliveryStatus` to show the refusal.

## Redis cache

All keys are in the site's Redis cache, under `frappe.cache.make_key()`. The code uses raw Redis calls, because Frappe's `get_value` and `set_value` pickle values and keep a copy per request.

| Key | Type | Content | TTL |
| --- | --- | --- | --- |
| `sending_limits:limit:<account>` | Hash | `cluster`, `daily`, `monthly`. An address that is no Mail Account has an empty `cluster` | 1 hour |
| `sending_limits:count:<account>:<period>` | Hash | `sent` and `refused` recipients. `<period>` is `d:YYYYMMDD` or `m:YYYYMM` | Day: 3 days. Month: 35 days. Set again on each write |
| `sending_limits:touched` | Set | `<account>\|<period>` of each counter written since the last flush | None |
| `sending_limits:touched:flushing` | Set | The touched set that a flush took | None |

The counter keys carry their day or month, so nothing resets them. Redis expires the old ones.

Mail Account clears its limit hash after commit when it is inserted, when its cluster or a limit changes, and when it is deleted. A limit change takes effect on the next recipient. A clear before commit would let another worker read the old row and cache it again. The 1-hour TTL covers a missed clear.

The cluster's max limits only cap the values a save accepts. They do not change enforcement.

## Counting and Mail Send Usage

When a counter is missing, the script asks for a seed. Suite Cloud reads the account's Mail Send Usage rows of the current day and month in one query and runs the script again with them, or with zeros. This happens once per account and period, and again after Redis loses a key.

`flush_usage()` runs every minute. It renames `touched` to `touched:flushing`, reads each counter in one pipeline, drops periods other than today, yesterday and their months, and writes the rows to Mail Send Usage in batches of 5,000. Then it deletes `touched:flushing`. A failed run leaves the set in place, and the next run writes it again. Mail Send Usage keeps the higher count, so a second write changes nothing.

The Mail Account form shows the stored totals: orange from 80% of a limit, red at 100%, with the refused recipients beside them. Suite Cloud keeps each account's day and month totals for **Sending Usage Retention (Days)** in Suite Cloud Settings (90 by default) and deletes older days, and months that ended before them, daily. Each day and month keeps the limit the account had while it was current. In Insights, the **Sending Usage** dashboard of the Stalwart Mail workbook shows sent and refused recipients for the latest UTC day, the accounts at or near a limit, the top senders and each account's daily history.

## Stalwart config

The cluster plan holds three operations, from `quota.cluster_operations()`:

| Operation | Purpose |
| --- | --- |
| `destroy` MtaHook with `url: http://127.0.0.1:8095/hooks/quota` | Removes the hook of the retired Suite Cloud Agent. It destroys nothing on a cluster without it. Remove it once every cluster pushed once |
| `upsert` MtaHook `sending-limits` | `enable` for signed-in sessions only, `stages: rcpt`, `timeout: 1000`, `tempFailOnError: false`, the calling node's `SUITE_CLOUD_TOKEN` as the bearer, and the header `X-Suite-Cloud-Hook: sending-limits`. It stays switched off until every Provisioned, Active and Draining node of the cluster has **Suite Cloud Token Installed**, since a reload that names a variable a running node lacks fails. The plan matches the hook on the header, so a **Public URL** change updates it in place |
| `upsert` MtaInboundThrottle `suite-cloud-quota-backstop` | Caps every signed-in account at the cluster's **Max Daily Send Limit** messages per UTC day, checked at MAIL FROM. Always on |

The hook URL uses **Public URL**, else the site's own URL, as the telemetry WebHooks do. A change reaches the cluster with the next Sync Config.

## Accepted losses

| Case | Effect |
| --- | --- |
| Suite Cloud down, slow (over 1 second) or failing | Stalwart accepts the recipient uncounted. The backstop caps each account at **Max Daily Send Limit** messages per UTC day. It counts messages, not recipients, so it is never stricter than an account's own limit. A deploy that restarts Suite Cloud opens a short gap |
| Redis evicts or loses counter keys | The counter is seeded again from Mail Send Usage. The counts since the last flush, up to 1 minute, are lost |
| A transaction ends after RCPT (RSET, a refusal at DATA, a disconnect) | Its recipients stay counted |
| Stalwart refuses a recipient after the hook accepted it, such as an unknown local address | The recipient stays counted. The hook runs before Stalwart's own recipient checks |
| A JMAP send over the limit | The submission succeeds at the API level. The user sees the refusal only if the client reads `deliveryStatus` |
| The same address appears twice in one message | It counts twice |

## Hosting load

Each recipient is one request to Suite Cloud. The check itself takes about 1 ms and reads no database row on a warm cache, but Frappe opens a database connection for each request. The cost per recipient is the network round trip from the node to Suite Cloud. Put Suite Cloud close to the clusters, serve the endpoint with keep-alive, and count the workers against the peak recipients per second of every cluster. Keep the RCPT reply p95 under 150 ms. Check `maxmemory` and `maxmemory-policy` of the production `redis_cache`: an evicted counter loses up to a minute of counts.

## Moving to the Suite Cloud token

The hook used to send the cluster's Quota Hook Token, and the WebHooks and OTLP sent `SUITE_CLOUD_TELEMETRY_TOKEN=<key>:<secret>`. Now every request sends the server's `SUITE_CLOUD_TOKEN=<key>.<secret>`. The patch `rename_suite_cloud_token` renames the fields to **Suite Cloud Key**, **Suite Cloud Secret** and **Suite Cloud Token Installed**, renames **Server Requests per Minute** to `server_requests_per_minute` and the Server Job callback to `after_install_token`, and deletes the clusters' Quota Hook Tokens. Each server is moved by hand, in this order, since a reload that names a variable a running server lacks fails:

1. Before the deploy, print each server's new line in `bench console`, with the field names of the old version:

   ```python
   for doctype in ("Stalwart Node", "Egress Gateway"):
       for name in frappe.get_all(doctype, pluck="name"):
           doc = frappe.get_doc(doctype, name)
           print(name, f"SUITE_CLOUD_TOKEN={doc.telemetry_key}.{doc.get_password('telemetry_secret')}")
   ```

2. On each server, add that line to `/etc/stalwart/stalwart.env`, keep the old line, and run `systemctl restart stalwart`, one server at a time.
3. Deploy and migrate.
4. Right away, **Sync Config** on each cluster and each Egress Gateway. Until then the old hook's token is refused, so Stalwart accepts recipients uncounted and only the backstop caps them, and the old telemetry token is refused: the WebHooks retry for up to **Webhook Discard After**, and the metrics of that window are lost.
5. Restart Stalwart on each server again, one at a time. OTLP reads its config only at start.
6. Later, delete the old `SUITE_CLOUD_TELEMETRY_TOKEN` line. It does no harm, and the next environment write from Suite Cloud drops it.
