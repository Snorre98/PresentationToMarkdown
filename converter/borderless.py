"""Deterministic borderless-table detection for PDF pages (ADR-0054).

Layout engines — Word in particular — can export tables without any ruling
lines: the grid exists only as whitespace. PyMuPDF's ``find_tables()`` uses the
ruling-lines strategy, finds nothing, and the cell fragments leak into the line
pipeline, where they scramble into interleaved text. This module reconstructs
those tables from the text layer alone:

- spans are read in *content-stream* order (``Page.get_texttrace()``). Word-like
  producers emit one span per cell, row by row, with a cell's wrapped lines
  inside a single span, so a row boundary is the one signal that separates two
  adjacent single-line rows from a wrapped cell even when the vertical geometry
  is identical (page 2 of the motivating assignment has a 17.1pt pitch both
  inside a cell and between two rows).
- the stream order is cross-checked with conservative geometry gates (row-run
  length, page-width cells, bullet/number markers, global column gutters) so
  prose, bullet/numbered lists and multi-column body text are never mistaken
  for tables.

Everything here is deterministic and text-layer only; no AI pass is involved.
A page shape this module does not understand degrades to *no tables* — the
caller then keeps its existing line-based output rather than a guessed table.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from converter import config

# Conservative gates. Missing a borderless table keeps today's text output;
# inventing one corrupts prose into a table, so every gate errs on the safe side.
_MIN_ROWS = 3  # a run of fewer table-like rows is left alone (2-row "tables" are ambiguous)
_MAX_COLS = 6
_MIN_CUT = 4.0  # pt: the narrowest clean gutter that separates two columns
_MAX_CELL_FRAC = 0.60  # a cell wider than this fraction of the page is prose
_X_TOL = 4.0  # pt: same-column tolerance when merging wrapped lines
_MERGE_GAP_FACTOR = 0.75  # a y-gap <= this * font size is a wrapped continuation
_ROW_TOL = 1.0  # pt: y-overlap tolerance when grouping cells into rows
_FRAGMENT_TOL = 3.0  # pt: a span starting this close to the previous cell continues it
_MAX_LINE_GROWTH = 40.0  # pt: a wrapped line may exceed its cell's width by this much
_LINE_TOL = 0.5  # pt: char-origin y difference that still counts as the same line

# A standalone bullet glyph at the start of a line marks a list item.
_BULLET_RE = re.compile(r"^\s*[\u2022\u2023\u2043\u25AA\u25CF\u25CB\u00B7\u2013\u2014]\s*$")
# A standalone numbering label ("1.", "2)", "a.") marks an enumerated list item.
_MARKER_RE = re.compile(r"^\s*(?:\d{1,3}|[A-Za-z])[.)]\s*$")

# Characters that bind two fragments without a space ("90" + "–" + "100").
_NO_SPACE_END = "-–—/(["
_NO_SPACE_START = "-–—.,;:!?%)]}/"


@dataclass(frozen=True)
class Span:
    """One visible text span, in page content-stream order."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = 0.0


@dataclass
class BorderlessTable:
    """A detected borderless table, shaped like PyMuPDF's ``Table``.

    Exposes the two members the PDF pipeline relies on — ``bbox`` (to filter the
    cell lines and interleave the table with surrounding text) and ``extract()``
    (rows fed to ``base._table_to_md``) — so it drops into the existing
    ruled-table plumbing unchanged.
    """

    bbox: tuple[float, float, float, float]
    rows: list[list[str]]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def col_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def extract(self) -> list[list[str]]:
        return [list(row) for row in self.rows]


@dataclass
class _Cell:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float


@dataclass
class _Row:
    y0: float
    y1: float
    cells: list[_Cell]
    marker: bool = False  # the row opens with a bullet/number marker (a list item)


def _append_text(text: str, addition: str) -> str:
    """Join two text fragments, inserting a space only where words need one."""
    text = text.strip()
    addition = addition.strip()
    if not text:
        return addition
    if not addition:
        return text
    if text[-1] in _NO_SPACE_END or addition[0] in _NO_SPACE_START:
        return text + addition
    return f"{text} {addition}"


def _span_text(span: dict) -> str:
    """The span's text, with wrapped lines joined by spaces.

    Word emits a whole table cell (all its wrapped lines) as one text span;
    the individual lines are recovered from each character's origin y.
    """
    lines: list[str] = []
    current: list[str] = []
    prev_y: float | None = None
    for char in span.get("chars") or []:
        try:
            codepoint, _gid, origin, _bbox = char
        except (TypeError, ValueError):
            continue
        y = origin[1]
        if prev_y is not None and abs(y - prev_y) > _LINE_TOL:
            lines.append("".join(current))
            current = []
        current.append(chr(codepoint))
        prev_y = y
    if current:
        lines.append("".join(current))
    text = ""
    for line in lines:
        text = _append_text(text, line)
    return text


def page_spans(page) -> list[Span]:
    """Return the page's visible text spans in content-stream order."""
    spans: list[Span] = []
    for raw in page.get_texttrace():
        if raw.get("type", 0) == 3:
            continue  # invisible text (render mode 3)
        text = _span_text(raw)
        if not text:
            continue
        x0, y0, x1, y1 = raw["bbox"]
        spans.append(
            Span(
                text=text,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                size=float(raw.get("size") or 0.0),
            )
        )
    return spans


def _is_marker(text: str) -> bool:
    return bool(_BULLET_RE.match(text) or _MARKER_RE.match(text))


def _merge_wrapped(spans: list[Span]) -> list[_Cell]:
    """Merge consecutive spans that are wrapped continuation lines of one cell.

    In Word output this is a no-op (a cell arrives as one span); producers that
    emit one span per text line are folded back into cells here. Only
    *consecutive* spans merge, which keeps two adjacent single-line rows apart:
    their cells belong to different columns and are never neighbours in the
    stream.
    """
    cells: list[_Cell] = []
    for span in spans:
        cell = _Cell(span.text, span.x0, span.y0, span.x1, span.y1, span.size)
        if cells:
            last = cells[-1]
            gap = cell.y0 - last.y1
            if (
                abs(cell.x0 - last.x0) <= _X_TOL
                and -0.5 <= gap <= max(3.0, _MERGE_GAP_FACTOR * (last.size or 10.0))
                and cell.x1 - last.x0 <= (last.x1 - last.x0) + _MAX_LINE_GROWTH
            ):
                last.text = _append_text(last.text, cell.text)
                last.x1 = max(last.x1, cell.x1)
                last.y1 = max(last.y1, cell.y1)
                continue
        cells.append(cell)
    return cells


def _continues(row: _Row, cell: _Cell) -> bool:
    """Whether ``cell`` continues the row's last cell (same run, no gutter)."""
    if not row.cells:
        return False
    last = row.cells[-1]
    return last.x0 - 1.0 <= cell.x0 <= last.x1 + _FRAGMENT_TOL


def _append_to_last(row: _Row, cell: _Cell) -> None:
    if not row.cells:
        row.cells.append(cell)
        return
    last = row.cells[-1]
    last.text = _append_text(last.text, cell.text)
    last.x1 = max(last.x1, cell.x1)
    last.y1 = max(last.y1, cell.y1)


def _to_rows(cells: list[_Cell]) -> list[_Row]:
    """Group cells into rows in content-stream order.

    The stream is row-major: a cell joins the current row while its y-interval
    overlaps the row's band, and otherwise opens a new row (the row boundary is
    the leftward jump of the cell column). Fragments and inline dashes that
    start mid-line continue the previous cell instead of becoming a column of
    their own. A bullet/number marker opens a *list* row, which disqualifies
    the line from table detection (and swallows the rest of its line).
    """
    rows: list[_Row] = []
    for cell in cells:
        if rows:
            row = rows[-1]
            overlaps = min(row.y1, cell.y1) - max(row.y0, cell.y0) >= -_ROW_TOL
            if overlaps:
                if row.marker:
                    continue
                if _is_marker(cell.text) or _continues(row, cell):
                    _append_to_last(row, cell)
                else:
                    row.cells.append(cell)
                row.y0 = min(row.y0, cell.y0)
                row.y1 = max(row.y1, cell.y1)
                continue
        marker = _is_marker(cell.text)
        rows.append(
            _Row(y0=cell.y0, y1=cell.y1, cells=[] if marker else [cell], marker=marker)
        )
    return rows


def _table_like(row: _Row, page_width: float) -> bool:
    """Whether a row can be part of a table (not a list, not prose)."""
    if row.marker or len(row.cells) < 2:
        return False
    return all(
        (cell.x1 - cell.x0) <= _MAX_CELL_FRAC * page_width for cell in row.cells
    )


def _column_cuts(cells: list[_Cell]) -> list[float] | None:
    """The run's column separators from its global x gutters, or ``None``.

    Columns are the x-intervals between gutters that *no* cell of the run
    crosses; a wide or centred header cell that reaches into a gutter therefore
    cannot silently merge two columns, and a prose-mixed run (whose fragments
    cover every gutter) is rejected outright.
    """
    intervals = sorted((cell.x0, cell.x1) for cell in cells)
    merged: list[list[float]] = []
    for x0, x1 in intervals:
        if merged and x0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    cuts = [
        (merged[i][1] + merged[i + 1][0]) / 2.0
        for i in range(len(merged) - 1)
        if merged[i + 1][0] - merged[i][1] >= _MIN_CUT
    ]
    if not 2 <= len(cuts) + 1 <= _MAX_COLS:
        return None
    return cuts


def _build_table(run: list[_Row], page_width: float) -> BorderlessTable | None:
    """Turn a run of table-like rows into a table grid, or ``None``."""
    cells = [cell for row in run for cell in row.cells]
    cuts = _column_cuts(cells)
    if cuts is None:
        return None
    rows: list[list[str]] = []
    for row in run:
        values = [""] * (len(cuts) + 1)
        for cell in row.cells:
            index = bisect_right(cuts, (cell.x0 + cell.x1) / 2.0)
            values[index] = _append_text(values[index], cell.text)
        rows.append(values)
    if sum(1 for row in rows if sum(1 for value in row if value) >= 2) < 2:
        return None
    x0 = min(cell.x0 for cell in cells)
    y0 = min(cell.y0 for cell in cells)
    x1 = max(cell.x1 for cell in cells)
    y1 = max(cell.y1 for cell in cells)
    return BorderlessTable(bbox=(x0, y0, x1, y1), rows=rows)


def detect_tables(spans: list[Span], page_width: float) -> list[BorderlessTable]:
    """Detect the borderless tables in one page's spans (pure, no I/O).

    Table candidates are maximal runs of consecutive table-like rows; each run
    is accepted only when its global gutters yield 2..6 columns and at least
    two rows carry two or more non-empty cells.
    """
    rows = _to_rows(_merge_wrapped(spans))
    tables: list[BorderlessTable] = []
    run: list[_Row] = []
    for row in [*rows, None]:
        if row is not None and _table_like(row, page_width):
            run.append(row)
            continue
        if len(run) >= _MIN_ROWS:
            table = _build_table(run, page_width)
            if table is not None:
                tables.append(table)
        run = []
    return tables


def detect_borderless_tables(page) -> list[BorderlessTable]:
    """Detect the borderless tables of one PDF page (never raises).

    Returns ``[]`` when the preference is off (``PDF_BORDERLESS_TABLES=off``)
    or when the page's text stream does not match the expected shape — the
    caller then keeps today's line-based output.
    """
    if not config.borderless_tables_enabled():
        return []
    try:
        return detect_tables(page_spans(page), page.rect.width)
    except Exception:  # noqa: BLE001 - degrade to "no tables", never fail a conversion
        return []
