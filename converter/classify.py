"""Cheap vision-classifier gate for the AI vision pass.

Uses a tiny vision-language model (Moondream2 by default, or any mlx-vlm
OpenAI-compatible endpoint) to decide whether an image or page contains
*educational* content (tables, charts, graphs, diagrams, flowcharts, equations)
worth an expensive transcription, versus *decorative* content (photographs,
logos, icons, clip-art, backgrounds) that should be left as an image link.

Configuration (environment variables):

- ``VISION_CLASSIFY_ENABLED`` — master switch for the gate. Default off.
- ``VISION_CLASSIFY_BASE_URL`` — classifier server base URL, default ``http://127.0.0.1:8082/v1``.
- ``VISION_CLASSIFY_MODEL`` — classifier model id, default ``vikhyatk/moondream2``
  (switch to e.g. ``mlx-community/Qwen2.5-VL-3B-Instruct-4bit`` via this var).
"""
from __future__ import annotations

import os

from converter.base import image_digest
from converter.vision import (
    VISION_ENABLED,
    _chat_completion,
    _image_content,
    image_mime,
    transcribe_image,
)

VISION_CLASSIFY_ENABLED = os.environ.get("VISION_CLASSIFY_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VISION_CLASSIFY_BASE_URL = os.environ.get("VISION_CLASSIFY_BASE_URL", "http://127.0.0.1:8082/v1")
VISION_CLASSIFY_MODEL = os.environ.get("VISION_CLASSIFY_MODEL", "vikhyatk/moondream2")

_PROMPT = (
    "Decide whether this image contains educational content worth transcribing "
    "into Markdown. Answer with exactly one word.\n\n"
    "Answer TRANSCRIBE if the image is a table, chart, graph, diagram, "
    "flowchart, equation, or any figure whose text and structure is worth "
    "extracting.\n"
    "Answer SKIP if the image is a photograph, logo, icon, clip-art, decorative "
    "background, or any purely visual image with nothing worth extracting.\n\n"
    "Answer:"
)

# Signals checked in order: a false signal (e.g. "photograph" contains "graph")
# must win over a true signal, so false words are tested first.
_FALSE_WORDS = (
    "skip",
    "photograph",
    "photo",
    "logo",
    "icon",
    "clip",
    "decorat",
    "background",
    "purely visual",
)
_TRUE_WORDS = (
    "transcribe",
    "educational",
    "chart",
    "diagram",
    "table",
    "equation",
    "flowchart",
    "graph",
    "figure",
    "worth extract",
)


def _parse_answer(answer: str) -> bool:
    a = answer.strip().lower()
    for word in _FALSE_WORDS:
        if word in a:
            return False
    for word in _TRUE_WORDS:
        if word in a:
            return True
    # Unrecognized answer: treat as "skip" (the conservative default for a gate
    # whose purpose is to save compute).
    return False

# Content-hash cache so deduplicated/repeated images are classified once.
_cache: dict[str, bool] = {}


def classify_image(
    image_bytes: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 120.0,
) -> bool:
    """Return True if the image looks worth transcribing, False otherwise.

    Raises on network/HTTP errors so callers can fall back safely.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _PROMPT}, _image_content(image_bytes, mime)]}
    ]
    answer = _chat_completion(
        messages,
        base_url=base_url or VISION_CLASSIFY_BASE_URL,
        model=model or VISION_CLASSIFY_MODEL,
        api_key=api_key,
        temperature=0.0,
        max_tokens=32,
        timeout=timeout,
    )
    return _parse_answer(answer)


def classify_image_cached(
    image_bytes: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 120.0,
) -> bool:
    """Like :func:`classify_image` but memoized by content digest."""
    digest = image_digest(image_bytes)
    if digest in _cache:
        return _cache[digest]
    decision = classify_image(image_bytes, mime, base_url, model, api_key, timeout)
    _cache[digest] = decision
    return decision


def should_transcribe(
    image_bytes: bytes,
    mime: str = "image/png",
    warnings: list[str] | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> bool:
    """Run the classifier gate, returning True when transcription should proceed.

    When ``VISION_CLASSIFY_ENABLED`` is off this always returns True (no gate).
    On classifier error it appends a warning and returns False, so a failed
    gate degrades to "keep the image link" rather than spending the transcriber.
    """
    if not VISION_CLASSIFY_ENABLED:
        return True
    try:
        return classify_image_cached(image_bytes, mime, base_url, model)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully on any failure
        if warnings is not None:
            warnings.append(f"Vision classifier failed: {exc}; skipping transcription")
        return False


def maybe_transcribe_image(
    blob: bytes,
    ext: str,
    warnings: list[str] | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> str | None:
    """Transcribe an embedded image if the classifier deems it educational.

    Only runs when both ``VISION_ENABLED`` and ``VISION_CLASSIFY_ENABLED`` are on;
    otherwise returns ``None`` (images stay link-only). Any error degrades to
    ``None`` with a warning rather than failing the conversion.
    """
    if not (VISION_ENABLED and VISION_CLASSIFY_ENABLED):
        return None
    mime = image_mime(ext)
    if not should_transcribe(blob, mime, warnings, base_url, model):
        return None
    try:
        return transcribe_image(blob, mime, base_url=base_url, model=model)
    except Exception as exc:  # noqa: BLE001
        if warnings is not None:
            warnings.append(f"Vision transcription failed: {exc}")
        return None
