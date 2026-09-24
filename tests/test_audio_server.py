"""Unit tests for the audio server's NB-Whisper ASR stage (``scripts/audio_server.py``).

Model-free: ``_run_asr`` is exercised with a fake pipeline, so nothing here
imports PyTorch or Hugging Face. The two knobs this pins down are the decoding
settings (``condition_on_previous_text=False``, configurable ``num_beams`` and
``chunk_length_s`` — ADR-0044/0008) and the ``return_words`` branch that yields
per-word timestamps for word-level re-segmentation (ADR-0045).
"""
from __future__ import annotations

import pytest

import scripts.audio_server as a


class _FakePipe:
    def __init__(self, chunks):
        self.chunks = chunks
        self.captured: dict = {}

    def __call__(self, path, **kwargs):
        self.captured = {
            "path": path,
            "generate_kwargs": kwargs.get("generate_kwargs"),
            "return_timestamps": kwargs.get("return_timestamps"),
            "chunk_length_s": kwargs.get("chunk_length_s"),
            "stride_length_s": kwargs.get("stride_length_s"),
        }
        return {"chunks": self.chunks}


_WORDS = [
    {"timestamp": [0.0, 1.0], "text": "hei"},
    {"timestamp": [1.0, 2.0], "text": "på"},
    {"timestamp": [2.0, 3.0], "text": "deg."},
]


@pytest.fixture
def fake_pipe(monkeypatch):
    pipe = _FakePipe(_WORDS)
    monkeypatch.setattr(a, "_get_asr_pipe", lambda: pipe)
    return pipe


def test_run_asr_generate_kwargs(monkeypatch, fake_pipe):
    monkeypatch.setattr(a, "AUDIO_ASR_BEAMS", 5)
    monkeypatch.setattr(a, "NB_WHISPER_CHUNK_SEC", 30)
    segs = a._run_asr("/tmp/x.flac", "no")

    gen = fake_pipe.captured["generate_kwargs"]
    assert gen["task"] == "transcribe"
    assert gen["num_beams"] == 5
    assert gen["condition_on_previous_text"] is False
    assert gen["language"] == "no"
    assert fake_pipe.captured["return_timestamps"] is True
    assert fake_pipe.captured["chunk_length_s"] == 30
    assert fake_pipe.captured["stride_length_s"] == 5
    # default: words grouped into utterances at the sentence boundary.
    assert segs == [{"start": 0.0, "end": 3.0, "text": "hei på deg."}]


def test_run_asr_defaults_beams_one_and_chunk_28(monkeypatch, fake_pipe):
    monkeypatch.setattr(a, "AUDIO_ASR_BEAMS", 1)
    monkeypatch.setattr(a, "NB_WHISPER_CHUNK_SEC", 28)
    a._run_asr("/tmp/x.flac", None)
    assert fake_pipe.captured["generate_kwargs"]["num_beams"] == 1
    assert "language" not in fake_pipe.captured["generate_kwargs"]
    assert fake_pipe.captured["chunk_length_s"] == 28


def test_run_asr_return_words(monkeypatch, fake_pipe):
    monkeypatch.setattr(a, "AUDIO_ASR_BEAMS", 1)
    words = a._run_asr("/tmp/x.flac", "no", return_words=True)
    assert words == [
        {"start": 0.0, "end": 1.0, "text": "hei"},
        {"start": 1.0, "end": 2.0, "text": "på"},
        {"start": 2.0, "end": 3.0, "text": "deg."},
    ]


def test_run_asr_skips_empty_words(fake_pipe):
    fake_pipe.chunks = [
        {"timestamp": [0.0, 1.0], "text": "hei"},
        {"timestamp": [1.0, 2.0], "text": "   "},
        {"timestamp": None, "text": "none"},
    ]
    segs = a._run_asr("/tmp/x.flac", "no")
    assert segs == [{"start": 0.0, "end": 1.0, "text": "hei"}]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
