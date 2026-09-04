"""Tests for the conversion-run telemetry in ``converter.logstore`` (ADR-0022)."""
from __future__ import annotations

import json

import pytest

from converter import logstore
from converter.db import engine as db_engine


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("VISION_LOG_DB", str(tmp_path / "test.sqlite"))
    db_engine.reset()
    yield tmp_path / "test.sqlite"
    db_engine.reset()


def _query(sql: str, params: tuple = ()):
    engine = db_engine.get_engine()
    with engine.connect() as conn:
        return conn.connection.driver_connection.execute(sql, params).fetchall()


def _events():
    return [tuple(r) for r in _query("SELECT source, stage, run_id FROM vision_events ORDER BY id")]


def test_run_start_tags_events(isolated_db):
    run_id = logstore.run_start("/data/deck.pptx", "deck.pptx")
    assert run_id is not None
    logstore.record(source="/data/deck.pptx", stage="classify", decision="text")
    logstore.record(source="/data/deck.pptx", stage="transcribe", decision=None)
    logstore.run_finish(run_id, "ok")

    rows = _events()
    assert rows == [
        ("/data/deck.pptx", "classify", run_id),
        ("/data/deck.pptx", "transcribe", run_id),
    ]


def test_run_finish_clears_context(isolated_db):
    run_id = logstore.run_start("/data/a.pptx")
    logstore.run_finish(run_id, "ok")
    logstore.record(source="/data/a.pptx", stage="classify")
    rows = _events()
    assert rows[0][2] is None


def test_run_finish_records_status_and_duration(isolated_db):
    run_id = logstore.run_start("/data/a.pptx")
    logstore.run_finish(run_id, "error")
    row = _query(
        "SELECT status, ended_at, duration_ms FROM conversion_runs WHERE id = ?",
        (run_id,),
    )[0]
    assert row[0] == "error"
    assert row[1] is not None
    assert row[2] is not None


def test_phases_record_start_end(isolated_db):
    run_id = logstore.run_start("/data/a.pptx")
    with logstore.phase(run_id, "convert", 1):
        pass
    with logstore.phase(run_id, "format", 3):
        pass
    logstore.run_finish(run_id, "ok")

    rows = _query(
        "SELECT phase, ordinal, status, duration_ms FROM run_phases ORDER BY ordinal"
    )
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("convert", 1, "done"),
        ("format", 3, "done"),
    ]
    assert all(r[3] is not None for r in rows)


def test_phase_marks_failed_and_reraises(isolated_db):
    run_id = logstore.run_start("/data/a.pptx")
    with pytest.raises(RuntimeError):
        with logstore.phase(run_id, "convert", 1):
            raise RuntimeError("boom")
    logstore.run_finish(run_id, "error")
    row = _query("SELECT status FROM run_phases WHERE phase = 'convert'")[0]
    assert row[0] == "failed"


def test_run_snapshot_round_trips_json(isolated_db):
    run_id = logstore.run_start("/data/a.pptx")
    logstore.run_snapshot(run_id, {"pdf_mode": "paper", "features": {"vision": True}})
    logstore.run_finish(run_id, "ok")
    raw = _query("SELECT snapshot FROM run_config WHERE run_id = ?", (run_id,))[0][0]
    assert json.loads(raw)["pdf_mode"] == "paper"


def test_disabled_logging_is_noop(monkeypatch, isolated_db):
    monkeypatch.setattr(logstore, "VISION_LOG_ENABLED", False)
    run_id = logstore.run_start("/data/a.pptx")
    assert run_id is None
    logstore.record(source="/data/a.pptx", stage="classify")
    logstore.run_finish(None, "ok")
    assert _events() == []


def test_migrate_adds_run_id_column(tmp_path, monkeypatch):
    import sqlite3

    from converter.db.engine import SCHEMA_VERSION

    plain = tmp_path / "old.sqlite"
    conn = sqlite3.connect(plain)
    conn.executescript(
        """
        CREATE TABLE vision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            page INTEGER,
            image_ref TEXT,
            image_digest TEXT,
            stage TEXT NOT NULL,
            model TEXT,
            decision TEXT,
            raw_answer TEXT,
            latency_ms INTEGER,
            prompt_tokens INTEGER,
            generated_tokens INTEGER,
            markdown TEXT,
            omitted_words TEXT,
            error TEXT,
            base_url TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("VISION_LOG_DB", str(plain))
    db_engine.reset()
    db_engine.get_engine()

    from sqlalchemy import create_engine, text

    ro = create_engine("sqlite:///" + str(plain))
    with ro.connect() as c:
        cols = {
            r[1] for r in c.execute(text("PRAGMA table_info(vision_events)"))
        }
    assert "run_id" in cols
    assert SCHEMA_VERSION == 2


def test_pre_orm_db_opens_and_accepts_new_writes(tmp_path, monkeypatch):
    """A pre-ORM ``ptm.sqlite`` opens unchanged and coexists with ORM writes."""
    import sqlite3

    from converter import settings

    plain = tmp_path / "pre_orm.sqlite"
    conn = sqlite3.connect(plain)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE vision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, source TEXT NOT NULL, page INTEGER,
            image_ref TEXT, image_digest TEXT, stage TEXT NOT NULL,
            model TEXT, decision TEXT, raw_answer TEXT, latency_ms INTEGER,
            prompt_tokens INTEGER, generated_tokens INTEGER, markdown TEXT,
            omitted_words TEXT, error TEXT, base_url TEXT, run_id INTEGER
        );
        INSERT INTO vision_events (ts, source, stage) VALUES ('t', '/old.pdf', 'classify');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("VISION_LOG_DB", str(plain))
    db_engine.reset()

    settings.set_setting("legacy", "kept")
    logstore.record(source="/data/new.pdf", stage="transcribe")
    rows = _query(
        "SELECT source, stage FROM vision_events ORDER BY id"
    )
    assert ("/old.pdf", "classify") in rows
    assert settings.get_setting("legacy") == "kept"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
