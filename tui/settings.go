package main

// settings.go — the settings modal: AI feature toggles, output preferences,
// transcription settings (ADR-0043) and live model-server health. Every change
// round-trips through the engine's /api/config so it persists in the shared
// settings store (converter.settings).

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
)

var featureOrder = []string{"vision", "classify", "interpret", "format", "summary", "structure"}

// Rows after the features: pdf_mode, image_links, compress, inference
// overwrite, duplicate, diarize, speakers, model, language, output_dir.
const settingsExtraRows = 10

// audioModels is the cycle of ASR models the TUI offers (ADR-0043/0044). The
// first is the machine default: NB-Whisper (Norwegian, routed through the audio
// server's /v1/asr).
var audioModels = []string{
	"nb-whisper-large",                     // norwegian (audio server ASR)
	"mlx-community/whisper-large-v3-mlx",   // max quality (mlx-whisper)
	"mlx-community/whisper-large-v3-turbo", // speed (mlx-whisper)
}

type editField int

const (
	editNone editField = iota
	editOutput
	editLanguage
	editSpeakers
)

type serversMsg struct {
	res ServersResult
	err error
}

func (m *model) fetchServers() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		res, err := m.client.Servers(ctx)
		return serversMsg{res, err}
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
		return "model"
	case 8:
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
		return m, m.fetchServers()
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
	case 7: // model — cycle between the bundled ASR models
		if editOnly {
			return m, nil
		}
		next := audioModels[0]
		if m.cfg.AudioModel == audioModels[0] {
			next = audioModels[1]
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
		line := fmt.Sprintf("%-16s %s", label, val)
		if i == m.setCursor {
			line = th.cursor.Render("> ") + fmt.Sprintf("%-16s %s", label, val)
		} else {
			line = "  " + line
		}
		b.WriteString(truncate(line, inner))
		b.WriteString("\n")
	}

	b.WriteString("\n")
	b.WriteString(truncate(th.subtle.Render("↑/↓ move · space toggle · e edit · r refresh servers · esc close"), inner))
	b.WriteString("\n")

	if len(m.servers) > 0 || len(m.missingServers) > 0 {
		b.WriteString("\n")
		b.WriteString(th.bold.Render("model servers"))
		b.WriteString("\n")
		shown := map[string]bool{}
		for _, s := range m.servers {
			glyph, style := "●", th.ok
			if !s.Up {
				glyph, style = "○", th.err
			}
			shown[s.Name] = true
			b.WriteString(truncate(fmt.Sprintf("  %s %-18s %s", style.Render(glyph), s.Name, th.subtle.Render(s.URL)), inner))
			b.WriteString("\n")
		}
		for _, s := range m.missingServers {
			if shown[s.Name] {
				continue
			}
			b.WriteString(truncate(fmt.Sprintf("  %s %-18s %s", th.err.Render("○"), s.Name, th.subtle.Render(s.URL)), inner))
			b.WriteString("\n")
		}
	}
	return th.box.Render(strings.TrimRight(b.String(), "\n"))
}
