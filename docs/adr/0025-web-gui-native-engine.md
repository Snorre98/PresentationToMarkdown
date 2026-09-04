# 0025. Web GUI with a native engine process

- Status: Accepted
- Date: 2026-09-04

## Context

The app has three user surfaces: a PySide6 desktop GUI (`gui.py`/`main.py`/
`ptm-start`), a headless CLI (`ptm`), and the read-only Flask dashboard
(ADR-0022). The dashboard already renders the conversion log — but it cannot
*start* a conversion, and the desktop GUI cannot be opened headlessly or on a
remote machine. With the run/phase telemetry (ADR-0022) now living in `ptm.sqlite`,
the natural next step is to make the web surface a full **converter**, not just
an observer.

Porting the desktop GUI to the web has one hard constraint that browsers cannot
do alone: **native filesystem access**. The GUI's workflow depends on
`QFileDialog` (file *and* folder pickers, recursive folder scan), and on
`QDesktopServices.openUrl` to open the output folder in Finder. A browser only
gets file drag-drop and (at best) directory-upload input; it cannot enumerate
directories, run `rglob`, or launch native apps.

## Decision

Introduce a **native engine process** that the web app drives over localhost,
keeping the PySide6 GUI as a supported fallback rather than replacing it.

### Process split

- **Web UI process** — a Flask app (extends the ADR-0022 `dashboard/` package).
  Serves the single-page frontend, proxies control-plane calls to the engine, and
  relays job progress over a WebSocket.
- **Engine process** — a UI-free Python process (`engine.py` / `ptm-engine`) that
  owns conversion, filesystem access, native "open", server preflight, and
  settings persistence. It is the **sole writer** to `ptm.sqlite` (WAL), so the
  web UI stays read-only against the log (ADR-0014 preserved for the *observer*),
  and the engine's single-conversion-at-a-time rule keeps the process-global
  `config._state` and `logstore` connection races impossible.

### Engine responsibilities (mapped 1:1 from the desktop GUI)

- **Run conversions** — `convert_files` on a worker thread; per-file and
  per-page progress forwarded as WebSocket frames (`ADR-0013` callback shapes).
- **Filesystem browse + glob** — directory listing, path resolution, recursive
  `rglob` (the `Add Folder...` equivalent), and a native `open` (the
  `QDesktopServices.openUrl` equivalent).
- **Server preflight + probe** — `config.missing_servers()`/`probe()` and the
  "block until up" gate, owned by the engine (not the browser).
- **Settings persistence** — `settings.get_setting`/`set_setting`/`record_recent`
  /`recent_files` (the same `ptm.sqlite` the log uses).

### Engine lifecycle

- The web UI shows an **engine status pill** plus a **Start engine** button.
- `POST /api/engine/start` spawns the engine as a child of the UI process, applying
  AI environment variables *before* importing `converter` (ADR-0012 rule, the
  same order `start.py` uses). The child PID is recorded for teardown.
- The engine binds `127.0.0.1` on a default port (`:8090`) with the same `+100`
  port-fallback as the dashboard, kept clear of the AI-server block
  (`:8081`–`:8084`, `:11434`). A manually-started `ptm-engine` is also supported.

### Transport

- **WebSocket** (via `flask-sock` + `simple-websocket`) for live progress, in both
  processes. Chosen over SSE for a single durable bidirectional channel and over
  polling for latency; acceptable because this is a single-viewer local tool.
- Plain HTTP JSON for the control plane (config, fs, jobs, engine start/health).

### Keep PySide6

`gui.py`/`main.py`/`start.py` and the `PySide6` dependency remain as a supported
fallback. The web app aims for feature parity with the GUI's *conversion* flow;
window geometry and OS-chrome specifics are browser-native and not reproduced.

## Consequences

- A full web converter with server-side file browsing and a native "open in
  Finder" action, reachable from a browser on the same machine.
- Two localhost services (UI `:8080`, engine `:8090`) whose ports must be
  coordinated with the AI-server block; documented in the README.
- New dependencies: `flask-sock` + `simple-websocket`.
- The engine becomes the second place (after `cli.py`) that drives `convert_files`;
  its single-job guard prevents concurrent conversion and the resulting
  process-global races.
- `converter` stays UI-free and deterministic; the engine and web UI both live
  outside the package (ADR-0001).

## Alternatives considered

- **Port the GUI into a single in-process Flask app** — simplest, but a long-running
  `convert_files` on a request thread blocks/races and the process-global
  `config`/`logstore` state is shared with the observer. Rejected in favour of
  isolating the writer in its own process.
- **Rewrite `converter` for concurrent/remote execution** — a large refactor for
  no current need; the single-job rule avoids it. Rejected.
- **SSE / polling for progress** — SSE breaks the single-channel model and polling
  adds latency + redundant SQLite reads. Rejected in favour of WebSocket.
- **Replace PySide6 entirely** — loses native file/folder dialogs and open-in-Finder
  without a clean browser equivalent. Rejected; both UIs coexist.
