# 0005. Use mlx-whisper with large-v3-turbo (large-v3 override) as the ASR engine

- Status: Accepted
- Date: 2026-08-26

## Context

The audio pass (ADR-0004) needs a local speech-to-text model. The target machine
is an **Apple M4, 10-core, 32 GB unified memory** (~24 GB usable, ~0.55 GB per 1B
params at 4-bit). The existing AI stack is MLX-first (mlx-vlm) and prefers
permissive licenses (MIT/Apache) over attribution terms.

The open ASR landscape in 2026 has moved past "Whisper is the only option", but
most of the new leaders trade away the two properties this project cares about:
**language coverage** and **runtime maturity**.

## Decision

Use **mlx-whisper** (the canonical MLX ASR, same "lane" as the vision model),
with:

- **`mlx-community/whisper-large-v3-turbo`** as the **default** — 809M params,
  ~4–5× faster than full large-v3 on Apple Silicon, within ~0.3–1.3 WER points
  (~7.7% vs ~7.4% aggregate). MIT.
- **`mlx-community/whisper-large-v3-mlx`** as the **max-quality override** via
  `AUDIO_MODEL` — the quality ceiling that fits comfortably (~3 GB) on this
  machine. MIT.

Both models are MIT-licensed and cover **99 languages** — decisive because the
user's lecture content is likely Norwegian and/or English. Segment-level
timestamps come for free.

The comparison that drove the choice:

| Model | Params | Aggregate WER | Languages | License | Apple-Silicon runtime |
| --- | --- | --- | --- | --- | --- |
| **Whisper large-v3** | 1.55B | ~7.4% | 99 + translate | MIT | `mlx-whisper` (mature) |
| **Whisper large-v3-turbo** | 809M | ~7.7% | 99 | MIT | `mlx-whisper` (mature) |
| Parakeet-TDT 0.6B v2/v3 | 0.6B | ~6.0% | EN only / 25 | CC-BY-4.0 | FluidAudio CoreML / `mlx-audio` |
| Canary Qwen | 2.5B | ~5.6% | en/de/fr/es | CC-BY-4.0 | `mlx-audio` |
| IBM Granite Speech 4.0 1B | 1B | ~5.5% | ~EN | Apache-2.0 | `mlx-audio` |

## Consequences

- ASR stays inside the existing MLX toolchain; no new runtime, no PyTorch.
- `AUDIO_MODEL` is the single knob to trade quality for speed (turbo ↔ large-v3).
- Low-resource-language accuracy is sacrificed *only* if the user overrides to
  turbo; the default already favors breadth.

## Alternatives considered

- **Parakeet-TDT** — best throughput and strong English WER, but v2 is
  English-only (v3 adds ~25 languages, no Norwegian) and it is CC-BY-4.0; the
  MLX path (`mlx-audio`) is young. Rejected.
- **Canary / Granite Speech** — lead English WER, but narrow language coverage
  (en/de/fr/es or EN-only), CC-BY-4.0/Apache respectively, and require the
  immature `mlx-audio` runtime. Rejected for v1; revisit if English-only batch
  transcription becomes the primary workload.
- **whisper.cpp (GGUF/Metal)** — portable single binary, but ~30–40% slower than
  MLX on this hardware and a second lane to maintain. Rejected.
- **faster-whisper (CTranslate2)** — no Metal on Apple Silicon, CPU-only and far
  slower. Rejected.
