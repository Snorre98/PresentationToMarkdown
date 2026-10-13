package main

// Entry point for ptm-tui. The Bubble Tea program starts immediately; the
// engine is attached to or spawned by the app itself (ADR-0040/0053), so the
// UI never blocks on engine startup. Configuration comes from the environment
// (set by scripts/ptm-tui.sh): PTM_ENGINE_PORT, PTM_ENGINE_CMD.

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"time"

	tea "charm.land/bubbletea/v2"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "ptm-tui:", err)
		os.Exit(1)
	}
}

func run() error {
	port := DefaultPort
	if v := os.Getenv("PTM_ENGINE_PORT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			port = n
		}
	}
	client := DefaultClient(port)

	cwd, err := os.Getwd()
	if err != nil {
		return err
	}

	m := newModel(client, cwd, port)
	p := tea.NewProgram(m)
	// Watch messages are pushed straight into the update loop.
	m.watch.onMsg = p.Send
	if _, err := p.Run(); err != nil {
		return err
	}

	// Only stop an engine this session spawned; an attached one keeps running
	// (ADR-0040).
	if m.spawned {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = client.Shutdown(ctx)
	}
	return nil
}

// waitHealthy polls the engine until it answers or d runs out.
func waitHealthy(c healthChecker, d time.Duration) error {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		_, err := c.Health(ctx)
		cancel()
		if err == nil {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return fmt.Errorf("ptm-engine did not become healthy within %s", d)
}

func env(key string) string { return os.Getenv(key) }

type healthChecker interface {
	Health(context.Context) (Health, error)
}

// shellCommand builds the engine spawn command from PTM_ENGINE_CMD.
func shellCommand(cmdLine string, port int) *exec.Cmd {
	return exec.Command("sh", "-c", cmdLine+" --port "+strconv.Itoa(port))
}
