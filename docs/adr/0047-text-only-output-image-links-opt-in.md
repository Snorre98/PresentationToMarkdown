# 0047. Text-only Markdown by default; image links behind an opt-in

- Status: Accepted
- Date: 2026-10-13

## Context

Conversion always emitted images as Markdown links: PPTX pictures and charts as
`![name](assets/<stem>/…)`, every PDF page as `[Page N](assets/…)` "visual
ground truth", embedded PDF images as `![image](…)`, and LaTeX figures plus
rendered display math the same way — with every asset extracted to an
`assets/<stem>/` sidecar (ADR-0038). Recurring images (logos/watermarks) were
inlined once and hyperlinked afterwards.

The tool feeds lecture notes into an Obsidian vault. For that workflow the links
are clutter: the asset folder accumulates files nothing references in the vault;
the source PDF/PPTX already holds every image; and where the vision passes
produced a description (verbatim transcription, diagram gist, chart
transcription, page transcription, diagram interpretation) the link adds nothing
on top. The request: **remove the image links, keep the AI descriptions**.

Constraints that shaped the decision:

- Conversion must stay deterministic and image-free in behavior when AI is off.
- LaTeX runs **no** vision passes (ADR-0038), so dropping its links deletes
  figures with no description to replace them; display math was only ever
  represented as a rendered PNG.
- The `format` and `structure` passes treat image/page links as structural
  anchors; they must keep working for legacy output and existing vault notes.
- `duplicate_if_exists` (ADR-0015) derives its per-stem asset folder from the
  output stem; that contract changes when no assets are written.

## Decision

**Default conversions emit no image links and write no image files.** A runtime
output preference, `IMAGE_LINKS` (lazy-read at conversion time, mirroring
`PDF_MODE`), restores the legacy behavior.

1. **PPTX** (`converter/pptx.py`). Pictures and charts are still read/rendered in
   memory and still classified and transcribed; `write_image` and the
   `![name](…)`/`[name](…)` emissions are skipped. The repeated-image pre-scan
   and repeat hyperlinks are skipped too — they only existed to deduplicate
   links.
2. **PDF** (`converter/pdf.py`). The page PNG is still rendered in memory (it is
   the page-vision/interpretation/structure input, ADR-0020) but is saved and
   linked only with `IMAGE_LINKS=on`; embedded images are still transcribed
   (full-page scans still skipped) and never linked by default. The
   `repeated_image_hashes` pre-scan runs only in legacy mode.
3. **LaTeX** (`converter/latex.py`). Figures are dropped and display math stays
   as pandoc's `$$…$$` text; pdflatex is no longer needed by default. With
   `IMAGE_LINKS=on` the old rewrite-into-`assets/` + render-to-PNG path runs.
4. **Descriptions win.** Every vision output — verbatim transcription,
   blockquoted diagram gist, chart transcription, page transcription, interpret
   relationships, structure reword — is emitted exactly as it was; it is the only
   trace the image leaves. An image without a description (vision off,
   decorative, unreadable, quality-rejected) contributes nothing.
5. **Toggle surfaces** follow the established runtime-toggle contract: env
   `IMAGE_LINKS=on|off` (default off), `ptm --image-links`, the desktop GUI
   checkbox (persisted setting `image_links`), the web UI Options checkbox via
   the engine's `/api/config`, and a TUI settings row.
   `config.image_links_enabled()` is the single reader; `config.snapshot()`
   records the value per run.

## Consequences

- Default output is text-only: one `.md` per source document, no `assets/`
  sidecar, no `![…]`/`[Page N](…)` lines — with AI descriptions kept where they
  exist.
- Legacy behavior remains available and byte-compatible via `IMAGE_LINKS=on`
  (assets, figure/page links, repeated-image hyperlinks, rendered display math).
- `format`/`structure` link handling is kept (inert when no links exist, needed
  in legacy mode and for pre-existing notes): no behavior change there.
- With the toggle off, `duplicate_if_exists` no longer writes a per-stem assets
  folder, and the PDF page PNGs are not left on disk to browse.
- Determinism is preserved: the routing signals (text-layer quality, full-page
  detection, readability) never depended on link emission.
- Trade-offs accepted for the vault workflow: decorative or unreadable images
  are invisible unless `IMAGE_LINKS=on`, and the per-page "visual ground truth"
  PNG must be re-derived from the source when needed. The source file remains the
  ground truth.

## Alternatives considered

- **Keep `[Page N]` page links** — rejected: they were the largest source of
  clutter (one per page) and the render is reproducible from the source.
- **Drop LaTeX figures but keep rendering display-math PNGs** — rejected after
  review: rendering images nothing references is wasted work; `$$…$$`
  passthrough is editable text.
- **Remove links unconditionally (no toggle)** — rejected after review: an
  escape hatch is wanted for browsing assets/ground truth and for A/B-checking a
  conversion, and `IMAGE_LINKS` costs little while mirroring `PDF_MODE`.
- **Put the toggle in `config.FEATURES`** — rejected: that registry drives the
  AI-server health checks and the "AI features" checkbox lists; this is a non-AI
  output preference, owned by `config` but outside `FEATURES`.
