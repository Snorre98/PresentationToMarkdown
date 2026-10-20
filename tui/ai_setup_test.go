package main

// ai_setup_test.go — the feature-centric AI requirements view (ADR-0065).

import (
	"strings"
	"testing"
	"time"
)

func TestRenderAISetupShowsPassModelsAndStartCommand(t *testing.T) {
	m := testApp()
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Daemon:      FleetDaemon{URL: "http://127.0.0.1:9300", Reachable: true},
		Source:      "fleet",
		OtherModels: 11,
		Requirements: []FeatureRequirement{
			{
				Key: "vision", Label: "Vision transcription", Enabled: true,
				Readiness: "missing",
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
				Readiness: "ready",
				Services: []RequirementService{
					{Role: "summary", Label: "Summary model", Key: "llama-3.2-3b-instruct-4bit", Name: "Llama 3.2 3B Instruct (4-bit)", State: "up"},
					{Role: "embeddings", Label: "Embeddings model", Key: "embeddinggemma-2-q8-0", Name: "EmbeddingGemma 2 (Q8_0)", State: "up", Optional: true},
				},
			},
		},
	}

	out := m.renderAISetupBox(110, 45)
	for _, want := range []string{
		"AI setup", "Vision transcription", "Presentation summary",
		"Qwen2.5-VL 7B Instruct", "Llama 3.2 3B Instruct", "EmbeddingGemma",
		"1 model(s) needed", "curl -X POST", "+11 other fleet models",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("AI setup missing %q:\n%s", want, out)
		}
	}
	for _, unwanted := range []string{"transcriber", "structure-text", "nomic-embed"} {
		if strings.Contains(out, unwanted) {
			t.Errorf("AI setup leaked internal label %q:\n%s", unwanted, out)
		}
	}
}

func TestRenderAISetupAllPassesOff(t *testing.T) {
	m := testApp()
	m.fleetAt = time.Now()
	m.fleet = FleetResult{
		Source: "fleet",
		Requirements: []FeatureRequirement{
			{
				Key: "vision", Label: "Vision transcription", Enabled: false,
				Readiness: "ready",
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
	if !strings.Contains(out, "Starting the models required") {
		t.Fatalf("starting hint missing:\n%s", out)
	}
	if strings.Contains(out, "All models required by the enabled passes are up") {
		t.Fatalf("starting model reported as up:\n%s", out)
	}
}

func TestRenderAISetupAllRequiredModelsUp(t *testing.T) {
	m := testApp()
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
