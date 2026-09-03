"""Tests for deterministic column-sliced OCR (converter.ocr_columns)."""
from __future__ import annotations

import pytest

from converter import config, ocr_columns
from converter.ocr_columns import detect_ink_bands, detect_text_bands, transcribe_columns


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    config.reset()
    monkeypatch.setattr(ocr_columns, "record", lambda **kw: None)
    yield
    config.reset()


class _FakePix:
    def __init__(self, width, height, ink_columns):
        self.width = width
        self.height = height
        self.stride = width
        self.samples = bytes(
            (0 if x in ink_columns else 255)
            for _ in range(height)
            for x in range(width)
        )


class _FakePage:
    def __init__(self, pix):
        self._pix = pix

    def get_pixmap(self, matrix=None, colorspace=None):
        return self._pix


def _two_col_lines():
    lines = []
    for i in range(4):
        lines.append(("", (50.0, 220.0 + 30 * i, 300.0, 235.0 + 30 * i)))
    for i in range(4):
        lines.append(("", (330.0, 220.0 + 30 * i, 580.0, 235.0 + 30 * i)))
    return lines


def _capture_render(rects):
    def _render(page, rect, scale=ocr_columns.OCR_SLICE_SCALE):
        rects.append(rect)
        return b"png"
    return _render


# --- band detection ---------------------------------------------------------


def test_detect_text_bands_two_columns():
    bboxes = [bbox for _, bbox in _two_col_lines()]
    bands = detect_text_bands(bboxes, 612.0, 792.0)
    assert len(bands) == 2
    assert bands[0][0] < bands[1][0]


def test_detect_text_bands_single_column():
    bboxes = [(50.0, 220.0 + 30 * i, 300.0, 235.0 + 30 * i) for i in range(6)]
    assert detect_text_bands(bboxes, 612.0, 792.0) == [(0.0, 612.0)]


def test_detect_text_bands_sparse_returns_empty():
    assert detect_text_bands([(50.0, 220.0, 300.0, 235.0)], 612.0, 792.0) == []


def test_detect_ink_bands_two_columns():
    page = _FakePage(_FakePix(100, 50, set(range(20, 40)) | set(range(60, 80))))
    bands = detect_ink_bands(page, 612.0, 792.0)
    assert len(bands) == 2
    pt = 612.0 / 100.0
    assert bands[0] == pytest.approx((20 * pt, 40 * pt))
    assert bands[1] == pytest.approx((60 * pt, 80 * pt))


def test_detect_ink_bands_single_column():
    page = _FakePage(_FakePix(100, 50, set(range(30, 70))))
    assert detect_ink_bands(page, 612.0, 792.0) == []


# --- slicing + reassembly ---------------------------------------------------


def test_two_bands_two_ocr_calls_in_left_to_right_order(monkeypatch):
    config.set_enabled("vision", True)
    rects: list[tuple[float, float, float, float]] = []
    monkeypatch.setattr(ocr_columns, "render_band", _capture_render(rects))
    outputs = ["COLUMN ZERO", "COLUMN ONE"]
    monkeypatch.setattr(
        ocr_columns, "transcribe_column_cached", lambda png, **kw: (outputs.pop(0), None)
    )
    result = transcribe_columns(
        None, _two_col_lines(), 612.0, 792.0, b"pagepng",
        bands=[(50.0, 320.0), (320.0, 612.0)],
    )
    assert result == "COLUMN ZERO\n\nCOLUMN ONE"
    assert rects == [(44.0, 0.0, 320.0, 792.0), (320.0, 0.0, 612.0, 792.0)]


def test_single_band_delegates_to_whole_page(monkeypatch):
    config.set_enabled("vision", True)
    calls: list[bytes] = []
    monkeypatch.setattr(
        ocr_columns, "transcribe_complex_page",
        lambda png, **kw: calls.append(png) or "WHOLE PAGE",
    )
    monkeypatch.setattr(
        ocr_columns, "transcribe_column_cached",
        lambda png, **kw: (_ for _ in ()).throw(AssertionError("must not slice")),
    )
    result = transcribe_columns(None, [], 612.0, 792.0, b"pagepng", bands=[(0.0, 612.0)])
    assert result == "WHOLE PAGE"
    assert calls == [b"pagepng"]


def test_full_width_title_and_table_are_separate_slices(monkeypatch):
    config.set_enabled("vision", True)
    title = ("The Great Title", (50.0, 100.0, 610.0, 120.0))
    lines = [title] + _two_col_lines()

    class _Table:
        def __init__(self, bbox):
            self.bbox = bbox

    tables = [_Table((50.0, 500.0, 610.0, 560.0))]
    rendered: list[tuple[float, float, float, float]] = []
    monkeypatch.setattr(ocr_columns, "render_band", _capture_render(rendered))
    kinds: list[str] = []
    monkeypatch.setattr(
        ocr_columns, "transcribe_column_cached", lambda png, **kw: kinds.append("col") or ("COL", None)
    )
    monkeypatch.setattr(
        ocr_columns, "transcribe_page_cached", lambda png, **kw: kinds.append("page") or ("PAGE", None)
    )
    result = transcribe_columns(
        None, lines, 612.0, 792.0, b"pagepng", tables=tables,
        bands=[(50.0, 320.0), (320.0, 612.0)],
    )
    assert kinds == ["page", "col", "col", "page"]
    assert result == "PAGE\n\nCOL\n\nCOL\n\nPAGE"
    assert len(rendered) == 4


def test_quality_gate_failure_falls_back_to_whole_page(monkeypatch):
    config.set_enabled("vision", True)
    monkeypatch.setattr(ocr_columns, "render_band", lambda page, rect: b"png")
    monkeypatch.setattr(ocr_columns, "transcribe_column_cached", lambda png, **kw: ("", None))
    calls: list[bytes] = []
    monkeypatch.setattr(
        ocr_columns, "transcribe_complex_page",
        lambda png, **kw: calls.append(png) or "WHOLE",
    )
    result = transcribe_columns(
        None, _two_col_lines(), 612.0, 792.0, b"pagepng",
        bands=[(50.0, 320.0), (320.0, 612.0)],
    )
    assert result == "WHOLE"
    assert calls == [b"pagepng"]


def test_fidelity_gate_omissions_falls_back_to_whole_page(monkeypatch):
    config.set_enabled("vision", True)
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    lines = [
        (" ".join(words), (50.0, 220.0 + 30 * i, 300.0, 235.0 + 30 * i)) for i in range(3)
    ] + [("", (330.0, 220.0 + 30 * i, 580.0, 235.0 + 30 * i)) for i in range(3)]
    monkeypatch.setattr(ocr_columns, "render_band", lambda page, rect: b"png")
    monkeypatch.setattr(
        ocr_columns, "transcribe_column_cached", lambda png, **kw: ("totally unrelated", None)
    )
    monkeypatch.setattr(ocr_columns, "transcribe_complex_page", lambda png, **kw: "WHOLE")
    result = transcribe_columns(
        None, lines, 612.0, 792.0, b"pagepng", bands=[(50.0, 320.0), (320.0, 612.0)]
    )
    assert result == "WHOLE"


def test_disabled_is_noop(monkeypatch):
    config.set_enabled("vision", False)
    monkeypatch.setattr(
        ocr_columns, "transcribe_column_cached",
        lambda png, **kw: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    assert transcribe_columns(None, [], 612.0, 792.0, b"pagepng") is None


# --- pdf.py wiring: a pure-scan page routes into the column OCR path ---------


def _make_scanned_two_column_pdf(path):
    import pymupdf as fitz

    src = fitz.open()
    sp = src.new_page(width=612, height=792)
    for i in range(10):
        sp.insert_text((50, 220 + 20 * i), "left column text row", fontsize=10)
        sp.insert_text((330, 220 + 20 * i), "right column text row", fontsize=10)
    pix = sp.get_pixmap(matrix=fitz.Matrix(1, 1))
    img = path.parent / "scan_columns.png"
    pix.save(str(img))
    src.close()

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(0, 0, 612, 792), filename=str(img))
    doc.save(path)
    doc.close()


def test_scanned_page_routes_to_column_ocr(tmp_path, monkeypatch):
    import converter.pdf as pdf
    from converter import convert_file

    config.set_enabled("vision", True)
    path = tmp_path / "scan.pdf"
    _make_scanned_two_column_pdf(path)
    monkeypatch.setattr(
        pdf, "transcribe_columns",
        lambda *a, **kw: "LEFT COLUMN\n\nRIGHT COLUMN",
    )
    result = convert_file(path, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "LEFT COLUMN" in text
    assert "RIGHT COLUMN" in text


def test_scanned_page_vision_off_unchanged(tmp_path):
    from converter import convert_file

    config.set_enabled("vision", False)
    path = tmp_path / "scan.pdf"
    _make_scanned_two_column_pdf(path)
    result = convert_file(path, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "LEFT COLUMN" not in text
    assert "Raw extracted text" not in text


def _make_scanned_garbage_pdf(path):
    """A full-page scan image plus a *garbage* (non-empty) text layer.

    The text layer repeats one token many times so its unique-word ratio is below
    the usable threshold (ADR-0020) — the exact case that used to be emitted as a
    duplicate alongside the full-page image transcription."""
    import pymupdf as fitz

    src = fitz.open()
    sp = src.new_page(width=612, height=792)
    for i in range(10):
        sp.insert_text((50, 220 + 20 * i), "left column text row", fontsize=10)
        sp.insert_text((330, 220 + 20 * i), "right column text row", fontsize=10)
    pix = sp.get_pixmap(matrix=fitz.Matrix(1, 1))
    img = path.parent / "scan_garbage.png"
    pix.save(str(img))
    src.close()

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(0, 0, 612, 792), filename=str(img))
    for i in range(40):
        page.insert_text((50, 100 + i * 15), "xxxx xxxx xxxx xxxx", fontsize=6)
    doc.save(path)
    doc.close()


def test_garbage_text_layer_routes_to_vision_and_skips_fullpage_image(tmp_path, monkeypatch):
    import converter.pdf as pdf
    from converter import convert_file

    config.set_enabled("vision", True)
    path = tmp_path / "scan_garbage.pdf"
    _make_scanned_garbage_pdf(path)
    image_calls: list[int] = []
    monkeypatch.setattr(
        pdf, "maybe_transcribe_image",
        lambda *a, **kw: image_calls.append(1) or "IMAGE TRANSCRIPTION",
    )
    monkeypatch.setattr(pdf, "transcribe_columns", lambda *a, **kw: "CLEAN PAGE TRANSCRIPTION")
    result = convert_file(path, tmp_path)
    assert result.error is None
    text = result.md_path.read_text(encoding="utf-8")
    assert "CLEAN PAGE TRANSCRIPTION" in text
    assert "xxxx" not in text
    assert "IMAGE TRANSCRIPTION" not in text
    assert image_calls == []
