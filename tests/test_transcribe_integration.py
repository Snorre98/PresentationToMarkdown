"""Integration tests for the audio pass.

- ``test_diarize_client_against_stub`` runs the stub diarization server and the
  real ``converter.audio.diarize`` client against it — no model, no binaries.
- ``test_transcribe_audio_real`` exercises the real ``ffmpeg`` + ``mlx_whisper``
  pipeline. It is skipped unless both binaries are on PATH *and*
  ``PTM_RUN_AUDIO_INTEGRATION=1`` is set (first run downloads a ~1.6 GB model).
"""
from __future__ import annotations

import math
import os
import shutil
import threading
import wave
from pathlib import Path

import pytest

from converter import transcribe as t
from converter.audio import diarize


def _make_tone_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> None:
    """Write a short mono 16 kHz sine tone as a real WAV (ffprobe can read it)."""
    frames = bytearray()
    for i in range(int(seconds * rate)):
        sample = int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / rate))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def test_diarize_client_against_stub(tmp_path):
    from scripts.stub_diarize_server import make_server

    audio = tmp_path / "talk.wav"
    _make_tone_wav(audio, seconds=1.0)

    httpd = make_server(0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        turns = diarize(str(audio), base_url=f"http://127.0.0.1:{port}/v1")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert isinstance(turns, list) and turns
    for turn in turns:
        assert set(turn) == {"start", "end", "speaker"}
        assert turn["end"] > turn["start"]


_requires_audio = pytest.mark.skipif(
    not (shutil.which("mlx_whisper") and shutil.which("ffmpeg"))
    or not os.environ.get("PTM_RUN_AUDIO_INTEGRATION"),
    reason="requires mlx_whisper + ffmpeg and PTM_RUN_AUDIO_INTEGRATION=1",
)


@_requires_audio
def test_transcribe_audio_real(tmp_path):
    audio = tmp_path / "tone.wav"
    _make_tone_wav(audio, seconds=2.0)
    segments = t.transcribe_audio(audio, language="en")
    assert isinstance(segments, list)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
