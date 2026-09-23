# Brainstorm: improving interview transcript quality (Norwegian, NB-Whisper)

## Verified ground truth (Sep 23, 2026 — corrected)

The original brainstorm was written without finding the artifacts. This version
is corrected against what is actually on disk.

**Artifacts live in the Obsidian vault, not the code repo or `~/Downloads`:**

```
/Volumes/Ex-SSD/Documents/Studies/Masters/obsidian-studies/H26/
  User-centered interaction design IT3402/deliverables/interviews/
    21-sep.m4a, 22-sep.m4a
    21-sep.transcript.{1,2,3,4}.md  + .srt
    21-sep-cloud.srt
    21-sep.clean{.1,.2,.3,.4,}.flac, 22-sep.clean.flac
    ptm.sqlite                         (547 rows in transcript_segments)
    questions/{norwegian,english}-questions.md
```

**`ptm.sqlite` holds TWO nb-whisper-large passes of 21-sep under the same source
(`21-sep.transcript.4.md`), plus one for 22-sep:**

| Pass | Rows | Import (UTC) | Local | Notes |
| --- | --- | --- | --- | --- |
| 21-sep finer | 193 | 07:13:53 | 09:13 | **contains every "missing" chunk** |
| 21-sep block | 104 | 07:40:02 | 09:40 | = `.4.md` / `.4.srt` (mtime 09:40); drops them |
| 21-sep turbo | 167 | 15:24 | — | `transcript.1.md` (mlx turbo) |
| 22-sep | 83 | 07:52 | — | only one import; no finer pass to compare |

Confirmed rows from the **finer** pass (all present, per the reviewer's citation):

- `id 219–220` (179–187 s): "Det var kjempekleit … veldig klein dag" — the dropped
  first-day answer.
- `id 244` (312.9 s): "Hva føler du ikke funker bra?" — the guide question the
  block pass folded away.
- `id 314–318` (611–635 s): the full "lage bruker på nettsiden … kompis …
  informasjon går rundt i bunnen" story.

The **block** pass instead emits 44–63 *second* segments that merge whole Q+A
turns (e.g. `163.42→211.12` runs the guide question *and* the answer together;
`601.04→647.12` swallows the nettsiden story into "Husker jeg var litt rot …").

## 1. Where each defect actually comes from (pipeline + evidence)

The critical architectural fact stands: **ASR segmentation and diarization are
never reconciled.** The server (`_run_asr`, `scripts/audio_server.py:184-212`)
does word-level NB-Whisper (`return_timestamps=True`, 30 s chunks, 5 s stride,
`num_beams=1` greedy) and `_group_words` (:215-241) groups words on sentence
punctuation / 20 s / 240-char caps **with no speaker information**. Only
afterwards does `assign_speakers` (`converter/audio.py:314-328`) stamp a single
speaker on each already-formed segment by **midpoint**.

### Category 1 — Content omissions (root cause CORRECTED)

**The primary cause is NOT a greedy-decode model ceiling — it is a
run-specific settings/segmentation difference.** Evidence, from the DB alone:

1. The same model (nb-whisper-large), same recording, decoded those exact
   windows **fully** 27 minutes earlier in the finer pass. So the content is not
   beyond the model.
2. The block pass's 44–63 s segments **cannot have come from the current
   `_group_words`** (which hard-caps at `max_dur=20 s`). The block pass ran a
   different segmentation/chunking path (older chunk-mode grouping, or a
   pre-edit `_group_words`), and that path is what dropped content.
3. The working tree was being edited *between* the two passes:
   - `converter/transcribe.py` mtime **09:22** — 9 min after the finer pass
     (09:13), 18 min before the block pass (09:40).
   - `scripts/audio_server.py` mtime 08:47, `converter/audio.py` 22:30 — the
     NB-Whisper server-ASR additions are **uncommitted** (ADR-0044 is an
     untracked file; `git status` shows the whole feature in the working tree).
   - `21-sep.clean.4.flac` was regenerated **09:27** — *after* the finer pass,
     *before* the block pass — and is 10.3 MB vs 12.2 MB for `.clean.3.flac`
     despite identical 817.52 s / 16 kHz mono duration. **The two passes may not
     even have transcribed the same cleaned audio.**
4. Therefore the recommendation "re-decode with beams" is **not** the fix for
   these omissions — the content already exists in the finer pass and can be
   salvaged today, independent of any re-transcription.

Secondary, still real: greedy decoding (`num_beams=1`) and
`condition_on_previous_text` (unset → Whisper's default True in the
`transformers` path) remain the likely causes of the *within-pass* word-level
compression and repetition that both passes share. The mlx lane already disables
conditioning; the server path does not.

### Category 2 — Word accuracy (unchanged)

1. Greedy decoding — `fadderuka`, `linjeforeningen`, `synergi`, `QR-koden` are
   rare/OOV student-jargon; beam search (`num_beams=5`) rescues this class. NB's
   own model card **recommends `num_beams=5` and `chunk_length_s=28`**.
2. No language-model rescoring — `den borten`→`der borte`, `samled ukene`→
   `samlede opplevelse`, `god doktor`→`gode råd` are LM-fixable, not
   decode-fixable.
3. Domain prior unused — the interview guide's questions are ~verbatim in the
   audio but never fed to ASR or used to bias rare tokens.
4. Crosstalk — occasional overlap degrades both channels; no isolation is safe
   here (§4).

### Category 3 — Speaker attribution (confirmed, both passes)

1. **Midpoint assignment is the deterministic weak link**
   (`converter/audio.py:314-328`). A segment spanning a Q+A boundary gets one
   label from its midpoint — why Q+A merge and short interviewer questions flip
   to `SPEAKER_01`.
2. **Decoupled segmentation** — `_group_words` ignores turns, so segments
   straddle speaker changes and even a perfect midpoint mislabels.
3. **pyannote 3.1** — degrades on crosstalk; 22-sep collapsed to one cluster
   despite `min=max=2`. The finer pass confirms the same label inconsistency
   spans both passes (interviewer's "Hvis du kunne endret på én ting …" is
   labeled `SPEAKER_01` at 640 s), so this is pipeline-level, not a one-off.

### Category 4 — Segment/utterance boundaries (confirmed)

1. Segmentation is text/acoustic-only — never splits at turns or pauses, so
   distinct turns bleed (worse in the block pass's 44–63 s megasegments).
2. `merge_utterances` (`converter/transcribe.py:636-665`) merges consecutive
   same-speaker segments; a wrong midpoint label on a middle segment merges the
   wrong neighbours.
3. No pause-based boundary — the 20 s cap is arbitrary; quick Q+A merges, long
   answers split mid-thought.

## 2. Concrete improvements (feasibility · effort · impact)

### Omissions / faithfulness — **reordered by the new evidence**

- **Salvage from the finer pass (DO THIS FIRST, free).** The missing content
  exists in `ptm.sqlite` under `21-sep.transcript.4.md`, rows imported 07:13.
  Extract the finer-pass text for 09:42–10:47 and 02:54–03:45, cross-check
  against the cloud `.srt` and `.2`/`.3`, and emit a corrected `.5.md`. Zero
  ASR work.
- **Determinism/regression oracle.** The finer pass is the reference: the block
  run must be re-run with the settings that produced the finer pass and match it
  (or beat it). Any future "improvement" is verified as: does it still contain
  rows 219/220, 244, 314–318?
- **Coverage detector (VAD-gated)** — diff word-timestamp union vs pyannote-VAD
  speech regions; report gaps. Would have flagged the block pass instantly.
  Medium effort, high value.
- **Fallback re-decode of low-coverage windows** — `num_beams=5` /
  `temperature=0.2` sampling on flagged windows only. Bounded cost, high impact
  for genuinely-hard passages (not the salvaged ones).
- **Disable `condition_on_previous_text` in the server path** — one-line in
  `generate_kwargs`; the mlx lane already does this. Trivial effort.
- **`chunk_length_s=28`** — NB's model card explicitly reports better results
  at 28 s than 30 s for longer files. One-line.
- **Word-timestamp repair of boundaries** — snap segment ends to pauses. Low-medium
  effort, medium impact.

### Word accuracy

- **`num_beams=5`** (configurable `AUDIO_ASR_BEAMS`) — one-line; biggest single
  lever for the *word* errors; NB recommends it; ~5× slower → opt-in quality
  flag, not default.
- **NB-Whisper verbatim variant (new lever the original missed).** NbAiLab
  publishes `NbAiLabBeta/nb-whisper-large-verbatim` (+`/nb-whisper-large-verbatim`):
  lower-cased, no punctuation, "considerably more verbatim", does not correct
  grammar — explicitly aimed at "detailed transcription / linguistic analysis".
  For interviews, this is worth testing *before or alongside* the MLX track: it
  targets exactly the verbatim-fidelity failure mode. **Note:** the model card
  pasted into the original conversation is the *Base* card (74M); the pipeline
  uses *large* (1550M) — verify with the large id, not the Base example.
- **LLM post-edit (opt-in, local)** — reuse `_chat_completion` +
  `verify_no_omissions`/`_words` (the `format.py` pattern) to fix proper nouns,
  gated so it never invents/drops words. Needs a Norwegian-capable local model.
- **Language-model rescoring** — low ROI locally (§4).
- **Larger/varied NB-Whisper checkpoints** — `nb-whisper-large` is the ceiling;
  the *verbatim* variant is the only nearby alternative. Low ROI beyond that.

### Diarization

- **Replace midpoint with overlap/majority assignment** — weight by temporal
  overlap (or label per word). Low effort, fixes the grossest flips; reuses
  existing segments + a diarization-only re-run (pyannote is fast — no
  re-transcription).
- **Word-level speaker assignment + re-segmentation at turn boundaries** — have
  `/v1/asr` return words (the server already produces them), assign speaker per
  word, re-derive utterances at speaker-change + pause boundaries. The correct
  fix for both §3 and §4. Medium effort, high impact.
- **Interview-guide prior (highest-leverage, novel)** — the guide is at
  `interviews/questions/norwegian-questions.md`, in-repo. Align its questions to
  the transcript (fuzzy/embedding), force those spans → interviewer, between →
  participant. Deterministic-ish, low hallucination risk, **post-pass over
  existing text, no re-transcription**. Recovers the missing question by
  inserting it where its answer lives. **Caveat (reviewer): the guide's
  auto-numbering is broken (5→7, 1→3→5) — parse by headings, not numbers.**
- **22-sep single-cluster fallback** — if pyannote returns <2 clusters despite
  `min=max=2`, split by pause/VAD and warn. Low effort.
- **Param/VAD tweaks** — low effort, marginal.

### Segmentation

- **Pause-based boundaries** from word timestamps (gap > ~0.7 s → new
  utterance). Low-medium effort, good impact.
- **LLM utterance splitting** — last resort; superseded by word-level
  re-segmentation. Low priority.

## 3. Ranked plan (corrected)

**Phase 0 — Evaluation harness + salvage (do first; free content is waiting).**
Script that reads `ptm.sqlite` (not just `.md`/`.srt`), splits the two 21-sep
passes by import `ts`, and:
- (a) diffs finer vs block pass to localize where text dropped (the built-in
  oracle — gap vs compression is answerable **without the cloud**);
- (b) emits a coverage map vs audio duration;
- (c) renders a corrected `.5.md` from the finer pass for the dropped windows,
  cross-checked against `21-sep-cloud.srt` and `.2`/`.3`.
**Verify:** the `.5.md` contains rows 219/220, 244, 314–318 content, timestamps
match the cloud within ~1–2 s.

**Phase 0.5 — Runtime consolidation + settings parity (parallel track).**
- Make the block run reproduce the finer pass: pin the server settings that
  produced it (chunk/stride/grouping), then add `condition_on_previous_text=
  False`, `chunk_length_s=28`, configurable `AUDIO_ASR_BEAMS`.
- Test **NB-Whisper verbatim large** on 21-sep (or the saved cleaner) as a
  comparison lane, reusing the Phase 0 harness. 
- Evaluate **MLX conversion** (`aalst/nb-whisper-large-mlx` already exists on
  HF; try `AUDIO_MODEL=aalst/nb-whisper-large-mlx` with zero code change) for
  speed parity and one-runtime simplification.
**Verify:** re-run 21-sep with each candidate; WER/disagreement vs cloud and
coverage vs audio; pick the lane + settings.

**Phase 1 — Server ASR decoding (tiny diff, big win).**
`num_beams` → configurable; `condition_on_previous_text=False`; `chunk_length_s=
28`; optional `words` return from `/v1/asr` (backward-compatible).
**Verify:** the two omission windows decode as fully as the finer pass (or
better); systematic mis-transcriptions drop.

**Phase 2 — Diarization rework (no re-transcription).**
(a) Overlap/majority assignment replacing midpoint; (b) word-level assignment +
turn/pause-aware re-segmentation when Phase-1 words are available; (c)
single-cluster fallback.
**Verify:** re-run diarization only on the existing clean FLACs, re-label the
existing segments — interviewer questions land on a stable label, Q+A stop
merging; 22-sep yields two speakers.

**Phase 3 — Coverage + fallback re-decode.**
VAD-gated coverage check + optional re-decode of low-coverage windows.
**Verify:** the 09:42–10:47 and 02:54–03:45 windows are flagged and, with
fallback on, filled — from re-decode if the salvage path is not used.

**Phase 4 — Interview-guide alignment + question insertion (opt-in post-pass).**
Parse `questions/norwegian-questions.md` by **headings** (not numbers); align
questions; re-attribute turns; insert missing questions.
**Verify:** 21-sep gets correct Q/A attribution and the missing "Hva følte du
ikke funket bra?" question, validated against the guide (not the cloud).

**Phase 5 — LLM post-edit (opt-in, gated, last).**
Evaluate a Norwegian-capable local model; fix low-confidence proper nouns only,
with the `format.py`-style word-preservation gate.
**Verify:** word disagreement vs cloud shrinks without invented content.

## 4. Low-ROI / unsolvable

- **Overlap resolution via dual-stream isolation** — unsolvable cleanly:
  `--isolate` keeps only the dominant stream and discards the quieter
  interviewee (ADR-0042). Not worth it for occasional overlap.
- **Fully reliable pyannote on crosstalk** — unsolvable; treat labels as
  advisory, lean on the guide-based re-attribution (Phase 4).
- **External LM rescoring of Whisper** — low ROI: no good offline Norwegian LM,
  complex integration; beams + LLM edit cover most of the benefit.
- **Cloud-pass comparison as a runtime feature** — not applicable; cloud is a
  dev-time evaluation reference only.
- **"God doktor"→"gode råd"-class judgments** — ambiguous; not worth
  automating.

## 5. Answers to the reviewer's questions

1. **Where does the transcriber code live?** In the **PresentationToMarkdown
   repo** (`/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown`,
   also mirrored at `/Volumes/Ex-SSD/Documents/Liv/Projects/PresentationToMarkdown`).
   The Obsidian vault (Masters) is only the *artifacts* folder. The plan is
   scoped to the PresentationToMarkdown repo: `scripts/audio_server.py` +
   `converter/{audio,transcribe}.py` + a new harness under `tests/` or `scripts/`
   operating on the interviews `ptm.sqlite`. Both repo paths exist on disk; the
   code is accessible.
2. **Re-transcription tolerance (Q3):** **Not needed for the omissions** — the
   content is already in `ptm.sqlite` (finer pass). Recommended constraint:
   *diarization-only re-run + salvage from `ptm.sqlite`* for the immediate fix;
   defer any full re-transcription to the Phase 0.5 lane/settings decision.
   If the verbatim or MLX lane wins, one ~13 min greedy run (or ~65 min with
   beams) is the budget to produce a clean `.5.md`.
3. **Local Norwegian-capable LLM (Q4):** The manifest (via the control daemon,
   `converter.fleet`/`config.SERVERS`) lists Llama-3.2-3B on the `text` daemon
   (`:8083`) and a referenced mistral-24b; Qwen2.5-VL variants serve vision.
   **Evaluate before committing to Phase 5** — probe `GET /models` on each
   daemon, and if none is strong at Norwegian, reconsider Phase 5 or accept a
   gate on a weaker model.
4. **Provenance (Q5):** **Recommendation:** interviewer questions may be inserted
   *verbatim from the guide* (they are the interviewer's scripted lines; the
   audio confirms them), and obviously-garbled ASR words may be corrected *only
   when confirmed by both the cloud pass and context*, with corrections
   surfaced as diffs (e.g. a `[fixed]` marker or an appendix table). Everything
   unconfirmed stays verbatim. This keeps the artifact showable while remaining
   auditable.

## 6. Addendum — the ASR runtime and the MLX option

NB-Whisper runs on **`transformers`/PyTorch-MPS, not MLX** — vanilla Whisper uses
`mlx_whisper` (Apple-native), NB-Whisper is a PyTorch model served by the audio
server (`pipeline("automatic-speech-recognition", model="NbAiLab/nb-whisper-large",
device="mps")`). ADR-0044 chose this deliberately (no HF→MLX converter at the
time). **This is the likely cause of NB-Whisper being slower than vanilla large
on the same machine**: PyTorch-MPS has higher dispatch overhead and less-optimized
kernels, plus `return_timestamps=True` adds a DTW word-alignment pass over
chunked windows.

**MLX conversion is feasible and already half-done:** `aalst/nb-whisper-large-mlx`
(fp16, `mlx-examples/whisper/convert.py`, format-only) exists on HF and can be
tried with zero code changes (`AUDIO_MODEL=aalst/nb-whisper-large-mlx`). Benefits:
one runtime, speed parity, drops MPS finickiness, inherits the mlx lane's
`condition_on_previous_text=False` default. Caveat: fixes speed/runtime, not the
segmentation/diarization logic bugs (those are lane-independent). For the HF
contribution angle, a **verified** conversion (WER vs the PyTorch original on
Fleurs `nb-NO`/NST, `generation_config.json` preserved, optional 4-bit) is the
valuable, still-missing piece. Effort: hours to a day.