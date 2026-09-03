# 0021. Dedicated summary model and lean RAG retrieval

- Status: Accepted
- Date: 2026-09-03

## Context

The per-presentation summary pass (`SUMMARY_ENABLED`, `converter/summary.py`)
indexes each slide and has a chat model write a standardized `# Summary` header.
On this machine the end-to-end pass was dominated by three costs, measured via
`ptm.sqlite` `vision_events.latency_ms`:

1. **The writer generation.** `SUMMARY_*` fell through to `WRITE_*` (ADR-0016),
   i.e. Qwen2.5-VL-7B on the `:8081` mlx-vlm server — a 7B *vision-language*
   model loaded and run just to emit a few bullets (~1–2 min per attempt, doubled
   by the unconditional retry).
2. **Embedding round-trips.** Each run embedded slides *and* a `["probe"]`
   string (to learn the vector dimension), plus three fixed retrieval queries,
   as separate calls — each a round-trip to a possibly-cold embedding model.
3. **Full-deck retrieval.** `top_k = len(slides)` retrieved *every* slide,
   defeating the KNN selectivity and padding the prompt.

## Decision

Lean the pass down without dropping the sqlite-vec RAG (the user confirmed RAG
should remain):

1. **Dedicated summary model.** `SUMMARY_BASE_URL`/`SUMMARY_MODEL` now default to
   a dedicated `summary` server (a small `mlx-lm` chat model,
   `mlx-community/Llama-3.2-3B-Instruct-4bit` on `:8084`) instead of the writer
   VLM. No 7B VLM is loaded for a short text summary. Overridable as before.
2. **Faster embeddings.** `EMBED_MODEL` defaults to `nomic-embed-text`; the
   dimension is derived from the *actual* embedding output (no `["probe"]`
   round-trip) and cached in `meta`, so warm re-runs re-embed only changed
   chunks and never probe.
3. **Bounded retrieval.** `top_k` is capped at `_RETRIEVE_TOP_K = 12` (was
   `len(slides)`), keeping the prompt under the context budget while restoring
   KNN selectivity.
4. **Shorter generation.** `SUMMARY_MAX_TOKENS` 2048 → 900.
5. **Retry only on garbled output.** The retry is no longer unconditional: a
   parseable-but-thin first attempt falls back to the deterministic header
   without a second call; only a structurally broken reply (no parseable
   sections, a section with no bullets, or low token diversity — `_looks_garbled`)
   is retried once. Model-call *exceptions* still `break` immediately.
6. **Strip code fences.** The summary reply runs through `strip_code_fences`
   (introduced in ADR-0020) before parsing, so a fenced model answer no longer
   spuriously fails validation and triggers a wasteful retry.

## Consequences

- Default summary latency drops: no 7B VLM, probeless + batched embeddings, a
  bounded prompt, shorter generation, and no unconditional retry.
- New config surface: `summary` server entry in `SERVERS`
  (`converter/config.py`) and a matching `summary` row in the sibling
  `servers.conf` (`:8084`); `_summary_url()` resolves `SUMMARY_BASE_URL` →
  that server. `SUMMARY_*`/`EMBED_*` remain import-time env vars (ADR-0002).
- `SUMMARY_*` no longer follows `WRITE_*` — this is a deliberate break from
  ADR-0016's fallback chain, scoped to the summary role only (format/structure/
  interpret still follow `WRITE_*`).
- Determinism and never-fails are preserved: the pass still degrades to the
  deterministic extractive header on any failure.
- A model whose embedding dimension differs from the cached `meta` value still
  triggers a rebuild (unchanged `_store_dim` logic).

## Alternatives considered

- **Keep summary on the writer VLM** — zero config churn, but keeps the largest
  single cost (a 7B VLM for a few bullets) and forces the big model resident
  just for this pass. Rejected.
- **Drop RAG for a pure extractive header** — removes embeddings entirely and is
  the fastest option, but loses the retrieval-then-abstract quality the user
  explicitly wants to keep. Rejected per request.
- **No `top_k` cap / no retry tuning** — simpler, but leaves the full-deck prompt
  and the doubled 7B worst case in place, which is exactly the latency being
  addressed. Rejected.
