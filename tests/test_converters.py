"""Smoke tests for both converters and the dispatch API."""
from pathlib import Path

import pytest

from converter import SUPPORTED_EXTENSIONS, convert_file, convert_files

TESTS_DIR = Path(__file__).parent
PPTX = TESTS_DIR / "test_deck.pptx"
PDF = TESTS_DIR / "test_doc.pdf"


def test_supported_extensions():
    assert {".pptx", ".pdf"} <= SUPPORTED_EXTENSIONS


def test_convert_pptx(tmp_path):
    result = convert_file(PPTX, tmp_path)
    assert result.error is None
    assert result.md_path is not None and result.md_path.exists()
    text = result.md_path.read_text(encoding="utf-8")
    assert text.startswith("# Test Deck")
    assert "assets/" in text


def test_convert_pdf(tmp_path):
    result = convert_file(PDF, tmp_path)
    assert result.error is None
    assert result.md_path is not None and result.md_path.exists()
    text = result.md_path.read_text(encoding="utf-8")
    assert "# Sample PDF Page" in text
    assert "# Second Page" in text
    assert "Some more body text." in text
    assert "![slide](assets/test_doc/" in text


def test_convert_pdf_bullets(tmp_path):
    result = convert_file(PDF, tmp_path)
    text = result.md_path.read_text(encoding="utf-8")
    assert "- First bullet item" in text
    assert "- Second bullet inline" in text


def test_convert_pdf_bold_formatting(tmp_path):
    result = convert_file(PDF, tmp_path)
    text = result.md_path.read_text(encoding="utf-8")
    assert "**Enterprise Modeling**" in text
    assert "** " not in text


def test_convert_pdf_footer_stripped(tmp_path):
    result = convert_file(PDF, tmp_path)
    text = result.md_path.read_text(encoding="utf-8")
    assert "Sample Footer" not in text


def test_unsupported_extension(tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("hello")
    result = convert_file(bogus, tmp_path)
    assert result.error is not None
    assert result.md_path is None


def test_convert_files_mixed(tmp_path):
    results = convert_files([PPTX, PDF], tmp_path)
    assert len(results) == 2
    assert all(r.error is None for r in results)


def test_convert_file_default_output_dir(tmp_path):
    import shutil

    src = tmp_path / "deck.pptx"
    shutil.copy(PPTX, src)
    result = convert_file(src)
    assert result.error is None
    assert result.md_path == tmp_path / "markdown" / "deck.md"
    assert result.md_path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
