# 0045. Engine-hosted audio transcription and unified job progress

- Status: Proposed
- Date: 2026-09-23
- Relates to: [0009](0009-decouple-transcription-from-conversion.md), [0040](0040-native-go-tui.md), [0041](0041-tui-transcription.md), [0043](0043-quality-first-asr-defaults.md), [0044](0044-nb-whisper-server-asr.md)

## Context

ADR-0041 made the TUI **spawn `ptm-transcribe` as a subprocess** and stream its
stdout/stderr lines into the log pane. Two follow-up requirements make that
choice untenable:

1. **No CLI transcription spikes.** The interview-quality work (A1–A5 in the
   implementation plan) must be verified through the engine or automated tests,
   never by invoking `ptm-transcribe` from a shell. The CLI remains a supported
   product surface, but it is not a test harness — and the line-streamed
   subprocess gives the TUI nothing structured to render.
2. **Unified job progress on the TUI main screen.** The picker must show, in a
   compact strip, which jobs are running (conversion *or* transcription) and how
   far along they are, without switching to a full-screen run view. The current
   architecture has two disjoint progress mechanisms: conversion streams
   structured `file`/`page` frames over the engine `/ws`, while transcription
   streams raw text from a child process. Neither updates a shared, observable
   state.

A further, data-driven motivation: the interview post-mortem showed that the
block-pass omissions were a **run-specific settings/segmentation difference**
(44–63 s segments that the current `_group_words` cannot produce, plus a
`converter/transcribe.py` edit *between* two passes). Deterministic,
engine-owned transcription with structured progress and a coverage/regression
oracle is exactly what prevents that class of silent regression.

## Decision

Make **transcription an engine-hosted job** with structured progress, and expose
a shared job-state endpoint so any observer can render what is running.

### 1. Engine-hosted transcription

- The engine gains a `/ws` message `{"type": "transcribe", "paths": [...],
  "audio_options": {...}}`, handled by a new `_transcribe_execute` worker that
  calls `converter.transcribe.transcribe_to_markdown` /
  `attach_transcript` directly, passing an `on_line` callback.
- The callback streams **structured frames** over the WS (phase, file, heartbeat,
  per-file `idx/total`) rather than raw stdout. `converter.transcribe` already
  emits phase lines ("ffmpeg: cleaning audio …", "dereverberating …",
  "enhancing …", "transcribing with …", "diarizing …"); the engine maps those to
  `{"type": "phase", "phase": ...}` frames and augments them with per-file
  counters.
- The single-instance **`transcribe.lock` flock** is acquired by the engine for
  the duration of the transcribe job and released on completion/failure
  (mirroring `cli_transcribe.main`). A second transcribe job is rejected while
  one holds the lock. The engine's existing single-job model already serializes
  conversion; transcription joins it under the same `_job_lock`.
- `buildTranscribeArgs` in the TUI is retired from the TUI path; the engine
  constructs the transcription options (model/language/diarize/speakers) from
  the request payload, sourced from the same `converter.settings` keys
  (ADR-0043).

### 2. Unified job model and `/api/status`

- `_engine_state` gains a structured `current_job` field covering both kinds:
  `{kind: "convert"|"transcribe", status, paths, idx, total, page, page_total,
  phase, log_tail}`. Every progress callback updates it, so state is observable
  even by a client that did not initiate the job.
- New `GET /api/status` returns `_engine_state` (with `current_job`), so the TUI
  main screen can poll (or WS-subscribe) for running jobs without owning the
  initiating socket.
- The TUI's picker renders a compact **jobs strip** at the top of the main
  screen (running jobs first, then recently done/failed), polled from
  `/api/status` on a ticker while anything is running, and switches to the
  full-screen run view only on explicit user action or detail request.

### 3. CLI stays, but is not the test path

- `ptm-transcribe` remains a supported standalone surface (batch conversion,
  `--env` overrides, interactive pairing).
- Verification for this work goes through `pytest` (unit/integration against the
  stub server) and the engine's HTTP/WS API. No transcription test invokes the
  CLI binary.

## Consequences

- The TUI shows conversion and transcription progress in one compact,
  uniform job strip on the main screen; no more full-screen takeover by default.
- Verification of the interview-quality changes (A1–A5) runs through the engine
  or tests only — no CLI spikes.
- Transcription joins the engine's single-job model; the `transcribe.lock` flock
  ownership moves from the per-invocation CLI to the engine process, preserving
  the cross-surface single-flight rule (a CLI run and an engine transcribe
  cannot race).
- `converter` stays free of Go and the engine stays free of transcription
  logic beyond options plumbing; heavy work remains in `converter.transcribe`
  and the audio server (ADR-0044).
- The web dashboard gains transcription job visibility for free (same
  `/api/status`), though this is out of scope for now.

## Alternatives considered

- **Keep ADR-0041's spawn model; parse child lines for progress.** Parsing
  `ptm-transcribe` output for structured progress is brittle (carriage-return
  bars, `[OK]` lines, phase text) and keeps the "run via CLI" test path that
  requirement 1 forbids. Rejected.
- **TUI-only polling of the audio server for ASR progress.** The audio server
  owns ASR/diarization stages but not the ffmpeg/enhance phases in
  `converter.transcribe`; progress would be partial and duplicated. Rejected.
- **Engine-hosted transcription without the flock.** Would let a concurrent CLI
  `ptm-transcribe` start a second model load and write artifacts simultaneously.
  Rejected: the flock must move with the owner.
- **Separate `/api/jobs` REST endpoint with long polling.** More surface for no
  benefit over WS for the initiator; `/api/status` covers observation and WS
  covers streaming. Rejected.