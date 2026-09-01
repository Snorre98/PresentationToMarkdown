# 0011. Paper-mode document-structure LLM pass

- Status: Accepted
- Date: 2026-09-01

## Context

Paper mode (ADR: the `PDF_MODE=paper` path in `converter/pdf.py`) reconstructs a
whitepaper's structure from the text layer with deterministic geometry
heuristics: column gutters via a coverage profile (`_detect_columns`), section
headings via bold/centered/short/flush heuristics (`_mark_headings`), a title
block via a centered top-run (`_title_block`), and running headers via
repeat-frequency keys (`_collect_top_keys`). These work on the sample
whitepaper but are fragile to other layouts and scans:

- 3+ columns, sidebars and footnotes defeat the gutter/heading heuristics.
- OCR garbage has no usable text at all — the page falls back to a
  `<details>` "Raw extracted text" block, or an opt-in vision transcription.
- There is **no confidence signal**: when the heuristics fail they silently
  emit interleaved text (e.g. the references column interleaved into the
  conclusion column) with no way to know the output is wrong.

The agreed architecture is a layered hybrid: (1) a deterministic parse, always
on, that keeps exact text + coordinates + bold/italic; (2) an optional LLM
"structure" pass; (3) a future image-transcription fallback gated by a
confidence signal. This ADR is layer 2, and it folds layer 3's core in.

## Decision

Add `converter/structure.py`, an opt-in post-pass over **paper-mode** PDF
output, invoked from inside `PDFConverter.convert` after the page loop and
before the file is written (hence before the `format`/`summary` passes in
`convert_file`). It is disabled by default and, when disabled, the converter's
output is byte-for-byte identical to today.

### Per-page confidence gate and routing

For each page, a cheap deterministic `_text_coverage` heuristic over the raw
extracted lines (word count, unique-word ratio, mean line length) plus a check
for the raw-text `<details>` fallback decides which of three regimes the page
takes:

| Deterministic page output | Coverage | Regime |
| --- | --- | --- |
| prose/columns (no `<details>`) | usable | **text regime** — check-and-amend, verbatim-gated |
| `<details>` "Raw extracted text" fallback | any | **image regime** — the pass reads the page image and rewords from it |
| interpret/vision output present (no `<details>`) | sparse | **skip** — already AI-handled, no double spend |

This coverage gate is the confidence signal: a page whose text layer is
usable keeps the strong verbatim guarantee; a page whose text layer is
unusable is rescued by reading the rendered image (which the converter already
renders for every page as the visual ground-truth PNG). The model default is a
vision-language model, so it can read images with no new infrastructure.

### Text regime (verbatim check-and-amend)

The model receives the page's current Markdown plus a numbered list of the
body lines with `[font-size bold x0]` layout metadata (the per-line text +
coordinates/size/bold the deterministic pass extracted). It returns amended
Markdown and may only:

- rejoin wrapped lines and reorder lines to fix multi-column linearization
  interleaving;
- fix the page-1 `# Title` + `*Authors*` block;
- blockquote the abstract with `> ` lines;
- add `##` headings (additive-only: existing `##` lines and `# Page N`
  headings are reproduced byte-exact, never demoted);
- wrap footnote lines in a blockquote;
- insert `## References` (and other structural block headings) where a block
  starts.

It must **not** reword prose, invent text, or "fix" OCR-garbled words. The
page is accepted only if all of these hold:

1. **Anchors intact** — every structural line (`##` headings, pipe tables,
   `<details>`, `<div>`, fenced code, the `[Page N](...)` link, and `# Page N`
   headings on pages 2+) appears in the reply byte-exact. Page 1's `# Title`
   and `*Authors*` lines are the amendment targets and are excluded.
2. **No omissions** — every content word in the deterministic page text is
   still present (`verify_no_omissions`, reused from `converter/vision.py`).
3. **No invented prose** — words added on non-heading lines reject the page.
   Added words are allowed only on heading lines, and only when the word is a
   structural block marker (`references`, `abstract`, `bibliography`,
   `appendix`, acknowledgements, `notes`, …) or is itself grounded in the
   document's own text.
4. **Chunking invariant** — the reply has exactly one `# ` heading, so
   `format._iter_slides` and `summary` keep their per-page chunking.

### Image regime (reword from the page image)

The model receives the rendered page PNG (plus the deterministic page Markdown
so the page heading/link contract is visible) and produces the page's Markdown
from the image: title/authors, abstract, `##` headings, body, footnotes,
`## References`. Because there is no usable text layer there is nothing to
preserve verbatim; instead the page is accepted only if:

1. the page image is readable (`image_readable`, blur/low-res gate);
2. the reply passes `transcription_quality` (repetition/placeholder/runaway
   guards);
3. the `[Page N](...)` link line is preserved byte-exact;
4. there is exactly one `# ` heading (pages 2+ may replace `# Page N` with the
   real title read from the image).

### Failure semantics

Every model call and gate failure is local to its page: the page keeps its
deterministic Markdown and a `[WARN]` is appended. A failure of the whole pass
returns `None` so the caller writes the deterministic output. The pass never
blocks or fails a conversion, and it logs each decision to `logstore`
(`stage="structure"`), like the other AI passes.

### Configuration and CLI

Follows ADR-0002. Read at import time:

- `STRUCTURE_ENABLED` — master switch. Default off.
- `STRUCTURE_BASE_URL` — defaults to `FORMAT_BASE_URL` (then `VISION_BASE_URL`).
- `STRUCTURE_MODEL` — defaults to `FORMAT_MODEL` (then `VISION_MODEL`, a VLM).
- `STRUCTURE_API_KEY` — defaults to `FORMAT_API_KEY` (then `VISION_API_KEY`).

A `--structure` flag is added to `cli_common.AI_FLAGS` mapping to
`STRUCTURE_ENABLED=1`. It is **not** part of `--all`: `--all` enables the five
slide passes, and structure is a paper-only pass (the same reasoning that keeps
audio transcription out of `--all`, ADR-0009). The GUI exposes no AI
checkboxes (consistent with `--format`/`--summary`), so the pass is reachable
from the GUI only via the `ptm-start` launcher or the environment.

## Consequences

- Paper-mode output with the pass on is a superset of the deterministic
  structure: real page-1 title/authors blocks, blockquoted abstracts, additive
  `##` headings, footnote blocks and a `## References` section.
- The deterministic heuristics are untouched and remain the always-on,
  fully-testable base layer; the pass amends their output.
- Pages with no usable text layer (scans, garbage OCR) are rescued from the
  raw-text `<details>` fallback by image reword, gated by readability +
  quality checks instead of the verbatim word gate.
- `# `-page boundaries are preserved, so `--format`'s slide split and
  `--summary`'s page chunking keep working unchanged.
- Cost: one chat call per page (text pages, no image; image pages, with
  image), only when enabled. Per-page failure locality keeps a bad page from
  discarding good pages.
- The confidence gate is a fixed heuristic (word count / TTR / line length),
  not a calibrated model — calibration is deferred.

## Alternatives considered

- **Pure role/line-range annotation protocol** (model emits `role: line N`
  only; a deterministic assembler applies `#`/`*`/`>`/`##`) — the strongest
  "cannot hallucinate new words" guarantee, but it cannot rescue pages with no
  usable text layer and cannot clean up interleaved columns beyond reordering.
  Rejected in favour of the check-and-amend rewrite, which the author of this
  feature prefers and which the verbatim gates still bound.
- **Whole-document LLM call** instead of per-page — simpler prompt but exceeds
  local-model context on long papers and loses failure locality. Rejected.
- **Deterministically route unusable pages to the existing vision
  transcription path** (reusing `transcribe_complex_page`) — least new code,
  but the structure model then never sees the image, so it cannot produce a
  titled, structured page. Rejected in favour of the fused image regime.
- **Demote heuristic `##` headings the model does not confirm** — true
  replacement of the fragile heuristic, but a model miss loses a real heading.
  Deferred; v1 is additive-only.
- **Confidence calibration / per-page confidence scores exposed to the user**
  — needed only once layer 3 lands; the fixed coverage heuristic suffices for
  now.
