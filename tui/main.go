package main

// Entry point for ptm-tui. Resolves the engine (attach or spawn), then runs the
// Bubble Tea program. Configuration comes from the environment (set by
// scripts/ptm-tui.sh), not from the terminal: PTM_ENGINE_PORT, PTM_ENGINE_CMD.

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"time"

	tea "github.com/charmbracelet/bubbletea"
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

	spawned := false
	if _, err := client.Health(context.Background()); err != nil {
		cmdLine := os.Getenv("PTM_ENGINE_CMD")
		if cmdLine == "" {
			return fmt.Errorf("ptm-engine not reachable on :%d and PTM_ENGINE_CMD is not set", port)
		}
		cmd := exec.Command("sh", "-c", cmdLine+" --port "+strconv.Itoa(port))
		if err := cmd.Start(); err != nil {
			return fmt.Errorf("failed to start ptm-engine: %w", err)
		}
		go cmd.Wait() // reap the child; the engine exits on /api/shutdown
		spawned = true
		if err := waitHealthy(client, 30*time.Second); err != nil {
			return err
		}
	}

	cwd, err := os.Getwd()
	if err != nil {
		return err
	}

	p := tea.NewProgram(newModel(client, cwd, spawned), tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		return err
	}

	if spawned {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = client.Shutdown(ctx)
	}
	return nil
}

func waitHealthy(c *Client, d time.Duration) error {
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
