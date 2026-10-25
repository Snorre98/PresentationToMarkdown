"""Golden tests for the page-scoped slide-mode paragraph model (ADR-0070).

The fixture is generated at test time: per-page titles, a repeated running
header/footer, wrapped body paragraphs, a split bold run, nested bullets, a
conservative subheading tier, a diagram-heavy page, and a monospace code page.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from converter import convert_file

PAGE_W, PAGE_H = 720, 540
HEADER = "Deck Running Header"
FOOTER = "Sample Footer 2026"


def _text(page, x, y, text, size, bold=False, mono=False):
    font = "hebo" if bold else ("cour" if mono else "helv")
    page.insert_text((x, y), text, fontsize=size, fontname=font)


def _mixed(page, x, y, parts, size):
    cursor = x
    for text, bold in parts:
        font = "hebo" if bold else "helv"
        page.insert_text((cursor, y), text, fontsize=size, fontname=font)
        cursor += fitz.get_text_length(text, fontname=font, fontsize=size)


def _make_slide_pdf(path: Path) -> Path:
    doc = fitz.open()

    # Page 1: title, subheading, two paragraphs, split bold run, nested bullets.
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _text(page, 72, 30, HEADER, 10)
    _text(page, 72, 70, "Deck One", 28)
    _text(page, 72, 112, "Context", 20)
    _text(page, 72, 152, "The first paragraph wraps across", 14)
    _text(page, 72, 170, "two visual lines and is one Markdown line.", 14)
    _text(page, 72, 205, "A second paragraph sits below the gap.", 14)
    _mixed(
        page, 72, 240,
        [("The estimates cover approximately ", False), ("10 available", True)],
        14,
    )
    _mixed(
        page, 72, 258,
        [("additional inspections", True), (".", False)],
        14,
    )
    page.insert_text((72, 300), "\u2022", fontsize=14)
    _text(page, 90, 300, "Outer bullet", 14)
    page.insert_text((100, 326), "\u2022", fontsize=14)
    _text(page, 118, 326, "Inner bullet", 14)
    page.insert_text((72, 352), "\u2022", fontsize=14)
    _text(page, 90, 352, "Second outer.", 14)
    _text(page, 72, 390, "A paragraph follows the list.", 14)
    _text(page, 72, 440, "This sentence continues", 14)
    _text(page, 72, 510, FOOTER, 10)
    page.insert_text((680, 510), "1", fontsize=10)

    # Page 2: same title/subheading styles; the page-1 sentence must not merge.
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _text(page, 72, 30, HEADER, 10)
    _text(page, 72, 70, "Deck Two", 28)
    _text(page, 72, 112, "Context", 20)
    _text(page, 72, 155, "on the next slide.", 14)
    _text(page, 72, 200, "A standalone paragraph on slide two.", 14)
    _text(page, 72, 250, "A Real Subheading", 14, bold=True)
    _text(page, 72, 280, "Its body follows.", 14)
    _text(page, 72, 510, FOOTER, 10)
    page.insert_text((680, 510), "2", fontsize=10)

    # Page 3: diagram-heavy; display-sized labels must not become headings.
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _text(page, 72, 30, HEADER, 10)
    _text(page, 72, 70, "Diagram", 28)
    for index, x in enumerate((40, 120, 200, 280, 360, 440, 520, 600)):
        _text(page, x, 140 + 30 * index, f"Label {index + 1}", 18)
    _text(page, 72, 510, FOOTER, 10)
    page.insert_text((680, 510), "3", fontsize=10)

    # Page 4: monospace code keeps its lines and is fenced.
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    _text(page, 72, 30, HEADER, 10)
    _text(page, 72, 70, "Code", 28)
    _text(page, 72, 140, "lw   $t0, 4($gp)", 12, mono=True)
    _text(page, 72, 158, "ori  $t1, $zero, 3", 12, mono=True)
    _text(page, 72, 176, "mult $t1, $t1, $t0", 12, mono=True)
    _text(page, 72, 220, "The code above is illustrative.", 14)
    _text(page, 72, 260, "1996", 14)
    _text(page, 72, 510, FOOTER, 10)
    page.insert_text((680, 510), "4", fontsize=10)

    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def slide_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_MODE", "slide")
    return _make_slide_pdf(tmp_path / "slides.pdf")


def _convert(path: Path, out: Path, monkeypatch, reflow: str | None = None) -> str:
    monkeypatch.setenv("PDF_MODE", "slide")
    if reflow is not None:
        monkeypatch.setenv("PDF_REFLOW", reflow)
    result = convert_file(path, out)
    assert result.error is None
    assert result.md_path is not None
    return result.md_path.read_text(encoding="utf-8")


def test_slide_reflows_paragraphs_and_merges_bold_runs(slide_pdf, tmp_path, monkeypatch):
    text = _convert(slide_pdf, tmp_path / "out", monkeypatch)

    assert "# Deck One — Page 1" in text
    assert (
        "The first paragraph wraps across two visual lines and is one Markdown line."
        in text
    )
    assert "A second paragraph sits below the gap." in text
    assert (
        "The estimates cover approximately **10 available additional inspections**."
        in text
    )
    # A paragraph after a bullet list is not absorbed.
    assert "\n\nA paragraph follows the list.\n" in text


def test_slide_reconstructs_nested_bullets_and_strips_running_blocks(
    slide_pdf, tmp_path, monkeypatch
):
    text = _convert(slide_pdf, tmp_path / "out", monkeypatch)

    assert "- Outer bullet\n  - Inner bullet\n- Second outer." in text
    assert HEADER not in text
    assert FOOTER not in text


def test_slide_keeps_page_boundaries(slide_pdf, tmp_path, monkeypatch):
    text = _convert(slide_pdf, tmp_path / "out", monkeypatch)

    assert "# Deck Two — Page 2" in text
    assert "This sentence continues" in text
    assert "on the next slide." in text
    assert "This sentence continues on the next slide." not in text
    assert text.count('<div style="page-break-after: always; break-after: page;"></div>') == 4
    assert text.count("\n---\n") == 4


def test_slide_heading_policy_is_conservative(slide_pdf, tmp_path, monkeypatch):
    text = _convert(slide_pdf, tmp_path / "out", monkeypatch)

    # The repeated 20 pt style is a real subheading tier.
    assert text.count("## Context") == 2
    assert "## A Real Subheading" in text
    # The diagram page's display-sized labels stay text.
    assert "## Label" not in text
    # The slide's own display size is the top-level title, not a subheading.
    assert "## Deck One" not in text


def test_slide_fences_monospace_code(slide_pdf, tmp_path, monkeypatch):
    text = _convert(slide_pdf, tmp_path / "out", monkeypatch)

    assert (
        "```text\nlw   $t0, 4($gp)\nori  $t1, $zero, 3\n"
        "mult $t1, $t1, $t0\n```"
    ) in text
    assert "The code above is illustrative." in text
    # A digit-only body line survives the numeric footer/page-number key.
    assert "1996" in text
    # Bottom-band slide numbers are removed.
    assert not any(line.strip() in {"1", "2", "3", "4"} for line in text.splitlines())


def test_slide_reflow_off_keeps_line_shape_but_reconstructs_headings(
    slide_pdf, tmp_path, monkeypatch
):
    text = _convert(slide_pdf, tmp_path / "out", monkeypatch, reflow="off")

    assert "The first paragraph wraps across\ntwo visual lines" in text
    assert "## Context" in text


def test_slide_structure_pass_is_wired_to_pdf_pages(slide_pdf, tmp_path, monkeypatch):
    from converter import config

    config.set_enabled("structure", True)
    captured: dict[str, list] = {}

    def fake_structure(pages, warnings=None, source=""):
        captured["pages"] = pages
        return [
            "# Deck One — Page 1",
            "",
            "## Overview",
            "",
            "Amended slide body.",
            "",
            "",
            '<div style="page-break-after: always; break-after: page;"></div>',
            "",
            "---",
            "",
        ]

    monkeypatch.setattr("converter.pdf.structure_slides", fake_structure)
    monkeypatch.setenv("PDF_MODE", "slide")
    result = convert_file(slide_pdf, tmp_path / "out")

    assert result.error is None
    assert result.md_path is not None
    text = result.md_path.read_text(encoding="utf-8")
    assert "## Overview" in text
    assert result.md_path.name.endswith("-inference-used.md")
    assert "structure" in result.inference_passes
    assert [page.pno for page in captured["pages"]] == [1, 2, 3, 4]
    assert captured["pages"][0].md_lines[0] == "# Deck One — Page 1"


def test_slide_title_strips_internal_private_use_glyph():
    from converter.pdf import Line, _detect_title

    def line(text, y, size=32.0):
        return Line(
            text=text,
            bbox=(72.0, y, 400.0, y + size),
            spans=[],
            size=size,
            bold=False,
            italic=False,
        )

    title, _ = _detect_title([line("Contents", 50.0), line("\uf07dModules", 100.0)], 540.0)

    assert title == "Contents Modules"
