package main

import tea "github.com/charmbracelet/bubbletea"

// Settings screen (placeholder; the full toggle list lands in the next commit).

func (m *model) updateSettings(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "esc", "s", "tab":
		m.screen = screenPick
	}
	return m, nil
}

func settingsView(m *model) string {
	return headerStyle.Render("settings") + "\n\n  coming soon (esc to return)"
}
