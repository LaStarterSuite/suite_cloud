// Command suite-cloud-agent enforces Suite Cloud's sending limits on a Stalwart node.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/frappe/suite_cloud/agent/internal/agent"
	"github.com/frappe/suite_cloud/agent/internal/config"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "dev"

func main() {
	configPath := flag.String("config", config.DefaultPath, "path of config.json")
	check := flag.Bool("check", false, "validate the config and the state directory, then exit")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		return
	}
	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "suite-cloud-agent:", err)
		os.Exit(2)
	}
	if *check {
		if err := agent.Check(cfg); err != nil {
			fmt.Fprintln(os.Stderr, "suite-cloud-agent:", err)
			os.Exit(1)
		}
		fmt.Println("ok", cfg.Summary())
		return
	}

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level(cfg.LogLevel)}))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := agent.Run(ctx, cfg, version, logger); err != nil {
		logger.Error("suite-cloud-agent failed", "error", err)
		os.Exit(1)
	}
}

func level(name string) slog.Level {
	switch name {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	}
	return slog.LevelInfo
}
