# AGENTS.md

## Project

Desktop app and reusable Python library that converts PowerPoint (`.pptx`), PDF (`.pdf`), and LaTeX projects into Markdown.

- The importable library lives in the `converter/` package and has no UI dependencies.
- **Default output layout (ADR-0048)**: no explicit output folder → the `.md` is written **beside the source** under a kebab-case name normalized from it, and the converted source is moved into a sibling `source/` folder with the same name (`03 What makes things fun.pdf` → `03-what-makes-things-fun.md` + `source/03-what-makes-things-fun.pdf`). Files already in a `source/` folder are not moved again (re-runs are idempotent), recursive folder scans skip `source/` archives, normalized-name collisions in one batch get a `-2` suffix + warning, and an explicit output folder (`ptm -o`, GUI/Web field) disables the organizing entirely. LaTeX project folders move as a unit; a lone `.tex` moves as a file.
- Archived **PDFs are compressed** best-effort after the move (ADR-0049): Ghostscript `-dPDFSETTINGS=/printer` (300 dpi), qpdf lossless fallback, original kept when compression fails or saves nothing. On by default; `--no-compress` / `PDF_COMPRESS=off` / GUI/web checkbox / TUI setting disable it. New module: `converter/compress.py`.
- One Markdown file is produced per source document. Image links are **off by default** (ADR-0047): no `assets/<name>/` sidecar and no `![...]`/`[Page N](...)` links — local-AI image descriptions are emitted in their place where available. `--image-links` (env `IMAGE_LINKS=on`, GUI/web checkbox) restores extraction + links, deduplicated by content hash; recurring images (logos/watermarks) are then inlined once, then hyperlinked.
- Both formats preserve **bold** and *italic*; output includes pipe tables, bullet/numbered lists, and per-slide/page headings carrying the slide or page number. PPTX also emits speaker notes as blockquotes; PDF renders a per-page PNG in memory for the vision passes (linked as visual ground truth only with `IMAGE_LINKS=on`).
- A LaTeX **project** is a folder containing `.tex` source; it converts to one `.md` named after the folder via pandoc (a required binary for `.tex`), with `\input`/`\include`/`\import` flattened, a `# Title` + `*Authors*` block, `##` sections, figures dropped by default, display math left as `$$…$$` passthrough, and inline `$…$` left as passthrough. With `IMAGE_LINKS=on`: content-hash-deduped figures plus display math rendered to PNG (pdflatex). LaTeX runs **no AI passes except the RAG summary** (ADR-0038).
- Multi-column PDFs (whitepapers, academic papers) are linearized column-by-column automatically; opt-in **paper mode** (`--paper` / `PDF_MODE=paper` / GUI checkbox) renders them as continuous documents with a title/authors block, `##` section headings, and stripped running headers. Slide decks keep the per-page default (`--slide`).
- GUI (`gui.py`, `main.py`, `ptm-start`) — drag-and-drop, batch convert, background-thread progress, per-file `[OK]`/`[ERR]`/`[WARN]` log.
- CLI (`cli.py` / `ptm`) — headless batch conversion mirroring the GUI; `cli_transcribe.py` / `ptm-transcribe` — decoupled local audio→Markdown transcription.
- Optional, all-off-by-default AI passes (vision transcription, classifier gate, diagram interpretation, LLM markdown restructure, paper structure tagging/reword, per-presentation RAG summary) run against local OpenAI-compatible model servers. Conversion is fully deterministic without them.

## Commands

Setup:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .        # optional, installs the ptm/ptm-start/ptm-transcribe commands
```

Run:

```bash
./.venv/bin/python main.py          # GUI
ptm deck.pptx handout.pdf           # headless conversion (after pip install -e .)
ptm --output out/ --vision .
ptm --paper whitepaper.pdf          # two-column paper -> continuous document
ptm --image-links deck.pptx         # legacy: extract assets + emit image/page links
ptm --no-compress deck.pdf          # skip Ghostscript compression of the archived PDF
ptm-start --all                     # GUI with AI flags
ptm-transcribe deck.md              # audio -> Markdown transcription
ptm-ab compare --format deck.pdf    # A/B the pre-flight need-check gate (ADR-0039)
ptm-ab shadow --paper --structure paper.pdf
ptm-tui                             # native Go TUI (after scripts/install-global.sh)
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
  - `pptx.py`, `pdf.py`, `latex.py` — the concrete converters (`latex.py` shells out to pandoc, ADR-0038)
  - `vision.py`, `classify.py`, `interpret.py`, `format.py`, `structure.py`, `summary.py` — optional AI post-passes
  - `organize.py` — the default output layout: kebab-case naming, the `source/` archive move, per-batch collision handling, `default_output_dir` (ADR-0048)
  - `compress.py` — best-effort PDF compression for archived sources (Ghostscript → qpdf fallback; ADR-0049)
  - `config.py` — runtime registry of AI feature toggles + local server catalog (ADR-0012) + the lazy `IMAGE_LINKS` (ADR-0047) and `PDF_COMPRESS` (ADR-0049) output preferences
  - `render.py` — LibreOffice + PyMuPDF rendering for PPTX charts
  - `logstore.py` — SQLite log of classifier/transcription decisions (`ptm.sqlite`)
  - `settings.py` — persistent app preferences + recent-files list (same DB as `logstore`)
  - `transcribe.py`, `audio.py` — standalone audio→text and speaker-diarization client
- `main.py` — GUI entry point; `gui.py` — PySide6 interface
- `cli.py` — `ptm` headless batch converter; `start.py` — `ptm-start` GUI launcher
- `cli_transcribe.py` — `ptm-transcribe`; `cli_common.py` — shared AI flag parser + env mapping
- `cli_ab.py` — `ptm-ab` A/B harness for the need-check gate (ADR-0039)
- `tui/` — native Go TUI (`ptm-tui`) over the engine, in a separate Go module (ADR-0040); audio discovery via `kinds=audio` glob and transcription by spawning `ptm-transcribe` (ADR-0041)
- `lock.py` — single-instance `flock` guard for transcription
- `scripts/` — audio-model server, plus shell wrappers around `ptm-start`/`ptm-transcribe`
- `docs/adr/` — architecture decision records (MADR form); `docs/ai-vision.md`, `docs/ai-audio.md`, `docs/runbook.md` — model-serving runbooks
- `tests/` — pytest suite with synthetic fixtures (`make_test_deck.py`, `make_test_pdf.py`)

## Conventions

- Python 3.10+ (`requires-python = ">=3.10"`).
- Every module starts with `from __future__ import annotations` and a module-level docstring; type hints use PEP 604 unions (`str | None`).
- New file formats subclass `Converter`, set `extensions`, implement `convert`, and are registered in `converter/__init__.py` via `registry.register(...)`. Project formats (a directory, not a file) additionally set `project_extensions` and a `project_entry` detector.
- Conversion is deterministic and never invokes audio transcription (that is the separate `ptm-transcribe` command, ADR-0009).
- AI/audio configuration is read from environment variables at **import time**; `cli_common.apply_ai_env` must be called before importing `converter` or `gui`. `cli_common` deliberately does not import `converter`.
- Exception to the import-time rule: the AI feature *on/off* toggles are runtime state owned by `converter.config` (`is_enabled`/`set_enabled`), seeded from the env at import; the GUI flips them per conversion without a restart (ADR-0012). Endpoint/model env vars (`*_BASE_URL`/`*_MODEL`/`EMBED_*`) remain import-time.
- Exception to the import-time rule: the PDF layout (`PDF_MODE`), the image-link output preference (`IMAGE_LINKS`, ADR-0047), and the PDF-compression toggle (`PDF_COMPRESS`, ADR-0049) are read lazily inside the converters/organizer, so the GUI checkbox can toggle them per conversion without a restart; `--paper`/`--slide`/`--image-links`/`--no-compress` set them via `apply_ai_env`.
- `converter` stays MLX-free: transcription runs `mlx_whisper`/`ffmpeg` as subprocesses.
- Tests use `pytest` and `tmp_path`; `conftest.py` isolates `PTM_STATE_DIR` to a temp dir so tests never touch the real state dir.
- `ptm.sqlite` and `.env` are gitignored.

## Gotchas

- `apply_ai_env(args)` (or equivalent env-var setup) must run before any `import converter` / `import gui`; otherwise AI flags are silently ignored.
- `requirements-audio.txt` targets an isolated Python 3.11 venv (deepfilternet has no cp312 wheel); pins are interdependent — see `docs/runbook.md` §2.
- PyMuPDF is AGPL-3.0 (or commercial) licensed — review before distributing.
- pandoc is a **required** external binary for `.tex` input (`brew install pandoc`); `pdflatex` is required only for legacy (`IMAGE_LINKS=on`) display-math rendering. Paths `PANDOC_PATH`/`PDLATEX_PATH` override the binaries.
- `gs` (Ghostscript) and `qpdf` are **optional** binaries for archiving: Ghostscript compresses moved PDFs, qpdf is the lossless fallback, and when neither is installed the move still succeeds with a warning. Paths `GS_PATH`/`QPDF_PATH` override the binaries; `PDF_COMPRESS_PRESET` overrides the gs preset (`/printer` default).
- A folder literally named `source` is treated as an archive: recursive scans skip it and a source already inside one is not moved again. Re-convert one explicitly by pointing `ptm` at the file or the `source/` folder.
- Adding a format: subclass `Converter`, register it in `converter/__init__.py`, then update the GUI file filter in `gui.py` if it should be listed.
