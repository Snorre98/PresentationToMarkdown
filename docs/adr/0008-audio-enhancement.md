# 0008. Enhance lecture-hall audio before ASR and persist a cleaned FLAC

- Status: Accepted
- Date: 2026-08-26

## Context

Lecture recordings made in large rooms degrade Whisper accuracy: reverberation
causes dropped words and hallucination, HVAC/projector hum causes syllable
hallucination, distant microphones produce quiet, uneven levels, and long
silences trigger repetition loops. The audio pass (ADR-0004) currently hands the
raw recording to `mlx-whisper` after a plain ffmpeg transcode to 16 kHz mono.

Research (Whisper's own docs, the openai/whisper discussion #2125, and 2026
source-separation writeups) agrees on two things: preprocessing the *input*
usually beats enlarging the model, and — importantly — enhancement is not free:
a denoiser can *reduce* quality on already-clean audio. So the pass needs a
conservative deterministic baseline plus an opt-out A/B toggle, not an
unconditional heavy pipeline.

## Decision

Enhance the audio in two stages before `mlx-whisper`, and **persist the cleaned
audio as a new file** rather than a throwaway intermediate:

1. **Deterministic ffmpeg chain** (on by default) applied while decoding:
   `highpass=f=80, lowpass=f=8000, afftdn=nf=-30, loudnorm=I=-16:TP=-1.5:LRA=11`
   — removes hum/hiss, reduces stationary noise, and normalises level for
   distant speech. Controlled by `AUDIO_PREPROCESS` (default `1`).

   > **Revised 2026-08-26:** the `afftdn` + `loudnorm` stages were removed after
   > field testing showed they over-suppressed already-clean recordings (a
   > ~15 dB level drop) and pushed Whisper into repetitive-hallucination loops
   > ("No? No? …", "she's a queen …"). The chain is now the gentle
   > `highpass=f=80, lowpass=f=8000` baseline only; denoise/dereverb remain the
   > server's job (`AUDIO_ENHANCE_ENABLED` / `AUDIO_DEREVERB_ENABLED`).
2. **DeepFilterNet denoise + dereverb** (on by default, degrades gracefully) in
   the same isolated PyTorch server as diarization, via a new `POST /v1/enhance`
   endpoint. Controlled by `AUDIO_ENHANCE_ENABLED` (default `1`) /
   `AUDIO_ENHANCE_BASE_URL` (defaults to the diarization server, `:8083`).

The output of stage 1 (upgraded in place by stage 2) is written as
**`<stem>.clean.flac`** beside the Markdown and `.srt`, and Whisper transcribes
that file — so the cleaned artifact, the transcript, and its timestamps are all
aligned, and the source recording is never modified.

The whole chain is default-on per the project's "it just works" goal, but each
stage degrades independently: missing `ffmpeg` → warning; enhancement server
down → warning + stage-1 audio; and `AUDIO_PREPROCESS=0` / `AUDIO_ENHANCE_ENABLED=0`
are the A/B escape hatches for recordings where enhancement hurts.

## Consequences

- A new persisted artifact, `<stem>.clean.flac`, appears next to the `.md`/`.srt`
  (FLAC: lossless, ~half the size of WAV, ffmpeg-native).
- Diarization now runs on the *cleaned* audio (cleaner speech → better
  segmentation), a small accuracy win at no extra cost.
- The isolated audio server (`scripts/audio_server.py`) now serves two models;
  `deepfilternet` becomes a runtime requirement of that server only, not of
  `converter`.

## Alternatives considered

- **Enhancement in-process in `converter`** — would pull a PyTorch (or MLX)
  model into the light library; rejected, mirroring ADR-0006.
- **Source separation (Demucs/UVR)** — helps BGM/overlapping speech but is heavy
  and can hurt single-speaker audio; deferred.
- **Keep enhancement in a temp file only** — rejected: the user wants the
  cleaned audio as a durable artifact, not a transient.
- **MLX/CoreML DeepFilterNet (Swift CLI)** — stays in the MLX lane but adds a
  Swift toolchain dependency; the PyTorch server reuses the ADR-0006 pattern.
  Rejected for now.
