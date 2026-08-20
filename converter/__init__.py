"""Presentation/PDF -> Markdown conversion library.

Public API::

    from converter import convert_file, convert_files, ConvertResult

    results = convert_files(["deck.pptx", "handout.pdf"], "out/")
"""
from __future__ import annotations

from pathlib import Path

from converter.base import (
    ConvertResult,
    Converter,
    ConverterRegistry,
    ProgressCallback,
    registry,
)
from converter.pptx import PPTXConverter
from converter.pdf import PDFConverter

registry.register(PPTXConverter)
registry.register(PDFConverter)

SUPPORTED_EXTENSIONS = registry.supported_extensions

__all__ = [
    "ConvertResult",
    "Converter",
    "ConverterRegistry",
    "ProgressCallback",
    "SUPPORTED_EXTENSIONS",
    "convert_file",
    "convert_files",
]


def convert_file(
    path: str | Path,
    output_dir: str | Path,
) -> ConvertResult:
    """Convert one supported file to a .md file plus an assets folder."""
    path = Path(path)
    output_dir = Path(output_dir)
    converter = registry.get(path)
    if converter is None:
        return ConvertResult(
            source_path=path,
            error=f"Unsupported file type: {path.suffix or '(none)'}",
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    return converter.convert(path, output_dir)


def convert_files(
    paths: list[str | Path],
    output_dir: str | Path,
    progress_callback: ProgressCallback | None = None,
) -> list[ConvertResult]:
    """Convert multiple files; errors are captured per file."""
    results: list[ConvertResult] = []
    total = len(paths)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for idx, path in enumerate(paths, start=1):
        result = convert_file(path, output_dir)
        results.append(result)
        if progress_callback:
            progress_callback(idx, total, Path(path).name)
    return results
