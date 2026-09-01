"""Cheap vision-classifier gate for the AI vision pass.

Uses a tiny vision-language model (Qwen2.5-VL-3B by default, or any mlx-vlm
OpenAI-compatible endpoint) to classify an image into one of three categories:

- ``text`` — mostly text (documents, slides, screenshots, tables) worth
  transcribing verbatim;
- ``diagram`` — a flowchart or conceptual figure, where a short high-level
  description is more useful than its labels;
- ``decorative`` — photographs, logos, icons, clip-art, backgrounds left as an
  image link.

Configuration (environment variables):

- ``VISION_CLASSIFY_ENABLED`` — master switch for the gate. Default off.
- ``VISION_CLASSIFY_BASE_URL`` — classifier server base URL, default ``http://127.0.0.1:8082/v1``.
- ``VISION_CLASSIFY_MODEL`` — classifier model id, default
  ``mlx-community/Qwen2.5-VL-3B-Instruct-4bit`` (switch to any mlx-vlm VLM via
  this var; note ``vikhyatk/moondream2`` does not load in mlx-vlm 0.6.15).
"""
from __future__ import annotations

import os
import time

from converter import config
from converter.base import image_digest
from converter.logstore import record
from converter.vision import (
    VISION_BASE_URL,
    VISION_MODEL,
    _chat_completion,
    _image_content,
    bullet_item_count,
    image_mime,
    image_readable,
    transcribe_image,
    transcribe_image_meta,
    transcribe_page,
    transcription_quality,
)

VISION_MIN_CONTENT_AREA = int(os.environ.get("VISION_MIN_CONTENT_AREA", "150000"))
VISION_MIN_CONTENT_ASPECT = float(os.environ.get("VISION_MIN_CONTENT_ASPECT", "2.0"))
VISION_CLASSIFY_BASE_URL = os.environ.get("VISION_CLASSIFY_BASE_URL", "http://127.0.0.1:8082/v1")
VISION_CLASSIFY_MODEL = os.environ.get("VISION_CLASSIFY_MODEL", "mlx-community/Qwen2.5-VL-3B-Instruct-4bit")

_PROMPT = (
    "Classify this image. Answer with exactly one word.\n\n"
    "Answer TEXT if the image is mostly text worth transcribing verbatim "
    "(a document, slide, screenshot, table, or matrix).\n"
    "Answer DIAGRAM if the image is a diagram, flowchart, or conceptual figure "
    "where a short high-level description is more useful than its labels.\n"
    "Answer DECORATIVE only if the image is a photograph, logo, icon, clip-art, "
    "or decorative background with nothing worth extracting.\n"
    "A wide table, matrix, or full-width figure is TEXT or DIAGRAM, never "
    "DECORATIVE.\n\n"
    "Answer:"
)

# Category keywords checked in priority order: decorative first (e.g. so
# "photograph" wins over "graph"), then diagram, then text.
_DECORATIVE_WORDS = (
    "decorative",
    "photograph",
    "photo",
    "logo",
    "icon",
    "clip",
    "background",
    "purely visual",
    "skip",
)
_DIAGRAM_WORDS = (
    "diagram",
    "flowchart",
    "figure",
    "chart",
    "graph",
    "conceptual",
)
_TEXT_WORDS = (
    "text",
    "table",
    "document",
    "slide",
    "screenshot",
    "transcribe",
)


def _parse_category(answer: str) -> str:
    """Map a loose classifier answer to ``text`` / ``diagram`` / ``decorative``."""
    a = answer.strip().lower()
    for word in _DECORATIVE_WORDS:
        if word in a:
            return "decorative"
    for word in _DIAGRAM_WORDS:
        if word in a:
            return "diagram"
    for word in _TEXT_WORDS:
        if word in a:
            return "text"
    # Unrecognized answer: default to "decorative" (the conservative choice for a
    # gate whose purpose is to save compute).
    return "decorative"


def _usage_counts(usage) -> tuple[int | None, int | None]:
    if not usage:
        return None, None
    prompt = usage.get("prompt_tokens")
    generated = usage.get("completion_tokens", usage.get("generated_tokens"))
    return prompt, generated


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# Content-hash caches so deduplicated/repeated images are classified/transcribed once.
_category_cache: dict[str, str] = {}
_transcribe_cache: dict[str, str] = {}
_meta_transcribe_cache: dict[str, str] = {}
_page_transcribe_cache: dict[str, str] = {}
_readability_cache: dict[str, str | None] = {}


def transcribe_image_cached(
    blob: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Transcribe an image, memoized by content digest.

    Returns the markdown, or ``(markdown, usage)`` when ``return_usage`` is True
    (``usage`` is ``None`` on a cache hit, since no model call is made).
    """
    digest = image_digest(blob)
    if digest in _transcribe_cache:
        if return_usage:
            return _transcribe_cache[digest], None
        return _transcribe_cache[digest]
    if return_usage:
        markdown, usage = transcribe_image(
            blob, mime, base_url=base_url, model=model, api_key=api_key, timeout=timeout, return_usage=True
        )
    else:
        markdown = transcribe_image(
            blob, mime, base_url=base_url, model=model, api_key=api_key, timeout=timeout
        )
        usage = None
    _transcribe_cache[digest] = markdown
    if return_usage:
        return markdown, usage
    return markdown


def transcribe_image_meta_cached(
    blob: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Transcribe an image as a high-level description, memoized by digest.

    Returns the markdown, or ``(markdown, usage)`` when ``return_usage`` is True.
    """
    digest = image_digest(blob)
    if digest in _meta_transcribe_cache:
        if return_usage:
            return _meta_transcribe_cache[digest], None
        return _meta_transcribe_cache[digest]
    if return_usage:
        markdown, usage = transcribe_image_meta(
            blob, mime, base_url=base_url, model=model, api_key=api_key, timeout=timeout, return_usage=True
        )
    else:
        markdown = transcribe_image_meta(
            blob, mime, base_url=base_url, model=model, api_key=api_key, timeout=timeout
        )
        usage = None
    _meta_transcribe_cache[digest] = markdown
    if return_usage:
        return markdown, usage
    return markdown


def transcribe_page_cached(
    png_bytes: bytes,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Transcribe a page render, memoized by content digest.

    Returns the markdown, or ``(markdown, usage)`` when ``return_usage`` is True.
    """
    digest = image_digest(png_bytes)
    if digest in _page_transcribe_cache:
        if return_usage:
            return _page_transcribe_cache[digest], None
        return _page_transcribe_cache[digest]
    if return_usage:
        markdown, usage = transcribe_page(
            png_bytes, base_url=base_url, model=model, api_key=api_key, timeout=timeout, return_usage=True
        )
    else:
        markdown = transcribe_page(
            png_bytes, base_url=base_url, model=model, api_key=api_key, timeout=timeout
        )
        usage = None
    _page_transcribe_cache[digest] = markdown
    if return_usage:
        return markdown, usage
    return markdown


def classify_category(
    image_bytes: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 120.0,
    return_meta: bool = False,
):
    """Classify an image as ``text`` / ``diagram`` / ``decorative``.

    When ``return_meta`` is True, returns ``(category, meta)`` where ``meta``
    holds the raw answer and token usage. Raises on network/HTTP errors so
    callers can fall back safely.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _PROMPT}, _image_content(image_bytes, mime)]}
    ]
    answer, usage = _chat_completion(
        messages,
        base_url=base_url or VISION_CLASSIFY_BASE_URL,
        model=model or VISION_CLASSIFY_MODEL,
        api_key=api_key,
        temperature=0.0,
        max_tokens=32,
        timeout=timeout,
        return_usage=True,
    )
    category = _parse_category(answer)
    if return_meta:
        return category, {"raw_answer": answer, "usage": usage}
    return category


def classify_category_cached(
    image_bytes: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 120.0,
    return_meta: bool = False,
):
    """Like :func:`classify_category` but memoized by content digest."""
    digest = image_digest(image_bytes)
    if digest in _category_cache:
        if return_meta:
            return _category_cache[digest], {"raw_answer": None, "usage": None}
        return _category_cache[digest]
    category, meta = classify_category(
        image_bytes, mime, base_url, model, api_key, timeout, return_meta=True
    )
    _category_cache[digest] = category
    if return_meta:
        return category, meta
    return category


def classify_image_with_log(
    image_bytes: bytes,
    mime: str = "image/png",
    warnings: list[str] | None = None,
    base_url: str | None = None,
    model: str | None = None,
    log_ctx: dict | None = None,
) -> str:
    """Classify an image into ``text`` / ``diagram`` / ``decorative``, logging it.

    When ``VISION_CLASSIFY_ENABLED`` is off this returns ``text`` (no gate, so the
    verbatim transcription runs as before). On classifier error it appends a
    warning and returns ``decorative``, so a failed gate degrades to "keep the
    image link" rather than spending the transcriber.
    """
    if not config.is_enabled("classify"):
        return "text"
    ctx = log_ctx or {}
    t0 = time.perf_counter()
    try:
        category, meta = classify_category_cached(
            image_bytes, mime, base_url, model, return_meta=True
        )
        prompt_tokens, generated_tokens = _usage_counts(meta["usage"])
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_ref=ctx.get("image_ref"),
            image_digest=ctx.get("image_digest"),
            stage="classify",
            model=model or VISION_CLASSIFY_MODEL,
            decision=category,
            raw_answer=meta["raw_answer"],
            latency_ms=_ms(t0),
            prompt_tokens=prompt_tokens,
            generated_tokens=generated_tokens,
            base_url=base_url or VISION_CLASSIFY_BASE_URL,
        )
        return category
    except Exception as exc:  # noqa: BLE001 - degrade gracefully on any failure
        if warnings is not None:
            warnings.append(f"Vision classifier failed: {exc}; skipping transcription")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_ref=ctx.get("image_ref"),
            image_digest=ctx.get("image_digest"),
            stage="classify",
            model=model or VISION_CLASSIFY_MODEL,
            latency_ms=_ms(t0),
            error=str(exc),
            base_url=base_url or VISION_CLASSIFY_BASE_URL,
        )
        return "decorative"


def should_transcribe(
    image_bytes: bytes,
    mime: str = "image/png",
    warnings: list[str] | None = None,
    base_url: str | None = None,
    model: str | None = None,
    log_ctx: dict | None = None,
) -> bool:
    """Run the classifier gate, returning True when transcription should proceed.

    Used by the chart path, which always transcribes verbatim (chart data is
    worth keeping) regardless of the text/diagram split.
    """
    return classify_image_with_log(image_bytes, mime, warnings, base_url, model, log_ctx) != "decorative"


# A high-level diagram gist is prose, not a list; more than a handful of bullet
# items means the model enumerated components (i.e. fabricated) instead.
_DIAGRAM_MAX_BULLETS = 6


def _looks_like_content(width: int | None, height: int | None) -> bool:
    """Return True if a large, wide image is almost certainly slide content.

    A false "decorative" classification drops real content (data loss), while a
    false "text"/"diagram" merely costs one transcription (caught by the quality
    gate). So the classifier's "decorative" verdict is overridden for images
    that are both large and wide — banner tables/matrices — which decorative
    images (logos, icons, photos) almost never are.
    """
    if not width or not height:
        return False
    long_side = max(width, height)
    short_side = min(width, height)
    return (
        width * height >= VISION_MIN_CONTENT_AREA
        and long_side / short_side >= VISION_MIN_CONTENT_ASPECT
    )


def _blockquote(md: str) -> str:
    """Wrap prose in a Markdown blockquote, one ``>`` per line."""
    return "\n".join("> " + line if line.strip() else ">" for line in md.splitlines())


def maybe_transcribe_image(
    blob: bytes,
    ext: str,
    warnings: list[str] | None = None,
    base_url: str | None = None,
    model: str | None = None,
    log_ctx: dict | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str | None:
    """Transcribe an embedded image based on its classifier category.

    Only runs when both ``VISION_ENABLED`` and ``VISION_CLASSIFY_ENABLED`` are on;
    otherwise returns ``None`` (images stay link-only). Images that are too
    low-resolution or blurry to read are skipped before any model call. Text
    images are transcribed verbatim; diagrams get a high-level description;
    decorative images are skipped. A transcription that fails the quality gate is
    discarded with a warning. Any error degrades to ``None``.
    """
    if not (config.is_enabled("vision") and config.is_enabled("classify")):
        return None
    mime = image_mime(ext)
    ctx = log_ctx or {}
    digest = image_digest(blob)
    if digest not in _readability_cache:
        _readability_cache[digest] = image_readable(blob, ext, width=width, height=height)
    reason = _readability_cache[digest]
    if reason is not None:
        if warnings is not None:
            warnings.append(f"Skipping unreadable image ({reason}); keeping image link")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_ref=ctx.get("image_ref"),
            image_digest=digest,
            stage="readability",
            model=model or VISION_MODEL,
            decision=reason,
            base_url=base_url or VISION_BASE_URL,
        )
        return None
    category = classify_image_with_log(blob, mime, warnings, base_url, model, ctx)
    if category == "decorative" and _looks_like_content(width, height):
        category = "diagram"
    if category == "decorative":
        return None
    t0 = time.perf_counter()
    try:
        if category == "diagram":
            markdown, usage = transcribe_image_meta_cached(
                blob, mime, base_url=base_url, model=model, return_usage=True
            )
        else:
            markdown, usage = transcribe_image_cached(
                blob, mime, base_url=base_url, model=model, return_usage=True
            )
    except Exception as exc:  # noqa: BLE001
        if warnings is not None:
            warnings.append(f"Vision transcription failed: {exc}")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_ref=ctx.get("image_ref"),
            image_digest=ctx.get("image_digest"),
            stage="transcribe",
            model=model or VISION_MODEL,
            latency_ms=_ms(t0),
            error=str(exc),
            base_url=base_url or VISION_BASE_URL,
        )
        return None
    prompt_tokens, generated_tokens = _usage_counts(usage)
    reason = transcription_quality(markdown)
    if category == "diagram" and reason is None:
        if bullet_item_count(markdown) > _DIAGRAM_MAX_BULLETS:
            reason = "enumerated"
    if reason is not None:
        if warnings is not None:
            warnings.append(f"Discarding low-value vision transcription ({reason})")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_ref=ctx.get("image_ref"),
            image_digest=ctx.get("image_digest"),
            stage="transcribe",
            model=model or VISION_MODEL,
            latency_ms=_ms(t0),
            prompt_tokens=prompt_tokens,
            generated_tokens=generated_tokens,
            markdown=markdown,
            error=f"quality gate: {reason}",
            base_url=base_url or VISION_BASE_URL,
        )
        return None
    record(
        source=ctx.get("source", ""),
        page=ctx.get("page"),
        image_ref=ctx.get("image_ref"),
        image_digest=ctx.get("image_digest"),
        stage="transcribe",
        model=model or VISION_MODEL,
        latency_ms=_ms(t0),
        prompt_tokens=prompt_tokens,
        generated_tokens=generated_tokens,
        markdown=markdown,
        base_url=base_url or VISION_BASE_URL,
    )
    return _blockquote(markdown) if category == "diagram" else markdown


def transcribe_complex_page(
    png_bytes: bytes,
    warnings: list[str] | None = None,
    base_url: str | None = None,
    model: str | None = None,
    log_ctx: dict | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str | None:
    """Transcribe a rendered page PNG for a page that failed linearization.

    Only runs when ``VISION_ENABLED`` is on. With the classifier on, routes by
    category — ``diagram`` gets a high-level gist (blockquoted), ``text`` a
    lossless page transcription, ``decorative`` is skipped — and without it,
    defaults to lossless transcription. Returns ``None`` when disabled, skipped,
    unreadable, or gated, so the caller keeps its deterministic fallback.
    """
    if not config.is_enabled("vision"):
        return None
    mime = image_mime("png")
    ctx = log_ctx or {}
    digest = image_digest(png_bytes)
    if digest not in _readability_cache:
        _readability_cache[digest] = image_readable(png_bytes, "png", width=width, height=height)
    reason = _readability_cache[digest]
    if reason is not None:
        if warnings is not None:
            warnings.append(f"Skipping unreadable page render ({reason}); keeping raw text")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_digest=digest,
            stage="readability",
            model=model or VISION_MODEL,
            decision=reason,
            base_url=base_url or VISION_BASE_URL,
        )
        return None
    if config.is_enabled("classify"):
        category = classify_image_with_log(png_bytes, mime, warnings, base_url, model, ctx)
        if category == "decorative" and _looks_like_content(width, height):
            category = "diagram"
        if category == "decorative":
            return None
    else:
        category = "text"
    t0 = time.perf_counter()
    try:
        if category == "diagram":
            markdown, usage = transcribe_image_meta_cached(
                png_bytes, mime, base_url=base_url, model=model, return_usage=True
            )
        else:
            markdown, usage = transcribe_page_cached(
                png_bytes, base_url=base_url, model=model, return_usage=True
            )
    except Exception as exc:  # noqa: BLE001
        if warnings is not None:
            warnings.append(f"Vision page transcription failed: {exc}")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_digest=digest,
            stage="transcribe",
            model=model or VISION_MODEL,
            latency_ms=_ms(t0),
            error=str(exc),
            base_url=base_url or VISION_BASE_URL,
        )
        return None
    prompt_tokens, generated_tokens = _usage_counts(usage)
    reason = transcription_quality(markdown)
    if category == "diagram" and reason is None:
        if bullet_item_count(markdown) > _DIAGRAM_MAX_BULLETS:
            reason = "enumerated"
    if reason is not None:
        if warnings is not None:
            warnings.append(f"Discarding low-value page transcription ({reason})")
        record(
            source=ctx.get("source", ""),
            page=ctx.get("page"),
            image_digest=digest,
            stage="transcribe",
            model=model or VISION_MODEL,
            latency_ms=_ms(t0),
            prompt_tokens=prompt_tokens,
            generated_tokens=generated_tokens,
            markdown=markdown,
            error=f"quality gate: {reason}",
            base_url=base_url or VISION_BASE_URL,
        )
        return None
    record(
        source=ctx.get("source", ""),
        page=ctx.get("page"),
        image_digest=digest,
        stage="transcribe",
        model=model or VISION_MODEL,
        latency_ms=_ms(t0),
        prompt_tokens=prompt_tokens,
        generated_tokens=generated_tokens,
        markdown=markdown,
        base_url=base_url or VISION_BASE_URL,
    )
    return _blockquote(markdown) if category == "diagram" else markdown
