package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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

func TestBuildTranscribeArgsPairsSiblingMarkdown(t *testing.T) {
	dir := t.TempDir()
	deck := filepath.Join(dir, "deck.mp3")
	os.WriteFile(deck, nil, 0o644)
	md := filepath.Join(dir, "deck.md")
	os.WriteFile(md, []byte("# Deck"), 0o644)
	standalone := filepath.Join(dir, "week-2.mp3")
	os.WriteFile(standalone, nil, 0o644)

	got := buildTranscribeArgs([]string{deck, standalone}, false, 0, "", "no")
	want := []string{"--env", "AUDIO_LANGUAGE=no", deck, md, standalone}
	if len(got) != len(want) {
		t.Fatalf("args = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("args = %v, want %v", got, want)
		}
	}
}

func TestBuildTranscribeArgsNoSiblingMarkdown(t *testing.T) {
	dir := t.TempDir()
	audio := filepath.Join(dir, "week-2.mp3")
	os.WriteFile(audio, nil, 0o644)
	if got := buildTranscribeArgs([]string{audio}, false, 0, "", "auto"); len(got) != 3 || got[0] != "--env" || got[1] != "AUDIO_LANGUAGE=" || got[2] != audio {
		t.Fatalf("args = %v, want auto language + just the audio", got)
	}
}

func TestBuildTranscribeArgsSpeakersAndDiarize(t *testing.T) {
	dir := t.TempDir()
	audio := filepath.Join(dir, "week-2.mp3")
	os.WriteFile(audio, nil, 0o644)
	md := filepath.Join(dir, "week-2.md")
	os.WriteFile(md, []byte("# Deck"), 0o644)

	got := buildTranscribeArgs([]string{audio}, false, 2, "mlx-community/whisper-large-v3-mlx", "no")
	want := []string{
		"--env", "AUDIO_MODEL=mlx-community/whisper-large-v3-mlx",
		"--env", "AUDIO_LANGUAGE=no",
		"--speakers", "2",
		audio, md,
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("args = %v, want %v", got, want)
		}
	}

	got = buildTranscribeArgs([]string{audio}, true, 0, "", "en")
	want = []string{"--env", "AUDIO_LANGUAGE=en", "--diarize", audio, md}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("args = %v, want %v", got, want)
		}
	}

	got = buildTranscribeArgs([]string{audio}, true, 2, "", "no")
	foundSpeakers := false
	for _, a := range got {
		if a == "--speakers" {
			foundSpeakers = true
		}
	}
	if !foundSpeakers {
		t.Fatalf("args = %v, want --speakers to win over --diarize", got)
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
