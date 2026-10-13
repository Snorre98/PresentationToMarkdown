"""Presentation/PDF -> Markdown conversion library.

Public API::

    from converter import convert_file, convert_files, ConvertResult

    results = convert_files(["deck.pptx", "handout.pdf"], "out/")

``output_dir`` is optional. When omitted (or ``None``), each file's Markdown is
written **beside the source** and the converted source is moved into a sibling
``source/`` archive folder under a kebab-case name derived from it (ADR-0048)::

    03 What makes things fun.pdf -> 03-what-makes-things-fun.md
                                 +  source/03-what-makes-things-fun.pdf

An explicit ``output_dir`` disables that organizing: the ``.md`` keeps its
original name and the source is left untouched.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from converter import compress, config
from converter.base import (
    ConvertResult,
    Converter,
    ConverterRegistry,
    PageProgressCallback,
    ProgressCallback,
    registry,
)
from converter.organize import (
    SOURCE_DIR_NAME,
    LayoutResolver,
    SourceResolver,
    archive_source,
    default_output_dir,
)
from converter.pptx import PPTXConverter
from converter.pdf import PDFConverter
from converter.latex import LatexConverter
from converter.format import polish_text
from converter.summary import prepend_summary
from converter.lifecycle import release_readers, release_writers
from converter.logstore import phase, run_finish, run_snapshot, run_start

registry.register(PPTXConverter)
registry.register(PDFConverter)
registry.register(LatexConverter)

SUPPORTED_EXTENSIONS = registry.supported_extensions

__all__ = [
    "ConvertResult",
    "Converter",
    "ConverterRegistry",
    "PageProgressCallback",
    "ProgressCallback",
    "SUPPORTED_EXTENSIONS",
    "collect_inputs",
    "convert_file",
    "convert_files",
    "default_output_dir",
]


def collect_inputs(paths: list[str | Path], recursive: bool = True) -> list[Path]:
    """Expand files/folders into concrete conversion inputs (ADR-0038).

    A directory that is a registered *project* (e.g. a LaTeX project) is
    yielded once, as the directory itself; other directories are scanned for
    supported files. ``source/`` archive folders (ADR-0048) are skipped during
    recursive scans — archived sources are not re-converted by a folder sweep;
    point at a file (or the ``source/`` folder) explicitly to convert one.
    Single supported files — including a lone ``.tex`` — are yielded as-is.
    Results are de-duplicated and order-preserving.
    """
    files: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        resolved = str(p.resolve())
        if resolved not in seen:
            files.append(p)
            seen.add(resolved)

    def scan_dir(directory: Path) -> None:
        if registry.get_project(directory) is not None:
            add(directory)
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for child in entries:
            if child.is_dir():
                if recursive and child.name.lower() != SOURCE_DIR_NAME:
                    scan_dir(child)
            elif child.suffix.lower() in SUPPORTED_EXTENSIONS:
                add(child)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            scan_dir(path)
        elif path.suffix.lower() in SUPPORTED_EXTENSIONS:
            add(path)
    return files


# A per-file output resolver: given the source path, return its output
# directory (or ``None`` to fall back to the default layout). Internal to the
# library; ``output_dir`` accepts ``str | Path | None`` plus this callable
# (ADR-0028).
OutputDirResolver = Callable[[Path], "Path | None"]


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
    output_dir: str | Path | OutputDirResolver | None = None,
    progress_callback: PageProgressCallback | None = None,
    duplicate_if_exists: bool = False,
    organize: bool | None = None,
    source_resolver: SourceResolver | None = None,
) -> ConvertResult:
    """Convert one supported file to a .md file (plus an assets folder only when
    ``IMAGE_LINKS`` is on; ADR-0047).

    ``output_dir`` defaults to the source's own folder; the output stem is then
    the source name normalized to kebab-case, and after a successful conversion
    the source is moved into a sibling ``source/`` folder (ADR-0048), where a
    PDF is best-effort compressed (ADR-0049). ``output_dir`` may also be a
    per-file resolver ``Callable[[Path], Path | None]`` returning the output
    directory for ``path`` (or ``None`` to use the default) — see ADR-0028.
    Files already inside a ``source/`` folder are not moved again, and an
    explicit ``output_dir`` (a path) disables organizing entirely.

    ``organize`` overrides the automatic behaviour: ``True``/``False`` forces
    the new layout on/off; ``None`` (default) organizes unless an explicit
    output folder was given.

    ``source_resolver``, when given, maps each conversion input to the on-disk
    file it represents (e.g. an upload's original); ``None`` disables
    organizing for that file. Used by the engine (ADR-0028).

    Conversion is deterministic: it never invokes audio transcription
    (see ``ptm-transcribe`` / ``converter.transcribe`` for that, ADR-0009).

    ``progress_callback``, when given, is called once per slide/page as
    ``(page, page_total, name)``.

    ``duplicate_if_exists`` writes to the next free ``stem (N).md`` when the
    target ``<stem>.md`` already exists, leaving the prior output intact
    (ADR-0015).
    """
    path = Path(path)
    resolver = LayoutResolver(
        output_dir, organize=organize, source_resolver=source_resolver
    )
    return _convert_one(path, resolver, progress_callback, duplicate_if_exists)


def _convert_one(
    path: Path,
    resolver: LayoutResolver,
    progress_callback: PageProgressCallback | None,
    duplicate_if_exists: bool,
) -> ConvertResult:
    """Convert one input using ``resolver``'s per-batch layout decisions."""
    converter = registry.get(path)
    if converter is None and path.is_dir():
        converter = registry.get_project(path)
    if converter is None:
        return ConvertResult(
            source_path=path,
            error=f"Unsupported file type: {path.suffix or '(none)'}",
        )
    layout = resolver.resolve(path)
    run_id = run_start(str(path))
    run_snapshot(run_id, config.snapshot())
    status = "error"
    try:
        layout.output_dir.mkdir(parents=True, exist_ok=True)
        output_stem: str | None = layout.stem
        if duplicate_if_exists and (layout.output_dir / f"{layout.stem}.md").exists():
            output_stem = _next_free_stem(layout.output_dir, layout.stem)
        with phase(run_id, "convert", 1):
            result = converter.convert(
                path,
                layout.output_dir,
                progress_callback=progress_callback,
                output_stem=output_stem,
            )
        if layout.warnings:
            result.warnings = layout.warnings + result.warnings
        if result.error is None and result.md_path is not None:
            try:
                original = result.md_path.read_text(encoding="utf-8")
                with phase(run_id, "format", 3):
                    polished = polish_text(
                        original,
                        warnings=result.warnings,
                        source=str(path),
                        allow_llm="format" in converter.ai_passes,
                    )
                rewritten = (polished + "\n") if polished else ""
                if rewritten != original:
                    result.md_path.write_text(rewritten, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - polish never fails the conversion
                result.warnings.append(f"Markdown polish failed: {exc}")
            release_readers()
            if "summary" in converter.ai_passes:
                try:
                    with phase(run_id, "summary", 4):
                        prepend_summary(result.md_path, path, result.warnings)
                except Exception as exc:  # noqa: BLE001 - summary never fails the conversion
                    result.warnings.append(f"Summary generation failed: {exc}")
            release_writers()
            if layout.source_dest is not None:
                moved = archive_source(path, layout.source_dest, result.warnings)
                if moved is not None:
                    result.moved_to = moved
                    if (
                        moved.suffix.lower() == ".pdf"
                        and config.pdf_compress_enabled()
                    ):
                        compress.compress_pdf(moved, result.warnings)
        status = "ok" if result.error is None else "error"
        return result
    finally:
        run_finish(run_id, status)


def convert_files(
    paths: list[str | Path],
    output_dir: str | Path | OutputDirResolver | None = None,
    progress_callback: ProgressCallback | None = None,
    page_progress_callback: PageProgressCallback | None = None,
    duplicate_if_exists: bool = False,
    organize: bool | None = None,
    source_resolver: SourceResolver | None = None,
) -> list[ConvertResult]:
    """Convert multiple files; errors are captured per file.

    ``output_dir`` defaults to the source's own folder per file when omitted;
    it may also be a per-file resolver ``Callable[[Path], Path | None]``
    (ADR-0028). With no explicit output folder the new default layout
    (ADR-0048) applies: kebab-case Markdown beside the source, sources archived
    into a sibling ``source/`` folder, PDFs compressed best-effort (ADR-0049);
    an explicit ``output_dir`` path disables all of that. ``organize`` and
    ``source_resolver`` override/parameterize that behaviour (see
    :func:`convert_file`).

    One :class:`~converter.organize.LayoutResolver` instance is shared across
    the batch, so two different sources whose names normalize identically get a
    ``-2`` suffix instead of silently overwriting each other.

    ``progress_callback`` fires once per completed file as ``(idx, total,
    name)``; ``page_progress_callback`` fires once per slide/page as ``(page,
    page_total, name)``. ``duplicate_if_exists`` is forwarded to
    :func:`convert_file` per file (ADR-0015).
    """
    resolver = LayoutResolver(
        output_dir, organize=organize, source_resolver=source_resolver
    )
    results: list[ConvertResult] = []
    total = len(paths)
    for idx, path in enumerate(paths, start=1):
        p = Path(path)
        result = _convert_one(
            p, resolver, page_progress_callback, duplicate_if_exists
        )
        results.append(result)
        if progress_callback:
            progress_callback(idx, total, p.name)
    return results
