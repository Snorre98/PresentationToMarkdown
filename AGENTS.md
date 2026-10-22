# AGENTS.md

## Project

Native terminal app and reusable Python library that converts PowerPoint (`.pptx`), PDF (`.pdf`), and LaTeX projects into Markdown.

- The importable library lives in the `converter/` package and has no UI dependencies.
- **Default output layout (ADR-0048)**: no explicit output folder → the `.md` is written **beside the source** under a kebab-case name normalized from it, and the converted source is moved into a sibling `source/` folder with the same name (`03 What makes things fun.pdf` → `03-what-makes-things-fun.md` + `source/03-what-makes-things-fun.pdf`). Files already in a `source/` folder are not moved again (re-runs are idempotent), recursive folder scans skip `source/` archives, normalized-name collisions in one batch get a `-2` suffix + warning, and an explicit output folder (engine `output_dir`, TUI settings) disables the organizing entirely. LaTeX project folders move as a unit; a lone `.tex` moves as a file.
- Archived **PDFs are compressed** best-effort after the move (ADR-0049): Ghostscript `-dPDFSETTINGS=/printer` (300 dpi), qpdf lossless fallback, original kept when compression fails or saves nothing. On by default; `PDF_COMPRESS=off` / TUI setting disable it. New module: `converter/compress.py`.
- A `.md` that received **model-produced content** is named `<stem>-inference-used.md` instead of `<stem>.md` (ADR-0050), so a deterministic re-run can never overwrite a file that cost inference time. Detection is content-based, not flag-based: the passes note what actually landed in a per-conversion ledger (`converter/inference.py`), covering vision, interpret, structure, an accepted format rewrite, and a model-written summary. The marker applies in every layout (beside-source, `-o`, engine uploads) and an existing marked file is never overwritten — the next run writes `-inference-used-2` unless `INFERENCE_OVERWRITE=on` / TUI setting opts into replacing it. The `source/` archive name stays unmarked; `ConvertResult.inference_passes` reports the contributing passes.
- A PDF converted with **EPUB export on** also writes `<stem>.epub` beside its Markdown (ADR-0067): `EPUB_OUTPUT=on` / TUI *epub export* / `ptm-engine --epub`. The EPUB is packaged with pandoc after the Markdown post-passes and the inference rename (so the marker propagates), embeds the figure images, carries PDF title/author/language metadata and a page-1 cover, and is additive — the `.md` is never deleted. Figure extraction is forced for the EPUB run, while page-render links keep the user's prior `IMAGE_LINKS` choice (`config.force_epub_images`), so an EPUB-only run writes no page PNGs; packaging failures only warn. A missing pandoc is reported and the Markdown still lands. PDF inputs only.
- One Markdown file is produced per source document. Image links are **off by default** (ADR-0047): no `assets/<name>/` sidecar and no `![...]`/`[Page N](...)` links — local-AI image descriptions are emitted in their place where available. `IMAGE_LINKS=on` (TUI setting) restores extraction + links, deduplicated by content hash; recurring images (logos/watermarks) are then inlined once, then hyperlinked.
- Both formats preserve **bold** and *italic*; output includes pipe tables, bullet/numbered lists, and per-slide/page headings carrying the slide or page number. PDF tables that carry no ruling lines (Word layout grids) are reconstructed from the text layer's content-stream order (`converter/borderless.py`, ADR-0054; disable with `PDF_BORDERLESS_TABLES=off`). In paper mode, paragraphs and headings are reconstructed from the document's **learned typography** (`converter/paragraphs.py`, ADR-0055): body size, line pitch, paragraph-gap threshold and display size tiers are profiled from the text layer, wrapped lines rejoin into one Markdown line per paragraph (across columns and pages), and headings promote through an evidence ladder (`##`/`###`/`####` from the tiers, a strict bold-only fallback at body size). `PDF_REFLOW=off` keeps the legacy line-per-line body output. PPTX also emits speaker notes as blockquotes; PDF renders a per-page PNG in memory for the vision passes (linked as visual ground truth only with `IMAGE_LINKS=on`).
- A LaTeX **project** is a folder containing `.tex` source; it converts to one `.md` named after the folder via pandoc (a required binary for `.tex`), with `\input`/`\include`/`\import` flattened, a `# Title` + `*Authors*` block (title from `\title`, else the title page's biggest font line, else the folder name), `##` sections with pandoc's `{#…}` attributes stripped, `\autoref`/`\ref`/`\eqref` rewritten to plain `Section 3` / `3` / `(1)` text from a deterministic source scan, and figures/tables as bold numbered `**Figure N: …**` / `**Table N: …**` captions (ADR-0056). The title page's other content survives as a **front block** (small-caps/`\large` lines joined with ` · `, `table` floats and bare `tabular`s as numbered pipe tables, `\IfFileExists` resolved — course, department, term, word count; ADR-0058), and float numbering counts what the compiled document counted, including title-page floats (ADR-0059). pandoc's literal escapes are undone outside math (`\~18/yr` → `~18/yr`, `\[TBD\]` → `[TBD]`; ADR-0058). TikZ/pgfplots figures render to PNG with `standalone` + pdflatex when `IMAGE_LINKS=on` (the project's own `\tikzset`/`\usetikzlibrary` is collected from its `.sty`/`.tex`; `LATEX_FIGURE_PREAMBLE=<file>` adds more); with no image written, the drawing's node/axis text is kept as a `*Figure content (from the TikZ source):*` note (ADR-0060); `LATEX_FIGURES=off` disables the pipeline (ADR-0057). The bibliography renders through a bundled author-year CSL close to biblatex's `authoryear` (source-case titles, publisher places, expanded en-dash ranges, visited dates) with case-protected copies of the project's `.bib`; `LATEX_CSL=<file.csl>` overrides the style (ADR-0061). Display math is `$$…$$` passthrough by default and a rendered PNG with `IMAGE_LINKS=on`; inline `$…$` is always passthrough. LaTeX runs **no AI passes except the presentation summary** (ADR-0038).
- Multi-column PDFs (whitepapers, academic papers) are linearized column-by-column automatically; opt-in **paper mode** (`PDF_MODE=paper` / TUI setting) renders them as one continuous document — a title block on page 1, tiered section headings (`##`/`###`/`####` from the document's learned size tiers, ADR-0055), **no per-page headings** (later-page content flows under the paper's own last section; ADR-0052) — with running headers stripped. Slide decks keep the per-page default (`PDF_MODE=slide`).
- TUI (`tui/`, `ptm-tui`) — native Go terminal app over the engine (ADR-0040/0053, Charm v2): fixed-frame shell (header · bounded jobs panel · `1` files / `2` results / `3` log tabs · help footer), discovery rows with kind/size/mtime and a recent section, `@` fuzzy filter, path-keyed multi-select, composite progress bars, cancel (`x`), results with open/reveal/copy and `E` to open an exported EPUB, scrollable colourised log, `a` **AI setup** control surface (toggle a pass, full description, resolved model states, start commands; ADR-0065/0066), `s` sectioned settings modal (output prefs incl. EPUB export, audio transcription), `?` help. The only user interface (ADR-0051).
- Engine (`engine.py` / `ptm-engine`) — local HTTP/WS backend the TUI spawns: filesystem access with metadata, conversion, transcription jobs, settings, and a **watch** subscription (`/ws {"type":"watch"}`) that pushes every job frame with `job_id`; `POST /api/jobs/cancel` stops a job at the next page/file boundary, `GET /api/runs` reports run history (ADR-0025/0045/0053). `converter` is imported once in `create_app` (concurrent lazy imports deadlocked under Flask's thread pool).
- Fleet model identity is flaate ADR-0007: `key` is the wire identity for start/status/log, `name` is the actual display name, and `tags` is the role/capability list. PtM resolves each role by tags to the first manifest match (ADR-0064/0065); losing matches are alternatives, not PtM servers. The static `SERVERS` catalog is only the offline fallback.
- Optional, all-off-by-default AI passes (Vision transcription, Image classifier, Diagram interpretation, Markdown restructure, Paper structure, Presentation summary) run against local OpenAI-compatible model servers. Conversion is fully deterministic without them. The labels are the user-facing vocabulary (ADR-0065); the env keys stay `VISION_*`/`FORMAT_*`/… The pass → model → start-command matrix is `docs/ai-requirements.md`, mirrored live by the TUI's `a` view.

## Commands

Setup:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .        # optional, installs the ptm-engine command
```

Run:

```bash
ptm-tui                             # native Go TUI (after scripts/install-global.sh)
scripts/ptm-tui.sh                  # build + run without the global install
ptm-engine --port 9091 --vision     # engine directly (the TUI spawns/attaches to it)
```

Test:

```bash
./.venv/bin/python -m pytest
```

No lint, typecheck, or formatter is configured (no ruff/black/mypy/pre-commit in the repo).

## Architecture

- `converter/` — conversion library, no UI dependencies
  - `__init__.py` — public API (`convert_file`, `convert_files`, `ConvertResult`) and extension-based dispatch
  - `base.py` — shared `Converter` interface, `ConverterRegistry`, and reusable Markdown helpers
  - `borderless.py` — deterministic borderless-table detection from the PDF text layer (ADR-0054)
  - `paragraphs.py` — paper-mode paragraph/heading model: learned typography profiler, paragraph segmentation, heading evidence ladder, run-level Markdown rendering (ADR-0055)
  - `pptx.py`, `pdf.py`, `latex.py` — the concrete converters (`latex.py` shells out to pandoc, ADR-0038, and renders TikZ/pgfplots figures itself, ADR-0056/0057)
  - `vision.py`, `classify.py`, `interpret.py`, `format.py`, `structure.py`, `summary.py` — optional AI post-passes
  - `organize.py` — the default output layout: kebab-case naming, the `source/` archive move, per-batch collision handling, `default_output_dir` (ADR-0048), and the `-inference-used` marker (ADR-0050)
  - `epub.py` — PDF → EPUB packaging of a converted Markdown document via pandoc (metadata, cover, figure images, sanitized page-break scaffolding; ADR-0067)
  - `compress.py` — best-effort PDF compression for archived sources (Ghostscript → qpdf fallback; ADR-0049)
  - `inference.py` — per-conversion ledger of the passes whose model output landed in the Markdown (ADR-0050)
  - `config.py` — runtime registry of AI feature toggles + local server catalog (ADR-0012), user-facing feature/role labels and the per-pass requirements projection (ADR-0065), plus the lazy `IMAGE_LINKS` (ADR-0047), `PDF_COMPRESS` (ADR-0049) and `EPUB_OUTPUT` (ADR-0067) output preferences and the context-local EPUB image override
  - `fleetview.py` — read-only fleet availability projection for the TUI: flaate `list`/`status/all`, resolved role → model rows, feature-centric requirements, ASR rows, and the static-probe fallback (ADR-0063/0064/0065)
  - `render.py` — LibreOffice + PyMuPDF rendering for PPTX charts
  - `logstore.py` — SQLite log of classifier/transcription decisions (`ptm.sqlite`)
  - `settings.py` — persistent app preferences + recent-files list (same DB as `logstore`)
  - `transcribe.py`, `audio.py` — standalone audio→text (fleet ASR via `transcribe_fleet`) and the audio-server client (diarization/enhancement/NB-Whisper)
- `engine.py` — `ptm-engine`: native HTTP/WS backend the TUI drives (filesystem with metadata, conversion, engine-hosted transcription, settings, watch/cancel/runs, fleet availability; ADR-0025/0045/0053/0063)
- `cli_common.py` — shared AI flag parser + env mapping (used by the engine)
- `tui/` — native Go TUI (`ptm-tui`) over the engine, in a separate Go module (Charm v2: Bubble Tea/Lip Gloss/Bubbles; ADR-0040/0053); one persistent `/ws` watch socket pushes job frames via `tea.Program.Send`, audio discovery via a `kinds=audio` glob, transcription as an engine-hosted job (ADR-0045)
- `lock.py` — single-instance `flock` guard for engine-hosted transcription
- `scripts/` — audio-model server, TUI launcher/installer, transcript QA harness, PDF corpus diff harness (`pdf-corpus-diff.py`, ADR-0055 review aid; `--copy-out` saves a run's outputs and report under the gitignored `artifacts/`)
- `docs/adr/` — architecture decision records (MADR form); `docs/ai-requirements.md` — pass → model → start-command matrix; `docs/ai-vision.md`, `docs/ai-audio.md`, `docs/runbook.md` — model-serving runbooks
- `tests/` — pytest suite with synthetic fixtures (`make_test_deck.py`, `make_test_pdf.py`)

## Conventions

- Python 3.10+ (`requires-python = ">=3.10"`).
- Every module starts with `from __future__ import annotations` and a module-level docstring; type hints use PEP 604 unions (`str | None`).
- New file formats subclass `Converter`, set `extensions`, implement `convert`, and are registered in `converter/__init__.py` via `registry.register(...)`. Project formats (a directory, not a file) additionally set `project_extensions` and a `project_entry` detector.
- Conversion is deterministic and never invokes audio transcription (that is an engine-hosted job, ADR-0009/0045).
- AI/audio configuration is read from environment variables at **import time**; `cli_common.apply_ai_env` must be called before importing `converter`. `cli_common` deliberately does not import `converter`.
- Exception to the import-time rule: the AI feature *on/off* toggles are runtime state owned by `converter.config` (`is_enabled`/`set_enabled`), seeded from the env at import; the engine/TUI flips them per conversion without a restart (ADR-0012). Endpoint/model env vars (`*_BASE_URL`/`*_MODEL`/`EMBED_*`) remain import-time.
- Exception to the import-time rule: the PDF layout (`PDF_MODE`), the image-link output preference (`IMAGE_LINKS`, ADR-0047), the PDF-compression toggle (`PDF_COMPRESS`, ADR-0049), the inference-overwrite opt-out (`INFERENCE_OVERWRITE`, ADR-0050), and the EPUB-export toggle (`EPUB_OUTPUT`, ADR-0067) are read lazily inside the converters/organizer, so the TUI settings can toggle them per conversion without a restart; the corresponding `ptm-engine` flags set them via `apply_ai_env`. An EPUB conversion additionally forces figure images on (page renders keep the user's prior setting) through a context-local override (`config.force_epub_images`), without mutating `IMAGE_LINKS`. Three more PDF output preferences are lazy as well but have no TUI row — set them via `ptm-engine --env`: `PDF_BORDERLESS_TABLES` (ADR-0054), `PDF_REFLOW` (ADR-0055), and `PDF_COMPRESS_PRESET`; `EPUB_LANG`/`EPUB_COVER` are the same kind of EPUB-only, TUI-less knobs. Three LaTeX output preferences are lazy and TUI-less for the same reason: `LATEX_FIGURES=off` disables the TikZ figure pipeline, `LATEX_FIGURE_PREAMBLE=<file.tex>` appends preamble for figure rendering (ADR-0057), and `LATEX_CSL=<file.csl>` replaces the bundled bibliography style (ADR-0061).
- `converter` stays MLX- and PyTorch-free: ASR is an HTTP call to the fleet `whisper` daemon (`whisper-large-v3-asr-fp16`/`whisper-large-v3-turbo-asr-fp16`, OpenAI multipart with `verbose_json`) or the audio server (`nb-whisper-large`, `/v1/asr`); ffmpeg remains a subprocess. There is no `mlx_whisper` lane (ADR-0062).
- Tests use `pytest` and `tmp_path`; `conftest.py` isolates `PTM_STATE_DIR` to a temp dir so tests never touch the real state dir.
- `ptm.sqlite` and `.env` are gitignored.

## Gotchas

- `apply_ai_env(args)` (or equivalent env-var setup) must run before any `import converter`; otherwise AI flags are silently ignored.
- ASR needs the fleet daemons: start the whisper daemon with `curl -X POST http://127.0.0.1:9300/start/whisper-large-v3-asr-fp16` (model key, not daemon name) and the audio server for NB-Whisper/diarization. `AUDIO_MODEL` takes the full fleet keys; the short `whisper-large-v3` names and old mlx-whisper HF ids are aliased. `AUDIO_MLX_WHISPER_BIN`/`AUDIO_CONDITION_ON_PREVIOUS_TEXT` no longer exist — the prompting knob is the daemon's `WHISPER_CONDITION_ON_PREVIOUS_TEXT`.
- `requirements-audio.txt` targets an isolated Python 3.11 venv (deepfilternet has no cp312 wheel); pins are interdependent — see `docs/runbook.md` §2.
- PyMuPDF is AGPL-3.0 (or commercial) licensed — review before distributing.
- pandoc is a **required** external binary for `.tex` input (`brew install pandoc`) and for PDF → EPUB export (`--epub` / `EPUB_OUTPUT=on`); a missing binary makes the PDF export warn and keeps the Markdown. `pdflatex` is required for `.tex` display-math rendering and TikZ/pgfplots figures with `IMAGE_LINKS=on` (both degrade to text with a warning). Figure rendering puts the project directory on `TEXINPUTS` and never passes `-shell-escape`, so a project's `\write18` cannot execute. Paths `PANDOC_PATH`/`PDLATEX_PATH` override the binaries.
- `gs` (Ghostscript) and `qpdf` are **optional** binaries for archiving: Ghostscript compresses moved PDFs, qpdf is the lossless fallback, and when neither is installed the move still succeeds with a warning. Paths `GS_PATH`/`QPDF_PATH` override the binaries; `PDF_COMPRESS_PRESET` overrides the gs preset (`/printer` default).
- A folder literally named `source` is treated as an archive: recursive scans skip it and a source already inside one is not moved again. Re-convert one explicitly by selecting the file itself (or the `source/` folder) rather than scanning its parent.
- Adding a format: subclass `Converter`, register it in `converter/__init__.py`, then update `_SUPPORTED_EXTENSIONS` in `engine.py` if the TUI should list it.
