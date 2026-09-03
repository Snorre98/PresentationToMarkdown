# 0023. Pre-call OCR-garbage self-check in the structure pass

- Status: Accepted
- Date: 2026-09-04

## Context

The paper-mode structure pass (ADR-0011) routes a page to its *text regime*
whenever `text_layer_quality` (ADR-0020) reports the layer "usable", then runs a
check-and-amend on the writer VLM (Qwen2.5-VL-7B on `:8081`) behind a verbatim
word-gate. On "03 What makes things fun to learn.pdf" this burned ~9.3 minutes:
all 8 pages passed `text_layer_quality` as "usable", each spent ~60–77s in the
VLM, and every page was then rejected by the word-gate
(`omitted: … / added: …`, e.g. `added: california, constitute, research` against
`cali, comlmter, constipate, gmnes, insiructio`).

The gate is correct — a model told to "never fix garbled words" cannot restructure
a layer full of OCR typos without normalizing or dropping them — but it fires
*after* a ~70s call that could have been predicted to fail. Root cause:
`text_layer_quality` (`_TEXT_QUALITY_MIN_WORDS=12`, `MIN_TTR=0.35`,
`MIN_MEAN_LEN=10.0`) measures only word count, unique-word ratio and mean line
length, so a *dense* garbled layer — many distinct, plausible-length typo tokens —
passes as "usable". ADR-0018/0020 fixed sparse/empty garbage; dense garbage with
high lexical variety was the remaining gap.

## Decision

Add a cheap, deterministic **pre-call garbage self-check** and use it to route a
text-regime page to `skip` before any model call.

### Signal (`converter.base.text_layer_is_garbage`)

Beside `text_layer_quality`, a new function classifies the layer's tokens
(the same `[A-Za-z0-9]{4,}` tokenization) as **junk** when a token either:

- holds no vowel in its alphabetic core (``gmnes``, pure digits ``1955``), or
- is one edit (deletion, transposition, substitution, or insertion) away from a
  bundled English word (``metagoa``→"metagoal", ``numbdr``→"number",
  ``interesti``→"interest", ``gucssing``→"guessing") — the telltale fingerprints
  of OCR.

The layer is garbage when it holds at least `_GARBAGE_MIN_TOKENS` (60) tokens
*and* at least `_GARBAGE_MIN_JUNK` (2) junk tokens. The absolute token floor stops
thin legitimate pages (a 30-token page with one real proper noun) from ever being
flagged; the fixed threshold means one stray citation does not flip a clean page.

The English vocabulary is bundled in `converter/_english_words.py`
(`ENGLISH_WORDS`, sourced from the public-domain `words_alpha.txt` list, lowercase
alphabetics of 2–12 chars), so the signal has no runtime dictionary dependency.
Words longer than 12 chars are never judged, so long domain terms cannot
false-positive.

### Routing change (`converter.structure._page_regime`)

The `usable` branch now additionally checks the garbage signal:

```python
if _text_coverage(page.line_meta) != "usable":
    return "skip"
if text_layer_is_garbage([m.get("text", "") for m in page.line_meta]):
    return "skip"
return "text"
```

A garbage-but-"usable" page keeps its deterministic Markdown and is skipped
instead of being routed to the 7B check-and-amend. The verbatim word-gate in
`_amend_page_text` is retained unchanged as the backstop for pages whose text
layer is genuinely prose.

Only the structure pass uses this signal (Phase 1 scope): `converter/pdf.py`'s
`page_via_vision` routing is untouched, so non-structure conversion output is
byte-identical.

## Consequences

- The observed paper's structure pass performs **zero** model calls and its output
  is byte-identical to the deterministic paper-mode output (verified end-to-end);
  ~9.3 minutes of wasted VLM time is eliminated for that document.
- Failure is graceful and local: a skipped page gets no warning in Phase 1 (it
  keeps its deterministic output silently, consistent with the existing
  "already-handled" skip regime), and the skip can never fail a conversion.
- Calibration is a fixed heuristic, not a measured model (consistent with
  ADR-0011's "confidence calibration deferred"). The threshold (2 junk tokens on a
  60+ token page) is deliberately conservative on the false-positive side; the
  observed garbage pages carry 20/4/2/10/11/2/19/17 junk tokens, the clean
  fixtures 0–1.
- The bundled word list adds ~4 MB of source data to the repo; it is data, not
  code, and is consumed only by this deterministic signal (no observability role).

## Alternatives considered

- **Route garbage to the image regime** (reword from the rendered PNG) — would
  rescue a scan's text, but spends another ~70s VLM call per page to re-read a
  layer whose prose is *mostly* present, and changes output. Rejected; skipping
  is cheaper and byte-identical.
- **Extend `text_layer_quality` with a word-fraction threshold** (share the
  existing `_TEXT_QUALITY_*` constants) — simplest, but the observed layer is
  ~87–96% real words (citations, names and prose), so non-dictionary fraction
  alone cannot separate it from clean text. Rejected; the near-miss typo signal is
  what discriminates.
- **Character n-gram plausibility table** — empirically weak on word-like typos
  (``metagoa`` contains mostly common trigrams). Rejected.
- **Let the word-gate alone decide** — the status quo that spent the ~70s/page.
  Rejected as the very cost being removed.
