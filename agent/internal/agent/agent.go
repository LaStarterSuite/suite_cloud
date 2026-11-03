// Package agent wires the config and the sending quota together.
package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/quota"
	"github.com/frappe/suite_cloud/agent/internal/sender"
)

// Run enforces the sending quota until the context ends. Without a quota section it idles, so a
// node with nothing to do does not restart in a loop.
func Run(ctx context.Context, cfg *config.Config, version string, logger *slog.Logger) error {
	if err := os.MkdirAll(cfg.StateDir, 0o750); err != nil {
		return fmt.Errorf("state_dir: %w", err)
	}
	logger.Info("suite-cloud-agent starting", "version", version, "config", cfg.Summary())
	if cfg.Quota == nil {
		logger.Info("no quota section; idle until stopped")
		<-ctx.Done()
		return nil
	}
	quotaStore, err := quota.Open(cfg.Quota.Store, cfg.StateDir)
	if err != nil {
		return fmt.Errorf("quota store: %w", err)
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var wg sync.WaitGroup
	var once sync.Once
	var failure error
	start := func(name string, run func(context.Context) error) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := run(ctx); err != nil && !errors.Is(err, context.Canceled) {
				once.Do(func() { failure = fmt.Errorf("%s: %w", name, err) })
				cancel()
			}
		}()
	}

	startQuota(cfg, quotaStore, sender.New(cfg.SuiteCloud, version), logger.With("component", "quota"), start)
	wg.Wait()
	logger.Info("suite-cloud-agent stopped")
	return failure
}

// startQuota runs the hook, the limit pull, the usage push and the store, each on its own. A part
// that fails is started again: Stalwart accepts mail while the hook is down. The store stops
// last, so its final save holds every count.
func startQuota(cfg *config.Config, store quota.Store, send *sender.Sender, logger *slog.Logger, start func(string, func(context.Context) error)) {
	touched := &quota.Touched{}
	hook := &quota.Hook{Port: cfg.Quota.HookPort, Token: cfg.Quota.HookToken, Store: store, Touched: touched, Logger: logger, Now: time.Now}
	limits := &quota.Limits{Store: store, Caller: send, HostIP: cfg.Node.HostIP, Interval: cfg.Quota.LimitsPollInterval(), Logger: logger, Now: time.Now}
	usage := &quota.Usage{Store: store, Caller: send, HostIP: cfg.Node.HostIP, Touched: touched, Interval: cfg.Quota.UsagePushInterval(), Logger: logger, Now: time.Now}
	parts := map[string]func(context.Context) error{"quota hook": hook.Run, "quota limits": limits.Run, "quota usage": usage.Run}
	var users sync.WaitGroup
	users.Add(len(parts))
	for name, run := range parts {
		start(name, func(ctx context.Context) error {
			defer users.Done()
			return quota.KeepRunning(ctx, logger.With("part", name), quota.RestartDelay, run)
		})
	}
	start("quota store", func(ctx context.Context) error {
		storeCtx, stop := context.WithCancel(context.WithoutCancel(ctx))
		defer stop()
		go func() {
			users.Wait()
			stop()
		}()
		return quota.KeepRunning(storeCtx, logger.With("part", "quota store"), quota.RestartDelay, store.Run)
	})
}

// Check reports what would stop the agent from running: a state directory that is not writable
// or a quota.json that cannot be read. It makes no request.
func Check(cfg *config.Config) error {
	var problems []error
	if err := os.MkdirAll(cfg.StateDir, 0o750); err != nil {
		problems = append(problems, fmt.Errorf("state_dir: %w", err))
	} else if probe, err := os.CreateTemp(cfg.StateDir, "check.*"); err != nil {
		problems = append(problems, fmt.Errorf("state_dir %s is not writable: %w", cfg.StateDir, err))
	} else {
		probe.Close()
		os.Remove(probe.Name())
	}
	if cfg.Quota != nil && cfg.Quota.Store.Type == "memory" {
		if _, err := quota.OpenMemory(cfg.StateDir); err != nil {
			problems = append(problems, fmt.Errorf("quota store: %w", err))
		}
	}
	return errors.Join(problems...)
}
