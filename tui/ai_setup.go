package main

// ai_setup.go — the read-only AI requirements view (ADR-0065).
//
// Settings (s) holds the toggles. This view answers the separate question:
// "what does each pass need, which model is the one PtM will actually use, and
// what do I start when it is down?" It never starts a model (ADR-0063); it shows
// the fleet start command.

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

func (m *model) updateAISetup(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "esc", "a", "q":
		m.overlay = overlayNone
		return m, nil
	case "s":
		m.overlay = overlaySettings
		return m, tea.Batch(m.fetchConfig(), m.fetchFleet())
	case "r":
		return m, m.fetchFleet()
	case "up", "k":
		if m.aiScroll > 0 {
			m.aiScroll--
		}
	case "down", "j":
		if m.aiScroll < m.aiSetupMaxScroll() {
			m.aiScroll++
		}
	}
	return m, nil
}

func (m *model) aiSetupViewport() int {
	max := m.height - 8
	if max < 5 {
		max = 5
	}
	return max
}

func (m *model) aiSetupMaxScroll() int {
	max := m.aiSetupViewport()
	lines := len(m.aiSetupLines())
	if lines <= max {
		return 0
	}
	return lines - max
}

func requirementGlyph(req FeatureRequirement) (string, lipgloss.Style) {
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

// aiSetupLines builds the body in a deterministic order: one block per pass,
// then the unique models that block the enabled passes.
func (m *model) aiSetupLines() []string {
	var lines []string

	if m.fleetAt.IsZero() {
		if m.fleetErr != "" {
			lines = append(lines, th.err.Render("fleet: "+m.fleetErr))
		} else {
			lines = append(lines, th.subtle.Render("checking the fleet…"))
		}
		return lines
	}

	header := m.fleetHeader() + " · " + m.fleetAt.Format("15:04")
	lines = append(lines, header)

	if len(m.fleet.Requirements) == 0 {
		lines = append(lines, th.warn.Render("engine did not return AI requirements — restart ptm-engine"))
		return lines
	}

	enabled := 0
	for _, req := range m.fleet.Requirements {
		if req.Enabled {
			enabled++
		}
		state := "off"
		if req.Enabled {
			state = "on"
		}
		label := req.Label
		if req.Scope == "paper" {
			label += " (paper PDFs)"
		}
		glyph, style := requirementGlyph(req)
		lines = append(
			lines,
			fmt.Sprintf("  %-30s %-3s %s", truncate(label, 30), state, style.Render(glyph)),
		)
		for _, service := range req.Services {
			serviceGlyph, serviceStyle := fleetGlyph(service.State)
			optional := ""
			if service.Optional {
				optional = " (optional)"
			}
			lines = append(
				lines,
				fmt.Sprintf(
					"      %s %-34s %-9s%s",
					serviceStyle.Render(serviceGlyph),
					truncate(service.Name, 34),
					service.State,
					th.subtle.Render(optional),
				),
			)
		}
	}

	lines = append(lines, "")
	lines = append(lines, th.subtle.Render(strings.Repeat("─", 62)))
	lines = append(lines, "")

	if enabled == 0 {
		lines = append(lines, th.subtle.Render("No AI passes are enabled. Turn one on in settings (s)."))
	} else if missing := m.missingRequirementServices(); len(missing) == 0 {
		lines = append(lines, th.ok.Render("All models required by the enabled passes are up."))
	} else {
		lines = append(lines, th.warn.Render(fmt.Sprintf("%d model(s) needed by the enabled passes:", len(missing))))
		for _, service := range missing {
			lines = append(lines, fmt.Sprintf("  %s (%s)", service.Name, service.Key))
			if service.Start != "" {
				lines = append(lines, th.subtle.Render("    "+service.Start))
			}
		}
	}

	if m.fleet.OtherModels > 0 {
		lines = append(
			lines,
			th.subtle.Render(fmt.Sprintf("+%d other fleet models (not used by PtM)", m.fleet.OtherModels)),
		)
	}
	lines = append(
		lines,
		th.subtle.Render("● up · ◌ starting · ○ down/unknown · optional = degrades gracefully"),
	)
	return lines
}

// missingRequirementServices returns the unique non-optional down services used
// by an enabled pass, in requirement order.
func (m *model) missingRequirementServices() []RequirementService {
	seen := map[string]bool{}
	var out []RequirementService
	for _, req := range m.fleet.Requirements {
		if !req.Enabled {
			continue
		}
		for _, service := range req.Services {
			if service.Optional || service.State == "up" {
				continue
			}
			if service.State == "starting" || service.State == "stopping" || service.State == "provisioning" {
				continue
			}
			key := service.Key
			if key == "" {
				key = service.Role
			}
			if seen[key] {
				continue
			}
			seen[key] = true
			out = append(out, service)
		}
	}
	return out
}

func (m *model) renderAISetupBox(width, height int) string {
	inner := width - 6
	if inner < 30 {
		inner = 30
	}
	lines := m.aiSetupLines()
	max := height - 8
	if max < 5 {
		max = 5
	}
	if max > len(lines) {
		max = len(lines)
	}
	maxScroll := len(lines) - max
	if maxScroll < 0 {
		maxScroll = 0
	}
	if m.aiScroll > maxScroll {
		m.aiScroll = maxScroll
	}
	if m.aiScroll < 0 {
		m.aiScroll = 0
	}
	start := m.aiScroll
	end := start + max
	if end > len(lines) {
		end = len(lines)
	}

	var b strings.Builder
	b.WriteString(th.title.Render("AI setup"))
	b.WriteString("\n\n")
	if start > 0 {
		b.WriteString(th.subtle.Render(fmt.Sprintf("  ↑ %d more", start)))
		b.WriteString("\n")
	}
	for _, line := range lines[start:end] {
		b.WriteString(truncate(line, inner))
		b.WriteString("\n")
	}
	if end < len(lines) {
		b.WriteString(th.subtle.Render(fmt.Sprintf("  ↓ %d more", len(lines)-end)))
		b.WriteString("\n")
	}
	b.WriteString("\n")
	b.WriteString(truncate(th.subtle.Render("↑/↓ scroll · r refresh · s settings · esc close"), inner))
	return th.box.Render(strings.TrimRight(b.String(), "\n"))
}
