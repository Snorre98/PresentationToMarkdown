# 0026. SQLAlchemy 2.0 ORM for the ptm.sqlite log/settings/RAG store

- Status: Accepted
- Date: 2026-09-04

## Context

Every database touch in the repo goes through the standard library `sqlite3`:

- `converter/logstore.py` — hand-rolls the schema (`_SCHEMA` string, schema v2),
  an idempotent `_migrate()` (`PRAGMA table_info` column add), and the
  `record`/`record_segment`/run/phase/snapshot helpers over a single cached
  connection (`_connection()`, `check_same_thread=False`, WAL) guarded by a
  `threading.Lock`.
- `converter/settings.py` — recent-files + the `meta` key/value store, borrowing
  `logstore`'s `_connection`/`_lock`.
- `converter/summary.py` — the RAG `deck_documents`/`deck_chunks` tables plus the
  `deck_chunk_vec` sqlite-vec `vec0` virtual table.
- `dashboard/app.py` — a read-only `_connect`/`_query`/`_scalar` layer (ADR-0014,
  reopened read-only per request with `mode=ro` + `query_only=ON`).

Four modules re-implement connection management, WAL/PRAGMA setup, schema
creation, migration, and row→dict mapping, each slightly differently. As the
schema grows (ADR-0022 added three tables and a column; ADR-0021 added the RAG
tables), that duplication is the obvious place for drift, mistyped columns, and
forgotten indexes. The schema is now stable enough — and well enough understood —
to warrant a single object-relational mapping layer that owns one schema
definition, one engine/session story, and typed read/write helpers, while
preserving the two hard guarantees the codebase depends on:

1. Conversion is **deterministic** and logging is **best-effort** — a failed DB
   write must never change (or fail) a conversion (ADR-0009, ADR-0012, ADR-0022).
2. The dashboard reads **read-only** and never imports `converter`
   (ADR-0014, ADR-0022).

## Decision

Introduce **SQLAlchemy 2.0** as the single ORM layer and migrate all current
queries onto it.

### Flavor: 2.0 declarative ORM, synchronous, Core only for raw necessities

- **ORM, not Core.** The tables are a natural fit for typed declarative models,
  and a declarative `Base.metadata` becomes the single source of truth for the
  schema (columns, types, primary keys, unique constraints, foreign keys,
  indexes) that today is split between `logstore._SCHEMA` and
  `summary._DOCS_CHUNKS_SCHEMA`. Core is used only where raw SQL cannot be
  avoided (see the sqlite-vec section).
- **SQLAlchemy 1.4 vs 2.0.** 2.0's declarative style (`Mapped`,
  `mapped_column`, `DeclarativeBase`) is chosen over the legacy 1.4
  `Column(...)` imperative style: it is the maintained surface, type-checks
  better, and is the default in current documentation and tutorials.
- **Sync, not async.** The converter runs on plain threads (GUI worker,
  engine worker) and the dashboard is WSGI/Flask (threaded). There is no
  `asyncio` event loop to integrate with, so a synchronous `Session` is simpler
  and avoids an `async` engine that nothing would consume.

### Package layout

A new `converter/db/` package (library-only, no UI dependencies, matching the
`converter` conventions in AGENTS.md):

- `engine.py` — writer engine + session factory and the read-only engine
  factory. Owns the SQLite URL/PATH handling, WAL and PRAGMA setup, and
  migration-on-first-use.
- `models.py` (alias `orm.py`) — the declarative `Base` and one model per real
  table: `vision_events`, `transcript_segments`, `conversion_runs`,
  `run_phases`, `run_config`, `deck_documents`, `deck_chunks`, `recent_files`,
  `meta`.
- `repos.py` — a small repository/service layer (plain functions, not class-based
  repositories, to minimize churn and fit the codebase style): the vision-event,
  transcript-segment, run/phase/snapshot, settings/meta, recent-files, and RAG
  document/chunk operations. Every write helper keeps the "never raises,
  no-op on failure" contract at the `logstore`/`settings` facade boundary.

`logstore.py` becomes a thin façade re-exporting the public API
(`record`, `record_segment`, `run_start`, `run_finish`, `run_phase_begin`,
`run_phase_end`, `phase`, `run_snapshot`, `current_run_id`) with unchanged
signatures, so the ~10 import sites (`from converter.logstore import record`,
etc.) do not change. The `contextvars` run-tagging stays in `logstore`.

### Concurrency and connection safety

- The **writer** engine uses `connect_args={"check_same_thread": False}` and a
  `SingletonThreadPool` (a single shared connection served to all threads) so
  the one-job-at-a-time converter (ADR-0025) has a single writer handle, as
  today. `PRAGMA journal_mode=WAL` and a `busy_timeout` are set via a
  `connect` event listener. The existing per-write `threading.Lock` in
  `logstore` is retained for write serialization.
- The **reader** (dashboard) is a separate process with a separate
  read-only engine (below), so the multi-process UI/engine/dashboard split from
  ADR-0014/0022/0025 is preserved: WAL lets the reader query concurrently
  without blocking the writer.

### Read-only guarantee (ADR-0014/0022) preserved

The dashboard keeps its "never import `converter`, never write" contract intact
by owning a **small self-contained read-only engine factory** in
`dashboard/db.py` (deliberately *not* importing `converter.db`). It builds:

- `create_engine("sqlite://", url="file:<path>?mode=ro&uri=true")` so the file
  is opened `mode=ro` exactly as the raw `_connect` did;
- a `connect` event listener that issues `PRAGMA query_only=ON` and
  `PRAGMA busy_timeout=1000` on every pooled connection.

Query helpers (`_query`/`_scalar`) run the same SQL through
`sqlalchemy.text()` against a fresh short-lived connection from that engine;
missing/locked DBs still degrade to empty results. Because SQL is unchanged,
the JSON shapes in ADR-0014/0022 are byte-for-byte identical.

### sqlite-vec virtual table

`deck_chunk_vec` is a `vec0` **virtual table**, not an ordinary table: it is
created with `CREATE VIRTUAL TABLE … USING vec0(...)`, has no rows SQLAlchemy
can introspect or map, and is queried with the extension's `MATCH ? … AND k = ?`
syntax. It is therefore kept **outside** the ORM metadata:

- `sqlite_vec.load()` requires the **raw DBAPI connection**, so all extension
  calls run against the raw connection obtained from SQLAlchemy
  (`engine.raw_connection()`'s `.driver_connection` / the session's
  `connection.connection.dbapi_connection`).
- `CREATE VIRTUAL TABLE`, `DROP TABLE`, and the KNN `MATCH` query are issued via
  `exec_driver_sql` (or `text()`), with `sqlite_vec.serialize_float32(vec)` for
  the embedding blobs, exactly as today.

The two *real* tables the RAG uses (`deck_documents`, `deck_chunks`) are normal
ORM models; only the vec0 table stays raw.

### Schema and migrations

The hand-rolled `_SCHEMA` string and `_migrate()` are replaced by:

- `Base.metadata` with one declarative model per table, mirrored exactly from the
  current `_SCHEMA` (same table names and column names/types/constraints), so
  existing `ptm.sqlite` files open unchanged — opening a pre-ORM database is a
  no-op for the schema.
- ``metadata.create_all(checkfirst=True)`` on engine first use, which is
  equivalent to the old `CREATE TABLE IF NOT EXISTS` batch.
- A **versioned migration step** in `repos.py` (or `engine.py`) that performs the
  one historical additive upgrade — adding `vision_events.run_id` — with
  `PRAGMA table_info` idempotence, matching today's `_migrate()`, so a pre-v2
  database still upgrades in place with no data loss. `meta.schema_version` is
  still written on create.

There is no forced migration that breaks existing databases, and no new schema
version is bumped (the physical schema is unchanged).

### Determinism and best-effort logging

The ORM changes nothing about what is written or when: every write remains
wrapped in `try/except Exception` at the facade (logging is best-effort and can
never fail or alter conversion output). Conversion output stays byte-identical;
only the log/settings/RAG storage layer changes.

## Consequences

- One schema definition (`converter/db/models.py`), one engine/session story
  (`converter/db/engine.py`), and one typed write path (`converter/db/repos.py`)
  replace four hand-rolled `sqlite3` layers.
- `requirements.txt` and `pyproject.toml` gain `sqlalchemy>=2.0`.
- `converter.logstore` keeps its public API and `contextvars` run-tagging, so all
  existing call sites continue to work unchanged; `converter.settings` keeps its
  function signatures and "no-op on failure" behavior.
- The dashboard gains a tiny `dashboard/db.py` read-only engine factory but still
  imports nothing from `converter`; the sqlite-vec extension remains unloaded by
  the dashboard (its RAG view still derives size from `deck_chunks` count +
  cached dim, never querying the vec0 table).
- Tests that monkeypatch `logstore._conn`/`VISION_LOG_DB` move to resetting the
  new engine/session factory; two new tests cover the read-only engine refusing
  writes and schema compatibility with a pre-ORM database.

## Alternatives considered

- **Stay on raw `sqlite3`** — no new dependency and zero churn, but keeps the
  four-way duplication and ad-hoc migration logic this ADR exists to remove.
  Rejected.
- **SQLAlchemy Core (no ORM)** — lighter, but loses the declarative models that
  give the schema a single, self-documenting, typed home, which is the main
  payoff here. Rejected.
- **SQLAlchemy 1.4 legacy declarative** — works, but 2.0 is the maintained,
  documented surface for new code. Rejected.
- **Async SQLAlchemy (`create_async_engine`)** — no event loop exists in the
  threaded converter or WSGI dashboard, so it adds complexity for no benefit.
  Rejected.
- **A third-party ORM (SQLModel / peewee / sqlalchemy-migrate)** — extra
  dependencies on top of SQLAlchemy or a new stack for a schema this size.
  Rejected in favour of plain SQLAlchemy 2.0.
- **Map the vec0 virtual table as an ORM model** — SQLAlchemy cannot introspect
  or reliably reflect a `vec0` table, and its `MATCH` query shape is
  extension-specific; keeping it on the raw DBAPI connection is the honest
  boundary. Chosen.
- **Have the dashboard import `converter.db.engine` for the read-only engine** —
  technically "imports converter", which ADR-0014/0022 forbid literally. A
  self-contained `dashboard/db.py` keeps the guarantee unambiguous. Chosen.
- **Alembic for migrations** — full migration tooling is overkill for a single
  additive historical column; the versioned in-place step keeps the existing
  "open old DBs unchanged" behavior with far less machinery. Rejected.
