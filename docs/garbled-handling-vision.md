# Garbled handling — a vision

The forward-looking companion to [`garbled-text-recovery.md`](garbled-text-recovery.md)
(the shared pipeline as it exists today) and [`ai-laya.md`](ai-laya.md) (the
proposed typed classifier). This page states *where we want to be*: the
principles and the layered machinery for treating garbled text as a recoverable
artifact rather than a failure.

## The vision

One language-aware, confidence-calibrated pipeline that, for any text that came
out of an extraction step — a parsed PDF text layer, OCR, machine translation,
or speech-to-text — can:

1. **say cheaply whether a chunk is garbled**, without a heavy model;
2. **say what kind of garble and how severe**, as a typed decision with a
   calibrated confidence;
3. **propose a repair** without inventing content; and
4. **only trust a repair** that survives a word-preservation gate and, where the
   gate cannot decide, human review.

Garbled text is the normal failure mode of extraction, not an edge case. The
goal is not to eliminate it — it is to make its handling *uniform, cheap, and
auditable* across sources, instead of the per-module heuristics we have today.

## The three decisions every recovery makes

Before any repair, decide what "classify" means. There are three independent
questions, and they map directly onto typed decision primitives:

| Question | Shape | Primitive |
| --- | --- | --- |
| Is it garbled? | binary: clean vs garbled | `noul` (P(garbled)) |
| What kind? | multi-class: OCR / ASR / MT / typo / disfluency | `choice` |
| How bad? | ordinal: readable / partial / gibberish | `score` |

The vision is to answer all three **uniformly** for every source, rather than
the current mix of an English-only deterministic signal, a generative
"list the indices" gate, and an ad-hoc coherence score.

## A layered signal stack (cheap → expensive)

Garbled text is not read like prose, so word *meaning* is the wrong first
signal. Detection works bottom-up, from characters to context:

1. **Character / structural** — character n-grams, punctuation-to-letter ratio,
   digit and uppercase density, symbol frequency. This catches `g@rbled`,
   vowel-less `gmnes`, stray `1955` — with no model and no dictionary. Already
   the idea behind `base.text_layer_is_garbage` (ADR-0023).
2. **Lexical / dictionary vouching** — the real-word ratio and one-edit
   near-misses of known words. High corruption ⇒ low real-word ratio. Today this
   is English-only (`_english_words.py`); the vision generalises it to other
   languages via edit distance, not a fixed word list.
3. **Semantic / perplexity** — a language model's surprise at the sentence
   (GPT-2/BERT-style perplexity, or a coherence score). High perplexity ⇒ likely
   garbled. This is the layer the STT prototype's coherence score occupies, and
   it is the *coarsest* layer — genuine disfluency and mild word substitution
   overlap with error, so it ranks rather than decides.
4. **Typed decision** — the System-1 classify step (`noul`/`choice`/`score`) with
   calibrated confidence. This is the layer that finally turns the lower
   signals into a decision you can gate on; see `ai-laya.md`.

Layers 1–2 are deterministic and always on; layer 3 is a ranking aid; layer 4 is
the decision. The ordering is the same **cheap-before-expensive** discipline as
`router.py` (ADR-0024).

## The classifier ladder

There is a spectrum of model choices, and the vision is to land on the *right*
one for each layer rather than one model to rule them all:

| Tier | Fits | Example |
| --- | --- | --- |
| Traditional ML | small data, fast, cheap features | char-level TF-IDF + SVM / Random Forest |
| Sequence deep learning | character sequences and typos | Char-CNN, Bi-LSTM |
| Transformer | context around the garbled span | BERT / RoBERTa fine-tuned on noisy data |
| Non-autoregressive System-1 | typed decisions, calibrated, ~ms | Laya (the proposed classify layer) |

Layers 1–2 stay deterministic; layers 3–4 converge on the non-autoregressive
System-1 classifier, because it returns *typed, calibrated* answers with no
generation to hallucinate or parse. The transformer tier remains the right tool
for the *repair* step, not the detection step.

## Repair without invention

Detection and classification only *find* garble. Repairing it is a separate act
with a hard rule: **a repair may fix, never fabricate**. Every proposed repair
passes a verbatim word-preservation gate (drop nothing, invent nothing) before
it is applied — the discipline already enforced by `structure.py`,
`transcript_edit._safe_edit` and `format.py` — and anything the gate cannot
decide goes to human review with provenance retained. A garbled word we cannot
recover confidently is better kept verbatim than "improved" into a lie.

## The data problem: synthesize to teach

Garbled data is scarce, especially per corruption class and per language. The
vision has two label sources:

- **Natural** — labels the pipeline already emits for free: word-gate rejections
  (`omitted: … / added: …`), `need.py` decisions, and the STT review queue.
- **Synthetic** — deliberately break clean text so every corruption class has
  examples: random insertion (`he!lo`), keyboard-adjacency swaps (`o`↔`p`),
  and visual mimicry for OCR (`l`→`1`, `O`→`0`, `rn`→`m`). Automate with
  `nlpaug`/`Augmentor` or a small in-repo corruptor.

Synthesis is how we teach the classifier the rare classes (OCR homoglyphs,
keyboard typos) and how we reach the volume a 322M classifier needs without
waiting for real corrupted text to accumulate — which matters most for
low-resource languages.

## The language dimension

The current detection is **English-only** — `text_layer_is_garbage` cannot flag
Norwegian garble, and the STT prototype had to hand-roll Norwegian signals that
still missed word substitutions. The vision is language-aware from the start:
a multilingual encoder (mmBERT's 256k vocab) plus per-language synthetic
corruption, so "garbled" means the same thing for Norwegian speech-to-text as
for English OCR.

## Principles

1. **Cheap before expensive** — deterministic signals skip model calls (ADR-0024).
2. **Never silently rewrite** — repair behind a word-preservation gate.
3. **Calibrated confidence** — gate on a measured probability, not a threshold guess.
4. **Language-aware, not English-only.**
5. **Synthesize labels** — teach the rare classes, don't wait for real corrupted data.

## Open questions

- Which layers to build first: solidify deterministic + lexical (generalise past
  English), then land the typed System-1 classifier, then the repair tier?
- Where the shared `recover.py` orchestrator lives, and how each source registers
  its own recover step.
- How much synthetic vs natural data is needed before fine-tuning beats the
  current coherence score.
