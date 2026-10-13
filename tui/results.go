package main

// results.go — the results tab: per-file outcomes of the session's finished
// jobs plus the durable run history from /api/runs (ADR-0053). Output paths,
// warnings and the archived source all come from the job record, so a finished
// conversion can be inspected, opened and revealed without touching the list.

import (
	"fmt"
	"strings"
)

type resultRow struct {
	jobID    int
	status   string // ok | error | warn | cancelled | history
	name     string
	source   string
	mdPath   string
	movedTo  string
	detail   string
	warnings []string
	logTail  []string
}

func (m *model) rebuildResults() {
	var rows []resultRow
	for _, j := range m.jobs.finished() {
		rows = append(rows, resultRow{
			status: "header",
			detail: fmt.Sprintf("%s %s · %s · %s", statusGlyph(j.status), j.kind,
				plural(j.total, "file"), formatDuration(j.elapsed(m.now()))),
		})
		if len(j.results) == 0 {
			row := resultRow{jobID: j.id, status: j.status, name: j.name(), warnings: j.logTail}
			switch {
			case j.err != "":
				row.detail = j.err
			case j.status == "cancelled":
				row.detail = "cancelled"
			}
			rows = append(rows, row)
			continue
		}
		for _, r := range j.results {
			status := "ok"
			if r.Error != "" {
				status = "error"
			} else if len(r.Warnings) > 0 {
				status = "warn"
			}
			row := resultRow{
				jobID:    j.id,
				status:   status,
				name:     baseName(r.Source),
				source:   r.Source,
				mdPath:   r.MDPath,
				movedTo:  r.MovedTo,
				warnings: r.Warnings,
				logTail:  j.logTail,
			}
			if r.Error != "" {
				row.detail = r.Error
			} else if r.DurationMS > 0 {
				row.detail = fmt.Sprintf("%.1fs", float64(r.DurationMS)/1000)
			}
			if len(r.InferencePasses) > 0 {
				row.detail = strings.TrimSpace(row.detail + " · inference: " + strings.Join(r.InferencePasses, "+"))
			}
			rows = append(rows, row)
		}
	}
	if len(m.histRuns) > 0 {
		rows = append(rows, resultRow{status: "header", detail: "history (ptm.sqlite)"})
		for _, r := range m.histRuns {
			status := "history"
			if r.Status != "ok" && r.Status != "" {
				status = "error"
			}
			rows = append(rows, resultRow{
				status: status,
				name:   baseName(r.Source),
				source: r.Source,
				detail: fmt.Sprintf("%s · %s", humanDurationMS(r.DurationMS), humanStamp(r.EndedAt)),
			})
		}
	}
	m.rstRows = rows
	if m.resCursor >= len(rows) {
		m.resCursor = len(rows) - 1
	}
	if m.resCursor < 0 {
		m.resCursor = 0
	}
}

func (m *model) renderResults(width, height int) []string {
	if len(m.rstRows) == 0 {
		if !m.engineReady {
			return []string{th.subtle.Render("  waiting for the engine…")}
		}
		return []string{th.subtle.Render("  no conversions yet — pick files, then enter")}
	}
	start, end := windowBounds(len(m.rstRows), m.resCursor, height)
	lines := []string{}
	for i := start; i < end; i++ {
		row := m.rstRows[i]
		if row.status == "header" {
			lines = append(lines, th.subtle.Render("  ── "+row.detail))
			continue
		}
		lines = append(lines, m.renderResultRow(row, i == m.resCursor, width))
		if m.resDetail[i] {
			for _, w := range row.warnings {
				lines = append(lines, th.warn.Render("      ⚠ "+w))
			}
			if row.movedTo != "" {
				lines = append(lines, th.subtle.Render("      archived: "+shortPath(m.cwd, row.movedTo)))
			}
			if row.jobID != 0 && len(row.logTail) > 0 {
				for _, l := range tail(row.logTail, 4) {
					lines = append(lines, th.subtle.Render("      · "+l))
				}
			}
		}
	}
	return lines
}

func (m *model) renderResultRow(row resultRow, cursor bool, width int) string {
	glyph, style := "✓", th.ok
	switch row.status {
	case "error":
		glyph, style = "✗", th.err
	case "warn":
		glyph, style = "!", th.warn
	case "cancelled":
		glyph, style = "✗", th.warn
	case "history":
		glyph, style = "·", th.subtle
	}
	prefix := "  "
	if cursor {
		prefix = th.cursor.Render("> ")
	}
	left := fmt.Sprintf("%s%s %s", prefix, style.Render(glyph), row.name)
	if row.mdPath != "" {
		left += "  →  " + th.ok.Render(shortPath(m.cwd, row.mdPath))
	} else if row.detail == "" {
		left += "  " + th.subtle.Render("(no output)")
	}
	if row.detail != "" {
		left += "  " + th.subtle.Render(row.detail)
	}
	return truncate(left, width)
}

func (m *model) selectedResult() *resultRow {
	if m.resCursor < 0 || m.resCursor >= len(m.rstRows) {
		return nil
	}
	row := m.rstRows[m.resCursor]
	if row.status == "header" {
		return nil
	}
	return &row
}

func statusGlyph(status string) string {
	switch status {
	case "error":
		return "✗"
	case "cancelled":
		return "✗"
	case "running":
		return "●"
	default:
		return "✓"
	}
}

func plural(n int, word string) string {
	if n == 1 {
		return fmt.Sprintf("1 %s", word)
	}
	return fmt.Sprintf("%d %ss", n, word)
}

func humanDurationMS(ms int) string {
	if ms <= 0 {
		return "-"
	}
	if ms < 1000 {
		return fmt.Sprintf("%dms", ms)
	}
	return fmt.Sprintf("%.1fs", float64(ms)/1000)
}

func humanStamp(iso string) string {
	if len(iso) >= 19 {
		return strings.ReplaceAll(iso[5:19], "T", " ")
	}
	return iso
}
