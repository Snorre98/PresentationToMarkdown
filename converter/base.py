"""Shared interface and markdown helpers for all file-format converters.

Each concrete converter lives in its own module (``pptx.py``, ``pdf.py``) and
subclasses :class:`Converter`. Dispatch by file extension happens through
:class:`ConverterRegistry`.
"""
from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from converter._english_words import ENGLISH_WORDS

ProgressCallback = Callable[[int, int, str], None]
PageProgressCallback = Callable[[int, int, str], None]

_MD_SPECIALS = str.maketrans({"\\": "\\\\", "`": "\\`", "*": "\\*", "_": "\\_"})
_MD_SPECIALS_NO_UNDERSCORE = str.maketrans({"\\": "\\\\", "`": "\\`", "*": "\\*"})

# Images whose content appears on at least this fraction of slides/pages are
# treated as recurring (logos/watermarks): shown inline once, then as a link.
REPEATED_IMAGE_THRESHOLD = 0.8


@dataclass
class ConvertResult:
    source_path: Path
    md_path: Path | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class Converter(ABC):
    """Convert one file format to a Markdown file plus an assets folder."""

    extensions: tuple[str, ...] = ()

    @abstractmethod
    def convert(
        self,
        path: Path,
        output_dir: Path,
        progress_callback: PageProgressCallback | None = None,
        output_stem: str | None = None,
    ) -> ConvertResult:
        """Convert ``path``, writing ``<stem>.md`` into ``output_dir``.

        ``progress_callback``, when given, is called once per slide/page as
        ``(page, page_total, name)`` from the main emission loop.

        ``output_stem`` overrides the output name (defaults to ``path.stem``);
        used by ``duplicate_if_exists`` to write ``stem (2).md`` etc. without
        clobbering an existing conversion.
        """


class ConverterRegistry:
    """Maps file extensions to :class:`Converter` implementations."""

    def __init__(self) -> None:
        self._converters: dict[str, type[Converter]] = {}

    def register(self, converter: type[Converter]) -> type[Converter]:
        for ext in converter.extensions:
            self._converters[ext.lower()] = converter
        return converter

    def get(self, path: Path) -> Converter | None:
        converter_cls = self._converters.get(path.suffix.lower())
        if converter_cls is None:
            return None
        return converter_cls()

    @property
    def supported_extensions(self) -> set[str]:
        return set(self._converters)


registry = ConverterRegistry()


def _link_dest(rel: str) -> str:
    """Percent-encode a relative path for use as a Markdown link destination.

    Spaces and other URL-unsafe characters (``(``, ``#``, ``%``, …) break link
    parsing in Obsidian and other CommonMark renderers; ``/`` is kept as-is.
    """
    return quote(rel, safe="/")


def _escape(text: str, *, underscore: bool = True) -> str:
    table = _MD_SPECIALS if underscore else _MD_SPECIALS_NO_UNDERSCORE
    text = text.translate(table)
    if text.startswith("#"):
        text = "\\" + text
    return text


def _format_md(text: str, bold: bool = False, italic: bool = False) -> str:
    """Escape and wrap a text span with bold/italic Markdown markers.

    ``_`` is left unescaped when the span is italic, since a single underscore
    inside ``*...*`` cannot close the italic marker (fixes ``<short_name>``).
    """
    text = _escape(text, underscore=not italic)
    if not text:
        return ""
    if bold and italic:
        return f"***{text}***"
    if bold:
        return f"**{text}**"
    if italic:
        return f"*{text}*"
    return text


# Text-layer quality thresholds: below these the layer is treated as OCR
# garbage and routed to the vision path (ADR-0020) rather than emitted verbatim.
_TEXT_QUALITY_MIN_WORDS = 12
_TEXT_QUALITY_MIN_TTR = 0.35
_TEXT_QUALITY_MIN_MEAN_LEN = 10.0


def text_layer_quality(texts: list[str]) -> str:
    """Classify a page's text layer as ``usable`` / ``sparse`` / ``empty``.

    ``sparse`` covers both genuinely thin pages and OCR-garbage layers: too few
    content words (4+ alphanumeric), a low unique-word ratio (repeated OCR
    artifacts), or fragmentary mean line length. Callers use this to route a
    page to the vision path instead of emitting the layer verbatim.
    """
    words = re.findall(r"[A-Za-z0-9]{4,}", " ".join(texts).lower())
    if not words:
        return "empty"
    if len(words) < _TEXT_QUALITY_MIN_WORDS:
        return "sparse"
    ttr = len(set(words)) / len(words)
    mean_len = sum(len(t.strip()) for t in texts) / max(len(texts), 1)
    if ttr < _TEXT_QUALITY_MIN_TTR or mean_len < _TEXT_QUALITY_MIN_MEAN_LEN:
        return "sparse"
    return "usable"


# Garbage-layer thresholds for :func:`text_layer_is_garbage`: below the token
# floor the page is too thin to judge and is trusted, and a page must carry at
# least this many junk tokens (vowel-less / digit-only / typo near-misses) to be
# treated as OCR garbage rather than prose with the odd citation.
_GARBAGE_MIN_TOKENS = 60
_GARBAGE_MIN_JUNK = 2


def text_layer_is_garbage(texts: list[str]) -> bool:
    """Whether a text layer that *looks* usable is in fact OCR garbage.

    :func:`text_layer_quality` only measures word count, uniqueness and line
    length, so a garbled layer whose tokens are plausible-length near-misses of
    real words (``metagoa`` for "metagoal", ``gmnes`` for "games", ``numbdr`` for
    "number") passes as "usable". The structure pass's verbatim word-gate then
    correctly rejects any rewrite of it — but only *after* an expensive model
    call. This self-check predicts that rejection pre-call (ADR-0023).

    A token is junk when it holds no vowel (``gmnes``, pure digits ``1955``) or
    is one edit (deletion, transposition, substitution, insertion) away from a
    bundled English word (``unable`` -> "uncerta"). Words longer than the bundled
    vocabulary are never judged, and pages with too few tokens overall are never
    flagged, so thin legitimate pages and domain terms cannot false-positive.
    """
    words = re.findall(r"[A-Za-z0-9]{4,}", " ".join(texts).lower())
    if len(words) < _GARBAGE_MIN_TOKENS:
        return False
    junk = 0
    for word in words:
        core = re.sub(r"[^a-z]", "", word)
        if not core or not any(c in "aeiouy" for c in core):
            junk += 1
        elif core not in ENGLISH_WORDS and _near_miss(core):
            junk += 1
        if junk >= _GARBAGE_MIN_JUNK:
            return True
    return False


_NEAR_MISS_ALPHA = "abcdefghijklmnopqrstuvwxyz"


def _near_miss(core: str) -> bool:
    """Whether ``core`` is one edit (del/trans/sub/ins) from a bundled word.

    Words of 13+ chars are not in the bundled set (by design) and return False,
    so long domain terms are never treated as garbage.
    """
    n = len(core)
    if n > 12:
        return False
    for i in range(n):
        if core[:i] + core[i + 1 :] in ENGLISH_WORDS:
            return True
    for i in range(n - 1):
        if core[:i] + core[i + 1] + core[i] + core[i + 2 :] in ENGLISH_WORDS:
            return True
    for i in range(n):
        prefix, suffix = core[:i], core[i + 1 :]
        for c in _NEAR_MISS_ALPHA:
            if prefix + c + suffix in ENGLISH_WORDS:
                return True
    for i in range(n + 1):
        prefix, suffix = core[:i], core[i:]
        for c in _NEAR_MISS_ALPHA:
            if prefix + c + suffix in ENGLISH_WORDS:
                return True
    return False


def image_digest(blob: bytes) -> str:
    """Return a short content digest for an image blob."""
    return hashlib.md5(blob).hexdigest()[:8]


def repeated_image_hashes(
    per_slide: list[set[str]], threshold: float = REPEATED_IMAGE_THRESHOLD
) -> set[str]:
    """Return image digests that appear on at least ``threshold`` of slides/pages.

    ``per_slide`` maps each slide/page to the set of image digests it contains.
    """
    total = len(per_slide)
    if total == 0:
        return set()
    counts: Counter = Counter()
    for hashes in per_slide:
        counts.update(hashes)
    min_slides = max(2, math.ceil(total * threshold))
    return {digest for digest, n in counts.items() if n >= min_slides}


def _table_to_md(rows: list[list[str]]) -> list[str]:
    """Render a list of rows (list of cells) as a Markdown pipe table."""
    lines: list[str] = []
    if not rows:
        return lines

    def cell_text(cell: str) -> str:
        return (cell or "").replace("|", "\\|").replace("\n", "<br>").strip()

    def render_row(row: list[str]) -> str:
        return "| " + " | ".join(cell_text(c) for c in row) + " |"

    header = render_row(rows[0])
    separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
    lines.extend([header, separator])
    for row in rows[1:]:
        lines.append(render_row(row))
    return lines


def write_image(
    blob: bytes,
    ext: str,
    assets_dir: Path,
    stem: str,
    counter: list[int],
    warnings: list[str],
    dedup: dict[str, str] | None = None,
) -> str | None:
    """Write an image blob to ``assets/<stem>/`` and return its filename.

    ``dedup`` is an optional ``{digest: filename}`` map; when provided, identical
    content is written only once and the existing filename is returned.
    """
    try:
        digest = image_digest(blob)
        if dedup is not None and digest in dedup:
            return dedup[digest]
        ext = (ext or "bin").lstrip(".")
        filename = f"{stem}_{counter[0]:02d}_{digest}.{ext}"
        counter[0] += 1
        (assets_dir / filename).write_bytes(blob)
        if dedup is not None:
            dedup[digest] = filename
        return filename
    except Exception as exc:
        warnings.append(f"Could not write image: {exc}")
        return None
