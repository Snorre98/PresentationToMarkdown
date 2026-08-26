"""Tests for the streaming/atomic parts of the transcription pipeline."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from converter import transcribe as t


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_run_capture_regression():
    out = t._run(_py("print('hello')"))
    assert out == "hello\n"


def test_run_capture_file_not_found():
    with pytest.raises(RuntimeError, match="not found"):
        t._run(["/definitely/not/a/real/binary"])


def test_run_stream_forwards_each_line():
    seen: list[str] = []
    out = t._run(_py("print('a'); print('b')"), on_line=seen.append)
    assert out == "a\nb\n"
    assert seen == ["a\n", "b\n"]


def test_run_stream_forwards_stderr():
    seen: list[str] = []
    t._run(_py("import sys; sys.stderr.write('oops\\n')"), on_line=seen.append)
    assert "oops\n" in seen


def test_run_stream_failure_captures_tail():
    seen: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        t._run(
            _py("import sys; print('boom', file=sys.stderr); sys.exit(3)"),
            on_line=seen.append,
        )
    assert any("boom" in line for line in seen)


def test_run_stream_heartbeat_fires_when_quiet():
    seen: list[str] = []
    t._run(_py("import time; time.sleep(0.4)"), on_line=seen.append, heartbeat=0.05)
    assert any(line.startswith("still working …") for line in seen)


def test_run_stream_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        t._run(_py("import time; time.sleep(10)"), timeout=0.2, on_line=lambda _: None)


def test_atomic_write_text(tmp_path):
    target = tmp_path / "out.md"
    t._atomic_write_text(target, "# hello\n")
    assert target.read_text(encoding="utf-8") == "# hello\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "out.md"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(t.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        t._atomic_write_text(target, "# hello\n")
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_transcribe_audio_streams_phases(monkeypatch, tmp_path):
    seen: list[str] = []

    def fake_run(cmd, timeout=3600.0, **kw):
        if cmd[0] == t.AUDIO_FFMPEG_BIN:
            return ""
        import json

        idx = cmd.index("--output-dir")
        outdir = Path(cmd[idx + 1])
        (outdir / (Path(cmd[1]).stem + ".json")).write_text(
            json.dumps({"segments": []}), encoding="utf-8"
        )
        return ""

    monkeypatch.setattr(t, "_run", fake_run)
    monkeypatch.setattr(t, "AUDIO_ENHANCE_ENABLED", False)
    t.transcribe_audio(tmp_path / "x.mp3", tmp_path / "x.clean.flac", on_line=seen.append)

    assert any("ffmpeg" in line for line in seen)
    assert any("transcribing" in line for line in seen)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
