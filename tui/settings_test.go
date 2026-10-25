package main

// settings_test.go — the settings modal now holds output and audio preferences;
// the AI capabilities live in the AI setup view (ADR-0066).

import (
	"strings"
	"testing"
)

func TestSettingsLayoutHasSectionsAndFifteenRows(t *testing.T) {
	m := testApp()
	if got := m.settingsRows(); got != 15 {
		t.Fatalf("settingsRows = %d, want 15", got)
	}
	layout := m.settingsLayout()
	var headers []string
	for _, row := range layout {
		if row.kind == settingsHeader {
			headers = append(headers, row.label)
		}
	}
	if len(headers) != 2 || headers[0] != "Output" || headers[1] != "Audio transcription" {
		t.Fatalf("headers = %v", headers)
	}
	if got := m.settingsLabel(0); got != "pdf mode" {
		t.Fatalf("first row = %q", got)
	}
	if got := m.settingsLabel(1); got != "epub export" {
		t.Fatalf("second row = %q", got)
	}
	if got := m.settingsLabel(m.settingsRows() - 1); got != "language" {
		t.Fatalf("last row = %q", got)
	}
	// Audio preprocessing + pre-flight are TUI settings (ADR-0069).
	for index, want := range map[int]string{
		9:  "dereverb",
		10: "denoise",
		11: "prep timeout",
		12: "check server",
	} {
		if got := m.settingsLabel(index); got != want {
			t.Fatalf("row %d = %q, want %q", index, got, want)
		}
	}
}

func TestSettingsAudioPreprocessingTogglesAndTimeout(t *testing.T) {
	m := testApp()
	m.cfg.AudioPrepTimeout = 300

	m.setCursor = 9 // dereverb
	if _, _ = m.activateSetting(false); !m.cfg.AudioDereverb {
		t.Fatal("dereverb did not toggle on")
	}
	m.setCursor = 10 // denoise
	if _, _ = m.activateSetting(false); !m.cfg.AudioEnhance {
		t.Fatal("denoise did not toggle on")
	}
	if got := m.settingsValueFor(settingsRow{key: "prep_timeout"}); got != "5 min" {
		t.Fatalf("prep timeout = %q, want 5 min", got)
	}
	m.editBuf = "12"
	m.editing = editPrepTimeout
	if cmd := m.commitEdit(); cmd == nil {
		t.Fatal("prep timeout commit did not persist")
	}
	if m.cfg.AudioPrepTimeout != 720 {
		t.Fatalf("prep timeout = %d s, want 720", m.cfg.AudioPrepTimeout)
	}
}

func TestSettingsEpubExportToggleAndPandocHint(t *testing.T) {
	m := testApp()
	m.cfg.Pandoc = true
	m.setCursor = 1
	if _, _ = m.activateSetting(false); !m.cfg.EpubOutput {
		t.Fatal("epub export did not toggle on")
	}
	if got := m.settingsValueFor(settingsRow{key: "epub_output"}); got != "on" {
		t.Fatalf("value = %q, want on", got)
	}
	m.cfg.Pandoc = false
	if got := m.settingsValueFor(settingsRow{key: "epub_output"}); got != "on (pandoc missing)" {
		t.Fatalf("value = %q, want pandoc hint", got)
	}
}

func TestRenderSettingsShowsSectionsAndNoAIRows(t *testing.T) {
	m := testApp()
	m.width, m.height = 100, 40
	m.cfg.PDFMode = "slide"
	m.cfg.PDFCompress = true
	out := m.renderSettingsBox(100, 40)
	for _, want := range []string{
		"settings", "Output", "Audio transcription", "pdf mode",
		"output dir", "audio model", "a AI setup",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("settings box missing %q:\n%s", want, out)
		}
	}
	for _, unwanted := range []string{
		"Vision transcription", "Image classifier", "Markdown restructure",
		"Presentation summary", "Paper structure",
	} {
		if strings.Contains(out, unwanted) {
			t.Errorf("AI row leaked into settings: %q:\n%s", unwanted, out)
		}
	}
}

func TestRenderSettingsKeepsSelectedRowVisibleAtSmallHeight(t *testing.T) {
	m := testApp()
	m.width, m.height = 80, 12
	m.setCursor = m.settingsRows() - 1
	out := m.renderSettingsBox(80, 12)
	if !strings.Contains(out, "language") {
		t.Fatalf("selected last row not visible at 80x12:\n%s", out)
	}
	lines := strings.Split(out, "\n")
	if len(lines) > 12 {
		t.Fatalf("settings box taller than terminal: %d lines", len(lines))
	}
}
