#!/usr/bin/env python3
"""Diff PDF conversions between the working tree and a baseline revision.

The review harness for ADR-0055: it converts a corpus of PDFs in slide and
paper mode twice — once with the working tree, once with a baseline git
revision extracted read-only via ``git archive`` — and prints a unified diff
per file so every changed line can be explained by the change under review.

It is read-only with respect to the repository and the corpus: sources are
never moved or compressed (an explicit output directory disables the archive
step), ``git archive`` never writes to ``.git``, and run state goes to a
temporary directory.

Usage:
    scripts/pdf-corpus-diff.py --corpus /path/to/pdfs --corpus tests
    scripts/pdf-corpus-diff.py --corpus /path/to/pdfs --baseline HEAD~1 --limit 20
    scripts/pdf-corpus-diff.py --corpus /path/to/pdfs --copy-out artifacts/corpus-sweep

``--copy-out`` keeps a run: it copies the working-tree Markdown outputs and the
diff report into a durable folder (``artifacts/`` is gitignored) together with
a provenance README.
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_RUNNER = r"""
import json, os, pathlib, sys

mode, out_root, log_db, state_dir, expect_root, files = json.loads(sys.argv[1])
os.environ["PDF_MODE"] = mode
os.environ["VISION_LOG_DB"] = log_db
os.environ["PTM_STATE_DIR"] = state_dir
from converter import convert_file
import converter

root = pathlib.Path(converter.__file__).resolve()
if not root.is_relative_to(pathlib.Path(expect_root).resolve()):
    raise SystemExit(f"converter resolved to {root}, not under {expect_root}")

for index, src in enumerate(files):
    src = pathlib.Path(src)
    out = pathlib.Path(out_root) / f"{index:03d}-{src.stem}"
    out.mkdir(parents=True, exist_ok=True)
    result = convert_file(src, str(out))
    print(json.dumps({
        "src": str(src),
        "md": str(result.md_path) if result.md_path else None,
        "error": result.error,
        "warnings": result.warnings,
    }))
"""

# Lines the structural passes may rewrite without changing the document's
# words: heading markers, bullet markers, quote markers and table pipes.
_MARKER_RE = re.compile(r"^\s*(?:#{1,6}|[-*+]|>)\s+")


def _tokens(text: str) -> list[str]:
    """The document's words, with Markdown markers and hyphenation normalised.

    A word hyphenated at a line end splits into two tokens in the line-per-line
    baseline and one de-hyphenated token in the reflowed output, so tokens are
    merged across a trailing hyphen and hyphens are then removed from every
    token on both sides.
    """
    words: list[str] = []
    for line in text.splitlines():
        line = _MARKER_RE.sub("", line).replace("*", "").replace("|", " ")
        words.extend(line.split())
    merged: list[str] = []
    for word in words:
        if (
            merged
            and merged[-1].endswith("-")
            and not merged[-1].endswith("--")
            and word[:1].isalnum()
        ):
            merged[-1] = merged[-1][:-1] + word
        else:
            merged.append(word)
    return [word.replace("-", "") for word in merged]


def _find_pdfs(directory: Path, limit: int | None, include_archives: bool) -> list[Path]:
    found: list[Path] = []
    for path in sorted(directory.rglob("*.pdf")):
        rel = path.relative_to(directory)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if not include_archives and any(part == "source" for part in rel.parts[:-1]):
            continue  # archive folders, mirroring the converter's scans
        found.append(path)
        if limit is not None and len(found) >= limit:
            break
    return found


def _extract_revision(rev: str, target: Path) -> None:
    data = subprocess.run(
        ["git", "archive", "--format=tar", rev],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(target)  # noqa: S202 - trusted local revision


def _run_conversions(
    tree: Path, pdfs: list[Path], mode: str, work: Path
) -> list[dict]:
    label = f"{tree.name}-{mode}"
    out_root = work / "out" / label
    out_root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        [mode, str(out_root), str(work / "ptm.sqlite"), str(work / "state"),
         str(tree), [str(path) for path in pdfs]]
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tree)
    proc = subprocess.run(
        [sys.executable, "-c", _RUNNER, payload],
        cwd=str(tree),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"conversion runner failed for {tree} ({mode}):\n{proc.stderr}"
        )
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    if len(results) != len(pdfs):
        raise SystemExit(
            f"conversion runner returned {len(results)} results for {len(pdfs)} PDFs"
        )
    return results


def _read(md_path: str | None) -> str:
    if not md_path:
        return ""
    path = Path(md_path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _save_artifacts(
    destination: Path,
    work: Path,
    args,
    pdfs: list[Path],
    modes: list[str],
    changed: int,
    errors: int,
    report_text: str,
) -> None:
    """Copy the working-tree outputs and the report to a durable folder."""
    destination.mkdir(parents=True, exist_ok=True)
    for mode in modes:
        source = work / "out" / f"{REPO.name}-{mode}"
        target = destination / mode
        if target.exists():
            shutil.rmtree(target)
        if source.exists():
            shutil.copytree(source, target)
    (destination / "diff-report.txt").write_text(report_text + "\n", encoding="utf-8")
    counts = {
        mode: len(list((destination / mode).rglob("*.md"))) if (destination / mode).exists() else 0
        for mode in modes
    }
    corpora = ", ".join(str(Path(c).resolve()) for c in (args.corpus or ["tests"]))
    provenance = [
        "# PDF corpus sweep artifacts",
        "",
        f"- Baseline revision: `{args.baseline}` (the working tree was compared against it)",
        f"- Corpus: {corpora}",
        f"- Modes: {', '.join(modes)}",
        f"- PDFs converted: {len(pdfs)}",
        "- Converted Markdown: " + ", ".join(f"{mode} {n}" for mode, n in counts.items()),
        f"- Changed files vs baseline: {changed}; conversion errors: {errors}",
        "",
        "Layout: `paper/` and `slide/` hold the working-tree outputs, one folder "
        "per source PDF; `diff-report.txt` holds the per-file unified diff and the "
        "`CONTENT CHANGED` / `ORDER CHANGED` markers. The outputs are explicit-"
        "directory conversions: the corpus sources were not moved or archived.",
        "",
        "Generated by `scripts/pdf-corpus-diff.py` (the ADR-0055 review harness); "
        "re-run with `--copy-out <this folder>` to refresh.",
        "",
    ]
    (destination / "README.md").write_text("\n".join(provenance), encoding="utf-8")
    print(f"artifacts saved to {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", action="append", default=None, metavar="DIR",
        help="directory to scan recursively for PDFs (repeatable; default: tests/)",
    )
    parser.add_argument(
        "--baseline", default="HEAD",
        help="git revision to compare against (default: HEAD)",
    )
    parser.add_argument(
        "--mode", default="slide,paper",
        help="comma-separated PDF modes to verify (default: slide,paper)",
    )
    parser.add_argument("--limit", type=int, default=None, help="max PDFs per corpus dir")
    parser.add_argument(
        "--include-archives", action="store_true",
        help="also scan source/ archive folders (default: skip them like the converter)",
    )
    parser.add_argument(
        "--work", default=None, metavar="DIR",
        help="keep the converted outputs and diff report in this directory",
    )
    parser.add_argument(
        "--copy-out", default=None, metavar="DIR",
        help="copy the working-tree outputs and the diff report to DIR (e.g. a "
             "durable, gitignored artifacts folder) and write a provenance README",
    )
    args = parser.parse_args()

    corpora = [Path(item).resolve() for item in (args.corpus or ["tests"])]
    modes = [mode.strip() for mode in args.mode.split(",") if mode.strip()]
    pdfs: list[Path] = []
    for directory in corpora:
        found = _find_pdfs(directory, args.limit, args.include_archives)
        print(f"corpus {directory}: {len(found)} PDFs")
        pdfs.extend(found)
    if not pdfs:
        raise SystemExit("no PDFs found")
    if len(set(pdfs)) != len(pdfs):
        raise SystemExit("duplicate PDF paths across corpus directories")

    tmp = Path(args.work).resolve() if args.work else Path(tempfile.mkdtemp(prefix="ptm-corpus-"))
    tmp.mkdir(parents=True, exist_ok=True)
    baseline_tree = tmp / "baseline"
    if not baseline_tree.exists():
        _extract_revision(args.baseline, baseline_tree)
    print(f"baseline {args.baseline} extracted to {baseline_tree}")
    print(f"outputs under {tmp / 'out'}")

    changed = 0
    errors = 0
    report: list[str] = []
    for mode in modes:
        base_results = _run_conversions(baseline_tree, pdfs, mode, tmp)
        head_results = _run_conversions(REPO, pdfs, mode, tmp)
        for base, head in zip(base_results, head_results):
            name = f"{mode}: {base['src']}"
            if base["error"] or head["error"]:
                errors += 1
                report.append(f"\n===== {name} =====\nERRORS: {base['error']} / {head['error']}")
                continue
            old = _read(base["md"])
            new = _read(head["md"])
            if old == new:
                continue
            changed += 1
            report.append(f"\n===== {name} =====")
            old_tokens, new_tokens = _tokens(old), _tokens(new)
            if sorted(old_tokens) != sorted(new_tokens):
                report.append("CONTENT CHANGED (word multiset differs from baseline)")
            elif old_tokens != new_tokens:
                report.append("ORDER CHANGED (same words, different order)")
            report.extend(
                difflib.unified_diff(
                    old.splitlines(), new.splitlines(),
                    fromfile="baseline", tofile="working-tree", lineterm="",
                )
            )
    text = "\n".join(report) if report else "no output differences"
    print(text)
    if args.work:
        (tmp / "diff-report.txt").write_text(text + "\n", encoding="utf-8")
    print(
        f"\n{len(pdfs)} PDFs x {len(modes)} mode(s): "
        f"{changed} changed file(s), {errors} error(s)"
    )
    if args.copy_out:
        _save_artifacts(
            Path(args.copy_out).resolve(), tmp, args, pdfs, modes, changed, errors, text
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
