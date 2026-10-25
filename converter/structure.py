"""PDF document-structure LLM pass (paper and slide modes, opt-in).

The deterministic PDF converter (``converter/pdf.py``) reconstructs
titles/authors/headings/footnotes/references (paper mode) or per-slide titles,
paragraphs and subheadings (slide mode). This optional post-pass improves that
structure with a local OpenAI-compatible chat model, per page, gated by a
deterministic confidence signal:

- **Text regime** — pages with a usable text layer are *check-and-amended*:
  paper mode may fix the page-1 title/authors block, blockquote the abstract,
  add ``##`` headings, wrap footnotes, insert a ``## References`` heading, and
  reorder/rejoin lines to fix multi-column linearization; slide mode may fix the
  slide title's Markdown, add ``##``/``###`` subheadings and reorder diagram
  labels. Every content word must stay verbatim (a word cross-check rejects any
  page that omits or invents prose).
- **Image regime** — pages whose text layer is unusable (scans, garbage OCR,
  the ``<details>`` raw-text fallback) are *reworded from the rendered page
  image* by the same pass, gated by image readability + transcription quality
  instead of the verbatim word gate. Slide pages get their `` — Page N`` suffix
  appended deterministically.
- **Skip** — pages already handled by the interpret/vision passes are left
  alone, and so are dense OCR-garbage text layers (typo-shaped tokens /
  vowel-less junk): a page whose text layer is not really prose can only fail
  the verbatim word-gate, so the model call is predicted and skipped pre-call
  (:func:`converter.base.text_layer_is_garbage`, ADR-0023).

Every model call and gate failure is local to its page: the page keeps its
deterministic Markdown and a warning is appended. The pass never blocks or
fails a conversion, and is disabled by default.

Configuration (environment variables):

- ``STRUCTURE_ENABLED`` — master switch. Default off.
- ``STRUCTURE_BASE_URL`` — defaults to ``FORMAT_BASE_URL`` then ``WRITE_BASE_URL``.
- ``STRUCTURE_MODEL`` — defaults to ``FORMAT_MODEL`` then ``WRITE_MODEL`` (a VLM,
  so the image regime needs no extra server).
- ``STRUCTURE_API_KEY`` — defaults to ``FORMAT_API_KEY`` then ``WRITE_API_KEY``.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from converter import config
from converter.base import text_layer_quality
from converter.format import (
    FORMAT_API_KEY,
    FORMAT_BASE_URL,
    FORMAT_MODEL,
    _PAGEBREAK,
    _anchors_intact,
    _deterministic_pass,
    _is_structural,
)
from converter.logstore import record
from converter.need import NEED_BASE_URL, NEED_MODEL, need_gate, needs_restructure
from converter.router import Route, structure_regime, structure_text_downgrade
from converter.vision import (
    _chat_completion,
    _image_content,
    image_readable,
    transcription_quality,
    verify_no_omissions,
)

STRUCTURE_BASE_URL = os.environ.get("STRUCTURE_BASE_URL", FORMAT_BASE_URL)
STRUCTURE_MODEL = os.environ.get("STRUCTURE_MODEL", FORMAT_MODEL)
STRUCTURE_API_KEY = os.environ.get("STRUCTURE_API_KEY", FORMAT_API_KEY)

# The text regime is a text-to-text check-and-amend (no image is sent), so it can
# run on a dedicated small text model rather than the writer VLM (ADR-0024). The
# image regime still sends the rendered page PNG and stays on ``STRUCTURE_*``.
STRUCTURE_TEXT_BASE_URL = os.environ.get(
    "STRUCTURE_TEXT_BASE_URL", config.SERVERS.get("structure-text", config.SERVERS["summary"]).base_url
)
STRUCTURE_TEXT_MODEL = os.environ.get(
    "STRUCTURE_TEXT_MODEL", config.SERVERS.get("structure-text", config.SERVERS["summary"]).model
)
STRUCTURE_TEXT_API_KEY = os.environ.get("STRUCTURE_TEXT_API_KEY", STRUCTURE_API_KEY)

STRUCTURE_MAX_TOKENS = 6000
_TIMEOUT = 600.0

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

# Slide subheadings are structural without being paper sections. These words
# may be added as heading-only text (the word gate still requires every body
# word to be verbatim); paper mode keeps the narrower paper set above.
_SLIDE_ALLOWED_HEADING_WORDS = _ALLOWED_HEADING_WORDS | frozenset(
    {
        "agenda",
        "overview",
        "summary",
        "takeaways",
        "recap",
        "outline",
        "questions",
        "exercise",
        "demo",
        "example",
        "examples",
        "goals",
        "objectives",
        "learning",
        "outcomes",
        "next",
        "steps",
        "reading",
        "further",
        "background",
        "context",
        "details",
        "section",
        "topic",
        "topics",
        "key",
        "points",
    }
)


@dataclass
class PageData:
    """One PDF page: the deterministic Markdown lines plus raw metadata.

    ``md_lines`` are exactly the lines ``PDFConverter.convert`` would write for
    the page. Paper mode: page 1 may carry the ``# Title`` heading and later
    pages carry no top-level heading (ADR-0052). Slide mode: every page carries
    its ``# {title} — Page N`` heading (ADR-0070). They include any legacy
    ``[Page N](...)`` link and any trailing blank line. ``line_meta`` carries
    the raw per-line text + coordinates/size/bold in reading order (text regime
    input and coverage). ``png`` is the rendered page image (image regime),
    ``pno`` the 1-based page number.
    """

    md_lines: list[str]
    line_meta: list[dict]
    png: bytes | None = None
    pno: int = 1


# Structural lines that are exempt from the anchor check per regime.
_RAW_DETAILS_MARKERS = ("<details", "</details>", "<summary")


def _text_coverage(line_meta: list[dict]) -> str:
    """Classify a page's raw text layer as ``usable`` / ``sparse`` / ``empty``.

    The confidence signal: pages with too few words, a low unique-word ratio
    (repeated OCR garbage) or fragmentary line lengths cannot be given semantic
    structure from the text layer and must go to the image regime instead.
    Delegates to the shared :func:`converter.base.text_layer_quality` so the
    routing signal in the PDF converter (ADR-0020) stays in sync.
    """
    return text_layer_quality([m.get("text", "") for m in line_meta])


def _page_regime(page: PageData) -> str:
    """Route a page to the ``text`` / ``image`` / ``skip`` regime.

    Delegates to :func:`converter.router.structure_regime` — the single source of
    truth for the cheap-before-expensive routing signals (ADR-0024) — so the
    structure pass's dynamics are legible from one place.
    """
    return structure_regime(page.line_meta, page.md_lines)


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

    The ``# `` page heading and any legacy ``[Page N](...)`` link are excluded:
    they are anchors the model must reproduce byte-exact, not annotate.
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
    "their words verbatim. Keep the level of every heading that is already "
    "present (`## `/`### `); never demote or remove an existing heading.\n"
    "- Wrap footnote lines as a blockquote block.\n"
    "- Insert `## References` (and, if present, `## Abstract`, `## Notes`, ...) "
    "headings where the corresponding block starts. New heading text may only be "
    "a structural marker (References, Abstract, Bibliography, Acknowledgements, "
    "Notes, Appendix, ...) — never invent prose headings.\n"
    "- Rejoin wrapped lines into paragraphs and, where multi-column linearization "
    "interleaved two columns' lines, reorder lines into the correct reading "
    "order.\n"
    "- Never add a top-level `# ` heading: only page 1 carries the paper title "
    "(`# ...`); later pages begin directly with body text or a section heading "
    "(`## `/`### `). "
    "Keep the page-1 title, any `[Page N](...)` image link, all pipe tables, "
    "blockquotes, `<details>` blocks and fenced code exactly as they are.\n\n"
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
    "- Keep any `[Page N](...)` image link line, if present, exactly as it is.\n"
    "- Use at most one `# ` top-level heading, and only on the paper's first "
    "page (the title read from the image). A later page must contain no `# ` "
    "line at all — a section heading it starts with is a `## ` heading.\n"
    "- Render tables as pipe tables and preserve bold/italic where present.\n\n"
    "Output only the Markdown, with no commentary or code fences."
)


_SLIDE_AMEND_PROMPT = (
    "You are restructuring the Markdown of one slide of a converted "
    "presentation. Below are the slide's body lines with line numbers and "
    "layout metadata ([font-size bold x0]), followed by the slide's current "
    "Markdown.\n\n"
    "Reproduce the Markdown with ONLY these structural amendments:\n"
    "- Keep every word of the body text verbatim. Never reword, add, translate "
    "or \"fix\" OCR-garbled words.\n"
    "- The slide has exactly one top-level `# ` heading: its title followed by "
    "the exact ` — Page N` suffix. You may fix the title's Markdown (for "
    "example remove a stray bullet glyph or move a subtitle into the body), "
    "but keep every title word and keep the exact ` — Page N` suffix. Never "
    "add or remove a top-level `# ` heading.\n"
    "- Promote lines that are real subheadings to `## ` or `### ` headings, "
    "keeping their words verbatim. Keep the level of every heading that is "
    "already present (`## `/`### `); never demote or remove an existing "
    "heading. New heading text may only be a structural marker (Agenda, "
    "Overview, Summary, Takeaways, Questions, References, ...) or words "
    "already present in the slide.\n"
    "- Rejoin wrapped lines into paragraphs and fix list nesting and blank-line "
    "spacing.\n"
    "- Where diagram or text-box linearization interleaved labels, reorder the "
    "lines into reading order.\n"
    "- Keep bullets, any `[Page N](...)` image link, all pipe tables, "
    "blockquotes, `<details>` blocks, fenced code, and horizontal rules exactly "
    "as they are.\n\n"
    "Numbered body lines with metadata:\n{numbered}\n\n"
    "Current Markdown:\n{md}\n\n"
    "Output only the amended Markdown, with no commentary or code fences."
)

_SLIDE_IMAGE_PROMPT = (
    "This slide has no usable extracted text layer. Read the rendered slide "
    "and produce its Markdown: one `# ` slide title first (do not add a page "
    "number; the writer appends ` — Page N`), then any `## `/`### ` "
    "subheadings, bullets, body paragraphs and tables.\n\n"
    "Rules:\n"
    "- Transcribe text verbatim from the image; do not invent text you cannot "
    "read; do not add commentary.\n"
    "- Use exactly one top-level `# ` heading, the slide title.\n"
    "- Keep any `[Page N](...)` image link line, if present, exactly as it is.\n"
    "- Render tables as pipe tables and preserve bold/italic where present.\n"
    "- Keep code on its own lines; a fenced code block is allowed.\n\n"
    "Output only the Markdown, with no commentary and no wrapper code fence."
)


def _heading_lines(text: str) -> list[tuple[int, str]]:
    """Top-level ``# `` lines outside fenced code blocks, with their indices."""
    headings: list[tuple[int, str]] = []
    fenced = False
    for index, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced and line.startswith("# "):
            headings.append((index, line))
    return headings


def _heading_count(text: str) -> int:
    return len(_heading_lines(text))


def _top_level_heading_ok(reply: str, pno: int) -> bool:
    """The paper-mode per-page top-level ``#`` heading contract (ADR-0052).

    Page 1 may carry at most one ``# `` line — the paper title, the amendment
    target — and may gain one when the deterministic pass found none. A later
    page must carry none: its content belongs under the paper's last real
    ``##`` section, and a reply that adds a page/top-level heading is rejected.
    Fenced code is ignored.
    """
    count = _heading_count(reply)
    return count <= 1 if pno == 1 else count == 0


def _slide_heading_ok(reply: str, pno: int) -> bool:
    """The slide-mode title contract: exactly one ``# `` heading with page N.

    The heading may be amended, but it must remain the single top-level heading
    and keep the `` — Page N`` provenance suffix (or the ``# Page N`` fallback).
    Fenced code is ignored (ADR-0071).
    """
    headings = [line.strip() for _, line in _heading_lines(reply)]
    if len(headings) != 1:
        return False
    heading = headings[0].rstrip()
    if heading == f"# Page {pno}":
        return True
    suffix = f" — Page {pno}"
    if not heading.endswith(suffix):
        return False
    return bool(heading[2 : -len(suffix)].strip())


def _canonical_slide_title(reply: str, pno: int) -> str:
    """Give a model-written slide Markdown the canonical `` — Page N`` title.

    Image-regime replies do not know the page number. A missing title falls back
    to ``# Page N``; an existing one is normalized. Multiple top-level headings
    are left for :func:`_slide_heading_ok` to reject.
    """
    lines = reply.splitlines()
    headings = _heading_lines(reply)
    if len(headings) > 1:
        return reply
    if not headings:
        return "\n".join([f"# Page {pno}", ""] + lines)
    index, line = headings[0]
    title = line[2:].strip()
    title = re.sub(
        r"\s*[-–—]\s*Page\s+\d+\s*$", "", title, flags=re.IGNORECASE
    ).strip()
    if not title or re.fullmatch(r"Page\s+\d+", title, flags=re.IGNORECASE):
        lines[index] = f"# Page {pno}"
    else:
        lines[index] = f"# {title} — Page {pno}"
    return "\n".join(lines)


def _link_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip().startswith("[Page "):
            return line.strip()
    return None


def _added_words_allowed(
    word: str,
    original: str,
    reply: str,
    allowed: frozenset[str] = _ALLOWED_HEADING_WORDS,
) -> bool:
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
    if word in allowed:
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


def _verify_preserved(
    original: str,
    reply: str,
    allowed: frozenset[str] = _ALLOWED_HEADING_WORDS,
) -> list[str]:
    """Return problems if the amendment dropped or invented content words."""
    problems: list[str] = []
    missing = verify_no_omissions(original, reply)
    if missing:
        problems.append("omitted: " + ", ".join(missing[:5]))
    added = {w for w in re.findall(r"[A-Za-z0-9]{4,}", reply.lower()) if len(w) >= 4} - {
        w for w in re.findall(r"[A-Za-z0-9]{4,}", original.lower()) if len(w) >= 4
    }
    illegal = sorted(
        w for w in added if not _added_words_allowed(w, original, reply, allowed)
    )
    if illegal:
        problems.append("added: " + ", ".join(illegal[:5]))
    return problems


def _anchors_for(page: PageData, regime: str, mode: str = "paper") -> list[str]:
    """Structural lines the amended page must reproduce byte-exact.

    The top-level ``# `` title is the amendment target: page 1's paper title in
    paper mode, and every slide's title in slide mode.
    """
    anchors: list[str] = []
    for line in page.md_lines:
        s = line.strip()
        if not s:
            continue
        if not _is_structural(s):
            continue
        if regime == "text" and s.startswith("# "):
            if mode == "slides" or page.pno == 1:
                continue
        if regime == "image" and s.startswith(_RAW_DETAILS_MARKERS):
            continue  # raw-text block is replaced by the image reword
        anchors.append(s)
    return anchors


def _text_model() -> tuple[str, str, str]:
    """Resolve the text regime's ``(base_url, model, api_key)``.

    The text regime is a text-to-text check-and-amend, so it downgrades off the
    writer VLM onto a dedicated small text model when one is configured
    (ADR-0024). The image regime still needs a VLM and stays on ``STRUCTURE_*``.
    """
    if structure_text_downgrade() == Route.DOWNGRADE:
        return STRUCTURE_TEXT_BASE_URL, STRUCTURE_TEXT_MODEL, STRUCTURE_TEXT_API_KEY
    return STRUCTURE_BASE_URL, STRUCTURE_MODEL, STRUCTURE_API_KEY


def _amend_page_text(
    page: PageData, warnings: list[str], source: str, mode: str = "paper"
) -> list[str] | None:
    """Text regime: check-and-amend the page, verbatim-gated."""
    anchors = _anchors_for(page, "text", mode)
    original = "\n".join(page.md_lines)
    numbered = _numbered_lines(page)
    if not numbered.strip():
        return None
    prompt = _SLIDE_AMEND_PROMPT if mode == "slides" else _AMEND_PROMPT
    allowed = (
        _SLIDE_ALLOWED_HEADING_WORDS
        if mode == "slides"
        else _ALLOWED_HEADING_WORDS
    )
    user = prompt.format(numbered=numbered, md=original)
    base_url, model, api_key = _text_model()
    t0 = time.perf_counter()
    try:
        reply = _chat_completion(
            [{"role": "user", "content": user}],
            base_url=base_url,
            model=model,
            api_key=api_key,
            max_tokens=STRUCTURE_MAX_TOKENS,
            timeout=_TIMEOUT,
        ).strip()
    except Exception as exc:  # noqa: BLE001 - degrade to deterministic output
        if warnings is not None:
            warnings.append(f"Structure pass failed on page {page.pno}: {exc}")
        _log(source, page, model=model, base_url=base_url, decision="error", error=str(exc), latency_ms=_ms(t0))
        return None
    if not reply:
        _reject(page, warnings, "empty reply")
        _log(source, page, model=model, base_url=base_url, decision="rejected", error="empty reply", latency_ms=_ms(t0))
        return None
    if not _anchors_intact(anchors, reply):
        _reject(page, warnings, "dropped structural lines")
        _log(source, page, model=model, base_url=base_url, decision="rejected", error="anchors dropped", latency_ms=_ms(t0))
        return None
    heading_ok = (
        _slide_heading_ok(reply, page.pno)
        if mode == "slides"
        else _top_level_heading_ok(reply, page.pno)
    )
    if not heading_ok:
        _reject(page, warnings, "heading invariant")
        _log(source, page, model=model, base_url=base_url, decision="rejected", error="heading invariant", latency_ms=_ms(t0))
        return None
    problems = _verify_preserved(original, reply, allowed)
    if problems:
        _reject(page, warnings, "; ".join(problems))
        _log(source, page, model=model, base_url=base_url, decision="rejected", error="; ".join(problems), latency_ms=_ms(t0))
        return None
    amended = reply.split("\n") + [""]
    unchanged = _deterministic_pass("\n".join(amended)) == _deterministic_pass(original)
    _log(source, page, model=model, base_url=base_url, decision="unchanged" if unchanged else "amended", latency_ms=_ms(t0))
    if unchanged and mode == "slides":
        # A model echo is not model-produced content (ADR-0050/0071).
        return None
    return amended


def _amend_page_image(
    page: PageData, warnings: list[str], source: str, mode: str = "paper"
) -> list[str] | None:
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
    prompt = _SLIDE_IMAGE_PROMPT if mode == "slides" else _IMAGE_PROMPT
    t0 = time.perf_counter()
    try:
        reply = _chat_completion(
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}, _image_content(page.png)],
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
    if mode == "slides":
        reply = _canonical_slide_title(reply, page.pno)
    heading_ok = (
        _slide_heading_ok(reply, page.pno)
        if mode == "slides"
        else _top_level_heading_ok(reply, page.pno)
    )
    if not heading_ok:
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
        model=kw.pop("model", STRUCTURE_MODEL),
        base_url=kw.pop("base_url", STRUCTURE_BASE_URL),
        **kw,
    )


def _structure_pages(
    pages: list[PageData],
    mode: str,
    warnings: list[str] | None = None,
    source: str = "",
) -> list[str] | None:
    """Run the structure pass over PDF pages, returning amended Markdown lines.

    Returns ``None`` (the caller keeps the deterministic output) when disabled,
    given no pages, or when nothing was amended. Never raises: any failure
    degrades per page with a warning.
    """
    if not config.is_enabled("structure") or not pages:
        return None
    regimes = {i: _page_regime(p) for i, p in enumerate(pages)}
    gate = need_gate()
    run_text: set[int] | None = None
    if gate in ("on", "shadow"):
        text_idx = [i for i in range(len(pages)) if regimes[i] == "text"]
        if text_idx:
            need = needs_restructure(
                ["\n".join(pages[i].md_lines) for i in text_idx],
                source=source,
                page_nos=[pages[i].pno for i in text_idx],
                mode=mode,
            )
            if need is None:
                need = set(range(len(text_idx)))
            run_text = {text_idx[i] for i in need}
        else:
            run_text = set()
        if gate == "shadow":
            run_text = None
    out: list[str] = []
    changed = False
    for i, page in enumerate(pages):
        regime = regimes[i]
        amended: list[str] | None = None
        if regime == "text":
            if run_text is None or i in run_text:
                amended = _amend_page_text(page, warnings, source, mode)
        elif regime == "image":
            amended = _amend_page_image(page, warnings, source, mode)
        if amended is None:
            out.extend(page.md_lines)
        else:
            out.extend(amended)
            changed = True
        if mode == "slides":
            # Reproduce the page-break scaffold the deterministic slide writer
            # inserts after every page (including the last).
            out.extend(["", _PAGEBREAK, "", "---", ""])
        else:
            # Reproduce the inter-page blank line the paper writer inserts, so
            # unchanged pages stay byte-identical.
            out.append("")
    if not changed:
        return None
    return out


def structure_paper(
    pages: list[PageData],
    warnings: list[str] | None = None,
    source: str = "",
) -> list[str] | None:
    """Run the structure pass over paper-mode pages (ADR-0011/0052)."""
    return _structure_pages(pages, "paper", warnings, source)


def structure_slides(
    pages: list[PageData],
    warnings: list[str] | None = None,
    source: str = "",
) -> list[str] | None:
    """Run the structure pass over slide-mode pages (ADR-0071).

    Every page carries its deterministic ``# {title} — Page N`` heading; the
    model may amend the title text but the page suffix is enforced. The
    page-break scaffold is reinserted here, so ``PageData`` stays page-local.
    """
    return _structure_pages(pages, "slides", warnings, source)
