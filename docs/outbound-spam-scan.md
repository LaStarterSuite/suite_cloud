# Outbound spam scan

Suite Cloud scans each message that a signed-in Mail Account sends, before Stalwart queues it. A message that scores as spam is refused while the client is still connected. The scan is on for every Mail Account. A System Manager can turn it off for one trusted account with **Skip Outbound Spam Scan**. Inbound mail is not affected: Stalwart's built-in spam filter scans it, as before.

Stalwart's built-in filter never scores a signed-in sender, whatever `MtaStageData.enableSpamFilter` says (`spam_classify` in `crates/smtp/src/inbound/spam.rs`). So each node runs Rspamd as a milter on `127.0.0.1:11332`.

The code is in `suite_cloud/mail/cluster/outbound_scan.py` (the milter and the trusted accounts), `suite_cloud/mail/cluster/rspamd.py` (the node files and the install state of each node) and `suite_cloud/telemetry/outbound_scans.py` (the scan records). The scan recorder and its shipper are in `suite_cloud/provisioning/rspamd/`. The playbook is `suite_cloud/provisioning/playbooks/install-rspamd.yml`.

## Request flow

1. A Mail Account signs in over SMTP submission, or sends with JMAP. A JMAP `EmailSubmission/set` opens a local SMTP session, so it takes the same path.
2. At RCPT, the [sending-limits hook](sending-limits.md) counts each recipient.
3. At DATA, Stalwart evaluates the milter's `enable` expression. It is true for a signed-in session whose login is not a key of the `suite-cloud-outbound-scan-exempt` namespace.
4. Stalwart connects to `127.0.0.1:11332` and sends the session and the message, with `{auth_authen}` set to the login.
5. Rspamd scans the message with the outbound profile. It answers `accept`, or `554 5.7.1` when the score reaches the reject score.
6. On accept, Stalwart signs and queues the message. On a refusal, Stalwart sends the reply to the client and drops the message.
7. If Rspamd does not answer in time or cannot be reached, Stalwart accepts the message without the scan.

| Client | Refused message |
| --- | --- |
| SMTP | `554 5.7.1 Message refused: it looks like spam. Contact your administrator if this is wrong.` The client shows its send error and keeps the message |
| JMAP | `EmailSubmission/set` returns `notCreated` with `type: forbiddenToSend` and `description: Server rejected DATA: 554 5.7.1 ...`. The draft stays in Drafts. A JMAP client must show `description` |

## Settings

The **Outbound Spam Scan** section is on the Stalwart tab of Suite Cloud Settings.

| Field | Default | Purpose |
| --- | --- | --- |
| **Outbound Spam Scan Mode** | Enforce | Off: no scan. Observe: scan and log the score, refuse nothing. Enforce: refuse at the reject score |
| **Outbound Spam Scan Reject Score** | 15 | The Rspamd score at which Enforce refuses a message |
| **Rspamd Version** | blank | The package version to install. Blank installs the current stable release |

A change from Off to another mode, or back, changes only the cluster plan. Suite Cloud queues a push to each Active cluster. A change between Observe and Enforce, or of the reject score in Enforce, changes the Rspamd files. Run **Install Outbound Scanners** on each cluster to apply it. The cluster form lists each running node whose files do not match the current settings.

## Stalwart config

The cluster plan holds one operation, from `outbound_scan.cluster_operations()`:

| Operation | Purpose |
| --- | --- |
| `upsert` MtaMilter `outbound-scan` | `127.0.0.1:11332`, `stages: data`, `tempFailOnError: false`, `timeoutConnect: 1000`, `timeoutCommand: 5000`, `timeoutData: 6000`, `protocolVersion: v6`. MtaMilter has no description, so the plan matches it on `hostname` and `port` |

`enable` stays off while the mode is Off, or while a Provisioned, Active or Draining node of the cluster lacks **Outbound Scanner Installed**. MtaMilter is not one of the objects Stalwart provisions itself, so the recovery plan carries it too, switched off. `timeoutData` is longer than Rspamd's `task_timeout` (4 s), so Rspamd answers with what it has before Stalwart gives up. Only the DATA stage is used: Stalwart opens a new milter connection for each stage and replays the session on it.

## Trusted accounts

Mail Account owns **Skip Outbound Spam Scan** (`skip_outbound_scan`). It is at permlevel 1: only a System Manager can read or set it. The tenant API does not take it and `to_api()` does not return it.

Stalwart holds a copy as Memory Lookup Keys:

```json
{"namespace": "suite-cloud-outbound-scan-exempt", "key": "jane@acme.example", "isGlobPattern": false}
```

| Event | Call |
| --- | --- |
| Insert with the field set | `set_exempt(cluster, email, True)` |
| Update that changes the field | `set_exempt(cluster, email, value)`, before the other changes go to the cluster |
| Delete with the field set | `set_exempt(cluster, email, False)` |

Each call runs in the request and runs `ReloadLookupStores` after a change. `ReloadSettings` does not rebuild the lookup stores, so a key without it has no effect. A failure raises, and the save rolls back. Each config push ends with `sync_exempt_accounts()`, which reads the namespace with a `namespace` query filter, creates the missing keys, deletes the extra ones, and reloads only when it changed a key. This repairs a key that a failed call or a hand edit left wrong.

## Rspamd on each node

`install-stalwart.yml` imports `install-rspamd.yml`, so provisioning (`bootstrap-cluster.yml`, `configure-node.yml`) and `upgrade-stalwart.yml` install Rspamd too. Only a Stalwart Node's variables set `install_rspamd`. An Egress Gateway skips the play: it relays mail that a node scanned already.

The playbook adds the `rspamd.com/apt-stable` repository (the Ubuntu package is old and unsupported), installs `rspamd` (pinned to **Rspamd Version** when set), removes every Rspamd file it does not write, writes the files below, runs `rspamadm configtest`, starts Rspamd or reloads it on a change, waits for port 11332, and starts `suite-cloud-scans.timer`. Rspamd listens on loopback only, so the firewall does not change. It resolves through the node's Unbound and runs without Redis, so Bayes, neural, rate limits, replies and history are off.

Suite Cloud owns the node's Rspamd configuration. The playbook deletes each file in `/etc/rspamd/local.d/` and `/etc/rspamd/override.d/` that it does not write, except the `*.example` files, and `/etc/rspamd/rspamd.conf.local`, `rspamd.conf.local.override` and `rspamd.conf.override`. A hand edit on a node is removed by the next install. Before this, a hand-made `actions.conf` (every threshold at 1000) and an `rspamd.conf.override` (the settings from a remote map) on the test node let a message with a score of 16.41 through in Enforce.

The files up to `arc.conf` are in `/etc/rspamd/local.d/`.

| File | Content |
| --- | --- |
| `worker-proxy.inc` | Milter on `127.0.0.1:11332`, `self_scan`, one process per vCPU, the reject message |
| `worker-normal.inc` | `enabled = false` |
| `worker-controller.inc` | `127.0.0.1:11334` |
| `options.inc` | DNS at `127.0.0.1` with a 1 s timeout, `task_timeout = 4s` |
| `actions.conf` | `reject` at the reject score (`null` in Observe and Off). `add_header` and `greylist` are off. The milter sends only signed-in senders' mail, so these are the outbound thresholds |
| `settings.conf` | The `outbound` profile for `authenticated = yes`: the `fuzzy`, `neural`, `statistics`, `asn` and `mx_check` groups off |
| `logging.inc` | Notice level to `/var/log/rspamd/rspamd.log`, one line with the user, score and symbols per message |
| `dkim_signing.conf`, `arc.conf` | `enabled = false`: Stalwart signs the message after the scan |
| `/etc/rspamd/rspamd.local.lua` | The scan recorder: one JSON line per scanned message to `/var/lib/rspamd/suite-cloud-scans.jsonl` |
| `/usr/local/lib/suite-cloud/ship_outbound_scans.py` | The shipper: sends the recorded lines to Suite Cloud |
| `/etc/systemd/system/suite-cloud-scans.service` and `.timer` | Run the shipper each minute, with the node's Suite Cloud token from `/etc/stalwart/stalwart.env` |

The profile keeps the URL blocklists, phishing, MIME and attachment checks, and the header and body rules. A taken-over account sends mostly links, so the URL checks are the most useful ones. `milter_headers` keeps `skip_authenticated = true`, so outbound mail gets no `X-Spam-*` header.

Suite Cloud renders the files in Python (`rspamd.get_rspamd_files()`) and passes them as job variables with their SHA-1 (`rspamd_digest`). Ansible templates the variables it is given, so the files must not hold `{{`, `{%` or `{#`. The playbook puts the node's vCPU count in place of `__CPU_COUNT__`.

## Node state

| Stalwart Node field | Set by |
| --- | --- |
| **Outbound Scanner Installed** | A passed `install-rspamd.yml`, provisioning or upgrade job |
| **Outbound Scanner Digest** | The `rspamd_digest` of that job's variables, not of the settings when the callback runs |

**Install Outbound Scanner** on a node, or **Install Outbound Scanners** on a cluster for each running node, creates a Server Job with the callback `after_install_outbound_scanner`. When the last running node of a cluster gets its scanner, Suite Cloud queues a cluster push, which switches the milter on.

## Form status

The Stalwart Node, Egress Gateway and Stalwart Cluster forms show one indicator for each kind of telemetry, from `suite_cloud/telemetry/status.py`. The API notes in the cache when each server last sent each kind. The silence window is **Telemetry Silence Minutes** (30 by default).

| Indicator | Green | Other states |
| --- | --- | --- |
| Metrics | Received within the window | Red: none since then. Blue: none received yet |
| Events | Received within the window | Grey: none since then, which is normal for an idle server. Blue: none received yet |
| Outbound scan (nodes only) | The shipper checked in within the window | Grey: the mode is Off, or the scanner is not installed. Orange: the scanner is out of date. Red: the shipper is silent. Blue: no check-in yet |
| Scan records (beside a green Outbound scan, once records arrived) | Records received within the window | Grey: none since then, which is normal while nobody sends |

The cluster form shows one indicator for each kind over its Active and Draining nodes and Active gateways. It takes the worst state and names the servers that need a look by their short name, for example `Metrics: 1 reporting, 1 silent (n2)`. Keep each text short: Frappe squeezes an indicator's dot when its text wraps. While telemetry is off, each form shows only `Telemetry: off`. A red Metrics or Outbound scan indicator also opens a **Server Alert** of type Metrics Silent or Outbound Scan Silent at the next five-minute check (see [the alerts](api/telemetry.md#alerts)).

## Telemetry

The events WebHook sends `milter.action-accept`, `milter.action-reply-code`, `milter.action-reject`, `milter.action-discard` and `milter.action-temp-fail` at info level. `milter.io-error` and `milter.timeout` are on the warnings stream. `Elapsed` on `milter.action-accept` is the scan time; the target is a p95 of 150 ms or less for a message without links. `milter.action-reply-code` counts the refused messages. An event carries `span_id` but not the account, so a query joins it to the `smtp.mail-from` event of the same span to get the sender.

## Scan records

Each node records every message it scans for a signed-in sender, and Suite Cloud stores the records in the telemetry warehouse. The **Outbound Spam Scan** dashboard of the Stalwart Mail workbook in Insights reads them.

1. The recorder registers the idempotent symbol `SUITE_CLOUD_SCAN_RECORD`. Rspamd runs it after the score, so it also records refused messages. It appends one JSON line to `/var/lib/rspamd/suite-cloud-scans.jsonl` and opens and closes the file for each line. The line is written in one call, so two proxy processes never mix their lines.
2. Each minute the timer runs the shipper. The shipper moves the spool to `/var/lib/rspamd/suite-cloud-scans.pending/` and posts it to `suite_cloud.api.telemetry.outbound_scans`, at most 2000 records for each request, with the node's Suite Cloud token. A run with nothing to send posts an empty batch, which is the shipper's check-in.
3. If a send fails, the file stays for the next run. The same file gives the same body, so Suite Cloud gives the batch the same id and stores it once. Past 100 MB of files the shipper deletes the oldest. This happens while telemetry is off, because the API then refuses the token.
4. The API stages a row for each record in `outbound_scans`, and a row for each matched rule in `outbound_scan_symbols`. The warehouse loads both tables every five minutes and prunes them with the other telemetry tables.

Nothing in the scan waits for Suite Cloud: the recorder only writes a local file. If Suite Cloud is down, sending is not slower.

| Record field | Column | Content |
| --- | --- | --- |
| `time` | `timestamp` | When Rspamd finished the scan, in UTC |
| `user` | `account` | The signed-in login, in lowercase. `account_domain` is its domain |
| `envelope_from` | `envelope_from` | The SMTP `MAIL FROM` address |
| `header_from` | `header_from` | The address of the `From` header |
| `message_id` | `message_id` | The `Message-ID` header |
| `queue_id` | `queue_id` | Stalwart's queue id, when the milter sends it |
| `client_ip` | `client_ip` | The address the sender connected from |
| `recipients` | `recipients` | The number of SMTP recipients. The addresses are not recorded |
| `size` | `size` | The message size in bytes |
| `action` | `action` | Rspamd's action: `reject` when it refused the message, else `no action` |
| `score` | `score` | Rspamd's score |
| `scan_time` | `scan_time` | Milliseconds from the start of the scan to the record |
| `symbols` | `symbols`, and the `outbound_scan_symbols` rows | `{rule: score}` of each matched rule |
| | `mode`, `reject_score` | The settings when the batch arrived, so Observe shows what Enforce would refuse |

The subject, the body and the recipient addresses are not recorded.

| Dashboard chart | Shows |
| --- | --- |
| Outbound Scan Readings | Scanned, refused and over-the-score messages, and the median scan time |
| Outbound Scan Results | Passed, over the reject score but not refused (Observe), and refused messages per day |
| Outbound Scores | Messages per score band of one point. Use it to set the reject score |
| Rules That Raised Scores | The rules that added to a score most often |
| Outbound Scan Time (ms) | The median and slowest scan per hour |
| Outbound Scores by Account | Each sender's message count, median and highest score, and refusals |
| Scanned Messages | One row per message, newest first |
| Rules per Message | Each rule that matched each message and what it added |

The filters are Date Range (the last 7 days by default), Host, Account and Message ID. To check one test message, put its `Message-ID` in the Message ID filter: Scanned Messages shows its score and action, and Rules per Message shows why. The records reach the dashboard up to six minutes after the send: one minute for the timer and five for the warehouse load.

## Accepted gaps

| Case | Effect |
| --- | --- |
| Rspamd is down, slow (over 6 s) or not installed yet | Stalwart sends the message without the scan. The sending limits still cap the account. `milter.io-error` or `milter.timeout` shows on the warnings stream |
| A trusted account | Its mail is never scanned |
| Mail that Stalwart makes itself (Sieve redirects, vacation replies, forwards, bounces) | Not scanned |
| An Egress Gateway | It does not scan. The node scanned the message before it relayed it |
| A sender who sends test messages | Can learn what the scanner refuses |
| No Redis | No Bayes, neural or reply learning. Scores come from rules and blocklists only |
| A refused message | The sending-limits hook counted its recipients at RCPT, so they still count |
| Records while telemetry is off, or Suite Cloud is down for a long time | The node keeps 100 MB of them, then deletes the oldest |

## Roll-out

1. Deploy and migrate. The patch `fill_outbound_scan_defaults` sets Enforce and 15 on an existing site. Nothing changes on a cluster, because no node has the scanner installed.
2. Set **Outbound Spam Scan Mode** to Observe.
3. On the test cluster, save the hand-installed Rspamd config of `n1` if it is still needed. The playbook deletes every Rspamd file it does not write (see [Rspamd on each node](#rspamd-on-each-node)). Then run **Install Outbound Scanners**.
4. Check on the live server that the plan matches the MtaMilter again on `hostname` and `port`, that `protocolVersion` takes `"v6"`, and, with `rspamadm configdump`, the proxy's `reject_message`, `enabled = false` for the normal worker and the group names.
5. Send GTUBE over SMTP and JMAP, then clean mail, then stop Rspamd and send again, then mark the account as trusted and send GTUBE. Expect a refusal (in Enforce), `milter.action-accept` times under 150 ms, `250` with a `milter.io-error` warning, and `250`. Within six minutes each scanned message shows on the **Outbound Spam Scan** dashboard under its Message-ID. If it does not, check `journalctl -u suite-cloud-scans` on the node.
6. Run **Install Outbound Scanners** on each production cluster. Leave Observe on for a week and read the scores of signed-in senders on the **Outbound Spam Scan** dashboard. Mark the trusted accounts.
7. Set the reject score from the Outbound Scores chart, set the mode to Enforce, and run **Install Outbound Scanners** on each cluster again.
8. Watch the refusals and the scan time on the **Outbound Spam Scan** dashboard for the first days.
