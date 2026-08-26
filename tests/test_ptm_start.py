"""Tests for the ``scripts/ptm-start.sh`` venv wrapper.

The wrapper resolves ``.venv/bin/ptm-start`` (or ``ptm``) without the venv being
activated. These tests only exercise ``--help``, which argparse handles before
``start.main`` imports ``gui``, so no PySide6/GUI is loaded.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ptm-start.sh"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not SCRIPT.exists(),
    reason="requires the POSIX ptm-start.sh script",
)


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )


def test_default_resolves_ptm_start_help():
    run = _run("--help")
    assert run.returncode == 0, run.stderr
    assert "usage: ptm-start" in run.stdout


def test_ptm_cmd_override_resolves_headless_cli():
    run = _run("--help", env={"PTM_CMD": "ptm"})
    assert run.returncode == 0, run.stderr
    assert "usage: ptm" in run.stdout


def test_unknown_cmd_is_rejected():
    run = _run("--help", env={"PTM_CMD": "bogus"})
    assert run.returncode != 0
    assert "unknown PTM_CMD" in run.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
