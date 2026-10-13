# Garbled-text recovery toolkit

A design note on one problem that recurs in three places in
PresentationToMarkdown: **text that came out of an extraction step and does not
make sense**, and how to detect, classify and recover it in a uniform way.

> Companion docs: [`garbled-handling-vision.md`](garbled-handling-vision.md) is
> the forward-looking *vision* (principles + the layered signal stack); this
> page is the *current state* and the shared pipeline shape;
> [`ai-laya.md`](ai-laya.md) is the proposed classifier for the classify stage.

> **TODO:** move the out-of-repo STT parse prototype into this repo —
> `parse_transcript.py`, `asr_reslice_helper.py`, `parse-process.md` (currently
> under `…/obsidian-studies/…/deliverables/interviews/`), reconciling with
> `converter/guide.py` and `converter/transcript_edit.py` rather than
> duplicating them.

The three sources are:

- **Parsed PDF text layer** — the embedded text PyMuPDF reads, which can be
  sparse, dense OCR-garbage, or interleaved multi-column (ADR-0018/0019/0020/0023).
- **OCR / vision transcription** — text a VLM or the vision pass reads off a
  rendered page, which can be low-resolution, blurry or invented (ADR-0011 image
  regime, `converter/vision.py`).
- **Speech-to-text** — an mlx-whisper / NB-Whisper transcript, which can garble
  words, merge speakers, or fold questions away (ADR-0004–0009, `guide.py`,
  `transcript_edit.py`).

The same machinery already exists in embryo for each source, but it is scattered
across modules and partly language-bound. This note states the shared shape, maps
the existing pieces into it, names the gaps, and points at
[`docs/ai-laya.md`](ai-laya.md) for the one proposed addition.

## The five-stage skeleton

Every recovery path is the same pipeline, even though each source currently
implements it separately:

| Stage | Question | Output |
| --- | --- | --- |
| 1. **detect** | Is this chunk worth looking at? | a cheap flag (deterministic or typed) |
| 2. **classify** | *Garbled?* what *kind*? how *bad*? | a typed decision with confidence |
| 3. **recover** | What is the corrected text? | a suggested fix, with a source |
| 4. **gate** | Does the fix preserve the original? | accept / reject (verbatim word cross-check) |
| 5. **review** | Is the fix actually right? | human approval, provenance kept |

The discipline that makes this safe is stages 4–5: a recovery is never applied
silently. It must survive a word-preservation gate (no dropped or invented
content) and, for anything the gate cannot decide, a human review that keeps a
provenance trail.

## Source × stage matrix

| Source | detect | classify | recover | gate | review |
| --- | --- | --- | --- | --- | --- |
| PDF text layer | `base.text_layer_quality` / `base.text_layer_is_garbage` (ADR-0020/0023) | `router.structure_regime` (deterministic skip/downgrade/run) | `structure.py` text regime (check-and-amend) or **image regime** (reword from render) | verbatim word-gate in `structure.py` | logstore decision events |
| OCR / vision | `vision.image_readable` (readability gate) | `classify.py` tiny VLM (text/diagram/decorative) | `vision.transcribe_*` | `vision.transcription_quality` | — |
| Speech-to-text | `transcribe.py` segment confidence / repetition | *(none — gap)* | `guide.py` (canonical-question injection) + `transcript_edit.py` (word-preserving LLM edit) | `transcript_edit._safe_edit` | `ptm.sqlite` segments + human |

Two more cross-cutting layers sit *above* the matrix:

- `router.py` (ADR-0024) — the deterministic **cheap-before-expensive** order:
  skip → downgrade → run.
- `need.py` (ADR-0039) — a generative "needs fixing?" gate that decides, per
  slide/page, whether the expensive rewrite pass should run at all.

## Cross-cutting principles (already in the codebase)

- **Cheap before expensive** (ADR-0024): a cheap deterministic signal that proves
  a step unnecessary must skip it before any model call. ADR-0023's garbage
  self-check eliminated ~9 minutes of wasted VLM time for one paper.
- **Degrade gracefully**: a down server or a failed gate never fails a
  conversion; it falls back to running the expensive pass or keeping the
  verbatim text (`need.py`, `transcript_edit.py`, `transcribe.py`).
- **Never silently rewrite**: recovery is gated by a verbatim word cross-check
  (`structure.py`, `transcript_edit._safe_edit`, `format.py`), so it can only
  fix, never fabricate.
- **Single content source per page** (ADR-0020): one authority (text layer or
  vision) emits the page; recovery amends, never duplicates.
- **Observable**: every pass records a decision/error/latency event to
  `ptm.sqlite` via `converter.logstore`, so a recovery is auditable.

## Gaps the toolkit is meant to close

1. **Detection is language-bound.** `base.text_layer_is_garbage` uses a bundled
   *English* word list (`converter/_english_words.py`); it cannot flag Norwegian
   (or other) garble. The STT side has no equivalent self-check at all, which is
   why an out-of-repo prototype had to hand-roll Norwegian signals and still
   missed word substitutions (`bor` for `lagde`, `mani`, `downtown`).
2. **Classification is ad hoc and generative.** `need.py` asks a small LLM to
   *list the indices* that need fixing and bails to "run all" on an unparseable
   reply; `transcript_edit.py`'s own docstring notes the local text daemon is
   "weak at Norwegian"; the STT prototype's coherence scoring returned verbose,
   partly hallucinated prose. None returns a *typed* decision.
3. **No calibrated confidence anywhere.** There is nothing to gate on: thresholds
   are fixed heuristics, not measured probabilities, so a human cannot triage by
   "how sure are we this is garbled?".

## The proposed addition

One typed, non-generative classifier that fills the **classify** slot for every
text source — regardless of whether the text came from a PDF text layer, OCR or
ASR — with calibrated confidence per decision. The candidate is **Laya**
(`convaiinnovations/laya-multilingual`), detailed with rationale, schema, serving
and a fine-tuning plan in [`docs/ai-laya.md`](ai-laya.md).

It is deliberately *text-only*: the image-level gates (`classify.py`,
`vision.py`) remain, because Laya reads text, not pixels. Laya classifies the
text **after** it has been extracted, whatever the extraction source was.

## Roadmap

1. Document the toolkit shape (this note) and the Laya rationale
   ([`ai-laya.md`](ai-laya.md)).
2. Fine-tune `laya-multilingual` on the labels the repo already emits (word-gate
   rejections, `need.py` decisions, the STT review queue) and serve it
   (`laya-serve` on the fleet manifest).
3. Introduce a shared `converter/recover.py` orchestrator so the three sources
   stop reimplementing detect → classify → recover → gate and instead register
   source-specific *steps* into one pipeline.
