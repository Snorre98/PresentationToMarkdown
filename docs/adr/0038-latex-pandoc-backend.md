# 0038. LaTeX project → Markdown converter via pandoc

- Status: Accepted
- Date: 2026-09-08

## Context

The project converts presentations and documents to Markdown, one `.md` per
source document, with images extracted to an `assets/<name>/` sidecar folder
and deduplicated by content hash. The input set is currently `.pptx` and
`.pdf`. A request came to add **LaTeX projects** (directories containing
`.tex` source, a `.bib`, and image assets) as a first-class input.

This is the re-open trigger ADR-0037 documented: pandoc (a Haskell CLI whose
reader/writer machinery covers LaTeX) was evaluated and deferred, with the note
that adding non-presentation text formats would justify revisiting it. The
alternatives are a native pure-Python LaTeX parser (large lift, bounded
dialect) and compiling to PDF then reusing `pdf.py` (lossy: sections/emphasis
re-guessed by heuristics, equations and citations lost, a failed compile
breaks conversion). pandoc's LaTeX reader is the standard, broadly robust way
to turn source-structured LaTeX into Markdown with sections, emphasis, lists,
tables, and math preserved.

## Decision

Add `converter/latex.py` — a `LatexConverter` — that converts a **LaTeX
project directory** to one `.md` (plus an `assets/<stem>/` sidecar) by
shelling out to pandoc, wrapped by a deterministic pre-processor and
post-normalizer. pandoc becomes a **required** external binary for `.tex`
input only; the pptx/pdf pipeline is unchanged.

### Input model: a project is a directory

A directory is a LaTeX project when it contains `.tex` files. The entry file
is the `.tex` containing `\documentclass` (fallback `main.tex`, then the sole
`.tex`). Build artifacts (`.aux`, `.bbl`, `.bcf`, `.log`, `.pdf`, `.toc`, …)
are never inputs. The output stem is the directory name, so `IT3212/` →
`IT3212.md`. This extends `ConverterRegistry` with a project layer (a
converter declares `project_extensions` and a `project_entry` detector), and
`convert_file` routes directories through it.

### Pre-processor (deterministic, format-general)

pandoc does not follow `\import`/`\subimport`, so before pandoc runs:

- **Flatten** `\input` / `\include` / `\import` / `\subimport` by resolving
  relative paths into inline content (cycle guard, max depth).
- **Title/authors** via a fallback chain: `\title`/`\author`/`\date`
  metadata → a `titlepage` environment's text → the directory name.
- **Strip** `titlepage`, `\tableofcontents`, `\listoffigures`,
  `\listoftables`, `\maketitle` (pandoc emits none of these usefully).

Macro expansion is delegated to pandoc, which reads `\newcommand` /
`\renewcommand` in the entry file and, for unknown commands, drops the command
and keeps its argument text. No template-specific macro table is introduced.

### pandoc invocation

```
pandoc -f latex -t gfm+tex_math_dollars --wrap=none \
  --shift-heading-level-by=1 --extract-media=<tmp> \
  [--citeproc --bibliography=<bib>] entry.tex -o <tmp>.md
```

- `--shift-heading-level-by=1` demotes `\section`→`##`, leaving room for the
  synthetic `# Title` heading (so `format._iter_slides` / `summary` keep their
  top-level-`#` chunking contract).
- `--citeproc --bibliography=<bib>` resolves `\cite` when the project
  references a `.bib` (`\bibliography{…}` / `\addbibresource{…}`).
- Run from the project directory so relative `\includegraphics` paths resolve.

### Post-normalizer

- Prepend `# <title>` + `*<authors>*` (matching paper-mode's title block).
- Rewrite pandoc-extracted images into `assets/<stem>/` via `write_image`
  (content-hash dedup and the `_NN_<hash>.<ext>` naming); recurring images get
  the "inline once, then hyperlink" treatment from `base.py`.
- **Display math** (`\begin{equation}` / `\[…\]`) is rendered to a PNG via
  `standalone` + `pdflatex` + PyMuPDF (content-trimmed, cached by expression)
  and inlined as `![…](assets/<stem>/…)`. **Inline math** (`$…$`) is emitted
  as `$…$` passthrough — Obsidian renders it, and rendering every inline span
  to an image would break prose and table cells.
- `latex_available()` mirrors `soffice_available()`; if `pdflatex` is absent
  display math degrades to `$$…$$` passthrough with a `[WARN]`.

### AI gating

LaTeX conversion runs **no AI passes except the RAG summary**. A new
`Converter.ai_passes: frozenset[str]` capability (default
`{"format", "summary"}`) is consulted by `convert_file`; `LatexConverter`
declares `{"summary"}` only, so the vision/classify/interpret/format/structure
passes never run on LaTeX output regardless of runtime toggles.

## Consequences

- **+** Source-structured fidelity: `\section`/`\textbf`/`\emph`/lists/tables
  come from the source, not visual heuristics, and display math becomes a real
  rendered image rather than dropped text.
- **+** Deterministic pure-Python pipeline is preserved *except* the pandoc
  and pdflatex subprocess calls, which are deterministic for a fixed input.
- **−** Two new external binaries: pandoc (required for `.tex`) and pdflatex
  (required only for display-math rendering; graceful passthrough otherwise).
- **−** pandoc's LaTeX reader is a best-effort parser: heavy custom macro
  frameworks and TikZ flowcharts degrade (macro dropped, argument kept);
  unknown constructs emit a `[WARN]` rather than failing the conversion.
- **−** Bibliography is re-rendered through pandoc's citeproc, so its style
  (e.g. numeric vs author-year) may differ from the compiled PDF.

## Alternatives considered

- **Native pure-Python LaTeX parser** — deterministic, no new binary, best
  alignment with the python-pptx/PyMuPDF ethos; but a full LaTeX reader is a
  large lift and would target a bounded dialect. Rejected in favour of pandoc's
  broad, maintained reader, with the deterministic pre/post-processor layered
  on top.
- **Compile to PDF, reuse `pdf.py`** — least new code, but lossy (structure
  re-guessed, equations/citations lost) and fragile (compile must succeed).
  Rejected: the user wants source-faithful conversion, not a PDF detour.
