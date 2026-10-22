package main

// keys.go — one binding table for the footer hints and the `?` overlay.

import "charm.land/bubbles/v2/key"

type keyMap struct {
	Move     key.Binding
	Up       key.Binding
	Down     key.Binding
	Select   key.Binding
	Run      key.Binding
	Filter   key.Binding
	Tabs     key.Binding
	Cancel   key.Binding
	Open     key.Binding
	Epub     key.Binding
	Settings key.Binding
	AI       key.Binding
	Help     key.Binding
	Quit     key.Binding
}

func defaultKeys() keyMap {
	return keyMap{
		Move:     key.NewBinding(key.WithKeys("up", "k", "down", "j"), key.WithHelp("↑/↓", "move")),
		Up:       key.NewBinding(key.WithKeys("up", "k"), key.WithHelp("↑/k", "move")),
		Down:     key.NewBinding(key.WithKeys("down", "j"), key.WithHelp("↓/j", "move")),
		Select:   key.NewBinding(key.WithKeys("space", " "), key.WithHelp("space", "select")),
		Run:      key.NewBinding(key.WithKeys("enter"), key.WithHelp("enter", "convert")),
		Filter:   key.NewBinding(key.WithKeys("@", "/"), key.WithHelp("@", "filter")),
		Tabs:     key.NewBinding(key.WithKeys("tab"), key.WithHelp("tab/1-3", "panes")),
		Cancel:   key.NewBinding(key.WithKeys("x"), key.WithHelp("x", "cancel job")),
		Open:     key.NewBinding(key.WithKeys("o", "f"), key.WithHelp("o/f", "open · reveal")),
		Epub:     key.NewBinding(key.WithKeys("E"), key.WithHelp("E", "open EPUB")),
		Settings: key.NewBinding(key.WithKeys("s"), key.WithHelp("s", "settings")),
		AI:       key.NewBinding(key.WithKeys("a"), key.WithHelp("a", "AI")),
		Help:     key.NewBinding(key.WithKeys("?"), key.WithHelp("?", "help")),
		Quit:     key.NewBinding(key.WithKeys("q", "ctrl+c"), key.WithHelp("q", "quit")),
	}
}

var keys = defaultKeys()

func (k keyMap) ShortHelp() []key.Binding {
	return []key.Binding{k.Move, k.Select, k.Run, k.Settings, k.AI, k.Help, k.Quit}
}

func (k keyMap) FullHelp() [][]key.Binding {
	return [][]key.Binding{
		{k.Move, k.Select, k.Run, k.Open},
		{k.Epub, k.Filter, k.Tabs, k.Cancel},
		{k.Settings, k.AI, k.Help, k.Quit},
	}
}
