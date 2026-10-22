package main

// settings.go — the settings modal: output preferences and audio transcription
// settings (ADR-0043). The AI capability toggles live in the AI setup view
// (a), which has the room for descriptions and model requirements (ADR-0066).
// Every change round-trips through the engine's /api/config so it persists in
// the shared settings store (converter.settings).

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

type settingsRowKind int

const (
	settingsHeader settingsRowKind = iota
	settingsSetting
)

type settingsRow struct {
	kind  settingsRowKind
	key   string
	label string
}

// settingsLayout is the fixed, sectioned order of the settings screen. Section
// headers are not selectable; the cursor only lands on settingsSetting rows.
func (m *model) settingsLayout() []settingsRow {
	return []settingsRow{
		{kind: settingsHeader, label: "Output"},
		{kind: settingsSetting, key: "pdf_mode", label: "pdf mode"},
		{kind: settingsSetting, key: "epub_output", label: "epub export"},
		{kind: settingsSetting, key: "image_links", label: "image links"},
		{kind: settingsSetting, key: "pdf_compress", label: "compress pdf"},
		{kind: settingsSetting, key: "inference_overwrite", label: "overwrite infer"},
		{kind: settingsSetting, key: "duplicate", label: "duplicate"},
		{kind: settingsSetting, key: "output_dir", label: "output dir"},
		{kind: settingsHeader, label: "Audio transcription"},
		{kind: settingsSetting, key: "diarize", label: "diarize"},
		{kind: settingsSetting, key: "speakers", label: "speakers"},
		{kind: settingsSetting, key: "audio_model", label: "audio model"},
		{kind: settingsSetting, key: "language", label: "language"},
	}
}

func (m *model) settingsRowAt(i int) settingsRow {
	if i < 0 {
		return settingsRow{}
	}
	n := 0
	for _, row := range m.settingsLayout() {
		if row.kind != settingsSetting {
			continue
		}
		if n == i {
			return row
		}
		n++
	}
	return settingsRow{}
}

func (m *model) settingsRows() int {
	n := 0
	for _, row := range m.settingsLayout() {
		if row.kind == settingsSetting {
			n++
		}
	}
	return n
}

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

func (m *model) settingsLabel(i int) string {
	return m.settingsRowAt(i).label
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
	return m.settingsValueFor(m.settingsRowAt(i))
}

func (m *model) settingsValueFor(row settingsRow) string {
	switch row.key {
	case "pdf_mode":
		if m.cfg.PDFMode == "" {
			return "slide"
		}
		return m.cfg.PDFMode
	case "epub_output":
		if !m.cfg.EpubOutput {
			return "off"
		}
		if !m.cfg.Pandoc {
			return "on (pandoc missing)"
		}
		return "on"
	case "image_links":
		return boolLabel(m.cfg.ImageLinks)
	case "pdf_compress":
		return boolLabel(m.cfg.PDFCompress)
	case "inference_overwrite":
		return boolLabel(m.cfg.InferenceOverwrite)
	case "duplicate":
		return boolLabel(m.duplicate)
	case "output_dir":
		if m.editing == editOutput {
			return m.editBuf + "▌"
		}
		if m.outputDir == "" {
			return "(default: beside source; source → source/)"
		}
		return m.outputDir
	case "diarize":
		return boolLabel(m.cfg.AudioDiarize)
	case "speakers":
		if m.editing == editSpeakers {
			return m.editBuf + "▌"
		}
		if m.cfg.AudioSpeakers <= 0 {
			return "auto"
		}
		return strconv.Itoa(m.cfg.AudioSpeakers)
	case "audio_model":
		return modelShort(m.cfg.AudioModel)
	case "language":
		if m.editing == editLanguage {
			return m.editBuf + "▌"
		}
		if m.cfg.AudioLanguage == "" {
			return "auto"
		}
		return m.cfg.AudioLanguage
	default:
		return ""
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
	case "a":
		m.overlay = overlayAI
		m.aiCursor = 0
		return m, m.fetchFleet()
	case "e":
		return m.activateSetting(true)
	case "enter", " ", "space":
		return m.activateSetting(false)
	}
	return m, nil
}

func (m *model) activateSetting(editOnly bool) (tea.Model, tea.Cmd) {
	row := m.settingsRowAt(m.setCursor)
	switch row.key {
	case "pdf_mode":
		if editOnly {
			return m, nil
		}
		next := "paper"
		if m.cfg.PDFMode == "paper" {
			next = "slide"
		}
		m.cfg.PDFMode = next
		return m, m.saveConfig(ConfigUpdate{PDFMode: next})
	case "epub_output":
		if editOnly {
			return m, nil
		}
		next := !m.cfg.EpubOutput
		m.cfg.EpubOutput = next
		return m, m.saveConfig(ConfigUpdate{EpubOutput: &next})
	case "image_links":
		if editOnly {
			return m, nil
		}
		next := !m.cfg.ImageLinks
		m.cfg.ImageLinks = next
		return m, m.saveConfig(ConfigUpdate{ImageLinks: &next})
	case "pdf_compress":
		if editOnly {
			return m, nil
		}
		next := !m.cfg.PDFCompress
		m.cfg.PDFCompress = next
		return m, m.saveConfig(ConfigUpdate{PDFCompress: &next})
	case "inference_overwrite":
		if editOnly {
			return m, nil
		}
		next := !m.cfg.InferenceOverwrite
		m.cfg.InferenceOverwrite = next
		return m, m.saveConfig(ConfigUpdate{InferenceOverwrite: &next})
	case "duplicate":
		if editOnly {
			return m, nil
		}
		next := !m.duplicate
		m.duplicate = next
		return m, m.saveConfig(ConfigUpdate{Duplicate: &next})
	case "diarize":
		if editOnly {
			return m, nil
		}
		next := !m.cfg.AudioDiarize
		m.cfg.AudioDiarize = next
		return m, m.saveConfig(ConfigUpdate{AudioDiarize: &next})
	case "speakers":
		m.editing = editSpeakers
		m.editBuf = strconv.Itoa(m.cfg.AudioSpeakers)
		return m, nil
	case "audio_model":
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
	case "language":
		m.editing = editLanguage
		m.editBuf = m.cfg.AudioLanguage
		if m.editBuf == "" {
			m.editBuf = "auto"
		}
		return m, nil
	case "output_dir":
		m.editing = editOutput
		m.editBuf = m.outputDir
		return m, nil
	}
	return m, nil
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
	inner := width - 6
	if inner < 30 {
		inner = 30
	}

	layout := m.settingsLayout()
	bodyLines := make([]string, 0, len(layout))
	selectable := 0
	selectedLine := 0
	for _, row := range layout {
		if row.kind == settingsHeader {
			bodyLines = append(bodyLines, padRight(th.bold.Render("  "+row.label), inner))
			continue
		}
		label := row.label
		val := m.settingsValueFor(row)
		line := fmt.Sprintf("%-22s %s", label, val)
		if selectable == m.setCursor {
			line = th.cursor.Render("> ") + fmt.Sprintf("%-22s %s", label, val)
			selectedLine = len(bodyLines)
		} else {
			line = "  " + line
		}
		bodyLines = append(bodyLines, padRight(truncate(line, inner), inner))
		selectable++
	}

	bodyHeight := overlayBodyHeight(height, 6)
	start, end := windowBounds(len(bodyLines), selectedLine, bodyHeight)

	var b strings.Builder
	b.WriteString(th.title.Render("settings"))
	b.WriteString("\n\n")
	for _, line := range bodyLines[start:end] {
		b.WriteString(line)
		b.WriteString("\n")
	}
	b.WriteString("\n")
	footer := "↑/↓ move · space toggle · e edit · a AI setup · esc close"
	if inner < 60 {
		footer = "↑/↓ · space · e · a · esc"
	}
	b.WriteString(truncate(th.subtle.Render(footer), inner))
	return th.box.Render(strings.TrimRight(b.String(), "\n"))
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
