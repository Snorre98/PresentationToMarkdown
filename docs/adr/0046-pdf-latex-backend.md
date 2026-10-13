# 0046. PDF LaTeX backend (PDF → LaTeX → Markdown)

- Status: Deprecated
- Date: 2026-10-13

## Context

`converter/pdf.py` reconstructs a page's Markdown from PyMuPDF span coordinates
plus heuristics (columns, headings, tables, footers) and optional AI passes. It
has **no math handling**: equations in the PDF arrive as mangled glyph runs and
are emitted as text. Complex layouts (dense tables, math/prose interleaving,
scans) lean on the vision/structure passes for a second guess. A request came
to add a "PDF → LaTeX → Markdown" path that hands pages to a structure-aware
document engine and then normalizes the result with the project's existing
conventions.

Two constraints bound the design:

- **pandoc cannot read PDF** (ADR-0037). A PDF→LaTeX *producer* is required,
  and every realistic producer is model-based — there is no deterministic
  converter in the same sense as PyMuPDF's text layer. The backend therefore
  belongs to the optional-AI family, never to the deterministic default path.
- **The intermediate differs by producer.** The maintained open engines emit
  Markdown with embedded LaTeX math (`$…$`); others emit LaTeX or Mathpix
  Markdown (`.mmd`). The post-processor must accept both formats.

A landscape survey (Oct 2026) found no maintained open-source converter from
PDF to compilable LaTeX — and the TeX toolchain, which only ever worked in the
other direction, has no reverse converter either: `pdftosrc` (pdfTeX) recovers
an embedded source stream (`/Type /SourceFile`) only when the producer
deliberately attached one, and `pstoedit -f latex2e` emits absolute-positioned
picture-environment code (a figure vectorizer, not document conversion).
Nougat — the tool most often described as "PDF → LaTeX" — emits Mathpix
Markdown (`.mmd`), has been unmaintained since 0.1.17 (Oct 2023), and its
weights are CC-BY-NC. Formula OCR (pix2tex/LaTeX-OCR, Texify, TexTeller) is
equation-level. The maintained open engines (marker, MinerU, Pix2Text,
olmOCR, Docling) emit Markdown with embedded LaTeX math; the only sources of
compilable `.tex` from an arbitrary PDF are proprietary (Mathpix's API/on-prem;
Aspose.PDF's `LaTeXSaveOptions`). The backend therefore accepts both
intermediates and treats the producer as a pluggable adapter.

The project already owns the LaTeX→Markdown half: `converter/latex.py`
(ADR-0038) wraps pandoc and normalizes to the pipeline's conventions — pandoc
flavors, image dedup via `write_image`, display math → PNG via `pdflatex`,
inline `$…$` passthrough. (ADR-0038's rejected alternative, "compile to PDF and
reuse `pdf.py`", is the reverse direction; it does not cover this case.)

## Decision

Add a **selectable PDF backend**: `PDF_BACKEND=direct|latex`, default
`direct`. Under `latex`, `PDFConverter.convert` hands the whole document to a
**producer**, post-processes the intermediate with the ADR-0038 machinery, and
emits one continuous Markdown document. Under `direct` — and on any producer
failure — the existing PyMuPDF path runs unchanged.

### Config surface

- `PDF_BACKEND` — `direct` | `latex`. Read lazily inside `convert()` (the
  `PDF_MODE` exception to the import-time rule, ADR-0012) so the GUI checkbox
  and the engine config can flip it per conversion. `PDF_MODE` is ignored when
  the backend is `latex` (the layout heuristics do not run).
- `PDF_LATEX_PROVIDER` — `auto` (default): resolves to `command` when
  `PDF2LATEX_CMD` is set, else `vlm`; can be forced to either.
- `PDF2LATEX_CMD` — the producer command, run in a temp working directory with
  `{input}`/`{outdir}` placeholders (and `PTM_PDF2TEX_IN`/`PTM_PDF2TEX_OUT` in
  the environment). It must leave exactly one output file in that directory.
- `PDF2LATEX_FORMAT` — `latex` (default) | `markdown`: the intermediate type.
  `latex` runs through pandoc; `markdown` is ingested directly. Both then get
  display-math rendering and image rewriting.
- `PDF_LATEX_BASE_URL` / `PDF_LATEX_MODEL` / `PDF_LATEX_API_KEY` — the built-in
  VLM producer's endpoint, defaulting to the transcriber chain (ADR-0029).
- `PDF_LATEX_TIMEOUT` — per producer call, seconds (default 600).
- `PDF_LATEX_SOURCE_RECOVERY` — `on` (default) | `off`: run the deterministic
  source-recovery probe (embedded `.tex` attachment, SyncTeX sidecar) before
  the producer.

CLI (the "PDF layout" group), GUI (a checkbox next to paper mode with a
persisted `pdf_backend` setting), the engine's `/api/config` plus dashboard,
and `config.snapshot()` telemetry all follow the existing `PDF_MODE` pattern.

### Deterministic source recovery (runs before any producer)

A PDF sometimes still carries its own source; when it does, no model is
needed:

- **Embedded source.** PyMuPDF (`doc.embfile_names()`, already a dependency)
  scans PDF attachments for a `.tex`/LaTeX file; the pdfTeX `/Type
  /SourceFile` stream (the `pdftosrc` case) is read the same way. A hit is
  extracted and handed to the ADR-0038 `LatexConverter`.
- **SyncTeX sidecar.** A sibling `*.synctex.gz` (written by
  `pdflatex -synctex=1`) records the `Input:` paths of the run that produced
  the PDF. If those `.tex` files still exist, their project converts through
  the ADR-0038 path.

Recovery supersedes every producer: it is deterministic, offline, and yields
the true source, so it runs first when `PDF_LATEX_SOURCE_RECOVERY` is `on`
(the default); `off` skips straight to the producer. Each successful recovery
is logged as a warning line naming the origin. Only when both probes miss does
the configured producer run.

### Producers

- **`command`** — an external document engine the user installs (marker,
  MinerU, Pix2Text, nougat, texify, Mathpix). Zero new Python dependencies;
  output discovery is by extension, so directory-shaped tools (marker writes
  `<outdir>/<stem>/<stem>.md`) and file-shaped tools both fit. Documented
  recipes in `docs/pdf-latex.md`.
- **`vlm`** — built in: each page is rendered to a PNG (2×) and sent to the
  local vision server (`converter.vision._chat_completion`) with a
  LaTeX-emitting prompt (body only, no preamble; `$…$` math; `\section`-level
  structure). Pages are concatenated and gated through
  `vision.transcription_quality`; `temperature=0` for reproducibility. This is
  the works-out-of-the-box producer; a document engine behind `command` is the
  quality path.

### Post-processing (shared by both producers)

- `latex` intermediate → `latex._run_pandoc` (no `--citeproc` on this route)
  → `latex._strip_attrs`. `markdown` intermediate → taken as-is.
- Then, for both: `latex._render_display_math` (display `$$…$$` → PNG via
  pdflatex; `$$…$$` passthrough with a `[WARN]` when pdflatex is absent),
  `latex._rewrite_images` into `assets/<stem>/` with content-hash dedup, and a
  `# Title` + `*Authors*` block (producer `\title`/`\author` → PDF metadata →
  stem).
- **Output contract:** a continuous document with `##` sections, like paper
  mode and the LaTeX input converter. No per-page `# … — Page N` headings,
  page-break divs, or per-page PNG ground-truth links.
- **AI gating:** `ai_passes={"summary"}` for this backend, ADR-0038's posture:
  the slide passes (vision/classify/interpret) never re-run over content the
  producer already transcribed, and `format` is skipped.

### Failure ladder

Any producer failure — command missing or non-zero exit, no output file found,
pandoc missing or failing, VLM server down or a page rejected by the quality
gate — degrades to the `direct` path for the whole document with a `[WARN]`
naming the cause. A conversion never fails because of the backend (the AI-pass
posture). The health pre-flight (`config.missing_servers`) includes the
transcriber endpoint when the VLM producer is active.

## Consequences

- **+** Math fidelity for the first time: equations become `$…$` or rendered
  PNG display math instead of mangled glyph runs, and structure arrives from
  the engine rather than from span heuristics.
- **+** The LaTeX half is reused wholesale (pandoc invocation, display-math
  rendering, image dedup, title block); no new Python dependencies and the
  converter stays ML-free (HTTP client + subprocess only).
- **+** PDFs that carry their source — an embedded `.tex` attachment, or a
  sibling SyncTeX sidecar pointing at a still-present project — convert
  deterministically through ADR-0038 with no model involved.
- **+** Deterministic default preserved: `direct` is untouched, and the new
  path never makes a conversion fail.
- **−** The producer path is model-based and therefore not deterministic; the
  VLM producer's LaTeX is only as good as the local vision model, and real
  quality needs an external engine installed by the user. Source recovery is
  the only deterministic sub-path, and only for PDFs that carry their source.
- **−** Output contract differs (continuous document, no page anchors), so
  `latex`-backend outputs are not interchangeable with `direct` outputs.
- **−** A failure anywhere in the document falls back the whole document (v1
  contract purity); per-page salvage is future work.
- **−** New config surface: `PDF_BACKEND`, eight `PDF2LATEX_*`/`PDF_LATEX_*`
  variables, one CLI flag, one GUI setting, and one snapshot field.

## Alternatives considered

- **Native PyMuPDF → LaTeX** — heuristic spans cannot reconstruct equations at
  all; rejected for the same reason ADR-0038 rejected a native LaTeX parser.
- **TeX-native reverse tools (`pdftosrc`, `pstoedit -f latex2e`)** — `pdftosrc`
  recovers only a deliberately embedded source stream (folded into the
  decision as the recovery probe); `pstoedit`'s LaTeX2e backend emits
  absolute-positioned picture-environment code for figures, not document
  source; rejected as a general path.
- **VLM-only producer** — works out of the box, but the quality ceiling is the
  local vision model; rejected in favour of also shipping the command hook,
  which costs little and carries the real engines.
- **Command-only producer** — leanest, but `PDF_BACKEND=latex` would do nothing
  until a tool is installed; the VLM producer gives a working default.
- **Bundled engine (isolated venv/server, like the audio server, ADR-0006)** —
  highest offline quality with no user installs, but drags torch-class
  dependencies and a new daemon into the project; deferred until the command
  hook proves insufficient.
- **Per-page hybrid routing** (only complex/math pages via LaTeX) — cheaper for
  mixed documents, but mixes two output regimes and re-opens the
  single-content-source contract (ADR-0020); deferred to a possible extension
  of the router (ADR-0024).
- **marker or Pix2Text as a Python dependency** — would make a quality path
  one `pip install`, but pulls torch/model stacks into a library that is
  deliberately ML-free; rejected in favour of the command hook.

## Update (2026-10-13) — deprecated before implementation

This decision is withdrawn: the backend's only general path is a model-based
producer (the local VLM, or an external document engine behind
`PDF2LATEX_CMD`), i.e. non-deterministic conversion, and it was decided not to
implement a non-deterministic PDF backend. `PDF_BACKEND` / `converter/
pdf_latex.py` will not be built; the default `direct` PyMuPDF path was never
dependent on this decision and is unchanged.

Two findings are retained for any future revisit: the producer landscape
survey (see Context), and the deterministic source-recovery probe (embedded
`.tex` attachment / SyncTeX sidecar → ADR-0038) — the only deterministic
remnant, which could stand as its own small change.
