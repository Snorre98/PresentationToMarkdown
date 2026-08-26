# 0003. `ptm` mirrors the GUI conversion semantics

- Status: Accepted
- Date: 2026-08-26

## Context

The GUI's conversion flow (`gui.py`) encodes specific behavior that users rely
on: folders are scanned recursively for supported files, the output folder
defaults to `<source>/markdown` per file when not set, progress is reported as
`[N/M] name`, and each file yields an `[OK]`/`[ERR]`/`[WARN]` log line followed
by `Done: X of Y converted.`, with converted files recorded in the recent-files
list.

The headless `ptm` command must be a drop-in equivalent for scripting, so its
behavior must not drift from the GUI.

## Decision

`ptm` reproduces the GUI's conversion semantics exactly:

- `PATH...` accepts files and folders; folders are scanned with `rglob` over
  `SUPPORTED_EXTENSIONS` (matching `gui.add_paths`), unless `--no-recursive`.
- `--output DIR` overrides the output folder; when omitted, `output_dir=None` is
  passed to `convert_files`, which writes each file to its own
  `<source>/markdown` (matching the library default).
- Log lines match the GUI verbatim: `[N/M] name`, `[OK] name -> md_path`,
  `[ERR] name: error`, `[WARN] name: warning`, `Done: X of Y converted.`.
- Converted files are recorded via `converter.settings.record_recent`, unless
  `--no-recent`.
- Exit codes: `0` all succeeded, `1` any failure, `2` usage error / no supported
  files — so the command composes with shell automation.

`ptm` reuses `convert_files` directly rather than reimplementing conversion, so
the parity contract is confined to *how files are gathered, reported, and
recorded* — the conversion itself is shared with the GUI.

## Consequences

- Scripts can replace GUI-driven batch conversions with `ptm`.
- New GUI features (e.g. a new log line) must be mirrored here; the shared
  `convert_files` API minimizes, but does not eliminate, that surface.
- Exit codes give a machine-readable success/failure signal the GUI lacks.

## Alternatives considered

- **A `--json`/machine-readable mode** — deferred; not needed for the current
  personal-use scope, easy to add later without breaking the human-readable
  default.
- **Reimplementing conversion in the CLI** — rejected; would duplicate the
  library and guarantee drift.
