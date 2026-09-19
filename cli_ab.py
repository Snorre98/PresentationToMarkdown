"""``ptm-ab`` — A/B compare the pre-flight need-check gate (ADR-0039).

Runs one input through the conversion pipeline under two (or three) gate modes
and reports the difference:

- ``compare`` runs the input twice — ``NEED_GATE=off`` (today's behaviour) and
  ``NEED_GATE=on`` (the gate applied) — writing side-by-side outputs, then diffs
  the Markdown and reports wall-clock per arm.
- ``shadow`` runs once with ``NEED_GATE=shadow``: output and compute are
  identical to ``off``, but the gate also logs its verdicts, which are joined to
  the expensive-pass outcomes to produce a true-skip / false-skip table.

The gate only matters when a gated pass (``--format`` or ``--structure``) is
enabled, so pass those flags (or ``--all`` / ``--paper``) as usual.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sqlite3
import sys
import time
from pathlib import Path

from cli_common import add_ai_flags, apply_ai_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptm-ab",
        description="A/B test the pre-flight need-check gate (ADR-0039).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("compare", "run off vs on and diff the Markdown"),
        ("shadow", "run once in shadow mode and report skip accuracy"),
    ):
        p = sub.add_parser(name, help=help_text)
        add_ai_flags(p)
        p.add_argument("path", help="input .pptx/.pdf/.tex file")
        p.add_argument(
            "-o",
            "--output",
            metavar="DIR",
            default="ab-out",
            help="output directory (default: ab-out)",
        )
        p.add_argument(
            "--duplicate",
            action="store_true",
            help="never overwrite an existing .md (write <stem> (N).md)",
        )
    return parser


def _db_path() -> str:
    return os.environ.get("VISION_LOG_DB", "ptm.sqlite")


def _convert(path: str, out_dir: Path, duplicate: bool) -> tuple[object, float]:
    from converter import convert_file

    t0 = time.perf_counter()
    result = convert_file(str(path), out_dir, duplicate_if_exists=duplicate)
    return result, time.perf_counter() - t0


def _diff_markdown(a: Path, b: Path) -> None:
    ta = a.read_text(encoding="utf-8").splitlines()
    tb = b.read_text(encoding="utf-8").splitlines()
    if ta == tb:
        print("Markdown outputs are byte-identical.")
        return
    print("Markdown outputs differ:")
    for line in difflib.unified_diff(ta, tb, fromfile=str(a), tofile=str(b), lineterm=""):
        print(line)


def _compare(args: argparse.Namespace) -> int:
    path = args.path
    out = Path(args.output)
    arms: dict[str, tuple[object, float]] = {}
    for mode in ("off", "on"):
        os.environ["NEED_GATE"] = mode
        d = out / mode
        d.mkdir(parents=True, exist_ok=True)
        arms[mode] = _convert(path, d, args.duplicate)

    for mode in ("off", "on"):
        result, elapsed = arms[mode]
        status = "OK" if result.error is None else f"ERR {result.error}"
        print(f"{mode:>4}: {elapsed:6.1f}s  {status}")

    off_res, _ = arms["off"]
    on_res, _ = arms["on"]
    if off_res.md_path and on_res.md_path:
        _diff_markdown(off_res.md_path, on_res.md_path)
    elif off_res.error or on_res.error:
        return 1
    return 0


def _shadow(args: argparse.Namespace) -> int:
    os.environ["NEED_GATE"] = "shadow"
    out = Path(args.output) / "shadow"
    out.mkdir(parents=True, exist_ok=True)
    result, elapsed = _convert(args.path, out, args.duplicate)
    status = "OK" if result.error is None else f"ERR {result.error}"
    print(f"shadow: {elapsed:6.1f}s  {status}")
    if result.error is not None:
        return 1
    _shadow_report(str(args.path))
    return 0


def _shadow_report(source: str) -> None:
    conn = sqlite3.connect(_db_path())
    try:
        run = conn.execute(
            "SELECT id, duration_ms FROM conversion_runs WHERE source = ? "
            "ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        if run is None:
            print(f"no run recorded for {source}")
            return
        run_id = run[0]
        rows = conn.execute(
            "SELECT page, stage, decision FROM vision_events "
            "WHERE run_id = ? AND page IS NOT NULL AND stage IN "
            "('need-format', 'need-structure', 'format', 'structure') "
            "ORDER BY page, stage",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    pages: dict[int, dict[str, str]] = {}
    for page, stage, decision in rows:
        pages.setdefault(page, {})[stage] = decision

    print(f"shadow run {run_id} ({run[1]} ms)")
    print(f"{'page':>4}  {'pass':<9}  {'gate':<9}  outcome")
    false_skips = 0
    true_skips = 0
    for page in sorted(pages):
        for label, gate_stage, out_stage in (
            ("format", "need-format", "format"),
            ("structure", "need-structure", "structure"),
        ):
            gate = pages[page].get(gate_stage)
            outcome = pages[page].get(out_stage)
            if gate is None and outcome is None:
                continue
            print(f"{page:>4}  {label:<9}  {gate or '-':<9}  {outcome or '-'}")
            if gate == "clean" and outcome == "amended":
                false_skips += 1
            elif gate == "clean" and outcome in ("unchanged", "rejected"):
                true_skips += 1
    print(f"\nfalse skips (clean but would have amended): {false_skips}")
    print(f"true skips (clean and unchanged/rejected):  {true_skips}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply_ai_env(args)
    if args.command == "compare":
        return _compare(args)
    return _shadow(args)


if __name__ == "__main__":
    raise SystemExit(main())
