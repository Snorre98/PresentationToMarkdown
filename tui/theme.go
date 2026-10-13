package main

// theme.go — one palette with dark and light variants. Lip Gloss v2 is pure
// (no terminal queries of its own), so the variant is chosen from the
// background color Bubble Tea reports (tea.RequestBackgroundColor).

import (
	"image/color"

	"charm.land/lipgloss/v2"
)

type theme struct {
	dark bool

	header lipgloss.Style
	title  lipgloss.Style
	bold   lipgloss.Style
	subtle lipgloss.Style
	accent lipgloss.Style
	ok     lipgloss.Style
	warn   lipgloss.Style
	err    lipgloss.Style
	cursor lipgloss.Style
	check  lipgloss.Style
	tabOn  lipgloss.Style
	tabOff lipgloss.Style
	box    lipgloss.Style
	bar    color.Color
	bar2   color.Color
}

func newTheme(dark bool) theme {
	pick := func(d, l string) color.Color {
		if dark {
			return lipgloss.Color(d)
		}
		return lipgloss.Color(l)
	}
	t := theme{
		dark:   dark,
		header: lipgloss.NewStyle().Bold(true).Foreground(pick("12", "4")),
		title:  lipgloss.NewStyle().Bold(true),
		bold:   lipgloss.NewStyle().Bold(true),
		subtle: lipgloss.NewStyle().Foreground(pick("240", "245")),
		accent: lipgloss.NewStyle().Foreground(pick("13", "5")),
		ok:     lipgloss.NewStyle().Foreground(pick("10", "2")),
		warn:   lipgloss.NewStyle().Foreground(pick("11", "3")),
		err:    lipgloss.NewStyle().Foreground(pick("9", "1")),
		cursor: lipgloss.NewStyle().Foreground(pick("10", "2")).Bold(true),
		check:  lipgloss.NewStyle().Foreground(pick("10", "2")),
		tabOn:  lipgloss.NewStyle().Bold(true).Foreground(pick("12", "4")).Underline(true),
		tabOff: lipgloss.NewStyle().Foreground(pick("240", "245")),
		box: lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(pick("12", "4")).
			Padding(0, 2),
		bar:  pick("14", "6"),
		bar2: pick("12", "4"),
	}
	return t
}

// th is the active theme; a BackgroundColorMsg swaps it once at startup.
var th = newTheme(true)
