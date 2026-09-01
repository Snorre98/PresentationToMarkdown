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


if __name__ == "__main__":
    main()
