"""Tests for best-effort model-residency orchestration (converter.lifecycle)."""
from __future__ import annotations

import json

import pytest

from converter.lifecycle import (
    release,
    release_model,
    resolve_runner,
)


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def recorder(monkeypatch):
    calls: list[dict] = []

    def _open(req, timeout=None):
        calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "data": req.data,
            }
        )
        return _Resp()

    monkeypatch.setattr("converter.lifecycle.urllib.request.urlopen", _open)
    return calls


def test_release_model_ollama(recorder):
    release_model("ollama", "http://localhost:11434/v1", "glm-ocr")
    assert len(recorder) == 1
    assert recorder[0]["url"] == "http://localhost:11434/api/generate"
    assert json.loads(recorder[0]["data"]) == {"model": "glm-ocr", "keep_alive": 0}


def test_release_model_ollama_without_model_is_noop(recorder):
    release_model("ollama", "http://localhost:11434/v1", None)
    assert recorder == []


def test_release_model_mlx_vlm(recorder):
    release_model("mlx-vlm", "http://127.0.0.1:8081/v1")
    assert len(recorder) == 1
    assert recorder[0]["url"] == "http://127.0.0.1:8081/unload"
    assert recorder[0]["data"] is None


def test_release_model_unknown_runner_is_noop(recorder):
    release_model("pytorch", "http://127.0.0.1:9999/v1", "whatever")
    assert recorder == []


def test_release_model_swallows_errors(monkeypatch):
    def _boom(req, timeout=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("converter.lifecycle.urllib.request.urlopen", _boom)
    release_model("ollama", "http://localhost:11434/v1", "glm-ocr")  # must not raise
    release_model("mlx-vlm", "http://127.0.0.1:8081/v1")  # must not raise


def test_resolve_runner_normalizes_localhost():
    assert resolve_runner("http://localhost:11434/v1") == "ollama"
    assert resolve_runner("http://127.0.0.1:11434/v1") == "ollama"
    assert resolve_runner("http://127.0.0.1:8081/v1") == "mlx-vlm"
    assert resolve_runner("http://127.0.0.1:8082/v1") == "mlx-vlm"


def test_resolve_runner_unresolved():
    assert resolve_runner("http://example.com:9999/v1") is None
    assert resolve_runner("not a url") is None


def test_release_resolved_hits_single_runner(recorder):
    release("http://localhost:11434/v1", "glm-ocr")
    assert len(recorder) == 1
    assert recorder[0]["url"] == "http://localhost:11434/api/generate"


def test_release_unresolved_tries_both_unloads(recorder):
    release("http://example.com:9999/v1", "glm-ocr")
    assert len(recorder) == 2
    assert recorder[0]["url"] == "http://example.com:9999/api/generate"
    assert recorder[1]["url"] == "http://example.com:9999/unload"
