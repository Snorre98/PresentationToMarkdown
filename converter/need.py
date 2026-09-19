"""Batched pre-flight "needs fixing?" classifier (ADR-0039).

The format/structure passes rewrite every slide/page with an expensive writer
model and gate the result only afterwards. This module is the cheap, text-only
gate that predicts — in one batched call per document — which slides/pages
actually need that rewrite, so the expensive pass runs only on those. It is a
pure decision layer in effect: it never changes output, only saves work, and any
failure degrades to "don't gate" (run the expensive pass on everything).

Configuration (environment variables):

- ``NEED_GATE`` — lazy-read toggle: ``off`` / ``on`` / ``shadow`` (default ``on``).
- ``NEED_BASE_URL`` — defaults to the ``text`` server (small text model), the same
  server as ``STRUCTURE_TEXT_*`` / ``SUMMARY_*`` (ADR-0017).
- ``NEED_MODEL`` — defaults to the ``text`` server's model.
- ``NEED_API_KEY`` — optional bearer token (unused for local servers).
"""
from __future__ import annotations

import os
import re
import time

from converter import config
from converter.logstore import record
from converter.vision import _chat_completion

NEED_BASE_URL = os.environ.get(
    "NEED_BASE_URL", config.SERVERS.get("structure-text", config.SERVERS["summary"]).base_url
)
NEED_MODEL = os.environ.get(
    "NEED_MODEL", config.SERVERS.get("structure-text", config.SERVERS["summary"]).model
)
NEED_API_KEY = os.environ.get("NEED_API_KEY") or None

NEED_MAX_TOKENS = 256
_TIMEOUT = 300.0
_MAX_BATCH_CHARS = 12000


def need_gate() -> str:
    """The lazy-read ``NEED_GATE`` mode: ``off`` / ``on`` / ``shadow``.

    Read at call time (like ``PDF_MODE``) so a conversion can flip it without a
    restart; the CLI/GUI and the A/B harness force it explicitly.
    """
    return os.environ.get("NEED_GATE", "on").strip().lower() or "on"


_REFORMAT_PROMPT = (
    "Here are the slides of a converted presentation, numbered. Some still need "
    "reformatting: wrapped lines to rejoin into paragraphs, heading-like bullets "
    "to promote to `##` headings, bold lead-ins to promote, or list nesting to "
    "fix.\n\n{items}\n\n"
    "Answer with only the numbers of the slides that need reformatting, "
    "comma-separated (for example: 2, 5, 9). If none need it, answer exactly: none"
)

_RESTRUCTURE_PROMPT = (
    "Here are pages of a paper linearized from a multi-column PDF, numbered. Some "
    "pages still need restructuring: interleaved columns to reorder, missing "
    "`##` section headings, a title/authors block to fix, or footnotes to "
    "blockquote.\n\n{items}\n\n"
    "Answer with only the numbers of the pages that need restructuring, "
    "comma-separated (for example: 1, 3). If none need it, answer exactly: none"
)


def _parse_indices(reply: str, count: int) -> set[int] | None:
    """Map a model reply to 0-based indices, or ``None`` when unparseable.

    ``None`` means "could not decide" (run all). An explicit ``none``-style answer
    means "skip all" (empty set); digits map to 1-based numbers.
    """
    if not reply or not reply.strip():
        return None
    nums = [int(n) for n in re.findall(r"\d+", reply)]
    if not nums:
        if re.search(r"\b(none|nothing|no)\b", reply, re.IGNORECASE):
            return set()
        return None
    return {i for i in range(count) if (i + 1) in nums}


def _batches(items: list[str]) -> list[tuple[int, list[str]]]:
    """Split ``items`` into ``(start, chunk)`` groups under the char budget."""
    batches: list[tuple[int, list[str]]] = []
    start = 0
    budget = 0
    for i, item in enumerate(items):
        cost = len(item) + 32
        if i > start and budget + cost > _MAX_BATCH_CHARS:
            batches.append((start, items[start:i]))
            start = i
            budget = 0
        budget += cost
    if start < len(items):
        batches.append((start, items[start:]))
    return batches


def _classify_batch(
    items: list[str],
    template: str,
    stage: str,
    source: str,
    offset: int,
    page_nos: list[int],
) -> set[int] | None:
    numbered = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))
    user = template.format(items=numbered)
    t0 = time.perf_counter()
    try:
        reply, usage = _chat_completion(
            [{"role": "user", "content": user}],
            base_url=NEED_BASE_URL,
            model=NEED_MODEL,
            api_key=NEED_API_KEY,
            max_tokens=NEED_MAX_TOKENS,
            timeout=_TIMEOUT,
            return_usage=True,
        )
        reply = (reply or "").strip()
    except Exception as exc:  # noqa: BLE001 - a failed gate never blocks
        record(
            source=source,
            stage=stage,
            model=NEED_MODEL,
            decision="error",
            error=str(exc),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            base_url=NEED_BASE_URL,
        )
        return None
    latency_ms = int((time.perf_counter() - t0) * 1000)
    prompt_tokens = (usage or {}).get("prompt_tokens")
    generated_tokens = (usage or {}).get("completion_tokens", (usage or {}).get("generated_tokens"))
    indices = _parse_indices(reply, len(items))
    if indices is None:
        record(
            source=source,
            stage=stage,
            model=NEED_MODEL,
            decision="error",
            raw_answer=reply,
            error="unparseable reply",
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            generated_tokens=generated_tokens,
            base_url=NEED_BASE_URL,
        )
        return None
    for i, _item in enumerate(items):
        record(
            source=source,
            stage=stage,
            page=page_nos[offset + i],
            model=NEED_MODEL,
            decision="needs_fix" if i in indices else "clean",
            latency_ms=latency_ms,
            base_url=NEED_BASE_URL,
        )
    return {offset + i for i in indices}


def _classify(
    items: list[str],
    template: str,
    stage: str,
    source: str,
    page_nos: list[int] | None = None,
) -> set[int] | None:
    if page_nos is None:
        page_nos = list(range(1, len(items) + 1))
    if not items:
        return set()
    need: set[int] = set()
    for start, chunk in _batches(items):
        result = _classify_batch(chunk, template, stage, source, offset=start, page_nos=page_nos)
        if result is None:
            return None
        need.update(result)
    return need


def needs_reformat(slides: list[str], source: str = "") -> set[int] | None:
    """Indices of ``slides`` (0-based) that need the writer restructure.

    Returns ``None`` when the gate could not decide (run the expensive pass on
    all of them). ``slides`` are the slide Markdown strings; the returned indices
    are positions into that list.
    """
    return _classify(slides, _REFORMAT_PROMPT, "need-format", source)


def needs_restructure(
    pages: list[str], source: str = "", page_nos: list[int] | None = None
) -> set[int] | None:
    """Indices of ``pages`` (0-based) that need the structure check-and-amend.

    ``pages`` are page Markdown strings (a paper page's ``md_lines`` joined); the
    returned indices are positions into that list. ``page_nos`` are the 1-based
    page numbers to log (defaults to ``1..N``).
    """
    return _classify(pages, _RESTRUCTURE_PROMPT, "need-structure", source, page_nos=page_nos)
