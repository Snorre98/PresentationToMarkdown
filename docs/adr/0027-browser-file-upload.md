# 0027. Browser file upload to the native engine

- Status: Accepted
- Date: 2026-09-04

## Context

The web UI (ADR-0025) lets the user add conversion inputs two ways: a
server-side file browser (directory listing via the engine's `/api/fs/list`,
then "Add" / "Choose this folder"), and the browser-native "Add Files" button
plus drag-and-drop. The browser path has a hard gap that only surfaced once
the UI was exercised in a real browser:

- A browser `File` object (from `<input type="file">` or a drop event) carries
  the file **content and name**, but **never its absolute path**. Browsers
  sandbox paths for security; `File.path` exists only in Electron/Node.
- The engine needs a **native path** to run `convert_files` (ADR-0025's
  `rglob`/resolve/open all operate on real filesystem paths).

The current frontend maps `file => file.path` in both entry points, so every
selected/dropped file resolves to `undefined`, the engine's `/api/fs/resolve`
returns an error, and the file is silently dropped. The server-side browser is
the only working selector. This is not a code bug so much as a missing
mechanism: a browser can hand the engine *bytes*, and the engine must turn
those bytes into a native path before conversion.

## Decision

Bridge the browser's content-only file selection to the engine's path-based
conversion with a **staged upload**: the browser posts file bytes to the engine,
the engine saves them to a local staging directory, and returns the resulting
native paths. Those paths then flow through the exact same pipeline as a file
chosen in the server-side browser.

### Endpoint and transport

- **`POST /api/engine/fs/upload`** on the dashboard, proxied same-origin to the
  engine's **`POST /api/fs/upload`**. Multipart/form-data, one or more file
  parts.
- The dashboard's `_proxy` (ADR-0025) currently only forwards JSON bodies. It is
  extended to forward a **raw request body** (with the original `Content-Type`)
  so multipart passes through unchanged. Going through the dashboard keeps the
  browser on one origin — no `flask-cors` on the engine, and the dashboard still
  imports nothing from `converter` (it forwards bytes, not library code).
- The engine responds `{"files": [{"name", "path", "size"}, ...]}` with
  resolved absolute native paths. The frontend appends them to the job's file
  list; conversion is unchanged from there.

### Staging directory

Uploads are written to `<state_dir>/uploads/`, where `state_dir` is the same
path `lock.py:_state_dir()` derives (`PTM_STATE_DIR` or
`~/.local/state/ptm`). The directory is created on demand. It lives under the
state dir (not `/tmp`) so uploads survive restarts, are colocated with the
app's other state, and are isolated to a single testable location
(`PTM_STATE_DIR` is already isolated per test in `conftest.py`).

### Validation and sanitization

- **Extension whitelist**: only `.pptx` and `.pdf` (the same `_SUPPORTED_EXTENSIONS`
  set `_fs_list`/`_fs_glob` use). Anything else is rejected with an error and
  nothing is written.
- **Filename sanitization**: the stored name is `Path(name).name` (drops any
  client-supplied directory components); names containing `..`, empty names, and
  leading-dot names are rejected outright. On a collision with an existing file,
  a numeric suffix is appended (`deck.pptx` → `deck-1.pptx`) so a re-upload never
  silently overwrites a file a conversion may still be reading.
- **Size cap**: Flask `MAX_CONTENT_LENGTH = 500 MB` — generous for large decks
  and scanned PDFs, with a clean 413 beyond it.

### Cleanup

Uploads accumulate, so the engine performs a **best-effort startup prune**:
files in the staging directory older than **7 days** are deleted before the
server begins accepting requests. The sweep is wrapped in `try/except` and
never fails engine startup, and it only ever touches files already older than
the threshold — it cannot disturb a conversion currently running from a fresh
upload.

### Frontend

"Add Files" and drag-and-drop both build a `FormData` from the selected/dropped
`File` objects, `POST` it to `/api/engine/fs/upload`, and add the returned
paths. The server-side "Add Folder" browser (ADR-0025) is unchanged; uploads
and browser picks produce identical native paths downstream.

## Consequences

- Drag-and-drop and the "Add Files" button work in a real browser, closing the
  ADR-0025 gap; both produce native paths indistinguishable from browser picks.
- Uploaded bytes persist under `<state_dir>/uploads/`; the 7-day prune bounds
  disk growth with no user action and no conversion impact.
- The dashboard proxy now forwards non-JSON (multipart) bodies — a small
  expansion of its control-plane role, still without importing `converter` or
  writing to `ptm.sqlite` (uploads are filesystem writes, not DB writes, so
  ADR-0014/0025's "engine is sole writer" contract is untouched).
- The engine gains its first filesystem-*write* surface beyond conversion; the
  sanitization + whitelist rules keep it from writing outside the staging dir.

## Alternatives considered

- **Direct browser→engine upload (CORS)** — posts straight to the engine's
  origin, but the engine runs on a different port than the UI, so it needs
  `flask-cors` (a new dependency) and a CORS policy. Rejected in favour of the
  existing same-origin proxy, which needs no new dependency.
- **Server-side browse only, drop file-input/drag-drop** — removes the broken
  paths but loses the "just pick the file" ergonomic and the drag-drop that the
  web UI advertises. Rejected: the user wants real upload.
- **Stage uploads in `/tmp`** — simplest, but `/tmp` is cleared on reboot, may
  live on a different volume, and gives no durable, testable location. Rejected
  in favour of the state dir.
- **Return the bytes to the browser and let it re-download** — pointless; the
  engine already has the bytes and needs them locally to convert.
