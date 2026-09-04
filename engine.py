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
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request
from flask_sock import Sock

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090

_SUPPORTED_EXTENSIONS = {".pptx", ".pdf"}

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


def _job_execute(wsock, paths: list[str], output_dir: str | None, duplicate: bool) -> None:
    _, config, convert_files = _import_converter()
    from converter.settings import record_recent

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

    out = Path(output_dir) if output_dir else None
    try:
        results = convert_files(
            [Path(p) for p in paths],
            out,
            progress_callback=on_progress,
            page_progress_callback=on_page_progress,
            duplicate_if_exists=duplicate,
        )
        for result in results:
            record_recent(str(result.source_path.resolve()))
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
    args = parser.parse_args(argv)
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
