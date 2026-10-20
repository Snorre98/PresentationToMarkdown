package main

// view.go — the fixed-frame composition. Every pane renders into an explicitly
// sized window and the final frame is cropped/padded to the terminal, so a
// runaway pane can never scroll the screen (the ADR-0053 bug class).

import (
	"fmt"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
)

func (m *model) View() tea.View {
	v := tea.NewView(m.render())
	v.AltScreen = true
	v.MouseMode = tea.MouseModeCellMotion
	v.WindowTitle = m.windowTitle()
	return v
}

func (m *model) render() string {
	if m.width <= 0 || m.height <= 0 {
		return "ptm-tui — starting…"
	}
	header := m.renderHeader()
	jobs := m.renderJobsPanel(m.width)
	tabs := m.renderTabs()
	toast := m.renderToast()

	// Rows above the body: header + jobs panel + tab bar.
	m.bodyTop = 1 + len(jobs) + 1
	body := m.renderBody(m.bodyHeight())

	parts := []string{header}
	parts = append(parts, jobs...)
	parts = append(parts, tabs)
	parts = append(parts, strings.Split(body, "\n")...)
	parts = append(parts, m.renderFooter())
	if toast != "" {
		parts = append(parts, toast)
	}
	base := strings.Join(parts, "\n")

	switch m.overlay {
	case overlaySettings:
		return overlayCenter(base, m.renderSettingsBox(m.width, m.height), m.width, m.height)
	case overlayHelp:
		return overlayCenter(base, m.renderHelpBox(), m.width, m.height)
	case overlayAI:
		return overlayCenter(base, m.renderAISetupBox(m.width, m.height), m.width, m.height)
	}
	return fitLines(base, m.width, m.height)
}

func (m *model) windowTitle() string {
	if n := len(m.jobs.running()); n > 0 {
		return fmt.Sprintf("ptm-tui — %d running", n)
	}
	return "ptm-tui — " + m.cwd
}

func (m *model) renderHeader() string {
	left := th.header.Render("ptm-tui") + "  " + th.subtle.Render(m.cwd)
	right := m.engineBadge()
	gap := m.width - visibleWidth(left) - visibleWidth(right)
	if gap < 1 {
		return truncate(left+"  "+right, m.width)
	}
	return left + strings.Repeat(" ", gap) + right
}

func (m *model) engineBadge() string {
	if m.engineErr != "" {
		return th.err.Render("✗ engine: " + truncate(m.engineErr, 48))
	}
	dot, style := th.ok.Render("●"), th.ok
	if !m.connected {
		dot, style = th.warn.Render("◌"), th.warn
	}
	text := fmt.Sprintf("%s :%d", dot, m.port)
	if n := len(m.jobs.running()); n > 0 {
		text += "  " + th.accent.Render(fmt.Sprintf("%d running", n))
	} else if m.engineReady {
		text += "  " + style.Render("ready")
	}
	return text
}

func (m *model) renderTabs() string {
	rows, _ := m.visibleEntries()
	labels := []string{
		fmt.Sprintf("files %d", len(rows)),
		fmt.Sprintf("results %d", len(m.rstRows)),
		"log",
	}
	parts := make([]string, 0, len(labels))
	for i, l := range labels {
		if tab(i) == m.tab {
			parts = append(parts, th.tabOn.Render("["+l+"]"))
		} else {
			parts = append(parts, th.tabOff.Render(" "+l+" "))
		}
	}
	return truncate(" "+strings.Join(parts, " "), m.width)
}

func (m *model) renderFooter() string {
	return truncate(" "+m.help.View(keys), m.width)
}

func (m *model) renderToast() string {
	if m.toast == "" || time.Since(m.toastAt) > 6*time.Second {
		return ""
	}
	style := th.ok
	if m.toastErr {
		style = th.err
	}
	return truncate(" "+style.Render(m.toast), m.width)
}

func (m *model) renderBody(h int) string {
	var lines []string
	switch m.tab {
	case tabFiles:
		lines = m.renderPicker(m.width, h)
	case tabResults:
		lines = m.renderResults(m.width, h)
	default:
		lines = m.renderLog(m.width, h)
	}
	return fitLines(strings.Join(lines, "\n"), m.width, h)
}

func (m *model) renderLog(width, height int) []string {
	if len(m.logLines) == 0 {
		return []string{th.subtle.Render("  no log output yet")}
	}
	if m.logFollow {
		m.logOffset = len(m.logLines) - height
		if m.logOffset < 0 {
			m.logOffset = 0
		}
	}
	max := len(m.logLines) - height
	if max < 0 {
		max = 0
	}
	if m.logOffset > max {
		m.logOffset = max
	}
	end := m.logOffset + height
	if end > len(m.logLines) {
		end = len(m.logLines)
	}
	lines := make([]string, 0, height)
	for _, l := range m.logLines[m.logOffset:end] {
		style := th.subtle
		switch l.kind {
		case "ok":
			style = th.ok
		case "warn":
			style = th.warn
		case "err":
			style = th.err
		}
		lines = append(lines, truncate("  "+style.Render(l.text), width))
	}
	return lines
}

func (m *model) renderHelpBox() string {
	body := th.title.Render("keys") + "\n\n" + m.help.FullHelpView(keys.FullHelp())
	body += "\n\n" + th.subtle.Render("esc close")
	return th.box.Render(body)
}
