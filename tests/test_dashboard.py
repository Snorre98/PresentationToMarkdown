"""Tests for the read-only conversion-log dashboard (``dashboard.py``).

Spins the stdlib HTTP server against a throwaway SQLite database and asserts the
JSON endpoints return the expected rows. The server is imported from the repo
root (``pythonpath = ["."]`` in ``pyproject.toml``) and never imports
``converter``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dashboard

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
    base_url         TEXT
);
CREATE TABLE recent_files (
    path      TEXT PRIMARY KEY,
    last_used TEXT NOT NULL
);
"""


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        # source A (recent) — two pages, one error
        ("/data/a.pdf", 1, "classify", "m", "diagram", 120, "md-a1", None, "2026-09-02T10:00:01+00:00"),
        ("/data/a.pdf", 2, "transcribe", "m", None, 240, "md-a2", None, "2026-09-02T10:00:02+00:00"),
        ("/data/a.pdf", 2, "structure", "m", "reworded", None, None, "server down", "2026-09-02T10:00:03+00:00"),
        # source B (not recent)
        ("/data/b.pdf", 1, "classify", "m", "text", 30, "md-b1", None, "2026-09-02T09:00:00+00:00"),
    ]
    conn.executemany(
        """
        INSERT INTO vision_events
            (source, page, stage, model, decision, latency_ms, markdown, error, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def server(tmp_path):
    db = _make_db(tmp_path / "ptm.sqlite")
    srv = dashboard.serve(str(db), host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_health_reports_event_count(server):
    health = _get(f"{server}/api/health")
    assert health["ok"] is True
    assert health["total_events"] == 4


def test_overview_lists_sources_recent_first(server):
    ov = _get(f"{server}/api/overview")
    assert ov["total_events"] == 4
    names = [s["name"] for s in ov["sources"]]
    assert names == ["a.pdf", "b.pdf"]

    a = next(s for s in ov["sources"] if s["source"] == "/data/a.pdf")
    assert a["total_events"] == 3
    assert a["max_page"] == 2
    assert a["total_latency_ms"] == 360


def test_events_ordered_by_ts_grouped_pages(server):
    ev = _get(f"{server}/api/events?source=/data/a.pdf")
    pages = [e["page"] for e in ev["events"]]
    assert pages == [1, 2, 2]
    assert ev["events"][0]["decision"] == "diagram"
    assert ev["events"][0]["markdown"] == "md-a1"


def test_errors_returns_only_non_null(server):
    err = _get(f"{server}/api/errors")
    assert len(err["errors"]) == 1
    assert err["errors"][0]["error"] == "server down"
    assert err["errors"][0]["name"] == "a.pdf"


def test_missing_db_degrades_to_empty(server, tmp_path):
    missing = dashboard._overview(str(tmp_path / "does-not-exist.sqlite"))
    assert missing["sources"] == []
    assert missing["total_events"] == 0
    assert dashboard._errors(str(tmp_path / "does-not-exist.sqlite"))["errors"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
