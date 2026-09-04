"""Tests for the web UI's engine gateway (``dashboard`` app, ADR-0025).

The engine integration routes proxy to a running engine and expose its status.
These tests verify the gateway behaviours without a real engine: the health/start
routes degrade gracefully, and the read-only history routes still work.
"""
from __future__ import annotations

import pytest

import dashboard.app as appmod
from dashboard import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "_engine_alive", lambda: False)
    monkeypatch.setattr(appmod, "_spawn_engine", lambda: {"ok": True, "pid": 1, "base_url": "http://127.0.0.1:8090"})
    return create_app(str(tmp_path / "ptm.sqlite"))


def test_engine_status_reports_stopped(app):
    r = app.test_client().get("/api/engine").get_json()
    assert r["running"] is False
    assert r["base_url"].startswith("http://")


def test_proxy_returns_503_when_engine_stopped(app):
    c = app.test_client()
    assert c.get("/api/engine/config").status_code == 503
    assert c.get("/api/engine/fs/list?path=/").status_code == 503
    assert c.get("/api/engine/recent").status_code == 503
    assert c.post("/api/engine/fs/upload").status_code == 503


def test_health_still_serves_read_only(app):
    h = app.test_client().get("/api/health").get_json()
    assert h["ok"] is True


def test_engine_start_delegates_to_spawn(app):
    r = app.test_client().post("/api/engine/start").get_json()
    assert r["ok"] is True


def test_engine_stop_returns_ok_when_stopped(app):
    r = app.test_client().post("/api/engine/stop").get_json()
    assert r["ok"] is True
    assert r["stopped_pid"] is None


def test_engine_stop_signals_recorded_pid(app, monkeypatch):
    import os
    import signal

    monkeypatch.setattr(appmod, "_engine_alive", lambda: True)
    appmod._engine_state["pid"] = 4242
    appmod._engine_state["process"] = object()
    appmod._engine_state["started_at"] = 1.0
    received = {}
    monkeypatch.setattr(os, "kill", lambda pid, sig: received.update(pid=pid, sig=sig))
    r = app.test_client().post("/api/engine/stop").get_json()
    assert received["pid"] == 4242
    assert received["sig"] == signal.SIGTERM
    assert r["stopped_pid"] == 4242
    assert appmod._engine_state["pid"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
