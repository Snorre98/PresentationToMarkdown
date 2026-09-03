"""Tests for the deterministic pipeline router and model downgrade (ADR-0024)."""
from __future__ import annotations

import pytest

from converter import config, structure
from converter.router import Route, structure_regime, structure_text_downgrade


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset()
    yield
    config.reset()


def _line_meta(*texts: str) -> list[dict]:
    return [{"text": t, "size": 10.0, "bold": False, "x0": 50.0} for t in texts]


def _clean_lines() -> list[str]:
    """Distinct, varied English sentences so ``text_layer_quality`` is 'usable'.

    Sliding-window lines over a large unique word pool keep the unique-word ratio
    high without repeating any sentence.
    """
    pool = (
        "quick learning players games rewards agency feedback constructive "
        "surprise uncertain intrinsic motivation curiosity challenge fantasy "
        "structure research practice theory students children adults experiments "
        "problems meaningful purpose design environment discover effort control "
        "skill improve measure results evidence careful clear direct method "
        "system process model simple complex early modern future global local "
        "central major minor active passive stable dynamic formal informal "
        "direct indirect primary secondary common special specific general unique "
    ).split()
    return [" ".join(pool[i : i + 7]) for i in range(0, len(pool) - 6, 3)]


_PROSE_LINES = _clean_lines()


def test_structure_regime_routes_garbage_to_skip():
    md = ["# Page 1", "", "a line"] * 20 + [""]
    meta = _line_meta(*(["metagoa"] * 80))
    assert structure_regime(meta, md) == "skip"


def test_structure_regime_routes_sparse_to_skip():
    md = ["# Page 1", "", "few"]
    assert structure_regime(_line_meta("a", "b"), md) == "skip"


def test_structure_regime_routes_details_to_image():
    md = ["# Page 1", "", "<details>", "<summary>Raw extracted text</summary>", "</details>", ""]
    assert structure_regime(_line_meta("garbled"), md) == "image"


def test_structure_regime_routes_clean_to_text():
    meta = [{"text": l, "size": 10.0, "bold": False, "x0": 50.0} for l in _PROSE_LINES]
    md = ["# Page 1", "", "[Page 1](assets/x/x.png)", ""] + _PROSE_LINES
    assert structure_regime(meta, md) == "text"

def test_text_downgrade_resolves_because_small_model_differ():
    # Default STRUCTURE_MODEL (writer VLM) != STRUCTURE_TEXT_MODEL (small text model).
    assert structure.STRUCTURE_TEXT_MODEL != structure.STRUCTURE_MODEL
    assert structure_text_downgrade() == Route.DOWNGRADE


def test_text_downgrade_disabled_when_models_identical(monkeypatch):
    monkeypatch.setattr(structure, "STRUCTURE_TEXT_MODEL", structure.STRUCTURE_MODEL)
    assert structure_text_downgrade() == Route.RUN


def test_text_model_selects_small_model_when_downgraded(monkeypatch):
    monkeypatch.setattr(structure, "STRUCTURE_TEXT_MODEL", "small-model")
    monkeypatch.setattr(structure, "STRUCTURE_MODEL", "vl-writer")
    base, model, _key = structure._text_model()
    assert base == structure.STRUCTURE_TEXT_BASE_URL
    assert model == "small-model"


def test_text_model_stays_on_writer_when_no_downgrade(monkeypatch):
    monkeypatch.setattr(structure, "STRUCTURE_TEXT_MODEL", structure.STRUCTURE_MODEL)
    base, model, _key = structure._text_model()
    assert model == structure.STRUCTURE_MODEL
    assert base == structure.STRUCTURE_BASE_URL


def test_text_regime_sends_text_only(monkeypatch):
    # The text regime's prompt is a single text string, never an image block, so
    # it can run on a text-only model. Capture the messages shape.
    config.set_enabled("structure", True)
    captured: dict = {}

    def fake_chat(messages, **kw):
        captured["messages"] = messages
        raise RuntimeError("probe only")

    monkeypatch.setattr("converter.structure._chat_completion", fake_chat)
    from converter.structure import PageData, structure_paper

    meta = [{"text": l, "size": 10.0, "bold": False, "x0": 50.0} for l in _PROSE_LINES]
    page = PageData(
        md_lines=["# Page 1", "", "[Page 1](assets/x/x.png)", ""] + _PROSE_LINES,
        line_meta=meta,
        pno=1,
    )
    warnings: list[str] = []
    structure_paper([page], warnings=warnings)
    content = captured["messages"][0]["content"]
    assert isinstance(content, str)
    assert "image_url" not in content
