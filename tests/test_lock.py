"""Tests for the single-instance transcription lock (``lock``)."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import lock


def test_acquire_then_release():
    l = lock.acquire_transcribe_lock()
    assert l.held
    assert l.pid == os.getpid()
    l.release()

    # Released — a second acquisition from a fresh descriptor succeeds.
    l2 = lock.acquire_transcribe_lock()
    assert l2.held
    l2.release()


def test_second_fd_fails_while_held():
    l1 = lock.acquire_transcribe_lock()
    assert l1.held
    try:
        l2 = lock.acquire_transcribe_lock()
        assert not l2.held
        assert l2.pid == os.getpid()
    finally:
        l1.release()


def test_lock_file_records_holder_pid():
    l = lock.acquire_transcribe_lock()
    assert l.held
    try:
        raw = l.path.read_text(encoding="ascii").strip()
        assert raw == str(os.getpid())
    finally:
        l.release()


def test_context_manager_releases():
    with lock.acquire_transcribe_lock() as l:
        assert l.held
    assert not l.held


def test_lock_auto_releases_on_process_death(tmp_path):
    # The child acquires and exits; the parent must then acquire successfully.
    child_code = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, os.environ["REPO_ROOT"])
        import lock
        l = lock.acquire_transcribe_lock()
        assert l.held, "child failed to acquire"
        sys.exit(0)
        """
    )
    env = dict(os.environ)
    env["REPO_ROOT"] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    l = lock.acquire_transcribe_lock()
    assert l.held
    l.release()


def test_missing_state_dir_degrades_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("PTM_STATE_DIR", str(tmp_path / "state" / "nested" / "deep"))
    l = lock.acquire_transcribe_lock()
    assert l.held  # dir is created, never crashes
    l.release()


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
