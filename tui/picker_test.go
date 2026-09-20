package main

import "testing"

func TestMatchScore(t *testing.T) {
	cases := []struct {
		pattern, s string
		want       bool
	}{
		{"deck", "week2/deck.pptx", true},
		{"wk", "week2/deck.pptx", true},
		{"wk2", "week2/deck.pptx", true},
		{"XYZ", "week2/deck.pptx", false},
		{"", "anything", true},
	}
	for _, c := range cases {
		got := matchScore(c.pattern, c.s) >= 0
		if got != c.want {
			t.Errorf("matchScore(%q, %q) matched=%v want=%v", c.pattern, c.s, got, c.want)
		}
	}
}

func TestMatchScoreRanksSubstringAboveSubsequence(t *testing.T) {
	// "deck" is a substring of "deck.pptx"; "dck" is only a subsequence.
	if matchScore("deck", "deck.pptx") <= matchScore("dck", "deck.pptx") {
		t.Fatal("substring should outrank subsequence")
	}
}

func TestFilterIndicesBestFirst(t *testing.T) {
	items := []string{"alpha/deck.pptx", "beta/notes.pdf", "deck.pdf"}
	idx := filterIndices("deck", items)
	if len(idx) != 2 {
		t.Fatalf("idx = %v", idx)
	}
	if idx[0] != 2 { // "deck.pdf" has the substring earliest
		t.Fatalf("first should be deck.pdf, got idx=%v", idx)
	}
}

func TestFilterIndicesCaseInsensitive(t *testing.T) {
	items := []string{"Week2/Deck.PPTX"}
	if idx := filterIndices("deck", items); len(idx) != 1 {
		t.Fatalf("idx = %v", idx)
	}
}
