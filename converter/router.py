"""Deterministic pre-execution pipeline decisions (ADR-0024).

The optional AI passes used to make routing decisions with cheap *post-hoc*
signals scattered across ``pdf.py``, ``structure.py`` and ``classify.py`` —
deciding whether to call an expensive model only inside that model's own module,
and validating its output only *after* the call. This layer centralizes those
decisions as one legible place, resolving, per page and per pass, an action from
**cheap-before-expensive** signals in a strict order:

1. **skip** — a cheap deterministic signal already proves the step unnecessary or
   already handled (text-layer quality, garbage self-check, image readability,
   complexity, prior-pass outcome).
2. **downgrade** — the step can run on a cheaper model than the default (e.g. the
   structure text regime moves off the writer VLM onto a small text model).
3. **run** — none of the above; run the (possibly downgraded) model.

Everything here is deterministic and pure: it never issues a model call, raises,
or writes output. A decision can only make a conversion do *less* work, so it can
never change deterministic output or fail a conversion.
"""
from __future__ import annotations

import re
from enum import Enum

from converter.base import text_layer_is_garbage, text_layer_quality

# Deterministic "already clean" signals for the format pass (ADR-0039). A bold
# lead-in (``**Purpose:**``), a heading-like bullet, or a non-bullet line that
# does not end a sentence are the things the writer-VLM restructure exists to fix.
_BOLD_LEAD_RE = re.compile(r"^\s*\*\*[^*]+\*\*\s*:")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]\u201d\u2019]*$")


class Route(str, Enum):
    """The action a pipeline step should take for a given page/pass."""

    RUN = "run"
    SKIP = "skip"
    DOWNGRADE = "downgrade"


def structure_regime(line_meta: list[dict], md_lines: list[str]) -> str:
    """Resolve a paper-mode page's structure-pass regime: ``text``/``image``/``skip``.

    Single source of truth for the structure pass's routing (ADR-0011 + ADR-0023).
    Order matters and is cheap-before-expensive:

    - a page whose deterministic output is a raw ``<details>`` fallback has no
      usable text to amend (the ``image`` regime reads the rendered page instead);
    - otherwise a text layer that is not usable — sparse/empty, or *dense OCR
      garbage* that would only fail the verbatim word gate — is skipped pre-call;
    - only genuinely usable prose gets the expensive check-and-amend (``text``).
    """
    if any("<details" in line for line in md_lines):
        return "image"
    texts = [m.get("text", "") for m in line_meta]
    if text_layer_quality(texts) != "usable":
        return "skip"
    if text_layer_is_garbage(texts):
        return "skip"
    return "text"


def structure_text_downgrade() -> Route:
    """Whether the structure *text* regime should downgrade off the writer VLM.

    The text regime is a pure text-to-text check-and-amend: it never sends an
    image, so it does not need a vision-language model (ADR-0016's writer role).
    When the dedicated small text model is configured it resolves to
    ``DOWNGRADE``; the image regime still needs a VLM and stays on ``STRUCTURE_*``.
    This is a runtime, import-time *model choice*, not a per-page signal.
    """
    from converter.structure import STRUCTURE_TEXT_MODEL, STRUCTURE_MODEL

    if STRUCTURE_TEXT_MODEL and STRUCTURE_TEXT_MODEL != STRUCTURE_MODEL:
        return Route.DOWNGRADE
    return Route.RUN


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _has_deeper_bullet(lines: list[str], i: int) -> bool:
    """Whether line ``i`` (a bullet) is followed, before a blank line, by a more
    deeply indented bullet — the shape of a heading-like lead-in bullet."""
    base = _indent(lines[i])
    for nxt in lines[i + 1:]:
        if not nxt.strip():
            return False
        m = _BULLET_RE.match(nxt)
        if m:
            return _indent(nxt) > base
    return False


def format_clean(slide_md: str) -> bool:
    """Whether a slide's deterministic Markdown is already clean (ADR-0039).

    Conservative: returns ``True`` only when the slide clearly needs no
    reformatting, so the writer-VLM restructure can be skipped. A ``False``
    ("needs fix") merely routes the slide to the cheap-model residual gate, so
    this can only ever save work — never change output or fail a conversion.

    A slide is clean when none of the format pass's targets are present: no bold
    lead-in, no heading-like bullet, and no non-bullet line that ends
    mid-sentence (a wrapped fragment the pass would rejoin).
    """
    from converter.format import _is_editable

    lines = slide_md.splitlines()
    for i, line in enumerate(lines):
        if not _is_editable(line):
            continue
        s = line.strip()
        if _BOLD_LEAD_RE.match(s):
            return False
        if _BULLET_RE.match(line):
            if s.endswith(":") and _has_deeper_bullet(lines, i):
                return False
            continue
        if not _SENTENCE_END_RE.search(s):
            return False
    return True
