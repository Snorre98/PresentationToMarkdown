# Presentation to Markdown

Native terminal app and reusable Python library that converts PowerPoint presentations (`.pptx`), PDF documents (`.pdf`), and LaTeX projects (`.tex`) into Markdown. Personal-use tool built for feeding lecture slides into an Obsidian vault.

- **TUI (`ptm-tui`)** — compiled Go terminal app: file discovery, batch conversion, live progress, history, and settings (ADR-0040). It is the only user interface (ADR-0051).
- **Engine (`ptm-engine`)** — the local backend the TUI drives: filesystem access, conversion, engine-hosted transcription, settings (ADR-0025)
- **Library** — importable `converter` package, no UI dependencies

## What it does

One Markdown file per source document. Image links are **off by default** — a
figure's local-AI description is kept where one exists, and nothing is extracted
to disk; turn on *Image links* in the TUI settings (env `IMAGE_LINKS=on`) to
restore the legacy `assets/<name>/` sidecar with `![…]`/`[Page N]` links
(ADR-0047). Both formats preserve **bold** and *italic* formatting.

### PowerPoint (`.pptx`)

| Source element | Markdown output |
| --- | --- |
| Slide title | `# Title — Slide N` heading (falls back to `# Slide N` when absent) |
| Text runs | `**bold**`, `*italic*`, `***both***` preserved |
| Bullets | `- ` list items |
| Numbered lists | `1. ` list items |
| Nested levels | Indented with two spaces per level |
| Tables | Pipe tables (`\|` and newlines escaped) |
| Images | No link by default — an AI description replaces it where available; `IMAGE_LINKS=on` extracts to `assets/<name>/` and references them |
| Speaker notes | `> **Notes:**` blockquote after each slide |

Also: slide-number and date placeholders are skipped, headers/footers are italicized, groups are walked recursively, and charts are skipped with a warning.

### PDF (`.pdf`)

| Source element | Markdown output |
| --- | --- |
| Each page | `# {slide title} — Page N` heading (falls back to `# Page N`) |
| Page render | Rendered in memory for the vision passes; linked as `[Page N](…)` only with `IMAGE_LINKS=on` |
| Text | Reconstructed into reading order, preserving bold/italic |
| Bullets | `- ` / nested `  - ` lists (bullet glyphs + indentation) |
| Tables | Pipe tables — bordered via `page.find_tables()`, borderless (Word layout grids) reconstructed from the text-layer stream order (ADR-0054); `PDF_BORDERLESS_TABLES=off` disables |
| Footer / slide number | Dropped (repeats identically on every page) |

Layout-aware: lines are reordered by their coordinates, bullets are detected from
the `•`/`–` glyphs and their indent, and tables are detected by PyMuPDF — plus
whitespace-only (borderless) tables rebuilt from the text layer's content-stream
order (`PDF_BORDERLESS_TABLES=off` disables that, ADR-0054).
Pages whose text can't be linearized (diagrams, multi-column flowcharts) fall
back to a collapsed raw-text block — and can optionally be transcribed by a local
vision model. See [docs/ai-vision.md](docs/ai-vision.md).

#### Whitepapers / multi-column documents (paper mode)

Academic papers, whitepapers and brochures are **two-column (or more)** layouts,
which default conversion treats as "complex" pages and dumps as raw text. Enable
**paper mode** in the TUI settings (*Paper layout*) or with `PDF_MODE=paper` to
convert them as continuous documents instead:

In paper mode each PDF is rendered column-by-column (full left column, then
right column) in reading order, and structured like a document rather than a
slide deck:

| Element | Markdown output |
| --- | --- |
| Title (page 1) | `# Title` — centered metadata blocks and flush-left titles at the learned title size, with wrapped title lines joined; `*Authors · Affiliation*` when a centered block is found |
| Section headings | `##`/`###`/`####` from the document's learned size tiers (24/18/14 pt in the sample above), including numbered headings and headings ending in `:`/`?`; uniform-size bold-only headings promote through a stricter evidence ladder |
| Body paragraphs | Wrapped lines rejoined into one Markdown line per paragraph, including paragraphs that continue in the next column or page; `PDF_REFLOW=off` keeps the legacy line-per-line output |
| Bullet lists | `- `/`  - ` items; a body paragraph after a list is not absorbed as a continuation |
| Page boundaries | No per-page headings — later-page content flows under the paper's last real `##` section (ADR-0052) |
| Running headers | Dropped (repeats at the top of every page) |
| Page separators | None — pages flow continuously (no `---` / page-break divs) |

Slide decks keep the default per-page behaviour; `PDF_MODE=slide` restores it
explicitly. The two modes are mutually exclusive. The TUI settings screen offers
the same choice as *Paper layout* (remembered across sessions).

With the structure pass enabled (TUI setting or `STRUCTURE_ENABLED=1`) an
optional LLM pass then improves
the structure: it fixes the page-1 title/authors block, blockquotes the
abstract, adds `##` headings and a `## References` section, wraps footnotes, and
repairs interleaved multi-column linearization — keeping every word verbatim.
Later pages carry no top-level heading and the model may not add one (ADR-0052).
Pages whose text layer is unusable (scans, garbage OCR) are instead reworded
from the rendered page image. Any page the model gets wrong is left in its
deterministic form with a warning. See
[Paper structure pass](#paper-structure-pass).

Shared slide-master background images are detected and skipped. With
`IMAGE_LINKS=on` (legacy), all extracted images are deduplicated by content hash,
and images that recur on ≥80% of slides/pages (logos, watermarks) are embedded
inline only on their first occurrence; later occurrences become a text hyperlink
to the same asset instead of a repeated image. By default no images are extracted
or linked at all — only their AI descriptions appear.

### LaTeX projects (`.tex`)

A LaTeX *project* is a folder containing `.tex` source; it converts to one `.md`
named after the folder through [pandoc](https://pandoc.org) (a required binary
for `.tex` input), with `\input`/`\include`/`\import` flattened, sections as
`##` headings, and citations resolved by citeproc with a bundled author-year
style (ADR-0038/0056/0058/0059/0060/0061):

| Source element | Markdown output |
| --- | --- |
| Title | `# Title` from `\title`, else the title page's biggest font line (`{\huge \bfseries …}`), else the folder name |
| Authors | `*Authors*` from `\author` (`\and`/`\\` becomes ` · `) |
| Title page content | A front block after the title: small-caps/`\large` lines joined with ` · `, `table` floats and bare `tabular`s as pipe tables with their `**Table N: …**` captions, `\IfFileExists` resolved (the word count); logo, rules and spacing dropped (ADR-0058) |
| TOC, list of figures/tables, `\maketitle` | Dropped |
| `\section` / `\subsection` | `##` / `###` headings with pandoc's `{#…}` attributes stripped |
| `\autoref` / `\ref` / `\eqref` | `Section 3` / bare `3` / `(1)` — numbered from the source, so references and captions agree (labels whose prefix is unknown keep pandoc's anchor form) |
| Float numbering | Matches the compiled document: floats in the stripped title page count (its members table is Table 1 in `main.lot`), labels there still resolve (ADR-0059) |
| TikZ & pgfplots figures | Rendered to PNG and linked as `![Figure N](assets/<name>/…)` with `IMAGE_LINKS=on`; the caption is always kept as a bold `**Figure N: …**` line — and with no image, the drawing's own node/axis text is kept as a `*Figure content (from the TikZ source):*` note (ADR-0060) |
| `\includegraphics` figures | Same numbered caption; the referenced image is extracted with `IMAGE_LINKS=on` (a PDF figure is rasterized) |
| Tables | Pipe tables with a bold `**Table N: caption**` line |
| Bibliography | Author-year entries close to biblatex's `authoryear`: `Author (Year). 'Title'. In: Journal 46.4, 44:1–44:37. Place: Publisher. url: … (visited on …)` — source-case titles, expanded en-dash ranges, publisher places, no serial comma (ADR-0061); `LATEX_CSL=<file.csl>` overrides the style |
| Literal text | pandoc's defensive escapes are undone outside math: `\~18/yr` → `~18/yr`, `\[TBD\]` → `[TBD]` (ADR-0058) |
| Display math | `$$…$$` passthrough, or a rendered PNG with `IMAGE_LINKS=on` |
| Inline math | `$…$` passthrough (also inside captions) |

TikZ flowcharts and pgfplots charts are rendered from their own source with
`standalone` + pdflatex, reusing the project's own `\tikzset`/
`\usetikzlibrary` declarations collected from its `.sty`/`.tex` files;
`LATEX_FIGURE_PREAMBLE=<file.tex>` appends extra preamble for anything else.
*Image links* off (the default) skips rendering entirely and keeps the numbered
captions plus each figure's extracted text; a missing pdflatex or a failed
compile does the same with a warning, and `LATEX_FIGURES=off` restores the
plain pandoc output. LaTeX runs **no AI passes** except the presentation summary
(ADR-0038). `.bib` files are passed to pandoc as case-protected copies (the
project's files are never modified).

## How to use

The app is `ptm-tui` (ADR-0040/0051): a compiled, opencode-style Go terminal UI
that discovers files in the current directory and drives the local `ptm-engine`
for conversion and transcription. The importable `converter` library remains
available for scripting (see [Library](#library)).

### TUI (native)

A compiled, opencode-style terminal app (`ptm-tui`) that discovers the
`.pptx`/`.pdf`/`.tex` files, LaTeX project folders, **and audio files** in the
current directory, selects them with `@` fuzzy search, and converts/transcribes
with live progress. The screen is a fixed frame (ADR-0053): a header with the
engine badge, a bounded jobs panel (composite progress bar, page counter,
transcription phase, elapsed time, `x` to cancel), three tabs — `1` files,
`2` results, `3` log — and a one-line help footer (`?` shows every binding).
`Space` multi-selects, `Enter` runs (an explicit selection first, otherwise the
cursor row; the selection is consumed either way), `r` re-discovers, and the
results tab lists each finished file's output path, warnings and duration
(`o` opens the Markdown, `f` reveals it in Finder, `y` copies the path) above
the durable `/api/runs` history.

Audio rows are marked `♫`: transcription is an engine-hosted job (ADR-0045),
which pairs a recording with its same-stem Markdown or writes a standalone
`<stem>.transcript.md`. The settings modal (`s`) toggles the AI passes, paper
layout, image links, PDF compression, inference overwrite, duplicate handling,
output folder, and transcription options (diarization, speaker count, audio
model, language) — persisted through the engine and applied to the next run
without a restart. Press `a` for **AI setup**: each pass, the models PtM will
actually use, their state, and the command to start anything that is down
(ADR-0065).

```bash
scripts/install-global.sh        # install ~/.local/bin/ptm-tui (once)
ptm-tui                          # run from any directory
```

Build/run without the global install:

```bash
scripts/ptm-tui.sh               # builds bin/ptm-tui, spawns the engine, opens the TUI
```

`ptm-engine` is a plain local HTTP/WS service that the TUI spawns and stops
itself (attach to an already-running one and it is left alone). Started by hand
it is `ptm-engine --port 9091`; it accepts the same AI-capability flags
(default: all off), which map onto the environment variables documented per
pass below:

| Flag | Enables |
| --- | --- |
| `--vision` | Vision transcription post-pass |
| `--classify` | Image classifier (implies `--vision`) |
| `--interpret` | Grounded diagram-interpretation pass |
| `--format` | LLM markdown-restructure pass |
| `--summary` | Presentation summary pass |
| `--structure` | LLM document-structure pass (paper-mode PDFs only) |
| `--all` | The five slide passes above (vision + classify + interpret + format + summary) |
| `--paper` | Treat PDFs as multi-column whitepapers (continuous document layout) |
| `--slide` | Treat PDFs as slide decks (the default) |
| `--image-links` | Legacy output: extract `assets/<name>/` and emit `![…]`/`[Page N](…)` links (default: no image links, AI descriptions only) |
| `--no-compress` | Skip Ghostscript/qpdf compression of PDFs archived into `source/` (default: compress) |
| `--overwrite-inference` | Replace an existing `<name>-inference-used.md` instead of writing `-2` (default: never overwrite inference output, ADR-0050) |
| `--env KEY=VALUE` | Set any other env var (repeatable) — model ids, URLs, log DB |

The pass labels, required models and start commands are in
[docs/ai-requirements.md](docs/ai-requirements.md); the TUI's `a` view shows the
live version.

Audio transcription is **not** among these flags — it is a separate engine job
the TUI dispatches (see [Audio transcription](#audio-transcription-optional)).

`--env` is the escape hatch for anything the flags don't cover, e.g.
`ptm-engine --vision --env VISION_MODEL=... --env VISION_LOG_DB=/tmp/ptm.sqlite`.
It also carries the lazy output preferences: `--env PDF_REFLOW=off` keeps the
legacy line-per-line paper output, and `--env PDF_BORDERLESS_TABLES=off`
disables borderless-table reconstruction. For `.tex` projects,
`--env LATEX_FIGURES=off` disables the TikZ/pgfplots figure pipeline,
`--env LATEX_FIGURE_PREAMBLE=/path/preamble.tex` adds preamble lines to the
standalone figure render, and `--env LATEX_CSL=/path/style.csl` replaces the
bundled author-year bibliography style.
See the [ADR index](docs/adr/README.md) for the rationale.

### Library

```python
from converter import convert_file, convert_files, SUPPORTED_EXTENSIONS

# one file (Markdown beside the source; source archived into source/)
result = convert_file("deck.pptx")
print(result.md_path or result.error)

# one file, explicit output folder
result = convert_file("deck.pptx", "out/")

# many files, with progress
def on_progress(idx, total, name):
    print(f"[{idx}/{total}] {name}")

results = convert_files(["deck1.pptx", "handout.pdf"], "out/", on_progress)
for r in results:
    print(r.source_path.name, "->", r.md_path or r.error)
    for warning in r.warnings:
        print("  warn:", warning)
```

- `convert_file(path, output_dir=None) -> ConvertResult`
- `convert_files(paths, output_dir=None, progress_callback=None) -> list[ConvertResult]`
- `progress_callback(idx: int, total: int, name: str) -> None`
- `ConvertResult` fields: `source_path`, `md_path`, `error`, `warnings`, `moved_to`
- `SUPPORTED_EXTENSIONS` — e.g. `{".pptx", ".pdf"}`
- `output_dir` is optional; when omitted, each file's Markdown is written beside the source under a kebab-case name and the source is moved into a sibling `source/` folder (ADR-0048); an explicit `output_dir` disables that organizing. `ConvertResult.moved_to` points at the archived copy, and `ConvertResult.inference_passes` lists the AI passes whose model output landed in the file — non-empty means the Markdown carries the `-inference-used` marker (ADR-0050).

### Ports

The local ML services are registered on the `macos-dev-config` fleet manifest
and owned by its control daemon (`:9300`; ADR-0029/0035) — the table below is
the projection PtM uses:

| Port | Used by |
| --- | --- |
| `:8081` | `qwen2.5-vl-7b-instruct-4bit` (mlx-vlm, Qwen2.5-VL 7B Instruct) — vision transcription, diagram interpretation, Markdown restructure, paper structure |
| `:8082` | `qwen2.5-vl-3b-instruct-4bit` (mlx-vlm, Qwen2.5-VL 3B Instruct) — image classifier |
| `:8083` | `llama-3.2-3b-instruct-4bit` (mlx-lm, Llama-3.2-3B) — presentation summary + paper structure text |
| `:8084` | `llama-3.2-1b-instruct-4bit` (writing assistant; not PtM) |
| `:8085` | `mistral-small-3.1-text-24b-instruct-2503-4bit` daemon (not PtM) |
| `:8089` | `audio` — audio pipeline (NB-Whisper / diarization / dereverb / enhance / isolate) |
| `:8090` | `embeddinggemma-2-q8-0` embeddings daemon (llama.cpp) |
| `:8099` | `whisper-large-v3-asr-fp16` / `whisper-large-v3-turbo-asr-fp16` (fleet `whisper` daemon) |
| `:9091` | **Native engine default** (`PTM_ENGINE_PORT` overrides; the TUI spawns it here) |

See [docs/ai-requirements.md](docs/ai-requirements.md) for the pass → model →
start-command matrix and the “not used by PtM” notes.

## Output layout

By default the Markdown is written **beside the source** under a kebab-case
name (normalized from the source: accents folded, spaces and punctuation
become `-`), and the converted source is moved into a sibling `source/` folder
with the same normalized name. Archived PDFs are compressed best-effort
(Ghostscript, moderate 300 dpi; `PDF_COMPRESS=off` — or the TUI's *Compress
archived PDFs* setting — disables; see
[ADR-0049](docs/adr/0049-pdf-compression-on-archive.md)):

```text
lectures/
├── 03-what-makes-things-fun-to-learn.md
└── source/
    └── 03-what-makes-things-fun-to-learn.pdf
```

One `.md` per source document — no `assets/` sidecar and no image links (AI
descriptions stand in for figures where the vision passes run).

- **LaTeX projects** are named after the project folder (`thesis/` →
  `thesis.md` beside it) and the whole project folder moves into `source/`.
- **Already-archived files** (directly inside a `source/` folder) are not
  moved again; their Markdown is (re)written in the `source/` folder's parent,
  so re-runs are idempotent. Recursive folder scans skip `source/` archives —
  point at a file (or the `source/` folder) to reconvert one explicitly.
- **Normalized-name collisions**: two different sources that normalize
  identically in one batch get a `-2` suffix (Markdown *and* archive copy)
  plus a warning, so nothing is silently overwritten. A pre-existing archive
  copy is replaced on re-conversion (deterministic names).
- **Inference-produced Markdown is name-marked** (ADR-0050): when any AI pass
  contributed content to the file (vision, interpret, structure, an accepted
  format rewrite, a model-written summary), the `.md` is named
  `<name>-inference-used.md` instead of `<name>.md`, so a free deterministic
  re-run can never overwrite a file that cost model time. An existing marked
  file is never overwritten — the next run writes `-inference-used-2` with a
  warning, unless the TUI's *Inference overwrite* setting (env
  `INFERENCE_OVERWRITE=on`) explicitly replaces it. The `source/` archive and
  `assets/<name>/` keep their plain names.
- **An explicit output folder disables the organizing** (TUI settings output
  folder): Markdown keeps its original name, sources are left untouched, and
  PDFs are neither moved nor compressed. The `-inference-used` marker still
  applies, since it describes the *content*, not the folder.

With `IMAGE_LINKS=on` the legacy sidecar is written next to the Markdown;
images are deduplicated by content hash and named `<name>_<NN>_<hash>.<ext>`:

```text
lectures/
├── deck.md
├── handout.md
└── assets/
    ├── deck/
    │   └── deck_01_19b314cf.png
    └── handout/
        └── handout_01_164ea19d.png
```

## Example

A slide with title "Bullets and *formatting*", a couple of bullets, and a note becomes:

```markdown
# Bullets and \*formatting\* — Slide 1

- Plain point
- **Bold ***italic* tail
  - Nested child

> **Notes:**
> First line
> Second line.

<div style="page-break-after: always; break-after: page;"></div>

---
```

Slides are separated by an HTML page break (`<div style="page-break-after: always;"></div>`) plus a visible `---` rule, and each heading carries its slide/page number.

## Install

Requires **Python 3.10+** and **Go 1.26+** (to build the TUI). Dependencies: `python-pptx`, `PyMuPDF` (PDF), `flask`/`flask-sock` + `simple-websocket` (engine), `SQLAlchemy` + `sqlite-vec` (log/settings/RAG store), and `numpy`. External binaries, all optional except where noted: `pandoc` (required for `.tex`), `pdflatex` (renders display math and TikZ/pgfplots figures for `.tex` with `IMAGE_LINKS=on`), `soffice` (PPTX chart transcription), and `gs`/`qpdf` (PDF compression on archive; Ghostscript preferred, qpdf the lossless fallback — `brew install ghostscript qpdf`).

### 1. Clone and set up a virtual environment

```bash
git clone <repo-url> PresentationToMarkdown
cd PresentationToMarkdown
python3 -m venv .venv
```

### 2. Install dependencies

```bash
./.venv/bin/pip install -r requirements.txt
```

### 3. (Optional) Install the engine command

```bash
./.venv/bin/pip install -e .
```

This puts `ptm-engine` — the TUI's backend — on the venv's `PATH`.
`scripts/ptm-tui.sh` bootstraps it automatically (`pip install -r` +
`pip install -e .`) when the binary is missing, so this step is only needed if
you want to launch the engine directly.

### 4. Run it

```bash
scripts/ptm-tui.sh               # build the TUI, spawn the engine, open the TUI
```

Install the global launcher once (`scripts/install-global.sh`) and `ptm-tui`
runs from any directory.

Library examples use the venv interpreter (`.venv/bin/python`);
substitute your system `python3` if you install the dependencies globally instead.

### Optional: AI passes

The AI passes (see [docs/ai-vision.md](docs/ai-vision.md) and
[docs/ai-requirements.md](docs/ai-requirements.md)) have **no Python
dependencies** — they talk to OpenAI-compatible HTTP endpoints — but they need
one or more model servers running locally. `mlx_vlm.server` serves **one chat
model per process**, so each model is its own server on its own port.

**The quickest answer:** in the TUI press `a`. It shows each pass, the model
PtM will actually use, whether it is up, and the exact start command for anything
down.

For all five slide passes, start these four models:

```sh
for m in qwen2.5-vl-7b-instruct-4bit qwen2.5-vl-3b-instruct-4bit \
         llama-3.2-3b-instruct-4bit embeddinggemma-2-q8-0; do
  curl -X POST "http://127.0.0.1:9300/start/$m"
done
```

| Model | Port | Needed for |
| --- | --- | --- |
| Qwen2.5-VL 7B Instruct (`qwen2.5-vl-7b-instruct-4bit`) | `:8081` | vision transcription, diagram interpretation, Markdown restructure, paper structure |
| Qwen2.5-VL 3B Instruct (`qwen2.5-vl-3b-instruct-4bit`) | `:8082` | image classifier (optional; implies vision) |
| Llama 3.2 3B Instruct (`llama-3.2-3b-instruct-4bit`) | `:8083` | presentation summary, paper structure text, Markdown need check (optional with `NEED_GATE=off`) |
| EmbeddingGemma 2 (`embeddinggemma-2-q8-0`) | `:8090` | presentation summary embeddings |

The 7B Qwen is the shared vision/rewrite model; the 3B Llama is the shared text
model. One model can serve several passes. Models carrying a matching tag but
losing to the first manifest match (Llama 1B, GLM-OCR, Qwen 3B as a transcriber)
are not used by PtM — see the requirements doc.

Install `mlx-vlm` (via `uv`, Python 3.12) if needed:

```bash
brew install uv
uv tool install mlx-vlm --python 3.12
```

Run each server in its own terminal, or via flaate's on-demand start commands.

PPTX chart transcription additionally needs LibreOffice:
`brew install --cask libreoffice`.

The model weights live on the external SSD under `HF_HOME` (see
`macos-dev-config/inference-readme.md` for the full serving/format/storage runbook).

## Structure

- `converter/` — conversion library, no UI dependencies
  - `__init__.py` — public API and extension-based dispatch (`convert_file`, `convert_files`)
  - `base.py` — shared `Converter` interface, registry, and reusable Markdown helpers
  - `pptx.py` — PowerPoint converter (python-pptx)
  - `pdf.py` — PDF converter (PyMuPDF), layout-aware text + table/bullet reconstruction
  - `latex.py` — LaTeX project converter via pandoc: title-page/title derivation, cross-reference and caption normalization (ADR-0056), TikZ/pgfplots figure rendering (ADR-0057)
  - `borderless.py` — deterministic borderless-table detection from the PDF text layer (ADR-0054)
  - `vision.py` — optional local vision-LLM post-pass (OpenAI-compatible endpoint)
  - `format.py` — Markdown polish post-pass (deterministic whitespace cleanup + optional LLM restructure)
  - `classify.py` — image classifier for the vision pass (tiny VLM)
  - `interpret.py` — grounded diagram-interpretation pass (typed relationships + prose)
  - `structure.py` — paper-mode document-structure LLM pass (verbatim text regime + image reword)
  - `organize.py` — default output layout: kebab-case naming, the `source/` archive, the `-inference-used` marker (ADR-0048/0050)
  - `compress.py` — best-effort PDF compression for archived sources (ADR-0049)
  - `inference.py` — per-conversion ledger of model-produced Markdown content (ADR-0050)
  - `config.py` — runtime registry of AI feature toggles + local server catalog (ADR-0012)
  - `render.py` — LibreOffice + PyMuPDF rendering for PPTX charts
  - `logstore.py` — conversion/vision telemetry façade over the SQLAlchemy ORM (`ptm.sqlite`)
  - `db/` — SQLAlchemy engine, declarative models, and repository layer (ADR-0026)
  - `summary.py` — per-presentation RAG index (sqlite-vec) + standardized summary header
  - `transcribe.py` — standalone local audio→text (ffmpeg + fleet ASR)
  - `audio.py` — fleet ASR client + speaker-diarization/enhancement client (audio server)
- `docs/ai-vision.md` — how to serve the vision model and enable the AI pass
- `docs/ai-audio.md` — how to serve the ASR/diarization models and enable the audio pass
- `docs/garbled-text-recovery.md` — the shared detect → classify → recover → gate → review pipeline across PDF/OCR/STT sources
- `docs/garbled-handling-vision.md` — the forward-looking vision: principles, layered signal stack, classifier ladder, and synthetic-data strategy
- `docs/ai-laya.md` — proposed typed classifier (Laya) for the classify stage, with rationale, schema, serving and fine-tuning
- `docs/adr/` — architecture decision records (MADR index)
- `engine.py` — native engine process the TUI drives (filesystem, conversion, engine-hosted transcription, settings; ADR-0025/0040)
- `cli_common.py` — AI flag parser + env mapping shared with the engine
- `tui/` — native Go TUI (`ptm-tui`), a separate Go module (ADR-0040)
- `scripts/` — TUI launcher/installer, audio-model server, transcript QA harness
- `tests/make_test_deck.py` — generates a synthetic deck covering all features
- `tests/make_test_pdf.py` — generates a synthetic PDF for testing
- `tests/make_test_latex.py` — generates synthetic LaTeX projects (title page, TikZ/pgfplots figures) for testing
- `tests/test_converters.py` — pytest smoke tests for both converters

## Adding a new file format

1. Create `converter/<format>.py` and subclass `Converter`:
   ```python
   from converter.base import Converter, ConvertResult

   class DocxConverter(Converter):
       extensions = (".docx",)

       def convert(self, path, output_dir) -> ConvertResult:
           ...
   ```
2. Register it in `converter/__init__.py`:
   ```python
   registry.register(DocxConverter)
   ```
3. Add the extension to `_SUPPORTED_EXTENSIONS` in `engine.py` so the TUI discovers it.

## AI vision pass

For diagrams, flowcharts and tables that appear as *images*, the converter can hand
them to a local **MLX vision model** (`mlx-vlm`) for a structured Markdown
transcription. It is off by default and fully deterministic without it. A cheap
**image classifier** (a small VLM such as Qwen2.5-VL-3B) classifies each image as
*text* (transcribed verbatim), *diagram* (a short high-level prose *gist* — type
and purpose, not its structure or labels — rendered as a blockquote), or
*decorative* (skipped), and enables
**image-level transcription** (embedded images in PDF and PPTX, plus PPTX charts
via LibreOffice). Before any model call, a **readability gate** skips images that
are too low-resolution or blurry to read (a VLM only hallucinates on those), and
every transcription passes a deterministic **quality gate** that discards
repetition loops, pathological nesting, placeholder/template echo, and runaway
output. Descriptions are emitted in place of the image link, so they survive the
default text-only output (ADR-0047); a skipped *decorative* image contributes
nothing at all. See **[docs/ai-vision.md](docs/ai-vision.md)** for how to serve
the models (referencing `macos-dev-config/inference-readme.md`) and the env vars
that enable it.

## Markdown polish pass

Every conversion finishes with a formatting post-pass (`converter/format.py`):

- **Deterministic (always on)** — strips trailing whitespace, collapses excess
  blank lines, and normalises heading spacing. No dependencies.
- **LLM restructure (opt-in)** — hands each slide to a local OpenAI-compatible
  chat model to reflow mid-sentence line breaks into paragraphs and promote
  heading-like bullets into `##`/`###` headings, while a word cross-check keeps
  the content verbatim. A continuous document (paper-mode PDF or LaTeX) is
  instead restructured one `##` section at a time with a conservative prompt
  that never adds or changes headings (ADR-0052).

Enable it by reusing the vision endpoint (see [AI vision pass](#ai-vision-pass)) —
toggle *Markdown restructure* in the TUI settings, or set `FORMAT_ENABLED=1` for
the engine.

`FORMAT_BASE_URL`/`FORMAT_MODEL`/`FORMAT_API_KEY` default to their `WRITE_*`
equivalents; override them to point the restructure pass at a different text
model.

## Paper structure pass (optional)

Paper-mode output (see
[Whitepapers / multi-column documents](#whitepapers--multi-column-documents-paper-mode))
is produced by deterministic geometry heuristics. The optional **structure pass**
(`converter/structure.py`) improves it with a local chat model, per page:

- **Text regime** — pages with a usable text layer are *check-and-amended*: the
  model fixes the page-1 `# Title` + `*Authors*` block, blockquotes the
  abstract, adds `##` headings (never demoting existing ones), wraps footnotes,
  inserts a `## References` heading, and repairs interleaved multi-column
  linearization — while every content word must stay verbatim. A word
  cross-check rejects any page that omits or invents prose. The page-1 title is
  the document's only `# ` heading; a later page may not gain one (ADR-0052),
  so its content stays under the paper's own `##` sections.
- **Image regime** — pages whose text layer is unusable (scans, garbage OCR;
  the raw-text `<details>` fallback) are reworded from the rendered page image,
  gated by image readability and transcription quality instead of the verbatim
  word gate.
- Pages already handled by the interpret/vision passes are skipped, and every
  rejected page keeps its deterministic Markdown with a `[WARN]`.
- A text layer that is *dense OCR garbage* (typo-shaped tokens / vowel-less
  junk, detected pre-call — ADR-0023) is skipped without a model call, since a
  layer that is not really prose can only fail the verbatim word gate.

Enable it together with paper mode: toggle *Structure pass* + *Paper layout* in
the TUI, or set `STRUCTURE_ENABLED=1` and `PDF_MODE=paper` for the engine.

| Var | Default | Purpose |
| --- | --- | --- |
| `STRUCTURE_ENABLED` | *(unset = off)* | Master switch for the structure pass |
| `STRUCTURE_BASE_URL` | `FORMAT_BASE_URL` (then `WRITE_BASE_URL`) | Structure model server |
| `STRUCTURE_MODEL` | `FORMAT_MODEL` (then `WRITE_MODEL`) | Structure model id (a VLM can read page images) |
| `STRUCTURE_API_KEY` | `FORMAT_API_KEY` (then `WRITE_API_KEY`) | Optional bearer token (unused locally) |
| `STRUCTURE_TEXT_BASE_URL` | `structure-text` server (`:8083`) | Small text model for the *text* regime (ADR-0024) |
| `STRUCTURE_TEXT_MODEL` | `mlx-community/Llama-3.2-3B-Instruct-4bit` | Text-regime model id — no image is sent, so a VLM is unnecessary |
| `STRUCTURE_TEXT_API_KEY` | `STRUCTURE_API_KEY` | Optional bearer token for the text-regime model |

The pass is PDF-only and therefore **not** part of the core slide passes; enable
it explicitly. The **text regime** (check-and-amend, no image)
runs on a small text model by default, while the **image regime** (reword from
the rendered page) stays on the writer VLM.

## Summary pass (per-presentation RAG)

Optionally, each converted presentation gets a **standardized English summary header**
prepended to its Markdown file, generated by a chat model grounded in the actual
slides through a **per-presentation RAG index** stored in `ptm.sqlite`.

How it works:

1. After conversion and polish, the Markdown is split back into chunks: one per
   slide for presentations, or one per `##` section for a continuous paper-mode
   PDF or LaTeX project (ADR-0052).
2. Each chunk is embedded (via an embeddings endpoint) and stored in a **sqlite-vec**
   table partitioned by document, alongside a plain-text copy in `deck_chunks`.
   Unchanged chunks are not re-embedded on re-conversion (content-hash cached).
3. A few section queries retrieve the most salient chunks; those, plus the chunk
   titles, are passed to a dedicated summary chat model.
4. The model writes the summary in a **deterministic format** — a `# Summary` block
   with `## Abstract`, `## Key Topics`, `## Key Takeaways`, `## Key Terms` and
   `## Metadata` sections. Bullet counts scale with the number of concepts detected
   in the content (topics capped at 16, takeaways at 12, terms at 8); `## Metadata`
   (source, number of slides/sections, date) is always computed locally, never by
   the model.
5. If the model output is *garbled* (no parseable sections, a section with no
   bullets, or suspiciously low word diversity), it is retried once; otherwise a
   deterministic extractive header (abstract = first chunk title, topics = chunk
   titles) is used. Any failure degrades gracefully — conversion never fails
   because of summaries.

Enable it by setting `SUMMARY_ENABLED=1` for the engine, or by toggling the
summary pass in the TUI settings.

| Var | Default | Purpose |
| --- | --- | --- |
| `SUMMARY_ENABLED` | *(unset = off)* | Master switch for the summary pass |
| `SUMMARY_BASE_URL` | `http://127.0.0.1:8083/v1` | Summary chat model server (`llama-3.2-3b-instruct-4bit`) |
| `SUMMARY_MODEL` | `mlx-community/Llama-3.2-3B-Instruct-4bit` | Summary chat model id (a small text model) |
| `SUMMARY_API_KEY` | *(unset)* | Optional bearer token (unused locally) |
| `EMBED_BASE_URL` | `http://127.0.0.1:8090/v1` | Embeddings server (`embeddinggemma-2-q8-0`) |
| `EMBED_MODEL` | `embeddinggemma-2-Q8_0.gguf` | Embeddings model id |
| `EMBED_API_KEY` | *(unset)* | Optional bearer token (unused locally) |

By default the summary uses the fleet's **Llama-3.2-3B text model**
(`llama-3.2-3b-instruct-4bit`, `:8083`) — the same model the structure pass uses
— rather than the writer VLM, so no 7B VLM is loaded for a short header.
Embeddings come from the fleet's **EmbeddingGemma 2** llama.cpp daemon
(`embeddinggemma-2-q8-0`, `:8090`). Start them with:

```sh
curl -X POST http://127.0.0.1:9300/start/llama-3.2-3b-instruct-4bit
curl -X POST http://127.0.0.1:9300/start/embeddinggemma-2-q8-0
```

Override `SUMMARY_*`/`EMBED_*` to point at different models if you prefer (for
example Ollama's `nomic-embed-text` on `:11434`).

## Audio transcription (optional)

A lecture recording can be transcribed **locally** into a timestamped,
speaker-labelled transcript. Transcription is **decoupled from conversion**
(ADR-0009) and runs as an **engine-hosted job** (ADR-0045), driven from the TUI:
select an audio row (`♫`) and press Enter. ASR goes over HTTP to the fleet's
`whisper` daemon (`whisper-large-v3-asr-fp16`/`whisper-large-v3-turbo-asr-fp16`, OpenAI multipart
with word timestamps) or the audio server (`nb-whisper-large`, `/v1/asr`), so
`converter` stays MLX- and PyTorch-free (ADR-0062). Speaker diarization
(optional) is served by the same PyTorch audio server. See
**[docs/ai-audio.md](docs/ai-audio.md)** for setup.

How the engine pairs audio with Markdown:

- **Audio with a same-stem `.md`** — the transcript is attached to that file as
  a `# Transcript` section (idempotent: re-running replaces the section).
- **Audio without a same-stem `.md`** — a standalone transcript is written to
  `<stem>.transcript.md` (plus `.srt` and `.clean.flac`). Standalone transcripts
  are **append-only**: re-running writes `<stem>.transcript.1.md`, `.2.md`, …
  with matching sidecars instead of overwriting the base file.
- **A `.md` selected directly** — its same-stem audio beside it is attached.

If transcription fails (missing `ffmpeg`, ASR daemon down), the job fails with a
warning — there is no mlx-whisper fallback. Every segment is recorded to
`ptm.sqlite` (`transcript_segments` table, keyed by the Markdown path).

The audio pipeline is `ffmpeg clean → WPE dereverb → DeepFilterNet enhance →
[SepFormer isolate] → fleet ASR`. Reverb removal (WPE) and denoising
(DeepFilterNet) run automatically whenever the audio server is up; voice
isolation is opt-in (`AUDIO_ISOLATE_ENABLED=1`) and writes a
`<stem>.isolated.flac` that ASR transcribes instead of the cleaned file.

Progress streams as engine WS frames (per phase and per file); local subprocess
phases emit a heartbeat when quiet. ASR itself is phase-level only (one HTTP
call per file). Only one transcription may run at a time — the engine holds an
exclusive `flock` on `<PTM_STATE_DIR or ~/.local/state/ptm>/transcribe.lock`
and a second job is rejected while it runs. See
[docs/runbook.md](docs/runbook.md).

| Var | Default | Purpose |
| --- | --- | --- |
| `AUDIO_ENABLED` | *(unset = off)* | Master switch |
| `AUDIO_MODEL` | `nb-whisper-large` | Fleet ASR name: `nb-whisper-large`, `whisper-large-v3-asr-fp16` (quality) or `whisper-large-v3-turbo-asr-fp16` (speed); the short `whisper-large-v3` names and old HF ids are aliased (ADR-0062/0064) |
| `AUDIO_FFMPEG_BIN` | `ffmpeg` | ffmpeg binary |
| `AUDIO_LANGUAGE` | `no` | Whisper language hint (`auto`/empty = auto-detect) |
| `AUDIO_HEARTBEAT_SECONDS` | `20` | Quiet-interval before a `still working …` heartbeat line (local subprocess phases) |
| `WHISPER_BASE_URL` | manifest `whisper` daemon (`:8099/v1`) | Whisper daemon base URL override |
| `WHISPER_API_KEY` | *(unset)* | Optional bearer token for the whisper daemon |
| `AUDIO_DEREVERB_ENABLED` | `1` | WPE dereverberation via the audio server |
| `AUDIO_ENHANCE_ENABLED` | `1` | DeepFilterNet denoise via the audio server |
| `AUDIO_ISOLATE_ENABLED` | *(unset = off)* | Voice isolation (SepFormer) via the audio server |
| `AUDIO_DIARIZE_ENABLED` | *(unset = off)* | Enable speaker labelling |
| `AUDIO_DIARIZE_BASE_URL` | `http://127.0.0.1:8089/v1` | Audio server base URL (env override; daemon-resolved by default, ADR-0035) |

### Running all AI passes at once

Each AI pass is independently opt-in (off by default), so you can mix and match.
The TUI settings screen toggles them (persisted through the engine and applied
without a restart). To enable every slide pass — vision transcription + image
classifier, diagram interpretation, markdown restructure, and the presentation
summary — at engine launch:

```bash
VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 INTERPRET_ENABLED=1 FORMAT_ENABLED=1 SUMMARY_ENABLED=1 \
  scripts/ptm-tui.sh
```

This uses four fleet models: Qwen2.5-VL 7B (`:8081`), Qwen2.5-VL 3B
(`:8082`), Llama 3.2 3B (`:8083`), and EmbeddingGemma 2 (`:8090`). See
[docs/ai-requirements.md](docs/ai-requirements.md).

Audio transcription is deliberately **not part of the slide-pass set** (it needs
an audio file, ffmpeg and a running ASR daemon, and is a separate job anyway).
Select audio rows in the TUI to run it.

The RAG index lives in the same `ptm.sqlite` as the vision log (`deck_documents`,
`deck_chunks`, and a `deck_chunk_vec` sqlite-vec table). `sqlite-vec` is an added
dependency; the embedding dimension is derived from the first embedding and
cached (no separate probe), and unchanged chunks are not re-embedded.

## Known limitations / future ideas

- SmartArt is skipped (SmartArt text lives in a separate XML part); charts are
  skipped unless the vision pass + classifier + LibreOffice are enabled
- PDF vector diagrams and non-column flowcharts are not linearized
  deterministically — they fall back to the rendered PNG plus a collapsed
  raw-text block (embedded images are still transcribed via the vision pass).
  Multi-column text pages *are* linearized: column-by-column automatically, and
  with full document structure in paper mode.
- Paper-mode headings and paragraphs come from the document's own learned
  typography (pitch, paragraph-gap, size tiers, ADR-0055). Unusual layouts can
  still slip through: a heading at body size with no bold, or prose styled at a
  heading tier (a pull-quote paragraph), may be missed or over-promoted. The
  optional structure pass is the semantic backstop, and `PDF_REFLOW=off`
  restores the previous line-per-line body output.
- **Residual review findings** (documented, not fixed; the sweep's outputs and
  report are kept in `artifacts/corpus-sweep/`). Across a 19-PDF corpus of
  lecture decks, papers and a 500-page book: (a) when a recovered `#` title
  also lives inside a detected table (some print-to-PDF web articles), those
  title words appear both as the heading and inside the pipe table;
  (b) diagram-heavy slides converted in *paper* mode can still have text
  fragments reordered by the row-major linearizer; (c) Markdown escaping of
  specials can shift where merged lines move run boundaries (same rendered
  text). Multi-column fragment order is unchanged from the previous converter,
  and no text was lost or garbled.
- PyMuPDF is AGPL-3.0 (or commercial) licensed — fine for personal use, review if you distribute
- Audio transcription uses segment-level timestamps (word-level needs wav2vec2 alignment) and does not yet auto-align the transcript to slides (see ADR-0007)
- **Markdown flavor toggle** (Obsidian `![[wiki-links]]`), note style, and heading level options could go in a settings pane
- There is **no standalone CLI** (ADR-0051): scripting goes through the `converter` library or the engine's local HTTP/WS API
- Packaging into a standalone executable with PyInstaller
