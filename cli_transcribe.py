"""``ptm-transcribe`` — standalone audio-to-Markdown transcription.

Operates directly on Markdown and/or audio (never PDF/PPTX). Two ways to use it:

- **With existing Markdown** — ``ptm-transcribe deck.md`` discovers a same-stem
  audio file beside it and attaches a ``# Transcript`` section.
- **Without Markdown** — ``ptm-transcribe week-2.mp3`` writes a standalone
  ``week-2.transcript.md`` (plus ``.clean.flac`` / ``.transcript.srt`` sidecars).

Pairing is by convention (same stem, same folder) or explicit (``--audio-file``,
``--to MARKDOWN.md``); when neither settles it and there are candidate lectures,
an interactive prompt lets you pick one. Env vars are set *before* importing
``converter`` (which reads them at import time), mirroring ``cli_common``.
"""
from __future__ import annotations

import argparse
import atexit
import os
import signal
import sys
from pathlib import Path
from typing import Callable

MD_SUFFIX = ".md"

# Exit code when another ``ptm-transcribe`` already holds the lock.
EXIT_LOCKED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptm-transcribe",
        description="Transcribe lecture audio to Markdown (attach to a .md, or "
        "write a standalone <stem>.transcript.md).",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="input .md files, audio files, and/or folders to scan recursively",
    )
    parser.add_argument(
        "--audio-file",
        metavar="PATH",
        help="audio file to transcribe (paired by stem to a .md, or the sole target)",
    )
    parser.add_argument(
        "--to",
        metavar="MARKDOWN.md",
        help="attach the transcript to this Markdown file instead of discovering one",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="enable speaker diarization via the audio server",
    )
    parser.add_argument(
        "--isolate",
        action="store_true",
        help="attempt to isolate the dominant voice (SepFormer via the audio server)",
    )
    parser.add_argument(
        "--language",
        metavar="LANG",
        help="Whisper language hint (e.g. 'no', 'en')",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace the base <stem>.transcript.md instead of writing a numbered copy",
    )
    parser.add_argument(
        "--env",
        action="append",
        metavar="KEY=VALUE",
        help="set an arbitrary environment variable (repeatable)",
    )
    return parser


def _apply_env(args: argparse.Namespace) -> dict[str, str]:
    """Set the audio env vars and return the applied mapping.

    Must be called before importing ``converter``. ``--env`` entries are applied
    last so they can override the defaults.
    """
    env: dict[str, str] = {"AUDIO_ENABLED": "1"}
    if getattr(args, "diarize", False):
        env["AUDIO_DIARIZE_ENABLED"] = "1"
    if getattr(args, "isolate", False):
        env["AUDIO_ISOLATE_ENABLED"] = "1"
    if getattr(args, "language", None):
        env["AUDIO_LANGUAGE"] = args.language
    for item in getattr(args, "env", None) or []:
        if "=" not in item:
            raise SystemExit(f"invalid --env value {item!r} (expected KEY=VALUE)")
        key, value = item.split("=", 1)
        env[key.strip()] = value
    os.environ.update(env)
    return env


def _resolved(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def collect_targets(paths: list[str]) -> tuple[list[Path], list[Path]]:
    """Expand files/folders into de-duplicated ``(md_files, audio_files)`` lists.

    Folders are scanned recursively for ``.md`` and known audio files.
    """
    from converter.transcribe import AUDIO_EXTENSIONS

    md_files: list[Path] = []
    audio_files: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = _resolved(path)
        if key in seen:
            return
        seen.add(key)
        suffix = path.suffix.lower()
        if suffix == MD_SUFFIX:
            if ".transcript" in path.stem:
                return  # skip our own transcript outputs (they have no audio)
            md_files.append(path)
        elif suffix in AUDIO_EXTENSIONS:
            audio_files.append(path)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for cand in sorted(path.rglob("*")):
                if cand.is_file():
                    add(cand)
        else:
            add(path)
    return md_files, audio_files


def _pick_lecture(audio: Path, candidates: list[Path]) -> Path | None:
    """Prompt (interactively) which lecture ``audio`` belongs to.

    Returns the chosen Markdown path, or ``None`` for a standalone transcript.
    Degrades to ``None`` when stdin is not a TTY (script-friendly) or the user
    aborts.
    """
    if not candidates or not sys.stdin.isatty():
        return None
    print(f"Which lecture does {audio.name} belong to?")
    for i, cand in enumerate(candidates, start=1):
        print(f"  [{i}] {cand}")
    print("  [0] none — write a standalone transcript")
    while True:
        try:
            choice = input("> ").strip()
        except EOFError:
            return None
        if choice in ("", "0"):
            return None
        try:
            idx = int(choice)
        except ValueError:
            print("  enter a number", file=sys.stderr)
            continue
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]
        print("  out of range", file=sys.stderr)


def _report(warnings: list[str], name: str) -> None:
    for warning in warnings:
        print(f"[WARN] {name}: {warning}")


def _make_progress_printer() -> Callable[[str], None]:
    """Return an ``on_line`` printer that streams progress to stderr.

    On a TTY, every line (including mlx-whisper/ffmpeg carriage-return progress
    bars) is forwarded verbatim. When stderr is piped, carriage-return bars are
    suppressed so only start / heartbeat / phase / result lines appear.
    """
    tty = sys.stderr.isatty()

    def printer(line: str) -> None:
        if not tty and "\r" in line:
            return
        text = line if line.endswith("\n") else line + "\n"
        sys.stderr.write(text)
        sys.stderr.flush()

    return printer


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_env(args)

    import lock

    lock_handle = lock.acquire_transcribe_lock()
    if not lock_handle.held:
        pid = lock_handle.pid
        if pid is not None:
            print(
                f"ptm-transcribe: another instance is already running (PID {pid})",
                file=sys.stderr,
            )
        else:
            print("ptm-transcribe: another instance is already running", file=sys.stderr)
        return EXIT_LOCKED
    atexit.register(lock_handle.release)

    from converter.transcribe import terminate_active_child

    printer = _make_progress_printer()

    def _handle(signum, _frame):
        terminate_active_child()
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(143)

    prev_int = signal.signal(signal.SIGINT, _handle)
    prev_term = signal.signal(signal.SIGTERM, _handle)

    try:
        return _run_targets(args, printer)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        lock_handle.release()


def _run_targets(args: argparse.Namespace, printer: Callable[[str], None]) -> int:
    from converter.transcribe import attach_transcript, find_audio_for, transcribe_to_markdown

    raw_targets = list(args.targets)
    if args.audio_file:
        raw_targets.append(args.audio_file)
    md_files, audio_files = collect_targets(raw_targets)

    if not md_files and not audio_files:
        print("ptm-transcribe: no .md or audio files found.", file=sys.stderr)
        return 2

    md_by_stem: dict[str, Path] = {}
    for md in md_files:
        md_by_stem.setdefault(md.stem, md)

    consumed: set[str] = set()
    ok = 0
    total = 0

    for md in md_files:
        total += 1
        audio: Path | None = None
        if args.audio_file:
            explicit = Path(args.audio_file)
            if explicit.stem == md.stem or len(md_files) == 1:
                audio = explicit
        if audio is None:
            audio = find_audio_for(md)
        if audio is None:
            print(f"[WARN] {md.name}: no audio found (pass an audio file or --to)")
            continue
        print(f"attaching transcript to {md.name} …", file=sys.stderr)
        warnings: list[str] = []
        segments = attach_transcript(md, warnings, audio_path=audio, on_line=printer)
        _report(warnings, md.name)
        if segments is None:
            if not warnings:
                print(f"[WARN] {md.name}: no transcript produced")
            continue
        consumed.add(_resolved(audio))
        ok += 1
        print(f"[OK]  {md.name} <- {audio.name}")

    for audio in audio_files:
        if _resolved(audio) in consumed:
            continue
        total += 1
        warnings: list[str] = []
        md: Path | None = None
        if args.to:
            md = Path(args.to)
        elif audio.stem in md_by_stem:
            md = md_by_stem[audio.stem]
        else:
            md = _pick_lecture(audio, md_files)

        if md is not None:
            if not md.exists():
                print(f"[WARN] {audio.name}: target Markdown does not exist: {md}")
                continue
            print(f"attaching transcript to {md.name} …", file=sys.stderr)
            segments = attach_transcript(md, warnings, audio_path=audio, on_line=printer)
            _report(warnings, md.name)
            if segments is None:
                if not warnings:
                    print(f"[WARN] {md.name}: no transcript produced")
                continue
            ok += 1
            print(f"[OK]  {md.name} <- {audio.name}")
        else:
            print(f"transcribing {audio.name} …", file=sys.stderr)
            out = transcribe_to_markdown(
                audio, warnings, on_line=printer, overwrite=args.overwrite
            )
            _report(warnings, audio.name)
            if out is None:
                if not warnings:
                    print(f"[WARN] {audio.name}: no transcript produced")
                continue
            ok += 1
            print(f"[OK]  {audio.name} -> {out}")

    print(f"Done: {ok} of {total} transcribed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
