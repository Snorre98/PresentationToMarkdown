"""EPUB export tests (ADR-0067).

Packaging tests use the real ``pandoc`` binary and are skipped when it is not
installed; the naming/configuration tests run everywhere.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from converter import config, convert_file, epub

PANDOC = epub.pandoc_available()

needs_pandoc = pytest.mark.skipif(not PANDOC, reason="pandoc not installed")


def _make_pdf(path: Path, *, title: str = "", author: str = "", image: bool = False) -> Path:
    """A small one-page PDF, optionally carrying an embedded figure."""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=480, height=360)
    page.insert_text((72, 72), "The Test Paper", fontsize=20)
    page.insert_text((72, 110), "Some body text on the first page.", fontsize=11)
    if image:
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40))
        pix.clear_with(180)
        page.insert_image(fitz.Rect(72, 150, 172, 220), pixmap=pix)
    doc.set_metadata({"title": title, "author": author})
    doc.save(path)
    doc.close()
    return path


def _write_figure(path: Path) -> None:
    import pymupdf as fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 14))
    pix.clear_with(120)
    pix.save(path)


def _sample_markdown(paper: bool = True) -> str:
    title = "# The Paper\n\n*Ada Lovelace*\n\n" if paper else ""
    return (
        f"{title}## Introduction\n\n"
        "Some **bold** and *italic* text with inline math $a^2+b^2=c^2$.\n\n"
        "![Figure](assets/the-paper/fig.png)\n\n"
        "$$E = mc^2$$\n\n"
        "| Col A | Col B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "[Page 1](assets/the-paper/page1.png)\n"
        "[image](assets/the-paper/logo.png)\n\n"
        '<div style="page-break-after: always; break-after: page;"></div>\n\n'
        "---\n\n"
        "## Second Section\n\nMore text.\n"
    )


def _texts(zf: zipfile.ZipFile, prefix: str = "EPUB/text/") -> str:
    return "\n".join(
        zf.read(name).decode("utf-8")
        for name in zf.namelist()
        if name.startswith(prefix) and name.endswith(".xhtml")
    )


@needs_pandoc
def test_build_epub_packages_structure_metadata_and_images(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_MODE", "paper")
    monkeypatch.delenv("IMAGE_LINKS", raising=False)
    source = _make_pdf(
        tmp_path / "source.pdf", title="The Paper", author="Ada Lovelace"
    )
    md = tmp_path / "the-paper.md"
    md.write_text(_sample_markdown(), encoding="utf-8")
    _write_figure(tmp_path / "assets" / "the-paper" / "fig.png")

    warnings: list[str] = []
    out = epub.build_epub(md, source, warnings)

    assert not any(
        "failed" in warning.lower() or "needs pandoc" in warning.lower()
        for warning in warnings
    )
    assert out == tmp_path / "the-paper.epub"
    assert out is not None and out.stat().st_size > 0

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        # A valid EPUB starts with an uncompressed mimetype entry.
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert "META-INF/container.xml" in names

        opf = zf.read("EPUB/content.opf").decode("utf-8")
        assert "The Paper" in opf
        assert "Ada Lovelace" in opf
        assert '<dc:language>en</dc:language>' in opf or "<dc:language>en" in opf
        assert "cover-image" in opf
        assert any(
            name.startswith("EPUB/media/") and name.endswith(".png")
            for name in names
        )

        body = _texts(zf)
        assert "Introduction" in body
        assert "Second Section" in body
        assert "<strong>bold</strong>" in body
        assert "<em>italic</em>" in body
        assert "<math" in body  # inline + display math become MathML
        assert "<table>" in body
        assert '<img src="../media/' in body

        # The metadata title replaces the duplicate paper-title heading, and
        # the authors line is not repeated in the body.
        assert "<h1>The Paper</h1>" not in body
        assert "<em>Ada Lovelace</em>" not in body

        # Slide-mode page-break scaffolding and non-image asset links are gone.
        assert "<hr" not in body
        assert "page-break-after" not in body
        assert 'href="assets/' not in body
        assert "Page 1</a>" not in body


@needs_pandoc
def test_epub_identifier_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_MODE", "paper")
    source = _make_pdf(tmp_path / "source.pdf", title="The Paper")
    md = tmp_path / "the-paper.md"
    md.write_text(_sample_markdown(), encoding="utf-8")
    _write_figure(tmp_path / "assets" / "the-paper" / "fig.png")

    first = epub.build_epub(md, source, [])
    assert first is not None
    with zipfile.ZipFile(first) as zf:
        first_id = re.search(
            r"urn:uuid:[0-9a-f-]+", zf.read("EPUB/content.opf").decode("utf-8")
        ).group(0)

    second = epub.build_epub(md, source, [])
    assert second is not None
    with zipfile.ZipFile(second) as zf:
        second_id = re.search(
            r"urn:uuid:[0-9a-f-]+", zf.read("EPUB/content.opf").decode("utf-8")
        ).group(0)

    assert first_id == second_id


@needs_pandoc
def test_epub_language_env_overrides_the_pdf_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("EPUB_LANG", "nb-NO")
    source = _make_pdf(tmp_path / "source.pdf", title="The Paper")
    md = tmp_path / "the-paper.md"
    md.write_text("# The Paper\n\nBody.\n", encoding="utf-8")

    out = epub.build_epub(md, source, [])

    assert out is not None
    with zipfile.ZipFile(out) as zf:
        opf = zf.read("EPUB/content.opf").decode("utf-8")
    assert "<dc:language>nb-NO</dc:language>" in opf


@needs_pandoc
def test_epub_uses_the_paper_author_line_when_metadata_is_empty(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PDF_MODE", "paper")
    source = _make_pdf(tmp_path / "source.pdf")
    md = tmp_path / "the-paper.md"
    md.write_text(
        "# The Paper\n\n*Ada Lovelace*\n\n## Introduction\n\nBody.\n",
        encoding="utf-8",
    )

    out = epub.build_epub(md, source, [])

    assert out is not None
    with zipfile.ZipFile(out) as zf:
        opf = zf.read("EPUB/content.opf").decode("utf-8")
        body = _texts(zf)
    assert "Ada Lovelace" in opf
    assert "<em>Ada Lovelace</em>" not in body


def test_missing_pandoc_warns_and_leaves_no_epub(tmp_path, monkeypatch):
    source = _make_pdf(tmp_path / "source.pdf")
    md = tmp_path / "the-paper.md"
    md.write_text("# Paper\n\nBody.\n", encoding="utf-8")
    monkeypatch.setattr(epub, "PANDOC_PATH", str(tmp_path / "no-such-pandoc"))

    warnings: list[str] = []
    assert epub.build_epub(md, source, warnings) is None
    assert not (tmp_path / "the-paper.epub").exists()
    assert any("pandoc" in warning.lower() for warning in warnings)


def test_force_epub_images_is_context_scoped(monkeypatch):
    monkeypatch.delenv("IMAGE_LINKS", raising=False)
    assert config.image_links_enabled() is False
    assert config.page_links_enabled() is False

    with config.force_epub_images():
        assert config.image_links_enabled() is True
        assert config.page_links_enabled() is False

    assert config.image_links_enabled() is False
    assert config.page_links_enabled() is False


def test_force_epub_images_keeps_explicit_page_links(monkeypatch):
    monkeypatch.setenv("IMAGE_LINKS", "on")
    with config.force_epub_images():
        assert config.image_links_enabled() is True
        assert config.page_links_enabled() is True


def test_config_snapshot_reports_epub(monkeypatch):
    monkeypatch.setenv("EPUB_OUTPUT", "on")
    snapshot = config.snapshot(probe=False)
    assert snapshot["epub_output"] is True
    assert isinstance(snapshot["pandoc"], bool)


def test_epub_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("EPUB_OUTPUT", raising=False)
    pdf = _make_pdf(tmp_path / "deck.pdf")
    result = convert_file(pdf, output_dir=tmp_path / "out")

    assert result.error is None
    assert result.md_path is not None and result.md_path.exists()
    assert result.epub_path is None
    assert not (tmp_path / "out" / "deck.epub").exists()


@needs_pandoc
def test_pdf_conversion_exports_epub_with_figures_but_no_page_renders(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EPUB_OUTPUT", "on")
    monkeypatch.delenv("IMAGE_LINKS", raising=False)
    monkeypatch.setenv("PDF_MODE", "slide")
    pdf = _make_pdf(tmp_path / "deck.pdf", image=True)
    out = tmp_path / "out"

    result = convert_file(pdf, output_dir=out)

    assert result.error is None
    assert result.md_path == out / "deck.md"
    assert result.epub_path == out / "deck.epub"
    assert result.epub_path is not None and result.epub_path.exists()

    markdown = result.md_path.read_text(encoding="utf-8")
    assert "![image](assets/deck/" in markdown
    assert "[Page " not in markdown  # page renders stay out of the EPUB run

    assets = list((out / "assets" / "deck").glob("*.png"))
    assert assets
    assert not any("_page_" in path.name for path in assets)

    with zipfile.ZipFile(result.epub_path) as zf:
        assert any(
            name.startswith("EPUB/media/") and name.endswith(".png")
            for name in zf.namelist()
        )


@needs_pandoc
def test_default_layout_exports_kebab_epub_before_archiving(tmp_path, monkeypatch):
    monkeypatch.setenv("EPUB_OUTPUT", "on")
    source = _make_pdf(tmp_path / "03 What makes things fun.pdf")

    result = convert_file(source)

    assert result.error is None
    assert result.md_path == tmp_path / "03-what-makes-things-fun.md"
    assert result.epub_path == tmp_path / "03-what-makes-things-fun.epub"
    assert result.moved_to == (
        tmp_path / "source" / "03-what-makes-things-fun.pdf"
    )
    assert result.epub_path.exists()
    assert result.moved_to.exists()


@needs_pandoc
def test_epub_export_keeps_explicit_image_links(tmp_path, monkeypatch):
    monkeypatch.setenv("EPUB_OUTPUT", "on")
    monkeypatch.setenv("IMAGE_LINKS", "on")
    monkeypatch.setenv("PDF_MODE", "slide")
    pdf = _make_pdf(tmp_path / "deck.pdf", image=True)
    out = tmp_path / "out"

    result = convert_file(pdf, output_dir=out)

    assert result.error is None
    assert result.md_path is not None and result.epub_path is not None
    markdown = result.md_path.read_text(encoding="utf-8")
    assert "[Page 1](assets/deck/" in markdown
    assets = list((out / "assets" / "deck").glob("*.png"))
    assert any("_page_" in path.name for path in assets)

    with zipfile.ZipFile(result.epub_path) as zf:
        assert "Page 1</a>" not in _texts(zf)


@needs_pandoc
def test_inference_marker_propagates_to_epub(tmp_path, monkeypatch):
    monkeypatch.setenv("EPUB_OUTPUT", "on")
    monkeypatch.setattr("converter.inference.used", lambda: True)
    monkeypatch.setattr("converter.inference.passes", lambda: ["format"])
    pdf = _make_pdf(tmp_path / "deck.pdf")
    out = tmp_path / "out"

    result = convert_file(pdf, output_dir=out)

    assert result.error is None
    assert result.md_path is not None
    assert result.epub_path is not None
    assert result.md_path.name == "deck-inference-used.md"
    assert result.epub_path.name == "deck-inference-used.epub"
    assert result.epub_path.exists()


@needs_pandoc
def test_existing_marked_epub_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("EPUB_OUTPUT", "on")
    monkeypatch.setattr("converter.inference.used", lambda: True)
    monkeypatch.setattr("converter.inference.passes", lambda: ["format"])
    pdf = _make_pdf(tmp_path / "deck.pdf")
    out = tmp_path / "out"
    out.mkdir()
    old = out / "deck-inference-used.epub"
    old.write_bytes(b"previous inference artifact")

    result = convert_file(pdf, output_dir=out)

    assert result.md_path is not None
    assert result.epub_path is not None
    assert result.md_path.name == "deck-inference-used-2.md"
    assert result.epub_path.name == "deck-inference-used-2.epub"
    assert old.read_bytes() == b"previous inference artifact"
