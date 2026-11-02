# Suite Cloud Agent

The agent runs on Stalwart nodes as a systemd service and enforces the sending limits (`internal/quota`). It answers the hook Stalwart calls for each message, counts every account's sends, pulls the limits from Suite Cloud and pushes the usage back. Stalwart pushes its own logs and metrics to Suite Cloud; the agent does not ship them. [docs/agent.md](../docs/agent.md) describes how Suite Cloud builds and installs it.

Suite Cloud writes `/etc/suite-cloud-agent/config.json`. Unknown keys are refused.

```json
{
  "suite_cloud": {"url": "https://cloud.example.com", "secret": "<agent key>:<agent secret>", "requests_per_minute": 300, "max_attempts": 8},
  "node": {"host_ip": "203.0.113.5", "hostname": "n1.c1.example.com"},
  "state_dir": "/var/lib/suite-cloud-agent",
  "quota": {"hook_port": 8095, "hook_token": "<token>", "store": {"type": "memory"}}
}
```

- `suite_cloud.request_timeout_seconds` is optional (default 60). `node.hostname` defaults to the machine's hostname. `log_level` is `debug`, `info` (default), `warn` or `error`.
- `quota.store.type` is `memory` on a single node, which keeps the counts in `quota.json` in `state_dir`, or `redis`, `redis_cluster` or `redis_sentinel` with `urls` on a multi-node cluster. `limits_poll_seconds` and `usage_push_seconds` default to 60.
- Without a `quota` section the agent starts and idles until it is stopped.

The only dependency is `github.com/redis/go-redis/v9`, for the quota counters on multi-node clusters. A build downloads it from the Go module proxy and checks it against `go.sum`. After a dependency change, run `go mod tidy` and commit `go.mod` and `go.sum`.

```sh
go test -race ./...
SCA_TEST_REDIS_URL=redis://localhost:6379/7 go test -race ./internal/quota/  # also tests the Redis store
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.version=$(cat VERSION)" -o suite-cloud-agent ./cmd/suite-cloud-agent
./suite-cloud-agent --config config.json --check
```

`VERSION` is the version operators see. Suite Cloud appends a digest of the sources when it builds, so a change here rolls out without a bump.
