"""Tests for the TUI's fleet availability projection (``converter.fleetview``, ADR-0063)."""
from __future__ import annotations

import pytest

from converter import config, fleet, fleetview


@pytest.fixture(autouse=True)
def _clean_features():
    config.reset()
    yield
    config.reset()


def _model(key: str, name: str, daemon: str, port: int, tags: list[str], runner: str = "mlx-lm") -> dict:
    return {
        "key": key,
        "name": name,
        "tags": tags,
        "daemon": daemon,
        "host": "127.0.0.1",
        "port": port,
        "runner": runner,
        "modelId": f"repo/{key}",
    }


def test_manifest_projection_roles_features_and_selection(monkeypatch):
    text_key = "llama-3.2-3b-instruct-4bit"
    vision_key = "qwen2.5-vl-7b-instruct-4bit"
    whisper_key = "whisper-large-v3-asr-fp16"
    mistral_key = "mistral-small-3.1-text-24b-instruct-2503-4bit"
    models = {
        text_key: _model(text_key, "Llama 3.2 3B Instruct (4-bit)", "llama-3.2-3b-instruct-4bit", 8083, ["text", "summary", "structure"]),
        vision_key: _model(vision_key, "Qwen2.5-VL 7B Instruct (4-bit)", "qwen2.5-vl-7b-instruct-4bit", 8081, ["vision", "ocr", "transcriber"], "mlx-vlm"),
        whisper_key: _model(whisper_key, "Whisper Large v3 (ASR fp16)", "whisper", 8099, ["asr", "audio", "stt"], "delegate"),
        mistral_key: _model(mistral_key, "Mistral Small 3.1 Text 24B Instruct", "mistral-small-3.1-text-24b-instruct-2503-4bit", 8085, ["text", "drafter"]),
    }
    monkeypatch.setattr(fleet, "list_models", lambda force=False: models)
    monkeypatch.setattr(
        fleet,
        "status_all",
        lambda: {text_key: "up", vision_key: "down", whisper_key: "up"},
    )
    monkeypatch.setattr(fleetview, "_select", lambda cfg: (whisper_key, False))
    config.set_enabled("summary", True)

    snap = fleetview.snapshot(config)
    rows = {row["key"]: row for row in snap["services"]}

    assert snap["source"] == "fleet"
    assert snap["daemon"]["reachable"] is True
    assert rows[text_key]["name"] == "Llama 3.2 3B Instruct (4-bit)"
    assert rows[text_key]["roles"] == ["summary", "structure-text"]
    assert rows[text_key]["features"] == ["summary", "format", "structure"]
    assert rows[text_key]["needed"] is True
    assert rows[text_key]["state"] == "up"
    assert rows[vision_key]["state"] == "down"
    assert rows[vision_key]["features"] == [
        "vision",
        "classify",
        "interpret",
        "format",
        "structure",
    ]
    assert rows[whisper_key]["selected"] is True
    assert rows[whisper_key]["needed"] is True
    assert rows[mistral_key]["roles"] == []
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
    rows = {row["key"]: row for row in snap["services"]}

    assert snap["source"] == "static"
    assert snap["daemon"]["reachable"] is False
    assert rows["transcriber"]["state"] == "up"
    assert rows["classifier"]["state"] == "down"
    assert rows["whisper-large-v3-asr-fp16"]["state"] == "down"
    assert rows["audio"]["state"] == "down"
    assert rows["audio"]["selected"] is True
    assert rows["audio"]["needed"] is True
    assert all(row["in_manifest"] is False for row in snap["services"])
    assert snap["other_models"] == 0


def test_needed_tracks_enabled_features(monkeypatch):
    text_key = "llama-3.2-3b-instruct-4bit"
    models = {text_key: _model(text_key, "Llama 3.2 3B Instruct (4-bit)", text_key, 8083, ["text", "summary", "structure"])}
    monkeypatch.setattr(fleet, "list_models", lambda force=False: models)
    monkeypatch.setattr(fleet, "status_all", lambda: {text_key: "up"})
    monkeypatch.setattr(fleetview, "_select", lambda cfg: ("whisper-large-v3-asr-fp16", False))

    snap = fleetview.snapshot(config)
    text = next(row for row in snap["services"] if row["key"] == text_key)
    assert text["needed"] is False

    config.set_enabled("structure", True)
    snap = fleetview.snapshot(config)
    text = next(row for row in snap["services"] if row["key"] == text_key)
    assert text["needed"] is True
