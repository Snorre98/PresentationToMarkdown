"""Shared interface and markdown helpers for all file-format converters.

Each concrete converter lives in its own module (``pptx.py``, ``pdf.py``) and
subclasses :class:`Converter`. Dispatch by file extension happens through
:class:`ConverterRegistry`.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ProgressCallback = Callable[[int, int, str], None]

_MD_SPECIALS = str.maketrans({"\\": "\\\\", "`": "\\`", "*": "\\*", "_": "\\_"})


@dataclass
class ConvertResult:
    source_path: Path
    md_path: Path | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class Converter(ABC):
    """Convert one file format to a Markdown file plus an assets folder."""

    extensions: tuple[str, ...] = ()

    @abstractmethod
    def convert(self, path: Path, output_dir: Path) -> ConvertResult:
        """Convert ``path``, writing ``<stem>.md`` into ``output_dir``."""


class ConverterRegistry:
    """Maps file extensions to :class:`Converter` implementations."""

    def __init__(self) -> None:
        self._converters: dict[str, type[Converter]] = {}

    def register(self, converter: type[Converter]) -> type[Converter]:
        for ext in converter.extensions:
            self._converters[ext.lower()] = converter
        return converter

    def get(self, path: Path) -> Converter | None:
        converter_cls = self._converters.get(path.suffix.lower())
        if converter_cls is None:
            return None
        return converter_cls()

    @property
    def supported_extensions(self) -> set[str]:
        return set(self._converters)


registry = ConverterRegistry()


def _escape(text: str) -> str:
    text = text.translate(_MD_SPECIALS)
    if text.startswith("#"):
        text = "\\" + text
    return text


def _format_md(text: str, bold: bool = False, italic: bool = False) -> str:
    """Escape and wrap a text span with bold/italic Markdown markers."""
    text = _escape(text)
    if not text:
        return ""
    if bold and italic:
        return f"***{text}***"
    if bold:
        return f"**{text}**"
    if italic:
        return f"*{text}*"
    return text


def _table_to_md(rows: list[list[str]]) -> list[str]:
    """Render a list of rows (list of cells) as a Markdown pipe table."""
    lines: list[str] = []
    if not rows:
        return lines

    def cell_text(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", "<br>").strip()

    def render_row(row: list[str]) -> str:
        return "| " + " | ".join(cell_text(c) for c in row) + " |"

    header = render_row(rows[0])
    separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
    lines.extend([header, separator])
    for row in rows[1:]:
        lines.append(render_row(row))
    return lines


def write_image(
    blob: bytes,
    ext: str,
    assets_dir: Path,
    stem: str,
    counter: list[int],
    warnings: list[str],
    dedup: dict[str, str] | None = None,
) -> str | None:
    """Write an image blob to ``assets/<stem>/`` and return its filename.

    ``dedup`` is an optional ``{digest: filename}`` map; when provided, identical
    content is written only once and the existing filename is returned.
    """
    try:
        digest = hashlib.md5(blob).hexdigest()[:8]
        if dedup is not None and digest in dedup:
            return dedup[digest]
        ext = (ext or "bin").lstrip(".")
        filename = f"{stem}_{counter[0]:02d}_{digest}.{ext}"
        counter[0] += 1
        (assets_dir / filename).write_bytes(blob)
        if dedup is not None:
            dedup[digest] = filename
        return filename
    except Exception as exc:
        warnings.append(f"Could not write image: {exc}")
        return None
