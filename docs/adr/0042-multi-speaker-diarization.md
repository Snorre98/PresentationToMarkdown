# 0042. Multi-speaker diarization with injectable speaker count

- Status: Accepted
- Date: 2026-09-22
- Relates to: [0006](0006-diarization-isolated-server.md), [0009](0009-decouple-transcription-from-conversion.md), [0041](0041-tui-transcription.md)

## Context

The audio pass can label speakers (ADR-0006), but the diarized speaker count is
**auto-detected**: `ptm-transcribe` exposes only a boolean `--diarize`, and
`converter.transcribe._transcribe` calls the diarize client with no bounds, so
pyannote guesses how many voices it hears. That is the wrong default for the
highest-value use case — a two-person interview — where the count is known up
front (`2`) and auto-detection over- or under-segments. The plumbing to pin the
count already exists but is unreachable from the CLI: the client
(`converter.audio.diarize`) and server (`scripts/audio_server.py`) both accept
`min_speakers`/`max_speakers` and forward them to
`pyannote/speaker-diarization-3.1`.

Two further gaps surfaced when the transcript was used as an interview artifact:

1. **Readability.** Diarized output is one line per ASR segment (~5 s), so an
   interview reads as many fragmented lines instead of alternating utterances.
2. **Surface.** The TUI (ADR-0041) spawns `ptm-transcribe` with no diarization
   flags at all; its settings screen cannot express a speaker count.

## Decision

Make diarization a first-class, count-aware feature of `ptm-transcribe` and the
TUI.

### 1. Inject the speaker count (CLI + env)

- New flags on `ptm-transcribe`: `--speakers N` (exact count),
  `--min-speakers N`, `--max-speakers N` (range). All imply `--diarize`;
  `--speakers` is mutually exclusive with the min/max pair; values are `>= 1`
  and `min <= max`.
- New import-time env vars in `converter.audio`:
  `AUDIO_DIARIZE_SPEAKERS`, `AUDIO_DIARIZE_MIN_SPEAKERS`,
  `AUDIO_DIARIZE_MAX_SPEAKERS`. Any speaker-count var **implies diarization**
  (`diarize_requested()`), so `AUDIO_DIARIZE_SPEAKERS=2` alone suffices for
  environments that cannot pass flags.
- `_transcribe` calls `diarize(path, **diarize_bounds())`; exact count becomes
  `min_speakers = max_speakers = N`. Failure degrades exactly as before — a
  warning and an unlabelled transcript, never a failed run.

### 2. Merge utterances for readability

- New `converter.transcribe.merge_utterances(segments)`: consecutive same-speaker
  ASR segments collapse into one entry (first `start`, last `end`, texts joined
  with a single space); a `speaker = None` segment or a speaker change starts a
  new entry.
- Applied at **render time** in `segments_to_markdown` and `segments_to_srt`.
  The SQLite `transcript_segments` table keeps the raw per-segment rows, so the
  fine-grained source stays searchable while the artifact reads as utterances.
  Unlabelled transcripts (no diarization) are unaffected — every segment has no
  speaker, so nothing merges.

### 3. TUI settings

- The TUI settings screen gains two rows — `diarize` (toggle) and `speakers`
  (numeric edit) — stored as TUI-local state and fed into the spawned
  `ptm-transcribe` argv by `buildTranscribeArgs` (`--diarize`, or `--speakers N`
  which wins). No engine `/api/config` round-trip and no restart, because these
  are not import-time engine settings.

## Consequences

- A two-person interview is one command: `ptm-transcribe --speakers 2 deck.md`,
  and the count is honoured by pyannote rather than guessed.
- Transcript output for a diarized recording reads as alternating
  `**SPEAKER_00:**`/`**SPEAKER_01:**` blocks.
- Speaker labels stay generic (`SPEAKER_00`, …); renaming to real names is
  deferred (not requested).
- Degradation is unchanged: a down audio server or a failed diarize call warns
  and yields an unlabelled transcript.
- `--isolate` remains unsuitable for interviews — it keeps only the higher-energy
  stream and would discard the quieter interviewee.

## Alternatives considered

- **Only `--speakers N` (no min/max range)** — simplest CLI, but pyannote's
  search over `[min, max]` is a genuine knob for imperfect recordings where the
  exact count is uncertain. The range costs three small flags. Adopted.
- **Merge utterances upstream (before `record_segment`)** — a single artifact
  everywhere, but changes the logstore's segment granularity and breaks the
  existing "record every ASR segment" contract (ADR-0004). Render-time merging
  keeps raw segments for search. Adopted instead.
- **Exact `num_speakers` parameter** — pyannote's `num_speakers` is sugar for
  `min = max`; expressing it as a pair keeps one code path for both modes.
- **Engine-backed TUI settings** — would round-trip through `/api/config`, but
  these values are import-time env vars in `converter`; the engine could not
  apply them without a restart (ADR-0012's exception covers only the AI on/off
  toggles). TUI-local argv is the fit for ADR-0041's "spawn and stream" design.