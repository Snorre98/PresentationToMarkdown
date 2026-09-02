# 0014. Read-only web dashboard for conversion logs

- Status: Accepted
- Date: 2026-09-02

## Context

`converter/logstore.py` records every classifier/vision/structure/transcription
decision to `ptm.sqlite` (WAL mode) as it happens, and `converter/settings.py`
keeps the recent-files list in the same database. A long conversion with the
opt-in AI passes (ADR-0012) generates thousands of `vision_events` rows — one
per slide/page, with per-call latency, the model's decision, and the resulting
markdown/error — but there is no way to *watch* that progress: inspecting the
log today means opening SQLite by hand (or `sqlite3` in a shell) and running ad
hoc queries. The GUI's progress bars (ADR-0013) report page granularity, but
only for the file currently converting, and only from inside the GUI — a
headless `ptm --vision …` run leaves no live view at all.

The database is written in WAL mode, so a *reader* can query it concurrently
without blocking the writer. That makes a separate read-only observer safe: it
can render the log as it fills, without ever touching the conversion.

## Decision

Add a small, self-contained, **read-only** local web dashboard: `dashboard.py` at
the repository root, using only the Python standard library (`http.server` +
`sqlite3`), no framework and no new dependencies.

### Placement and shape (ADR-0001)

`dashboard.py` lives at the repo root, outside the `converter` package, for the
same reason `cli.py`/`start.py` do: the library must stay UI-free. It imports
nothing from `converter` — it only *reads* the database the library *writes* —
so the two stay decoupled at runtime. It is a standalone tool launched as
`./.venv/bin/python dashboard.py` (no `ptm-dashboard` console script; see
Alternatives).

### Read-only access (never interfere with the writer)

- Each request opens a **fresh** `sqlite3` connection with
  `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` and
  `PRAGMA query_only=ON`, plus a short `busy_timeout`.
- A fresh, short-lived connection per query means the dashboard never holds a
  handle across polls and never blocks a WAL checkpoint; on `DB locked` it
  simply returns an empty result for that poll.
- A missing/empty database (or one whose `-wal`/`-shm` haven't been created yet)
  raises on open; that is caught and degrades to "no events yet", never a crash —
  matching the "degrade gracefully, never fail" rule the converter already
  follows (ADR-0009, ADR-0012).

### Server and config

- `http.server.ThreadingHTTPServer` bound to `127.0.0.1` only (loopback; this is
  a local debug surface, not a network service).
- `--db PATH` overrides the database path (default `<repo root>/ptm.sqlite`, the
  same default `logstore` uses for `VISION_LOG_DB`); `--port N` overrides the
  port (default `8080`).
- On startup it prints a friendly `open http://127.0.0.1:<port>` line; if the
  port is already in use (`EADDRINUSE`, common since the local AI servers squat
  `:8081`/`:8082`/`:11434`), it falls back to the next free port and prints the
  actual URL.

### API and frontend

A small JSON API plus one inline HTML/JS page (embedded as a constant, so the
tool is a single self-contained file — no static asset directory):

- `GET /` — the single-page frontend.
- `GET /api/overview` — one row per distinct `source`: filename, total events,
  max page, total latency, last event `ts`; ordered recent-files-first (the
  `recent_files` order), then by last event time.
- `GET /api/events?source=<path>` — the per-page timeline for one source,
  ordered by `ts`, with `stage`/`model`/`decision`/`latency_ms` and a truncated
  `markdown`/`error`.
- `GET /api/errors` — all events with a non-null `error`.
- `GET /api/health` — `{ok, db, total_events}`.

The frontend re-polls these endpoints every ~2s and re-renders in place, so the
user sees per-page progress while the GUI/CLI is converting.

## Consequences

- `converter` is untouched and conversion stays deterministic: the dashboard is a
  purely additive, read-only observer.
- Polling (vs push) means up to ~2s of staleness, which is fine for a human
  watching a multi-minute conversion; it costs a tiny amount of CPU per poll.
- The read-only WAL reader relies on the `-wal`/`-shm` sidecars being present
  and readable; they always are while the writer is active, and absent only when
  the DB has never been written — which is exactly the "no events yet" case.
- If the log grows very large, the whole-events payload for a source can get
  heavy; acceptable for the current personal-use scale, and easily paged later.

## Alternatives considered

- **Flask / FastAPI (or any WSGI dependency)** — richer routing, but a new
  dependency and a heavier process for a local debug viewer. Rejected in favour
  of stdlib `http.server`.
- **Live inside `converter`** — would put a web server (a UI concern) in the
  UI-free library, violating ADR-0001. Rejected; it reads the DB *file* instead
  of importing the library.
- **WebSocket / SSE push** — lower latency, but needs an async server or more
  plumbing for a single-viewer local tool. Rejected; ~2s polling is sufficient.
- **Separate static `.html`/`.js` files served from disk** — more conventional,
  but splits a single-purpose tool into multiple files. Rejected in favour of an
  inline page for a self-contained `dashboard.py`.
- **A `ptm-dashboard` console-script entry point** — convenient, but this is a
  developer-facing debug tool, not a user command; a plain
  `./.venv/bin/python dashboard.py` keeps `pyproject.toml` unchanged. Deferred.
- **Fail hard on a busy port** — simpler, but `:8080` is frequently taken by
  local dev servers; falling back to the next free port is friendlier for a
  tool you start in a hurry.
