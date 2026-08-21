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
| `VISION_BASE_URL` | `http://127.0.0.1:8081/v1` | Server base URL |
| `VISION_MODEL` | `mlx-community/Ornith-1.0-9B-8bit` | Model id the server exposes |
| `VISION_API_KEY` | *(unset)* | Optional bearer token (unused for local servers) |

Example:

```sh
VISION_ENABLED=1 ./python main.py
```

When `VISION_ENABLED` is off (the default), conversion is fully deterministic and
offline — diagrams fall back to the rendered PNG plus a collapsed
`<details>` block of the raw extracted text.

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
