# 0007. Relate the transcript to the Markdown as a companion section (plus sidecar)

- Status: Accepted
- Date: 2026-08-26

## Context

The audio pass produces a timestamped, speaker-labelled transcript. It must be
"related" to the Markdown converted from the PDF. There are two natural homes:
*inside* the Markdown (inline, per slide) or *beside* it (a companion section or
file). The user's value is a single artifact they can drop into Obsidian and
skim slide-by-slide, with the transcript reachable without a second lookup.

The project already has per-slide chunking (`format._iter_slides`) and an
embeddings + `sqlite-vec` retrieval stack (`summary.py`), which could in
principle align transcript segments to slides.

## Decision

For v1, emit the transcript as a **companion `# Transcript` section appended to
the end of the Markdown**, plus a **`.srt` sidecar** for portability:

- The Markdown section is a sequence of timestamped lines, one per ASR segment:
  `[HH:MM:SS] **Speaker A:** text…` (speaker omitted when diarization is off).
  It is appended *after* the summary pass so it does not pollute slide chunking
  or RAG indexing.
- The `.srt` sidecar (`<stem>.transcript.srt`) preserves machine-readable
  timestamps and speaker cues for subtitle use.
- Association of an audio file to a source document is by **convention** (same
  stem, same folder — `lecture.pdf` + `lecture.mp3`) with an **explicit
  override** (`--audio-file PATH`, and an `audio_path` argument on
  `convert_file`/`convert_files`).

**Slide alignment is deferred** to v2. Auto-aligning transcript segments to
slides (embedding-similarity via the existing `sqlite-vec` index, or speaker
slide-change heuristics) is genuinely useful but adds a fuzzy, error-prone
boundary that deserves its own design pass. The v1 companion section already
makes the transcript "related" — reachable, ordered, and timestamped — without
claiming per-slide precision it cannot yet deliver.

## Consequences

- The transcript is part of the same artifact as the slides; one file in the
  Obsidian vault, not a sidecar the user must remember to open.
- The `.srt` gives a standards-compatible export for subtitle/caption workflows.
- Deferred alignment means v1 does not answer "which slide was on screen at
  00:12:34"; the timestamps make that manually discoverable.

## Alternatives considered

- **Inline per-slide speaker notes** (insert segments under matching slides) —
  the ideal end state, but requires alignment (deferred) and risks mangling the
  deterministic structure on a bad alignment. Rejected for v1.
- **Sidecar file only** — simplest, but splits the vault artifact in two and
  loses the "one file per lecture" property the project is built around.
  Rejected.
- **A separate `# Transcript` *heading level* / collapsible `<details>`** — the
  transcript can be long; wrapping it in `<details>` keeps the file scannable.
  Adopted as an implementation detail, not a structural decision.
