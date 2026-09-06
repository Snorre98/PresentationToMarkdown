# Remote inference over Tailscale

Run the converter on a **separate machine** — with its own files, its own
`ptm.sqlite`, and its own local pipeline — while all LLM inference stays on the
Mac, reachable over **Tailscale**.

This needs **no code changes**: the converter is already a model-free client that
speaks OpenAI-compatible HTTP to whatever endpoints the environment points at
(see `docs/ai-vision.md`). Everything below is serving-side config (Mac) plus a
few environment variables (client). The Mac-side serving config — the fleet
manifest, the control daemon that reads it (the one always-on agent), and the
`tailscale/` ACL — lives in **`macos-dev-config`**, not here.

## Architecture

```
Linux client (own files)                      Mac (inference only)
────────────────────────                      ─────────────────────
python-pptx / PyMuPDF / numpy
sqlite-vec + ptm.sqlite (RAG DB)      ──┐
LibreOffice soffice (PPTX charts)       │   base64 images / text   ┌─ mlx-vlm :8081  transcriber / format / summary
own .pptx/.pdf + markdown output        └─────── Tailscale ─────▶ ├─ mlx-vlm :8082  classifier gate
                                          (encrypted mesh VPN)     └─ llama.cpp :8090  nomic-embed embeddings
```

The client is "dumb": it does all the deterministic work locally (parsing,
layout, chart rendering, vector storage) and only ships model prompts out over
the VPN. It needs no ML runtime — no torch/mlx/ollama.

## What runs where

| Piece | Linux client | Mac |
| --- | --- | --- |
| PPTX/PDF parsing, image extraction, layout | ✅ python-pptx, PyMuPDF, numpy | — |
| PPTX chart rendering | ✅ LibreOffice `soffice` | — |
| Vector store / RAG DB | ✅ `sqlite-vec` + `ptm.sqlite` (in-process) | — |
| Files + output | ✅ its own | — |
| Chat/VLM inference | — | ✅ `mlx_vlm.server` `:8081` + `:8082` |
| Embeddings | — | ✅ llama.cpp `nomic-embed` `:8090` |

## Part 1 — Mac: bind the servers to the network

The control daemon (`macos-dev-config`) starts these runners on demand
(`curl -X POST http://127.0.0.1:9300/start/<name>`, ADR-0029) but binds them to
`127.0.0.1` from its manifest, and its pre-bind gate refuses ungated
non-localhost binds (ADR-0021 — fail-closed). For remote access today, run the
raw commands bound to `0.0.0.0` so the Tailscale interface can reach them:

```sh
mlx_vlm.server --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --port 8081 --host 0.0.0.0
mlx_vlm.server --model mlx-community/Qwen2.5-VL-3B-Instruct-4bit --port 8082 --host 0.0.0.0
```

`transcriber` (Qwen2.5-VL-7B, `:8081`) and `classifier` (Qwen2.5-VL-3B, `:8082`)
are the same entries this project uses locally — their models/ports live in the
fleet manifest (`macos-dev-config/models.json`, which the daemon serves and
enforces).

Ollama needs its host set as an environment variable:

```sh
launchctl setenv OLLAMA_HOST 0.0.0.0
brew services restart ollama
```

> The macOS application firewall may prompt once to allow `mlx_vlm.server` to
> accept incoming connections — allow it.

> **Security:** these servers have no built-in auth. `0.0.0.0` exposes them to
> any interface, so the Tailscale ACL (Part 3) is what actually restricts who can
> reach them. If you prefer not to bind `0.0.0.0`, bind the servers to the
> Mac's Tailscale IP instead.

## Part 2 — Mac: keep them running (launchd)

The always-on `classifier`/`transcriber` LaunchAgents that used to live in
`macos-dev-config/launchd/` were **retired** (PtM ADR-0029): those runners are
on-demand under the control daemon, which itself is the one always-on agent
(`com.macosdev.fleetdaemon`). For a boot-persistent remote server, run the raw
command from Part 1 under your own launchd plist (or `screen`). Ollama stays a
`brew services` daemon; the `ollama-env` plist only re-applies `OLLAMA_HOST` at
login, so `brew services restart ollama` may be needed after a login.

## Part 3 — Tailscale ACL (restrict access)

Both machines join the same tailnet. Note the Mac's **MagicDNS hostname** (e.g.
`mac.tailXXXX.ts.net`) — that's the stable target the client uses.

Use `macos-dev-config/tailscale/acl.hujson` to allow only the client to reach
the ports. Tag the machines in the admin console (`tag:inference-server` on
the Mac, `tag:inference-client` on the client), then paste the ACL. It is
deny-by-default, so it *replaces* the default open policy — re-add the default
rules if you need them.

## Part 4 — Linux client setup

```sh
# 1. Tailscale on the same tailnet; confirm reachability
tailscale up
tailscale ping mac.tailXXXX.ts.net

# 2. Python 3.10+ and the package (no ML deps)
git clone <this-repo> && cd PresentationToMarkdown
pip install -e .

# 3. LibreOffice headless, for PPTX chart rendering (optional but recommended)
sudo apt install libreoffice

# 4. Confirm sqlite-vec installed (used for the summary RAG DB)
python -c "import sqlite_vec; print(sqlite_vec.__version__)"
```

`soffice` is the default `SOFFICE_PATH`; if charts don't render, point
`SOFFICE_PATH` at the installed binary.

## Part 5 — Configure the client

Point the endpoints at the Mac's Tailscale hostname. The CLI accepts `--env`
per invocation, or you can export them:

```sh
ptm --all \
  --env VISION_BASE_URL=http://mac.tailXXXX.ts.net:8081/v1 \
  --env VISION_CLASSIFY_BASE_URL=http://mac.tailXXXX.ts.net:8082/v1 \
  --env SUMMARY_BASE_URL=http://mac.tailXXXX.ts.net:8081/v1 \
  --env EMBED_BASE_URL=http://mac.tailXXXX.ts.net:8090/v1 \
  /path/on/client/slides.pptx
```

| Var | Default | Remote value |
| --- | --- | --- |
| `VISION_BASE_URL` | `http://127.0.0.1:8081/v1` | `http://<mac>:8081/v1` |
| `VISION_CLASSIFY_BASE_URL` | `http://127.0.0.1:8082/v1` | `http://<mac>:8082/v1` |
| `EMBED_BASE_URL` | `http://127.0.0.1:8090/v1` | `http://<mac>:8090/v1` |
| `WRITE_BASE_URL` | `http://127.0.0.1:8081/v1` | `http://<mac>:8081/v1` |
| `FORMAT_BASE_URL` | `WRITE_BASE_URL` | follows automatically |
| `SUMMARY_BASE_URL` | `WRITE_BASE_URL` | follows automatically |

`FORMAT_*` and `SUMMARY_*` inherit `WRITE_*`, so they follow `WRITE_BASE_URL`
with no extra setting. Point `WRITE_BASE_URL` at the Mac (or set
`FORMAT_BASE_URL`/`SUMMARY_BASE_URL` individually) when you enable those passes.
Models (`*_MODEL`) and optional `*_API_KEY` vars are unchanged from the defaults.

## Caveats

- **Bandwidth / latency** — images travel as base64 data-URLs over the VPN. The
  classifier gate (see `docs/ai-vision.md`) already skips decorative images, and
  the 600 s request timeout is generous; still, large decks will be slower than
  local inference.
- **Summary pass** — embeddings go to the Mac's `nomic-embed` llama.cpp daemon,
  so a big deck means many small round-trips; correct, but the slowest pass over
  a VPN.
- **No server auth** — Tailscale is the security boundary. Keep the ACL tight,
  and prefer binding to the Tailscale IP over `0.0.0.0` if the Mac ever joins a
  LAN you don't trust.

## Reference

- Serving, model formats, and storage: **`macos-dev-config/inference-readme.md`**
- On-demand / always-on serving + Tailscale ACL: **`macos-dev-config/`**
  (fleet manifest `models.json`, control daemon `docs/contracts/daemon-http.md`,
  `tailscale/`)
- The client's AI passes and env vars: **`docs/ai-vision.md`**
