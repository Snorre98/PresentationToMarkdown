"""Per-conversion ledger of model-produced Markdown content (ADR-0050).

Every optional AI pass that *lands* model output in a conversion's Markdown
records it here: the vision transcriptions (page, column, image, chart), a
grounded diagram interpretation, a paper-structure amendment, a Markdown
restructure that survived the word gate, and a model-generated RAG summary.
Decision-only passes (the classifier gate, the pre-flight need gate) record
nothing — they produce no content.

The ledger is what lets the conversion name its artifact
``<stem>-inference-used.md`` (see :mod:`converter.organize`), so a later
deterministic run — which writes the unmarked ``<stem>.md`` — can never
overwrite a file that cost model time to produce.

State lives in a :class:`contextvars.ContextVar`, like ``logstore``'s run tag:
one conversion per context, so a batch in one thread cannot leak into the next
file and the GUI's per-batch worker thread stays isolated. Nothing here raises
or changes conversion behaviour; a pass called outside :func:`tracking` (unit
tests calling a pass directly) simply records nothing.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

# Stable reporting order for :func:`passes` (registration order of the passes).
KNOWN_PASSES = ("vision", "interpret", "structure", "format", "summary")

_current: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "ptm_inference_passes", default=None
)


@contextmanager
def tracking() -> Iterator[None]:
    """Give one conversion a fresh, empty ledger for the duration of the block."""
    token = _current.set(set())
    try:
        yield
    finally:
        _current.reset(token)


def note(pass_name: str) -> None:
    """Record that ``pass_name`` contributed content to the current conversion.

    Idempotent per pass; a no-op outside :func:`tracking`, and never raises.
    """
    passes = _current.get()
    if passes is not None:
        passes.add(pass_name)


def used() -> bool:
    """Whether any model output landed in the current conversion."""
    return bool(_current.get())


def passes() -> list[str]:
    """The passes that contributed content, in :data:`KNOWN_PASSES` order."""
    current = _current.get() or set()
    return [name for name in KNOWN_PASSES if name in current]
