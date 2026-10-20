package main

// settings_test.go — the fleet availability view (ADR-0063): state glyphs and
// the settings section's rows.

import (
	"strings"
	"testing"
	"time"
)

func TestFeatureAvailabilityWorstState(t *testing.T) {
	m := testApp()
	m.fleetAt = time.Now()
	m.fleet = FleetResult{Services: []FleetService{
		{Name: "transcriber", State: "up", Features: []string{"vision"}},
		{Name: "classifier", State: "down", Features: []string{"classify"}},
		{Name: "text", State: "starting", Features: []string{"summary"}},
	}}

	cases := map[string]string{
		"vision":    "●",
		"classify":  "○",
		"summary":   "◌",
		"structure": "…", // no server advertises it
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

func TestRenderSettingsShowsFleetSection(t *testing.T) {
	m := testApp()
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Daemon:      FleetDaemon{URL: "http://127.0.0.1:9300", Reachable: true},
		Source:      "fleet",
		OtherModels: 2,
		Services: []FleetService{
			{
				Name: "text", State: "up", Roles: []string{"summary", "structure-text"},
				Features: []string{"summary", "format", "structure"}, Needed: true,
			},
			{
				Name: "audio", State: "down", Roles: []string{"audio", "nb-whisper-large"},
				Selected: true,
			},
			{Name: "mistral-24b", State: "up"},
		},
	}
	out := m.renderSettingsBox(100, 40)
	for _, want := range []string{
		"fleet", "flaate", "text", "summary", "structure-text", "needed",
		"audio", "ASR nb-whisper (selected)", "+2 other models",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("settings box missing %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "mistral-24b") {
		t.Errorf("role-less manifest model leaked into the PtM view:\n%s", out)
	}

	m.fleet.Source = "static"
	m.fleet.Daemon.Reachable = false
	if out := m.renderSettingsBox(100, 40); !strings.Contains(out, "static probes") {
		t.Errorf("static fallback not labelled:\n%s", out)
	}
}

func TestServiceDetailSelectedWhisper(t *testing.T) {
	got := serviceDetail(FleetService{
		Name: "whisper-large-v3", Selected: true, Roles: []string{"whisper-large-v3"},
	})
	if got != "ASR (selected)" {
		t.Fatalf("detail = %q", got)
	}
}
