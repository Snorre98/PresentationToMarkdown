# 0006. Run speaker diarization in a dedicated PyTorch server

- Status: Accepted
- Date: 2026-08-26

## Context

The audio pass must label *who said what*. The best open diarizer is
**pyannote-audio** (`pyannote/speaker-diarization-3.1`), but it is a PyTorch
model that also requires a Hugging Face token and acceptance of gated model
terms. There is no production MLX diarizer for Python, so diarization cannot be
subsumed by the MLX lane (ADR-0005).

Adding PyTorch directly to `converter` would break the library's "light, no
heavy ML deps" contract that the vision pass established by talking to a
separate process.

## Decision

Run diarization in a **dedicated server process in its own venv**, mirroring the
"one model per process" pattern already used for the vision transcriber and
classifier (mlx-vlm on `:8081`/`:8082`):

- A small PyTorch service serves `pyannote/speaker-diarization-3.1` on
  `http://127.0.0.1:8083`.
- `converter/audio.py` is a thin client: it POSTs the audio path (and optional
  `min_speakers`/`max_speakers`) and receives `[{start, end, speaker}, …]`
  speaker turns. `converter` stays PyTorch-free.
- Speaker labels are merged onto ASR segments by midpoint overlap
  (`assign_speakers`), producing per-segment `speaker` fields; segments without
  a turn keep `speaker = None`.

The Hugging Face token and gated-model acceptance are documented as setup steps
in `docs/ai-audio.md`, exactly as the vision model's serving steps live in
`docs/ai-vision.md`.

Configuration:

| Var | Default | Purpose |
| --- | --- | --- |
| `AUDIO_DIARIZE_ENABLED` | *(unset = off)* | Master switch for speaker labelling |
| `AUDIO_DIARIZE_BASE_URL` | `http://127.0.0.1:8083/v1` | Diarization service base URL |
| `AUDIO_DIARIZE_API_KEY` | *(unset)* | Optional bearer token |

## Consequences

- PyTorch and the HF token are confined to the server process; the converter and
  its dependency graph stay clean.
- Diarization is optional *and* independently degradable: if the server is down,
  transcription still succeeds, just without speaker labels.
- Word-level timestamps (wav2vec2 forced alignment) are **deferred** to v2 — they
  would also live in this server (or a sibling), keeping the same isolation.

## Alternatives considered

- **WhisperX-style MLX fork (`whispermlx` / `whisperx-mlx`) in-process** — gets
  word timestamps + diarization in one shot, but drags PyTorch *and* HF tokens
  into `converter`, and the forks swap only ASR while keeping PyTorch alignment
  and diarization anyway. Rejected.
- **NeMo TitaNet diarization** — no HF token, but heavier (~10 GB) and a second
  diarization stack to serve; pyannote is the community default. Rejected for v1.
- **whisper.cpp `-tdrz` turn detection** — cheap, MLX-adjacent, but only flags
  "speaker changed", no cross-file speaker clustering. Not real diarization;
  noted as a fallback if the pyannote server proves onerous.
