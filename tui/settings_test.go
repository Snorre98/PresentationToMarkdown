package main

// settings_test.go — the settings modal labels and the feature availability
// glyphs derived from the server-computed requirements (ADR-0065).

import (
	"strings"
	"testing"
	"time"
)

func TestFeatureAvailabilityUsesResolvedReadiness(t *testing.T) {
	m := testApp()
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Services: []FleetService{
			{Name: "Qwen2.5-VL 7B", State: "up", Features: []string{"vision"}},
			{Name: "Qwen2.5-VL 3B", State: "down", Features: []string{"vision", "classify"}},
		},
		Requirements: []FeatureRequirement{
			{Key: "vision", Label: "Vision transcription", Readiness: "ready"},
			{Key: "classify", Label: "Image classifier", Readiness: "missing"},
			{Key: "summary", Label: "Presentation summary", Readiness: "pending"},
		},
	}

	cases := map[string]string{
		"vision":    "●",
		"classify":  "○",
		"summary":   "◌",
		"structure": "…", // no requirement advertised
	}
	for key, want := range cases {
		got, _ := m.featureAvailability(key)
		if got != want {
			t.Errorf("featureAvailability(%q) = %q, want %q", key, got, want)
		}
	}
}

func TestFeatureAvailabilityLoading(t *testing.T) {
	m := testApp()
	if got, _ := m.featureAvailability("vision"); got != "…" {
		t.Fatalf("loading glyph = %q", got)
	}
}

func TestRenderSettingsShowsLabelsAndAISetupHint(t *testing.T) {
	m := testApp()
	m.cfg.FeatureInfo = []FeatureInfo{
		{Key: "vision", Label: "Vision transcription", Scope: "all"},
		{Key: "classify", Label: "Image classifier", Scope: "all"},
		{Key: "interpret", Label: "Diagram interpretation", Scope: "all"},
		{Key: "format", Label: "Markdown restructure", Scope: "all"},
		{Key: "summary", Label: "Presentation summary", Scope: "all"},
		{Key: "structure", Label: "Paper structure", Scope: "paper"},
	}
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Daemon:      FleetDaemon{URL: "http://127.0.0.1:9300", Reachable: true},
		Source:      "fleet",
		OtherModels: 2,
		Requirements: []FeatureRequirement{
			{
				Key: "vision", Label: "Vision transcription", Enabled: true,
				Readiness: "missing",
				Services: []RequirementService{
					{Key: "qwen2.5-vl-7b-instruct-4bit", Name: "Qwen2.5-VL 7B", State: "down"},
				},
			},
		},
	}
	out := m.renderSettingsBox(100, 40)
	for _, want := range []string{
		"Vision transcription", "Markdown restructure", "Presentation summary",
		"Paper structure", "audio model", "AI setup", "a AI setup",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("settings box missing %q:\n%s", want, out)
		}
	}
	for _, unwanted := range []string{"transcriber", "structure-text", "nomic-embed"} {
		if strings.Contains(out, unwanted) {
			t.Errorf("settings box leaked internal label %q:\n%s", unwanted, out)
		}
	}
}
