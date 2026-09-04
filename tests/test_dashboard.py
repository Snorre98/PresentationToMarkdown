"""Tests for the read-only conversion-log dashboard (``dashboard`` package).

Builds the Flask app against a throwaway SQLite database via
``dashboard.create_app`` and asserts the JSON endpoints return the expected
rows. The app is imported from the package and never imports ``converter``.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dashboard import create_app

SCHEMA = """
CREATE TABLE vision_events (
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
CREATE TABLE recent_files (
    path      TEXT PRIMARY KEY,
    last_used TEXT NOT NULL
);
CREATE TABLE conversion_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    source      TEXT NOT NULL,
    name        TEXT,
    status      TEXT,
    ended_at    TEXT,
    duration_ms INTEGER
);
CREATE TABLE run_phases (
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
CREATE TABLE run_config (
    run_id   INTEGER PRIMARY KEY REFERENCES conversion_runs(id) ON DELETE CASCADE,
    snapshot TEXT NOT NULL
);
CREATE TABLE deck_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL UNIQUE,
    source_hash  TEXT NOT NULL,
    stem         TEXT NOT NULL,
    slide_count  INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE deck_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES deck_documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    title        TEXT,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(document_id, chunk_index)
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        # source A — two pages, one error
        ("/data/a.pdf", 1, "classify", "m", "diagram", 120, "md-a1", None, "2026-09-02T10:00:01+00:00", 1),
        ("/data/a.pdf", 2, "transcribe", "m", None, 240, "md-a2", None, "2026-09-02T10:00:02+00:00", 1),
        ("/data/a.pdf", 2, "structure", "m", "rejected", None, None, "server down", "2026-09-02T10:00:03+00:00", 1),
        # source B (not recent)
        ("/data/b.pdf", 1, "classify", "m", "text", 30, "md-b1", None, "2026-09-02T09:00:00+00:00", None),
    ]
    conn.executemany(
        """
        INSERT INTO vision_events
            (source, page, stage, model, decision, latency_ms, markdown, error, ts, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.executemany(
        "INSERT INTO recent_files (path, last_used) VALUES (?, ?)",
        [
            ("/data/b.pdf", "2026-09-01T12:00:00+00:00"),
            ("/data/a.pdf", "2026-09-02T12:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO conversion_runs (id, ts, source, name, status, ended_at, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-09-02T10:00:00+00:00", "/data/a.pdf", "a.pdf", "ok", "2026-09-02T10:00:10+00:00", 10000),
            (2, "2026-09-02T09:00:00+00:00", "/data/b.pdf", "b.pdf", "error", "2026-09-02T09:00:05+00:00", 5000),
        ],
    )
    conn.executemany(
        "INSERT INTO run_phases (run_id, phase, ordinal, status, started_at, ended_at, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "convert", 1, "done", "2026-09-02T10:00:00+00:00", "2026-09-02T10:00:01+00:00", 1000),
            (1, "structure", 2, "done", "2026-09-02T10:00:02+00:00", "2026-09-02T10:00:03+00:00", 1000),
        ],
    )
    conn.execute(
        "INSERT INTO run_config (run_id, snapshot) VALUES (?, ?)",
        (1, json.dumps({"pdf_mode": "slide", "features": {"vision": True}})),
    )
    conn.execute(
        "INSERT INTO deck_documents (id, source, source_hash, stem, slide_count, created_at, updated_at) "
        "VALUES (1, '/data/a.pdf', 'h', 'a', 3, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO deck_chunks (document_id, chunk_index, title, content, content_hash) "
        "VALUES (1, 1, 't', 'c', 'ch')"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def app(tmp_path):
    db = _make_db(tmp_path / "ptm.sqlite")
    return create_app(str(db))


def test_health_reports_event_count(app):
    health = app.test_client().get("/api/health").get_json()
    assert health["ok"] is True
    assert health["total_events"] == 4


def test_overview_lists_sources_recent_first(app):
    ov = app.test_client().get("/api/overview").get_json()
    assert ov["total_events"] == 4
    names = [s["name"] for s in ov["sources"]]
    assert names == ["a.pdf", "b.pdf"]

    a = next(s for s in ov["sources"] if s["source"] == "/data/a.pdf")
    assert a["total_events"] == 3
    assert a["max_page"] == 2
    assert a["total_latency_ms"] == 360


def test_events_ordered_by_ts_grouped_pages(app):
    ev = app.test_client().get("/api/events?source=/data/a.pdf").get_json()
    pages = [e["page"] for e in ev["events"]]
    assert pages == [1, 2, 2]
    assert ev["events"][0]["decision"] == "diagram"
    assert ev["events"][0]["markdown"] == "md-a1"


def test_events_filter_by_run_id(app):
    ev = app.test_client().get("/api/events?run_id=1").get_json()
    assert len(ev["events"]) == 3
    assert all(e["run_id"] == 1 for e in ev["events"])


def test_errors_returns_only_non_null(app):
    err = app.test_client().get("/api/errors").get_json()
    assert len(err["errors"]) == 1
    assert err["errors"][0]["error"] == "server down"
    assert err["errors"][0]["name"] == "a.pdf"


def test_runs_lists_newest_first(app):
    runs = app.test_client().get("/api/runs").get_json()["runs"]
    assert [r["id"] for r in runs] == [1, 2]
    assert runs[0]["status"] == "ok"
    assert runs[0]["events"] == 3
    assert runs[0]["errors"] == 1


def test_run_phases_include_derived(app):
    ph = app.test_client().get("/api/runs/1/phases").get_json()
    assert [p["phase"] for p in ph["phases"]] == ["convert", "structure"]
    derived = {d["phase"]: d for d in ph["derived"]}
    assert "classify" in derived
    assert "transcribe" in derived
    assert derived["classify"]["count"] == 1


def test_run_config_parses_snapshot(app):
    cfg = app.test_client().get("/api/runs/1/config").get_json()
    assert cfg["config"]["pdf_mode"] == "slide"
    assert cfg["config"]["features"]["vision"] is True


def test_summary_view(app):
    s = app.test_client().get("/api/summary").get_json()
    assert len(s["documents"]) == 1
    assert s["documents"][0]["slide_count"] == 3
    assert s["documents"][0]["chunk_count"] == 1


def test_models_aggregate_latency(app):
    m = app.test_client().get("/api/models").get_json()["models"]
    by_stage = {x["stage"]: x for x in m}
    assert by_stage["classify"]["count"] == 2
    assert by_stage["classify"]["total_ms"] == 150


def test_structure_surfaces_rejections(app):
    st = app.test_client().get("/api/structure").get_json()
    assert len(st["rejections"]) == 1
    assert st["rejections"][0]["stage"] == "structure"


def test_index_returns_html(app):
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert b"PTM Dashboard" in resp.data


def test_missing_db_degrades_to_empty(tmp_path):
    app = create_app(str(tmp_path / "does-not-exist.sqlite"))
    c = app.test_client()
    assert c.get("/api/overview").get_json()["sources"] == []
    assert c.get("/api/errors").get_json()["errors"] == []
    assert c.get("/api/runs").get_json()["runs"] == []


def test_readonly_engine_refuses_writes(tmp_path):
    """The dashboard's read-only engine must reject writes (query_only + mode=ro)."""
    from dashboard.db import make_readonly_engine
    from sqlalchemy import text

    db = _make_db(tmp_path / "ptm.sqlite")
    engine = make_readonly_engine(str(db))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM vision_events")).scalar() == 4
        with pytest.raises(Exception):
            conn.execute(text("INSERT INTO vision_events (ts, source, stage) VALUES ('x', 'y', 'z')"))


def test_pre_orm_db_is_readable(tmp_path):
    """A database built by the pre-ORM raw-sqlite schema stays fully readable."""
    db = _make_db(tmp_path / "ptm.sqlite")
    app = create_app(str(db))
    c = app.test_client()
    assert c.get("/api/overview").get_json()["total_events"] == 4
    assert [r["id"] for r in c.get("/api/runs").get_json()["runs"]] == [1, 2]
    assert c.get("/api/summary").get_json()["documents"][0]["chunk_count"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
