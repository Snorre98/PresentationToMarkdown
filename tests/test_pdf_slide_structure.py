"""Tests for the slide-mode structure pass (ADR-0071)."""
from __future__ import annotations

import pytest

from converter import config
from converter.structure import PageData, _canonical_slide_title, structure_slides

BODY = (
    "Slides need reliable paragraph reconstruction because wrapped lines hide "
    "the paragraph boundaries in the extracted text layer."
)


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    monkeypatch.setenv("NEED_GATE", "off")
    config.reset()
    yield
    config.reset()


def _page(pno: int = 1, title: str = "Slide One", body: str = BODY) -> PageData:
    return PageData(
        md_lines=[f"# {title} — Page {pno}", "", body, ""],
        line_meta=[{"text": body, "size": 14.0, "bold": False, "x0": 72.0}],
        png=None,
        pno=pno,
    )


def _reply(title: str, pno: int, heading: str = "", body: str = BODY) -> str:
    parts = [f"# {title} — Page {pno}", ""]
    if heading:
        parts.extend([heading, ""])
    parts.extend([body, ""])
    return "\n".join(parts)


def test_slide_structure_amends_subheadings(monkeypatch):
    config.set_enabled("structure", True)
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: _reply("Slide One", 1, "## Overview"),
    )

    out = structure_slides([_page()], warnings=[])

    assert out is not None
    text = "\n".join(out)
    assert "## Overview" in text
    assert '<div style="page-break-after: always; break-after: page;"></div>' in text
    assert text.rstrip().endswith("---")


def test_slide_structure_rejects_lost_page_suffix(monkeypatch):
    config.set_enabled("structure", True)
    warnings: list[str] = []
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: f"# Slide One\n\n{BODY}\n",
    )

    assert structure_slides([_page()], warnings=warnings) is None
    assert any("heading invariant" in warning for warning in warnings)


def test_slide_structure_rejects_second_top_level_heading(monkeypatch):
    config.set_enabled("structure", True)
    warnings: list[str] = []
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: (
            f"# Slide One — Page 1\n\n# Another Title\n\n{BODY}\n"
        ),
    )

    assert structure_slides([_page()], warnings=warnings) is None
    assert any("heading invariant" in warning for warning in warnings)


def test_slide_structure_rejects_wrong_page_number(monkeypatch):
    config.set_enabled("structure", True)
    warnings: list[str] = []
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: _reply("Slide One", 2),
    )

    assert structure_slides([_page()], warnings=warnings) is None
    assert any("heading invariant" in warning for warning in warnings)


def test_slide_structure_rejects_dropped_title_word(monkeypatch):
    config.set_enabled("structure", True)
    warnings: list[str] = []
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: f"# Slide — Page 1\n\n{BODY}\n",
    )

    assert structure_slides([_page(title="Slide Alpha")], warnings=warnings) is None
    assert any("omitted" in warning for warning in warnings)


def test_heading_invariant_ignores_fenced_code():
    from converter.structure import _slide_heading_ok, _top_level_heading_ok

    fenced = "# Title — Page 1\n\n```text\n# comment\n```\n"
    assert _slide_heading_ok(fenced, 1)
    assert _top_level_heading_ok(fenced, 1)
    assert not _slide_heading_ok(
        fenced + "\n# Second — Page 1\n", 1
    )
    assert not _slide_heading_ok("# — Page 1\n", 1)


def test_slide_structure_preserves_separator_scaffold(monkeypatch):
    config.set_enabled("structure", True)
    replies = iter(
        [
            _reply("Slide One", 1, "## Overview"),
            _reply("Slide Two", 2, "## Details"),
        ]
    )
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: next(replies),
    )

    out = structure_slides([_page(1, "Slide One"), _page(2, "Slide Two")], warnings=[])

    assert out is not None
    text = "\n".join(out)
    assert text.count('<div style="page-break-after: always; break-after: page;"></div>') == 2
    assert text.count("\n---\n") == 2
    assert text.index("# Slide One — Page 1") < text.index("## Overview")
    assert text.index("## Overview") < text.index("# Slide Two — Page 2")
    assert text.index("# Slide Two — Page 2") < text.index("## Details")


def test_slide_structure_image_reply_gets_page_suffix(monkeypatch):
    config.set_enabled("structure", True)
    page = PageData(
        md_lines=[
            "# Page 3",
            "",
            "<details>",
            "<summary>Raw extracted text</summary>",
            "",
            "garbled",
            "",
            "</details>",
            "",
        ],
        line_meta=[],
        png=b"fake-png",
        pno=3,
    )
    monkeypatch.setattr("converter.structure.image_readable", lambda *a, **k: None)
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: "# Model View\n\nSlide body text.\n",
    )

    out = structure_slides([page], warnings=[])

    assert out is not None
    text = "\n".join(out)
    assert "# Model View — Page 3" in text


def test_slide_structure_canonicalizes_a_model_page_number():
    assert (
        _canonical_slide_title("# Title — Page 9\n\nBody.", 3)
        == "# Title — Page 3\n\nBody."
    )
    assert _canonical_slide_title("Body.", 3).startswith("# Page 3")


def test_slide_structure_disabled_returns_none(monkeypatch):
    config.set_enabled("structure", False)
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: pytest.fail("model must not be called"),
    )
    assert structure_slides([_page()], warnings=[]) is None


def test_slide_structure_unchanged_echo_returns_none(monkeypatch):
    config.set_enabled("structure", True)
    monkeypatch.setattr(
        "converter.structure._chat_completion",
        lambda *args, **kwargs: _reply("Slide One", 1),
    )
    assert structure_slides([_page()], warnings=[]) is None
