package main

// app.go — the root Bubble Tea model: a fixed-frame layout (header, jobs panel,
// tabbed body, footer) over the engine watch stream (ADR-0053). Every pane
// renders into a bounded window, so the view can never exceed the terminal.

import (
	"context"
	"fmt"
	"path/filepath"
	"time"

	"charm.land/bubbles/v2/help"
	tea "charm.land/bubbletea/v2"
)

type tab int

const (
	tabFiles tab = iota
	tabResults
	tabLog
)

type overlay int

const (
	overlayNone overlay = iota
	overlaySettings
	overlayHelp
	overlayAI
)

// engineAPI is the engine surface the app consumes; *Client implements it.
type engineAPI interface {
	Health(ctx context.Context) (Health, error)
	Glob(ctx context.Context, path string, recursive bool) (GlobResult, error)
	GlobAudio(ctx context.Context, path string, recursive bool) (GlobResult, error)
	Recent(ctx context.Context) ([]string, error)
	Config(ctx context.Context) (Config, error)
	SetConfig(ctx context.Context, u ConfigUpdate) (Config, error)
	Open(ctx context.Context, path string, reveal bool) error
	CancelJob(ctx context.Context, id int) error
	Runs(ctx context.Context, limit int) ([]Run, error)
	Fleet(ctx context.Context) (FleetResult, error)
}

type logLine struct {
	kind string
	text string
}

type model struct {
	client engineAPI
	watch  *watchClient

	ctx    context.Context
	cancel context.CancelFunc
	port   int

	spawned bool

	cwd           string
	width, height int
	bodyTop       int

	engineReady bool
	engineErr   string
	connected   bool

	entries []GlobEntry
	recent  []string
	loadErr error

	jobs         jobStore
	audioPending []string

	// picker
	cursor    int
	offset    int
	filter    string
	filtering bool
	selected  map[string]bool

	// results
	rstRows   []resultRow
	resCursor int
	resDetail map[int]bool
	histRuns  []Run

	// log
	logLines  []logLine
	logOffset int
	logFollow bool

	tab tab

	// settings
	cfg       Config
	outputDir string
	duplicate bool
	setCursor int
	editing   editField
	editBuf   string
	fleet     FleetResult
	fleetAt   time.Time
	fleetErr  string
	aiCursor  int

	overlay overlay
	help    help.Model

	toast    string
	toastErr bool
	toastAt  time.Time

	confirmQuit bool
}

func newModel(client *Client, cwd string, port int) *model {
	m := newModelCore(client, cwd, port)
	m.watch = newWatchClient(client)
	return m
}

// newModelCore wires the state without a watch socket (tests inject a fake
// engine and drive frames through Update directly).
func newModelCore(client engineAPI, cwd string, port int) *model {
	ctx, cancel := context.WithCancel(context.Background())
	return &model{
		client:    client,
		cwd:       cwd,
		port:      port,
		ctx:       ctx,
		cancel:    cancel,
		jobs:      newJobStore(),
		selected:  map[string]bool{},
		resDetail: map[int]bool{},
		logFollow: true,
		help:      help.New(),
	}
}

// --- messages ----------------------------------------------------------------

type filesMsg struct {
	convert GlobResult
	audio   GlobResult
	err     error
}

type recentMsg struct {
	files []string
	err   error
}

type runsMsg struct {
	runs []Run
	err  error
}

type engineUpMsg struct {
	spawned bool
	err     error
}

type cfgMsg struct {
	cfg Config
	err error
}

// --- commands ----------------------------------------------------------------

func (m *model) Init() tea.Cmd {
	if m.watch != nil {
		go m.watch.run(m.ctx)
	}
	return tea.Batch(m.startEngine(), tea.RequestBackgroundColor)
}

// startEngine attaches to a healthy engine or spawns one via PTM_ENGINE_CMD.
func (m *model) startEngine() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(m.ctx, 5*time.Second)
		_, err := m.client.Health(ctx)
		cancel()
		if err == nil {
			return engineUpMsg{}
		}
		cmdLine := env("PTM_ENGINE_CMD")
		if cmdLine == "" {
			return engineUpMsg{err: fmt.Errorf("ptm-engine not reachable on :%d and PTM_ENGINE_CMD is not set", m.port)}
		}
		cmd := shellCommand(cmdLine, m.port)
		if err := cmd.Start(); err != nil {
			return engineUpMsg{err: fmt.Errorf("failed to start ptm-engine: %w", err)}
		}
		go cmd.Wait()
		if err := waitHealthy(m.client, 30*time.Second); err != nil {
			return engineUpMsg{err: err}
		}
		return engineUpMsg{spawned: true}
	}
}

func (m *model) loadFiles() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		res, err := m.client.Glob(ctx, m.cwd, true)
		if err != nil {
			return filesMsg{err: err}
		}
		audio, err := m.client.GlobAudio(ctx, m.cwd, true)
		if err != nil {
			return filesMsg{err: err}
		}
		return filesMsg{convert: res, audio: audio}
	}
}

func (m *model) loadRecent() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		files, err := m.client.Recent(ctx)
		return recentMsg{files, err}
	}
}

func (m *model) loadRuns() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		runs, err := m.client.Runs(ctx, 30)
		return runsMsg{runs, err}
	}
}

// refreshCmd re-discovers inputs and the recent list (archive moves change
// both, ADR-0048).
func (m *model) refreshCmd() tea.Cmd {
	return tea.Batch(m.loadFiles(), m.loadRecent(), m.loadRuns())
}

func (m *model) fetchConfig() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		cfg, err := m.client.Config(ctx)
		return cfgMsg{cfg, err}
	}
}

// --- update ------------------------------------------------------------------

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.help.SetWidth(m.width)
		return m, nil

	case tea.BackgroundColorMsg:
		th = newTheme(msg.IsDark())
		m.help.Styles = help.DefaultStyles(msg.IsDark())
		return m, nil

	case engineUpMsg:
		if msg.err != nil {
			m.engineErr = msg.err.Error()
			return m, nil
		}
		if msg.spawned {
			m.spawned = true
		}
		m.engineReady = true
		// Load settings up front: output dir and duplicate handling apply to
		// the first dispatch, not only after opening the settings modal.
		return m, tea.Batch(m.loadFiles(), m.loadRecent(), m.loadRuns(), m.fetchConfig())

	case filesMsg:
		if msg.err != nil {
			m.loadErr = msg.err
			return m, nil
		}
		m.loadErr = nil
		m.setEntries(msg)
		return m, nil

	case recentMsg:
		if msg.err == nil {
			m.recent = msg.files
		}
		return m, nil

	case runsMsg:
		if msg.err == nil {
			m.histRuns = msg.runs
			m.rebuildResults()
		}
		return m, nil

	case cfgMsg:
		if msg.err != nil {
			m.toastf(true, "settings: %s", msg.err)
			return m, nil
		}
		m.cfg = msg.cfg
		m.duplicate = msg.cfg.Duplicate
		if m.overlay == overlayAI {
			// A toggle replies with the effective feature state (implies
			// included); refresh readiness for the new pass set.
			return m, m.fetchFleet()
		}
		return m, nil

	case fleetMsg:
		if msg.err != nil {
			m.fleetErr = msg.err.Error()
			return m, nil
		}
		m.fleet = msg.res
		m.fleetAt = time.Now()
		m.fleetErr = ""
		return m, nil

	case watchConnMsg:
		m.connected = msg.connected
		if msg.connected {
			m.engineReady = true
		} else if m.engineReady {
			m.toastf(true, "engine connection lost — reconnecting…")
		}
		return m, nil

	case watchStatusMsg:
		m.connected = true
		m.engineReady = true
		m.jobs.mergeSnapshot(msg.status)
		m.rebuildResults()
		return m, nil

	case JobEvent:
		return m.onJobEvent(msg)

	case errMsg:
		m.toastf(true, "%s", msg.err)
		return m, nil

	case tea.MouseWheelMsg:
		return m.onWheel(msg)

	case tea.KeyPressMsg:
		return m.onKey(msg)
	}
	return m, nil
}

func (m *model) onJobEvent(ev JobEvent) (tea.Model, tea.Cmd) {
	m.jobs.apply(ev)
	switch ev.Type {
	case "log":
		m.logLine(ev.Kind, ev.Message)
	case "error":
		m.logLine("err", ev.Message)
		if ev.JobID == 0 {
			m.jobs.failPending(ev.Message)
			m.toastf(true, "%s", ev.Message)
		}
	default:
		m.logLine("info", frameSummary(ev))
	}
	if ev.Type == "done" || ev.Type == "cancelled" || ev.Type == "error" {
		m.rebuildResults()
		if ev.Type == "done" {
			m.toastf(ev.Error != "", "%s", doneSummary(ev))
		}
		cmds := []tea.Cmd{m.refreshCmd()}
		if ev.Type == "done" && len(m.audioPending) > 0 {
			pending := m.audioPending
			m.audioPending = nil
			cmds = append(cmds, m.dispatchTranscribe(pending))
		}
		return m, tea.Batch(cmds...)
	}
	return m, nil
}

func (m *model) onKey(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	s := msg.String()
	if s != "q" {
		m.confirmQuit = false
	}
	switch s {
	case "ctrl+c":
		m.cancel()
		return m, tea.Quit
	case "esc":
		if m.overlay != overlayNone {
			m.overlay = overlayNone
			m.editing = editNone
			return m, nil
		}
	}
	if m.overlay == overlaySettings {
		return m.updateSettings(msg)
	}
	if m.overlay == overlayHelp {
		if s == "?" || s == "q" {
			m.overlay = overlayNone
		}
		return m, nil
	}
	if m.overlay == overlayAI {
		return m.updateAISetup(msg)
	}
	if m.filtering && m.tab == tabFiles {
		return m.updateFiles(msg)
	}
	switch s {
	case "q":
		if !m.jobs.hasRunning() {
			m.cancel()
			return m, tea.Quit
		}
		if !m.confirmQuit {
			m.confirmQuit = true
			m.toastf(true, "a job is running — press q again to quit")
			return m, nil
		}
		m.cancel()
		return m, tea.Quit
	case "?":
		m.overlay = overlayHelp
		return m, nil
	case "s":
		m.overlay = overlaySettings
		if m.setCursor >= m.settingsRows() {
			m.setCursor = m.settingsRows() - 1
		}
		return m, m.fetchConfig()
	case "a":
		m.overlay = overlayAI
		m.aiCursor = 0
		return m, m.fetchFleet()
	case "x":
		if j := m.jobs.latest(); j != nil && j.running() {
			return m, m.cancelCmd(j.id)
		}
		return m, nil
	case "tab":
		m.tab = (m.tab + 1) % 3
		return m, nil
	case "shift+tab":
		m.tab = (m.tab + 2) % 3
		return m, nil
	case "1":
		m.tab = tabFiles
		return m, nil
	case "2":
		m.tab = tabResults
		return m, nil
	case "3":
		m.tab = tabLog
		return m, nil
	}
	switch m.tab {
	case tabFiles:
		return m.updateFiles(msg)
	case tabResults:
		return m.updateResults(msg)
	default:
		return m.updateLog(msg)
	}
}

func (m *model) updateFiles(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	rows, _ := m.visibleEntries()
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
			if msg.Text != "" {
				m.filter += msg.Text
				m.refilter()
			}
		}
		return m, nil
	}
	switch msg.String() {
	case "@", "/":
		m.filtering = true
		m.filter = ""
		m.refilter()
	case "up", "k":
		if m.cursor > 0 {
			m.cursor--
		}
	case "down", "j":
		if m.cursor < len(rows)-1 {
			m.cursor++
		}
	case "pgup":
		m.cursor -= 10
		if m.cursor < 0 {
			m.cursor = 0
		}
	case "pgdown", "ctrl+f":
		m.cursor += 10
		if m.cursor > len(rows)-1 {
			m.cursor = len(rows) - 1
		}
	case "home", "g":
		m.cursor = 0
	case "end", "G":
		m.cursor = len(rows) - 1
	case " ", "space":
		m.toggleSelect()
	case "a":
		m.selectAllVisible()
	case "c":
		m.selected = map[string]bool{}
	case "r":
		return m, m.refreshCmd()
	case "enter":
		convert, audio := m.takeSelection()
		if len(convert) == 0 && len(audio) == 0 {
			return m, nil
		}
		return m, m.dispatch(convert, audio)
	}
	if m.cursor < 0 {
		m.cursor = 0
	}
	return m, nil
}

func (m *model) updateResults(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "up", "k":
		if m.resCursor > 0 {
			m.resCursor--
		}
	case "down", "j":
		if m.resCursor < len(m.rstRows)-1 {
			m.resCursor++
		}
	case "pgup":
		m.resCursor -= 10
		if m.resCursor < 0 {
			m.resCursor = 0
		}
	case "pgdown":
		m.resCursor += 10
		if m.resCursor > len(m.rstRows)-1 {
			m.resCursor = len(m.rstRows) - 1
		}
	case "home", "g":
		m.resCursor = 0
	case "end", "G":
		m.resCursor = len(m.rstRows) - 1
	case "enter":
		if row := m.selectedResult(); row != nil {
			m.resDetail[m.resCursor] = !m.resDetail[m.resCursor]
		}
	case "o":
		if row := m.selectedResult(); row != nil {
			target := row.mdPath
			if target == "" {
				target = row.source
			}
			return m, m.openCmd(target, false)
		}
	case "E":
		if row := m.selectedResult(); row != nil && row.epubPath != "" {
			return m, m.openCmd(row.epubPath, false)
		}
	case "f":
		if row := m.selectedResult(); row != nil {
			target := row.mdPath
			if target == "" {
				target = row.source
			}
			return m, m.openCmd(target, true)
		}
	case "y":
		if row := m.selectedResult(); row != nil {
			target := row.mdPath
			if target == "" {
				target = row.source
			}
			if target != "" {
				return m, tea.SetClipboard(target)
			}
		}
	case "r":
		return m, m.loadRuns()
	}
	if m.resCursor < 0 {
		m.resCursor = 0
	}
	return m, nil
}

func (m *model) updateLog(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	h := m.bodyHeight()
	switch msg.String() {
	case "up", "k":
		if m.logFollow {
			m.logOffset = len(m.logLines) - h
			m.logFollow = false
		}
		if m.logOffset > 0 {
			m.logOffset--
		}
	case "down", "j":
		if m.logOffset < len(m.logLines)-h {
			m.logOffset++
		}
	case "pgup":
		m.logFollow = false
		m.logOffset -= h
	case "pgdown":
		m.logOffset += h
	case "home", "g":
		m.logFollow = false
		m.logOffset = 0
	case "end", "G", "f":
		m.logFollow = true
	}
	if m.logOffset < 0 {
		m.logOffset = 0
	}
	if max := len(m.logLines) - h; m.logOffset > max {
		m.logOffset = max
		if m.logOffset < 0 {
			m.logOffset = 0
		}
	}
	return m, nil
}

func (m *model) onWheel(msg tea.MouseWheelMsg) (tea.Model, tea.Cmd) {
	delta := -3
	if msg.Button == tea.MouseWheelDown {
		delta = 3
	}
	switch m.tab {
	case tabFiles:
		if msg.Y >= m.bodyTop {
			rows, sections := m.visibleEntries()
			_ = sections
			m.cursor += delta
			if m.cursor < 0 {
				m.cursor = 0
			}
			if m.cursor > len(rows)-1 {
				m.cursor = len(rows) - 1
			}
			if m.cursor < 0 {
				m.cursor = 0
			}
		}
	case tabResults:
		m.resCursor += delta
		if m.resCursor < 0 {
			m.resCursor = 0
		}
		if m.resCursor > len(m.rstRows)-1 {
			m.resCursor = len(m.rstRows) - 1
		}
		if m.resCursor < 0 {
			m.resCursor = 0
		}
	case tabLog:
		if m.logFollow {
			m.logFollow = false
			m.logOffset = len(m.logLines) - m.bodyHeight()
		}
		m.logOffset += delta
		if m.logOffset < 0 {
			m.logOffset = 0
		}
	}
	return m, nil
}

// --- dispatch ----------------------------------------------------------------

func (m *model) dispatch(convert, audio []string) tea.Cmd {
	if m.watch == nil || !m.watch.isConnected() {
		m.toastf(true, "engine is not connected")
		return nil
	}
	if len(convert) > 0 {
		m.jobs.startLocal("convert", convert)
		m.audioPending = audio
		m.logLine("info", fmt.Sprintf("converting %d file(s)…", len(convert)))
		if err := m.watch.send(map[string]any{
			"type":       "start",
			"paths":      convert,
			"output_dir": m.outputDir,
			"duplicate":  m.duplicate,
		}); err != nil {
			m.jobs.failPending(err.Error())
			m.toastf(true, "%s", err)
		}
		return nil
	}
	if len(audio) > 0 {
		return m.dispatchTranscribe(audio)
	}
	return nil
}

func (m *model) dispatchTranscribe(paths []string) tea.Cmd {
	if m.watch == nil || !m.watch.isConnected() {
		m.toastf(true, "engine is not connected")
		return nil
	}
	m.jobs.startLocal("transcribe", paths)
	m.logLine("info", fmt.Sprintf("transcribing %d file(s)…", len(paths)))
	opts := map[string]any{
		"model":        m.cfg.AudioModel,
		"language":     m.cfg.AudioLanguage,
		"diarize":      m.cfg.AudioDiarize,
		"speakers":     m.cfg.AudioSpeakers,
		"dereverb":     m.cfg.AudioDereverb,
		"enhance":      m.cfg.AudioEnhance,
		"prep_timeout": m.cfg.AudioPrepTimeout,
		"preflight":    m.cfg.AudioPreflight,
	}
	if err := m.watch.send(map[string]any{
		"type":          "transcribe",
		"paths":         paths,
		"audio_options": opts,
	}); err != nil {
		m.jobs.failPending(err.Error())
		m.toastf(true, "%s", err)
	}
	return nil
}

func (m *model) cancelCmd(id int) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := m.client.CancelJob(ctx, id); err != nil {
			return errMsg{err}
		}
		return nil
	}
}

type errMsg struct{ err error }

func (m *model) openCmd(path string, reveal bool) tea.Cmd {
	return func() tea.Msg {
		if path == "" {
			return nil
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := m.client.Open(ctx, path, reveal); err != nil {
			return errMsg{err}
		}
		return nil
	}
}

// --- small helpers -----------------------------------------------------------

func (m *model) setEntries(msg filesMsg) {
	entries := make([]GlobEntry, 0, len(msg.convert.Entries)+len(msg.audio.Entries))
	entries = append(entries, msg.convert.Entries...)
	for _, e := range msg.audio.Entries {
		e.Kind = "audio"
		entries = append(entries, e)
	}
	// Older engines may not send entries; fall back to the files array.
	if len(entries) == 0 {
		for _, p := range msg.convert.Files {
			entries = append(entries, GlobEntry{Path: p, Name: filepath.Base(p), Kind: "convert"})
		}
		for _, p := range msg.audio.Files {
			entries = append(entries, GlobEntry{Path: p, Name: filepath.Base(p), Kind: "audio"})
		}
	}
	m.entries = entries
	m.pruneSelection()
	m.refilter()
}

func (m *model) pruneSelection() {
	known := map[string]bool{}
	for _, e := range m.entries {
		known[e.Path] = true
	}
	for _, p := range m.recent {
		known[p] = true
	}
	for p := range m.selected {
		if !known[p] {
			delete(m.selected, p)
		}
	}
}

func (m *model) logLine(kind, text string) {
	if text == "" {
		return
	}
	m.logLines = append(m.logLines, logLine{kind, text})
	if len(m.logLines) > 2000 {
		m.logLines = m.logLines[len(m.logLines)-2000:]
	}
}

func (m *model) toastf(isErr bool, format string, args ...any) {
	m.toast = fmt.Sprintf(format, args...)
	m.toastErr = isErr
	m.toastAt = time.Now()
}

func (m *model) now() time.Time { return time.Now() }

func (m *model) bodyHeight() int {
	h := m.height - 1 - 1 - 1 - len(m.renderJobsPanel(m.width))
	if m.renderToast() != "" {
		h--
	}
	if h < 3 {
		h = 3
	}
	return h
}

func frameSummary(ev JobEvent) string {
	switch ev.Type {
	case "file":
		return fmt.Sprintf("file %d/%d: %s", ev.Idx, ev.Total, ev.Name)
	case "page":
		return fmt.Sprintf("page %d/%d: %s", ev.Page, ev.Total, ev.Name)
	case "phase":
		return "phase: " + ev.Phase
	}
	return ev.Type
}

func doneSummary(ev JobEvent) string {
	if ev.Error != "" {
		return "failed: " + ev.Error
	}
	return fmt.Sprintf("%d/%d done", ev.Ok, ev.Total)
}
