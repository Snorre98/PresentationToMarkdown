# Implementation Plan: interview transcript quality + TUI job progress

- Status: Draft
- Related: [`interview-quality-brainstorm.md`](interview-quality-brainstorm.md), ADRs 0040–0044
- Repo: PresentationToMarkdown (`converter/`, `scripts/`, `tui/`, `engine.py`)

## 0. Ground truth (verified Sep 23, 2026)

- Interview artifacts live in the Obsidian vault, **not** `~/Downloads` or the
  repo root:
  `/Volumes/Ex-SSD/Documents/Studies/Masters/obsidian-studies/H26/User-centered
  interaction design IT3402/deliverables/interviews/`
- `ptm.sqlite` there holds **547 rows**: two `nb-whisper-large` passes of 21-sep
  under one source, distinguished by import `ts` (UTC):
  - finer pass `2026-09-23T07:13:53` — 193 rows, ids ~168–360, **contains all
    the "missing" content** (rows 219/220, 244, 314–318);
  - block pass `2026-09-23T07:40:02` — 104 rows, ids ~361–464, rendered as
    `.4.md`/`.4.srt`, **drops that content**.
- Root cause of the omissions is **run-specific**, not a model ceiling: the
  block pass's 44–63 s segments cannot come from the current `_group_words`
  (`max_dur=20 s`), and `converter/transcribe.py` + `21-sep.clean.4.flac` were
  both changed *between* the two runs. The content is salvageable today from the
  finer pass.
- The transcriber code lives in **this repo** (both disk copies), not in the
  vault. The vault holds only artifacts.

## 1. Constraints (hard requirements)

1. **No CLI transcription spikes.** Do not test/validate by running
   `ptm-transcribe` from a shell. All verification goes through the **engine**
   (`engine.py` endpoints) or automated tests (`pytest`, Go unit tests). The
   `ptm-transcribe` CLI remains a supported product surface but is **not** a
   test harness.
2. **The Go TUI must show job progress on the main screen** for any job —
   conversion *or* transcription — compactly: which processes are running and
   how far along, without switching away from the file picker.
3. Keep everything local; the cloud `.srt` is a dev-time reference only.
4. Reuse existing ASR text from `ptm.sqlite` where possible; avoid
   re-transcription unless a lane decision requires it.
5. AI/LLM passes opt-in, local, degrade gracefully.

## 2. Architecture decisions (new, need ADRs)

- **D1 — Engine-hosted transcription.** Transcription moves into the engine as a
  first-class job (reverses ADR-0041's "spawn `ptm-transcribe`"): the engine
  calls `converter.transcribe` directly on a worker thread and streams structured
  progress over the existing `/ws` channel, exactly like conversion. The single
  `transcribe.lock` flock stays owned by the engine while a transcribe job runs
  (the engine process itself holds it, matching how `ptm-transcribe` holds it
  today). **ADR needed (0045).**
- **D2 — Unified job model.** `_engine_state` gains a structured
  `current_job` field covering both kinds: `{kind: convert|transcribe, status,
  paths, idx, total, page, page_total, phase, log_tail}`. `_engine_state` is
  updated on every progress event so any observer (TUI, dashboard) can render a
  running job without holding the initiating WS.
- **D3 — `/api/status` endpoint.** Returns `_engine_state` (with `current_job`)
  so the TUI main screen can poll (or WS-subscribe) for what is running without
  owning the job socket.
- **D4 — Word-level speaker assignment + turn-aware re-segmentation.** The
  server's `/v1/asr` may return word-level timestamps; speaker labels are
  assigned per word, then utterances are re-segmented at speaker-change and
  pause boundaries. Replaces midpoint assignment.
- **D5 — Interview-guide prior (opt-in post-pass).** Parse the guide by
  **headings, not numbers** (guide numbering is broken); align questions to the
  transcript; force those spans to the interviewer; everything between to the
  participant; insert missing guide questions.

## 3. Workstream A — transcript quality (Phases 0–5)

### A0 — Salvage + evaluation harness (do first)

**Goal:** recover the dropped 21-sep content from `ptm.sqlite` today, and give
every later change a regression oracle.

- **Task A0.1** — `scripts/transcript_qa.py` (new; or extend `tests/`):
  - opens the interviews `ptm.sqlite`,
  - splits passes by `substr(ts,1,19)` for a given `source`,
  - diffs finer vs block pass (by timestamp overlap), localizing dropped text,
  - writes a coverage map (time ranges with no words) vs audio duration,
  - emits a corrected `21-sep.transcript.5.md` from the finer pass for the
    dropped windows, cross-checked against `21-sep-cloud.srt`.
- **Task A0.2** — Tests (`tests/test_transcript_qa.py`) using the real
  `ptm.sqlite` read-only (copy to `tmp_path`), asserting rows 219/220, 244,
  314–318 survive into the rebuilt transcript.
- **Accept:** script reproduces the three known omissions from the DB alone;
  `.5.md` contains them; timestamps match the cloud within ~1–2 s.

### A1 — Server ASR decoding settings (tiny diff, big win)

**Files:** `scripts/audio_server.py`.

- **Task A1.1** — In `_run_asr` (`generate_kwargs`):
  - add `condition_on_previous_text=False`,
  - make `num_beams` configurable (`AUDIO_ASR_BEAMS`, default `1`),
  - make `chunk_length_s` configurable (`NB_WHISPER_CHUNK_SEC`, default `28` —
    NB's model card reports better results at 28 s than 30 s).
- **Task A1.2** — Add optional `words` return to `/v1/asr` (request flag
  `return_words=true`); default response stays segment-shaped (backward
  compatible).
- **Accept:** unit test asserts the `generate_kwargs`/chunk settings; integration
  test against the stub server passes unchanged payloads.

### A2 — Diarization rework (no re-transcription)

**Files:** `converter/audio.py`, `converter/transcribe.py`.

- **Task A2.1** — Replace midpoint `assign_speakers` with overlap/majority
  assignment (weight each segment by temporal overlap with each turn).
- **Task A2.2** — When word timestamps are available (A1.2), assign speaker per
  word, then re-derive utterance boundaries at speaker changes and pauses
  (>~0.7 s gap); feed those into `merge_utterances`.
- **Task A2.3** — Single-cluster fallback: if pyannote returns fewer than
  `min_speakers` clusters, split by pause/VAD and warn.
- **Accept:** with the existing turns + finer-pass segments, interviewer
  questions land on a stable label and Q+A stops merging; 22-sep yields two
  speakers.

### A3 — Coverage + fallback re-decode

**Files:** `converter/transcribe.py`, `scripts/audio_server.py`.

- **Task A3.1** — Coverage detector: diff word-timestamp union vs
  pyannote-VAD speech regions; emit a warning + report for any speech span with
  no words (would have flagged the block pass instantly).
- **Task A3.2** — Fallback re-decode: for flagged windows, re-POST the window
  with `num_beams=5` (or `temperature=0.2` sampling) and keep the higher-score
  result.
- **Accept:** A0 harness flags 09:42–10:47 and 02:54–03:45 as low-coverage;
  fallback fills them from a re-decode when salvage is not used.

### A4 — Interview-guide alignment + question insertion (opt-in post-pass)

**Files:** `converter/` new module (e.g. `converter/guide.py`), wired into
`converter/transcribe.py` as a runtime toggled pass.

- **Task A4.1** — Parse `interviews/questions/norwegian-questions.md` by
  headings (Introduction / Background / General / Info / Social / Alcohol /
  Improvements / Closing), not the broken auto-numbers.
- **Task A4.2** — Fuzzy-align each guide question into the transcript
  (normalized token overlap; fall back to embedding match if needed).
- **Task A4.3** — Re-attribute turns: aligned question spans → interviewer,
  between-question spans → participant; insert missing guide questions at their
  aligned answer.
- **Accept:** on `21-sep`, the missing "Hva følte du ikke funket bra?" is
  recovered and inserted; Q/A attribution is correct per the guide.

### A5 — LLM post-edit (opt-in, gated, last)

**Files:** `converter/` new module (e.g. `converter/transcript_edit.py`),
`converter/config.py` (new feature toggle).

- **Task A5.1** — Evaluate the local text daemons (`text` Llama-3.2-3B,
  referenced mistral-24b; probe `GET /models`) for Norwegian capability; if none
  is strong, gate the pass off by default and document the model gap.
- **Task A5.2** — Edit low-confidence proper nouns / obvious ASR errors using
  the `format.py` pattern (`_chat_completion` + `verify_no_omissions`/`_words`
  word-preservation gate); only accept edits that drop no words and are flagged
  low-confidence.
- **Accept:** word disagreement vs the cloud `.srt` shrinks; no invented content
  (word-preservation gate holds); pass degrades to a no-op when the server is
  down.

## 4. Workstream B — TUI job progress (ADR 0045)

### B0 — ADR + engine job model

- **Task B0.1** — Write `docs/adr/0045-engine-hosted-transcription.md`:
  transcription as an engine job (reverses ADR-0041's spawn decision), flock
  ownership, unified `current_job` state, `/api/status`.
- **Task B0.2** — `engine.py`: extend `_engine_state` with a structured
  `current_job`; make both conversion (`_job_execute`) and a new
  `_transcribe_execute` update it on every progress callback.
- **Task B0.3** — `engine.py`: add `GET /api/status` returning `_engine_state`.
- **Accept:** while a job (either kind) runs, `/api/status` reports kind, path,
  idx/total, page, phase.

### B1 — Engine-hosted transcription

- **Task B1.1** — `engine.py`: new `/ws` message `{"type":"transcribe",
  "paths":[...], "audio_options":{...}}` → `_transcribe_execute` runs
  `converter.transcribe.transcribe_to_markdown`/`attach_transcript` on a worker
  with an `on_line` callback that streams structured frames (phase, file,
  heartbeat) over the WS.
- **Task B1.2** — `engine.py`: acquire the `transcribe.lock` flock for the
  duration of a transcribe job; release on completion/failure (mirror
  `cli_transcribe.main`).
- **Accept:** WS clients see structured transcribe progress (phase lines +
  heartbeat), not raw stdout only; a second transcribe job is rejected while one
  runs.

### B2 — TUI job panel on the main screen

**Files:** `tui/model.go`, `tui/engine.go`, `tui/transcribe.go`.

- **Task B2.1** — `tui/engine.go`: `Status()` client for `/api/status`;
  `RunTranscribeJob()` WS client mirroring `RunJob`.
- **Task B2.2** — `tui/model.go`: drop the full-screen `screenRun` takeover for
  transcription; instead keep a compact **jobs strip** rendered at the top of
  the picker view (and a log popover available on `l`):
  ```
  ● convert   deck.pdf   page 12/18   ▓▓▓▓▓░░░░░
  ● transcribe  21-sep.m4a  phase: diarizing   elapsed 3m12s
  ✓ done 2 · ✗ err 0
  ```
- **Task B2.3** — Poll `/api/status` on a ticker while a job is running (no
  screen switch); keep the existing WS-driven event handling as the live source
  of progress, using the poll only to detect externally-started jobs.
- **Task B2.4** — `tui/transcribe.go`: `buildTranscribeArgs` is retired from
  the TUI path (engine-hosted); keep `partitionSelected`/`siblingMarkdown` for
  the engine request.
- **Accept:** during a conversion or transcription, the main picker screen shows
  the running job(s) and their progress without switching views; multiple recent
  jobs render as a compact list (running first, then done/failed).

## 5. Sequencing & dependencies

```
A0 (salvage + harness) ────────────────► everything else depends on it for verify
A1 (server ASR settings)  ──► A2 (diarization)  ──► A3 (coverage/fallback)
A4 (guide alignment)  ──► A5 (LLM edit, gated)   [A4/A5 independent of A1–A3]
B0 (ADR + job model) ──► B1 (engine transcribe) ──► B2 (TUI panel)
```

- A0 first; it yields the corrected `.5.md` immediately and is the oracle for
  A1–A5.
- B0/B1/B2 can run in parallel with A1–A5 (different files); B2's verification
  needs B1.
- A5 is last and gated on the Norwegian-model evaluation.

## 6. Verification (using `21-sep-cloud.srt` as reference)

| Change | Verify against |
| --- | --- |
| A0 salvage | `.5.md` contains rows 219/220, 244, 314–318; timestamps match cloud ±1–2 s |
| A1 settings | re-run 21-sep via engine; omission windows decode as fully as the finer pass; systematic mis-transcriptions drop |
| A2 diarization | interviewer questions stable label; Q+A not merged; 22-sep two speakers |
| A3 coverage | harness flags the two known windows; fallback fills them |
| A4 guide | missing "Hva følte du ikke funket bra?" recovered; attribution per guide |
| A5 LLM | word-disagreement vs cloud shrinks; no invented words |
| B0–B2 | TUI main screen shows running convert/transcribe jobs + progress; `/api/status` reflects state |

All verification goes through `pytest`, Go tests, or the engine HTTP/WS API —
**never** by invoking `ptm-transcribe` from a shell.

## 7. Out of scope / low ROI (from brainstorm §4)

- Dual-stream isolation for overlapping speech (unsolvable cleanly).
- Fully reliable pyannote on crosstalk (labels stay advisory; rely on A4).
- External language-model rescoring of Whisper (low ROI locally).
- Cloud-pass comparison as a runtime feature (dev-time only).
- "god doktor → gode råd"-class judgments (ambiguous).