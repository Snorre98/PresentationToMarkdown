# 0039. Pre-flight inference gate before expensive rewrite passes

- Status: Accepted
- Date: 2026-09-19

## Context

The optional AI passes spend local inference on pages/slides that do not need
it. ADR-0023 removed the structure pass's worst time-burn (dense OCR garbage
that could only fail the verbatim word-gate) and ADR-0024 centralized the
structure routing behind a deterministic `converter/router.py`. But the same
waste remains in two places that the router does not yet cover:

- The **format pass** sends *every* slide to the writer VLM (Qwen2.5-VL-7B),
  then word-gates the reply *after* the call (`converter/format.py`). A slide
  whose deterministic output is already clean still pays a full 7B call.
- The **structure text regime** check-and-amends *every* "usable" prose page;
  only garbage/sparse layers are skipped pre-call. A page whose deterministic
  structure is already correct still pays the call.

The agreed lever is a **cascade**: decide with cheap signals — and, for the
ambiguous remainder, a cheap *model* — whether the expensive rewrite is needed
at all, before spending it. This generalizes ADR-0023/0024's
"cheap-before-expensive" order from the structure pass to the format pass.

## Decision

### Three-state gate (`NEED_GATE`)

Introduce a `NEED_GATE` toggle, read **lazily inside `convert`** (the same
pattern as `PDF_MODE`, so the GUI/CLI can flip it per conversion without a
restart). It has three states:

1. **`off`** — the gate code path never runs; the format/structure passes behave
   exactly as today. This is the A/B **baseline** ("current implementation").
2. **`on`** — the gate actually skips expensive calls.
3. **`shadow`** — output and compute are identical to `off` (the expensive pass
   always runs), but the gate *also* runs and logs its verdict. This is the
   single-run **ground truth** that measures what `on` would have skipped.

### Cascade (`converter/need.py` + `router.format_clean`)

Per page/slide, resolve an action from cheap-before-expensive signals:

1. **skip** — a deterministic signal already proves the rewrite unnecessary:
   `router.format_clean` (no mid-sentence wrapped lines, no bold lead-ins, no
   heading-like bullets) for the format pass, and the existing
   `structure_regime` for the structure pass.
2. **downgrade/run** — a batched **text-only** small model
   (`converter/need.py`) classifies the *remaining* slides/pages in one call per
   document ("which of these numbered slides/pages need reformatting?") and only
   the ones it names proceed to the expensive writer pass.

The small model reuses the `text` daemon (`Llama-3.2-3B`, already resident for
`structure-text`/`summary` — ADR-0017), so no new server or resident model is
added. The gate is **pure in effect**: it can only make the conversion do *less*
work, never more, and it never changes deterministic output (ADR-0002) or fails
a conversion (ADR-0024). On any gate failure the need-set resolves to "run all",
degrading to today's behavior.

### A/B harness (`ptm-ab`)

A dedicated `cli_ab.py` / `ptm-ab` command (the `cli_transcribe.py` pattern)
that, per input, runs `compare` (an `off` run and an `on` run, side-by-side
outputs + per-slide diff + latency/token deltas) and `shadow` (one `shadow` run
whose `stage="need"` rows are joined to the `stage="format"`/`"structure"` rows
on `run_id + source + page`, yielding a true-skip/false-skip table). The
`format`/`structure` `decision` column gains an `unchanged` value (an accepted
reply that is byte-identical to its input) so a "false skip" — the gate said
clean but the expensive pass *changed* the page — is measurable. No schema
migration: all of this rides on the existing `vision_events` columns plus the
`run_config` snapshot.

## Consequences

- The format pass stops paying a 7B call per clean slide; the structure text
  regime stops paying per already-structured page.
- Every gate decision is logged to `logstore` (`stage="need"`,
  `decision=clean|needs_fix|error`), so the savings and the false-skip rate are
  visible in the existing dashboard.
- `NEED_GATE=off` is byte-identical to the pre-change pipeline, giving a
  trustworthy A/B baseline within one build.
- New config surface: `NEED_BASE_URL` / `NEED_MODEL` / `NEED_API_KEY`
  (import-time, ADR-0002/0012), defaulting to the `text` server; `NEED_GATE` is
  lazy-read. Registered under the `format` and `structure` endpoints so the GUI
  health probe covers it.
- Calibration remains a fixed heuristic + a small model, consistent with
  ADR-0011's "confidence calibration deferred" posture; the `shadow` mode is the
  calibration instrument.

## Alternatives considered

- **Deterministic signals only** — free, but misses the ragged cases heuristics
  cannot see; the cheap model handles the residual. Rejected as insufficient on
  its own.
- **Cheap model only** — simpler, but adds latency/tokens for slides the
  deterministic signal already catches for free. Rejected in favour of
  deterministic-first.
- **Per-slide/per-page gate calls** — finer control, but N small-model
  round-trips negate much of the saving. Rejected in favour of one batched call
  per document.
- **Route garbage/clean decisions through the vision VLM** — re-introduces the
  very over-billing ADR-0024 removed. Rejected.
