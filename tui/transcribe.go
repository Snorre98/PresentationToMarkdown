package main

// Audio transcription support (ADR-0041 → ADR-0045). The TUI does not
// transcribe itself: transcription is an engine-hosted job (ADR-0045), so this
// file only keeps the small helpers that split a selection into conversion vs
// audio inputs and pair an audio file with its sibling Markdown.

import (
	"os"
	"path/filepath"
	"sort"
)

// partitionSelected splits the selection (keyed by absolute path) into
// conversion inputs and audio files by kind (any non-"audio" kind converts).
func partitionSelected(files, kinds []string, selected map[string]bool) (convert, audio []string) {
	for i, f := range files {
		if !selected[f] {
			continue
		}
		if i < len(kinds) && kinds[i] == "audio" {
			audio = append(audio, f)
		} else {
			convert = append(convert, f)
		}
	}
	sort.Strings(convert)
	sort.Strings(audio)
	return convert, audio
}

// siblingMarkdown returns `<stem>.md` beside audio path p, or "" when absent.
func siblingMarkdown(audio string) string {
	stem := audio[:len(audio)-len(filepath.Ext(audio))]
	md := stem + ".md"
	if fi, err := os.Stat(md); err == nil && !fi.IsDir() {
		return md
	}
	return ""
}
