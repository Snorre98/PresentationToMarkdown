"""Best-effort PDF compression for archived sources (ADR-0049).

When a PDF is moved into its folder's ``source/`` archive, this module shrinks
the archived copy in place:

- **Ghostscript** (``gs``) rewrites the PDF with ``-dPDFSETTINGS=/printer``
  (moderate, 300 dpi image downsampling) — the default.
- **qpdf** is the lossless fallback (stream recompression) when ``gs`` is not
  installed. Both are optional external binaries; when neither exists the move
  still succeeds and a warning is recorded.

The compressed candidate replaces the original only when it passes a sanity
check (PyMuPDF can open it, the page count is unchanged) and is strictly
smaller. Any failure — missing binary, non-zero exit, invalid output, no size
gain — keeps the original bytes untouched and appends a warning.

The on/off toggle is the lazy ``PDF_COMPRESS`` preference
(:func:`converter.config.pdf_compress_enabled`), read at archive time like
``IMAGE_LINKS`` so the engine can flip it per conversion. ``GS_PATH``,
``QPDF_PATH`` and ``PDF_COMPRESS_PRESET`` override the binaries/preset.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

GS_PATH = os.environ.get("GS_PATH", "gs")
QPDF_PATH = os.environ.get("QPDF_PATH", "qpdf")

_TIMEOUT = 600.0
_DEFAULT_PRESET = "/printer"
_PRESETS = {"/screen", "/ebook", "/printer", "/prepress", "/default"}


def _available(binary: str) -> bool:
    if os.path.isabs(binary):
        return Path(binary).exists()
    return shutil.which(binary) is not None


def gs_available() -> bool:
    """Whether the configured Ghostscript binary can be run."""
    return _available(GS_PATH)


def qpdf_available() -> bool:
    """Whether the configured qpdf binary can be run."""
    return _available(QPDF_PATH)


def _preset() -> str:
    value = os.environ.get("PDF_COMPRESS_PRESET", "").strip()
    return value if value in _PRESETS else _DEFAULT_PRESET


def _sane(compressed: Path, original: Path) -> bool:
    """Whether ``compressed`` is a readable PDF with the original's page count."""
    try:
        import pymupdf as fitz

        with fitz.open(original) as before, fitz.open(compressed) as after:
            return after.page_count == before.page_count
    except Exception:
        return False


def compress_pdf(path: Path, warnings: list[str]) -> bool:
    """Compress ``path`` in place; never raises. Returns True when replaced.

    Appends a human-readable warning when compression was skipped or failed.
    """
    try:
        original_size = path.stat().st_size
    except OSError as exc:
        warnings.append(f"PDF compression skipped: {exc}")
        return False

    def command_for(out: Path) -> list[str]:
        if gs_available():
            return [
                GS_PATH,
                "-sDEVICE=pdfwrite",
                f"-dPDFSETTINGS={_preset()}",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                f"-sOutputFile={out}",
                str(path),
            ]
        return [
            QPDF_PATH,
            "--object-streams=generate",
            "--recompress-flate",
            "--compress-streams=y",
            str(path),
            str(out),
        ]

    if not gs_available() and not qpdf_available():
        warnings.append(
            "PDF compression skipped: neither gs nor qpdf is installed "
            "(brew install ghostscript qpdf)"
        )
        return False

    try:
        # Same directory as the target: the final replace is a cheap rename.
        with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
            out = Path(tmp) / path.name
            subprocess.run(
                command_for(out), check=True, capture_output=True, timeout=_TIMEOUT
            )
            if not out.exists() or not _sane(out, path):
                warnings.append(
                    f"PDF compression produced an invalid file; keeping {path.name}"
                )
                return False
            if out.stat().st_size >= original_size:
                return False
            os.replace(out, path)
            return True
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.append(f"PDF compression failed: {exc}")
        return False
