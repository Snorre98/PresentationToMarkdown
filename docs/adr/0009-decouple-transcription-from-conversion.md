# 0009. Decouple transcription from conversion

- Status: Accepted
- Date: 2026-08-26

## Context

Audio transcription was wired into the PDF/PPTX → Markdown conversion pipeline
as an opt-in post-pass (ADR-0004): `convert_file` discovered same-stem audio and
appended a `# Transcript` section to the freshly converted Markdown, gated by
`AUDIO_ENABLED`. Two problems emerged once the pass saw real use:

1. **The convert step is no longer purely deterministic.** A "convert" that also
   runs `ffmpeg` + `mlx_whisper` conflates two different user intents — *turn
   this deck into Markdown* vs. *transcribe this recording* — and makes the
   transcription's soft runtime requirements (ffmpeg, mlx-whisper, the audio
   server) leak into every conversion path.
2. **Transcription requires a source document.** The post-pass only made sense
   *after* converting a PDF/PPTX. But a recording is valuable on its own: a user
   with just `week-2.mp3` (or a `.md` they already have) had no way to get a
   transcript without first fabricating a "conversion".

ADR-0004 explicitly deferred a dedicated `ptm-transcribe` command ("can be
layered on later"). This is that layering.

## Decision

Remove transcription from the conversion pipeline entirely and expose it as a
standalone `ptm-transcribe` command that operates directly on Markdown and/or
audio — never on PDF/PPTX.

- `convert_file`/`convert_files` no longer accept `audio_path`/`audio_paths` and
  never invoke `converter.transcribe`. Conversion is deterministic again.
- `converter/transcribe.py` is refactored to target Markdown:
  - `attach_transcript(md_path, warnings, audio_path=None) -> list[dict] | None`
    discovers same-stem audio beside the Markdown (renamed
    `find_audio_for_source` → `find_audio_for`), records segments with
    `source = str(md_path)`, and replaces any existing `# Transcript` section
    idempotently.
  - `transcribe_to_markdown(audio_path, warnings=None) -> Path | None` handles the
    *without-Markdown* case: it writes a standalone `<stem>.transcript.md` (plus
    the `.clean.flac` and `.transcript.srt` sidecars).
- A new `cli_transcribe.py` entry point (`ptm-transcribe`) accepts `.md` files,
  audio files, and folders; sets `AUDIO_ENABLED=1` (and `--diarize`/
  `--language`) in the environment *before* importing `converter` (ADR-0002);
  and pairs audio to Markdown by convention (same stem) or explicitly
  (`--audio-file`, `--to MARKDOWN.md`), with an interactive prompt as a fallback
  when the pairing is ambiguous.
- `--audio`/`--diarize` are removed from `ptm`/`ptm-start` (`cli_common.AI_FLAGS`).

## Consequences

- Conversion is deterministic and has no audio dependency; `ptm`/`ptm-start`
  reject `--audio`/`--diarize`.
- Transcription is its own workflow: `ptm-transcribe deck.md` attaches a
  transcript, `ptm-transcribe week-2.mp3` emits `week-2.transcript.md`, and
  re-running either is idempotent.
- The `transcript_segments` log now records `source` as the Markdown path (the
  transcript artifact), not the original PDF/PPTX.
- Degradation behavior is unchanged: missing binaries/models/servers warn, never
  fail.

## Alternatives considered

- **Keep the post-pass and merely add a command alongside it** — smallest diff,
  but leaves the same dual-purpose conversion and two code paths to maintain.
  Rejected.
- **A transcription server (whisper.cpp/OpenAI-compatible)** — consistent with
  the vision pass, but adds a long-running process for a bursty one-shot task
  (already rejected in ADR-0004). Rejected again.
- **Auto-pair only, no interactive prompt** — simpler CLI, but forces a silent
  guess when a recording's stem matches no Markdown. The prompt (plus the `--to`
  override) keeps the ergonomics explicit without ceremony.
