package main

// watch.go — the persistent engine subscription (ADR-0053).
//
// One WebSocket serves the whole session: it registers as a watcher (status
// snapshot plus every live job frame, local or external) and carries the
// start/cancel requests of this session's jobs. It reconnects with backoff, so
// the app survives an engine restart; while it is down the app refuses to
// dispatch and shows the connection state.

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/gorilla/websocket"
)

// watchStatusMsg carries the full job snapshot the engine sends right after a
// successful (re)subscribe.
type watchStatusMsg struct {
	status Status
}

// watchConnMsg reports the watch socket going up or down.
type watchConnMsg struct {
	connected bool
	err       error
}

type watchClient struct {
	client *Client

	// onMsg delivers messages into the Bubble Tea update loop. It is set to
	// tea.Program.Send once the program exists; watchClient never blocks on a
	// channel, so a dropped re-arm cannot stall the stream.
	onMsg func(tea.Msg)

	mu        sync.Mutex
	conn      *websocket.Conn
	connected bool
}

func newWatchClient(c *Client) *watchClient {
	return &watchClient{client: c}
}

// run dials the engine and streams messages until ctx is cancelled, retrying
// with bounded backoff while the engine is unavailable.
func (w *watchClient) run(ctx context.Context) {
	backoff := 500 * time.Millisecond
	for ctx.Err() == nil {
		err := w.session(ctx)
		if ctx.Err() != nil {
			return
		}
		w.setConnected(false, err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff < 5*time.Second {
			backoff *= 2
		}
	}
}

// session runs one connected stretch: dial, subscribe, read until an error.
func (w *watchClient) session(ctx context.Context) error {
	conn, _, err := websocket.DefaultDialer.DialContext(ctx, wsURL(w.client.baseURL), nil)
	if err != nil {
		return err
	}
	defer conn.Close()

	w.mu.Lock()
	w.conn, w.connected = conn, true
	w.mu.Unlock()
	// Reset the backoff on a successful connect.
	defer w.setConnected(false, nil)

	if err := conn.WriteJSON(map[string]any{"type": "watch"}); err != nil {
		return err
	}
	w.emit(watchConnMsg{connected: true})

	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		var probe struct {
			Type  string          `json:"type"`
			State json.RawMessage `json:"state"`
		}
		if err := json.Unmarshal(data, &probe); err != nil {
			continue
		}
		if probe.Type == "status" {
			var st Status
			if err := json.Unmarshal(probe.State, &st); err == nil {
				w.emit(watchStatusMsg{status: st})
			}
			continue
		}
		var ev JobEvent
		if err := json.Unmarshal(data, &ev); err == nil {
			w.emit(ev)
		}
	}
}

func (w *watchClient) emit(msg tea.Msg) {
	if w.onMsg != nil {
		w.onMsg(msg)
	}
}

func (w *watchClient) setConnected(connected bool, err error) {
	w.mu.Lock()
	was := w.connected
	w.connected = connected
	if !connected {
		w.conn = nil
	}
	w.mu.Unlock()
	if !connected && was {
		w.emit(watchConnMsg{connected: false, err: err})
	}
}

func (w *watchClient) isConnected() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.connected
}

// send writes one control message (start/transcribe) on the watch socket.
func (w *watchClient) send(payload map[string]any) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn == nil {
		return fmt.Errorf("engine is not connected")
	}
	return w.conn.WriteJSON(payload)
}
