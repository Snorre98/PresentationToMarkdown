"""Tests for the batched pre-flight need-check classifier (ADR-0039)."""
from __future__ import annotations

import pytest

from converter.need import (
    _batches,
    _parse_indices,
    need_gate,
    needs_reformat,
    needs_restructure,
)


def test_parse_indices_numbers():
    assert _parse_indices("2, 5, 9", 10) == {1, 4, 8}


def test_parse_indices_none_answer():
    assert _parse_indices("none", 5) == set()


def test_parse_indices_garbage_is_unparseable():
    assert _parse_indices("just some prose here", 5) is None


def test_parse_indices_empty_is_unparseable():
    assert _parse_indices("", 5) is None


def test_parse_indices_ignores_out_of_range():
    assert _parse_indices("3, 99", 5) == {2}


def test_batches_splits_large_items():
    big = ["x" * 12000, "y"]
    batches = _batches(big)
    assert len(batches) >= 2
    assert batches[0][0] == 0
    assert [i for _, chunk in batches for i in chunk] == big


def test_need_gate_defaults_on(monkeypatch):
    monkeypatch.delenv("NEED_GATE", raising=False)
    assert need_gate() == "on"


def test_need_gate_reads_env(monkeypatch):
    monkeypatch.setenv("NEED_GATE", "shadow")
    assert need_gate() == "shadow"


def test_needs_reformat_batches_and_maps(monkeypatch):
    calls: list[list] = []

    def fake_chat(messages, **kw):
        calls.append(messages)
        return "2", {"prompt_tokens": 10, "completion_tokens": 1}

    monkeypatch.setattr("converter.need._chat_completion", fake_chat)
    assert needs_reformat(["- a", "- b", "- c"], source="x") == {1}
    assert len(calls) == 1
    prompt = calls[0][0]["content"]
    assert "1. - a" in prompt and "2. - b" in prompt and "3. - c" in prompt


def test_needs_restructure_maps_indices(monkeypatch):
    monkeypatch.setattr(
        "converter.need._chat_completion", lambda m, **kw: ("1, 3", {})
    )
    pages = ["# Page 1\n...", "# Page 2\n...", "# Page 3\n..."]
    assert needs_restructure(pages, source="x") == {0, 2}


def test_needs_reformat_returns_none_on_error(monkeypatch):
    def boom(messages, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr("converter.need._chat_completion", boom)
    assert needs_reformat(["- a"], source="x") is None


def test_needs_reformat_returns_none_on_unparseable(monkeypatch):
    monkeypatch.setattr("converter.need._chat_completion", lambda m, **kw: ("blah", {}))
    assert needs_reformat(["- a"], source="x") is None


def test_needs_reformat_empty_is_clean():
    assert needs_reformat([], source="x") == set()
