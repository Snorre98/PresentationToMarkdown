const REFRESH = 2000;
let state = {
  runs: null, errors: null, events: null, phases: null, models: null,
  rag: null, structure: null, config: null,
  source: null, runId: null, tab: "convert", lastOk: Date.now(),
  engine: { running: false, base_url: "" },
  engineConfig: null, servers: null,
  files: [], outputDir: "", duplicate: false,
  ws: null, converting: false, logLines: [],
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
function badge(decision) {
  const d = (decision || "").toLowerCase();
  const cls = d === "decorative" ? "decorative" : d === "diagram" ? "diagram" : d === "text" ? "text" : d === "error" ? "err" : "";
  return '<span class="badge ' + cls + '">' + esc(decision || "\u2014") + '</span>';
}
function statusBadge(s) {
  const cls = s === "running" ? "running" : s === "error" || s === "failed" ? "err" : "ok";
  return '<span class="badge ' + cls + '">' + esc(s || "\u2014") + '</span>';
}

async function api(path, opts) {
  const r = await fetch(path, opts || {});
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  return { status: r.status, body };
}

async function load() {
  try {
    const [runs, errs, models, eng] = await Promise.all([
      api("/api/runs"), api("/api/errors"), api("/api/models"), api("/api/engine"),
    ]);
    state.runs = runs.body; state.errors = errs.body; state.models = models.body;
    state.engine.running = eng.body.running;
    state.engine.base_url = eng.body.base_url;
    if (state.engine.running) {
      const [cfg, srv] = await Promise.all([
        api("/api/engine/config"), api("/api/engine/health/servers"),
      ]);
      if (cfg.status === 200) state.engineConfig = cfg.body;
      if (srv.status === 200) state.servers = srv.body;
    }
    if (state.runId) {
      const [ev, ph, cf, st] = await Promise.all([
        api("/api/events?run_id=" + state.runId),
        api("/api/runs/" + state.runId + "/phases"),
        api("/api/runs/" + state.runId + "/config"),
        api("/api/structure"),
      ]);
      state.events = (ev.body && ev.body.events) || []; state.phases = ph.body;
      state.config = cf.body.config; state.structure = st.body;
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
  renderEnginePill();
  const h = { convert: renderConvert, runs: renderRuns, timeline: renderTimeline,
              errors: renderErrors, models: renderModels, rag: renderRag }[state.tab];
  if (h) h();
}

function renderEnginePill() {
  const pill = $("engine-pill"), btn = $("engine-btn"), stop = $("engine-stop-btn");
  if (state.engine.running) {
    pill.className = "pill running";
    pill.textContent = "\u25CF Engine running";
    btn.classList.add("hidden");
    stop.classList.remove("hidden");
    stop.onclick = stopEngineButton;
  } else {
    pill.className = "pill stopped";
    pill.textContent = "\u25CB Engine stopped";
    btn.classList.remove("hidden");
    stop.classList.add("hidden");
    stop.onclick = null;
  }
}

function renderConvert() {
  $("db").textContent = "";
  let html = '';

  if (!state.engine.running) {
    html += '<div class="cost"><h3>Engine is not running</h3><p>Start the native engine to convert files, browse folders, and open results in Finder.</p><button class="btn" id="start-engine-btn">Start engine</button></div>';
  }

  html += '<div class="section-head">Files</div>';
  html += '<div class="browser"><button class="btn ghost" id="add-files-btn">Add Files</button><button class="btn ghost" id="add-folder-btn">Add Folder</button><button class="btn ghost" id="recent-btn">Recent</button><button class="btn ghost" id="clear-files-btn">Clear</button></div>';
  html += '<div class="filelist" id="filelist">' + renderFileList() + '</div>';

  html += '<div class="section-head">Output</div>';
  html += '<div class="row"><input class="field wide" id="output-edit" placeholder="Defaults to &lt;input-folder&gt;/markdown" value="' + esc(state.outputDir) + '"><button class="btn ghost" id="browse-out-btn">Browse</button><button class="btn ghost" id="open-out-btn">Open in Finder</button></div>';

  html += '<div class="section-head">Options</div>';
  html += '<div class="checklist">';
  html += '<label><input type="checkbox" id="paper-check"> Paper layout (multi-column whitepapers)</label>';
  html += '<label><input type="checkbox" id="duplicate-check"> Duplicate if exists (keep prior output)</label>';
  html += '</div>';

  html += '<div class="section-head">AI features</div><div class="checklist" id="ai-checks"></div>';
  html += '<div class="row"><span id="server-status" class="muted"></span><button class="btn ghost" id="check-servers-btn">Check servers</button></div>';

  html += '<div class="section-head">Convert</div>';
  html += '<div class="row"><button class="btn" id="convert-btn">Convert</button><span id="convert-status" class="muted"></span></div>';
  html += '<div class="progressbar" id="file-bar" style="display:none"><div class="fill" id="file-fill"></div></div>';
  html += '<div class="progressbar" id="page-bar" style="display:none"><div class="fill page" id="page-fill"></div></div>';
  html += '<div class="section-head">Log</div><div class="logpane" id="logpane"></div>';

  $("main").innerHTML = html;
  bindConvertEvents();
  renderAiChecks();
  renderServerStatus();
  renderLog();
}

function renderFileList() {
  const files = state.files;
  if (!files.length) return '<div class="empty">Drop .pptx / .pdf files here, or use Add Files / Add Folder.</div>';
  let h = '<table><thead><tr><th>File</th><th></th></tr></thead><tbody>';
  for (let i = 0; i < files.length; i++) {
    h += '<tr><td class="mono">' + esc(files[i]) + '</td><td style="width:80px;text-align:right"><button class="btn ghost" data-rm="' + i + '">Remove</button></td></tr>';
  }
  return h + '</tbody></table>';
}

function renderAiChecks() {
  const f = state.engineConfig && state.engineConfig.features;
  if (!f) { $("ai-checks").innerHTML = '<span class="muted">Start the engine to see AI toggles.</span>'; return; }
  let h = '';
  for (const key of Object.keys(f)) {
    h += '<label><input type="checkbox" data-ai="' + esc(key) + '"' + (f[key] ? ' checked' : '') + '> ' + esc(key) + '</label>';
  }
  $("ai-checks").innerHTML = h;
  document.querySelectorAll("[data-ai]").forEach(cb => cb.onchange = () => setAiFeature(cb.dataset.ai, cb.checked));
}

function renderServerStatus() {
  const s = state.servers;
  if (!s) { $("server-status").textContent = ""; return; }
  const parts = s.servers.map(x => x.up ? '<span class="badge ok">' + esc(x.name) + '</span>' : '<span class="badge err">' + esc(x.name) + '</span>');
  $("server-status").innerHTML = (parts.length ? "Servers: " + parts.join(" ") : "AI disabled — no servers needed.");
}

function renderLog() {
  $("logpane").innerHTML = state.logLines.map(l => '<div class="' + esc(l.kind) + '">' + esc(l.message) + '</div>').join('')
    || '<div class="muted">Log appears here during conversion.</div>';
  $("logpane").scrollTop = $("logpane").scrollHeight;
}

function bindConvertEvents() {
  const fl = $("filelist");
  fl.addEventListener("dragover", e => { e.preventDefault(); fl.classList.add("drag"); });
  fl.addEventListener("dragleave", () => fl.classList.remove("drag"));
  fl.addEventListener("drop", e => {
    e.preventDefault(); fl.classList.remove("drag");
    uploadFiles([...e.dataTransfer.files]);
  });

  $("add-files-btn").onclick = () => {
    const input = document.createElement("input");
    input.type = "file"; input.accept = ".pptx,.pdf"; input.multiple = true;
    input.onchange = () => uploadFiles([...input.files]);
    input.click();
  };
  $("add-folder-btn").onclick = () => openBrowser("", "folder");
  $("recent-btn").onclick = loadRecent;
  $("clear-files-btn").onclick = () => { state.files = []; render(); };
  $("browse-out-btn").onclick = () => openBrowser("", "output");
  $("open-out-btn").onclick = async () => {
    const target = state.outputDir || (state.files[0] ? state.files[0].replace(/[^/]+$/, "") + "markdown" : "");
    if (!target) return;
    await api("/api/engine/fs/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: target }) });
  };
  $("output-edit").onchange = e => { state.outputDir = e.target.value; };
  $("paper-check").onchange = e => setPdfMode(e.target.checked ? "paper" : "slide");
  $("duplicate-check").onchange = e => { state.duplicate = e.target.checked; persistDuplicate(e.target.checked); };
  $("check-servers-btn").onclick = async () => {
    const r = await api("/api/engine/health/servers");
    if (r.status === 200) { state.servers = r.body; renderServerStatus(); }
  };
  $("convert-btn").onclick = startConvert;
  const se = $("start-engine-btn");
  if (se) se.onclick = startEngineButton;
  document.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { state.files.splice(Number(b.dataset.rm), 1); render(); });
}

async function startEngineButton() {
  const r = await api("/api/engine/start", { method: "POST" });
  if (r.body && r.body.ok) { state.engine.running = true; state.engine.base_url = r.body.base_url; load(); }
  else alert("Engine failed to start: " + ((r.body && r.body.error) || "unknown"));
}

async function stopEngineButton() {
  const r = await api("/api/engine/stop", { method: "POST" });
  if (r.status !== 200) { alert("Failed to stop the engine."); return; }
  state.engine.running = false;
  load();
}

async function uploadFiles(files) {
  if (!state.engine.running) { alert("Start the engine first."); return; }
  if (!files.length) return;
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  const r = await api("/api/engine/fs/upload", { method: "POST", body: form });
  if (r.status !== 200 || !r.body) {
    for (const f of files) addLog("err", "Upload failed for " + f.name);
    renderLog();
    return;
  }
  const seen = new Set(state.files);
  let added = 0;
  for (const f of (r.body.files || [])) {
    if (!seen.has(f.path)) { seen.add(f.path); state.files.push(f.path); added++; }
  }
  for (const err of (r.body.errors || [])) addLog("err", "Not added: " + (err.name || "unknown") + " (" + err.error + ")");
  if (added) { addLog("ok", "Added " + added + " (uploaded to the engine)"); renderLog(); }
  render();
}

async function resolveAndAdd(paths) {
  const seen = new Set(state.files);
  for (const p of paths) {
    const r = await api("/api/engine/fs/resolve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p }) });
    if (r.status !== 200 || !r.body || r.body.error) { addLog("err", "Not added: " + p); continue; }
    if (r.body.is_dir) {
      const g = await api("/api/engine/fs/glob?path=" + encodeURIComponent(r.body.path));
      for (const f of (g.body && g.body.files) || []) { if (!seen.has(f)) { seen.add(f); state.files.push(f); } }
    } else if (!seen.has(r.body.path)) {
      seen.add(r.body.path); state.files.push(r.body.path);
    }
  }
  render();
}

async function openBrowser(path, mode) {
  const r = await api("/api/engine/fs/list?path=" + encodeURIComponent(path || "/"));
  if (r.status !== 200 || !r.body || r.body.error) { alert((r.body && r.body.error) || "cannot browse"); return; }
  renderBrowser(r.body, mode);
}

function renderBrowser(dir, mode) {
  let html = '<div class="browser"><button class="btn ghost" id="up-btn">Up</button><button class="btn ghost" id="choose-here-btn">Choose this folder</button><span class="crumbs" id="bcrumbs">' + esc(dir.path) + '</span></div>';
  html += '<div class="filelist"><table><thead><tr><th>Name</th><th></th></tr></thead><tbody>';
  for (const e of dir.entries) {
    const icon = e.is_dir ? "\u25B6 " : "";
    let action = "";
    if (e.is_dir) action = '<button class="btn ghost" data-nav="' + esc(e.path) + '">Open</button>';
    else if (e.supported) action = '<button class="btn ghost" data-addfile="' + esc(e.path) + '">Add</button>';
    html += '<tr><td class="' + (e.is_dir ? "" : "mono") + '">' + icon + esc(e.name) + '</td><td style="width:140px;text-align:right">' + action + '</td></tr>';
  }
  html += '</tbody></table></div>';
  $("main").innerHTML = html;
  document.querySelectorAll("[data-nav]").forEach(b => b.onclick = () => openBrowser(b.dataset.nav, mode));
  document.querySelectorAll("[data-addfile]").forEach(b => b.onclick = () => {
    if (!state.files.includes(b.dataset.addfile)) state.files.push(b.dataset.addfile);
    render();
  });
  $("up-btn").onclick = () => openBrowser(dir.parent, mode);
  $("choose-here-btn").onclick = () => {
    if (mode === "folder") {
      api("/api/engine/fs/glob?path=" + encodeURIComponent(dir.path)).then(g => {
        const seen = new Set(state.files);
        for (const f of (g.body && g.body.files) || []) { if (!seen.has(f)) { seen.add(f); state.files.push(f); } }
        render();
      });
    } else if (mode === "output") {
      state.outputDir = dir.path;
      render();
    }
  };
}

async function loadRecent() {
  const r = await api("/api/engine/recent");
  if (r.status !== 200) return;
  const recent = (r.body.recent || []).slice(0, 10);
  if (!recent.length) { addLog("warn", "No recent files."); renderLog(); return; }
  let html = '<div class="browser"><button class="btn ghost" id="done-btn">Done</button></div><div class="filelist"><table><thead><tr><th>Recent file</th><th></th></tr></thead><tbody>';
  for (const p of recent) {
    html += '<tr><td class="mono">' + esc(p) + '</td><td style="width:140px;text-align:right"><button class="btn ghost" data-add="' + esc(p) + '">Add</button></td></tr>';
  }
  html += '</tbody></table></div>';
  $("main").innerHTML = html;
  $("done-btn").onclick = render;
  document.querySelectorAll("[data-add]").forEach(b => b.onclick = () => { if (!state.files.includes(b.dataset.add)) state.files.push(b.dataset.add); render(); });
}

async function setAiFeature(key, value) {
  await api("/api/engine/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ features: { [key]: value } }) });
  const r = await api("/api/engine/config");
  if (r.status === 200) { state.engineConfig = r.body; renderAiChecks(); }
}

async function setPdfMode(mode) {
  await api("/api/engine/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pdf_mode: mode }) });
}

async function persistDuplicate(v) {
  await api("/api/engine/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ duplicate: v }) });
}

function addLog(kind, message) {
  state.logLines.push({ kind, message });
}

function startConvert() {
  if (!state.engine.running) { alert("Start the engine first."); return; }
  if (!state.files.length) { alert("Add at least one .pptx or .pdf file."); return; }
  state.logLines = [];
  state.converting = true;
  $("convert-btn").disabled = true;
  $("convert-status").textContent = "Connecting...";
  $("file-bar").style.display = "block";
  $("page-bar").style.display = "block";

  const wsUrl = state.engine.base_url.replace(/^http/, "ws") + "/ws";
  const ws = new WebSocket(wsUrl);
  state.ws = ws;
  ws.onopen = () => {
    $("convert-status").textContent = "Converting...";
    ws.send(JSON.stringify({ type: "start", paths: state.files, output_dir: state.outputDir || null, duplicate: state.duplicate }));
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "file") {
      const pct = msg.total ? (msg.idx / msg.total * 100) : 0;
      $("file-fill").style.width = pct + "%";
      addLog("warn", "[" + msg.idx + "/" + msg.total + "] " + msg.name);
    } else if (msg.type === "page") {
      const noun = msg.name.toLowerCase().endsWith(".pptx") ? "Slide" : "Page";
      $("page-fill").style.width = (msg.total ? msg.page / msg.total * 100 : 0) + "%";
      $("page-bar").setAttribute("title", noun + " " + msg.page + "/" + msg.total);
    } else if (msg.type === "log") {
      addLog(msg.kind, msg.message);
    } else if (msg.type === "done") {
      addLog("warn", "Done: " + msg.ok + " of " + msg.total + " converted.");
      if (msg.error) addLog("err", msg.error);
      finishConvert();
    } else if (msg.type === "error") {
      addLog("err", msg.message);
      finishConvert();
    }
    renderLog();
  };
  ws.onerror = () => { addLog("err", "WebSocket error."); finishConvert(); };
  ws.onclose = () => { if (state.converting) { addLog("err", "Connection closed."); finishConvert(); } };
}

function finishConvert() {
  state.converting = false;
  $("convert-btn").disabled = false;
  $("convert-status").textContent = "";
  $("file-fill").style.width = "0%";
  $("page-fill").style.width = "0%";
  if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
  load();
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
  let rag;
  try { rag = (await api("/api/summary")).body; } catch (e) { rag = null; }
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

["convert", "runs", "timeline", "errors", "models", "rag"].forEach(t => $("tab-" + t).onclick = () => setTab(t));
load();
setInterval(load, REFRESH);
setInterval(tick, 1000);
