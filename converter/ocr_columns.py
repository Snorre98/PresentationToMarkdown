"""Deterministic column-sliced OCR for hard/scanned PDF pages.

When a page's text layer cannot be linearized (complex scatter, or a pure scan),
the converter hands the rendered page to the vision model. Doing that in one
shot scrambles multi-column reading order: a two-column paper comes back as
interleaved fragments. This module instead:

1. detects vertical column bands deterministically (text-layer coverage profile,
   falling back to an image ink-projection when the text layer is absent),
2. renders each band as its own clip-rect image,
3. OCRs each band with a column-specific prompt,
4. reassembles the chunks left-to-right into one linear Markdown stream.

Everything here is deterministic except the OCR text itself (the model output).
All AI calls are gated on ``config.is_enabled("vision")`` and never raise, so
conversion still cannot fail.
"""
from __future__ import annotations

import time

import pymupdf as fitz

from converter import config
from converter.base import image_digest
from converter.classify import (
    transcribe_column_cached,
    transcribe_complex_page,
    transcribe_page_cached,
)
from converter.logstore import record
from converter.vision import (
    VISION_BASE_URL,
    VISION_MODEL,
    _words,
    transcription_quality,
    verify_no_omissions,
)

# Render scale for OCR slices (the 2x full-page PNG stays the visual ground truth;
# this is tunable to trade latency/memory for fidelity).
OCR_SLICE_SCALE = 2.0

# Column detection (coverage-profile based). See detect_text_bands.
MIN_TEXT_LINES = 6  # below this the page is treated as having no usable text layer
_COLUMN_STEP = 2.0
_COLUMN_GUTTER_COV = 0.02
_COLUMN_MIN_LINES = 3
_COLUMN_MIN_SPAN = 0.07
_COLUMN_LEFT_MAX_X0 = 0.18
_COLUMN_RIGHT_MIN_X0 = 0.35
_COLUMN_MIN_COVERAGE = 0.5

# Ink-projection column detection (see detect_ink_bands).
_INK_SCALE = 0.5  # low-res full-page render
_INK_WHITE = 250  # pixels >= this grey level count as background
_INK_GUTTER_FRAC = 0.02  # a column x is "gutter" when its ink < 2% of the max
_INK_MIN_BAND_WIDTH = 0.15  # a band must span at least 15% of the page width

# Slicing / reassembly.
_GUTTER_PAD = 6.0  # points added to each side of a clip rect (edge glyphs)
_FULLWIDTH_TITLE_TOP = 0.28  # full-width title search region: top 28% of the page

# Per-column fidelity gate: reject a chunk when more than this fraction of a
# column's deterministic content words are missing from the transcription.
_MAX_OMISSION_FRACTION = 0.5


def _usage_counts(usage) -> tuple[int | None, int | None]:
    if not usage:
        return None, None
    prompt = usage.get("prompt_tokens")
    generated = usage.get("completion_tokens", usage.get("generated_tokens"))
    return prompt, generated


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def detect_text_bands(
    bboxes: list[tuple[float, float, float, float]],
    page_width: float,
    page_height: float,
) -> list[tuple[float, float]]:
    """Return the page's side-by-side text column x-bands (points), sorted by x0.

    Builds a vertical coverage profile (for each x, the summed height of every
    line whose x-extent contains x) and treats low-coverage corridors as gutters;
    the bands between gutters are the columns. A single-column page returns
    ``[(0.0, page_width)]``; a page with too little text to trust returns ``[]``
    (the caller then falls back to image ink-projection).
    """
    if len(bboxes) < MIN_TEXT_LINES:
        return []
    step = _COLUMN_STEP
    n = max(1, int(page_width / step))
    cov = [0.0] * n
    for x0, y0, x1, y1 in bboxes:
        i0 = max(0, int(x0 / step))
        i1 = min(n - 1, int(x1 / step))
        h = max(y1 - y0, 1.0)
        for i in range(i0, i1 + 1):
            cov[i] += h
    max_cov = max(cov)
    if max_cov <= 0.0:
        return []
    threshold = max_cov * _COLUMN_GUTTER_COV
    bands: list[tuple[float, float]] = []
    band_start = 0
    in_gutter = cov[0] < threshold
    for i in range(1, n):
        if cov[i] < threshold and not in_gutter:
            bands.append((band_start * step, i * step))
            in_gutter = True
        elif cov[i] >= threshold and in_gutter:
            in_gutter = False
            band_start = i
    if not in_gutter and (bands or n > 1):
        bands.append((band_start * step, page_width))

    valid: list[tuple[float, float]] = []
    for b0, b1 in bands:
        band_bboxes = [b for b in bboxes if b0 <= b[0] <= b1]
        if len(band_bboxes) < _COLUMN_MIN_LINES:
            continue
        ys = [b[1] for b in band_bboxes]
        if max(ys) - min(ys) < _COLUMN_MIN_SPAN * page_height:
            continue
        valid.append((b0, b1))
    if len(valid) < 2:
        return [(0.0, page_width)]
    valid.sort(key=lambda b: b[0])
    if valid[0][0] > _COLUMN_LEFT_MAX_X0 * page_width:
        return [(0.0, page_width)]
    if valid[-1][0] < _COLUMN_RIGHT_MIN_X0 * page_width:
        return [(0.0, page_width)]
    total = sum(len([b for b in bboxes if b0 <= b[0] <= b1]) for b0, b1 in valid)
    if total < _COLUMN_MIN_COVERAGE * len(bboxes):
        return [(0.0, page_width)]
    return valid


def detect_ink_bands(
    page, page_width: float, page_height: float
) -> list[tuple[float, float]]:
    """Detect column bands from a low-res ink projection of the rendered page.

    Used when the page has no (or a garbage) text layer — a pure scan. Renders
    the full page once at low resolution, sums non-white ("ink") pixels per x,
    and treats low-ink vertical corridors as gutters. Deterministic for a given
    page and scale.
    """
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(_INK_SCALE, _INK_SCALE), colorspace=fitz.csGRAY)
    except Exception:
        return []
    width, height = pix.width, pix.height
    if width < 2 or height < 2:
        return []
    samples = pix.samples
    stride = pix.stride
    ink = [0] * width
    for y in range(height):
        row = y * stride
        for x in range(width):
            if samples[row + x] < _INK_WHITE:
                ink[x] += 1
    max_ink = max(ink)
    if max_ink <= 0:
        return []
    threshold = max_ink * _INK_GUTTER_FRAC
    bands: list[tuple[float, float]] = []
    band_start = 0
    in_gutter = ink[0] < threshold
    for x in range(1, width):
        if ink[x] < threshold and not in_gutter:
            bands.append((band_start, x))
            in_gutter = True
        elif ink[x] >= threshold and in_gutter:
            in_gutter = False
            band_start = x
    if not in_gutter and (bands or width > 1):
        bands.append((band_start, width))

    pt_per_px = page_width / width
    valid: list[tuple[float, float]] = []
    for b0, b1 in bands:
        w = (b1 - b0) * pt_per_px
        if w < _INK_MIN_BAND_WIDTH * page_width:
            continue
        valid.append((b0 * pt_per_px, b1 * pt_per_px))
    if len(valid) < 2:
        return []
    return valid


def detect_bands(
    page,
    bboxes: list[tuple[float, float, float, float]],
    page_width: float,
    page_height: float,
) -> list[tuple[float, float]]:
    """Return the page's column x-bands, preferring the text layer over ink.

    A single column (or a page too sparse to slice) collapses to one full-width
    band ``[(0.0, page_width)]``.
    """
    bands = detect_text_bands(bboxes, page_width, page_height)
    if len(bands) >= 2:
        return bands
    if len(bboxes) < MIN_TEXT_LINES:
        ink = detect_ink_bands(page, page_width, page_height)
        if len(ink) >= 2:
            return ink
    return [(0.0, page_width)]


def _pad_bands(bands: list[tuple[float, float]], page_width: float) -> list[tuple[float, float]]:
    """Expand each band a few points on both sides, clamped to page bounds and
    capped at half the gutter so edge glyphs survive without bleeding into the
    neighbouring column."""
    padded: list[tuple[float, float]] = []
    for i, (b0, b1) in enumerate(bands):
        left = _GUTTER_PAD if i == 0 else min(_GUTTER_PAD, (b0 - bands[i - 1][1]) / 2.0)
        right = _GUTTER_PAD if i == len(bands) - 1 else min(_GUTTER_PAD, (bands[i + 1][0] - b1) / 2.0)
        x0 = max(0.0, b0 - max(0.0, left))
        x1 = min(page_width, b1 + max(0.0, right))
        padded.append((x0, x1))
    return padded


def render_band(page, rect: tuple[float, float, float, float], scale: float = OCR_SLICE_SCALE) -> bytes:
    """Clip-render one band of the page and return it as PNG bytes."""
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(*rect))
    return pix.tobytes("png")


def _full_width_title(
    text_lines: list[tuple[str, tuple[float, float, float, float]]],
    bands: list[tuple[float, float]],
    page_width: float,
    page_height: float,
) -> tuple[list[tuple[str, tuple[float, float, float, float]]], tuple[float, float, float, float] | None]:
    """Split off full-width title/heading lines (top of page, spanning the gutters).

    Returns ``(remaining_lines, title_rect)``; ``title_rect`` is ``None`` when no
    full-width title block is found. The title is OCR'd as its own full-width
    slice so it is never sliced in half.
    """
    if len(bands) < 2:
        return text_lines, None
    left = bands[0][0]
    right = bands[-1][1]
    title_lines = [
        (text, bbox)
        for text, bbox in text_lines
        if bbox[1] < page_height * _FULLWIDTH_TITLE_TOP
        and bbox[0] <= left + _GUTTER_PAD
        and bbox[2] >= right - _GUTTER_PAD
    ]
    if not title_lines:
        return text_lines, None
    title_ids = {id(bbox) for _, bbox in title_lines}
    remaining = [(text, bbox) for text, bbox in text_lines if id(bbox) not in title_ids]
    x0 = min(bbox[0] for _, bbox in title_lines)
    y0 = min(bbox[1] for _, bbox in title_lines)
    x1 = max(bbox[2] for _, bbox in title_lines)
    y1 = max(bbox[3] for _, bbox in title_lines)
    rect = (
        max(0.0, x0 - _GUTTER_PAD),
        max(0.0, y0 - _GUTTER_PAD),
        min(page_width, x1 + _GUTTER_PAD),
        min(page_height, y1 + _GUTTER_PAD),
    )
    return remaining, rect


def _full_width_tables(
    tables: list[object] | None,
    bands: list[tuple[float, float]],
    page_width: float,
    page_height: float,
) -> list[tuple[float, float, float, float]]:
    """Return full-width table clip rects (tables whose bbox spans the gutters)."""
    if not tables or len(bands) < 2:
        return []
    left = bands[0][0]
    right = bands[-1][1]
    rects: list[tuple[float, float, float, float]] = []
    for table in tables:
        x0, y0, x1, y1 = table.bbox
        if x0 <= left + _GUTTER_PAD and x1 >= right - _GUTTER_PAD:
            rects.append(
                (
                    max(0.0, x0 - _GUTTER_PAD),
                    max(0.0, y0 - _GUTTER_PAD),
                    min(page_width, x1 + _GUTTER_PAD),
                    min(page_height, y1 + _GUTTER_PAD),
                )
            )
    return rects


def _column_text(
    text_lines: list[tuple[str, tuple[float, float, float, float]]],
    b0: float,
    b1: float,
) -> str:
    """The deterministic text of the lines whose x0 falls in ``[b0, b1]``."""
    return "\n".join(text for text, bbox in text_lines if b0 <= bbox[0] <= b1)


def _fidelity_failure(raw_text: str, markdown: str) -> bool:
    """Return True when a chunk omits too many of its column's content words."""
    missing = verify_no_omissions(raw_text, markdown)
    if not missing:
        return False
    words = _words(raw_text)
    if not words:
        return False
    return len(missing) / len(words) > _MAX_OMISSION_FRACTION


def transcribe_columns(
    page,
    text_lines: list[tuple[str, tuple[float, float, float, float]]],
    page_width: float,
    page_height: float,
    page_png: bytes,
    tables: list[object] | None = None,
    warnings: list[str] | None = None,
    log_ctx: dict | None = None,
    width: int | None = None,
    height: int | None = None,
    bands: list[tuple[float, float]] | None = None,
) -> str | None:
    """OCR a hard page by slicing it into column bands and reassembling them.

    For a single-column page this delegates to whole-page OCR (the existing
    ``transcribe_complex_page`` behaviour). For >=2 bands, each band is rendered
    and transcribed with a column prompt; the chunks are concatenated
    left-to-right. A chunk that fails the quality/fidelity gates falls back to
    whole-page OCR, and that falling back to ``None`` (the caller then keeps its
    raw-text block). Never raises; returns ``None`` when disabled or degenerate.
    """
    if not config.is_enabled("vision"):
        return None
    ctx = log_ctx or {}
    bboxes = [bbox for _, bbox in text_lines]
    if bands is None:
        bands = detect_bands(page, bboxes, page_width, page_height)
    if len(bands) < 2:
        return transcribe_complex_page(
            page_png,
            warnings=warnings,
            log_ctx=ctx,
            width=width,
            height=height,
        )

    remaining, title_rect = _full_width_title(text_lines, bands, page_width, page_height)
    table_rects = _full_width_tables(tables, bands, page_width, page_height)
    padded = _pad_bands(bands, page_width)

    chunks: list[str] = []
    # (rect, prompt_kind, band_index) in reading order: title, columns, tables.
    slices: list[tuple[tuple[float, float, float, float], str, int | None]] = []
    if title_rect is not None:
        slices.append((title_rect, "page", None))
    for bi, (x0, x1) in enumerate(padded):
        slices.append(((x0, 0.0, x1, page_height), "column", bi))
    for rect in table_rects:
        slices.append((rect, "page", None))

    for idx, (rect, kind, bi) in enumerate(slices):
        png = render_band(page, rect)
        digest = image_digest(png)
        t0 = time.perf_counter()
        try:
            if kind == "column":
                markdown, usage = transcribe_column_cached(png, return_usage=True)
            else:
                markdown, usage = transcribe_page_cached(png, return_usage=True)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            if warnings is not None:
                warnings.append(f"Vision column transcription failed: {exc}")
            record(
                source=ctx.get("source", ""),
                page=ctx.get("page"),
                image_ref=f"slice_{idx}",
                image_digest=digest,
                stage="transcribe",
                model=VISION_MODEL,
                latency_ms=_ms(t0),
                error=str(exc),
                base_url=VISION_BASE_URL,
            )
            return transcribe_complex_page(
                page_png, warnings=warnings, log_ctx=ctx, width=width, height=height
            )
        prompt_tokens, generated_tokens = _usage_counts(usage)

        reason = transcription_quality(markdown)
        omitted: list[str] = []
        if reason is None and kind == "column":
            b0, b1 = bands[bi]
            raw = _column_text(remaining, b0, b1)
            omitted = verify_no_omissions(raw, markdown)
            if _fidelity_failure(raw, markdown):
                reason = "omissions"

        if reason is not None:
            if warnings is not None:
                warnings.append(f"Discarding low-value column transcription ({reason})")
            record(
                source=ctx.get("source", ""),
                page=ctx.get("page"),
                image_ref=f"slice_{idx}",
                image_digest=digest,
                stage="transcribe",
                model=VISION_MODEL,
                latency_ms=_ms(t0),
                prompt_tokens=prompt_tokens,
                generated_tokens=generated_tokens,
                markdown=markdown,
                omitted_words=omitted or None,
                error=f"quality gate: {reason}",
                base_url=VISION_BASE_URL,
            )
            return transcribe_complex_page(
                page_png, warnings=warnings, log_ctx=ctx, width=width, height=height
            )

        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_ref=f"slice_{idx}",
            image_digest=digest,
            stage="transcribe",
            model=VISION_MODEL,
            latency_ms=_ms(t0),
            prompt_tokens=prompt_tokens,
            generated_tokens=generated_tokens,
            markdown=markdown,
            omitted_words=omitted or None,
            base_url=VISION_BASE_URL,
        )
        chunks.append(markdown)

    return "\n\n".join(chunk.rstrip() for chunk in chunks if chunk.strip())
