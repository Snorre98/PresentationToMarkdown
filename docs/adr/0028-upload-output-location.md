# 0028. Upload output location

- Status: Accepted
- Date: 2026-09-04

## Context

The web GUI (ADR-0025) gains real browser file upload (ADR-0027): a selected or
dropped file's bytes are posted to the engine, staged under
`<state_dir>/uploads/`, and the staged native path is handed to `convert_files`.

This staged path is the problem. `convert_file` defaults its output to
`<source-folder>/markdown` (`converter/__init__.py:_default_output_dir`), so a
file that the user **uploads** — and which already exists on disk elsewhere —
has its Markdown written to `<state_dir>/uploads/markdown/` rather than next to
the real file. Observed: uploading
`~/…/papers/03 What makes things fun to learn.pdf` wrote to
`~/.local/state/ptm/uploads/markdown/…` instead of `~/…/papers/markdown/…`, where
earlier desktop-GUI runs correctly wrote their output.

The desktop GUI does the right thing only because its input path *is* the real
file. The upload flow replaces that input path with a staged copy, so the same
`<source>/markdown` default silently relocates the output into the staging tree.

## Decision

Give uploads a stable notion of their **original path** and make the output
directory resolve against it, reproducing desktop-GUI parity without changing
the conversion core's defaulting behaviour for non-uploaded files.

### 1. Per-file output resolver in `converter`

Widen `convert_file`/`convert_files` `output_dir` from `str | Path | None` to
also accept a per-file resolver `OutputDirResolver = Callable[[Path], Path | None]`.
When `output_dir` is callable, `convert_file` evaluates it with the source
`Path`:

```python
if callable(output_dir):
    resolved = output_dir(path) or _default_output_dir(path)
else:
    resolved = Path(output_dir) if output_dir else _default_output_dir(path)
```

`convert_files` already loops per file and delegates to `convert_file`, so the
callable is naturally invoked once per path. The change is **additive** — GUI and
CLI keep passing `Path | None` and are untouched. `OutputDirResolver` stays an
internal alias (not exported in `__all__`).

### 2. Original-path resolution and persistence (engine)

- An upload's original is resolved by **persisted map first, `recent_files`
  fallback**: the engine looks up the staged path in a persisted mapping; if
  absent, it matches the upload's **pre-dedup basename** against
  `settings.recent_files()` (most-recent-first), taking the first existing path
  **not under the uploads dir**. Matching is layered for robustness: an exact
  basename wins, then a match of the `-<digits>`-suffix-stripped stems (so both
  the staging dedup form `deck-2.pdf` and genuine on-disk `deck-2.pdf` files
  resolve against the plain `deck.pdf`), and within each class a size-equal
  candidate is preferred but never required.
- The map is persisted as **one `meta` row per staged path** (ADR-0026 `Meta`
  store), key `upload_original:<staged_abs_path>`, value `original_abs_path`.
  Per-path rows (rather than one JSON blob) keep each entry's lifecycle tied to
  its staged file.
- Blank output (user left the Output field empty) defaults to
  `original.parent / "markdown"`; when no original is known it falls back to the
  staging `<source>/markdown`. An **explicit** output directory in the UI always
  wins — the resolver is only used when `output_dir` is falsy.
- `record_recent` records the **original** path (not the staged path) for
  uploads, so future name-matching stays healthy.
- The 7-day prune sweep (ADR-0027) deletes each staged file's `meta` row when it
  deletes the file, so stale originals never accumulate.

### 3. Mixed selections

Because the resolver is per-file, a mixed job (some uploads, some server-side
browser picks) resolves each to its own correct folder — exact GUI parity,
including the "each file to its own `<folder>/markdown`" case.

## Consequences

- Uploaded files now write Markdown beside the real on-disk file, matching the
  desktop GUI; the staging dir stops quietly capturing output.
- One new `meta` key namespace (`upload_original:*`) and one new repo helper
  (`delete_meta`); **no schema change, no version bump, no migration** — the
  ADR-0026 `Meta` store is reused.
- `converter` gains an internal callable `output_dir` capability; its public
  `Path | None` contract is unchanged.
- The engine becomes the sole writer of the upload-original mapping (consistent
  with ADR-0025's writer isolation); the dashboard never touches it.

## Alternatives considered

- **In-memory map of staged → original** — simplest, but lost on engine restart,
  which the restart-survival requirement rules out. Rejected.
- **One JSON blob under a single `meta` key** — fewer rows, but stale entries
  accumulate and their lifecycle is entangled. Rejected in favour of per-path
  rows deleted by the prune sweep.
- **Persist a sidecar file in the uploads dir** — a second source of truth
  outside the DB. Rejected; the `meta` store (ADR-0026) is the established home.
- **Add a per-file `output_dir` to `convert_files`** — would churn every call
  site; the resolver callable is a smaller, backward-compatible change.
  Rejected.
- **Write uploads' output into the explicit UI output dir only** — correct when
  the user sets it, but leaves the blank-field default (the reported bug)
  unresolved. Rejected.
