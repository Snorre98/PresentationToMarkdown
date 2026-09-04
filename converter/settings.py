"""Persistent app configuration backed by ``ptm.sqlite``.

Small key/value store (the ``meta`` table) plus a recent-files list, living in
the same SQLite database as the vision log (see ``converter.logstore``). The
connection, WAL setup, and ``meta`` table are all owned by the SQLAlchemy layer
(``converter.db``, ADR-0026); this module just adds app-preferences on top.

All functions are no-ops on failure and thread-safe, mirroring
:func:`converter.logstore.record`.
"""
from __future__ import annotations

from converter.db import repos
from converter.logstore import _lock


def get_setting(key: str, default: str | None = None) -> str | None:
    """Return a stored preference value, or ``default`` when unset."""
    try:
        with _lock:
            return repos.get_meta(key, default)
    except Exception:
        return default


def set_setting(key: str, value: str) -> None:
    """Store a preference value. Never raises."""
    try:
        with _lock:
            repos.set_meta(key, value)
    except Exception:
        pass


def record_recent(path: str) -> None:
    """Record a file in the recent-files list, most-recent first. Never raises."""
    try:
        with _lock:
            repos.record_recent_path(path)
    except Exception:
        pass


def recent_files(limit: int = 10) -> list[str]:
    """Return the most recently used file paths, most-recent first."""
    try:
        with _lock:
            return repos.recent_paths(limit)
    except Exception:
        return []
