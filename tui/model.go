package main

// The Bubble Tea model: file picking (`@` fuzzy), conversion/transcription
// progress as a compact jobs strip on the picker, and settings (ADR-0040/0045).
// The model owns no conversion logic — it drives ptm-engine, whose hosted
// transcription streams the same structured WS frames as conversion.

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

type screen int

const (
	screenPick screen = iota
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

// jobView is one entry in the compact jobs strip (running first, then done/failed).
type jobView struct {
	kind      string // "convert" | "transcribe"
	status    string // "running" | "done" | "error"
	name      string
	idx       int
	total     int
	page      int
	pageTotal int
	phase     string
	ok        int
	err       string
}

type model struct {
	client  *Client
	cwd     string
	spawned bool

	screen screen

	// picking
	files     []string
	kinds     []string // parallel to files: "convert" | "audio"
	relFiles  []string
	visible   []int
	cursor    int
	selected  map[int]bool
	filtering bool
	filter    string
	loadErr   error

	// running jobs (engine-hosted)
	eventsCh     <-chan JobEvent
	audioPending []string
	jobs         []jobView
	logs         []string
	done         *JobEvent
	showLog      bool

	// settings
	cfg            Config
	outputDir      string
	duplicate      bool
	settingsCursor int
	editingOutput  bool

	// transcription settings (engine-persisted via /api/config, ADR-0043)
	editingLanguage bool
	languageInput   string
	editingSpeakers bool
	speakersInput   string

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
	kind string // "convert" | "audio"
	res  GlobResult
	err  error
}

type cfgMsg struct {
	cfg Config
	err error
}

type jobClosedMsg struct{}

type statusMsg struct {
	status Status
	err    error
}

func (m *model) Init() tea.Cmd {
	return tea.Batch(m.loadFiles(), m.statusTick())
}

func (m *model) loadFiles() tea.Cmd {
	glob := func(kind string, fn func(ctx context.Context, path string, recursive bool) (GlobResult, error)) tea.Cmd {
		return func() tea.Msg {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			res, err := fn(ctx, m.cwd, true)
			return globMsg{kind, res, err}
		}
	}
	return tea.Batch(
		glob("convert", m.client.Glob),
		glob("audio", m.client.GlobAudio),
	)
}

func (m *model) fetchConfig() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		cfg, err := m.client.Config(ctx)
		return cfgMsg{cfg, err}
	}
}

func (m *model) fetchStatus() tea.Msg {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	s, err := m.client.Status(ctx)
	return statusMsg{s, err}
}

// statusTick re-arms the /api/status poll once per second, so externally-started
// jobs show up on the picker without the TUI owning their socket (ADR-0045).
func (m *model) statusTick() tea.Cmd {
	return tea.Tick(time.Second, func(time.Time) tea.Msg {
		return m.fetchStatus()
	})
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

	case statusMsg:
		m.mergeExternalJob(msg)
		return m, m.statusTick()

	case globMsg:
		if msg.err != nil {
			m.loadErr = msg.err
			return m, nil
		}
		for _, f := range msg.res.Files {
			m.files = append(m.files, f)
			m.kinds = append(m.kinds, msg.kind)
		}
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
	case "l":
		m.showLog = !m.showLog
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
	convertPaths, audioPaths := partitionSelected(m.files, m.kinds, m.selected)

	m.jobs = nil
	m.done = nil
	m.showLog = false
	m.audioPending = audioPaths

	if len(convertPaths) > 0 {
		m.jobs = append(m.jobs, jobView{kind: "convert", status: "running", total: len(convertPaths)})
		return m.runConvert(convertPaths)
	}
	if len(audioPaths) > 0 {
		m.jobs = append(m.jobs, jobView{kind: "transcribe", status: "running", total: len(audioPaths)})
		return m.runTranscribe(audioPaths)
	}
	return nil
}

func (m *model) runConvert(paths []string) tea.Cmd {
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

// runTranscribe starts an engine-hosted transcription (ADR-0045) rather than
// spawning ptm-transcribe; progress arrives as the same structured WS frames.
func (m *model) runTranscribe(paths []string) tea.Cmd {
	ch := make(chan JobEvent, 32)
	m.eventsCh = ch
	opts := map[string]any{
		"model":    m.cfg.AudioModel,
		"language": m.cfg.AudioLanguage,
		"diarize":  m.cfg.AudioDiarize,
		"speakers": m.cfg.AudioSpeakers,
	}
	go func() {
		defer close(ch)
		if err := m.client.RunTranscribeJob(context.Background(), paths, opts, func(ev JobEvent) {
			ch <- ev
		}); err != nil {
			ch <- JobEvent{Type: "error", Message: err.Error()}
		}
	}()
	return waitEvent(ch)
}

func (m *model) onJobEvent(ev JobEvent) (tea.Model, tea.Cmd) {
	switch ev.Type {
	case "file":
		m.updateJob(func(j *jobView) { j.name = ev.Name; j.idx = ev.Idx; j.total = ev.Total })
	case "page":
		m.updateJob(func(j *jobView) { j.page = ev.Page; j.pageTotal = ev.Total })
	case "phase":
		m.updateJob(func(j *jobView) { j.phase = ev.Phase })
	case "log":
		m.logs = append(m.logs, fmt.Sprintf("[%s] %s", ev.Kind, ev.Message))
	case "done", "error":
		m.finishJob(ev)
		if ev.Type == "done" && len(m.audioPending) > 0 {
			pending := m.audioPending
			m.audioPending = nil
			m.jobs = append(m.jobs, jobView{kind: "transcribe", status: "running", total: len(pending)})
			return m, m.runTranscribe(pending)
		}
		return m, nil
	}
	return m, waitEvent(m.eventsCh)
}

func (m *model) updateJob(fn func(*jobView)) {
	for i := len(m.jobs) - 1; i >= 0; i-- {
		if m.jobs[i].status == "running" {
			fn(&m.jobs[i])
			return
		}
	}
}

func (m *model) finishJob(ev JobEvent) {
	for i := len(m.jobs) - 1; i >= 0; i-- {
		if m.jobs[i].status == "running" {
			if ev.Type == "error" {
				m.jobs[i].status = "error"
				if ev.Error != "" {
					m.jobs[i].err = ev.Error
				} else {
					m.jobs[i].err = ev.Message
				}
			} else {
				m.jobs[i].status = "done"
				m.jobs[i].ok = ev.Ok
			}
			m.done = &ev
			return
		}
	}
}

// mergeExternalJob surfaces an externally-started job found via /api/status,
// but only when the WS is not already the live source of progress.
func (m *model) mergeExternalJob(msg statusMsg) {
	if msg.err != nil || msg.status.CurrentJob == nil {
		return
	}
	for _, existing := range m.jobs {
		if existing.status == "running" {
			return
		}
	}
	j := msg.status.CurrentJob
	m.jobs = append(m.jobs, jobView{
		kind:      j.Kind,
		status:    j.Status,
		name:      lastName(j.Paths),
		idx:       j.Idx,
		total:     j.Total,
		page:      j.Page,
		pageTotal: j.PageTotal,
		phase:     j.Phase,
	})
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
	case screenSettings:
		return settingsView(m)
	default:
		return pickView(m)
	}
}

func jobsStrip(m *model) string {
	if len(m.jobs) == 0 {
		return ""
	}
	var b strings.Builder
	for _, j := range m.jobs {
		glyph := "●"
		if j.status == "done" {
			glyph = "✓"
		} else if j.status == "error" {
			glyph = "✗"
		}
		line := fmt.Sprintf("%s %s", glyph, j.kind)
		if j.name != "" {
			line += "  " + filepath.Base(j.name)
		}
		if j.total > 0 {
			line += fmt.Sprintf("  %d/%d", j.idx, j.total)
		}
		if j.kind == "convert" && j.pageTotal > 0 {
			line += fmt.Sprintf("  page %d/%d", j.page, j.pageTotal)
		}
		if j.kind == "transcribe" && j.phase != "" {
			line += fmt.Sprintf("  phase: %s", j.phase)
		}
		if j.status == "error" && j.err != "" {
			line += "  " + j.err
		}
		b.WriteString(line)
		b.WriteString("\n")
	}
	return b.String()
}

func pickView(m *model) string {
	var b strings.Builder
	b.WriteString(headerStyle.Render("ptm-tui"))
	b.WriteString("\n")
	b.WriteString(subtleStyle.Render(m.cwd))
	b.WriteString("\n")

	if strip := jobsStrip(m); strip != "" {
		b.WriteString("\n")
		b.WriteString(strip)
	}

	if m.showLog {
		b.WriteString("\n")
		for _, line := range tail(m.logs, 12) {
			b.WriteString(subtleStyle.Render("  " + line))
			b.WriteString("\n")
		}
		b.WriteString("\n")
	}

	if m.loadErr != nil {
		b.WriteString("\n")
		b.WriteString(errStyle.Render("discovery failed: " + m.loadErr.Error()))
		b.WriteString("\n")
	}

	b.WriteString("\n")

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
			if m.kinds[idx] == "audio" {
				line = mark + subtleStyle.Render("♫ ") + m.relFiles[idx]
			}
			if i == m.cursor {
				line = cursorStyle.Render("> ") + line
			}
			b.WriteString(line)
			b.WriteString("\n")
		}
	}

	b.WriteString("\n")
	b.WriteString(hintStyle.Render("↑/↓ move · space select · @ filter · enter convert/transcribe · l log · s settings · q quit"))

	if m.done != nil {
		b.WriteString("\n")
		if m.done.Type == "error" {
			b.WriteString(errStyle.Render(fmt.Sprintf("error: %s", m.done.Message)))
		} else {
			b.WriteString(fmt.Sprintf("%d/%d done", m.done.Ok, m.done.Total))
		}
	}
	return b.String()
}

func tail(lines []string, n int) []string {
	if len(lines) <= n {
		return lines
	}
	return lines[len(lines)-n:]
}

func lastName(paths []string) string {
	if len(paths) == 0 {
		return ""
	}
	return filepath.Base(paths[len(paths)-1])
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
