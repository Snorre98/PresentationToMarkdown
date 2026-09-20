package main

// The Bubble Tea model: file picking (`@` fuzzy), conversion progress, and
// settings (ADR-0040). The model owns no conversion logic — it drives ptm-engine.

import (
	"context"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

type screen int

const (
	screenPick screen = iota
	screenRun
	screenSettings
)

var (
	headerStyle = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("12"))
	subtleStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
	cursorStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("10")).Bold(true)
	checkStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color("10"))
	promptStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("13")).Bold(true)
	hintStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
	errStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("9"))
)

type model struct {
	client  *Client
	cwd     string
	spawned bool

	screen screen

	// picking
	files     []string
	relFiles  []string
	visible   []int
	cursor    int
	selected  map[int]bool
	filtering bool
	filter    string
	loadErr   error

	// running
	eventsCh   <-chan JobEvent
	curFile    string
	curIdx     int
	curTotal   int
	curPage    int
	curPageTot int
	logs       []string
	done       *JobEvent

	// settings
	cfg            Config
	outputDir      string
	duplicate      bool
	settingsCursor int
	editingOutput  bool

	width, height int
}

func newModel(client *Client, cwd string, spawned bool) *model {
	return &model{
		client:   client,
		cwd:      cwd,
		spawned:  spawned,
		screen:   screenPick,
		selected: map[int]bool{},
	}
}

// --- messages ----------------------------------------------------------------

type globMsg struct {
	res GlobResult
	err error
}

type cfgMsg struct {
	cfg Config
	err error
}

type jobClosedMsg struct{}

func (m *model) Init() tea.Cmd {
	return m.loadFiles()
}

func (m *model) loadFiles() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		res, err := m.client.Glob(ctx, m.cwd, true)
		return globMsg{res, err}
	}
}

func (m *model) fetchConfig() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		cfg, err := m.client.Config(ctx)
		return cfgMsg{cfg, err}
	}
}

func waitEvent(ch <-chan JobEvent) tea.Cmd {
	return func() tea.Msg {
		ev, ok := <-ch
		if !ok {
			return jobClosedMsg{}
		}
		return ev
	}
}

// --- update ------------------------------------------------------------------

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil

	case globMsg:
		if msg.err != nil {
			m.loadErr = msg.err
			return m, nil
		}
		m.files = msg.res.Files
		m.relFiles = relPaths(m.cwd, m.files)
		m.refilter()
		return m, nil

	case cfgMsg:
		if msg.err == nil {
			m.cfg = msg.cfg
			m.duplicate = msg.cfg.Duplicate
		}
		return m, nil

	case JobEvent:
		return m.onJobEvent(msg)

	case jobClosedMsg:
		if m.screen == screenRun {
			m.screen = screenPick
		}
		return m, nil

	case tea.KeyMsg:
		switch msg.String() {
		case "ctrl+c":
			return m, tea.Quit
		case "q":
			if m.screen == screenPick && !m.filtering {
				return m, tea.Quit
			}
		}
		switch m.screen {
		case screenPick:
			return m.updatePick(msg)
		case screenRun:
			return m, nil // ignore keys while running
		case screenSettings:
			return m.updateSettings(msg)
		}
	}
	return m, nil
}

func (m *model) updatePick(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	if m.filtering {
		switch msg.String() {
		case "esc":
			m.filtering = false
			m.filter = ""
			m.refilter()
		case "enter":
			m.filtering = false
		case "backspace":
			if len(m.filter) > 0 {
				m.filter = m.filter[:len(m.filter)-1]
				m.refilter()
			}
		default:
			if msg.Type == tea.KeyRunes {
				m.filter += string(msg.Runes)
				m.refilter()
			}
		}
		return m, nil
	}

	switch msg.String() {
	case "@":
		m.filtering = true
		m.filter = ""
		m.refilter()
	case "up", "k":
		if m.cursor > 0 {
			m.cursor--
		}
	case "down", "j":
		if m.cursor < len(m.visible)-1 {
			m.cursor++
		}
	case " ":
		m.toggleSelect()
	case "enter":
		if len(m.selected) == 0 && len(m.visible) > 0 {
			m.selected[m.visible[m.cursor]] = true
		}
		if len(m.selected) == 0 {
			return m, nil
		}
		return m, m.startJob()
	case "s", "tab":
		m.screen = screenSettings
		return m, m.fetchConfig()
	}
	return m, nil
}

func (m *model) toggleSelect() {
	if len(m.visible) == 0 {
		return
	}
	idx := m.visible[m.cursor]
	if m.selected[idx] {
		delete(m.selected, idx)
	} else {
		m.selected[idx] = true
	}
}

func (m *model) startJob() tea.Cmd {
	paths := make([]string, 0, len(m.selected))
	for idx := range m.selected {
		paths = append(paths, m.files[idx])
	}
	sort.Strings(paths)

	m.screen = screenRun
	m.curFile, m.curIdx, m.curTotal = "", 0, len(paths)
	m.curPage, m.curPageTot = 0, 0
	m.logs = nil
	m.done = nil

	ch := make(chan JobEvent, 32)
	m.eventsCh = ch
	go func() {
		defer close(ch)
		if err := m.client.RunJob(context.Background(), paths, m.outputDir, m.duplicate, func(ev JobEvent) {
			ch <- ev
		}); err != nil {
			ch <- JobEvent{Type: "error", Message: err.Error()}
		}
	}()
	return waitEvent(ch)
}

func (m *model) onJobEvent(ev JobEvent) (tea.Model, tea.Cmd) {
	if m.screen != screenRun {
		return m, nil
	}
	switch ev.Type {
	case "file":
		m.curFile = ev.Name
		m.curIdx = ev.Idx
		m.curTotal = ev.Total
	case "page":
		m.curPage = ev.Page
		m.curPageTot = ev.Total
	case "log":
		m.logs = append(m.logs, fmt.Sprintf("[%s] %s", ev.Kind, ev.Message))
	case "done", "error":
		m.done = &ev
		m.screen = screenPick
		return m, nil
	}
	return m, waitEvent(m.eventsCh)
}

func (m *model) refilter() {
	if m.filter == "" {
		m.visible = make([]int, len(m.files))
		for i := range m.files {
			m.visible[i] = i
		}
	} else {
		m.visible = filterIndices(m.filter, m.relFiles)
	}
	if m.cursor >= len(m.visible) {
		m.cursor = len(m.visible) - 1
	}
	if m.cursor < 0 && len(m.visible) > 0 {
		m.cursor = 0
	}
}

// --- view --------------------------------------------------------------------

func (m *model) View() string {
	switch m.screen {
	case screenRun:
		return runView(m)
	case screenSettings:
		return settingsView(m)
	default:
		return pickView(m)
	}
}

func pickView(m *model) string {
	var b strings.Builder
	b.WriteString(headerStyle.Render("ptm-tui"))
	b.WriteString("\n")
	b.WriteString(subtleStyle.Render(m.cwd))
	b.WriteString("\n\n")

	if m.loadErr != nil {
		b.WriteString(errStyle.Render("discovery failed: "+m.loadErr.Error()))
		b.WriteString("\n\n")
	}

	if m.filtering {
		b.WriteString(promptStyle.Render("@") + m.filter + cursorStyle.Render("▌"))
		b.WriteString("\n\n")
	}

	if len(m.visible) == 0 {
		b.WriteString(subtleStyle.Render("  no matches"))
	} else {
		for i, idx := range m.visible {
			mark := "  "
			if m.selected[idx] {
				mark = checkStyle.Render("✓ ")
			}
			line := mark + m.relFiles[idx]
			if i == m.cursor {
				line = cursorStyle.Render("> ") + mark + m.relFiles[idx]
			}
			b.WriteString(line)
			b.WriteString("\n")
		}
	}

	b.WriteString("\n")
	b.WriteString(hintStyle.Render("↑/↓ move · space select · @ filter · enter convert · s settings · q quit"))

	if m.done != nil {
		b.WriteString("\n")
		if m.done.Type == "error" {
			b.WriteString(errStyle.Render(fmt.Sprintf("error: %s", m.done.Message)))
		} else {
			b.WriteString(fmt.Sprintf("%d/%d converted", m.done.Ok, m.done.Total))
		}
	}
	return b.String()
}

func runView(m *model) string {
	var b strings.Builder
	b.WriteString(headerStyle.Render("converting"))
	b.WriteString("\n\n")
	if m.curFile != "" {
		b.WriteString(fmt.Sprintf("  [%d/%d] %s", m.curIdx, m.curTotal, m.curFile))
		b.WriteString("\n")
	}
	if m.curPageTot > 0 {
		b.WriteString(fmt.Sprintf("  page %d/%d", m.curPage, m.curPageTot))
		b.WriteString("\n")
	}
	for _, line := range tail(m.logs, 12) {
		b.WriteString("  " + subtleStyle.Render(line))
		b.WriteString("\n")
	}
	return b.String()
}

func tail(lines []string, n int) []string {
	if len(lines) <= n {
		return lines
	}
	return lines[len(lines)-n:]
}

func relPaths(cwd string, abs []string) []string {
	out := make([]string, len(abs))
	for i, p := range abs {
		if rel, err := filepath.Rel(cwd, p); err == nil {
			out[i] = rel
		} else {
			out[i] = p
		}
	}
	return out
}
