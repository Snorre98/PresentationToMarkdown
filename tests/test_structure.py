"""Tests for the paper-mode document-structure LLM pass (converter.structure)."""
from pathlib import Path

import pytest

from converter import convert_file
from converter.structure import (
    STRUCTURE_ENABLED,
    PageData,
    _match_meta,
    _page_regime,
    _text_coverage,
    structure_paper,
)

TESTS_DIR = Path(__file__).parent
PAPER = TESTS_DIR / "test_paper.pdf"

PAGE1_META = [
    {"text": "What Makes Things Fun to Learn?", "size": 16.0, "bold": False, "x0": 200.0},
    {"text": "Heuristics for Design", "size": 13.0, "bold": False, "x0": 220.0},
    {"text": "T. Malone", "size": 11.0, "bold": False, "x0": 260.0},
    {"text": "Xerox PARC", "size": 9.0, "bold": False, "x0": 255.0},
    {"text": "Challenge", "size": 12.0, "bold": True, "x0": 50.0},
    {"text": "The first challenge is keeping players", "size": 10.0, "bold": False, "x0": 50.0},
    {"text": "engaged through uncertain goals.", "size": 10.0, "bold": False, "x0": 50.0},
    {"text": "Fantasy", "size": 12.0, "bold": True, "x0": 330.0},
    {"text": "Fantasy can make a game more", "size": 10.0, "bold": False, "x0": 330.0},
    {"text": "compelling when it is intrinsic.", "size": 10.0, "bold": False, "x0": 330.0},
]

PAGE1_MD = [
    "# What Makes Things Fun to Learn? Heuristics for Design",
    "",
    "*T. Malone · Xerox PARC*",
    "",
    "[Page 1](assets/test_paper/test_paper_page_01.png)",
    "",
    "## Challenge",
    "",
    "The first challenge is keeping players",
    "engaged through uncertain goals.",
    "",
    "## Fantasy",
    "",
    "Fantasy can make a game more",
    "compelling when it is intrinsic.",
    "",
]

PAGE2_META = [
    {"text": "Curiosity", "size": 12.0, "bold": True, "x0": 50.0},
    {"text": "Curiosity is sparked by surprise and by constructive feedback.", "size": 10.0, "bold": False, "x0": 50.0},
    {"text": "Rewards can even backfire when they undermine intrinsic motivation.", "size": 10.0, "bold": False, "x0": 50.0},
    {"text": "A well-designed game provides both challenge and fantasy together.", "size": 10.0, "bold": False, "x0": 330.0},
    {"text": "Players stay engaged longer when they feel a sense of agency.", "size": 10.0, "bold": False, "x0": 330.0},
]

PAGE2_MD = [
    "# Page 2",
    "",
    "[Page 2](assets/test_paper/test_paper_page_02.png)",
    "",
    "## Curiosity",
    "",
    "Curiosity is sparked by surprise and by constructive feedback.",
    "Rewards can even backfire when they undermine intrinsic motivation.",
    "A well-designed game provides both challenge and fantasy together.",
    "Players stay engaged longer when they feel a sense of agency.",
    "",
]


def _page1() -> PageData:
    return PageData(md_lines=list(PAGE1_MD), line_meta=[dict(m) for m in PAGE1_META], pno=1)


def _page2() -> PageData:
    return PageData(md_lines=list(PAGE2_MD), line_meta=[dict(m) for m in PAGE2_META], png=b"x", pno=2)


def _image_page() -> PageData:
    return PageData(
        md_lines=[
            "# Page 3",
            "",
            "[Page 3](assets/test_paper/test_paper_page_03.png)",
            "",
            "<details>",
            "<summary>Raw extracted text</summary>",
            "",
            "garbled text that means nothing at all to anyone",
            "",
            "</details>",
            "",
        ],
        line_meta=[
            {"text": "garbled text that means nothing at all to anyone", "size": 10.0, "bold": False, "x0": 50.0}
        ],
        png=b"fake-png-bytes",
        pno=3,
    )


def _enable(monkeypatch):
    monkeypatch.setattr("converter.structure.STRUCTURE_ENABLED", True)


def _mock(monkeypatch, reply):
    monkeypatch.setattr("converter.structure._chat_completion", lambda messages, **kw: reply)


# --- confidence gate and routing --------------------------------------------


def test_text_coverage_levels():
    assert _text_coverage(PAGE1_META) == "usable"
    assert _text_coverage([]) == "empty"
    assert _text_coverage([{"text": "garbled nonsense", "size": 10.0, "bold": False, "x0": 50.0}]) == "sparse"


def test_page_regime_routing():
    assert _page_regime(_page1()) == "text"
    assert _page_regime(_image_page()) == "image"
    sparse = PageData(md_lines=["# Page 1", "", "a few words"], line_meta=PAGE1_META[:2], pno=1)
    assert _page_regime(sparse) == "skip"


def test_match_meta_best_effort():
    assert _match_meta("## Challenge", PAGE1_META) == (12.0, True, 50.0)
    assert _match_meta("| a | b |", PAGE1_META) == (0.0, False, 0.0)


# --- text regime ------------------------------------------------------------


def test_structure_paper_disabled_returns_none():
    assert structure_paper([_page1()], warnings=[]) is None


def test_text_regime_accepts_structural_amendments(monkeypatch):
    _enable(monkeypatch)
    reply = (
        "# What Makes Things Fun to Learn? Heuristics for Design\n\n"
        "*T. Malone · Xerox PARC*\n\n"
        "[Page 1](assets/test_paper/test_paper_page_01.png)\n\n"
        "## Challenge\n\n"
        "The first challenge is keeping players\n"
        "engaged through uncertain goals.\n\n"
        "## Fantasy\n\n"
        "Fantasy can make a game more\n"
        "compelling when it is intrinsic.\n\n"
        "## References\n"
    )
    _mock(monkeypatch, reply)
    out = structure_paper([_page1()], warnings=[])
    assert out is not None
    text = "\n".join(out)
    assert "## References" in text
    assert "# What Makes Things Fun to Learn? Heuristics for Design" in text
    assert "[Page 1](assets/test_paper/test_paper_page_01.png)" in text


def test_text_regime_rejects_altered_prose(monkeypatch):
    """The verbatim word cross-check rejects a model that rewrites prose."""
    _enable(monkeypatch)
    reply = "\n".join(PAGE1_MD).replace(
        "The first challenge is keeping players",
        "The first obstacle is keeping players",
    )
    warnings: list[str] = []
    _mock(monkeypatch, reply)
    assert structure_paper([_page1()], warnings=warnings) is None
    assert any("rejected" in w for w in warnings)


def test_text_regime_rejects_omission(monkeypatch):
    _enable(monkeypatch)
    reply = "\n".join(PAGE1_MD).replace("compelling when it is intrinsic.", "")
    warnings: list[str] = []
    _mock(monkeypatch, reply)
    assert structure_paper([_page1()], warnings=warnings) is None
    assert any("omitted" in w for w in warnings)


def test_text_regime_rejects_invented_heading(monkeypatch):
    _enable(monkeypatch)
    reply = "\n".join(PAGE1_MD) + "\n## Made Up Section\n"
    warnings: list[str] = []
    _mock(monkeypatch, reply)
    assert structure_paper([_page1()], warnings=warnings) is None
    assert any("added" in w for w in warnings)


def test_text_regime_keeps_page_n_heading_exact(monkeypatch):
    """On pages 2+ the `# Page N` heading is an anchor and cannot be renamed."""
    _enable(monkeypatch)
    reply = "\n".join(PAGE2_MD).replace("# Page 2", "# Page Two")
    warnings: list[str] = []
    _mock(monkeypatch, reply)
    assert structure_paper([_page2()], warnings=warnings) is None


def test_text_regime_allows_reorder_and_reflow(monkeypatch):
    _enable(monkeypatch)
    reply = (
        "# Page 2\n\n"
        "[Page 2](assets/test_paper/test_paper_page_02.png)\n\n"
        "## Curiosity\n\n"
        "Curiosity is sparked by surprise and by constructive feedback. "
        "Rewards can even backfire when they undermine intrinsic motivation.\n"
        "A well-designed game provides both challenge and fantasy together. "
        "Players stay engaged longer when they feel a sense of agency.\n\n"
        "## References\n"
    )
    _mock(monkeypatch, reply)
    out = structure_paper([_page2()], warnings=[])
    assert out is not None
    assert "## References" in "\n".join(out)


# --- image regime -----------------------------------------------------------


def test_image_regime_rewords_page(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("converter.structure.image_readable", lambda blob, ext="png": None)
    reply = (
        "# Conclusion\n\n"
        "[Page 3](assets/test_paper/test_paper_page_03.png)\n\n"
        "## Conclusion\n\n"
        "A conclusion wraps up the paper and points at future work.\n\n"
        "## References\n\n"
        "References and notes go here.\n"
    )
    _mock(monkeypatch, reply)
    out = structure_paper([_image_page()], warnings=[])
    assert out is not None
    text = "\n".join(out)
    assert "## References" in text
    assert "# Conclusion" in text
    assert "<details>" not in text


def test_image_regime_skips_unreadable_image(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("converter.structure.image_readable", lambda blob, ext="png": "blurry")
    warnings: list[str] = []
    _mock(monkeypatch, "anything")
    assert structure_paper([_image_page()], warnings=warnings) is None
    assert any("unreadable" in w for w in warnings)


def test_image_regime_discards_garbage_transcription(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("converter.structure.image_readable", lambda blob, ext="png": None)
    warnings: list[str] = []
    _mock(monkeypatch, "")
    assert structure_paper([_image_page()], warnings=warnings) is None
    assert any("discarded" in w for w in warnings)


# --- integration with convert_file ------------------------------------------


def test_pdf_paper_structure_off_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_MODE", "paper")
    monkeypatch.setattr("converter.structure.STRUCTURE_ENABLED", False)
    monkeypatch.setattr("converter.pdf.STRUCTURE_ENABLED", False)
    result = convert_file(PAPER, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "## Challenge" in text
    assert "## References" not in text
    assert "# Page 2" in text
    assert "page-break-after" not in text


def test_pdf_paper_structure_model_failure_matches_off_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_MODE", "paper")
    monkeypatch.setattr("converter.structure.STRUCTURE_ENABLED", True)
    monkeypatch.setattr("converter.pdf.STRUCTURE_ENABLED", True)

    def boom(*args, **kwargs):
        raise RuntimeError("server down")

    monkeypatch.setattr("converter.structure._chat_completion", boom)
    result = convert_file(PAPER, tmp_path / "on")
    assert result.error is None
    assert any("Structure pass failed" in w for w in result.warnings)

    monkeypatch.setattr("converter.structure.STRUCTURE_ENABLED", False)
    monkeypatch.setattr("converter.pdf.STRUCTURE_ENABLED", False)
    off = convert_file(PAPER, tmp_path / "off")
    assert off.error is None
    assert result.md_path.read_bytes() == off.md_path.read_bytes()


def test_pdf_paper_structure_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_MODE", "paper")
    monkeypatch.setattr("converter.structure.STRUCTURE_ENABLED", True)
    monkeypatch.setattr("converter.pdf.STRUCTURE_ENABLED", True)

    def echo(messages, **kw):
        content = messages[0]["content"]
        md = content.split("Current Markdown:\n", 1)[1].split("\n\nOutput only", 1)[0]
        return md + "\n## References\n"

    monkeypatch.setattr("converter.structure._chat_completion", echo)
    result = convert_file(PAPER, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "## References" in text
    assert "# Page 2" in text
    assert "# Page 3" in text
    assert "page-break-after" not in text
