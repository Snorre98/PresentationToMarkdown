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

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from converter.base import (
    ConvertResult,
    Converter,
    _format_md,
    _link_dest,
    _table_to_md,
    image_digest,
    repeated_image_hashes,
    write_image,
)
from converter.classify import maybe_transcribe_image, should_transcribe
from converter.vision import (
    VISION_ENABLED,
    transcribe_page,
    verify_no_omissions,
)

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


def _page_images(page, doc, assets_dir, stem, counter, warnings, skip_xrefs, dedup, pno, repeated, seen) -> list[str]:
    refs: list[str] = []
    assets_dir.mkdir(parents=True, exist_ok=True)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    png_name = f"{stem}_page_{pno:02d}.png"
    pix.save(str(assets_dir / png_name))
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
            transcription = maybe_transcribe_image(
                extracted["image"], extracted.get("ext", "bin"), warnings
            )
            if transcription:
                refs.extend(transcription.splitlines())
                refs.append("")
    return refs


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
                }
            )
            i += 2
        else:
            merged.append(item)
            i += 1
    return merged


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
        if glyph is not None:
            level = bullet_levels.get(_round5(item["x0"]), 0)
            indent = "  " * level
            if mode not in (None, "list"):
                out.append("")
            out.append(f"{indent}- {rest}" if rest else f"{indent}-")
            mode, list_level = "list", level
        elif mode == "list":
            if gap <= h * 1.6:
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


def _page_is_complex(lines: list[Line]) -> bool:
    distinct_x = len({_round5(line.bbox[0]) for line in lines})
    if distinct_x >= _COMPLEX_DISTINCT_X:
        return True
    return _has_parallel_columns(lines)


def _has_parallel_columns(lines: list[Line], tol: float = _ROW_TOL, gap: float = 30.0, min_rows: int = 3) -> bool:
    """Detect side-by-side columns: multiple rows where two lines are separated by
    a horizontal gap (as opposed to a bullet glyph immediately before its text)."""
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: l.bbox[1]):
        if rows and abs(line.bbox[1] - rows[-1][0].bbox[1]) <= tol:
            rows[-1].append(line)
        else:
            rows.append([line])
    count = 0
    for row in rows:
        if len(row) < 2:
            continue
        row = sorted(row, key=lambda l: l.bbox[0])
        for a, b in zip(row, row[1:]):
            if b.bbox[0] - a.bbox[2] >= gap:
                count += 1
                break
    return count >= min_rows


class PDFConverter(Converter):
    extensions = (".pdf",)

    def convert(self, path: Path, output_dir: Path) -> ConvertResult:
        result = ConvertResult(source_path=path)
        try:
            doc = fitz.open(path)
            stem = path.stem
            assets_dir = output_dir / "assets" / stem
            skip_xrefs = _background_xrefs(doc)
            footer_keys = _collect_footer_keys(doc)
            repeated = repeated_image_hashes(_collect_page_image_digests(doc, skip_xrefs))
            seen: set[str] = set()
            counter = [1]
            dedup: dict[str, str] = {}
            lines: list[str] = []
            for pno, page in enumerate(doc, start=1):
                lines.extend(
                    self._page_to_md(
                        page, doc, pno, assets_dir, stem, counter, skip_xrefs,
                        footer_keys, dedup, result.warnings, repeated, seen,
                    )
                )
                lines.extend([
                    "",
                    '<div style="page-break-after: always; break-after: page;"></div>',
                    "",
                    "---",
                    "",
                ])
            doc.close()
            md_path = output_dir / f"{stem}.md"
            md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            result.md_path = md_path
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def _page_to_md(
        self, page, doc, pno, assets_dir, stem, counter, skip_xrefs, footer_keys, dedup, warnings, repeated, seen
    ) -> list[str]:
        out: list[str] = []
        all_lines = _ordered_lines(_page_lines(page))

        title, title_ids = _detect_title(all_lines, page.rect.height)
        heading = f"{title} — Page {pno}" if title else f"Page {pno}"
        out.append(f"# {heading}")
        out.append("")

        image_refs = _page_images(
            page, doc, assets_dir, stem, counter, warnings, skip_xrefs, dedup, pno, repeated, seen
        )
        out.extend(image_refs)
        out.append("")

        tables = [t for t in page.find_tables().tables if t.row_count >= 2 and t.col_count >= 2]

        content = [
            line
            for line in all_lines
            if id(line) not in title_ids
            and not _is_page_number(line)
            and _footer_key(line.text) not in footer_keys
            and not _line_in_tables(line, tables)
        ]

        complex_page = _page_is_complex(content)

        if complex_page and VISION_ENABLED:
            markdown = self._vision_or_none(page, content, warnings)
            if markdown is not None:
                out.extend(markdown.splitlines())
            else:
                out.append(_details_block("Raw extracted text", _raw_text(content)))
        elif complex_page:
            out.append(_details_block("Raw extracted text", _raw_text(content)))
        else:
            bullet_levels = self._bullet_levels(content)
            items = [
                {
                    "md": _spans_to_md(line.spans),
                    "x0": line.bbox[0],
                    "y0": line.bbox[1],
                    "y1": line.bbox[3],
                    "h": max(line.bbox[3] - line.bbox[1], 1.0),
                }
                for line in content
            ]
            items = _merge_lone_bullets(items)
            out.extend(self._with_table_flow(items, tables, bullet_levels))

        out.append("")
        return out

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

    def _vision_or_none(self, page, content, warnings) -> str | None:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        png = pix.tobytes("png")
        raw = _raw_text(content)
        try:
            if not should_transcribe(png, "image/png", warnings):
                return None
            markdown = transcribe_page(png)
        except Exception as exc:
            warnings.append(f"Vision transcription failed: {exc}")
            return None
        missing = verify_no_omissions(raw, markdown)
        if missing:
            limit = max(3, int(len(missing) * 0.2))
            if len(missing) > limit:
                warnings.append(
                    f"Vision output may omit text ({len(missing)} tokens missing, "
                    f"e.g. {', '.join(missing[:5])}); keeping raw text instead"
                )
                return None
        return markdown


def _details_block(summary: str, body: str) -> str:
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"
