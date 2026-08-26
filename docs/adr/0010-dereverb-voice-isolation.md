# 0010. Dereverberation and voice isolation in the audio server

- Status: Accepted
- Date: 2026-08-26

## Context

The audio pass cleaned lecture recordings with a deterministic ffmpeg chain plus
DeepFilterNet enhancement (ADR-0008). Two gaps surfaced once it saw real use:

1. **Reverb/echo was under-treated.** DeepFilterNet is a strong denoiser but a
   weak dereverberator (its short input window cannot model late reflections), so
   room echo survived the chain largely intact — and echo is the dominant
   complaint on lecture-hall recordings.
2. **There was no way to isolate a voice.** The pass could denoise and de-reverb,
   but could not separate the lecturer from background music or competing
   talkers.

Both are speech problems that demand their own algorithms, and both are heavy
enough (a NumPy iterative filter and a PyTorch transformer) that they belong with
the isolated audio-model server, not in `converter` (ADR-0006/0008).

## Decision

Add two endpoints to the `:8083` audio server and thread them into the
transcription pipeline:

- `POST /v1/dereverb {path, output}` — **WPE** (`nara_wpe`), the canonical blind
  dereverberation algorithm (long-term linear prediction on the STFT). Pure
  NumPy, no model, no gating, 16 kHz in/out.
- `POST /v1/isolate {path, output}` — **SepFormer** (`speechbrain/sepformer-whamr`,
  trained on speech + noise + reverberation), splitting the mixture into two
  streams. The voice is picked as the higher-RMS stream (best-effort) and
  resampled 8 kHz → 16 kHz for ASR.

Pipeline order in `converter/transcribe.py`:

```
ffmpeg clean → WPE dereverb → DeepFilterNet enhance → [SepFormer isolate] → mlx-whisper
```

- Dereverberation is **always-on** (`AUDIO_DEREVERB_ENABLED`, default `1`),
  folded into the enhancement chain; no CLI flag.
- Voice isolation is **opt-in** via `--isolate` (`AUDIO_ISOLATE_ENABLED`, default
  off), writing a `<stem>.isolated.<N>.flac` that Whisper transcribes instead of
  the cleaned file. It shares the append-only version number with the transcript
  and `.clean.<N>.flac` so a run's artifacts stay paired.

Both steps follow the same temp-file + duration-validation + `os.replace` pattern
as enhancement (a server that returns a wrong-length file can never corrupt the
cleaned audio), and both degrade to a warning on failure.

## Consequences

- Reverb is now a first-class step, not a side effect of the denoiser.
- Voice isolation is a separate, explicit, best-effort step; the "which stream is
  the voice" heuristic (RMS) is documented as fallible — a dominant second
  speaker can still win.
- `requirements-audio.txt` grows by `nara_wpe` and `speechbrain` (both ungated);
  SepFormer downloads its weights on first use (~100–200 MB).
- `converter` still has no MLX/PyTorch dependency — it talks to the two new
  endpoints through `converter.audio`, exactly like `enhance`.

## Alternatives considered

- **WPE in `converter`** (pure NumPy, no server round-trip) — works offline and
  is arguably "deterministic preprocessing", but adds `nara_wpe` to the main
  dependency set and in-process STFT/DSP to a module that otherwise only drives
  subprocesses; rejected in favour of keeping all audio-quality processing in one
  server (ADR-0008).
- **Demucs (Meta) for isolation** — strong vocal separation, but music-domain and
  heavier; SepFormer is speech-native and matches the lecture use case. Rejected.
- **MDX-Net (UVR) for isolation** — ONNX, torch-free, but trained on music
  vocals/instruments, not speech; a worse fit for lecture chatter. Rejected.
