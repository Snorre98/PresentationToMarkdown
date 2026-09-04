"""Tests for the native engine process (``engine.py``, ADR-0025)."""
from __future__ import annotations

import io

import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PTM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("engine._SUPPORTED_EXTENSIONS", {".pptx", ".pdf"})
    monkeypatch.setattr(
        "engine._import_converter",
        lambda: (
            frozenset({".pptx", ".pdf"}),
            _FakeConfig(),
            lambda paths, output_dir, **kw: [],
        ),
    )
    from engine import create_app

    app = create_app()
    return app.test_client()


class _FakeConfig:
    FEATURES = {"vision": None, "summary": None}

    def enabled_keys(self):
        return []

    def feature_endpoints(self, key):
        return []

    def probe(self, url):
        return False

    def missing_servers(self):
        return []

    def snapshot(self, probe=False):
        return {
            "pdf_mode": "slide",
            "features": {"vision": False, "summary": False},
            "passes": {},
            "embed_model": None,
            "missing_servers": [],
        }


def test_engine_health(client):
    h = client.get("/api/health").get_json()
    assert h["ok"] is True
    assert h["engine"] is True


def test_engine_fs_list_directory(tmp_path, client):
    d = tmp_path / "sub"
    d.mkdir()
    (d / "a.pptx").write_bytes(b"x")
    r = client.get(f"/api/fs/list?path={d}").get_json()
    assert r["path"] == str(d)
    names = [e["name"] for e in r["entries"]]
    assert "a.pptx" in names


def test_engine_fs_list_missing_dir(client):
    r = client.get("/api/fs/list?path=/definitely/not/here").get_json()
    assert "error" in r


def test_engine_fs_glob_recursive(tmp_path, client):
    d = tmp_path / "root"
    (d / "nested").mkdir(parents=True)
    (d / "one.pptx").write_bytes(b"x")
    (d / "nested" / "two.pdf").write_bytes(b"y")
    (d / "ignore.txt").write_bytes(b"z")
    r = client.get(f"/api/fs/glob?path={d}").get_json()
    files = [f for f in r["files"]]
    assert len(files) == 2
    assert all(f.endswith((".pptx", ".pdf")) for f in files)


def test_engine_fs_resolve_file(tmp_path, client):
    f = tmp_path / "deck.pptx"
    f.write_bytes(b"x")
    r = client.post("/api/fs/resolve", json={"path": str(f)}).get_json()
    assert r["is_dir"] is False
    assert r["path"] == str(f.resolve())


def test_engine_fs_resolve_missing(client):
    r = client.post("/api/fs/resolve", json={"path": "/nope.pptx"}).get_json()
    assert "error" in r


def test_engine_config_get(client):
    c = client.get("/api/config").get_json()
    assert c["pdf_mode"] in ("slide", "paper")
    assert "features" in c


def test_engine_recent(client):
    r = client.get("/api/recent").get_json()
    assert isinstance(r["recent"], list)


def test_engine_upload_supported(client, tmp_path):
    data = {"files": (io.BytesIO(b"fake-pptx"), "deck.pptx")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert len(r["files"]) == 1
    assert r["files"][0]["name"] == "deck.pptx"
    assert r["files"][0]["path"].startswith(str(tmp_path / "state" / "uploads"))
    assert (tmp_path / "state" / "uploads" / "deck.pptx").exists()


def test_engine_upload_rejects_unsupported(client, tmp_path):
    data = {"files": (io.BytesIO(b"x"), "notes.txt")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert r["files"] == []
    assert r["errors"] and r["errors"][0]["name"] == "notes.txt"
    assert not any((tmp_path / "state" / "uploads").glob("*"))


def test_engine_upload_neutralizes_path_traversal(client, tmp_path):
    data = {"files": (io.BytesIO(b"x"), "../../evil.pptx")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert r["files"][0]["name"] == "evil.pptx"
    assert r["files"][0]["path"].startswith(str(tmp_path / "state" / "uploads"))
    assert (tmp_path / "state" / "uploads" / "evil.pptx").exists()


def test_engine_upload_dedupes_collision(client, tmp_path):
    client.post(
        "/api/fs/upload",
        data={"files": (io.BytesIO(b"x"), "deck.pptx")},
        content_type="multipart/form-data",
    ).get_json()
    r = client.post(
        "/api/fs/upload",
        data={"files": (io.BytesIO(b"y"), "deck.pptx")},
        content_type="multipart/form-data",
    ).get_json()
    assert r["files"][0]["name"] == "deck-1.pptx"


def test_engine_prune_removes_stale_uploads(client, tmp_path):
    up = tmp_path / "state" / "uploads"
    up.mkdir(parents=True)
    old = up / "stale.pptx"
    old.write_bytes(b"x")
    fresh = up / "fresh.pptx"
    fresh.write_bytes(b"y")
    import os
    import time

    now = time.time()
    os.utime(old, (now - 8 * 24 * 3600, now - 8 * 24 * 3600))
    import engine

    engine._prune_uploads(now=now)
    assert not old.exists()
    assert fresh.exists()


def test_engine_shutdown_returns_ok(client, monkeypatch):
    killed = []
    monkeypatch.setattr("engine.os.kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("engine.time.sleep", lambda s: None)
    r = client.post("/api/shutdown").get_json()
    assert r["ok"] is True


def test_engine_pids_filters_self_and_matches_marker(monkeypatch):
    import engine

    me = 12345
    monkeypatch.setattr("engine.os", type("_os", (), {"getpid": lambda: me}))
    fake_ps = type("_r", (), {"stdout": (
        f"{me} python -m engine --port 8090\n"
        "9999 python -m engine --port 8090\n"
        "8888 /usr/bin/python ptm-engine --host 127.0.0.1\n"
        "7777 python -m converter.something\n"
        "6666 python -m engine\n"
        "5555 python dashboard -m engine.py\n"
    )})()
    monkeypatch.setattr("engine.subprocess", type("_sp", (), {
        "run": lambda *a, **k: fake_ps,
        "SubprocessError": Exception,
    })())
    pids = engine._engine_pids()
    assert me not in pids
    assert 9999 in pids
    assert 8888 in pids
    assert 7777 not in pids


def test_engine_kill_running(monkeypatch):
    import engine

    monkeypatch.setattr(engine, "_engine_pids", lambda port=None: [9999, 8888])
    killed = []
    monkeypatch.setattr(engine, "_kill_pid", lambda pid: (killed.append(pid), True)[1])
    assert engine._kill_running_engines() == [9999, 8888]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
