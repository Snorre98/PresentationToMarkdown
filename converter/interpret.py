"""Grounded diagram-interpretation pass.

Whereas the vision pass *describes* a slide (verbatim text, or a high-level gist),
this pass *interprets* a diagram: it extracts the typed relationships the diagram
asserts ("A --supports--> B") and writes a short plain-language meaning.

The trick that keeps it honest is **grounding**: the deterministic layout pass
already extracts every text label on the page, so the model is given those labels
verbatim and asked only to *bind* them into relationships — never to re-read or
re-word them. A grounding gate then rejects any relationship whose entities or
relationship label are not in the supplied set, which is what prevents the
describe-the-description drift (inventing or paraphrasing labels).

Configuration (environment variables):

- ``INTERPRET_ENABLED`` — master switch. Default off.
- ``INTERPRET_BASE_URL`` — defaults to ``VISION_BASE_URL`` (reuses the transcriber).
- ``INTERPRET_MODEL`` — defaults to ``VISION_MODEL``. Point it at a larger model
  (e.g. ``mlx-community/Qwen2.5-VL-32B-Instruct-8bit``) on its own endpoint for
  higher-quality interpretation.
- ``INTERPRET_API_KEY`` — defaults to ``VISION_API_KEY``.
"""
from __future__ import annotations

import os
import re
import time

from converter import config
from converter.base import image_digest
from converter.logstore import record
from converter.vision import (
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
    _chat_completion,
    _image_content,
    image_mime,
    image_readable,
    transcription_quality,
)

INTERPRET_BASE_URL = os.environ.get("INTERPRET_BASE_URL", VISION_BASE_URL)
INTERPRET_MODEL = os.environ.get("INTERPRET_MODEL", VISION_MODEL)
INTERPRET_API_KEY = os.environ.get("INTERPRET_API_KEY", VISION_API_KEY)

INTERPRET_MAX_TOKENS = 1024

_readability_cache: dict[str, str | None] = {}

_PROMPT = (
    "This is a diagram or flowchart. Below are the text labels that appear on it, "
    "one per line.\n\n"
    "LABELS:\n{labels}\n\n"
    "Some labels name the boxes/entities; others are relationship labels written on "
    "the arrows or lines between them (for example \"supports\", \"hinders\", "
    "\"performs\", \"triggers\", \"depends on\", \"produces\", \"flow\").\n\n"
    "Report only the connections you can actually see, one per line, as three parts "
    "separated by a pipe character:\n"
    "  <entity A> | <relationship label> | <entity B>\n"
    "Use only the labels above, verbatim, for both entities and relationship labels.\n\n"
    "Then, after a blank line, write \"Meaning:\" followed by one or two plain-language "
    "sentences interpreting what the diagram conveys.\n\n"
    "Do not invent labels or connections you cannot read. Output only the connection "
    "lines and the Meaning line, nothing else."
)

_MEANING_RE = re.compile(r"^\s*meaning\s*[:：]?\s*", re.IGNORECASE)


def _normalize(s: str) -> str:
    s = s.strip().strip('"\u201c\u201d\u2018\u2019`*')
    return re.sub(r"\s+", " ", s).strip().lower()


def _matches(candidate: str, label: str) -> bool:
    c = _normalize(candidate)
    l = _normalize(label)
    if not c or not l:
        return False
    if c == l:
        return True
    if len(c) >= 4 and (c in l or l in c):
        return True
    return False


def _grounds(candidate: str, labels: list[str]) -> bool:
    return any(_matches(candidate, l) for l in labels)


def _parse(reply: str, labels: list[str]) -> tuple[list[tuple[str, str, str]], str]:
    """Split a model reply into grounded ``(A, type, B)`` triples and prose meaning."""
    statements: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    meaning_lines: list[str] = []
    in_meaning = False
    for raw in reply.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _MEANING_RE.match(line)
        if m:
            in_meaning = True
            rest = line[m.end():].strip()
            if rest:
                meaning_lines.append(rest)
            continue
        if in_meaning:
            meaning_lines.append(line)
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not all(parts):
            continue
        a, rel, b = parts
        if not (_grounds(a, labels) and _grounds(rel, labels) and _grounds(b, labels)):
            continue
        triple = (a, rel, b)
        if triple not in seen:
            seen.add(triple)
            statements.append(triple)
    meaning = " ".join(meaning_lines).strip()
    return statements, meaning


def _render(statements: list[tuple[str, str, str]], meaning: str) -> str:
    lines = ["**Relationships:**", ""]
    for a, rel, b in statements:
        lines.append(f"- `{a}` —`{rel}`→ `{b}`")
    if meaning:
        lines += ["", "**Meaning:**", "", meaning]
    return "\n".join(lines)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def interpret_diagram(
    png_bytes: bytes,
    labels: list[str],
    warnings: list[str] | None = None,
    log_ctx: dict | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str | None:
    """Interpret a rendered diagram page into grounded relationship statements.

    Returns Markdown (``**Relationships:**`` bullets plus an optional
    ``**Meaning:**`` paragraph), or ``None`` when disabled, unreadable, label-less,
    or rejected by the grounding/quality gates — so the caller falls back to the
    ordinary vision transcription (or raw text).
    """
    if not config.is_enabled("interpret"):
        return None
    labels = [l for l in (labels or []) if l.strip()]
    if not labels:
        return None
    ctx = log_ctx or {}
    digest = image_digest(png_bytes)
    if digest not in _readability_cache:
        _readability_cache[digest] = image_readable(png_bytes, "png", width=width, height=height)
    reason = _readability_cache[digest]
    if reason is not None:
        if warnings is not None:
            warnings.append(f"Skipping unreadable page render for interpretation ({reason})")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_digest=digest,
            stage="interpret",
            model=INTERPRET_MODEL,
            decision=reason,
            base_url=INTERPRET_BASE_URL,
        )
        return None

    prompt = _PROMPT.format(labels="\n".join(labels))
    t0 = time.perf_counter()
    try:
        reply = _chat_completion(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        _image_content(png_bytes, image_mime("png")),
                    ],
                }
            ],
            base_url=INTERPRET_BASE_URL,
            model=INTERPRET_MODEL,
            api_key=INTERPRET_API_KEY,
            max_tokens=INTERPRET_MAX_TOKENS,
            timeout=600.0,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        if warnings is not None:
            warnings.append(f"Diagram interpretation failed: {exc}")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_digest=digest,
            stage="interpret",
            model=INTERPRET_MODEL,
            latency_ms=_ms(t0),
            error=str(exc),
            base_url=INTERPRET_BASE_URL,
        )
        return None

    reason = transcription_quality(reply)
    if reason is not None:
        if warnings is not None:
            warnings.append(f"Discarding low-value diagram interpretation ({reason})")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_digest=digest,
            stage="interpret",
            model=INTERPRET_MODEL,
            latency_ms=_ms(t0),
            markdown=reply,
            error=f"quality gate: {reason}",
            base_url=INTERPRET_BASE_URL,
        )
        return None

    statements, meaning = _parse(reply, labels)
    if not statements:
        if warnings is not None:
            warnings.append("Discarding diagram interpretation (no grounded relationships)")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_digest=digest,
            stage="interpret",
            model=INTERPRET_MODEL,
            latency_ms=_ms(t0),
            markdown=reply,
            error="quality gate: no grounded relationships",
            base_url=INTERPRET_BASE_URL,
        )
        return None

    md = _render(statements, meaning)
    record(
        source=ctx.get("source", ""),
        page=ctx.get("page"),
        image_digest=digest,
        stage="interpret",
        model=INTERPRET_MODEL,
        latency_ms=_ms(t0),
        markdown=md,
        base_url=INTERPRET_BASE_URL,
    )
    return md
