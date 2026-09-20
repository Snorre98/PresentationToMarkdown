package main

import "sort"

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
