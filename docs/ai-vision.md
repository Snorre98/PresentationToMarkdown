# AI vision pass (optional)

For diagrams, flowcharts and tables that appear as *images* — embedded raster
images in PDFs, pictures in PowerPoint decks, and charts — the converter can hand
them to a **local vision-language model** and transcribe them back as structured
Markdown. This is strictly a *post-pass*: the deterministic text extraction always
runs first, and every page still keeps its rendered PNG as the visual ground
truth.

## Serving the model

The canonical way to download, serve and store models on this machine lives in
**`macos-dev-config/inference-readme.md`** — follow that for formats, the
MLX/GGUF "lanes", SSD storage layout, and LAN serving. In short:

- Vision models run on **MLX via `mlx-vlm`** (Apple-native, fastest on Apple
  Silicon). `mlx-vlm` is already installed (`~/.local/bin/mlx_vlm.server`).
- The transcriber used here is `mlx-community/Qwen2.5-VL-7B-Instruct-4bit` (MLX,
  qwen2_5_vl arch, 4-bit).

Serve it (OpenAI-compatible API on `:8081`):

```sh
# on-demand via macos-dev-config (see its inference-readme.md):
tools/serve.sh start transcriber

# or the underlying command:
mlx_vlm.server --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --port 8081
```

`tools/serve.sh reach transcriber` prints the exact base URL for `VISION_BASE_URL`.
Add `SERVE_HOST=0.0.0.0` (or `--host 0.0.0.0`) only if you need to reach it from
another device on the LAN. For a server that must be up at boot with no manual
step, use the always-on `launchd/` agents in `macos-dev-config` instead.

> Qwen2.5-VL-7B is the *default*, not the only option — override it with
> `VISION_MODEL`, and see [Improving the vision model](#improving-the-vision-model)
> below for candidates (including the previous default, Ornith).

> No `ollama pull` and no separate download: this follows the
> `inference-readme.md` "lanes" convention (one runner per model). The
> PresentationToMarkdown converter never downloads models itself.

## Improving the vision model

This section is a starting point for doing your own deep research, not an
exhaustive ranking. `mlx-vlm` supports a much wider range of models than just
the default — including dedicated **OCR** models that are arguably a better fit
for *lossless* slide transcription than a general-purpose VLM.

> Note: the default transcriber is now `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`
> (fast, document-optimised). Ornith, discussed below as the previous default, is
> kept as a research reference and remains usable via `VISION_MODEL`.

### What the current model is (and its limit)

`mlx-community/Ornith-1.0-9B-8bit` is a **9B general vision-language model**
(qwen3_5 arch). Its strength is producing structured Markdown (headings, lists,
tables) in one shot. Its weakness for *this* task is that, like any generalist,
it can paraphrase or silently drop text — which is exactly why the converter
runs a missing-word cross-check and keeps the raw text when too much is missing.

### Two directions to explore

| Direction | Models | Output | Fits when |
| --- | --- | --- | --- |
| **A. General VLM** | Qwen2.5-VL / Qwen3-VL, Gemma 4, MiniCPM-V | Structured Markdown directly | You want readable diagrams/flowcharts in one call |
| **B. OCR specialist** | DeepSeek-OCR / 2, GLM-OCR, DOTS-OCR, PaddleOCR-VL, Falcon-OCR | Near-verbatim text (+ boxes) | Verbatim fidelity is the priority; structure comes from the deterministic layout pass |

Direction **B** is interesting precisely because the converter already
reconstructs reading order, bullets and tables from coordinates. An OCR model
that is *faithful* but flat can be paired with that pass, instead of asking one
generalist to do both "read every word" and "lay it out" at once.

### Candidate shortlist (verify the exact `mlx-community` quant before pulling)

General VLMs:

- **Qwen2.5-VL** (3B / 7B / 32B) — the strongest well-established document
  understanding in the mlx-vlm set; e.g. `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`,
  `mlx-community/Qwen2.5-VL-32B-Instruct-8bit`.
- **Qwen3-VL** (4B+) — newer Qwen VL generation (e.g. `Qwen/Qwen3-VL-4B-Instruct`).
- **Gemma 4** (E2B / 26B / 31B) — Google multimodal; note the Gemma license
  (attribution) vs Qwen's Apache-2.0.
- **MiniCPM-V 4.6** — small but strong at OCR-like tasks.
- **Moondream2 / 3** — tiny and fast, for a cheap first sanity check.

OCR specialists (best verbatim fidelity, lowest hallucination, but flat text):

- **DeepSeek-OCR / DeepSeek-OCR-2**, **GLM-OCR**, **DOTS-OCR**, **PaddleOCR-VL**,
  **Falcon-OCR**.

### Selection criteria for this task

1. **Verbatim fidelity** — does it reproduce exact wording, numbers, URLs?
2. **Table reconstruction** — does it keep columns/cells or mangle them?
3. **Bullet / indent preservation** — list nesting survives intact.
4. **Low hallucination** — does it invent text or fill gaps?
5. **License** — Qwen (Apache-2.0) preferred per `inference-readme.md` lanes;
   Gemma carries attribution terms; Llama is avoided as a default.
6. **Size** — M4 / 32 GB → ~24 GB usable, ~0.55 GB per 1B params at 4-bit. Up to
   ~30B at 4-bit is comfortable; 70B+ is archive-only.

### Recommended first candidate

Start with **`mlx-community/Qwen2.5-VL-7B-Instruct-4bit`** — strong document
understanding at ~4 GB, a clear step up from the 9B generalist. If text
*omission* remains the dominant failure mode, switch to **DeepSeek-OCR**
(verbatim OCR + the deterministic layout pass). `Qwen2.5-VL-32B` (8-bit) is the
quality ceiling this machine can run.

### Experiment workflow (A/B a candidate)

```sh
# 1. Serve the candidate (add --trust-remote-code if the model requires it)
mlx_vlm.server --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --port 8081

# 2. Point the converter at it and convert the same deck
VISION_ENABLED=1 \
VISION_BASE_URL=http://127.0.0.1:8081/v1 \
VISION_MODEL=mlx-community/Qwen2.5-VL-7B-Instruct-4bit \
./python main.py
```

Compare the complex pages against the rendered PNG, and treat the converter's
missing-word warning count as a quantitative omission metric — lower is better.
For throughput, `mlx_vlm.server` also supports speculative decoding
(`--draft-model`, DFlash for Qwen3.5 / MTP for Gemma 4).

## Configuration

The converter talks to any **OpenAI-compatible `/v1/chat/completions` endpoint**
(the convention every local server in `inference-readme.md` shares), configured
via environment variables:

| Var | Default | Purpose |
| --- | --- | --- |
| `VISION_ENABLED` | *(unset = off)* | Master switch — `1`/`true`/`yes`/`on` enables the pass |
| `VISION_BASE_URL` | `http://127.0.0.1:8081/v1` | Transcriber server base URL |
| `VISION_MODEL` | `mlx-community/Qwen2.5-VL-7B-Instruct-4bit` | Transcriber model id |
| `VISION_API_KEY` | *(unset)* | Optional bearer token (unused for local servers) |
| `VISION_CLASSIFY_ENABLED` | *(unset = off)* | Enables the cheap classifier gate (and image-level transcription) |
| `VISION_CLASSIFY_BASE_URL` | `http://127.0.0.1:8082/v1` | Classifier server base URL |
| `VISION_CLASSIFY_MODEL` | `mlx-community/Qwen2.5-VL-3B-Instruct-4bit` | Classifier model id (switch to any mlx-vlm VLM) |
| `VISION_MIN_CONTENT_AREA` | `150000` | A "decorative" verdict is overridden for images with at least this many native pixels |
| `VISION_MIN_CONTENT_ASPECT` | `2.0` | …when the image is also at least this wide (long side / short side) |
| `VISION_LOG_ENABLED` | `on` | Record vision decisions/transcriptions to SQLite |
| `VISION_LOG_DB` | `ptm.sqlite` | SQLite log path (the project dir for now) |
| `VISION_MIN_IMAGE_DIM` | `250` | Skip transcription for images whose smaller native side is below this many pixels |
| `VISION_BLUR_THRESHOLD` | `30.0` | Skip transcription for images whose Laplacian variance is below this value |
| `FORMAT_ENABLED` | *(unset = off)* | Enables the LLM markdown-restructure pass (reuses the writer endpoint) |
| `FORMAT_BASE_URL` | `WRITE_BASE_URL` | Restructure-pass server base URL |
| `FORMAT_MODEL` | `WRITE_MODEL` | Restructure-pass model id |
| `FORMAT_API_KEY` | `WRITE_API_KEY` | Optional bearer token for the restructure pass |
| `WRITE_BASE_URL` | `http://127.0.0.1:8081/v1` | Writer server base URL (rewrite passes) |
| `WRITE_MODEL` | `mlx-community/Qwen2.5-VL-7B-Instruct-4bit` | Writer model id (a VLM) |
| `SOFFICE_PATH` | `soffice` | LibreOffice binary, for PPTX chart rendering only |

Example:

```sh
VISION_ENABLED=1 ./python main.py
```

When `VISION_ENABLED` is off (the default), conversion is fully deterministic and
offline — diagrams fall back to the rendered PNG plus a collapsed
`<details>` block of the raw extracted text.

## The classifier gate (optional)

The expensive transcriber (Qwen2.5-VL-7B) is wasteful on decorative images —
lecture decks are full of photographs, logos and backgrounds that have no
educational value to extract. A **second, small vision model** (Qwen2.5-VL-3B by
default) sits in front of it and classifies each image into one of three
categories, so the right thing happens to each:

```
                          ┌─ TEXT ────────▶ verbatim transcription (Qwen2.5-VL-7B, :8081)
classifier (:8082) ───────┼─ DIAGRAM ─────▶ high-level description (Qwen2.5-VL-7B)
                          └─ DECORATIVE ─▶ keep the image link as-is
```

- **TEXT** — a document, slide, screenshot or table whose text is worth
  transcribing verbatim.
- **DIAGRAM** — a flowchart or conceptual figure. The transcriber produces a
  short high-level *gist* — the diagram's type (process flow, layered
  architecture, org chart, …) and its purpose or main idea — plus any title or
  clearly-legible labels it can read. The result is rendered as a blockquote.
  It deliberately does **not** attempt to reconstruct the diagram's structure
  or every label: a local VLM cannot reliably read small labels and would only
  hallucinate filler.
- **DECORATIVE** — a photograph, logo, icon or background, left as a plain image
  link.

Serve it on its own port (mlx-vlm runs one chat model per process):

```sh
tools/serve.sh start classifier                 # on-demand, or:
mlx_vlm.server --model mlx-community/Qwen2.5-VL-3B-Instruct-4bit --port 8082
```

Enable the gate (and image-level transcription) with `VISION_CLASSIFY_ENABLED=1`:

```sh
VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 ./python main.py
```

What the gate changes:

- **Image-level transcription** — embedded images (PDF and PPTX pictures, and
  PPTX charts rendered via LibreOffice) are classified, and educational ones get
  a transcription appended below their `![image]` link. Text images are
  transcribed verbatim; diagrams get a high-level description. This only runs
  when **both** `VISION_ENABLED` *and* `VISION_CLASSIFY_ENABLED` are on.

The classifier is cheap and switchable — set `VISION_CLASSIFY_MODEL` to any
mlx-vlm VLM. Its answer is parsed loosely (a decorative signal like "photograph"
wins over "graph"), and any classifier error degrades to "keep the link" — never
to data loss.

Because a false `decorative` verdict silently drops real content (data loss),
while a false `text`/`diagram` verdict merely costs one transcription (caught by
the quality gate), a `decorative` verdict is overridden for images that are both
**large and wide** (native area ≥ `VISION_MIN_CONTENT_AREA` and aspect ≥
`VISION_MIN_CONTENT_ASPECT`) — full-width banner tables/matrices, which
decorative images almost never are. The override downgrades the image to
`diagram`, so it gets a high-level gist instead of being dropped.

### Readability gate

Before *any* model call, an image is checked for **readability** — a VLM cannot
read a blurry or tiny image, and will only hallucinate filler (e.g. a numbered
list of invented "Data Source 1…111"). Skipping such images saves both the
classifier *and* transcriber:

- **Resolution** — images whose smaller native side is below
  `VISION_MIN_IMAGE_DIM` (default `250` px) are skipped. This is pure metadata
  (no decoding).
- **Blur** — otherwise, the image's Laplacian variance (a sharpness proxy) is
  computed from its pixels; if it is below `VISION_BLUR_THRESHOLD` (default
  `30.0`), the image is skipped. The metric measures edge energy, so clean
  line-art diagrams score low even when sharp — the default is deliberately
  conservative (tune it up if you see blurry images slipping through).

A skipped image keeps its `![image]` link, logs a `[WARN]`, and records a
`stage = "readability"` event in the SQLite log.

### Quality gate

Every transcription (image, chart, or diagram description) is passed through a
deterministic quality gate before it is written. The gate catches the classic
vision-model failure modes and discards the transcription, keeping only the
image link:

- **Repetition loops** — the same label emitted over and over, including
  monotonically numbered filler (e.g. `Data Source 1…111`, collapsed by stripping
  trailing enumeration digits before comparing).
- **Placeholder/template echo** — output containing `...` or bracketed
  placeholders like `[specific …]` (the model echoing a fill-in-the-blank
  template instead of reading).
- **Excessive nesting** — bullet indentation that deepens pathologically.
- **Enumerated filler** — a diagram description that comes back as a bullet list
  (more than 6 items) rather than prose, a sign the model enumerated components
  instead of describing.
- **Runaway length** — an implausibly long transcription for one figure.
- **Low information density** — near-zero unique-word ratio.

A discarded transcription is logged as a warning, and the reason is recorded in
the SQLite log (`stage = "transcribe"`, `error = "quality gate: <reason>"`).

### Why not Mermaid / structured extraction?

Mermaid (or any structured diagram language) is the *right* representation — but
only if the node text and topology can be read accurately. A local 4-bit 7B VLM
can't reliably read small labels, let alone reconstruct which node connects to
which; a confidently-wrong Mermaid block is worse than nothing. So the converter
deliberately stays at the *gist* level. Structured extraction could be
revisited later with an OCR-first pipeline or a larger model, gated behind the
same quality checks.

> **Moondream2 note:** `vikhyatk/moondream2` does not currently load in
> `mlx-vlm 0.6.15` — its `config.json` declares `model_type: "moondream1"`,
> which mlx-vlm routes to a broken loader. Qwen2.5-VL-3B is the default instead.

## Diagram interpretation pass (optional)

The transcription/gist layers *describe* a slide. The interpretation pass
(`INTERPRET_ENABLED=1`) goes one level deeper: it reads the *meaning* a diagram
asserts, as typed relationships plus a short plain-language reading.

```
- `Constraint 1` —`hinders`→ `Goal 3.5`

**Meaning:**

Overtime rules block cutting peak-time temporary recruitment.
```

The key design point is **grounding**: the deterministic layout pass already
extracts every text label on the page, so the model is handed those labels
verbatim and asked only to *bind* them into relationships (`<A> | <label> | <B>`)
— never to re-read or re-word them. A grounding gate then drops any relationship
whose entities or relationship label aren't in the supplied set, which is what
keeps the interpretation anchored to the slide instead of drifting (the
"describe the description" failure mode). Unreadable pages and outputs that fail
the quality gate fall back to the ordinary vision transcription, then to the raw
text block.

| Var | Default | Purpose |
| --- | --- | --- |
| `INTERPRET_ENABLED` | *(unset = off)* | Master switch for the interpretation pass |
| `INTERPRET_BASE_URL` | `WRITE_BASE_URL` | Model server (reuses the writer by default) |
| `INTERPRET_MODEL` | `WRITE_MODEL` | Model id (`Qwen2.5-VL-7B` by default) |
| `INTERPRET_API_KEY` | `WRITE_API_KEY` | Optional bearer token |

To use a larger model for interpretation without changing the transcriber, serve
`Qwen2.5-VL-32B-Instruct-8bit` on its own port and point `INTERPRET_BASE_URL` /
`INTERPRET_MODEL` at it — the same override pattern `SUMMARY_*` uses. The pass is
general-purpose: the relationship vocabulary comes from the diagram's own labels,
not a fixed meta-model, so it applies to any diagram, not just 4EM.

### PPTX charts

Charts live in the file as data, not pixels, so `python-pptx` can't read them
directly. With vision + classifier enabled, the converter renders them via
headless LibreOffice (deck → PDF → cropped chart PNG), then classifies and
transcribes them:

```sh
brew install --cask libreoffice
```

If LibreOffice is missing, charts are skipped with a warning and everything else
still converts.

## How it works

1. Each embedded image / picture / chart is extracted (charts are rendered to a
   PNG via LibreOffice first).
2. The image is **classified** by the small classifier model into `text`
   (transcribe verbatim), `diagram` (describe at a high level), or `decorative`
   (leave as a plain image link).
3. `text` images are **transcribed** with a lossless-transcription prompt
   (verbatim text, headings, bullets, tables; no commentary). `diagram` images
   get a high-level prose **gist** instead (type + purpose, plus any clearly
   legible labels), rendered as a blockquote. The result is passed through the
   **quality gate** and appended below the image link only if it passes.
4. Transcription results are **cached by image digest**, so identical content is
   only transcribed once per run.
5. On any error (server down, model missing), the converter warns and falls back
   to the plain image link.

## Markdown polish pass

A separate, text-only formatting post-pass runs after conversion
(`converter/format.py`). It has two layers:

- **Deterministic (always on)** — strips trailing whitespace, collapses excess
  blank lines, and normalises heading spacing. No model, no dependencies.
- **LLM restructure (opt-in, `FORMAT_ENABLED=1`)** — sends each slide to a chat
  model (the same OpenAI-compatible endpoint as the vision pass) to reflow
  mid-sentence line breaks into paragraphs and promote heading-like bullets into
  `##`/`###` headings.

Because "keep the content exact" is the hard requirement, the restructure pass is
gated: the original text and the model output are compared word-for-word, and
any slide where content is dropped or invented is rejected (the deterministic
output is kept instead). Structural lines — the slide title, page/image links,
tables, blockquotes, `<details>` blocks, and the page-break `<div>` — must also
survive verbatim. `FORMAT_BASE_URL`/`FORMAT_MODEL`/`FORMAT_API_KEY` default to
their `WRITE_*` equivalents.

## Summary pass (per-presentation RAG)

A separate opt-in pass (`SUMMARY_ENABLED=1`, see the README) prepends a
standardized English summary header to each converted presentation. It stores
per-slide chunks and **sqlite-vec** embeddings in `ptm.sqlite`, retrieves the most
salient chunks, and has a dedicated summary chat model write the header.

- **Summary model** — reuses the **writer** by default
  (`SUMMARY_BASE_URL` defaults to `WRITE_BASE_URL`,
  `SUMMARY_MODEL` to `WRITE_MODEL`, i.e. Qwen2.5-VL-7B on `:8081`), so no extra
  server is needed; override `SUMMARY_*` to use a dedicated text model instead.
- **Embeddings** — served by Ollama (`EMBED_BASE_URL=http://localhost:11434/v1`,
  `EMBED_MODEL=embeddinggemma`, a 768-dim embedding model). The dimension is
  auto-detected, so any embedding model works.

The `sqlite-vec` extension is a Python dependency (`pip install sqlite-vec`) and
loads on its own; no separate server is needed for the vector store.

All three AI passes can be enabled together in one command (vision + classifier,
markdown restructure, and the RAG summary):

```sh
VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 FORMAT_ENABLED=1 SUMMARY_ENABLED=1 \
  ./.venv/bin/python main.py
```

## Logging

Every classifier decision and transcription is recorded to a SQLite database so
the vision pipeline is easy to inspect. Each row carries the source file, page
number, image reference/digest, model, the classifier's decision and raw answer,
latency, token counts, and the transcription output.

- **Where:** `ptm.sqlite` in the project directory (override with `VISION_LOG_DB`).
- **On/off:** on by default; disable with `VISION_LOG_ENABLED=0`.
- This is also the DB that will hold app configuration later (schema is versioned
  via the `meta` table).

Example queries:

```sql
-- every event for one source file, in order
SELECT * FROM vision_events WHERE source = ? ORDER BY id;

-- all decisions + transcripts for a specific image digest
SELECT stage, decision, latency_ms, generated_tokens, markdown
FROM vision_events WHERE image_digest = ? ORDER BY id;
```

## Reference

- Serving, model formats, and storage: **`macos-dev-config/inference-readme.md`**
- On-demand serving: **`macos-dev-config/tools/serve.sh`** (`serve.sh start transcriber classifier`)
- Ollama daemon tuning: **`macos-dev-config/ollama/README.md`**
