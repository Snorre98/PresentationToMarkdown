"""Deterministic paragraph and heading reconstruction for PDF text layers.

The converter's paper mode turns a stream of visually-wrapped PDF lines into
blocks: paragraphs, list items and headings. This module owns that model
(ADR-0055) so the heuristics stay testable without opening a PDF:

- :func:`build_profile` learns per-document typography from the text layer:
  the character-weighted body size, the modal line pitch, the paragraph-gap
  threshold and the display size tiers above body (with minimum support).
- :func:`segment_group` groups lines into blocks using the learned gap, the
  sentence boundary of the previous line and the formatting context (the
  previous line's trailing run vs. this line's leading run).
- :func:`classify_block` promotes block starts to headings through an evidence
  ladder: a learned size tier, or — when no size signal exists — a strict
  bold-only fallback. Multiple independent signals must agree; ambiguity
  leaves the line as text.
- :func:`render_block` renders Markdown from span runs (never from
  pre-rendered strings), merging formatting across wrapped line breaks so
  ``**10 available`` + ``additional…**`` becomes one balanced run.

All thresholds are safety rails or fallbacks; the decision criteria are the
learned statistics.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from converter.base import _format_md
from converter._english_words import ENGLISH_WORDS

_BOLD_FLAG = 2**4
_ITALIC_FLAG = 2**1

# Bullet glyphs at line start (same set as the converter's slide-mode path).
_BULLET_RE = re.compile(r"^\s*([\u2022\u2023\u2043\u25AA\u25CF\u25CB\u00B7\u2013\u2014])\s*")
_NUMBER_RE = re.compile(r"^\d{1,2}[.)]\s")
_SENTENCE_END_RE = re.compile(r"[.!?:][\"'\u2019\u201d\u00bb)\]]*\s*$")
_QUOTE_START_RE = re.compile(r"^[\"'\u2018\u2019\u201c\u201d\u00ab\u00bb]")

ROW_TOL = 6.0

# Safety rails (never the decision criterion, ADR-0055).
_MAX_HEADING_WORDS = 25
_BOLD_ONLY_MAX_WORDS = 12
_TIER_MIN_LINES = 2
_TIER_SHAPE_MIN = 0.5
_TIER_SHAPE_WORDS = 15
_FLUSH_TOL = 5.0
_CENTER_TOL = 15.0
_TITLE_BAND_FRAC = 0.30
_TITLE_CONTINUE_FACTOR = 2.6
_COLUMN_X_TOL = 6.0
_FALLBACK_PITCH_FACTOR = 1.4
_FALLBACK_GAP_FACTOR = 1.5
_MIN_GAP_FACTOR = 1.15


@dataclass(frozen=True)
class Run:
    """A same-format text run (bold/italic only; size stays on the line)."""

    text: str
    bold: bool = False
    italic: bool = False


@dataclass
class TextLine:
    """One visual PDF line with its runs — the model's input unit."""

    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    runs: list[Run]
    bullet: str | None = None
    crowded: bool = False


@dataclass
class Block:
    """A paragraph, list item or heading."""

    kind: str  # "heading" | "para" | "bullet"
    lines: list[TextLine] = field(default_factory=list)
    level: int = 0
    bullet_level: int = 0
    continues: bool = False
    start_gap: float | None = None


@dataclass
class Profile:
    """Per-document typography learned from the text layer."""

    body_size: float
    pitch: float
    para_gap: float
    tiers: list[float]  # descending
    title_size: float | None = None
    title_reused: bool = False


# --- span/run conversion -----------------------------------------------------


def runs_from_spans(spans: list[dict]) -> list[Run]:
    """Merge a PDF line's spans into same-format runs (bold/italic keyed)."""
    runs: list[Run] = []
    for span in spans:
        bold = bool(span["flags"] & _BOLD_FLAG)
        italic = bool(span["flags"] & _ITALIC_FLAG)
        if runs and runs[-1].bold == bold and runs[-1].italic == italic:
            runs[-1] = Run(runs[-1].text + span["text"], bold, italic)
        else:
            runs.append(Run(span["text"], bold, italic))
    return runs


def render_runs(runs: list[Run]) -> str:
    """Render runs to Markdown, keeping whitespace outside ``**``/``*`` markers."""
    out: list[str] = []
    for run in runs:
        match = re.match(r"^(\s*)(.*?)(\s*)$", run.text, re.DOTALL)
        if not match:
            out.append(run.text)
            continue
        lead, core, trail = match.group(1), match.group(2), match.group(3)
        out.append(lead + _format_md(core, run.bold, run.italic) + trail)
    return "".join(out)


def plain_text(runs: list[Run]) -> str:
    """The run text without formatting, whitespace-normalised."""
    return re.sub(r"\s+", " ", "".join(run.text for run in runs)).strip()


def join_runs(lines: list[TextLine]) -> list[Run]:
    """Merge the runs of consecutive lines into one paragraph-level run list.

    Same-format runs are merged across the line break (so a bold phrase split
    by the wrap stays one ``**…**`` span) and a boundary space is inserted
    when the producer omitted one. A line broken at an existing hyphen
    (``data-``/``science``) rejoins without a space, and a hyphenated word
    (``fail-``/``ure``) rejoins as the dictionary word when the merged form is
    in the bundled vocabulary.
    """
    merged: list[Run] = []
    for line in lines:
        need_space = bool(merged) and not merged[-1].text[-1:].isspace()
        for run in line.runs:
            if not run.text:
                continue
            text = run.text
            if need_space and not text[:1].isspace():
                joined = _hyphen_join(merged[-1].text, text)
                if joined == "keep":
                    pass
                elif joined == "drop":
                    merged[-1] = Run(
                        merged[-1].text[:-1], merged[-1].bold, merged[-1].italic
                    )
                else:
                    text = " " + text
            need_space = False
            if merged and merged[-1].bold == run.bold and merged[-1].italic == run.italic:
                merged[-1] = Run(merged[-1].text + text, run.bold, run.italic)
            else:
                merged.append(Run(text, run.bold, run.italic))
    return merged


def _hyphen_join(previous: str, following: str) -> str:
    """How to join a line ending in ``-`` with the next line.

    Returns ``"drop"`` when the joined form is a known English word (a word
    hyphenated at the column edge — ``fail-`` + ``ure``), ``"keep"`` for an
    existing compound (``data-`` + ``science``) or a numeric range, and
    ``"space"`` for an ordinary wrap.
    """
    if not previous.endswith("-") or previous.endswith("--"):
        return "space"
    if following[:1].isdigit():
        return "keep"
    if not following[:1].isalpha():
        return "space"
    prefix = re.search(r"([A-Za-z]{2,})$", previous[:-1])
    suffix = re.match(r"([A-Za-z]{2,})", following)
    if prefix and suffix:
        word = (prefix.group(1) + suffix.group(1)).lower()
        if word in ENGLISH_WORDS:
            return "drop"
    return "keep"


def block_plain(block: Block) -> str:
    """The block's plain text (markers stripped, whitespace normalised)."""
    return plain_text(join_runs(block.lines))


def render_block(block: Block, reflow: bool = True) -> list[str]:
    """Render one block to Markdown lines (headings render as plain text).

    With ``reflow`` off each visual line is emitted separately (the legacy
    line-per-line output); the paragraph model still drives segmentation and
    heading promotion.
    """
    if block.kind == "heading":
        text = block_plain(block)
        return [f"{'#' * max(block.level, 1)} {text}"] if text else []
    if not reflow:
        out: list[str] = []
        indent = "  " * block.bullet_level
        for index, line in enumerate(block.lines):
            text = re.sub(r"[ \t]+", " ", render_runs(line.runs)).strip()
            if not text:
                continue
            if block.kind == "bullet":
                marker = f"{indent}- " if not out else indent + "  "
                out.append(marker + text)
            else:
                out.append(text)
        return out
    text = render_runs(join_runs(block.lines))
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    if block.kind == "bullet":
        return [f"{'  ' * block.bullet_level}- {text}"]
    return [text]


# --- line helpers ------------------------------------------------------------


def _plain(line: TextLine) -> str:
    return plain_text(line.runs)


def _leading_run(line: TextLine) -> Run | None:
    for run in line.runs:
        if run.text.strip():
            return run
    return None


def _trailing_run(line: TextLine) -> Run | None:
    for run in reversed(line.runs):
        if run.text.strip():
            return run
    return None


def _fmt_continues(prev: TextLine, line: TextLine) -> bool:
    """Whether ``line`` continues ``prev``'s formatting context."""
    a, b = _trailing_run(prev), _leading_run(line)
    if a is None or b is None:
        return False
    return (
        a.bold == b.bold
        and a.italic == b.italic
        and abs(prev.size - line.size) <= 1.0
    )


def _sentence_final(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text))


def _starts_sentence(text: str) -> bool:
    stripped = text.lstrip("\"'([\u2018\u2019\u201c\u201d\u00ab\u00bb")
    return bool(stripped) and (stripped[0].isupper() or stripped[0].isdigit())


def _continuation(prev: TextLine, line: TextLine, profile: Profile) -> bool:
    """Whether ``line`` is a wrapped continuation of ``prev``.

    A gap above the learned paragraph threshold starts a new block; the
    threshold scales with this line's size because leading grows with the font
    (a 14 pt block's line pitch is larger than the 9.5 pt body's). Within a
    paragraph, a change of formatting context only breaks when the previous
    line plainly ends a sentence and this line starts one; a wrapped run or a
    mid-sentence break continues.
    """
    threshold = profile.para_gap
    if profile.body_size > 0.0:
        threshold *= max(1.0, line.size / profile.body_size)
    if line.y0 - prev.y0 > threshold:
        return False
    prev_plain = _plain(prev)
    if _sentence_final(prev_plain) and _starts_sentence(_plain(line)):
        return False
    if _fmt_continues(prev, line):
        return True
    return not _sentence_final(prev_plain)


def _cross_continuation(prev: TextLine, line: TextLine) -> bool:
    """Continuation across a column or page boundary (no comparable gap)."""
    if _sentence_final(_plain(prev)) and _starts_sentence(_plain(line)):
        return False
    if _fmt_continues(prev, line):
        return True
    return not _sentence_final(_plain(prev))


# --- profiling ---------------------------------------------------------------


def _round_half(value: float) -> float:
    return round(value * 2.0) / 2.0


def _body_size(pages: list[list[TextLine]]) -> float:
    """Character-weighted modal size — the document's body text."""
    weights: Counter = Counter()
    for page in pages:
        for line in page:
            text = _plain(line) or line.text.strip()
            if text and line.size > 0.0:
                weights[round(line.size, 1)] += len(text)
    if not weights:
        return 10.0
    return max(weights.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def _column_pairs(pages: list[list[TextLine]], body_size: float):
    """Consecutive same-column body-size line pairs (candidate pitches)."""
    for page in pages:
        for prev, line in zip(page, page[1:]):
            if abs(prev.size - body_size) > 0.4 or abs(line.size - body_size) > 0.4:
                continue
            if abs(prev.x0 - line.x0) > 2.0:
                continue
            delta = line.y0 - prev.y0
            if 0.5 * body_size <= delta <= 3.0 * body_size:
                yield delta


def _spacing(pages: list[list[TextLine]], body_size: float) -> tuple[float, float]:
    """Learn ``(pitch, paragraph-gap threshold)`` from same-column body deltas.

    Repeated line deltas cluster around the intra-paragraph pitch and the
    paragraph gap; documents where breaks outnumber wrapped lines (short
    paragraphs, or a paragraph after every line) would fool a plain mode, so
    the largest significant gap between distinct deltas splits the two
    clusters. Without a split there is no observed paragraph gap and the
    fallback rail applies.
    """
    deltas = sorted(_round_half(delta) for delta in _column_pairs(pages, body_size))
    counts = Counter(deltas)
    pitch: float | None = None
    gap_mode: float | None = None
    unique = sorted(counts)
    split_at: int | None = None
    best_gap = 0.0
    for a, b in zip(unique, unique[1:]):
        if b - a < 0.25 * a:
            continue
        small = sum(counts[v] for v in unique if v <= a)
        large = sum(counts[v] for v in unique if v >= b)
        if small < 2 or large < 2:
            continue
        if b - a > best_gap:
            best_gap = b - a
            split_at = b
    if unique:
        if split_at is not None:
            small = [d for d in deltas if d < split_at]
            large = [d for d in deltas if d >= split_at]
            pitch = max(Counter(small).items(), key=lambda kv: (kv[1], -kv[0]))[0]
            gap_mode = max(Counter(large).items(), key=lambda kv: (kv[1], -kv[0]))[0]
        else:
            pitch = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    if pitch is None:
        pitch = _FALLBACK_PITCH_FACTOR * body_size
    if gap_mode is None or gap_mode <= pitch:
        gap = _FALLBACK_GAP_FACTOR * pitch
    else:
        gap = max((pitch + gap_mode) / 2.0, _MIN_GAP_FACTOR * pitch)
    return pitch, gap


def _heading_shaped(line: TextLine) -> bool:
    """A short line that does not end in a full stop — heading-like, not prose."""
    plain = _plain(line)
    if not plain:
        return False
    return not plain.endswith(".") and len(plain.split()) <= _TIER_SHAPE_WORDS


def _tiers(
    pages: list[list[TextLine]], body_size: float, pitch: float, gap: float
) -> list[float]:
    """Display sizes above body that behave like heading tiers.

    A tier needs at least two lines and at least half of them must be
    approximate paragraph starts (not continuations of the line before) that
    are heading-shaped. A prose block styled above body — an abstract, a pull
    quote — fails the block-start test because its lines continue one another.
    """
    base = Profile(body_size=body_size, pitch=pitch, para_gap=gap, tiers=[])
    by_size: dict[float, list[tuple[TextLine, bool]]] = {}
    for page in pages:
        prev: TextLine | None = None
        for line in page:
            start = (
                prev is None
                or abs(line.x0 - prev.x0) > _COLUMN_X_TOL
                or not _continuation(prev, line, base)
            )
            if line.size > body_size + 0.4:
                by_size.setdefault(round(line.size, 1), []).append((line, start))
            prev = line
    tiers: list[float] = []
    for size, entries in by_size.items():
        starts = sum(1 for line, start in entries if start and _heading_shaped(line))
        if len(entries) >= _TIER_MIN_LINES and starts >= _TIER_SHAPE_MIN * len(entries):
            tiers.append(size)
    return sorted(tiers, reverse=True)


def _title_sized(line: TextLine, width: float) -> bool:
    """Sanity rails for a title line: real text, inside the text margins."""
    plain = _plain(line)
    if len(plain) < 3:
        return False
    if width > 0.0 and line.x0 > 0.75 * width:
        return False  # a corner logo/decoration, not a title
    return True


def _title_cutoff(page1: list[TextLine], height: float, body_size: float) -> float:
    """Everything above this y is title-block territory (top band and above the
    first body-size line)."""
    band = height * _TITLE_BAND_FRAC if height > 0 else float("inf")
    first_body = next(
        (line.y0 for line in page1 if abs(line.size - body_size) <= 0.4), None
    )
    return min(first_body, band) if first_body is not None else band


def _title_run(
    page1: list[TextLine], cutoff: float, width: float, size: float
) -> list[TextLine]:
    """The page-1 title lines at ``size``: the last contiguous run of same-size
    lines that starts above the title cutoff (a wrapped title extends below it,
    and a letterhead above the real title is a separate run)."""
    lines = [
        line
        for line in page1
        if abs(line.size - size) <= 0.5 and _title_sized(line, width)
    ]
    runs: list[list[TextLine]] = []
    for line in lines:
        if runs and line.y0 - runs[-1][-1].y0 <= _TITLE_CONTINUE_FACTOR * size:
            runs[-1].append(line)
        else:
            runs.append([line])
    for run in reversed(runs):
        if run[0].y0 < cutoff:
            return run
    return []


def _title_info(
    page1: list[TextLine],
    height: float,
    width: float,
    body_size: float,
    all_pages: list[list[TextLine]],
) -> tuple[float | None, bool]:
    """(title size, reused as a section size) from page 1's title block.

    A title size that also appears outside the page-1 title run (e.g. the same
    24 pt styling reused for part headings) keeps its tier in the section
    outline instead of being dropped with the title.
    """
    cutoff = _title_cutoff(page1, height, body_size)
    candidates = [
        line
        for line in page1
        if line.y0 < cutoff
        and line.size > body_size + 0.4
        and _title_sized(line, width)
    ]
    if not candidates:
        return None, False
    size = max(line.size for line in candidates)
    if size < body_size + 1.5:
        return None, False
    run = _title_run(page1, cutoff, width, size)
    if not run or any(_NUMBER_RE.match(_plain(line)) for line in run):
        return None, False
    reused = any(
        abs(line.size - size) <= 0.5 and all(line is not titled for titled in run)
        for page in all_pages
        for line in page
    )
    return size, reused


def build_profile(
    pages: list[list[TextLine]],
    heights: list[float],
    widths: list[float] | None = None,
) -> Profile:
    """Learn the document's typography from its text-layer lines (raw pages)."""
    lines = [line for page in pages for line in page]
    if not lines:
        return Profile(body_size=10.0, pitch=14.0, para_gap=21.0, tiers=[])
    body = _body_size(pages)
    pitch, gap = _spacing(pages, body)
    tiers = _tiers(pages, body, pitch, gap)
    title_size, reused = (None, False)
    if pages:
        width = widths[0] if widths else 0.0
        title_size, reused = _title_info(
            pages[0], heights[0] if heights else 0.0, width, body, pages
        )
    return Profile(
        body_size=body, pitch=pitch, para_gap=gap, tiers=tiers,
        title_size=title_size, title_reused=reused,
    )


def heading_levels(profile: Profile) -> dict[float, int]:
    """Map each section tier to a Markdown heading level (``##`` onwards)."""
    tiers = list(profile.tiers)
    if profile.title_size is not None and not profile.title_reused:
        tiers = [t for t in tiers if abs(t - profile.title_size) > 0.5]
    return {size: min(2 + index, 6) for index, size in enumerate(tiers)}


def detect_title(
    page1: list[TextLine], height: float, width: float, profile: Profile
) -> list[TextLine]:
    """The page-1 title lines: the learned title size's run in the title block.

    Returns ``[]`` when no candidate exists (no invented ``#`` heading).
    """
    if profile.title_size is None:
        return []
    cutoff = _title_cutoff(page1, height, profile.body_size)
    run = _title_run(page1, cutoff, width, profile.title_size)
    if not run or any(_NUMBER_RE.match(_plain(line)) for line in run):
        return []
    return run


# --- segmentation ------------------------------------------------------------


def bullet_split(text: str) -> tuple[str | None, str]:
    """Return ``(glyph, rest)`` when ``text`` starts with a bullet glyph."""
    match = _BULLET_RE.match(text)
    if not match:
        return None, text
    return match.group(1), text[match.end():].lstrip()


def strip_glyph(line: TextLine) -> None:
    """Remove the leading bullet glyph from the line's runs, in place."""
    match = _BULLET_RE.match(line.text)
    if not match:
        return
    cut = match.end()
    runs: list[Run] = []
    for run in line.runs:
        text = run.text
        if cut <= 0:
            runs.append(run)
        elif cut >= len(text):
            cut -= len(text)
        else:
            runs.append(Run(text[cut:], run.bold, run.italic))
            cut = 0
    line.runs = runs


def segment_group(
    lines: list[TextLine], profile: Profile, *, prev: TextLine | None = None
) -> list[Block]:
    """Group one reading-order run of lines into paragraph/item blocks."""
    blocks: list[Block] = []
    for i, line in enumerate(lines):
        if i == 0:
            new = True
        else:
            previous = lines[i - 1]
            if abs(line.y0 - previous.y0) <= ROW_TOL:
                new = True
                line.crowded = True
                previous.crowded = True
            elif line.bullet:
                new = True
            elif _NUMBER_RE.match(_plain(line)):
                new = True
            elif blocks and blocks[-1].kind == "bullet":
                bullet_x0 = blocks[-1].lines[0].x0
                new = not (
                    _continuation(previous, line, profile)
                    and line.x0 > bullet_x0 + 2.0
                )
            else:
                new = not _continuation(previous, line, profile)
        if new:
            kind = "bullet" if line.bullet else "para"
            block = Block(kind=kind, lines=[line])
            block.start_gap = None if i == 0 else line.y0 - lines[i - 1].y0
            blocks.append(block)
        else:
            blocks[-1].lines.append(line)
    if prev is not None and blocks:
        first = blocks[0]
        if first.kind == "bullet":
            if prev.bullet:
                first.continues = _cross_continuation(prev, first.lines[0]) and (
                    first.lines[0].x0 > prev.x0 + 2.0
                )
        else:
            first.continues = _cross_continuation(prev, first.lines[0])
    return blocks


# --- heading evidence ladder -------------------------------------------------


def _tier_level(size: float, levels: dict[float, int]) -> int:
    for tier, level in levels.items():
        if abs(size - tier) <= 0.5:
            return level
    return 0


def _whole_line_bold(block: Block) -> bool:
    seen = False
    for line in block.lines:
        for run in line.runs:
            if not run.text.strip():
                continue
            if not run.bold:
                return False
            seen = True
    return seen


def classify_block(
    block: Block,
    profile: Profile,
    levels: dict[float, int],
    *,
    col_min: float,
    col_max: float,
) -> int:
    """Return the Markdown heading level for a block, or ``0`` for body text."""
    if block.kind != "para" or block.continues or not block.lines:
        return 0
    first = block.lines[0]
    if first.crowded:
        return 0
    plain = block_plain(block)
    if not plain or len(plain.split()) > _MAX_HEADING_WORDS:
        return 0
    flush = abs(first.x0 - col_min) <= _FLUSH_TOL
    center = (first.x0 + first.x1) / 2.0
    centered = abs(center - (col_min + col_max) / 2.0) <= _CENTER_TOL
    if not flush and not centered:
        return 0
    level = _tier_level(first.size, levels)
    if level:
        return level
    # No size signal: a strict bold-only fallback (ADR-0055).
    if not _whole_line_bold(block):
        return 0
    if _QUOTE_START_RE.match(plain) or plain.endswith("."):
        return 0
    if not _starts_sentence(plain):
        return 0
    if len(plain.split()) > _BOLD_ONLY_MAX_WORDS:
        return 0
    if block.start_gap is not None and block.start_gap <= profile.para_gap:
        return 0
    return 2
