# 0065. AI capability labels and the resolved requirements projection

- Status: Accepted
- Date: 2026-10-20
- Relates to: [0012](0012-runtime-ai-features.md),
  [0029](0029-serving-via-control-daemon.md),
  [0062](0062-asr-on-the-fleet-whisper-daemon.md),
  [0063](0063-fleet-service-availability-in-tui.md),
  [0064](0064-fleet-model-identity-key-name-tags.md)

## Context

ADR-0064 gave models a stable key, a display name and capability tags, and made
PtM resolve roles by tags. The user-facing layer did not follow:

- the settings modal rendered internal feature keys (`format`, `classify`,
  `structure`) and a bare `model` row for ASR;
- the fleet panel rendered internal role names (`transcriber`, `structure-text`,
  `nomic-embed`) even when the model was something else (EmbeddingGemma);
- `fleetview` marked *every* tag match as a server for a role, and
  `featureAvailability` took the worst state across all of them. With Qwen 7B up
  and Qwen 3B/GLM-OCR down, `vision` showed down although PtM resolves vision to
  the 7B; `config.missing_servers()` disagreed;
- `needed` also covered the selected ASR model, so the only “needed” marker in
  the default configuration pointed at audio, a separate job;
- the summary embeddings default in `converter/summary.py` was still Ollama
  `:11434`, while the catalog, README and ADR-0029 said the fleet's `:8090`
  embeddings server. Documentation also carried stale `:8084`/`:8085` ports.

The result was a panel that could not answer “what does this pass need?” and
could say a pass was down when its model was up.

## Decision

1. **Capability labels are user-facing; keys stay stable.** `config.FEATURES`
   carries a `label`, `description` and `scope`; `config.Server` carries a
   `label`. The TUI renders labels only. The internal keys remain the env-var and
   API names.
2. **The fleet rows are resolved, not tag-matched.** `fleetview` resolves each
   role to the first manifest entry carrying all of its tags (the same rule as
   `fleet.find_by_tags`) and emits rows only for models that win a role. Losing
   tag matches are alternatives and count under `other_models`.
3. **The requirements projection is feature-centric.** `/api/fleet` carries
   `requirements`: one entry per pass with its label, toggle state, scope, the
   models it needs, their state, readiness, and the on-demand start command.
   `optional` marks graceful-degradation dependencies (the need-check gate and
   summary embeddings) so they do not make a pass look blocked.
4. **Readiness is server-computed.** The settings glyph uses the requirement's
   `readiness` (`ready` / `pending` / `missing` / `unknown`), never the worst
   state among every tag match.
5. **ASR is separate.** `needed` means “required by an enabled AI pass”.
   The selected ASR model is reported as `selected`; it is not a conversion
   dependency.
6. **The TUI has an AI setup view.** `a` opens a read-only, scrollable view of
   pass → models → state → start command. Settings keeps the toggles and a
   one-line pointer; the inline fleet list is removed (the modal was already at
   the terminal-height limit).
7. **Defaults match the projection.** `summary.py` embeddings default to the
   fleet `embeddings` role (EmbeddingGemma on `:8090`); env overrides still work.
   `nomic-embed` is renamed to `embeddings` in the catalog.
8. **Documentation has one matrix.** [`docs/ai-requirements.md`](../ai-requirements.md)
   is the reference for labels, models, ports, start commands, alternatives and
   overrides. README/runbooks link to it instead of repeating drifting defaults.

## Consequences

- **+** “What does PtM need?” has one answer, and it is the same answer the pass
  will use.
- **+** A down alternative no longer makes an available feature look down.
- **+** The TUI no longer leaks `transcriber`/`nomic-embed`/`format` as primary
  labels; the fleet panel is smaller and readable.
- **+** ASR no longer looks like a conversion requirement.
- **+** The start command is visible where the missing model is reported.
- **−** Tag matches that lose disappear from the main list; users who expected
  to see every possible model must use the `+N other fleet models` count or
  flaate-tui.
- **−** There is still no failover; a down resolved model is a hard block for
  that pass and must be started.
- **−** The requirements payload is a new engine/TUI contract; old engines fall
  back to the previous glyph scan and the TUI shows a restart hint.
- **−** The embeddings default changes from Ollama to the fleet server; users
  who relied on Ollama must set `EMBED_BASE_URL`/`EMBED_MODEL` explicitly.

## Alternatives considered

- **Keep all tag matches and mark them “alternative”** — still makes the main
  list ambiguous; the user has to know which row is selected. Rejected.
- **Fail over to the first *up* tag match** — would switch an OCR specialist into
  the writer role and silently change output quality. Rejected; manifest order
  stays the deterministic tiebreak.
- **Put the full requirements table in the settings modal** — the modal is
  already at the height limit and ignores `height`; a separate scrollable view is
  clearer. Rejected for this ADR.
- **Add a `ptm` tag to the fleet manifest** — duplicates PtM’s role map in a
  second authority and drifts. Rejected; PtM owns its requirements.
- **Rename the internal keys too** — breaks env vars, scripts and run snapshots.
  Deferred; labels solve the user-facing problem, and `nomic-embed` is the only
  factually wrong key, so it is renamed.
