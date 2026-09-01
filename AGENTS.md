# AGENTS.md

## Project

Desktop app and reusable Python library that converts PowerPoint (`.pptx`) and PDF (`.pdf`) documents into Markdown.

- The importable library lives in the `converter/` package and has no UI dependencies.
- One Markdown file is produced per source document, with images extracted to a sidecar `assets/<name>/` folder and deduplicated by content hash. Recurring images (logos/watermarks) are inlined once, then hyperlinked.
- Both formats preserve **bold** and *italic*; output includes pipe tables, bullet/numbered lists, and per-slide/page headings carrying the slide or page number. PPTX also emits speaker notes as blockquotes; PDF also links a per-page rendered PNG as visual ground truth.
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
ptm-start --all                     # GUI with AI flags
ptm-transcribe deck.md              # audio -> Markdown transcription
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
  - `pptx.py`, `pdf.py` — the two concrete converters
  - `vision.py`, `classify.py`, `interpret.py`, `format.py`, `structure.py`, `summary.py` — optional AI post-passes
  - `render.py` — LibreOffice + PyMuPDF rendering for PPTX charts
  - `logstore.py` — SQLite log of classifier/transcription decisions (`ptm.sqlite`)
  - `settings.py` — persistent app preferences + recent-files list (same DB as `logstore`)
  - `transcribe.py`, `audio.py` — standalone audio→text and speaker-diarization client
- `main.py` — GUI entry point; `gui.py` — PySide6 interface
- `cli.py` — `ptm` headless batch converter; `start.py` — `ptm-start` GUI launcher
- `cli_transcribe.py` — `ptm-transcribe`; `cli_common.py` — shared AI flag parser + env mapping
- `lock.py` — single-instance `flock` guard for transcription
- `scripts/` — audio-model server, plus shell wrappers around `ptm-start`/`ptm-transcribe`
- `docs/adr/` — architecture decision records (MADR form); `docs/ai-vision.md`, `docs/ai-audio.md`, `docs/runbook.md` — model-serving runbooks
- `tests/` — pytest suite with synthetic fixtures (`make_test_deck.py`, `make_test_pdf.py`)

## Conventions

- Python 3.10+ (`requires-python = ">=3.10"`).
- Every module starts with `from __future__ import annotations` and a module-level docstring; type hints use PEP 604 unions (`str | None`).
- New file formats subclass `Converter`, set `extensions`, implement `convert`, and are registered in `converter/__init__.py` via `registry.register(...)`.
- Conversion is deterministic and never invokes audio transcription (that is the separate `ptm-transcribe` command, ADR-0009).
- AI/audio configuration is read from environment variables at **import time**; `cli_common.apply_ai_env` must be called before importing `converter` or `gui`. `cli_common` deliberately does not import `converter`.
- Exception to the import-time rule: the PDF layout (`PDF_MODE`) is read lazily inside `PDFConverter.convert`, so the GUI checkbox can toggle it per conversion without a restart; `--paper`/`--slide` set it via `apply_ai_env`.
- `converter` stays MLX-free: transcription runs `mlx_whisper`/`ffmpeg` as subprocesses.
- Tests use `pytest` and `tmp_path`; `conftest.py` isolates `PTM_STATE_DIR` to a temp dir so tests never touch the real state dir.
- `ptm.sqlite` and `.env` are gitignored.

## Gotchas

- `apply_ai_env(args)` (or equivalent env-var setup) must run before any `import converter` / `import gui`; otherwise AI flags are silently ignored.
- `requirements-audio.txt` targets an isolated Python 3.11 venv (deepfilternet has no cp312 wheel); pins are interdependent — see `docs/runbook.md` §2.
- PyMuPDF is AGPL-3.0 (or commercial) licensed — review before distributing.
- Adding a format: subclass `Converter`, register it in `converter/__init__.py`, then update the GUI file filter in `gui.py` if it should be listed.
