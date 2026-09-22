# 0043. Quality-first ASR defaults, engine-backed transcription settings

- Status: Accepted
- Date: 2026-09-22
- Relates to: [0005](0005-asr-engine-and-model.md), [0041](0041-tui-transcription.md), [0042](0042-multi-speaker-diarization.md)

## Context

The audio pass defaulted to `whisper-large-v3-turbo` (speed) with auto-detected
language and no speaker count (ADR-0005). In practice the recordings are
**Norwegian two-person interviews**, where the known-answer transcript matters
more than speed:

1. The turbo default trades ~0.3–1.3 WER points and, on Norwegian, Whisper's
   ~7–10% baseline WER is the visible ceiling — the full `large-v3` is the
   quality ceiling that fits comfortably on the 32 GB M4 (ADR-0005).
2. Whisper auto-detection on short clips is a real failure mode; the language is
   known in advance (`no`).
3. Diarization and the speaker count (ADR-0042) were already TUI-controllable
   but **session-local** — the TUI's `diarize`/`speakers` rows died on restart.

ADR-0041 §3 excluded transcription settings from the engine-backed settings
screen because they are import-time env vars (ADR-0002) that the engine could
not apply without a restart. That is the wrong frame: the TUI does not need the
engine to *apply* them — it needs the engine to *store* them, and injects them
into the spawned `ptm-transcribe` argv at run time.

## Decision

Make the max-quality Norwegian defaults the baseline everywhere, and move the
transcription settings into the engine-backed settings store.

### 1. Library defaults (ADR-0005 amended)

- `AUDIO_MODEL` default → `mlx-community/whisper-large-v3-mlx` (the quality
  ceiling; `…-large-v3-turbo` becomes the speed override).
- `AUDIO_LANGUAGE` default → `no`; an empty/`auto` value means auto-detect.
  This is a deliberate project-wide default (the owner's content is Norwegian);
  it is overridable via `AUDIO_LANGUAGE`, `--language`, or the TUI.

### 2. Engine-backed transcription settings

- Four new keys in `converter.settings` (the SQLite `meta` KV store, same as
  the GUI/web preferences): `audio_model` (default
  `mlx-community/whisper-large-v3-mlx`), `audio_language` (default `no`),
  `audio_diarize` (default `on`), `audio_speakers` (default `2`).
- `converter.config.snapshot()` exposes them (`audio_model`/`audio_language`/
  `audio_diarize`/`audio_speakers`); the engine `/api/config` GET/POST reads and
  writes them via `set_setting` (speakers sanitised to `>= 0`).
- The engine only **stores** — it never applies them to `converter` at import
  (they stay import-time env vars). The TUI reads the snapshot and injects them
  into the spawned command.

### 3. TUI

- The settings screen (`s`) gains `model` (cycles `large-v3 (max)` ↔
  `turbo (fast)`) and `language` (freeform edit, `auto` = auto-detect) rows;
  `diarize` and `speakers` now round-trip through `/api/config` instead of
  session-local state.
- `buildTranscribeArgs` injects them at spawn time:
  `--env AUDIO_MODEL=<model>`, `--env AUDIO_LANGUAGE=<lang-or-empty-for-auto>`,
  plus the existing `--diarize` / `--speakers N`. An empty `AUDIO_LANGUAGE`
  overrides the library `no` default with auto-detection.
- TUI defaults mirror the library/settings defaults, so a fresh launch is
  interview-first: max-quality model, Norwegian, diarized, two speakers.

## Consequences

- A bare `ptm-transcribe file.m4a` is a Norwegian, max-quality transcript; a
  bare TUI run labels a two-person interview out of the box.
- The TUI's transcription settings persist across launches (engine store) and
  stay in sync with any web/GUI surface that reads `/api/config`.
- Runtime cost: the default ASR is ~1× realtime instead of ~4–5× (a 13.6-min
  recording transcribes in ~13–14 min rather than ~3–4).
- Non-Norwegian recordings need an explicit `--language en` / `--env
  AUDIO_LANGUAGE=` or the TUI `auto` row — the global `no` default is a
  deliberate tradeoff for this project's content.
- ADR-0041 §3's "transcription settings do not fit the engine-backed settings
  screen" limitation is resolved: storage lives in the engine, application stays
  at spawn time.

## Alternatives considered

- **Keep the library defaults generic; default only in the TUI** — cleaner for a
  language-agnostic library, but leaves the CLI at turbo/auto and splits the
  "one default" mental model. Rejected: the owner's content is uniformly
  Norwegian and the CLI and TUI should agree.
- **Apply the settings at engine import (env mutation)** — would work for the
  engine's own subprocesses but contradicts the import-time contract (ADR-0002)
  and cannot take effect without a restart. Rejected: spawn-time injection (the
  ADR-0041 pattern) needs no restart and keeps the settings as data, not
  process state.
- **A TUI-local state file** — simpler than the engine round-trip, but duplicates
  persistence and diverges from the "one settings store" design (ADR-0026/0040).
  Rejected.