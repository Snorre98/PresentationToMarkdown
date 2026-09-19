"""Tests for the LaTeX project converter (ADR-0038)."""
from pathlib import Path

import pytest

from converter import collect_inputs, config, convert_file
from converter.latex import LatexConverter, latex_available, pandoc_available

from tests.make_test_latex import build_project

needs_pandoc = pytest.mark.skipif(
    not pandoc_available(), reason="pandoc not installed"
)
needs_latex = pytest.mark.skipif(
    not latex_available(), reason="pdflatex not installed"
)


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset()
    yield
    config.reset()


def test_project_entry_detection(tmp_path):
    project = build_project(tmp_path)
    assert LatexConverter.project_entry(project) == project / "main.tex"
    assert LatexConverter.project_entry(tmp_path) is None  # parent of projects
    assert LatexConverter.project_entry(project / "Sections") is None  # fragments
    assert LatexConverter.project_entry(project / "images") is None  # no .tex


def test_latex_ai_passes_are_summary_only():
    assert LatexConverter.ai_passes == frozenset({"summary"})


def test_collect_inputs_detects_projects(tmp_path):
    build_project(tmp_path, "proj_a")
    build_project(tmp_path, "proj_b")
    (tmp_path / "deck.pptx").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    inputs = collect_inputs([tmp_path])
    assert sorted(p.name for p in inputs) == ["deck.pptx", "proj_a", "proj_b"]


def test_convert_latex_missing_pandoc(tmp_path, monkeypatch):
    project = build_project(tmp_path)
    monkeypatch.setattr("converter.latex.PANDOC_PATH", "/nonexistent/pandoc")
    result = convert_file(project, tmp_path)
    assert result.error is not None
    assert "pandoc" in result.error
    assert result.md_path is None


@needs_pandoc
def test_convert_latex_project(tmp_path):
    project = build_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    assert result.md_path == tmp_path / "paper.md"
    text = result.md_path.read_text(encoding="utf-8")
    assert text.startswith("# Test LaTeX Paper")
    assert "*Alice · Bob*" in text
    assert "## Introduction" in text
    assert "## Results" in text
    assert "**bold**" in text
    assert "*emphasis*" in text
    assert "Inline math $x^2 + y^2 = z^2$." in text  # inline math passthrough
    assert "![A plot.](assets/paper/" in text
    assert (tmp_path / "assets" / "paper").exists()
    assert list((tmp_path / "assets" / "paper").glob("*.png"))  # extracted image


@needs_pandoc
def test_convert_latex_single_tex_file(tmp_path):
    project = build_project(tmp_path)
    result = convert_file(project / "main.tex", tmp_path)
    assert result.error is None
    assert result.md_path == tmp_path / "main.md"
    text = result.md_path.read_text(encoding="utf-8")
    assert text.startswith("# Test LaTeX Paper")


@needs_pandoc
@needs_latex
def test_convert_latex_display_math_to_image(tmp_path):
    project = build_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "E = mc^2" in text  # equation body (as the rendered image's alt)
    assert "$$" not in text  # display math rendered, not left as $$...$$
    pngs = list((tmp_path / "assets" / "paper").glob("*.png"))
    assert len(pngs) == 2  # the figure PNG plus the rendered equation


@needs_pandoc
def test_convert_latex_no_ai_format_pass(tmp_path):
    project = build_project(tmp_path)
    config.set_enabled("format", True)
    result = convert_file(project, tmp_path)
    assert result.error is None
    assert not any("Markdown LLM reformat" in w for w in result.warnings)


@needs_pandoc
def test_convert_latex_bibliography(tmp_path):
    project = build_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "## Bibliography" in text
    assert "Knuth" in text
