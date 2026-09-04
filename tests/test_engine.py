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
            "duplicate": False,
            "vault_root": None,
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
    assert "vault_root" in c


def test_engine_config_set_vault_root(client, monkeypatch, tmp_path):
    stored = {}
    monkeypatch.setattr(
        "converter.settings.set_setting",
        lambda key, value: stored.__setitem__(key, value),
    )
    r = client.post("/api/config", json={"vault_root": str(tmp_path / "vault")}).get_json()
    assert r["vault_root"] is not None or "vault_root" in r
    assert stored.get("vault_root") == str(tmp_path / "vault")


def test_engine_upload_no_original_sets_fallback_dir(client, tmp_path):
    import engine

    # no recent_files, no vault root -> unresolved -> fallback_dir set
    data = {"files": (io.BytesIO(b"fake"), "fresh.pdf")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert len(r["files"]) == 1
    f = r["files"][0]
    assert f["original"] is None
    assert f["fallback_dir"].endswith("uploads" + "/markdown")


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


def test_engine_upload_multiple_files(client, tmp_path):
    data = {"files": [
        (io.BytesIO(b"fake-pptx-a"), "a.pptx"),
        (io.BytesIO(b"fake-pdf-b"), "b.pdf"),
    ]}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert len(r["files"]) == 2
    names = {e["name"] for e in r["files"]}
    assert names == {"a.pptx", "b.pdf"}
    assert (tmp_path / "state" / "uploads" / "a.pptx").exists()
    assert (tmp_path / "state" / "uploads" / "b.pdf").exists()


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


def test_resolve_original_matches_recent_basename(monkeypatch, tmp_path):
    import engine

    real = tmp_path / "real" / "03 What makes things fun to learn.pdf"
    real.parent.mkdir()
    real.write_bytes(b"x")
    uploads = tmp_path / "state" / "uploads"

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [
            str(real),
            str(uploads / "03 What makes things fun to learn.pdf"),
        ],
    )
    got = engine._resolve_original("03 What makes things fun to learn.pdf", uploads)
    assert got == real


def test_resolve_original_skips_staging_and_missing(monkeypatch, tmp_path):
    import engine

    uploads = tmp_path / "state" / "uploads"
    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [
            str(uploads / "deck.pptx"),
            str(tmp_path / "gone" / "deck.pptx"),
        ],
    )
    assert engine._resolve_original("deck.pptx", uploads) is None


def test_resolve_original_strips_dedup_suffix(monkeypatch, tmp_path):
    import engine

    real = tmp_path / "real" / "deck.pdf"
    real.parent.mkdir()
    real.write_bytes(b"x")
    uploads = tmp_path / "state" / "uploads"

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [str(real)],
    )
    got = engine._resolve_original("deck-2.pdf", uploads)
    assert got == real


def test_resolve_original_prefers_size_equal(monkeypatch, tmp_path):
    import engine

    small = tmp_path / "real" / "deck-1.pdf"
    small.parent.mkdir()
    small.write_bytes(b"tiny")
    big = tmp_path / "real" / "deck-3.pdf"
    big.write_bytes(b"a much larger payload")

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [str(big), str(small)],
    )
    got = engine._resolve_original("deck-9.pdf", tmp_path / "state" / "uploads", size=len(b"a much larger payload"))
    assert got == big


def test_resolve_original_prefers_exact_over_stripped(monkeypatch, tmp_path):
    import engine

    real_exact = tmp_path / "real" / "deck-2.pdf"
    real_exact.parent.mkdir()
    real_exact.write_bytes(b"x")
    real_plain = tmp_path / "real" / "deck.pdf"
    real_plain.write_bytes(b"x")
    uploads = tmp_path / "state" / "uploads"

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [str(real_plain), str(real_exact)],
    )
    got = engine._resolve_original("deck-2.pdf", uploads)
    assert got == real_exact


def test_resolve_original_size_is_preference_not_filter(monkeypatch, tmp_path):
    import engine

    real = tmp_path / "real" / "deck.pdf"
    real.parent.mkdir()
    real.write_bytes(b"whatever size")
    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [str(real)],
    )
    got = engine._resolve_original("deck.pdf", tmp_path / "state" / "uploads", size=123456789)
    assert got == real


def test_resolve_original_vault_root_scan(monkeypatch, tmp_path):
    import engine

    vault = tmp_path / "vault"
    real = vault / "lectures" / "cluster presentation 2026-1.pdf"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"fresh-drop")
    uploads = tmp_path / "state" / "uploads"

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [],
    )
    monkeypatch.setattr(
        "converter.settings.get_setting",
        lambda key, default="": str(vault) if key == "vault_root" else default,
    )
    got = engine._resolve_original("cluster presentation 2026-1.pdf", uploads, size=len(b"fresh-drop"))
    assert got == real


def test_resolve_original_vault_root_scan_miss(monkeypatch, tmp_path):
    import engine

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "unrelated.pdf").write_bytes(b"x")
    uploads = tmp_path / "state" / "uploads"

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [],
    )
    monkeypatch.setattr(
        "converter.settings.get_setting",
        lambda key, default="": str(vault) if key == "vault_root" else default,
    )
    assert engine._resolve_original("nope.pdf", uploads) is None


def test_resolve_original_vault_root_strips_suffix(monkeypatch, tmp_path):
    import engine

    vault = tmp_path / "vault"
    real = vault / "deck.pdf"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"x")
    uploads = tmp_path / "state" / "uploads"

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [],
    )
    monkeypatch.setattr(
        "converter.settings.get_setting",
        lambda key, default="": str(vault) if key == "vault_root" else default,
    )
    got = engine._resolve_original("deck-3.pdf", uploads)
    assert got == real


def test_resolve_original_no_vault_root_returns_none(monkeypatch, tmp_path):
    import engine

    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [],
    )
    monkeypatch.setattr(
        "converter.settings.get_setting",
        lambda key, default="": None if key == "vault_root" else default,
    )
    assert engine._resolve_original("deck.pdf", tmp_path / "state" / "uploads") is None


def test_fs_upload_resolves_original_when_name_collides(client, isolated_db, monkeypatch, tmp_path):
    real = tmp_path / "real" / "deck.pdf"
    real.parent.mkdir()
    real.write_bytes(b"x")
    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [str(real)],
    )
    data = {"files": (io.BytesIO(b"x"), "deck.pdf")}
    client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    data2 = {"files": (io.BytesIO(b"x"), "deck.pdf")}
    r = client.post("/api/fs/upload", data=data2, content_type="multipart/form-data").get_json()
    assert r["files"][0]["name"].startswith("deck-")
    assert r["files"][0]["original"] == str(real.resolve())


def test_fs_upload_persists_and_returns_original(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [str(tmp_path / "real" / "deck.pdf")],
    )
    real = tmp_path / "real" / "deck.pdf"
    real.parent.mkdir()
    real.write_bytes(b"x")

    import engine

    data = {"files": (io.BytesIO(b"fake"), "deck.pdf")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert r["files"][0]["original"] == str(real.resolve())
    from converter.settings import get_upload_original

    assert get_upload_original(r["files"][0]["path"]) == str(real.resolve())


def test_fs_upload_no_original_when_unknown(client, tmp_path):
    data = {"files": (io.BytesIO(b"fake"), "deck.pdf")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert r["files"][0]["original"] is None


def test_prune_deletes_upload_original_meta(monkeypatch, tmp_path):
    import engine
    import os
    import time

    up = tmp_path / "state" / "uploads"
    up.mkdir(parents=True)
    stale = up / "old.pptx"
    stale.write_bytes(b"x")
    from converter.settings import set_upload_original

    set_upload_original(str(stale), "/some/real/old.pptx")

    now = time.time()
    os.utime(stale, (now - 8 * 24 * 3600, now - 8 * 24 * 3600))
    engine._prune_uploads(now=now)

    from converter.settings import get_upload_original

    assert get_upload_original(str(stale)) is None


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_LOG_DB", str(tmp_path / "engine_test.sqlite"))
    from converter.db import engine as db_engine

    db_engine.reset()
    yield
    db_engine.reset()


def test_fs_upload_persists_and_returns_original(client, isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "converter.settings.recent_files",
        lambda limit=10: [str(tmp_path / "real" / "deck.pdf")],
    )
    real = tmp_path / "real" / "deck.pdf"
    real.parent.mkdir()
    real.write_bytes(b"x")

    data = {"files": (io.BytesIO(b"fake"), "deck.pdf")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert r["files"][0]["original"] == str(real.resolve())
    from converter.settings import get_upload_original

    assert get_upload_original(r["files"][0]["path"]) == str(real.resolve())


def test_fs_upload_no_original_when_unknown(client, isolated_db, tmp_path):
    data = {"files": (io.BytesIO(b"fake"), "deck.pdf")}
    r = client.post("/api/fs/upload", data=data, content_type="multipart/form-data").get_json()
    assert r["files"][0]["original"] is None


def test_prune_deletes_upload_original_meta(isolated_db, monkeypatch, tmp_path):
    import engine
    import os
    import time

    monkeypatch.setenv("PTM_STATE_DIR", str(tmp_path / "state"))
    up = tmp_path / "state" / "uploads"
    up.mkdir(parents=True)
    stale = up / "old.pptx"
    stale.write_bytes(b"x")
    from converter.settings import set_upload_original

    set_upload_original(str(stale), "/some/real/old.pptx")

    now = time.time()
    os.utime(stale, (now - 8 * 24 * 3600, now - 8 * 24 * 3600))
    engine._prune_uploads(now=now)

    from converter.settings import get_upload_original

    assert get_upload_original(str(stale)) is None


def test_job_execute_uses_resolver_when_no_output_dir(isolated_db, monkeypatch, tmp_path):
    from pathlib import Path

    import engine

    real = tmp_path / "real" / "deck.pdf"
    real.parent.mkdir()
    real.write_bytes(b"x")
    staged = str(tmp_path / "state" / "uploads" / "deck.pdf")

    from converter.settings import set_upload_original

    set_upload_original(staged, str(real))
    monkeypatch.setenv("PTM_STATE_DIR", str(tmp_path / "state"))

    captured = {}

    def fake_convert(paths, output_dir, **kw):
        captured["output_dir"] = output_dir
        return []

    monkeypatch.setattr(
        "engine._import_converter",
        lambda: (frozenset(), _FakeConfig(), fake_convert),
    )
    import converter.settings as settings_mod

    monkeypatch.setattr(settings_mod, "record_recent", lambda p: None)

    class _WS:
        def send(self, msg):
            pass

    engine._job_execute(_WS(), [staged], None, False)
    resolver = captured["output_dir"]
    assert callable(resolver)
    assert resolver(Path(staged)) == real.parent / "markdown"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
