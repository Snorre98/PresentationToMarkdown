"""SQLite-backed logging for the vision pass and conversion runs.

Records every classifier decision and transcription — which source file, page or
image, how long the call took, and the outcome — so the vision pipeline is easy
to inspect. This is also the DB that holds app configuration.

Since ADR-0026 this module is a thin façade: the actual persistence is handled by
the SQLAlchemy ORM in :mod:`converter.db` (``repos`` + ``models`` + ``engine``).
This module retains the public callable API, the ``contextvars`` run-tagging, and
the ``_lock``/``VISION_LOG_ENABLED`` semantics so every existing call site
(``from converter.logstore import record``, etc.) is unaffected.

Configuration (environment variables):

- ``VISION_LOG_ENABLED`` — master switch (default on). ``1``/``true``/``yes``/``on``.
- ``VISION_LOG_DB`` — path to the SQLite file, default ``ptm.sqlite`` in the
  current working directory (the project dir for now).
"""
from __future__ import annotations

import contextvars
import os
import threading
import time
from contextlib import contextmanager

from converter.db import repos

VISION_LOG_ENABLED = os.environ.get("VISION_LOG_ENABLED", "on").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VISION_LOG_DB = os.environ.get("VISION_LOG_DB", "ptm.sqlite")

_SCHEMA_VERSION = 2

_lock = threading.Lock()

_current_run: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "ptm_current_run", default=None
)


def current_run_id() -> int | None:
    """The id of the conversion run active in this context, or ``None``."""
    return _current_run.get()


def record(
    *,
    source: str,
    stage: str,
    page: int | None = None,
    image_ref: str | None = None,
    image_digest: str | None = None,
    model: str | None = None,
    decision: str | None = None,
    raw_answer: str | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    generated_tokens: int | None = None,
    markdown: str | None = None,
    omitted_words: list[str] | None = None,
    error: str | None = None,
    base_url: str | None = None,
) -> None:
    """Insert a vision event. No-op when disabled; never raises."""
    if not VISION_LOG_ENABLED:
        return
    try:
        with _lock:
            repos.record_vision_event(
                source=source,
                stage=stage,
                run_id=_current_run.get(),
                page=page,
                image_ref=image_ref,
                image_digest=image_digest,
                model=model,
                decision=decision,
                raw_answer=raw_answer,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                generated_tokens=generated_tokens,
                markdown=markdown,
                omitted_words=omitted_words,
                error=error,
                base_url=base_url,
            )
    except Exception:
        pass


def record_segment(
    *,
    source: str,
    start: float,
    end: float,
    text: str,
    speaker: str | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    """Insert one transcript segment. No-op when disabled; never raises."""
    if not VISION_LOG_ENABLED:
        return
    try:
        with _lock:
            repos.record_transcript_segment(
                source=source,
                start=start,
                end=end,
                text=text,
                speaker=speaker,
                model=model,
                error=error,
            )
    except Exception:
        pass


def run_start(source: str, name: str | None = None) -> int | None:
    """Begin a conversion run, returning its id (or ``None`` when disabled/failed).

    Sets the current run in a context variable so every subsequent
    :func:`record` in this thread is tagged with ``run_id``.
    """
    if not VISION_LOG_ENABLED:
        return None
    try:
        with _lock:
            run_id = repos.start_run(source, name)
        _current_run.set(run_id)
        return run_id
    except Exception:
        return None


def run_finish(run_id: int | None, status: str = "ok", error: str | None = None) -> None:
    """Mark a run finished, recording its status and wall-clock duration."""
    if run_id is None:
        return
    try:
        with _lock:
            repos.finish_run(run_id, status)
    except Exception:
        pass
    finally:
        _current_run.set(None)


def run_phase_begin(
    run_id: int | None, phase: str, ordinal: int, detail: dict | None = None
) -> int | None:
    """Start a phase within ``run_id``, returning its row id (or ``None``)."""
    if not VISION_LOG_ENABLED or run_id is None:
        return None
    try:
        with _lock:
            return repos.begin_phase(run_id, phase, ordinal, detail)
    except Exception:
        return None


def run_phase_end(
    phase_id: int | None,
    status: str = "done",
    duration_ms: int | None = None,
    detail: dict | None = None,
) -> None:
    """Finish a phase, recording status, end time and duration."""
    if phase_id is None:
        return
    try:
        with _lock:
            repos.end_phase(phase_id, status, duration_ms, detail)
    except Exception:
        pass


@contextmanager
def phase(run_id: int | None, name: str, ordinal: int, detail: dict | None = None):
    """Context manager: record one run phase, marking it ``done``/``failed``.

    Re-raises any exception from the wrapped block, so a telemetry failure or a
    failing phase can never change conversion behaviour.
    """
    phase_id = run_phase_begin(run_id, name, ordinal, detail)
    t0 = time.perf_counter()
    try:
        yield phase_id
        run_phase_end(
            phase_id, "done", duration_ms=int((time.perf_counter() - t0) * 1000)
        )
    except Exception:
        run_phase_end(
            phase_id, "failed", duration_ms=int((time.perf_counter() - t0) * 1000)
        )
        raise


def run_snapshot(run_id: int | None, snapshot: dict) -> None:
    """Store a per-run configuration snapshot. Never raises."""
    if run_id is None:
        return
    try:
        with _lock:
            repos.snapshot_run(run_id, snapshot)
    except Exception:
        pass
