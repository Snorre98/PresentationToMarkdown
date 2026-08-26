"""Single-instance guard for the ``ptm-transcribe`` command.

Ensures only one transcription runs at a time: concurrent ``mlx_whisper``
processes would thrash MLX unified memory and clobber each other's
``.clean.flac`` / ``.md`` / ``.srt`` sidecars. The lock is a ``flock``-based
exclusive lock on ``<PTM_STATE_DIR or ~/.local/state/ptm>/transcribe.lock``:

- ``flock`` is advisory and per-open-file-description, so a second process that
  re-opens the path blocks (here: fails fast) while the first holds it.
- The kernel releases the lock automatically when the holding process exits —
  even on a crash or SIGKILL — so the lock file is never deleted manually.
- The holder's PID is written into the file after acquisition, purely for the
  "another instance is already running (PID …)" message.

The state directory is read at *call* time (not import time) so callers can set
``PTM_STATE_DIR`` (e.g. via ``--env``) before acquiring. A missing or
un-writable state directory degrades gracefully: the command proceeds *without*
a lock rather than crashing.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


def _state_dir() -> Path:
    return Path(os.environ.get("PTM_STATE_DIR", Path.home() / ".local" / "state" / "ptm"))


class TranscribeLock:
    """A ``flock``-based exclusive lock with ``held`` / ``pid`` / ``release()``."""

    def __init__(self) -> None:
        self.path = _state_dir() / "transcribe.lock"
        self._fd: int | None = None
        self.held = False
        self.pid: int | None = None

    def acquire(self) -> bool:
        """Acquire the lock; return ``True`` on success, ``False`` when held.

        Sets ``held`` / ``pid`` accordingly. Idempotent — a second call while
        already held is a no-op. Never raises: a state directory that cannot be
        created/opened degrades to an unlocked (no-op) "held" lock so the
        command still runs.
        """
        if self._fd is not None:
            self.held = True
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            # Degrade gracefully — proceed unlocked rather than crash.
            self.held = True
            return True

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.pid = self._read_pid()
            os.close(fd)
            self.held = False
            return False
        except OSError:
            # Unusual flock failure (not contention) — degrade to unlocked.
            os.close(fd)
            self.held = True
            return True

        self._fd = fd
        self.held = True
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        self.pid = os.getpid()
        return True

    def release(self) -> None:
        """Release the lock. Idempotent; safe to call multiple times."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        self.held = False

    def _read_pid(self) -> int | None:
        try:
            raw = self.path.read_text(encoding="ascii").strip()
            return int(raw) if raw.isdigit() else None
        except (OSError, ValueError):
            return None

    def __enter__(self) -> "TranscribeLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


def acquire_transcribe_lock() -> TranscribeLock:
    """Create and attempt to acquire the transcription lock.

    Inspect ``.held`` (and ``.pid``) on the returned object; the CLI exits fast
    when ``held`` is ``False``. Call ``.release()`` (or use the context manager)
    to drop it on normal exit.
    """
    lock = TranscribeLock()
    lock.acquire()
    return lock
