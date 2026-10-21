package main

// settings_test.go — the settings modal now holds output and audio preferences;
// the AI capabilities live in the AI setup view (ADR-0066).

import (
	"strings"
	"testing"
)

func TestSettingsLayoutHasSectionsAndTenRows(t *testing.T) {
	m := testApp()
	if got := m.settingsRows(); got != 10 {
		t.Fatalf("settingsRows = %d, want 10", got)
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
	if got := m.settingsLabel(m.settingsRows() - 1); got != "language" {
		t.Fatalf("last row = %q", got)
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
