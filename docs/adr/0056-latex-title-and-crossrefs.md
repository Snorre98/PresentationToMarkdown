# 0056. LaTeX title page and pandoc leftovers normalized

- Status: Accepted (amended by [0058](0058-latex-title-page-front-block.md) and
  [0059](0059-latex-compiled-float-numbering.md))
- Date: 2026-10-15

## Context

The LaTeX converter (ADR-0038) hands a flattened project to pandoc and prepends
a synthetic `# Title` + `*Authors*` block. On a real report template (the NTNU
`NTNU_Rapport_template` used by two TDT4259 deliverables) the output was
technically complete and practically hard to read:

- **Title.** The articles set their title on a **title page** (`title.tex`,
  `\import`-ed), not through `\title`, so the fallback printed the project
  folder name (`# NTNU_Rapport_template`) instead of
  `Assignment 1 - Problem D`. The title page was then stripped wholesale,
  losing nothing else of value (logo, course line, members table, word count).
- **Attributes.** Pandoc emits `{#sec:problem}` on every heading and
  `{#tab:business-kpis}` on every table caption line. Obsidian shows both
  literally.
- **Cross-references.** `\autoref{sec:data}` became `[3](#sec:data)`: pandoc
  resolves the number but leaves a link to an anchor that no longer exists once
  the attribute is gone — a dead anchor that reads like a citation marker.
  Pandoc cannot number an equation at all (`\autoref{eq:x}` prints the label).
- **Captions.** Figures carried no number, and inline math inside a caption was
  emitted as a MathML blob, because pandoc renders a caption it cannot attach
  to an image as raw HTML (HTML cannot contain Markdown math).
- Citeproc also leaves case-preserving spans (`[scikit-learn
  developers]{.nocase}`) in prose.

## Decision

Normalize the source *before* pandoc runs, from one deterministic scanner, and
clean the Markdown after it.

### Title block

Fallback chain: `\title{}` → the title page's biggest font tier → the project
folder name. The tier scan accepts the command and environment forms of
`\huge`/`\Huge`/`\LARGE`/`\Large`/`\large`; the highest tier wins, `\textsc`
lines lose ties (they carry the course and department name in this template),
and the fallback below that is the title page's first bold line. From
`{\huge \bfseries Assignment 1 - Problem D}\\[0.4cm]` the title is
`Assignment 1 - Problem D` (the balanced braced group is taken first, so the
line-break spacing never leaks in).

`\author{}` remains the only author source; the members table is not treated as
one. The rest of the title page (logo, course line, word count, term) is
template chrome and stays dropped. A project that sets `\title`/`\author` gets
exactly the previous title block.

### Numbering

A single linear pass over the masked source (comments and
`verbatim`/`lstlisting`/`minted`/`comment` bodies blanked out, offsets
preserved) numbers what the compiled PDF numbers:

- sections level by level (`\section` → `3`, `\subsection` → `2.1`), starting at
  the first *used* level so an article-class document prints `3`, not `0.3`;
- floats at their `\caption` (an uncaptioned float consumes no number, as
  `\refstepcounter` does not run without one); a `\label` inside a float takes
  that float's number even when it precedes the caption;
- numbered display-math environments (`equation`, `align`, `gather`, …) in
  order.

The index is the authority for both the reference text and the caption numbers,
so the two always agree. Pandoc's own counters are not used: they cannot number
an equation, and a replaced figure environment (ADR-0057) takes its `\label`
out of pandoc's sight.

### Cross-references and attributes

`\autoref{fig:x}` / `\cref{fig:x}` print `Figure 1` (word from the label
prefix: `sec`→Section, `fig`→Figure, `tab`→Table, `eq`→Equation, plus
chapter/appendix/algorithm/listing), `\ref{fig:x}` prints the bare number the
PDF shows, `\eqref{eq:x}` prints `(1)`. A label whose prefix has no word or
whose number is unknown is left to pandoc, which emits the raw anchor form —
the documented fallback. A post-pandoc safety net rewrites any reference link
pandoc marked as such (never an author-written link).

Pandoc attributes are stripped: `](…){}` after links, `[text]{.nocase}` spans,
and a line-end `{#id}` (heading and caption lines).

### Captions

Every numbered caption is rendered as a bold line:

```markdown
![Figure 2](assets/A1/A1_02_b6899296.png)

**Figure 2: The decision unit: features fixed at the evening cutoff, label read
from the following $H$ days, censoring by scheduled service, leakage sources
marked.**
```

Table captions become `**Table 2: Business KPIs**` (pandoc's `: <caption>`
line, numbered in the source, bolded after pandoc). Caption math therefore
stays `$…$` — it never passes through the HTML/MathML path — and the caption is
visible in Obsidian's rendering instead of living in an image's alt text. The
image alt is the short `Figure N`; the markdown is a reference list, not the
rendered page, so a `\ref`-style number without the word stays that way.

## Consequences

- **+** Sample A1/A2 convert with `# Assignment 1 - Problem D` /
  `# Assignment 2 - Problem D`, `(Section 3)`-style references, bold numbered
  captions, no `{#…}`, no MathML and no dead anchors.
- **+** Numbering is deterministic and self-consistent; it reuses pandoc's
  structure (headings, pipe tables) untouched.
- **−** The `.md` numbers its own floats. The stripped title page consumed
  counter values in the compiled PDF (the members table is Table 1 there), so a
  later table can be one lower in the Markdown than in the PDF; the captions and
  references inside the file agree with each other.
- **−** Known edges, all left in their previous form: `\appendix` letter
  numbering, a `\label` outside its float, multi-line `align` numbering and
  `\tag`, `\cref` plural/range lists, and non-`\autoref` commands pandoc does
  not understand (`\pageref`/`\nameref` are dropped by pandoc itself).

## Alternatives considered

- **Post-process pandoc's numbers** (`[3](#sec:data)` → `Section 3`). Rejected:
  pandoc cannot number equations (`[\[eq:x\]](#eq:x)`), its table counter starts
  after the stripped title page, and once ADR-0057 removes a figure environment
  its label disappears, so pandoc prints the label instead of a number. A
  second numbering source would drift from the captions.
- **Keep the `{#…}` attributes** so the anchors resolve. Rejected: Obsidian
  anchors are heading-text based (`#problem--decision-framing`), not
  pandoc-attribute based, so the attributes were never links — they were noise
  plus dead anchors.
- **Number captions but keep them in the image alt.** Rejected: alt text is
  invisible in the rendered note, and it cannot carry Markdown math.
- **Keep the title page's members table / word count** behind a preference.
  Rejected: nothing in the samples wants them, and every extra knob is a
  behaviour to document and test.
