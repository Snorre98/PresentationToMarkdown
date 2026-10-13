"""Tests for best-effort PDF compression (ADR-0049)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from converter import compress

TESTS_DIR = Path(__file__).parent
PDF = TESTS_DIR / "test_doc.pdf"


def _blank_pdf(path: Path, pages: int = 4) -> None:
    import pymupdf as fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    doc.close()


def _out_arg(cmd: list[str]) -> Path:
    for arg in cmd:
        if arg.startswith("-sOutputFile="):
            return Path(arg.split("=", 1)[1])
    return Path(cmd[-1])


def _patch_run(monkeypatch, run) -> None:
    """Replace the subprocess module the compressor calls ``run`` on."""
    monkeypatch.setattr(
        compress,
        "subprocess",
        SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError),
    )


def _writes_pdf(pages: int = 4):
    def run(cmd, check=True, capture_output=True, timeout=None):
        _blank_pdf(_out_arg(cmd), pages=pages)

    return run


def test_compress_pdf_replaces_when_smaller(tmp_path, monkeypatch):
    source = tmp_path / "doc.pdf"
    source.write_bytes(PDF.read_bytes())
    monkeypatch.setattr(compress, "gs_available", lambda: True)
    _patch_run(monkeypatch, _writes_pdf(pages=4))
    warnings: list[str] = []
    assert compress.compress_pdf(source, warnings) is True
    assert warnings == []
    import pymupdf as fitz

    with fitz.open(source) as doc:
        assert doc.page_count == 4


def test_compress_pdf_keeps_original_without_gain(tmp_path, monkeypatch):
    source = tmp_path / "doc.pdf"
    source.write_bytes(PDF.read_bytes())
    original_size = source.stat().st_size
    monkeypatch.setattr(compress, "gs_available", lambda: True)
    monkeypatch.setattr(compress, "_sane", lambda out, original: True)

    def run(cmd, check=True, capture_output=True, timeout=None):
        out = _out_arg(cmd)
        _blank_pdf(out)
        with out.open("ab") as fh:
            fh.write(b"x" * (original_size + 1024))

    _patch_run(monkeypatch, run)
    warnings: list[str] = []
    assert compress.compress_pdf(source, warnings) is False
    assert warnings == []
    assert source.stat().st_size == original_size


def test_compress_pdf_keeps_original_when_output_invalid(tmp_path, monkeypatch):
    source = tmp_path / "doc.pdf"
    source.write_bytes(PDF.read_bytes())
    original = source.read_bytes()
    monkeypatch.setattr(compress, "gs_available", lambda: True)

    def run(cmd, check=True, capture_output=True, timeout=None):
        _out_arg(cmd).write_bytes(b"junk")

    _patch_run(monkeypatch, run)
    warnings: list[str] = []
    assert compress.compress_pdf(source, warnings) is False
    assert any("invalid" in w.lower() for w in warnings)
    assert source.read_bytes() == original


def test_compress_pdf_qpdf_fallback(tmp_path, monkeypatch):
    source = tmp_path / "doc.pdf"
    source.write_bytes(PDF.read_bytes())
    monkeypatch.setattr(compress, "gs_available", lambda: False)
    monkeypatch.setattr(compress, "qpdf_available", lambda: True)
    seen: list[list[str]] = []

    def run(cmd, check=True, capture_output=True, timeout=None):
        seen.append(cmd)
        _blank_pdf(_out_arg(cmd))

    _patch_run(monkeypatch, run)
    assert compress.compress_pdf(source, []) is True
    assert seen and seen[0][0] == compress.QPDF_PATH


def test_compress_pdf_skips_without_binaries(tmp_path, monkeypatch):
    source = tmp_path / "doc.pdf"
    source.write_bytes(PDF.read_bytes())
    original = source.read_bytes()
    monkeypatch.setattr(compress, "gs_available", lambda: False)
    monkeypatch.setattr(compress, "qpdf_available", lambda: False)
    warnings: list[str] = []
    assert compress.compress_pdf(source, warnings) is False
    assert any("neither gs nor qpdf" in w for w in warnings)
    assert source.read_bytes() == original


def test_compress_pdf_subprocess_failure_keeps_original(tmp_path, monkeypatch):
    source = tmp_path / "doc.pdf"
    source.write_bytes(PDF.read_bytes())
    original = source.read_bytes()
    monkeypatch.setattr(compress, "gs_available", lambda: True)

    def run(cmd, check=True, capture_output=True, timeout=None):
        raise subprocess.CalledProcessError(1, cmd)

    _patch_run(monkeypatch, run)
    warnings: list[str] = []
    assert compress.compress_pdf(source, warnings) is False
    assert any("failed" in w.lower() for w in warnings)
    assert source.read_bytes() == original
