# 0001. Add `ptm` and `ptm-start` console-script entry points

- Status: Accepted
- Date: 2026-08-26

## Context

The application had two interfaces: a PySide6 desktop GUI (`main.py` → `gui.py`)
and an importable `converter` library. There was no command-line interface: to
launch the GUI with AI passes enabled you had to prefix a long chain of
environment variables (`VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 ... ./python
main.py`), and there was no way to batch-convert from a script or cron.

Two distinct needs:

1. A simple way to *start the app* with the different AI capabilities.
2. A way to *leverage the GUI's conversion functionality* headlessly.

## Decision

Add two console-script entry points, installed via `pyproject.toml`
(`[project.scripts]`, `pip install -e .`):

- **`ptm-start`** (`start:main`) — launches the GUI, optionally enabling AI
  passes via flags.
- **`ptm`** (`cli:main`) — headless batch conversion, mirroring the GUI.

We chose two separate commands over a single `ptm start` / `ptm convert`
subcommand CLI because the two uses are distinct (launch an interactive window
vs. run a conversion) and two flat commands keep `--help` focused. Both share
one flag parser (`cli_common.py`).

The CLI code lives at the repository root (`cli.py`, `start.py`,
`cli_common.py`), **outside** the `converter` package, because:

- `converter` must stay UI-free (the library has no `PySide6` dependency);
- `ptm` (headless convert) must not import `PySide6` at all, so it stays
  runnable in contexts without a display;
- `ptm-start` must import `gui` (and thus `PySide6`), which is a UI concern.

## Consequences

- `pip install -e .` exposes `ptm` and `ptm-start` on `PATH`.
- `ptm` pulls only the `converter` dependency chain; `ptm-start` adds `PySide6`.
- The old `./.venv/bin/python main.py` entry point is unchanged and still works.
- The `ptm` namespace prefix avoids collisions with generic names like `start`.

## Alternatives considered

- **Shell wrappers** exporting env vars — simpler, but non-portable and hard to
  test; Python entry points are testable and share the existing interpreter.
- **A single subcommand CLI** — cleaner namespace, but muddies `--help` and the
  two use cases; rejected in favour of two flat commands.
- **Living inside `converter`** — would force `PySide6` (or an import guard) into
  the library, breaking the UI-free contract.
