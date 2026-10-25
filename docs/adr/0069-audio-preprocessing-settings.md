# 0069. Audio preprocessing is a per-run TUI setting, off by default

- Status: Accepted
- Date: 2026-10-25
- Relates to: [0043](0043-quality-first-asr-defaults.md), [0045](0045-engine-hosted-transcription.md),
  [0062](0062-asr-on-the-fleet-whisper-daemon.md), [0068](0068-chunked-diarization.md)

## Context

A 55-minute transcription run on 2026-10-24 spent **59 of its 68 minutes** in the
two optional preprocessing stages and produced no speaker labels:

- WPE dereverberation ran for the full client timeout (1800 s) and was abandoned
  with the cleaned file unchanged.
- DeepFilterNet enhancement then took the audio server down while its torch
  stack loaded next to the still-running WPE work; the client waited out a
  second 1800 s and every diarization chunk after it got `Connection refused`.

Both stages were on by default (`AUDIO_DEREVERB_ENABLED`/`AUDIO_ENHANCE_ENABLED`
defaulted to `1`) and neither could be changed from the TUI: they were
import-time environment flags. The only way to turn off 30 minutes of failed
work was to relaunch the engine with an env var, which is exactly the kind of
hidden configuration the settings screen exists to remove.

Enhancement had also never worked in the project's audio venv —
`ModuleNotFoundError: No module named 'df.'` at `df/model.py:12` — so its
default was pure risk.

## Decision

1. **Both optional stages are off by default.** `AUDIO_DEREVERB_ENABLED` and
   `AUDIO_ENHANCE_ENABLED` default to off in `converter.audio`, and the persisted
   settings default to `off` too. Close-mic recordings rarely need WPE, and a
   broken stage must not be the default path.
2. **They are TUI settings, applied per job.** Four rows in the settings screen's
   *Audio transcription* section:

   | row | key | default | meaning |
   |---|---|---|---|
   | `dereverb` | `audio_dereverb` | off | WPE dereverberation |
   | `denoise` | `audio_enhance` | off | DeepFilterNet enhancement |
   | `prep timeout` | `audio_prep_timeout` | 5 min | per-stage timeout for the two above |
   | `check server` | `audio_preflight` | on | probe the audio server before diarizing |

   Values round-trip through `POST /api/config` into `converter.settings` and are
   sent with each transcription job's `audio_options`, so a toggle takes effect
   on the next run without restarting anything (the ADR-0043/0045 pattern).
3. **The gates are read lazily.** `converter.transcribe` reads
   `audio_mod.AUDIO_DEREVERB_ENABLED` / `AUDIO_ENHANCE_ENABLED` at call time
   instead of copying them at import, so the engine's per-job override actually
   applies. This replaces the previous import-time copies, which would have made
   a TUI toggle silently do nothing.
4. **The preprocessing timeout is short.** `AUDIO_PREP_TIMEOUT` defaults to
   300 s. A wedged optional stage now costs minutes, not half an hour; the
   request is abandoned and the run continues with the un-preprocessed audio,
   exactly as before.
5. **Diarization pre-flights the server.** `AUDIO_DIARIZE_PREFLIGHT` (default on)
   probes `GET {base}/models` with a 2 s timeout before the first chunk. An
   unreachable server raises immediately with its URL, so the transcript header
   says "audio server unreachable at …" instead of listing four connection
   errors after a full ASR pass.
6. **The fleet view probes the audio service.** flaate reports last-known state;
   a dead daemon kept showing `up` while nothing listened on `:8089`. The
   `audio` row's claimed `up` is now confirmed with a TCP connect (0.5 s) and
   flipped to `down` with `state_stale: true` when the port is closed. Other rows
   remain a pure projection — they fail cheaply and per pass.

## Consequences

- A one-hour transcription with defaults is ASR + chunked diarization only:
  ~45 minutes on CPU, or ~15 minutes with `DIARIZE_DEVICE=mps`, instead of being
  dominated by preprocessing.
- Existing behaviour is recoverable per run: `dereverb`/`denoise` in the TUI, or
  `AUDIO_DEREVERB_ENABLED=1`/`AUDIO_ENHANCE_ENABLED=1` in the engine env.
- `AUDIO_PREP_TIMEOUT` applies to dereverb and enhancement only. Diarization
  keeps `AUDIO_DIARIZE_TIMEOUT` (1800 s per chunk, ADR-0068) because a 15-minute
  chunk legitimately takes ~9 minutes at 0.62× realtime.
- Enhancement stays installed but off. Fixing its venv import is a separate task;
  until then it is opt-in and cannot surprise a default run.
- Verified by `tests/test_engine.py` (config round-trip, per-job module state),
  `tests/test_diarize_chunks.py` (pre-flight short-circuit),
  `tests/test_audio_serve.py` (probe against the stub and a closed port) and
  `tests/test_fleetview.py` (audio row probe overrides a stale `up`).
