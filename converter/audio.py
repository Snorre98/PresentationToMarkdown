"""Client for the fleet's audio models (diarization + enhancement + ASR).

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

ASR has **two wire shapes** (ADR-0062): the MLX whisper models are served by the
fleet's ``whisper`` daemon through the OpenAI-compatible endpoint

    POST {base}/audio/transcriptions            (multipart/form-data)
    model, language, response_format=verbose_json, word_timestamps, file
    -> {"text": "...", "language": "en",
        "segments": [{"start", "end", "text", "words": [{word, start, end}]}]}

and NB-Whisper keeps the audio server's JSON ``/v1/asr`` route. The one public
entry point is :func:`transcribe_fleet`, which normalizes the fleet model name
and hides which shape is in play.

Configuration (environment variables):

- ``AUDIO_DIARIZE_ENABLED`` — diarization master switch. Default off.
- ``AUDIO_DIARIZE_SPEAKERS`` — exact speaker count (e.g. ``2`` for an
  interview). Implies diarization; overrides the min/max range.
- ``AUDIO_DIARIZE_MIN_SPEAKERS`` / ``AUDIO_DIARIZE_MAX_SPEAKERS`` — speaker-count
  range for pyannote to search. Either implies diarization.
- ``AUDIO_DIARIZE_BASE_URL`` — audio server base URL override. Default: the
  manifest's ``audio`` daemon (``:8089``, ADR-0035) via the control daemon's
  ``list`` projection, falling back to ``http://127.0.0.1:8089/v1``.
- ``AUDIO_DIARIZE_API_KEY`` — optional bearer token for the audio server.
- ``WHISPER_BASE_URL`` — whisper daemon base URL override. Default: the
  manifest's ``whisper`` daemon (``:8099``) via the ``list`` projection,
  falling back to ``http://127.0.0.1:8099/v1``.
- ``WHISPER_API_KEY`` — optional bearer token for the whisper daemon.
- ``AUDIO_ENHANCE_ENABLED`` — enhancement master switch. Default on.
- ``AUDIO_ENHANCE_BASE_URL`` — enhancement base URL, defaults to ``AUDIO_DIARIZE_BASE_URL``.
- ``AUDIO_ENHANCE_API_KEY`` — optional bearer token, defaults to ``AUDIO_DIARIZE_API_KEY``.
- ``AUDIO_DEREVERB_ENABLED`` — WPE dereverberation master switch. Default on.
- ``AUDIO_ISOLATE_ENABLED`` — voice isolation master switch. Default off.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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


# ── ASR model registry (ADR-0062) ─────────────────────────────────────────────

# The machine/library default: NB-Whisper (Norwegian-first) on the audio server,
# matching the engine setting default (ADR-0043/0044).
DEFAULT_AUDIO_MODEL = "nb-whisper-large"


@dataclass(frozen=True)
class AsrModelSpec:
    """One ASR model's serving contract (ADR-0062).

    ``repo`` is the fallback id sent as the OpenAI ``model`` field — the fleet
    ``list`` entry's ``modelId`` wins when the control daemon is reachable, so
    the manifest stays the source of truth. ``base_url`` is the static fallback
    when neither a protocol env override nor the manifest resolves.
    ``protocol`` selects the wire shape: ``openai`` for the whisper daemon's
    multipart ``/v1/audio/transcriptions``; ``audio_json`` for the audio
    server's ``/v1/asr``.
    """

    repo: str
    base_url: str
    protocol: str  # "openai" | "audio_json"


ASR_MODELS: dict[str, AsrModelSpec] = {
    "whisper-large-v3-asr-fp16": AsrModelSpec(
        repo="mlx-community/whisper-large-v3-asr-fp16",
        base_url="http://127.0.0.1:8099/v1",
        protocol="openai",
    ),
    "whisper-large-v3-turbo-asr-fp16": AsrModelSpec(
        repo="mlx-community/whisper-large-v3-turbo-asr-fp16",
        base_url="http://127.0.0.1:8099/v1",
        protocol="openai",
    ),
    "nb-whisper-large": AsrModelSpec(
        repo="NbAiLab/nb-whisper-large",
        base_url="http://127.0.0.1:8089/v1",
        protocol="audio_json",
    ),
}

# Persisted settings from before the fleet cutover hold the old mlx-whisper HF
# ids (ADR-0043/0044); aliasing keeps them working without a re-save.
AUDIO_MODEL_ALIASES = {
    # Pre-ADR-0007 friendly names and the old mlx-whisper HF ids.
    "whisper-large-v3": "whisper-large-v3-asr-fp16",
    "whisper-large-v3-turbo": "whisper-large-v3-turbo-asr-fp16",
    "mlx-community/whisper-large-v3-mlx": "whisper-large-v3-asr-fp16",
    "mlx-community/whisper-large-v3-turbo": "whisper-large-v3-turbo-asr-fp16",
}


def normalize_audio_model(value: str | None) -> str:
    """Return the canonical fleet ASR model name for ``value`` (ADR-0062).

    Old mlx-whisper HF ids are aliased to their fleet names; unknown values
    pass through so settings can still display (and operators can still set)
    a name this build does not know. Empty/``None`` means the default.
    """
    name = (value or "").strip()
    if not name:
        return DEFAULT_AUDIO_MODEL
    return AUDIO_MODEL_ALIASES.get(name, name)


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
    return_words: bool = False,
    num_beams: int | None = None,
) -> list[dict]:
    """Transcribe ``audio_path`` via the audio server's NB-Whisper ASR (ADR-0044).

    The audio-server lane for ``nb-whisper-large`` (ADR-0062); the MLX whisper
    models go through :func:`transcribe_fleet` instead. Returns
    ``[{start, end, text}, ...]`` segments. With ``return_words`` the server
    returns per-word timestamps instead of grouped utterances (ADR-0045), which
    the caller re-segments by speaker/pause. ``num_beams`` overrides the
    server's default beam width for a quality re-decode. Raises on any
    network/HTTP error so callers can fail loudly or degrade to a warning.
    """
    payload: dict = {"path": str(audio_path)}
    if language:
        payload["language"] = language
    if return_words:
        payload["return_words"] = True
    if num_beams is not None:
        payload["num_beams"] = int(num_beams)
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


def _asr_base_url(spec: AsrModelSpec, model_name: str) -> str:
    """Resolve one ASR model's base URL: env override → manifest → static.

    The override is protocol-scoped: ``WHISPER_BASE_URL`` for the OpenAI whisper
    daemon; the audio server keeps its existing ``AUDIO_DIARIZE_BASE_URL`` /
    manifest-``audio`` resolution. The control daemon's ``list`` projection wins
    over the static fallback (ADR-0029/ADR-0062).
    """
    if spec.protocol == "audio_json":
        return _audio_base_url()
    env = os.environ.get("WHISPER_BASE_URL")
    if env:
        return env.rstrip("/")
    return fleet.base_url(model_name) or spec.base_url


def asr_service(model_name: str) -> str:
    """The fleet service that serves an ASR model (ADR-0062).

    A whisper model is its own manifest entry; NB-Whisper is a route on the
    ``audio`` daemon. Unknown model names degrade to ``audio`` so the settings
    panel still has a row to show.
    """
    spec = ASR_MODELS.get(model_name)
    if spec is None or spec.protocol == "audio_json":
        return "audio"
    return model_name


def asr_base_url(model_name: str) -> str:
    """Resolve an ASR model's base URL for the availability projection (ADR-0063)."""
    spec = ASR_MODELS.get(model_name)
    if spec is None:
        return _audio_base_url()
    return _asr_base_url(spec, model_name)


def _manifest_repo(model_name: str, fallback: str) -> str:
    """The id the fleet serves ``model_name`` under (``list`` ``modelId``), else fallback.

    Keeps the manifest authoritative: the OpenAI ``model`` field must be the
    repo the daemon actually loaded, not a client-side guess (ADR-0062).
    """
    entry = fleet.list_models().get(model_name) or {}
    return str(entry.get("modelId") or fallback)


def _post_transcription(
    url: str,
    fields: dict[str, str],
    file_path: Path,
    *,
    api_key: str | None,
    timeout: float,
) -> dict:
    """POST a multipart transcription request, streaming the file from disk.

    ``http.client`` with a precomputed ``Content-Length`` and 1 MiB file reads:
    a multi-hundred-MB clean FLAC is never materialized in memory. Returns the
    parsed JSON body; raises ``RuntimeError`` with the server's ``error`` /
    ``detail`` on a non-2xx.
    """
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    file_header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{file_path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    closing = f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_length = (
        sum(len(part) for part in parts)
        + len(file_header)
        + file_path.stat().st_size
        + len(closing)
    )

    parsed = urlsplit(url)
    conn: http.client.HTTPConnection
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=timeout
        )
    else:
        conn = http.client.HTTPConnection(
            parsed.hostname, parsed.port or 80, timeout=timeout
        )
    try:
        conn.putrequest("POST", parsed.path or "/")
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(content_length))
        if api_key:
            conn.putheader("Authorization", f"Bearer {api_key}")
        conn.endheaders()
        for part in parts:
            conn.send(part)
        conn.send(file_header)
        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(closing)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    finally:
        conn.close()

    if not 200 <= status < 300:
        detail = None
        try:
            payload = json.loads(body)
            detail = payload.get("error") or payload.get("detail")
            if isinstance(detail, dict):  # OpenAI-style {"error": {"message": …}}
                detail = detail.get("message") or json.dumps(detail)
        except Exception:  # noqa: BLE001 - best-effort; fall back to the status code
            pass
        raise RuntimeError(
            str(detail) if detail else f"transcription returned HTTP {status}"
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"transcription returned a non-JSON body: {body[:200]!r}"
        ) from exc


def transcribe_fleet(
    audio_path: str,
    *,
    model: str,
    language: str | None = None,
    word_timestamps: bool = False,
    timeout: float = _ASR_TIMEOUT,
    base_url: str | None = None,
) -> list[dict]:
    """Transcribe ``audio_path`` on the fleet, hiding the two ASR wire shapes.

    ``whisper-*`` models POST multipart to the whisper daemon and parse
    ``verbose_json``: segments ``[{start, end, text}, ...]``, or the segments'
    per-word ``words`` when ``word_timestamps`` is set (ADR-0062).
    ``nb-whisper-large`` keeps the audio server's ``/v1/asr`` route unchanged.
    Raises on any network/HTTP error: callers must fail loudly — there is no
    subprocess fallback.
    """
    name = normalize_audio_model(model)
    spec = ASR_MODELS.get(name)
    if spec is None:
        raise RuntimeError(
            f"unknown ASR model {name!r}; known models: {', '.join(sorted(ASR_MODELS))}"
        )
    if spec.protocol == "audio_json":
        return asr(
            audio_path,
            language=language or "no",
            base_url=base_url or _asr_base_url(spec, name),
            timeout=timeout,
            return_words=word_timestamps,
        )

    base = (base_url or _asr_base_url(spec, name)).rstrip("/")
    fields = {
        "model": _manifest_repo(name, spec.repo),
        "response_format": "verbose_json",
        "word_timestamps": "true" if word_timestamps else "false",
    }
    if language:
        fields["language"] = language
    body = _post_transcription(
        base + "/audio/transcriptions",
        fields,
        Path(audio_path),
        api_key=os.environ.get("WHISPER_API_KEY") or None,
        timeout=timeout,
    )
    if word_timestamps:
        words: list[dict] = []
        for seg in body.get("segments") or []:
            for word in seg.get("words") or []:
                text = str(word.get("word") or word.get("text") or "").strip()
                if not text:
                    continue
                words.append(
                    {
                        "start": float(word["start"]),
                        "end": float(word["end"]),
                        "text": text,
                    }
                )
        return words
    segments: list[dict] = []
    for seg in body.get("segments") or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            {
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": text,
            }
        )
    return segments


def vad(
    audio_path: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = _ASR_TIMEOUT,
) -> list[dict]:
    """Return VAD speech regions as ``[{start, end}, ...]`` (ADR-0045).

    Used by the coverage detector to find speech spans the ASR produced no words
    for. Raises on any network/HTTP error so callers can degrade to "no coverage
    report".
    """
    payload: dict = {"path": str(audio_path)}
    url = (base_url or AUDIO_DIARIZE_BASE_URL).rstrip("/") + "/vad"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    key = api_key or AUDIO_DIARIZE_API_KEY
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    body = _post_json(req, timeout, "VAD")
    regions = body if isinstance(body, list) else body.get("regions", [])
    return [
        {"start": float(r["start"]), "end": float(r["end"])} for r in regions
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


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Seconds of temporal overlap between two ``[start, end)`` intervals."""
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Label each segment with the speaker occupying most of its span.

    ``segments`` and ``turns`` are both ``[{start, end, ...}]`` dicts; the
    segment's ``speaker`` key is set in place and the list is returned. Each
    segment is assigned by **temporal-overlap majority** rather than its
    midpoint, so a segment straddling a speaker change takes the label of the
    speaker who actually held the floor for most of it (and a segment with no
    overlapping turn keeps ``speaker = None``).
    """
    for seg in segments:
        best_speaker: str | None = None
        best_overlap = 0.0
        for turn in turns:
            ov = _overlap((seg["start"], seg["end"]), (turn["start"], turn["end"]))
            if ov > best_overlap:
                best_overlap = ov
                best_speaker = turn["speaker"]
        seg["speaker"] = best_speaker if best_overlap > 0 else None
    return segments


def assign_speakers_by_words(words: list[dict], turns: list[dict]) -> list[dict]:
    """Label each word with the speaker active at its midpoint.

    ``words`` are ``[{start, end, text}]`` (the server's per-word timestamps,
    ADR-0045); ``turns`` are ``[{start, end, speaker}]``. The word's ``speaker``
    key is set in place and the list returned. Word timestamps are fine-grained,
    so the containing turn is unambiguous; uncovered words keep ``speaker =
    None``.
    """
    for word in words:
        midpoint = (word["start"] + word["end"]) / 2.0
        word["speaker"] = None
        for turn in turns:
            if turn["start"] <= midpoint < turn["end"]:
                word["speaker"] = turn["speaker"]
                break
    return words
