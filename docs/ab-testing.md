# A/B testing vision models — what to expect

This document captures *why* we A/B test the vision model, the *exact problems* that make it hard, and *what to expect* when a run is going. It's written so a human or an AI agent can pick it up cold and reason about any A/B run.

## 1. Why we A/B test

The converter has an optional AI pass that transcribes images/diagrams/complex pages into Markdown via a local vision-language model. The default is **Qwen2.5-VL-7B** — a 7B *generalist* VLM. Generalists are strong at structured output but are known to paraphrase, drop words, and invent text, which is why the converter runs `verify_no_omissions` and a quality gate around every transcription (`converter/vision.py`).

The open question: are **OCR-specialist** models — smaller, faster, trained for verbatim reading — a better fit? The 2026 landscape that motivates the test:

| Model | Size | Notes |
|---|---|---|
| Qwen2.5-VL-7B (baseline) | 7B | generalist VLM, mlx-vlm `:8081` |
| **GLM-OCR** | 0.9B | OCR specialist, #1 OmniDocBench (94.6), Ollama `:11434` |
| PaddleOCR-VL-1.6 | 0.9B | OCR specialist, OmniDocBench 96.3 |
| DeepSeek-OCR-2 | 3B | OCR specialist, efficiency-focused |

**The A/B goal:** does a candidate beat the baseline on (a) *fidelity* — no omitted or invented words — and (b) *latency/throughput*, on our real complex pages? The answer drives whether we switch `VISION_MODEL`.

## 2. What "A/B" means here

- **Baseline** = Qwen2.5-VL-7B on every pass. **Candidate** = GLM-OCR (or another) as the vision model.
- Same source file, same AI flags (vision + classify + structure + summary ON), only the model differs.
- The candidate writes to `stem (2).md` via **"Duplicate if conversion exists"** (ADR-0015), so the baseline `.md` is never clobbered.
- Compare the two `.md` files *and* the `ptm.sqlite` `vision_events` rows grouped by `model`.

## 3. How a conversion actually runs (pass order + which model)

`converter/__init__.py` → `convert_file` runs, in order:

1. **`converter.convert`** — deterministic text extraction (PyMuPDF), then:
   - **classify** each image → `VISION_CLASSIFY_*` (Qwen2.5-VL-3B, mlx-vlm `:8082`)
   - **transcribe** text/diagram images → `VISION_*`
   - **structure** pass (paper mode) → `STRUCTURE_*`
2. **`polish_text`** — deterministic cleanup + optional **format** restructure → `FORMAT_*`
3. **`prepend_summary`** — **embed** (embeddinggemma, Ollama) + **summary** chat → `SUMMARY_*`

The critical detail is the **fallback chain** (the source of most trouble):

| Pass | Defaults to | Follows `VISION_*`? |
|---|---|---|
| vision (transcribe) | `VISION_*` | — (the reader) |
| classify | `VISION_CLASSIFY_*` | separate 3B model |
| **format** | `VISION_*` | **yes** ← problem |
| **structure** | `FORMAT_*` → `VISION_*` | **yes** ← problem |
| **interpret** | `VISION_*` | **yes** ← problem |
| summary | hardcoded `:8081`/Qwen | no |

So repointing `VISION_MODEL=glm-ocr` silently repoints **format, structure, and interpret too** — even though those are *rewriting* tasks, not OCR.

## 4. The exact problems (all observed in this repo)

### 4.1 The A/B isn't clean — reword passes run on the OCR model

Because structure/format/interpret cascade to `VISION_*`, the GLM-OCR run also ran the **structure pass on GLM-OCR**. GLM-OCR is a 0.9B OCR specialist: good at verbatim reading, weak at *generating/restructuring prose*. The result, visible in `ptm.sqlite`:

```
structure  p1..p8  anchors dropped                    ← headings/bold/code-fences/lists lost
structure  p1      added: obstacle / made / section   ← invented words
structure  p1      omitted: compelling / intrinsic    ← dropped real words
structure  p3      unreadable: blurry / quality gate: empty
```

Visible in the `.md` diff: page-1 title/authors block (kept verbatim in a ````markdown` fence in the baseline) was scattered into loose text; `# Challenge` → bare `Challenge`; `**Uncertain outcome.**` → plain; `metagame` (4×) → `metagoal` (7×, "metagame" gone).

**Takeaway:** a valid OCR A/B must pin format/structure/interpret/summary to the *writing* model (Qwen2.5-VL-7B) and change **only** `VISION_*`.

### 4.2 Memory overload — two big models resident at once

This is a 32 GB M4 with unified memory, **no VRAM cap, no OOM killer** — two large models at once can freeze macOS. Observed: GLM-OCR (~3.5 GB, Ollama) + Qwen2.5-VL-7B (~6.6 GB, mlx-vlm) + classifier (~1.5 GB) pushed the machine to 31/32 GB with ~9.6 GB compressed.

Root causes, in order of impact:
1. **Two Docker VMs** (Colima, 8 GiB each = 16 GB reserved) — now stopped.
2. **mlx-vlm never auto-unloads** — Qwen7B loaded by one run's summary persists into the next run.
3. **Ollama `keep_alive`/`MAX_LOADED_MODELS`** only govern Ollama, not mlx-vlm.

Server-side config already exists (`../macos-dev-config/ollama/ollama.env`: `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1`) but cannot coordinate across runners.

### 4.3 The progress bar lies

The per-page progress bar only tracks the page transcription loop. The **structure pass and summary pass emit no progress**, so the bar shows "complete" while the conversion is still mid-structure. (Known gap, follow-up to ADR-0013.)

## 5. What to expect during a run

Approximate per-page latency on this machine (from `vision_events`):

| Pass | Model | Latency/page |
|---|---|---|
| classify | Qwen2.5-VL-3B | ~59–62 s |
| transcribe | GLM-OCR | ~31–37 s |
| structure | GLM-OCR | ~10–106 s (mostly ~96–106 s) |
| summary (chat) | Qwen2.5-VL-7B | ~1–2 min (embeddings can be minutes when cold) |

An 8-page scanned paper takes roughly **20–30 minutes** end-to-end with all passes on. GLM-OCR is faster per transcribe than Qwen2.5-VL-7B, but the **structure pass dominates wall-clock**, and it degrades on GLM-OCR (see §4.1).

**Signals to watch in `ptm.sqlite` (`vision_events`):** `anchors dropped`, `added:`, `omitted:`, `unreadable: blurry`, `quality gate: empty`, `server down`. The `added:`/`omitted:` rows are the fidelity metric — a faithful model has none.

## 6. How to run a clean A/B

```bash
# candidate (GLM-OCR as the READER only; writer stays Qwen)
VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 \
VISION_BASE_URL=http://localhost:11434/v1 VISION_MODEL=glm-ocr \
./.venv/bin/python main.py
```

Then add the same file and tick **"Duplicate if conversion exists"** so the candidate writes `stem (2).md`. (Precondition: Ollama is running and has `glm-ocr`; the Ollama env is applied — `launchctl getenv OLLAMA_MAX_LOADED_MODELS` → `1`.)

**Caveat:** as of this writing this is *not yet* a clean A/B — structure/format/interpret still cascade to GLM-OCR (see §7).

## 7. The fixes in flight (ADRs)

1. **Reader/writer role separation** — add `WRITE_BASE_URL`/`WRITE_MODEL` (default Qwen2.5-VL-7B) and repoint format/structure/interpret/summary at it, so `VISION_*` only selects the OCR reader. Makes the A/B clean.
2. **App-level residency orchestration** — a `release_model(runner, base_url, model)` helper using the two unload endpoints (`POST /unload` for mlx-vlm, `POST /api/generate {"keep_alive":0}` for Ollama), releasing the reader before the writer passes and the writer after, so only one big model is resident at a time.

## 8. Reference

- Models/passes: `converter/vision.py`, `classify.py`, `format.py`, `structure.py`, `interpret.py`, `summary.py`, `config.py`, `pdf.py`, `__init__.py`.
- Log: `ptm.sqlite` → `vision_events` (transcribe/classify/structure with `latency_ms`, `model`, `error`), `deck_documents`/`deck_chunks` (summary index), `recent_files` (`_on_finished` marker).
- ADRs: `docs/adr/` (0013 progress, 0014 dashboard, 0015 duplicate-if-exists, 0016+ roles/residency pending).
- Ollama config: `../macos-dev-config/ollama/ollama.env`.
- Useful queries:

```sql
SELECT model, stage, count(*), round(avg(latency_ms)), round(max(latency_ms))
FROM vision_events GROUP BY model, stage;
SELECT page, stage, error FROM vision_events
WHERE error IS NOT NULL AND error != '' ORDER BY id;
```
