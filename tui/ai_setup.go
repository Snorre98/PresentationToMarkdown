package main

// ai_setup.go — the AI control surface (ADR-0065/0066).
//
// Settings (s) holds output and audio preferences. This view owns the AI
// capabilities: each pass, what it does, which model PtM resolves for it, its
// live state, the on-demand start command, and the space toggle. It never
// starts a model (ADR-0063); it shows the command.

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
		if m.setCursor >= m.settingsRows() {
			m.setCursor = m.settingsRows() - 1
		}
		return m, m.fetchConfig()
	case "r":
		return m, m.fetchFleet()
	case "up", "k":
		if m.aiCursor > 0 {
			m.aiCursor--
		}
	case "down", "j":
		if m.aiCursor < len(m.fleet.Requirements)-1 {
			m.aiCursor++
		}
	case "enter", " ", "space":
		return m, m.toggleAIPass()
	}
	return m, nil
}

func (m *model) toggleAIPass() tea.Cmd {
	if m.aiCursor < 0 || m.aiCursor >= len(m.fleet.Requirements) {
		return nil
	}
	req := &m.fleet.Requirements[m.aiCursor]
	next := !req.Enabled
	req.Enabled = next
	if m.cfg.Features == nil {
		m.cfg.Features = map[string]bool{}
	}
	m.cfg.Features[req.Key] = next
	// Mirror the engine's implies rule locally so the screen responds before
	// the /api/config round-trip returns.
	if next && req.Key == "classify" {
		m.cfg.Features["vision"] = true
		for i := range m.fleet.Requirements {
			if m.fleet.Requirements[i].Key == "vision" {
				m.fleet.Requirements[i].Enabled = true
			}
		}
	}
	if !next && req.Key == "vision" {
		m.cfg.Features["classify"] = false
		for i := range m.fleet.Requirements {
			if m.fleet.Requirements[i].Key == "classify" {
				m.fleet.Requirements[i].Enabled = false
			}
		}
	}
	return m.saveConfig(ConfigUpdate{Features: map[string]bool{req.Key: next}})
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

// requiredRequirementServices is the unique, non-optional service set used by
// the enabled passes, keyed by service key.
func (m *model) requiredRequirementServices() map[string]RequirementService {
	out := map[string]RequirementService{}
	for _, req := range m.fleet.Requirements {
		if !req.Enabled {
			continue
		}
		for _, service := range req.Services {
			if service.Optional {
				continue
			}
			key := service.Key
			if key == "" {
				key = service.Role
			}
			out[key] = service
		}
	}
	return out
}

func (m *model) aiStats() (on, required, up, pending, down int) {
	for _, req := range m.fleet.Requirements {
		if req.Enabled {
			on++
		}
	}
	for _, service := range m.requiredRequirementServices() {
		required++
		switch service.State {
		case "up":
			up++
		case "starting", "stopping", "provisioning":
			pending++
		default:
			down++
		}
	}
	return on, required, up, pending, down
}

// aiSetupLines builds the body and the line index of each pass heading.
func (m *model) aiSetupLines(inner int) ([]string, []int) {
	var lines []string
	var starts []int

	if m.fleetAt.IsZero() {
		if m.fleetErr != "" {
			return []string{th.err.Render("fleet: " + m.fleetErr)}, nil
		}
		return []string{th.subtle.Render("checking the fleet…")}, nil
	}

	lines = append(lines, m.fleetHeader()+" · "+m.fleetAt.Format("15:04"))

	if len(m.fleet.Requirements) == 0 {
		lines = append(lines, th.warn.Render("engine did not return AI requirements — restart ptm-engine"))
		return lines, nil
	}
	if m.aiCursor >= len(m.fleet.Requirements) {
		m.aiCursor = len(m.fleet.Requirements) - 1
	}
	if m.aiCursor < 0 {
		m.aiCursor = 0
	}

	on, required, up, _, down := m.aiStats()
	passWord, modelWord := "passes", "models"
	if on == 1 {
		passWord = "pass"
	}
	if required == 1 {
		modelWord = "model"
	}
	if required == 0 {
		lines = append(lines, th.subtle.Render(fmt.Sprintf("%d %s on · no models required", on, passWord)))
	} else {
		lines = append(
			lines,
			th.subtle.Render(
				fmt.Sprintf(
					"%d %s on · %d %s required · %d up, %d down",
					on, passWord, required, modelWord, up, down,
				),
			),
		)
	}

	labelW := inner - 12
	if labelW < 18 {
		labelW = 18
	}
	nameW := inner - 32
	if nameW < 18 {
		nameW = 18
	}

	paperHeader := false
	slideHeader := false
	for i, req := range m.fleet.Requirements {
		// The pass block starts at the section header (when one is emitted), so
		// scrolling to a pass also brings its section label into view.
		blockStart := len(lines)
		if req.Scope == "paper" && !paperHeader {
			lines = append(lines, "")
			lines = append(lines, th.bold.Render("  Paper pass"))
			paperHeader = true
		} else if req.Scope != "paper" && !slideHeader {
			lines = append(lines, "")
			lines = append(lines, th.bold.Render("  Document passes"))
			slideHeader = true
		}

		starts = append(starts, blockStart)
		state := "off"
		if req.Enabled {
			state = "on"
		}
		label := req.Label
		if req.Scope == "paper" {
			label += " (paper PDFs)"
		}
		glyph, style := requirementGlyph(req)
		prefix := "  "
		if i == m.aiCursor {
			prefix = th.cursor.Render("> ")
		}
		lines = append(
			lines,
			fmt.Sprintf(
				"%s%-*s %-3s %s",
				prefix,
				labelW,
				truncate(label, labelW),
				state,
				style.Render(glyph),
			),
		)
		for _, text := range wrapText(req.Description, inner-6) {
			lines = append(lines, th.subtle.Render("    "+text))
		}
		if req.Scope == "paper" && m.cfg.PDFMode != "paper" {
			lines = append(lines, th.subtle.Render("    paper mode is off — enable it in settings (s)"))
		}
		for _, service := range req.Services {
			serviceGlyph, serviceStyle := fleetGlyph(service.State)
			line := fmt.Sprintf(
				"    %s %-*s %-9s",
				serviceStyle.Render(serviceGlyph),
				nameW,
				truncate(service.Name, nameW),
				service.State,
			)
			if service.Optional {
				line += th.subtle.Render("(optional)")
			}
			lines = append(lines, line)
		}
	}

	lines = append(lines, "")
	lines = append(lines, th.subtle.Render(strings.Repeat("─", inner-2)))
	lines = append(lines, "")

	if on == 0 {
		lines = append(lines, th.subtle.Render("No AI passes are enabled. Space toggles a pass."))
	} else if missing := m.missingRequirementServices(); len(missing) > 0 {
		word := "models"
		if len(missing) == 1 {
			word = "model"
		}
		lines = append(lines, th.warn.Render(fmt.Sprintf("%d %s needed by the enabled passes:", len(missing), word)))
		for _, service := range missing {
			lines = append(lines, fmt.Sprintf("  %s (%s)", service.Name, service.Key))
			if service.Start != "" {
				for _, text := range wrapText(service.Start, inner-4) {
					lines = append(lines, th.subtle.Render("    "+text))
				}
			}
		}
	} else if pending := m.pendingRequirementServices(); len(pending) > 0 {
		lines = append(lines, th.warn.Render("Waiting for the models required by the enabled passes:"))
		for _, service := range pending {
			lines = append(lines, fmt.Sprintf("  %s (%s) — %s", service.Name, service.Key, service.State))
		}
	} else {
		lines = append(lines, th.ok.Render("All models required by the enabled passes are up."))
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
	return lines, starts
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

// pendingRequirementServices returns the unique non-optional services used by
// an enabled pass that the fleet is still starting/stopping.
func (m *model) pendingRequirementServices() []RequirementService {
	seen := map[string]bool{}
	var out []RequirementService
	for _, req := range m.fleet.Requirements {
		if !req.Enabled {
			continue
		}
		for _, service := range req.Services {
			if service.Optional {
				continue
			}
			switch service.State {
			case "starting", "stopping", "provisioning":
			default:
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
	lines, starts := m.aiSetupLines(inner)

	// Keep the fleet status and summary fixed when there is room. On a very
	// short terminal the two header lines rejoin the scroll body.
	headerCount := 2
	if height < 12 || len(lines) < 2 {
		headerCount = 0
	}
	bodyLines := lines[headerCount:]
	body := overlayBodyHeight(height, 6+headerCount)

	selected := 0
	if m.aiCursor >= 0 && m.aiCursor < len(starts) {
		selected = starts[m.aiCursor] - headerCount
	}
	if selected < 0 {
		selected = 0
	}
	// Put the selected pass block at the top of the viewport; it is clearer
	// than keeping the cursor merely visible while showing the previous block.
	start := selected
	maxStart := len(bodyLines) - body
	if maxStart < 0 {
		maxStart = 0
	}
	if start > maxStart {
		start = maxStart
	}
	end := start + body
	if end > len(bodyLines) {
		end = len(bodyLines)
	}

	var b strings.Builder
	b.WriteString(th.title.Render("AI setup"))
	b.WriteString("\n\n")
	for _, line := range lines[:headerCount] {
		b.WriteString(padRight(truncate(line, inner), inner))
		b.WriteString("\n")
	}
	for _, line := range bodyLines[start:end] {
		b.WriteString(padRight(truncate(line, inner), inner))
		b.WriteString("\n")
	}
	b.WriteString("\n")
	footer := "↑/↓ move · space toggle · r refresh · s settings · esc close"
	if inner < 60 {
		footer = "↑/↓ · space toggle · r · s · esc"
	}
	b.WriteString(truncate(th.subtle.Render(footer), inner))
	return th.box.Render(strings.TrimRight(b.String(), "\n"))
}
