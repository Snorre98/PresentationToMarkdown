"""Unit tests for deterministic borderless-table detection (ADR-0054).

The detector works on ``Span`` objects — the geometry-only projection of a
page's content stream — so the tests can model both producer styles (Word's
one-span-per-cell and one-span-per-line) and every trap the real fixtures
showed without shipping exotic PDFs.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from converter import config
from converter.borderless import Span, detect_borderless_tables, detect_tables

TESTS_DIR = Path(__file__).parent
BORDERLESS_PDF = TESTS_DIR / "test_borderless.pdf"

PAGE_W = 612.0


def _word_spans() -> list[Span]:
    """A Word-style subset of the motivating assignment's page-2 table.

    Each cell (including the 4-line one) is a single span; the header's first
    cell is empty (a whitespace-only span, dropped by the extractor); rows 3
    and 4 sit at the same 17pt pitch as the wrapped lines.
    """
    return [
        Span("Current process", 224.9, 214.7, 315.8, 226.7, 12.0),
        Span("Model A", 358.4, 214.7, 402.3, 226.7, 12.0),
        Span("Main approach", 77.5, 261.2, 157.5, 273.2, 12.0),
        Span(
            "Scheduled maintenance + warning signs + engineer judgement",
            207.1, 235.7, 330.4, 298.7, 12.0,
        ),
        Span("Simpler failure-risk model", 342.1, 244.2, 400.3, 290.2, 12.0),
        Span("Bus age, mileage and service history", 77.5, 303.7, 189.8, 332.9, 12.0),
        Span("Yes", 207.1, 312.2, 228.3, 324.2, 12.0),
        Span("Yes", 342.1, 312.2, 363.3, 324.2, 12.0),
        Span("Previous repairs/faults", 77.5, 337.9, 197.0, 349.9, 12.0),
        Span("Used manually", 207.1, 337.9, 287.1, 349.9, 12.0),
        Span("Yes", 342.1, 337.9, 363.3, 349.9, 12.0),
        Span("Warning codes", 77.5, 355.0, 157.3, 367.0, 12.0),
        Span("Used manually", 207.1, 355.0, 287.1, 367.0, 12.0),
        Span("Yes", 342.1, 355.0, 363.3, 367.0, 12.0),
    ]


EXPECTED_WORD_GRID = [
    ["", "Current process", "Model A"],
    [
        "Main approach",
        "Scheduled maintenance + warning signs + engineer judgement",
        "Simpler failure-risk model",
    ],
    ["Bus age, mileage and service history", "Yes", "Yes"],
    ["Previous repairs/faults", "Used manually", "Yes"],
    ["Warning codes", "Used manually", "Yes"],
]


def test_word_style_cells_reconstruct_exactly():
    tables = detect_tables(_word_spans(), PAGE_W)
    assert len(tables) == 1
    assert tables[0].extract() == EXPECTED_WORD_GRID


def test_per_line_wrapped_cells_merge_into_one_grid():
    """A producer that emits one span per text line yields the same table."""
    spans = [
        Span("Current process", 224.9, 214.7, 315.8, 226.7, 12.0),
        Span("Model A", 358.4, 214.7, 402.3, 226.7, 12.0),
        Span("Main approach", 77.5, 261.2, 157.5, 273.2, 12.0),
        Span("Scheduled maintenance", 207.1, 235.7, 330.4, 247.7, 12.0),
        Span("+ warning signs +", 207.1, 252.7, 330.4, 264.7, 12.0),
        Span("engineer judgement", 207.1, 269.7, 330.4, 281.7, 12.0),
        Span("Simpler failure-risk", 342.1, 244.2, 400.3, 256.2, 12.0),
        Span("model", 342.1, 261.2, 400.3, 273.2, 12.0),
        Span("Bus age, mileage and", 77.5, 303.7, 189.8, 315.7, 12.0),
        Span("service history", 77.5, 320.9, 189.8, 332.9, 12.0),
        Span("Yes", 207.1, 312.2, 228.3, 324.2, 12.0),
        Span("Yes", 342.1, 312.2, 363.3, 324.2, 12.0),
        Span("Previous repairs/faults", 77.5, 337.9, 197.0, 349.9, 12.0),
        Span("Used manually", 207.1, 337.9, 287.1, 349.9, 12.0),
        Span("Yes", 342.1, 337.9, 363.3, 349.9, 12.0),
        Span("Warning codes", 77.5, 355.0, 157.3, 367.0, 12.0),
        Span("Used manually", 207.1, 355.0, 287.1, 367.0, 12.0),
        Span("Yes", 342.1, 355.0, 363.3, 367.0, 12.0),
    ]
    tables = detect_tables(spans, PAGE_W)
    assert len(tables) == 1
    expected = [EXPECTED_WORD_GRID[0]] + [
        ["Main approach", "Scheduled maintenance + warning signs + engineer judgement", "Simpler failure-risk model"],
    ] + EXPECTED_WORD_GRID[2:]
    assert tables[0].extract() == expected


def test_adjacent_rows_same_pitch_as_wrapped_lines_stay_apart():
    """Rows 3/4 of the motivating table are 17pt apart — exactly the wrapped
    line pitch. Stream order (a leftward column jump) is the separator; a gap
    heuristic alone would merge them into one 2-line cell."""
    grid = detect_tables(_word_spans(), PAGE_W)[0].extract()
    assert ["Previous repairs/faults", "Used manually", "Yes"] in grid
    assert ["Warning codes", "Used manually", "Yes"] in grid


def test_table_needs_three_rows():
    """A header plus a single data row is too ambiguous; it stays text."""
    spans = [
        Span("Header A", 200, 100, 260, 112, 12.0),
        Span("Header B", 400, 100, 460, 112, 12.0),
        Span("left", 72, 120, 120, 132, 12.0),
        Span("right", 300, 120, 360, 132, 12.0),
    ]
    assert detect_tables(spans, PAGE_W) == []


def test_bullet_list_is_not_a_table():
    spans = [
        Span("\u2022", 90.1, 297.0, 95.6, 309.0, 12.0),
        Span("avoided emergency repair and towing costs;", 108.1, 297.4, 340.1, 309.4, 12.0),
        Span("\u2022", 90.1, 314.7, 95.6, 326.7, 12.0),
        Span("reduced replacement-bus and service-disruption costs;", 108.1, 315.2, 401.1, 327.2, 12.0),
        Span("\u2022", 90.1, 332.5, 95.6, 344.5, 12.0),
        Span("reduced time buses spend unexpectedly out of service.", 108.1, 332.9, 397.9, 344.9, 12.0),
    ]
    assert detect_tables(spans, PAGE_W) == []


def test_numbered_list_is_not_a_table():
    spans = [
        Span("1.", 90.1, 331.4, 100.0, 343.4, 12.0),
        Span("Evaluate the evidence", 108.1, 331.4, 228.1, 343.4, 12.0),
        Span("2.", 90.1, 348.5, 100.0, 360.5, 12.0),
        Span("Reason about trade-offs", 108.1, 348.5, 360.2, 360.5, 12.0),
        Span("3.", 90.1, 365.5, 100.0, 377.5, 12.0),
        Span("Make and justify a decision", 108.1, 365.5, 255.1, 377.5, 12.0),
    ]
    assert detect_tables(spans, PAGE_W) == []


def test_prose_fragments_are_not_a_table():
    spans = [
        Span("The team also examined how often an upcoming failure appeared", 72.0, 203.2, 506.9, 232.2, 12.0),
        Span("10 highest-risk buses", 129.5, 220.2, 245.0, 232.2, 12.0),
        Span("on the days leading up to the failure.", 245.1, 220.2, 436.4, 232.2, 12.0),
        Span("Model B performs substantially better for newer buses.", 72.0, 363.2, 541.5, 392.5, 12.0),
    ]
    assert detect_tables(spans, PAGE_W) == []


def test_two_column_body_text_is_not_a_table():
    """Aligned left/right columns (paper layout) never form table rows here:
    the columns are written column by column, so every row has one cell."""
    spans = [
        Span("The first challenge is keeping players", 50, 250, 218, 260, 10.0),
        Span("engaged through uncertain goals.", 50, 280, 218, 290, 10.0),
        Span("Fantasy can make a game more", 330, 250, 498, 260, 10.0),
        Span("compelling when it is intrinsic.", 330, 280, 498, 290, 10.0),
    ]
    assert detect_tables(spans, PAGE_W) == []


def test_page_wide_cells_are_prose_not_a_table():
    """Aligned wide fragments with a short trailing label would otherwise look
    like two columns; the page-width gate keeps them as prose."""
    spans = [
        Span("A sentence that spans almost the entire width of the page", 72, 100, 570, 112, 12.0),
        Span("Label", 580, 100, 600, 112, 12.0),
        Span("Another long prose line covering nearly the whole page width", 72, 120, 570, 132, 12.0),
        Span("Label", 580, 120, 600, 132, 12.0),
        Span("Yet another wide sentence that spans the whole page width", 72, 140, 570, 152, 12.0),
        Span("Label", 580, 140, 600, 152, 12.0),
    ]
    assert detect_tables(spans, PAGE_W) == []


def test_dash_range_joins_without_spaces():
    spans = [
        Span("Points", 72, 100, 110, 112, 12.0),
        Span("Grade", 200, 100, 240, 112, 12.0),
        Span("90", 72, 117, 84, 129, 12.0),
        Span("\u2013", 84.5, 117, 90, 129, 12.0),
        Span("100", 90.5, 117, 108, 129, 12.0),
        Span("A", 200, 117, 208, 129, 12.0),
        Span("50", 72, 134, 84, 146, 12.0),
        Span("\u2013", 84.5, 134, 90, 146, 12.0),
        Span("59", 90.5, 134, 102, 146, 12.0),
        Span("E", 200, 134, 208, 146, 12.0),
    ]
    tables = detect_tables(spans, PAGE_W)
    assert len(tables) == 1
    assert tables[0].extract() == [["Points", "Grade"], ["90\u2013100", "A"], ["50\u201359", "E"]]


def test_generated_fixture_detects_and_reports_shape():
    doc = fitz.open(BORDERLESS_PDF)
    page = doc[0]
    tables = detect_borderless_tables(page)
    assert len(tables) == 1
    table = tables[0]
    assert table.row_count == 6
    assert table.col_count == 3
    assert table.extract()[2] == ["Bus age, mileage and service history", "Yes", "Yes"]
    assert table.bbox[0] == 72.0
    assert table.bbox[1] < 140.0  # header row top
    assert table.bbox[2] > 430.0
    assert table.bbox[3] > 300.0
    doc.close()


def test_ruled_table_cells_are_also_visible_to_the_detector():
    """The detector is purely geometric and also sees a ruled page's cells; the
    converter resolves that overlap in favour of ``find_tables()`` (ADR-0054)."""
    doc = fitz.open(BORDERLESS_PDF)
    tables = detect_borderless_tables(doc[1])
    assert len(tables) == 1
    assert tables[0].extract() == [
        ["Ruled A", "Ruled B"],
        ["one", "two"],
        ["three", "four"],
    ]
    doc.close()


def test_prose_fixtures_have_no_borderless_tables():
    for name in ("test_doc.pdf", "test_paper.pdf"):
        doc = fitz.open(TESTS_DIR / name)
        for page in doc:
            assert detect_borderless_tables(page) == []
        doc.close()


def test_kill_switch_disables_detection(monkeypatch):
    monkeypatch.setenv("PDF_BORDERLESS_TABLES", "off")
    assert config.borderless_tables_enabled() is False
    doc = fitz.open(BORDERLESS_PDF)
    assert detect_borderless_tables(doc[0]) == []
    doc.close()
    monkeypatch.setenv("PDF_BORDERLESS_TABLES", "on")
    assert config.borderless_tables_enabled() is True
