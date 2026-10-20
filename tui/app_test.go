package main

// app_test.go — model-level regression tests for the ADR-0053 TUI: the job
// store's id-keyed merge, picker selection semantics, and the render invariant
// that the frame never exceeds the terminal.

import (
	"context"
	"fmt"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
)

// fakeEngine satisfies engineAPI; the tests drive frames through Update
// directly, so every method is a harmless stub.
type fakeEngine struct{}

func (fakeEngine) Health(context.Context) (Health, error) { return Health{OK: true}, nil }
func (fakeEngine) Glob(context.Context, string, bool) (GlobResult, error) {
	return GlobResult{}, nil
}
func (fakeEngine) GlobAudio(context.Context, string, bool) (GlobResult, error) {
	return GlobResult{}, nil
}
func (fakeEngine) Recent(context.Context) ([]string, error) { return nil, nil }
func (fakeEngine) Config(context.Context) (Config, error)   { return Config{}, nil }
func (fakeEngine) SetConfig(context.Context, ConfigUpdate) (Config, error) {
	return Config{}, nil
}
func (fakeEngine) Open(context.Context, string, bool) error { return nil }
func (fakeEngine) CancelJob(context.Context, int) error     { return nil }
func (fakeEngine) Runs(context.Context, int) ([]Run, error) { return nil, nil }
func (fakeEngine) Fleet(context.Context) (FleetResult, error) {
	return FleetResult{}, nil
}

func testApp() *model {
	return newModelCore(fakeEngine{}, "/wd", 9091)
}

func setEntriesPath(m *model, paths ...string) {
	entries := make([]GlobEntry, 0, len(paths))
	for _, p := range paths {
		entries = append(entries, GlobEntry{Path: p, Name: baseName(p), Kind: "convert"})
	}
	m.entries = entries
	m.refilter()
}

// --- job store ---------------------------------------------------------------

func TestJobStoreMergesFramesByID(t *testing.T) {
	m := testApp()
	m.jobs.apply(JobEvent{Type: "file", JobID: 7, JobKind: "convert", Idx: 1, Total: 3, Name: "a.pdf"})
	m.jobs.apply(JobEvent{Type: "page", JobID: 7, Page: 4, Total: 10})
	if n := len(m.jobs.running()); n != 1 {
		t.Fatalf("running = %d, want 1", n)
	}
	j := m.jobs.latest()
	if j.id != 7 || j.kind != "convert" || j.page != 4 || j.total != 3 {
		t.Fatalf("job = %+v", j)
	}
	m.jobs.apply(JobEvent{Type: "done", JobID: 7, Ok: 3, Total: 3})
	if m.jobs.hasRunning() {
		t.Fatal("job still running after done")
	}
	if j.status != "done" {
		t.Fatalf("status = %q", j.status)
	}
}

func TestJobStoreSnapshotDoesNotDuplicateOrResurrect(t *testing.T) {
	m := testApp()
	snap := Status{
		Status:     "running",
		CurrentJob: &Job{ID: 4, Kind: "convert", Status: "running", Paths: []string{"/a/deck.pdf"}, Idx: 1, Total: 2},
	}
	m.jobs.mergeSnapshot(snap)
	m.jobs.mergeSnapshot(snap) // the old duplicate-line bug
	if n := len(m.jobs.running()); n != 1 {
		t.Fatalf("running = %d, want 1", n)
	}

	finished := Status{
		Status: "done",
		Jobs: []Job{{
			ID: 5, Kind: "convert", Status: "done", Paths: []string{"/a/two.pdf"},
			Ok: 1,
		}},
	}
	m.jobs.mergeSnapshot(finished)
	m.jobs.mergeSnapshot(finished)
	ids := map[int]int{}
	for _, j := range m.jobs.finished() {
		ids[j.id]++
	}
	// id 4 was the running job the engine no longer knows (marked lost) and
	// id 5 the finished one; neither is duplicated by the second snapshot.
	if ids[5] != 1 || ids[4] != 1 {
		t.Fatalf("finished ids = %v", ids)
	}
}

func TestJobStoreAdoptsPendingLocalJob(t *testing.T) {
	m := testApp()
	m.jobs.startLocal("convert", []string{"/a/deck.pdf", "/a/other.pdf"})
	m.jobs.apply(JobEvent{Type: "file", JobID: 11, Idx: 1, Total: 2, Name: "deck.pdf"})
	if m.jobs.pending != nil {
		t.Fatal("pending not adopted")
	}
	j := m.jobs.latest()
	if j.id != 11 || j.kind != "convert" || len(j.paths) != 2 {
		t.Fatalf("job = %+v", j)
	}
}

func TestJobStoreMarksLostJobsAfterEngineRestart(t *testing.T) {
	m := testApp()
	m.jobs.apply(JobEvent{Type: "file", JobID: 3, JobKind: "convert", Idx: 1, Total: 1, Name: "x.pdf"})
	m.jobs.mergeSnapshot(Status{Status: "idle"}) // engine restarted, no jobs
	j := m.jobs.latest()
	if j.running() {
		t.Fatalf("lost job still running: %+v", j)
	}
}

// --- picker selection --------------------------------------------------------

func TestTakeSelectionConsumesCursor(t *testing.T) {
	m := testApp()
	setEntriesPath(m, "/wd/a.pptx", "/wd/b.pdf")

	convert, audio := m.takeSelection()
	if len(convert) != 1 || convert[0] != "/wd/a.pptx" || len(audio) != 0 {
		t.Fatalf("first = %v %v", convert, audio)
	}
	if len(m.selected) != 0 {
		t.Fatalf("selection survived dispatch: %v", m.selected)
	}
	m.cursor = 1
	convert, _ = m.takeSelection()
	if len(convert) != 1 || convert[0] != "/wd/b.pdf" {
		t.Fatalf("second = %v", convert)
	}
}

func TestTakeSelectionUsesFilteredOutSelection(t *testing.T) {
	m := testApp()
	setEntriesPath(m, "/wd/a.pptx", "/wd/b.pdf")
	m.selected["/wd/b.pdf"] = true
	m.filter = "a" // hides b, but the explicit selection still dispatches
	m.refilter()

	convert, _ := m.takeSelection()
	if len(convert) != 1 || convert[0] != "/wd/b.pdf" {
		t.Fatalf("convert = %v", convert)
	}
}

func TestPruneSelectionDropsArchived(t *testing.T) {
	m := testApp()
	setEntriesPath(m, "/wd/gone.pptx")
	m.selected["/wd/gone.pptx"] = true
	m.setEntries(filesMsg{convert: GlobResult{Entries: []GlobEntry{{Path: "/wd/new.pptx", Name: "new.pptx", Kind: "convert"}}}})
	if len(m.entries) != 1 || len(m.selected) != 0 {
		t.Fatalf("entries=%d selected=%v", len(m.entries), m.selected)
	}
}

func TestSetEntriesFallsBackToFilesArray(t *testing.T) {
	m := testApp()
	m.setEntries(filesMsg{
		convert: GlobResult{Files: []string{"/wd/a.pptx"}},
		audio:   GlobResult{Files: []string{"/wd/talk.m4a"}},
	})
	if len(m.entries) != 2 || m.entries[1].Kind != "audio" {
		t.Fatalf("entries = %+v", m.entries)
	}
}

// --- render invariants -------------------------------------------------------

func renderStates(m *model) []string {
	states := map[string]func(){
		"empty": func() {},
		"files": func() {
			setEntriesPath(m, "/wd/03-what-makes-things-fun.pdf", "/wd/07-lecture-notes.pdf")
			m.selected["/wd/03-what-makes-things-fun.pdf"] = true
		},
		"filtering": func() {
			m.filtering, m.filter = true, "lec"
			m.refilter()
		},
		"running": func() {
			m.filtering, m.filter = false, ""
			m.jobs.apply(JobEvent{Type: "file", JobID: 1, JobKind: "convert", Idx: 2, Total: 5, Name: "deck.pdf"})
			m.jobs.apply(JobEvent{Type: "page", JobID: 1, Page: 3, Total: 12})
		},
		"finished": func() {
			m.jobs.apply(JobEvent{Type: "done", JobID: 1, Ok: 5, Total: 5})
			m.rebuildResults()
		},
		"results-tab": func() { m.tab = tabResults },
		"log-tab": func() {
			m.tab = tabLog
			for i := 0; i < 120; i++ {
				m.logLine("info", fmt.Sprintf("line %d", i))
			}
		},
		"settings": func() {
			m.tab = tabFiles
			m.overlay = overlaySettings
		},
		"help": func() { m.overlay = overlayHelp },
	}
	order := []string{"empty", "files", "filtering", "running", "finished", "results-tab", "log-tab", "settings", "help"}
	out := make([]string, 0, len(order))
	for _, name := range order {
		states[name]()
		out = append(out, m.render())
		m.overlay = overlayNone
	}
	return out
}

func TestRenderNeverExceedsTerminal(t *testing.T) {
	sizes := [][2]int{{40, 12}, {80, 24}, {120, 40}, {30, 8}}
	for _, size := range sizes {
		m := testApp()
		m.width, m.height = size[0], size[1]
		m.help.SetWidth(size[0])
		for i, view := range renderStates(m) {
			lines := strings.Split(view, "\n")
			if len(lines) != size[1] {
				t.Fatalf("%dx%d state %d: %d lines, want %d", size[0], size[1], i, len(lines), size[1])
			}
			for n, line := range lines {
				if w := visibleWidth(line); w > size[0] {
					t.Fatalf("%dx%d state %d line %d: width %d: %q", size[0], size[1], i, n, w, line)
				}
			}
		}
	}
}

// --- keys --------------------------------------------------------------------

func keyMsg(s string) tea.KeyPressMsg {
	switch s {
	case "enter":
		return tea.KeyPressMsg{Code: tea.KeyEnter}
	case "esc":
		return tea.KeyPressMsg{Code: tea.KeyEsc}
	case "tab":
		return tea.KeyPressMsg{Code: tea.KeyTab}
	case "backspace":
		return tea.KeyPressMsg{Code: tea.KeyBackspace}
	}
	return tea.KeyPressMsg{Text: s}
}

func TestSettingsOverlayToggles(t *testing.T) {
	m := testApp()
	m.width, m.height = 80, 24

	if _, _ = m.Update(keyMsg("s")); m.overlay != overlaySettings {
		t.Fatal("settings overlay did not open")
	}
	if _, _ = m.Update(keyMsg("space")); !m.cfg.Features["vision"] {
		t.Fatalf("feature toggle: %v", m.cfg.Features)
	}
	if _, _ = m.Update(keyMsg("esc")); m.overlay != overlayNone {
		t.Fatal("settings overlay did not close")
	}
}

func TestQuitConfirmsWhileJobRuns(t *testing.T) {
	m := testApp()
	m.jobs.apply(JobEvent{Type: "file", JobID: 1, JobKind: "convert", Idx: 0, Total: 1, Name: "x.pdf"})
	if _, cmd := m.Update(keyMsg("q")); cmd != nil {
		t.Fatal("quit did not ask for confirmation")
	}
	if !m.confirmQuit {
		t.Fatal("confirmQuit not set")
	}
	if _, cmd := m.Update(keyMsg("q")); cmd == nil {
		t.Fatal("second q should quit")
	}
}
