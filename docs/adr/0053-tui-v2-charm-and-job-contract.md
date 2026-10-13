# 0053. TUI v2: Charm v2 shell, fixed-frame layout, engine job contract

- Status: Accepted
- Date: 2026-10-13
- Relates to: [0012](0012-runtime-ai-features.md), [0013](0013-per-page-progress.md), [0025](0025-web-gui-native-engine.md), [0040](0040-native-go-tui.md), [0041](0041-tui-transcription.md), [0045](0045-engine-hosted-transcription.md), [0048](0048-markdown-beside-source-archive.md), [0051](0051-tui-sole-surface.md)

## Context

The first Go TUI (ADR-0040) accumulated a small set of failures that all traced
back to the same design gap — the app had no notion of a **bounded frame** or a
**job identity**:

1. **The status line "printed in a loop".** After every finished conversion,
   `mergeExternalJob` appended a fresh strip entry on each 1 Hz `/api/status`
   poll because the engine advertised the finished job as `current_job`
   forever (`_set_job(status="done")`, never cleared). The view grew one line
   per second, and once it was taller than the terminal the v1 renderer
   scrolled the alternate screen, pushing the picker off the top. (The same
   growth also leaked UI state unboundedly.)
2. **A second conversion was impossible.** `Enter` converted `m.selected` when
   non-empty and the first `Enter` auto-selected the cursor row — but nothing
   ever cleared the selection, and the default layout (ADR-0048) moves the
   converted source into `source/`. Every later `Enter` therefore re-ran the
   previous, now-archived path; the engine answered `done ok=0` with a
   `FileNotFoundError`, and the picker list was never refreshed either.
3. **Discovery raced the engine.** `/api/fs/glob` (importing `converter`) and
   `/api/runs` (importing `converter.db`) issued concurrently from a cold
   start deadlocked in CPython's per-module import locks (`_ModuleLock`
   deadlock, HTTP 500), because the engine imported `converter` lazily inside
   Flask's thread-pool handlers.
4. The framework itself moved on: **Bubble Tea / Lip Gloss / Bubbles v2**
   (Feb 2026) ship the Cursed Renderer (screen-buffer based — oversized
   content is clipped, not scrolled), declarative `tea.View` fields, a cell
   compositor in Lip Gloss, and the Bubbles components (`progress`, `help`,
   `key`) used here.

The product direction (ADR-0051) keeps the TUI as the sole surface, so its
foundations are worth fixing properly rather than patching symptom by symptom.

## Decision

### 1. The frame is fixed and the job is the unit of UI state

- The engine gives every job a monotonic **id**, `started_at`/`finished_at`,
  per-file `results`, and moves finished jobs out of `current_job` into a
  capped `jobs` history. `current_job` is non-`null` **only while running**.
- The TUI keys jobs by that id. A finished job can never be re-added by a late
  snapshot, a reconnect, or a second poll.
- Every pane renders into an explicitly sized window; the composed view is
  cropped/padded to the terminal (`fitLines`), so no state can make the frame
  taller or wider than the screen. This removes the bug class structurally,
  not just the instance.

### 2. One persistent watch socket; push, not poll

`/ws` accepts `{"type": "watch"}`: the socket receives a status snapshot and
then every live job frame (`file`, `page`, `phase`, `log`, `done`,
`cancelled`, `error`) for locally started and external jobs alike. Frames
carry `job_id` and `job_kind`; the engine fans them out through a
send-locked multi-watcher `_emit` that drops dead sockets. The TUI keeps one
watch connection for the whole session (start requests go out on the same
socket) and delivers frames into the update loop with `tea.Program.Send` — no
read command to re-arm, and a reconnect cannot stall the stream. The 1 Hz
`/api/status` poll is gone.

### 3. Cooperative cancel, run history, richer discovery

- `POST /api/jobs/cancel {"id"}` sets a flag checked at page and file
  boundaries. The job retires as `cancelled` and emits a `cancelled` frame;
  the in-flight file leaves no Markdown (converters write at the end) and is
  not archived. Transcription cancels between files.
- `GET /api/runs` exposes the `conversion_runs` rows for the results tab's
  history section, so past runs survive an engine restart.
- `/api/fs/glob` returns `entries` with `kind`/`size`/`mtime`/`is_dir` next to
  the backward-compatible `files` array; `/api/fs/open` takes `reveal`
  (`open -R`). `/api/health/servers` reports `missing` as objects.

### 4. Charm v2 shell

Bubble Tea v2.1 / Lip Gloss v2.0 / Bubbles v2.2 (`charm.land/...`). The
layout is a header (cwd + engine/watch badge), a bounded jobs panel (composite
progress = files × current page, phase spinner, elapsed, cancel), a tabbed
body (`1` files, `2` results, `3` log), and a one-line help footer generated
from a single `key` table; `?` opens the full help, `s` a settings modal
(features, output prefs, transcription options, live server health). Selections
are keyed by path, consumed on dispatch, and pruned when discovery refreshes,
so the ADR-0048 archive move can never strand one. The engine is attached or
spawned from inside the app (`Init`), so the UI exists immediately instead of
waiting up to 30 s on a silent health poll; `r` re-discovers on demand.

### 5. Engine imports eagerly

`create_app` imports `converter` once, on its own thread, before serving.
Request handlers may still import submodules lazily, but the parent package is
loaded before Flask's thread pool can race two first imports.

## Consequences

- **+** The two reported failures are gone at the root: no unbounded strip and
  no stale selection; a second conversion works from the same session.
- **+** All progress, local or external, arrives on one socket with ids;
  server-side state is observable and consistent for late joiners.
- **+** Output artifacts are first-class: the results tab opens/reveals them,
  and `/api/runs` gives post-restart history.
- **+** The frame invariant is unit-tested across sizes and states.
- **−** Two contract additions to maintain: the watch protocol and the job
  record shape; `/api/status` remains for health/debug.
- **−** Cancel is cooperative (between pages/files), not immediate; an
  in-flight AI call finishes before the flag is observed.
- **−** In-memory job history dies with the engine; md paths exist only for
  the session (the DB schema stores runs, not outputs).

## Alternatives considered

- **Keep the 1 Hz `/api/status` poll, fix `mergeExternalJob` to dedupe.** It
  patches one call site while leaving the app unable to distinguish "running"
  from "stale"; rejected in favour of a running-only `current_job` plus ids.
- **Per-job WebSocket connections (the ADR-0045 model).** The initiating
  client owns progress and other observers poll; this kept two sources of
  truth and the poll bug's conditions. Rejected for one watch socket.
- **Channel + `tea.Cmd` re-arming for watch frames.** A single missed
  re-arm silently stops all updates (observed live); `tea.Program.Send` is
  push-by-construction.
- **Immediate cancel (kill the subprocess / abort the render).** The converter
  writes outputs at file granularity; aborting mid-write risks partial
  artifacts. Cooperative boundaries keep ADR-0048/0049/0050 invariants.
- **A text-input/filter component per pane.** The existing matcher and manual
  edit buffers already satisfy the workflow; Bubbles `list`/`textinput` would
  add API surface without changing behaviour. Rejected for now.
