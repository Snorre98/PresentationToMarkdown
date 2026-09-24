package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gorilla/websocket"
)

func TestHealth(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/health" {
			json.NewEncoder(w).Encode(map[string]any{"ok": true, "engine": true, "status": "idle"})
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	h, err := c.Health(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !h.OK || h.Engine != true || h.Status != "idle" {
		t.Fatalf("unexpected health: %+v", h)
	}
}

func TestGlob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/fs/glob" {
			json.NewEncoder(w).Encode(map[string]any{
				"path":  r.URL.Query().Get("path"),
				"files": []string{"/a/deck.pptx", "/a/paper.pdf"},
			})
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	r, err := c.Glob(context.Background(), "/a", true)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Files) != 2 {
		t.Fatalf("files = %v", r.Files)
	}
}

func TestGlobPropagatesError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"path": "/nope", "error": "not a directory"})
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	if _, err := c.Glob(context.Background(), "/nope", true); err == nil {
		t.Fatal("expected error, got nil")
	}
}

func TestConfigRoundTrip(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			json.NewEncoder(w).Encode(map[string]any{
				"pdf_mode": "slide", "need_gate": "on", "duplicate": false,
				"features": map[string]bool{"vision": true, "format": false},
			})
			return
		}
		var body ConfigUpdate
		json.NewDecoder(r.Body).Decode(&body)
		json.NewEncoder(w).Encode(map[string]any{
			"pdf_mode": body.PDFMode, "features": body.Features,
		})
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	cfg, err := c.Config(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PDFMode != "slide" || cfg.Features["vision"] != true {
		t.Fatalf("cfg = %+v", cfg)
	}

	updated, err := c.SetConfig(context.Background(), ConfigUpdate{
		PDFMode:  "paper",
		Features: map[string]bool{"vision": false},
	})
	if err != nil {
		t.Fatal(err)
	}
	if updated.PDFMode != "paper" || updated.Features["vision"] != false {
		t.Fatalf("updated = %+v", updated)
	}
}

func TestRunJobWS(t *testing.T) {
	upgrader := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		var start map[string]any
		if err := conn.ReadJSON(&start); err != nil {
			return
		}
		if start["type"] != "start" {
			return
		}
		conn.WriteJSON(map[string]any{"type": "file", "idx": 1, "total": 1, "name": "deck.pptx"})
		conn.WriteJSON(map[string]any{"type": "page", "page": 3, "total": 10, "name": "deck.pptx"})
		conn.WriteJSON(map[string]any{"type": "done", "ok": 1, "total": 1})
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	var events []JobEvent
	err := c.RunJob(context.Background(), []string{"/a/deck.pptx"}, "", false, func(ev JobEvent) {
		events = append(events, ev)
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 3 {
		t.Fatalf("events = %d", len(events))
	}
	if events[0].Type != "file" || events[1].Type != "page" || events[2].Type != "done" {
		t.Fatalf("events = %+v", events)
	}
}

func TestWsURL(t *testing.T) {
	if got := wsURL("http://127.0.0.1:9091"); got != "ws://127.0.0.1:9091/ws" {
		t.Fatalf("wsURL = %q", got)
	}
	if got := wsURL("https://example.com"); got != "wss://example.com/ws" {
		t.Fatalf("wsURL = %q", got)
	}
}

func TestStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/status" {
			http.NotFound(w, r)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{
			"status": "running",
			"current_job": map[string]any{
				"kind": "transcribe", "status": "running",
				"paths": []string{"/a/21-sep.m4a"}, "idx": 1, "total": 1,
				"phase": "diarize",
			},
		})
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	s, err := c.Status(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if s.Status != "running" || s.CurrentJob == nil {
		t.Fatalf("status = %+v", s)
	}
	if s.CurrentJob.Kind != "transcribe" || s.CurrentJob.Phase != "diarize" {
		t.Fatalf("job = %+v", s.CurrentJob)
	}
}

func TestRunTranscribeJobWS(t *testing.T) {
	upgrader := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		var start map[string]any
		if err := conn.ReadJSON(&start); err != nil {
			return
		}
		if start["type"] != "transcribe" {
			return
		}
		conn.WriteJSON(map[string]any{"type": "file", "idx": 1, "total": 1, "name": "21-sep.m4a"})
		conn.WriteJSON(map[string]any{"type": "phase", "phase": "diarize"})
		conn.WriteJSON(map[string]any{"type": "done", "ok": 1, "total": 1})
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	var events []JobEvent
	err := c.RunTranscribeJob(context.Background(), []string{"/a/21-sep.m4a"}, map[string]any{"model": "nb-whisper-large"}, func(ev JobEvent) {
		events = append(events, ev)
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 3 {
		t.Fatalf("events = %d", len(events))
	}
	if events[0].Type != "file" || events[1].Type != "phase" || events[1].Phase != "diarize" || events[2].Type != "done" {
		t.Fatalf("events = %+v", events)
	}
}
