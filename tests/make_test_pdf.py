"""Generate a synthetic .pdf exercising the PDF converter."""
from pathlib import Path

import pymupdf as fitz

from tests.make_test_deck import make_png


def main():
    out = Path(__file__).parent / "test_doc.pdf"
    img = Path(__file__).parent / "test_image.png"
    make_png(img)

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Sample PDF Page", fontsize=20)
    page.insert_text((72, 120), "Plain paragraph text.", fontsize=12)
    page.insert_text((72, 160), "Line one of two.", fontsize=12)
    page.insert_text((72, 180), "Line two of two.", fontsize=12)
    page.insert_image(fitz.Rect(72, 240, 208, 336), filename=str(img))

    page2 = doc.new_page()
    page2.insert_text((72, 72), "Second Page", fontsize=16)
    page2.insert_text((72, 110), "Some more body text.", fontsize=12)

    doc.save(out)
    doc.close()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
