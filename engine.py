"""Native engine process for the PTM TUI (ADR-0025/0040).

The native Go TUI is a thin presentation client; this process owns everything
a native client cannot do alone:

- running conversions (``convert_files`` on a worker thread),
- engine-hosted audio transcription (ADR-0045),
- filesystem access (directory listing, path resolution, recursive glob, and a
  native "open in Finder"),
- the AI server preflight (``config.probe`` / ``config.missing_servers``), and
- app settings persistence (``converter.settings``).

It is the **sole writer** to ``ptm.sqlite`` (WAL): the TUI stays a pure client
while run telemetry is written here via ``converter.logstore``.

It applies environment variables *before* importing ``converter`` so the
import-time configuration (ADR-0002/ADR-0012) is correct.

Concurrency model: **one conversion at a time**. ``converter.config``'s mutable
feature state and ``converter.logstore``'s connection are process-global, so a
second job is rejected while one is running.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request
from flask_sock import Sock

DEFAULT_HOST = "127.0.0.1"
# 9091: the manifest reserves 808x/809x for model daemons (nomic-embed owns 8090),
# so the engine binds in the 909x range — ADR-0035.
DEFAULT_PORT = 9091

_SUPPORTED_EXTENSIONS = {".pptx", ".pdf", ".tex"}

# Uploaded files are staged under the state dir (ADR-0027) and pruned after
# this long. Sizes are enforced via Flask's MAX_CONTENT_LENGTH on the app.
UPLOAD_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

# Finished jobs kept for late-joining /api/status readers (ADR-0053). The
# engine never advertises a finished job as ``current_job``: clients key their
# own UI on this history by job id instead.
_JOBS_HISTORY_MAX = 20
_JOB_LOG_TAIL_MAX = 20

_engine_state = {
    "status": "idle",  # idle | running | done | error | cancelled
    "paths": [],
    "output_dir": None,
    "duplicate": False,
    "results": None,
    "log": [],
    "current_job": None,  # structured running job (ADR-0045/0053)
    "jobs": [],  # finished jobs, newest first (ADR-0053)
    "job_seq": 0,  # monotonic job id source (ADR-0053)
}

_job_lock = threading.Lock()
_job_running = threading.Event()

# --- job frames: initiator + watchers (ADR-0053) ------------------------------

_watch_lock = threading.Lock()
_send_lock = threading.Lock()
_watchers: set = set()
_job_cancel = threading.Event()


class _JobCancelled(Exception):
    """Raised inside job callbacks when a cancel was requested (ADR-0053)."""


def _check_cancelled() -> None:
    """Raise ``_JobCancelled`` when this job's cancel flag is set."""
    if _job_cancel.is_set():
        raise _JobCancelled("cancelled")


def _send(sock, payload: str) -> bool:
    """Send one frame; ``False`` when the socket is gone. Never raises."""
    try:
        with _send_lock:
            sock.send(payload)
        return True
    except Exception:  # noqa: BLE001 - a dead socket must not break the job
        return False


def _emit(event: dict, origin=None) -> None:
    """Send one job frame to every watcher (and the originating socket).

    Watchers receive live frames without owning the job's socket, so several
    TUI sessions can observe one engine (ADR-0053). Dead watchers are dropped.
    """
    payload = json.dumps(event)
    targets = []
    with _watch_lock:
        targets = list(_watchers)
    if origin is not None and origin not in targets:
        targets.append(origin)
    for sock in targets:
        if not _send(sock, payload):
            with _watch_lock:
                _watchers.discard(sock)


def _status_snapshot() -> dict:
    """The subset of engine state a watcher needs to render jobs (ADR-0053)."""
    return {
        "status": _engine_state["status"],
        "job_seq": _engine_state["job_seq"],
        "current_job": _engine_state["current_job"],
        "jobs": _engine_state["jobs"],
    }


def _new_job(kind: str, paths: list[str]) -> dict:
    """Return a fresh ``current_job`` record for a conversion or transcribe job."""
    _engine_state["job_seq"] += 1
    return {
        "id": _engine_state["job_seq"],
        "kind": kind,
        "status": "running",
        "paths": list(paths),
        "idx": 0,
        "total": len(paths),
        "page": 0,
        "page_total": 0,
        "phase": None,
        "started_at": time.time(),
        "finished_at": None,
        "cancel_requested": False,
        "results": [],
        "log_tail": [],
    }


def _set_job(**fields) -> None:
    """Update the shared ``current_job`` record (ADR-0045); ``reset`` replaces it."""
    if fields.pop("reset", False):
        _engine_state["current_job"] = _new_job(fields.pop("kind"), fields.pop("paths"))
    job = _engine_state["current_job"]
    if job is None:
        return
    job.update(fields)
    if job["log_tail"] and len(job["log_tail"]) > _JOB_LOG_TAIL_MAX:
        job["log_tail"] = job["log_tail"][-_JOB_LOG_TAIL_MAX:]


def _finish_job(status: str, **fields) -> None:
    """Retire the running job into the finished-jobs ring (ADR-0053).

    ``current_job`` is cleared so observers can distinguish "a job is running"
    from "the last job finished"; the record (with results and timestamps)
    lives on in ``_engine_state["jobs"]``, newest first.
    """
    job = _engine_state.get("current_job")
    if job is None:
        return
    job.update(status=status, phase=None, finished_at=time.time())
    job.update(fields)
    _engine_state["jobs"].insert(0, job)
    del _engine_state["jobs"][_JOBS_HISTORY_MAX:]
    _engine_state["current_job"] = None


def _import_converter():
    """Import converter lazily (after env is set); separate for test seams."""
    from converter import SUPPORTED_EXTENSIONS, config, convert_files
    from converter import settings  # noqa: F401

    return SUPPORTED_EXTENSIONS, config, convert_files


def _fs_list(path: str) -> dict:
    """List one directory: subdirectories then files, filtered to supported types."""
    target = Path(path).expanduser()
    if not target.is_dir():
        return {"path": path, "error": f"not a directory: {path}"}
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "supported": child.is_dir() or child.suffix.lower() in _SUPPORTED_EXTENSIONS,
                }
            )
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    return {"path": str(target), "parent": str(target.parent), "entries": entries}


def _entry_kind(path: Path) -> str:
    """Classify one conversion discovery result for the picker (ADR-0053)."""
    if path.is_dir():
        return "latex-project"
    if path.suffix.lower() == ".tex":
        return "latex"
    return "convert"


def _fs_entry(path: Path, kind: str | None = None) -> dict:
    """Path plus stat metadata for one picker row (ADR-0053)."""
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {
        "path": str(path),
        "name": path.name,
        "kind": kind or _entry_kind(path),
        "size": size,
        "mtime": mtime,
        "is_dir": path.is_dir(),
    }


def _fs_glob(path: str, recursive: bool = True) -> dict:
    """Expand a file or folder into supported inputs (the Add Folder equivalent).

    A folder that is a LaTeX project (ADR-0038) is yielded once, as the folder;
    other folders are scanned for supported files. ``entries`` carries the
    same paths with size/mtime/kind metadata for the TUI picker (ADR-0053).
    """
    from converter import collect_inputs

    target = Path(path).expanduser()
    files = [f.resolve() for f in collect_inputs([target], recursive=recursive)]
    return {
        "path": str(target),
        "files": [str(f) for f in files],
        "entries": [_fs_entry(f) for f in files],
    }


def _fs_glob_audio(path: str, recursive: bool = True) -> dict:
    """Expand a file or folder into audio files for transcription (ADR-0041).

    Mirrors ``_fs_glob`` but for the transcription pipeline: yields audio files
    (``converter.transcribe.AUDIO_EXTENSIONS``) so the TUI can discover and
    transcribe them without re-implementing discovery in Go. ``converter`` is
    imported lazily (env is applied before the engine serves requests).
    """
    from converter.transcribe import AUDIO_EXTENSIONS

    target = Path(path).expanduser()
    if target.is_file() and target.suffix.lower() in AUDIO_EXTENSIONS:
        files = [target]
    elif target.is_dir():
        walk = target.rglob("*") if recursive else target.iterdir()
        files = [
            p
            for p in sorted(walk, key=lambda p: p.name)
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        ]
    else:
        files = []
    resolved = [f.resolve() for f in files]
    return {
        "path": str(target),
        "files": [str(f) for f in resolved],
        "entries": [_fs_entry(f, kind="audio") for f in resolved],
    }


def _fs_open(path: str, reveal: bool = False) -> dict:
    """Open a path in the OS default app; ``reveal`` selects it in Finder."""
    target = Path(path).expanduser()
    if not target.exists():
        return {"path": path, "error": f"does not exist: {path}"}
    cmd = ["open", "-R", str(target)] if reveal else ["open", str(target)]
    try:
        subprocess.Popen(cmd)
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    return {"path": str(target), "ok": True}


def _resolve_fs(path: str) -> dict:
    """Resolve dropped/typed paths to canonical absolute paths (files or folders)."""
    target = Path(path).expanduser()
    if not target.exists():
        return {"path": path, "error": f"does not exist: {path}"}
    return {"path": str(target.resolve()), "is_dir": target.is_dir()}


def _state_dir() -> Path:
    """Return the app state dir (mirrors ``lock._state_dir``)."""
    return Path(os.environ.get("PTM_STATE_DIR", Path.home() / ".local" / "state" / "ptm"))


def _uploads_dir() -> Path:
    """Staging directory for browser-uploaded files (ADR-0027)."""
    return _state_dir() / "uploads"


def _is_staged_upload(path: Path) -> bool:
    """Whether ``path`` lives under the browser-upload staging dir (ADR-0027)."""
    try:
        uploads = _uploads_dir().resolve()
        return uploads in path.resolve().parents
    except OSError:
        return False


def _safe_upload_name(name: str) -> str:
    """Reduce a client-supplied filename to a safe basename, or '' when rejected."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not base or base in (".", "..") or base.startswith("."):
        return ""
    return base


def _dedup_path(target: Path) -> Path:
    """Return ``target`` if free, else a ``stem-N.suffix`` sibling that is."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(1, 10000):
        candidate = target.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem}-{time.time_ns()}{suffix}")


_COUNTER_SUFFIX = re.compile(r"-\d+$")


def _strip_counter_suffix(stem: str) -> str:
    """Strip a trailing ``-<digits>`` from a filename *stem* (the ``_dedup_path`` form)."""
    return _COUNTER_SUFFIX.sub("", stem)


def _key(name: str) -> tuple[str, str]:
    """Return a ``(stem, suffix)`` identity for basename matching."""
    path = Path(name)
    return path.stem, path.suffix.lower()


def _matches(name: str, candidate: str) -> str | None:
    """Return 'exact' or 'stripped' when ``candidate`` matches ``name``, else None.

    Exact basename wins; a match of the ``-<digits>``-stripped stems is the
    fallback (handles the staging dedup suffix AND on-disk ``-N`` files).
    """
    if candidate == name:
        return "exact"
    c_stem, c_suffix = _key(candidate)
    n_stem, n_suffix = _key(name)
    if c_suffix == n_suffix and _strip_counter_suffix(c_stem) == _strip_counter_suffix(n_stem):
        return "stripped"
    return None


def _resolve_original(name: str, uploads_dir: Path, size: int | None = None) -> Path | None:
    """Resolve an uploaded file's on-disk original by basename match (ADR-0028).

    Consults ``recent_files`` (most-recent first) for a path that matches
    ``name`` exactly, else by ``-<digits>``-suffix-stripped stem; staging-dir
    paths and missing files are skipped. When ``size`` is given, size-equal
    candidates are *preferred* within each match class but never required.

    When ``recent_files`` yields no match, falls back to scanning the configured
    vault root (the ``vault_root`` preference) for the basename, so a
    freshly-dropped, never-converted file still resolves to its on-disk original
    and outputs beside it rather than in the staging tree. Returns ``None`` when
    no such original is known.
    """
    from converter.settings import recent_files

    uploads = str(uploads_dir)

    def _best(candidates) -> Path | None:
        exact: list[Path] = []
        stripped: list[Path] = []
        for raw_path in candidates:
            if not raw_path:
                continue
            path = Path(raw_path)
            if uploads and (str(path) == uploads or str(path).startswith(uploads.rstrip("/") + "/")):
                continue
            if not path.exists():
                continue
            kind = _matches(name, path.name)
            if kind == "exact":
                exact.append(path)
            elif kind == "stripped":
                stripped.append(path)

        def _prefer(bucket: list[Path]) -> Path | None:
            if not bucket:
                return None
            if size is None:
                return bucket[0]
            sized = [p for p in bucket if p.stat().st_size == size]
            return sized[0] if sized else bucket[0]

        return _prefer(exact) or _prefer(stripped) or None

    try:
        found = _best(recent_files(limit=100))
    except Exception:  # noqa: BLE001
        found = None
    if found is not None:
        return found

    return _find_in_vault(name, uploads_dir, size)


def _vault_root_pref() -> Path | None:
    """Return the configured vault root as a ``Path``, or ``None`` when unset/invalid."""
    from converter.settings import get_setting

    try:
        value = get_setting("vault_root", "")
    except Exception:  # noqa: BLE001
        return None
    if not value:
        return None
    root = Path(value).expanduser()
    return root if root.is_dir() else None


def _find_in_vault(name: str, uploads_dir: Path, size: int | None = None) -> Path | None:
    """Scan the configured vault root for ``name`` (bounded), excluding staging.

    Runs a bounded depth-first walk of the vault root, matching exact basename
    first then ``-<digits>``-stripped sterns, preferring a size-equal candidate.
    Uses the same matching semantics as the ``recent_files`` fast path but
    against the real on-disk tree, so never-seen files resolve to their source.
    """
    root = _vault_root_pref()
    if root is None:
        return None
    uploads = str(uploads_dir)
    exact: list[Path] = []
    stripped: list[Path] = []
    for path in _walk_vault(root, uploads):
        try:
            if not path.is_file() or path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                continue
        except OSError:
            continue
        kind = _matches(name, path.name)
        if kind == "exact":
            exact.append(path)
        elif kind == "stripped":
            stripped.append(path)

    def _prefer(bucket: list[Path]) -> Path | None:
        if not bucket:
            return None
        if size is None:
            return bucket[0]
        sized = [p for p in bucket if _safe_size(p) == size]
        return sized[0] if sized else bucket[0]

    return _prefer(exact) or _prefer(stripped) or None


def _safe_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


_MAX_VAULT_SCAN_FILES = 20000


def _walk_vault(root: Path, uploads_dir: str):
    """Depth-first walk of ``root``, yielding paths and pruning hidden/symlinked dirs.

    Bounded by ``_MAX_VAULT_SCAN_FILES`` (not by depth); hidden entries,
    symlinked dirs, and the staging dir itself are skipped so the scan stays
    cheap and never re-enters the uploads tree.
    """
    seen = 0
    stack = [root]
    while stack and seen < _MAX_VAULT_SCAN_FILES:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen >= _MAX_VAULT_SCAN_FILES:
                        return
                    if entry.name.startswith("."):
                        continue
                    path = Path(entry.path)
                    if str(path) == uploads_dir:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    else:
                        yield path
        except OSError:
            continue


def _fs_upload(parts) -> dict:
    """Persist uploaded file parts into the staging dir; return native paths."""
    out_dir = _uploads_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"files": [], "error": f"cannot create upload dir: {exc}"}

    saved: list[dict] = []
    errors: list[dict] = []
    for storage in parts:
        original = storage.filename or ""
        safe = _safe_upload_name(original)
        if not safe:
            errors.append({"name": original, "error": "invalid filename"})
            continue
        suffix = Path(safe).suffix.lower()
        if suffix not in _SUPPORTED_EXTENSIONS:
            errors.append({"name": original, "error": f"unsupported type {suffix or '(none)'}"})
            continue
        dest = _dedup_path(out_dir / safe)
        try:
            storage.save(str(dest))
            size = dest.stat().st_size
        except OSError as exc:
            errors.append({"name": original, "error": str(exc)})
            continue
        entry = {"name": dest.name, "path": str(dest.resolve()), "size": size, "original": None}
        on_disk = _resolve_original(safe, out_dir, size=size)
        if on_disk is not None:
            entry["original"] = str(on_disk.resolve())
            from converter.settings import set_upload_original

            set_upload_original(str(dest.resolve()), str(on_disk.resolve()))
        else:
            entry["fallback_dir"] = str((dest.parent / "markdown").resolve())
        saved.append(entry)
    return {"files": saved, "errors": errors or None}


def _prune_uploads(now: float | None = None) -> None:
    """Best-effort startup sweep of staged uploads older than the retention window."""
    out_dir = _uploads_dir()
    if not out_dir.is_dir():
        return
    cutoff = (now if now is not None else time.time()) - UPLOAD_MAX_AGE_SECONDS
    try:
        for child in out_dir.iterdir():
            try:
                if child.is_file() and child.stat().st_mtime < cutoff:
                    child.unlink()
                    from converter.settings import delete_upload_original

                    delete_upload_original(str(child))
            except OSError:
                continue
    except OSError:
        return


_PID_MARKERS = ("-m engine", "ptm-engine")


def _engine_pids(port: int | None = None) -> list[int]:
    """Return the PIDs of running engine processes, excluding this one.

    Matches by command line (``python -m engine`` / ``ptm-engine``); when a
    port is given it is also used as a cross-check via the process list, but
    the command-line match is authoritative (keeps the manifest's model daemons
    out of the kill set).
    """
    pids: set[int] = set()
    me = os.getpid()
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid == me:
                continue
            command = parts[1] if len(parts) > 1 else ""
            if any(marker in command for marker in _PID_MARKERS):
                pids.add(pid)
    except (OSError, subprocess.SubprocessError):
        pass

    if port is not None:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for token in out.stdout.split():
                try:
                    pid = int(token)
                except ValueError:
                    continue
                if pid != me:
                    pids.add(pid)
        except (OSError, subprocess.SubprocessError):
            pass

    return sorted(pids)


def _kill_pid(pid: int) -> bool:
    """Send SIGTERM to ``pid``; return True on success."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _kill_running_engines(port: int | None = None) -> list[int]:
    """Terminate every running engine process; return the PIDs that were killed."""
    killed: list[int] = []
    for pid in _engine_pids(port):
        if _kill_pid(pid):
            killed.append(pid)
    return killed


def _result_record(result, durations: list[float], idx: int) -> dict:
    """Serialize one ``ConvertResult`` for the job history (ADR-0053)."""
    duration_ms = None
    if idx < len(durations):
        duration_ms = int(durations[idx] * 1000)
    return {
        "source": str(result.source_path),
        "md_path": str(result.md_path) if result.md_path else None,
        "moved_to": str(result.moved_to) if result.moved_to else None,
        "error": result.error,
        "warnings": list(result.warnings),
        "inference_passes": list(result.inference_passes),
        "duration_ms": duration_ms,
    }


def _job_execute(wsock, paths: list[str], output_dir: str | None, duplicate: bool) -> None:
    _, config, convert_files = _import_converter()
    from converter.settings import get_upload_original, record_recent

    with _job_lock:
        if _job_running.is_set():
            _engine_state["status"] = "error"
            _engine_state["log"] = [("err", "A conversion is already running.")]
            _emit({"type": "error", "message": "A conversion is already running."}, origin=wsock)
            return
        _job_running.set()
        _job_cancel.clear()

    _engine_state.update(
        status="running", paths=paths, output_dir=output_dir, duplicate=duplicate,
        results=None, log=[],
    )
    _set_job(reset=True, kind="convert", paths=paths)
    job_id = _engine_state["current_job"]["id"]

    def log(kind: str, msg: str) -> None:
        _engine_state["log"].append((kind, msg))
        job = _engine_state.get("current_job")
        if job is not None:
            job["log_tail"].append(f"[{kind}] {msg}")
        _emit({"type": "log", "kind": kind, "message": msg, "job_id": job_id}, origin=wsock)

    file_durations: list[float] = []
    last_mark = time.time()

    def on_progress(idx: int, total: int, name: str) -> None:
        nonlocal last_mark
        _check_cancelled()
        now = time.time()
        file_durations.append(now - last_mark)
        last_mark = now
        _set_job(idx=idx, total=total, phase=name)
        _emit({"type": "file", "idx": idx, "total": total, "name": name, "job_id": job_id}, origin=wsock)

    def on_page_progress(page: int, total: int, name: str) -> None:
        _check_cancelled()
        _set_job(page=page, page_total=total, phase=name)
        _emit({"type": "page", "page": page, "total": total, "name": name, "job_id": job_id}, origin=wsock)

    if output_dir:
        out = Path(output_dir)
        source_resolver = None
    else:
        from converter.organize import default_output_dir

        def resolve_output(path):
            resolved = Path(path).resolve()
            original = get_upload_original(str(resolved))
            if original:
                return default_output_dir(Path(original))
            if _is_staged_upload(resolved):
                # Unknown original: keep the Markdown inside the uploads tree
                # (the prune sweep only touches files directly in uploads/).
                return Path(path).parent / "markdown"
            return default_output_dir(Path(path))

        def resolve_source(path):
            """The real on-disk file a conversion represents (ADR-0028/0048).

            Uploaded files convert their staged copy but organize (kebab name,
            ``source/`` archive, compression) the original on disk. A staged
            upload with no known original is left to the prune-safe fallback.
            """
            resolved = Path(path).resolve()
            original = get_upload_original(str(resolved))
            if original:
                return Path(original)
            return None if _is_staged_upload(resolved) else Path(path)

        out = resolve_output
        source_resolver = resolve_source

    try:
        results = convert_files(
            [Path(p) for p in paths],
            out,
            progress_callback=on_progress,
            page_progress_callback=on_page_progress,
            duplicate_if_exists=duplicate,
            source_resolver=source_resolver,
        )
        for result in results:
            src = str(result.source_path.resolve())
            original = get_upload_original(src)
            recorded = result.moved_to or (
                Path(original) if original else result.source_path
            )
            record_recent(str(recorded.resolve()))
            if result.error:
                log("err", f"{result.source_path.name}: {result.error}")
            else:
                log("ok", f"{result.source_path.name} -> {result.md_path}")
                for warning in result.warnings:
                    log("warn", f"{result.source_path.name}: {warning}")
        _engine_state["results"] = [r.error or str(r.md_path) for r in results]
        _engine_state["status"] = "done"
        records = [_result_record(r, file_durations, i) for i, r in enumerate(results)]
        ok = sum(1 for r in results if not r.error)
        _finish_job("done", ok=ok, results=records)
        _emit({"type": "done", "ok": ok, "total": len(results), "job_id": job_id}, origin=wsock)
    except _JobCancelled:
        _engine_state["status"] = "cancelled"
        log("warn", "Conversion cancelled")
        _finish_job("cancelled", ok=len(file_durations))
        _emit({"type": "cancelled", "ok": len(file_durations), "total": len(paths), "job_id": job_id}, origin=wsock)
    except Exception as exc:  # noqa: BLE001
        _engine_state["status"] = "error"
        log("err", f"Conversion failed: {exc}")
        _finish_job("error", ok=0, error=str(exc))
        _emit({"type": "done", "ok": 0, "total": len(paths), "error": str(exc), "job_id": job_id}, origin=wsock)
    finally:
        _job_running.clear()


# Recognized phase lines emitted by ``converter.transcribe``'s ``on_line``
# callback, mapped to short phase labels for the WS/TUI (ADR-0045).
_PHASE_MARKERS: list[tuple[str, str]] = [
    ("ffmpeg: cleaning audio", "cleaning"),
    ("dereverberating", "dereverb"),
    ("enhancing", "enhance"),
    ("isolating", "isolate"),
    ("transcribing", "transcribe"),
    ("diarizing", "diarize"),
    ("checking coverage", "coverage"),
    ("re-decoding", "re-decode"),
    ("aligning interview guide", "guide"),
    ("editing transcript", "edit"),
]


def _map_phase(line: str) -> str | None:
    """Map an ``on_line`` phase line to a short phase label, else ``None``."""
    low = line.strip().lower()
    for marker, phase in _PHASE_MARKERS:
        if low.startswith(marker):
            return phase
    return None


def _apply_audio_options(options: dict) -> None:
    """Apply transcription options to the converter's module-level config.

    ``converter.transcribe``/``converter.audio`` read their settings at import
    time (ADR-0043); for engine-hosted transcription we set the same module
    attributes per job from the request payload (model/language/diarize/speakers).
    """
    from converter import audio as audio_mod
    from converter import transcribe as transcribe_mod

    transcribe_mod.AUDIO_ENABLED = True
    model = options.get("model")
    if model:
        transcribe_mod.AUDIO_MODEL = model
    language = options.get("language")
    transcribe_mod.AUDIO_LANGUAGE = None if language in (None, "", "auto") else language
    speakers = int(options.get("speakers") or 0)
    diarize = bool(options.get("diarize", False)) or speakers > 0
    audio_mod.AUDIO_DIARIZE_ENABLED = diarize
    audio_mod.AUDIO_DIARIZE_SPEAKERS = speakers if speakers > 0 else None
    audio_mod.AUDIO_DIARIZE_MIN_SPEAKERS = None
    audio_mod.AUDIO_DIARIZE_MAX_SPEAKERS = None


def _transcribe_execute(wsock, paths: list[str], audio_options: dict | None) -> None:
    """Run engine-hosted transcription for ``paths`` (ADR-0045).

    Owns the ``transcribe.lock`` flock for the job's duration and streams
    structured ``phase``/``file``/``log`` frames over the WS. Each path is either
    a Markdown file (transcript attached to it) or an audio file (paired to a
    sibling ``.md`` if present, else a standalone transcript).
    """
    with _job_lock:
        if _job_running.is_set():
            _engine_state["status"] = "error"
            _engine_state["log"] = [("err", "A job is already running.")]
            _emit({"type": "error", "message": "A job is already running."}, origin=wsock)
            return
        _job_running.set()
        _job_cancel.clear()

    _apply_audio_options(audio_options or {})

    from converter.transcribe import attach_transcript, transcribe_to_markdown
    import lock

    lock_handle = lock.acquire_transcribe_lock()
    if not lock_handle.held:
        _job_running.clear()
        _engine_state["status"] = "error"
        _engine_state["log"] = [("err", "Another transcription is already running.")]
        _emit({"type": "error", "message": "Another transcription is already running."}, origin=wsock)
        return

    _engine_state.update(status="running", results=None, log=[])
    _set_job(reset=True, kind="transcribe", paths=paths)
    job_id = _engine_state["current_job"]["id"]

    def on_line(line: str) -> None:
        phase = _map_phase(line)
        if phase is not None:
            _set_job(phase=phase)
            _emit({"type": "phase", "phase": phase, "job_id": job_id}, origin=wsock)
            return
        text = line.rstrip("\n").strip()
        if text:
            _engine_state["log"].append(("info", text))
            job = _engine_state.get("current_job")
            if job is not None:
                job["log_tail"].append(text)
                job["log_tail"] = job["log_tail"][-_JOB_LOG_TAIL_MAX:]
            _emit({"type": "log", "kind": "info", "message": text, "job_id": job_id}, origin=wsock)

    ok = 0
    total = len(paths)
    try:
        for idx, raw in enumerate(paths, start=1):
            _check_cancelled()
            path = Path(raw)
            _set_job(idx=idx, total=total, phase="starting")
            _emit({"type": "file", "idx": idx, "total": total, "name": path.name, "job_id": job_id}, origin=wsock)
            warnings: list[str] = []
            if path.suffix.lower() == ".md":
                result = attach_transcript(path, warnings, on_line=on_line)
            else:
                md = path.with_suffix(".md")
                if md.exists():
                    result = attach_transcript(md, warnings, audio_path=path, on_line=on_line)
                else:
                    result = transcribe_to_markdown(path, warnings, on_line=on_line)
            for warning in warnings:
                _emit({"type": "log", "kind": "warn", "message": f"{path.name}: {warning}", "job_id": job_id}, origin=wsock)
            if result is None and not warnings:
                _emit({"type": "log", "kind": "warn", "message": f"{path.name}: no transcript produced", "job_id": job_id}, origin=wsock)
            else:
                ok += 1
        _engine_state["status"] = "done"
        _finish_job("done", ok=ok)
        _emit({"type": "done", "ok": ok, "total": total, "job_id": job_id}, origin=wsock)
    except _JobCancelled:
        _engine_state["status"] = "cancelled"
        _finish_job("cancelled", ok=ok)
        _emit({"type": "cancelled", "ok": ok, "total": total, "job_id": job_id}, origin=wsock)
    except Exception as exc:  # noqa: BLE001
        _engine_state["status"] = "error"
        _finish_job("error", ok=ok, error=str(exc))
        _emit({"type": "log", "kind": "err", "message": f"Transcription failed: {exc}", "job_id": job_id}, origin=wsock)
        _emit({"type": "done", "ok": 0, "total": total, "error": str(exc), "job_id": job_id}, origin=wsock)
    finally:
        lock_handle.release()
        _job_running.clear()


def create_app() -> Flask:
    app = Flask("ptm-engine")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    _prune_uploads()
    sock = Sock(app)

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "engine": True, "status": _engine_state["status"]})

    @app.get("/api/fs/list")
    def fs_list():
        return jsonify(_fs_list(request.args.get("path", "")))

    @app.get("/api/fs/glob")
    def fs_glob():
        recursive = request.args.get("recursive", "1") not in ("0", "false", "no")
        kinds = request.args.get("kinds", "convert")
        if kinds == "audio":
            return jsonify(_fs_glob_audio(request.args.get("path", ""), recursive))
        return jsonify(_fs_glob(request.args.get("path", ""), recursive))

    @app.post("/api/fs/resolve")
    def fs_resolve():
        return jsonify(_resolve_fs((request.get_json(silent=True) or {}).get("path", "")))

    @app.post("/api/fs/open")
    def fs_open():
        data = request.get_json(silent=True) or {}
        return jsonify(_fs_open(data.get("path", ""), reveal=bool(data.get("reveal", False))))

    @app.post("/api/fs/upload")
    def fs_upload():
        return jsonify(_fs_upload(request.files.getlist("files")))

    @app.post("/api/shutdown")
    def shutdown():
        def _stop():
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_stop, daemon=True).start()
        return jsonify({"ok": True})

    @app.get("/api/recent")
    def recent():
        from converter.settings import recent_files

        return jsonify({"recent": recent_files()})

    @app.get("/api/config")
    def config_get():
        _, config, _ = _import_converter()
        return jsonify(config.snapshot(probe=False))

    @app.post("/api/config")
    def config_set():
        _, config, _ = _import_converter()
        from converter.settings import set_setting

        data = request.get_json(silent=True) or {}
        toggles = data.get("features")
        if isinstance(toggles, dict):
            for key, value in toggles.items():
                if key in config.FEATURES:
                    config.set_enabled(key, bool(value))
                    set_setting("ai_" + key, "1" if value else "0")
        if "pdf_mode" in data:
            mode = "paper" if data["pdf_mode"] == "paper" else "slide"
            os.environ["PDF_MODE"] = mode
            set_setting("pdf_mode", mode)
        if "image_links" in data:
            value = "on" if data["image_links"] else "off"
            os.environ["IMAGE_LINKS"] = value
            set_setting("image_links", value)
        if "pdf_compress" in data:
            value = "on" if data["pdf_compress"] else "off"
            os.environ["PDF_COMPRESS"] = value
            set_setting("pdf_compress", value)
        if "inference_overwrite" in data:
            value = "on" if data["inference_overwrite"] else "off"
            os.environ["INFERENCE_OVERWRITE"] = value
            set_setting("inference_overwrite", value)
        if "vault_root" in data:
            set_setting("vault_root", str(data["vault_root"]).strip())
        if "duplicate" in data:
            set_setting("duplicate_if_exists", "on" if data["duplicate"] else "off")
        if "audio_model" in data:
            set_setting("audio_model", str(data["audio_model"]).strip())
        if "audio_language" in data:
            set_setting("audio_language", str(data["audio_language"]).strip())
        if "audio_diarize" in data:
            set_setting("audio_diarize", "on" if data["audio_diarize"] else "off")
        if "audio_speakers" in data:
            try:
                n = int(data["audio_speakers"] or 0)
            except (TypeError, ValueError):
                n = 0
            set_setting("audio_speakers", str(n) if n > 0 else "0")
        return jsonify(config.snapshot(probe=False))

    @app.get("/api/health/servers")
    def servers():
        _, config, _ = _import_converter()
        results = []
        seen: set[str] = set()
        for key in config.enabled_keys():
            for name, url in config.feature_endpoints(key):
                if name in seen:
                    continue
                seen.add(name)
                results.append(
                    {"name": name, "up": config.probe(url), "url": url,
                     "command": config.SERVERS[name].start_command}
                )
        return jsonify({"servers": results, "missing": config.missing_servers()})

    @app.get("/api/status")
    def status():
        return jsonify(_engine_state)

    @app.post("/api/jobs/cancel")
    def jobs_cancel():
        """Request cooperative cancellation of the running job (ADR-0053).

        The job stops at the next page/file boundary; the final frame is
        ``{"type": "cancelled", ...}`` and the job retires with status
        ``cancelled``.
        """
        data = request.get_json(silent=True) or {}
        with _job_lock:
            job = _engine_state.get("current_job")
            if not _job_running.is_set() or job is None:
                return jsonify({"ok": False, "error": "no job is running"}), 409
            wanted = data.get("id")
            try:
                wanted = int(wanted) if wanted is not None else None
            except (TypeError, ValueError):
                wanted = None
            if wanted is not None and wanted != job["id"]:
                return jsonify({"ok": False, "error": "job id mismatch", "job_id": job["id"]}), 409
            _job_cancel.set()
            job["cancel_requested"] = True
            job_id = job["id"]
        return jsonify({"ok": True, "job_id": job_id, "status": "cancelling"})

    @app.get("/api/runs")
    def runs():
        """Recent conversion runs from ptm.sqlite, newest first (ADR-0053)."""
        from converter.db.repos import recent_runs

        try:
            limit = int(request.args.get("limit", "20"))
        except ValueError:
            limit = 20
        return jsonify({"runs": recent_runs(max(1, min(limit, 200)))})

    @sock.route("/ws")
    def job_ws(wsock):
        try:
            while True:
                msg = wsock.receive()
                if msg is None:
                    return
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")
                if mtype == "watch":
                    # Read-only subscription: the snapshot drives the initial
                    # UI, then live job frames arrive without owning the job
                    # (ADR-0053). A socket may watch and start jobs.
                    with _watch_lock:
                        _watchers.add(wsock)
                    _send(wsock, json.dumps({"type": "status", "state": _status_snapshot()}))
                elif mtype == "start":
                    _job_execute(
                        wsock,
                        data.get("paths", []),
                        data.get("output_dir"),
                        bool(data.get("duplicate", False)),
                    )
                elif mtype == "transcribe":
                    _transcribe_execute(
                        wsock,
                        data.get("paths", []),
                        data.get("audio_options"),
                    )
        finally:
            with _watch_lock:
                _watchers.discard(wsock)

    return app


def _main(argv: list[str] | None = None) -> int:
    from cli_common import add_ai_flags, apply_ai_env

    parser = argparse.ArgumentParser(
        prog="ptm-engine",
        description="Native engine process for the PTM TUI (ADR-0025/0040).",
    )
    add_ai_flags(parser)
    parser.add_argument("--host", default=DEFAULT_HOST, help="host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to bind (default: 9091)")
    parser.add_argument(
        "--kill",
        action="store_true",
        help="stop any running ptm-engine processes and exit",
    )
    args = parser.parse_args(argv)

    if args.kill:
        port = int(os.environ.get("PTM_ENGINE_PORT", str(args.port)))
        killed = _kill_running_engines(port)
        if killed:
            print(f"Stopped engine process(es): {', '.join(str(p) for p in killed)}", flush=True)
        else:
            print("No running ptm-engine process found.", flush=True)
        return 0

    apply_ai_env(args)

    app = create_app()
    print(f"Engine: http://{args.host}:{args.port} (native converter, single-job)", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point; applying env-before-import is the caller's job.

    When spawned by the TUI (or its launcher), the environment is inherited from
    that process; when launched by hand, ``ptm-engine`` inherits the current
    shell environment.
    """
    raise SystemExit(_main(argv))


if __name__ == "__main__":
    main()
