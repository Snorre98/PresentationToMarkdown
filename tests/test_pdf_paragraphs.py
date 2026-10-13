"""Unit tests for the deterministic paragraph/heading model (ADR-0055).

These pin the learned-statistic profiler, the paragraph segmentation rules, the
heading evidence ladder and the marker-safe rendering without opening a PDF.
"""
from __future__ import annotations

from converter import paragraphs
from converter.paragraphs import Block, Profile, Run, TextLine


def _line(text, size=12.0, x0=72.0, y0=100.0, bold=False):
    return TextLine(
        text=text,
        x0=x0,
        x1=x0 + 200.0,
        y0=y0,
        y1=y0 + size,
        size=size,
        runs=[Run(text, bold, False)],
    )


def _runs(parts, size=12.0, x0=72.0, y0=100.0):
    text = "".join(part for part, _ in parts)
    return TextLine(
        text=text,
        x0=x0,
        x1=x0 + 200.0,
        y0=y0,
        y1=y0 + size,
        size=size,
        runs=[Run(part, bold, False) for part, bold in parts],
    )


# --- profiler ----------------------------------------------------------------


def test_profile_learns_body_pitch_and_paragraph_gap():
    page = [
        _line("Body one.", y0=100.0),
        _line("Body one cont.", y0=117.0),
        _line("Body two.", y0=146.0),
        _line("Body two cont.", y0=163.0),
        _line("Heading", size=18.0, bold=True, y0=196.0),
        _line("Body three.", y0=240.0),
        _line("Body three cont.", y0=257.0),
        _line("Heading two", size=18.0, bold=True, y0=290.0),
        _line("Body four.", y0=334.0),
        _line("Body four cont.", y0=351.0),
        _line("Body five.", y0=380.0),
        _line("Body five cont.", y0=397.0),
    ]
    profile = paragraphs.build_profile([page], [792.0])

    assert profile.body_size == 12.0
    assert profile.pitch == 17.0
    assert profile.para_gap == 23.0
    assert profile.tiers == [18.0]


def test_profile_splits_clusters_when_breaks_outnumber_wraps():
    """A document of short one-line paragraphs would fool a plain delta mode."""
    page = [_line(f"Line {i}.", y0=100.0 + 16.0 * i) for i in range(3)]
    page += [_line(f"Para {i}.", y0=260.0 + 28.0 * i) for i in range(4)]

    profile = paragraphs.build_profile([page], [792.0])

    assert profile.pitch == 16.0
    assert profile.para_gap == 22.0


def test_profile_detects_title_size_and_reuse():
    page1 = [
        _line("The Document Title", size=24.0, bold=True, y0=90.0),
        _line("A subtitle line", size=18.0, bold=True, y0=140.0),
        _line("Body one.", y0=200.0),
        _line("Body one cont.", y0=217.0),
        _line("Body two.", y0=246.0),
        _line("Body two cont.", y0=263.0),
    ]
    page2 = [
        _line("Evidence", size=24.0, bold=True, y0=90.0),
        _line("Second heading", size=18.0, bold=True, y0=140.0),
        _line("Other body.", y0=200.0),
        _line("Other body cont.", y0=217.0),
        _line("Body x.", y0=246.0),
        _line("Body x cont.", y0=263.0),
    ]
    profile = paragraphs.build_profile([page1, page2], [792.0, 792.0])

    assert profile.title_size == 24.0
    assert profile.title_reused is True
    assert paragraphs.heading_levels(profile) == {24.0: 2, 18.0: 3}


def test_title_size_used_only_on_page_one_leaves_section_levels():
    page1 = [
        _line("The Document Title", size=24.0, bold=True, y0=90.0),
        _line("Wrapped title tail", size=24.0, bold=True, y0=120.0),
        _line("First heading", size=18.0, bold=True, y0=170.0),
        _line("Body one.", y0=210.0),
        _line("Body one cont.", y0=227.0),
    ]
    page2 = [
        _line("Second heading", size=18.0, bold=True, y0=90.0),
        _line("Body two.", y0=130.0),
        _line("Body two cont.", y0=147.0),
    ]
    profile = paragraphs.build_profile([page1, page2], [792.0, 792.0])

    assert profile.title_size == 24.0
    assert profile.title_reused is False
    assert paragraphs.heading_levels(profile) == {18.0: 2}


def test_detect_title_skips_numbered_and_out_of_band_lines():
    profile = Profile(
        body_size=12.0, pitch=17.0, para_gap=23.0, tiers=[24.0], title_size=24.0
    )
    title = _line("The Study of Things", size=24.0, bold=True, y0=90.0)
    numbered = _line("1. Introduction", size=24.0, bold=True, y0=90.0)
    deep = _line("Deep down the page", size=24.0, bold=True, y0=400.0)

    assert paragraphs.detect_title([title], 792.0, 612.0, profile) == [title]
    assert paragraphs.detect_title([numbered], 792.0, 612.0, profile) == []
    assert paragraphs.detect_title([deep], 792.0, 612.0, profile) == []


def test_detect_title_skips_logos_and_letterheads():
    """A corner decoration is not a title, and the last same-size run before the
    body (the real title) wins over a letterhead above it."""
    profile = Profile(
        body_size=10.0, pitch=13.0, para_gap=19.0, tiers=[24.0], title_size=24.0
    )
    body = [
        _line("Body line one.", size=10.0, y0=300.0),
        _line("Body line two.", size=10.0, y0=313.0),
    ]
    logo = _line("a", size=24.0, x0=520.0, y0=20.0)
    letterhead = _line("The University of Rostock", size=24.0, y0=30.0)
    title = _line("Case Study for Enterprise Modeling", size=24.0, y0=130.0)

    assert paragraphs.detect_title([logo] + body, 842.0, 595.0, profile) == []
    assert paragraphs.detect_title([logo, letterhead, title] + body, 842.0, 595.0, profile) == [title]


# --- segmentation ------------------------------------------------------------


def test_segment_merges_wraps_and_breaks_sentences():
    profile = Profile(body_size=12.0, pitch=17.0, para_gap=23.0, tiers=[])
    lines = [
        _line("A sentence that wraps", y0=100.0),
        _line("onto the next line.", y0=117.0),
        _line("A new sentence starts.", y0=146.0),
        _line("Another paragraph.", y0=175.0),
    ]

    blocks = paragraphs.segment_group(lines, profile)

    assert [paragraphs.block_plain(block) for block in blocks] == [
        "A sentence that wraps onto the next line.",
        "A new sentence starts.",
        "Another paragraph.",
    ]


def test_segment_bullets_numbered_items_and_crowded_rows():
    profile = Profile(body_size=12.0, pitch=17.0, para_gap=23.0, tiers=[])
    bullets = [
        TextLine(
            text="first item;", x0=90.0, x1=270.0, y0=130.0, y1=142.0, size=12.0,
            runs=[Run("first item;")], bullet="\u2022",
        ),
        _line("wrapped item text.", x0=108.0, y0=147.0),
        TextLine(
            text="second item;", x0=90.0, x1=270.0, y0=176.0, y1=188.0, size=12.0,
            runs=[Run("second item;")], bullet="\u2022",
        ),
        _line("1. First step", x0=90.0, y0=205.0),
        _line("2. Second step", x0=90.0, y0=222.0),
    ]
    blocks = paragraphs.segment_group([_line("Lead-in:", y0=100.0)] + bullets, profile)

    assert [block.kind for block in blocks] == ["para", "bullet", "bullet", "para", "para"]
    assert [len(block.lines) for block in blocks] == [1, 2, 1, 1, 1]
    assert paragraphs.block_plain(blocks[1]) == "first item; wrapped item text."

    row = paragraphs.segment_group(
        [_line("Left label", x0=72.0, y0=100.0), _line("Right label", x0=300.0, y0=100.0)],
        profile,
    )
    assert [block.kind for block in row] == ["para", "para"]
    assert all(line.crowded for block in row for line in block.lines)


# --- rendering ---------------------------------------------------------------


def test_join_runs_merges_markers_across_lines_and_hyphens():
    lines = [
        _runs([("The estimate is ", False), ("10 available", True)], y0=100.0),
        _runs([("additional inspections", True), (".", False)], y0=117.0),
    ]
    assert (
        paragraphs.render_runs(paragraphs.join_runs(lines))
        == "The estimate is **10 available additional inspections**."
    )

    hyphen = [
        _runs([("the data-", False)], y0=100.0),
        _runs([("science pipeline", False)], y0=117.0),
    ]
    assert paragraphs.render_runs(paragraphs.join_runs(hyphen)) == "the data-science pipeline"


def test_render_block_reflow_off_emits_each_line():
    block = Block(kind="para", lines=[_line("Line one", y0=100.0), _line("line two", y0=117.0)])
    assert paragraphs.render_block(block) == ["Line one line two"]
    assert paragraphs.render_block(block, reflow=False) == ["Line one", "line two"]

    bullet = Block(
        kind="bullet",
        lines=[_line("item text", y0=100.0), _line("wrapped", y0=117.0)],
        bullet_level=1,
    )
    assert paragraphs.render_block(bullet, reflow=False) == ["  - item text", "    wrapped"]


# --- heading evidence ladder -------------------------------------------------


def _candidate(text, *, size=11.0, bold=False, gap=30.0, continues=False, x0=72.0):
    return Block(
        kind="para",
        lines=[_line(text, size=size, x0=x0, bold=bold)],
        start_gap=gap,
        continues=continues,
    )


def test_heading_ladder_tier_path():
    profile = Profile(body_size=11.0, pitch=16.0, para_gap=22.0, tiers=[13.0])
    levels = {13.0: 2}

    assert paragraphs.classify_block(_candidate("A Heading", size=13.0), profile, levels, col_min=72.0, col_max=300.0) == 2
    # A mid-sentence continuation is never a heading, even at a tier size.
    assert paragraphs.classify_block(_candidate("A Heading", size=13.0, continues=True), profile, levels, col_min=72.0, col_max=300.0) == 0
    # A tier-sized line that is indented off both flush and centered positions.
    assert paragraphs.classify_block(_candidate("A Heading", size=13.0, x0=200.0), profile, levels, col_min=72.0, col_max=300.0) == 0


def test_heading_ladder_bold_only_strict_gates():
    profile = Profile(body_size=11.0, pitch=16.0, para_gap=22.0, tiers=[])

    assert paragraphs.classify_block(_candidate("A Bold Heading", bold=True), profile, {}, col_min=72.0, col_max=300.0) == 2
    assert paragraphs.classify_block(_candidate("A bold sentence.", bold=True), profile, {}, col_min=72.0, col_max=300.0) == 0
    assert paragraphs.classify_block(_candidate("a bold fragment", bold=True), profile, {}, col_min=72.0, col_max=300.0) == 0
    assert paragraphs.classify_block(_candidate("A Bold Heading", bold=True, gap=17.0), profile, {}, col_min=72.0, col_max=300.0) == 0
    assert paragraphs.classify_block(_candidate("A Bold Heading", bold=True, continues=True), profile, {}, col_min=72.0, col_max=300.0) == 0
    long_line = " ".join(["word"] * 13) + " end"
    assert paragraphs.classify_block(_candidate(long_line, bold=True), profile, {}, col_min=72.0, col_max=300.0) == 0

    crowded = _candidate("A Bold Heading", bold=True)
    crowded.lines[0].crowded = True
    assert paragraphs.classify_block(crowded, profile, {}, col_min=72.0, col_max=300.0) == 0
