# 0055. Paper-mode paragraphs and headings from learned typography

- Status: Accepted
- Amended by: [0070](0070-slide-paragraphs-and-headings.md)
- Date: 2026-10-13

## Context

Paper mode reconstructed headings with a handful of absolute rules in
`converter/pdf.py`: a line was promoted to `##` when it sat alone on a row,
was not a bullet, ended in anything but `:;,`, was at most forty **characters
including its `**` markers**, and was either bold or at least 1.5 pt larger
than the column's median size. Paragraphs were emitted one Markdown line per
visual PDF line, with block breaks decided by a single `gap <= h * 1.6` test.

Converting a real assignment brief exposed the whole class of failures:

- **Wrapped bold tails became headings.** A bold sentence that wrapped across
  two lines had its tail line promoted (`## additional preventive
  inspections**.`), producing nine false `##` headings in one ten-page
  document. The `.strip("*")` "cleanup" left orphan `**` markers behind.
- **A one-character cliff decided real headings.** `**4. Estimated business
  value**` was 31 marked characters and promoted; `**3. Effect on the
  maintenance decision**` was 41 and stayed text — even though both were 18 pt
  real section headings. A 40-character rule applied to marked-up text is
  arbitrary and cannot generalize across documents.
- **The title was rendered as `##`.** The centered title-block heuristics
  never fired on a flush-left title, so the document's only 24 pt line became
  a section heading and no `#` existed at all.
- **Paragraphs were wrong in both directions.** With `h = 12`, real paragraph
  breaks (`gap = 17`) passed the `h * 1.6 = 19.2` continuation test and were
  merged into one visually unbroken Markdown paragraph, while the body of a
  bullet list absorbed a following paragraph as a two-space-indented
  continuation because `_looks_like_lead` only recognized `**`-prefixed
  lines.
- **Bold-only and size-only documents had no model at all**: promotion was an
  OR of bold/size/uppercase, so bullet labels, table fragments and emphasized
  prose qualified whenever they happened to be short.

The failure mode is the same in each case: rules compared absolute numbers
against absolute numbers, and nothing modelled the document or the paragraph.

## Decision

Paper mode reconstructs paragraphs and headings from **statistics learned from
the document's own text layer** (`converter/paragraphs.py`).

1. **Typography profiler.** From the raw page lines (footers/running headers
   excluded) the converter learns: the character-weighted modal **body size**;
   the **line pitch** and the **paragraph-gap threshold**, by splitting
   same-column body-size baseline deltas into the largest significant gap
   between clusters (a plain mode is fooled when breaks outnumber wrapped
   lines, e.g. one-line paragraphs); the display **size tiers** above body
   (each needs at least two *paragraph-start* lines that are short and do not
   end in a full stop — a prose block styled above body, such as an abstract
   or a pull quote, continues its lines and is rejected); and the page-1
   **title size** — the maximum size above the first body-size line, with
   corner decorations and one-or-two-character letterheads rejected and the
   same-size run nearest the body winning over a letterhead above it — plus
   whether that size is reused later as a section level.

2. **Paragraph objects.** Lines are grouped into blocks per reading-order
   group. A line continues the block above when it is within the learned
   paragraph gap and either its formatting context continues (the previous
   line's trailing run matches this line's leading run) or the previous line
   does not end a sentence. A sentence-final previous line followed by a
   capitalized line always breaks, so tight paragraph styles stay separate.
   Bullets, numbered items (`1.`/`1)`), crowded rows and table regions always
   start new blocks; a non-bullet line only continues a list item when it is
   indented past the glyph.

3. **Evidence ladder.** Only a block *start* is a heading candidate. It must
   be alone on its row, flush with or centered in its column, and under a
   25-word safety rail. A candidate whose size is a learned tier promotes
   (`##`, then `###`, `####` for deeper tiers; the page-1 title is `#`). When
   no tier exists, a strict bold-only fallback applies: the whole block bold,
   a paragraph-gap start, capitalized, not wrapped in quotation marks, not
   ending in a period, at most twelve words. Ambiguity always leaves text as
   text.

4. **Marker-safe rendering.** Blocks render from span *runs*, not from
   pre-rendered strings. Runs merge across wrapped line breaks, so
   `**10 available` + `additional…**` becomes one balanced
   `**10 available additional preventive inspections**`; headings render as
   plain text with no markers at all.

5. **Reflow and cross-boundary paragraphs.** With `PDF_REFLOW` on (default)
   each paragraph emits one Markdown line and a block that continues in the
   next column or page is merged with its owner before rendering. A line
   broken at an existing hyphen (`data-`/`science`) rejoins without a space,
   and a word hyphenated at the column edge (`fail-`/`ure`) rejoins as the
   dictionary word when the merged form is in the bundled English vocabulary
   (ADR-0023). `PDF_REFLOW=off` keeps the legacy line-per-visual-line output
   (headings are still reconstructed). Slide mode (`PDF_MODE=slide`) is
   untouched, byte for byte.

## Consequences

- The document classes exercised by the fixtures (faux-bold tiers, bold-only
  uniform size, size-only hierarchies, numbered/`:`/`?` headings, two-column
  text, wrapped bold tails, paragraphs after lists, cross-column/page
  paragraphs) reconstruct correctly; the headings, not a character count,
  decide the outline.
- The structure pass and the format pass now see joined paragraphs and may see
  `###` headings; their prompts were amended to keep existing heading levels
  instead of retagging them. `_iter_sections` still chunks at `##`.
- Determinism is unchanged: the profile is computed from the text layer, and
  the same input yields byte-identical output on re-runs. Reflow can be
  disabled per conversion (`PDF_REFLOW=off`, lazy like `IMAGE_LINKS`).
- Pre-existing crash fixed: pages or columns with no text lines no longer call
  the removed `_mark_headings` with an empty list (`min()` on empty).
- A review sweep over 19 real PDFs (lecture decks, papers, a 500-page book;
  `scripts/pdf-corpus-diff.py`) found the changed output structural throughout
  except three documented, intentionally-unfixed cases: a title recovered from
  inside a detected table is duplicated, diagram-slide fragments keep the
  linearizer's row order, and Markdown escaping shifts with merged run
  boundaries. See the README's *Known limitations*; a run's outputs and report
  are kept under `artifacts/`.
- A heading exactly at body size with no bold, or a pull-quote box styled at a
  heading tier, can still be missed or over-promoted; the evidence ladder
  fails safe (text) where possible, and the optional structure pass remains
  the semantic backstop (ADR-0011).

## Alternatives considered

- **Keep the constraints and add more conditions** (paragraph-start check,
  balanced markers, longer cap). Extends the cliff instead of removing it; any
  fixed character count mis-serves some document.
- **Promote by bold alone.** Bold is used for emphasis at every level
  (including wrapped inline runs); without the size tier or the strict
  bold-only ladder this reintroduces the fixture's false headings.
- **Learn nothing; use fixed gap and size ratios.** The fixtures alone span
  pitch/break ratios from 16/28 to 17/29, and real corpora double-spaced or
  indented styles; the two-cluster split adapts, constants do not.
- **No reflow toggle.** Consistent with the other output preferences
  (`IMAGE_LINKS`, `PDF_COMPRESS`, `PDF_BORDERLESS_TABLES`) and keeps a
  deterministic escape hatch for consumers of the legacy line shape.
