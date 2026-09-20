# 0040. Native Go TUI (`ptm-tui`) over the Python engine

- Status: Accepted
- Date: 2026-09-20
- Supersedes: [0036](0036-terminal-tui.md)

## Context

ADR-0036 specified a terminal UI, `ptm-tui`, on **Textual** (Python), running
conversion **in-process** on a worker thread. It was accepted but never built
(no `tui.py`, no `textual` dependency, no `ptm-tui` console script). Before
implementation began, the desire was restated as a **native app** — an
opencode-style surface, a compiled binary with `@file` discovery scoped to the
current directory — which Textual does not satisfy (it is a Python runtime, not
a compiled binary, and needs the venv).

The conversion core is and stays Python (`converter/`). Separately, the project
already ships a **native engine sidecar** (`engine.py` / `ptm-engine`,
ADR-0025) that exposes everything a native TUI needs over HTTP + WebSocket:
filesystem discovery (`/api/fs/list`, `/api/fs/glob` — the latter reuses
`converter.collect_inputs`, so LaTeX-project folders are yielded as single
inputs), configuration read/write (`/api/config`), recent files
(`/api/recent`), model health (`/api/health/servers`), and live conversion
(`/ws` with per-file/per-page progress plus `log`/`done` events).

Go 1.26 is available on the target machine. This re-opens ADR-0036's framework
decision with a new option — a Go TUI as a native client to the existing engine.

## Decision

Build `ptm-tui` as a **Go (Bubble Tea + Charm)** terminal app in a new
monorepo `tui/` module (`github.com/Snorre98/PresentationToMarkdown/tui`). It is
a **client to `ptm-engine`**, not a converter: conversion stays in Python, the
TUI owns presentation, discovery, and control. ADR-0036's product goals are
kept (cwd-scoped, `@` fuzzy selector, global launcher, settings parity); its
*architecture* (in-process worker) and *framework* (Textual) are replaced.

### Responsibilities

1. **Engine lifecycle — TUI manages it.** On start the TUI health-checks
   `:9091/api/health`; if down, it spawns `ptm-engine` via the venv-resolved
   `PTM_ENGINE_CMD` (with `--port 9091`) and polls until ready. If it spawned
   the process it calls `POST /api/shutdown` on exit; if it attached to a
   running engine it leaves it running.

2. **`@` discovery — delegated.** On launch the TUI calls
   `/api/fs/glob?path=<cwd>` and renders the results relative to cwd. `@`
   focuses a fuzzy filter (a small subsequence matcher over the relative
   paths); Space multi-selects; Enter dispatches conversion for `.pptx`/`.pdf`/
   `.tex` (and LaTeX-project folders).

3. **Conversion — WS streaming.** Open `/ws`, send
   `{"type":"start","paths":[…],"output_dir":…,"duplicate":…}`, and render the
   `file`/`page`/`log`/`done` events into a progress pane with per-file
   `[OK]`/`[ERR]`/`[WARN]` results and "open in Finder" via `/api/fs/open`.

4. **Settings — engine-backed.** `GET /api/config` feeds a toggle list (AI
   features, `pdf_mode`, duplicate, output dir); `POST /api/config` persists
   changes, so the TUI, GUI, and web surfaces stay in sync through
   `converter.settings`.

### Distribution

`go build` produces a single `bin/ptm-tui` binary. `scripts/ptm-tui.sh` (the
`scripts/ptm-start.sh` pattern) resolves the repo venv, bootstraps it if
missing, sets `PTM_ENGINE_CMD="$VENV/bin/ptm-engine"`, and execs the binary.
`scripts/install-global.sh` writes `~/.local/bin/ptm-tui` → that wrapper. A
macOS `.app` bundle / `brew` formula is deferred.

## Consequences

- **+** A compiled, opencode-like native surface; no venv activation, no
  Python runtime needed to *run the UI* (only to run the engine it drives).
- **+** No conversion logic is duplicated in Go: file discovery, LaTeX-project
  detection, config, and progress all come from the engine.
- **+** The browser↔filesystem impedance (ADR-0028) is gone for this surface by
  construction: the TUI passes real local paths to the engine.
- **−** A second language in the monorepo (Go) and a new build step; the engine
  must be reachable for the TUI to be useful (it auto-spawns, so this is only a
  startup cost).
- **−** MVP excludes transcription and history/RAG; those are follow-on work
  (spawn `ptm-transcribe`; add a read-only `/api/history` to the engine).

## Alternatives considered

- **Textual (Python) TUI — ADR-0036 as written** — no new toolchain, in-process
  conversion. Rejected: not a compiled native binary, which is the stated
  requirement.
- **Go TUI shelling out to `ptm` per file** — stateless, but coarser progress
  and it would re-implement file discovery (and LaTeX-project detection) in Go,
  duplicating `collect_inputs`. Rejected in favour of the engine sidecar.
- **Full conversion in Go** — a re-implementation of `converter/`; out of scope
  and would fork the pipeline. Rejected.
- **Reuse the PySide6 GUI / web dashboard as the terminal surface** — neither is
  a terminal; the TUI is a new interaction model. Rejected.
