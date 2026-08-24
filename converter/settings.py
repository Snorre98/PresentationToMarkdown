"""Persistent app configuration backed by ``ptm.sqlite``.

Small key/value store (the ``meta`` table) plus a recent-files list, living in
the same SQLite database as the vision log (see ``converter.logstore``). The
connection, WAL setup, and ``meta`` table are all owned by ``logstore``; this
module just adds app-preferences on top.

All functions are no-ops on failure and thread-safe, mirroring
:func:`converter.logstore.record`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from converter.logstore import _connection, _lock

_RECENT_TABLE = """
CREATE TABLE IF NOT EXISTS recent_files (
    path      TEXT PRIMARY KEY,
    last_used TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recent_last_used ON recent_files(last_used);
"""


def _ensure_tables() -> None:
    conn = _connection()
    conn.executescript(_RECENT_TABLE)
    conn.commit()


def get_setting(key: str, default: str | None = None) -> str | None:
    """Return a stored preference value, or ``default`` when unset."""
    try:
        with _lock:
            conn = _connection()
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str) -> None:
    """Store a preference value. Never raises."""
    try:
        with _lock:
            conn = _connection()
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
    except Exception:
        pass


def record_recent(path: str) -> None:
    """Record a file in the recent-files list, most-recent first. Never raises."""
    try:
        with _lock:
            _ensure_tables()
            conn = _connection()
            conn.execute(
                """
                INSERT INTO recent_files (path, last_used) VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET last_used = excluded.last_used
                """,
                (path, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception:
        pass


def recent_files(limit: int = 10) -> list[str]:
    """Return the most recently used file paths, most-recent first."""
    try:
        with _lock:
            _ensure_tables()
            conn = _connection()
            rows = conn.execute(
                "SELECT path FROM recent_files ORDER BY last_used DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [row[0] for row in rows]
    except Exception:
        return []
