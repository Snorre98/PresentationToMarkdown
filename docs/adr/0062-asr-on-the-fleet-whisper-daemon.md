# 0062. ASR on the fleet: one whisper daemon, no subprocess lane

- Status: Accepted
- Date: 2026-10-19
- Relates to: [0005](0005-asr-engine-and-model.md),
  [0008](0008-audio-enhancement.md), [0035](0035-audio-on-fleet-manifest.md),
  [0043](0043-quality-first-asr-defaults.md),
  [0044](0044-nb-whisper-server-asr.md),
  [0045](0045-engine-hosted-transcription.md)

## Context

The MLX ASR lane was a **subprocess inside the consumer**:
`converter/transcribe.py` shelled out to `mlx_whisper --output-format json`.
That made `whisper` invisible to the fleet: it never appeared in flaate's model
list, could not be started/stopped or memory-accounted like every other model,
and its model ids lived in PtM settings rather than the manifest. NB-Whisper
already lived behind the `audio` daemon's `/v1/asr` route (ADR-0044), so the
project had two hidden serving implementations in two different places.

`mlx-audio` 0.5.9 (the tool that already serves `higgs-tts` and `voice-design`)
exposes an OpenAI-compatible STT server: `POST /v1/audio/transcriptions` with
`model`, `language`, `response_format=verbose_json` and `word_timestamps`. It
also exposes model lifecycle (`GET/POST/DELETE /v1/models`) that the flaate
control daemon can health-probe.

Two upstream behaviors needed a launcher patch (see
`macos-dev-config/tools/mlx-whisper-server.py`):

1. The OpenAI transcription form has no `condition_on_previous_text` field, and
   upstream defaults it to `True`. ADR-0008 depends on it being **off** (the
   repetition-loop hallucination on long recordings), so the launcher patches
   `Model.generate` to default it to `False` (env-overridable with
   `WHISPER_CONDITION_ON_PREVIOUS_TEXT=1`). `functools.wraps` is load-bearing:
   the STT adapter filters request kwargs against
   `inspect.signature(stt_model.generate)`.
2. `ModelProvider` caches every loaded model forever. One daemon hosts both
   whisper models, so the launcher evicts FIFO above `WHISPER_MAX_LOADED`
   (default `1`), keeping at most one ~3 GB model resident.

A runtime correction to the original plan: the `mlx-community/whisper-large-v3-mlx`
and `mlx-community/whisper-large-v3-turbo` repos are **weights-only** mlx-whisper
conversions. mlx-audio's whisper loader needs a Hugging Face processor in the
same repo to build its tokenizer and fails with `ValueError: Processor not
found`. The manifest therefore uses mlx-audio's documented whisper family,
`mlx-community/whisper-large-v3-asr-fp16` and
`mlx-community/whisper-large-v3-turbo-asr-fp16`. The manifest model names remain
`whisper-large-v3` / `whisper-large-v3-turbo`.

## Decision

### 1. One `whisper` daemon on the fleet

- `127.0.0.1:8099`, runner `delegate`, wrapper
  `macos-dev-config/tools/serve-whisper.sh`, wrapping `mlx_audio.server` through
  the patched launcher.
- Two manifest models on that daemon: `whisper-large-v3` (max quality) and
  `whisper-large-v3-turbo` (speed), both `source.kind: "hf"` so flaate's `list`
  projects their real HF `modelId`.
- The daemon keeps at most one model resident (`WHISPER_MAX_LOADED=1`); the
  other reloads from the HF cache on its next request. Start/status use the
  **model** name (`POST /start/whisper-large-v3`), not the daemon name.

### 2. `converter.audio.transcribe_fleet()` hides two wire shapes

- `ASR_MODELS` maps fleet names to `{repo, base_url, protocol}`.
  `whisper-*` uses `protocol="openai"`: a streamed multipart POST (http.client,
  precomputed Content-Length, 1 MiB file reads) to
  `<base>/audio/transcriptions` with `response_format=verbose_json`, parsing
  `segments[*]` / `segments[*].words[*]`.
- `nb-whisper-large` keeps `protocol="audio_json"` and the audio server's
  `/v1/asr` route unchanged (ADR-0044).
- Base URL resolution: protocol env override (`WHISPER_BASE_URL` /
  `AUDIO_DIARIZE_BASE_URL`) → flaate `list` (manifest) → static fallback. The
  OpenAI `model` field uses the manifest's `modelId`, so the manifest stays the
  source of truth.
- Old mlx-whisper HF ids are aliased to the fleet names on read and write, so
  persisted settings keep working without a migration.

### 3. One-step cutover, no fallback lane

The same release ships the daemon, switches `converter.transcribe` to
`transcribe_fleet`, and **deletes** `AUDIO_MLX_WHISPER_BIN`, the `mlx_whisper`
command block and its JSON-glob/exit-0 handling, the `nb-whisper-large`
sentinel and the server-ASR→mlx fallback. There is no `AUDIO_ASR_BACKEND` flag.
If the ASR call fails, the job fails (the engine surfaces the warning/error);
it never silently re-runs on another lane.

Diarization now requests per-word timestamps on **both** routes
(`want_words = diarize_requested()`), so the word-level speaker assignment and
pause-based utterance segmentation (ADR-0045) apply to whisper models too.

Beam-search re-decode of low-coverage windows remains **NB-Whisper only**: the
OpenAI transcription form exposes no beam width. For whisper models
`redecode_windows` appends one warning and skips, leaving the transcript
unchanged.

## Consequences

- `whisper` appears in flaate's model list with its HF `modelId`, can be
  started/stopped, and is visible/accountable like every other model.
- Transcription now requires the flaate control daemon **and** a running
  `whisper` daemon for the MLX models (and the `audio` daemon for NB-Whisper).
  A dead daemon fails the job loudly; there is no subprocess fallback.
- The first request after a start loads the model from the HF cache (tens of
  seconds; ~3 GB for large-v3). `serve-whisper.sh warm [model]` pays that cost
  explicitly.
- One resident model per whisper daemon; switching quality is a reload, not a
  second allocation.
- The two models share one port, so flaate's per-model state registry can show
  a sibling as `down` even while the daemon is up (starting the sibling while
  the port is bound returns `409 port-in-use`). Treat the daemon as up when
  either model reports up; renaming/moving state to the daemon level is a
  flaate follow-up.
- `converter` stays MLX- and PyTorch-free: ASR is HTTP, ffmpeg remains a
  subprocess, and the audio server keeps the PyTorch stages.
- NB-Whisper still lives on the `audio` daemon and still owns its own
  `num_beams`; unifying its contract with the OpenAI endpoint is a follow-up.
- The vision `transcriber` daemon keeps its name (renaming would break
  consumers); the `whisper` name is reserved for ASR.

## Alternatives considered

- **Keep the mlx_whisper subprocess and add fleet visibility around it.** Would
  need a shim daemon that proxies subprocess calls; ASR stays invisible to the
  manifest, memory accounting, and start/stop. Rejected.
- **Put NB-Whisper on the same whisper daemon.** NB-Whisper is PyTorch while
  this daemon is MLX-only; moving it would pull torch into the mlx-audio tool or
  fork the daemon. Deferred with the audio-serving extraction (Plan B).
- **Unify the audio server onto `/v1/audio/transcriptions` in this change.**
  Larger blast radius (diarization/enhance/dereverb share the server) for no
  PtM-side benefit; `transcribe_fleet` already hides the two shapes. Deferred.
- **Keep a fallback to mlx_whisper on failure.** Hides fleet outages, keeps the
  subprocess lane alive, and reintroduces the invisible serving path. Rejected
  by the one-step cutover decision.
- **Use the mlx-community `whisper-large-v3-mlx` / `-turbo` repos.** They have
  no HF processor, so mlx-audio cannot tokenize them. Rejected after runtime
  verification; the `-asr-fp16` repos are mlx-audio's supported conversions.
