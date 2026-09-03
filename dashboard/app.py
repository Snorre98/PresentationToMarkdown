"""Flask app factory and routes for the read-only PTM dashboard.

Builds a small JSON API plus a rendered frontend over the ``ptm.sqlite`` log.
Every request opens a fresh, read-only SQLite connection (``mode=ro`` +
``query_only=ON``), so the dashboard never blocks or writes to the conversion's
WAL database. It imports nothing from ``converter`` — it only reads the file the
library writes (ADR-0014 / ADR-0022).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_DB = Path(__file__).resolve().parents[1] / "ptm.sqlite"


def _connect(db_path: str) -> sqlite3.Connection:
    """Open the log database read-only; never interferes with the writer."""
    return sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, timeout=1.0
    )


def _query(db_path: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Run a read-only query, returning [] on any failure (missing/locked DB)."""
    try:
        conn = _connect(db_path)
        try:
            conn.execute("PRAGMA query_only=ON;")
            conn.execute("PRAGMA busy_timeout=1000;")
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def _scalar(db_path: str, sql: str, params: tuple = (), default=None):
    rows = _query(db_path, sql, params)
    return rows[0][0] if rows else default


def _rowdicts(columns: tuple[str, ...], rows: list[tuple]) -> list[dict]:
    return [dict(zip(columns, row)) for row in rows]


def _overview(db_path: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT DISTINCT source, COUNT(*) AS total_events, MAX(page) AS max_page,
               COALESCE(SUM(latency_ms), 0) AS total_latency_ms, MAX(ts) AS last_ts
        FROM vision_events
        WHERE source IS NOT NULL AND source != ''
        GROUP BY source
        """,
    )
    recents = _query(db_path, "SELECT path FROM recent_files ORDER BY last_used DESC")
    recent_order = {path: idx for idx, (path,) in enumerate(recents)}

    def sort_key(row: tuple):
        idx = recent_order.get(row[0])
        return (0, idx) if idx is not None else (1, 0)

    ordered = sorted(rows, key=sort_key)
    sources = [
        {
            "source": source,
            "name": Path(source).name or source,
            "total_events": total_events,
            "max_page": max_page,
            "total_latency_ms": total_latency_ms,
            "last_ts": last_ts,
        }
        for source, total_events, max_page, total_latency_ms, last_ts in ordered
    ]
    total = _scalar(db_path, "SELECT COUNT(*) FROM vision_events", default=0)
    return {
        "db": db_path,
        "total_events": total,
        "sources": sources,
    }


def _events(db_path: str, source: str, run_id: int | None) -> dict:
    if run_id is not None:
        rows = _query(
            db_path,
            """
            SELECT id, ts, page, image_ref, stage, model, decision, latency_ms,
                   markdown, error, base_url, run_id
            FROM vision_events
            WHERE run_id = ?
            ORDER BY ts ASC, id ASC
            """,
            (run_id,),
        )
    else:
        rows = _query(
            db_path,
            """
            SELECT id, ts, page, image_ref, stage, model, decision, latency_ms,
                   markdown, error, base_url, run_id
            FROM vision_events
            WHERE source = ?
            ORDER BY ts ASC, id ASC
            """,
            (source,),
        )
    events = [
        {
            "id": id_,
            "ts": ts,
            "page": page,
            "image_ref": image_ref,
            "stage": stage,
            "model": model,
            "decision": decision,
            "latency_ms": latency_ms,
            "markdown": markdown,
            "error": error,
            "base_url": base_url,
            "run_id": run_id_,
        }
        for id_, ts, page, image_ref, stage, model, decision, latency_ms, markdown, error, base_url, run_id_ in rows
    ]
    return {"db": db_path, "source": source, "run_id": run_id, "events": events}


def _errors(db_path: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT id, ts, source, page, stage, model, decision, error
        FROM vision_events
        WHERE error IS NOT NULL AND error != ''
        ORDER BY ts DESC, id DESC
        """,
    )
    errors = [
        {
            "id": id_,
            "ts": ts,
            "source": source,
            "name": Path(source).name if source else "",
            "page": page,
            "stage": stage,
            "model": model,
            "decision": decision,
            "error": error,
        }
        for id_, ts, source, page, stage, model, decision, error in rows
    ]
    return {"db": db_path, "errors": errors}


def _health(db_path: str) -> dict:
    total = _scalar(db_path, "SELECT COUNT(*) FROM vision_events", default=0)
    return {
        "ok": True,
        "db": db_path,
        "total_events": total,
    }


def _runs(db_path: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT r.id, r.ts, r.source, r.name, r.status, r.ended_at, r.duration_ms,
               (SELECT COUNT(*) FROM vision_events e WHERE e.run_id = r.id) AS events,
               (SELECT COUNT(*) FROM vision_events e WHERE e.run_id = r.id
                 AND e.error IS NOT NULL AND e.error != '') AS errors
        FROM conversion_runs r
        ORDER BY r.ts DESC, r.id DESC
        """,
    )
    runs = [
        {
            "id": id_,
            "ts": ts,
            "source": source,
            "name": name,
            "status": status,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "events": events,
            "errors": errors,
        }
        for id_, ts, source, name, status, ended_at, duration_ms, events, errors in rows
    ]
    return {"db": db_path, "runs": runs}


def _phases(db_path: str, run_id: int) -> dict:
    rows = _query(
        db_path,
        """
        SELECT id, phase, ordinal, status, started_at, ended_at, duration_ms, detail
        FROM run_phases
        WHERE run_id = ?
        ORDER BY ordinal ASC, id ASC
        """,
        (run_id,),
    )
    return {
        "db": db_path,
        "run_id": run_id,
        "phases": [
            {
                "id": id_,
                "phase": phase,
                "ordinal": ordinal,
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "detail": json.loads(detail) if detail else None,
            }
            for id_, phase, ordinal, status, started_at, ended_at, duration_ms, detail in rows
        ],
    }


def _derived_phases(db_path: str, run_id: int) -> list[dict]:
    """Span per-stage event latency into synthetic phases for classify/transcribe/interpret."""
    rows = _query(
        db_path,
        """
        SELECT stage, MIN(ts) AS started_at, MAX(ts) AS ended_at,
               COUNT(*) AS count, COALESCE(SUM(latency_ms), 0) AS latency_ms,
               MAX(latency_ms) AS max_latency_ms
        FROM vision_events
        WHERE run_id = ? AND stage IN ('classify', 'transcribe', 'interpret', 'readability')
        GROUP BY stage
        """,
        (run_id,),
    )
    return [
        {
            "phase": stage,
            "status": "done",
            "started_at": started_at,
            "ended_at": ended_at,
            "count": count,
            "latency_ms": latency_ms,
            "max_latency_ms": max_latency_ms,
            "derived": True,
        }
        for stage, started_at, ended_at, count, latency_ms, max_latency_ms in rows
    ]


def _run_detail(db_path: str, run_id: int) -> dict:
    row = _query(
        db_path,
        """
        SELECT id, ts, source, name, status, ended_at, duration_ms
        FROM conversion_runs WHERE id = ?
        """,
        (run_id,),
    )
    return {
        "db": db_path,
        "run_id": run_id,
        "run": dict(zip(
            ("id", "ts", "source", "name", "status", "ended_at", "duration_ms"),
            row[0],
        )) if row else None,
    }


def _config(db_path: str, run_id: int) -> dict:
    row = _query(
        db_path, "SELECT snapshot FROM run_config WHERE run_id = ?", (run_id,)
    )
    snapshot = None
    if row:
        try:
            snapshot = json.loads(row[0][0])
        except Exception:
            snapshot = None
    return {"db": db_path, "run_id": run_id, "config": snapshot}


def _summary_view(db_path: str) -> dict:
    dim = _scalar(
        db_path, "SELECT value FROM meta WHERE key = 'summary_embed_dim'", default=None
    )
    documents = _rowdicts(
        ("id", "source", "stem", "slide_count", "created_at", "updated_at"),
        _query(
            db_path,
            """
            SELECT id, source, stem, slide_count, created_at, updated_at
            FROM deck_documents ORDER BY updated_at DESC
            """,
        ),
    )
    for doc in documents:
        doc["chunk_count"] = _scalar(
            db_path, "SELECT COUNT(*) FROM deck_chunks WHERE document_id = ?",
            (doc["id"],), default=0,
        )
    return {
        "db": db_path,
        "embed_dim": int(dim) if dim and str(dim).isdigit() else None,
        "documents": documents,
    }


def _models(db_path: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT stage, COALESCE(model, '(none)') AS model, base_url,
               COUNT(*) AS count,
               MIN(latency_ms) AS min_ms, MAX(latency_ms) AS max_ms,
               COALESCE(SUM(latency_ms), 0) AS total_ms,
               COALESCE(AVG(latency_ms), 0) AS avg_ms
        FROM vision_events
        WHERE latency_ms IS NOT NULL
        GROUP BY stage, model, base_url
        ORDER BY total_ms DESC
        """,
    )
    entries = [
        {
            "stage": stage,
            "model": model,
            "base_url": base_url,
            "count": count,
            "min_ms": min_ms,
            "max_ms": max_ms,
            "total_ms": total_ms,
            "avg_ms": round(avg_ms, 1),
        }
        for stage, model, base_url, count, min_ms, max_ms, total_ms, avg_ms in rows
    ]
    return {"db": db_path, "models": entries}


def _percentile(values: list[int], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    frac = k - lo
    return values[lo] + (values[hi] - values[lo]) * frac


def _models_hist(db_path: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT stage, COALESCE(model, '(none)') AS model, base_url, latency_ms
        FROM vision_events
        WHERE latency_ms IS NOT NULL
        ORDER BY stage, model
        """,
    )
    grouped: dict[tuple, list[int]] = {}
    for stage, model, base_url, latency_ms in rows:
        grouped.setdefault((stage, model, base_url), []).append(latency_ms)
    bars = 8
    entries = []
    for (stage, model, base_url), vals in grouped.items():
        if not vals:
            continue
        vmax = max(vals)
        bucket = [sum(1 for v in vals if vmax * i / bars <= v < vmax * (i + 1) / bars)
                  for i in range(bars)]
        entries.append({
            "stage": stage,
            "model": model,
            "base_url": base_url,
            "count": len(vals),
            "min_ms": min(vals),
            "avg_ms": round(sum(vals) / len(vals), 1),
            "p50_ms": round(_percentile(vals, 0.50), 1),
            "p95_ms": round(_percentile(vals, 0.95), 1),
            "max_ms": max(vals),
            "total_ms": sum(vals),
            "hist_buckets": bucket,
            "hist_max_ms": vmax,
        })
    entries.sort(key=lambda e: -e["total_ms"])
    return {"db": db_path, "models": entries}


def _structure(db_path: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT source, page, stage, decision, error, latency_ms, ts
        FROM vision_events
        WHERE stage IN ('structure', 'format')
          AND (decision IN ('rejected', 'error') OR error IS NOT NULL)
        ORDER BY ts DESC, id DESC
        """,
    )
    rejections = [
        {
            "source": source,
            "name": Path(source).name if source else "",
            "page": page,
            "stage": stage,
            "decision": decision,
            "error": error,
            "latency_ms": latency_ms,
            "ts": ts,
        }
        for source, page, stage, decision, error, latency_ms, ts in rows
    ]
    agg = _query(
        db_path,
        """
        SELECT stage, COUNT(*) AS count, COALESCE(SUM(latency_ms), 0) AS total_ms
        FROM vision_events
        WHERE stage IN ('structure', 'format')
          AND (decision IN ('rejected', 'error') OR error IS NOT NULL)
        GROUP BY stage
        """,
    )
    return {
        "db": db_path,
        "rejections": rejections,
        "aggregates": [
            {"stage": stage, "count": count, "total_ms": total_ms}
            for stage, count, total_ms in agg
        ],
    }


def create_app(db_path: str | None = None) -> Flask:
    """Build the dashboard Flask app bound to ``db_path`` (test seam)."""
    db = str(db_path or DEFAULT_DB)
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["PTM_DB"] = db

    @app.after_request
    def _no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/overview")
    def api_overview():
        return jsonify(_overview(db))

    @app.get("/api/events")
    def api_events():
        source = request.args.get("source", "")
        run_id = request.args.get("run_id")
        run_id = int(run_id) if run_id and run_id.isdigit() else None
        return jsonify(_events(db, source, run_id))

    @app.get("/api/errors")
    def api_errors():
        return jsonify(_errors(db))

    @app.get("/api/health")
    def api_health():
        return jsonify(_health(db))

    @app.get("/api/runs")
    def api_runs():
        return jsonify(_runs(db))

    @app.get("/api/runs/<int:run_id>")
    def api_run_detail(run_id: int):
        return jsonify(_run_detail(db, run_id))

    @app.get("/api/runs/<int:run_id>/phases")
    def api_run_phases(run_id: int):
        result = _phases(db, run_id)
        result["derived"] = _derived_phases(db, run_id)
        return jsonify(result)

    @app.get("/api/runs/<int:run_id>/config")
    def api_run_config(run_id: int):
        return jsonify(_config(db, run_id))

    @app.get("/api/runs/<int:run_id>/summary")
    def api_run_summary(run_id: int):
        return jsonify(_summary_view(db))

    @app.get("/api/summary")
    def api_summary():
        return jsonify(_summary_view(db))

    @app.get("/api/models")
    def api_models():
        return jsonify(_models_hist(db))

    @app.get("/api/structure")
    def api_structure():
        return jsonify(_structure(db))

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ptm-dashboard",
        description="Read-only web dashboard for the PTM conversion log.",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="path to ptm.sqlite (default: <repo root>/ptm.sqlite)",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="host to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port to bind (default: 8080)",
    )
    args = parser.parse_args(argv)

    app = create_app(args.db)
    port = args.port
    last_error: OSError | None = None
    bound = False
    try:
        host_port = _port_or_raise(args.host, port)
    except OSError as exc:
        print(
            f"ptm-dashboard: could not bind {args.host}:{args.port} ({exc}). "
            "Try --port <free-port>.",
            file=sys.stderr,
        )
        host_port = None
        last_error = exc
    if host_port is not None:
        bound = True
        host, port = host_port
    else:
        while not bound and port < args.port + 100:
            try:
                host_port = _port_or_raise(args.host, port)
                bound = True
                host, port = host_port
            except OSError as exc:
                last_error = exc
                port += 1
    if not bound:
        print(
            f"ptm-dashboard: could not bind any port from {args.port} (last: {last_error}). "
            "Try --port <free-port>.",
            file=sys.stderr,
        )
        return 1

    print(f"Dashboard: open http://{host}:{port}", flush=True)
    if port != args.port:
        print(f"  (port {args.port} was in use; fell back to {port})", flush=True)
    print(f"  Watching {args.db} (read-only)", flush=True)
    app.run(host=host, port=port, threaded=True)
    return 0


def _port_or_raise(host: str, port: int) -> tuple[str, int]:
    """Bind-check a host:port by opening a socket, closing it immediately."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    finally:
        sock.close()
    return host, port
