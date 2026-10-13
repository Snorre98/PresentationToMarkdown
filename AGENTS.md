# AGENTS.md

## Project

Native terminal app and reusable Python library that converts PowerPoint (`.pptx`), PDF (`.pdf`), and LaTeX projects into Markdown.

- The importable library lives in the `converter/` package and has no UI dependencies.
- **Default output layout (ADR-0048)**: no explicit output folder → the `.md` is written **beside the source** under a kebab-case name normalized from it, and the converted source is moved into a sibling `source/` folder with the same name (`03 What makes things fun.pdf` → `03-what-makes-things-fun.md` + `source/03-what-makes-things-fun.pdf`). Files already in a `source/` folder are not moved again (re-runs are idempotent), recursive folder scans skip `source/` archives, normalized-name collisions in one batch get a `-2` suffix + warning, and an explicit output folder (engine `output_dir`, TUI settings) disables the organizing entirely. LaTeX project folders move as a unit; a lone `.tex` moves as a file.
- Archived **PDFs are compressed** best-effort after the move (ADR-0049): Ghostscript `-dPDFSETTINGS=/printer` (300 dpi), qpdf lossless fallback, original kept when compression fails or saves nothing. On by default; `PDF_COMPRESS=off` / TUI setting disable it. New module: `converter/compress.py`.
- A `.md` that received **model-produced content** is named `<stem>-inference-used.md` instead of `<stem>.md` (ADR-0050), so a deterministic re-run can never overwrite a file that cost inference time. Detection is content-based, not flag-based: the passes note what actually landed in a per-conversion ledger (`converter/inference.py`), covering vision, interpret, structure, an accepted format rewrite, and a model-written summary. The marker applies in every layout (beside-source, `-o`, engine uploads) and an existing marked file is never overwritten — the next run writes `-inference-used-2` unless `INFERENCE_OVERWRITE=on` / TUI setting opts into replacing it. The `source/` archive name stays unmarked; `ConvertResult.inference_passes` reports the contributing passes.
- One Markdown file is produced per source document. Image links are **off by default** (ADR-0047): no `assets/<name>/` sidecar and no `![...]`/`[Page N](...)` links — local-AI image descriptions are emitted in their place where available. `IMAGE_LINKS=on` (TUI setting) restores extraction + links, deduplicated by content hash; recurring images (logos/watermarks) are then inlined once, then hyperlinked.
- Both formats preserve **bold** and *italic*; output includes pipe tables, bullet/numbered lists, and per-slide/page headings carrying the slide or page number. PDF tables that carry no ruling lines (Word layout grids) are reconstructed from the text layer's content-stream order (`converter/borderless.py`, ADR-0054; disable with `PDF_BORDERLESS_TABLES=off`). PPTX also emits speaker notes as blockquotes; PDF renders a per-page PNG in memory for the vision passes (linked as visual ground truth only with `IMAGE_LINKS=on`).
- A LaTeX **project** is a folder containing `.tex` source; it converts to one `.md` named after the folder via pandoc (a required binary for `.tex`), with `\input`/`\include`/`\import` flattened, a `# Title` + `*Authors*` block, `##` sections, figures dropped by default, display math left as `$$…$$` passthrough, and inline `$…$` left as passthrough. With `IMAGE_LINKS=on`: content-hash-deduped figures plus display math rendered to PNG (pdflatex). LaTeX runs **no AI passes except the RAG summary** (ADR-0038).
- Multi-column PDFs (whitepapers, academic papers) are linearized column-by-column automatically; opt-in **paper mode** (`PDF_MODE=paper` / TUI setting) renders them as one continuous document — a title/authors block on page 1, `##` section headings, **no per-page headings** (later-page content flows under the paper's own last section; ADR-0052) — with running headers stripped. Slide decks keep the per-page default (`PDF_MODE=slide`).
- TUI (`tui/`, `ptm-tui`) — native Go terminal app over the engine (ADR-0040/0053, Charm v2): fixed-frame shell (header · bounded jobs panel · `1` files / `2` results / `3` log tabs · help footer), discovery rows with kind/size/mtime and a recent section, `@` fuzzy filter, path-keyed multi-select, composite progress bars, cancel (`x`), results with open/reveal/copy, scrollable colourised log, `s` settings modal (features, output prefs, transcription, live server health), `?` help. The only user interface (ADR-0051).
- Engine (`engine.py` / `ptm-engine`) — local HTTP/WS backend the TUI spawns: filesystem access with metadata, conversion, transcription jobs, settings, and a **watch** subscription (`/ws {"type":"watch"}`) that pushes every job frame with `job_id`; `POST /api/jobs/cancel` stops a job at the next page/file boundary, `GET /api/runs` reports run history (ADR-0025/0045/0053). `converter` is imported once in `create_app` (concurrent lazy imports deadlocked under Flask's thread pool).
- Optional, all-off-by-default AI passes (vision transcription, classifier gate, diagram interpretation, LLM markdown restructure, paper structure tagging/reword, per-presentation RAG summary) run against local OpenAI-compatible model servers. Conversion is fully deterministic without them.

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
  - `pptx.py`, `pdf.py`, `latex.py` — the concrete converters (`latex.py` shells out to pandoc, ADR-0038)
  - `vision.py`, `classify.py`, `interpret.py`, `format.py`, `structure.py`, `summary.py` — optional AI post-passes
  - `organize.py` — the default output layout: kebab-case naming, the `source/` archive move, per-batch collision handling, `default_output_dir` (ADR-0048), and the `-inference-used` marker (ADR-0050)
  - `compress.py` — best-effort PDF compression for archived sources (Ghostscript → qpdf fallback; ADR-0049)
  - `inference.py` — per-conversion ledger of the passes whose model output landed in the Markdown (ADR-0050)
  - `config.py` — runtime registry of AI feature toggles + local server catalog (ADR-0012) + the lazy `IMAGE_LINKS` (ADR-0047) and `PDF_COMPRESS` (ADR-0049) output preferences
  - `render.py` — LibreOffice + PyMuPDF rendering for PPTX charts
  - `logstore.py` — SQLite log of classifier/transcription decisions (`ptm.sqlite`)
  - `settings.py` — persistent app preferences + recent-files list (same DB as `logstore`)
  - `transcribe.py`, `audio.py` — standalone audio→text and speaker-diarization client
- `engine.py` — `ptm-engine`: native HTTP/WS backend the TUI drives (filesystem with metadata, conversion, engine-hosted transcription, settings, watch/cancel/runs; ADR-0025/0045/0053)
- `cli_common.py` — shared AI flag parser + env mapping (used by the engine)
- `tui/` — native Go TUI (`ptm-tui`) over the engine, in a separate Go module (Charm v2: Bubble Tea/Lip Gloss/Bubbles; ADR-0040/0053); one persistent `/ws` watch socket pushes job frames via `tea.Program.Send`, audio discovery via a `kinds=audio` glob, transcription as an engine-hosted job (ADR-0045)
- `lock.py` — single-instance `flock` guard for engine-hosted transcription
- `scripts/` — audio-model server, TUI launcher/installer, transcript QA harness
- `docs/adr/` — architecture decision records (MADR form); `docs/ai-vision.md`, `docs/ai-audio.md`, `docs/runbook.md` — model-serving runbooks
- `tests/` — pytest suite with synthetic fixtures (`make_test_deck.py`, `make_test_pdf.py`)

## Conventions

- Python 3.10+ (`requires-python = ">=3.10"`).
- Every module starts with `from __future__ import annotations` and a module-level docstring; type hints use PEP 604 unions (`str | None`).
- New file formats subclass `Converter`, set `extensions`, implement `convert`, and are registered in `converter/__init__.py` via `registry.register(...)`. Project formats (a directory, not a file) additionally set `project_extensions` and a `project_entry` detector.
- Conversion is deterministic and never invokes audio transcription (that is an engine-hosted job, ADR-0009/0045).
- AI/audio configuration is read from environment variables at **import time**; `cli_common.apply_ai_env` must be called before importing `converter`. `cli_common` deliberately does not import `converter`.
- Exception to the import-time rule: the AI feature *on/off* toggles are runtime state owned by `converter.config` (`is_enabled`/`set_enabled`), seeded from the env at import; the engine/TUI flips them per conversion without a restart (ADR-0012). Endpoint/model env vars (`*_BASE_URL`/`*_MODEL`/`EMBED_*`) remain import-time.
- Exception to the import-time rule: the PDF layout (`PDF_MODE`), the image-link output preference (`IMAGE_LINKS`, ADR-0047), the PDF-compression toggle (`PDF_COMPRESS`, ADR-0049), and the inference-overwrite opt-out (`INFERENCE_OVERWRITE`, ADR-0050) are read lazily inside the converters/organizer, so the TUI settings can toggle them per conversion without a restart; the corresponding `ptm-engine` flags set them via `apply_ai_env`. The borderless-table toggle (`PDF_BORDERLESS_TABLES`, ADR-0054) is lazy as well but has no TUI row — set it via `ptm-engine --env`.
- `converter` stays MLX-free: transcription runs `mlx_whisper`/`ffmpeg` as subprocesses.
- Tests use `pytest` and `tmp_path`; `conftest.py` isolates `PTM_STATE_DIR` to a temp dir so tests never touch the real state dir.
- `ptm.sqlite` and `.env` are gitignored.

## Gotchas

- `apply_ai_env(args)` (or equivalent env-var setup) must run before any `import converter`; otherwise AI flags are silently ignored.
- `requirements-audio.txt` targets an isolated Python 3.11 venv (deepfilternet has no cp312 wheel); pins are interdependent — see `docs/runbook.md` §2.
- PyMuPDF is AGPL-3.0 (or commercial) licensed — review before distributing.
- pandoc is a **required** external binary for `.tex` input (`brew install pandoc`); `pdflatex` is required only for legacy (`IMAGE_LINKS=on`) display-math rendering. Paths `PANDOC_PATH`/`PDLATEX_PATH` override the binaries.
- `gs` (Ghostscript) and `qpdf` are **optional** binaries for archiving: Ghostscript compresses moved PDFs, qpdf is the lossless fallback, and when neither is installed the move still succeeds with a warning. Paths `GS_PATH`/`QPDF_PATH` override the binaries; `PDF_COMPRESS_PRESET` overrides the gs preset (`/printer` default).
- A folder literally named `source` is treated as an archive: recursive scans skip it and a source already inside one is not moved again. Re-convert one explicitly by selecting the file itself (or the `source/` folder) rather than scanning its parent.
- Adding a format: subclass `Converter`, register it in `converter/__init__.py`, then update `_SUPPORTED_EXTENSIONS` in `engine.py` if the TUI should list it.
