# 0060. TikZ figure text survives when no image is written

- Status: Accepted (amends [0057](0057-latex-tikz-figures.md), [0047](0047-text-only-output-image-links-opt-in.md))
- Date: 2026-10-15

## Context

ADR-0057 renders a TikZ/pgfplots figure to a PNG with `IMAGE_LINKS=on`. With
image links **off** (the default, ADR-0047) the figure marker is dropped and
the numbered caption stands alone — but a TikZ drawing's content lives in its
source as `\node {…}` text, not in the caption. On the TDT4259 report all four
figures are diagrams and every one lost its content:

- Figure 1's 2×2 cost matrix (`will fail` / `will not fail`, `c_FN` / `c_FP`
  cells) and the threshold rule `p* = c_FP/c_FN`;
- Figure 2's timeline labels and the leakage list;
- Figure 3's three chain stages, arrow verifications and six break-point
  entries;
- Figure 4's six flow blocks and the feedback loop.

A caption-only line is not a substitute for the drawing; and unlike PPTX/PDF,
LaTeX runs no vision pass that could describe it (ADR-0038).

## Decision

Extract the drawing's **text content deterministically** and emit it as a
single note line under the caption whenever the figure has no image:

```markdown
**Figure 3: The evidence chain: each arrow is verified by a different evidence stage and has known break points.**

*Figure content (from the TikZ source):* 1. Better ranking; (avg. precision, calibration) · 2. Better selection; … · verified by offline out-of-time eval vs. baseline · Breaks: drift, proxy misaligned, …
```

The extractor (`_tikz_labels`) walks the figure body for:

- every `\node` command (`\node [options] (name) at (x,y) {text};`) and every
  `node[…]` attachment on a path (`(a) -- (b) node[midway] {label};`), so box
  text *and* edge labels are kept;
- pgfplots axis labels (`xlabel`, `ylabel`, `zlabel`) and `\legend` entries.

Node text is cleaned for inline Markdown: inline `$…$` math is preserved
verbatim, `\\` line breaks become `; `, spacing commands are dropped, and the
labels are joined with ` · ` in document order. The note is emitted as a plain
paragraph after the caption, so it survives the summary chunking and is
searchable.

The note appears only when the figure's image was **not** written: image links
off, a failed render, or no pdflatex. With `IMAGE_LINKS=on` and a successful
render the drawing itself is the content and no note is added. `LATEX_FIGURES=off`
keeps the pre-ADR-0057 pandoc output untouched.

## Consequences

- **+** The default path (text-only Markdown) keeps what the figure says:
  matrix cells, lists, stage names, the rules and parameters drawn as text.
- **+** Deterministic and offline: no model, no rendering, no pdflatex needed.
- **−** The note flattens the drawing's two-dimensional layout into a linear
  sequence; spatial structure (a 2×2 matrix) is not reconstructed.
- **−** Only textual content is extracted. A pure `\draw`-shaped figure (no
  nodes) contributes nothing, and a pgfplots data series is quantitative
  content the note cannot represent.
- **−** TikZ macros the extractor does not know degrade to their arguments (the
  same best-effort rule pandoc applies to unknown macros).

## Alternatives considered

- **Append the labels to the caption line.** Rejected: captions can be long
  already, and the text is figure content, not part of the caption.
- **Emit the TikZ source as a code block.** Rejected in ADR-0057 and still
  true: source is unreadable in a study note.
- **Fall back to a vision pass.** Rejected: LaTeX conversion is deliberately
  AI-free (ADR-0038), and node text is exact where a description is not.
- **Only keep the figure's caption.** Rejected: that is the status quo the
  report showed to be lossy.
