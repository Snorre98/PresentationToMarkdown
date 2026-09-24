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
  ``mlx-community/whisper-large-v3-mlx`` (max quality; override to
  ``…-large-v3-turbo`` for speed). Set to ``nb-whisper-large`` to route ASR
  through the audio server's NB-Whisper route instead (``/v1/asr``, ADR-0044);
  if that route fails, transcription falls back to ``mlx_whisper``.
- ``AUDIO_MLX_WHISPER_BIN`` — mlx-whisper CLI, default ``mlx_whisper``.
- ``AUDIO_FFMPEG_BIN`` — ffmpeg binary, default ``ffmpeg``.
- ``AUDIO_LANGUAGE`` — Whisper language hint (default ``no``; set to ``auto`` /
  empty for auto-detection).
- ``AUDIO_TIMEOUT`` — per-file subprocess timeout in seconds, default ``3600``.
- ``AUDIO_HEARTBEAT_SECONDS`` — quiet-interval before a ``still working …`` line
  is emitted while streaming subprocess output, default ``20``.
- ``AUDIO_PREPROCESS`` — deterministic ffmpeg enhancement chain. Default on.
- ``AUDIO_DEREVERB_ENABLED`` — WPE dereverberation (via the audio server). Default on.
- ``AUDIO_ISOLATE_ENABLED`` — voice isolation (SepFormer, via the audio server). Default off.
- ``AUDIO_DIARIZE_ENABLED`` — speaker labelling (via the audio server). Default off.
- ``AUDIO_DIARIZE_SPEAKERS`` / ``AUDIO_DIARIZE_MIN_SPEAKERS`` /
  ``AUDIO_DIARIZE_MAX_SPEAKERS`` — exact/range speaker count for pyannote
  (see ``converter.audio``). Any of them implies diarization.
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
    AUDIO_DEREVERB_ENABLED,
    AUDIO_ENHANCE_ENABLED,
    AUDIO_ISOLATE_ENABLED,
    asr,
    assign_speakers,
    assign_speakers_by_words,
    dereverb,
    diarize,
    diarize_bounds,
    diarize_requested,
    enhance,
    isolate,
    vad,
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
DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-mlx"

# When ``AUDIO_MODEL`` is this sentinel, transcription runs on the audio server's
# NB-Whisper route (``/v1/asr``, ADR-0044) instead of the ``mlx_whisper``
# subprocess. The server owns the actual HF model id; ``converter`` only knows
# the routing sentinel.
AUDIO_ASR_SERVER_SENTINEL = "nb-whisper-large"

AUDIO_MODEL = os.environ.get("AUDIO_MODEL", DEFAULT_MLX_MODEL)
AUDIO_MLX_WHISPER_BIN = os.environ.get("AUDIO_MLX_WHISPER_BIN", "mlx_whisper")
AUDIO_FFMPEG_BIN = os.environ.get("AUDIO_FFMPEG_BIN", "ffmpeg")
AUDIO_FFPROBE_BIN = os.environ.get("AUDIO_FFPROBE_BIN", "ffprobe")
AUDIO_LANGUAGE = os.environ.get("AUDIO_LANGUAGE", "no").strip().lower()
AUDIO_LANGUAGE = None if not AUDIO_LANGUAGE or AUDIO_LANGUAGE == "auto" else AUDIO_LANGUAGE
AUDIO_TIMEOUT = float(os.environ.get("AUDIO_TIMEOUT", "3600"))
AUDIO_HEARTBEAT_SECONDS = float(os.environ.get("AUDIO_HEARTBEAT_SECONDS", "20"))

# Coverage + fallback re-decode (ADR-0045). ``AUDIO_COVERAGE_ENABLED`` runs a VAD
# round-trip after transcription and warns about speech spans with no words —
# the same check that would have caught the 21-sep block pass. With
# ``AUDIO_FALLBACK_REDECODE`` those windows are re-decoded with beam search.
AUDIO_COVERAGE_ENABLED = os.environ.get("AUDIO_COVERAGE_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_FALLBACK_REDECODE = os.environ.get("AUDIO_FALLBACK_REDECODE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_REDECODE_BEAMS = int(os.environ.get("AUDIO_REDECODE_BEAMS", "5"))

# Interview-guide alignment (ADR-0045): with ``AUDIO_GUIDE_ENABLED`` and
# ``AUDIO_GUIDE_PATH`` set, the guide's questions are aligned to the transcript
# and used to re-attribute interviewer/participant roles and insert any question
# the ASR dropped (see ``converter.guide``).
AUDIO_GUIDE_ENABLED = os.environ.get("AUDIO_GUIDE_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIO_GUIDE_PATH = os.environ.get("AUDIO_GUIDE_PATH", "")

# mlx-whisper's default (``--condition-on-previous-text True``) feeds the previous
# window's text back as a prompt, which triggers the classic repetition-loop
# hallucination ("log log log …") on long recordings. We disable it by default;
# set ``AUDIO_CONDITION_ON_PREVIOUS_TEXT=1`` to re-enable it for smoother
# cross-window continuity at the cost of possible loops.
AUDIO_CONDITION_ON_PREVIOUS_TEXT = os.environ.get(
    "AUDIO_CONDITION_ON_PREVIOUS_TEXT", "0"
).strip().lower() in {"1", "true", "yes", "on"}

# Deterministic speech-enhancement chain for lecture-hall audio (ADR-0008).
# Kept deliberately gentle: only DC/rumble removal + band-limiting. The old
# chain (afftdn + loudnorm) over-suppressed already-clean recordings, cutting
# ~15 dB of level and pushing Whisper into repetitive-hallucination loops
# ("No? No? …" / "she's a queen …"). Denoise/dereverb are the server's job
# (DeepFilterNet / WPE) and are also available via `AUDIO_ENHANCE_ENABLED`.
_ENHANCE_FILTER = "highpass=f=80,lowpass=f=8000"

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
    return (proc.stdout or "") + (proc.stderr or "")


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


def _paired_version(md_base: Path, clean_base: Path) -> int:
    """Return the first ``N >= 0`` where *neither* transcript ``N`` nor clean ``N`` exists.

    Version numbers are shared across the transcript (``.md``/``.srt``) and the
    cleaned audio (``.clean.flac``) so every run's three artifacts stay paired:
    ``a.transcript.md`` ↔ ``a.clean.flac``, ``a.transcript.1.md`` ↔ ``a.clean.1.flac``,
    … Checking both files means a failed run can never leave a gap that would let
    a later run overwrite a previous clean file.
    """
    n = 0
    while True:
        md = md_base if n == 0 else md_base.with_name(f"{md_base.stem}.{n}{md_base.suffix}")
        clean = clean_base if n == 0 else clean_base.with_name(f"{clean_base.stem}.{n}{clean_base.suffix}")
        if not md.exists() and not clean.exists():
            return n
        n += 1


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


def _audio_duration(path: Path) -> float | None:
    """Return ``path``'s duration in seconds via ffprobe, or ``None`` on failure.

    ``ffprobe`` ships with ``ffmpeg``; if it's missing this degrades to ``None``
    (callers treat it as "cannot validate").
    """
    try:
        proc = subprocess.run(
            [
                AUDIO_FFPROBE_BIN,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _enhance_audio(clean_path: Path, warnings: list[str] | None) -> None:
    """Enhance ``clean_path`` via DeepFilterNet, validating the result.

    The enhanced audio is written to a temp ``.flac`` beside ``clean_path`` and
    only swapped in if its duration matches the source (within 5%); otherwise the
    temp is discarded and a warning appended. A misbehaving/buggy server therefore
    can never corrupt the cleaned file.
    """
    src_duration = _audio_duration(clean_path)
    fd, enhanced_tmp = tempfile.mkstemp(
        prefix=clean_path.stem + ".enhanced.", suffix=".flac", dir=clean_path.parent
    )
    os.close(fd)
    try:
        enhance(str(clean_path.resolve()), enhanced_tmp)
        out_duration = _audio_duration(Path(enhanced_tmp))
        if (
            src_duration is not None
            and out_duration is not None
            and src_duration > 0
            and abs(out_duration - src_duration) / src_duration >= 0.05
        ):
            if warnings is not None:
                warnings.append(
                    "Audio enhancement produced a bad-length file "
                    f"({out_duration:.1f}s vs {src_duration:.1f}s); using preprocessed audio"
                )
            return
        os.replace(enhanced_tmp, clean_path)
    finally:
        try:
            os.unlink(enhanced_tmp)
        except OSError:
            pass


def _dereverb_audio(clean_path: Path, warnings: list[str] | None) -> None:
    """Dereverberate ``clean_path`` in place (WPE), validating the result.

    Mirrors :func:`_enhance_audio`: the dereverberated audio is written to a temp
    ``.flac`` and only swapped in if its duration matches the source (within 5%).
    """
    src_duration = _audio_duration(clean_path)
    fd, tmp = tempfile.mkstemp(
        prefix=clean_path.stem + ".dereverb.", suffix=".flac", dir=clean_path.parent
    )
    os.close(fd)
    try:
        dereverb(str(clean_path.resolve()), tmp)
        out_duration = _audio_duration(Path(tmp))
        if (
            src_duration is not None
            and out_duration is not None
            and src_duration > 0
            and abs(out_duration - src_duration) / src_duration >= 0.05
        ):
            if warnings is not None:
                warnings.append(
                    "Audio dereverberation produced a bad-length file "
                    f"({out_duration:.1f}s vs {src_duration:.1f}s); using reverberant audio"
                )
            return
        os.replace(tmp, clean_path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _isolate_audio(
    clean_path: Path, isolated_path: Path, warnings: list[str] | None
) -> bool:
    """Isolate the dominant voice into ``isolated_path``; return success.

    Mirrors :func:`_enhance_audio`'s temp + duration-validation dance, but writes
    to a *separate* ``isolated_path`` rather than replacing ``clean_path``. On a
    bad-length result (or failure) the temp is discarded, a warning is appended
    and ``False`` is returned so the caller falls back to the unisolated audio.
    """
    src_duration = _audio_duration(clean_path)
    fd, tmp = tempfile.mkstemp(
        prefix=isolated_path.stem + ".", suffix=".flac", dir=isolated_path.parent
    )
    os.close(fd)
    try:
        isolate(str(clean_path.resolve()), tmp)
        out_duration = _audio_duration(Path(tmp))
        if (
            src_duration is not None
            and out_duration is not None
            and src_duration > 0
            and abs(out_duration - src_duration) / src_duration >= 0.05
        ):
            if warnings is not None:
                warnings.append(
                    "Voice isolation produced a bad-length file "
                    f"({out_duration:.1f}s vs {src_duration:.1f}s); using unisolated audio"
                )
            return False
        os.replace(tmp, isolated_path)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


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
    return_words: bool = False,
) -> list[dict]:
    """Enhance ``audio_path``, persist it as a FLAC, and transcribe it.

    The cleaned audio is written to ``clean_path`` (16 kHz mono FLAC) — first by
    the deterministic ffmpeg chain, then dereverberated (WPE) and upgraded by
    DeepFilterNet when those steps are enabled and the server is up. With
    ``AUDIO_ISOLATE_ENABLED``, a voice-isolated ``<stem>.isolated.flac`` is also
    produced and that is what Whisper transcribes (each failure only warns).

    ``return_words`` requests per-word timestamps from the *server* ASR lane
    (ADR-0045); it is ignored on the mlx-whisper fallback, which always yields
    segments. The returned list is ``[{start, end, text}, ...]`` either way.

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

    if AUDIO_DEREVERB_ENABLED:
        if on_line is not None:
            on_line("dereverberating audio …\n")
        try:
            _dereverb_audio(clean_path, warnings)
        except Exception as exc:  # noqa: BLE001 - degrade to reverberant audio
            if warnings is not None:
                warnings.append(f"Audio dereverberation failed: {exc}; using reverberant audio")

    if AUDIO_ENHANCE_ENABLED:
        if on_line is not None:
            on_line("enhancing audio …\n")
        try:
            _enhance_audio(clean_path, warnings)
        except Exception as exc:  # noqa: BLE001 - degrade to preprocessed audio
            if warnings is not None:
                warnings.append(f"Audio enhancement failed: {exc}; using preprocessed audio")

    target = clean_path
    if AUDIO_ISOLATE_ENABLED:
        if on_line is not None:
            on_line("isolating voice …\n")
        isolated_path = clean_path.with_name(clean_path.name.replace(".clean.", ".isolated."))
        try:
            if _isolate_audio(clean_path, isolated_path, warnings):
                target = isolated_path
        except Exception as exc:  # noqa: BLE001 - degrade to unisolated audio
            if warnings is not None:
                warnings.append(f"Voice isolation failed: {exc}; using unisolated audio")

    use_server_asr = AUDIO_MODEL == AUDIO_ASR_SERVER_SENTINEL

    if use_server_asr:
        if on_line is not None:
            on_line(f"transcribing with {AUDIO_MODEL} (audio server) …\n")
        try:
            lang = language or AUDIO_LANGUAGE or "no"
            # Resolve to an absolute path: the audio server runs from a
            # different cwd and cannot open a relative ``target``.
            return asr(str(target.resolve()), language=lang, timeout=timeout, return_words=return_words)
        except Exception as exc:  # noqa: BLE001 - degrade to the mlx-whisper fallback
            if warnings is not None:
                warnings.append(
                    f"Server ASR ({AUDIO_MODEL}) failed: {exc}; falling back to mlx-whisper"
                )

    if on_line is not None:
        on_line(f"transcribing with {mlx_bin or AUDIO_MLX_WHISPER_BIN} …\n")

    with tempfile.TemporaryDirectory(prefix="ptm-audio-") as tmp:
        tmpdir = Path(tmp)
        cmd = [
            mlx_bin or AUDIO_MLX_WHISPER_BIN,
            str(target),
            "--model", (model or AUDIO_MODEL) if not use_server_asr else DEFAULT_MLX_MODEL,
            "--output-format", "json",
            "--output-dir", str(tmpdir),
        ]
        if not AUDIO_CONDITION_ON_PREVIOUS_TEXT:
            cmd += ["--condition-on-previous-text", "False"]
        lang = language or AUDIO_LANGUAGE
        if lang:
            cmd += ["--language", lang]
        whisper_out = _run(cmd, timeout=timeout, **stream_kw)
        # mlx-whisper's writer does `Path(output_name).with_suffix(".json")`, which
        # re-interprets dots in the stem (``week-2.clean.flac`` -> ``week-2.json``),
        # so we can't predict the exact name — glob the (fresh, single-file) dir.
        json_files = sorted(tmpdir.glob("*.json"))
        if not json_files:
            # mlx-whisper exits 0 even when transcription fails (it prints
            # "Skipping <audio> due to <exc>" + a traceback and moves on), so a
            # missing JSON is the only reliable signal — surface its output.
            tail = [ln.strip() for ln in (whisper_out or "").splitlines() if ln.strip()][-15:]
            detail = "\n  ".join(tail) if tail else "(no output captured)"
            raise RuntimeError(
                f"{AUDIO_MLX_WHISPER_BIN} produced no output for {target.name} "
                f"(it likely failed and exited 0):\n  {detail}"
            )
        data = json.loads(json_files[0].read_text(encoding="utf-8"))

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


# Gap (seconds) between consecutive words above which a new utterance starts
# (ADR-0045): speaker changes and pauses longer than this split an utterance.
PAUSE_GAP_SECONDS = 0.7


def words_to_utterances(
    words: list[dict], pause_gap: float = PAUSE_GAP_SECONDS
) -> list[dict]:
    """Group speaker-labelled words into utterances at speaker and pause boundaries.

    ``words`` are ``[{start, end, text, speaker}]`` (see
    :func:`converter.audio.assign_speakers_by_words`). A new utterance starts
    when the speaker changes or the inter-word gap exceeds ``pause_gap``; each
    utterance keeps the first word's ``start``, the last word's ``end``, and its
    speaker, with texts joined by single spaces. Empty words are dropped.
    """
    utterances: list[dict] = []
    current: list[dict] = []
    for word in words:
        text = (word.get("text") or "").strip()
        if not text:
            continue
        if current:
            prev = current[-1]
            if (
                word.get("speaker") != prev.get("speaker")
                or (word["start"] - prev["end"]) > pause_gap
            ):
                utterances.append(_join_words(current))
                current = []
        current.append(word)
    if current:
        utterances.append(_join_words(current))
    return utterances


def _join_words(words: list[dict]) -> dict:
    return {
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "text": " ".join((w.get("text") or "").strip() for w in words).strip(),
        "speaker": words[0].get("speaker"),
    }


def merge_utterances(
    segments: list[dict], pause_gap: float = PAUSE_GAP_SECONDS
) -> list[dict]:
    """Merge consecutive same-speaker ASR segments into continuous utterances.

    A two-person interview then reads as alternating ``**SPEAKER_00:**`` blocks
    instead of many fragmented ~5 s lines. A segment without a speaker, a speaker
    change, or an inter-segment pause longer than ``pause_gap`` starts a new
    entry; the merged utterance keeps the first segment's ``start`` and the last
    segment's ``end``, with texts joined by a single space. ``segments`` is not
    mutated.
    """
    merged: list[dict] = []
    for seg in segments:
        text = seg.get("text", "").strip()
        if not text:
            continue
        speaker = seg.get("speaker")
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and speaker is not None
            and prev.get("speaker") == speaker
            and seg["start"] >= prev["end"]
            and (seg["start"] - prev["end"]) <= pause_gap
        ):
            prev["text"] = prev["text"] + " " + text
            prev["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged


def segments_to_markdown(segments: list[dict], model: str | None = None) -> str:
    """Render timestamped segments as a Markdown ``# Transcript`` section."""
    lines = ["# Transcript", "", "<details>", f"<summary>Auto-generated transcript ({model or AUDIO_MODEL})</summary>", ""]
    for seg in merge_utterances(segments):
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
    for i, seg in enumerate(merge_utterances(segments), start=1):
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


def detect_low_coverage(
    segments: list[dict],
    vad_regions: list[dict],
    min_overlap: float = 0.3,
) -> list[dict]:
    """Return VAD speech spans with too little ASR word coverage.

    ``segments`` are ASR ``[{start, end, ...}]`` and ``vad_regions`` are
    ``[{start, end}]`` speech regions. A region whose span is less than
    ``min_overlap``-covered by ASR timestamps is returned as
    ``{start, end, covered_fraction}`` — exactly the signature of the 21-sep
    block pass, which dropped whole answer windows while VAD still saw speech.
    """
    gaps: list[dict] = []
    for region in vad_regions:
        total = region["end"] - region["start"]
        if total <= 0:
            continue
        covered = 0.0
        for seg in segments:
            covered += max(
                0.0,
                min(seg["end"], region["end"]) - max(seg["start"], region["start"]),
            )
        fraction = min(1.0, covered / total)
        if fraction < min_overlap:
            gaps.append(
                {
                    "start": region["start"],
                    "end": region["end"],
                    "covered_fraction": fraction,
                }
            )
    return gaps


def redecode_windows(
    clean_path: Path,
    windows: list[dict],
    language: str | None = None,
    warnings: list[str] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> list[dict]:
    """Re-decode low-coverage windows with beam search; return replacement segments.

    Each window is sliced from ``clean_path`` with ffmpeg and re-POSTed to the
    server ASR with ``AUDIO_REDECODE_BEAMS``; returned segments are shifted back
    into the original timeline. Best-effort: a failed window only appends a
    warning and yields nothing for that window.
    """
    warnings = warnings if warnings is not None else []
    out: list[dict] = []
    for window in windows:
        w_start = window["start"]
        w_end = window["end"]
        if on_line is not None:
            on_line(f"re-decoding {w_start:.1f}-{w_end:.1f}s with beam search …\n")
        tmp = _temp_sibling(clean_path)
        try:
            _run(
                [
                    AUDIO_FFMPEG_BIN,
                    "-y",
                    "-ss",
                    f"{w_start:.3f}",
                    "-to",
                    f"{w_end:.3f}",
                    "-i",
                    str(clean_path),
                    "-c:a",
                    "flac",
                    "-f",
                    "flac",
                    tmp,
                ],
                timeout=AUDIO_TIMEOUT,
            )
            segs = asr(
                str(Path(tmp).resolve()),
                language=language or AUDIO_LANGUAGE or "no",
                num_beams=AUDIO_REDECODE_BEAMS,
            )
            for seg in segs:
                seg["start"] += w_start
                seg["end"] += w_start
            out.extend(segs)
        except Exception as exc:  # noqa: BLE001 - best-effort window re-decode
            warnings.append(
                f"Fallback re-decode failed for {w_start:.1f}-{w_end:.1f}s: {exc}"
            )
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return out


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

    When diarization is requested and the server ASR lane is in use, per-word
    timestamps are requested so speakers are assigned per word and utterances
    re-derived at speaker/pause boundaries (ADR-0045); otherwise the coarser
    segment-level overlap assignment runs.
    """
    want_words = diarize_requested() and AUDIO_MODEL == AUDIO_ASR_SERVER_SENTINEL
    segments = transcribe_audio(
        audio_path,
        clean_path,
        warnings=warnings,
        on_line=on_line,
        heartbeat=heartbeat,
        return_words=want_words,
    )
    if not segments:
        return segments
    if diarize_requested():
        if on_line is not None:
            on_line("diarizing …\n")
        try:
            turns = diarize(str(clean_path.resolve()), **diarize_bounds())
            turns = _ensure_speaker_count(turns, warnings)
            if want_words:
                assign_speakers_by_words(segments, turns)
                segments = words_to_utterances(segments)
            else:
                assign_speakers(segments, turns)
        except Exception as exc:  # noqa: BLE001 - degrade to unlabelled transcript
            warnings.append(f"Diarization failed: {exc}; keeping unlabelled transcript")

    if AUDIO_COVERAGE_ENABLED:
        _run_coverage_check(clean_path, segments, warnings, on_line)

    if AUDIO_GUIDE_ENABLED and AUDIO_GUIDE_PATH:
        _apply_guide(segments, warnings, on_line)

    _apply_edit(segments, warnings, on_line)

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


def _ensure_speaker_count(turns: list[dict], warnings: list[str]) -> list[dict]:
    """Split a collapsed single-cluster diarization back into ``min_speakers``.

    pyannote can return fewer clusters than requested (22-sep collapsed to one
    despite ``min=max=2``). When that happens we warn, split the cluster at its
    turn/pause boundaries into ``min_speakers`` alternating labels, and return
    the corrected turns. A healthy result (enough distinct labels) passes through
    unchanged.
    """
    bounds = diarize_bounds()
    min_speakers = bounds.get("min_speakers")
    if min_speakers is None or not turns:
        return turns
    distinct = {turn.get("speaker") for turn in turns}
    if len(distinct) >= min_speakers:
        return turns
    warnings.append(
        f"Diarization found {len(distinct)} speaker(s), fewer than the requested "
        f"{min_speakers}; splitting the cluster by pause"
    )
    turns = sorted(turns, key=lambda t: t["start"])
    while len(turns) < min_speakers:
        longest = max(turns, key=lambda t: t["end"] - t["start"])
        mid = (longest["start"] + longest["end"]) / 2.0
        turns.remove(longest)
        turns.append({"start": longest["start"], "end": mid, "speaker": longest["speaker"]})
        turns.append({"start": mid, "end": longest["end"], "speaker": longest["speaker"]})
        turns.sort(key=lambda t: t["start"])
    for i, turn in enumerate(turns):
        turn["speaker"] = f"SPEAKER_{i % min_speakers:02d}"
    return turns


def _run_coverage_check(
    clean_path: Path,
    segments: list[dict],
    warnings: list[str],
    on_line: Callable[[str], None] | None,
) -> None:
    """VAD-gate the transcript: warn about low-coverage speech, optionally re-decode.

    Runs a VAD round-trip (``converter.audio.vad``), diffs speech regions against
    ASR segment coverage, and warns for any speech span with too few words. When
    ``AUDIO_FALLBACK_REDECODE`` is on, those windows are re-decoded with beam
    search and their segments spliced (in place) into ``segments``. Never raises;
    any failure only warns.
    """
    if on_line is not None:
        on_line("checking coverage …\n")
    try:
        regions = vad(str(clean_path.resolve()))
        gaps = detect_low_coverage(segments, regions)
    except Exception as exc:  # noqa: BLE001 - coverage is best-effort
        warnings.append(f"Coverage check failed: {exc}")
        return
    for gap in gaps:
        warnings.append(
            f"Low ASR coverage {gap['start']:.1f}-{gap['end']:.1f}s "
            f"({gap['covered_fraction']:.0%} covered)"
        )
    if gaps and AUDIO_FALLBACK_REDECODE:
        extra = redecode_windows(clean_path, gaps, warnings=warnings, on_line=on_line)
        if extra:
            segments.extend(extra)
            segments.sort(key=lambda s: s["start"])


def _apply_guide(
    segments: list[dict],
    warnings: list[str],
    on_line: Callable[[str], None] | None,
) -> None:
    """Re-attribute segments by the interview guide (opt-in, in place via rebind).

    Loads the guide at ``AUDIO_GUIDE_PATH``, aligns its questions to the
    transcript, and replaces ``segments``' contents with the interviewer/
    participant re-attributed list (missing questions inserted). Any failure only
    warns and leaves the transcript unchanged.
    """
    try:
        from converter.guide import load_guide, apply_guide

        questions = load_guide(AUDIO_GUIDE_PATH)
        if not questions:
            return
        if on_line is not None:
            on_line("aligning interview guide …\n")
        re_attributed = apply_guide(segments, questions)
        segments[:] = re_attributed
    except Exception as exc:  # noqa: BLE001 - guide pass degrades to unlabelled
        warnings.append(f"Guide alignment failed: {exc}; keeping diarized labels")


def _apply_edit(
    segments: list[dict],
    warnings: list[str],
    on_line: Callable[[str], None] | None,
) -> None:
    """Apply the opt-in LLM transcript edit (word-preserving, in place via rebind).

    No-op unless ``TRANSCRIPT_EDIT_ENABLED`` is set (the pass is gated off by
    default because the local text model is weak at Norwegian). Any failure only
    warns and leaves the transcript verbatim.
    """
    try:
        from converter.transcript_edit import TRANSCRIPT_EDIT_ENABLED, edit_transcript

        if not TRANSCRIPT_EDIT_ENABLED:
            return
        if on_line is not None:
            on_line("editing transcript …\n")
        segments[:] = edit_transcript(segments)
    except Exception as exc:  # noqa: BLE001 - edit degrades to verbatim
        warnings.append(f"Transcript edit failed: {exc}; keeping verbatim")


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
    overwrite: bool = False,
) -> Path | None:
    """Transcribe ``audio_path`` into a standalone ``<stem>.transcript.md``.

    For the case where no Markdown document exists yet. Writes the transcript
    Markdown plus the ``<stem>.clean.flac`` and ``<stem>.transcript.srt``
    sidecars. Returns the transcript Markdown path, or ``None`` on a no-op/failure
    (which only warns). Never raises.

    By default the transcript is **append-only**: when ``<stem>.transcript.md``
    already exists, the new one is written as ``<stem>.transcript.<N>.md`` (with a
    matching ``.srt`` **and** ``.clean.<N>.flac``, sharing the same number) so prior
    transcripts *and* cleaned audio are preserved for comparison. Pass
    ``overwrite=True`` to replace the base (un-numbered) files instead.
    """
    warnings = warnings if warnings is not None else []
    audio = Path(audio_path)
    if not audio.exists():
        warnings.append(f"Audio file not found: {audio}")
        return None
    try:
        base_md = audio.with_name(audio.stem + ".transcript.md")
        base_clean = audio.with_name(audio.stem + ".clean.flac")
        n = 0 if overwrite else _paired_version(base_md, base_clean)
        md_path = base_md if n == 0 else base_md.with_name(f"{base_md.stem}.{n}{base_md.suffix}")
        clean_path = base_clean if n == 0 else base_clean.with_name(f"{base_clean.stem}.{n}{base_clean.suffix}")
        segments = _transcribe(
            audio, clean_path, str(md_path), warnings, on_line=on_line, heartbeat=heartbeat
        )
        if not segments:
            return None
        _atomic_write_text(md_path, segments_to_markdown(segments) + "\n")
        srt_path = md_path.with_suffix(".srt")
        _atomic_write_text(srt_path, segments_to_srt(segments))
        return md_path
    except Exception as exc:  # noqa: BLE001 - transcription never fails the caller
        warnings.append(f"Audio transcription failed: {exc}")
        return None
