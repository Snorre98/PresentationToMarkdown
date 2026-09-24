package main

// Audio transcription support (ADR-0041 → ADR-0045). The TUI does not
// transcribe itself and no longer spawns `ptm-transcribe`: transcription is an
// engine-hosted job (ADR-0045), so this file only keeps the small helpers that
// split a selection into conversion vs audio inputs and pair an audio file with
// its sibling Markdown.

import (
	"os"
	"path/filepath"
	"sort"
)

// partitionSelected splits the selected indices into conversion inputs and
// audio files by kind (any non-"audio" kind converts).
func partitionSelected(files, kinds []string, selected map[int]bool) (convert, audio []string) {
	for idx := range selected {
		if idx >= len(files) || idx >= len(kinds) {
			continue
		}
		if kinds[idx] == "audio" {
			audio = append(audio, files[idx])
		} else {
			convert = append(convert, files[idx])
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
