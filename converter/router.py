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

from enum import Enum

from converter.base import text_layer_is_garbage, text_layer_quality


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
