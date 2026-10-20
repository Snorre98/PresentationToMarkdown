# 0063. Fleet service availability in the TUI

- Status: Accepted (identity shape amended by [0064](0064-fleet-model-identity-key-name-tags.md))
- Date: 2026-10-20
- Relates to: [0012](0012-runtime-ai-features.md),
  [0029](0029-serving-via-control-daemon.md),
  [0035](0035-audio-on-fleet-manifest.md),
  [0044](0044-nb-whisper-server-asr.md),
  [0053](0053-tui-v2-charm-and-job-contract.md),
  [0062](0062-asr-on-the-fleet-whisper-daemon.md)

## Context

The TUI is the sole user surface (ADR-0051), but its only availability display
is feature-scoped. Opening settings (`s`) calls `GET /api/health/servers`, which
walks the **enabled** features, probes each endpoint with a boolean `GET /models`
(ADR-0012/0053), and renders a `model servers` section only when that list is
non-empty. The ASR services are not part of it at all.

This leaves three gaps:

1. **Availability is a consequence of the toggles, not an answer to them.**
   With every AI feature off the section disappears; there is no way to see what
   *could* be enabled, which is exactly the question the toggles raise.
2. **Transcription is invisible.** `nb-whisper-large` (the `audio` daemon) and
   `whisper-large-v3-asr-fp16` / `whisper-large-v3-turbo-asr-fp16` (the `whisper` daemon, ADR-0062) never appear.
   Downtime surfaces only as `Audio transcription failed: …` after a job.
3. **The panel bypasses the fleet authority.** `probe()` is a direct HTTP call;
   `missing_servers()` already asks flaate first (ADR-0029), so the same panel
   can disagree with itself. flaate's batch projection `GET /status/all`
   (`up | down | starting | stopping | provisioning | unknown`) is unused, as is
   the `start` command the endpoint already returns and the TUI parses but never
   renders.

flaate is the sole manifest reader and lifecycle authority (ADR-0029). Its
contract exposes `GET /list` (every servable model with daemon/runner/host/port)
and `GET /status/all` (all states in one roundtrip). `converter/fleet.py` wraps
`list` and single-model `status`, but not the batch verb.

## Decision

### 1. Availability is projected from flaate, not probed per endpoint

Add `fleet.status_all()` (one uncached `GET /status/all`, 1.5 s timeout; the
30 s `list_models()` cache stays) and a read-only engine endpoint
`GET /api/fleet`:

```json
{
  "daemon": { "url": "http://127.0.0.1:9300", "reachable": true },
  "source": "fleet",
  "services": [
    {
      "key": "llama-3.2-3b-instruct-4bit",
      "name": "Llama 3.2 3B Instruct (4-bit)",
      "tags": ["text", "summary", "structure"],
      "daemon": "llama-3.2-3b-instruct-4bit",
      "host": "127.0.0.1",
      "port": 8083,
      "runner": "mlx-lm",
      "modelId": "mlx-community/Llama-3.2-3B-Instruct-4bit",
      "state": "up",
      "roles": ["summary", "structure-text"],
      "features": ["summary", "format", "structure"],
      "needed": true,
      "selected": false,
      "in_manifest": true,
      "start": "curl -X POST http://127.0.0.1:9300/start/llama-3.2-3b-instruct-4bit"
    }
  ],
  "other_models": 0
}
```

- `services` covers every manifest entry in manifest order. `key` is the stable
  identity, `name` is the display label, and `tags` is the fleet tag list.
  `roles` is the reverse projection of `config.SERVERS[*].tags` plus the ASR
  roles (ADR-0064), so the one Llama-3.2-3B row carries both `summary` and
  `structure-text`.
- `features` lists every feature that references the row (enabled or not).
  `needed` is true when one of them is enabled, when the row is the selected
  ASR target, or when the row is `audio` and diarization is on.
- `selected` marks the current `AUDIO_MODEL`.
- A catalogue role absent from the manifest is still emitted with
  `state: "unknown"` and `in_manifest: false`, so a renamed or removed entry is
  visible instead of silently disappearing.
- `start` is flaate's `POST /start/{key}` command — the report-and-run
  contract of ADR-0029 — not an action.
- `other_models` counts manifest entries that serve no PtM role.

When flaate is unreachable, `source` is `"static"`, `daemon.reachable` is
false, and rows are built from `config.SERVERS` + `ASR_MODELS` with the existing
direct `probe()`. The UI labels this state; no static row is ever reported `up`
without a probe.

`GET /api/health/servers` remains as the legacy feature-scoped projection and
keeps its shape, so the ADR-0053 contract and its tests do not break.

### 2. The TUI shows every PtM-consumable service, always

The settings modal's `model servers` section becomes a `fleet` section:

```text
fleet (flaate ●)                              r refresh
  ● Qwen2.5-VL 7B Instruct   up    vision · interpret · structure
  ○ Qwen2.5-VL 3B Instruct   down  classify (needed)
  ● Llama 3.2 3B Instruct    up    summary · structure · format
  ● Nomic Embed Text v1.5    up    summary
  ● Whisper Large v3         up    ASR (selected)
  ○ Whisper Large v3 Turbo   down  ASR
  ○ Audio pipeline           down  ASR nb-whisper · diarization
  4/7 up · +2 other models · as of 12:04
```

- Rows are listed **regardless of the feature toggles**; `needed` is an accent,
  not a filter. This is the "what is available to enable?" view.
- Each feature row in the settings list gains an availability glyph derived
  from its servers (e.g. `vision off ○`), so a disabled feature still shows
  whether enabling it would work. Until the first response arrives, the glyph
  is `…`.
- State glyphs: `up` ●, `down`/`unknown` ○, `starting`/`stopping`/
  `provisioning` ◌ in the warn colour. Provisioning progress is deliberately
  omitted (`status/all` does not carry bytes/total).
- The selected ASR target is marked. Both whisper models are listed on their
  shared endpoint, which makes ADR-0062's caveat visible: the sibling model can
  read `down` while the daemon is up because only one model is resident.
- The full fleet console stays `flaate-tui`; PtM shows what it can consume plus
  `+N other models`. No manifest browsing in PtM.
- Fetched on modal open and on `r`, never polled; the panel displays the
  snapshot time. Starting a service stays an operator action.

### 3. No conversion coupling

The endpoint and panel are read-only. They never gate, start, or retry a
conversion; conversion keeps warning and falling back per file (ADR-0012), and
`convert` still never probes. The blocking preflight gate retired with the GUI
(ADR-0051) and does not return.

### 4. Contract and tests

`/api/fleet` is additive; `/api/health/servers` keeps its shape. Tests cover:
the pure role/feature projection and `other_models` count, the daemon-up path
with a fake flaate, the static fallback with flaate down (no row reported `up`
without a probe), and the TUI renderer's grouping, glyphs, truncation, and the
empty/`…` states.

## Consequences

- **+** One glance answers "which AI services does flaate have up, and which
  can I turn on?" — including ASR, without running a job.
- **+** One `status/all` roundtrip replaces N sequential `/models` probes, and
  the states are flaate's (`starting`/`provisioning` included).
- **+** Toggles become informed: availability precedes need.
- **+** flaate remains the sole manifest reader; the static table is only the
  labelled offline fallback (ADR-0029).
- **−** The settings modal grows; the frame invariant (ADR-0053) needs the
  section bounded and truncated to the terminal height.
- **−** A new engine contract (`/api/fleet`), a `status_all()` client, and the
  tests above.
- **−** When flaate is down the view is a best-effort probe and can drift from
  the manifest; the label makes that explicit rather than hiding it.
- **−** PtM duplicates a sliver of flaate-tui's read surface; the boundary
  ("PtM-consumable only") must hold as the fleet grows.

## Alternatives considered

- **Keep the feature-scoped panel and add ASR rows** — smallest change, but it
  still cannot show what is available with AI off, and it keeps the direct
  probes. Rejected.
- **Have the TUI talk to flaate directly** — the TUI would need `DAEMON_URL`,
  the role map, and the env-override precedence the engine already owns; two
  clients in one app. Rejected.
- **Show the full fleet manifest in PtM** — duplicates `flaate-tui` and turns
  the settings modal into a fleet console. Rejected; `+N other models` points at
  the real console.
- **Auto-start needed servers when a feature is enabled** — blocking (60 s),
  changes runtime behaviour, and is the decision ADR-0029 explicitly deferred.
  Rejected for this ADR; a manual `start` action is a possible follow-up.
- **Poll availability in the background** — adds traffic for a settings-time
  question; the watch socket is for jobs. Rejected.
