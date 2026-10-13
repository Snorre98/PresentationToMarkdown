"""Tests for ``cli_common`` (the surviving AI flag -> env mapping).

The ``ptm``/``ptm-start``/``ptm-transcribe``/``ptm-ab`` entry points were
retired (ADR-0051); ``ptm-engine`` still parses these flags at launch, so the
mapping stays covered here with a throwaway parser.
"""
from __future__ import annotations

import argparse

import pytest

from cli_common import add_ai_flags, ai_env_vars, apply_ai_env


def parse(argv):
    parser = argparse.ArgumentParser(prog="ptm-engine")
    add_ai_flags(parser)
    return parser.parse_args(argv)


# --- flag -> env mapping ----------------------------------------------------


def test_no_flags_maps_to_nothing():
    assert ai_env_vars(parse([])) == {}


def test_vision_flag():
    assert ai_env_vars(parse(["--vision"])) == {"VISION_ENABLED": "1"}


def test_classify_implies_vision():
    env = ai_env_vars(parse(["--classify"]))
    assert env == {"VISION_CLASSIFY_ENABLED": "1", "VISION_ENABLED": "1"}


def test_individual_flags():
    assert ai_env_vars(parse(["--format"])) == {"FORMAT_ENABLED": "1"}
    assert ai_env_vars(parse(["--summary"])) == {"SUMMARY_ENABLED": "1"}
    assert ai_env_vars(parse(["--interpret"])) == {"INTERPRET_ENABLED": "1"}
    assert ai_env_vars(parse(["--structure"])) == {"STRUCTURE_ENABLED": "1"}


def test_structure_not_part_of_all():
    env = ai_env_vars(parse(["--all"]))
    assert "STRUCTURE_ENABLED" not in env


def test_all_enables_every_pass():
    env = ai_env_vars(parse(["--all"]))
    assert env == {
        "VISION_ENABLED": "1",
        "VISION_CLASSIFY_ENABLED": "1",
        "INTERPRET_ENABLED": "1",
        "FORMAT_ENABLED": "1",
        "SUMMARY_ENABLED": "1",
    }


def test_audio_flags_removed():
    with pytest.raises(SystemExit):
        parse(["--audio"])
    with pytest.raises(SystemExit):
        parse(["--diarize"])
    assert "audio" not in ai_env_vars(parse([]))


def test_no_gate_maps_to_off():
    assert ai_env_vars(parse(["--no-gate"])) == {"NEED_GATE": "off"}


def test_no_gate_with_format():
    env = ai_env_vars(parse(["--format", "--no-gate"]))
    assert env == {"FORMAT_ENABLED": "1", "NEED_GATE": "off"}


def test_image_links_maps_to_env():
    assert ai_env_vars(parse([])) == {}
    assert ai_env_vars(parse(["--image-links"])) == {"IMAGE_LINKS": "on"}


def test_no_compress_maps_to_env():
    assert ai_env_vars(parse([])) == {}
    assert ai_env_vars(parse(["--no-compress"])) == {"PDF_COMPRESS": "off"}


def test_overwrite_inference_maps_to_env():
    assert ai_env_vars(parse([])) == {}
    assert ai_env_vars(parse(["--overwrite-inference"])) == {
        "INFERENCE_OVERWRITE": "on"
    }


def test_env_passthrough():
    env = ai_env_vars(parse(["--vision", "--env", "VISION_MODEL=foo", "--env", "VISION_LOG_DB=/tmp/x.sqlite"]))
    assert env["VISION_ENABLED"] == "1"
    assert env["VISION_MODEL"] == "foo"
    assert env["VISION_LOG_DB"] == "/tmp/x.sqlite"


def test_env_passthrough_invalid_raises():
    with pytest.raises(SystemExit):
        ai_env_vars(parse(["--env", "nokey"]))


def test_apply_ai_env_sets_environment(monkeypatch):
    import os

    for var in ("VISION_ENABLED", "VISION_CLASSIFY_ENABLED"):
        monkeypatch.setenv(var, "")
    applied = apply_ai_env(parse(["--classify"]))

    assert os.environ["VISION_ENABLED"] == "1"
    assert os.environ["VISION_CLASSIFY_ENABLED"] == "1"
    assert applied == {"VISION_ENABLED": "1", "VISION_CLASSIFY_ENABLED": "1"}
