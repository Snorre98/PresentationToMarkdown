"""Tests for the LaTeX project converter (ADR-0038/0056/0057)."""
from pathlib import Path

import pytest

from converter import collect_inputs, config, convert_file
from converter.latex import LatexConverter, latex_available, pandoc_available

from tests.make_test_latex import (
    build_figure_project,
    build_project,
    build_titlepage_project,
)

needs_pandoc = pytest.mark.skipif(
    not pandoc_available(), reason="pandoc not installed"
)
needs_latex = pytest.mark.skipif(
    not latex_available(), reason="pdflatex not installed"
)


def _tikz_available() -> bool:
    """Whether this pdflatex can render a standalone TikZ figure.

    Probed once through the converter's own renderer — the private helpers are
    imported on purpose, so the marker skips exactly when the figure pass would
    degrade to captions.
    """
    if not latex_available():
        return False
    from converter.latex import _FIGURE_PREAMBLE_DEFAULT, _render_figure_png

    png = _render_figure_png(
        "\\begin{tikzpicture}\\draw (0,0) rectangle (1,0.6);\\end{tikzpicture}",
        Path("."),
        _FIGURE_PREAMBLE_DEFAULT,
    )
    return png is not None


needs_tikz = pytest.mark.skipif(
    not _tikz_available(), reason="pdflatex with tikz not available"
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


def test_latex_chunks_by_sections():
    # LaTeX output is one continuous document (`# Title` + `##` sections),
    # so the summary pass chunks it by section, not by slide (ADR-0052).
    assert LatexConverter.chunking == "sections"


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
    assert "![A plot.](" not in text  # figures dropped by default (ADR-0047)
    assert not (tmp_path / "assets").exists()


@needs_pandoc
def test_convert_latex_project_default_layout(tmp_path):
    """A project folder defaults to <folder>.md beside it + source/<folder>/ (ADR-0048)."""
    project = build_project(tmp_path)
    result = convert_file(project)
    assert result.error is None
    assert result.md_path == tmp_path / "paper.md"
    assert result.moved_to == tmp_path / "source" / "paper"
    assert (result.moved_to / "main.tex").exists()
    assert not project.exists()


@needs_pandoc
def test_convert_latex_image_links_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_LINKS", "on")
    project = build_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "![Figure 1](assets/paper/" in text  # image extracted, numbered alt
    assert "**Figure 1: A plot.**" in text  # caption kept as a numbered line
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
def test_convert_latex_display_math_passthrough(tmp_path):
    project = build_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "E = mc^2" in text  # display math stays as text by default
    assert "$$" in text  # $$...$$ passthrough, not rendered to a PNG
    assert not (tmp_path / "assets").exists()


@needs_pandoc
@needs_latex
def test_convert_latex_display_math_to_image(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_LINKS", "on")
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


# --- Title block (ADR-0056) ------------------------------------------------


@needs_pandoc
def test_latex_title_from_titlepage(tmp_path):
    """No ``\\title``: the title page's biggest line becomes the title."""
    project = build_titlepage_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert text.startswith("# Assignment 7 - Problem D\n\n## Introduction\n")
    # The rest of the title page is template chrome, not content.
    assert "*Alice*" not in text  # no \author → no authors line
    assert "Department of Computer Science" not in text
    assert "Word count" not in text
    assert "Autumn 2026" not in text


@needs_pandoc
def test_latex_title_block_unchanged_with_title(tmp_path):
    """A project that sets ``\\title``/``\\author`` keeps the old title block."""
    project = build_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert text.startswith("# Test LaTeX Paper\n\n*Alice · Bob*\n\n## Introduction\n")


# --- Figures, tables, cross-references (ADR-0056/0057) ---------------------


@needs_pandoc
def test_latex_refs_become_plain_text(tmp_path):
    """``\\autoref``/``\\ref``/``\\eqref`` print like the compiled PDF."""
    project = build_figure_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    # \autoref → "<Word> N", \ref → the bare number, \eqref → "(N)".
    assert "See Figure 1, 2, Table 1, (1) and" in text
    # An unresolvable label keeps pandoc's raw anchor form.
    assert "](#app:missing)" in text
    assert "{#" not in text  # no pandoc attributes left, on headings or captions
    assert "<math" not in text
    assert "<figure" not in text


@needs_pandoc
def test_latex_heading_attrs_stripped(tmp_path):
    project = build_project(tmp_path)
    result = convert_file(project, tmp_path)
    text = result.md_path.read_text(encoding="utf-8")
    assert "## Introduction\n" in text
    assert "## Results\n" in text
    assert "{#" not in text


@needs_pandoc
@needs_tikz
def test_latex_tikz_and_pgfplots_figures_rendered(tmp_path, monkeypatch):
    """TikZ/pgfplots figures render to PNGs; captions stay as numbered lines."""
    monkeypatch.setenv("IMAGE_LINKS", "on")
    project = build_figure_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "![Figure 1](assets/figs/" in text
    assert "![Figure 2](assets/figs/" in text
    assert "![Figure 3](assets/figs/" in text  # the \includegraphics figure
    assert "**Figure 1: The $c_{FN} \\gg c_{FP}$ flow.**" in text
    assert "**Figure 2: A pgfplots line.**" in text
    assert "**Figure 3: A picture.**" in text
    assert "**Table 1: Key figures**" in text
    assert "<figure" not in text and "<math" not in text
    pngs = sorted((tmp_path / "assets" / "figs").glob("*.png"))
    assert len(pngs) == 4  # three figures plus the rendered display equation
    assert all(p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for p in pngs)
    # The two rendered figures are real drawings; the third is the fixture's
    # own tiny synthetic PNG, and the fourth the rendered equation.
    rendered = [p for p in pngs if p.name.startswith(("figs_01", "figs_02"))]
    assert len(rendered) == 2
    assert all(p.stat().st_size > 1000 for p in rendered)


@needs_pandoc
def test_latex_figures_captions_only_without_image_links(tmp_path):
    """``IMAGE_LINKS`` off: captions stand in, one warning per figure."""
    project = build_figure_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "**Figure 1: The $c_{FN} \\gg c_{FP}$ flow.**" in text
    assert "**Figure 2: A pgfplots line.**" in text
    assert "**Figure 3: A picture.**" in text
    assert "![Figure" not in text
    assert "<figure" not in text and "<math" not in text
    assert not (tmp_path / "assets").exists()
    dropped = [w for w in result.warnings if "image omitted (IMAGE_LINKS=off)" in w]
    assert len(dropped) == 3


@needs_pandoc
def test_latex_figure_render_degrades_without_pdflatex(tmp_path, monkeypatch):
    """A missing pdflatex warns and keeps captions; the conversion succeeds."""
    monkeypatch.setenv("IMAGE_LINKS", "on")
    monkeypatch.setattr("converter.latex.PDLATEX_PATH", "/nonexistent/pdflatex")
    project = build_figure_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "**Figure 1: The $c_{FN} \\gg c_{FP}$ flow.**" in text
    assert "![Figure 1]" not in text
    assert any("pdflatex not available" in w for w in result.warnings)
    # The \includegraphics figure needs no pdflatex and is still extracted.
    assert "![Figure 3](assets/figs/" in text


@needs_pandoc
@needs_tikz
def test_latex_figure_style_from_project_sty(tmp_path, monkeypatch):
    """A figure style defined in the project's own ``.sty`` is picked up."""
    monkeypatch.setenv("IMAGE_LINKS", "on")
    project = build_figure_project(tmp_path)
    # The flowchart uses ``box`` from packages.sty: without that file the
    # render fails, so a reference proves the style was collected.
    (project / "packages.sty").unlink()
    main = project / "main.tex"
    main.write_text(
        main.read_text(encoding="utf-8").replace("\\usepackage{packages}\n", ""),
        encoding="utf-8",
    )
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "![Figure 1]" not in text  # style gone → degrade


@needs_pandoc
@needs_tikz
def test_latex_figure_preamble_env(tmp_path, monkeypatch):
    """``LATEX_FIGURE_PREAMBLE`` supplies the styles a project does not."""
    project = build_figure_project(tmp_path)
    (project / "packages.sty").unlink()
    main = project / "main.tex"
    main.write_text(
        main.read_text(encoding="utf-8").replace("\\usepackage{packages}\n", ""),
        encoding="utf-8",
    )
    preamble = tmp_path / "preamble.tex"
    preamble.write_text(
        "\\tikzset{box/.style = {rectangle, draw, fill=green!20}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("IMAGE_LINKS", "on")
    monkeypatch.setenv("LATEX_FIGURE_PREAMBLE", str(preamble))
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "![Figure 1](assets/figs/" in text


@needs_pandoc
def test_latex_figures_off_keeps_pandoc_output(tmp_path, monkeypatch):
    """``LATEX_FIGURES=off`` is the escape hatch to the pre-ADR-0057 output."""
    monkeypatch.setenv("IMAGE_LINKS", "on")
    monkeypatch.setenv("LATEX_FIGURES", "off")
    project = build_figure_project(tmp_path)
    result = convert_file(project, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "<figure" in text  # pandoc's own caption-only stub
    assert "![Figure" not in text
