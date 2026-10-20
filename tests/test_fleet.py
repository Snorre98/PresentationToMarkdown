"""Tests for the flaate client's availability verbs (ADR-0063)."""
from __future__ import annotations

from converter import fleet


def test_status_all_parses_manifest_order(monkeypatch):
    monkeypatch.setattr(
        fleet,
        "_get",
        lambda path: {
            "states": [
                {"name": "text", "state": "up"},
                {"name": "whisper-large-v3", "state": "starting"},
            ]
        },
    )
    assert fleet.status_all() == {"text": "up", "whisper-large-v3": "starting"}


def test_status_all_returns_none_when_unreachable(monkeypatch):
    monkeypatch.setattr(fleet, "_get", lambda path: None)
    assert fleet.status_all() is None


def test_status_all_keeps_missing_state_unknown(monkeypatch):
    monkeypatch.setattr(fleet, "_get", lambda path: {"states": [{"name": "text"}]})
    assert fleet.status_all() == {"text": "unknown"}
