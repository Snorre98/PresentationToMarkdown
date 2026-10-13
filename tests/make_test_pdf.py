"""Generate a synthetic .pdf exercising the PDF converter's layout features."""
from pathlib import Path

import pymupdf as fitz

from tests.make_test_deck import make_png

FOOTER = "Sample Footer 2026"
RUNNING_HEADER = "A Study of Fun Games"

PAGE_W, PAGE_H = 720, 540
PAPER_W, PAPER_H = 612, 792


def _footer(page):
    page.insert_text((72, PAGE_H - 40), FOOTER, fontsize=11)


def _new_page(doc):
    return doc.new_page(width=PAGE_W, height=PAGE_H)


def _paper_page(doc):
    return doc.new_page(width=PAPER_W, height=PAPER_H)


def _left(page, lines, y=220):
    """Insert left-column body lines (size 10, flush at x=50)."""
    for i, text in enumerate(lines):
        page.insert_text((50, y + 30 * i), text, fontsize=10)


def _right(page, lines, y=220):
    """Insert right-column body lines (size 10, flush at x=330)."""
    for i, text in enumerate(lines):
        page.insert_text((330, y + 30 * i), text, fontsize=10)


def _make_paper():
    """A synthetic 2-column academic paper exercising paper mode.

    Page 1: centered title/subtitle/authors, then a left column ("Challenge")
    and a right column ("Fantasy"). Pages 2-3 carry a running header (stripped
    in paper mode) and continue the columns ("Curiosity" heading on page 2).
    """
    out = Path(__file__).parent / "test_paper.pdf"
    doc = fitz.open()

    page = _paper_page(doc)
    page.insert_text((200, 120), "What Makes Things Fun to Learn?", fontsize=16)
    page.insert_text((220, 136), "Heuristics for Design", fontsize=13)
    page.insert_text((260, 152), "T. Malone", fontsize=11)
    page.insert_text((255, 164), "Xerox PARC", fontsize=9)
    page.insert_text((50, 220), "Challenge", fontname="hebo", fontsize=12)
    _left(
        page,
        [
            "The first challenge is keeping players",
            "engaged through uncertain goals.",
        ],
        y=250,
    )
    page.insert_text((70, 340), "A wrapped continuation line.", fontsize=10)
    page.insert_text((330, 220), "Fantasy", fontname="hebo", fontsize=12)
    _right(
        page,
        [
            "Fantasy can make a game more",
            "compelling when it is intrinsic.",
            "Players identify with avatars.",
        ],
        y=250,
    )

    for left_heading, right_heading in [("Curiosity", None), (None, None)]:
        page = _paper_page(doc)
        page.insert_text((216, 40), RUNNING_HEADER, fontsize=9)
        if left_heading:
            page.insert_text((50, 220), left_heading, fontname="hebo", fontsize=12)
            _left(
                page,
                [
                    "Curiosity is sparked by surprise.",
                    "and by constructive feedback.",
                    "Rewards can even backfire.",
                ],
                y=250,
            )
        else:
            _left(
                page,
                [
                    "A conclusion wraps up the paper",
                    "and points at future work.",
                ],
                y=220,
            )
        if right_heading:
            _right(
                page,
                [
                    "A well-designed game provides",
                    "both challenge and fantasy.",
                    "Players stay engaged longer.",
                ],
                y=220,
            )
        else:
            _right(
                page,
                [
                    "References and notes go here",
                    "in the final column.",
                    "Continuing the reference list.",
                ],
                y=220,
            )

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


def _text(page, x, y, text, size, bold=False):
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")


def _mixed(page, x, y, parts, size):
    """Insert one line as ``(text, bold)`` parts sharing one baseline."""
    cursor = x
    for text, bold in parts:
        font = "hebo" if bold else "helv"
        page.insert_text((cursor, y), text, fontsize=size, fontname=font)
        cursor += fitz.get_text_length(text, fontname=font, fontsize=size)


def _make_paper_fauxbold():
    """F1: a fixture-A-style brief (ADR-0055).

    24/18/14/12 size tiers, faux-bold headings, wrapped bold tails that must
    never become headings, a real heading longer than forty characters, a
    bullet list followed by a body paragraph, and a paragraph that continues
    across the page boundary.
    """
    out = Path(__file__).parent / "test_paper_fauxbold.pdf"
    doc = fitz.open()

    page = _paper_page(doc)
    _text(page, 72, 100, "Assignment 2 - Problem D", 24, bold=True)
    _text(page, 72, 155, "Prioritizing buses for preventive maintenance", 18, bold=True)
    _text(page, 72, 195, "Scenario", 14, bold=True)
    y = 230
    for line in (
        "The team reviewed the fleet records",
        "and compared them with the workshop",
        "logs from last year.",
    ):
        _text(page, 72, y, line, 12)
        y += 17
    y += 12
    _mixed(page, 72, y, [("The estimates cover approximately ", False), ("10 available", True)], 12)
    y += 17
    _mixed(page, 72, y, [("additional preventive inspections", True), (".", False)], 12)
    y += 29
    _text(page, 72, y, "3. Effect on the maintenance decision", 18, bold=True)
    y += 33
    _mixed(page, 72, y, [("The numbers should be treated as ", False), ("estimates rather", True)], 12)
    y += 17
    _mixed(page, 72, y, [("than guaranteed savings", True), (".", False)], 12)
    y += 29
    _text(page, 72, y, "4. This heading is deliberately longer than forty characters", 18, bold=True)
    y += 33
    _mixed(page, 72, y, [("A sentence that ends with ", False), ("a bold run", True)], 12)
    y += 17
    _text(page, 72, y, "and continues in regular text.", 12)
    y += 29
    _text(page, 90, y, "\u2022 have fewer sensors;", 12)
    y += 17
    _text(page, 90, y, "\u2022 have experienced sensor replacements;", 12)
    y += 29
    _text(page, 72, y, "The model relies much more strongly on these data.", 12)
    y += 29
    _text(page, 72, y, "The report should connect the", 12)

    page = _paper_page(doc)
    _text(page, 72, 90, "evidence to the recommendation.", 12)
    _text(page, 72, 135, "Evidence provided", 24, bold=True)
    _text(page, 72, 190, "6. Important limitations and uncertainties", 18, bold=True)
    _text(page, 72, 225, "Maintenance actions influence the historical data", 14, bold=True)
    y = 265
    _mixed(
        page, 72, y,
        [("If maintenance prevented a failure, the record may show ", False), ("no failure", True), (", even", False)],
        12,
    )
    y += 17
    _text(page, 72, y, "though the bus was at high risk.", 12)
    y += 29
    _text(page, 72, y, "Some buses were never inspected.", 12)

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


def _make_paper_boldonly():
    """F2: uniform-size document whose only headings are bold (no size signal)."""
    out = Path(__file__).parent / "test_paper_boldonly.pdf"
    doc = fitz.open()

    page = _paper_page(doc)
    y = 100
    for line in (
        "The document uses one size everywhere.",
        "Its section headings are only bold.",
    ):
        _text(page, 72, y, line, 11)
        y += 16
    y += 12
    _text(page, 72, y, "A Real Section Heading", 11, bold=True)
    y += 28
    for line in (
        "A paragraph follows the heading and",
        "wraps mid-sentence as usual.",
    ):
        _text(page, 72, y, line, 11)
        y += 16
    y += 12
    _text(page, 72, y, "This key insight is emphasised.", 11, bold=True)
    y += 28
    _mixed(page, 72, y, [("The process begins with ", False), ("a very important step", True)], 11)
    y += 16
    _mixed(page, 72, y, [("and continues elsewhere", True), (".", False)], 11)
    y += 28
    _text(page, 72, y, "Another Point:", 11, bold=True)
    y += 28
    _mixed(page, 72, y, [("Lead-in term:", True), (" the rest of the sentence follows here.", False)], 11)
    y += 28
    _mixed(page, 72, y, [("The paragraph continues here and then ", False), ("No way this becomes", True)], 11)
    y += 16
    _mixed(page, 72, y, [("a heading at all", True), (" and the text moves on.", False)], 11)

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


def _make_paper_sizeonly():
    """F3: a size-only hierarchy with no bold anywhere."""
    out = Path(__file__).parent / "test_paper_sizeonly.pdf"
    doc = fitz.open()

    page = _paper_page(doc)
    _text(page, 72, 100, "Size Only Study", 15)
    _text(page, 72, 160, "1. Introduction", 13)
    y = 200
    for line in ("The measurements were taken", "in a controlled setting."):
        _text(page, 72, y, line, 11)
        y += 16
    y += 12
    _text(page, 72, y, "Data collection", 12)
    y += 28
    for line in ("The recorder sampled every event", "for two full weeks."):
        _text(page, 72, y, line, 11)
        y += 16
    y += 12
    _text(page, 72, y, "Data processing", 12)
    y += 28
    for line in ("The raw log was normalised", "before the analysis."):
        _text(page, 72, y, line, 11)
        y += 16
    y += 12
    _text(page, 72, y, "2. Methods in detail", 13)
    y += 28
    for line in ("The procedure is described", "in the following section."):
        _text(page, 72, y, line, 11)
        y += 16

    page = _paper_page(doc)
    _text(page, 72, 100, "3. Results", 13)
    y = 140
    for line in ("The effects were consistent", "across the tested groups."):
        _text(page, 72, y, line, 11)
        y += 16
    y += 12
    _text(page, 72, y, "Measurements", 12)
    y += 28
    for line in ("Every metric was recorded twice", "and checked by a second reader."):
        _text(page, 72, y, line, 11)
        y += 16

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


def _make_paper_punct_columns():
    """F4: two columns, numbered headings ending ``:``/``?``, one paragraph that
    continues from the left column into the right column."""
    out = Path(__file__).parent / "test_paper_punct_columns.pdf"
    doc = fitz.open()

    page = _paper_page(doc)
    _text(page, 190, 100, "Colon and Question Study", 20, bold=True)
    _text(page, 50, 280, "1. What is measured:", 17, bold=True)
    y = 320
    for line in ("The recorder logs every", "event in the workshop."):
        _text(page, 50, y, line, 11)
        y += 16
    y += 12
    _text(page, 50, y, "The first column begins", 11)
    y += 16
    _text(page, 50, y, "the thought", 11)

    _text(page, 330, 280, "and the second column", 11)
    _text(page, 330, 296, "completes it.", 11)
    _text(page, 330, 336, "2. Why does it matter?", 17, bold=True)
    y = 376
    for line in ("The question drives the", "design of the study."):
        _text(page, 330, y, line, 11)
        y += 16

    page = _paper_page(doc)
    _text(page, 72, 100, "3. How is it decided?", 17, bold=True)
    y = 140
    for line in ("The decision follows from", "the measured evidence."):
        _text(page, 72, y, line, 11)
        y += 16

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


def _make_paper_negatives():
    """F5: paragraph-model negatives — nothing here is a heading."""
    out = Path(__file__).parent / "test_paper_negatives.pdf"
    doc = fitz.open()

    page = _paper_page(doc)
    y = 100
    for line in ("The following lines should not", "become headings at all."):
        _text(page, 72, y, line, 11)
        y += 16
    y += 12
    _text(page, 72, y, "Stay awhile.", 11)
    y += 28
    _text(page, 72, y, "The river runs.", 11)
    y += 28
    _mixed(page, 72, y, [("A note about ", False), ("the most important", True)], 11)
    y += 16
    _mixed(page, 72, y, [("consideration of all", True), (".", False)], 11)
    y += 28
    _text(page, 90, y, "\u2022 first bullet;", 11)
    y += 16
    _text(page, 90, y, "\u2022 second bullet;", 11)
    y += 28
    _text(page, 72, y, "The following paragraph stands alone.", 11)
    y += 28
    _text(page, 100, y, "This line is indented like a quotation.", 11)
    y += 28
    _text(page, 72, y, "This bold sentence is not a heading.", 11, bold=True)
    y += 28
    _mixed(page, 72, y, [("The paragraph continues here and then ", False), ("No way this becomes", True)], 11)
    y += 16
    _mixed(page, 72, y, [("a heading at all", True), (" and the text moves on.", False)], 11)

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


def _make_borderless():
    """A synthetic PDF exercising borderless (whitespace-only) table detection (ADR-0054).

    Page 1 holds prose plus a 3-column table with no ruling lines, inserted line
    by line (``insert_text`` writes one span per line, so this also covers the
    producer style where a cell's wrapped lines arrive as separate spans). The
    table has a header without a first-column cell, a wrapped three-line cell,
    and two adjacent single-line rows at the same 17pt pitch as the wrapped
    lines — geometrically indistinguishable from a wrapped cell, separated only
    by the content-stream order. Page 2 holds a ruled table: the existing
    ``find_tables()`` path must keep winning over borderless detection.
    """
    out = Path(__file__).parent / "test_borderless.pdf"
    doc = fitz.open()

    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Borderless Tables", fontsize=16)
    page.insert_text((72, 100), "The table below has no ruling lines at all.", fontsize=11)
    # Header row: the first column's cell is empty; the headers sit centred
    # over the body columns (their x0 does not match any body cell's x0).
    page.insert_text((290, 140), "Current process", fontsize=11)
    page.insert_text((470, 140), "Model A", fontsize=11)
    # Row 1: a wrapped three-line cell, centred against the tall row.
    page.insert_text((72, 176), "Main approach", fontsize=11)
    page.insert_text((250, 158), "Scheduled maintenance", fontsize=11)
    page.insert_text((250, 175), "+ warning signs +", fontsize=11)
    page.insert_text((250, 192), "engineer judgement", fontsize=11)
    page.insert_text((430, 175), "Simpler model", fontsize=11)
    # Row 2: a wrapped two-line label, 17pt pitch inside the cell.
    page.insert_text((72, 215), "Bus age, mileage and", fontsize=11)
    page.insert_text((72, 232), "service history", fontsize=11)
    page.insert_text((250, 223), "Yes", fontsize=11)
    page.insert_text((430, 223), "Yes", fontsize=11)
    # Rows 3-4: adjacent single-line rows at the same 17pt pitch as the
    # wrapped lines above.
    page.insert_text((72, 258), "Previous repairs/faults", fontsize=11)
    page.insert_text((250, 258), "Used manually", fontsize=11)
    page.insert_text((430, 258), "Yes", fontsize=11)
    page.insert_text((72, 275), "Warning codes", fontsize=11)
    page.insert_text((250, 275), "Used manually", fontsize=11)
    page.insert_text((430, 275), "Yes", fontsize=11)
    page.insert_text((72, 325), "Technical complexity", fontsize=11)
    page.insert_text((250, 325), "Low", fontsize=11)
    page.insert_text((430, 325), "Higher", fontsize=11)
    page.insert_text((72, 380), "Both models update their risk score every evening.", fontsize=11)

    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Ruled Table", fontsize=16)
    page.draw_rect(fitz.Rect(72, 120, 360, 240))
    page.draw_line(fitz.Point(72, 160), fitz.Point(360, 160))
    page.draw_line(fitz.Point(72, 200), fitz.Point(360, 200))
    page.draw_line(fitz.Point(216, 120), fitz.Point(216, 240))
    page.insert_text((90, 145), "Ruled A", fontsize=11)
    page.insert_text((234, 145), "Ruled B", fontsize=11)
    page.insert_text((90, 185), "one", fontsize=11)
    page.insert_text((234, 185), "two", fontsize=11)
    page.insert_text((90, 225), "three", fontsize=11)
    page.insert_text((234, 225), "four", fontsize=11)

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


def main():
    out = Path(__file__).parent / "test_doc.pdf"
    img = Path(__file__).parent / "test_image.png"
    make_png(img)

    doc = fitz.open()

    page = _new_page(doc)
    page.insert_text((72, 72), "Sample PDF Page", fontsize=20)
    page.insert_text((72, 120), "Plain paragraph text.", fontsize=12)
    page.insert_text((72, 160), "Line one of two.", fontsize=12)
    page.insert_text((72, 180), "Line two of two.", fontsize=12)
    page.insert_image(fitz.Rect(72, 240, 208, 336), filename=str(img))
    _footer(page)

    page = _new_page(doc)
    page.insert_text((72, 72), "Second Page", fontsize=16)
    page.insert_text((72, 110), "Some more body text.", fontsize=12)
    _footer(page)

    page = _new_page(doc)
    page.insert_text((72, 72), "Bullets", fontsize=20)
    page.insert_text((72, 100), "\u2022", fontsize=12)
    page.insert_text((90, 100), "First bullet item", fontsize=12)
    page.insert_text((72, 120), "\u2022 Second bullet inline", fontsize=12)
    _footer(page)

    page = _new_page(doc)
    page.insert_text((72, 72), "Formatting", fontsize=20)
    page.insert_text((72, 100), "\u2013", fontsize=12)
    page.insert_text((80, 100), " ", fontname="hebo", fontsize=12)
    page.insert_text((88, 100), "Enterprise Modeling", fontname="hebo", fontsize=12)
    _footer(page)

    doc.save(out)
    doc.close()
    print(f"wrote {out}")
    _make_paper()
    _make_borderless()
    _make_paper_fauxbold()
    _make_paper_boldonly()
    _make_paper_sizeonly()
    _make_paper_punct_columns()
    _make_paper_negatives()


if __name__ == "__main__":
    main()
