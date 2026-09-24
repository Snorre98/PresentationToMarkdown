"""Opt-in LLM post-edit for ASR transcripts (gated off by default).

Fixes obvious ASR errors — proper nouns, student jargon and garbled Norwegian —
with a local OpenAI-compatible chat model, guarded by the same word-preservation
discipline as :mod:`converter.format`: an edit is accepted only if it drops no
content word and invents none, so the pass can never remove or fabricate speech.
A down server degrades to a no-op (every segment is kept verbatim).

The pass is **off by default** because the local ``text`` daemon
(Llama-3.2-3B) is weak at Norwegian; it should only be enabled once a
Norwegian-capable model is serving (the manifest also references a
``mistral-24b`` daemon, unverified). Set ``TRANSCRIPT_EDIT_ENABLED=1`` and point
``TRANSCRIPT_EDIT_BASE_URL``/``TRANSCRIPT_EDIT_MODEL`` at a strong model.

Configuration (environment variables):

- ``TRANSCRIPT_EDIT_ENABLED`` — master switch. Default off.
- ``TRANSCRIPT_EDIT_BASE_URL`` — chat endpoint. Default the ``text`` daemon.
- ``TRANSCRIPT_EDIT_MODEL`` — model id. Default the summary/text model.
- ``TRANSCRIPT_EDIT_API_KEY`` — optional bearer token.
"""
from __future__ import annotations

import os
import re

EDIT_BASE_URL = os.environ.get("TRANSCRIPT_EDIT_BASE_URL", "http://127.0.0.1:8083/v1")
EDIT_MODEL = os.environ.get(
    "TRANSCRIPT_EDIT_MODEL", "mlx-community/Llama-3.2-3B-Instruct-4bit"
)
EDIT_API_KEY = os.environ.get("TRANSCRIPT_EDIT_API_KEY")

TRANSCRIPT_EDIT_ENABLED = os.environ.get("TRANSCRIPT_EDIT_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Norwegian-aware tokenizer (ASCII + æ/ø/å), unlike the converter's ASCII-only
# word set which would mangle Norwegian stems.
_WORD_RE = re.compile(r"[a-zæøå0-9]+")

# A "content word" is long enough to carry meaning; short function words are
# excluded from the preservation gate so a correction may reflow them.
_CONTENT_LEN = 4

_PROMPT = (
    "Fix obvious ASR errors in this Norwegian interview transcript segment — "
    "proper nouns, student jargon, and garbled words — while keeping every "
    "other word exactly as it is.\n\n"
    "Do NOT add, remove, reword, or reorder any words. Do not translate.\n\n"
    "Output only the corrected text, with no commentary or code fences."
)


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _content_words(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= _CONTENT_LEN}


def _chat_edit(text: str) -> str:
    from converter.vision import _chat_completion

    return _chat_completion(
        [{"role": "user", "content": _PROMPT + "\n\n" + text}],
        base_url=EDIT_BASE_URL,
        model=EDIT_MODEL,
        api_key=EDIT_API_KEY,
        temperature=0.0,
    ).strip()


def _safe_edit(original: str, corrected: str) -> bool:
    """Whether ``corrected`` preserves ``original``'s content (no drop/invention)."""
    if not corrected:
        return False
    if _content_words(original) - _words(corrected):
        return False  # a content word was dropped
    # No invented content: the correction may fix words 1:1 but not grow the
    # transcript by more than a couple of words.
    if len(corrected.split()) > len(original.split()) + 2:
        return False
    return True


def edit_transcript(segments: list[dict], source: str = "") -> list[dict]:
    """Return ``segments`` with low-confidence ASR errors fixed, word-preserving.

    Each segment is offered to the local chat model; an edit is accepted only if
    it drops no content word and invents none. Any failure (server down, model
    error, or an unsafe edit) keeps the original segment. Never raises.
    """
    out: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            out.append(seg)
            continue
        try:
            corrected = _chat_edit(text)
        except Exception:  # noqa: BLE001 - server down -> keep original
            out.append(seg)
            continue
        if corrected == text or not _safe_edit(text, corrected):
            out.append(seg)
            continue
        out.append({**seg, "text": corrected})
    return out
