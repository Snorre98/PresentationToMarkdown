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


def test_feature_endpoints_format_falls_back_to_vision():
    vision = config.feature_endpoints("vision")
    assert vision == [("transcriber", "http://127.0.0.1:8081/v1")]
    format_ = config.feature_endpoints("format")
    assert format_[0][0] == "transcriber"
    assert format_[0][1] == vision[0][1]


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
