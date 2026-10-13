# 0051. Native TUI is the sole user interface; desktop GUI, dashboard, and CLIs retired

- Status: Accepted
- Date: 2026-10-13
- Relates to: [0001](0001-cli-entry-points.md), [0003](0003-convert-cli-gui-parity.md), [0009](0009-decouple-transcription-from-conversion.md), [0014](0014-read-only-log-dashboard.md), [0022](0022-flask-dashboard-conversion-telemetry.md), [0025](0025-web-gui-native-engine.md), [0027](0027-browser-file-upload.md), [0028](0028-upload-output-location.md), [0039](0039-preflight-inference-gate.md), [0040](0040-native-go-tui.md), [0041](0041-tui-transcription.md), [0045](0045-engine-hosted-transcription.md)

## Context

The app had accumulated five surfaces over the same Python core:

- the PySide6 desktop GUI (`gui.py` / `main.py`, launched by `ptm-start`),
- the Flask web dashboard (`dashboard/`, `ptm-dashboard`) and the native
  engine it controlled (`engine.py`, `ptm-engine`, ADR-0025),
- the standalone CLIs `ptm` (conversion, ADR-0001), `ptm-transcribe` (audio,
  ADR-0009), and `ptm-ab` (the need-gate A/B harness, ADR-0039),
- and the native Go TUI `ptm-tui` (ADR-0040), a client of that same engine.

Every output-layout change (ADR-0047/0048/0049/0050) had to be threaded through
GUI checkboxes, dashboard proxies, CLI flags, and TUI settings. ADR-0003 made
CLI/GUI parity a correctness contract, so drift between surfaces was a bug, not
just duplication. Meanwhile ADR-0045 moved audio transcription *into* the
engine, so no remaining surface depends on the `ptm-transcribe` CLI.

The Go TUI is the intended interaction model; the other surfaces now only cost
maintenance and dependency weight.

## Decision

Retire every user interface except the native TUI:

- **Delete** the PySide6 desktop GUI (`gui.py`, `main.py`, `start.py`,
  `ptm-start`, `scripts/ptm-start.sh`).
- **Delete** the Flask web dashboard (`dashboard/`, `ptm-dashboard`).
- **Delete** the standalone CLIs `ptm` (`cli.py`), `ptm-transcribe`
  (`cli_transcribe.py`, `scripts/ptm-transcribe.sh`), and `ptm-ab`
  (`cli_ab.py`).
- **Keep** `ptm-engine` (`engine.py`) — it is the TUI's backend, not a user
  interface, and the TUI spawns/attaches to it (ADR-0025/0040) — together with
  the `converter/` library it drives.
- **Keep** `scripts/transcript_qa.py` (a read-only QA oracle, not a product
  entry point) and the audio-model server scripts.
- Drop PySide6 from `requirements.txt`/`pyproject.toml`; `ptm-engine` is the
  only remaining console-script entry point.

The engine's browser-only code (upload staging, `/api/fs/upload`,
`/api/fs/resolve`, the `upload_original:` settings helpers; ADR-0027/0028)
becomes dormant. Removing it is deferred so this change stays a surface
deletion rather than an engine rewrite.

## Consequences

- **+** One surface to change: output-layout and AI-toggle work no longer has
  to be mirrored across GUI, dashboard, CLI, and TUI.
- **+** Fewer dependencies and packaging entry points (PySide6 gone; the
  dashboard package gone).
- **+** Conversion and transcription share one runtime path — the engine —
  which is already where their settings and telemetry live (ADR-0014/0025).
- **−** No headless CLI: scripting and automation go through the engine's
  HTTP/WS API or the Python library (`converter.convert_files`).
- **−** No browser access, and no `ptm-ab`; gate experiments run through pytest.
- **−** Dormant browser-upload code remains in the engine and settings store
  until a follow-up cleanup.

## Alternatives considered

- **Keep the CLIs for scripting** — rejected: the goal is a single maintained
  surface, and the library plus the engine API already cover automation.
- **Keep the GUIs behind a "deprecated" flag** — rejected: dead surfaces still
  demand parity work; deletion is the deprecation.
- **Delete the engine and move conversion into the TUI** — rejected:
  conversion stays Python by design (ADR-0040); the engine *is* the contract
  between the library and any native client.
