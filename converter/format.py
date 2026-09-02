"""Markdown polish post-pass for converted slides.

Applied after the deterministic converters produce their output, this module
improves the *formatting* of the generated Markdown without changing its
content:

- **Deterministic** (always on) — strip trailing whitespace, collapse excess
  blank lines, and normalise heading spacing. Pure Python, no dependencies.
- **LLM restructure** (opt-in) — hand each slide to a local OpenAI-compatible
  chat model to reflow wrapped lines into paragraphs and promote heading-like
  bullets into ``##``/``###`` headings, while keeping every word verbatim.

Configuration (environment variables):

- ``FORMAT_ENABLED`` — master switch for the LLM pass. Default off.
- ``FORMAT_BASE_URL`` — defaults to ``WRITE_BASE_URL``.
- ``FORMAT_MODEL`` — defaults to ``WRITE_MODEL``.
- ``FORMAT_API_KEY`` — defaults to ``WRITE_API_KEY``.
"""
from __future__ import annotations

import os
import re

from converter import config
from converter.vision import _chat_completion, _words, verify_no_omissions
from converter.write import WRITE_API_KEY, WRITE_BASE_URL, WRITE_MODEL

FORMAT_BASE_URL = os.environ.get("FORMAT_BASE_URL", WRITE_BASE_URL)
FORMAT_MODEL = os.environ.get("FORMAT_MODEL", WRITE_MODEL)
FORMAT_API_KEY = os.environ.get("FORMAT_API_KEY", WRITE_API_KEY)

FORMAT_MAX_TOKENS = 4096

_PAGEBREAK = '<div style="page-break-after: always; break-after: page;"></div>'

_PROMPT = (
    "Reformat this lecture slide into cleaner Markdown while keeping every word "
    "exactly as it is.\n\n"
    "Do NOT add, remove, reword, reorder, or translate any text, numbers, or URLs.\n\n"
    "Only these structural changes are allowed:\n"
    "- Rejoin lines that were wrapped mid-sentence into single paragraphs.\n"
    "- Promote a bullet whose text reads as a heading (short, title-style, "
    "followed by sub-bullets) into a `##` or `###` heading, keeping the lead-in "
    "text verbatim and moving its sub-bullets under it.\n"
    "- Promote a bolded lead-in such as `**Purpose:**` into a `##` heading (or "
    "keep it as a bold paragraph if that reads better).\n"
    "- Fix list nesting and blank-line spacing.\n\n"
    "Keep the slide title, image links, page links, tables, blockquotes, "
    "`<details>` blocks, fenced code, and the page-break `<div>` exactly as-is.\n\n"
    "Output only the Markdown, with no commentary or code fences."
)

# Lines the model must reproduce verbatim: headings, image/page links, blockquotes,
# tables, `<details>`/`<div>` blocks, fenced code, and horizontal rules.
_STRUCTURAL_RE = re.compile(
    r"^\s*(\!\[|\[Page\s+\d+\]\(|\[image\]\(|\>|<div|<details|</details>|```|\||\-\-\-)"
)


def _is_structural(line: str) -> bool:
    s = line.strip()
    return bool(s.startswith("#")) or bool(_STRUCTURAL_RE.match(line))


def _is_editable(line: str) -> bool:
    return bool(line.strip()) and not _is_structural(line)


def _deterministic_pass(md: str) -> str:
    """Strip trailing whitespace, collapse 3+ blank lines to 2, and space headings."""
    out: list[str] = []
    fence = False
    blank_run = 0
    prev_heading = False
    for raw in md.split("\n"):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            fence = not fence
        if line == "":
            blank_run += 1
            if blank_run > 2:
                continue
            out.append("")
            prev_heading = False
            continue
        blank_run = 0
        if not fence and line.startswith("#"):
            if out and out[-1] != "":
                out.append("")
            out.append(line)
            prev_heading = True
        else:
            if prev_heading:
                out.append("")
            out.append(line)
            prev_heading = False
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _iter_slides(md: str) -> list[str]:
    """Split a document into slides at top-level ``# `` headings (fence-aware)."""
    slides: list[list[str]] = []
    current: list[str] = []
    fence = False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("# ") and current:
            slides.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        slides.append(current)
    return ["\n".join(s) for s in slides]


def _peel_trailer(lines: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(body, trailer)`` where ``trailer`` is the page-break + ``---`` block."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == _PAGEBREAK:
            body = lines[:i]
            trailer = lines[i:]
            while body and not body[-1].strip():
                body.pop()
            while trailer and not trailer[-1].strip():
                trailer.pop()
            return body, trailer
    return lines, []


def _anchors_intact(anchors: list[str], reformatted: str) -> bool:
    present = {line.strip() for line in reformatted.split("\n")}
    return all(anchor in present for anchor in anchors)


def _verify_preserved(original: str, reformatted: str) -> list[str]:
    """Return a list of problems if the reformat dropped or invented content words."""
    problems: list[str] = []
    missing = verify_no_omissions(original, reformatted)
    if missing:
        problems.append("omitted: " + ", ".join(missing[:5]))
    added = sorted(_words(reformatted) - _words(original))
    if added:
        problems.append("added: " + ", ".join(added[:5]))
    return problems


def _reformat_slide(slide: str) -> str:
    body, trailer = _peel_trailer(slide.split("\n"))
    if not body or not any(_is_editable(line) for line in body):
        return slide
    anchors = [line.strip() for line in body if _is_structural(line)]
    original = "\n".join(body)
    reformatted = _chat_completion(
        [{"role": "user", "content": _PROMPT + "\n\nSlide:\n\n" + original}],
        base_url=FORMAT_BASE_URL,
        model=FORMAT_MODEL,
        api_key=FORMAT_API_KEY,
        max_tokens=FORMAT_MAX_TOKENS,
        timeout=600.0,
    ).strip()
    if not _anchors_intact(anchors, reformatted):
        return slide
    if _verify_preserved(original, reformatted):
        return slide
    result = reformatted.rstrip()
    if trailer:
        result += "\n\n" + "\n".join(trailer)
    return result


def _llm_pass(md: str, warnings: list[str]) -> str:
    try:
        return "\n\n".join(_reformat_slide(slide) for slide in _iter_slides(md))
    except Exception as exc:  # noqa: BLE001 - degrade to deterministic-only output
        warnings.append(f"Markdown LLM reformat failed: {exc}; keeping deterministic output")
        return md


def polish_text(md: str, warnings: list[str] | None = None) -> str:
    """Normalise formatting of converted Markdown, optionally restructuring via an LLM.

    Returns the polished text (no trailing newline). Content is preserved: the
    deterministic pass only touches whitespace, and the LLM pass is gated by a
    word cross-check that rejects any reformat which drops or invents content.
    """
    text = _deterministic_pass(md)
    if config.is_enabled("format") and text:
        text = _deterministic_pass(_llm_pass(text, warnings or []))
    return text
