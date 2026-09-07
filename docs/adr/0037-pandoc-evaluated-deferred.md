# 0037. Pandoc evaluated; adoption deferred as documented future work

- Status: Accepted
- Date: 2026-09-07

## Context

The question came up whether [pandoc](https://pandoc.org/) (a Haskell CLI
that converts between markup formats, including a `pptx` reader) could improve
the project's presentation→Markdown conversion. Pandoc is not currently
installed, so adopting it would add a new external binary (`brew install
pandoc`), with the optional LibreOffice dependency already the precedent for
external binaries in the pipeline (`converter/render.py`).

The comparison was made against `converter/pptx.py` (python-pptx based) and
`converter/pdf.py` (PyMuPDF based).

## Decision

Evaluate pandoc, adopt nothing for now, and record the findings here as
future work. The python-pptx / PyMuPDF pipeline remains the sole conversion
backend; no runtime or build-time pandoc dependency is added.

## Findings

### Where pandoc is stronger than the current converters

- **Equations.** Pandoc converts PowerPoint OMML (`m:oMath`) equations to
  LaTeX math; `pptx.py` has no math handling and drops equation text.
- **Per-run hyperlinks.** Pandoc preserves hyperlinks inside text runs;
  python-pptx cannot read run-level hyperlinks (only shape-level click
  actions).
- **Merged table cells.** Pandoc emits grid tables; python-pptx duplicates
  merged-cell text into every spanned cell.
- **New output formats.** `pptx → revealjs/slidy/s5` HTML slideshows, a
  net-new export capability.
- **Text formats generally.** The same reader/writer machinery covers docx,
  xlsx, odt, epub, HTML, and LaTeX — useful if the project ever wants
  generic "text document → Markdown" conversion beyond pptx/pdf.
- **Smart typography** (curly quotes, dashes, ellipses) via its `smart`
  extension.

### Where the current converters are stronger than pandoc

- Content-hash image deduplication with the "inline once, then hyperlink"
  treatment for recurring images (`converter/base.py`) — pandoc's
  `--extract-media` deduplicates files by SHA1 but repeats `![]()` references.
- Per-slide numbered headings (`# Title — Slide 3`), placeholder filtering
  (slide number, date), footer-as-italic handling, and `> **Notes:**`
  speaker-note blockquotes — pandoc uses `::: notes` divs and no numbered
  slide headings.
- Chart rendering via LibreOffice plus opt-in AI transcription — pandoc
  skips chart shapes.
- Deterministic pure-Python pipeline with no external process (LibreOffice
  already optional).
- **PDF:** pandoc has no PDF reader at all, so it is irrelevant to
  `pdf.py` (column linearization, paper mode, per-page PNG ground truth).

### Options considered (kept open as future work)

- **A. Optional pandoc pptx backend.** `PPTX_BACKEND=pandoc` env toggle that
  shells out to pandoc and post-normalizes output (headings, notes, image
  dedup) to the pipeline's conventions. Best text fidelity for math-heavy
  decks; requires output normalization plus AI-pass/test updates.
- **B. Native gap-filling.** Implement OMML→LaTeX (subset), run-level
  hyperlinks, and merged-cell tables in Python; pandoc as reference only.
  No new dependency, but OMML conversion is a substantial lift.
- **C. Reveal.js export.** Additive `pptx → revealjs` HTML export via
  pandoc, leaving the Markdown pipeline untouched.
- **D. Pandoc as test oracle.** Compare pandoc's text output against the
  converters in `test_quality.py` to measure fidelity; no runtime dependency.

Reference invocations, for when this is revisited:

```bash
pandoc -f pptx -t gfm --wrap=none deck.pptx -o deck.md
pandoc -f pptx -t gfm --extract-media=media deck.pptx -o deck.md
pandoc -f pptx -t revealjs -s deck.pptx -o deck.html
pandoc -f pptx -t json deck.pptx -o deck.json   # AST for filters
```

## Consequences

- **+** The pandoc comparison is captured in the ADR index, so the next
  evaluation does not repeat it.
- **+** No new dependency, no determinism or packaging risk.
- **−** Known gaps remain in the interim: equations are dropped from pptx
  output, run-level hyperlinks are lost, and merged table cells duplicate
  content.
- **Re-open triggers:** math-heavy decks cause user-visible loss; a
  reveal.js export is requested; or the project adds non-presentation text
  formats (docx/xlsx) to the input set.
