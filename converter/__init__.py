"""Presentation/PDF -> Markdown conversion library.

Public API::

    from converter import convert_file, convert_files, ConvertResult

    results = convert_files(["deck.pptx", "handout.pdf"], "out/")

``output_dir`` is optional; when omitted (or ``None``) each file is written to
``<source-folder>/markdown/``.
"""
from __future__ import annotations

from pathlib import Path

from converter import config
from converter.base import (
    ConvertResult,
    Converter,
    ConverterRegistry,
    PageProgressCallback,
    ProgressCallback,
    registry,
)
from converter.pptx import PPTXConverter
from converter.pdf import PDFConverter
from converter.format import polish_text
from converter.summary import prepend_summary
from converter.lifecycle import release_readers, release_writers
from converter.logstore import phase, run_finish, run_snapshot, run_start

registry.register(PPTXConverter)
registry.register(PDFConverter)

SUPPORTED_EXTENSIONS = registry.supported_extensions

__all__ = [
    "ConvertResult",
    "Converter",
    "ConverterRegistry",
    "PageProgressCallback",
    "ProgressCallback",
    "SUPPORTED_EXTENSIONS",
    "convert_file",
    "convert_files",
]


def _default_output_dir(path: Path) -> Path:
    return path.parent / "markdown"


def _next_free_stem(output_dir: Path, stem: str) -> str:
    """Return the next free Finder-style stem, e.g. ``deck (2)``, ``deck (3)``.

    Only *reads* existence — never touches existing files — so a duplicate run
    cannot clobber a prior conversion.
    """
    n = 2
    while (
        (output_dir / f"{stem} ({n}).md").exists()
        or (output_dir / "assets" / f"{stem} ({n})").exists()
    ):
        n += 1
    return f"{stem} ({n})"


def convert_file(
    path: str | Path,
    output_dir: str | Path | None = None,
    progress_callback: PageProgressCallback | None = None,
    duplicate_if_exists: bool = False,
) -> ConvertResult:
    """Convert one supported file to a .md file plus an assets folder.

    ``output_dir`` defaults to ``<source-folder>/markdown`` when omitted.
    Conversion is deterministic: it never invokes audio transcription
    (see ``ptm-transcribe`` / ``converter.transcribe`` for that, ADR-0009).

    ``progress_callback``, when given, is called once per slide/page as
    ``(page, page_total, name)``.

    ``duplicate_if_exists`` writes to the next free ``stem (N).md`` when the
    target ``<stem>.md`` already exists, leaving the prior output intact
    (ADR-0015).
    """
    path = Path(path)
    converter = registry.get(path)
    if converter is None:
        return ConvertResult(
            source_path=path,
            error=f"Unsupported file type: {path.suffix or '(none)'}",
        )
    run_id = run_start(str(path))
    run_snapshot(run_id, config.snapshot())
    status = "error"
    try:
        resolved = Path(output_dir) if output_dir else _default_output_dir(path)
        resolved.mkdir(parents=True, exist_ok=True)
        output_stem: str | None = None
        if duplicate_if_exists and (resolved / f"{path.stem}.md").exists():
            output_stem = _next_free_stem(resolved, path.stem)
        with phase(run_id, "convert", 1):
            result = converter.convert(
                path, resolved, progress_callback=progress_callback, output_stem=output_stem
            )
        if result.error is None and result.md_path is not None:
            try:
                original = result.md_path.read_text(encoding="utf-8")
                with phase(run_id, "format", 3):
                    polished = polish_text(original, warnings=result.warnings, source=str(path))
                rewritten = (polished + "\n") if polished else ""
                if rewritten != original:
                    result.md_path.write_text(rewritten, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - polish never fails the conversion
                result.warnings.append(f"Markdown polish failed: {exc}")
            release_readers()
            try:
                with phase(run_id, "summary", 4):
                    prepend_summary(result.md_path, path, result.warnings)
            except Exception as exc:  # noqa: BLE001 - summary never fails the conversion
                result.warnings.append(f"Summary generation failed: {exc}")
            release_writers()
        status = "ok" if result.error is None else "error"
        return result
    finally:
        run_finish(run_id, status)


def convert_files(
    paths: list[str | Path],
    output_dir: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
    page_progress_callback: PageProgressCallback | None = None,
    duplicate_if_exists: bool = False,
) -> list[ConvertResult]:
    """Convert multiple files; errors are captured per file.

    ``output_dir`` defaults to ``<source-folder>/markdown`` per file when
    omitted.

    ``progress_callback`` fires once per completed file as ``(idx, total,
    name)``; ``page_progress_callback`` fires once per slide/page as ``(page,
    page_total, name)``. ``duplicate_if_exists`` is forwarded to
    :func:`convert_file` per file (ADR-0015).
    """
    results: list[ConvertResult] = []
    total = len(paths)
    for idx, path in enumerate(paths, start=1):
        p = Path(path)
        result = convert_file(
            path,
            output_dir,
            progress_callback=page_progress_callback,
            duplicate_if_exists=duplicate_if_exists,
        )
        results.append(result)
        if progress_callback:
            progress_callback(idx, total, p.name)
    return results
