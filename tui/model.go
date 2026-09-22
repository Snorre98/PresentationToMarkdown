package main

// The Bubble Tea model: file picking (`@` fuzzy), conversion progress, and
// settings (ADR-0040). The model owns no conversion logic — it drives ptm-engine.

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
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
	kinds     []string // parallel to files: "convert" | "audio"
	relFiles  []string
	visible   []int
	cursor    int
	selected  map[int]bool
	filtering bool
	filter    string
	loadErr   error

	// running
	eventsCh      <-chan JobEvent
	transEventsCh chan tea.Msg
	runPhase      string // "convert" | "transcribe"
	audioPending  []string
	transcribing  bool
	transcribeBin string
	transcribeCmd *exec.Cmd
	transOk       int
	transTotal    int
	transFailed   bool
	curFile       string
	curIdx        int
	curTotal      int
	curPage       int
	curPageTot    int
	logs          []string
	done          *JobEvent

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
		client:        client,
		cwd:           cwd,
		spawned:       spawned,
		screen:        screenPick,
		selected:      map[int]bool{},
		transcribeBin: TranscribeBin(),
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

type transLineMsg struct {
	line string
}

type transDoneMsg struct {
	err error
}

type jobClosedMsg struct{}

var transDoneRe = regexp.MustCompile(`Done: (\d+) of (\d+) transcribed\.`)

func (m *model) Init() tea.Cmd {
	return m.loadFiles()
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

func waitEvent(ch <-chan JobEvent) tea.Cmd {
	return func() tea.Msg {
		ev, ok := <-ch
		if !ok {
			return jobClosedMsg{}
		}
		return ev
	}
}

func waitTransMsg(ch <-chan tea.Msg) tea.Cmd {
	return func() tea.Msg {
		msg, ok := <-ch
		if !ok {
			return transDoneMsg{}
		}
		return msg
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

	case transLineMsg:
		if m.screen == screenRun && m.transcribing {
			m.logs = append(m.logs, msg.line)
			if mm := transDoneRe.FindStringSubmatch(msg.line); mm != nil {
				m.transOk, _ = strconv.Atoi(mm[1])
				m.transTotal, _ = strconv.Atoi(mm[2])
			}
		}
		return m, waitTransMsg(m.transEventsCh)

	case transDoneMsg:
		m.transcribing = false
		m.audioPending = nil
		m.transcribeCmd = nil
		if msg.err != nil {
			m.transFailed = true
			m.logs = append(m.logs, "transcription failed: "+msg.err.Error())
		}
		m.screen = screenPick
		return m, nil

	case jobClosedMsg:
		if m.screen == screenRun && !m.transcribing {
			m.screen = screenPick
		}
		return m, nil

	case tea.KeyMsg:
		switch msg.String() {
		case "ctrl+c":
			if m.transcribeCmd != nil && m.transcribeCmd.Process != nil {
				_ = m.transcribeCmd.Process.Kill()
			}
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
	convertPaths, audioPaths := partitionSelected(m.files, m.kinds, m.selected)

	m.screen = screenRun
	m.runPhase = "convert"
	m.audioPending = audioPaths
	m.transcribeCmd = nil
	m.transcribing = false
	m.transOk, m.transTotal, m.transFailed = 0, 0, false
	m.curFile, m.curIdx, m.curTotal = "", 0, len(convertPaths)
	m.curPage, m.curPageTot = 0, 0
	m.logs = nil
	m.done = nil

	if len(convertPaths) == 0 {
		// audio-only selection: skip the engine, go straight to transcription
		m.runPhase = "transcribe"
		m.curTotal = len(audioPaths)
		return m.startTranscription()
	}

	ch := make(chan JobEvent, 32)
	m.eventsCh = ch
	go func() {
		defer close(ch)
		if err := m.client.RunJob(context.Background(), convertPaths, m.outputDir, m.duplicate, func(ev JobEvent) {
			ch <- ev
		}); err != nil {
			ch <- JobEvent{Type: "error", Message: err.Error()}
		}
	}()
	return waitEvent(ch)
}

// startTranscription spawns ptm-transcribe for every pending audio file and
// streams its stdout/stderr lines into the log pane. stdin is the null device
// so ptm-transcribe's interactive lecture picker (sys.stdin.isatty()) degrades
// to a standalone transcript instead of blocking the TUI.
func (m *model) startTranscription() tea.Cmd {
	m.transcribing = true
	cmd := exec.Command(m.transcribeBin, buildTranscribeArgs(m.audioPending)...)
	cmd.Stdin = nil
	m.transcribeCmd = cmd

	ch := make(chan tea.Msg, 64)
	m.transEventsCh = ch
	go func() {
		defer close(ch)
		stdout, err1 := cmd.StdoutPipe()
		stderr, err2 := cmd.StderrPipe()
		if err1 != nil || err2 != nil {
			ch <- transDoneMsg{fmt.Errorf("cannot stream ptm-transcribe output")}
			return
		}
		if err := cmd.Start(); err != nil {
			ch <- transDoneMsg{fmt.Errorf("ptm-transcribe failed to start: %w", err)}
			return
		}
		var wg sync.WaitGroup
		wg.Add(2)
		scan := func(r io.Reader) {
			defer wg.Done()
			sc := bufio.NewScanner(r)
			sc.Buffer(make([]byte, 64*1024), 1024*1024)
			for sc.Scan() {
				ch <- transLineMsg{sc.Text()}
			}
		}
		go scan(stdout)
		go scan(stderr)
		wg.Wait()
		ch <- transDoneMsg{cmd.Wait()}
	}()
	return waitTransMsg(ch)
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
		if len(m.audioPending) > 0 {
			// chained dispatch: conversion done (or failed) -> transcribe audio
			m.runPhase = "transcribe"
			m.curFile = ""
			m.curPage, m.curPageTot = 0, 0
			m.curTotal = len(m.audioPending)
			return m, m.startTranscription()
		}
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
		b.WriteString(errStyle.Render("discovery failed: " + m.loadErr.Error()))
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
	b.WriteString(hintStyle.Render("↑/↓ move · space select · @ filter · enter convert/transcribe · s settings · q quit"))

	if m.done != nil {
		b.WriteString("\n")
		if m.done.Type == "error" {
			b.WriteString(errStyle.Render(fmt.Sprintf("error: %s", m.done.Message)))
		} else {
			b.WriteString(fmt.Sprintf("%d/%d converted", m.done.Ok, m.done.Total))
		}
	}
	if m.transOk > 0 || m.transFailed {
		b.WriteString("\n")
		if m.transFailed {
			b.WriteString(errStyle.Render("transcription failed"))
		} else {
			b.WriteString(fmt.Sprintf("%d/%d transcribed", m.transOk, m.transTotal))
		}
	}
	return b.String()
}

func runView(m *model) string {
	var b strings.Builder
	title := "converting"
	if m.runPhase == "transcribe" {
		title = "transcribing"
	}
	b.WriteString(headerStyle.Render(title))
	b.WriteString("\n\n")
	if m.runPhase == "transcribe" {
		b.WriteString(fmt.Sprintf("  transcribing %d audio file(s)", m.curTotal))
		b.WriteString("\n")
	} else if m.curFile != "" {
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
