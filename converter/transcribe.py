"""Standalone audio-to-text transcription for lecture recordings.

Turns a lecture recording into a timestamped, speaker-labelled transcript and
attaches it to an existing Markdown file, or writes it to a fresh
``<stem>.transcript.md`` when no Markdown exists yet. Local only: the audio is
cleaned (deterministic ffmpeg chain + optional DeepFilterNet denoise/dereverb)
and persisted as a ``<stem>.clean.flac``, then transcribed by **mlx-whisper** run
as a subprocess, so ``converter`` stays free of MLX. Speaker labelling (optional)
comes from a separate diarization service (see ``converter.audio``, ADR-0006/0008).

This module is **not** part of the conversion pipeline: ``convert_file``/``convert_files``
never call into it (ADR-0009). It is driven by the dedicated ``ptm-transcribe``
command (``cli_transcribe``), which operates on Markdown and/or audio directly.

Configuration (environment variables):

- ``AUDIO_ENABLED`` — master switch. Default off.
- ``AUDIO_MODEL`` — ASR model id, default
  ``mlx-community/whisper-large-v3-turbo`` (override to ``…-large-v3-mlx``).
- ``AUDIO_MLX_WHISPER_BIN`` — mlx-whisper CLI, default ``mlx_whisper``.
- ``AUDIO_FFMPEG_BIN`` — ffmpeg binary, default ``ffmpeg``.
- ``AUDIO_LANGUAGE`` — optional Whisper language hint (e.g. ``no``, ``en``).
- ``AUDIO_TIMEOUT`` — per-file subprocess timeout in seconds, default ``3600``.
- ``AUDIO_HEARTBEAT_SECONDS`` — quiet-interval before a ``still working …`` line
  is emitted while streaming subprocess output, default ``20``.
- ``AUDIO_PREPROCESS`` — deterministic ffmpeg enhancement chain. Default on.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

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
AUDIO_HEARTBEAT_SECONDS = float(os.environ.get("AUDIO_HEARTBEAT_SECONDS", "20"))

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


def _run(
    cmd: list[str],
    timeout: float = AUDIO_TIMEOUT,
    on_line: Callable[[str], None] | None = None,
    heartbeat: float | None = None,
) -> str:
    """Run ``cmd``, returning stdout; raise on a non-zero exit.

    When ``on_line`` is ``None`` this behaves exactly as before: the subprocess
    output is captured and only inspected on failure. When ``on_line`` is set,
    stdout/stderr are merged and streamed line-by-line to ``on_line``, with a
    ``still working …`` heartbeat emitted whenever no output arrives for
    ``heartbeat`` seconds (defaults to ``AUDIO_HEARTBEAT_SECONDS``).
    """
    if on_line is None:
        return _run_capture(cmd, timeout)
    return _run_stream(cmd, timeout, on_line, heartbeat or AUDIO_HEARTBEAT_SECONDS)


def _run_capture(cmd: list[str], timeout: float = AUDIO_TIMEOUT) -> str:
    """Capture the subprocess output (the original, non-streaming path)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{cmd[0]} not found — is it installed and on PATH?") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): " + " | ".join(tail))
    return proc.stdout


# The single child currently being streamed, so a signal handler in the CLI can
# reach in and terminate it. Cleared as soon as the child exits.
_ACTIVE_PROC: subprocess.Popen | None = None
_ACTIVE_LOCK = threading.Lock()


def _set_active(proc: subprocess.Popen) -> None:
    global _ACTIVE_PROC
    with _ACTIVE_LOCK:
        _ACTIVE_PROC = proc


def _clear_active(proc: subprocess.Popen) -> None:
    global _ACTIVE_PROC
    with _ACTIVE_LOCK:
        if _ACTIVE_PROC is proc:
            _ACTIVE_PROC = None


def terminate_active_child(grace: float = 5.0) -> None:
    """Terminate the currently streaming subprocess, if any (SIGTERM then SIGKILL).

    Called from the CLI's signal handler; a no-op when nothing is running.
    """
    with _ACTIVE_LOCK:
        proc = _ACTIVE_PROC
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _fmt_elapsed(seconds: float) -> str:
    """Format a number of seconds as ``1m 30s`` / ``45s`` / ``2h 3m 4s``."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _run_stream(
    cmd: list[str],
    timeout: float,
    on_line: Callable[[str], None],
    heartbeat: float,
) -> str:
    """Stream ``cmd``'s merged output to ``on_line``; keep a tail for errors."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{cmd[0]} not found — is it installed and on PATH?") from exc

    _set_active(proc)
    lines: deque[str] = deque(maxlen=10)
    buf = queue.Queue()

    def reader() -> None:
        # Read in binary mode and decode manually so carriage-return progress
        # bars (ffmpeg/mlx-whisper) survive intact — text mode would translate
        # ``\r`` to ``\n`` and erase the bar the caller may want to forward.
        try:
            for raw in proc.stdout:
                buf.put(raw.decode("utf-8", errors="replace"))
        finally:
            buf.put(None)  # sentinel: EOF

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    start = time.monotonic()
    interval = heartbeat if heartbeat > 0 else AUDIO_HEARTBEAT_SECONDS
    try:
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
            try:
                line = buf.get(timeout=min(interval, remaining))
            except queue.Empty:
                elapsed = time.monotonic() - start
                on_line(f"still working … (elapsed {_fmt_elapsed(elapsed)})\n")
                continue
            if line is None:
                break
            lines.append(line)
            on_line(line)
        thread.join()
        rc = proc.wait()
    finally:
        _clear_active(proc)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    if rc != 0:
        tail = [ln.strip() for ln in lines if ln.strip()][-10:]
        raise RuntimeError(f"{cmd[0]} failed ({rc}): " + " | ".join(tail))
    return "".join(lines)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``).

    A crash or interrupt can never leave a truncated ``path``; on any failure the
    temp file is removed and the exception re-raised.
    """
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _temp_sibling(path: Path) -> str:
    """Reserve a unique temp path beside ``path`` and return it.

    The returned path exists (empty) — ffmpeg's ``-y`` overwrites it in place.
    """
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    return tmp


def find_audio_for(source_path: Path) -> Path | None:
    """Return a same-stem audio file beside ``source_path``, or ``None``.

    Matches on the file stem, so it works for a Markdown file (``deck.md`` →
    ``deck.mp3``) as well as any other same-stem companion.
    """
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
    on_line: Callable[[str], None] | None = None,
    heartbeat: float | None = None,
) -> list[dict]:
    """Enhance ``audio_path``, persist it as a FLAC, and transcribe it.

    The cleaned audio is written to ``clean_path`` (16 kHz mono FLAC) — first by
    the deterministic ffmpeg chain, then upgraded in place by DeepFilterNet when
    ``AUDIO_ENHANCE_ENABLED`` and the server is up (a failure only warns).

    ``on_line`` (optional) receives raw subprocess output plus short phase lines
    as they happen; ``heartbeat`` overrides the quiet-interval before a
    ``still working …`` line.

    Returns ``[{start, end, text}, ...]`` (seconds, verbatim text). Raises on any
    fatal subprocess failure so callers can degrade gracefully.
    """
    stream_kw: dict = {}
    if on_line is not None:
        stream_kw["on_line"] = on_line
        stream_kw["heartbeat"] = heartbeat
    if on_line is not None:
        on_line("ffmpeg: cleaning audio …\n")

    tmp_clean = _temp_sibling(clean_path)
    try:
        ffmpeg_cmd = [
            ffmpeg_bin or AUDIO_FFMPEG_BIN,
            "-y", "-i", str(audio_path),
        ]
        if AUDIO_PREPROCESS:
            ffmpeg_cmd += ["-af", _ENHANCE_FILTER]
        ffmpeg_cmd += ["-ar", "16000", "-ac", "1", "-c:a", "flac", "-f", "flac", tmp_clean]
        _run(ffmpeg_cmd, timeout=timeout, **stream_kw)
        os.replace(tmp_clean, clean_path)
    except BaseException:
        try:
            os.unlink(tmp_clean)
        except OSError:
            pass
        raise

    if AUDIO_ENHANCE_ENABLED:
        if on_line is not None:
            on_line("enhancing audio …\n")
        try:
            enhance(str(clean_path), str(clean_path))
        except Exception as exc:  # noqa: BLE001 - degrade to preprocessed audio
            if warnings is not None:
                warnings.append(f"Audio enhancement failed: {exc}; using preprocessed audio")

    if on_line is not None:
        on_line(f"transcribing with {mlx_bin or AUDIO_MLX_WHISPER_BIN} …\n")

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
        _run(cmd, timeout=timeout, **stream_kw)
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


def _strip_transcript(md: str) -> str:
    """Remove any existing ``# Transcript`` section from ``md``.

    The section is always appended at the end of the file, so truncating at the
    heading line is safe and makes re-attachment idempotent.
    """
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "# Transcript":
            head = lines[:i]
            return ("\n".join(head).rstrip("\n") + "\n") if head else ""
    return md


def _transcribe(
    audio_path: Path,
    clean_path: Path,
    source: str,
    warnings: list[str],
    on_line: Callable[[str], None] | None = None,
    heartbeat: float | None = None,
) -> list[dict]:
    """Enhance, transcribe, and (optionally) label ``audio_path``.

    Persists the cleaned audio to ``clean_path``, records every segment to
    ``ptm.sqlite`` under ``source``, and returns the segment list. Diarization
    failure only warns; any fatal subprocess failure is raised to the caller.
    """
    segments = transcribe_audio(
        audio_path, clean_path, warnings=warnings, on_line=on_line, heartbeat=heartbeat
    )
    if not segments:
        return segments
    if AUDIO_DIARIZE_ENABLED:
        if on_line is not None:
            on_line("diarizing …\n")
        try:
            turns = diarize(str(clean_path))
            assign_speakers(segments, turns)
        except Exception as exc:  # noqa: BLE001 - degrade to unlabelled transcript
            warnings.append(f"Diarization failed: {exc}; keeping unlabelled transcript")

    for seg in segments:
        record_segment(
            source=source,
            start=seg["start"],
            end=seg["end"],
            text=seg["text"],
            speaker=seg.get("speaker"),
            model=AUDIO_MODEL,
        )
    return segments


def attach_transcript(
    md_path: Path,
    warnings: list[str],
    audio_path: str | Path | None = None,
    on_line: Callable[[str], None] | None = None,
    heartbeat: float | None = None,
) -> list[dict] | None:
    """Transcribe audio and attach it to ``md_path`` as a ``# Transcript`` section.

    Returns the attached segment list, or ``None`` when it is a no-op
    (``AUDIO_ENABLED`` off, no audio found, no segments, or a failure — which
    only warns). Never raises. The cleaned audio is persisted as
    ``<stem>.clean.flac`` and a ``<stem>.transcript.srt`` sidecar is written.
    Re-attaching is idempotent: any existing ``# Transcript`` section is replaced.
    """
    if not AUDIO_ENABLED:
        return None
    try:
        audio = Path(audio_path) if audio_path else find_audio_for(md_path)
        if audio is None or not audio.exists():
            return None
        clean_path = md_path.with_name(md_path.stem + ".clean.flac")
        segments = _transcribe(
            audio, clean_path, str(md_path), warnings, on_line=on_line, heartbeat=heartbeat
        )
        if not segments:
            return None

        md = md_path.read_text(encoding="utf-8")
        _atomic_write_text(
            md_path,
            _strip_transcript(md).rstrip("\n") + "\n\n" + segments_to_markdown(segments) + "\n",
        )

        srt_path = md_path.with_name(md_path.stem + ".transcript.srt")
        _atomic_write_text(srt_path, segments_to_srt(segments))
        return segments
    except Exception as exc:  # noqa: BLE001 - transcription never fails the caller
        warnings.append(f"Audio transcription failed: {exc}")
        return None


def transcribe_to_markdown(
    audio_path: str | Path,
    warnings: list[str] | None = None,
    on_line: Callable[[str], None] | None = None,
    heartbeat: float | None = None,
) -> Path | None:
    """Transcribe ``audio_path`` into a standalone ``<stem>.transcript.md``.

    For the case where no Markdown document exists yet. Writes the transcript
    Markdown plus the ``<stem>.clean.flac`` and ``<stem>.transcript.srt``
    sidecars (all named from the audio stem, so the names stay readable). Returns
    the transcript Markdown path, or ``None`` on a no-op/failure (which only
    warns). Never raises.
    """
    warnings = warnings if warnings is not None else []
    audio = Path(audio_path)
    if not audio.exists():
        warnings.append(f"Audio file not found: {audio}")
        return None
    try:
        md_path = audio.with_name(audio.stem + ".transcript.md")
        clean_path = audio.with_name(audio.stem + ".clean.flac")
        segments = _transcribe(
            audio, clean_path, str(md_path), warnings, on_line=on_line, heartbeat=heartbeat
        )
        if not segments:
            return None
        _atomic_write_text(md_path, segments_to_markdown(segments) + "\n")
        srt_path = audio.with_name(audio.stem + ".transcript.srt")
        _atomic_write_text(srt_path, segments_to_srt(segments))
        return md_path
    except Exception as exc:  # noqa: BLE001 - transcription never fails the caller
        warnings.append(f"Audio transcription failed: {exc}")
        return None
