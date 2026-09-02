# Remote inference over Tailscale

Run the converter on a **separate machine** — with its own files, its own
`ptm.sqlite`, and its own local pipeline — while all LLM inference stays on the
Mac, reachable over **Tailscale**.

This needs **no code changes**: the converter is already a model-free client that
speaks OpenAI-compatible HTTP to whatever endpoints the environment points at
(see `docs/ai-vision.md`). Everything below is serving-side config (Mac) plus a
few environment variables (client). The Mac-side serving config — the `serve.sh`
launcher, the always-on `launchd/` agents, and the `tailscale/` ACL — lives in
**`macos-dev-config`**, not here.

## Architecture

```
Linux client (own files)                      Mac (inference only)
────────────────────────                      ─────────────────────
python-pptx / PyMuPDF / numpy
sqlite-vec + ptm.sqlite (RAG DB)      ──┐
LibreOffice soffice (PPTX charts)       │   base64 images / text   ┌─ mlx-vlm :8081  transcriber / format / summary
own .pptx/.pdf + markdown output        └─────── Tailscale ─────▶ ├─ mlx-vlm :8082  classifier gate
                                          (encrypted mesh VPN)     └─ Ollama   :11434 embeddings
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
| Embeddings | — | ✅ Ollama `:11434` |

## Part 1 — Mac: bind the servers to the network

The two `mlx_vlm.server` processes and Ollama bind to `127.0.0.1` by default.
Bind them to `0.0.0.0` so the Tailscale interface can reach them. The simplest
way is the `serve.sh` launcher in `macos-dev-config` with a global host override:

```sh
cd macos-dev-config
SERVE_HOST=0.0.0.0 tools/serve.sh start transcriber classifier
```

`transcriber` (Qwen2.5-VL-7B, `:8081`) and `classifier` (Qwen2.5-VL-3B, `:8082`)
are the same entries this project uses locally — their models/ports live in
`servers.conf`. The underlying commands, if you prefer them raw:

```sh
mlx_vlm.server --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --port 8081 --host 0.0.0.0
mlx_vlm.server --model mlx-community/Qwen2.5-VL-3B-Instruct-4bit --port 8082 --host 0.0.0.0
```

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

`SERVE_HOST=0.0.0.0 tools/serve.sh start …` is on-demand and dies with the login
session. For servers that must be up at boot (the remote client can't run a
command on the Mac), use the always-on LaunchAgents in
**`macos-dev-config/launchd/`**:

```sh
cd macos-dev-config
# substitute your short username for USERNAME, then:
sed 's/USERNAME/snorresaether/g' launchd/com.macosdev.transcriber.plist \
  > ~/Library/LaunchAgents/com.macosdev.transcriber.plist
sed 's/USERNAME/snorresaether/g' launchd/com.macosdev.classifier.plist \
  > ~/Library/LaunchAgents/com.macosdev.classifier.plist
cp launchd/com.macosdev.ollama-env.plist ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.macosdev.transcriber.plist
launchctl load ~/Library/LaunchAgents/com.macosdev.classifier.plist
launchctl load ~/Library/LaunchAgents/com.macosdev.ollama-env.plist
```

The plists use the absolute path `~/.local/bin/mlx_vlm.server` (a uv-tool
entry point) and set `HOME`/`PATH` explicitly so the model cache resolves under
launchd. `KeepAlive` restarts a server if it crashes; logs go to
`/tmp/mlx-vlm-{transcriber,classifier}.log`.

Ollama stays a `brew services` daemon; the `ollama-env` plist only re-applies
`OLLAMA_HOST` at login. Because env vars are inherited at process start and
launchd doesn't guarantee ordering, `brew services restart ollama` may be needed
after a login for Ollama to pick the variable up.

## Part 3 — Tailscale ACL (restrict access)

Both machines join the same tailnet. Note the Mac's **MagicDNS hostname** (e.g.
`mac.tailXXXX.ts.net`) — that's the stable target the client uses.

Use `macos-dev-config/tailscale/acl.hujson` to allow only the client to reach
the three ports. Tag the machines in the admin console (`tag:inference-server` on
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
  --env EMBED_BASE_URL=http://mac.tailXXXX.ts.net:11434/v1 \
  /path/on/client/slides.pptx
```

| Var | Default | Remote value |
| --- | --- | --- |
| `VISION_BASE_URL` | `http://127.0.0.1:8081/v1` | `http://<mac>:8081/v1` |
| `VISION_CLASSIFY_BASE_URL` | `http://127.0.0.1:8082/v1` | `http://<mac>:8082/v1` |
| `EMBED_BASE_URL` | `http://localhost:11434/v1` | `http://<mac>:11434/v1` |
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
- **Summary pass** — embeddings go to the Mac's Ollama, so a big deck means many
  small round-trips; correct, but the slowest pass over a VPN.
- **No server auth** — Tailscale is the security boundary. Keep the ACL tight,
  and prefer binding to the Tailscale IP over `0.0.0.0` if the Mac ever joins a
  LAN you don't trust.

## Reference

- Serving, model formats, and storage: **`macos-dev-config/inference-readme.md`**
- On-demand / always-on serving + Tailscale ACL: **`macos-dev-config/`**
  (`tools/serve.sh`, `launchd/`, `tailscale/`)
- The client's AI passes and env vars: **`docs/ai-vision.md`**
