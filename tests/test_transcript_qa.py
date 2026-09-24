"""Tests for the transcript QA harness (``scripts/transcript_qa.py``).

These run against a *read-only copy* of the real interviews ``ptm.sqlite``: the
source store (in the Obsidian vault, outside this repo) is copied to ``tmp_path``
and the copy is made read-only before the harness opens it, so the real artifact
is never touched. When the vault store is absent (CI, a different machine), the
whole module skips.

The oracle is the 21-sep two-pass record: the finer nb-whisper-large pass
(``2026-09-23T07:13:53``, 193 rows) contains the content the block pass
(``2026-09-23T07:40:02``, 104 rows) dropped — rows 219/220 (the "kjempekleit …
klein dag" first-day answer), row 244 ("Hva føler du ikke funker bra?"), and
rows 314–318 (the "nettsiden … kompis" story).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from scripts.transcript_qa import (
    cross_check_cloud,
    diff_passes,
    identify_passes,
    load_passes,
    parse_srt,
    render_transcript,
)

_QA_DIR = Path(
    "/Volumes/Ex-SSD/Documents/Studies/Masters/obsidian-studies/H26/"
    "User-centered interaction design IT3402/deliverables/interviews"
)

KNOWN_ROWS = {219, 220, 244, 314, 315, 316, 317, 318}


def _qa_db() -> Path:
    return Path(os.environ.get("PTM_QA_DB", _QA_DIR / "ptm.sqlite"))


def _qa_cloud() -> Path:
    return Path(os.environ.get("PTM_QA_CLOUD_SRT", _QA_DIR / "21-sep-cloud.srt"))


pytestmark = pytest.mark.skipif(
    not _qa_db().exists(),
    reason="interview ptm.sqlite not available (set PTM_QA_DB to run)",
)


@pytest.fixture
def db_copy(tmp_path: Path) -> Path:
    """A read-only copy of the real interviews store, isolated to ``tmp_path``."""
    dst = tmp_path / "ptm.sqlite"
    shutil.copy2(_qa_db(), dst)
    os.chmod(dst, 0o444)
    return dst


@pytest.fixture
def cloud() -> Path:
    if not _qa_cloud().exists():
        pytest.skip("cloud srt not available (set PTM_QA_CLOUD_SRT to run)")
    return _qa_cloud()


@pytest.fixture
def finer_block(db_copy: Path) -> tuple[list[dict], list[dict]]:
    passes = load_passes(db_copy, "21-sep.transcript.4.md")
    finer, block = identify_passes(passes)
    assert finer is not None and block is not None
    return finer, block


def test_passes_split_finer_block(db_copy: Path):
    passes = load_passes(db_copy, "21-sep.transcript.4.md")
    assert len(passes) == 2
    finer, block = identify_passes(passes)
    assert len(finer) == 193
    assert len(block) == 104
    assert [s["id"] for s in finer][0] == 168
    assert [s["id"] for s in block][0] == 361


def test_reconstruction_contains_omissions(finer_block):
    finer, _ = finer_block
    md, srt = render_transcript(finer)
    fragments = (
        "Det var kjempekleit",  # row 219
        "veldig klein dag",  # row 220
        "Hva føler du ikke funker bra?",  # row 244
        "lage bruker på nettsiden",  # row 314
        "går rundt i bunnen",  # row 318
    )
    for fragment in fragments:
        assert fragment in md
        assert fragment in srt


def test_diff_localizes_dropped_words(finer_block):
    finer, block = finer_block
    _, dropped_words = diff_passes(finer, block)
    for word in ("kjempekleit", "klein", "funker", "kompis"):
        assert word in dropped_words


def test_diff_annotates_known_omission_segments(finer_block):
    finer, block = finer_block
    dropped_segments, _ = diff_passes(finer, block)
    by_id = {s["id"]: s for s in dropped_segments}
    assert "kjempekleit" in by_id[219]["missing"]
    assert "klein" in by_id[220]["missing"]
    assert "funker" in by_id[244]["missing"]
    assert "kompis" in by_id[316]["missing"]


def test_timestamps_match_cloud(finer_block, cloud):
    finer, _ = finer_block
    known = [s for s in finer if s["id"] in KNOWN_ROWS]
    report = cross_check_cloud(known, parse_srt(cloud))
    assert len(report) == len(known)
    # The cloud is a different transcriber (TurboScribe), so allow ~3 s skew.
    for item in report:
        assert abs(item["delta"]) <= 3.0, f"{item['start']} -> {item['cloud_start']}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
