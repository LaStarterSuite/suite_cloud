package config

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"path/filepath"
	"strings"
)

// Validate reports every problem at once, each naming its key.
func (c *Config) Validate() error {
	var problems []error
	fail := func(format string, args ...any) { problems = append(problems, fmt.Errorf(format, args...)) }

	if err := checkURL(c.SuiteCloud.URL); err != nil {
		fail("suite_cloud.url: %v", err)
	}
	if strings.TrimSpace(c.SuiteCloud.Secret) == "" {
		fail("suite_cloud.secret is required")
	}
	checkRange(fail, "suite_cloud.requests_per_minute", c.SuiteCloud.RequestsPerMinute, 1, 100000)
	checkRange(fail, "suite_cloud.request_timeout_seconds", c.SuiteCloud.RequestTimeoutSeconds, 5, 600)

	if net.ParseIP(c.Node.HostIP) == nil {
		fail("node.host_ip %q is not an IP address", c.Node.HostIP)
	}
	if len(c.Node.Hostname) > 253 {
		fail("node.hostname is longer than 253 characters")
	}
	if !filepath.IsAbs(c.StateDir) {
		fail("state_dir must be an absolute path")
	}
	switch c.LogLevel {
	case "debug", "info", "warn", "error":
	default:
		fail("log_level must be debug, info, warn or error")
	}

	if q := c.Quota; q != nil {
		validateQuota(fail, q)
	}
	return errors.Join(problems...)
}

func validateQuota(fail func(string, ...any), q *Quota) {
	checkRange(fail, "quota.hook_port", q.HookPort, 1024, 65535)
	if strings.TrimSpace(q.HookToken) == "" {
		fail("quota.hook_token is required")
	}
	checkRange(fail, "quota.limits_poll_seconds", q.LimitsPollSeconds, 5, 3600)
	checkRange(fail, "quota.usage_push_seconds", q.UsagePushSeconds, 5, 3600)
	switch q.Store.Type {
	case "memory":
	case "redis", "redis_cluster", "redis_sentinel":
		if len(q.Store.URLs) == 0 {
			fail("quota.store.urls is required for %s", q.Store.Type)
		}
		for _, raw := range q.Store.URLs {
			if parsed, err := url.Parse(raw); err != nil || parsed.Scheme != "redis" && parsed.Scheme != "rediss" || parsed.Host == "" {
				fail("quota.store.urls: %q must be a redis:// or rediss:// URL", raw)
			}
		}
		if q.Store.Type == "redis" && len(q.Store.URLs) != 1 {
			fail("quota.store.urls must hold one URL for redis")
		}
		if q.Store.Type == "redis_sentinel" && strings.TrimSpace(q.Store.ServiceName) == "" {
			fail("quota.store.service_name is required for redis_sentinel")
		}
	default:
		fail("quota.store.type must be memory, redis, redis_cluster or redis_sentinel")
	}
}

// Summary describes the config for the startup log without its secrets.
func (c *Config) Summary() string {
	var b strings.Builder
	fmt.Fprintf(&b, "url=%s secret=%s host_ip=%s hostname=%s requests_per_minute=%d",
		c.SuiteCloud.URL, setOrNot(c.SuiteCloud.Secret), c.Node.HostIP, c.Node.Hostname,
		c.SuiteCloud.RequestsPerMinute)
	if q := c.Quota; q != nil {
		fmt.Fprintf(&b, " quota_port=%d quota_token=%s quota_store=%s", q.HookPort, setOrNot(q.HookToken), q.Store.Type)
	}
	return b.String()
}

func checkURL(raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" || parsed.Host == "" {
		return fmt.Errorf("%q must be an http or https URL", raw)
	}
	return nil
}

func checkRange(fail func(string, ...any), key string, value, low, high int) {
	if value < low || value > high {
		fail("%s must be between %d and %d, not %d", key, low, high, value)
	}
}

func setOrNot(value string) string {
	if value == "" {
		return "empty"
	}
	return "set"
}
