"""Integration tests for the audio pass.

- ``test_diarize_client_against_stub`` / ``test_enhance_client_against_stub`` run
  the stub audio server and the real ``converter.audio`` clients against it — no
  model, no binaries.
- ``test_transcribe_audio_writes_clean_flac`` runs the *real* ffmpeg to produce
  the persisted cleaned FLAC (mlx-whisper is faked), so it needs only ``ffmpeg``.
- ``test_transcribe_audio_real`` exercises the full real ``ffmpeg`` +
  ``mlx_whisper`` pipeline. It is skipped unless both binaries are on PATH *and*
  ``PTM_RUN_AUDIO_INTEGRATION=1`` is set (first run downloads a ~1.6 GB model).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import wave
from pathlib import Path

import pytest

from converter import transcribe as t
from converter.audio import diarize, enhance


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


def _start_stub():
    from scripts.stub_audio_server import make_server

    httpd = make_server(0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port, thread


def test_diarize_client_against_stub(tmp_path):
    httpd, port, thread = _start_stub()
    audio = tmp_path / "talk.wav"
    _make_tone_wav(audio, seconds=1.0)
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


def test_enhance_client_against_stub(tmp_path):
    httpd, port, thread = _start_stub()
    src = tmp_path / "talk.wav"
    _make_tone_wav(src, seconds=1.0)
    dst = tmp_path / "talk.clean.flac"
    try:
        enhance(str(src), str(dst), base_url=f"http://127.0.0.1:{port}/v1")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert dst.exists()
    assert dst.read_bytes() == src.read_bytes()


def test_enhance_client_surfaces_server_error(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _ErrorHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.dumps({"error": "DeepFilterNet exploded"}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: N802
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ErrorHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(RuntimeError, match="DeepFilterNet exploded"):
            enhance(
                str(tmp_path / "x.flac"),
                str(tmp_path / "y.flac"),
                base_url=f"http://127.0.0.1:{port}/v1",
            )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="requires ffmpeg on PATH"
)
def test_transcribe_audio_writes_clean_flac(tmp_path, monkeypatch):
    audio = tmp_path / "tone.wav"
    _make_tone_wav(audio, seconds=1.0)
    clean = tmp_path / "tone.clean.flac"

    def fake_run(cmd, timeout=3600.0):
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            subprocess.run(cmd, check=True, capture_output=True)
            return ""
        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        (outdir / (Path(cmd[1]).stem + ".json")).write_text(
            json.dumps({"segments": []}), encoding="utf-8"
        )
        return ""

    monkeypatch.setattr(t, "_run", fake_run)
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", False)
    t.transcribe_audio(audio, clean)

    assert clean.exists()
    assert clean.read_bytes()[:4] == b"fLaC"


_requires_audio = pytest.mark.skipif(
    not (shutil.which("mlx_whisper") and shutil.which("ffmpeg"))
    or not os.environ.get("PTM_RUN_AUDIO_INTEGRATION"),
    reason="requires mlx_whisper + ffmpeg and PTM_RUN_AUDIO_INTEGRATION=1",
)


@_requires_audio
def test_transcribe_audio_real(tmp_path):
    audio = tmp_path / "tone.wav"
    _make_tone_wav(audio, seconds=2.0)
    segments = t.transcribe_audio(audio, tmp_path / "tone.clean.flac", language="en")
    assert isinstance(segments, list)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
