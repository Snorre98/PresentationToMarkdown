"""Speaker-diarization client for the audio pass.

Diarization needs ``pyannote-audio``, a PyTorch model that is deliberately kept
out of ``converter`` (see ADR-0006). A dedicated server process serves it behind
a single endpoint, and this module is only a thin client:

    POST {base}/diarize
    {"path": "<audio>", "min_speakers": n, "max_speakers": n}
    -> [{"start": float, "end": float, "speaker": "SPEAKER_00"}, ...]

Configuration (environment variables):

- ``AUDIO_DIARIZE_ENABLED`` — master switch. Default off.
- ``AUDIO_DIARIZE_BASE_URL`` — service base URL, default ``http://127.0.0.1:8083/v1``.
- ``AUDIO_DIARIZE_API_KEY`` — optional bearer token.
"""
from __future__ import annotations

import json
import os
import urllib.request

AUDIO_DIARIZE_ENABLED = os.environ.get("AUDIO_DIARIZE_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_DIARIZE_BASE_URL = os.environ.get("AUDIO_DIARIZE_BASE_URL", "http://127.0.0.1:8083/v1")
AUDIO_DIARIZE_API_KEY = os.environ.get("AUDIO_DIARIZE_API_KEY") or None

_DIARIZE_TIMEOUT = 1800.0


def diarize(
    audio_path: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = _DIARIZE_TIMEOUT,
) -> list[dict]:
    """Return speaker turns as ``[{start, end, speaker}, ...]``.

    Raises on any network/HTTP error so callers can degrade to an unlabelled
    transcript.
    """
    payload: dict = {"path": str(audio_path)}
    if min_speakers is not None:
        payload["min_speakers"] = min_speakers
    if max_speakers is not None:
        payload["max_speakers"] = max_speakers
    url = (base_url or AUDIO_DIARIZE_BASE_URL).rstrip("/") + "/diarize"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    key = api_key or AUDIO_DIARIZE_API_KEY
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    turns = body if isinstance(body, list) else body.get("turns", [])
    result: list[dict] = []
    for turn in turns:
        result.append(
            {
                "start": float(turn["start"]),
                "end": float(turn["end"]),
                "speaker": str(turn.get("speaker") or turn.get("label") or "SPEAKER"),
            }
        )
    return result


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Label each segment with the speaker active at its midpoint.

    ``segments`` and ``turns`` are both ``[{start, end, ...}]`` dicts; the
    segment's ``speaker`` key is set in place and the list is returned. Segments
    with no overlapping turn keep ``speaker = None``.
    """
    for seg in segments:
        midpoint = (seg["start"] + seg["end"]) / 2.0
        seg["speaker"] = None
        for turn in turns:
            if turn["start"] <= midpoint < turn["end"]:
                seg["speaker"] = turn["speaker"]
                break
    return segments
