"""``ptm`` — headless batch converter with GUI-parity behavior.

Converts ``.pptx``/``.pdf`` files (or folders, scanned recursively) to Markdown,
mirroring the desktop GUI's semantics: per-file ``[OK]``/``[ERR]``/``[WARN]`` log
lines, an optional output folder (defaulting to ``<source>/markdown``), recent-file
recording, and the same AI-capability flags.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cli_common import add_ai_flags, apply_ai_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptm",
        description="Convert PowerPoint (.pptx) and PDF files to Markdown.",
    )
    add_ai_flags(parser)
    parser.add_argument(
        "paths",
        nargs="+",
        help="input .pptx/.pdf files and/or folders to scan recursively",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="DIR",
        help="write all output to DIR (default: <source>/markdown per file)",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="do not descend into subfolders when scanning a folder",
    )
    parser.add_argument(
        "--no-recent",
        action="store_true",
        help="do not record converted files in the recent-files list",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress per-file progress lines",
    )
    parser.add_argument(
        "--duplicate",
        action="store_true",
        help="write to <stem> (N).md instead of overwriting an existing .md",
    )
    return parser


def collect_files(paths: list[str], recursive: bool) -> tuple[list[Path], set[str]]:
    """Expand files/folders into a de-duplicated, order-preserving list of inputs.

    Returns ``(files, supported_extensions)``. Folders are scanned with
    ``rglob`` (or ``iterdir`` when ``recursive`` is false) for supported files,
    matching the GUI's ``add_paths``.
    """
    from converter import SUPPORTED_EXTENSIONS

    files: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            candidates = sorted(
                cand
                for cand in (path.rglob("*") if recursive else path.iterdir())
                if cand.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        else:
            candidates = [path]
        for cand in candidates:
            resolved = str(cand.resolve())
            if resolved not in seen:
                files.append(cand)
                seen.add(resolved)
    return files, SUPPORTED_EXTENSIONS


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply_ai_env(args)

    from converter import convert_files

    files, _ = collect_files(args.paths, recursive=not args.no_recursive)
    if not files:
        print("ptm: no supported .pptx/.pdf files found.", file=sys.stderr)
        return 2

    output_dir = Path(args.output) if args.output else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    if not args.quiet:
        if output_dir is None:
            print(f"Converting {len(files)} file(s) to <input-folder>/markdown ...")
        else:
            print(f"Converting {len(files)} file(s) to {output_dir} ...")

    results = convert_files(
        files,
        output_dir,
        progress_callback=None if args.quiet else _progress,
        page_progress_callback=None if args.quiet else _page_progress,
        duplicate_if_exists=args.duplicate,
    )

    if not args.no_recent:
        from converter.settings import record_recent

        for result in results:
            record_recent(str(result.source_path.resolve()))

    ok = 0
    for result in results:
        if result.error:
            print(f"[ERR] {result.source_path.name}: {result.error}")
        else:
            ok += 1
            print(f"[OK]  {result.source_path.name} -> {result.md_path}")
            for warning in result.warnings:
                print(f"[WARN] {result.source_path.name}: {warning}")

    print(f"Done: {ok} of {len(results)} converted.")
    return 0 if ok == len(results) else 1


def _progress(idx: int, total: int, name: str) -> None:
    if sys.stderr.isatty():
        print("", file=sys.stderr)
    print(f"[{idx}/{total}] {name}")


def _page_progress(page: int, total: int, name: str) -> None:
    """Report per-page progress as a carriage-return status line on a TTY only.

    On a pipe (scripts), the status line is suppressed so logs don't accumulate
    one line per page; the per-file ``_progress`` lines still appear.
    """
    if not sys.stderr.isatty():
        return
    noun = "Slide" if name.lower().endswith(".pptx") else "Page"
    print(f"\r{name}: {noun} {page}/{total}", end="", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
