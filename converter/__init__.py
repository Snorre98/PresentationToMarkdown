"""Presentation/PDF -> Markdown conversion library.

Public API::

    from converter import convert_file, convert_files, ConvertResult

    results = convert_files(["deck.pptx", "handout.pdf"], "out/")

``output_dir`` is optional; when omitted (or ``None``) each file is written to
``<source-folder>/markdown/``.
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
from converter.format import polish_text
from converter.summary import prepend_summary
from converter.transcribe import attach_transcript

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


def _default_output_dir(path: Path) -> Path:
    return path.parent / "markdown"


def convert_file(
    path: str | Path,
    output_dir: str | Path | None = None,
    audio_path: str | Path | None = None,
) -> ConvertResult:
    """Convert one supported file to a .md file plus an assets folder.

    ``output_dir`` defaults to ``<source-folder>/markdown`` when omitted.
    ``audio_path`` optionally names an audio file to transcribe (ADR-0004);
    when omitted, a same-stem file is discovered by convention.
    """
    path = Path(path)
    converter = registry.get(path)
    if converter is None:
        return ConvertResult(
            source_path=path,
            error=f"Unsupported file type: {path.suffix or '(none)'}",
        )
    resolved = Path(output_dir) if output_dir else _default_output_dir(path)
    resolved.mkdir(parents=True, exist_ok=True)
    result = converter.convert(path, resolved)
    if result.error is None and result.md_path is not None:
        try:
            original = result.md_path.read_text(encoding="utf-8")
            polished = polish_text(original, warnings=result.warnings)
            rewritten = (polished + "\n") if polished else ""
            if rewritten != original:
                result.md_path.write_text(rewritten, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - polish never fails the conversion
            result.warnings.append(f"Markdown polish failed: {exc}")
        try:
            prepend_summary(result.md_path, path, result.warnings)
        except Exception as exc:  # noqa: BLE001 - summary never fails the conversion
            result.warnings.append(f"Summary generation failed: {exc}")
        try:
            attach_transcript(result.md_path, path, result.warnings, audio_path)
        except Exception as exc:  # noqa: BLE001 - transcription never fails the conversion
            result.warnings.append(f"Audio transcription failed: {exc}")
    return result


def convert_files(
    paths: list[str | Path],
    output_dir: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
    audio_paths: dict[str, str | Path] | None = None,
) -> list[ConvertResult]:
    """Convert multiple files; errors are captured per file.

    ``output_dir`` defaults to ``<source-folder>/markdown`` per file when
    omitted. ``audio_paths`` optionally maps a source path (as given or resolved)
    to its audio file for the transcription pass.
    """
    results: list[ConvertResult] = []
    total = len(paths)
    for idx, path in enumerate(paths, start=1):
        p = Path(path)
        audio = None
        if audio_paths:
            audio = audio_paths.get(str(path)) or audio_paths.get(str(p))
            if audio is None:
                try:
                    audio = audio_paths.get(str(p.resolve()))
                except OSError:
                    audio = None
        result = convert_file(path, output_dir, audio_path=audio)
        results.append(result)
        if progress_callback:
            progress_callback(idx, total, p.name)
    return results
