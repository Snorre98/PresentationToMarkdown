package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestGlobAudio(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/fs/glob" {
			http.NotFound(w, r)
			return
		}
		kinds := r.URL.Query().Get("kinds")
		if kinds != "audio" {
			t.Errorf("kinds = %q, want audio", kinds)
		}
		json.NewEncoder(w).Encode(map[string]any{
			"path":  r.URL.Query().Get("path"),
			"files": []string{"/a/lecture.mp3", "/a/week-2.m4a"},
		})
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	r, err := c.GlobAudio(context.Background(), "/a", true)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Files) != 2 || r.Files[0] != "/a/lecture.mp3" {
		t.Fatalf("files = %v", r.Files)
	}
}

func TestPartitionSelected(t *testing.T) {
	files := []string{"/a/deck.pptx", "/a/notes.md", "/a/lecture.mp3", "/a/week-2.m4a"}
	kinds := []string{"convert", "convert", "audio", "audio"}
	selected := map[int]bool{0: true, 2: true, 3: true}

	convert, audio := partitionSelected(files, kinds, selected)
	if len(convert) != 1 || convert[0] != "/a/deck.pptx" {
		t.Fatalf("convert = %v", convert)
	}
	if len(audio) != 2 || audio[0] != "/a/lecture.mp3" || audio[1] != "/a/week-2.m4a" {
		t.Fatalf("audio = %v", audio)
	}
}

func TestPartitionSelectedSorts(t *testing.T) {
	files := []string{"/b/2.mp3", "/a/1.mp3", "/a/deck.pptx"}
	kinds := []string{"audio", "audio", "convert"}
	selected := map[int]bool{0: true, 1: true, 2: true}

	_, audio := partitionSelected(files, kinds, selected)
	if len(audio) != 2 || audio[0] != "/a/1.mp3" || audio[1] != "/b/2.mp3" {
		t.Fatalf("audio = %v", audio)
	}
}
