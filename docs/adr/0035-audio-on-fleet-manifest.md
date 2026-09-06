# 0035. Audio server on the fleet manifest

- Status: Accepted
- Date: 2026-09-07

## Context

After ADR-0029 the fleet manifest (`macos-dev-config/models.json`) is the
machine-local authority for every ML/LLM service, and the control daemon its
sole lifecycle owner. The audio server (`scripts/audio_server.py` — WPE
dereverb, DeepFilterNet enhance, SepFormer isolate, pyannote diarize) remained
the last standalone service: PtM's own `audio_serve.sh` lifecycle, its own
pidfile/log, and a hardcoded default port.

That hardcoding collided with the manifest in two places:

- The audio server defaulted to `:8083`, which the manifest assigns to the
  `text` daemon — the very daemon that serves PtM's `summary` and
  `structure-text` passes since ADR-0029. While the audio server was up, a
  daemon-driven `start text` could never succeed (409 `port-in-use`).
- The web engine (`engine.py`, the web GUI's native backend, ADR-0025) defaulted
  to `:8090`, which the manifest assigns to the `nomic-embed` llama.cpp daemon —
  the embeddings endpoint the summary pass uses (ADR-0021/0029).

Both collisions were live: with the daemon down (its LaunchAgent was never
installed) nothing caught them, and the health panel correctly reported
`summary`/`structure-text`/`nomic-embed` down — their ports were held by PtM's
own audio server and engine.

## Decision

Every ML/LLM service PtM runs is registered on the fleet manifest, and the
audio server becomes a first-class manifest daemon like any other:

1. **`audio` daemon in the manifest** (`macos-dev-config/models.json`):
   `{name: audio, runner: delegate, delegate: serve-audio.sh, host:
   127.0.0.1, port: 8089}` — a free port inside the manifest's 808x range. The
   model entry carries `source.kind: "delegate"`, a new kind added to the
   schema's enum (`hf | gguf | needle | delegate`) and to
   `internal/fleetdaemon`'s embedded copy; `modelField` already yields `""` for
   it, which the delegate branch of `serve.sh` needs.
2. **`tools/serve-audio.sh`** (macos-dev-config): a delegate wrapper in the
   `serve-needle.sh` mould. It launches PtM's `scripts/audio_server.py` with the
   isolated `~/tools/audio-env` venv, honors the daemon's `HOST`/`PORT`
   per-invocation seam, resolves `HF_TOKEN` from PtM's `.env`, and logs to
   `var/serve-audio.log`.
3. **`GET /health` on the audio server** — the daemon's `healthy()` probe
   (`/health`, `/v1/models`, `/api/tags`) needs a GET endpoint the audio server
   did not have.
4. **PtM client resolution** (`converter/audio.py`): `AUDIO_DIARIZE_BASE_URL`
   becomes env override → daemon `list` projection of `audio` → static fallback
   `http://127.0.0.1:8089/v1`, mirroring the AI-pass chain (ADR-0029).
5. **`scripts/audio_serve.sh`** keeps only the non-manifest roles: venv
   bootstrap (`install`) and the no-PyTorch test stub (`stub-*`). Real
   start/stop/status/log proxy the daemon's verbs; the `com.ptm.audio`
   LaunchAgent (launchd-install/uninstall) is retired — the fleet daemon's
   agent is the always-on authority.
6. **Web engine moves off `:8090`** — it is an app backend, not an ML service,
   so it stays off-manifest, but its default binds `:9091` (next to the
   dashboard's `:9090`), freeing the manifest's `nomic-embed` port.

Env overrides (`AUDIO_DIARIZE_BASE_URL`, `--env KEY=VALUE`) are unchanged: they
still take precedence over the daemon and the catalog.

## Consequences

- **+** The audio server gets the manifest's port-uniqueness and pre-bind gates,
  so the `:8083`-vs-`text` collision is impossible by construction.
- **+** One lifecycle authority for every ML service: `audio_serve.sh start`
  now blocks on the daemon's `start` (60s health bound) and the fleetdaemon's
  always-on LaunchAgent covers reboots.
- **+** The daemon's `/list` exposes `audio` to any consumer (the same contract
  PtM's `fleet.py` already parses), so the transcription pipeline degrades the
  same way every other pass does when a server is down — `[WARN]`, never fail.
- **−** Audio's endpoint moves `:8083` → `:8089`; the engine's default moves
  `:8090` → `:9091`. Any persisted run snapshots or scripts referencing the old
  ports must be updated.
- **−** Running the full audio pipeline now requires the control daemon to be
  up (as does any other manifest-managed pass).

## Alternatives considered

- **Remap the manifest's `text`/`nomic-embed` via `SERVE_PORT_*`** — rejected:
  bends the manifest around two PtM defaults instead of registering PtM's
  services; the collision recurs whenever the audio server or engine restarts
  on its own default.
- **Keep the audio server standalone under `audio_serve.sh`** — rejected: it is
  the exact drift/duplication ADR-0029 exists to remove, and it cannot discover
  the manifest's port assignments.
- **Register the engine on the manifest too** — rejected: it is not an ML/LLM
  serving daemon; the manifest is for model runners, and the engine needs no
  daemon-managed lifecycle.