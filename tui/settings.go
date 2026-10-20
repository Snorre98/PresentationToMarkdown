package main

// settings.go — the settings modal: AI feature toggles, output preferences,
// transcription settings (ADR-0043) and live fleet availability (ADR-0063).
// Every change round-trips through the engine's /api/config so it persists in
// the shared settings store (converter.settings).

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

// Rows after the features: pdf_mode, image_links, compress, inference
// overwrite, duplicate, diarize, speakers, model, language, output_dir.
const settingsExtraRows = 10

// audioModels is the cycle of fleet ASR model names the TUI offers
// (ADR-0043/0044/0062). The first is the machine default: NB-Whisper
// (Norwegian, audio server /v1/asr); the whisper models are served by the
// fleet's `whisper` daemon.
var audioModels = []string{
	"nb-whisper-large",       // norwegian (audio server ASR)
	"whisper-large-v3",       // max quality (fleet whisper daemon)
	"whisper-large-v3-turbo", // speed (fleet whisper daemon)
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
	if id == "whisper-large-v3-turbo" {
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
	b.WriteString(truncate(th.subtle.Render("↑/↓ move · space toggle · e edit · r refresh fleet · esc close"), inner))
	b.WriteString("\n")
	b.WriteString("\n")
	b.WriteString(truncate(m.fleetHeader(), inner))
	b.WriteString("\n")
	if !m.fleetAt.IsZero() {
		for _, s := range m.fleetRows() {
			glyph, style := fleetGlyph(s.State)
			line := fmt.Sprintf(
				"  %s %-18s %-5s %s",
				style.Render(glyph),
				s.Name,
				s.State,
				th.subtle.Render(serviceDetail(s)),
			)
			b.WriteString(truncate(line, inner))
			b.WriteString("\n")
		}
		b.WriteString(truncate(th.subtle.Render(m.fleetSummary()), inner))
		b.WriteString("\n")
	}
	return th.box.Render(strings.TrimRight(b.String(), "\n"))
}

// featureAvailability is the worst state among a feature's servers: all up ●,
// transitional ◌, any down ○. "…" until the first fleet response arrives.
func (m *model) featureAvailability(key string) (string, lipgloss.Style) {
	if m.fleetAt.IsZero() {
		return "…", th.subtle
	}
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

// serviceDetail renders the roles a fleet row serves, marking the ASR target
// and the rows the current toggles need.
func serviceDetail(s FleetService) string {
	var detail string
	switch {
	case s.Selected && s.Name == "audio":
		detail = "ASR nb-whisper (selected)"
	case s.Selected:
		detail = "ASR (selected)"
	case slices.Contains(s.Roles, "audio") || slices.Contains(s.Roles, "nb-whisper-large") ||
		strings.HasPrefix(s.Name, "whisper-"):
		detail = "ASR"
	default:
		detail = strings.Join(s.Roles, " · ")
	}
	if s.Needed {
		detail += " · needed"
	}
	return detail
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

// fleetRows is the PtM-consumable subset: manifest models that serve no PtM
// role stay out of the settings modal (they are the "+N other models" count).
func (m *model) fleetRows() []FleetService {
	rows := make([]FleetService, 0, len(m.fleet.Services))
	for _, s := range m.fleet.Services {
		if len(s.Roles) == 0 {
			continue
		}
		rows = append(rows, s)
	}
	return rows
}

func (m *model) fleetSummary() string {
	rows := m.fleetRows()
	up := 0
	for _, s := range rows {
		if s.State == "up" {
			up++
		}
	}
	return fmt.Sprintf(
		"  %d/%d up · +%d other models · as of %s",
		up,
		len(rows),
		m.fleet.OtherModels,
		m.fleetAt.Format("15:04"),
	)
}
