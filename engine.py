"""Native engine process for the web GUI (ADR-0025).

The web UI (the Flask ``dashboard`` app) is a read-only observer and a thin
control-plane now. This process owns everything a browser cannot do alone:

- running conversions (``convert_files`` on a worker thread),
- filesystem access (directory listing, path resolution, recursive glob, and a
  native "open in Finder"),
- the AI server preflight (``config.probe`` / ``config.missing_servers``), and
- app settings persistence (``converter.settings``).

It is the **sole writer** to ``ptm.sqlite`` (WAL), so the web UI process can keep
its read-only guarantee (ADR-0014) while this process writes run telemetry via
``converter.logstore``.

Like ``start.py``, it applies environment variables *before* importing
``converter`` so the import-time configuration (ADR-0002/ADR-0012) is correct.

Concurrency model: **one conversion at a time**. ``converter.config``'s mutable
feature state and ``converter.logstore``'s connection are process-global, so a
second job is rejected while one is running (the desktop GUI has the same
single-threaded constraint).
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
DEFAULT_PORT = 8090

_SUPPORTED_EXTENSIONS = {".pptx", ".pdf"}

# Uploaded files are staged under the state dir (ADR-0027) and pruned after
# this long. Sizes are enforced via Flask's MAX_CONTENT_LENGTH on the app.
UPLOAD_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

_engine_state = {
    "status": "idle",  # idle | running | done | error
    "paths": [],
    "output_dir": None,
    "duplicate": False,
    "results": None,
    "log": [],
}

_job_lock = threading.Lock()
_job_running = threading.Event()


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


def _fs_glob(path: str, recursive: bool = True) -> dict:
    """Expand a file or folder into supported file paths (the Add Folder equivalent)."""
    target = Path(path).expanduser()
    if target.is_dir():
        it = target.rglob("*") if recursive else target.iterdir()
        files = sorted(
            cand for cand in it if cand.suffix.lower() in _SUPPORTED_EXTENSIONS
        )
    elif target.suffix.lower() in _SUPPORTED_EXTENSIONS:
        files = [target]
    else:
        files = []
    return {"path": str(target), "files": [str(f.resolve()) for f in files]}


def _fs_open(path: str) -> dict:
    """Open a path in the OS default app (Finder / default handler)."""
    target = Path(path).expanduser()
    if not target.exists():
        return {"path": path, "error": f"does not exist: {path}"}
    try:
        subprocess.Popen(["open", str(target)])
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
    the command-line match is authoritative (keeps AI servers on 8081-8084 /
    11434 out of the kill set).
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


def _job_execute(wsock, paths: list[str], output_dir: str | None, duplicate: bool) -> None:
    _, config, convert_files = _import_converter()
    from converter.settings import get_upload_original, record_recent

    with _job_lock:
        if _job_running.is_set():
            _engine_state["status"] = "error"
            _engine_state["log"] = [("err", "A conversion is already running.")]
            wsock.send(json.dumps({"type": "error", "message": "A conversion is already running."}))
            return
        _job_running.set()

    _engine_state.update(
        status="running", paths=paths, output_dir=output_dir, duplicate=duplicate,
        results=None, log=[],
    )

    def log(kind: str, msg: str) -> None:
        _engine_state["log"].append((kind, msg))
        wsock.send(json.dumps({"type": "log", "kind": kind, "message": msg}))

    def on_progress(idx: int, total: int, name: str) -> None:
        wsock.send(json.dumps({"type": "file", "idx": idx, "total": total, "name": name}))

    def on_page_progress(page: int, total: int, name: str) -> None:
        wsock.send(json.dumps({"type": "page", "page": page, "total": total, "name": name}))

    if output_dir:
        out = Path(output_dir)
    else:
        def resolve_output(path):
            original = get_upload_original(str(Path(path).resolve()))
            if original:
                return Path(original).parent / "markdown"
            return None

        out = resolve_output

    try:
        results = convert_files(
            [Path(p) for p in paths],
            out,
            progress_callback=on_progress,
            page_progress_callback=on_page_progress,
            duplicate_if_exists=duplicate,
        )
        for result in results:
            src = str(result.source_path.resolve())
            original = get_upload_original(src)
            record_recent(original or src)
            if result.error:
                log("err", f"{result.source_path.name}: {result.error}")
            else:
                log("ok", f"{result.source_path.name} -> {result.md_path}")
                for warning in result.warnings:
                    log("warn", f"{result.source_path.name}: {warning}")
        _engine_state["results"] = [r.error or str(r.md_path) for r in results]
        _engine_state["status"] = "done"
        wsock.send(json.dumps({"type": "done", "ok": sum(1 for r in results if not r.error), "total": len(results)}))
    except Exception as exc:  # noqa: BLE001
        _engine_state["status"] = "error"
        log("err", f"Conversion failed: {exc}")
        wsock.send(json.dumps({"type": "done", "ok": 0, "total": len(paths), "error": str(exc)}))
    finally:
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
        return jsonify(_fs_glob(request.args.get("path", ""), recursive))

    @app.post("/api/fs/resolve")
    def fs_resolve():
        return jsonify(_resolve_fs((request.get_json(silent=True) or {}).get("path", "")))

    @app.post("/api/fs/open")
    def fs_open():
        return jsonify(_fs_open((request.get_json(silent=True) or {}).get("path", "")))

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
        if "vault_root" in data:
            set_setting("vault_root", str(data["vault_root"]).strip())
        if "duplicate" in data:
            set_setting("duplicate_if_exists", "on" if data["duplicate"] else "off")
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
                     "command": config.SERVERS[name].serve_command}
                )
        return jsonify({"servers": results, "missing": config.missing_servers()})

    @sock.route("/ws")
    def job_ws(wsock):
        while True:
            msg = wsock.receive()
            if msg is None:
                return
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "start":
                _job_execute(
                    wsock,
                    data.get("paths", []),
                    data.get("output_dir"),
                    bool(data.get("duplicate", False)),
                )

    return app


def _main(argv: list[str] | None = None) -> int:
    from cli_common import add_ai_flags, apply_ai_env

    parser = argparse.ArgumentParser(
        prog="ptm-engine",
        description="Native engine process for the PTM web GUI (ADR-0025).",
    )
    add_ai_flags(parser)
    parser.add_argument("--host", default=DEFAULT_HOST, help="host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to bind (default: 8090)")
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
    """Console-script entry point; applies env-before-import is the caller's job.

    When launched by the web UI, the environment is inherited from the UI process
    (which already applied ``apply_ai_env`` or ``--env``). When launched by hand,
    ``ptm-engine`` inherits the current shell environment.
    """
    raise SystemExit(_main(argv))


if __name__ == "__main__":
    main()
