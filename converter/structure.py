"""Paper-mode document-structure LLM pass (opt-in).

The deterministic paper-mode converter (``converter/pdf.py``) reconstructs
title/authors/headings/footnotes/references with geometry heuristics. This
optional post-pass improves that structure with a local OpenAI-compatible chat
model, per page, gated by a deterministic confidence signal:

- **Text regime** — pages with a usable text layer are *check-and-amended*:
  the model may fix the page-1 title/authors block, blockquote the abstract,
  add ``##`` headings, wrap footnotes, insert a ``## References`` heading, and
  reorder/rejoin lines to fix multi-column linearization — while every content
  word must stay verbatim (a word cross-check rejects any page that omits or
  invents prose).
- **Image regime** — pages whose text layer is unusable (scans, garbage OCR,
  the ``<details>`` raw-text fallback) are *reworded from the rendered page
  image* by the same pass, gated by image readability + transcription quality
  instead of the verbatim word gate.
- **Skip** — pages already handled by the interpret/vision passes are left
  alone.

Every model call and gate failure is local to its page: the page keeps its
deterministic Markdown and a warning is appended. The pass never blocks or
fails a conversion, and is disabled by default.

Configuration (environment variables):

- ``STRUCTURE_ENABLED`` — master switch. Default off.
- ``STRUCTURE_BASE_URL`` — defaults to ``FORMAT_BASE_URL`` then ``VISION_BASE_URL``.
- ``STRUCTURE_MODEL`` — defaults to ``FORMAT_MODEL`` then ``VISION_MODEL`` (a VLM,
  so the image regime needs no extra server).
- ``STRUCTURE_API_KEY`` — defaults to ``FORMAT_API_KEY`` then ``VISION_API_KEY``.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from converter.format import (
    FORMAT_API_KEY,
    FORMAT_BASE_URL,
    FORMAT_MODEL,
    _anchors_intact,
    _is_structural,
)
from converter.logstore import record
from converter.vision import (
    _chat_completion,
    _image_content,
    image_readable,
    transcription_quality,
    verify_no_omissions,
)

STRUCTURE_ENABLED = os.environ.get("STRUCTURE_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STRUCTURE_BASE_URL = os.environ.get("STRUCTURE_BASE_URL", FORMAT_BASE_URL)
STRUCTURE_MODEL = os.environ.get("STRUCTURE_MODEL", FORMAT_MODEL)
STRUCTURE_API_KEY = os.environ.get("STRUCTURE_API_KEY", FORMAT_API_KEY)

STRUCTURE_MAX_TOKENS = 6000
_TIMEOUT = 600.0

# Confidence-gate thresholds for the raw text layer.
_MIN_WORDS = 12  # below this many content words the layer is "sparse"
_MIN_TTR = 0.35  # below this unique-word ratio the text is likely OCR garbage
_MIN_MEAN_LEN = 10.0  # below this mean line length the text is fragmentary

# Structural block markers a model may legally add as heading text (beyond
# words that are already grounded in the document's own text).
_ALLOWED_HEADING_WORDS = frozenset(
    {
        "references",
        "bibliography",
        "abstract",
        "appendix",
        "acknowledgments",
        "acknowledgements",
        "notes",
        "footnote",
        "footnotes",
        "glossary",
        "abbreviations",
        "notation",
        "introduction",
        "conclusion",
        "methods",
        "methodology",
        "results",
        "discussion",
        "related",
        "work",
    }
)


@dataclass
class PageData:
    """One paper-mode page: the deterministic Markdown lines plus raw metadata.

    ``md_lines`` are exactly the lines ``PDFConverter.convert`` would write for
    the page (including the ``# `` heading, the ``[Page N](...)`` link and any
    trailing blank line). ``line_meta`` carries the raw per-line text +
    coordinates/size/bold in reading order (text regime input and coverage).
    ``png`` is the rendered page image (image regime), ``pno`` the 1-based page
    number.
    """

    md_lines: list[str]
    line_meta: list[dict]
    png: bytes | None = None
    pno: int = 1


# Structural lines that are exempt from the anchor check per regime.
_RAW_DETAILS_MARKERS = ("<details", "</details>", "<summary")


def _word_text(text: str) -> list[str]:
    """Content words of ``text`` (4+ chars) for coverage scoring."""
    return re.findall(r"[A-Za-z0-9]{4,}", text)


def _text_coverage(line_meta: list[dict]) -> str:
    """Classify a page's raw text layer as ``usable`` / ``sparse`` / ``empty``.

    The confidence signal: pages with too few words, a low unique-word ratio
    (repeated OCR garbage) or fragmentary line lengths cannot be given semantic
    structure from the text layer and must go to the image regime instead.
    """
    words = _word_text(" ".join(m.get("text", "") for m in line_meta))
    if not words:
        return "empty"
    if len(words) < _MIN_WORDS:
        return "sparse"
    ttr = len({w.lower() for w in words}) / len(words)
    texts = [m.get("text", "") for m in line_meta]
    mean_len = sum(len(t.strip()) for t in texts) / max(len(texts), 1)
    if ttr < _MIN_TTR or mean_len < _MIN_MEAN_LEN:
        return "sparse"
    return "usable"


def _page_regime(page: PageData) -> str:
    """Route a page to the ``text`` / ``image`` / ``skip`` regime."""
    if any("<details" in line for line in page.md_lines):
        return "image"
    if _text_coverage(page.line_meta) == "usable":
        return "text"
    return "skip"


def _norm(text: str) -> str:
    """Normalise a Markdown line for matching it back to its source line."""
    s = text.strip()
    s = re.sub(r"^#{1,6}\s+", "", s)
    s = re.sub(r"^-\s+", "", s)
    s = re.sub(r"\*\*+|\*", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _match_meta(line: str, line_meta: list[dict]) -> tuple[float, bool, float]:
    """Return ``(size, bold, x0)`` for a Markdown line via source-line matching.

    Best-effort: the deterministic output reorders/merges lines, so this finds
    the first source line whose normalised text matches (equal, or a contained
    fragment). Unmatched lines (tables, links) get neutral defaults.
    """
    n = _norm(line)
    for meta in line_meta:
        m = _norm(meta.get("text", ""))
        if not n or not m:
            continue
        if n == m or (len(n) >= 4 and n in m) or (len(m) >= 4 and m in n):
            return float(meta.get("size", 0.0)), bool(meta.get("bold", False)), float(meta.get("x0", 0.0))
    return 0.0, False, 0.0


def _numbered_lines(page: PageData) -> str:
    """Build the numbered line list (with layout metadata) shown to the model.

    The ``# `` page heading and ``[Page N](...)`` link are excluded: they are
    anchors the model must reproduce byte-exact, not annotate.
    """
    out: list[str] = []
    for line in page.md_lines:
        s = line.strip()
        if not s or s.startswith("# ") or s.startswith("[Page "):
            continue
        size, bold, x0 = _match_meta(s, page.line_meta)
        flag = "B" if bold else "-"
        out.append(f"{len(out) + 1}  [{size:.1f} {flag} x={x0:.0f}]  {s}")
    return "\n".join(out)


_AMEND_PROMPT = (
    "You are restructuring the Markdown of one page of an academic paper that was "
    "linearized from a multi-column PDF. Below are the page's lines with line "
    "numbers and layout metadata ([font-size bold x0]), followed by the page's "
    "current Markdown.\n\n"
    "Reproduce the Markdown with ONLY these structural amendments:\n"
    "- Keep every word of the body text verbatim. Never reword, add, translate or "
    "\"fix\" OCR-garbled words.\n"
    "- On page 1 only, fix the paper title (`# ...`) and authors (`*...*`) block.\n"
    "- Blockquote the abstract with `> ` lines.\n"
    "- Promote lines that are real section headings to `## ...` headings, keeping "
    "their words verbatim. Never demote or remove an existing heading.\n"
    "- Wrap footnote lines as a blockquote block.\n"
    "- Insert `## References` (and, if present, `## Abstract`, `## Notes`, ...) "
    "headings where the corresponding block starts. New heading text may only be "
    "a structural marker (References, Abstract, Bibliography, Acknowledgements, "
    "Notes, Appendix, ...) — never invent prose headings.\n"
    "- Rejoin wrapped lines into paragraphs and, where multi-column linearization "
    "interleaved two columns' lines, reorder lines into the correct reading "
    "order.\n"
    "- Keep the `# ` page heading, the `[Page N](...)` image link, all pipe "
    "tables, blockquotes, `<details>` blocks and fenced code exactly as they are.\n\n"
    "Numbered lines with metadata:\n{numbered}\n\n"
    "Current Markdown:\n{md}\n\n"
    "Output only the amended Markdown, with no commentary or code fences."
)

_IMAGE_PROMPT = (
    "This page of an academic paper has no usable extracted text layer. Read the "
    "page image and produce its Markdown: the paper title and authors if present, "
    "any abstract, `## ` section headings, body paragraphs, footnote blocks, and a "
    "`## References` section if present.\n\n"
    "Rules:\n"
    "- Transcribe text verbatim from the image; do not invent text you cannot "
    "read; do not add commentary.\n"
    "- Keep the `[Page N](...)` image link line exactly as it is.\n"
    "- Include exactly one `# ` page heading line (on pages after the first you "
    "may replace `# Page N` with the real page title).\n"
    "- Render tables as pipe tables and preserve bold/italic where present.\n\n"
    "Output only the Markdown, with no commentary or code fences."
)


def _heading_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("# "))


def _link_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip().startswith("[Page "):
            return line.strip()
    return None


def _added_words_allowed(word: str, original: str, reply: str) -> bool:
    """Whether an added word is legal: heading-only AND (structural OR grounded).

    ``word`` must appear on a heading line in the reply (``# ``/``## ``) and
    nowhere in the reply's prose; it must then be a structural block marker or
    already occur in the document's own text (so ``## References`` is grounded
    when the paper contains the word "references").
    """
    if re.search(rf"\b{re.escape(word)}\b", _prose_text(reply), re.IGNORECASE):
        return False
    if not re.search(rf"\b{re.escape(word)}\b", _heading_text(reply), re.IGNORECASE):
        return False
    if word in _ALLOWED_HEADING_WORDS:
        return True
    return bool(re.search(rf"\b{re.escape(word)}\b", original, re.IGNORECASE))


def _heading_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith("#")
    )


def _prose_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _verify_preserved(original: str, reply: str) -> list[str]:
    """Return problems if the amendment dropped or invented content words."""
    problems: list[str] = []
    missing = verify_no_omissions(original, reply)
    if missing:
        problems.append("omitted: " + ", ".join(missing[:5]))
    added = {w for w in re.findall(r"[A-Za-z0-9]{4,}", reply.lower()) if len(w) >= 4} - {
        w for w in re.findall(r"[A-Za-z0-9]{4,}", original.lower()) if len(w) >= 4
    }
    illegal = sorted(w for w in added if not _added_words_allowed(w, original, reply))
    if illegal:
        problems.append("added: " + ", ".join(illegal[:5]))
    return problems


def _anchors_for(page: PageData, regime: str) -> list[str]:
    """Structural lines the amended page must reproduce byte-exact."""
    anchors: list[str] = []
    for line in page.md_lines:
        s = line.strip()
        if not s:
            continue
        if not _is_structural(s):
            continue
        if regime == "text" and page.pno == 1 and s.startswith("# "):
            continue  # page-1 title line is the amendment target
        if regime == "image" and s.startswith(_RAW_DETAILS_MARKERS):
            continue  # raw-text block is replaced by the image reword
        anchors.append(s)
    return anchors


def _amend_page_text(page: PageData, warnings: list[str], source: str) -> list[str] | None:
    """Text regime: check-and-amend the page, verbatim-gated."""
    anchors = _anchors_for(page, "text")
    original = "\n".join(page.md_lines)
    numbered = _numbered_lines(page)
    if not numbered.strip():
        return None
    user = _AMEND_PROMPT.format(numbered=numbered, md=original)
    t0 = time.perf_counter()
    try:
        reply = _chat_completion(
            [{"role": "user", "content": user}],
            base_url=STRUCTURE_BASE_URL,
            model=STRUCTURE_MODEL,
            api_key=STRUCTURE_API_KEY,
            max_tokens=STRUCTURE_MAX_TOKENS,
            timeout=_TIMEOUT,
        ).strip()
    except Exception as exc:  # noqa: BLE001 - degrade to deterministic output
        if warnings is not None:
            warnings.append(f"Structure pass failed on page {page.pno}: {exc}")
        _log(source, page, decision="error", error=str(exc), latency_ms=_ms(t0))
        return None
    if not reply:
        _reject(page, warnings, "empty reply")
        _log(source, page, decision="rejected", error="empty reply", latency_ms=_ms(t0))
        return None
    if not _anchors_intact(anchors, reply):
        _reject(page, warnings, "dropped structural lines")
        _log(source, page, decision="rejected", error="anchors dropped", latency_ms=_ms(t0))
        return None
    if _heading_count(reply) != 1:
        _reject(page, warnings, "heading invariant")
        _log(source, page, decision="rejected", error="heading invariant", latency_ms=_ms(t0))
        return None
    problems = _verify_preserved(original, reply)
    if problems:
        _reject(page, warnings, "; ".join(problems))
        _log(source, page, decision="rejected", error="; ".join(problems), latency_ms=_ms(t0))
        return None
    _log(source, page, decision="amended", latency_ms=_ms(t0))
    return reply.split("\n") + [""]


def _amend_page_image(page: PageData, warnings: list[str], source: str) -> list[str] | None:
    """Image regime: reword the page from its rendered image."""
    if not page.png:
        return None
    try:
        reason = image_readable(page.png, "png")
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
    if reason is not None:
        if warnings is not None:
            warnings.append(
                f"Structure pass: page {page.pno} image unreadable ({reason}); keeping raw text"
            )
        _log(source, page, decision="skip", error=f"unreadable: {reason}")
        return None
    original = "\n".join(page.md_lines)
    t0 = time.perf_counter()
    try:
        reply = _chat_completion(
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": _IMAGE_PROMPT}, _image_content(page.png)],
                }
            ],
            base_url=STRUCTURE_BASE_URL,
            model=STRUCTURE_MODEL,
            api_key=STRUCTURE_API_KEY,
            max_tokens=STRUCTURE_MAX_TOKENS,
            timeout=_TIMEOUT,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        if warnings is not None:
            warnings.append(f"Structure pass failed on page {page.pno}: {exc}")
        _log(source, page, decision="error", error=str(exc), latency_ms=_ms(t0))
        return None
    reason = transcription_quality(reply)
    if reason is not None or not reply:
        if warnings is not None:
            warnings.append(
                f"Structure pass discarded page {page.pno} transcription ({reason or 'empty'})"
            )
        _log(source, page, decision="rejected", error=f"quality gate: {reason}", latency_ms=_ms(t0))
        return None
    if _heading_count(reply) != 1:
        _reject(page, warnings, "heading invariant")
        _log(source, page, decision="rejected", error="heading invariant", latency_ms=_ms(t0))
        return None
    link = _link_line(original)
    if link is not None and link not in {l.strip() for l in reply.splitlines()}:
        _reject(page, warnings, "page link dropped")
        _log(source, page, decision="rejected", error="page link dropped", latency_ms=_ms(t0))
        return None
    _log(source, page, decision="reworded", latency_ms=_ms(t0))
    return reply.split("\n") + [""]


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _reject(page: PageData, warnings: list[str] | None, reason: str) -> None:
    """Append a per-page rejection warning (degrade to deterministic output)."""
    if warnings is not None:
        warnings.append(f"Structure pass rejected page {page.pno} ({reason})")


def _log(source: str, page: PageData, **kw) -> None:
    record(
        source=source,
        page=page.pno,
        stage="structure",
        model=STRUCTURE_MODEL,
        base_url=STRUCTURE_BASE_URL,
        **kw,
    )


def structure_paper(
    pages: list[PageData],
    warnings: list[str] | None = None,
    source: str = "",
) -> list[str] | None:
    """Run the structure pass over paper-mode pages, returning amended Markdown lines.

    Returns ``None`` (the caller keeps the deterministic output) when disabled,
    given no pages, or when nothing was amended. Never raises: any failure
    degrades per page with a warning.
    """
    if not STRUCTURE_ENABLED or not pages:
        return None
    out: list[str] = []
    changed = False
    for page in pages:
        regime = _page_regime(page)
        amended: list[str] | None = None
        if regime == "text":
            amended = _amend_page_text(page, warnings, source)
        elif regime == "image":
            amended = _amend_page_image(page, warnings, source)
        if amended is None:
            out.extend(page.md_lines)
        else:
            out.extend(amended)
            changed = True
        # Reproduce the inter-page blank line the deterministic writer inserts
        # in paper mode, so unchanged pages stay byte-identical.
        out.append("")
    if not changed:
        return None
    return out
