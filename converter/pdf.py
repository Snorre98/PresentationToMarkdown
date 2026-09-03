"""PDF -> Markdown converter built on PyMuPDF.

The converter reconstructs a page's visual layout from per-span coordinates:

- every page is rendered to a PNG (the visual ground truth, kept even where the
  text layer can't be linearized),
- the shared slide-master background image is detected and skipped,
- text lines are reordered by ``(row, x)`` and formatted into bullets, paragraphs
  and pipe tables,
- footers and slide numbers are dropped (they repeat on every page),
- "complex" pages (diagrams / multi-column flowcharts) fall back to the rendered
  image, optionally with a local vision-model transcription.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from converter import config
from converter.base import (
    ConvertResult,
    Converter,
    PageProgressCallback,
    _format_md,
    _link_dest,
    _table_to_md,
    image_digest,
    repeated_image_hashes,
    text_layer_quality,
    write_image,
)
from converter.classify import maybe_transcribe_image
from converter.interpret import interpret_diagram
from converter.logstore import current_run_id, phase
from converter.ocr_columns import detect_bands, detect_text_bands, transcribe_columns
from converter.structure import PageData, structure_paper

_BOLD_FLAG = 2**4
_ITALIC_FLAG = 2**1

# Bullet glyphs at line start: bullet, heavy bullets, en/em dashes.
_BULLET_RE = re.compile(r"^\s*([\u2022\u2023\u2043\u25AA\u25CF\u25CB\u00B7\u2013\u2014])\s*")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}$")
_LEADING_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s+")

_TITLE_MIN_SIZE = 16.0
_FOOTER_MAX_SIZE = 16.0
_FOOTER_BOTTOM_BAND = 70.0
_ROW_TOL = 6.0
_COMPLEX_DISTINCT_X = 8

# Full-page image detection (ADR-0020): an embedded image covering at least this
# fraction of the page's width and height is treated as "the page" itself and is
# handled by the page-level path, never transcribed as a separate figure.
_FULLPAGE_MIN_WIDTH_FRAC = 0.95
_FULLPAGE_MIN_HEIGHT_FRAC = 0.90

# Paper mode heuristics.
_HEADING_MAX_LEN = 40
_HEADING_CENTER_TOL = 15.0  # heading must be centered within this many pt of its column
_HEADING_FLUSH_TOL = 5.0  # ... or start flush with the column's left edge
_TITLE_BLOCK_TOP = 0.28  # title block search area: top 28% of page 1
_TOP_BAND_FRAC = 0.09  # running-header band at the top of each page


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    spans: list[dict]
    size: float
    bold: bool
    italic: bool


def _spans_to_md(spans: list[dict]) -> str:
    """Join spans preserving bold/italic, merging same-flag runs and trimming
    whitespace out of the ``**``/``*`` markers (fixes ``** ****word**``)."""
    runs: list[list] = []
    for span in spans:
        bold = bool(span["flags"] & _BOLD_FLAG)
        italic = bool(span["flags"] & _ITALIC_FLAG)
        if runs and runs[-1][1] == bold and runs[-1][2] == italic:
            runs[-1][0] += span["text"]
        else:
            runs.append([span["text"], bold, italic])
    out: list[str] = []
    for text, bold, italic in runs:
        match = re.match(r"^(\s*)(.*?)(\s*)$", text, re.DOTALL)
        lead, core, trail = match.group(1), match.group(2), match.group(3)
        out.append(lead + _format_md(core, bold, italic) + trail)
    return "".join(out)


def _strip_bullet(md: str) -> tuple[str | None, str]:
    """Return (glyph, rest) if ``md`` starts with a bullet glyph, else (None, md)."""
    match = _BULLET_RE.match(md)
    if not match:
        return None, md
    return match.group(1), md[match.end():].lstrip()


def _page_lines(page) -> list[Line]:
    lines: list[Line] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            spans = ln.get("spans", [])
            text = "".join(span["text"] for span in spans)
            if not text.strip():
                continue
            lines.append(
                Line(
                    text=text,
                    bbox=tuple(ln["bbox"]),
                    spans=spans,
                    size=max((span["size"] for span in spans), default=0.0),
                    bold=any(span["flags"] & _BOLD_FLAG for span in spans),
                    italic=any(span["flags"] & _ITALIC_FLAG for span in spans),
                )
            )
    return lines


def _ordered_lines(lines: list[Line]) -> list[Line]:
    """Sort lines into visual reading order (top-to-bottom, left-to-right within a row)."""
    lines = sorted(lines, key=lambda l: (l.bbox[1], l.bbox[0]))
    rows: list[list[Line]] = []
    for line in lines:
        if rows and abs(line.bbox[1] - rows[-1][0].bbox[1]) <= _ROW_TOL:
            rows[-1].append(line)
        else:
            rows.append([line])
    ordered: list[Line] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda l: l.bbox[0]))
    return ordered


def _detect_title(lines: list[Line], page_height: float) -> tuple[str | None, set[int]]:
    """Return (title, line ids) — the largest-font text, minus page numbers/footers.

    Absorbs one or two wrapped continuation lines directly below (e.g. a title
    that breaks across lines at a slightly smaller font size)."""
    candidates = [
        line
        for line in lines
        if line.size >= _TITLE_MIN_SIZE and not _PAGE_NUMBER_RE.match(line.text.strip())
    ]
    if not candidates:
        return None, set()
    max_size = max(line.size for line in candidates)
    title_lines = [line for line in candidates if abs(line.size - max_size) < 0.5]
    if not title_lines or len(title_lines) > 4:
        return None, set()
    title_lines.sort(key=lambda l: l.bbox[1])
    absorbed = list(title_lines)
    title_ids = {id(line) for line in title_lines}
    ref_y = title_lines[-1].bbox[1]
    below = sorted(
        (line for line in candidates if id(line) not in title_ids and line.bbox[1] >= ref_y - _ROW_TOL),
        key=lambda l: l.bbox[1],
    )
    for line in below:
        if len(absorbed) >= 4:
            break
        if line.size >= 0.7 * max_size and line.bbox[3] <= page_height * 0.45:
            absorbed.append(line)
        else:
            break
    absorbed.sort(key=lambda l: l.bbox[1])
    text = " ".join(line.text.strip() for line in absorbed)
    text = _LEADING_NUMBER_RE.sub("", text).strip()
    return (text or None), {id(line) for line in absorbed}


def _title_block(lines: list[Line], page_width: float, page_height: float) -> tuple[str | None, str, list[Line]]:
    """Extract the paper's title, authors, and their lines from page 1.

    Looks for a contiguous run of centered lines at the top of the page (title,
    subtitle, authors/affiliations) above the first column content. Returns
    ``(title, authors, block_lines)``; ``authors`` may be empty.
    """
    top = [
        ln
        for ln in lines
        if ln.bbox[1] < page_height * _TITLE_BLOCK_TOP
        and ln.bbox[0] >= page_width * 0.25
        and ln.bbox[2] <= page_width * 0.80
    ]
    centered = sorted(top, key=lambda ln: ln.bbox[1])
    if not centered:
        return None, "", []
    heights = [max(ln.bbox[3] - ln.bbox[1], 1.0) for ln in centered]
    median_h = sorted(heights)[len(heights) // 2] if heights else 10.0
    run: list[Line] = [centered[0]]
    for ln in centered[1:]:
        if ln.bbox[1] - run[-1].bbox[1] <= 2.5 * median_h:
            run.append(ln)
        else:
            break
    if len(run) > 6:
        run = run[:6]
    if not run:
        return None, "", []
    title = run[0].text.strip()
    if len(run) > 1:
        title = f"{title} {run[1].text.strip()}"
    authors = " · ".join(ln.text.strip() for ln in run[2:])
    return title, authors, run


def _is_page_number(line: Line) -> bool:
    return (
        bool(_PAGE_NUMBER_RE.match(line.text.strip()))
        and line.size <= _FOOTER_MAX_SIZE
        and line.bbox[1] < 40.0
    )


def _footer_key(text: str) -> str:
    key = re.sub(r"\d", "", text).lower()
    return re.sub(r"\s+", " ", key).strip()


def _collect_footer_keys(doc) -> set[str]:
    """Bottom-band lines repeated on most pages — the per-page footer boilerplate."""
    from collections import Counter

    counts: Counter = Counter()
    for page in doc:
        for line in _page_lines(page):
            if line.bbox[1] >= page.rect.height - _FOOTER_BOTTOM_BAND and line.size <= _FOOTER_MAX_SIZE:
                counts[_footer_key(line.text)] += 1
    threshold = max(2, int(doc.page_count * 0.5))
    return {key for key, n in counts.items() if n >= threshold}


def _collect_top_keys(doc) -> set[str]:
    """Lines repeating at the top of most pages — the paper's running headers."""
    from collections import Counter

    counts: Counter = Counter()
    for page in doc:
        for line in _page_lines(page):
            if line.bbox[1] < page.rect.height * _TOP_BAND_FRAC and line.size <= _FOOTER_MAX_SIZE:
                counts[_footer_key(line.text)] += 1
    threshold = max(2, int(doc.page_count * 0.5))
    return {key for key, n in counts.items() if n >= threshold}


def _pdf_mode() -> bool:
    """Whether the PDF converter should render documents as multi-column papers."""
    return os.environ.get("PDF_MODE", "").strip().lower() == "paper"


def _background_xrefs(doc) -> set[int]:
    """Image xrefs that are full-page AND shared across pages (the slide-master background)."""
    info: dict[int, dict] = {}
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            info.setdefault(xref, {"pages": set(), "fullpage": True})
            info[xref]["pages"].add(page.number)
    shared = {xref for xref, d in info.items() if len(d["pages"]) > 1}
    result: set[int] = set()
    for xref in shared:
        fullpage = True
        for page in doc:
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            if not any(_is_fullpage(r, page.rect) for r in rects):
                fullpage = False
                break
        if fullpage:
            result.add(xref)
    return result


def _is_fullpage(rect, page_rect, tol: float = 2.0) -> bool:
    return (
        rect.x0 <= tol
        and rect.y0 <= tol
        and rect.x1 >= page_rect.width - tol
        and rect.y1 >= page_rect.height - tol
    )


def _is_fullpage_image(rect, page_rect) -> bool:
    """Whether an embedded image effectively covers the whole page (area ratio).

    The 2pt-tolerance :func:`_is_fullpage` misses off-by-margin scans (a scan
    clipped or offset a few points); area ratio is robust to that. Used to decide
    whether an image is "the page" — handled by the page-level vision/text path —
    rather than an embedded figure to transcribe separately.
    """
    return (
        rect.width >= _FULLPAGE_MIN_WIDTH_FRAC * page_rect.width
        and rect.height >= _FULLPAGE_MIN_HEIGHT_FRAC * page_rect.height
    )


def _collect_page_image_digests(doc, skip_xrefs: set[int]) -> list[set[str]]:
    """Return the set of image content digests present on each page.

    Each unique xref is extracted once and cached by content digest.
    """
    cache: dict[int, str | None] = {}
    per_page: list[set[str]] = []
    for page in doc:
        digests: set[str] = set()
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in skip_xrefs:
                continue
            if xref not in cache:
                try:
                    extracted = doc.extract_image(xref)
                    cache[xref] = image_digest(extracted["image"])
                except Exception:
                    cache[xref] = None
            digest = cache[xref]
            if digest:
                digests.add(digest)
        per_page.append(digests)
    return per_page


def _line_in_tables(line: Line, tables) -> bool:
    lr = fitz.Rect(line.bbox)
    for table in tables:
        tr = fitz.Rect(table.bbox)
        inter = lr & tr
        if not inter.is_empty and inter.get_area() >= lr.get_area() * 0.5:
            return True
    return False


def _page_images(page, doc, assets_dir, stem, counter, warnings, skip_xrefs, dedup, pno, repeated, seen, source) -> tuple[list[str], bytes, int, int]:
    refs: list[str] = []
    assets_dir.mkdir(parents=True, exist_ok=True)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    png_name = f"{stem}_page_{pno:02d}.png"
    pix.save(str(assets_dir / png_name))
    png_bytes = pix.tobytes("png")
    refs.append(f"[Page {pno}]({_link_dest(f'assets/{stem}/{png_name}')})")
    for img in page.get_images(full=True):
        xref = img[0]
        if xref in skip_xrefs:
            continue
        try:
            extracted = doc.extract_image(xref)
        except Exception as exc:
            warnings.append(f"Could not extract image: {exc}")
            continue
        digest = image_digest(extracted["image"])
        filename = write_image(
            extracted["image"],
            extracted.get("ext", "bin"),
            assets_dir,
            stem,
            counter,
            warnings,
            dedup,
        )
        if filename:
            rel = _link_dest(f'assets/{stem}/{filename}')
            if digest in repeated:
                if digest in seen:
                    refs.append(f"[image]({rel})")
                    continue
                seen.add(digest)
            refs.append(f"![image]({rel})")
            if any(_is_fullpage_image(r, page.rect) for r in page.get_image_rects(xref)):
                continue
            transcription = maybe_transcribe_image(
                extracted["image"],
                extracted.get("ext", "bin"),
                warnings,
                log_ctx={
                    "source": source,
                    "page": pno,
                    "image_ref": filename,
                    "image_digest": digest,
                },
                width=extracted.get("width"),
                height=extracted.get("height"),
            )
            if transcription:
                refs.extend(transcription.splitlines())
                refs.append("")
    return refs, png_bytes, pix.width, pix.height


def _merge_lone_bullets(items: list[dict]) -> list[dict]:
    """Fold a lone bullet glyph line into the text line that follows it on the same row.

    The bullet's own ``x0``/``y0`` are kept so the merged item is indented by the
    bullet's position, not the text's."""
    merged: list[dict] = []
    i = 0
    while i < len(items):
        item = items[i]
        glyph, rest = _strip_bullet(item["md"])
        if glyph and not rest and i + 1 < len(items) and abs(items[i + 1]["y0"] - item["y0"]) <= _ROW_TOL:
            nxt = items[i + 1]
            merged.append(
                {
                    **nxt,
                    "md": glyph + " " + nxt["md"].lstrip(),
                    "x0": item["x0"],
                    "y0": item["y0"],
                    "heading": False,
                }
            )
            i += 2
        else:
            merged.append(item)
            i += 1
    return merged


def _make_items(lines: list[Line]) -> list[dict]:
    """Turn ordered lines into emission items carrying layout + font metadata."""
    items = []
    for line in lines:
        items.append(
            {
                "md": _spans_to_md(line.spans),
                "x0": line.bbox[0],
                "x1": line.bbox[2],
                "y0": line.bbox[1],
                "y1": line.bbox[3],
                "h": max(line.bbox[3] - line.bbox[1], 1.0),
                "size": line.size,
                "bold": line.bold,
            }
        )
    return _merge_lone_bullets(items)


def _looks_like_lead(md: str) -> bool:
    """A non-bullet line that starts a bolded term is a paragraph lead-in, not a
    wrapped continuation of the previous bullet (e.g. ``**Process** is a
    collection of activities that:``)."""
    return md.lstrip().startswith("**")


def _emit_items(items: list[dict], bullet_levels: dict[int, int]) -> list[str]:
    out: list[str] = []
    mode = None  # None | "list" | "para"
    list_level = 0
    prev_y1: float | None = None
    for item in items:
        md = item["md"].strip()
        if not md:
            continue
        glyph, rest = _strip_bullet(md)
        y0, y1, h = item["y0"], item["y1"], item["h"]
        gap = y0 - prev_y1 if prev_y1 is not None else 0.0
        if item.get("heading"):
            out.append("")
            out.append(md)
            out.append("")
            mode, list_level = None, 0
        elif glyph is not None:
            level = bullet_levels.get(_round5(item["x0"]), 0)
            indent = "  " * level
            if mode not in (None, "list"):
                out.append("")
            out.append(f"{indent}- {rest}" if rest else f"{indent}-")
            mode, list_level = "list", level
        elif mode == "list":
            if gap <= h * 1.6 and not _looks_like_lead(md):
                out.append("  " * (list_level + 1) + md)
            else:
                out.append("")
                out.append(md)
                mode = "para"
        else:
            if mode == "para" and gap <= h * 1.6:
                out.append(md)
            else:
                if mode == "para":
                    out.append("")
                out.append(md)
                mode = "para"
        prev_y1 = y1
    return out


def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)


def _raw_text(lines: list[Line]) -> str:
    return "\n".join(line.text.strip() for line in lines)


def _strip_transcribed_title(md: str) -> str:
    """Drop a leading heading the vision model adds, since we already emit the
    page heading. Leaves blockquote gists (which start with ``>``) untouched."""
    lines = md.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("#"):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines)


def _page_is_complex(lines: list[Line]) -> bool:
    """A page is "complex" when its lines scatter across too many x positions
    (diagrams, flowcharts). Multi-column *text* pages are not complex — they are
    linearized column-by-column by :func:`_detect_columns` instead."""
    distinct_x = len({_round5(line.bbox[0]) for line in lines})
    return distinct_x >= _COMPLEX_DISTINCT_X


def _detect_columns(lines: list[Line], page_width: float, page_height: float) -> list[list[Line]]:
    """Return the page's side-by-side text columns, or ``[]`` for one column.

    Delegates the coverage-profile band detection to
    :func:`converter.ocr_columns.detect_text_bands`, then groups ``lines`` into
    those bands by their ``x0``. Multi-column *text* pages are linearized
    column-by-column rather than OCR'd; the OCR path reuses the same bands.
    """
    bands = detect_text_bands([ln.bbox for ln in lines], page_width, page_height)
    if len(bands) < 2:
        return []
    cols: list[list[Line]] = []
    for b0, b1 in bands:
        cols.append([ln for ln in lines if b0 <= ln.bbox[0] <= b1])
    cols.sort(key=lambda c: min(ln.bbox[0] for ln in c))
    return cols


def _tables_for_columns(tables, columns: list[list[Line]]) -> list[list[object]]:
    """Split the page's detected tables among columns by their mid-x position.

    A table whose mid-x falls in no column (full-width tables) is attached to
    the last column."""
    x_ranges = []
    for col in columns:
        x_ranges.append((min(ln.bbox[0] for ln in col), max(ln.bbox[2] for ln in col)))
    result: list[list[object]] = [[] for _ in columns]
    for table in tables:
        cx = (table.bbox[0] + table.bbox[2]) / 2.0
        idx = next((i for i, (a, b) in enumerate(x_ranges) if a <= cx <= b), len(columns) - 1)
        result[idx].append(table)
    return result


def _body_size(lines: list[Line]) -> float:
    """Median font size of a column's lines — the reference for heading detection."""
    sizes = sorted(ln.size for ln in lines if ln.size > 0.0)
    if not sizes:
        return 10.0
    return sizes[len(sizes) // 2]


def _heading_text(md: str) -> str:
    """Strip bold/italic markers so headings render cleanly (``## Challenge``)."""
    return md.strip().strip("*").strip()


def _mark_alone(items: list[dict]) -> None:
    """Mark items that share a text row with another item (``crowded``).

    Two side-by-side labels on one line (e.g. figure labels) are not section
    headings, even when bold."""
    for i, item in enumerate(items):
        if item.get("crowded"):
            continue
        row_y = item["y0"]
        others = [
            j for j, other in enumerate(items)
            if j != i and abs(other["y0"] - row_y) <= _ROW_TOL
        ]
        if others:
            for j in others + [i]:
                items[j]["crowded"] = True


def _mark_headings(items: list[dict], body_size: float) -> None:
    """Promote standalone bold/larger/short lines to ``## Heading`` (paper mode).

    A candidate must sit at a "heading position": centered within its column, or
    flush with the column's left edge. This keeps bold *continuation* lines (wrapped
    fragments, table rows) from being promoted. Rewrites ``item["md"]`` to the
    heading itself and sets the ``heading`` flag, which :func:`_emit_items`
    renders with blank lines around it."""
    xs0 = [item.get("x0", 0.0) for item in items]
    xs1 = [item.get("x1", 0.0) for item in items]
    col_min = min(xs0)
    col_max = max(xs1)
    col_center = (col_min + col_max) / 2.0
    for item in items:
        md = item["md"].strip()
        if not md or item.get("crowded"):
            continue
        if len(md) > _HEADING_MAX_LEN or _strip_bullet(md)[0] is not None:
            continue
        if md.endswith((":", ";", ",")):
            continue
        size = item.get("size", 0.0)
        if not (
            item.get("bold")
            or size >= body_size + 1.5
            or (md.isupper() and size >= body_size)
        ):
            continue
        x_center = (item.get("x0", 0.0) + item.get("x1", 0.0)) / 2.0
        centered = abs(x_center - col_center) <= _HEADING_CENTER_TOL
        flush = abs(item.get("x0", 0.0) - col_min) <= _HEADING_FLUSH_TOL
        if not centered and not flush:
            continue
        item["md"] = f"## {_heading_text(md)}"
        item["heading"] = True


class PDFConverter(Converter):
    extensions = (".pdf",)

    def convert(
        self,
        path: Path,
        output_dir: Path,
        progress_callback: PageProgressCallback | None = None,
        output_stem: str | None = None,
    ) -> ConvertResult:
        result = ConvertResult(source_path=path)
        try:
            doc = fitz.open(path)
            stem = output_stem or path.stem
            paper = _pdf_mode()
            page_count = doc.page_count
            assets_dir = output_dir / "assets" / stem
            skip_xrefs = _background_xrefs(doc)
            footer_keys = _collect_footer_keys(doc)
            top_keys = _collect_top_keys(doc) if paper else set()
            repeated = repeated_image_hashes(_collect_page_image_digests(doc, skip_xrefs))
            seen: set[str] = set()
            counter = [1]
            dedup: dict[str, str] = {}
            lines: list[str] = []
            paper_pages: list[PageData] = []
            source = str(path)
            for pno, page in enumerate(doc, start=1):
                page_out, page_content, page_png = self._page_to_md(
                    page, doc, pno, assets_dir, stem, counter, skip_xrefs,
                    footer_keys, top_keys, dedup, result.warnings, repeated,
                    seen, source, paper,
                )
                lines.extend(page_out)
                if paper:
                    if config.is_enabled("structure"):
                        paper_pages.append(
                            PageData(
                                md_lines=page_out,
                                line_meta=[
                                    {
                                        "text": ln.text,
                                        "size": ln.size,
                                        "bold": ln.bold,
                                        "x0": ln.bbox[0],
                                    }
                                    for ln in page_content
                                ],
                                png=page_png,
                                pno=pno,
                            )
                        )
                    lines.append("")
                else:
                    lines.extend([
                        "",
                        '<div style="page-break-after: always; break-after: page;"></div>',
                        "",
                        "---",
                        "",
                    ])
                if progress_callback:
                    progress_callback(pno, page_count, path.name)
            doc.close()
            if paper and config.is_enabled("structure"):
                with phase(current_run_id(), "structure", 2):
                    structured = structure_paper(
                        paper_pages, warnings=result.warnings, source=source
                    )
                if structured is not None:
                    lines = structured
            md_path = output_dir / f"{stem}.md"
            md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            result.md_path = md_path
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def _page_to_md(
        self, page, doc, pno, assets_dir, stem, counter, skip_xrefs, footer_keys,
        top_keys, dedup, warnings, repeated, seen, source, paper,
    ) -> tuple[list[str], list[Line], bytes | None]:
        """Return ``(md_lines, content_lines, page_png)`` for one page.

        ``content_lines`` is the ordered, filtered line list used for emission
        (the text-layer input for the structure pass); ``page_png`` is the
        rendered page image (the image-regime input). ``page_png`` is ``None``
        when the page could not be rendered.
        """
        out: list[str] = []
        all_lines = _ordered_lines(_page_lines(page))

        if paper:
            if pno == 1:
                title, authors, block_lines = _title_block(
                    all_lines, page.rect.width, page.rect.height
                )
                title_ids = {id(ln) for ln in block_lines}
                if title:
                    out.append(f"# {title}")
                    out.append("")
                    if authors:
                        out.append(f"*{authors}*")
                        out.append("")
                else:
                    out.append("# Page 1")
                    out.append("")
            else:
                title_ids = set()
                out.append(f"# Page {pno}")
                out.append("")
        else:
            title, title_ids = _detect_title(all_lines, page.rect.height)
            heading = f"{title} — Page {pno}" if title else f"Page {pno}"
            out.append(f"# {heading}")
            out.append("")

        tables = [t for t in page.find_tables().tables if t.row_count >= 2 and t.col_count >= 2]

        content = [
            line
            for line in all_lines
            if id(line) not in title_ids
            and not _is_page_number(line)
            and _footer_key(line.text) not in footer_keys
            and _footer_key(line.text) not in top_keys
            and not _line_in_tables(line, tables)
        ]

        columns = _detect_columns(content, page.rect.width, page.rect.height)
        quality = text_layer_quality([ln.text for ln in content])
        page_via_vision = config.is_enabled("vision") and (
            quality != "usable"
            or (not columns and _page_is_complex(content))
        )

        image_refs, page_png, png_w, png_h = _page_images(
            page, doc, assets_dir, stem, counter, warnings, skip_xrefs, dedup, pno, repeated, seen, source,
        )
        out.extend(image_refs)
        out.append("")

        if page_via_vision:
            labels = _raw_text(content).splitlines()
            bands = detect_bands(
                page, [ln.bbox for ln in content], page.rect.width, page.rect.height
            )
            interpretation = None
            if len(bands) < 2 and config.is_enabled("interpret"):
                interpretation = interpret_diagram(
                    page_png,
                    labels,
                    warnings,
                    log_ctx={"source": source, "page": pno},
                    width=png_w,
                    height=png_h,
                )
            if interpretation:
                out.extend(interpretation.splitlines())
            else:
                transcription = transcribe_columns(
                    page,
                    [(ln.text, ln.bbox) for ln in content],
                    page.rect.width,
                    page.rect.height,
                    page_png,
                    tables=tables,
                    warnings=warnings,
                    log_ctx={"source": source, "page": pno},
                    width=png_w,
                    height=png_h,
                    bands=bands,
                )
                if transcription:
                    out.extend(_strip_transcribed_title(transcription).splitlines())
                elif _raw_text(content).strip():
                    out.append(_details_block("Raw extracted text", _raw_text(content)))
        elif columns:
            for col_lines, col_tables in zip(
                (_ordered_lines(col) for col in columns),
                _tables_for_columns(tables, columns),
            ):
                out.extend(self._emit_group(col_lines, col_tables, paper))
            out.append("")
        else:
            out.extend(self._emit_group(content, tables, paper))

        out.append("")
        return out, content, page_png

    def _emit_group(self, lines: list[Line], tables, promote_heads: bool) -> list[str]:
        """Emit one reading-order group (a column, or a single-column page)."""
        items = _make_items(lines)
        if promote_heads:
            _mark_alone(items)
            _mark_headings(items, _body_size(lines))
        return self._with_table_flow(items, tables, self._bullet_levels(lines))

    @staticmethod
    def _bullet_levels(lines: list[Line]) -> dict[int, int]:
        xs = sorted({_round5(line.bbox[0]) for line in lines if _strip_bullet(_spans_to_md(line.spans))[0]})
        return {x: idx for idx, x in enumerate(xs)}

    def _with_table_flow(self, items, tables, bullet_levels) -> list[str]:
        """Interleave text items and tables by vertical position."""
        nodes: list[tuple[float, str, object]] = []
        for item in items:
            nodes.append((item["y0"], "text", item))
        for table in tables:
            nodes.append((table.bbox[1], "table", table))
        nodes.sort(key=lambda n: (n[0], 0 if n[1] == "text" else 1))

        out: list[str] = []
        text_buffer: list[dict] = []
        for _, kind, payload in nodes:
            if kind == "text":
                text_buffer.append(payload)
            else:
                out.extend(_emit_items(_merge_lone_bullets(text_buffer), bullet_levels))
                text_buffer = []
                rows = payload.extract()
                out.extend(_table_to_md(rows))
                out.append("")
        if text_buffer:
            out.extend(_emit_items(_merge_lone_bullets(text_buffer), bullet_levels))
        return out

def _details_block(summary: str, body: str) -> str:
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"
