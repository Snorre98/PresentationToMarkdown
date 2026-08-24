"""Unit tests for the Markdown polish post-pass (converter.format)."""
from converter.format import (
    _deterministic_pass,
    _is_structural,
    _peel_trailer,
    _reformat_slide,
    _verify_preserved,
    polish_text,
)


def test_deterministic_strips_trailing_whitespace():
    md = "line one  \n- bullet\t\n\nnext"
    assert _deterministic_pass(md) == "line one\n- bullet\n\nnext"


def test_deterministic_collapses_blank_lines():
    assert _deterministic_pass("a\n\n\n\n\nb") == "a\n\n\nb"


def test_deterministic_preserves_single_and_double_blank_lines():
    assert _deterministic_pass("a\n\nb") == "a\n\nb"
    assert _deterministic_pass("a\n\n\nb") == "a\n\n\nb"
    assert _deterministic_pass("a\n\n\n\nb") == "a\n\n\nb"


def test_deterministic_spaces_heading():
    md = "# Heading\nbody\n# Next"
    out = _deterministic_pass(md)
    assert "# Heading\n\nbody\n\n# Next" == out


def test_deterministic_does_not_space_heading_inside_code_block():
    md = "```markdown\n# Title\n\nbody\n```"
    assert _deterministic_pass(md) == md


def test_deterministic_keeps_pagebreak_separator():
    md = (
        "# Slide — Page 1\n\n"
        "- item\n\n"
        '<div style="page-break-after: always; break-after: page;"></div>\n\n'
        "---\n\n"
        "# Slide — Page 2"
    )
    assert _deterministic_pass(md) == md


def test_is_structural():
    assert _is_structural("# Title")
    assert _is_structural("![image](assets/x.png)")
    assert _is_structural("[Page 2](assets/x.png)")
    assert _is_structural("> blockquote")
    assert _is_structural("| a | b |")
    assert _is_structural("<details>")
    assert _is_structural("```markdown")
    assert _is_structural("---")
    assert not _is_structural("- plain bullet")
    assert not _is_structural("**Purpose**: some text")


def test_peel_trailer():
    lines = [
        "# Title — Page 1",
        "",
        "- item",
        "",
        '<div style="page-break-after: always; break-after: page;"></div>',
        "",
        "---",
        "",
    ]
    body, trailer = _peel_trailer(lines)
    assert body == ["# Title — Page 1", "", "- item"]
    assert trailer[0] == '<div style="page-break-after: always; break-after: page;"></div>'
    assert "---" in trailer
    assert trailer[-1] != ""


def test_verify_preserved_accepts_reorder():
    original = "- The course topics:\n  - Modelling\n  - Innovation"
    reformatted = "## The course topics\n- Modelling\n- Innovation"
    assert _verify_preserved(original, reformatted) == []


def test_verify_preserved_rejects_omission():
    original = "- The course topics:\n  - Modelling\n  - Innovation"
    reformatted = "## The course topics\n- Modelling"
    problems = _verify_preserved(original, reformatted)
    assert any("omitted" in p and "innovation" in p for p in problems)


def test_verify_preserved_rejects_addition():
    original = "- Modelling"
    reformatted = "- Modelling\n- Fabricated new item"
    problems = _verify_preserved(original, reformatted)
    assert any("added" in p and "fabricated" in p for p in problems)


def test_reformat_slide_promotes_headings(monkeypatch):
    slide = (
        "# Course Outline — Page 7\n\n"
        "- The course will consist of the following topics:\n"
        "  - Enterprise Modelling\n"
        "  - Sustainable Business models\n\n"
        '<div style="page-break-after: always; break-after: page;"></div>\n\n'
        "---"
    )
    restructured = (
        "# Course Outline — Page 7\n\n"
        "## The course will consist of the following topics\n"
        "- Enterprise Modelling\n"
        "- Sustainable Business models"
    )

    monkeypatch.setattr(
        "converter.format._chat_completion",
        lambda messages, **kw: restructured,
    )
    out = _reformat_slide(slide)
    assert "## The course will consist of the following topics" in out
    assert 'page-break-after: always' in out
    assert out.rstrip().endswith("---")


def test_reformat_slide_keeps_original_on_omission(monkeypatch):
    slide = (
        "# Slide — Page 1\n\n"
        "- The course topics:\n"
        "  - Modelling\n"
        "  - Innovation\n\n"
        '<div style="page-break-after: always; break-after: page;"></div>\n\n'
        "---"
    )
    monkeypatch.setattr(
        "converter.format._chat_completion",
        lambda messages, **kw: "# Slide — Page 1\n\n## The course topics\n- Modelling",
    )
    assert _reformat_slide(slide) == slide


def test_polish_text_disabled_llm_is_deterministic_only(monkeypatch):
    monkeypatch.setattr("converter.format.FORMAT_ENABLED", False)
    assert polish_text("a   \n\n\n\n\nb") == "a\n\n\nb"


def test_polish_text_enabled_runs_llm(monkeypatch):
    monkeypatch.setattr("converter.format.FORMAT_ENABLED", True)
    monkeypatch.setattr(
        "converter.format._chat_completion",
        lambda messages, **kw: "# Slide — Page 1\n\n## The course topics\n- Modelling",
    )
    md = "# Slide — Page 1\n\n- The course topics:\n  - Modelling"
    out = polish_text(md, warnings=[])
    assert "## The course topics" in out
