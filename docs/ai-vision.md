# AI vision pass (optional)

For slides whose text layer can't be linearized into clean Markdown (diagrams,
flowcharts, multi-column timelines), the PDF converter can hand the rendered page
to a **local vision-language model** and transcribe it back as structured
Markdown. This is strictly a *post-pass*: the deterministic text extraction always
runs first, and every page still keeps its rendered PNG as the visual ground
truth.

## Serving the model

The canonical way to download, serve and store models on this machine lives in
**`macos-dev-config/inference-readme.md`** — follow that for formats, the
MLX/GGUF "lanes", SSD storage layout, and LAN serving. In short:

- Vision models run on **MLX via `mlx-vlm`** (Apple-native, fastest on Apple
  Silicon). `mlx-vlm` is already installed (`~/.local/bin/mlx_vlm.server`).
- The model used here, `mlx-community/Ornith-1.0-9B-8bit` (MLX, qwen3_5 vision
  arch), is already downloaded to the SSD's Hugging Face cache.

Serve it (OpenAI-compatible API on `:8081`):

```sh
mlx_vlm.server --model mlx-community/Ornith-1.0-9B-8bit --port 8081
```

Add `--host 0.0.0.0` only if you need to reach it from another device on the LAN.

> Ornith is the *default*, not the only option — see
> [Improving the vision model](#improving-the-vision-model) below for candidates
> that are a better fit for lossless slide transcription.

> No `ollama pull` and no separate download: this follows the
> `inference-readme.md` "lanes" convention (one runner per model). The
> PresentationToMarkdown converter never downloads models itself.

## Improving the vision model

This section is a starting point for doing your own deep research, not an
exhaustive ranking. `mlx-vlm` supports a much wider range of models than just
Ornith — including dedicated **OCR** models that are arguably a better fit for
*lossless* slide transcription than a general-purpose VLM.

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
| `VISION_MODEL` | `mlx-community/Ornith-1.0-9B-8bit` | Transcriber model id |
| `VISION_API_KEY` | *(unset)* | Optional bearer token (unused for local servers) |
| `VISION_CLASSIFY_ENABLED` | *(unset = off)* | Enables the cheap classifier gate (and image-level transcription) |
| `VISION_CLASSIFY_BASE_URL` | `http://127.0.0.1:8082/v1` | Classifier server base URL |
| `VISION_CLASSIFY_MODEL` | `vikhyatk/moondream2` | Classifier model id (switch to any mlx-vlm VLM) |
| `SOFFICE_PATH` | `soffice` | LibreOffice binary, for PPTX chart rendering only |

Example:

```sh
VISION_ENABLED=1 ./python main.py
```

When `VISION_ENABLED` is off (the default), conversion is fully deterministic and
offline — diagrams fall back to the rendered PNG plus a collapsed
`<details>` block of the raw extracted text.

## The classifier gate (optional)

The expensive transcriber (Ornith) is wasteful on decorative images — lecture
decks are full of photographs, logos and backgrounds that have no educational
value to extract. A **second, tiny vision model** (Moondream2 by default) can sit
in front of it and decide whether an image/page is worth transcribing at all:

```
                  ┌─ TRANSCRIBE ─▶ transcriber (Ornith, :8081)
classifier (:8082)┤
                  └─ SKIP ───────▶ keep the image link as-is
```

Serve it on its own port (mlx-vlm runs one chat model per process):

```sh
mlx_vlm.server --model vikhyatk/moondream2 --port 8082
```

Enable the gate (and image-level transcription) with `VISION_CLASSIFY_ENABLED=1`:

```sh
VISION_ENABLED=1 VISION_CLASSIFY_ENABLED=1 ./python main.py
```

What the gate changes:

- **Page pass** — a *complex* page is only sent to the transcriber when the
  classifier says it is a diagram/table/chart. Without the gate,
  `VISION_ENABLED=1` alone transcribes every complex page.
- **Image-level transcription** — *new*: embedded images (PDF and PPTX pictures,
  and PPTX charts rendered via LibreOffice) are classified, and educational ones
  get a transcription appended below their `![image]` link. This only runs when
  **both** `VISION_ENABLED` *and* `VISION_CLASSIFY_ENABLED` are on.

The classifier is cheap and switchable — set `VISION_CLASSIFY_MODEL` to any
mlx-vlm VLM (e.g. `mlx-community/Qwen2.5-VL-3B-Instruct-4bit`). Its answer is
parsed loosely (a false signal like "photograph" wins over "graph"), and any
classifier error degrades to "keep the link" — never to data loss.

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

1. A page is flagged **complex** when its text is scattered (many distinct left
   edges) or laid out in parallel columns — i.e. when a linear reading order
   can't faithfully represent it.
2. The rendered page PNG is sent to the model with a lossless-transcription
   prompt (verbatim text, headings, bullets, tables; no commentary).
3. The model's Markdown is **cross-checked** against the deterministic text
   layer: if too many content words are missing, the output is discarded and the
   raw text is kept instead — so a hallucinating or truncating model can't drop
   information silently.
4. On any error (server down, model missing), the converter warns and falls back
   to the raw-text block.

## Reference

- Serving, model formats, and storage: **`macos-dev-config/inference-readme.md`**
- Ollama daemon tuning: **`macos-dev-config/ollama/README.md`**
