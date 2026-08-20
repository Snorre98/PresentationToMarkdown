"""Generate a synthetic .pdf exercising the PDF converter's layout features."""
from pathlib import Path

import pymupdf as fitz

from tests.make_test_deck import make_png

FOOTER = "Sample Footer 2026"

PAGE_W, PAGE_H = 720, 540


def _footer(page):
    page.insert_text((72, PAGE_H - 40), FOOTER, fontsize=11)


def _new_page(doc):
    return doc.new_page(width=PAGE_W, height=PAGE_H)


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


if __name__ == "__main__":
    main()
