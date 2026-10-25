package main

// ai_setup_test.go — the AI control surface (ADR-0065/0066).

import (
	"strings"
	"testing"
	"time"
)

func TestRenderAISetupShowsPassDescriptionModelsAndCommand(t *testing.T) {
	m := testApp()
	m.width, m.height = 110, 45
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Daemon:      FleetDaemon{URL: "http://127.0.0.1:9300", Reachable: true},
		Source:      "fleet",
		OtherModels: 11,
		Requirements: []FeatureRequirement{
			{
				Key: "vision", Label: "Vision transcription", Enabled: true,
				Description: "Reads images, diagrams and charts and turns them into Markdown text or a short description.",
				Readiness:   "missing",
				Services: []RequirementService{
					{
						Role: "transcriber", Label: "Vision + rewrite model",
						Key: "qwen2.5-vl-7b-instruct-4bit", Name: "Qwen2.5-VL 7B Instruct (4-bit)",
						State: "down",
						Start: "curl -X POST http://127.0.0.1:9300/start/qwen2.5-vl-7b-instruct-4bit",
					},
				},
			},
			{
				Key: "summary", Label: "Presentation summary", Enabled: false,
				Description: "Adds a summary header with abstract, key topics, takeaways and key terms to each converted file.",
				Readiness:   "ready",
				Services: []RequirementService{
					{Role: "summary", Label: "Summary model", Key: "llama-3.2-3b-instruct-4bit", Name: "Llama 3.2 3B Instruct (4-bit)", State: "up"},
					{Role: "embeddings", Label: "Embeddings model", Key: "embeddinggemma-2-q8-0", Name: "EmbeddingGemma 2 (Q8_0)", State: "up", Optional: true},
				},
			},
		},
	}

	out := m.renderAISetupBox(110, 45)
	for _, want := range []string{
		"AI setup", "Document passes", "Vision transcription", "Presentation summary",
		"Reads images, diagrams and charts", "Qwen2.5-VL 7B Instruct",
		"Llama 3.2 3B Instruct", "EmbeddingGemma",
		"1 model needed", "curl -X POST", "+11 other fleet models",
		"space toggle",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("AI setup missing %q:\n%s", want, out)
		}
	}
	if strings.Count(out, "AI setup") != 1 {
		t.Errorf("AI setup title is duplicated:\n%s", out)
	}
	for _, unwanted := range []string{"transcriber", "structure-text", "nomic-embed"} {
		if strings.Contains(out, unwanted) {
			t.Errorf("AI setup leaked internal label %q:\n%s", unwanted, out)
		}
	}
}

func TestRenderAISetupAllPassesOff(t *testing.T) {
	m := testApp()
	m.width, m.height = 100, 40
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Source: "fleet",
		Requirements: []FeatureRequirement{
			{
				Key: "vision", Label: "Vision transcription", Enabled: false,
				Description: "Reads images, diagrams and charts into Markdown.",
				Readiness:   "ready",
				Services: []RequirementService{
					{Key: "qwen2.5-vl-7b-instruct-4bit", Name: "Qwen2.5-VL 7B", State: "up"},
				},
			},
		},
	}
	out := m.renderAISetupBox(100, 40)
	if !strings.Contains(out, "No AI passes are enabled") {
		t.Fatalf("all-off hint missing:\n%s", out)
	}
	if strings.Contains(out, "model(s) needed") {
		t.Fatalf("all-off view shows a missing-model list:\n%s", out)
	}
}

func TestRenderAISetupStartingModel(t *testing.T) {
	m := testApp()
	m.width, m.height = 100, 40
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Source: "fleet",
		Requirements: []FeatureRequirement{
			{
				Key: "vision", Label: "Vision transcription", Enabled: true,
				Readiness: "pending",
				Services: []RequirementService{
					{Key: "qwen2.5-vl-7b-instruct-4bit", Name: "Qwen2.5-VL 7B", State: "starting"},
				},
			},
		},
	}
	out := m.renderAISetupBox(100, 40)
	if !strings.Contains(out, "Waiting for the models required") {
		t.Fatalf("starting hint missing:\n%s", out)
	}
	if strings.Contains(out, "All models required by the enabled passes are up") {
		t.Fatalf("starting model reported as up:\n%s", out)
	}
}

func TestRenderAISetupAllRequiredModelsUp(t *testing.T) {
	m := testApp()
	m.width, m.height = 100, 40
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Source: "fleet",
		Requirements: []FeatureRequirement{
			{
				Key: "vision", Label: "Vision transcription", Enabled: true,
				Readiness: "ready",
				Services: []RequirementService{
					{Key: "qwen2.5-vl-7b-instruct-4bit", Name: "Qwen2.5-VL 7B", State: "up"},
				},
			},
		},
	}
	out := m.renderAISetupBox(100, 40)
	if !strings.Contains(out, "All models required by the enabled passes are up") {
		t.Fatalf("ready hint missing:\n%s", out)
	}
}

func TestToggleAIPassMirrorsClassifierImpliesVision(t *testing.T) {
	m := testApp()
	m.cfg.Features = map[string]bool{"vision": false, "classify": false}
	m.fleet.Requirements = []FeatureRequirement{
		{Key: "vision", Label: "Vision transcription", Enabled: false},
		{Key: "classify", Label: "Image classifier", Enabled: false},
	}
	m.aiCursor = 1
	_ = m.toggleAIPass()
	if !m.cfg.Features["classify"] || !m.cfg.Features["vision"] {
		t.Fatalf("classifier toggle did not enable vision: %v", m.cfg.Features)
	}
	if !m.fleet.Requirements[0].Enabled || !m.fleet.Requirements[1].Enabled {
		t.Fatalf("requirements not updated: %+v", m.fleet.Requirements)
	}

	m.aiCursor = 0
	_ = m.toggleAIPass()
	if m.cfg.Features["vision"] || m.cfg.Features["classify"] {
		t.Fatalf("disabling vision did not disable classifier: %v", m.cfg.Features)
	}
}

func TestRenderAISetupSmallTerminalKeepsSelectedPassVisible(t *testing.T) {
	m := testApp()
	m.width, m.height = 60, 12
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Source: "fleet",
		Requirements: []FeatureRequirement{
			{Key: "vision", Label: "Vision transcription", Enabled: true, Description: "Reads images into Markdown.", Readiness: "ready",
				Services: []RequirementService{{Key: "qwen", Name: "Qwen2.5-VL 7B", State: "up"}}},
			{Key: "summary", Label: "Presentation summary", Enabled: false, Description: "Adds a summary header.", Readiness: "ready",
				Services: []RequirementService{{Key: "llama", Name: "Llama 3.2 3B", State: "up"}}},
		},
	}
	m.aiCursor = 1
	out := m.renderAISetupBox(60, 12)
	if !strings.Contains(out, "Presentation summary") {
		t.Fatalf("selected second pass not visible at 60x12:\n%s", out)
	}
	if lines := strings.Split(out, "\n"); len(lines) > 12 {
		t.Fatalf("AI setup box taller than terminal: %d lines", len(lines))
	}
}
