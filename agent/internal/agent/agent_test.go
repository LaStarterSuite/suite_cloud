package agent

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
)

func TestRunWithoutQuotaIdlesUntilStopped(t *testing.T) {
	cfg := &config.Config{StateDir: t.TempDir()}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- Run(ctx, cfg, "test", slog.New(slog.NewTextHandler(io.Discard, nil))) }()
	select {
	case err := <-done:
		t.Fatalf("returned before the stop: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
