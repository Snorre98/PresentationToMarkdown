# AI requirements — what each pass needs

PresentationToMarkdown translates the fleet manifest's capability **tags** into
the passes it runs. This page is the single reference for that mapping. In the
TUI, press **`a`** for the live version: it shows each pass, what it does, the
model PtM will actually use, its state, and the command to start it. `space`
toggles the selected pass. Press **`s`** for output and audio preferences.

## The vocabulary

| TUI label | Internal key | Env var | Engine flag |
| --- | --- | --- | --- |
| Vision transcription | `vision` | `VISION_ENABLED` | `--vision` |
| Image classifier | `classify` | `VISION_CLASSIFY_ENABLED` | `--classify` |
| Diagram interpretation | `interpret` | `INTERPRET_ENABLED` | `--interpret` |
| Markdown restructure | `format` | `FORMAT_ENABLED` | `--format` |
| Presentation summary | `summary` | `SUMMARY_ENABLED` | `--summary` |
| Paper structure | `structure` | `STRUCTURE_ENABLED` | `--structure` |
| Audio transcription | — | `AUDIO_MODEL` | TUI audio job |

The internal keys remain stable because they are the env-var and API names. The
TUI never shows them; it uses the labels above.

## What each pass needs

| Pass | Models | Notes |
| --- | --- | --- |
| Vision transcription | Qwen2.5-VL 7B (`qwen2.5-vl-7b-instruct-4bit`, `:8081`) | |
| Image classifier | Qwen2.5-VL 3B (`qwen2.5-vl-3b-instruct-4bit`, `:8082`) + Qwen2.5-VL 7B | The classifier implies vision; the 7B does the transcription |
| Diagram interpretation | Qwen2.5-VL 7B | |
| Markdown restructure | Qwen2.5-VL 7B + Llama 3.2 3B (`llama-3.2-3b-instruct-4bit`, `:8083`) | Llama is the need-check gate; optional with `NEED_GATE=off` |
| Presentation summary | Llama 3.2 3B + EmbeddingGemma 2 (`embeddinggemma-2-q8-0`, `:8090`) | Embeddings are optional for retrieval; without them the summary uses a bounded sample |
| Paper structure | Qwen2.5-VL 7B + Llama 3.2 3B | Paper-mode PDFs only; the 7B handles image pages, the 3B text pages |
| Audio transcription | `audio` (`:8089`) or a Whisper model (`whisper-large-v3-asr-fp16` / `-turbo-`, `:8099`) | Separate engine job; not part of slide conversion |

The model rows the TUI shows are the models PtM **resolves** for these roles,
not every model whose tags could match. The current manifest resolves:

| Role label | Resolved model | Serves |
| --- | --- | --- |
| Vision + rewrite model | Qwen2.5-VL 7B Instruct (`qwen2.5-vl-7b-instruct-4bit`) | Vision transcription, Diagram interpretation, Markdown restructure, Paper structure |
| Image classifier | Qwen2.5-VL 3B Instruct (`qwen2.5-vl-3b-instruct-4bit`) | Image classifier |
| Summary model | Llama 3.2 3B Instruct (`llama-3.2-3b-instruct-4bit`) | Presentation summary |
| Text model (summary + paper structure) | Llama 3.2 3B Instruct (`llama-3.2-3b-instruct-4bit`) | Markdown restructure (need check), Paper structure text pages |
| Embeddings model | EmbeddingGemma 2 (`embeddinggemma-2-q8-0`) | Presentation summary |

One model can serve several passes. Enabling more passes does not always mean
starting more models.

## Start the models

For the five slide passes (`--all`):

```sh
for m in qwen2.5-vl-7b-instruct-4bit qwen2.5-vl-3b-instruct-4bit \
         llama-3.2-3b-instruct-4bit embeddinggemma-2-q8-0; do
  curl -X POST "http://127.0.0.1:9300/start/$m"
done
```

For a single pass, start only its rows from the table. The TUI's `a` view prints
the exact command for each model that is down.

Audio transcription is separate: start `audio` for NB-Whisper, or one of the
whisper model keys for the fleet's `whisper` daemon. It is not needed to convert
slides or papers.

## Tags, manifest order, and alternatives

The fleet manifest is a machine-wide capability vocabulary, not a per-app list.
PtM resolves a role by requiring all of its tags and taking the **first**
matching model in manifest order (ADR-0064). Consequences:

- **No failover.** If the resolved model is down, PtM does not silently switch
  to another tag match. Start the model shown in the `a` view.
- **Tag matches that lose are not PtM servers.** Llama 3.2 1B carries
  `summary`, GLM-OCR carries `transcriber`, and Qwen2.5-VL 3B carries
  `transcriber`, but the 3B Llama and the 7B Qwen win by manifest order.
  They are counted under “other fleet models”.
- **ASR selection is not a conversion dependency.** The selected audio model is
  marked `selected`, not `needed`; it only matters when you run a transcription.

## Defaults and overrides

- Endpoint and model resolution is: explicit `*_BASE_URL` / `*_MODEL` env var →
  flaate's `list` projection → the static fallback catalog in
  `converter/config.py`.
- The summary embeddings default to the fleet's `embeddings` role
  (`:8090`, EmbeddingGemma). To use Ollama instead, set
  `EMBED_BASE_URL=http://localhost:11434/v1` and
  `EMBED_MODEL=nomic-embed-text`.
- `NEED_GATE=off` removes the small text model from the Markdown restructure
  requirement; every slide is then rewritten.
- `PDF_MODE=slide` means Paper structure will not run even if it is enabled.

## Troubleshooting

- **A pass's glyph is `○`** — one of its required models is down or unknown.
  Open `a` and run the start command it prints.
- **A model appears in flaate but not in `a`** — it is an alternative or serves
  no PtM role. That is expected; see “other fleet models”.
- **`a` says the engine did not return requirements** — restart `ptm-engine`
  (the TUI spawns it; a stale engine from before the upgrade does not have the
  projection).
- **The summary runs but has no RAG retrieval** — the embeddings model is down;
  the summary falls back to a bounded sample and logs a warning.
