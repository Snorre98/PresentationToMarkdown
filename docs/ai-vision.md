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

> No `ollama pull` and no separate download: this follows the
> `inference-readme.md` "lanes" convention (one runner per model). The
> PresentationToMarkdown converter never downloads models itself.

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
