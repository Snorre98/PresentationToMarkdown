# 0057. LaTeX figures: TikZ/pgfplots rendered to PNG

- Status: Accepted (amended by [0060](0060-latex-tikz-text-without-image.md))
- Date: 2026-10-15

## Context

The LaTeX converter (ADR-0038) drops figures by default (ADR-0047: no image
links, AI descriptions stand in) and extracts referenced images with
`IMAGE_LINKS=on`. Neither path helps the most common LaTeX figure in the sample
deliverables: a **TikZ drawing** (four flowcharts in assignment 1) or a
**pgfplots chart** (assignment 2's crossover figure). Their content exists only
as LaTeX/TikZ source, so pandoc produced a captionless `<figure id="fig:x">`
stub per drawing, with the caption kept as a MathML blob — no image, no text.

LaTeX also runs **no AI passes** except the RAG summary (ADR-0038), so unlike
PPTX/PDF there is no vision pass that could describe the diagram instead. The
push and the pull both point at rendering the figure from its own source:
deterministic, no model, and the drawing is already a vector program.

## Decision

Add a figure pipeline that renders TikZ/pgfplots bodies through
`standalone` + pdflatex and gives every figure a numbered caption.

### Pre-pandoc: marker plus caption

Each `figure`/`figure*` environment becomes a marker image and a numbered
caption, so pandoc never sees raw TikZ:

```latex
\includegraphics{ptmfig-1}

\textbf{Figure 1: The $c_{FN} \gg c_{FP}$ flow.}
```

The caption goes through pandoc as ordinary text, which keeps `$…$` math,
emphasis, citations and `\autoref` text intact. Three kinds of figure:

- **render** — the body contains a `tikzpicture` or a pgfplots axis; rendered
  from source (below).
- **file** — the body references exactly one project image; that image is used
  (a PDF is rasterized with PyMuPDF, so a `\includegraphics{diagram.pdf}`
  becomes a usable PNG).
- figures with several images (subfigures) or none are left to pandoc as
  before, with their caption numbered in place.

### Rendering

`standalone` (`[preview,border=4pt]`) in a temp directory, pdflatex with
`-interaction=nonstopmode -halt-on-error`, `SOURCE_DATE_EPOCH=0` for a
reproducible PDF, then a 2× PyMuPDF pixmap — the same recipe as display-math
rendering (`_render_math_png`). `-shell-escape` is never passed, so a project's
`\write18` cannot execute. The project directory is prepended to `TEXINPUTS`,
so a figure's own `\includegraphics{Images/logo.png}` resolves (the render runs
in a temp dir).

The preamble is tikz + pgfplots (`compat=1.18`) + xcolor + amsmath/amssymb/bm +
textcomp + graphicx and a broad `\usetikzlibrary` set, followed by the
project's **own style declarations** — `\usetikzlibrary`, `\tikzset`,
`\tikzstyle`, `\pgfplotsset`, `\colorlet`, `\definecolor` collected verbatim
from its `.sty`/`.tex` files. Templates define their shared styles that way
(`packages.sty`), and those files are `\usepackage`'d, not flattened, so the
render would otherwise miss them. `LATEX_FIGURE_PREAMBLE=<file.tex>` appends
arbitrary extra preamble last, for projects that need more than the collector
knows.

### Output

- **`IMAGE_LINKS=on`**: the rendered PNG (or the referenced image) is written
  through `write_image` into `assets/<stem>/` — content-hash deduplicated and
  sharing the counter with extracted images and rendered equations — and
  referenced as `![Figure 2](assets/<stem>/…)`.
- **`IMAGE_LINKS=off`** (default, ADR-0047): the image is not written; the
  numbered caption line stands alone and each figure adds a
  `Figure 2 (fig:x): image omitted (IMAGE_LINKS=off)` warning. Rendering is
  skipped entirely, so the default path neither needs nor runs pdflatex.
- **Degrade**: pdflatex missing, a compile failure, a missing style or an
  unresolvable image warns and keeps the caption; the conversion never fails.
  `LATEX_FIGURES=off` disables the pipeline and restores the pre-ADR-0057
  pandoc output (`<figure>` stubs).

Rendering happens lazily inside `_replace_figure_markers`, after pandoc, and
only for cases that reach it: a document with no figures or with links off
costs nothing.

## Consequences

- **+** Assignment 1's four flowcharts and assignment 2's 4-line chart convert
  to real, visually non-empty PNGs referenced in place, with their captions as
  searchable Markdown text (math included).
- **+** Deterministic: same input → same PNG bytes → same asset filename
  (verified by converting twice and diffing). No new Python dependencies; TikZ
  figures simply extend pdflatex's existing role.
- **−** pdflatex (TeX Live) is now needed for TikZ figures with
  `IMAGE_LINKS=on`, not just for display math. A machine without it keeps
  captions and warns.
- **−** A Markdown image is a static raster: the TikZ source stays in the
  project (archived under `source/`, ADR-0048), the drawing is not editable
  from the note.
- **−** Rendering the *whole* figure environment means a figure body that
  depends on project macros outside the collected style commands degrades to
  caption-only; that is the documented escape hatch's purpose
  (`LATEX_FIGURE_PREAMBLE`).

## Alternatives considered

- **Leave the stub, or keep the tikz source as a code block.** Rejected: the
  drawing is the content; a stub is empty and a code block is unreadable in a
  study note.
- **Compile the whole project to PDF and rasterize the figure pages.**
  Rejected: needs a full, successful document compile (bibliography passes,
  shell-escape for minted, project-class assumptions), while the figure alone
  usually compiles standalone.
- **Give the vision pass the rendered figure** (transcribe it as a diagram).
  Rejected for now: LaTeX conversion is deliberately AI-free (ADR-0038), and
  the deterministic render is strictly more faithful than a description.
- **`pandoc --extract-media`** — pandoc has no TikZ renderer; it would extract
  nothing for these figures.
