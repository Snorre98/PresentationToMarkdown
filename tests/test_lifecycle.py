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


def release_calls(calls: list[dict]) -> list[dict]:
    """Only the release-path calls — ADR-0029's fleet ``/list`` lookup (against
    the control daemon on :9300) is incidental to these tests."""
    return [c for c in calls if ":9300" not in c["url"]]


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
    # ADR-0029: the catalog mirrors the fleet manifest — embeddings moved from
    # ollama :11434 to the llama.cpp nomic-embed daemon :8090, so :11434 no
    # longer resolves to a catalog runner (the caller tries both unloads).
    assert resolve_runner("http://localhost:11434/v1") is None
    assert resolve_runner("http://127.0.0.1:11434/v1") is None
    assert resolve_runner("http://127.0.0.1:8081/v1") == "mlx-vlm"
    assert resolve_runner("http://127.0.0.1:8082/v1") == "mlx-vlm"
    assert resolve_runner("http://127.0.0.1:8090/v1") == "llama.cpp"
    assert resolve_runner("http://127.0.0.1:8083/v1") == "mlx-lm"


def test_resolve_runner_unresolved():
    assert resolve_runner("http://example.com:9999/v1") is None
    assert resolve_runner("not a url") is None


def test_release_resolved_hits_single_runner(recorder):
    release("http://127.0.0.1:8081/v1")
    calls = release_calls(recorder)
    assert len(calls) == 1
    assert calls[0]["url"] == "http://127.0.0.1:8081/unload"


def test_release_unresolved_tries_both_unloads(recorder):
    release("http://example.com:9999/v1", "glm-ocr")
    calls = release_calls(recorder)
    assert len(calls) == 2
    assert calls[0]["url"] == "http://example.com:9999/api/generate"
    assert calls[1]["url"] == "http://example.com:9999/unload"
