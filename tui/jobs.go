package main

// jobs.go — the in-session job store. Live `/ws` frames and status snapshots
// fold into job records; the store is the single source for the jobs panel and
// the results tab (ADR-0053). Jobs are keyed by the engine's monotonic id, so
// a finished job can never be re-added by a late snapshot.

import (
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"charm.land/bubbles/v2/progress"
)

const (
	jobsKept         = 50
	logTailKept      = 200
	jobsPanelMaxRows = 3
)

type job struct {
	id              int
	kind            string // convert | transcribe
	status          string // running | done | error | cancelled
	paths           []string
	current         string // file being worked on
	idx, total      int
	page, pageTotal int
	phase           string
	startedAt       float64
	finishedAt      float64
	cancelRequested bool
	ok              int
	err             string
	results         []JobResult
	logTail         []string
}

func (j *job) running() bool { return j.status == "running" || j.status == "cancelling" }

func (j *job) name() string {
	if j.current != "" {
		return filepath.Base(j.current)
	}
	if len(j.paths) > 0 {
		return filepath.Base(j.paths[len(j.paths)-1])
	}
	return ""
}

// progress is the composite 0..1 fraction: completed files plus the current
// file's page fraction (ADR-0013 gives per-page granularity for free).
func (j *job) progress() float64 {
	if j.total <= 0 {
		return 0
	}
	done := float64(j.idx - 1)
	if done < 0 {
		done = 0
	}
	frac := 0.0
	if j.pageTotal > 0 {
		frac = float64(j.page) / float64(j.pageTotal)
		if frac > 1 {
			frac = 1
		}
	}
	p := (done + frac) / float64(j.total)
	if p < 0 {
		p = 0
	}
	if p > 1 {
		p = 1
	}
	return p
}

func (j *job) elapsed(now time.Time) time.Duration {
	if j.startedAt == 0 {
		return 0
	}
	end := now
	if j.finishedAt > 0 {
		end = time.Unix(int64(j.finishedAt), 0)
	}
	return end.Sub(time.Unix(int64(j.startedAt), 0))
}

type jobStore struct {
	byID    map[int]*job
	order   []int // newest first
	pending *job  // dispatched locally; adopts the id from its first frame
}

func newJobStore() jobStore {
	return jobStore{byID: map[int]*job{}}
}

// startLocal registers an optimistic entry for a dispatched batch. It is
// replaced by the matching engine job as soon as a frame with an id arrives.
func (s *jobStore) startLocal(kind string, paths []string) *job {
	j := &job{
		kind:      kind,
		status:    "running",
		paths:     append([]string(nil), paths...),
		total:     len(paths),
		startedAt: float64(time.Now().Unix()),
	}
	s.pending = j
	return j
}

// failPending marks the optimistic entry failed (dispatch refused by the
// engine, e.g. "a job is already running").
func (s *jobStore) failPending(err string) {
	if s.pending == nil {
		return
	}
	s.pending.status = "error"
	s.pending.err = err
	s.pending.finishedAt = float64(time.Now().Unix())
	s.adopt(s.pending)
	s.pending = nil
}

// adopt files a job under its engine id (idempotent).
func (s *jobStore) adopt(j *job) {
	if _, ok := s.byID[j.id]; ok {
		return
	}
	s.byID[j.id] = j
	s.order = append([]int{j.id}, s.order...)
	for len(s.order) > jobsKept {
		oldest := s.order[len(s.order)-1]
		s.order = s.order[:len(s.order)-1]
		delete(s.byID, oldest)
	}
}

// apply folds one live frame into the store.
func (s *jobStore) apply(ev JobEvent) {
	if ev.JobID == 0 {
		if ev.Type == "error" {
			s.failPending(ev.Message)
		}
		return
	}
	j, ok := s.byID[ev.JobID]
	if !ok {
		if s.pending != nil {
			j = s.pending
			j.id = ev.JobID
			s.pending = nil
		} else {
			j = &job{id: ev.JobID, startedAt: float64(time.Now().Unix()), status: "running"}
		}
		if ev.JobKind != "" {
			j.kind = ev.JobKind
		}
		s.adopt(j)
	}
	switch ev.Type {
	case "file":
		j.current = ev.Name
		j.idx, j.total = ev.Idx, ev.Total
	case "page":
		j.page, j.pageTotal = ev.Page, ev.Total
		if ev.Name != "" {
			j.current = ev.Name
		}
	case "phase":
		j.phase = ev.Phase
	case "log":
		if ev.Message != "" {
			j.logTail = append(j.logTail, ev.Message)
			if len(j.logTail) > logTailKept {
				j.logTail = j.logTail[len(j.logTail)-logTailKept:]
			}
		}
	case "done":
		if ev.Error != "" {
			j.status, j.err = "error", ev.Error
		} else {
			j.status, j.ok = "done", ev.Ok
		}
		if len(ev.Results) > 0 {
			j.results = ev.Results
		}
		j.finishedAt = float64(time.Now().Unix())
		j.phase = ""
	case "cancelled":
		j.status, j.ok = "cancelled", ev.Ok
		j.finishedAt = float64(time.Now().Unix())
		j.phase = ""
	case "error":
		j.status, j.err = "error", ev.Message
		j.finishedAt = float64(time.Now().Unix())
	}
}

// mergeSnapshot folds a full engine snapshot in (subscribe/reconnect and the
// initial load). Jobs the engine no longer knows about are marked lost.
func (s *jobStore) mergeSnapshot(st Status) {
	seen := map[int]bool{}
	if st.CurrentJob != nil {
		s.mergeJob(*st.CurrentJob)
		seen[st.CurrentJob.ID] = true
	}
	for _, j := range st.Jobs {
		s.mergeJob(j)
		seen[j.ID] = true
	}
	for id, j := range s.byID {
		if j.running() && !seen[id] {
			j.status = "error"
			j.err = "engine no longer knows this job (restarted?)"
			j.finishedAt = float64(time.Now().Unix())
		}
	}
}

func (s *jobStore) mergeJob(src Job) {
	j, ok := s.byID[src.ID]
	if !ok {
		j = &job{id: src.ID}
		s.adopt(j)
	}
	j.kind, j.status, j.paths = src.Kind, src.Status, src.Paths
	j.idx, j.total, j.page, j.pageTotal, j.phase = src.Idx, src.Total, src.Page, src.PageTotal, src.Phase
	j.startedAt, j.finishedAt = src.StartedAt, derefFloat(src.FinishedAt)
	j.cancelRequested, j.results, j.logTail = src.CancelRequested, src.Results, src.LogTail
	j.ok = src.Ok
	if src.Error != "" {
		j.err = src.Error
	}
}

func derefFloat(f *float64) float64 {
	if f == nil {
		return 0
	}
	return *f
}

func (s *jobStore) running() []*job {
	var out []*job
	for _, id := range s.order {
		if j := s.byID[id]; j != nil && j.running() {
			out = append(out, j)
		}
	}
	return out
}

func (s *jobStore) finished() []*job {
	var out []*job
	for _, id := range s.order {
		if j := s.byID[id]; j != nil && !j.running() {
			out = append(out, j)
		}
	}
	return out
}

func (s *jobStore) hasRunning() bool { return len(s.running()) > 0 }

// latest returns the most recent job of any state.
func (s *jobStore) latest() *job {
	if len(s.order) == 0 {
		return nil
	}
	return s.byID[s.order[0]]
}

// --- rendering ---------------------------------------------------------------

func jobBarColors() []progress.Option {
	return []progress.Option{progress.WithColors(th.bar, th.bar2), progress.WithoutPercentage()}
}

func (m *model) renderJobsPanel(width int) []string {
	running := m.jobs.running()
	if len(running) > 0 {
		var rows []string
		for _, j := range running {
			rows = append(rows, m.renderRunningJob(j, width)...)
		}
		if len(rows) > jobsPanelMaxRows {
			extra := len(rows) - jobsPanelMaxRows + 1
			rows = append(rows[:jobsPanelMaxRows-1], th.subtle.Render(fmt.Sprintf("  … %d more lines", extra)))
		}
		return rows
	}
	if j := m.jobs.latest(); j != nil && !j.running() {
		return []string{m.renderFinishedJob(j, width)}
	}
	return nil
}

func (m *model) renderRunningJob(j *job, width int) []string {
	glyph := "●"
	if j.cancelRequested {
		glyph = "◐"
	}
	head := fmt.Sprintf("%s %s %s", glyph, j.kind, th.bold.Render(j.name()))
	if j.kind == "convert" && j.total > 0 {
		head += fmt.Sprintf("  %d/%d files", j.idx, j.total)
	}
	if j.kind == "transcribe" && j.phase != "" {
		head += "  phase: " + j.phase
	}
	if j.cancelRequested {
		head += "  " + th.warn.Render("stopping…")
	}
	head += "  " + th.subtle.Render(formatDuration(j.elapsed(m.now())))

	barWidth := width - 4
	if barWidth < 10 {
		barWidth = 10
	}
	if barWidth > 40 {
		barWidth = 40
	}
	bar := progress.New(jobBarColors()...)
	bar.SetWidth(barWidth)
	detail := ""
	if j.kind == "convert" && j.pageTotal > 0 {
		detail = fmt.Sprintf("page %d/%d", j.page, j.pageTotal)
	} else if j.kind == "transcribe" && j.total > 0 {
		detail = fmt.Sprintf("file %d/%d", j.idx, j.total)
	}
	line2 := "  " + bar.ViewAs(j.progress())
	if detail != "" {
		line2 += "  " + th.subtle.Render(detail)
	}
	if j.kind == "transcribe" {
		line2 += "  " + th.accent.Render(spinnerFrame(m.now()))
	}
	// Two lines per running job keeps the panel bounded and scannable; narrow
	// terminals get the headline only.
	if width < 60 {
		return []string{"  " + head}
	}
	return []string{"  " + head, line2}
}

func (m *model) renderFinishedJob(j *job, width int) string {
	glyph, style := "✓", th.ok
	switch j.status {
	case "error":
		glyph, style = "✗", th.err
	case "cancelled":
		glyph, style = "✗", th.warn
	}
	line := fmt.Sprintf("%s %s %s", style.Render(glyph), j.kind, th.bold.Render(j.name()))
	if j.status == "cancelled" {
		line += "  " + th.warn.Render("cancelled")
	}
	if j.total > 1 {
		line += fmt.Sprintf("  %d files", j.total)
	}
	if j.status == "done" && j.ok >= 0 && j.ok != j.total {
		line += fmt.Sprintf("  %s", th.warn.Render(fmt.Sprintf("%d/%d ok", j.ok, j.total)))
	}
	if j.err != "" {
		line += "  " + th.err.Render(truncate(j.err, 60))
	}
	line += "  " + th.subtle.Render(formatDuration(j.elapsed(m.now())))
	return "  " + line
}

var spinnerFrames = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}

func spinnerFrame(now time.Time) string {
	return spinnerFrames[int(now.UnixMilli()/100)%len(spinnerFrames)]
}

func formatDuration(d time.Duration) string {
	if d < 0 {
		d = 0
	}
	secs := int(d.Seconds())
	if secs < 60 {
		return fmt.Sprintf("0:%02d", secs)
	}
	return fmt.Sprintf("%d:%02d", secs/60, secs%60)
}

// shortPath renders a path relative to cwd when possible.
func shortPath(cwd, path string) string {
	if rel, err := filepath.Rel(cwd, path); err == nil && !strings.HasPrefix(rel, "..") {
		return rel
	}
	return path
}
