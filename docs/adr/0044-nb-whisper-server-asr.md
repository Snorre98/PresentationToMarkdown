# 0044. NB-Whisper Norwegian ASR in the audio server

- Status: Accepted
- Date: 2026-09-22
- Relates to: [0005](0005-asr-engine-and-model.md), [0006](0006-diarization-isolated-server.md), [0043](0043-quality-first-asr-defaults.md)

## Context

The ASR lane is `mlx-whisper` (ADR-0005), chosen for Apple-native MLX
performance and 99-language coverage. On **Norwegian Bokmål** — the owner's
primary content — stock Whisper large-v3 sits at ~10.4% WER (Fleurs) / 6.8%
(NST), and the produced transcripts are visibly garbled ("faderuka" for
"fadderuka", "Lindeforeningen" for "Linjeforeningen"). The National Library of
Norway's **NB-Whisper** (`NbAiLab/nb-whisper-large`, Apache-2.0) is a Whisper
large-v3 fine-tune on 20k hours of Norwegian, cutting those numbers to 6.6% /
2.2% — roughly a 50–70% error reduction on Norwegian.

NB-Whisper is **PyTorch**, not MLX, so it does not run in the `mlx_whisper`
subprocess lane. The project already isolates PyTorch speech models in the
dedicated audio server (`scripts/audio_server.py`, ADR-0006/0008/0010) — so
hosting ASR there is **not a new runtime lane**: the PyTorch lane already
exists for diarization/enhancement/isolation, and `converter` stays MLX- and
PyTorch-free by talking to it over HTTP exactly as it does for the other
endpoints.

## Decision

Serve NB-Whisper ASR from the audio server and route `converter` to it via a
model sentinel.

### 1. Audio server — `POST /v1/asr`

- `{"path": "<clean.flac>", "language": "no"}` →
  `[{"start": f, "end": f, "text": "..."}, ...]`, produced by a lazily-loaded
  transformers `pipeline("automatic-speech-recognition",
  model="NbAiLab/nb-whisper-large", device="mps")` (CPU fallback on load
  failure), with `return_timestamps="chunk"` mapping onto `{start, end, text}`.
- Runs in the **worker-subprocess harness** (like isolate/diarize): a hang or
  OOM in the 1.55B-model forward pass reaps only the worker, never the server.
- Config: `NB_WHISPER_MODEL` (the HF id) and `AUDIO_ASR_DEVICE` (default `mps`).
  `transformers` is a new runtime requirement of the audio venv only, not of
  `converter`.

### 2. `converter` — sentinel routing with mlx fallback

- `AUDIO_MODEL=nb-whisper-large` is a **sentinel**: `converter.transcribe`
  POSTs the cleaned audio to the audio server's `/v1/asr` instead of invoking
  the `mlx_whisper` subprocess. The actual HF model id stays on the server.
- If the server ASR route fails (down, timeout), transcription **falls back to
  `mlx_whisper` with the default MLX model** (large-v3-mlx) and a `[WARN]` —
  never fails the run.
- `converter.audio.asr()` is a thin client mirroring `diarize`.
- The library default `AUDIO_MODEL` stays `mlx-community/whisper-large-v3-mlx`;
  the **machine/TUI default** (`audio_model` setting, ADR-0043) becomes
  `nb-whisper-large`, so the owner's transcripts are Norwegian-first by default
  while the generic CLI remains language-agnostic.

## Consequences

- Norwegian transcripts get NB-Whisper's ~2× error reduction out of the box
  (via the TUI/engine default); the CLI opts in with
  `AUDIO_MODEL=nb-whisper-large` or `--env AUDIO_MODEL=nb-whisper-large`.
- `converter` gains no PyTorch/MLX dependency; all heavy model work stays in the
  isolated server (same pattern as diarization).
- The audio server now owns a 1.55B ASR model alongside diarization; memory on
  the 32 GB M4 is fine (~3 GB fp16), and the worker harness contains any
  crash/hang.
- ASR through the server is slower than turbo (~1× realtime on MPS) but matches
  the large-v3-mlx default the ADR-0043 quality-first default already chose.
- Non-Norwegian recordings should keep `AUDIO_MODEL` on an mlx-whisper model
  (NB-Whisper is Norwegian/English focused).

## Alternatives considered

- **MLX-convert NB-Whisper and run in the `mlx_whisper` lane** — keeps one ASR
  runtime, but the installed mlx-whisper has no HF→MLX converter, conversion is
  a one-off manual spike, and it would fork the MLX lane away from
  `mlx-community` convenience. Deferred; the server route is simpler and
  reversible.
- **whisper.cpp (ggml) for NB-Whisper** — the HF repo ships ggml weights, but
  this adds a genuinely new runtime lane (rejected in ADR-0005 for being ~30–40%
  slower) with no architectural home. Rejected.
- **NB-Whisper as the library default** — would bake Norwegian into every
  consumer of the library; the ADR-0043 settings mechanism already makes the
  machine default Norwegian without changing the library's general default.
  Adopted.