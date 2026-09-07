# 0036. Terminal UI (ptm-tui) — one native terminal surface for the whole app

- Status: Accepted
- Date: 2026-09-07

## Context

The app has four user surfaces with overlapping capabilities: the PySide6
desktop GUI (`ptm-start`), the headless CLI (`ptm`), the web dashboard
(`ptm-dashboard`, read-only history/RAG telemetry driven by the `ptm-engine`
sidecar, ADR-0022/0025/0027/0028), and the audio transcription CLI
(`ptm-transcribe`, ADR-0009). Each is a different technology with a different
interaction model, and the web surface has a structural weakness: a browser
cannot hand the app an arbitrary local path, so output/vault selection falls
back to a server-side directory browser (ADR-0028) that is awkward and has
reported failures ("select markdown vault" broken; outputs land in the staging
tree instead of beside the source).

The user works in the terminal and wants the `opencode`/`code .` experience: a
single command, invocable **globally from any directory**, that opens an
interactive surface scoped to the current working directory, with direct access
to its files. It must control every capability the ptm commands expose —
conversion with all AI passes, audio transcription, and the history/RAG view —
so that one surface subsumes the others for day-to-day work.

## Decision

Build `ptm-tui`, a Textual terminal UI that subsumes the conversion, history,
and transcription surfaces. One ADR series is kept in `docs/adr/` (PtM
precedent, Nygard format).

1. **Framework — Textual.** A full TUI (screens, workers, DataTable, ProgressBar,
   Input with fuzzy filtering) on top of Rich; pure Python, MIT, no native
   toolchain. This is a new, optional surface — the PySide6 GUI and the web
   dashboard remain supported (ADR-0025), not replaced.

2. **Global launcher (must-have).** `scripts/ptm-tui.sh` resolves the repo venv,
   bootstrapping it when missing (venv + `pip install -e .`), then execs the
   `ptm-tui` console script. `scripts/install-global.sh` writes an idempotent
   launcher to `~/.local/bin/ptm-tui` (absolute-path exec, symlink-safe);
   `~/.local/bin` is already on `PATH`. Acceptance: `ptm-tui` runs from any
   directory with no venv activated and opens in `os.getcwd()`, listing the
   directory's convertibles. Only `ptm-tui` is installed globally.

3. **File selection — `@` fuzzy selector.** The app scans cwd (recursive
   rglob) for `.pptx`/`.pdf` and audio files. `@query` focuses a fuzzy filter
   over relative paths; Space multi-selects; Enter dispatches by extension:
   presentations/PDFs convert, audio transcribes. Duplicate-if-exists keeps the
   Finder-style `stem (N).md` semantics (ADR-0015).

4. **Conversion runs in-process** on a Textual worker thread (the desktop GUI's
   model, ADR-0012/0013) — not a sidecar engine process. Output defaults to
   `<source-folder>/markdown` (the library default, ADR-0028) and is
   overridable, persisted via the shared `last_output_dir` setting.

5. **Stage progress via a phase listener.** `converter/logstore.set_phase_listener`
   fires on enter/exit of the existing `phase()` stages (convert → structure →
   format → summary), giving the TUI a live stage line. This also closes the
   ADR-0013 gap ("progress bar idle during structure/summary") for every
   surface, at zero interface churn.

6. **Audio transcription via `ptm-transcribe` subprocess.** The transcription
   pipeline stays decoupled (ADR-0009): the TUI spawns `ptm-transcribe` per
   audio file and streams its progress lines into the log pane. This preserves
   the single-instance flock (`lock.py`), the atomic artifact naming, and the
   `[WARN]`-degradation floor without re-implementing the pipeline.

7. **History screen — read-only.** The TUI reads `ptm.sqlite` through the
   dashboard's read-only SQLAlchemy engine (`dashboard.db`, ADR-0014/0022):
   recent runs, per-run phases/errors, models/RAG stats. Query logic is shared
   with the dashboard, never written from the TUI.

8. **Settings parity.** AI toggles, paper/slide, duplicate, and output dir read
   and write the same `converter.settings` keys the GUI/web use (`ai_*`,
   `pdf_mode`, `duplicate_if_exists`, `last_output_dir`), so state carries
   across surfaces. Startup accepts the shared `cli_common` AI flags
   (`--vision … --env KEY=VALUE`) applied before the `converter` import
   (ADR-0002/0012); runtime toggle changes flip `config.set_enabled`.

9. **Entry point + docs.** `ptm-tui = "tui:main"` console script,
   `requirements.txt` gains `textual>=2.0`. Tests use Textual's Pilot harness
   plus a phase-listener unit test; the existing suite must stay green.
   README + AGENTS.md document the command; `docs/runbook.md` gains a section.

## Consequences

- **+** One native, terminal-native surface controls every ptm capability;
  `@paper.pdf` + Enter converts to the `markdown/` sibling, `@lecture.mp3` +
  Enter transcribes beside the audio.
- **+** The browser↔filesystem impedance (ADR-0028's broken vault selection) is
  gone for this surface by construction: the TUI has real local paths.
- **+** The ADR-0013 progress gap is closed app-wide via the phase listener.
- **+** No new runtime processes: no engine sidecar, no extra daemons; the
  fleet control daemon (ADR-0029/0035) remains the only background authority.
- **−** `ptm-tui` duplicates some surface logic (rendering, settings) that the
  GUI and dashboard already own — mitigated by the shared settings keys and
  shared read-only query module.
- **−** Transcription within the TUI is subprocess-mediated, so its progress is
  line-streamed rather than structured; the same information is visible, the
  granularity is coarser than the conversion stage line.
- **−** Textual is a new runtime dependency; terminal-notifier-grade rendering
  (ANSI, width) means the TUI is at its best in a real terminal, not an IDE
  pane.

## Alternatives considered

- **Real engine sidecar (reuse `ptm-engine` + its HTTP/WS protocol)** — rejected
  for the TUI: it adds a Flask process, a WS client, and restart logic for no
  benefit over an in-process worker thread (the desktop GUI already proves the
  thread model), and the web engine's API is shaped for a browser client.
- **Reuse the PySide6 GUI with a terminal front-end** — rejected: Qt has no
  terminal surface; the TUI is a new interaction model, not a GUI skin.
- **A CLI-only flow (arguments, no interactive picker)** — rejected: the
  `opencode`-style in-directory experience with `@` selection is the stated
  must-have.
- **Full Rust/Tauri port of the UI** — deferred as a separate decision
  (web-dashboard direction, not the terminal surface); the converter and TUI
  stay Python.