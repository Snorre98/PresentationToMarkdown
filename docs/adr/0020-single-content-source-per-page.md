# 0020. Single content source per page (no duplicate text/vision emission)

- Status: Accepted
- Date: 2026-09-03

## Context

A scanned PDF that also carries a *garbage* text layer (common for OCR'd papers
like "What Makes Things Fun to Learn?") produced **duplicate content**: every
page was emitted twice.

Two independent paths each rendered the page, with nothing making them exclusive:

1. `_page_images` → `maybe_transcribe_image` (classifier + reader transcription)
   classified the embedded **full-page scan image** as `text` and transcribed it.
2. `_page_to_md` then **also** emitted the deterministic text layer
   (`_detect_columns` / `_emit_group`) for the same page.

The result was the clean vision text (sometimes wrapped in a ```markdown fence,
sometimes not — the model is nondeterministic) followed by the garbled text
layer. Two existing guards should have prevented this and did not:

- `skip_fullpage_ocr` only fires when the page has *no* text layer
  (`scan_like = not content`); a garbage-but-non-empty text layer never triggers
  it.
- `_is_fullpage` uses a 2pt tolerance and misses off-by-margin scans (e.g. a
  scan clipped 21pt short), so full-page images are not reliably recognised.

ADR-0018 flagged this as a known limitation: "a scan with a *sparse but
non-empty garbage* text layer … is not routed to OCR". Beyond the duplicate
output, the redundant per-page reader calls are the most expensive work in the
AI pipeline, and the mixed fenced+garbled output caused the structure pass
(ADR-0011) to reject every page.

## Decision

Resolve a page's content source **exactly once, before any vision work**, and
never emit both. Three changes:

1. **Full-page images are the page, not embedded figures.** Detect full-page
   images by *area ratio* (≈≥95% width and ≈≥90% height) rather than the
   2pt-tolerance `_is_fullpage`, and in `_page_images` skip
   `maybe_transcribe_image` for them — a full-page image is handled by the
   page-level path. Genuine sub-page figures/diagrams are still transcribed.
   The `skip_fullpage_ocr` parameter becomes redundant and is removed.

2. **Text-layer quality is the routing signal.** Add a shared
   `text_layer_quality(texts) -> "usable" | "sparse" | "empty"` (word count,
   unique-word ratio, mean line length — the same signal ADR-0011 already uses
   for its text/image regime). In `_page_to_md`:

   ```python
   page_via_vision = config.is_enabled("vision") and (
       quality != "usable"                              # garbage/empty → vision wins
       or (not columns and _page_is_complex(content))   # complex diagram (existing)
   )
   ```

   - `page_via_vision` → page-level vision (`transcribe_columns` for multi-column
     scans, `transcribe_complex_page` otherwise; `interpret_diagram` unchanged),
     and the text layer is **not** emitted.
   - otherwise → the deterministic text layer (`_detect_columns` / `_emit_group`).

   Garbage/empty text now routes to vision even when columns are detected,
   replacing the garbled text instead of duplicating it. When vision is off the
   text-layer path is byte-identical to today.

3. **Strip code fences from every vision output.** Add `strip_code_fences`
   (`vision.py`) and apply it in `transcribe_page`, `transcribe_column`,
   `transcribe_image`, and `transcribe_image_meta`, so a model's ```markdown
   wrapper never leaks into the document. The same helper is available to the
   structure/format/summary prompts later.

## Consequences

- One content stream per page; at most one reader transcription per page,
  eliminating the redundant `maybe_transcribe_image` calls on scans.
- No ```markdown fences in output; duplicate content is gone for
  scan-with-garbage-text PDFs.
- The structure pass (ADR-0011) receives clean input and stops rejecting pages
  that previously failed only because of the mixed fenced+garbled duplicate.
- `maybe_transcribe_image` now handles only genuine sub-page images; a
  near-full-page figure is treated as "the page" and covered by the complex-page
  path instead.
- Determinism is unchanged (ADR-0002): the routing signal and full-page
  detection are deterministic; only the reader text remains model output.
- Resolves the ADR-0018 known limitation on sparse-garbage text layers.

## Alternatives considered

- **Skip full-page image transcription only** (fix `_is_fullpage`/area ratio,
  nothing else) — removes the duplicate, but leaves the garbled text layer as
  the *only* output for scans, degrading quality and leaving the structure pass
  fed with garbage. Rejected as insufficient.
- **Dedupe at write time** (emit both, drop the lower-quality stream afterward) —
  still performs all the redundant reader work and adds a fragile content
  similarity heuristic. Rejected.
- **OCR-first everywhere, drop the text layer** — simplest, but discards a
  cheaper, faithful, deterministic source and guarantees hallucination risk on
  every page. Rejected (same reasoning as ADR-0018).
