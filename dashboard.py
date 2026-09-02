"""Read-only web dashboard for the PresentationToMarkdown conversion log.

Serves a single-page, auto-refreshing view of the ``ptm.sqlite`` database that
:mod:`converter.logstore` writes while conversions run (see ADR-0014). It opens
the database READ-ONLY and never imports ``converter``, so it cannot interfere
with a running conversion.

Usage::

    ./.venv/bin/python dashboard.py            # default ptm.sqlite, port 8080
    ./.venv/bin/python dashboard.py --db /path/to/ptm.sqlite --port 9000
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_DB = Path(__file__).resolve().parent / "ptm.sqlite"

_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PTM Dashboard</title>
<style>
:root{--bg:#0f1115;--panel:#181b22;--border:#262b36;--text:#d8dee9;--muted:#8a94a6;--accent:#4f8cff;--ok:#3fb950;--err:#f85149;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text)}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;border-bottom:1px solid var(--border);background:var(--panel);position:sticky;top:0;z-index:10}
header h1{font-size:16px;margin:0;font-weight:600}
.meta{margin-left:auto;color:var(--muted);font-size:12px;display:flex;gap:16px;align-items:center}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok)}
.dot.stale{background:var(--err)}
nav{display:flex;gap:4px;padding:10px 20px 0}
nav button{background:none;border:1px solid transparent;color:var(--muted);padding:8px 14px;cursor:pointer;border-radius:6px 6px 0 0;font-size:14px}
nav button.active{color:var(--text);border-color:var(--border);border-bottom-color:var(--bg);background:var(--bg);font-weight:600}
main{padding:20px;max-width:1200px;margin:0 auto}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:top}
th{background:#1c2027;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:none}
tbody tr.clickable{cursor:pointer}
tbody tr.clickable:hover{background:#20252e}
.name{font-weight:600;color:var(--text)}
.muted{color:var(--muted)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:#262b36;color:var(--muted);border:1px solid var(--border)}
.badge.decorative{color:#8a94a6}
.badge.diagram{color:#4f8cff}
.badge.text{color:#3fb950}
.badge.err{color:#f85149}
.page-head{font-weight:700;color:var(--accent);margin:24px 0 8px;font-size:15px}
.page-head:first-child{margin-top:0}
pre{white-space:pre-wrap;word-break:break-word;background:#0c0e12;border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:12px;max-height:220px;overflow:auto;cursor:pointer;margin:4px 0 0}
pre.clip{overflow:hidden;max-height:64px;position:relative}
pre.clip::after{content:"\25BC click to expand";position:absolute;right:8px;bottom:4px;color:var(--accent);font-size:11px}
.empty{padding:40px;text-align:center;color:var(--muted)}
.back{color:var(--accent);cursor:pointer;margin-bottom:12px;display:inline-block}
select,button.small{background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 10px}
.stat{display:flex;gap:24px;margin-bottom:16px}
.stat div{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px 16px}
.stat .k{color:var(--muted);font-size:12px}
.stat .v{font-size:20px;font-weight:700}
</style>
</head>
<body>
<header>
  <h1>PTM Dashboard</h1>
  <div class="meta">
    <span id="db" class="muted mono"></span>
    <span><span class="dot" id="dot"></span> <span id="stale"></span></span>
  </div>
</header>
<nav>
  <button id="tab-overview" class="active">Overview</button>
  <button id="tab-timeline">Timeline</button>
  <button id="tab-errors">Errors</button>
</nav>
<main id="main"></main>
<script>
const REFRESH = 2000;
let state = {overview:null, errors:null, events:null, source:null, tab:"overview", lastOk:Date.now()};
const $ = (id)=>document.getElementById(id);
const esc = (s)=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const msToLabel = (ms)=>{
  if(ms==null) return "";
  if(ms<1000) return ms+" ms";
  if(ms<60000) return (ms/1000).toFixed(1)+" s";
  const m=Math.floor(ms/60000), s=Math.round((ms%60000)/1000);
  return m+"m "+s+"s";
};
const tsLabel = (t)=>{
  if(!t) return "";
  try{return new Date(t).toLocaleString();}catch(e){return t;}
};
const clip = (s)=>{
  if(!s) return "";
  return s.length>400 ? s.slice(0,400)+"\u2026" : s;
};
function badge(decision){
  const d=(decision||"").toLowerCase();
  const cls = d==="decorative"?"decorative":d==="diagram"?"diagram":d==="text"?"text":d==="error"?"err":"";
  return '<span class="badge '+cls+'">'+esc(decision||"\u2014")+'</span>';
}
async function load(){
  try{
    const [ov,er] = await Promise.all([
      fetch("/api/overview").then(r=>r.json()),
      fetch("/api/errors").then(r=>r.json())
    ]);
    state.overview=ov; state.errors=er;
    if(state.source){
      const ev = await fetch("/api/events?source="+encodeURIComponent(state.source)).then(r=>r.json());
      state.events=ev.events||[];
    }
    state.lastOk=Date.now();
    render();
  }catch(e){ tick(); }
}
function tick(){
  const age=Math.round((Date.now()-state.lastOk)/1000);
  $("stale").textContent = age>3 ? "stale ("+age+"s)" : "live";
  $("dot").className = "dot"+(age>3?" stale":"");
}
function setTab(t){ state.tab=t; document.querySelectorAll("nav button").forEach(b=>b.classList.remove("active")); $("tab-"+t).classList.add("active"); render(); }
function render(){
  if(state.tab==="overview") renderOverview();
  else if(state.tab==="timeline") renderTimeline();
  else renderErrors();
}
function renderOverview(){
  const ov=state.overview||{sources:[],total_events:0};
  $("db").textContent = ov.db||"";
  let html='<div class="stat"><div><div class="k">Events</div><div class="v">'+(ov.total_events||0)+'</div></div><div><div class="k">Sources</div><div class="v">'+(ov.sources||[]).length+'</div></div><div><div class="k">Errors</div><div class="v">'+(state.errors&&state.errors.errors?state.errors.errors.length:0)+'</div></div></div>';
  if(!ov.sources||!ov.sources.length){ html+='<div class="empty">No events yet. Run a conversion with AI features to populate the log.</div>'; }
  else{
    html+='<table><thead><tr><th>Source</th><th>Events</th><th>Max page</th><th>Total latency</th><th>Last event</th></tr></thead><tbody>';
    for(const s of ov.sources){
      html+='<tr class="clickable" data-source="'+esc(s.source)+'"><td class="name">'+esc(s.name)+'<div class="muted mono" style="font-size:11px">'+esc(s.source)+'</div></td><td>'+s.total_events+'</td><td>'+(s.max_page!=null?s.max_page:"\u2014")+'</td><td>'+msToLabel(s.total_latency_ms)+'</td><td class="muted">'+tsLabel(s.last_ts)+'</td></tr>';
    }
    html+='</tbody></table>';
  }
  $("main").innerHTML=html;
  document.querySelectorAll("tr.clickable").forEach(r=>r.onclick=()=>{ state.source=r.dataset.source; setTab("timeline"); });
}
function renderTimeline(){
  $("db").textContent = (state.overview&&state.overview.db)||"";
  if(!state.overview||!state.overview.sources||!state.overview.sources.length){
    $("main").innerHTML='<div class="empty">No sources yet.</div>'; return;
  }
  let html='<span class="back" onclick="setTab(\'overview\')">\u2190 Overview</span>';
  const srcs=state.overview.sources;
  if(state.source==null) state.source=srcs[0].source;
  html+='<div style="margin:8px 0 16px"><select id="src-select">';
  for(const s of srcs){
    html+='<option value="'+esc(s.source)+'"'+(s.source===state.source?" selected":"")+'>'+esc(s.name)+'</option>';
  }
  html+='</select></div>';
  $("main").innerHTML=html;
  $("src-select").onchange=(e)=>{ state.source=e.target.value; load(); };
  const evs=state.events||[];
  if(!evs.length){ $("main").innerHTML+='<div class="empty">No events for this source yet.</div>'; return; }
  let page=null;
  for(const e of evs){
    if(e.page!==page){ page=e.page; $("main").innerHTML+='<div class="page-head">Page '+(e.page==null?"\u2014":e.page)+'</div>'; }
    let row='<table style="margin-bottom:16px"><thead><tr><th style="width:110px">Stage</th><th>Model</th><th>Decision</th><th style="width:90px">Latency</th><th style="width:150px">Time</th></tr></thead><tbody><tr><td class="mono">'+esc(e.stage)+'</td><td class="muted mono">'+esc(e.model||"\u2014")+'</td><td>'+badge(e.decision)+'</td><td>'+msToLabel(e.latency_ms)+'</td><td class="muted">'+tsLabel(e.ts)+'</td></tr>';
    if(e.error) row+='<tr><td colspan="5"><div class="badge err">error</div><pre>'+esc(e.error)+'</pre></td></tr>';
    if(e.markdown) row+='<tr><td colspan="5"><pre class="clip" onclick="this.classList.toggle(\'clip\')">'+esc(e.markdown)+'</pre></td></tr>';
    row+='</tbody></table>';
    $("main").innerHTML+=row;
  }
}
function renderErrors(){
  $("db").textContent = (state.overview&&state.overview.db)||"";
  const errs=(state.errors&&state.errors.errors)||[];
  if(!errs.length){ $("main").innerHTML='<div class="empty">No errors recorded.</div>'; return; }
  let html='<table><thead><tr><th>Time</th><th>Source</th><th>Page</th><th>Stage</th><th>Error</th></tr></thead><tbody>';
  for(const e of errs){
    html+='<tr><td class="muted">'+tsLabel(e.ts)+'</td><td class="name">'+esc(e.name||e.source)+'</td><td>'+(e.page!=null?e.page:"\u2014")+'</td><td class="mono">'+esc(e.stage)+'</td><td><span class="badge err">'+esc(e.error)+'</span></td></tr>';
  }
  html+='</tbody></table>';
  $("main").innerHTML=html;
}
$("tab-overview").onclick=()=>setTab("overview");
$("tab-timeline").onclick=()=>setTab("timeline");
$("tab-errors").onclick=()=>setTab("errors");
load();
setInterval(load, REFRESH);
setInterval(tick, 1000);
</script>
</body>
</html>
"""


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


def _overview(db_path: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT source,
               COUNT(*)                        AS total_events,
               MAX(page)                       AS max_page,
               COALESCE(SUM(latency_ms), 0)    AS total_latency_ms,
               MAX(ts)                         AS last_ts
        FROM vision_events
        WHERE source IS NOT NULL AND source != ''
        GROUP BY source
        """,
    )
    recents = _query(
        db_path, "SELECT path FROM recent_files ORDER BY last_used DESC"
    )
    recent_order = {path: idx for idx, (path,) in enumerate(recents)}

    def sort_key(row: tuple) -> tuple:
        source = row[0]
        idx = recent_order.get(source)
        if idx is not None:
            return (0, idx)
        return (1, 0)

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
    total = _query(db_path, "SELECT COUNT(*) FROM vision_events")
    return {
        "db": db_path,
        "total_events": total[0][0] if total else 0,
        "sources": sources,
    }


def _events(db_path: str, source: str) -> dict:
    rows = _query(
        db_path,
        """
        SELECT id, ts, page, image_ref, stage, model, decision, latency_ms,
               markdown, error, base_url
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
        }
        for id_, ts, page, image_ref, stage, model, decision, latency_ms, markdown, error, base_url in rows
    ]
    return {"db": db_path, "source": source, "events": events}


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
    total = _query(db_path, "SELECT COUNT(*) FROM vision_events")
    return {
        "ok": True,
        "db": db_path,
        "total_events": total[0][0] if total else 0,
    }


def make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class bound to ``db_path`` (test seam)."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: dict | str, content_type: str, status: int = 200) -> None:
            body = payload if isinstance(payload, str) else json.dumps(payload)
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/" or path == "/index.html":
                self._send(_HTML, "text/html; charset=utf-8")
            elif path == "/api/overview":
                self._send(_overview(db_path), "application/json")
            elif path == "/api/events":
                source = query.get("source", [""])[0]
                self._send(_events(db_path, source), "application/json")
            elif path == "/api/errors":
                self._send(_errors(db_path), "application/json")
            elif path == "/api/health":
                self._send(_health(db_path), "application/json")
            else:
                self._send('{"error": "not found"}', "application/json", status=404)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    return Handler


def serve(db_path: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Create (but do not start) the dashboard server (test seam)."""
    server = ThreadingHTTPServer((host, port), make_handler(db_path))
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard",
        description="Read-only web dashboard for the PTM conversion log.",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="path to ptm.sqlite (default: <repo root>/ptm.sqlite)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port to bind (default: 8080)",
    )
    args = parser.parse_args(argv)

    server = None
    port = args.port
    last_error: OSError | None = None
    while server is None and port < args.port + 100:
        try:
            server = serve(args.db, DEFAULT_HOST, port)
        except OSError as exc:
            last_error = exc
            port += 1
    if server is None:
        print(
            f"dashboard: could not bind any port from {args.port} (last: {last_error}). "
            "Try --port <free-port>.",
            file=sys.stderr,
        )
        return 1

    print(f"Dashboard: open http://{DEFAULT_HOST}:{port}", flush=True)
    if port != args.port:
        print(f"  (port {args.port} was in use; fell back to {port})", flush=True)
    print(f"  Watching {args.db} (read-only)", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nDashboard: shutting down.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
