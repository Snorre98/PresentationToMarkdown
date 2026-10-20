# 0064. Fleet model identity in PtM — key, display name, tags

- Status: Accepted (amended by [0065](0065-ai-capability-labels-and-requirements.md): only the resolved model per role is a PtM server row)
- Date: 2026-10-20
- Relates to: [0029](0029-serving-via-control-daemon.md),
  [0035](0035-audio-on-fleet-manifest.md),
  [0062](0062-asr-on-the-fleet-whisper-daemon.md),
  [0063](0063-fleet-service-availability-in-tui.md)
- Adopts: flaate ADR-0007 (model key / display name / tags)

> **Amended by [0065](0065-ai-capability-labels-and-requirements.md):** a role's
> tag matches are resolved to the first manifest entry; losing matches are
> alternatives, not additional PtM server rows. User-facing role names come from
> `Server.label`, and `nomic-embed` is renamed `embeddings`.

## Context

flaate ADR-0007 split the fleet manifest's overloaded `Model.name` into a
stable wire `key`, a human display `name` (the actual model name), and free-form
`tags`. PtM still assumed:

- `converter.fleet.list_models()` keyed by `name`;
- `config.SERVERS[*].daemon` held a manifest model name;
- `fleetview` derived PtM roles by mapping `config.SERVERS[*].daemon_name`;
- the TUI fleet panel rendered the manifest model name (so it showed `text`,
  `transcriber`, … instead of the actual model);
- `AUDIO_MODEL` used the short whisper names.

With model-derived keys, the daemon is no longer the model, and the friendly
short whisper names are not the manifest keys.

## Decision

1. **The fleet client speaks keys.** `converter.fleet.list_models()` returns
   `{key: entry}` (falling back to `name` for a pre-ADR-0007 engine), and
   `status_all()` returns `{key: state}`. `status`, `start_command`, `base_url`
   and `runner_for` use the key/entry.
2. **PtM roles resolve by tags.** A `config.Server` carries:
   - `key` — the fallback manifest key (offline projection);
   - `tags` — the role/capability tags a model must carry.
   `Server.model_key` resolves the first manifest entry whose tags contain every
   server tag (manifest order is the tiebreak); `_resolve`,
   `missing_servers`, `start_command` and `fleetview` use it. The static
   fallback catalog keeps `host`/`port`/`runner`/`model` for the daemon-down path.
3. **The shipped roles are:**

   | Role | Tags | Fallback key |
   |---|---|---|
   | transcriber | `vision`, `transcriber` | `qwen2.5-vl-7b-instruct-4bit` |
   | classifier | `vision`, `classifier` | `qwen2.5-vl-3b-instruct-4bit` |
   | summary | `text`, `summary` | `llama-3.2-3b-instruct-4bit` |
   | structure-text | `text`, `structure` | `llama-3.2-3b-instruct-4bit` |
   | nomic-embed | `embeddings` | `nomic-embed-text-v1.5.q8-0` |

   The ASR roles stay identity-based: a whisper model is its own manifest key;
   NB-Whisper is a route on the `audio` service (`ADR-0062`).
4. **`fleetview` rows carry `key`, `name`, `tags`.** Roles are derived from the
   entry's tags (and ASR identity); the row label is the display name. Catalogue
   rows for roles absent from the manifest keep the role name, `in_manifest:
   false`, and the static probe path.
5. **The TUI shows actual names.** The settings fleet section renders `name`
   and the PtM `roles` detail; `selected`/ASR checks use `key`/tags.
6. **`AUDIO_MODEL` uses the full whisper keys**
   (`whisper-large-v3-asr-fp16`, `whisper-large-v3-turbo-asr-fp16`).
   `normalize_audio_model` aliases the old friendly names and mlx-whisper HF
   ids, so persisted settings and env vars keep working. `nb-whisper-large`
   (the audio-server route) is unchanged.

## Consequences

- **+** The TUI shows `Qwen2.5-VL 7B Instruct (4-bit)` instead of
  `transcriber`, while start/status/log still use a stable key.
- **+** A model swap (same role tags) needs no PtM code change; the catalog's
  fallback key only matters when the daemon is unreachable.
- **+** The shared text model carries both `summary` and `structure` roles
  naturally, with no daemon-name special case.
- **+** The static fallback remains a genuine offline projection.
- **−** Role matching is tag-based: a missing/misspelled tag silently removes a
  model from that role. Mitigation: the documented vocabulary in
  `SERVING-MANIFEST.md` and `fleetview`'s catalogue row for an unmatched role.
- **−** `AUDIO_MODEL` values change; aliases cover persisted settings but docs,
  logs and scripts must move to the full keys.
- **−** The `/api/fleet` payload grows `key`/`name`/`tags`; the engine/TUI
  contract and tests update together.

## Alternatives considered

- **Keep `daemon_name` as the role key and hardcode the new model keys** — works
  today but re-couples PtM to model identity and defeats the tags design.
  Rejected.
- **Resolve roles by a single tag** — `summary` and `text` collide across models;
  requiring all tags of a role is deterministic and explicit. Rejected the
  single-tag variant.
- **Keep `AUDIO_MODEL` short and map inside every call** — spreads the alias
  across the code; the settings value should be the manifest key. Rejected;
  aliases live in `normalize_audio_model`.
- **Drop the static fallback and fail when flaate is down** — breaks the
  documented offline path (ADR-0029). Rejected.
