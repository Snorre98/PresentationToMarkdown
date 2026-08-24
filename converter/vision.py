"""Optional vision-language post-pass for hard PDF pages and images.

Talks to any OpenAI-compatible ``/v1/chat/completions`` endpoint — on this
machine that is the local **mlx-vlm** server (MLX, Apple-native). See
``docs/ai-vision.md`` and the canonical ``macos-dev-config/inference-readme.md``
for how the model is downloaded, served, and stored.

Configuration (environment variables):

- ``VISION_ENABLED`` — master switch (``1``/``true``/``yes``/``on``). Default off.
- ``VISION_BASE_URL`` — server base URL, default ``http://127.0.0.1:8081/v1``.
- ``VISION_MODEL`` — model id, default ``mlx-community/Ornith-1.0-9B-8bit``.
- ``VISION_API_KEY`` — optional bearer token (not needed for local servers).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from collections import Counter

VISION_ENABLED = os.environ.get("VISION_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "http://127.0.0.1:8081/v1")
VISION_MODEL = os.environ.get("VISION_MODEL", "mlx-community/Qwen2.5-VL-7B-Instruct-4bit")
VISION_API_KEY = os.environ.get("VISION_API_KEY") or None

TRANSCRIBE_MAX_TOKENS = 1024

_PROMPT = (
    "Transcribe this presentation slide to Markdown. Be lossless: include every "
    "piece of text on the slide, verbatim, and keep its structure.\n\n"
    "Rules:\n"
    "- Use a top-level heading for the slide title.\n"
    "- Use `- ` bullets (nested `  - ` for sub-bullets).\n"
    "- Render tables as pipe tables.\n"
    "- Preserve bold/italic where present.\n"
    "- Do not add commentary, summaries, or your own explanations.\n"
    "- Do not invent text. If text is illegible, leave it out rather than guessing.\n\n"
    "Output only the Markdown."
)

_IMAGE_PROMPT = (
    "Transcribe this image to Markdown. Be lossless: include every piece of "
    "text in the image, verbatim, and keep its structure.\n\n"
    "Rules:\n"
    "- Use a top-level heading for a title if one is present.\n"
    "- Use `- ` bullets (nested `  - ` for sub-bullets).\n"
    "- Render tables as pipe tables.\n"
    "- Preserve bold/italic where present.\n"
    "- Do not add commentary, summaries, or your own explanations.\n"
    "- Do not invent text. If text is illegible, leave it out rather than guessing.\n\n"
    "Output only the Markdown."
)

_DIAGRAM_PROMPT = (
    "Describe this diagram at a high level. Do NOT transcribe every label "
    "verbatim.\n\n"
    "In Markdown, briefly state:\n"
    "- What the diagram represents and its purpose.\n"
    "- Its main stages, components, or actors (a few bullets).\n"
    "- The overall flow or relationships between them.\n\n"
    "Rules:\n"
    "- Keep it short: one heading plus 3-6 bullets.\n"
    "- Do not repeat the same label or text.\n"
    "- Do not add commentary or explanations beyond the diagram.\n\n"
    "Output only the Markdown."
)

# Words shorter than this, or in this set, are ignored by the omission check.
_STOPWORDS = {
    "this", "that", "these", "those", "with", "from", "have", "will", "would",
    "which", "there", "their", "about", "other", "your", "into", "been", "they",
    "were", "when", "what", "then", "than", "also", "such", "over", "only",
    "very", "each", "some", "most", "more", "used", "shall", "where", "after",
    "before", "between", "through", "during", "because", "course",
}

_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "tiff": "image/tiff",
}


def image_mime(ext: str) -> str:
    """Map an image file extension to a MIME type, defaulting to PNG."""
    return _MIME.get((ext or "").lstrip(".").lower(), "image/png")


def _image_content(image_bytes: bytes, mime: str = "image/png") -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _chat_completion(
    messages: list[dict],
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Send a chat request to an OpenAI-compatible endpoint.

    Returns the assistant text, or ``(text, usage)`` when ``return_usage`` is
    True (``usage`` is the response's token-count dict, or ``None`` if absent).
    """
    payload = {
        "model": model or VISION_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    url = (base_url or VISION_BASE_URL).rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    key = api_key or VISION_API_KEY
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"].strip()
    if return_usage:
        return content, body.get("usage")
    return content


def _words(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[A-Za-z0-9]{4,}", text.lower())
        if w not in _STOPWORDS
    }


def verify_no_omissions(raw_text: str, markdown: str) -> list[str]:
    """Return content words present in ``raw_text`` but missing from ``markdown``."""
    raw = _words(raw_text)
    if not raw:
        return []
    missing = sorted(raw - _words(markdown))
    return missing


# Quality-gate thresholds for flagging degenerate transcriptions (the model
# stuck in a repetition loop, runaway nesting, or near-empty content).
_QUALITY_MAX_NESTING = 12
_QUALITY_MAX_REPEATS = 8
_QUALITY_MIN_UNIQUE_RATIO = 0.5
_QUALITY_MIN_LINES_FOR_RATIO = 20
_QUALITY_MAX_LINES = 300
_QUALITY_MIN_TTR = 0.05
_QUALITY_MIN_WORDS_FOR_TTR = 50


def _normalize_line(line: str) -> str:
    """Strip emphasis and list markers so repeated lines compare equal."""
    line = line.strip()
    line = re.sub(r"\*\*+|\*", "", line)
    line = re.sub(r"^[-+]\s+", "", line)
    return re.sub(r"\s+", " ", line).strip().lower()


def transcription_quality(markdown: str) -> str | None:
    """Return a rejection reason if a transcription looks worthless, else None.

    Detects the classic vision-model failure modes: a repetition loop where the
    same label is emitted over and over, pathological bullet nesting, runaway
    length, and near-zero information density.
    """
    if not markdown or not markdown.strip():
        return "empty"
    lines = [ln for ln in markdown.splitlines() if ln.strip()]
    if len(lines) > _QUALITY_MAX_LINES:
        return "runaway length"

    max_depth = 0
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith(("- ", "* ", "+ ")):
            depth = (len(ln) - len(stripped)) // 2
            max_depth = max(max_depth, depth)
    if max_depth > _QUALITY_MAX_NESTING:
        return "excessive nesting"

    counts = Counter(_normalize_line(ln) for ln in lines)
    if max(counts.values()) > _QUALITY_MAX_REPEATS:
        return "repetitive"
    if len(lines) >= _QUALITY_MIN_LINES_FOR_RATIO:
        if len(counts) / len(lines) < _QUALITY_MIN_UNIQUE_RATIO:
            return "repetitive"

    words = re.findall(r"[A-Za-z0-9]+", markdown.lower())
    if len(words) >= _QUALITY_MIN_WORDS_FOR_TTR:
        if len(set(words)) / len(words) < _QUALITY_MIN_TTR:
            return "low information"

    return None


def transcribe_page(
    png_bytes: bytes,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Send a page PNG to the vision model and return its Markdown transcription.

    Returns the markdown, or ``(markdown, usage)`` when ``return_usage`` is True.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _PROMPT}, _image_content(png_bytes)]}
    ]
    return _chat_completion(
        messages,
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=TRANSCRIBE_MAX_TOKENS,
        timeout=timeout,
        return_usage=return_usage,
    )


def transcribe_image(
    image_bytes: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Send an image to the vision model and return its Markdown transcription.

    Returns the markdown, or ``(markdown, usage)`` when ``return_usage`` is True.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _IMAGE_PROMPT}, _image_content(image_bytes, mime)]}
    ]
    return _chat_completion(
        messages,
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=TRANSCRIBE_MAX_TOKENS,
        timeout=timeout,
        return_usage=return_usage,
    )


def transcribe_image_meta(
    image_bytes: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Send an image to the vision model and return a high-level description.

    Used for diagrams/flowcharts where verbatim label transcription is worthless;
    asks for the diagram's purpose and main components instead.

    Returns the markdown, or ``(markdown, usage)`` when ``return_usage`` is True.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _DIAGRAM_PROMPT}, _image_content(image_bytes, mime)]}
    ]
    return _chat_completion(
        messages,
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=TRANSCRIBE_MAX_TOKENS,
        timeout=timeout,
        return_usage=return_usage,
    )
