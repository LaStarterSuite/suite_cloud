# Outbound spam scan

Suite Cloud scans each message that a signed-in Mail Account sends, before Stalwart queues it. A message that scores as spam is refused while the client is still connected. The scan is on for every Mail Account. A System Manager can turn it off for one trusted account with **Skip Outbound Spam Scan**. Inbound mail is not affected: Stalwart's built-in spam filter scans it, as before.

Stalwart's built-in filter never scores a signed-in sender, whatever `MtaStageData.enableSpamFilter` says (`spam_classify` in `crates/smtp/src/inbound/spam.rs`). So each node runs Rspamd as a milter on `127.0.0.1:11332`.

The code is in `suite_cloud/mail/cluster/outbound_scan.py` (the milter and the trusted accounts) and `suite_cloud/mail/cluster/rspamd.py` (the Rspamd files and the install state of each node). The playbook is `suite_cloud/provisioning/playbooks/install-rspamd.yml`.

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

The playbook adds the `rspamd.com/apt-stable` repository (the Ubuntu package is old and unsupported), installs `rspamd` (pinned to **Rspamd Version** when set), writes the files under `/etc/rspamd/local.d/`, runs `rspamadm configtest`, starts Rspamd or reloads it on a change, and waits for port 11332. Rspamd listens on loopback only, so the firewall does not change. It resolves through the node's Unbound and runs without Redis, so Bayes, neural, rate limits, replies and history are off.

| File | Content |
| --- | --- |
| `worker-proxy.inc` | Milter on `127.0.0.1:11332`, `self_scan`, one process per vCPU, the reject message |
| `worker-normal.inc` | `enabled = false` |
| `worker-controller.inc` | `127.0.0.1:11334` |
| `options.inc` | DNS at `127.0.0.1` with a 1 s timeout, `task_timeout = 4s` |
| `settings.conf` | The `outbound` profile for `authenticated = yes`: the `fuzzy`, `neural`, `statistics`, `asn` and `mx_check` groups off, `reject` at the reject score (`null` in Observe and Off), no header, subject or greylist actions |
| `logging.inc` | Notice level to `/var/log/rspamd/rspamd.log`, one line with the user, score and symbols per message |

The profile keeps the URL blocklists, phishing, MIME and attachment checks, and the header and body rules. A taken-over account sends mostly links, so the URL checks are the most useful ones. `milter_headers` keeps `skip_authenticated = true`, so outbound mail gets no `X-Spam-*` header.

Suite Cloud renders the files in Python (`rspamd.get_rspamd_files()`) and passes them as job variables with their SHA-1 (`rspamd_digest`). Ansible templates the variables it is given, so the files must not hold `{{`, `{%` or `{#`. The playbook puts the node's vCPU count in place of `__CPU_COUNT__`.

## Node state

| Stalwart Node field | Set by |
| --- | --- |
| **Outbound Scanner Installed** | A passed `install-rspamd.yml`, provisioning or upgrade job |
| **Outbound Scanner Digest** | The `rspamd_digest` of that job's variables, not of the settings when the callback runs |

**Install Outbound Scanner** on a node, or **Install Outbound Scanners** on a cluster for each running node, creates a Server Job with the callback `after_install_outbound_scanner`. When the last running node of a cluster gets its scanner, Suite Cloud queues a cluster push, which switches the milter on.

## Telemetry

The events WebHook sends `milter.action-accept`, `milter.action-reply-code`, `milter.action-reject`, `milter.action-discard` and `milter.action-temp-fail` at info level. `milter.io-error` and `milter.timeout` are on the warnings stream. `Elapsed` on `milter.action-accept` is the scan time; the target is a p95 of 150 ms or less for a message without links. `milter.action-reply-code` counts the refused messages. An event carries `span_id` but not the account, so a query joins it to the `smtp.mail-from` event of the same span to get the sender.

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

## Roll-out

1. Deploy and migrate. The patch `fill_outbound_scan_defaults` sets Enforce and 15 on an existing site. Nothing changes on a cluster, because no node has the scanner installed.
2. Set **Outbound Spam Scan Mode** to Observe.
3. On the test cluster, save the hand-installed Rspamd config of `n1`, because the playbook writes over `local.d`. Then run **Install Outbound Scanners**.
4. Check on the live server that the plan matches the MtaMilter again on `hostname` and `port`, that `protocolVersion` takes `"v6"`, and, with `rspamadm configdump`, the proxy's `reject_message`, `enabled = false` for the normal worker and the group names.
5. Send GTUBE over SMTP and JMAP, then clean mail, then stop Rspamd and send again, then mark the account as trusted and send GTUBE. Expect a refusal (in Enforce), `milter.action-accept` times under 150 ms, `250` with a `milter.io-error` warning, and `250`.
6. Run **Install Outbound Scanners** on each production cluster. Leave Observe on for a week and read the scores of signed-in senders in `/var/log/rspamd/rspamd.log`. Mark the trusted accounts.
7. Set the reject score from the logs, set the mode to Enforce, and run **Install Outbound Scanners** on each cluster again.
8. Watch `milter.action-reply-code` and the scan time in Insights for the first days.
