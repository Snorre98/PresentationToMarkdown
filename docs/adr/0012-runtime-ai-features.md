# 0012. Runtime-togglable AI features with server health checks

- Status: Accepted
- Date: 2026-09-01

## Context

ADR-0002 mapped AI flags onto environment variables read at import time and
explicitly rejected "read config lazily" as out of scope. Consequently the six
AI passes (vision, classify, interpret, format, summary, structure) can only be
turned on *before* the app starts: the GUI has no AI checkboxes, and a user who
launched `main.py` must restart via `ptm-start --vision` (or export env vars) to
change AI behaviour. The only runtime-togglable feature is `PDF_MODE`, which
`PDFConverter.convert` reads lazily (the paper checkbox).

Two layers freeze the flags: the module-level boolean constants computed at
import, and the cross-module `from converter.x import Y_ENABLED` imports that
copy the value into each importer's namespace (tests must monkeypatch both
`converter.structure` *and* `converter.pdf`). On top of that, the GUI gives no
feedback about *why* an AI pass is silently falling back: the local
`mlx-vlm`/`ollama` servers must be started by hand, and a down server only
surfaces as a per-file `[WARN]` after conversion has already begun.

## Decision

Introduce a central runtime configuration module, `converter/config.py`, owning
the on/off state of every AI feature and a catalog of the servers they need.

### Feature state (lazy, mutable)

- A `Feature` registry: `key`, GUI `label`, `env_var`, `description`, `implies`
  (`classify` → `vision`), and the endpoints it needs.
- Mutable `_state: dict[str, bool]` seeded from the environment at import time,
  so `apply_ai_env` and the CLI flags keep working unchanged (ADR-0002's
  headless path is preserved).
- API: `is_enabled(key)`, `set_enabled(key, value)`, `set_many(mapping)`,
  `reset()` (re-read env), `enabled_features()`, `missing_servers(...)`, and
  `probe(base_url, timeout)`.
- The AI modules drop their module-level `*_ENABLED` constants and call
  `config.is_enabled(key)` at each gate, so a toggle takes effect on the next
  conversion with no restart. Endpoint/model constants (`*_BASE_URL`,
  `*_MODEL`, `EMBED_*`) remain environment-read (reachable via `--env`); the
  health probe resolves effective base URLs with the same documented fallback
  chain (e.g. `FORMAT_BASE_URL` → `VISION_BASE_URL` → `:8081`).

### Server catalog and health probe

- A `Server` record: `name`, `runner`, `host`, `port`, `model`, `description`,
  `base_url` (`http://{host}:{port}/v1`), and `serve_command`
  (`tools/serve.sh start {name}`).
- A **built-in** catalog seeded from the current `servers.conf` rows the AI
  passes reference (transcriber `:8081`, classifier `:8082`, ollama `:11434`),
  with an **optional runtime refresh** that re-parses the sibling
  `../macos-dev-config/servers.conf` when present (path overridable via
  `PTM_SERVERS_CONF`). Parsing is
  `name|runner|model|port|host|extra-args|description`, `#`-comments stripped.
- `probe(base_url)` issues a cheap `GET {base_url}/models` (the OpenAI-compatible
  convention every local server shares) with a short timeout; result `up`/`down`.
- `missing_servers(enabled_features)` returns the unique down servers (deduped by
  base URL) for the currently enabled set — what the GUI renders as "start this".

### GUI (block-until-up preflight)

- One checkbox per AI feature, seeded from `config` and **persisted** across
  sessions in the settings store (the same pattern as the paper checkbox).
  Checking `classify` auto-checks `vision`; unchecking `vision` auto-unchecks
  `classify`.
- A status panel, refreshed in a background thread (never during conversion),
  probes each referenced server and shows up/down.
- **Preflight gate**: before starting a conversion, the GUI computes
  `missing_servers(enabled_features)`; if non-empty it blocks with a dialog
  listing each down server and its exact `tools/serve.sh start …` command, with
  a **Retry** (re-probe) and **Cancel**. The conversion thread is not started
  until every enabled feature's servers are up (or the user cancels and disables
  the feature).
- The converter itself never probes: conversion stays deterministic and, should a
  server drop mid-run, still warns and falls back per file (the existing,
  never-fails behaviour).

### CLI

`cli_common.AI_FLAGS` / `apply_ai_env` are unchanged; they set env vars which
`config` reads at import. `--env` remains the escape hatch.

## Consequences

- AI features can be toggled at runtime from the GUI and persisted; flags become
  a thin layer over runtime state rather than the only on/off path.
- A partial reversal of ADR-0002's import-time choice: the on/off booleans
  become lazy; endpoint/model env vars stay import-time.
- Tests that monkeypatch module-level `*_ENABLED` constants must target
  `converter.config` instead.
- Conversion remains deterministic and self-contained; the probe is a GUI-layer
  concern (also available to the CLI) and never runs inside `convert`.
- The server catalog is a small built-in table plus an optional refresh from the
  sibling `macos-dev-config` repo, so `converter` gains no hard dependency on it.

## Alternatives considered

- **Keep module constants, add setters + re-export** — least churn, but the
  cross-module `from ... import` copies mean the GUI must know every module's
  attribute name; a central registry is the only clean home for the
  server→feature mapping the health check needs. Rejected.
- **Parse `servers.conf` only (no built-in)** — always in sync, but breaks when
  `macos-dev-config` is absent. Rejected in favour of built-in + optional refresh.
- **Probe servers inside `convert` and block there** — immediate feedback but
  breaks determinism, adds latency, and violates "never fails a conversion".
  Rejected; the block is a GUI preflight, not a converter behaviour.
- **Full centralization of all endpoint/model constants in `config`** — removes
  the base-URL fallback duplication entirely, but is a much wider mechanical
  refactor for no behaviour change. Rejected; only the toggles move, and the
  probe mirrors the documented fallbacks.
