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
mlx_vlm.server --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --port 8081
```

Add `--host 0.0.0.0` only if you need to reach it from another device on the LAN.

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
| `VISION_LOG_ENABLED` | `on` | Record vision decisions/transcriptions to SQLite |
| `VISION_LOG_DB` | `ptm.sqlite` | SQLite log path (the project dir for now) |
| `VISION_MIN_IMAGE_DIM` | `250` | Skip transcription for images whose smaller native side is below this many pixels |
| `VISION_BLUR_THRESHOLD` | `30.0` | Skip transcription for images whose Laplacian variance is below this value |
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
- **DIAGRAM** — a flowchart or conceptual figure. Instead of a lossless
  transcription (which degenerates into repeated node labels), the transcriber
  produces a short *description*: what the diagram represents and its purpose,
  its main stages/components, and the overall flow.
- **DECORATIVE** — a photograph, logo, icon or background, left as a plain image
  link.

Serve it on its own port (mlx-vlm runs one chat model per process):

```sh
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
- **Runaway length** — an implausibly long transcription for one figure.
- **Low information density** — near-zero unique-word ratio.

A discarded transcription is logged as a warning, and the reason is recorded in
the SQLite log (`stage = "transcribe"`, `error = "quality gate: <reason>"`).

> **Moondream2 note:** `vikhyatk/moondream2` does not currently load in
> `mlx-vlm 0.6.15` — its `config.json` declares `model_type: "moondream1"`,
> which mlx-vlm routes to a broken loader. Qwen2.5-VL-3B is the default instead.

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
   get a high-level description instead (purpose, main components, flow). The
   result is passed through the **quality gate** and appended below the image
   link only if it passes.
4. Transcription results are **cached by image digest**, so identical content is
   only transcribed once per run.
5. On any error (server down, model missing), the converter warns and falls back
   to the plain image link.

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
- Ollama daemon tuning: **`macos-dev-config/ollama/README.md`**
