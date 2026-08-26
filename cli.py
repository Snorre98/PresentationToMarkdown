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
        "--audio-file",
        metavar="PATH",
        help="audio file to transcribe (with --audio), paired by stem to an "
        "input file; default: discover a same-stem audio file beside the source",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress per-file progress lines",
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

    audio_paths: dict[str, str] = {}
    if args.audio_file:
        audio = Path(args.audio_file)
        matches = [f for f in files if f.stem == audio.stem]
        target = matches[0] if matches else (files[0] if len(files) == 1 else None)
        if target is None:
            print(
                f"ptm: cannot pair --audio-file {audio.name} with an input file "
                "(use a matching stem, or a single input).",
                file=sys.stderr,
            )
            return 2
        audio_paths[str(target)] = str(audio)
        try:
            audio_paths[str(target.resolve())] = str(audio)
        except OSError:
            pass

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
        audio_paths=audio_paths or None,
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
    print(f"[{idx}/{total}] {name}")


if __name__ == "__main__":
    raise SystemExit(main())
