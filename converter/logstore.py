"""SQLite-backed logging for the vision pass.

Records every classifier decision and transcription — which source file, page or
image, how long the call took, and the outcome — so the vision pipeline is easy
to inspect. This is also the DB that will eventually hold app configuration.

Configuration (environment variables):

- ``VISION_LOG_ENABLED`` — master switch (default on). ``1``/``true``/``yes``/``on``.
- ``VISION_LOG_DB`` — path to the SQLite file, default ``ptm.sqlite`` in the
  current working directory (the project dir for now).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

VISION_LOG_ENABLED = os.environ.get("VISION_LOG_ENABLED", "on").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VISION_LOG_DB = os.environ.get("VISION_LOG_DB", "ptm.sqlite")

_SCHEMA_VERSION = 1

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
    base_url         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_source ON vision_events(source);
CREATE INDEX IF NOT EXISTS idx_events_digest ON vision_events(image_digest);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    Path(VISION_LOG_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(VISION_LOG_DB, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()
    _conn = conn
    return conn


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
        with _lock:
            conn = _connection()
            conn.execute(
                """
                INSERT INTO vision_events (
                    ts, source, page, image_ref, image_digest, stage, model,
                    decision, raw_answer, latency_ms, prompt_tokens,
                    generated_tokens, markdown, omitted_words, error, base_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
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
                ),
            )
            conn.commit()
    except Exception:
        pass
