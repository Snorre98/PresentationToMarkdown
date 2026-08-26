"""Shared test fixtures.

Isolates the audio state dir (``PTM_STATE_DIR``) to a per-test temp directory so
the ``ptm-transcribe`` single-instance lock never touches the user's real
``~/.local/state/ptm`` during a test run.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PTM_STATE_DIR", str(tmp_path / "state"))
