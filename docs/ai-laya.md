# Laya — System 1 typed classifier for garbled-text recovery

Proposed use of [Laya](https://github.com/NandhaKishorM/laya) as the **classify**
step of the [garbled-text recovery toolkit](garbled-text-recovery.md): a fast,
typed, multilingual "does this text make sense?" decision that runs on every
chunk and, with a calibrated confidence, decides what needs recovery and what is
clean. This is a design proposal, not an implemented pass.

## What Laya is

Laya is a **non-autoregressive "System 1" decision engine**. Given a piece of
text and a schema of typed questions, it returns typed answers with
probabilities in a single forward pass — no text generation, so there is nothing
to parse and nothing to hallucinate:

| Primitive | Output | Use here |
| --- | --- | --- |
| `choice` | top label + per-option probabilities + confidence | what *kind* of garble |
| `score` | expected ordinal level + distribution + confidence | how *bad* |
| `noul` | calibrated probability P(true) | *is it garbled at all* |

Three checkpoints, selected per request by a `Router`:

| Checkpoint | Encoder | Params | Context | For |
| --- | --- | --- | --- | --- |
| `convaiinnovations/laya` | ModernBERT-large | 421M | 512 | English |
| `convaiinnovations/laya-multilingual` | mmBERT-base (256k vocab) | 322M | 1024 (up to 8192) | 100+ languages, cross-lingual NLI |
| `convaiinnovations/laya-typed-decisions` | ModernBERT-large | 421M | 1024 | typed-decision workflows |

Latency is ~33 ms per question (~7 ms/question batched), so classifying an entire
document — every cell, page or slide — costs fractions of a second. It runs
self-hosted (Apache-2.0).

## Why: what it fixes

The codebase already has a "cheap classifier before an expensive rewrite" shape
(ADR-0024, ADR-0039), but today's cheap classifiers have three weaknesses, each
with a concrete instance:

1. **Language-bound determinism.** `converter.base.text_layer_is_garbage`
   (ADR-0023) decides "OCR garbage" with a bundled *English* word list
   (`converter/_english_words.py`) — vowel-less tokens and near-misses of English
   words. It cannot flag Norwegian garble. The STT side has no equivalent: an
   out-of-repo prototype had to hand-roll Norwegian signals and still missed word
   substitutions (`bor` for `lagde`, `mani`, `downtown`).
2. **Generative fragility.** `converter/need.py` (ADR-0039) asks a small LLM to
   *list the indices* of slides/pages that need fixing, then parses the reply and
   bails to "run all" when it is unparseable. `converter/transcript_edit.py`'s
   own docstring notes the local text daemon is "weak at Norwegian". The STT
   prototype's coherence scoring returned verbose, partly hallucinated prose.
   None of these produce a *typed* decision.
3. **No calibrated confidence.** There is nothing to gate on: thresholds are
   fixed heuristics, not measured probabilities, so nothing answers "how sure are
   we that this is garbled?".

Laya removes all three:

- **Non-autoregressive** → no hallucination, no parse step, a fixed output shape.
- **Typed** → `choice`/`noul`/`score`, which is exactly the classify-step shape.
- **Calibrated probabilities** (RLCD-trained against strictly-proper scoring
  rules) → after fitting temperatures, `confidence` is a real number to gate on,
  matching ADR-0039's "pure decision layer that only saves work".
- **Multilingual** → mmBERT-base's 256k vocab covers English OCR *and* Norwegian
  STT with one checkpoint.
- **~33 ms** → cheap enough to run everywhere, which is the whole point of
  cheap-before-expensive.

### The honest caveat

Laya is "a fast base to specialise, not a zero-shot decision engine". Its base
checkpoints score **near chance** on held-out typed decisions (0.362/0.352 vs
0.318 random; the 0.766 figure is the checkpoint *fine-tuned* on that benchmark's
own split). For our subtle Norwegian task — word substitution vs genuine
disfluency — a zero-shot 322M classifier will not beat the 24B model it replaces
and probably does worse. The value is in **fine-tuning** (below) on the labels
the repo already emits for free.

## Proposed schema

One schema, reused across all text sources:

```python
questions = {
    "garbled": {
        "type": "noul",
        "instructions": "Does this text contain an extraction error that breaks its meaning?",
        "criteria": {
            "false": "the text reads as correct, natural speech or prose",
            "true": "the text is garbled, wrong or nonsensical",
        },
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of problem, if any, does this text have?",
        "criteria": {
            "ocr_garbage":    "dense OCR typos, vowel-less or near-miss tokens",
            "asr_substitution": "a speech-to-text word was replaced by a similar but wrong word",
            "asr_garbled":    "speech-to-text collapsed or merged words into nonsense",
            "disfluency":     "genuine incomplete or hesitant speech, not an error",
            "clean":          "no problem",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "How badly does the problem hurt understanding?",
        "criteria": ["none", "minor", "major"],
    },
}
```

Known Laya pitfalls the schema must respect (from the project's own docs):

- **No boolean-word choice keys** — `true`/`false` keys get followed instead of
  the descriptions (#377). Use semantic keys and, for `noul`, the `labels`
  mapping so the model reads opaque `A`/`B` while the answer stays P(true)
  (#156).
- **`score` has a position bias on the multilingual checkpoint** (rarely picks
  the first level, #131) — validate or drop `severity`; it is the weakest of the
  three primitives.
- **Short text misroutes.** Our chunks are short, and short Latin text carries no
  language signal, so the `Router` may pick the English checkpoint. Pin
  `default="multilingual"` (and/or `lang_guess="no"`).

The `garbled` `noul` is the primary signal; `kind` and `severity` enrich the
recovery routing and the review queue. `noul` is also the primitive mmBERT is
strongest at (cross-lingual NLI, 0.731 XNLI on 14 languages).

## How it slots in

Laya is **text-only** — it reads the text after extraction, whatever produced it
(PDF text layer, OCR output, ASR segment). The image-level gates
(`classify.py`, `vision.py`) are unaffected.

It would sit in the classify slot, **after** the deterministic pre-filters and
**before** any expensive recovery, replacing or narrowing:

- `converter/need.py` — the generative "needs fixing?" gate becomes a typed
  `noul`/`choice` per item (one batched `predict_batch` per document).
- `converter/base.text_layer_is_garbage` — the English-only heuristic is kept as
  a cheap first line, but Laya generalises it to any language and to ASR garble.
- The STT coherence scoring (out-of-repo) — replaced by a calibrated `garbled`
  probability.

Recovery then runs only on the flagged chunks, and only down the path the `kind`
decision suggests (re-word / re-OCR / re-ASR / canonical injection / LLM edit),
each still behind the existing verbatim word-gate and human review.

Confidence gating mirrors ADR-0039 exactly:

```
if P(garbled) < low:    skip   # clean
if P(garbled) > high:   recover (auto, still word-gated)
else:                   review  # ambiguous -> human
```

## Serving and integration

Laya ships an HTTP server (`pip install laya[serve]` → `laya-serve`) speaking the
Jev-compatible `POST /v1/systemone` protocol. It is **not** an OpenAI
`/v1/chat/completions` endpoint, so it cannot reuse `converter.vision._chat_completion`;
a small client (or the `laya-mcp` stdio tools) is needed.

To wire it into the existing server catalog (ADR-0029):

1. Add a `laya` daemon row to the `macos-dev-config` fleet manifest (models.json)
   and a `Server(name="laya", daemon="laya", runner="laya-serve", …)` row to
   `converter.config.SERVERS` (a free port clear of the `:8080–:8090` ML block).
2. Add a `Feature` row (`RECOVER_ENABLED` or `LAYA_ENABLED`) and a
   `_laya_url()` resolver, following the `need.py` / `summary` pattern.
3. Batch with `Router.predict_batch` so all chunks of a document share forward
   passes.

**Open point:** `laya-serve` serves the *public* checkpoints by name; a
fine-tuned checkpoint either needs a private Hugging Face push + reference, or a
thin FastAPI wrapper (based on the repo's `examples/server.py`) that loads the
local checkpoint. This is the main integration unknown.

## Fine-tuning

The pipeline is Laya's own notebook loop: build the dataset → train with **RLCD**
(proper-scoring-rule rewards, GRPO-style) on the frozen mmBERT encoder → fit
calibration temperatures → evaluate → push to the Hub.

### Labelling

The repo already emits labels for free:

- **word-gate rejections** from `structure.py` (`omitted: … / added: …`) → OCR-garbage
  examples.
- **`need.py` decisions** (needs_fix / clean, logged per page) → reformat examples.
- **the STT review queue** from the parse prototype → ASR-garbage / disfluency /
  clean examples, already triaged by deterministic signals.

One JSONL row per chunk:

```json
{"id":"[00:01:15]:S#15","source":"stt","text":"Eller downtown, egentlig.",
 "garbled":true,"kind":"asr_substitution","severity":2}
```

### Calibration and split

- `laya-multilingual` ships with **no fitted temperatures** and is over-confident;
  fit one temperature per (question type, option count) on a **held-out** slice —
  not the training items (the notebook's own calibration samples come from the
  train set, which the docs warn against).
- With ~140 cells across two interviews, prefer **leave-one-interview-out** over a
  single split; revisit once more interviews land. Report **ECE** after fitting,
  then choose the P(garbled) thresholds for skip / recover / review.

### Hardware and volume

- The reference fine-tune ran on Kaggle's free 2×T4 (~4–5 h for 30k questions);
  ours is tiny, so it is minutes on a T4. Apple MPS is unproven — plan for
  Colab/Kaggle.
- ~140 cells × 3 decisions ≈ 420 labels is small. **Prioritise `garbled` (noul)**
  — the smallest and most reliable target — and treat `kind`/`severity` as bonus
  heads that fill in as data accumulates.

## Swapping the encoder

The **head** is tied to a specific encoder, but the **recipe** is not. You
cannot drop a different model in and keep the shipped head weights — the head's
dimensions and learned vectors match `mmBERT-base`'s (or `ModernBERT-large`'s)
hidden states, so a different encoder makes them meaningless. What you *can* do
is reuse Laya's training loop and train a **fresh head on a different frozen
encoder** (the same pattern the community `stuntd` tool uses: "trains a head per
decision on the frozen encoder").

Constraints on the replacement:

1. **Must be a masked-language-model encoder** — Laya scores each option at a
   `[MASK]` token, so it needs a bidirectional BERT-family model (BERT, RoBERTa,
   XLM-R, ModernBERT, mmBERT, NorBERT, …). A decoder (Llama/Mistral/NorMistral)
   has no `[MASK]` and won't fit the scheme.
2. **Hidden size changes the head** — base (768) vs large (1024) means rebuilding
   the head; that is a trivial linear layer, the cheap part.
3. **You lose the shipped heads** — every head (`garbled`, `kind`, `severity`) is
   trained from scratch on your labels.
4. **You lose the `Router`** — it selects among the three *shipped* checkpoints, so
   a custom encoder means self-hosting a custom checkpoint (see the serving
   caveat below), not `laya-serve` by name.
5. **Re-calibrate** — a new head is over-confident again, so refit temperatures
   before gating.

Why you might do it here: `mmBERT-base` is a generalist over 1800+ languages, so
a **Norwegian-specific encoder** — `NorBERT` (NorwAI) or one of `NbAiLab`'s
Norwegian BERTs (the same org as `nb-whisper-large`) — would plausibly encode
Norwegian ASR text better. The trade is *better Norwegian accuracy* against
*losing out-of-the-box multilingual coverage* and *training every head yourself*.
Worth a spike only if mmBERT zero-shot/fine-tuned proves weak on the transcripts.

## Confidence calibration & gating policy

| Quantity | Meaning | Use |
| --- | --- | --- |
| `confidence` (choice/score) | 1 − normalised entropy over the options | rank, not a probability of correctness |
| `answer_confidence` | probability of the reported answer | the number to gate on |
| fitted `temperature` | per (type, option-count) | makes the above calibrated |

Gating uses `answer_confidence` (or the `noul` P(true)) after temperature
fitting; the raw `confidence` is only a ranking aid. The repo's own guidance —
"gate on confidence, which reaches AUROC 0.77; `act_probability` carries no
usable signal (#185)" — applies unchanged.

## Open questions / decision points

1. **Zero-shot baseline first** — measure `garbled` `noul` zero-shot against the
   current Mistral coherence score before committing to fine-tuning; it also
   doubles as the pre-labeller.
2. **Volume threshold** — fine-tune now on ~140 cells, or accumulate ~300 across
   4–5 interviews first.
3. **Primitives** — ship `garbled` first, or all three heads?
4. **Serving a custom checkpoint** — private Hub push vs a thin wrapper around
   `examples/server.py`.
5. **Wire protocol** — add a `/v1/systemone` client (and optionally a
   `laya-mcp` tool) rather than bending it into `_chat_completion`.
6. **Encoder swap** — if mmBERT underperforms on Norwegian, spike a
   Norwegian encoder (`NorBERT` / `NbAiLab` Norwegian BERT) with a retrained
   head before accepting the multilingual checkpoint's ceiling.
