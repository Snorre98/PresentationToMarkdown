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


_UPLOAD_ORIGINAL_PREFIX = "upload_original:"


def set_upload_original(staged_path: str, original_path: str) -> None:
    """Persist the on-disk original for a staged upload (ADR-0028). Never raises."""
    try:
        with _lock:
            repos.set_meta(_UPLOAD_ORIGINAL_PREFIX + staged_path, original_path)
    except Exception:
        pass


def get_upload_original(staged_path: str) -> str | None:
    """Return the persisted original path for a staged upload, or ``None``."""
    try:
        with _lock:
            return repos.get_meta(_UPLOAD_ORIGINAL_PREFIX + staged_path)
    except Exception:
        return None


def delete_upload_original(staged_path: str) -> None:
    """Forget the persisted original for a staged upload. Never raises."""
    try:
        with _lock:
            repos.delete_meta(_UPLOAD_ORIGINAL_PREFIX + staged_path)
    except Exception:
        pass
