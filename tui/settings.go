package main

// Settings screen: AI feature toggles, pdf mode, duplicate, and the output dir.
// Feature/pdf/duplicate changes round-trip through the engine's /api/config so
// they stay in sync with the GUI and web surfaces (converter.settings).

import (
	"context"
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

var featureOrder = []string{"vision", "classify", "interpret", "format", "summary", "structure"}

const settingsExtraRows = 3 // pdf_mode, duplicate, output_dir

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
	default: // output_dir
		m.editingOutput = true
		return m, nil
	}
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
	default:
		return "output dir"
	}
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
	b.WriteString(hintStyle.Render("↑/↓ move · space toggle · e edit output dir · esc back"))
	return b.String()
}
