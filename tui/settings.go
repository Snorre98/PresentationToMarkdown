package main

// Settings screen: AI feature toggles, pdf mode, duplicate, output dir, and
// transcription settings (diarize/speakers/model/language). Feature/pdf/
// duplicate/transcription changes round-trip through the engine's /api/config
// so they stay in sync with the GUI and web surfaces (converter.settings). The
// transcription settings (ADR-0043) are injected into the spawned
// ptm-transcribe argv by the TUI at run time.

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

var featureOrder = []string{"vision", "classify", "interpret", "format", "summary", "structure"}

// pdf_mode, duplicate, diarize, speakers, model, language, output_dir.
const settingsExtraRows = 7

// audioModels is the cycle of ASR models the TUI offers (ADR-0043/0044). The
// first is the machine default: NB-Whisper (Norwegian, routed through the audio
// server's /v1/asr).
var audioModels = []string{
	"nb-whisper-large",                      // norwegian (audio server ASR)
	"mlx-community/whisper-large-v3-mlx",    // max quality (mlx-whisper)
	"mlx-community/whisper-large-v3-turbo",  // speed (mlx-whisper)
}

func (m *model) updateSettings(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	if m.editingOutput {
		switch msg.String() {
		case "esc", "enter":
			m.editingOutput = false
		case "backspace":
			if len(m.outputDir) > 0 {
				m.outputDir = m.outputDir[:len(m.outputDir)-1]
			}
		default:
			if msg.Type == tea.KeyRunes {
				m.outputDir += string(msg.Runes)
			}
		}
		return m, nil
	}
	if m.editingLanguage {
		switch msg.String() {
		case "esc", "enter":
			return m.commitLanguage()
		case "backspace":
			if len(m.languageInput) > 0 {
				m.languageInput = m.languageInput[:len(m.languageInput)-1]
			}
		default:
			if msg.Type == tea.KeyRunes {
				m.languageInput += string(msg.Runes)
			}
		}
		return m, nil
	}
	if m.editingSpeakers {
		switch msg.String() {
		case "esc", "enter":
			return m.commitSpeakers()
		case "backspace":
			if len(m.speakersInput) > 0 {
				m.speakersInput = m.speakersInput[:len(m.speakersInput)-1]
			}
		default:
			if msg.Type == tea.KeyRunes {
				for _, r := range msg.Runes {
					if r >= '0' && r <= '9' {
						m.speakersInput += string(r)
					}
				}
			}
		}
		return m, nil
	}

	switch msg.String() {
	case "esc", "s", "tab", "q":
		m.screen = screenPick
		return m, nil
	case "up", "k":
		if m.settingsCursor > 0 {
			m.settingsCursor--
		}
		return m, nil
	case "down", "j":
		if m.settingsCursor < m.settingsRows()-1 {
			m.settingsCursor++
		}
		return m, nil
	case "enter", " ", "e":
		return m.activateSetting()
	}
	return m, nil
}

func (m *model) activateSetting() (tea.Model, tea.Cmd) {
	i := m.settingsCursor
	if i < len(featureOrder) {
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
		next := "paper"
		if m.cfg.PDFMode == "paper" {
			next = "slide"
		}
		m.cfg.PDFMode = next
		return m, m.saveConfig(ConfigUpdate{PDFMode: next})
	case 1: // duplicate
		next := !m.duplicate
		m.duplicate = next
		return m, m.saveConfig(ConfigUpdate{Duplicate: &next})
	case 2: // diarize
		next := !m.cfg.AudioDiarize
		m.cfg.AudioDiarize = next
		return m, m.saveConfig(ConfigUpdate{AudioDiarize: &next})
	case 3: // speakers
		m.editingSpeakers = true
		m.speakersInput = strconv.Itoa(m.cfg.AudioSpeakers)
		return m, nil
	case 4: // model — cycle between the bundled ASR models
		next := audioModels[0]
		if m.cfg.AudioModel == audioModels[0] {
			next = audioModels[1]
		}
		m.cfg.AudioModel = next
		return m, m.saveConfig(ConfigUpdate{AudioModel: &next})
	case 5: // language
		m.editingLanguage = true
		m.languageInput = m.cfg.AudioLanguage
		if m.languageInput == "" {
			m.languageInput = "auto"
		}
		return m, nil
	default: // output_dir
		m.editingOutput = true
		return m, nil
	}
}

// commitSpeakers parses the numeric input into AudioSpeakers (0 = auto) and
// persists it.
func (m *model) commitSpeakers() (tea.Model, tea.Cmd) {
	n := 0
	if v, err := strconv.Atoi(m.speakersInput); err == nil && v >= 1 {
		n = v
	}
	m.cfg.AudioSpeakers = n
	m.editingSpeakers = false
	return m, m.saveConfig(ConfigUpdate{AudioSpeakers: &n})
}

// commitLanguage normalises the language input ("auto"/empty = auto-detect)
// and persists it.
func (m *model) commitLanguage() (tea.Model, tea.Cmd) {
	val := strings.TrimSpace(m.languageInput)
	if val == "" {
		val = "auto"
	}
	m.cfg.AudioLanguage = val
	m.editingLanguage = false
	return m, m.saveConfig(ConfigUpdate{AudioLanguage: &val})
}

func (m *model) saveConfig(u ConfigUpdate) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		cfg, err := m.client.SetConfig(ctx, u)
		return cfgMsg{cfg, err}
	}
}

func (m *model) settingsRows() int {
	return len(featureOrder) + settingsExtraRows
}

func (m *model) settingsLabel(i int) string {
	if i < len(featureOrder) {
		return featureOrder[i]
	}
	switch i - len(featureOrder) {
	case 0:
		return "pdf mode"
	case 1:
		return "duplicate"
	case 2:
		return "diarize"
	case 3:
		return "speakers"
	case 4:
		return "model"
	case 5:
		return "language"
	default:
		return "output dir"
	}
}

// modelShort renders a compact label for the current ASR model.
func modelShort(id string) string {
	if id == "nb-whisper-large" {
		return "nb-whisper (no)"
	}
	if strings.HasSuffix(id, "whisper-large-v3-turbo") {
		return "turbo (fast)"
	}
	return "large-v3 (max)"
}

func (m *model) settingsValue(i int) string {
	if i < len(featureOrder) {
		return boolLabel(m.cfg.Features[featureOrder[i]])
	}
	switch i - len(featureOrder) {
	case 0:
		return m.cfg.PDFMode
	case 1:
		return boolLabel(m.duplicate)
	case 2:
		return boolLabel(m.cfg.AudioDiarize)
	case 3:
		if m.editingSpeakers {
			return m.speakersInput + "▌"
		}
		if m.cfg.AudioSpeakers <= 0 {
			return "auto"
		}
		return strconv.Itoa(m.cfg.AudioSpeakers)
	case 4:
		return modelShort(m.cfg.AudioModel)
	case 5:
		if m.editingLanguage {
			return m.languageInput + "▌"
		}
		return m.cfg.AudioLanguage
	default:
		if m.editingOutput {
			return m.outputDir + "▌"
		}
		if m.outputDir == "" {
			return "(default: <source>/markdown)"
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

func settingsView(m *model) string {
	var b strings.Builder
	b.WriteString(headerStyle.Render("settings"))
	b.WriteString("\n\n")
	for i := 0; i < m.settingsRows(); i++ {
		label := m.settingsLabel(i)
		val := m.settingsValue(i)
		line := fmt.Sprintf("  %-14s %s", label, val)
		if i == m.settingsCursor {
			line = cursorStyle.Render("> ") + fmt.Sprintf("%-14s %s", label, val)
		}
		b.WriteString(line)
		b.WriteString("\n")
	}
	b.WriteString("\n")
	b.WriteString(hintStyle.Render("↑/↓ move · space toggle · e edit value · esc back"))
	return b.String()
}