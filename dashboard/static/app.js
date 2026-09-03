const REFRESH = 2000;
let state = {
  runs: null, errors: null, events: null, phases: null, models: null,
  rag: null, structure: null, config: null,
  source: null, runId: null, tab: "runs", lastOk: Date.now(),
};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const msToLabel = (ms) => {
  if (ms == null) return "";
  if (ms < 1000) return ms + " ms";
  if (ms < 60000) return (ms / 1000).toFixed(1) + " s";
  const m = Math.floor(ms / 60000), s = Math.round((ms % 60000) / 1000);
  return m + "m " + s + "s";
};
const tsLabel = (t) => {
  if (!t) return "";
  try { return new Date(t).toLocaleString(); } catch (e) { return t; }
};
const clip = (s) => (!s ? "" : (s.length > 400 ? s.slice(0, 400) + "\u2026" : s));
function badge(decision) {
  const d = (decision || "").toLowerCase();
  const cls = d === "decorative" ? "decorative" : d === "diagram" ? "diagram" : d === "text" ? "text" : d === "error" ? "err" : "";
  return '<span class="badge ' + cls + '">' + esc(decision || "\u2014") + '</span>';
}
function statusBadge(s) {
  const cls = s === "running" ? "running" : s === "error" || s === "failed" ? "err" : "ok";
  return '<span class="badge ' + cls + '">' + esc(s || "\u2014") + '</span>';
}

async function load() {
  try {
    const [runs, errs, models] = await Promise.all([
      fetch("/api/runs").then(r => r.json()),
      fetch("/api/errors").then(r => r.json()),
      fetch("/api/models").then(r => r.json()),
    ]);
    state.runs = runs; state.errors = errs; state.models = models;
    if (state.runId) {
      const [ev, ph, cf, st] = await Promise.all([
        fetch("/api/events?run_id=" + state.runId).then(r => r.json()),
        fetch("/api/runs/" + state.runId + "/phases").then(r => r.json()),
        fetch("/api/runs/" + state.runId + "/config").then(r => r.json()),
        fetch("/api/structure").then(r => r.json()),
      ]);
      state.events = ev.events || []; state.phases = ph;
      state.config = cf.config; state.structure = st;
    }
    state.lastOk = Date.now();
    render();
  } catch (e) { tick(); }
}

function tick() {
  const age = Math.round((Date.now() - state.lastOk) / 1000);
  $("stale").textContent = age > 3 ? "stale (" + age + "s)" : "live";
  $("dot").className = "dot" + (age > 3 ? " stale" : "");
}

function setTab(t) {
  state.tab = t;
  document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
  $("tab-" + t).classList.add("active");
  render();
}

function render() {
  if (state.tab === "runs") renderRuns();
  else if (state.tab === "timeline") renderTimeline();
  else if (state.tab === "errors") renderErrors();
  else if (state.tab === "models") renderModels();
  else if (state.tab === "rag") renderRag();
}

function renderRuns() {
  const runs = (state.runs && state.runs.runs) || [];
  $("db").textContent = (state.runs && state.runs.db) || "";
  let html = '<div class="stat"><div><div class="k">Runs</div><div class="v">' + runs.length + '</div></div><div><div class="k">Events</div><div class="v">' + ((state.runs && state.runs.runs.reduce((a, r) => a + r.events, 0)) || 0) + '</div></div></div>';
  if (!runs.length) { html += '<div class="empty">No runs yet. Run a conversion to populate the log (ADR-0022).</div>'; }
  else {
    html += '<table><thead><tr><th>Run</th><th>Name</th><th>Status</th><th>Duration</th><th>Events</th><th>Errors</th><th>Started</th></tr></thead><tbody>';
    for (const r of runs) {
      html += '<tr class="clickable" data-run="' + r.id + '"><td class="mono">#' + r.id + '</td><td class="name">' + esc(r.name) + '<div class="muted mono" style="font-size:11px">' + esc(r.source) + '</div></td><td>' + statusBadge(r.status) + '</td><td>' + msToLabel(r.duration_ms) + '</td><td>' + r.events + '</td><td>' + r.errors + '</td><td class="muted">' + tsLabel(r.ts) + '</td></tr>';
    }
    html += '</tbody></table>';
  }
  $("main").innerHTML = html;
  document.querySelectorAll("tr.clickable").forEach(r => r.onclick = () => { state.runId = Number(r.dataset.run); setTab("timeline"); load(); });
}

function renderTimeline() {
  $("db").textContent = (state.runs && state.runs.db) || "";
  let html = '<span class="back" onclick="setTab(\'runs\')">\u2190 Runs</span>';
  const runs = (state.runs && state.runs.runs) || [];
  if (!runs.length) { $("main").innerHTML = html + '<div class="empty">No runs yet.</div>'; return; }
  if (state.runId == null) state.runId = runs[0].id;
  html += '<div style="margin:8px 0 16px"><select id="run-select">';
  for (const r of runs) {
    html += '<option value="' + r.id + '"' + (r.id === state.runId ? " selected" : "") + '>#' + r.id + ' ' + esc(r.name) + '</option>';
  }
  html += '</select></div>';
  $("main").innerHTML = html;
  $("run-select").onchange = (e) => { state.runId = Number(e.target.value); load(); };

  if (state.config) {
    const c = state.config;
    html = '<div class="page-head">Configuration</div><div class="kv">';
    html += '<div><span class="k">PDF_MODE</span></div><div class="v">' + esc(c.pdf_mode) + '</div>';
    for (const key of Object.keys(c.features || {})) {
      const on = c.features[key];
      html += '<div><span class="k">' + esc(key) + '</span></div><div class="v">' + (on ? '<span class="badge ok">on</span>' : '<span class="badge">off</span>') + '</div>';
      if (on && c.passes && c.passes[key]) {
        const p = c.passes[key];
        html += '<div><span class="k">&nbsp; model</span></div><div class="v">' + esc(p.model || "\u2014") + '</div>';
        for (const ep of p.endpoints || []) {
          html += '<div><span class="k">&nbsp; ' + esc(ep.server) + '</span></div><div class="v">' + esc(ep.base_url) + '</div>';
        }
      }
    }
    if (c.embed_model) html += '<div><span class="k">embed_model</span></div><div class="v">' + esc(c.embed_model) + '</div>';
    if (c.missing_servers && c.missing_servers.length) {
      html += '<div><span class="k">down servers</span></div><div class="v">' + c.missing_servers.map(s => '<span class="badge err">' + esc(s[0]) + '</span> ' + esc(s[1]) + ' (' + esc(s[2]) + ')').join('<br>') + '</div>';
    }
    html += '</div>';
  }

  if (state.phases) {
    const ph = (state.phases.phases || []);
    const derived = (state.phases.derived || []);
    html += '<div class="page-head">Phases</div>';
    for (const p of ph) {
      html += '<div class="phase ' + esc(p.status) + '"><div class="mono" style="width:90px">' + esc(p.phase) + '</div><div class="bar"><div class="fill" style="width:100%"></div></div><div style="width:100px">' + msToLabel(p.duration_ms) + '</div><div class="muted" style="width:130px">' + statusBadge(p.status) + '</div></div>';
    }
    for (const d of derived) {
      html += '<div class="phase done"><div class="mono" style="width:90px">' + esc(d.phase) + ' *</div><div class="bar"><div class="fill" style="width:100%"></div></div><div style="width:100px">' + msToLabel(d.latency_ms) + '</div><div class="muted" style="width:130px">' + d.count + ' events</div></div>';
    }
  }

  if (state.structure && state.structure.rejections && state.structure.rejections.length) {
    const agg = state.structure.aggregates || [];
    const totalMs = agg.reduce((a, x) => a + x.total_ms, 0);
    html += '<div class="page-head">Cost driver</div><div class="cost"><h3>Structure/format rejected pages — ' + state.structure.rejections.length + ' rejections, ' + msToLabel(totalMs) + ' spent</h3>';
    for (const a of agg) html += '<div class="mono">' + esc(a.stage) + ': ' + a.count + ' rejects (' + msToLabel(a.total_ms) + ')</div>';
    html += '</div>';
  }

  const evs = state.events || [];
  html += '<div class="page-head">Events</div>';
  if (!evs.length) { html += '<div class="empty">No events for this run yet.</div>'; }
  else {
    let page = null;
    for (const e of evs) {
      if (e.page !== page) { page = e.page; html += '<div class="page-head">Page ' + (e.page == null ? "\u2014" : e.page) + '</div>'; }
      let row = '<table style="margin-bottom:12px"><thead><tr><th style="width:100px">Stage</th><th>Model</th><th>Decision</th><th style="width:90px">Latency</th><th style="width:150px">Time</th></tr></thead><tbody><tr><td class="mono">' + esc(e.stage) + '</td><td class="muted mono">' + esc(e.model || "\u2014") + '</td><td>' + badge(e.decision) + '</td><td>' + msToLabel(e.latency_ms) + '</td><td class="muted">' + tsLabel(e.ts) + '</td></tr>';
      if (e.error) row += '<tr><td colspan="5"><div class="badge err">error</div><pre>' + esc(e.error) + '</pre></td></tr>';
      if (e.markdown) row += '<tr><td colspan="5"><pre class="clip" onclick="this.classList.toggle(\'clip\')">' + esc(e.markdown) + '</pre></td></tr>';
      row += '</tbody></table>';
      html += row;
    }
  }
  $("main").innerHTML = html;
}

function renderErrors() {
  $("db").textContent = (state.runs && state.runs.db) || "";
  const errs = (state.errors && state.errors.errors) || [];
  let html = "";
  if (state.structure && state.structure.rejections && state.structure.rejections.length) {
    html += '<div class="page-head">Structure/format rejections (cost driver)</div>';
    html += '<table><thead><tr><th>Time</th><th>Source</th><th>Page</th><th>Stage</th><th>Decision</th><th>Reason</th></tr></thead><tbody>';
    for (const r of state.structure.rejections) {
      html += '<tr><td class="muted">' + tsLabel(r.ts) + '</td><td class="name">' + esc(r.name || r.source) + '</td><td>' + (r.page != null ? r.page : "\u2014") + '</td><td class="mono">' + esc(r.stage) + '</td><td><span class="badge rejected">' + esc(r.decision) + '</span></td><td><span class="mono">' + esc(r.error) + '</span></td></tr>';
    }
    html += '</tbody></table><div class="page-head">All errors</div>';
  }
  if (!errs.length) { html += '<div class="empty">No errors recorded.</div>'; }
  else {
    html += '<table><thead><tr><th>Time</th><th>Source</th><th>Page</th><th>Stage</th><th>Error</th></tr></thead><tbody>';
    for (const e of errs) {
      html += '<tr><td class="muted">' + tsLabel(e.ts) + '</td><td class="name">' + esc(e.name || e.source) + '</td><td>' + (e.page != null ? e.page : "\u2014") + '</td><td class="mono">' + esc(e.stage) + '</td><td><span class="badge err">' + esc(e.error) + '</span></td></tr>';
    }
    html += '</tbody></table>';
  }
  $("main").innerHTML = html;
}

function renderModels() {
  $("db").textContent = (state.runs && state.runs.db) || "";
  const models = (state.models && state.models.models) || [];
  if (!models.length) { $("main").innerHTML = '<div class="empty">No timed model calls yet.</div>'; return; }
  let html = '<table><thead><tr><th>Stage</th><th>Model</th><th>Base URL</th><th>Count</th><th>Min</th><th>Avg</th><th>p50</th><th>p95</th><th>Max</th><th>Total</th><th>Distribution</th></tr></thead><tbody>';
  for (const m of models) {
    const buckets = m.hist_buckets || [];
    let hist = '<div class="hist">';
    const hmax = Math.max(...buckets, 1);
    for (const b of buckets) hist += '<div class="b" style="height:' + (100 * b / hmax) + '%"></div>';
    hist += '</div>';
    html += '<tr><td class="mono">' + esc(m.stage) + '</td><td class="mono">' + esc(m.model) + '</td><td class="muted mono">' + esc(m.base_url || "\u2014") + '</td><td>' + m.count + '</td><td>' + msToLabel(m.min_ms) + '</td><td>' + m.avg_ms + ' ms</td><td>' + m.p50_ms + ' ms</td><td>' + m.p95_ms + ' ms</td><td>' + msToLabel(m.max_ms) + '</td><td>' + msToLabel(m.total_ms) + '</td><td style="width:180px">' + hist + '</td></tr>';
  }
  html += '</tbody></table>';
  $("main").innerHTML = html;
}

async function renderRag() {
  $("db").textContent = (state.runs && state.runs.db) || "";
  try {
    const rag = await fetch("/api/summary").then(r => r.json());
    state.rag = rag;
  } catch (e) { state.rag = null; }
  const rag = state.rag;
  if (!rag) { $("main").innerHTML = '<div class="empty">RAG unavailable.</div>'; return; }
  let html = '<div class="stat"><div><div class="k">Embedding dim</div><div class="v">' + (rag.embed_dim != null ? rag.embed_dim : "\u2014") + '</div></div><div><div class="k">Documents</div><div class="v">' + (rag.documents || []).length + '</div></div></div>';
  const docs = rag.documents || [];
  if (!docs.length) { html += '<div class="empty">No RAG index yet — run a conversion with the summary pass (SUMMARY_ENABLED).</div>'; }
  else {
    html += '<table><thead><tr><th>Document</th><th>Stem</th><th>Slides</th><th>Chunks</th><th>Updated</th></tr></thead><tbody>';
    for (const d of docs) {
      html += '<tr><td class="name">' + esc(d.source) + '</td><td class="mono">' + esc(d.stem) + '</td><td>' + d.slide_count + '</td><td>' + d.chunk_count + '</td><td class="muted">' + tsLabel(d.updated_at) + '</td></tr>';
    }
    html += '</tbody></table>';
  }
  $("main").innerHTML = html;
}

["runs", "timeline", "errors", "models", "rag"].forEach(t => $("tab-" + t).onclick = () => setTab(t));
load();
setInterval(load, REFRESH);
setInterval(tick, 1000);
