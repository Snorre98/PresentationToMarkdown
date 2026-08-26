"""Client for the isolated audio-model server (diarization + enhancement).

PyTorch models (``pyannote-audio`` for speaker diarization, ``deepfilternet`` for
denoise/dereverb) are deliberately kept out of ``converter`` (ADR-0006, ADR-0008).
A dedicated server process (``scripts/audio_server.py``) serves them, and this
module is only a thin client:

    POST {base}/diarize
    {"path": "<audio>", "min_speakers": n, "max_speakers": n}
    -> [{"start": float, "end": float, "speaker": "SPEAKER_00"}, ...]

    POST {base}/enhance
    {"path": "<in.flac>", "output": "<out.flac>"}
    -> {"ok": true}

Configuration (environment variables):

- ``AUDIO_DIARIZE_ENABLED`` — diarization master switch. Default off.
- ``AUDIO_DIARIZE_BASE_URL`` — service base URL, default ``http://127.0.0.1:8083/v1``.
- ``AUDIO_DIARIZE_API_KEY`` — optional bearer token.
- ``AUDIO_ENHANCE_ENABLED`` — enhancement master switch. Default on.
- ``AUDIO_ENHANCE_BASE_URL`` — enhancement base URL, defaults to ``AUDIO_DIARIZE_BASE_URL``.
- ``AUDIO_ENHANCE_API_KEY`` — optional bearer token, defaults to ``AUDIO_DIARIZE_API_KEY``.
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

AUDIO_ENHANCE_ENABLED = os.environ.get("AUDIO_ENHANCE_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_ENHANCE_BASE_URL = os.environ.get("AUDIO_ENHANCE_BASE_URL", AUDIO_DIARIZE_BASE_URL)
AUDIO_ENHANCE_API_KEY = os.environ.get("AUDIO_ENHANCE_API_KEY") or AUDIO_DIARIZE_API_KEY

_DIARIZE_TIMEOUT = 1800.0
_ENHANCE_TIMEOUT = 1800.0


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


def enhance(
    audio_path: str,
    output_path: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = _ENHANCE_TIMEOUT,
) -> None:
    """Enhance ``audio_path`` (denoise/dereverb) and write it to ``output_path``.

    Raises on any network/HTTP error so callers can degrade to the
    non-enhanced audio.
    """
    payload = {"path": str(audio_path), "output": str(output_path)}
    url = (base_url or AUDIO_ENHANCE_BASE_URL).rstrip("/") + "/enhance"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    key = api_key or AUDIO_ENHANCE_API_KEY
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if isinstance(body, dict) and body.get("ok") is False:
        raise RuntimeError(body.get("error") or "enhancement failed")


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
