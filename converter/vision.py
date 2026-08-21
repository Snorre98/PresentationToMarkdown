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

VISION_ENABLED = os.environ.get("VISION_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "http://127.0.0.1:8081/v1")
VISION_MODEL = os.environ.get("VISION_MODEL", "mlx-community/Ornith-1.0-9B-8bit")
VISION_API_KEY = os.environ.get("VISION_API_KEY") or None

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
) -> str:
    """Send a chat request to an OpenAI-compatible endpoint and return its text."""
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
    return body["choices"][0]["message"]["content"].strip()


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


def transcribe_page(
    png_bytes: bytes,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
) -> str:
    """Send a page PNG to the vision model and return its Markdown transcription."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _PROMPT}, _image_content(png_bytes)]}
    ]
    return _chat_completion(
        messages, base_url=base_url, model=model, api_key=api_key, timeout=timeout
    )


def transcribe_image(
    image_bytes: bytes,
    mime: str = "image/png",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
) -> str:
    """Send an image to the vision model and return its Markdown transcription."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _IMAGE_PROMPT}, _image_content(image_bytes, mime)]}
    ]
    return _chat_completion(
        messages, base_url=base_url, model=model, api_key=api_key, timeout=timeout
    )
