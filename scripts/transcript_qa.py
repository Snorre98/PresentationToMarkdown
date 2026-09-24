"""Transcript quality-assurance harness (evaluation oracle for the ASR pipeline).

Reads the ``transcript_segments`` table of a ``ptm.sqlite`` store and recovers a
complete transcript from the *finer* of two ASR passes over the same source, so
a run that silently dropped content can be compared and corrected without
re-transcription.

It is a read-only analysis tool, not part of the conversion pipeline: it never
touches the audio model, the audio server, or the ``ptm-transcribe`` CLI. The
only output it writes is the reconstructed ``<stem>.N.md`` / ``<stem>.N.srt``
pair requested on the command line.

What it does for a given ``source``:

- **pass splitting** — groups rows into import runs by their ``ts`` gap (a run
  boundary is a ``ts`` jump of more than ``_PASS_GAP_SECONDS``). This is more
  robust than ``substr(ts,1,19)``: a single run can straddle a second boundary
  (22-sep spans two seconds), while two runs of 21-sep are minutes apart.
- **finer/block diff** — the finer pass (more rows) is the reference; the block
  pass is what the tool suspects dropped content. Segments in the finer pass
  that the block pass's coverage does not overlap are localised as *dropped*.
- **coverage map** — time ranges with no words, up to the audio duration.
- **reconstruction** — the finer pass is rendered as Markdown/SRT (reusing
  ``converter.transcribe`` so the output format matches the rest of the tool),
  with no words added or removed.
- **cloud cross-check** — when a reference ``.srt`` is supplied, each dropped
  window's start time is matched to the nearest cloud cue and the delta reported.

Usage::

    ./.venv/bin/python scripts/transcript_qa.py \
        --db interviews/ptm.sqlite \
        --source 21-sep.transcript.4.md \
        --cloud-srt interviews/21-sep-cloud.srt \
        --audio-duration 817.52
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# A ``ts`` gap larger than this separates two import runs. Within a run, segments
# are recorded milliseconds apart; between runs the gap is minutes (or, for a
# second-straddling run, at most ~1 s).
_PASS_GAP_SECONDS = 5.0

_DEFAULT_SOURCE = "21-sep.transcript.4.md"


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _rows(db_path: str | Path, source: str) -> list[dict]:
    """Return ``transcript_segments`` rows for ``source`` ordered by ``ts, id``.

    Opened read-only (``mode=ro``) so the QA tool can never mutate the store it
    is analysing, even by accident.
    """
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT id, ts, start, end, speaker, text, model "
            "FROM transcript_segments WHERE source = ? ORDER BY ts, id",
            (source,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "start": float(r[2]),
            "end": float(r[3]),
            "speaker": r[4],
            "text": r[5],
            "model": r[6],
        }
        for r in rows
    ]


def split_passes(segments: list[dict]) -> list[list[dict]]:
    """Split ``segments`` into import runs by their ``ts`` gap.

    ``segments`` must be ordered by ``ts`` (as returned by :func:`_rows`).
    """
    passes: list[list[dict]] = []
    current: list[dict] = []
    gap = timedelta(seconds=_PASS_GAP_SECONDS)
    for seg in segments:
        if current and (_parse_ts(seg["ts"]) - _parse_ts(current[-1]["ts"])) > gap:
            passes.append(current)
            current = []
        current.append(seg)
    if current:
        passes.append(current)
    return passes


def load_passes(db_path: str | Path, source: str = _DEFAULT_SOURCE) -> list[list[dict]]:
    """Load and split the passes recorded for ``source``."""
    return split_passes(_rows(db_path, source))


def identify_passes(passes: list[list[dict]]) -> tuple[list[dict] | None, list[dict] | None]:
    """Return ``(finer, block)`` for a two-pass source, else ``(None, None)``.

    The finer pass is the one with more segments (the reference); the block pass
    is the one suspected of dropping content.
    """
    if len(passes) != 2:
        return None, None
    a, b = passes
    return (a, b) if len(a) >= len(b) else (b, a)


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


_WORD_RE = re.compile(r"[a-zæøå0-9]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _word_set(segments: list[dict]) -> set[str]:
    out: set[str] = set()
    for seg in segments:
        out.update(_words(seg["text"]))
    return out


def diff_passes(
    finer: list[dict], block: list[dict]
) -> tuple[list[dict], list[str]]:
    """Return ``(dropped_segments, dropped_words)``.

    ``dropped_words`` are the words present in the finer pass but absent from the
    block pass — the content the block pass dropped. ``dropped_segments`` are the
    finer segments annotated with their ``missing`` words, most-affected first,
    so the omission windows sort to the top. Two different ASR passes share most
    vocabulary, so the distinctive dropped tokens (``kjempekleit``, ``klein``,
    ``funker``, ``kompis`` …) are what localise a folded turn or story.
    """
    block_words = _word_set(block)
    dropped_segments: list[dict] = []
    for seg in finer:
        missing = [w for w in _words(seg["text"]) if w not in block_words]
        dropped_segments.append({**seg, "missing": missing})
    dropped_segments.sort(key=lambda s: len(s["missing"]), reverse=True)
    dropped_words = sorted({w for seg in dropped_segments for w in seg["missing"]})
    return dropped_segments, dropped_words


def coverage_map(
    segments: list[dict], audio_duration: float | None = None
) -> list[tuple[float, float]]:
    """Return time ranges (up to ``audio_duration``) with no words."""
    cov = _merge_intervals([(s["start"], s["end"]) for s in segments])
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cov:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if audio_duration is not None and cursor < audio_duration:
        gaps.append((cursor, audio_duration))
    return gaps


def _reconstruct_renderers():
    """Return the converter's renderers, or minimal local fallbacks.

    Lazy import keeps the QA tool free of the converter's import-time
    environment; a local fallback keeps it usable outside the repo venv.
    """
    try:
        from converter.transcribe import segments_to_markdown, segments_to_srt

        return segments_to_markdown, segments_to_srt
    except Exception:  # noqa: BLE001 - fall back to the local renderers
        return _local_markdown, _local_srt


def _format_timestamp(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _srt_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _local_merge(segments: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker")
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and speaker is not None
            and prev.get("speaker") == speaker
            and seg["start"] >= prev.get("_end", 0.0)
        ):
            prev["text"] = prev["text"] + " " + text
            prev["end"] = seg["end"]
            prev["_end"] = seg["end"]
        else:
            merged.append(dict(seg))
    for item in merged:
        item.pop("_end", None)
    return merged


def _local_markdown(segments: list[dict], model: str | None = None) -> str:
    lines = ["# Transcript", "", "<details>", f"<summary>Auto-generated transcript ({model or 'finer ASR pass'})</summary>", ""]
    for seg in _local_merge(segments):
        ts = _format_timestamp(seg["start"])
        text = seg["text"].strip()
        speaker = seg.get("speaker")
        lines.append(f"[{ts}] **{speaker}:** {text}" if speaker else f"[{ts}] {text}")
    lines += ["", "</details>"]
    return "\n".join(lines)


def _local_srt(segments: list[dict]) -> str:
    blocks: list[str] = []
    for i, seg in enumerate(_local_merge(segments), start=1):
        text = seg["text"].strip()
        speaker = seg.get("speaker")
        if speaker:
            text = f"[{speaker}] {text}"
        blocks.append(
            f"{i}\n{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def render_transcript(
    segments: list[dict], model: str | None = None
) -> tuple[str, str]:
    """Render ``segments`` as ``(markdown, srt)``, sorted by start time."""
    ordered = sorted(segments, key=lambda s: (s["start"], s["end"]))
    to_md, to_srt = _reconstruct_renderers()
    return to_md(ordered, model), to_srt(ordered)


def parse_srt(path: str | Path) -> list[dict]:
    """Parse a SubRip file into ``[{start, end, text}, ...]`` (seconds)."""
    raw = Path(path).read_text(encoding="utf-8")
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        text = " ".join(lines[2:]).strip()
        cues.append({"start": start, "end": end, "text": text})
    return cues


def cross_check_cloud(
    segments: list[dict], cloud: list[dict]
) -> list[dict]:
    """Return per-segment cloud matches: ``{start, end, cloud_start, delta, text}``.

    Each segment is matched to the cloud cue with the largest temporal overlap;
    ``delta`` is the signed start-time difference in seconds.
    """
    report: list[dict] = []
    for seg in segments:
        best: dict | None = None
        best_overlap = -1.0
        for cue in cloud:
            ov = _overlap((seg["start"], seg["end"]), (cue["start"], cue["end"]))
            if ov > best_overlap:
                best_overlap = ov
                best = cue
        if best is None:
            continue
        report.append(
            {
                "start": seg["start"],
                "end": seg["end"],
                "cloud_start": best["start"],
                "delta": seg["start"] - best["start"],
                "text": seg["text"],
            }
        )
    return report


def _bump_version(stem: str) -> str:
    m = re.match(r"^(.*\.)(\d+)$", stem)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + 1}"
    return stem + ".qa"


def _fmt_delta(seconds: float) -> str:
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{abs(seconds):.1f}s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcript_qa",
        description="Recover a dropped transcript pass from ptm.sqlite (read-only QA oracle).",
    )
    parser.add_argument("--db", required=True, help="path to ptm.sqlite")
    parser.add_argument("--source", default=_DEFAULT_SOURCE, help="source column value")
    parser.add_argument("--audio-duration", type=float, default=None, help="audio length in seconds")
    parser.add_argument("--cloud-srt", default=None, help="reference .srt for timestamp cross-check")
    parser.add_argument("--out-md", default=None, help="output Markdown path (default: next version beside --db)")
    parser.add_argument("--out-srt", default=None, help="output SRT path (default: next version beside --db)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Path(args.db)
    passes = load_passes(db, args.source)
    if not passes:
        print(f"no transcript_segments rows for source {args.source!r}")
        return 1

    finer, block = identify_passes(passes)
    if finer is None:
        print(f"expected exactly two passes for {args.source!r}, found {len(passes)}")
        return 1

    print(f"{args.source}: finer={len(finer)} segments, block={len(block)} segments")

    dropped_segments, dropped_words = diff_passes(finer, block)
    if dropped_words:
        print(f"dropped words ({len(dropped_words)} in finer, absent from block):")
        print("  " + ", ".join(dropped_words[:40]) + (" …" if len(dropped_words) > 40 else ""))
        print("most-affected finer segments:")
        for seg in dropped_segments[:8]:
            print(
                f"  {_fmt_clock(seg['start'])}  missing {len(seg['missing'])}: "
                f"{', '.join(seg['missing'])}"
            )
    else:
        print("no dropped content detected between the two passes")

    duration = args.audio_duration
    if duration is None and finer:
        duration = max(s["end"] for s in finer)
    gaps = coverage_map(finer, duration)
    if gaps:
        print("coverage gaps (no words):")
        for start, end in gaps:
            print(f"  {_fmt_clock(start)} - {_fmt_clock(end)}")

    bumped = _bump_version(Path(args.source).stem)
    out_md = Path(args.out_md) if args.out_md else db.parent / f"{bumped}.md"
    out_srt = Path(args.out_srt) if args.out_srt else db.parent / f"{bumped}.srt"

    out_md.parent.mkdir(parents=True, exist_ok=True)
    md_text, srt_text = render_transcript(finer, None)
    out_md.write_text(md_text + "\n", encoding="utf-8")
    out_srt.write_text(srt_text, encoding="utf-8")
    print(f"wrote {out_md} and {out_srt} from the finer pass ({len(finer)} segments)")

    if args.cloud_srt:
        cloud = parse_srt(args.cloud_srt)
        report = cross_check_cloud(dropped_segments, cloud)
        if report:
            print("cloud cross-check (dropped content):")
            for item in report[:12]:
                print(
                    f"  {_fmt_clock(item['start'])}  cloud {_fmt_clock(item['cloud_start'])}  "
                    f"delta {_fmt_delta(item['delta'])}"
                )
    return 0


def _fmt_clock(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
