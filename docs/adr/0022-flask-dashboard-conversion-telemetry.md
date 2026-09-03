# 0022. Flask dashboard and conversion-level run telemetry

- Status: Accepted
- Date: 2026-09-04

## Context

ADR-0014 shipped a read-only web dashboard (`dashboard.py`) on the standard
library `http.server`, serving a single auto-refreshing page (Overview /
Timeline / Errors) that reads `ptm.sqlite` via `/api/overview`, `/api/events`,
`/api/errors`, `/api/health`. It opened the DB `mode=ro` per request and never
imported `converter`, so it could not interfere with a running conversion.

Two limits have become visible in practice:

1. **The dashboard can only show what the log records, and the log only records
   per-image/per-page `vision_events`.** `converter/logstore.record` is called
   by the reader/writer passes (`classify`, `transcribe`, `interpret`,
   `structure`), each carrying `stage`/`model`/`decision`/`latency_ms`/`error`/
   `markdown` for one page or image. The `format` pass records nothing at all
   (a slide whose LLM restructure is silently rejected by the word/anchors gate
   is invisible), and the `summary` pass records nothing to `vision_events`
   either — it writes `deck_documents`/`deck_chunks`/`deck_chunk_vec` (ADR-0021).
   So the timeline goes quiet exactly when a long conversion is doing its most
   expensive work: the whole-document `structure` and `summary` passes that run
   *after* the per-page loop (the ADR-0013 progress gap, now observed at the
   dashboard level rather than only the GUI).

2. **Diagnosing a slow run means correlating state by hand.** Which feature
   toggles were on, which base URLs and model ids each pass resolved
   (ADR-0016's `WRITE_*` chain, ADR-0021's dedicated `:8084` summary model),
   what `PDF_MODE` was, and which servers were down (`config.missing_servers`)
   are all import-time environment state. None of it is captured next to the
   events it produced, so a `ptm.sqlite` inspected after the fact cannot answer
   "why was this run slow / which model did the structure pass actually hit".

3. **`http.server` + an inline HTML constant is a ceiling.** Routing, static
   assets, and the page are all hand-rolled in one file; growing the surface
   (run timelines, per-model latency aggregates, a RAG view) makes that string
   and routing block unwieldy.

## Decision

Add conversion-level telemetry and rebuild the dashboard as a small Flask app,
preserving ADR-0014's read-only / never-import-`converter` contract.

### Telemetry (written by `converter`, owned by `logstore`)

Bump `_SCHEMA_VERSION` to 2 and add, via `CREATE TABLE IF NOT EXISTS` plus an
idempotent `_migrate()` (column checked with `PRAGMA table_info`, so existing
`ptm.sqlite` files upgrade in place with no data loss):

- `conversion_runs(id, ts, source, name, status, ended_at, duration_ms)` — one
  row per `convert_file` attempt.
- `run_phases(id, run_id, phase, ordinal, status, started_at, ended_at,
  duration_ms, detail)` — one row per instrumented phase: `convert`,
  `structure`, `format`, `summary`.
- `run_config(run_id PK, snapshot TEXT)` — JSON from `config.snapshot()`.
- `vision_events` gains a nullable `run_id` column, plus an index.

A `contextvars.ContextVar` holds the current run id so `record()` tags every
`vision_events` row with `run_id` without changing any call site. New
`logstore` helpers — `run_start`, `run_phase_begin`, `run_phase_end`,
`run_finish`, `run_snapshot` — are all best-effort (`try/except`, never raise,
no-op when `VISION_LOG_ENABLED` is off), mirroring `record`. Conversion output
stays byte-identical: telemetry only ever writes the log.

Instrumentation points (minimal, additive):

- `converter/__init__.py` `convert_file`: `run_start`, then phase spans around
  `converter.convert` (`convert`), `polish_text` (`format`), and
  `prepend_summary` (`summary`), then `run_finish` — all in `try/finally`.
- `converter/pdf.py`: a `structure` phase span around `structure_paper(...)`.
- `converter/format.py`: a per-slide `stage="format"` event (decision
  `amended`/`rejected`/`kept`, latency, model) so silent anchor/word-gate
  rejections surface.
- `converter/summary.py`: one `stage="summary"` event (model, base_url, latency,
  decision `model`/`fallback`, error) so the summary pass shows live activity
  and its fallback reason is visible.

`classify`/`transcribe`/`interpret` stay per-event in `vision_events` and are
*derived* as phase spans from `min`/`max(ts)` per `run_id`, not double-written.

`converter/config.py` gains `snapshot() -> dict` (JSON-serialisable): the
enabled feature toggles, each pass's resolved base URL + model id + server
(resolved via deferred imports of the pass modules to avoid import cycles,
honouring ADR-0016/0021), `PDF_MODE`, and the `missing_servers()` probe result.
The probe runs once at run start, best-effort, only over the unique base URLs of
enabled features; it never changes conversion output.

### Dashboard (Flask)

- New dependency `flask>=3.0` (added to `requirements.txt` and `pyproject.toml`).
- Restructure into a `dashboard/` package: `__init__.py` (public `create_app`/
  `main`), `app.py` (factory + routes + the read-only query layer),
  `templates/index.html`, `static/app.js`, `static/style.css`, `__main__.py`.
- Root `dashboard.py` is removed: a module file and a package cannot share the
  name in `import dashboard`. The entry point becomes `python -m dashboard`
  (via `__main__.py`) and/or a `ptm-dashboard` console script. This is the one
  deliberate change to ADR-0014's launch command.
- CLI: `--db`/`--port` kept, `--host` added (default `127.0.0.1`); the
  port-fallback loop (next free port up to +100) and friendly startup print are
  preserved (Flask's threaded dev server, catching `OSError` on bind).
- Read-only contract unchanged: each request opens
  `sqlite3.connect("file:…?mode=ro")` with `query_only=ON`; **no `converter`
  import and no sqlite-vec extension load** (the RAG view derives index size
  from `deck_chunks` count + `meta.summary_embed_dim`, never querying the
  `deck_chunk_vec` vec0 virtual table).

### API and UI

Preserve `/api/overview`, `/api/events`, `/api/errors`, `/api/health` with
identical JSON shapes (`/api/events` gains an optional `run_id` filter but keeps
`source`). Add:

- `GET /api/runs`, `GET /api/runs/<id>`, `…/phases`, `…/config`, `…/summary`
- `GET /api/models` — per-model/per-stage latency aggregates (count, min/avg/
  p50/p95/max, total) and histogram bins.
- `GET /api/structure` — structure/format rejections aggregated as the cost
  driver (count, total latency, worst pages).

Tabs: **Runs** (list), **Timeline** (per-run phase swimlane + per-page events),
**Errors** (with rejections pinned on top), **Models**, **RAG**, and a per-run
**Config** panel.

## Consequences

- The dashboard now shows a run-level phase timeline that keeps moving through
  the whole-document `structure` and `summary` passes, closing the observed
  ADR-0013 gap at the dashboard level.
- Per-run config snapshots make "which toggles/models/servers for this run" a
  first-class query, not a hand-correlation.
- New tables are additive and migrated in place; old dashboards/DBs keep working
  (the legacy `/api/*` shapes are unchanged).
- `dashboard.py` → `dashboard/` changes the launch command and the test import
  (`from dashboard import create_app`); `tests/test_dashboard.py` is rewritten
  to use Flask's `test_client()` against a throwaway DB.
- One new runtime dependency (Flask), superseding ADR-0014's "no framework"
  choice. The `sqlite-vec` extension is still never loaded by the dashboard.

## Alternatives considered

- **Keep `http.server`** — no new dependency, but the inline-page/routing ceiling
  is exactly what this ADR exists to lift. Rejected.
- **FastAPI / Starlette** — richer async, but another dependency stack and
  concurrency the single-viewer local tool does not need. Rejected in favour of
  Flask (small, WSGI, well-understood).
- **WebSocket / SSE push** — lower latency than polling, but more plumbing for a
  ~2s-poll human viewer. Rejected (as in ADR-0014).
- **Instrument classify/transcribe/interpret as explicit phases** — would touch
  every per-image call site; they are already richly recorded per event, so
  deriving their spans from `run_id`-grouped events is equivalent with no churn.
  Rejected.
- **Live-probe servers from the dashboard** — breaks the "read-only, no network,
  never interfere" guarantee; a per-run *snapshot* captured at run start is
  historical and safe. Rejected.
- **Query `deck_chunk_vec` directly for index size** — requires loading the
  sqlite-vec extension in the dashboard connection, reintroducing a
  converter-adjacent dependency. Rejected; chunk count + cached dim suffice.
