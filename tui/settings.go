package main

// settings.go — the settings modal: AI feature toggles, output preferences and
// transcription settings (ADR-0043), plus a one-line pointer to the AI setup
// view (ADR-0065). Every change round-trips through the engine's /api/config so
// it persists in the shared settings store (converter.settings).

import (
	"context"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

var featureOrder = []string{"vision", "classify", "interpret", "format", "summary", "structure"}

// fallbackFeatureLabels mirrors config.FEATURES for an older engine that does
// not send feature_info yet. The engine's labels win (ADR-0065).
var fallbackFeatureLabels = map[string]string{
	"vision":    "Vision transcription",
	"classify":  "Image classifier",
	"interpret": "Diagram interpretation",
	"format":    "Markdown restructure",
	"summary":   "Presentation summary",
	"structure": "Paper structure",
}

func (m *model) featureLabel(key string) string {
	for _, info := range m.cfg.FeatureInfo {
		if info.Key == key && info.Label != "" {
			return info.Label
		}
	}
	if label, ok := fallbackFeatureLabels[key]; ok {
		return label
	}
	return key
}

func (m *model) featureScope(key string) string {
	for _, info := range m.cfg.FeatureInfo {
		if info.Key == key {
			return info.Scope
		}
	}
	return ""
}

// Rows after the features: pdf_mode, image_links, compress, inference
// overwrite, duplicate, diarize, speakers, model, language, output_dir.
const settingsExtraRows = 10

// audioModels is the cycle of fleet ASR model names the TUI offers
// (ADR-0043/0044/0062). The first is the machine default: NB-Whisper
// (Norwegian, audio server /v1/asr); the whisper models are served by the
// fleet's `whisper` daemon.
var audioModels = []string{
	"nb-whisper-large",                // norwegian (audio server ASR)
	"whisper-large-v3-asr-fp16",       // max quality (fleet whisper daemon)
	"whisper-large-v3-turbo-asr-fp16", // speed (fleet whisper daemon)
}

type editField int

const (
	editNone editField = iota
	editOutput
	editLanguage
	editSpeakers
)

type fleetMsg struct {
	res FleetResult
	err error
}

func (m *model) fetchFleet() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		res, err := m.client.Fleet(ctx)
		return fleetMsg{res, err}
	}
}

func (m *model) settingsRows() int {
	return len(featureOrder) + settingsExtraRows
}

func (m *model) settingsLabel(i int) string {
	if i < len(featureOrder) {
		return m.featureLabel(featureOrder[i])
	}
	switch i - len(featureOrder) {
	case 0:
		return "pdf mode"
	case 1:
		return "image links"
	case 2:
		return "compress pdf"
	case 3:
		return "overwrite infer"
	case 4:
		return "duplicate"
	case 5:
		return "diarize"
	case 6:
		return "speakers"
	case 7:
		return "audio model"
	case 8:
		return "language"
	default:
		return "output dir"
	}
}

// modelShort renders a compact label for the current ASR model.
func modelShort(id string) string {
	if id == "nb-whisper-large" {
		return "NB-Whisper (Norwegian)"
	}
	if id == "whisper-large-v3-turbo-asr-fp16" {
		return "Whisper turbo (fast)"
	}
	return "Whisper large-v3 (best)"
}

func (m *model) settingsValue(i int) string {
	if i < len(featureOrder) {
		return boolLabel(m.cfg.Features[featureOrder[i]])
	}
	switch i - len(featureOrder) {
	case 0:
		return m.cfg.PDFMode
	case 1:
		return boolLabel(m.cfg.ImageLinks)
	case 2:
		return boolLabel(m.cfg.PDFCompress)
	case 3:
		return boolLabel(m.cfg.InferenceOverwrite)
	case 4:
		return boolLabel(m.duplicate)
	case 5:
		return boolLabel(m.cfg.AudioDiarize)
	case 6:
		if m.editing == editSpeakers {
			return m.editBuf + "▌"
		}
		if m.cfg.AudioSpeakers <= 0 {
			return "auto"
		}
		return strconv.Itoa(m.cfg.AudioSpeakers)
	case 7:
		return modelShort(m.cfg.AudioModel)
	case 8:
		if m.editing == editLanguage {
			return m.editBuf + "▌"
		}
		if m.cfg.AudioLanguage == "" {
			return "auto"
		}
		return m.cfg.AudioLanguage
	default:
		if m.editing == editOutput {
			return m.editBuf + "▌"
		}
		if m.outputDir == "" {
			return "(default: beside source; source → source/)"
		}
		return m.outputDir
	}
}

func boolLabel(b bool) string {
	if b {
		return "on"
	}
	return "off"
}

func (m *model) updateSettings(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	if m.editing != editNone {
		switch msg.String() {
		case "esc":
			m.editing = editNone
		case "enter":
			return m, m.commitEdit()
		case "backspace":
			if len(m.editBuf) > 0 {
				m.editBuf = m.editBuf[:len(m.editBuf)-1]
			}
		default:
			if msg.Text != "" {
				if m.editing == editSpeakers {
					for _, r := range msg.Text {
						if r >= '0' && r <= '9' {
							m.editBuf += string(r)
						}
					}
				} else {
					m.editBuf += msg.Text
				}
			}
		}
		return m, nil
	}

	switch msg.String() {
	case "esc", "s", "q":
		m.overlay = overlayNone
		return m, nil
	case "up", "k":
		if m.setCursor > 0 {
			m.setCursor--
		}
	case "down", "j":
		if m.setCursor < m.settingsRows()-1 {
			m.setCursor++
		}
	case "r":
		return m, m.fetchFleet()
	case "a":
		m.overlay = overlayAI
		m.aiScroll = 0
		return m, m.fetchFleet()
	case "e":
		return m.activateSetting(true)
	case "enter", " ", "space":
		return m.activateSetting(false)
	}
	return m, nil
}

func (m *model) activateSetting(editOnly bool) (tea.Model, tea.Cmd) {
	i := m.setCursor
	if i < len(featureOrder) {
		if editOnly {
			return m, nil
		}
		key := featureOrder[i]
		if m.cfg.Features == nil {
			m.cfg.Features = map[string]bool{}
		}
		next := !m.cfg.Features[key]
		m.cfg.Features[key] = next
		return m, m.saveConfig(ConfigUpdate{Features: map[string]bool{key: next}})
	}
	switch i - len(featureOrder) {
	case 0: // pdf_mode
		if editOnly {
			return m, nil
		}
		next := "paper"
		if m.cfg.PDFMode == "paper" {
			next = "slide"
		}
		m.cfg.PDFMode = next
		return m, m.saveConfig(ConfigUpdate{PDFMode: next})
	case 1: // image_links
		if editOnly {
			return m, nil
		}
		next := !m.cfg.ImageLinks
		m.cfg.ImageLinks = next
		return m, m.saveConfig(ConfigUpdate{ImageLinks: &next})
	case 2: // compress archived PDFs
		if editOnly {
			return m, nil
		}
		next := !m.cfg.PDFCompress
		m.cfg.PDFCompress = next
		return m, m.saveConfig(ConfigUpdate{PDFCompress: &next})
	case 3: // overwrite an existing -inference-used.md (ADR-0050)
		if editOnly {
			return m, nil
		}
		next := !m.cfg.InferenceOverwrite
		m.cfg.InferenceOverwrite = next
		return m, m.saveConfig(ConfigUpdate{InferenceOverwrite: &next})
	case 4: // duplicate
		if editOnly {
			return m, nil
		}
		next := !m.duplicate
		m.duplicate = next
		return m, m.saveConfig(ConfigUpdate{Duplicate: &next})
	case 5: // diarize
		if editOnly {
			return m, nil
		}
		next := !m.cfg.AudioDiarize
		m.cfg.AudioDiarize = next
		return m, m.saveConfig(ConfigUpdate{AudioDiarize: &next})
	case 6: // speakers
		m.editing = editSpeakers
		m.editBuf = strconv.Itoa(m.cfg.AudioSpeakers)
		return m, nil
	case 7: // model — cycle between the fleet ASR models
		if editOnly {
			return m, nil
		}
		next := audioModels[0]
		for i, name := range audioModels {
			if m.cfg.AudioModel == name {
				next = audioModels[(i+1)%len(audioModels)]
				break
			}
		}
		m.cfg.AudioModel = next
		return m, m.saveConfig(ConfigUpdate{AudioModel: &next})
	case 8: // language
		m.editing = editLanguage
		m.editBuf = m.cfg.AudioLanguage
		if m.editBuf == "" {
			m.editBuf = "auto"
		}
		return m, nil
	default: // output_dir
		m.editing = editOutput
		m.editBuf = m.outputDir
		return m, nil
	}
}

// commitEdit persists the active inline edit.
func (m *model) commitEdit() tea.Cmd {
	field := m.editing
	m.editing = editNone
	switch field {
	case editSpeakers:
		n := 0
		if v, err := strconv.Atoi(m.editBuf); err == nil && v >= 1 {
			n = v
		}
		m.cfg.AudioSpeakers = n
		return m.saveConfig(ConfigUpdate{AudioSpeakers: &n})
	case editLanguage:
		val := strings.TrimSpace(m.editBuf)
		if val == "" {
			val = "auto"
		}
		m.cfg.AudioLanguage = val
		return m.saveConfig(ConfigUpdate{AudioLanguage: &val})
	case editOutput:
		m.outputDir = strings.TrimSpace(m.editBuf)
	}
	return nil
}

func (m *model) saveConfig(u ConfigUpdate) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		cfg, err := m.client.SetConfig(ctx, u)
		return cfgMsg{cfg, err}
	}
}

// --- rendering ---------------------------------------------------------------

func (m *model) renderSettingsBox(width, height int) string {
	var b strings.Builder
	b.WriteString(th.title.Render("settings"))
	b.WriteString("\n\n")

	inner := width - 6
	if inner < 30 {
		inner = 30
	}
	for i := 0; i < m.settingsRows(); i++ {
		label := m.settingsLabel(i)
		val := m.settingsValue(i)
		if i < len(featureOrder) {
			glyph, style := m.featureAvailability(featureOrder[i])
			val += " " + style.Render(glyph)
		}
		line := fmt.Sprintf("%-22s %s", label, val)
		if i == m.setCursor {
			line = th.cursor.Render("> ") + fmt.Sprintf("%-22s %s", label, val)
		} else {
			line = "  " + line
		}
		b.WriteString(truncate(line, inner))
		b.WriteString("\n")
	}

	b.WriteString("\n")
	b.WriteString(truncate(th.subtle.Render("↑/↓ move · space toggle · e edit · r refresh · a AI setup · esc close"), inner))
	b.WriteString("\n")
	b.WriteString(truncate(m.aiSetupHint(), inner))
	return th.box.Render(strings.TrimRight(b.String(), "\n"))
}

// aiSetupHint is the one-line pointer to the feature-centric requirements view.
func (m *model) aiSetupHint() string {
	if m.fleetAt.IsZero() {
		return th.subtle.Render("AI setup: checking… · press a")
	}
	on, blocked, pending := 0, 0, 0
	if len(m.fleet.Requirements) == 0 {
		// Older engine: the requirements projection is not in the payload yet.
		for _, enabled := range m.cfg.Features {
			if enabled {
				on++
			}
		}
	} else {
		for _, req := range m.fleet.Requirements {
			if !req.Enabled {
				continue
			}
			on++
			switch req.Readiness {
			case "missing", "unknown":
				blocked++
			case "pending":
				pending++
			}
		}
	}
	if on == 0 {
		return th.subtle.Render("AI setup: no passes on · press a")
	}
	if len(m.fleet.Requirements) == 0 {
		return th.subtle.Render(fmt.Sprintf("AI setup: %d passes on · press a", on))
	}
	if blocked == 0 && pending == 0 {
		return th.ok.Render(fmt.Sprintf("AI setup: %d passes on · models up · press a", on))
	}
	if blocked == 0 {
		return th.warn.Render(fmt.Sprintf("AI setup: %d passes on · %d starting · press a", on, pending))
	}
	return th.warn.Render(fmt.Sprintf("AI setup: %d passes on · %d need models · press a", on, blocked))
}

// featureAvailability uses the server-computed readiness for the *resolved*
// models, not the worst state among every tag match (ADR-0065).
func (m *model) featureAvailability(key string) (string, lipgloss.Style) {
	if m.fleetAt.IsZero() {
		return "…", th.subtle
	}
	for _, req := range m.fleet.Requirements {
		if req.Key != key {
			continue
		}
		switch req.Readiness {
		case "ready":
			return "●", th.ok
		case "pending":
			return "◌", th.warn
		case "missing":
			return "○", th.err
		default:
			return "…", th.subtle
		}
	}
	// Older engine without the requirements projection: fall back to the
	// service rows so the glyph still means something.
	found := false
	worst := 0
	for _, s := range m.fleet.Services {
		if !slices.Contains(s.Features, key) {
			continue
		}
		found = true
		switch s.State {
		case "up":
		case "starting", "stopping", "provisioning":
			if worst < 1 {
				worst = 1
			}
		default:
			worst = 2
		}
	}
	if !found {
		return "…", th.subtle
	}
	switch worst {
	case 0:
		return "●", th.ok
	case 1:
		return "◌", th.warn
	default:
		return "○", th.err
	}
}

// fleetGlyph maps a flaate state to the availability glyph (ADR-0063).
func fleetGlyph(state string) (string, lipgloss.Style) {
	switch state {
	case "up":
		return "●", th.ok
	case "starting", "stopping", "provisioning":
		return "◌", th.warn
	default:
		return "○", th.err
	}
}

func (m *model) fleetHeader() string {
	if m.fleetAt.IsZero() {
		if m.fleetErr != "" {
			return th.err.Render("fleet: " + truncate(m.fleetErr, 40))
		}
		return th.subtle.Render("fleet (checking…)")
	}
	if m.fleetErr != "" {
		return th.bold.Render("fleet") + " " + th.warn.Render("(refresh failed)")
	}
	if m.fleet.Source == "static" || !m.fleet.Daemon.Reachable {
		return th.warn.Render("fleet (flaate ✗ static probes)")
	}
	return th.bold.Render("fleet") + " " + th.ok.Render("(flaate ●)")
}
