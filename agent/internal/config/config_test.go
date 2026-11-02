package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const example = `{
  "suite_cloud": {"url": "https://cloud.example.com/", "secret": "s3cret"},
  "node": {"host_ip": "203.0.113.5", "hostname": "n1.c1.example.com"}
}`

func load(t *testing.T, text string) (*Config, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
	return Load(path)
}

func TestLoadAppliesDefaults(t *testing.T) {
	cfg, err := load(t, example)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.SuiteCloud.RequestsPerMinute != 300 || cfg.SuiteCloud.MaxAttempts != 8 || cfg.SuiteCloud.RequestTimeoutSeconds != 60 {
		t.Fatalf("limits %+v", cfg.SuiteCloud)
	}
	if cfg.StateDir != "/var/lib/suite-cloud-agent" || cfg.LogLevel != "info" || cfg.Quota != nil {
		t.Fatalf("paths %+v", cfg)
	}
	if strings.Contains(cfg.Summary(), "s3cret") || !strings.Contains(cfg.Summary(), "secret=set") {
		t.Fatalf("summary leaks: %s", cfg.Summary())
	}
}

func TestLoadRejectsBadValues(t *testing.T) {
	cases := map[string]string{
		"unknown key":         strings.Replace(example, `"node":`, `"nod": 1, "node":`, 1),
		"missing secret":      strings.Replace(example, `"secret": "s3cret"`, `"secret": " "`, 1),
		"bad url":             strings.Replace(example, "https://cloud.example.com/", "cloud.example.com", 1),
		"bad ip":              strings.Replace(example, "203.0.113.5", "node-1", 1),
		"relative state dir":  strings.Replace(example, `"node":`, `"state_dir": "state", "node":`, 1),
		"bad timeout":         strings.Replace(example, `"secret": "s3cret"`, `"secret": "s3cret", "request_timeout_seconds": 1`, 1),
		"records per request": strings.Replace(example, `"secret": "s3cret"`, `"secret": "s3cret", "records_per_request": 5000`, 1),
		"node source":         strings.Replace(example, `"host_ip":`, `"source": "stalwart", "host_ip":`, 1),
		"logs":                strings.Replace(example, `"node":`, `"logs": [], "node":`, 1),
		"metrics":             strings.Replace(example, `"node":`, `"metrics": {}, "node":`, 1),
	}
	for name, text := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := load(t, text); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}

func TestUnknownKeyNamesIt(t *testing.T) {
	_, err := load(t, strings.Replace(example, `"node"`, `"host"`, 1))
	if err == nil || !strings.Contains(err.Error(), "host") {
		t.Fatalf("error %v", err)
	}
}

func TestValidationNamesEveryKey(t *testing.T) {
	cfg := &Config{}
	cfg.ApplyDefaults()
	err := cfg.Validate()
	for _, key := range []string{"suite_cloud.url", "suite_cloud.secret", "node.host_ip"} {
		if !strings.Contains(err.Error(), key) {
			t.Fatalf("%q not named in %v", key, err)
		}
	}
}

const quota = `"quota": {"hook_port": 8091, "hook_token": "t0ken", "store": {"type": "memory"}},
  "node":`

func TestLoadQuotaAppliesDefaults(t *testing.T) {
	cfg, err := load(t, strings.Replace(example, `"node":`, quota, 1))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Quota.LimitsPollSeconds != 60 || cfg.Quota.UsagePushSeconds != 60 {
		t.Fatalf("quota %+v", cfg.Quota)
	}
	if strings.Contains(cfg.Summary(), "t0ken") || !strings.Contains(cfg.Summary(), "quota_token=set") {
		t.Fatalf("summary leaks: %s", cfg.Summary())
	}
}

func TestLoadRejectsBadQuota(t *testing.T) {
	cases := map[string]string{
		"low port":            strings.Replace(quota, "8091", "80", 1),
		"no token":            strings.Replace(quota, "t0ken", " ", 1),
		"unknown store":       strings.Replace(quota, `"memory"`, `"memcached"`, 1),
		"redis no url":        strings.Replace(quota, `"memory"`, `"redis"`, 1),
		"redis two urls":      strings.Replace(quota, `"type": "memory"`, `"type": "redis", "urls": ["redis://a:6379", "redis://b:6379"]`, 1),
		"redis bad url":       strings.Replace(quota, `"type": "memory"`, `"type": "redis_cluster", "urls": ["http://a:6379"]`, 1),
		"sentinel no service": strings.Replace(quota, `"type": "memory"`, `"type": "redis_sentinel", "urls": ["redis://a:26379"]`, 1),
	}
	for name, section := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := load(t, strings.Replace(example, `"node":`, section, 1)); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}
