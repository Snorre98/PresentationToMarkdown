package main

// Audio transcription support (ADR-0041). The TUI does not transcribe itself:
// it spawns the venv's `ptm-transcribe` (PTM_TRANSCRIBE_CMD) so the decoupled
// pipeline (ADR-0009) — ffmpeg + mlx-whisper, the single-instance flock, and
// the warn-degradation floor — is preserved. This file only builds the child
// command and resolves the binary; the run/progress plumbing lives in model.go.

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
)

// DefaultTranscribeBin is used when PTM_TRANSCRIBE_CMD is unset (a venv-less
// manual `go run`); the launcher script always sets the venv binary.
const DefaultTranscribeBin = "ptm-transcribe"

// TranscribeBin returns the ptm-transcribe binary from the environment, or the
// PATH default when PTM_TRANSCRIBE_CMD is unset.
func TranscribeBin() string {
	if v := os.Getenv("PTM_TRANSCRIBE_CMD"); v != "" {
		return v
	}
	return DefaultTranscribeBin
}

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

// buildTranscribeArgs turns selected audio paths into `ptm-transcribe` argv.
//
// Each audio file is passed to ptm-transcribe (which pairs by stem itself via
// its collect_targets / md_by_stem logic). When a sibling `<stem>.md` exists,
// that Markdown is passed too so the transcript is attached to the deck
// (`deck.md` + `deck.mp3` -> "# Transcript" section) rather than emitted as a
// standalone `<stem>.transcript.md`.
//
// Speaker diarization is controlled by the TUI settings: `speakers` pins an
// exact count (`--speakers N`, which implies diarization) and `diarize` toggles
// labelling when no exact count is set.
func buildTranscribeArgs(audioPaths []string, diarize bool, speakers int) []string {
	args := make([]string, 0, len(audioPaths)*2)
	if speakers > 0 {
		args = append(args, "--speakers", strconv.Itoa(speakers))
	} else if diarize {
		args = append(args, "--diarize")
	}
	for _, p := range audioPaths {
		args = append(args, p)
		if md := siblingMarkdown(p); md != "" {
			args = append(args, md)
		}
	}
	return args
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
