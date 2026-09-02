# 0015. Duplicate-if-exists conversion option

- Status: Accepted
- Date: 2026-09-02

## Context

A conversion always overwrites its output: `<stem>.md` and `assets/<stem>/` are
written unconditionally, so re-converting a file (for example to A/B a different
vision model against a known-good result) destroys the previous output. There is
no way to produce a second, coexisting conversion for comparison without manually
copying files or juggling output folders.

The determinism guarantee (ADR-0003, and the repo's "one `.md` per source"
contract) makes overwriting the sensible default, but a model swap is exactly the
kind of experiment where the old output is the baseline you are measuring
against — losing it would corrupt the experiment.

## Decision

Add an opt-in **duplicate-if-exists** mode. When enabled, `convert_file` checks
whether the target `<stem>.md` already exists; if so it writes to the next free
Finder-style name — `stem (2).md`, `stem (3).md`, … — and to a matching
`assets/<stem> (N)/` folder, leaving every prior output byte-for-byte intact.

- The mode never opens an existing `.md` for writing: it only *reads existence*
  to pick a free name. The original file, and its assets folder, are never
  touched, renamed, or deleted.
- The whole stem changes (not just the `.md` suffix), so embedded image links
  (`assets/<stem>/...`) stay self-consistent with no link rewriting.
- Default is `False` (overwrite), preserving the current behaviour.
- The dedup/repeated-image caches are per-run and keyed by content digest, so a
  duplicated conversion is independent of the prior one.

Surfacing:

- Library: `convert_file(..., duplicate_if_exists=False)` and
  `convert_files(..., duplicate_if_exists=False)`; `Converter.convert` gains an
  optional `output_stem: str | None = None` (defaults to `path.stem`).
- CLI: `ptm --duplicate` (headless parity, ADR-0003).
- GUI: a "Duplicate if conversion exists" checkbox, persisted via the settings
  store (the same pattern as the paper-mode checkbox, ADR-0012).

## Consequences

- Safe A/B runs: point `VISION_MODEL` at a candidate, enable duplicate, and the
  baseline `.md` survives for diffing.
- The `.md`-name contract gains a documented variant; tools that glob for
  `*.md` may pick up `stem (2).md` — acceptable for the personal-use scope.
- A regression test asserts the original output is byte-identical after a
  duplicate run.

## Alternatives considered

- **Manual output-folder switching** — works today but is error-prone and
  doesn't help the GUI user; rejected.
- **Timestamped suffixes (`stem-20260902.md`)** — sortable and self-describing,
  but re-running the same day still collides and the names aren't obviously
  ordered by recency; the incremental `(N)` scheme is simpler to reason about.
- **A `--variant NAME` flag** — explicit naming for A/B runs, but shifts the
  burden of inventing names onto the user; deferred in favour of the automatic
  `(N)` scheme.
- **A "confirm before overwrite" dialog** — orthogonal; noted as a possible
  follow-up, not part of this decision.
