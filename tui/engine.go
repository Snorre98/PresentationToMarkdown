package main

// HTTP + WebSocket client for the ptm-engine sidecar (ADR-0025 / ADR-0040).
//
// The TUI is a native client: it never imports converter. Everything it needs —
// file discovery, configuration, and live conversion — comes from the engine's
// HTTP/WS surface documented in engine.py.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// DefaultPort is the engine's bind port (engine.py DEFAULT_PORT).
const DefaultPort = 9091

// Client talks to one ptm-engine over HTTP and WebSocket.
type Client struct {
	baseURL string
	http    *http.Client
}

// NewClient returns a client for an engine at baseURL (e.g. "http://127.0.0.1:9091").
func NewClient(baseURL string) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		http:    &http.Client{Timeout: 30 * time.Second},
	}
}

// DefaultClient returns a client for the engine on localhost:port.
func DefaultClient(port int) *Client {
	return NewClient(fmt.Sprintf("http://127.0.0.1:%d", port))
}

// Health is the engine's /api/health payload.
type Health struct {
	OK     bool   `json:"ok"`
	Engine bool   `json:"engine"`
	Status string `json:"status"`
}

// GlobResult is the engine's /api/fs/glob payload.
type GlobResult struct {
	Path  string   `json:"path"`
	Files []string `json:"files"`
	Error string   `json:"error"`
}

// Config is the subset of config.snapshot() the TUI renders/edits.
type Config struct {
	PDFMode   string          `json:"pdf_mode"`
	NeedGate  string          `json:"need_gate"`
	Duplicate bool            `json:"duplicate"`
	VaultRoot string          `json:"vault_root"`
	Features  map[string]bool `json:"features"`
}

// ConfigUpdate is the engine's /api/config POST body (engine.py config_set).
type ConfigUpdate struct {
	Features  map[string]bool `json:"features,omitempty"`
	PDFMode   string          `json:"pdf_mode,omitempty"`
	VaultRoot string          `json:"vault_root,omitempty"`
	Duplicate *bool           `json:"duplicate,omitempty"`
}

// JobEvent is one WebSocket frame from the engine's /ws stream.
type JobEvent struct {
	Type    string `json:"type"` // file | page | log | done | error
	Idx     int    `json:"idx"`
	Total   int    `json:"total"`
	Name    string `json:"name"`
	Page    int    `json:"page"`
	Kind    string `json:"kind"` // log kind: ok | warn | err
	Message string `json:"message"`
	Ok      int    `json:"ok"`
	Error   string `json:"error"`
}

// Health returns the engine's health.
func (c *Client) Health(ctx context.Context) (Health, error) {
	var h Health
	if err := c.getJSON(ctx, "/api/health", &h); err != nil {
		return Health{}, err
	}
	return h, nil
}

// Glob returns the supported inputs under path (the engine reuses
// converter.collect_inputs, so LaTeX-project folders are single entries).
func (c *Client) Glob(ctx context.Context, path string, recursive bool) (GlobResult, error) {
	q := url.Values{}
	q.Set("path", path)
	if recursive {
		q.Set("recursive", "1")
	} else {
		q.Set("recursive", "0")
	}
	var r GlobResult
	if err := c.getJSON(ctx, "/api/fs/glob?"+q.Encode(), &r); err != nil {
		return GlobResult{}, err
	}
	if r.Error != "" {
		return r, fmt.Errorf("%s", r.Error)
	}
	return r, nil
}

// Config returns the engine's runtime config snapshot.
func (c *Client) Config(ctx context.Context) (Config, error) {
	var cfg Config
	if err := c.getJSON(ctx, "/api/config", &cfg); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// SetConfig applies a partial update and returns the resulting snapshot.
func (c *Client) SetConfig(ctx context.Context, u ConfigUpdate) (Config, error) {
	var cfg Config
	if err := c.postJSON(ctx, "/api/config", u, &cfg); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Open opens a path in the OS default app (Finder).
func (c *Client) Open(ctx context.Context, path string) error {
	var r struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := c.postJSON(ctx, "/api/fs/open", map[string]string{"path": path}, &r); err != nil {
		return err
	}
	if r.Error != "" {
		return fmt.Errorf("%s", r.Error)
	}
	return nil
}

// Shutdown asks a TUI-spawned engine to stop.
func (c *Client) Shutdown(ctx context.Context) error {
	return c.postJSON(ctx, "/api/shutdown", nil, nil)
}

// RunJob streams one conversion over /ws, invoking onEvent per frame until
// "done" or "error". outputDir may be "" (the engine then uses its default).
func (c *Client) RunJob(ctx context.Context, paths []string, outputDir string, duplicate bool, onEvent func(JobEvent)) error {
	conn, _, err := websocket.DefaultDialer.DialContext(ctx, wsURL(c.baseURL), nil)
	if err != nil {
		return err
	}
	defer conn.Close()
	if err := conn.WriteJSON(map[string]any{
		"type":       "start",
		"paths":      paths,
		"output_dir": outputDir,
		"duplicate":  duplicate,
	}); err != nil {
		return err
	}
	for {
		var ev JobEvent
		if err := conn.ReadJSON(&ev); err != nil {
			return err
		}
		if onEvent != nil {
			onEvent(ev)
		}
		if ev.Type == "done" || ev.Type == "error" {
			return nil
		}
	}
}

func wsURL(base string) string {
	u, err := url.Parse(base)
	if err != nil {
		return strings.Replace(base, "http", "ws", 1) + "/ws"
	}
	if u.Scheme == "https" {
		u.Scheme = "wss"
	} else {
		u.Scheme = "ws"
	}
	u.Path = "/ws"
	return u.String()
}

func (c *Client) getJSON(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("GET %s: %s", path, resp.Status)
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) postJSON(ctx context.Context, path string, body, out any) error {
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			return err
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("POST %s: %s", path, resp.Status)
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
