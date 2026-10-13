package main

// render.go — small ANSI-aware layout helpers. The whole view is composed from
// these so it can never be taller or wider than the terminal (the class of bug
// that made the v1 renderer scroll the screen, ADR-0053).

import (
	"strings"

	"github.com/charmbracelet/x/ansi"
)

const ellipsis = "…"

// baseName and tail are the two tiny path/line helpers shared across panes.
func baseName(path string) string {
	if i := strings.LastIndexAny(path, "/\\"); i >= 0 {
		return path[i+1:]
	}
	return path
}

// tail returns the last n lines of a slice (all of them when shorter).
func tail(lines []string, n int) []string {
	if len(lines) <= n {
		return lines
	}
	return lines[len(lines)-n:]
}

func visibleWidth(s string) int { return ansi.StringWidth(s) }

func truncate(s string, w int) string {
	if w <= 0 {
		return ""
	}
	if ansi.StringWidth(s) <= w {
		return s
	}
	if w == 1 {
		return ellipsis
	}
	return ansi.Truncate(s, w, ellipsis)
}

// fitLines crops or pads s to exactly h lines and truncates each to w cells.
func fitLines(s string, w, h int) string {
	if h <= 0 {
		return ""
	}
	lines := strings.Split(s, "\n")
	if len(lines) > h {
		lines = lines[:h]
	}
	for i, l := range lines {
		lines[i] = truncate(l, w)
	}
	for len(lines) < h {
		lines = append(lines, "")
	}
	return strings.Join(lines, "\n")
}

// padRight pads a rendered line to w cells (no-op when it is already wider).
func padRight(s string, w int) string {
	if pad := w - visibleWidth(s); pad > 0 {
		return s + strings.Repeat(" ", pad)
	}
	return s
}

// overlayCenter splices box into the middle of base (both already rendered),
// preserving the surrounding frame.
func overlayCenter(base, box string, w, h int) string {
	baseLines := strings.Split(fitLines(base, w, h), "\n")
	boxLines := strings.Split(box, "\n")
	boxW := 0
	for _, l := range boxLines {
		if x := ansi.StringWidth(l); x > boxW {
			boxW = x
		}
	}
	boxH := len(boxLines)
	if boxW > w {
		boxW = w
	}
	startY := (h - boxH) / 2
	startX := (w - boxW) / 2
	if startX < 0 {
		startX = 0
	}
	for i, bl := range boxLines {
		row := startY + i
		if row < 0 || row >= h {
			continue
		}
		bl = truncate(bl, boxW)
		prefix := ansi.Truncate(baseLines[row], startX, "")
		suffix := ansi.TruncateLeft(baseLines[row], startX+boxW, "")
		baseLines[row] = prefix + bl + suffix
	}
	return strings.Join(baseLines, "\n")
}

// windowBounds returns the [start,end) slice of n rows that keeps the cursor
// visible inside a viewport of the given height.
func windowBounds(n, cursor, height int) (int, int) {
	if n == 0 || height <= 0 {
		return 0, 0
	}
	if height > n {
		height = n
	}
	start := 0
	if cursor >= height {
		start = cursor - height + 1
	}
	if start > n-height {
		start = n - height
	}
	return start, start + height
}
