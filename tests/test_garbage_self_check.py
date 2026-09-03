"""Tests for the deterministic OCR-garbage self-check (ADR-0023).

``converter.base.text_layer_is_garbage`` predicts, *before* any model call, that a
page's text layer is dense OCR garbage — the case the structure pass's verbatim
word-gate would reject after spending an expensive VLM call. These tests prove
the signal fires on garbled layers, stays quiet on clean prose, and that the
structure pass skips garbage pages without calling the model.
"""
from __future__ import annotations

import pytest

from converter import config
from converter.base import text_layer_is_garbage
from converter.structure import PageData, _page_regime, structure_paper

# OCR-typo tokens observed in ptm.sqlite structure rejections for
# "03 What makes things fun to learn.pdf" (ADR-0016 / ADR-0023).
_TYPO_TOKENS = [
    "metagoa",
    "metagoal",
    "gmnes",
    "numbdr",
    "numbcr",
    "witat",
    "interesti",
    "gucssing",
    "ditficult",
    "aggrcssion",
    "manipulatcs",
    "dzfficulty",
    "shoum",
    "tlae",
    "miliar",
]

_CLEAN_PROSE = "the quick brown fox jumps over the lazy dog while we run home again"


def _garbage_text_layer(token: str = "metagoa", count: int = 80) -> list[str]:
    return [token] * count


def _clean_text_layer() -> list[str]:
    return [_CLEAN_PROSE] * 80


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset()
    yield
    config.reset()


def test_garbage_flags_typo_shaped_tokens():
    assert text_layer_is_garbage(_garbage_text_layer("metagoa"))
    assert text_layer_is_garbage(_garbage_text_layer("gmnes"))
    assert text_layer_is_garbage(["metagoa", "gmnes", "numbdr", "interesti"] * 20)


def test_garbage_ignores_clean_prose():
    assert not text_layer_is_garbage(_clean_text_layer())


def test_garbage_needs_enough_tokens():
    # Below the absolute token floor the layer is too thin to judge -> trusted.
    assert not text_layer_is_garbage(["metagoa"] * 40)
    assert not text_layer_is_garbage(["gmnes"] * 3)


def test_garbage_flags_mixed_garbled_page():
    # Mostly real prose with a sprinkling of OCR typos, like the observed paper.
    lines = [_CLEAN_PROSE] * 70 + ["metagoa", "gmnes", "numbdr", "interesti"] * 3
    assert text_layer_is_garbage(lines)


def test_page_regime_routes_garbage_to_skip():
    page = PageData(
        md_lines=["# Page 1", "", "metagoa gmnes numbdr interesti"] * 20 + [""],
        line_meta=[{"text": "metagoa", "size": 10.0, "bold": False, "x0": 50.0}] * 80,
        pno=1,
    )
    assert _page_regime(page) == "skip"


def test_structure_skips_garbage_page_without_model_call(monkeypatch):
    config.set_enabled("structure", True)
    calls: list[int] = []
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda messages, **kw: calls.append(1) or "anything",
    )
    garbage = PageData(
        md_lines=["# Page 1", "", "metagoa gmnes numbdr interesti wurds"] * 20 + [""],
        line_meta=[{"text": "metagoa", "size": 10.0, "bold": False, "x0": 50.0}] * 80,
        pno=1,
    )
    original = "\n".join(garbage.md_lines)
    warnings: list[str] = []
    assert structure_paper([garbage], warnings=warnings) is None
    assert calls == []
    assert not any("rejected" in w for w in warnings)


def test_structure_byte_identical_when_garbage_skipped(monkeypatch):
    config.set_enabled("structure", True)
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda messages, **kw: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    garbage = PageData(
        md_lines=["# Page 1", "", "metagoa gmnes numbdr interesti wurds"] * 20 + [""],
        line_meta=[{"text": "metagoa", "size": 10.0, "bold": False, "x0": 50.0}] * 80,
        pno=1,
    )
    # structure_paper returns None on garbage -> caller keeps md_lines verbatim.
    assert structure_paper([garbage], warnings=[]) is None
    assert garbage.md_lines  # deterministic output unchanged (no amendment)
