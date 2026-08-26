"""Optional audio-to-text transcription post-pass for converted slides.

Turns a lecture recording into a timestamped, speaker-labelled transcript and
attaches it to the Markdown converted from a PDF/PPTX. Local only: the audio is
cleaned (deterministic ffmpeg chain + optional DeepFilterNet denoise/dereverb)
and persisted as a ``<stem>.clean.flac``, then transcribed by **mlx-whisper** run
as a subprocess, so ``converter`` stays free of MLX. Speaker labelling (optional)
comes from a separate diarization service (see ``converter.audio``, ADR-0006/0008).

Configuration (environment variables):

- ``AUDIO_ENABLED`` — master switch. Default off.
- ``AUDIO_MODEL`` — ASR model id, default
  ``mlx-community/whisper-large-v3-turbo`` (override to ``…-large-v3-mlx``).
- ``AUDIO_MLX_WHISPER_BIN`` — mlx-whisper CLI, default ``mlx_whisper``.
- ``AUDIO_FFMPEG_BIN`` — ffmpeg binary, default ``ffmpeg``.
- ``AUDIO_LANGUAGE`` — optional Whisper language hint (e.g. ``no``, ``en``).
- ``AUDIO_TIMEOUT`` — per-file subprocess timeout in seconds, default ``3600``.
- ``AUDIO_PREPROCESS`` — deterministic ffmpeg enhancement chain. Default on.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from converter.audio import (
    AUDIO_DIARIZE_ENABLED,
    AUDIO_ENHANCE_ENABLED,
    assign_speakers,
    diarize,
    enhance,
)
from converter.logstore import record_segment

AUDIO_ENABLED = os.environ.get("AUDIO_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_PREPROCESS = os.environ.get("AUDIO_PREPROCESS", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_MODEL = os.environ.get("AUDIO_MODEL", "mlx-community/whisper-large-v3-turbo")
AUDIO_MLX_WHISPER_BIN = os.environ.get("AUDIO_MLX_WHISPER_BIN", "mlx_whisper")
AUDIO_FFMPEG_BIN = os.environ.get("AUDIO_FFMPEG_BIN", "ffmpeg")
AUDIO_LANGUAGE = os.environ.get("AUDIO_LANGUAGE") or None
AUDIO_TIMEOUT = float(os.environ.get("AUDIO_TIMEOUT", "3600"))

# Deterministic speech-enhancement chain for lecture-hall audio (ADR-0008):
# remove hum/rumble, cut hiss, reduce stationary noise, normalise loudness.
_ENHANCE_FILTER = "highpass=f=80,lowpass=f=8000,afftdn=nf=-30,loudnorm=I=-16:TP=-1.5:LRA=11"

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".m4b", ".mp4",
    ".mov", ".webm", ".aiff", ".aif", ".wma",
}

# When several same-stem candidates exist, prefer lossless/compact first.
_AUDIO_PRIORITY = {
    ".wav": 0, ".m4a": 1, ".mp3": 2, ".aac": 3, ".flac": 4, ".ogg": 5,
    ".aiff": 6, ".aif": 6, ".m4b": 7, ".mp4": 8, ".mov": 9, ".webm": 10,
    ".wma": 11,
}


def _run(cmd: list[str], timeout: float = AUDIO_TIMEOUT) -> str:
    """Run ``cmd``, returning stdout; raise on a non-zero exit."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{cmd[0]} not found — is it installed and on PATH?") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): " + " | ".join(tail))
    return proc.stdout


def find_audio_for_source(source_path: Path) -> Path | None:
    """Return a same-stem audio file beside ``source_path``, or ``None``."""
    try:
        parent = source_path.parent
        stem = source_path.stem
        candidates = [
            p
            for p in parent.iterdir()
            if p.is_file() and p.stem == stem and p.suffix.lower() in AUDIO_EXTENSIONS
        ]
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: _AUDIO_PRIORITY.get(p.suffix.lower(), 99))
    return candidates[0]


def transcribe_audio(
    audio_path: Path,
    clean_path: Path,
    model: str | None = None,
    language: str | None = None,
    mlx_bin: str | None = None,
    ffmpeg_bin: str | None = None,
    timeout: float = AUDIO_TIMEOUT,
    warnings: list[str] | None = None,
) -> list[dict]:
    """Enhance ``audio_path``, persist it as a FLAC, and transcribe it.

    The cleaned audio is written to ``clean_path`` (16 kHz mono FLAC) — first by
    the deterministic ffmpeg chain, then upgraded in place by DeepFilterNet when
    ``AUDIO_ENHANCE_ENABLED`` and the server is up (a failure only warns).

    Returns ``[{start, end, text}, ...]`` (seconds, verbatim text). Raises on any
    fatal subprocess failure so callers can degrade gracefully.
    """
    ffmpeg_cmd = [
        ffmpeg_bin or AUDIO_FFMPEG_BIN,
        "-y", "-i", str(audio_path),
    ]
    if AUDIO_PREPROCESS:
        ffmpeg_cmd += ["-af", _ENHANCE_FILTER]
    ffmpeg_cmd += ["-ar", "16000", "-ac", "1", "-c:a", "flac", str(clean_path)]
    _run(ffmpeg_cmd, timeout=timeout)

    if AUDIO_ENHANCE_ENABLED:
        try:
            enhance(str(clean_path), str(clean_path))
        except Exception as exc:  # noqa: BLE001 - degrade to preprocessed audio
            if warnings is not None:
                warnings.append(f"Audio enhancement failed: {exc}; using preprocessed audio")

    with tempfile.TemporaryDirectory(prefix="ptm-audio-") as tmp:
        tmpdir = Path(tmp)
        cmd = [
            mlx_bin or AUDIO_MLX_WHISPER_BIN,
            str(clean_path),
            "--model", model or AUDIO_MODEL,
            "--output-format", "json",
            "--output-dir", str(tmpdir),
        ]
        lang = language or AUDIO_LANGUAGE
        if lang:
            cmd += ["--language", lang]
        _run(cmd, timeout=timeout)
        data = json.loads((tmpdir / (clean_path.stem + ".json")).read_text(encoding="utf-8"))

    segments: list[dict] = []
    for seg in data.get("segments", []):
        text = (seg.get("text") or "").strip()
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


def format_timestamp(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS``."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def segments_to_markdown(segments: list[dict], model: str | None = None) -> str:
    """Render timestamped segments as a Markdown ``# Transcript`` section."""
    lines = ["# Transcript", "", "<details>", f"<summary>Auto-generated transcript ({model or AUDIO_MODEL})</summary>", ""]
    for seg in segments:
        ts = format_timestamp(seg["start"])
        text = seg["text"].strip()
        speaker = seg.get("speaker")
        lines.append(f"[{ts}] **{speaker}:** {text}" if speaker else f"[{ts}] {text}")
    lines += ["", "</details>"]
    return "\n".join(lines)


def _srt_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    """Render segments as a SubRip (``.srt``) file with speaker cues."""
    blocks: list[str] = []
    for i, seg in enumerate(segments, start=1):
        text = seg["text"].strip()
        speaker = seg.get("speaker")
        if speaker:
            text = f"[{speaker}] {text}"
        blocks.append(
            f"{i}\n{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def attach_transcript(
    md_path: Path,
    source_path: Path,
    warnings: list[str],
    audio_path: str | Path | None = None,
) -> None:
    """Transcribe the source's audio and attach it to ``md_path``.

    No-op unless ``AUDIO_ENABLED``; never raises. The cleaned audio is persisted
    as ``<stem>.clean.flac``, the transcript is appended as a ``# Transcript``
    section and written to a ``<stem>.transcript.srt`` sidecar, and every
    segment is recorded to ``ptm.sqlite``.
    """
    if not AUDIO_ENABLED:
        return
    try:
        audio = Path(audio_path) if audio_path else find_audio_for_source(source_path)
        if audio is None or not audio.exists():
            return
        clean_path = md_path.with_name(md_path.stem + ".clean.flac")
        segments = transcribe_audio(audio, clean_path, warnings=warnings)
        if not segments:
            return
        if AUDIO_DIARIZE_ENABLED:
            try:
                turns = diarize(str(clean_path))
                assign_speakers(segments, turns)
            except Exception as exc:  # noqa: BLE001 - degrade to unlabelled transcript
                warnings.append(f"Diarization failed: {exc}; keeping unlabelled transcript")

        for seg in segments:
            record_segment(
                source=str(source_path),
                start=seg["start"],
                end=seg["end"],
                text=seg["text"],
                speaker=seg.get("speaker"),
                model=AUDIO_MODEL,
            )

        md = md_path.read_text(encoding="utf-8")
        md_path.write_text(
            md.rstrip("\n") + "\n\n" + segments_to_markdown(segments) + "\n",
            encoding="utf-8",
        )

        srt_path = md_path.with_name(md_path.stem + ".transcript.srt")
        srt_path.write_text(segments_to_srt(segments), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - transcription never fails the conversion
        warnings.append(f"Audio transcription failed: {exc}")
