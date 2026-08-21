"""Render PPTX slides/charts to PNG via headless LibreOffice + PyMuPDF.

``python-pptx`` has no rendering engine, so charts (which live as a
``graphicFrame`` with an embedded workbook rather than pixels) are rendered by
converting the deck to PDF with ``soffice --headless`` and rasterising the
target region with PyMuPDF.

LibreOffice is optional: when it is not installed, chart transcription is
skipped with a warning and everything else keeps working.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf as fitz

SOFFICE_PATH = os.environ.get("SOFFICE_PATH", "soffice")


def soffice_available() -> bool:
    if os.path.isabs(SOFFICE_PATH):
        return Path(SOFFICE_PATH).exists()
    return shutil.which(SOFFICE_PATH) is not None


def _soffice_to_pdf(pptx_path: Path, out_dir: Path) -> Path:
    subprocess.run(
        [
            SOFFICE_PATH,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(pptx_path),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return out_dir / f"{pptx_path.stem}.pdf"


def emu_rect_to_points(
    left, top, width, height, slide_w_emu, slide_h_emu, page_w_pt, page_h_pt
) -> fitz.Rect:
    """Map a shape's EMU bounding box to PDF points on the rendered page."""
    sx = page_w_pt / slide_w_emu
    sy = page_h_pt / slide_h_emu
    return fitz.Rect(left * sx, top * sy, (left + width) * sx, (top + height) * sy)


class PPTXRenderer:
    """Convert a deck to PDF once, then clip out arbitrary regions as PNGs."""

    def __init__(self, pptx_path: Path, work_dir: Path | None = None) -> None:
        self._tmp: tempfile.TemporaryDirectory | None = None
        if work_dir is not None:
            self._out = work_dir
        else:
            self._tmp = tempfile.TemporaryDirectory(prefix="ptm-render-")
            self._out = Path(self._tmp.name)
        self.pdf_path = _soffice_to_pdf(Path(pptx_path), self._out)
        self.doc = fitz.open(self.pdf_path)

    def page_count(self) -> int:
        return self.doc.page_count

    def page_rect(self, page_index: int) -> fitz.Rect:
        return self.doc[page_index].rect

    def render_rect(
        self, page_index: int, rect: fitz.Rect, matrix: fitz.Matrix = fitz.Matrix(2, 2)
    ) -> bytes:
        """Render ``rect`` (PDF points) on ``page_index`` to PNG bytes."""
        page = self.doc[page_index]
        clipped = fitz.Rect(rect) & page.rect
        if clipped.is_empty:
            return b""
        return page.get_pixmap(matrix=matrix, clip=clipped).tobytes("png")

    def close(self) -> None:
        self.doc.close()
        if self._tmp is not None:
            self._tmp.cleanup()
