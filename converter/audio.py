"""Client for the isolated audio-model server (diarization + enhancement + dereverb + isolate).

PyTorch models (``pyannote-audio`` for speaker diarization, ``deepfilternet`` for
denoise/dereverb, ``speechbrain`` SepFormer for voice isolation) are deliberately
kept out of ``converter`` (ADR-0006, ADR-0008, ADR-0010). A dedicated server
process (``scripts/audio_server.py``) serves them, and this module is only a thin
client:

    POST {base}/diarize
    {"path": "<audio>", "min_speakers": n, "max_speakers": n}
    -> [{"start": float, "end": float, "speaker": "SPEAKER_00"}, ...]

    POST {base}/asr
    {"path": "<clean.flac>", "language": "no"}
    -> [{"start": float, "end": float, "text": "..."}, ...]

    POST {base}/enhance
    {"path": "<in.flac>", "output": "<out.flac>"}
    -> {"ok": true}

    POST {base}/dereverb
    {"path": "<in.flac>", "output": "<out.flac>"}
    -> {"ok": true}

    POST {base}/isolate
    {"path": "<in.flac>", "output": "<out.flac>"}
    -> {"ok": true}

Configuration (environment variables):

- ``AUDIO_DIARIZE_ENABLED`` — diarization master switch. Default off.
- ``AUDIO_DIARIZE_SPEAKERS`` — exact speaker count (e.g. ``2`` for an
  interview). Implies diarization; overrides the min/max range.
- ``AUDIO_DIARIZE_MIN_SPEAKERS`` / ``AUDIO_DIARIZE_MAX_SPEAKERS`` — speaker-count
  range for pyannote to search. Either implies diarization.
- ``AUDIO_DIARIZE_BASE_URL`` — service base URL override. Default: the manifest's
  ``audio`` daemon (``:8089``, ADR-0035) via the control daemon's ``list``
  projection, falling back to ``http://127.0.0.1:8089/v1`` when unreachable.
- ``AUDIO_DIARIZE_API_KEY`` — optional bearer token.
- ``AUDIO_ENHANCE_ENABLED`` — enhancement master switch. Default on.
- ``AUDIO_ENHANCE_BASE_URL`` — enhancement base URL, defaults to ``AUDIO_DIARIZE_BASE_URL``.
- ``AUDIO_ENHANCE_API_KEY`` — optional bearer token, defaults to ``AUDIO_DIARIZE_API_KEY``.
- ``AUDIO_DEREVERB_ENABLED`` — WPE dereverberation master switch. Default on.
- ``AUDIO_ISOLATE_ENABLED`` — voice isolation master switch. Default off.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from converter import fleet

AUDIO_DIARIZE_ENABLED = os.environ.get("AUDIO_DIARIZE_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _int_env(name: str) -> int | None:
    """Parse ``name`` as an int, tolerating unset/garbage (``None``)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# Speaker-count hints for pyannote. ``SPEAKERS`` is an exact count; the min/max
# pair is a range to search. All three imply diarization (see
# ``diarize_requested``), so ``AUDIO_DIARIZE_SPEAKERS=2`` alone is enough for an
# interview.
AUDIO_DIARIZE_SPEAKERS = _int_env("AUDIO_DIARIZE_SPEAKERS")
AUDIO_DIARIZE_MIN_SPEAKERS = _int_env("AUDIO_DIARIZE_MIN_SPEAKERS")
AUDIO_DIARIZE_MAX_SPEAKERS = _int_env("AUDIO_DIARIZE_MAX_SPEAKERS")


def diarize_requested() -> bool:
    """True when speaker labelling should run (explicit switch or a count hint)."""
    return (
        AUDIO_DIARIZE_ENABLED
        or AUDIO_DIARIZE_SPEAKERS is not None
        or AUDIO_DIARIZE_MIN_SPEAKERS is not None
        or AUDIO_DIARIZE_MAX_SPEAKERS is not None
    )


def diarize_bounds() -> dict:
    """Return ``{min_speakers, max_speakers}`` kwargs for the diarize client.

    The exact ``AUDIO_DIARIZE_SPEAKERS`` wins over the min/max range; an empty
    dict means pyannote auto-detects the count.
    """
    if AUDIO_DIARIZE_SPEAKERS is not None:
        return {
            "min_speakers": AUDIO_DIARIZE_SPEAKERS,
            "max_speakers": AUDIO_DIARIZE_SPEAKERS,
        }
    kwargs: dict = {}
    if AUDIO_DIARIZE_MIN_SPEAKERS is not None:
        kwargs["min_speakers"] = AUDIO_DIARIZE_MIN_SPEAKERS
    if AUDIO_DIARIZE_MAX_SPEAKERS is not None:
        kwargs["max_speakers"] = AUDIO_DIARIZE_MAX_SPEAKERS
    return kwargs


def _audio_base_url() -> str:
    """Resolve the audio server base URL: env override → daemon list → fallback.

    The audio server is a manifest daemon (``audio``, ``:8089``, ADR-0035); the
    control daemon's ``list`` projection wins over the static fallback, mirroring
    the AI-pass endpoint resolution in ``converter.config`` (ADR-0029).
    """
    env = os.environ.get("AUDIO_DIARIZE_BASE_URL")
    if env:
        return env.rstrip("/")
    return fleet.base_url("audio") or "http://127.0.0.1:8089/v1"


AUDIO_DIARIZE_BASE_URL = _audio_base_url()
AUDIO_DIARIZE_API_KEY = os.environ.get("AUDIO_DIARIZE_API_KEY") or None

AUDIO_ENHANCE_ENABLED = os.environ.get("AUDIO_ENHANCE_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_ENHANCE_BASE_URL = os.environ.get("AUDIO_ENHANCE_BASE_URL", AUDIO_DIARIZE_BASE_URL)
AUDIO_ENHANCE_API_KEY = os.environ.get("AUDIO_ENHANCE_API_KEY") or AUDIO_DIARIZE_API_KEY

AUDIO_DEREVERB_ENABLED = os.environ.get("AUDIO_DEREVERB_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_ISOLATE_ENABLED = os.environ.get("AUDIO_ISOLATE_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_DIARIZE_TIMEOUT = 1800.0
_ASR_TIMEOUT = 3600.0
_ENHANCE_TIMEOUT = 1800.0
_DEREVERB_TIMEOUT = 1800.0
_ISOLATE_TIMEOUT = 1800.0


def _post_json(req: urllib.request.Request, timeout: float, what: str):
    """POST ``req`` and return the parsed JSON body, surfacing server errors.

    ``urlopen`` raises ``HTTPError`` on a 4xx/5xx *before* we can read the body,
    so the server's ``{"error": ...}`` would otherwise be lost. We read it here
    and fold it into a clear ``RuntimeError`` instead.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001 - best-effort; fall back to the status code
            detail = None
        raise RuntimeError(detail or f"{what} returned HTTP {exc.code}") from exc


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
    body = _post_json(req, timeout, "diarization")
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


def asr(
    audio_path: str,
    language: str = "no",
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = _ASR_TIMEOUT,
) -> list[dict]:
    """Transcribe ``audio_path`` via the server's NB-Whisper ASR (ADR-0044).

    Returns ``[{start, end, text}, ...]`` segments. Raises on any network/HTTP
    error so callers can degrade to the mlx-whisper fallback.
    """
    payload: dict = {"path": str(audio_path)}
    if language:
        payload["language"] = language
    url = (base_url or AUDIO_DIARIZE_BASE_URL).rstrip("/") + "/asr"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    key = api_key or AUDIO_DIARIZE_API_KEY
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    body = _post_json(req, timeout, "ASR")
    segs = body if isinstance(body, list) else body.get("segments", [])
    return [
        {"start": float(s["start"]), "end": float(s["end"]), "text": s["text"]}
        for s in segs
    ]


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
    _post_audio("enhance", audio_path, output_path, base_url, api_key, timeout, "enhancement")


def dereverb(
    audio_path: str,
    output_path: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = _DEREVERB_TIMEOUT,
) -> None:
    """Dereverberate ``audio_path`` (WPE) and write it to ``output_path``.

    Raises on any network/HTTP error so callers can degrade to the reverberant
    audio.
    """
    _post_audio("dereverb", audio_path, output_path, base_url, api_key, timeout, "dereverberation")


def isolate(
    audio_path: str,
    output_path: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = _ISOLATE_TIMEOUT,
) -> None:
    """Isolate the dominant voice in ``audio_path`` and write it to ``output_path``.

    Raises on any network/HTTP error so callers can degrade to the unisolated
    audio.
    """
    _post_audio("isolate", audio_path, output_path, base_url, api_key, timeout, "voice isolation")


def _post_audio(
    endpoint: str,
    audio_path: str,
    output_path: str,
    base_url: str | None,
    api_key: str | None,
    timeout: float,
    what: str,
) -> None:
    payload = {"path": str(audio_path), "output": str(output_path)}
    url = (base_url or AUDIO_ENHANCE_BASE_URL).rstrip("/") + "/" + endpoint
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    key = api_key or AUDIO_ENHANCE_API_KEY
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    body = _post_json(req, timeout, what)
    if isinstance(body, dict) and body.get("ok") is False:
        raise RuntimeError(body.get("error") or f"{what} failed")


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
