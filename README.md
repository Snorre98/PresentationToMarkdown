# Presentation to Markdown

Web app, desktop app and reusable Python library that converts PowerPoint presentations (`.pptx`) and PDF documents (`.pdf`) into Markdown. Personal-use tool built for feeding lecture slides into an Obsidian vault.

- **Web GUI** — a full in-browser converter (drag-and-drop, batch convert, live progress, history) driven by a local native **engine** process
- **Desktop GUI** — PySide6 interface (file list, output folder, progress, log), kept as a fallback
- **Library** — importable `converter` package, no UI dependencies

## What it does

One Markdown file per source document, with images extracted to a sidecar `assets/<name>/` folder. Both formats preserve **bold** and *italic* formatting.

### PowerPoint (`.pptx`)

| Source element | Markdown output |
| --- | --- |
| Slide title | `# Title — Slide N` heading (falls back to `# Slide N` when absent) |
| Text runs | `**bold**`, `*italic*`, `***both***` preserved |
| Bullets | `- ` list items |
| Numbered lists | `1. ` list items |
| Nested levels | Indented with two spaces per level |
| Tables | Pipe tables (`\|` and newlines escaped) |
| Images | Extracted to `assets/<name>/` and referenced |
| Speaker notes | `> **Notes:**` blockquote after each slide |

Also: slide-number and date placeholders are skipped, headers/footers are italicized, groups are walked recursively, and charts are skipped with a warning.

### PDF (`.pdf`)

| Source element | Markdown output |
| --- | --- |
| Each page | `# {slide title} — Page N` heading (falls back to `# Page N`) |
| Page render | Rendered PNG linked as `[Page N](…)` — the visual ground truth |
| Text | Reconstructed into reading order, preserving bold/italic |
| Bullets | `- ` / nested `  - ` lists (bullet glyphs + indentation) |
| Tables | Pipe tables via `page.find_tables()` |
| Footer / slide number | Dropped (repeats identically on every page) |

Layout-aware: lines are reordered by their coordinates, bullets are detected from
the `•`/`–` glyphs and their indent, and real tables are detected by PyMuPDF.
Pages whose text can't be linearized (diagrams, multi-column flowcharts) fall
back to a link to the rendered PNG plus a collapsed raw-text block — and can
optionally be transcribed by a local vision model. See [docs/ai-vision.md](docs/ai-vision.md).

#### Whitepapers / multi-column documents (`--paper`)

Academic papers, whitepapers and brochures are **two-column (or more)** layouts,
which default conversion treats as "complex" pages and dumps as raw text. Enable
**paper mode** to convert them as continuous documents instead:

```bash
ptm --paper paper.pdf            # CLI
PDF_MODE=paper ptm paper.pdf     # same, via env var
```

In paper mode each PDF is rendered column-by-column (full left column, then
right column) in reading order, and structured like a document rather than a
slide deck:

| Element | Markdown output |
| --- | --- |
| Title + subtitle + authors (page 1) | `# Title` + `*Authors · Affiliation*` metadata block |
| Section headings | `## Heading` (bold / larger / centered standalone lines) |
| Each page | `# Page N` heading + `[Page N](…)` PNG link |
| Running headers | Dropped (repeats at the top of every page) |
| Page separators | None — pages flow continuously (no `---` / page-break divs) |

Slide decks keep the default per-page behaviour; pass `--slide` (or
`PDF_MODE=slide`) to restore it explicitly. `--paper` and `--slide` are mutually
exclusive. The GUI offers the same toggle as a *Paper layout* checkbox
(remembered across sessions).

With `--structure` (or `STRUCTURE_ENABLED=1`) an optional LLM pass then improves
the structure: it fixes the page-1 title/authors block, blockquotes the
abstract, adds `##` headings and a `## References` section, wraps footnotes, and
repairs interleaved multi-column linearization — keeping every word verbatim.
Pages whose text layer is unusable (scans, garbage OCR) are instead reworded
from the rendered page image. Any page the model gets wrong is left in its
deterministic form with a warning. See
[Paper structure pass](#paper-structure-pass).

Shared slide-master background images are detected and skipped, and all extracted
images are deduplicated by content hash. Images that recur on ≥80% of slides/pages
(logos, watermarks) are embedded inline only on their first occurrence; later
occurrences become a text hyperlink to the same asset instead of a repeated image.

## How to use

The **web GUI** (below, *Web GUI*) is the primary interface: it converts, browses
files, and watches history through a native engine process. The **desktop GUI**
(`main.py` / `ptm-start`) remains a supported fallback, and the **CLI**
(`ptm`) is the headless equivalent.

### Desktop GUI

```bash
./.venv/bin/python main.py
```

1. **Add files** — click *Add Files...* to pick `.pptx`/`.pdf` files, *Add Folder...* to scan a folder (and subfolders) for supported files, or drag-and-drop either onto the list.
2. **Adjust the list** — *Remove* selected entries or *Clear* everything.
3. **Pick an output folder (optional)** — by default each file is written to its own `<input-folder>/markdown/`; enter a path and click *Browse...* to override and send everything to one folder instead.
4. **Convert** — click *Convert*. Conversion runs on a background thread, so the UI stays responsive while a progress bar advances and the log reports one line per file:
   - `[OK]  deck.pptx -> /path/deck.md`
   - `[ERR] broken.pdf: ...` on failure
   - `[WARN] ...` for non-fatal issues (e.g. skipped charts)

The AI passes (vision, classifier gate, diagram interpretation, LLM restructure,
RAG summary, paper structure) each have a checkbox in the GUI, toggled at
runtime and remembered across sessions — no restart or `ptm-start --vision`
needed. A *Check servers* status line probes the local model servers and shows
which are up/down; if you enable a pass whose server is not running, **Convert**
blocks and tells you the exact terminal command to start it (e.g.
`tools/serve.sh start transcriber`). The server catalog defaults to a built-in
table and optionally refreshes from the sibling `macos-dev-config/servers.conf`.

### CLI

Three console commands (`pip install -e .` puts them on `PATH`):

```bash
# convert files/folders headlessly — same behavior as the GUI's Convert button
ptm deck.pptx handout.pdf
ptm --output out/ --vision --format .
ptm --all --quiet folder_of_slides/
ptm --paper whitepaper.pdf       # two-column paper -> continuous document
ptm --slide handout.pdf          # force per-page slide layout (the default)

# launch the GUI, enabling AI passes via flags
ptm-start --vision
ptm-start --all

# transcribe lecture audio to Markdown (decoupled from conversion)
ptm-transcribe deck.md          # attach to existing Markdown
ptm-transcribe week-2.mp3       # write week-2.transcript.md (no Markdown needed)

# run the full web UI (convert + watch history); spawns its native engine
ptm-dashboard --port 9090       # see "Web GUI" below
```

These are the five entry points: `ptm`, `ptm-start`, `ptm-transcribe`,
`ptm-dashboard`, and `ptm-engine` (the engine is usually started from the web UI
rather than by hand).

**`ptm`** — headless batch conversion, mirroring the GUI: folders are scanned
recursively for `.pptx`/`.pdf`, the output folder defaults to
`<source>/markdown`, progress and per-file results are logged as
`[N/M]` / `[OK]` / `[ERR]` / `[WARN]` lines, and converted files are recorded
in the recent list.

```
ptm [AI flags] [-o DIR] [--no-recursive] [--no-recent] [-q] PATH...
```

**`ptm-start`** — the same GUI as `./python main.py`, with AI capabilities toggled
by flags instead of env vars.

**`ptm-transcribe`** — local audio→text, decoupled from conversion. Give it a
`.md` to attach a transcript to, an audio file to produce a standalone
`<stem>.transcript.md`, or a folder to scan both. See
[Audio transcription](#audio-transcription-pass-optional).

Both accept the same AI flags (default: all off):

| Flag | Enables |
| --- | --- |
| `--vision` | Vision transcription post-pass |
| `--classify` | Classifier gate (implies `--vision`) |
| `--interpret` | Grounded diagram-interpretation pass |
| `--format` | LLM markdown-restructure pass |
| `--summary` | Per-presentation RAG summary pass |
| `--structure` | LLM document-structure pass (paper-mode PDFs only) |
| `--all` | The five slide passes above (vision + classify + interpret + format + summary) |
| `--paper` | Treat PDFs as multi-column whitepapers (continuous document layout) |
| `--slide` | Treat PDFs as slide decks (the default) |
| `--env KEY=VALUE` | Set any other env var (repeatable) — model ids, URLs, log DB |

Audio transcription is **not** an AI flag on `ptm`/`ptm-start` — it is its own
command, `ptm-transcribe` (see [Audio transcription](#audio-transcription-pass-optional)).

`--env` is the escape hatch for anything the flags don't cover, e.g.
`ptm --vision --env VISION_MODEL=... --env VISION_LOG_DB=/tmp/ptm.sqlite deck.pptx`.
See the [ADR index](docs/adr/README.md) for the rationale.

### Library

```python
from converter import convert_file, convert_files, SUPPORTED_EXTENSIONS

# one file (output defaults to <input-folder>/markdown)
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
- `ConvertResult` fields: `source_path`, `md_path`, `error`, `warnings`
- `SUPPORTED_EXTENSIONS` — e.g. `{".pptx", ".pdf"}`
- `output_dir` is optional; when omitted, each file is written to `<source-folder>/markdown/`.

### Web GUI (dashboard + native engine)

The web frontend is now a **full converter**, not just a log viewer. A separate
native **engine** process (ADR-0025) owns what a browser cannot do alone —
running `convert_files`, browsing the filesystem, opening folders in Finder, and
persisting settings — while the browser UI drives it over localhost with a
WebSocket progress stream:

```bash
./.venv/bin/python -m dashboard --port 9090      # web UI (opens the browser)
ptm-dashboard --port 9090                         # same, after pip install -e .
```

Open the printed URL, then click **Start engine** in the header (or run
`./.venv/bin/python -m engine --port 8090` / `ptm-engine` yourself). The
**Convert** tab reproduces the desktop GUI's workflow:

- **Files** — drag-and-drop `.pptx`/`.pdf`, *Add Files* (browser picker),
  *Add Folder* (server-side directory browser with recursive scan), or *Recent*.
- **Output** — type a path, *Browse* a server-side folder, or *Open in Finder*
  (the engine invokes the OS `open`).
- **Options** — Paper layout and Duplicate-if-exists checkboxes.
- **AI features** — the same six toggles as the GUI (with `implies`), plus
  *Check servers* for the up/down probe.
- **Convert** — runs on the engine; per-file and per-page progress bars and the
  `[OK]/[ERR]/[WARN]` log stream in live over WebSocket.

The history tabs (Runs / Timeline / Errors / Models / RAG) come from the same
`ptm.sqlite` the engine writes, so a conversion you start in the browser shows up
in the history immediately. The desktop GUI (`main.py` / `ptm-start`) remains
available as a fallback.

### Dashboard (read-only log view)

Beyond conversion, the web app keeps the ADR-0022 read-only log surface:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--db PATH` | `<repo root>/ptm.sqlite` | Log database to read |
| `--host HOST` | `127.0.0.1` | Bind address (loopback only, a local surface) |
| `--port N` | `8080` | UI port to bind |
| `--engine-port N` | `8090` | Engine port (also `PTM_ENGINE_PORT`) |

The UI opens the database **read-only** (`mode=ro` + `query_only=ON`) and never
imports `converter`; the engine is the sole writer. History tabs auto-refresh
every ~2s:

- **Runs** — one row per conversion run (`conversion_runs`): status, duration,
  event and error counts; click a run to drill in.
- **Timeline** — the run's phase swimlane (`convert → structure → format →
  summary`, plus derived `classify`/`transcribe`/`interpret` spans from their
  events) and the per-page events beneath it. This is where the old
  "progress bar goes idle during structure/summary" gap (ADR-0013) is fixed.
- **Errors** — all `vision_events` with an error, with structure/format
  rejections pinned on top as the main cost driver.
- **Models** — per-stage/per-model latency aggregates (count, min/avg/p50/p95/
  max/total) with a histogram, so slow passes and outliers are easy to spot.
- **RAG** — the per-presentation summary index (`deck_documents` slide counts,
  `deck_chunks`, embedding dimension), populated when the summary pass runs
  (ADR-0021).

A per-run **Config** panel (inside the Timeline tab) shows the snapshot captured
when the conversion started: which feature toggles were on, the resolved base
URLs and model ids each pass used, `PDF_MODE`, and which servers were down at run
start (ADR-0022).

### Ports

The local AI passes reserve a block of loopback ports, and the web app sits
right next to them:

| Port | Used by |
| --- | --- |
| `:8080` | **Web UI default** |
| `:8081` | Transcriber (mlx-vlm, Qwen2.5-VL-7B) |
| `:8082` | Classifier gate (mlx-vlm, Qwen2.5-VL-3B) |
| `:8083` | Audio server (dereverb / enhance / isolate / diarize) |
| `:8084` | Summary chat model (mlx-lm, Llama-3.2-3B) |
| `:8090` | **Native engine default** |
| `:11434` | Ollama (embeddings) |

The web UI's default `:8080` sits directly below the transcriber, and its
automatic port-fallback walks up to `+100` (`:8080` → `:8180`) — straight through
`:8081`/`:8082`/`:8083`/`:8084` and the engine's `:8090`. So if `:8080` is already
taken and any AI server (or the engine) is running, the fallback will collide.
When AI servers are up, start the web app (and, if you launch it by hand, the
engine) on ports clear of the whole block:

```bash
ptm-dashboard --port 9090                 # web UI
./.venv/bin/python -m engine --port 9091  # engine (only if started by hand)
```

If the port you ask for is free it simply binds there; the fallback only kicks
in when that exact port is already occupied.

## Output layout

```text
out/
├── deck.md
├── handout.md
└── assets/
    ├── deck/
    │   └── deck_01_19b314cf.png
    └── handout/
        └── handout_01_164ea19d.png
```

Images are deduplicated by content hash and named `<name>_<NN>_<hash>.<ext>`.

With the default `output_dir=None`, the same layout is written under each source file's own folder, i.e. `<input-folder>/markdown/`.

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

Requires **Python 3.10+**. Dependencies: `python-pptx`, `PyMuPDF` (PDF), `PySide6` (desktop GUI), `flask`/`flask-sock` + `simple-websocket` (web GUI), `SQLAlchemy` + `sqlite-vec` (log/settings/RAG store), and `numpy`.

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

### 3. (Optional) Install the CLI commands

```bash
./.venv/bin/pip install -e .
```

This puts `ptm` (headless convert), `ptm-start` (GUI launcher),
`ptm-transcribe` (audio→Markdown transcription), `ptm-dashboard` (web UI), and
`ptm-engine` (native engine) on the venv's `PATH`. Skip it if you only want the
GUI/library. If you'd rather not activate
the venv, `scripts/ptm-start.sh` and `scripts/ptm-transcribe.sh` resolve
`.venv/bin/ptm-start` / `.venv/bin/ptm-transcribe` for you (each bootstraps with
`pip install -e .` if the binary is missing).

### 4. Run it

```bash
./.venv/bin/python main.py      # desktop GUI (or: ptm-start)
ptm deck.pptx                   # headless conversion (if step 3 was run)
ptm-dashboard --port 9090       # web UI (if step 3 was run)
```

All other commands in this document use the venv interpreter (`.venv/bin/python`);
substitute your system `python3` if you install the dependencies globally instead.

### Optional: AI vision pass

The vision post-pass (see [docs/ai-vision.md](docs/ai-vision.md)) has **no Python
dependencies** — it talks to an OpenAI-compatible HTTP endpoint — but it needs one
or two model servers running locally. `mlx_vlm.server` serves **one chat model per
process**, so each model is its own server on its own port.

1. Install `mlx-vlm` (via `uv`, Python 3.12):
   ```bash
   brew install uv
   uv tool install mlx-vlm --python 3.12
   ```

2. Start the **transcriber** (required for any vision use; leave it running):
   ```bash
   mlx_vlm.server --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --port 8081
   ```
   Convert with just this one model running:
   ```bash
   VISION_ENABLED=1 ./.venv/bin/python main.py
   ```

3. *(Optional)* Start the **classifier** — a second, small model that gates what
   gets transcribed (skips photos/logos) and enables image-level transcription:
   ```bash
   mlx_vlm.server --model mlx-community/Qwen2.5-VL-3B-Instruct-4bit --port 8082
   VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 ./.venv/bin/python main.py
   ```

| Model | Port | Needed for |
| --- | --- | --- |
| Qwen2.5-VL-7B (transcriber) | `:8081` | any vision transcription |
| Qwen2.5-VL-3B (classifier) | `:8082` | the classifier gate + image-level transcription (optional) |

Running both uses ~8 GB of unified memory (~4 GB transcriber + ~4 GB classifier).
Run each server in its own terminal, or in the background (`nohup`/`launchd`).

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
  - `vision.py` — optional local vision-LLM post-pass (OpenAI-compatible endpoint)
  - `format.py` — Markdown polish post-pass (deterministic whitespace cleanup + optional LLM restructure)
  - `classify.py` — cheap classifier gate for the vision pass (tiny VLM)
  - `interpret.py` — grounded diagram-interpretation pass (typed relationships + prose)
  - `structure.py` — paper-mode document-structure LLM pass (verbatim text regime + image reword)
  - `config.py` — runtime registry of AI feature toggles + local server catalog (ADR-0012)
  - `render.py` — LibreOffice + PyMuPDF rendering for PPTX charts
  - `logstore.py` — conversion/vision telemetry façade over the SQLAlchemy ORM (`ptm.sqlite`)
  - `db/` — SQLAlchemy engine, declarative models, and repository layer (ADR-0026)
  - `summary.py` — per-presentation RAG index (sqlite-vec) + standardized summary header
  - `transcribe.py` — standalone local audio→text (ffmpeg + mlx-whisper subprocess)
  - `audio.py` — speaker-diarization client (isolated PyTorch server)
- `docs/ai-vision.md` — how to serve the vision model and enable the AI pass
- `docs/ai-audio.md` — how to serve the ASR/diarization models and enable the audio pass
- `docs/adr/` — architecture decision records (MADR index)
- `dashboard/` — Flask web app for conversion + the read-only log view (`app.py`, `db.py`, `templates/`, `static/`, `__main__.py`; ADR-0022, ADR-0025, ADR-0026)
- `engine.py` — native engine process the web app drives (filesystem, conversion, settings; ADR-0025)
- `gui.py` — PySide6 interface (file list, output folder, progress, log) — fallback desktop UI
- `main.py` — entry point
- `cli.py` — `ptm` headless batch converter (GUI parity)
- `cli_transcribe.py` — `ptm-transcribe` standalone audio→Markdown transcription
- `start.py` — `ptm-start` GUI launcher with AI flags
- `cli_common.py` — shared AI flag parser + env mapping
- `tests/make_test_deck.py` — generates a synthetic deck covering all features
- `tests/make_test_pdf.py` — generates a synthetic PDF for testing
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
3. Add the extension to the GUI file filter and placeholder text if you want it listed.

## AI vision pass

For diagrams, flowcharts and tables that appear as *images*, the converter can hand
them to a local **MLX vision model** (`mlx-vlm`) for a structured Markdown
transcription. It is off by default and fully deterministic without it. A cheap
**classifier gate** (a small VLM such as Qwen2.5-VL-3B) classifies each image as
*text* (transcribed verbatim), *diagram* (a short high-level prose *gist* — type
and purpose, not its structure or labels — rendered as a blockquote), or
*decorative* (skipped), and enables
**image-level transcription** (embedded images in PDF and PPTX, plus PPTX charts
via LibreOffice). Before any model call, a **readability gate** skips images that
are too low-resolution or blurry to read (a VLM only hallucinates on those), and
every transcription passes a deterministic **quality gate** that discards
repetition loops, pathological nesting, placeholder/template echo, and runaway
output. See **[docs/ai-vision.md](docs/ai-vision.md)** for how to serve the models
(referencing `macos-dev-config/inference-readme.md`) and the env vars that enable
it.

## Markdown polish pass

Every conversion finishes with a formatting post-pass (`converter/format.py`):

- **Deterministic (always on)** — strips trailing whitespace, collapses excess
  blank lines, and normalises heading spacing. No dependencies.
- **LLM restructure (opt-in)** — hands each slide to a local OpenAI-compatible
  chat model to reflow mid-sentence line breaks into paragraphs and promote
  heading-like bullets into `##`/`###` headings, while a word cross-check keeps
  the content verbatim.

Enable it by reusing the vision endpoint (see [AI vision pass](#ai-vision-pass)):

```bash
FORMAT_ENABLED=1 ./.venv/bin/python main.py
```

`FORMAT_BASE_URL`/`FORMAT_MODEL`/`FORMAT_API_KEY` default to their `WRITE_*`
equivalents; override them to point the restructure pass at a different text
model.

## Paper structure pass (optional)

Paper-mode output (see
[Whitepapers / multi-column documents](#whitepapers--multi-column-documents-paper))
is produced by deterministic geometry heuristics. The optional **structure pass**
(`converter/structure.py`) improves it with a local chat model, per page:

- **Text regime** — pages with a usable text layer are *check-and-amended*: the
  model fixes the page-1 `# Title` + `*Authors*` block, blockquotes the
  abstract, adds `##` headings (never demoting existing ones), wraps footnotes,
  inserts a `## References` heading, and repairs interleaved multi-column
  linearization — while every content word must stay verbatim. A word
  cross-check rejects any page that omits or invents prose.
- **Image regime** — pages whose text layer is unusable (scans, garbage OCR;
  the raw-text `<details>` fallback) are reworded from the rendered page image,
  gated by image readability and transcription quality instead of the verbatim
  word gate.
- Pages already handled by the interpret/vision passes are skipped, and every
  rejected page keeps its deterministic Markdown with a `[WARN]`.
- A text layer that is *dense OCR garbage* (typo-shaped tokens / vowel-less
  junk, detected pre-call — ADR-0023) is skipped without a model call, since a
  layer that is not really prose can only fail the verbatim word gate.

Enable it together with paper mode:

```bash
ptm --paper --structure paper.pdf
STRUCTURE_ENABLED=1 PDF_MODE=paper ptm paper.pdf
```

| Var | Default | Purpose |
| --- | --- | --- |
| `STRUCTURE_ENABLED` | *(unset = off)* | Master switch for the structure pass |
| `STRUCTURE_BASE_URL` | `FORMAT_BASE_URL` (then `WRITE_BASE_URL`) | Structure model server |
| `STRUCTURE_MODEL` | `FORMAT_MODEL` (then `WRITE_MODEL`) | Structure model id (a VLM can read page images) |
| `STRUCTURE_API_KEY` | `FORMAT_API_KEY` (then `WRITE_API_KEY`) | Optional bearer token (unused locally) |
| `STRUCTURE_TEXT_BASE_URL` | `structure-text` server (`:8085`) | Small text model for the *text* regime (ADR-0024) |
| `STRUCTURE_TEXT_MODEL` | `mlx-community/Llama-3.2-3B-Instruct-4bit` | Text-regime model id — no image is sent, so a VLM is unnecessary |
| `STRUCTURE_TEXT_API_KEY` | `STRUCTURE_API_KEY` | Optional bearer token for the text-regime model |

The pass is PDF-only and therefore **not** part of `--all`; enable it
explicitly with `--structure`. The **text regime** (check-and-amend, no image)
runs on a small text model by default, while the **image regime** (reword from
the rendered page) stays on the writer VLM.

## Summary pass (per-presentation RAG)

Optionally, each converted presentation gets a **standardized English summary header**
prepended to its Markdown file, generated by a chat model grounded in the actual
slides through a **per-presentation RAG index** stored in `ptm.sqlite`.

How it works:

1. After conversion and polish, the Markdown is split back into per-slide chunks.
2. Each chunk is embedded (via an embeddings endpoint) and stored in a **sqlite-vec**
   table partitioned by document, alongside a plain-text copy in `deck_chunks`.
   Unchanged chunks are not re-embedded on re-conversion (content-hash cached).
3. A few section queries retrieve the most salient chunks; those, plus the slide
   titles, are passed to a dedicated summary chat model.
4. The model writes the summary in a **deterministic format** — a `# Summary` block
   with `## Abstract`, `## Key Topics`, `## Key Takeaways`, `## Key Terms` and
   `## Metadata` sections. Bullet counts scale with the number of concepts detected
   in the content (topics capped at 16, takeaways at 12, terms at 8); `## Metadata`
   (source, slide count, date) is always computed locally, never by the model.
5. If the model output is *garbled* (no parseable sections, a section with no
   bullets, or suspiciously low word diversity), it is retried once; otherwise a
   deterministic extractive header (abstract = first slide title, topics = slide
   titles) is used. Any failure degrades gracefully — conversion never fails
   because of summaries.

Enable it with `SUMMARY_ENABLED=1`:

```bash
SUMMARY_ENABLED=1 ./.venv/bin/python main.py
```

| Var | Default | Purpose |
| --- | --- | --- |
| `SUMMARY_ENABLED` | *(unset = off)* | Master switch for the summary pass |
| `SUMMARY_BASE_URL` | `http://127.0.0.1:8084/v1` | Summary chat model server (dedicated `summary` server) |
| `SUMMARY_MODEL` | `mlx-community/Llama-3.2-3B-Instruct-4bit` | Summary chat model id (a small text model) |
| `SUMMARY_API_KEY` | *(unset)* | Optional bearer token (unused locally) |
| `EMBED_BASE_URL` | `http://localhost:11434/v1` | Embeddings server (Ollama) |
| `EMBED_MODEL` | `nomic-embed-text` | Embeddings model id |
| `EMBED_API_KEY` | *(unset)* | Optional bearer token (unused locally) |

By default the summary uses a **dedicated small text model** (ADR-0021) —
`mlx_lm.server` serving `Llama-3.2-3B-Instruct-4bit` on `:8084` — rather than
the writer VLM, so no 7B VLM is loaded for a short header. Embeddings come from
**Ollama**. Start the summary model with
`mlx_lm.server --model mlx-community/Llama-3.2-3B-Instruct-4bit --port 8084`
(or `tools/serve.sh start summary` once the `summary` row is registered in
`servers.conf`). Override `SUMMARY_*`/`EMBED_*` to point at different models if
you prefer.

## Audio transcription pass (optional)

A lecture recording can be transcribed **locally** into a timestamped,
speaker-labelled transcript. Transcription is **decoupled from conversion**
(ADR-0009): it runs as its own `ptm-transcribe` command, which works *with or
without* an existing Markdown file. It runs via **mlx-whisper**
(`mlx-community/whisper-large-v3-turbo` by default), invoked as a subprocess so
`converter` stays MLX-free. Speaker diarization (optional) is served by a
separate PyTorch server. See **[docs/ai-audio.md](docs/ai-audio.md)** for setup.

```bash
# with existing Markdown: discover same-stem audio beside it and attach
ptm-transcribe deck.md                    # deck.md + deck.mp3 -> "# Transcript" section

# without Markdown: transcribe straight to a transcript file
ptm-transcribe week-2.mp3                 # -> week-2.transcript.md (+ .srt, .clean.flac)

# explicit pairing / speaker labels
ptm-transcribe week-2.mp3 --to deck.md    # attach week-2's audio to deck.md
ptm-transcribe --diarize deck.md          # + speaker labels
ptm-transcribe --isolate deck.md          # + isolate the dominant voice
ptm-transcribe --language no week-2.mp3   # language hint
```

The audio file is paired to a Markdown file by **convention** (same stem, same
folder) or explicitly (`--audio-file`, `--to MARKDOWN.md`); when neither settles
it, `ptm-transcribe` prompts to pick the lecture (`[0]` = standalone). For a
standalone audio file the transcript is **append-only**: re-running writes
`week-2.transcript.1.md`, `.2.md`, … (with matching `.srt` and `.clean.<N>.flac`)
instead of
overwriting — pass `--overwrite` to replace the base file instead. Attaching to a
`.md` is idempotent: the `# Transcript` section is replaced, not duplicated.
If transcription fails (missing `ffmpeg`/`mlx_whisper`, server down), it degrades
to a warning. Every segment is recorded to `ptm.sqlite` (`transcript_segments`
table, keyed by the Markdown path).

The audio pipeline is `ffmpeg clean → WPE dereverb → DeepFilterNet enhance →
[SepFormer isolate] → mlx-whisper`. Reverb removal (WPE) and denoising
(DeepFilterNet) run automatically whenever the audio server is up; voice
isolation is opt-in via `--isolate` and writes a `<stem>.isolated.flac` that
Whisper transcribes instead of the cleaned file.

Transcription streams live progress to stderr (ffmpeg + mlx-whisper output and
phase lines), with a `still working … (elapsed …)` heartbeat when a phase goes
quiet (e.g. the first-run model download). Only one `ptm-transcribe` may run at a
time — it holds an exclusive `flock` on
`<PTM_STATE_DIR or ~/.local/state/ptm>/transcribe.lock` and a second invocation
fails fast with exit `3` (`another instance is already running (PID …)`). Output
files are written atomically, and Ctrl-C kills the child, cleans temp files, and
releases the lock. See [docs/runbook.md](docs/runbook.md).

| Var | Default | Purpose |
| --- | --- | --- |
| `AUDIO_ENABLED` | *(unset = off)* | Master switch |
| `AUDIO_MODEL` | `mlx-community/whisper-large-v3-turbo` | ASR model id (`…-large-v3-mlx` for max quality) |
| `AUDIO_MLX_WHISPER_BIN` | `mlx_whisper` | mlx-whisper CLI |
| `AUDIO_FFMPEG_BIN` | `ffmpeg` | ffmpeg binary |
| `AUDIO_LANGUAGE` | *(unset = auto-detect)* | Whisper language hint |
| `AUDIO_HEARTBEAT_SECONDS` | `20` | Quiet-interval before a `still working …` heartbeat line |
| `AUDIO_CONDITION_ON_PREVIOUS_TEXT` | *(unset = off)* | Feed prior output back as a prompt (off avoids the "log log log" repetition loop on long recordings) |
| `AUDIO_DEREVERB_ENABLED` | `1` | WPE dereverberation via the audio server |
| `AUDIO_ENHANCE_ENABLED` | `1` | DeepFilterNet denoise via the audio server |
| `AUDIO_ISOLATE_ENABLED` | *(unset = off)* | Voice isolation (SepFormer) via the audio server |
| `AUDIO_DIARIZE_ENABLED` | *(unset = off)* | Enable speaker labelling |
| `AUDIO_DIARIZE_BASE_URL` | `http://127.0.0.1:8083/v1` | Diarization service base URL |

### Running all AI passes at once

Each AI pass is independently opt-in (off by default), so you can mix and match.
In the GUI the same toggles are checkboxes (persisted across sessions); the
`ptm-start` flags below are the headless equivalent. To enable every pass —
vision transcription + classifier gate, diagram interpretation, markdown
restructure, and the RAG summary — in one command:

```bash
# GUI
ptm-start --all

# headless
ptm --all deck.pptx

# or, the underlying env vars directly:
VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 INTERPRET_ENABLED=1 FORMAT_ENABLED=1 SUMMARY_ENABLED=1 \
  ./.venv/bin/python main.py
```

This uses only what's already running: the vision transcriber (`:8081`), the
classifier (`:8082`), the summary chat model (`:8084`), and Ollama for embeddings
(`nomic-embed-text`).

Audio transcription is deliberately **not part of `--all`** (it needs an audio
file and the mlx-whisper/ffmpeg toolchain, and is a separate step anyway). Run
it before or after conversion with `ptm-transcribe`:

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
  with full document structure under `--paper`.
- PyMuPDF is AGPL-3.0 (or commercial) licensed — fine for personal use, review if you distribute
- Audio transcription uses segment-level timestamps (word-level needs wav2vec2 alignment) and does not yet auto-align the transcript to slides (see ADR-0007)
- Markdown flavor toggle (Obsidian `![[wiki-links]]`), note style, and heading level options could go in a settings pane
- Packaging into a standalone executable with PyInstaller
