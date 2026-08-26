# 0004. Add an opt-in local audio-transcription post-pass

- Status: Accepted
- Date: 2026-08-26

## Context

The converter turns PDFs/PowerPoint decks into Markdown, and already has several
opt-in local-AI post-passes (vision transcription, classifier gate, markdown
restructure, RAG summary). None of them capture what is *said* about the slides —
a lecture recording is the highest-value companion to a slide deck, but it is
currently ignored.

We want local audio-to-text transcription that can be **related to** the
Markdown produced from a PDF: a timestamped, speaker-labelled transcript that
sits alongside (and can later be aligned to) the slides. The hard constraints
carried over from the existing passes are:

- **Local only** — no audio leaves the machine.
- **Opt-in and off by default** — conversion stays fully deterministic without it.
- **Degrade gracefully** — a missing binary/model/server must never fail the
  conversion, only warn.
- **No new heavy Python dependency in `converter`** — the vision pass talks to a
  separate process precisely so the library stays light.

## Decision

Add a new opt-in pass, `converter/transcribe.py`, that runs *after* the summary
pass inside `convert_file`:

1. Resolve an audio file for the source document, by explicit override or by
   convention (same stem, same folder — `lecture.pdf` + `lecture.mp3`).
2. Transcode to 16 kHz mono WAV with `ffmpeg`, then transcribe with **mlx-whisper**
   invoked as a **subprocess** (`mlx_whisper … --output-format json`), keeping
   `converter` free of MLX.
3. Optionally label speakers via a separate diarization service (ADR-0006).
4. Append a `# Transcript` section to the Markdown (timestamped lines, speaker
   labels when present) and write a `.srt` sidecar for portability.
5. Record every segment to `ptm.sqlite` (new `transcript_segments` table) so the
   transcript is searchable and re-runnable.

Configuration follows the existing env-var pattern (ADR-0002), exposed as
`--audio` (and `--diarize`) flags:

| Var | Default | Purpose |
| --- | --- | --- |
| `AUDIO_ENABLED` | *(unset = off)* | Master switch |
| `AUDIO_MODEL` | `mlx-community/whisper-large-v3-turbo` | ASR model id |
| `AUDIO_MLX_WHISPER_BIN` | `mlx_whisper` | mlx-whisper CLI binary |
| `AUDIO_FFMPEG_BIN` | `ffmpeg` | ffmpeg binary |
| `AUDIO_LANGUAGE` | *(unset = auto-detect)* | Whisper language hint |

The transcript is a *companion* to the deterministic Markdown, never a
replacement: it is appended, and any failure degrades to a warning with the
deterministic output untouched.

## Consequences

- `converter` gains no MLX/PyTorch dependency; transcription runs as a subprocess
  exactly as the vision pass runs as an HTTP client.
- `ffmpeg` and `mlx-whisper` become soft runtime requirements (like LibreOffice
  for PPTX charts): missing → warning, not failure.
- The pass is a real second-order feature: it needs an audio file to exist, so it
  is only meaningful when the user records alongside their slides.

## Alternatives considered

- **`mlx-whisper` as an in-process library** — simpler call site, but pulls MLX
  (and its load) into `converter`'s import graph, breaking the "light library"
  contract the vision pass established. Rejected.
- **A transcription server (whisper.cpp/OpenAI-compatible)** — consistent with
  the vision pass, but adds a long-running process for a workload that is
  bursty and one-shot; subprocess is simpler and equally local. Rejected for v1.
- **A dedicated `ptm-transcribe` command instead of a post-pass** — decoupled but
  requires the user to run a second step and manually link output; the post-pass
  folds transcription into the existing "convert this lecture" workflow.
  Rejected for v1 (can be layered on later).
