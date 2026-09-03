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


def test_parse_servers_conf():
    text = (
        "# comment\n"
        "transcriber | mlx-vlm | mlx-community/Qwen2.5-VL-7B-Instruct-4bit | 8081 | 127.0.0.1 | | Vision transcriber\n"
        "classifier  | mlx-vlm | mlx-community/Qwen2.5-VL-3B-Instruct-4bit | 8082 | 127.0.0.1 | | Classifier gate\n"
    )
    servers = config.parse_servers_conf(text)
    assert set(servers) == {"transcriber", "classifier"}
    assert servers["transcriber"].port == 8081
    assert servers["transcriber"].base_url == "http://127.0.0.1:8081/v1"
    assert servers["transcriber"].serve_command == "tools/serve.sh start transcriber"
    assert servers["classifier"].description == "Classifier gate"


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
    # Summary uses a dedicated small text model (ADR-0021), not the writer.
    assert config.feature_endpoints("summary")[0][1] == "http://127.0.0.1:8084/v1"


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
    assert ("transcriber", "http://127.0.0.1:8081/v1", "tools/serve.sh start transcriber") in missing


def test_missing_servers_empty_when_nothing_enabled(monkeypatch):
    monkeypatch.setattr(config, "probe", lambda url, timeout=1.5: True)
    assert config.missing_servers() == []
