"""Tests for the native engine process (``engine.py``, ADR-0025)."""
from __future__ import annotations

import io
import os

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
            "image_links": os.environ.get("IMAGE_LINKS", "").strip().lower()
            in {"1", "true", "yes", "on"},
            "duplicate": False,
            "vault_root": None,
            "audio_model": "nb-whisper-large",
            "audio_language": "no",
            "audio_diarize": True,
            "audio_speakers": 2,
            "features": {"vision": False, "summary": False},
            "passes": {},
            "embed_model": None,
            "missing_servers": [],
        }


def test_engine_health(client):
    h = client.get("/api/health").get_json()
    assert h["ok"] is True
    assert h["engine"] is True


def test_engine_fleet_projection(client, monkeypatch):
    """The settings modal reads one read-only projection (ADR-0063)."""
    monkeypatch.setattr(
        "converter.fleetview.snapshot",
        lambda cfg: {
            "daemon": {"url": "http://127.0.0.1:9300", "reachable": True},
            "source": "fleet",
            "services": [{"name": "text", "state": "up"}],
            "other_models": 0,
        },
    )
    r = client.get("/api/fleet").get_json()
    assert r["source"] == "fleet"
    assert r["services"][0]["name"] == "text"


def test_create_app_imports_converter_eagerly(monkeypatch):
    """The lazy per-request import deadlocks under Flask's thread pool."""
    import engine

    calls = []
    monkeypatch.setattr(
        "engine._import_converter",
        lambda: (calls.append(1), (frozenset(), _FakeConfig(), lambda *a, **k: []))[1],
    )
    engine.create_app()
    assert calls, "create_app must import converter on one thread"


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


def test_engine_fs_glob_audio(tmp_path, client):
    d = tmp_path / "root"
    (d / "nested").mkdir(parents=True)
    (d / "one.pptx").write_bytes(b"x")
    (d / "lecture.mp3").write_bytes(b"a")
    (d / "nested" / "week-2.m4a").write_bytes(b"b")
    (d / "notes.txt").write_bytes(b"z")
    r = client.get(f"/api/fs/glob?path={d}&kinds=audio").get_json()
    files = [f for f in r["files"]]
    assert len(files) == 2
    assert all(f.endswith((".mp3", ".m4a")) for f in files)


def test_engine_fs_glob_audio_file_target(tmp_path, client):
    audio = tmp_path / "week-2.mp3"
    audio.write_bytes(b"a")
    r = client.get(f"/api/fs/glob?path={audio}&kinds=audio").get_json()
    assert r["files"] == [str(audio.resolve())]


def test_engine_fs_glob_default_excludes_audio(tmp_path, client):
    d = tmp_path / "root"
    d.mkdir()
    (d / "deck.pptx").write_bytes(b"x")
    (d / "deck.mp3").write_bytes(b"a")
    r = client.get(f"/api/fs/glob?path={d}").get_json()
    assert [f for f in r["files"]] == [str((d / "deck.pptx").resolve())]


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
    assert c["image_links"] is False
    assert "features" in c
    assert "vault_root" in c
    assert c["audio_model"] == "nb-whisper-large"
    assert c["audio_language"] == "no"
    assert c["audio_diarize"] is True
    assert c["audio_speakers"] == 2


def test_engine_config_set_image_links(client, monkeypatch):
    monkeypatch.setenv("IMAGE_LINKS", "off")
    stored = {}
    monkeypatch.setattr(
        "converter.settings.set_setting",
        lambda key, value: stored.__setitem__(key, value),
    )
    r = client.post("/api/config", json={"image_links": True}).get_json()
    assert r["image_links"] is True
    assert stored["image_links"] == "on"


def test_engine_config_set_vault_root(client, monkeypatch, tmp_path):
    stored = {}
    monkeypatch.setattr(
        "converter.settings.set_setting",
        lambda key, value: stored.__setitem__(key, value),
    )
    r = client.post("/api/config", json={"vault_root": str(tmp_path / "vault")}).get_json()
    assert r["vault_root"] is not None or "vault_root" in r
    assert stored.get("vault_root") == str(tmp_path / "vault")


def test_engine_config_set_audio(client, monkeypatch):
    stored = {}
    monkeypatch.setattr(
        "converter.settings.set_setting",
        lambda key, value: stored.__setitem__(key, value),
    )
    r = client.post(
        "/api/config",
        json={
            "audio_model": "mlx-community/whisper-large-v3-turbo",
            "audio_language": "auto",
            "audio_diarize": False,
            "audio_speakers": 0,
        },
    ).get_json()
    # Writes are canonicalized too, so the store never regains old HF ids.
    assert stored["audio_model"] == "whisper-large-v3-turbo-asr-fp16"
    assert stored["audio_language"] == "auto"
    assert stored["audio_diarize"] == "off"
    assert stored["audio_speakers"] == "0"
    assert "audio_model" in r and "audio_speakers" in r


def test_engine_config_set_audio_speakers_negative(client, monkeypatch):
    stored = {}
    monkeypatch.setattr(
        "converter.settings.set_setting",
        lambda key, value: stored.__setitem__(key, value),
    )
    client.post("/api/config", json={"audio_speakers": -3}).get_json()
    assert stored["audio_speakers"] == "0"


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
        f"{me} python -m engine --port 9091\n"
        "9999 python -m engine --port 9091\n"
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
        captured.update(kw)
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
    # Markdown lands beside the real on-disk file (ADR-0048) ...
    assert resolver(Path(staged)) == real.parent
    # ... and the resolver names the original as the file to organize.
    source_resolver = captured["source_resolver"]
    assert source_resolver(Path(staged)) == real


def test_job_execute_disables_organizing_for_unknown_uploads(isolated_db, monkeypatch, tmp_path):
    from pathlib import Path

    import engine

    staged = str(tmp_path / "state" / "uploads" / "deck.pdf")
    monkeypatch.setenv("PTM_STATE_DIR", str(tmp_path / "state"))

    captured = {}

    def fake_convert(paths, output_dir, **kw):
        captured["output_dir"] = output_dir
        captured.update(kw)
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
    # Unknown uploads keep the prune-safe uploads/markdown fallback and are
    # never moved or renamed.
    assert captured["output_dir"](Path(staged)) == Path(staged).parent / "markdown"
    assert captured["source_resolver"](Path(staged)) is None


def test_job_execute_organizes_browser_picked_files(isolated_db, monkeypatch, tmp_path):
    from pathlib import Path

    import engine

    picked = tmp_path / "papers" / "Deck.pdf"
    picked.parent.mkdir()
    picked.write_bytes(b"x")
    monkeypatch.setenv("PTM_STATE_DIR", str(tmp_path / "state"))

    captured = {}

    def fake_convert(paths, output_dir, **kw):
        captured["output_dir"] = output_dir
        captured.update(kw)
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

    engine._job_execute(_WS(), [str(picked)], None, False)
    assert captured["output_dir"](picked) == picked.parent
    assert captured["source_resolver"](picked) == picked


class _RecordingWS:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, msg):
        import json

        self.sent.append(json.loads(msg))


def test_engine_status_endpoint(client):
    r = client.get("/api/status").get_json()
    assert "status" in r
    assert "current_job" in r
    assert isinstance(r["jobs"], list)
    assert "job_seq" in r


@pytest.fixture
def engine_state(monkeypatch):
    """Reset the module-global engine job state around a test (ADR-0053)."""
    import engine

    def reset():
        engine._engine_state.update(
            status="idle",
            paths=[],
            output_dir=None,
            duplicate=False,
            results=None,
            log=[],
            current_job=None,
            jobs=[],
            job_seq=0,
        )
        engine._job_running.clear()
        engine._job_cancel.clear()
        engine._watchers.clear()

    reset()
    monkeypatch.setattr("converter.settings.record_recent", lambda path: None)
    monkeypatch.setattr("converter.settings.get_upload_original", lambda path: None)
    yield engine
    reset()


def _fake_converter(fn):
    return lambda: (frozenset(), _FakeConfig(), fn)


def test_job_finish_records_history_and_clears_current(engine_state, monkeypatch, tmp_path):
    from pathlib import Path

    from converter.base import ConvertResult

    def fake_convert(paths, output_dir, **kw):
        p = Path(paths[0])
        return [
            ConvertResult(
                source_path=p,
                md_path=p.with_suffix(".md"),
                moved_to=p.parent / "source" / p.name,
            )
        ]

    monkeypatch.setattr("engine._import_converter", _fake_converter(fake_convert))
    ws = _RecordingWS()
    engine_state._job_execute(ws, [str(tmp_path / "deck.pdf")], str(tmp_path), False)

    state = engine_state._engine_state
    assert state["current_job"] is None
    assert state["status"] == "done"
    job = state["jobs"][0]
    assert job["id"] == 1
    assert job["status"] == "done"
    assert job["started_at"] <= job["finished_at"]
    assert job["results"][0]["md_path"].endswith("deck.md")
    assert job["results"][0]["moved_to"].endswith("source/deck.pdf")
    done = next(m for m in ws.sent if m["type"] == "done")
    assert done["job_id"] == 1


def test_job_ids_increment_and_history_is_newest_first(engine_state, monkeypatch, tmp_path):
    from pathlib import Path

    from converter.base import ConvertResult

    def ok_convert(paths, output_dir, **kw):
        p = Path(paths[0])
        return [ConvertResult(source_path=p, md_path=p.with_suffix(".md"))]

    monkeypatch.setattr("engine._import_converter", _fake_converter(ok_convert))
    engine_state._job_execute(_RecordingWS(), [str(tmp_path / "a.pdf")], str(tmp_path), False)

    def boom(paths, output_dir, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("engine._import_converter", _fake_converter(boom))
    engine_state._job_execute(_RecordingWS(), [str(tmp_path / "b.pdf")], str(tmp_path), False)

    state = engine_state._engine_state
    assert state["current_job"] is None
    assert [j["id"] for j in state["jobs"]] == [2, 1]
    assert state["jobs"][0]["status"] == "error"
    assert state["jobs"][0]["error"] == "kaboom"
    assert state["jobs"][1]["status"] == "done"
    assert state["status"] == "error"


def test_job_history_is_capped(engine_state, monkeypatch, tmp_path):
    from pathlib import Path

    from converter.base import ConvertResult

    def ok_convert(paths, output_dir, **kw):
        p = Path(paths[0])
        return [ConvertResult(source_path=p, md_path=p.with_suffix(".md"))]

    monkeypatch.setattr("engine._import_converter", _fake_converter(ok_convert))
    for _ in range(engine_state._JOBS_HISTORY_MAX + 3):
        engine_state._job_execute(_RecordingWS(), [str(tmp_path / "a.pdf")], str(tmp_path), False)

    state = engine_state._engine_state
    assert len(state["jobs"]) == engine_state._JOBS_HISTORY_MAX
    assert state["jobs"][0]["id"] == engine_state._JOBS_HISTORY_MAX + 3


# --- ADR-0053: watch fan-out, cancel, runs, fs metadata ----------------------


class _BoomWS:
    """A watcher whose socket is gone."""

    def send(self, msg):
        raise RuntimeError("dead socket")


def test_emit_fans_out_once_and_drops_dead_watchers(engine_state):
    a, b = _RecordingWS(), _RecordingWS()
    engine_state._watchers.update({a, b, _BoomWS()})

    # ``a`` is both watcher and origin: it must receive exactly one frame.
    engine_state._emit({"type": "file", "idx": 1}, origin=a)
    assert len(a.sent) == 1
    assert len(b.sent) == 1
    assert len(engine_state._watchers) == 2

    # A non-watching origin still receives its own frames.
    c = _RecordingWS()
    engine_state._emit({"type": "done"}, origin=c)
    assert len(c.sent) == 1


def test_status_snapshot_shape(engine_state):
    engine_state._set_job(reset=True, kind="convert", paths=["/a/x.pdf"])
    snap = engine_state._status_snapshot()
    assert set(snap) == {"status", "job_seq", "current_job", "jobs"}
    assert snap["current_job"]["id"] == engine_state._engine_state["job_seq"]
    assert snap["current_job"]["status"] == "running"
    assert snap["jobs"] == []


def test_jobs_cancel_requires_running_job(client, engine_state):
    r = client.post("/api/jobs/cancel", json={})
    assert r.status_code == 409


def test_jobs_cancel_sets_flag(client, engine_state):
    engine_state._job_running.set()
    engine_state._set_job(reset=True, kind="convert", paths=["/a/deck.pdf"])
    job_id = engine_state._engine_state["current_job"]["id"]

    r = client.post("/api/jobs/cancel", json={"id": job_id}).get_json()
    assert r["ok"] is True
    assert r["job_id"] == job_id
    assert engine_state._job_cancel.is_set()
    assert engine_state._engine_state["current_job"]["cancel_requested"] is True

    # A mismatching id is refused.
    engine_state._job_cancel.clear()
    r = client.post("/api/jobs/cancel", json={"id": job_id + 99})
    assert r.status_code == 409


def test_job_cancel_stops_between_pages(engine_state, monkeypatch, tmp_path):
    from pathlib import Path

    from converter.base import ConvertResult

    def cancel_midway(paths, output_dir, progress_callback=None, page_progress_callback=None, **kw):
        page_progress_callback(1, 10, "deck.pdf")
        engine_state._job_cancel.set()
        page_progress_callback(2, 10, "deck.pdf")  # raises _JobCancelled
        return [ConvertResult(source_path=Path(paths[0]))]

    monkeypatch.setattr("engine._import_converter", _fake_converter(cancel_midway))
    ws = _RecordingWS()
    engine_state._job_execute(ws, [str(tmp_path / "deck.pdf")], str(tmp_path), False)

    state = engine_state._engine_state
    assert state["current_job"] is None
    assert state["status"] == "cancelled"
    assert state["jobs"][0]["status"] == "cancelled"
    assert [m["type"] for m in ws.sent].count("cancelled") == 1
    assert ws.sent[-1]["job_id"] == state["jobs"][0]["id"]


def test_fs_glob_entries_carry_metadata(client, tmp_path):
    d = tmp_path / "root"
    (d / "nested").mkdir(parents=True)
    (d / "deck.pptx").write_bytes(b"12345")
    (d / "nested" / "two.pdf").write_bytes(b"x")
    r = client.get(f"/api/fs/glob?path={d}").get_json()
    by_name = {e["name"]: e for e in r["entries"]}
    assert by_name["deck.pptx"]["kind"] == "convert"
    assert by_name["deck.pptx"]["size"] == 5
    assert by_name["deck.pptx"]["is_dir"] is False
    assert by_name["two.pdf"]["mtime"] > 0
    assert sorted(by_name) == ["deck.pptx", "two.pdf"]


def test_fs_glob_marks_latex_project_folder(client, tmp_path):
    proj = tmp_path / "paper"
    proj.mkdir()
    (proj / "main.tex").write_text("\\documentclass{article}")
    r = client.get(f"/api/fs/glob?path={tmp_path}").get_json()
    by_name = {e["name"]: e for e in r["entries"]}
    assert by_name["paper"]["kind"] == "latex-project"
    assert by_name["paper"]["is_dir"] is True


def test_fs_glob_audio_entries(client, tmp_path):
    d = tmp_path / "root"
    d.mkdir()
    (d / "talk.m4a").write_bytes(b"abc")
    r = client.get(f"/api/fs/glob?path={d}&kinds=audio").get_json()
    assert [e["kind"] for e in r["entries"]] == ["audio"]
    assert r["entries"][0]["size"] == 3


def test_fs_open_reveal(monkeypatch, tmp_path):
    import engine

    f = tmp_path / "out.md"
    f.write_text("x")
    calls = []
    monkeypatch.setattr("engine.subprocess.Popen", lambda cmd: calls.append(cmd))

    r = engine._fs_open(str(f), reveal=True)
    assert r["ok"] is True
    assert calls == [["open", "-R", str(f)]]

    r = engine._fs_open(str(f))
    assert calls[-1] == ["open", str(f)]


def test_runs_endpoint_lists_recent_runs(client, isolated_db):
    from converter.db import repos

    run_id = repos.start_run("/a/deck.pdf")
    repos.finish_run(run_id, "ok")
    r = client.get("/api/runs?limit=5").get_json()
    assert r["runs"][0]["source"] == "/a/deck.pdf"
    assert r["runs"][0]["status"] == "ok"
    assert r["runs"][0]["id"] == run_id


def test_map_phase():
    import engine

    assert engine._map_phase("transcribing with nb-whisper-large …") == "transcribe"
    assert engine._map_phase("diarizing …") == "diarize"
    assert engine._map_phase("ffmpeg: cleaning audio …") == "cleaning"
    assert engine._map_phase("some raw stdout line") is None


def test_transcribe_execute_streams_structured_frames(monkeypatch, tmp_path):
    import threading

    import converter.transcribe as T
    import engine

    monkeypatch.setattr(engine, "_job_running", threading.Event())
    monkeypatch.setattr(engine, "_apply_audio_options", lambda options: None)
    fake_lock = type("_L", (), {"held": True, "release": lambda self: None})()
    monkeypatch.setattr("lock.acquire_transcribe_lock", lambda: fake_lock)

    def fake_attach(md, warnings, audio_path=None, on_line=None, **kw):
        if on_line is not None:
            on_line("transcribing with nb-whisper-large …\n")
            on_line("diarizing …\n")
        return [{"start": 0.0, "end": 1.0, "text": "hi"}]

    monkeypatch.setattr(T, "attach_transcript", fake_attach)
    monkeypatch.setattr(
        T, "transcribe_to_markdown", lambda p, warnings, on_line=None, **kw: None
    )

    ws = _RecordingWS()
    engine._transcribe_execute(ws, ["/tmp/deck.md"], {"model": "nb-whisper-large"})

    types = [m["type"] for m in ws.sent]
    assert "file" in types and "done" in types
    phases = [m["phase"] for m in ws.sent if m["type"] == "phase"]
    assert "transcribe" in phases and "diarize" in phases
    done = next(m for m in ws.sent if m["type"] == "done")
    assert done["ok"] == 1 and done["total"] == 1


def test_transcribe_execute_failure_is_not_counted_ok(monkeypatch):
    """A dead ASR daemon yields a warning and ok=0, with no fallback lane."""
    import threading

    import converter.transcribe as T
    import engine

    monkeypatch.setattr(engine, "_job_running", threading.Event())
    monkeypatch.setattr(engine, "_apply_audio_options", lambda options: None)
    fake_lock = type("_L", (), {"held": True, "release": lambda self: None})()
    monkeypatch.setattr("lock.acquire_transcribe_lock", lambda: fake_lock)

    def fake_attach(md, warnings, audio_path=None, on_line=None, **kw):
        warnings.append("Audio transcription failed: whisper daemon down")
        return None

    monkeypatch.setattr(T, "attach_transcript", fake_attach)
    ws = _RecordingWS()
    engine._transcribe_execute(ws, ["/tmp/deck.md"], {"model": "whisper-large-v3"})

    done = next(m for m in ws.sent if m["type"] == "done")
    assert done["ok"] == 0 and done["total"] == 1
    assert any(
        m["type"] == "log" and m.get("kind") == "warn" and "whisper daemon down" in m["message"]
        for m in ws.sent
    )


def test_transcribe_execute_rejects_when_lock_held(monkeypatch):
    import threading

    import engine

    monkeypatch.setattr(engine, "_job_running", threading.Event())
    monkeypatch.setattr(engine, "_apply_audio_options", lambda options: None)
    fake_lock = type("_L", (), {"held": False, "pid": 42})()
    monkeypatch.setattr("lock.acquire_transcribe_lock", lambda: fake_lock)

    ws = _RecordingWS()
    engine._transcribe_execute(ws, ["/tmp/x.m4a"], None)

    assert any(m["type"] == "error" for m in ws.sent)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
