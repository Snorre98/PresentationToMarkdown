# Implementation Plan: PDF LaTeX backend (PDF → LaTeX → Markdown)

- Status: **Deprecated** (2026-10-13, before implementation) — the backend
  requires a non-deterministic producer; not to be implemented. Retained as the
  design record.
- Related: [ADR-0046](adr/0046-pdf-latex-backend.md) (decision),
  [ADR-0037](adr/0037-pandoc-evaluated-deferred.md),
  [ADR-0038](adr/0038-latex-pandoc-backend.md),
  [ADR-0012](adr/0012-runtime-ai-features.md),
  [ADR-0029](adr/0029-serving-via-control-daemon.md)
- Repo: PresentationToMarkdown (`converter/`, `cli_common.py`, `gui.py`,
  `engine.py`, `dashboard/`, `tests/`, `docs/`)

> Deprecated before implementation: the feature's general path needs a
> non-deterministic producer (the local VLM or an external model engine), and
> it was decided not to implement a non-deterministic PDF backend. The
> deterministic source-recovery probe (C1.2.2) is the only piece that could be
> revived independently.

## 0. Ground truth (verified 2026-10-13, code review)

- `converter/pdf.py::PDFConverter.convert` owns the whole PDF path; layout mode
  is read lazily via `_pdf_mode()` (the ADR-0012 exception), so the same
  pattern works for `PDF_BACKEND`.
- `converter/latex.py` already provides the reusable half:
  `pandoc_available()` / `latex_available()`, `_run_pandoc`, `_strip_attrs`,
  `_rewrite_images`, `_resolve_image`, `_render_display_math`,
  `_render_math_png`. `_run_pandoc` is citation-oriented and needs a
  `citeproc: bool = True` parameter for this route.
- `converter/base.py::Converter.ai_passes` is consulted per instance in
  `convert_file`; `LatexConverter` already narrows it to `{"summary"}`.
- Env/flag plumbing to mirror for `PDF_BACKEND`: `cli_common.py` "PDF layout"
  group + `ai_env_vars`; `gui.py` `_PDF_MODE_KEY` / `paper_check` /
  `start_conversion`; `engine.py` `POST /api/config`; `config.snapshot()`;
  `dashboard/static/app.js` renders `pdf_mode`.
- `converter/vision.py` exposes the generic `_chat_completion`, image-content
  helpers, `strip_code_fences`, and `transcription_quality` — the VLM producer
  reuses all of them.
- `config.missing_servers()` probes only `FEATURES` endpoints; the backend's
  VLM endpoint must be added there for the GUI/engine pre-flight.
- Tests already use `needs_pandoc` / `needs_latex` skip markers
  (`tests/test_latex.py`); synthetic PDFs come from `tests/make_test_pdf.py`
  (`tests/test_doc.pdf`, `tests/test_paper.pdf`).
- Landscape survey (2026-10-13): **no maintained open-source PDF →
  compilable-LaTeX converter exists.** The TeX toolchain has no reverse tool
  (`pdftosrc` reads only deliberately embedded `/Type /SourceFile` streams;
  `pstoedit -f latex2e` emits picture-environment graphics). Nougat emits
  `.mmd`, last release 0.1.17 (Oct 2023), weights CC-BY-NC. Formula OCR
  (pix2tex, Texify, TexTeller) is equation-level. Maintained engines (marker,
  MinerU, Pix2Text, olmOCR, Docling) emit Markdown with embedded `$…$` math;
  compilable `.tex` as a service is proprietary (Mathpix, Aspose.PDF).
  Consequences: the `latex | markdown` intermediate stays; step 0 is a
  deterministic source-recovery probe (embedded attachment or SyncTeX);
  recipes cover marker, MinerU, Pix2Text, nougat, Mathpix.

## 1. Constraints

1. No new Python dependencies; `direct` stays byte-identical and the default.
2. `converter` stays ML-free: the VLM producer is an HTTP client, external
   engines run as subprocesses only.
3. The backend never fails a conversion: `[WARN]` + fall back to `direct`.
4. Env-first, lazy reads (no import-time gating), following `PDF_MODE`.
5. Reuse `converter/latex.py` helpers instead of duplicating them.
6. Tests skip on missing binaries via the existing markers.

## 2. Settled decisions (from the design discussion)

| Question | Decision |
| --- | --- |
| Producer for v1 | Command hook (`PDF2LATEX_CMD`) **and** built-in VLM (`vlm`), resolved by `auto` |
| Scope | Whole-document backend; no per-page hybrid routing |
| Output contract | Continuous document (`# Title` + `*Authors*` + `##` sections); no per-page headings or PNG links |
| Failure posture | Whole-document fallback to `direct` with a warning |

## 3. Config surface (final)

| Env var | Default | Meaning |
| --- | --- | --- |
| `PDF_BACKEND` | `direct` | `direct` (PyMuPDF path) or `latex` (this backend); `PDF_MODE` ignored under `latex` |
| `PDF_LATEX_PROVIDER` | `auto` | `auto` = `command` when `PDF2LATEX_CMD` set, else `vlm` |
| `PDF2LATEX_CMD` | *(unset)* | Producer command; `{input}`/`{outdir}` placeholders + `PTM_PDF2TEX_IN`/`PTM_PDF2TEX_OUT`; must leave exactly one output file |
| `PDF2LATEX_FORMAT` | `latex` | `latex` (→ pandoc) or `markdown` (ingested directly); discovery accepts `.tex` / `.md`/`.mmd` |
| `PDF_LATEX_BASE_URL` / `_MODEL` / `_API_KEY` | transcriber chain | VLM producer endpoint (ADR-0029 resolution order) |
| `PDF_LATEX_TIMEOUT` | `600` | Per producer call (subprocess / HTTP), seconds |
| `PDF_LATEX_SOURCE_RECOVERY` | `on` | Step-0 probes (embedded `.tex` attachment, SyncTeX sidecar); `off` skips straight to the producer |

CLI: `--pdf-backend {direct,latex}` (PDF layout group). GUI: checkbox +
persisted `pdf_backend` setting. Engine: `POST /api/config` accepts it;
`snapshot()` reports it; dashboard renders it.

## 4. Workstream C1 — backend core + command producer

### C1.1 — Parameterize the pandoc helper

**Files:** `converter/latex.py`.

- **Task C1.1.1** — `_run_pandoc(body, project_dir, bibs, warnings, *,
  citeproc: bool = True)`; the LaTeX-input route keeps `True`, the PDF route
  passes `False` (no bibliography).
- **Accept:** `tests/test_latex.py` unchanged and green.

### C1.2 — New module `converter/pdf_latex.py`

**Files:** `converter/pdf_latex.py` (new).

- **Task C1.2.1** — lazy env accessors (`backend()`, `provider()`,
  `command()`, `fmt()`, `timeout()`, `source_recovery()`) and the module
  docstring documenting every variable.
- **Task C1.2.2** — source recovery (step 0):
  `recover_source(path, warnings) -> Path | None`:
  - (a) PyMuPDF `embfile_names()`/`embfile_get` for a `.tex`/LaTeX attachment
    (a pdfTeX `/Type /SourceFile` stream is read the same way);
  - (b) a sibling `*.synctex.gz` (gzip or plain): parse `Input:<n>:<path>`
    records, resolve relative paths against the sidecar directory, and take
    the first existing `.tex` that `LatexConverter.project_entry` accepts;
  returns the ADR-0038 input (project directory or lone `.tex`) or `None`.
  Skipped when `PDF_LATEX_SOURCE_RECOVERY=off`.
- **Task C1.2.3** — `Intermediate` dataclass `{text, fmt, base_dir, title,
  authors}` and `produce_command(path, workdir, warnings) -> Intermediate |
  None`: `subprocess.run(cmd, shell=True, cwd=workdir, env=…,
  timeout=timeout())`, exactly-one-output-file discovery by extension
  (recursive), `None` + warning on failure.
- **Task C1.2.4** — `postprocess(inter, assets_dir, stem, warnings) ->
  list[str]`: `latex` → `latex._run_pandoc(..., citeproc=False)` then
  `latex._strip_attrs`; then `latex._render_display_math` and
  `latex._rewrite_images` (dedup via `write_image`), and the `# Title` +
  `*Authors*` block (`\title`/`\author` → PDF metadata → stem).
- **Task C1.2.5** — `convert_via_latex(path, output_dir, progress_callback,
  output_stem, warnings) -> ConvertResult | None` orchestrator: recovery
  first — on a hit, delegate to `LatexConverter()` (ADR-0038) and record a
  warning naming the origin; otherwise run the producer. Writes `<stem>.md`;
  `None` signals "fall back"; wraps the producer in
  `logstore.phase(current_run_id(), "pdf-latex", 2)`.
- **Accept:** covered by C1.5.

### C1.3 — Branch the converter

**Files:** `converter/pdf.py`.

- **Task C1.3.1** — at the top of `convert()`: `latex` backend → delegate to
  `pdf_latex.convert_via_latex`; on `None` append the warning and continue on
  the direct path.
- **Task C1.3.2** — `PDFConverter.__init__` sets `self.ai_passes =
  frozenset({"summary"})` when the backend is `latex` (instance attribute; the
  registry constructs one instance per conversion). `polish_text` still runs
  deterministically with `allow_llm=False`.
- **Accept:** with `PDF_BACKEND` unset the full suite is unchanged; with the
  stub producer, output has no vision/format passes even when enabled.

### C1.4 — CLI flag

**Files:** `cli_common.py`.

- **Task C1.4.1** — `--pdf-backend {direct,latex}` in the "PDF layout" group;
  map to `env["PDF_BACKEND"]` in `ai_env_vars`; `--paper`/`--slide` continue to
  map `PDF_MODE` (ignored under `latex`, documented in help text).
- **Accept:** `tests/test_cli.py` mapping test.

### C1.5 — Tests

**Files:** `tests/test_pdf_latex.py` (new), `tests/test_cli.py` (touch),
`tests/make_test_pdf.py` (reuse only).

- **Task C1.5.1** — stub producer scripts: a `sh`/`python` one-liner writing
  `out.tex` (with `\section`, `$x^2$`, `$$…$$`, an image reference) and one
  writing `out.md`; driven via `PDF2LATEX_CMD` + `PDF2LATEX_FORMAT`.
- **Task C1.5.2** — `needs_pandoc` / `needs_latex` marks; asserts: title/authors
  block, `##` sections, inline `$…$` passthrough, display-math PNG (or `$$`
  passthrough + warning without pdflatex), image rewrite into `assets/` with
  dedup, `markdown`-route ingestion, provider/`auto` resolution.
- **Task C1.5.3** — fallback tests: failing command, no output file, missing
  pandoc → direct-path result + warning naming the cause.
- **Task C1.5.4** — source recovery tests: a synthetic PDF with an embedded
  `.tex` attachment (`doc.embfile_add`), a fake `*.synctex.gz` beside a built
  LaTeX project, and the `off` switch; assert delegation to the ADR-0038
  output and that no producer is invoked.
- **Accept:** `./.venv/bin/python -m pytest tests/test_pdf_latex.py
  tests/test_cli.py`.

## 5. Workstream C2 — VLM producer + surfaces

### C2.1 — VLM producer

**Files:** `converter/pdf_latex.py`.

- **Task C2.1.1** — `produce_vlm(path, progress_callback, warnings) ->
  Intermediate | None`: per page, 2× pixmap → `vision._chat_completion` with a
  LaTeX prompt (body only, no preamble; `$…$`; `\section`-level structure;
  no commentary); `strip_code_fences` + `transcription_quality` per page; one
  rejected page → whole-document fallback; `temperature=0`; page progress via
  the existing callback shape.
- **Task C2.1.2** — endpoint resolution: `PDF_LATEX_*` env → `VISION_*`
  constants (`converter/vision.py`); document the default chain.
- **Accept:** monkeypatched `_chat_completion` produces a multi-page document;
  a garbage page triggers fallback.

### C2.2 — Config integration

**Files:** `converter/config.py`.

- **Task C2.2.1** — `snapshot()` gains `pdf_backend` and `pdf_latex_provider`
  (plus `pdf_latex_command: bool`), next to `pdf_mode`.
- **Task C2.2.2** — `missing_servers()`: when backend is `latex` and the
  provider resolves to `vlm`, include the transcriber endpoint (same
  daemon/`probe` fallback logic as `vision`).
- **Accept:** `tests/test_config.py` additions; GUI pre-flight blocks on a
  down transcriber with `PDF_BACKEND=latex`.

### C2.3 — GUI surface

**Files:** `gui.py`.

- **Task C2.3.1** — `_PDF_BACKEND_KEY = "pdf_backend"`; a "LaTeX backend"
  checkbox beside `paper_check` with a tooltip (pandoc / command / VLM
  prerequisites); seeding from env → setting; `start_conversion` sets
  `os.environ["PDF_BACKEND"]`; persisted on toggle.
- **Accept:** `tests/test_ptm_start.py` (or the existing GUI-state pattern)
  covers seeding/persistence; manual smoke: checkbox flips the backend for one
  conversion without restart.

### C2.4 — Engine + dashboard

**Files:** `engine.py`, `dashboard/static/app.js`.

- **Task C2.4.1** — `POST /api/config` accepts `pdf_backend`
  (`direct|latex`, validated), persists `pdf_backend`, sets the env for the
  next job (mirroring `pdf_mode`).
- **Task C2.4.2** — dashboard shows `pdf_backend` beside `pdf_mode`.
- **Accept:** `tests/test_engine.py` config round-trip.

### C2.5 — Tests

- **Task C2.5.1** — VLM provider tests (C2.1), snapshot/preflight tests
  (C2.2), engine round-trip (C2.4).
- **Accept:** `./.venv/bin/python -m pytest`.

## 6. Workstream C3 — docs

### C3.1 — Runbook `docs/pdf-latex.md`

- **Task C3.1.1** — env table (§3) + recipes:
  - marker: `PDF2LATEX_CMD='marker_single {input} --output_dir {outdir}
    --output_format markdown'`, `PDF2LATEX_FORMAT=markdown`; prerequisites
    (`pip install marker-pdf`, `brew install llama.cpp`), macOS `fast` mode
    default, `balanced` needs a GPU;
  - nougat (`.mmd` → `markdown`; note: unmaintained, CC-BY-NC weights),
    MinerU and Pix2Text (`p2t` CLI; confirm exact flags against its
    command-tool docs) → `markdown`; texify and Mathpix exports → `latex`;
  - source recovery: what fires it (embedded `.tex`, sibling `.synctex.gz`),
    the warning it logs, and `PDF_LATEX_SOURCE_RECOVERY=off`;
  - `ptm --pdf-backend latex --env PDF2LATEX_CMD=… paper.pdf` walkthrough.
- **Task C3.1.2** — troubleshooting: pandoc/pdflatex absence, fallback
  warnings, timeouts, non-determinism note.
- **Accept:** recipes render correctly; each documented command matches the
  implemented placeholder protocol.

### C3.2 — Project docs

- **Task C3.2.1** — `README.md`: PDF section (backends, when to use), flag and
  env tables.
- **Task C3.2.2** — `AGENTS.md`: `converter/pdf_latex.py` in the architecture
  list, a PDF-backend bullet in the feature summary, the
  `ptm --pdf-backend latex …` example, and the lazy-read note.
- **Task C3.2.3** — `docs/ai-vision.md`: one line that the VLM producer shares
  the transcriber endpoint.
- **Accept:** docs links resolve; no stale "PDF has no LaTeX path" claims.

## 7. Sequencing

```
ADR-0046 + index + ADR-0037 note         (this change)
C1 (core + command producer)  ──►  C2 (VLM + surfaces)  ──►  C3 (docs)
```

C1 is independently shippable: with one commit, `PDF_BACKEND=latex` +
`PDF2LATEX_CMD` already converts via marker/nougat/Pix2Text. C2 adds the
untooled default and surface parity. C3 can start while C2 is in review; the
tool recipes are verified manually only if the tools are installed.

## 8. Verification

| Phase | pytest | Manual |
| --- | --- | --- |
| C1 | `tests/test_pdf_latex.py` (source recovery, stub producers, fallback), `test_cli.py` | `ptm --pdf-backend latex --env PDF2LATEX_CMD=… tests/test_paper.pdf` |
| C2 | VLM provider (monkeypatched), `test_config.py`, `test_engine.py` | GUI checkbox flips backend without restart; pre-flight reports a down transcriber |
| C3 | — | recipes run once each on the tools that are installed |

## 9. Out of scope (v1)

- Bundled document engine (isolated venv/server, ADR-0006 pattern).
- Per-page hybrid routing or per-page salvage (single-content-source contract).
- marker/MinerU/Pix2Text as Python dependencies.
- A TUI-side toggle (the engine config surface already covers it).
- An `cli_ab.py` A/B arm for the backend.

## 10. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| VLM LaTeX is weak for dense math | Documented as the convenience path; the command hook carries marker/Mathpix-class quality |
| Producer is slow / times out on long PDFs | `PDF_LATEX_TIMEOUT`; per-page progress for the VLM route; fallback stays available |
| Output contract differs from `direct` | Documented in ADR-0046 and README; the backend is explicitly opt-in |
| Silent quality regressions from a bad producer | Fallback warnings surface in the log/dashboard; `snapshot()` records the backend and provider per run |
| Recovered source diverges from the PDF (source edited since the compile) | SyncTeX/embedded source are compile-time artifacts of *that* PDF; recovery is logged with its origin, and `PDF_LATEX_SOURCE_RECOVERY=off` forces the producer path |
| Helper coupling to `latex.py` internals | Reuse is deliberate (house pattern: `base.py` private helpers); the `citeproc` parameter is the only refactor |
