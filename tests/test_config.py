"""Tests for the runtime feature/config registry (converter.config)."""
from __future__ import annotations

import urllib.request

import pytest

from converter import config


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset()
    yield
    config.reset()


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_features_default_off():
    assert config.enabled_keys() == []
    assert not config.is_enabled("vision")


def test_set_enabled_and_implies():
    config.set_enabled("classify", True)
    assert config.is_enabled("classify")
    assert config.is_enabled("vision")  # classify implies vision

    config.set_enabled("vision", False)  # un-checking vision disables classify
    assert not config.is_enabled("vision")
    assert not config.is_enabled("classify")


def test_set_many_and_enabled_keys_order():
    config.set_many({"summary": True, "format": True})
    assert config.enabled_keys() == ["format", "summary"]


def test_set_enabled_unknown_raises():
    with pytest.raises(KeyError):
        config.set_enabled("nope", True)


def test_reset_rereads_environment(monkeypatch):
    monkeypatch.setenv("VISION_ENABLED", "1")
    config.reset()
    assert config.is_enabled("vision")
    monkeypatch.delenv("VISION_ENABLED")
    config.reset()
    assert not config.is_enabled("vision")


def test_server_catalog_maps_features_to_manifest_daemons():
    # ADR-0029: the catalog is the offline projection of the fleet manifest
    # (macos-dev-config/models.json); each feature names the manifest daemon it
    # is served by, and health/start go through the control daemon.
    assert config.SERVERS["transcriber"].daemon_name == "transcriber"
    assert config.SERVERS["transcriber"].port == 8081
    assert (
        config.SERVERS["transcriber"].start_command
        == "curl -X POST http://127.0.0.1:9300/start/transcriber"
    )
    assert config.SERVERS["summary"].daemon_name == "text"  # summary rides the `text` daemon
    assert config.SERVERS["summary"].port == 8083
    assert config.SERVERS["structure-text"].daemon_name == "text"
    assert config.SERVERS["structure-text"].port == 8083  # was :8085 — collided with mistral-24b
    assert config.SERVERS["nomic-embed"].port == 8090  # was ollama :11434
    assert config.SERVERS["classifier"].description == "Classifier gate (PresentationToMarkdown)"


def test_feature_endpoints_format_falls_back_to_write():
    vision = config.feature_endpoints("vision")
    assert vision == [("transcriber", "http://127.0.0.1:8081/v1")]
    format_ = config.feature_endpoints("format")
    assert format_[0][0] == "transcriber"
    assert format_[0][1] == vision[0][1]


def test_feature_endpoints_follow_write_not_vision(monkeypatch):
    monkeypatch.setenv("WRITE_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("VISION_BASE_URL", "http://127.0.0.1:8081/v1")
    assert config.feature_endpoints("vision")[0][1] == "http://127.0.0.1:8081/v1"
    for key in ("format", "interpret", "structure"):
        assert config.feature_endpoints(key)[0][1] == "http://127.0.0.1:9999/v1"
    # Summary is served by the manifest's `text` daemon (:8083, ADR-0029).
    assert config.feature_endpoints("summary")[0][1] == "http://127.0.0.1:8083/v1"


def test_writer_models_follow_write_not_vision():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = (
        "import os\n"
        "os.environ['VISION_MODEL'] = 'glm-ocr'\n"
        "os.environ['WRITE_MODEL'] = 'writer-model'\n"
        "import converter.format as f\n"
        "import converter.structure as s\n"
        "import converter.interpret as i\n"
        "print(f.FORMAT_MODEL, s.STRUCTURE_MODEL, i.INTERPRET_MODEL, sep='|')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    )
    assert out.stdout.strip().split("|") == ["writer-model"] * 3


def test_summary_model_is_dedicated_not_write():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = (
        "import os\n"
        "os.environ['WRITE_MODEL'] = 'writer-model'\n"
        "import converter.summary as m\n"
        "print(m.SUMMARY_MODEL, sep='|')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    )
    assert out.stdout.strip() == "mlx-community/Llama-3.2-3B-Instruct-4bit"


def test_probe_up(monkeypatch):
    monkeypatch.setattr(
        "converter.config.urllib.request.urlopen", lambda req, timeout: _Resp()
    )
    assert config.probe("http://127.0.0.1:8081/v1") is True


def test_probe_down(monkeypatch):
    def _boom(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("converter.config.urllib.request.urlopen", _boom)
    assert config.probe("http://127.0.0.1:8081/v1") is False


def test_missing_servers_reports_enabled_down(monkeypatch):
    monkeypatch.setattr(config, "probe", lambda url, timeout=1.5: False)
    config.set_enabled("vision", True)
    missing = config.missing_servers()
    assert (
        "transcriber",
        "http://127.0.0.1:8081/v1",
        "curl -X POST http://127.0.0.1:9300/start/transcriber",
    ) in missing


def test_missing_servers_empty_when_nothing_enabled(monkeypatch):
    monkeypatch.setattr(config, "probe", lambda url, timeout=1.5: True)
    assert config.missing_servers() == []


def test_snapshot_is_json_serialisable_and_describes_state():
    import json

    config.set_enabled("summary", True)
    snap = config.snapshot(probe=False)
    json.dumps(snap)  # must not raise

    assert snap["pdf_mode"] in ("slide", "paper")
    assert snap["features"]["summary"] is True
    assert snap["features"]["vision"] is False

    summary_pass = snap["passes"]["summary"]
    assert summary_pass["enabled"] is True
    assert summary_pass["endpoints"][0]["server"] == "summary"
    assert summary_pass["endpoints"][0]["base_url"] == "http://127.0.0.1:8083/v1"
    assert summary_pass["model"] == "mlx-community/Llama-3.2-3B-Instruct-4bit"
    assert snap["embed_model"] is not None


def test_snapshot_omits_endpoints_when_disabled():
    snap = config.snapshot(probe=False)
    assert snap["passes"]["format"]["enabled"] is False
    assert snap["passes"]["format"]["endpoints"] == []
    assert snap["passes"]["format"]["model"] is None
    assert snap["embed_model"] is None


def test_snapshot_never_raises_even_with_no_servers(monkeypatch):
    monkeypatch.setattr(config, "probe", lambda url, timeout=1.5: False)
    config.set_enabled("vision", True)
    snap = config.snapshot(probe=True)
    assert snap["missing_servers"]  # down transcriber recorded


def test_snapshot_reports_duplicate_preference(tmp_path, monkeypatch):
    from converter.db import engine as db_engine
    from converter import settings

    monkeypatch.setenv("VISION_LOG_DB", str(tmp_path / "config_test.sqlite"))
    db_engine.reset()
    try:
        assert config.snapshot(probe=False)["duplicate"] is False
        settings.set_setting("duplicate_if_exists", "on")
        assert config.snapshot(probe=False)["duplicate"] is True
        settings.set_setting("duplicate_if_exists", "off")
        assert config.snapshot(probe=False)["duplicate"] is False
    finally:
        db_engine.reset()


def test_snapshot_reports_audio_defaults(tmp_path, monkeypatch):
    from converter.db import engine as db_engine

    monkeypatch.setenv("VISION_LOG_DB", str(tmp_path / "config_audio.sqlite"))
    db_engine.reset()
    try:
        snap = config.snapshot(probe=False)
        assert snap["audio_model"] == "mlx-community/whisper-large-v3-mlx"
        assert snap["audio_language"] == "no"
        assert snap["audio_diarize"] is True
        assert snap["audio_speakers"] == 2
    finally:
        db_engine.reset()


def test_snapshot_reports_audio_settings(tmp_path, monkeypatch):
    from converter.db import engine as db_engine
    from converter import settings

    monkeypatch.setenv("VISION_LOG_DB", str(tmp_path / "config_audio2.sqlite"))
    db_engine.reset()
    try:
        settings.set_setting("audio_model", "mlx-community/whisper-large-v3-turbo")
        settings.set_setting("audio_language", "auto")
        settings.set_setting("audio_diarize", "off")
        settings.set_setting("audio_speakers", "0")
        snap = config.snapshot(probe=False)
        assert snap["audio_model"] == "mlx-community/whisper-large-v3-turbo"
        assert snap["audio_language"] == "auto"
        assert snap["audio_diarize"] is False
        assert snap["audio_speakers"] == 0
    finally:
        db_engine.reset()
