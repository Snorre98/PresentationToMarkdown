# 0024. Centralized pre-execution pipeline router and structure text-model downgrade

- Status: Accepted
- Date: 2026-09-04

## Context

ADR-0023 removed the single worst time-burn by adding a garbage self-check,
but it left the pipeline's *dynamics* scattered: whether a page routes to the
text/image/skip regime, whether an image is worth transcribing, and whether a
page is already vision-handled were decided by ad-hoc signals buried in
`converter/pdf.py` (`page_via_vision`), `structure.py` (`_page_regime`),
`classify.py` (`maybe_transcribe_image`, `transcribe_complex_page`), and
`ocr_columns.py` (per-column fidelity/quality fallbacks). Each pass decides
*inside itself*, often after running the expensive step it should have predicted
was unnecessary.

Separately, the structure pass's **text regime** still ran on the writer VLM
(Qwen2.5-VL-7B). That regime is a pure text-to-text check-and-amend — it sends no
image — yet it pays for a vision-language model, the same over-billing ADR-0021
eliminated for the summary role.

## Decision

### Centralized, deterministic router (`converter/router.py`)

Introduce a pure decision layer that, per page and per pass, resolves an action
from **cheap-before-expensive** signals in a strict order:

1. **skip** — a cheap deterministic signal already proves the step unnecessary:
   text-layer quality, the garbage self-check (ADR-0023), image readability,
   page complexity, or a prior-pass outcome (already vision-handled);
2. **downgrade** — the step can run on a cheaper model;
3. **run** — otherwise.

The layer is *pure*: it issues no model call, writes nothing, and never raises.
A decision can only make a conversion do *less* work, so it can never change
deterministic output or fail a conversion (ADR-0002 determinism preserved).

`structure_regime(line_meta, md_lines)` is the first consolidated signal: it owns
the structure pass's `text`/`image`/`skip` routing (previously `_page_regime`),
composing ADR-0020's single-content-source-per-page resolution with ADR-0023's
garbage check. `structure._page_regime` now delegates to it, so the routing is
legible from one place.

The router is introduced incrementally: it currently owns the structure routing
and the downgrade decision; consolidating `pdf._page_to_md`'s `page_via_vision`
and `classify`'s readability/category gates into the same layer is the follow-on
and is intentionally left to subsequent commits, since those call sites already
implement the same cheap-before-expensive order in place.

### Model downgrade for the structure text regime

Move the structure text regime off the writer VLM onto a **dedicated small text
model**, mirroring ADR-0021's summary-model shape:

- a new `config.SERVERS["structure-text"]` entry (`mlx-lm`,
  `mlx-community/Llama-3.2-3B-Instruct-4bit`, `:8085`), plus
- `STRUCTURE_TEXT_BASE_URL` / `STRUCTURE_TEXT_MODEL` / `STRUCTURE_TEXT_API_KEY`
  env vars (defaulting to that entry).

`structure._amend_page_text` selects the model via `structure_text_downgrade()`:
when `STRUCTURE_TEXT_MODEL` differs from the writer VLM it uses the small model
(`Route.DOWNGRADE`), otherwise it stays on `STRUCTURE_*`. The **image regime**
still sends the rendered page PNG and therefore stays on the writer VLM. This
respects ADR-0016 (the writer stays a capable instruction model; the small text
model is a writer-role model, never an OCR specialist) and ADR-0017 (a small
model is cheaper to load/keep resident; `lifecycle.release_writers` releases it).
The text regime's prompt is text-only, so the downgrade loses no capability.

## Consequences

- Pre-execution signals are legible from `converter/router.py` rather than
  scattered across pass modules; the failure (and skip) behaviour is unchanged
  and stays local-to-page, graceful, and byte-identical for the non-AI path.
- The structure text regime no longer loads the 7B VLM for a text-only task;
  on the observed paper this is moot (its pages are skipped pre-call, ADR-0023),
  but any *genuine* paper page now pays a small-model token cost instead of a
  VLM token cost.
- New config surface: `structure-text` server entry and `STRUCTURE_TEXT_*` env
  vars, import-time per ADR-0002/0012, and reachable via `--env`. Returned in
  `config.feature_endpoints("structure")` (secondary to the writer endpoint) so
  the GUI health check probes it.
- Calibration of the routing signals remains a fixed heuristic (ADR-0011's
  "confidence calibration deferred" posture), not a measured model.

## Alternatives considered

- **Do nothing beyond ADR-0023** — leaves the routing scattered and the text
  regime overbilled. Rejected; this ADR's router is the abstraction the task
  needs, and the downgrade is a concrete saving.
- **Move the whole structure pass (image regime too) off the VLM** — impossible:
  the image regime reads the rendered page. Rejected; only the text regime
  downgrades.
- **Reuse ADR-0021's `summary` server for the text regime instead of a new
  entry** — zero new topology, but couples two passes' model choice (repointing
  `SUMMARY_*` would silently re-point the structure text model, re-creating the
  ADR-0016 "two variables at once" trap). Rejected in favour of a dedicated
  `structure-text` entry.
- **A full rewrite of every pass to consume the router** — the correct end state,
  but larger than this change; the router is introduced where it is currently
  needed and extended incrementally.
