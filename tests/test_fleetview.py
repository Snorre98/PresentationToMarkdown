"""Tests for the TUI's fleet availability projection (``converter.fleetview``, ADR-0063)."""
from __future__ import annotations

import pytest

from converter import config, fleet, fleetview


@pytest.fixture(autouse=True)
def _clean_features():
    config.reset()
    yield
    config.reset()


def _model(name: str, daemon: str, port: int, runner: str = "mlx-lm") -> dict:
    return {
        "name": name,
        "daemon": daemon,
        "host": "127.0.0.1",
        "port": port,
        "runner": runner,
        "modelId": f"repo/{name}",
    }


def test_manifest_projection_roles_features_and_selection(monkeypatch):
    models = {
        "text": _model("text", "text", 8083),
        "transcriber": _model("transcriber", "transcriber", 8081, "mlx-vlm"),
        "whisper-large-v3": _model("whisper-large-v3", "whisper", 8099, "delegate"),
        "mistral-24b": _model("mistral-24b", "mistral-24b", 8085),
    }
    monkeypatch.setattr(fleet, "list_models", lambda force=False: models)
    monkeypatch.setattr(
        fleet,
        "status_all",
        lambda: {"text": "up", "transcriber": "down", "whisper-large-v3": "up"},
    )
    monkeypatch.setattr(fleetview, "_select", lambda cfg: ("whisper-large-v3", False))
    config.set_enabled("summary", True)

    snap = fleetview.snapshot(config)
    rows = {row["name"]: row for row in snap["services"]}

    assert snap["source"] == "fleet"
    assert snap["daemon"]["reachable"] is True
    assert rows["text"]["roles"] == ["summary", "structure-text"]
    assert rows["text"]["features"] == ["summary", "format", "structure"]
    assert rows["text"]["needed"] is True
    assert rows["text"]["state"] == "up"
    assert rows["transcriber"]["state"] == "down"
    assert rows["transcriber"]["features"] == [
        "vision",
        "classify",
        "interpret",
        "format",
        "structure",
    ]
    assert rows["whisper-large-v3"]["selected"] is True
    assert rows["whisper-large-v3"]["needed"] is True
    assert rows["mistral-24b"]["roles"] == []
    assert snap["other_models"] == 1

    # A catalogue role absent from the manifest is visible, not hidden.
    assert rows["classifier"]["state"] == "unknown"
    assert rows["classifier"]["in_manifest"] is False
    assert rows["audio"]["in_manifest"] is False
    assert "nb-whisper-large" in rows["audio"]["roles"]


def test_static_fallback_probes_and_labels_itself(monkeypatch):
    monkeypatch.setattr(fleet, "status_all", lambda: None)
    monkeypatch.setattr(
        config, "probe", lambda url, timeout=1.5: "127.0.0.1:8081" in url
    )
    monkeypatch.setattr(fleetview, "_select", lambda cfg: ("nb-whisper-large", False))

    snap = fleetview.snapshot(config)
    rows = {row["name"]: row for row in snap["services"]}

    assert snap["source"] == "static"
    assert snap["daemon"]["reachable"] is False
    assert rows["transcriber"]["state"] == "up"
    assert rows["classifier"]["state"] == "down"
    assert rows["whisper-large-v3"]["state"] == "down"
    assert rows["audio"]["state"] == "down"
    assert rows["audio"]["selected"] is True
    assert rows["audio"]["needed"] is True
    assert all(row["in_manifest"] is False for row in snap["services"])
    assert snap["other_models"] == 0


def test_needed_tracks_enabled_features(monkeypatch):
    models = {"text": _model("text", "text", 8083)}
    monkeypatch.setattr(fleet, "list_models", lambda force=False: models)
    monkeypatch.setattr(fleet, "status_all", lambda: {"text": "up"})
    monkeypatch.setattr(fleetview, "_select", lambda cfg: ("whisper-large-v3", False))

    snap = fleetview.snapshot(config)
    text = next(row for row in snap["services"] if row["name"] == "text")
    assert text["needed"] is False

    config.set_enabled("structure", True)
    snap = fleetview.snapshot(config)
    text = next(row for row in snap["services"] if row["name"] == "text")
    assert text["needed"] is True
