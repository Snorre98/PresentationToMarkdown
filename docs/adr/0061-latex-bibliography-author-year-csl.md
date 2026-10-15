# 0061. LaTeX bibliography rendered through a bundled author-year CSL

- Status: Accepted (amends [0038](0038-latex-pandoc-backend.md))
- Date: 2026-10-15

## Context

ADR-0038 recorded as a consequence that "bibliography is re-rendered through
pandoc's citeproc, so its style may differ from the compiled PDF", and used
pandoc's built-in CSL (Chicago author-date). Converting the TDT4259 report
against its own `references.bib`, `main.bbl` and compiled PDF exposed four
fidelity gaps plus their root causes, all verified by experiment:

- **page ranges collapse**: `973--78`, `44:1--37` where the sources have
  `973–978`, `44:1–44:37` (the default CSL's `page-range-format="chicago-16"`;
  the Markdown writer then escapes an en dash as `--`, which Obsidian shows
  literally);
- **titles are title-cased**: `Datasets: Class-Imbalanced Datasets` vs the
  author's `Datasets: Class-imbalanced datasets` (the default CSL applies
  `text-case="title"`);
- **publisher locations are dropped** (Boca Raton / Melbourne / Cambridge —
  present in the `.bib` as `address`);
- **serial commas differ** from biblatex (`A, B, and C` vs `A, B and C`).

A second root cause sits in pandoc itself: its BibTeX reader **sentence-cases
`title`/`booktitle`/`series` values that are not brace-protected**, so even a
style that applies no case transform would print the author's capitalisation
lowercased.

## Decision

Pass the bundled `_AUTHOR_YEAR_CSL` — an author-year style written for this
pipeline — to pandoc with `--csl`, and hand pandoc **case-protected copies**
of the project's `.bib` files. The originals are never modified; the copies
are written into pandoc's temp directory with the values of `title`,
`booktitle`, `series` and `subtitle` wrapped in a brace group (content
unchanged), which is BibTeX's own case-protection mechanism.

Citations keep citeproc's disambiguation (year suffixes, `et al.`), so in-text
references render as before: `(IBM 2025a)`, `(Hernán and Robins 2020)`,
`(Manning et al. 2008)`. The bibliography renders close to biblatex's
`authoryear`:

```markdown
- Elkan, Charles (2001). 'The Foundations of Cost-Sensitive Learning'. In: *Proceedings of the Seventeenth International Joint Conference on Artificial Intelligence (IJCAI)*, pp. 973–978. url: <https://example.org/elkan.pdf>.
- Hernán, Miguel A. and James M. Robins (2020). *Causal Inference: What If*. Boca Raton: Chapman & Hall/CRC.
- IBM (2025a). Causal Inference. url: <…> (visited on 30th Sept. 2026).
- — (2025b). What is human-in-the-loop? <…>
```

Details: `page-range-format="expanded"`, no title-casing, `publisher-place`
before the publisher, `delimiter-precedes-last="never"`, `default-locale="en-GB"`
for single-quoted titles, `et-al-min="4"` in the bibliography (biblatex's
`maxbibnames`), `accessed` for visited dates, and a `—` substitute for a
repeated author list. A post-pandoc pass in the bibliography block turns the
writer's escaped `--`/`---` back into `–`/`—` (elsewhere in the document the
author's dashes are left alone).

`LATEX_CSL=<file.csl>` overrides the bundled style, mirroring
`LATEX_FIGURE_PREAMBLE` (ADR-0057) — a numeric-style project can supply its
own CSL. Read lazily, so an engine `--env` flip applies to the next conversion.

## Consequences

- **+** The four fidelity gaps are fixed, and the bibliography's overall shape
  (author (year), `In:`, `pp.`, publisher place, visited dates, em-dash
  repeats) reads like the compiled PDF's.
- **+** Case protection is a pre-pass on copies, so the project's `.bib` is
  untouched and the protection is idempotent (an already-braced value is left
  as it is).
- **+** Citations stay citeproc's: suffix letters (`2025a`/`2025b`, `2026a`/
  `2026b`) and name disambiguation remain correct by construction.
- **−** The bundled CSL covers the common entry types (book, article,
  inproceedings/chapter, online, plus a title/URL fallback); an exotic type
  renders through the fallback rather than a type-specific form.
- **−** URLs are written as Markdown autolinks (`<https://…>`) and DOIs keep
  citeproc's link form (`doi: [10.1145/2523813](https://doi.org/…)`); both
  differ cosmetically from biblatex's plain `url:`/`doi:` lines.
- **−** Bibliography typography is now a pipeline concern: changing the style
  means editing the CSL string, not passing a flag (or setting `LATEX_CSL`).

## Alternatives considered

- **Post-process citeproc's default output into shape.** Rejected: reversing
  title case is lossy (it has to re-read the `.bib` and match entries by URL),
  and it parses our own output instead of a style.
- **Render the bibliography in Python from the `.bib`.** Rejected: it would
  have to re-derive year suffixes that citeproc already assigned, and any
  drift between the two label sources would be invisible.
- **Vendor pandoc's `default.csl` and patch it.** Rejected: a 4 000-line file
  to keep current, and its non-title-cased variants still lack the publisher
  place and the serial-comma convention.
- **Copy the `.bbl` rendered by biber.** Rejected: requires a compile step and
  a TeX distribution, and the `.bbl` is a cache, not source.
