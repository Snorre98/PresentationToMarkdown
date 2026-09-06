# 0029. Serving control via the macos-dev-config control daemon

- Status: Accepted
- Date: 2026-09-06

## Context

Until now PresentationToMarkdown reached local LLMs through its own parallel
control path: a hardcoded `SERVERS` catalog in `converter/config.py` (ports
:8081/:8082/:8084/:8085, ollama :11434), a direct `probe()` of each OpenAI
endpoint, and a `serve_command` that shelled out to
`tools/serve.sh start <name>` in the sibling `macos-dev-config` repo. Health and
start lived outside any shared authority, and the catalog had already drifted
from the machine's actual manifest:

- `servers.conf` (which `refresh_servers_from_conf()` parsed) was deleted by the
  serving migration; the parser targeted a file that no longer exists.
- `structure-text` defaulted to `:8085`, which the fleet manifest assigns to the
  `mistral-24b` daemon — a live port collision.
- Embeddings defaulted to ollama `:11434`, but ollama is no longer a manifest
  runner; embeddings moved to a dedicated `nomic-embed` llama.cpp daemon (`:8090`).
- The `classifier`/`transcriber` were always-on LaunchAgents in
  `macos-dev-config/launchd/`, contradicting the on-demand fleet discipline.

Meanwhile `macos-dev-config` became the machine-local LLM control plane
(texteditor ADR-0033): the **control daemon** there is the sole reader of
`models.json`, the lifecycle authority (`list/status/start/stop/provision/
log/reach`, canonical contract `macos-dev-config/docs/contracts/daemon-http.md`),
and the intended front door for *every* app on the machine — the texteditor
engine is one consumer; this app is the second.

## Decision

PtM consumes local LLMs through the control daemon. The static catalog becomes
an offline fallback, never a second authority:

1. **New `converter/fleet.py`** — a small, stdlib-only, never-raising client for
   the daemon: `list_models()` (cached, 30s TTL), `status(name)`,
   `base_url(name)`, `start_command(name)`, `runner_for(base_url)`. Endpoint
   resolution chain everywhere is: env override → daemon `list` → static
   catalog fallback (`converter/config.py:_resolve`).
2. **`config.SERVERS`** stays as the fallback projection of the manifest's
   PtM-serving daemons, each entry naming its manifest daemon:
   - `transcriber` → manifest `transcriber` (`:8081`, unchanged),
   - `classifier` → manifest `classifier` (`:8082`, unchanged),
   - `summary` → manifest `text` daemon (`:8083`, Llama-3.2-3B — the model this
     pass always used; the manifest's own 1B `summary` daemon belongs to the
     writing-assistant engine),
   - `structure-text` → manifest `text` daemon (`:8083`; `:8085` was a live
     conflict with `mistral-24b`),
   - embeddings → manifest `nomic-embed` (`:8090`, llama.cpp; replaces ollama
     `:11434`).
3. **Health + start go through the daemon.** `missing_servers()` checks the
   daemon's `status` verb first, probes the endpoint directly as a fallback (a
   server started outside the daemon still reads as up), and reports the
   on-demand start command as the daemon `start` curl. The GUI health panel and
   the engine's `/api/health/servers` surface the same command.
4. **Runner identity comes from the manifest.** `lifecycle.resolve_runner()`
   resolves a base URL's runner via the daemon `list` (which exposes `runner`
   and `daemon` since texteditor ADR-0033 §4) before the static catalog, so
   memory-release paths (mlx-vlm `/unload`, ollama `keep_alive:0` — ADR-0017)
   stay correct without a hardcoded port→runner table.
5. **Dead code removed.** `parse_servers_conf`, `refresh_servers_from_conf`,
   `_default_servers_conf` (all targeting the deleted `servers.conf`) are gone.
6. **Always-on agents retired** in `macos-dev-config`: the
   `com.macosdev.classifier` and `com.macosdev.transcriber` LaunchAgents are
   deleted; those runners now start on demand via the daemon `start` verb, so
   the always-on footprint is exactly the daemon itself.

Env-var overrides (`VISION_BASE_URL`, `EMBED_BASE_URL`, `--env KEY=VALUE`, …)
are unchanged: they still take precedence over both the daemon and the catalog.

## Consequences

- **+** One authority for what runs where: PtM discovers host/port/runner from
  the same manifest the daemon enforces (lanes, port-uniqueness, pre-bind
  gate), so a catalog drift like the `:8085` collision is impossible by
  construction.
- **+** The daemon reachable at `http://127.0.0.1:9300` becomes the app's
  control surface; `DAEMON_URL` overrides it for remote/tailnet setups.
- **+** Two always-on GPU-resident agents disappear from the machine's boot
  footprint; the transcriber/classifier load on first `start`, matching the
  on-demand fleet.
- **−** When the daemon is down, behaviour is exactly the pre-migration one
  (static catalog + direct probes) — the fallback keeps working but can again
  drift from the manifest; the drift is bounded by the manifest being the
  daemon's load gate (a conflicting edit fails daemon start loudly).
- **−** PtM's summary/embeddings endpoints move (:8084→:8083, :11434→:8090);
  any persisted per-run snapshots (ADR-0022) older than this change record the
  previous endpoints.

## Alternatives considered

- **Keep the hardcoded catalog + `serve.sh` shell-out** — rejected: it is a
  second, drifting control path beside the daemon, and its `serve.sh`/`servers.conf`
  hooks are already dead (the parse target was deleted).
- **Auto-start servers via the daemon `start` verb on feature enable** —
  deferred: today's UX is report-and-run (the health panel shows the command);
  auto-start changes runtime behaviour and blocking-start latency, and is a
  separate decision.
- **Read `models.json` directly from PtM** — rejected: reintroduces a second
  manifest reader (the drift ADR-0027 exists to prevent); the daemon is the
  sole reader.
