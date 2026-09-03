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
- ``VISION_MIN_IMAGE_DIM`` — skip transcription for images whose smaller native
  side is below this many pixels (default ``250``). Unreadably low-res images
  just waste inference time.
- ``VISION_BLUR_THRESHOLD`` — skip transcription for images whose Laplacian
  variance (a sharpness proxy) is below this value (default ``30.0``). Note the
  metric measures edge energy, so clean line-art diagrams (flat backgrounds,
  thin lines) score low even when sharp; the default is deliberately
  conservative to avoid skipping them.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from collections import Counter

VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "http://127.0.0.1:8081/v1")
VISION_MODEL = os.environ.get("VISION_MODEL", "mlx-community/Qwen2.5-VL-7B-Instruct-4bit")
VISION_API_KEY = os.environ.get("VISION_API_KEY") or None

VISION_MIN_IMAGE_DIM = int(os.environ.get("VISION_MIN_IMAGE_DIM", "250"))
VISION_BLUR_THRESHOLD = float(os.environ.get("VISION_BLUR_THRESHOLD", "30.0"))

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

_COLUMN_PROMPT = (
    "Transcribe this text column to Markdown, reading top-to-bottom. Be lossless: "
    "include every piece of text in the column, verbatim, and keep its structure.\n\n"
    "Rules:\n"
    "- Do not add a title or heading unless one is clearly present in this column.\n"
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
    "Describe this diagram in plain prose. State what kind of diagram it is "
    "(e.g. process flow, layered architecture, org chart) and the main idea or "
    "purpose it conveys.\n\n"
    "Rules:\n"
    "- Write 1-3 sentences of prose. Do not use bullet lists or headings.\n"
    "- Do not enumerate every node or label; a high-level gist is what matters.\n"
    "- If you can read a title or a few prominent labels confidently, append "
    "them verbatim after your prose, one per line.\n"
    "- Do not invent text or labels you cannot actually read.\n"
    "- Do not output code or Mermaid.\n\n"
    "Output only the description (and any verbatim labels)."
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


def _laplacian_variance(gray: list[int], width: int, height: int) -> float:
    """Variance of the Laplacian of a grayscale buffer — a sharpness proxy.

    A blurry image has little high-frequency energy, so its Laplacian variance
    is low; a sharp image has edges, so it is high. ``gray`` is a flat list of
    byte values (0-255), row-major.
    """
    if width < 3 or height < 3 or len(gray) < width * height:
        return 0.0
    laps: list[float] = []
    for y in range(1, height - 1):
        row = y * width
        up = row - width
        down = row + width
        for x in range(1, width - 1):
            lap = (
                4 * gray[row + x]
                - gray[up + x]
                - gray[down + x]
                - gray[row + x - 1]
                - gray[row + x + 1]
            )
            laps.append(float(lap))
    if not laps:
        return 0.0
    mean = sum(laps) / len(laps)
    return sum((v - mean) ** 2 for v in laps) / len(laps)


def image_sharpness(blob: bytes, ext: str = "png") -> float | None:
    """Return the Laplacian variance (sharpness) of an image, or ``None`` on failure."""
    try:
        import pymupdf as fitz

        doc = fitz.open(stream=blob, filetype=(ext or "png").lstrip("."))
        pix = doc[0].get_pixmap()
        doc.close()
    except Exception:
        return None
    n = pix.n
    width, height = pix.width, pix.height
    samples = pix.samples
    stride = pix.stride
    gray: list[int] = []
    for y in range(height):
        row_start = y * stride
        for x in range(width):
            idx = row_start + x * n
            if n >= 3:
                v = (samples[idx] + samples[idx + 1] + samples[idx + 2]) // 3
            else:
                v = samples[idx]
            gray.append(v)
    return _laplacian_variance(gray, width, height)


def image_readable(
    blob: bytes,
    ext: str = "png",
    width: int | None = None,
    height: int | None = None,
) -> str | None:
    """Return a reason the image is unreadable, or ``None`` if it is worth trying.

    Checks native resolution first (cheap metadata), then blur (decodes the
    image). Unreadably low-resolution or blurry images are skipped before any
    model call, since a VLM cannot read them and will only hallucinate.
    """
    if width is not None and height is not None:
        if min(width, height) < VISION_MIN_IMAGE_DIM:
            return "low resolution"
    sharpness = image_sharpness(blob, ext)
    if sharpness is not None and sharpness < VISION_BLUR_THRESHOLD:
        return "blurry"
    return None


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
    """Strip emphasis, list markers and trailing enumeration digits so repeated
    lines compare equal (e.g. ``Data Source 1`` … ``Data Source 111``)."""
    line = line.strip()
    line = re.sub(r"\*\*+|\*", "", line)
    line = re.sub(r"^[-+]\s+", "", line)
    line = re.sub(r"\s*\d+\s*$", "", line)
    return re.sub(r"\s+", " ", line).strip().lower()


def bullet_item_count(markdown: str) -> int:
    """Count bullet list items in a transcription."""
    return sum(
        1
        for line in markdown.splitlines()
        if line.lstrip().startswith(("- ", "* ", "+ "))
    )


_PLACEHOLDER_RE = re.compile(r"\.\.\.|\[[^\]]*\.\.\.[^\]]*\]|\[(?:specific|your|insert|placeholder)[^\]]*\]")


def transcription_quality(markdown: str) -> str | None:
    """Return a rejection reason if a transcription looks worthless, else None.

    Detects the classic vision-model failure modes: a repetition loop where the
    same label is emitted over and over (including monotonically numbered
    filler), pathological bullet nesting, runaway length, placeholder/template
    echo, and near-zero information density.
    """
    if not markdown or not markdown.strip():
        return "empty"
    lines = [ln for ln in markdown.splitlines() if ln.strip()]
    if len(lines) > _QUALITY_MAX_LINES:
        return "runaway length"

    if _PLACEHOLDER_RE.search(markdown):
        return "placeholder"

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


def transcribe_column(
    png_bytes: bytes,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
    return_usage: bool = False,
):
    """Send a single column-slice PNG to the vision model for a Markdown transcription.

    Uses a column-specific prompt (top-to-bottom, no slide title) so a sliced
    column is read as a continuous text stream rather than a slide.

    Returns the markdown, or ``(markdown, usage)`` when ``return_usage`` is True.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _COLUMN_PROMPT}, _image_content(png_bytes)]}
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
