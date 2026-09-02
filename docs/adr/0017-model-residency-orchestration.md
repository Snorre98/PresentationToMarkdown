# 0017. App-level model-residency orchestration across the local servers

- Status: Accepted
- Date: 2026-09-03

## Context

This 32 GB M4 has no VRAM cap and no OOM killer: macOS will happily swap a
memory-starved system into heavy compression rather than kill a process. During
vision A/B testing, the vision model (GLM-OCR on Ollama, ~3.5 GB), the summary
model (Qwen2.5-VL-7B on mlx-vlm, ~6.6 GB) and the classifier (Qwen2.5-VL-3B,
~1.5 GB) ended up resident at once, pushing the machine to 31/32 GB used.

Server-level configuration exists in the sibling repo
`../macos-dev-config/ollama/ollama.env` (`OLLAMA_MAX_LOADED_MODELS=1`,
`OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1`), but it only governs
Ollama. **mlx-vlm never auto-unloads**, so a 7B VLM loaded for one pass persists
across runs and conversions. Passive config is therefore necessary but not
sufficient: the app must actively release models between passes.

Both runners expose a programmatic unload:

- **mlx-vlm** — `POST <root>/unload` (no auth unless started with a key; this
  machine's servers aren't).
- **Ollama** — `POST <root>/api/generate` with `{"model": "<model>",
  "keep_alive": 0}` (the native API at the host root, NOT under `/v1`).

## Decision

Add `converter/lifecycle.py` with a best-effort, never-raising residency API, and
wire release points into the conversion pipeline.

### Release primitives

- `release_model(runner, base_url, model=None)` — `"ollama"` posts the
  `keep_alive: 0` generate call (skipped when no model is given, since a
  targeted unload needs a model name); `"mlx-vlm"` posts `/unload`; unknown
  runners are a no-op. `<root>` is `base_url` minus a trailing `/v1`. 5-second
  timeout; every exception is swallowed — a failed unload only means the model
  lingers.
- `resolve_runner(base_url)` — maps an effective base URL to a runner by matching
  host:port against `config.SERVERS`, normalizing `localhost` ↔ `127.0.0.1` (a
  user sets `VISION_BASE_URL=http://localhost:11434/v1` but the catalog records
  `ollama` on `127.0.0.1`). Unresolved URLs fall back to trying **both** unloads,
  which is idempotent.
- `release(base_url, model=None)` — resolve then unload, or try both.

### Release points in `convert_file`

Gated on the relevant features being enabled, and only when the endpoints
actually differ:

1. **Before `prepend_summary`** — `release_readers()` releases the reader
   (`VISION_*`) and the classifier (`VISION_CLASSIFY_*`) when their
   `(base_url, model)` differs from the `WRITE_*` target, so the writer
   (Qwen2.5-VL-7B) loads into freed memory.
2. **After `prepend_summary`** — `release_writers()` releases the writer
   (`WRITE_*`) and, when summary ran, the embeddings model (`EMBED_*`), so
   nothing lingers into the next conversion in the same session.

Neither step releases when reader and writer resolve to the same model/server —
the default all-Qwen case releases nothing, avoiding a pointless unload/reload
churn. Releasing never changes output and is a no-op when AI features are off.

## Consequences

- The multi-model pipeline keeps at most one big model resident at a time; the
  `ollama.env` settings remain a passive backstop for Ollama's own scheduling.
- `converter` stays UI-free and deterministic: `lifecycle.py` imports only
  `urllib` and `config`, requires no running server, and never raises.
- A failed release is invisible to the user (same as today), so orchestration
  degrades gracefully to the pre-existing "model lingers" behaviour.

## Alternatives considered

- **Pure server config** — `OLLAMA_MAX_LOADED_MODELS=1` handles Ollama but does
  nothing for mlx-vlm, whose models are the largest and the ones that persist.
  Rejected as insufficient on its own (kept as backstop).
- **Move Qwen onto Ollama** — unifies eviction under one `keep_alive` mechanism,
  but mlx-vlm is chosen for Apple-native MLX performance and a config flip on a
  larger model is not free. Rejected; the two-endpoint abstraction is smaller.
- **Decouple the summary into a separate command/process** — a separate process
  would have its own address space and not share the 32 GB ceiling, but it
  breaks the one-shot `convert_file` UX and the RAG index depends on the just-
  written Markdown. Rejected.
- **No active orchestration, rely on a restart** — the status quo that produced
  31/32 GB used; rejected as the very problem being solved.
