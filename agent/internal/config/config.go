// Package config reads and checks the agent's config.json.
package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"time"
)

// DefaultPath is where provisioning writes the file; Stalwart's own config.json lives elsewhere.
const DefaultPath = "/etc/suite-cloud-agent/config.json"

type Config struct {
	SuiteCloud SuiteCloud `json:"suite_cloud"`
	Node       Node       `json:"node"`
	StateDir   string     `json:"state_dir"`
	LogLevel   string     `json:"log_level"`
	Quota      *Quota     `json:"quota,omitempty"`
}

// SuiteCloud is where requests go and the limits the site enforces on them.
type SuiteCloud struct {
	URL                   string `json:"url"`
	Secret                string `json:"secret"`
	RequestsPerMinute     int    `json:"requests_per_minute"`
	RequestTimeoutSeconds int    `json:"request_timeout_seconds"`
}

// Node identifies this machine; the quota calls send host_ip, which must be the address Suite Cloud stores.
type Node struct {
	HostIP   string `json:"host_ip"`
	Hostname string `json:"hostname"`
}

// Quota is the sending-limit hook Stalwart calls on this node, and where its counters live.
type Quota struct {
	HookPort          int        `json:"hook_port"`
	HookToken         string     `json:"hook_token"`
	Store             QuotaStore `json:"store"`
	LimitsPollSeconds int        `json:"limits_poll_seconds"`
	UsagePushSeconds  int        `json:"usage_push_seconds"`
}

// QuotaStore is "memory" on a single node, or the cluster's Redis: "redis" (one URL),
// "redis_cluster" (its nodes' URLs) or "redis_sentinel" (the sentinels' URLs).
type QuotaStore struct {
	Type             string   `json:"type"`
	URLs             []string `json:"urls,omitempty"`
	Username         string   `json:"username,omitempty"`
	Password         string   `json:"password,omitempty"`
	ServiceName      string   `json:"service_name,omitempty"`
	SentinelUsername string   `json:"sentinel_username,omitempty"`
	SentinelPassword string   `json:"sentinel_password,omitempty"`
}

// Load reads, completes and validates a config file. Unknown keys are errors, so a typo cannot
// silently turn a setting off.
func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	cfg := &Config{}
	if err := decoder.Decode(cfg); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	cfg.ApplyDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return cfg, nil
}

func (c *Config) ApplyDefaults() {
	setInt(&c.SuiteCloud.RequestsPerMinute, 300)
	setInt(&c.SuiteCloud.RequestTimeoutSeconds, 60)
	if c.Node.Hostname == "" {
		c.Node.Hostname, _ = os.Hostname()
	}
	if c.StateDir == "" {
		c.StateDir = "/var/lib/suite-cloud-agent"
	}
	if c.LogLevel == "" {
		c.LogLevel = "info"
	}
	if q := c.Quota; q != nil {
		setInt(&q.LimitsPollSeconds, 60)
		setInt(&q.UsagePushSeconds, 60)
	}
}

func (q Quota) LimitsPollInterval() time.Duration {
	return time.Duration(q.LimitsPollSeconds) * time.Second
}
func (q Quota) UsagePushInterval() time.Duration {
	return time.Duration(q.UsagePushSeconds) * time.Second
}

func setInt(field *int, value int) {
	if *field == 0 {
		*field = value
	}
}
