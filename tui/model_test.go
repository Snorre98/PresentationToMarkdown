package main

// Regression tests for the picker/job-strip bugs of ADR-0053: a finished job
// must never be re-added to the strip by the status poll, and dispatching a run
// must consume the selection so the next enter acts on the cursor.

import (
	"strings"
	"testing"
)

func testModel() *model {
	return &model{
		client:   NewClient("http://127.0.0.1:1"), // never dialed in these tests
		cwd:      "/tmp",
		screen:   screenPick,
		selected: map[string]bool{},
	}
}

func withFiles(m *model, files, kinds []string) {
	m.files = files
	m.kinds = kinds
	m.relFiles = relPaths(m.cwd, files)
	m.refilter()
}

func TestMergeExternalJobOnlyWhileRunning(t *testing.T) {
	m := testModel()

	// No current job: nothing to merge.
	m.mergeExternalJob(statusMsg{status: Status{}})
	if len(m.jobs) != 0 {
		t.Fatalf("jobs = %d, want 0", len(m.jobs))
	}

	// A finished job (defensive: the engine should not send one) is ignored.
	done := &Job{ID: 3, Kind: "convert", Status: "done", Idx: 2, Total: 2}
	m.mergeExternalJob(statusMsg{status: Status{CurrentJob: done}})
	if len(m.jobs) != 0 {
		t.Fatalf("finished job merged: %+v", m.jobs)
	}

	// A running job merges exactly once, then only updates in place.
	running := &Job{ID: 4, Kind: "convert", Status: "running", Idx: 1, Total: 10, Page: 3, PageTotal: 12, Paths: []string{"/a/deck.pdf"}}
	m.mergeExternalJob(statusMsg{status: Status{CurrentJob: running}})
	running.Idx, running.Page = 2, 4
	m.mergeExternalJob(statusMsg{status: Status{CurrentJob: running}})
	if len(m.jobs) != 1 {
		t.Fatalf("jobs = %d, want 1", len(m.jobs))
	}
	j := m.jobs[0]
	if j.id != 4 || j.idx != 2 || j.page != 4 || j.status != "running" {
		t.Fatalf("job = %+v", j)
	}

	// A new job with a different id is a new strip entry.
	running = &Job{ID: 5, Kind: "convert", Status: "running", Idx: 1, Total: 1}
	m.jobs[0].status = "done" // the previous job finished locally
	m.mergeExternalJob(statusMsg{status: Status{CurrentJob: running}})
	if len(m.jobs) != 2 || m.jobs[1].id != 5 {
		t.Fatalf("jobs = %+v", m.jobs)
	}
}

func TestMergeExternalJobDoesNotDuplicateLocalRun(t *testing.T) {
	m := testModel()
	m.jobs = append(m.jobs, jobView{kind: "convert", status: "running", total: 3})
	// The locally started job has no id yet; the same job seen through
	// /api/status must not be appended a second time.
	m.mergeExternalJob(statusMsg{status: Status{CurrentJob: &Job{ID: 9, Kind: "convert", Status: "running", Total: 3}}})
	if len(m.jobs) != 1 {
		t.Fatalf("jobs = %d, want 1", len(m.jobs))
	}
}

func TestTakeSelectionConsumesCursor(t *testing.T) {
	m := testModel()
	withFiles(m, []string{"/a/one.pptx", "/a/two.pdf"}, []string{"convert", "convert"})

	convert, audio := m.takeSelection()
	if len(convert) != 1 || convert[0] != "/a/one.pptx" || len(audio) != 0 {
		t.Fatalf("first = %v %v", convert, audio)
	}
	if len(m.selected) != 0 {
		t.Fatalf("selection survived dispatch: %v", m.selected)
	}

	// Moving to the next item and pressing enter must run that item, not the
	// previously dispatched (and now archived) one.
	m.cursor = 1
	convert, _ = m.takeSelection()
	if len(convert) != 1 || convert[0] != "/a/two.pdf" {
		t.Fatalf("second = %v", convert)
	}
}

func TestTakeSelectionPrefersExplicitSelection(t *testing.T) {
	m := testModel()
	withFiles(m, []string{"/a/one.pptx", "/a/two.pdf"}, []string{"convert", "convert"})
	m.selected["/a/one.pptx"] = true
	m.cursor = 1

	convert, _ := m.takeSelection()
	if len(convert) != 1 || convert[0] != "/a/one.pptx" {
		t.Fatalf("explicit selection = %v", convert)
	}
}

func TestSetFilesPrunesSelectionAndRefreshes(t *testing.T) {
	m := testModel()
	withFiles(m, []string{"/a/gone.pptx"}, []string{"convert"})
	m.selected["/a/gone.pptx"] = true

	m.setFiles(filesMsg{
		convert: GlobResult{Files: []string{"/a/new.pptx"}},
		audio:   GlobResult{Files: []string{"/a/talk.m4a"}},
	})
	if len(m.files) != 2 || m.files[0] != "/a/new.pptx" || m.kinds[1] != "audio" {
		t.Fatalf("files = %v kinds = %v", m.files, m.kinds)
	}
	if len(m.selected) != 0 {
		t.Fatalf("stale selection kept: %v", m.selected)
	}

	m.selected["/a/new.pptx"] = true
	m.setFiles(filesMsg{convert: GlobResult{Files: []string{"/a/new.pptx"}}})
	if !m.selected["/a/new.pptx"] {
		t.Fatalf("existing selection dropped: %v", m.selected)
	}
}

func TestJobsStripIsCapped(t *testing.T) {
	m := testModel()
	for i := 0; i < 50; i++ {
		m.jobs = append(m.jobs, jobView{kind: "convert", status: "done", ok: 1, total: 1})
	}
	strip := jobsStrip(m)
	if got := strings.Count(strip, "\n"); got != maxStripJobs {
		t.Fatalf("strip lines = %d, want %d", got, maxStripJobs)
	}
}

func TestSettingsColumnsStable(t *testing.T) {
	// The settings view must not be empty and must keep one row per setting.
	m := testModel()
	view := settingsView(m)
	if got := strings.Count(view, "\n"); got < m.settingsRows() {
		t.Fatalf("settings rows = %d, want >= %d", got, m.settingsRows())
	}
}
