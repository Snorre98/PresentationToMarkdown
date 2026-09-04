"""Read-only SQLAlchemy engine for the PTM dashboard (ADR-0026).

The dashboard must never import ``converter`` (ADR-0014/0022), so this module
owns its own small read-only engine factory rather than importing
``converter.db.engine``. Every connection opened here is ``mode=ro`` with
``PRAGMA query_only=ON`` and a short ``busy_timeout``, so the dashboard never
writes to, or blocks, the conversion's WAL database.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool


def make_readonly_engine(db_path: str) -> Engine:
    """Build a read-only engine over ``db_path`` (``mode=ro`` + ``query_only``).

    ``NullPool`` means each ``engine.connect()`` opens a fresh, short-lived
    SQLite connection, mirroring ADR-0014's "never hold a handle across polls".
    """
    url = f"sqlite:///file:{db_path}?mode=ro&uri=true"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 1.0, "uri": True},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _readonly(dbapi_conn, connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA query_only=ON;")
        cursor.execute("PRAGMA busy_timeout=1000;")
        cursor.close()

    return engine
