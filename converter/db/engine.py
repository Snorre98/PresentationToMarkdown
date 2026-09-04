"""Engine and session factories for the ``ptm.sqlite`` store (ADR-0026).

Provides:

- ``get_engine()`` / ``get_session()`` — the writer engine/session for the
  converter and settings. A single cached engine opening the WAL database with
  ``check_same_thread=False`` and a ``SingletonThreadPool`` so the one-job
  converter has a single writer handle. ``connect``-time PRAGMAs set WAL and a
  busy timeout.
- ``reset()`` — drop the cached engine/session so tests (and ``VISION_LOG_DB``
  overrides) can point at a fresh path.
- ``init_db(engine)`` — create all modelled tables if absent and run the
  versioned migration (adds ``vision_events.run_id`` on pre-v2 databases),
  rewriting ``meta.schema_version``.

This module owns the DB path resolution: it reads ``VISION_LOG_DB`` at engine
build time so a monkeypatched path is honoured (the same seam the old
``logstore._connection`` offered).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import SingletonThreadPool

from converter.db.models import Base

SCHEMA_VERSION = 2

DEFAULT_DB = "ptm.sqlite"


def _db_path() -> str:
    return os.environ.get("VISION_LOG_DB", DEFAULT_DB)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_lock = threading.Lock()


def _build_engine(db_path: str | None = None) -> Engine:
    path = db_path if db_path is not None else _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        "sqlite:///" + path,
        connect_args={"check_same_thread": False, "timeout": 30.0},
        poolclass=SingletonThreadPool,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()

    init_db(engine)
    return engine


def get_engine(db_path: str | None = None) -> Engine:
    """Return the cached writer engine, building (and migrating) it once."""
    global _engine, _session_factory
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _build_engine(db_path)
                _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session(db_path: str | None = None) -> Session:
    """Return a new ``Session`` bound to the writer engine."""
    get_engine(db_path)
    assert _session_factory is not None
    return _session_factory()


def reset() -> None:
    """Dispose the cached engine/session so a new path is picked up.

    Used by tests to isolate a throwaway DB and by ``VISION_LOG_DB`` overrides.
    """
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def init_db(engine: Engine) -> None:
    """Create all modelled tables (if absent) and run the versioned migration.

    Never raises; a missing/partial database is left for a later attempt.
    """
    try:
        with engine.begin() as conn:
            _migrate(conn)
            Base.metadata.create_all(conn)
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO meta(key, value) "
                    "VALUES ('schema_version', :v)"
                ),
                {"v": str(SCHEMA_VERSION)},
            )
    except Exception:
        pass


def _migrate(conn) -> None:
    """Idempotent column add for pre-v2 databases: ``vision_events.run_id``."""
    try:
        cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(vision_events)")
        }
        if "run_id" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE vision_events ADD COLUMN run_id INTEGER"
            )
    except Exception:
        pass
