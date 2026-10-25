# 0068. Chunked diarization with cross-chunk speaker identity

- Status: Accepted
- Date: 2026-10-24
- Relates to: [0006](0006-pytorch-out-of-converter.md), [0043](0043-quality-first-asr-defaults.md),
  [0045](0045-engine-hosted-transcription.md), [0062](0062-asr-on-the-fleet-whisper-daemon.md)

## Context

A single `POST /diarize` call is bounded twice at 1800 s — the server's
`AUDIO_STAGE_TIMEOUT_SEC` and the client's hardcoded `_DIARIZE_TIMEOUT`
(`converter/audio.py`). On this machine pyannote 3.1 runs at ~0.62× realtime
(CPU, embedding-bound), so any recording longer than roughly 48 minutes can
never finish: the worker is reaped mid-run and the transcript silently loses
every speaker label. That is exactly what happened to the 55-minute
`full-v1-audio` file — 1800 s after diarization started, 7156 words, zero
labels.

Raising the timeout only moves the wall. A 2-hour lecture, a 3-hour workshop
recording or a day-long session would need it raised again, and the whole run
stays uninterruptible and unlabelled-on-failure while it happens. The real
constraint is that diarization is a *global* operation whose cost is linear in
duration but whose request budget is not.

Split the audio and the global clustering breaks: `SPEAKER_00` is only
meaningful inside the call that produced it. Any chunked design therefore has to
solve identity, not just scheduling.

## Decision

1. **Chunk the audio, stitch the identities.** A recording longer than
   `AUDIO_DIARIZE_CHUNK_SECONDS` (default 900 s) is split at VAD silences into
   chunks whose cores tile the timeline exactly. Each chunk is one `/diarize`
   request, so the per-request timeout stops being a per-file ceiling.
2. **Cuts live in silence, not at fixed offsets.** `plan_diarize_chunks` takes
   the `/vad` regions (already served) and cuts at the middle of the silence
   nearest each nominal boundary, searching within `[min_seconds, 1.5 ×
   chunk_seconds]`. No VAD, or a VAD failure, falls back to fixed cuts and a
   warning — chunking must never be the reason a transcript fails.
3. **Identity comes from embeddings, not from label order.** The server returns
   one centroid per local label (`return_embeddings`, pyannote's own clustering
   centroids). `stitch_chunk_turns` L2-normalises them, clusters with
   average-linkage cosine similarity — the same family pyannote uses — and
   renames the survivors `SPEAKER_00…` by first appearance. With an exact
   speaker count configured (the TUI's `speakers` row) the merge stops at that
   count; otherwise the threshold is `AUDIO_DIARIZE_SPEAKER_THRESHOLD`
   (default 0.65). A local label with no embedding keeps a private identity
   rather than being merged into someone else.
4. **Every second is covered exactly once.** Turns are clamped to their chunk's
   core span, so a turn that straddles a boundary is split rather than
   duplicated or dropped: a word always has a label to inherit.
5. **One failed chunk is not a failed transcript.** A chunk that errors leaves
   only its own span unlabelled, warns, and lets the rest keep their labels. If
   every chunk fails the error is raised and the caller keeps the historical
   unlabelled fallback.
6. **No embeddings means no chunking.** A server that predates this ADR returns
   turns without centroids; guessing identity from label order would fabricate
   wrong attribution, so the client warns and falls back to one request.
7. **The strategy stays on the client.** Chunk planning, slicing, stitching and
   naming live in `converter/` (numpy-free pure Python); the server stays a thin
   model host that also reports `device` and `elapsed_s`. This keeps PyTorch out
   of `converter` (ADR-0006) and lets the whole orchestration be tested against
   fakes.
8. **Device selection is explicit.** `DIARIZE_DEVICE=cpu|mps|auto` moves the
   pipeline (default stays `cpu`, unchanged behaviour). Diarization is
   embedding-bound on CPU — the dominant cost for long files — so the option is
   the first thing to try before buying more chunking.
9. **The transcript states what happened.** The header carries a
   `Speaker diarization: …` line (speakers, chunk count, labelled-word ratio, or
   the failure reason), so "unlabelled" is never ambiguous.

## Consequences

- Arbitrarily long recordings diarize with stable identities; wall time stays
  linear in duration (~0.62× realtime on CPU, ~15 min per hour of audio).
- Chunk count is observable in the transcript and the engine log, and each
  chunk is a natural cancellation boundary (the engine still checks cancel
  between files, not between chunks — a follow-up).
- Clustering quality now depends on the centroids, so a chunk whose local
  clustering is poor can pull an identity. The exact-count path (the default
  `speakers=2`) is the most stable; auto mode needs the threshold tuned on real
  material.
- Persisting turns/embeddings so a re-render never costs another diarization is
  **not** part of this ADR; it remains the next step for making the output
  reusable.
- Verified by `tests/test_diarize_chunks.py` (planning, clustering, stitching,
  fallbacks, header text) plus the existing transcription suite.
