package main

// picker.go — the files tab: discovery rows (with metadata), the `@` fuzzy
// filter, multi-select keyed by path, and section rendering (recent + files).

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// matchScore returns a positive score when pattern fuzzy-matches s, else -1.
// Exact substring ranks highest, then subsequence. Higher is better.
func matchScore(pattern, s string) int {
	p := lower(pattern)
	t := lower(s)
	if p == "" {
		return 0
	}
	if i := indexOf(t, p); i >= 0 {
		// substring: prefer earlier, shorter matches
		return 2000 - i - len(p)
	}
	// subsequence match
	pi := 0
	for i := 0; i < len(t) && pi < len(p); i++ {
		if t[i] == p[pi] {
			pi++
		}
	}
	if pi != len(p) {
		return -1
	}
	return 100
}

// filterIndices returns the indices of items matching pattern, best-first.
func filterIndices(pattern string, items []string) []int {
	type scored struct {
		idx   int
		score int
	}
	matches := make([]scored, 0, len(items))
	for i, it := range items {
		if s := matchScore(pattern, it); s >= 0 {
			matches = append(matches, scored{i, s})
		}
	}
	sort.SliceStable(matches, func(a, b int) bool {
		if matches[a].score != matches[b].score {
			return matches[a].score > matches[b].score
		}
		return matches[a].idx < matches[b].idx
	})
	out := make([]int, len(matches))
	for i, m := range matches {
		out[i] = m.idx
	}
	return out
}

func lower(s string) string {
	b := []byte(s)
	for i := range b {
		if b[i] >= 'A' && b[i] <= 'Z' {
			b[i] += 'a' - 'A'
		}
	}
	return string(b)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

// --- picker state ------------------------------------------------------------

type pickSection struct {
	title string
	row   int // first row index of the section
}

// allEntries returns recent files (deduped against discovery, capped) followed
// by the discovery entries, regardless of the active filter.
func (m *model) allEntries() []GlobEntry {
	known := map[string]bool{}
	for _, e := range m.entries {
		known[e.Path] = true
	}
	var recent []GlobEntry
	for _, p := range m.recent {
		if p == "" || known[p] {
			continue
		}
		known[p] = true
		recent = append(recent, GlobEntry{Path: p, Name: baseName(p), Kind: "recent"})
		if len(recent) >= maxRecentRows {
			break
		}
	}
	return append(recent, m.entries...)
}

// visibleEntries returns the filter-ordered rows: recent files first (when no
// filter is active), then discovery entries. Sections are derived alongside.
func (m *model) visibleEntries() ([]GlobEntry, []pickSection) {
	all := m.allEntries()
	nRecent := len(all) - len(m.entries)

	if m.filter == "" {
		var sections []pickSection
		if nRecent > 0 {
			sections = append(sections, pickSection{"recent", 0})
		}
		if len(m.entries) > 0 {
			sections = append(sections, pickSection{"files", nRecent})
		}
		return all, sections
	}

	labels := make([]string, len(all))
	for i, e := range all {
		labels[i] = shortPath(m.cwd, e.Path)
	}
	var out []GlobEntry
	for _, idx := range filterIndices(m.filter, labels) {
		out = append(out, all[idx])
	}
	return out, nil
}

func (m *model) refilter() {
	rows, _ := m.visibleEntries()
	if m.cursor >= len(rows) {
		m.cursor = len(rows) - 1
	}
	if m.cursor < 0 {
		m.cursor = 0
	}
}

func (m *model) toggleSelect() {
	rows, _ := m.visibleEntries()
	if len(rows) == 0 {
		return
	}
	p := rows[m.cursor].Path
	if m.selected[p] {
		delete(m.selected, p)
	} else {
		m.selected[p] = true
	}
}

func (m *model) selectAllVisible() {
	rows, _ := m.visibleEntries()
	for _, e := range rows {
		m.selected[e.Path] = true
	}
}

// takeSelection resolves what a run should convert: an explicit multi-selection
// wins, otherwise the cursor row is used. Either way the selection is consumed
// so the next enter acts on the cursor again (ADR-0053).
func (m *model) takeSelection() (convert, audio []string) {
	rows, _ := m.visibleEntries()
	if len(m.selected) == 0 && len(rows) > 0 {
		m.selected[rows[m.cursor].Path] = true
	}
	// Resolve against every known entry, not just the filtered rows: a
	// selection made before a filter change must still dispatch.
	var files, kinds []string
	for _, e := range m.allEntries() {
		if m.selected[e.Path] {
			files = append(files, e.Path)
			kinds = append(kinds, kindOf(e.Kind))
		}
	}
	convert, audio = partitionSelected(files, kinds, m.selected)
	m.selected = map[string]bool{}
	return convert, audio
}

// kindOf maps discovery kinds to the two dispatch kinds.
func kindOf(kind string) string {
	if kind == "audio" {
		return "audio"
	}
	return "convert"
}

// --- rendering ---------------------------------------------------------------

func (m *model) renderPicker(width, height int) []string {
	rows, sections := m.visibleEntries()
	sectionAt := map[int]string{}
	for _, s := range sections {
		sectionAt[s.row] = s.title
	}

	var lines []string
	if m.filtering || m.filter != "" {
		cursor := ""
		if m.filtering {
			cursor = th.cursor.Render("▌")
		}
		lines = append(lines, th.accent.Render("@")+m.filter+cursor)
	}

	if len(rows) == 0 {
		if m.filter != "" {
			lines = append(lines, th.subtle.Render("  no matches"))
		} else if m.loadErr != nil {
			lines = append(lines, th.err.Render("  discovery failed: "+m.loadErr.Error()))
		} else if !m.engineReady {
			lines = append(lines, th.subtle.Render("  waiting for the engine…"))
		} else {
			lines = append(lines, th.subtle.Render("  no convertible files in this folder"))
		}
		return lines
	}

	// Keep the cursor inside the window.
	h := height - len(lines)
	if h < 1 {
		h = 1
	}
	if m.cursor < m.offset {
		m.offset = m.cursor
	}
	if m.cursor >= m.offset+h {
		m.offset = m.cursor - h + 1
	}
	if m.offset < 0 {
		m.offset = 0
	}

	end := m.offset + h
	if end > len(rows) {
		end = len(rows)
	}
	for i := m.offset; i < end; i++ {
		if title, ok := sectionAt[i]; ok {
			lines = append(lines, th.subtle.Render("  ── "+title))
			// A section header consumes a line; if it pushed the last row out,
			// keep the invariant and stop here.
			if len(lines) >= height {
				break
			}
		}
		e := rows[i]
		line := m.renderPickRow(e, i == m.cursor, width)
		lines = append(lines, line)
	}
	return lines
}

func (m *model) renderPickRow(e GlobEntry, cursor bool, width int) string {
	mark := "  "
	if m.selected[e.Path] {
		mark = th.check.Render("✓ ")
	}
	glyph := kindGlyph(e.Kind)
	name := shortPath(m.cwd, e.Path)
	meta := ""
	if e.Kind == "recent" {
		meta = th.subtle.Render("recent")
	} else {
		parts := []string{}
		if e.IsDir {
			parts = append(parts, "project")
		} else if e.Size > 0 {
			parts = append(parts, humanSize(e.Size))
		}
		if e.MTime > 0 {
			parts = append(parts, humanAge(e.MTime, m.now()))
		}
		meta = th.subtle.Render(strings.Join(parts, " · "))
	}

	prefix := "  "
	if cursor {
		prefix = th.cursor.Render("> ")
	}
	left := fmt.Sprintf("%s%s %s %s", prefix, mark, glyph, name)
	if meta == "" {
		return truncate(left, width)
	}
	pad := width - visibleWidth(left) - visibleWidth(meta) - 2
	if pad < 1 {
		return truncate(left, width)
	}
	return left + strings.Repeat(" ", pad) + meta
}

func kindGlyph(kind string) string {
	switch kind {
	case "audio":
		return "♫"
	case "latex", "latex-project":
		return "λ"
	case "recent":
		return "★"
	default:
		return "▤"
	}
}

func humanSize(n int64) string {
	switch {
	case n >= 1<<30:
		return fmt.Sprintf("%.1f GB", float64(n)/(1<<30))
	case n >= 1<<20:
		return fmt.Sprintf("%.1f MB", float64(n)/(1<<20))
	case n >= 1<<10:
		return fmt.Sprintf("%.0f kB", float64(n)/(1<<10))
	default:
		return fmt.Sprintf("%d B", n)
	}
}

func humanAge(mtime float64, now time.Time) string {
	d := now.Sub(time.Unix(int64(mtime), 0))
	switch {
	case d < time.Minute:
		return "now"
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd", int(d.Hours()/24))
	}
}
