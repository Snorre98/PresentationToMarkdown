# 0019. Image ink-projection column detection

- Status: Accepted
- Date: 2026-09-03

## Context

ADR-0018 OCRs a page per column band, so it needs the bands before it can slice.
The existing detector (`detect_text_bands`, extracted from `_detect_columns`)
builds a **coverage profile** from the page's text layer: for each x, the summed
height of every line whose x-extent contains x, with low-coverage corridors
treated as gutters. That works when a usable text layer exists.

But the pages that most need column OCR are often exactly the ones without a
text layer: **pure scans** (a raster of the page with no extractable text). On a
scan the coverage profile is empty or garbage, so a different, equally
deterministic source is required.

## Decision

Add a second, deterministic band detector — **image ink-projection** — and use it
whenever the text layer is absent or too sparse to trust.

### Algorithm (`converter/ocr_columns.detect_ink_bands`)

1. Render the full page **once** at low resolution (`Matrix(0.5, 0.5)`), in
   grayscale.
2. For each pixel column x, count the "ink" — pixels darker than a whiteness
   threshold (`< 250`). This is a vertical projection of the page's ink.
3. A pixel column whose ink count is below `_INK_GUTTER_FRAC` (2%) of the
   maximum column count is a **gutter**; contiguous low-ink runs are the gutters
   between text columns.
4. The bands between gutters are the columns. Bands narrower than
   `_INK_MIN_BAND_WIDTH` (15% of the page width) are dropped, and fewer than two
   surviving bands collapses to a single full-width band.

`detect_bands` prefers the text-layer bands, falls back to ink-projection only
when the text layer has fewer than `MIN_TEXT_LINES` (6) lines, and otherwise
returns one full-width band. Everything is deterministic for a given page and
scale.

## Consequences

- Pure-scan multi-column pages now slice into columns and OCR in correct order.
- Ink-projection is one extra low-resolution render per hard page — cheap and
  already amortized against the existing 2x full-page ground-truth render.
- The gutter threshold is a heuristic tuned for typical papers; a layout with a
  very narrow side column (< 15% width) or heavy full-width figures may mis-detect.
  These constants are module-level and documented as tunable.
- Left-to-right is the only ordering produced; right-to-left scripts (RTL) are
  explicitly out of scope.

## Alternatives considered

- **Text-layer coverage profile only** — the existing detector; fails on scans,
  the very case this ADR exists to serve. Rejected as insufficient alone (kept
  as the preferred source when text exists).
- **Connected-component / block analysis (PyMuPDF drawings)** — more faithful to
  visual layout but substantially more code and more sensitive to antialiasing
  and thin rules; the one-dimensional projection is deterministic, cheap, and
  adequate. Rejected as disproportionate.
- **A layout model (PP-DocLayout) for every page** — accurate but adds a third
  resident model (see ADR-0018 alternatives) and non-determinism to a step that
  can be solved deterministically. Rejected.
