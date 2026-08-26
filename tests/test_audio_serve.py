"""Tests for the audio-server lifecycle script (``scripts/audio_serve.sh``).

Model-free: these drive the **stub** server (no PyTorch, no Hugging Face) through
the management script's ``stub-start``/``stub-status``/``stub-stop`` subcommands,
with a throwaway state dir and an ephemeral port, so nothing here touches the
real venv, the HF token, or launchd.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audio_serve.sh"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not SCRIPT.exists(),
    reason="requires the POSIX audio_serve.sh script",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(tmp_path: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {
        "PTM_STATE_DIR": str(tmp_path / "state"),
        "AUDIO_STUB_PYTHON": sys.executable,
    }
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )


def test_stub_lifecycle(tmp_path):
    port = _free_port()

    start = _run(tmp_path, "stub-start", "--port", str(port))
    assert start.returncode == 0, start.stderr
    assert f"{port}" in start.stdout

    up = _run(tmp_path, "stub-status")
    assert up.returncode == 0, up.stderr
    assert "running" in up.stdout
    assert str(port) in up.stdout

    stop = _run(tmp_path, "stub-stop")
    assert stop.returncode == 0, stop.stderr

    down = _run(tmp_path, "stub-status")
    assert down.returncode != 0
    assert "not running" in down.stderr


def test_stub_start_is_idempotent(tmp_path):
    port = _free_port()
    first = _run(tmp_path, "stub-start", "--port", str(port))
    assert first.returncode == 0
    second = _run(tmp_path, "stub-start", "--port", str(port))
    assert second.returncode == 0
    assert "already running" in second.stderr

    _run(tmp_path, "stub-stop")


def test_stop_without_start_is_harmless(tmp_path):
    stop = _run(tmp_path, "stub-stop")
    assert stop.returncode == 0
    assert "nothing running" in stop.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
