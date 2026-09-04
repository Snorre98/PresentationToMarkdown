"""Tests for the native engine process (``engine.py``, ADR-0025)."""
from __future__ import annotations

import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
