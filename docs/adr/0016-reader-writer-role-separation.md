# 0016. Reader/writer role separation for the AI passes

- Status: Accepted
- Date: 2026-09-03

## Context

The AI passes form two distinct *roles* that were previously glued together by
one environment-variable chain. The **reader** role (OCR) transcribes images
verbatim — `vision` (`VISION_*`) and the classifier gate (`VISION_CLASSIFY_*`).
The **writer** role (rewrite) restructures and condenses prose — `format`
(`FORMAT_*`), `structure` (`STRUCTURE_*`), `interpret` (`INTERPRET_*`), and
`summary` (`SUMMARY_*`).

Because every writer pass cascaded to the reader's defaults
(`FORMAT_*` → `VISION_*`, `STRUCTURE_*` → `FORMAT_*`, `INTERPRET_*` → `VISION_*`,
and `SUMMARY_*` was hardcoded to the transcriber server), repointing the reader
changed the writer at the same time. This made a clean OCR A/B impossible.

The failure was observed concretely while A/B-testing the vision model. Setting
`VISION_MODEL=glm-ocr` (a 0.9B OCR specialist) to improve *reading* silently
also re-pointed the *rewrite* passes to GLM-OCR, which is weak at rewriting. The
`ptm.sqlite` `vision_events` for the GLM-OCR run logged `structure` → `anchors
dropped` on all 8 pages, plus `added: obstacle / made / section` (hallucinated
words) and `omitted: compelling / intrinsic` (dropped words), and the paper's
`metagame` was normalized to `metagoal`. Repointing `VISION_*` had changed two
variables at once — the reader *and* the reworder — so the A/B was not clean.

## Decision

Introduce a **writer default** that the rewrite passes share, independent of the
reader. A new set of environment variables defaults the writer to the
transcriber server (Qwen2.5-VL-7B on `:8081`):

- `WRITE_BASE_URL` (default `http://127.0.0.1:8081/v1`)
- `WRITE_MODEL` (default `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`)
- `WRITE_API_KEY`

The constants live in a new `converter/write.py` (mirroring `converter/vision.py`
for the reader), and the writer passes re-point:

- `FORMAT_*` → `WRITE_*` (was `VISION_*`)
- `STRUCTURE_*` → `FORMAT_*` (unchanged, now transitively `WRITE_*`)
- `INTERPRET_*` → `WRITE_*` (was `VISION_*`)
- `SUMMARY_*` → `WRITE_*` (was hardcoded to the transcriber server)

`VISION_*` and `VISION_CLASSIFY_*` remain the reader (OCR) role, untouched.
`config.py`'s base-URL resolvers (`_format_url`/`_interpret_url`/`_structure_url`/
`_summary_url`) mirror the same chain through a new `_write_url()`.

The writer stays a VLM: `structure`'s image regime sends the rendered page PNG,
so `WRITE_MODEL` must be able to read images (Qwen2.5-VL-7B qualifies). The
writer must never be pointed at an OCR specialist like GLM-OCR.

## Consequences

- With nothing overridden the configuration is byte-identical: every model
  defaults to Qwen2.5-VL-7B, so existing output is unchanged.
- Repointing `VISION_MODEL` now affects **only** the reader; `FORMAT_MODEL` /
  `STRUCTURE_MODEL` / `INTERPRET_MODEL` / `SUMMARY_MODEL` follow `WRITE_MODEL`.
  This is what makes a clean OCR A/B possible.
- One new module (`converter/write.py`), UI-free and deterministic like the rest
  of `converter`. Env vars remain import-time, reachable via `--env`, per
  ADR-0002/ADR-0012.

## Alternatives considered

- **Keep the single-model cascade** — least churn, but the A/B contamination
  above is exactly what this ADR exists to fix. Rejected.
- **Decouple summary into a separate command** — isolates the writer from the
  reader, but adds a whole new CLI/UX surface for a pass that is already
  opt-in and best-effort. Rejected as disproportionate.
- **Independent defaults per writer pass** (no shared `WRITE_*`) — maximum
  flexibility, but a user who re-points the writer would have to set four vars,
  and it re-creates the "two variables at once" trap at a different level.
  Rejected in favour of one shared writer default.
