# 0070. Slide-mode paragraphs and headings from learned typography

- Status: Accepted
- Date: 2026-10-25
- Relates to: [0012](0012-runtime-ai-features.md),
  [0020](0020-single-content-source-per-page.md),
  [0052](0052-paper-continuous-document.md),
  [0054](0054-borderless-pdf-tables.md),
  [0055](0055-pdf-paragraphs-and-headings.md),
  [0067](0067-pdf-epub-export.md)

## Context

ADR-0055 introduced the learned paragraph/heading model for paper mode and
explicitly left slide mode "untouched, byte for byte". ADR-0052 kept slide
mode's per-`#`-page chunking. The consequence is two very different PDF
converters behind one `PDF_MODE` switch:

- **Slide mode** emits one Markdown line per visual PDF line, has no `##`
  subheadings inside a slide, decides paragraph breaks with a single
  `gap <= h * 1.6` test, leaves bold/italic markers split across a wrapped line,
  does not join hyphenated words, and keeps repeated running headers.
- **Paper mode** learns body size, pitch, paragraph gap and display tiers from
  the document, rejoins wrapped lines, merges formatting runs across line
  breaks, reconstructs nested lists, and promotes headings through an evidence
  ladder.

Real lecture decks need the paper model's body handling as much as papers do.
Their text boxes wrap mid-sentence, bold phrases split across line breaks,
nested bullets are common, PowerPoint embeds private-use bullet glyphs
(`\uf07d`, `\uf0b7`), and code/command examples must keep their line breaks.
Converting those decks with the line-per-line path produces fragmented
paragraphs, unbalanced marker pairs and bullet-less lists.

Paper mode itself is not a drop-in replacement. It is one continuous document:
page 1 carries the only `#` title, later pages merge into the paper's `##`
sections, and paragraphs continue across page boundaries. Applied to a lecture
deck it merges slides together, turns slide titles into `######` headings and
loses the per-slide outline (the repo's corpus sweep, `artifacts/`, records
this failure mode). Slide mode therefore needs a **page-scoped** version of the
same model, not paper mode.

## Decision

Slide mode adopts the learned paragraph/heading model, scoped to one PDF page.

1. **Shared profile.** Slide mode builds the same typography profile as paper
   mode (body size, pitch, paragraph gap) from the deck's text layer. The
   profile is computed once per conversion, not per page.

2. **Page-scoped emission.** Each reading-order group (a column, or the whole
   single-column page) runs through `paragraphs.segment_group` →
   `classify_block` → `render_block`, exactly like paper mode, but with
   **no `_PaperFlow`**. The last paragraph of a column or page is never held
   and merged with the next group: slides are independent units, so a sentence
   that continues on the next PDF page stays split. Page boundaries and the
   `<div page-break>` + `---` scaffold are unchanged.

3. **Slide heading policy.** Slide decks use many display sizes for design
   (big numbers, pull quotes, diagram labels); unfiltered paper tier learning
   promoted nearly all of them in the corpus. A size above body is a slide
   heading tier only when it:
   - is not the deck's dominant slide-title size;
   - appears on at least `max(2, ceil(10% of pages))` pages;
   - is alone on its row for at least 90% of its lines;
   - is followed by smaller text for at least 50% of its lines.

   Surviving tiers map to `##`, `###`, `####`. Heading promotion is disabled
   on `_page_is_complex` pages (diagram-heavy slides), and a block ending in a
   period is not promoted (a contents entry such as "Introduction and
   Methodology 1." is a list item, not a heading). The strict bold-only
   fallback from ADR-0055 still promotes genuine whole-line bold paragraph
   starts.

4. **Title tightening.** The per-page title contract is unchanged — one
   top-level `# {title} — Page N` (or `# Page N`), title lines removed from the
   body — but detection no longer swallows body text:
   - candidates are preferred in the top 45% of the page;
   - the title is a contiguous run at the largest display size (at most four
     lines);
   - only lines at >= 90% of the title size, directly below, may extend it
     (the old 0.7x absorption turned the first bullet into part of the title);
   - a leading bullet glyph and a leading page/order number are stripped.

5. **Bullets and lists.** Slide mode opts into the common Symbol/Wingdings
   private-use glyphs (paper mode keeps the classic set so its output stays
   unchanged). A wrapped bullet line at the bullet's own x is a continuation of
   that item (PowerPoint wraps to the glyph x, not the text indent), so a
   wrapped bold bullet no longer becomes a false paragraph/heading.

6. **Code.** A line whose visible spans are all monospace is a `code` line.
   Contiguous code lines form a `code` block that is never reflowed, never
   promoted to a heading, and renders as a fenced ` ```text ` block. Unknown
   monospace families fall back to normal paragraph handling.

7. **Shared output preferences.**
   - `PDF_REFLOW` now applies to both PDF modes. `PDF_REFLOW=off` keeps the
     line-per-visual-line body shape (headings and code are still
     reconstructed); it is not a byte-for-byte rollback to the old slide
     output.
   - `PDF_MODE=slide` keeps `chunking="slides"`, so the format/summary passes
     still chunk at top-level `#`.
   - Running headers (repeated small top-band lines) are stripped in slide
     mode too; footers and slide numbers already were. Bottom-band 1–3 digit
     page numbers are filtered by position, and a digit-only footer no longer
     adds an empty key to the filter set (which used to drop every digit-only
     body line on the page, such as a year `1996`).

8. **Structure pass.** The optional document-structure pass now covers slide
   PDFs; that contract is [ADR-0071](0071-structure-pass-for-slide-pdfs.md).

## Consequences

- Slide-mode Markdown changes substantially: wrapped lines rejoin, lists nest,
  bold/italic runs merge, subheadings appear, running headers disappear and
  code is fenced. The old golden test is replaced by the new page-scoped
  contract.
- A review sweep over 36 PDFs in slide mode (0 conversion errors) found one
  real content-loss bug — a digit-only year swallowed by the empty
  footer-key — which the bottom-band page-number filter fixes. The
  `CONTENT CHANGED` markers in `artifacts/slide-model/` are dominated by
  intentionally stripped running headers, removed private-use bullet glyphs,
  removed title/table duplication and hyphenation rejoin; paper mode's sweep is
  byte-identical.
- Paragraphs never continue across a page or column boundary. This is
  intentional: a slide's text box ends with the slide, even when a sentence
  visually continues on the next page.
- Heading promotion is conservative. A real subheading at body size with no
  bold can still be missed; a display-sized label on a simple (non-complex)
  page can still be promoted. The optional structure pass is the semantic
  backstop.
- Code blocks depend on monospace font names in the PDF. Proportional-font code
  still reflows as prose.
- Paper mode's deterministic output and the paper structure pass are
  unchanged.
- Determinism is unchanged without AI passes: the profile, tier filter,
  title selection and code/bullet rules are deterministic, and the same input
  yields byte-identical output.
- `PDF_REFLOW=off` remains the escape hatch for line-per-line consumers, but
  bullets, headings and code keep the new model's shape.

## Alternatives considered

- **Leave slide mode on the legacy path.** Status quo; the fragmented body,
  split markers and bullet-less lists remain. Rejected because slide decks are
  the default PDF input and the paper model's body handling is strictly better
  where it applies.
- **Run slide decks through paper mode.** Loses page boundaries, merges slide
  bodies, produces one title for the whole deck and over-promotes display
  sizes. Rejected; this is what the corpus sweep demonstrated.
- **Adopt paper mode's tiers unfiltered.** In the corpus, every above-body
  display size became a heading (`######` labels, big numbers, diagram text).
  Rejected in favour of the repeated-style filter plus the complex-page gate.
- **Let paragraphs continue across pages.** Matches paper behavior but merges
  independent slides into one running text. Rejected for slide semantics.
- **Promote headings only through the bold-only ladder.** Removes the
  size-tier false positives but misses size-only subheadings; rejected as the
  default, though it is the natural fallback if corpus review finds the tier
  filter too eager.
- **Keep code as raw lines (no fence).** Preserves the old visual shape but
  Markdown collapses consecutive lines into one paragraph and interprets code
  characters. Rejected in favour of a fenced `text` block.
