# 0018. Deterministic column-sliced OCR

- Status: Accepted
- Date: 2026-09-03

## Context

When a PDF page's text layer cannot be linearized — a multi-column paper, a
scanned page, or a scatter of labels — the converter hands the **whole rendered
page** to the vision model in one shot (`transcribe_complex_page`, the reader
role of ADR-0016). A generalist VLM reads a full page by raster order, so a
two-column layout comes back as interleaved fragments: the first line of column
one, then the first line of column two, and so on. The reading order is
scrambled, which defeats the point of OCR on precisely the pages that most need
it.

The text-layer path already knows the correct answer: `_detect_columns` computes
vertical column bands from a coverage profile and linearizes clean multi-column
*pages* deterministically. That same band geometry was being discarded the moment
a page fell back to OCR.

## Decision

OCR hard pages **per column band** and reassemble the chunks into one linear
Markdown stream, instead of OCRing the whole page at once.

### Mechanism (`converter/ocr_columns.py`)

1. **Detect bands** deterministically (see ADR-0019 for the two sources). Bands
   are `(x0, x1)` x-extents in points, sorted by min-x. A single column collapses
   to one full-width band `[(0, page_width)]`.
2. **Slice**: for ≥2 bands, render each band with a clip-rect
   `page.get_pixmap(matrix=Matrix(2,2), clip=Rect(x0, 0, x1, page_height))` — a
   clip-render, no post-crop. Each clip rect is padded a few points on both
   sides (clamped to page bounds, capped at half the gutter) so edge glyphs
   survive. The render scale is a module constant `OCR_SLICE_SCALE` (2x), kept
   distinct from the 2x full-page PNG that remains the visual ground truth.
3. **OCR** each band with a column-specific prompt (`_COLUMN_PROMPT` in
   `vision.py`): "transcribe this text column top-to-bottom", same lossless /
   verbatim / no-invention rules as the slide prompt, but with no slide-title
   instruction.
4. **Reassemble** the chunks left-to-right (band order is already sorted by
   min-x), blank-line separated, into one continuous document. No column markers.
5. **Full-width elements** that span the gutters are never sliced: a
   title/heading block in the top 28% of the page is OCR'd as its own full-width
   slice placed *before* the columns, and full-width `page.find_tables()` tables
   as slices placed *after*. Full-width figures and mid-page full-width headings
   are a documented limitation (see Consequences).
6. **Per-column gates**: `transcription_quality` runs on every chunk; for a
   column with a usable text layer, `verify_no_omissions` checks the chunk
   against that column's deterministic text, rejecting a chunk that omits more
   than `_MAX_OMISSION_FRACTION` (0.5) of its content words.
7. **Graceful fallback**: a chunk that fails a gate falls back to whole-page OCR
   (`transcribe_complex_page`), and that to the raw-text `<details>` block. A
   single-column page delegates straight to whole-page OCR (the prior behaviour).
   Conversion never fails.
8. **Caching**: each band PNG is memoized by content digest
   (`transcribe_column_cached`), so repeated bands/images are transcribed once
   per run.

### Wiring (`converter/pdf.py`)

`_detect_columns` delegates band computation to `ocr_columns.detect_text_bands`,
and `_page_to_md` replaces the whole-page `transcribe_complex_page` path with
`transcribe_columns`. Pages with no text layer at all (pure scans) are routed
into the same path when vision is on, and `_page_images` skips its own
full-page-image transcription to avoid double OCR. `interpret_diagram` is kept
for genuinely diagrammatic single-column pages. When vision is off, the OCR path
is a no-op and output is byte-identical to before.

## Consequences

- Multi-column and scanned pages OCR in correct reading order; the full-page PNG
  is still saved as visual ground truth alongside.
- **Determinism boundary**: bands, order, padding and concatenation are
  deterministic; only the OCR text is model output. The repo's determinism
  guarantee (ADR-0002) continues to apply to the non-AI path.
- This is a **reader-side** technique only (`VISION_*`, ADR-0016). It does not
  implement reader/writer role separation or model-residency orchestration
  (ADR-0016/0017); it composes with them: a clean OCR A/B now pins `VISION_MODEL`
  to the reader while this path changes only how the reader is called.
- **Known limitations**: full-width figures and full-width headings below the
  top 28% band are not detected and may be sliced (their x0 lands in one band);
  a scan with a *sparse but non-empty garbage* text layer (1–5 lines) is not
  routed to OCR unless it also trips the existing complex-page detection.
- Per-column OCR issues more, smaller model calls than whole-page OCR (latency
  rises roughly linearly with column count; memory per call drops). The
  `vision_events` table records one `transcribe` row per slice with
  `omitted_words` populated, giving a per-column fidelity signal for A/B runs.

## Alternatives considered

- **Keep whole-page OCR** — the status quo; scrambles multi-column order. Rejected.
- **A dedicated layout model as a third server (e.g. PP-DocLayout)** — predicts
  reading order more accurately, but adds a third resident model and server to
  orchestrate on a memory-constrained machine (ADR-0017); the deterministic
  coverage/ink band approach needs no new model. Rejected for now; revisitable.
- **OCR-first-everywhere (drop the text layer)** — simplest mentally, but throws
  away the deterministic text layer and guarantees hallucination risk on every
  page; the text layer is both cheaper and more faithful when it exists.
  Rejected.
