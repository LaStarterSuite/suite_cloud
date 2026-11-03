# Suite Cloud Agent

The Suite Cloud Agent is one Go binary, `suite-cloud-agent`, that runs as a systemd service on every Stalwart node. It enforces the [sending limits](#sending-limits) of Mail Accounts. Stalwart pushes its own events and metrics to the [telemetry API](api/telemetry.md), and egress gateways run no agent. Its source is the `agent/` folder of this repository. Suite Cloud builds it on the bench host and installs it with the node's provisioning job. A later change moves the sending limits into Stalwart, and the agent goes then.

## Configuration

The agent starts from `/etc/suite-cloud-agent/config.json`. Provisioning writes the file from Suite Cloud Settings and the node record. A change to either reaches the node with the next Provision, Upgrade or Install Agent job, and the agent restarts to read it. Unknown keys are refused, so a typo cannot turn a setting off.

```json
{
  "suite_cloud": {"url": "https://cloud.example.com", "secret": "<agent key>:<agent secret>", "requests_per_minute": 300},
  "node": {"host_ip": "203.0.113.5", "hostname": "n1.c1.example.com"},
  "state_dir": "/var/lib/suite-cloud-agent",
  "quota": {"hook_port": 8095, "hook_token": "<quota hook token>", "store": {"type": "memory"}}
}
```

| Key | Source | Meaning |
| --- | --- | --- |
| `suite_cloud.url` | **Public URL**, else the site's own URL | Where the calls go |
| `suite_cloud.secret` | **Agent Key** and **Agent Secret** of the cluster | Sent as the bearer token, `<agent key>:<agent secret>` |
| `suite_cloud.requests_per_minute` | **Agent Requests per Minute** | The agent keeps to the limit the site enforces |
| `suite_cloud.request_timeout_seconds` | 60 | Time allowed for one request |
| `node.host_ip`, `node.hostname` | The node record | The sending-limit calls name the node by `host_ip` |
| `state_dir` | `/var/lib/suite-cloud-agent` | Where the agent keeps `quota.json` |
| `log_level` | `info` | `debug`, `info`, `warn` or `error` |
| `quota.hook_port`, `quota.hook_token` | 8095, **Quota Hook Token** of the cluster | The loopback port and bearer token of the sending-limit hook. The section is absent on outbound-only nodes, which take no client mail, and until the last build is 0.2.0 or newer. Without it the agent starts and idles |
| `quota.store` | The cluster's in-memory store | `{"type": "memory"}`, or `redis`, `redis_cluster` or `redis_sentinel` with the store's `urls` and credentials |
| `quota.limits_poll_seconds`, `usage_push_seconds` | Not written. The agent uses 60, 60 | How often limits are pulled and counts pushed |

`suite-cloud-agent --config <path> --check` validates the file and the state directory without a request. `--version` prints the version.

## Running on a node

The service runs as the `stalwart` user, sandboxed away from Stalwart's configuration and data. `journalctl -u suite-cloud-agent` shows what it does. Provisioning installs the agent in every Provision, Upgrade and Re-provision job of a node, after Stalwart. The job copies the binary of the node's architecture from the bench host, writes `config.json` and the unit, and restarts the agent when any of them changed. When **Enable Telemetry** is off or no agent build exists, the job stops and disables the agent instead, and an Error Log names the missing build. **Show Agent Config** on a Stalwart Node shows the `config.json` that job would write, with every secret masked.

**Install Agent** on a Stalwart Node and **Upgrade Agents** on a Stalwart Cluster also install the node's telemetry token, as the [cluster telemetry](../README.md#telemetry) section describes. **Rotate Agent Secret** on a Stalwart Cluster replaces its Agent Secret and runs Upgrade Agents for it.

## Sending limits

A Mail Account's **Daily Send Limit** and **Monthly Send Limit** count recipients per UTC day and month, each at least 1 and at most the cluster's **Max Daily Send Limit** and **Max Monthly Send Limit**. A new account starts at its tenant's **Default Daily Send Limit** and **Default Monthly Send Limit**, 2,000 and 10,000 unless an operator changes them, and a tenant sets an account's limits through the API only while **Tenant May Set Send Limits** is on. Stalwart enforces them through an MTA Hook at the data stage, for signed-in senders only, which covers SMTP submission and JMAP sends. The hook calls the Agent on the same node at `http://127.0.0.1:8095/hooks/quota` with the cluster's **Quota Hook Token**. The Agent reads only the context and the envelope of the request, never the message, and in one atomic step either adds the recipients to the account's day and month counters or, when that would pass a limit, counts the message as refused instead and answers `550 5.4.5` with the limit and when it resets.

| Cluster | Counters and limits |
| --- | --- |
| Single node, no Redis | The Agent's memory, saved to `quota.json` in the state directory every 5 seconds and on shutdown |
| With a Redis in-memory store (every multi-node cluster) | That Redis under `sca:quota:`. A Lua script checks and counts, and every key of an account shares the hash tag `{account}`, so it also works on Redis Cluster |

The Agent pulls the limits changed since its last pull every 60 seconds and all of them on start and every hour; an account that two full pulls in a row do not return, such as a deleted one, loses its limits. One miss is not enough, because a pull leaves out saves younger than five seconds. It pushes the day and month totals of the accounts it counted, sent and refused, every 60 seconds and on shutdown, and keeps them for the next push when one fails. Until a push of the current UTC day goes through, after midnight, after a start or after failed pushes, and in the first ten minutes of every UTC day, each push also sends yesterday's totals (and an account whose yesterday total a failed request held gets it again until it is taken), and the previous month's when a month ended, so what was counted after yesterday's last push is not lost. Suite Cloud takes nothing older than yesterday. On a start the Agent also pushes every account its store counted today or yesterday, asking the store again at each push until it answers, so counts left by a crash are not lost. Both calls are in the [telemetry API](api/telemetry.md#sending-limits). The Mail Account form shows the last pushed totals: orange from 80% of a limit, red at 100%, with the refused messages beside them. Suite Cloud keeps each account's day and month totals for **Sending Usage Retention (Days)** in Suite Cloud Settings (90 by default) and deletes older days, and months that ended before them, daily. In Insights, the **Sending Usage** dashboard of the Stalwart Mail workbook shows recipients and refusals for the latest UTC day, the accounts at or near a limit, the top senders, recipients and refusals per day, and each account's daily history, filtered by tenant, cluster or account. Its "today" is the latest day and month the Agents reported rather than the database's date, so it stays right whatever the database's time zone. Each day and month keeps the limit the account had while it was current, so the history shows how close a past day came to the limit of that day.

Nothing in the quota path stops mail. When the Agent fails or takes longer than 2 seconds, Stalwart accepts the message uncounted. A part of the quota that fails, such as a hook whose port is taken or a `quota.json` that cannot be written, is logged and started again after 10 seconds. A native throttle, `suite-cloud-quota-backstop`, caps every signed-in account meanwhile at the cluster's **Max Daily Send Limit**, 2,000 by default. It counts messages, not recipients, so it is never stricter than an account's own limit. A change applies with the next Sync Config. A message the hook accepted stays counted when Stalwart refuses it later, for example over a queue quota.

The hook and the backstop are part of the cluster plan, and both they and the `quota` section of `config.json` wait for **Enable Telemetry** and a build of 0.2.0 or newer. Until then the plan keeps both switched off. Turning **Enable Telemetry** off queues a push of every active cluster's config, so the hook installed before is switched off with the Agents. A cluster takes one config push at a time. A desk change that alters the plan, such as an egress pool or a tenant's pool, queues its push after the save, so a push already running never fails the save; a cluster has at most one push waiting, which takes in every change made before it starts. A pool change pushes the cluster first and its gateways after. A queued push that fails shows its error on the cluster's form until a push goes through. **Sync Config** waits up to 20 seconds and then asks you to try again. To turn the limits on for a cluster: run **Build Agent**, then **Upgrade Agents**, then **Sync Config**. In the other order nothing breaks, but every message logs a hook error until the Agents are up. A single-node cluster that later gets a Redis in-memory store needs Upgrade Agents again to move its counters there.

## Building

Suite Cloud builds the agent on the bench host with the Go toolchain found on `PATH` or at `/usr/local/go/bin/go`. The build needs Go 1.22 or newer and access to the Go module proxy, `proxy.golang.org` by default: it downloads the one dependency, `go-redis`, checks it against `agent/go.sum` and keeps it under `private/files/agent/.gomodcache`, so later builds need no network. A host behind a proxy sets `GOPROXY` for the bench's workers. Ubuntu 24.04 ships a recent enough `golang-go` package; on older releases, or to pick the version, install the release from [go.dev/dl](https://go.dev/dl/) under `/usr/local`, where Suite Cloud finds it without a `PATH` change:

```sh
sudo apt install golang-go                          # Ubuntu 24.04 or newer
curl -fsSL https://go.dev/dl/go1.22.12.linux-amd64.tar.gz | sudo tar -C /usr/local -xzf -   # any release
/usr/local/go/bin/go version
```

The bench's workers run the build, so the toolchain must be readable by the bench user. **Build Agent** in Suite Cloud Settings queues a build; a migrate queues one when the agent's sources changed since the last build. The build compiles `linux/amd64` and `linux/arm64` into `private/files/agent/<version>/` under the site, records the version in **Built Agent Version**, and keeps the previous build so a job in progress can finish. Provisioning installs the build recorded there, so a source change that is not built yet never stops a running agent. A failure is shown in **Last Build Error** and in an Error Log. The version is the `agent/VERSION` file plus a digest of the sources and `go.sum`, so a source change makes a new build without a bump; the install job compares the binary's checksum with the node's and copies it only when it differs.

## Installing by hand

For a node that Suite Cloud does not provision, copy `private/files/agent/<version>/suite-cloud-agent-linux-<arch>` to `/usr/local/bin/suite-cloud-agent`, write `/etc/suite-cloud-agent/config.json` from the example above with mode 0600, create `/var/lib/suite-cloud-agent` for the service user, and install this unit as `/etc/systemd/system/suite-cloud-agent.service`:

```ini
[Unit]
Description=Suite Cloud Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stalwart
Group=stalwart
ExecStartPre=/usr/local/bin/suite-cloud-agent --config /etc/suite-cloud-agent/config.json --check
ExecStart=/usr/local/bin/suite-cloud-agent --config /etc/suite-cloud-agent/config.json
Restart=always
RestartSec=5
TimeoutStopSec=90
StateDirectory=suite-cloud-agent
StateDirectoryMode=0750
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
InaccessiblePaths=/etc/stalwart /var/lib/stalwart
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

Then `systemctl enable --now suite-cloud-agent`.
