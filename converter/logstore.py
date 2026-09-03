"""SQLite-backed logging for the vision pass and conversion runs.

Records every classifier decision and transcription — which source file, page or
image, how long the call took, and the outcome — so the vision pipeline is easy
to inspect. This is also the DB that will eventually hold app configuration.

Since the conversion-level telemetry landed (ADR-0022), this module also records
whole-conversion runs (``conversion_runs``), the phases within a run
(``run_phases``), a per-run configuration snapshot (``run_config``), and tags
every ``vision_events`` row with the run it belongs to via a
``contextvars.ContextVar``.

Configuration (environment variables):

- ``VISION_LOG_ENABLED`` — master switch (default on). ``1``/``true``/``yes``/``on``.
- ``VISION_LOG_DB`` — path to the SQLite file, default ``ptm.sqlite`` in the
  current working directory (the project dir for now).
"""
from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

VISION_LOG_ENABLED = os.environ.get("VISION_LOG_ENABLED", "on").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VISION_LOG_DB = os.environ.get("VISION_LOG_DB", "ptm.sqlite")

_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS vision_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    source           TEXT NOT NULL,
    page             INTEGER,
    image_ref        TEXT,
    image_digest     TEXT,
    stage            TEXT NOT NULL,
    model            TEXT,
    decision         TEXT,
    raw_answer       TEXT,
    latency_ms       INTEGER,
    prompt_tokens    INTEGER,
    generated_tokens INTEGER,
    markdown         TEXT,
    omitted_words    TEXT,
    error            TEXT,
    base_url         TEXT,
    run_id           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_source ON vision_events(source);
CREATE INDEX IF NOT EXISTS idx_events_digest ON vision_events(image_digest);
CREATE INDEX IF NOT EXISTS idx_events_run ON vision_events(run_id);
CREATE TABLE IF NOT EXISTS transcript_segments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    source   TEXT NOT NULL,
    start    REAL NOT NULL,
    end      REAL NOT NULL,
    speaker  TEXT,
    text     TEXT NOT NULL,
    model    TEXT,
    error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_transcript_source ON transcript_segments(source);
CREATE TABLE IF NOT EXISTS conversion_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    source      TEXT NOT NULL,
    name        TEXT,
    status      TEXT,
    ended_at    TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON conversion_runs(source);
CREATE TABLE IF NOT EXISTS run_phases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES conversion_runs(id) ON DELETE CASCADE,
    phase       TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    status      TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    duration_ms INTEGER,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_phases_run ON run_phases(run_id);
CREATE TABLE IF NOT EXISTS run_config (
    run_id   INTEGER PRIMARY KEY REFERENCES conversion_runs(id) ON DELETE CASCADE,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deck_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL UNIQUE,
    source_hash  TEXT NOT NULL,
    stem         TEXT NOT NULL,
    slide_count  INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deck_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES deck_documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    title        TEXT,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(document_id, chunk_index)
);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_current_run: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "ptm_current_run", default=None
)


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    Path(VISION_LOG_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(VISION_LOG_DB, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    _migrate(conn)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()
    _conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema upgrades for existing databases (never raises)."""
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(vision_events)")}
        if "run_id" not in cols:
            conn.execute("ALTER TABLE vision_events ADD COLUMN run_id INTEGER")
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> int | None:
    """The id of the conversion run active in this context, or ``None``."""
    return _current_run.get()


def record(
    *,
    source: str,
    stage: str,
    page: int | None = None,
    image_ref: str | None = None,
    image_digest: str | None = None,
    model: str | None = None,
    decision: str | None = None,
    raw_answer: str | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    generated_tokens: int | None = None,
    markdown: str | None = None,
    omitted_words: list[str] | None = None,
    error: str | None = None,
    base_url: str | None = None,
) -> None:
    """Insert a vision event. No-op when disabled; never raises."""
    if not VISION_LOG_ENABLED:
        return
    try:
        run_id = _current_run.get()
        with _lock:
            conn = _connection()
            conn.execute(
                """
                INSERT INTO vision_events (
                    ts, source, page, image_ref, image_digest, stage, model,
                    decision, raw_answer, latency_ms, prompt_tokens,
                    generated_tokens, markdown, omitted_words, error, base_url,
                    run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    source,
                    page,
                    image_ref,
                    image_digest,
                    stage,
                    model,
                    decision,
                    raw_answer,
                    latency_ms,
                    prompt_tokens,
                    generated_tokens,
                    markdown,
                    json.dumps(omitted_words) if omitted_words is not None else None,
                    error,
                    base_url,
                    run_id,
                ),
            )
            conn.commit()
    except Exception:
        pass


def record_segment(
    *,
    source: str,
    start: float,
    end: float,
    text: str,
    speaker: str | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    """Insert one transcript segment. No-op when disabled; never raises."""
    if not VISION_LOG_ENABLED:
        return
    try:
        with _lock:
            conn = _connection()
            conn.execute(
                """
                INSERT INTO transcript_segments (
                    ts, source, start, end, speaker, text, model, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    source,
                    start,
                    end,
                    speaker,
                    text,
                    model,
                    error,
                ),
            )
            conn.commit()
    except Exception:
        pass


def run_start(source: str, name: str | None = None) -> int | None:
    """Begin a conversion run, returning its id (or ``None`` when disabled/failed).

    Sets the current run in a context variable so every subsequent
    :func:`record` in this thread is tagged with ``run_id``.
    """
    if not VISION_LOG_ENABLED:
        return None
    try:
        with _lock:
            conn = _connection()
            cur = conn.execute(
                """
                INSERT INTO conversion_runs (ts, source, name, status)
                VALUES (?, ?, ?, 'running')
                """,
                (_now(), source, name or Path(source).name or source),
            )
            conn.commit()
            run_id = int(cur.lastrowid)
        _current_run.set(run_id)
        return run_id
    except Exception:
        return None


def run_finish(run_id: int | None, status: str = "ok", error: str | None = None) -> None:
    """Mark a run finished, recording its status and wall-clock duration."""
    if run_id is None:
        return
    try:
        with _lock:
            conn = _connection()
            row = conn.execute(
                "SELECT ts FROM conversion_runs WHERE id = ?", (run_id,)
            ).fetchone()
            duration_ms = None
            if row:
                try:
                    start = datetime.fromisoformat(row[0])
                    duration_ms = int(
                        (datetime.now(timezone.utc) - start).total_seconds() * 1000
                    )
                except Exception:
                    duration_ms = None
            conn.execute(
                """
                UPDATE conversion_runs
                SET status = ?, ended_at = ?, duration_ms = COALESCE(?, duration_ms)
                WHERE id = ?
                """,
                (status, _now(), duration_ms, run_id),
            )
            conn.commit()
    except Exception:
        pass
    finally:
        _current_run.set(None)


def run_phase_begin(
    run_id: int | None, phase: str, ordinal: int, detail: dict | None = None
) -> int | None:
    """Start a phase within ``run_id``, returning its row id (or ``None``)."""
    if not VISION_LOG_ENABLED or run_id is None:
        return None
    try:
        with _lock:
            conn = _connection()
            cur = conn.execute(
                """
                INSERT INTO run_phases (run_id, phase, ordinal, status, started_at, detail)
                VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    phase,
                    ordinal,
                    _now(),
                    json.dumps(detail) if detail is not None else None,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
    except Exception:
        return None


def run_phase_end(
    phase_id: int | None,
    status: str = "done",
    duration_ms: int | None = None,
    detail: dict | None = None,
) -> None:
    """Finish a phase, recording status, end time and duration."""
    if phase_id is None:
        return
    try:
        with _lock:
            conn = _connection()
            conn.execute(
                """
                UPDATE run_phases
                SET status = ?, ended_at = ?, duration_ms = ?,
                    detail = COALESCE(?, detail)
                WHERE id = ?
                """,
                (
                    status,
                    _now(),
                    duration_ms,
                    json.dumps(detail) if detail is not None else None,
                    phase_id,
                ),
            )
            conn.commit()
    except Exception:
        pass


@contextmanager
def phase(run_id: int | None, name: str, ordinal: int, detail: dict | None = None):
    """Context manager: record one run phase, marking it ``done``/``failed``.

    Re-raises any exception from the wrapped block, so a telemetry failure or a
    failing phase can never change conversion behaviour.
    """
    phase_id = run_phase_begin(run_id, name, ordinal, detail)
    t0 = time.perf_counter()
    try:
        yield phase_id
        run_phase_end(
            phase_id, "done", duration_ms=int((time.perf_counter() - t0) * 1000)
        )
    except Exception:
        run_phase_end(
            phase_id, "failed", duration_ms=int((time.perf_counter() - t0) * 1000)
        )
        raise


def run_snapshot(run_id: int | None, snapshot: dict) -> None:
    """Store a per-run configuration snapshot. Never raises."""
    if run_id is None:
        return
    try:
        with _lock:
            conn = _connection()
            conn.execute(
                "INSERT OR REPLACE INTO run_config (run_id, snapshot) VALUES (?, ?)",
                (run_id, json.dumps(snapshot)),
            )
            conn.commit()
    except Exception:
        pass
