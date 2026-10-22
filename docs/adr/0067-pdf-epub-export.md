# 0067. PDF → EPUB export beside the Markdown

- Status: Accepted
- Date: 2026-10-21
- Relates to: [0037](0037-pandoc-evaluated-deferred.md),
  [0038](0038-latex-pandoc-backend.md), [0047](0047-text-only-output-image-links-opt-in.md),
  [0048](0048-markdown-beside-source-archive.md), [0050](0050-inference-marked-markdown.md)

## Context

PDFs converted to Markdown are readable, but an e-reader wants a reflowable
book file. The converter already does the hard part: it extracts headings,
paragraphs, tables, figures and math from the PDF. What was missing was a
packaging step, not a second extraction backend.

`pandoc` is already a required binary for `.tex` input (ADR-0038), and its EPUB
writer embeds local images, converts `$…$`/`$$…$$` to MathML, builds a TOC and
writes a title page. A probe against the project's PDF Markdown confirmed all of
that, including pipe tables and MathML, before any code was written.

The project deliberately has one Markdown artifact per source (ADR-0048); the
EPUB must not break that contract or the inference marker (ADR-0050).

## Decision

1. **EPUB is an additive PDF artifact.** With `EPUB_OUTPUT=on` (the TUI's
   *epub export* row, or `ptm-engine --epub`), a PDF conversion writes
   `<stem>.epub` beside `<stem>.md`. The Markdown is never deleted, and
   non-PDF inputs are unaffected. The source archive/compression flow is
   unchanged.
2. **Packaging happens after the post-passes.** `converter.epub.build_epub`
   runs after the format/summary passes and after the `-inference-used` rename,
   so the EPUB carries the polished text and the final stem
   (`foo-inference-used.md` → `foo-inference-used.epub`).
3. **pandoc is the only new dependency, and it is optional per run.** A missing
   binary, a timeout or a failed invocation appends a warning and leaves the
   Markdown intact; the conversion itself still succeeds. `config.snapshot()`
   exposes `pandoc` so the TUI can show `on (pandoc missing)`.
4. **Figure images are forced on for the EPUB run.** The
   `config.force_epub_images()` context-local override (like the inference
   ledger) makes `IMAGE_LINKS` behave as on, so figures are extracted. Page
   renders keep the user's prior `IMAGE_LINKS` choice: an EPUB-only run (image
   links off) writes no `[Page N](…)` PNGs, while an explicit image-links run
   keeps them (the packaging copy strips the links either way). The user's
   `IMAGE_LINKS` preference is not mutated.
5. **Metadata and cover are best-effort.** Title/author come from the PDF
   metadata, falling back to the paper-mode `# ` heading and its italic author
   line (skipping the RAG `# Summary`/`## Abstract` block) and then the source
   stem. Language is `EPUB_LANG` when set, else the PDF catalog's `/Lang`, else
   `en`. Page 1 is the cover (`EPUB_COVER=off` disables it). The
   `dc:identifier` is a UUIDv5 of the Markdown stem, so it is stable across
   rebuilds and source moves.
6. **The temp Markdown is sanitized.** Slide-mode page-break `<div>`/`---`
   pairs, a title heading duplicated by the metadata title page, and
   non-image asset links (`[Page N](assets/…)`, recurring-image `[image](…)`)
   are removed from the packaging copy only. Everything else is untouched,
   including `![…]` figure images, math and tables.
7. **Naming/collision checks include the EPUB.** `organize._exists`,
   `claim_inference_path` and duplicate-if-exists see `<stem>.epub`; an existing
   inference-marked EPUB is never overwritten just because its Markdown was
   moved or deleted.

## Consequences

- **+** A PDF can become an e-reader book with no second extraction pipeline and
  no model calls: deterministic, fast, and consistent with the Markdown output.
- **+** The Markdown remains the primary artifact, so the existing layout,
  archive, marker and results contracts stay intact.
- **+** Missing pandoc degrades gracefully and is visible in the TUI.
- **−** EPUB quality is bounded by PDF extraction. Paper mode is the good path;
  slide mode becomes a chapter-per-slide text book; a scanned PDF without the
  vision pass stays poor; wide tables and page-anchored citations reflow badly.
- **−** The export forces figure extraction, so the Markdown also gains the
  figure links/`assets/` sidecar for that run (the documented side effect of the
  export).
- **−** `dcterms:modified`/`dc:date` in the EPUB container are build timestamps,
  so EPUB bytes are not fully reproducible even though the document content is;
  the deterministic identifier keeps diffs meaningful.
- **−** pandoc is now a de-facto requirement for the EPUB feature, in addition to
  `.tex` input. The binary stays absent-tolerant for PDF→Markdown.

## Alternatives considered

- **Calibre's `ebook-convert`** — a heavyweight second converter with its own
  PDF extraction; rejected because it bypasses the project's Markdown.
- **`ebooklib` + a Python Markdown→HTML stack** — adds Python dependencies and
  would reimplement EPUB packaging, TOC and MathML handling; worse fidelity for
  more code. Rejected.
- **PDF → EPUB as the only output** — would break the one-Markdown-per-source
  contract and the marker/archive behaviour. Rejected in favour of an additive
  artifact.
- **Embedding every page render in a slide-mode EPUB** — makes a huge file,
  duplicates the extracted text, and would require image-based chapters;
  deferred.
- **Custom CSS, footnote repair, EPUB validation** — useful polish, but not
  needed to ship the export; left for a follow-up.
