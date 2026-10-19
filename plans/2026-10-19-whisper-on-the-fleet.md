# Whisper on the fleet — implementation plan

Date: 2026-10-19
Status: planned (owner decisions locked)
Scope: macos-dev-config manifest + wrapper, PresentationToMarkdown client/cutover

## Decisions

- **One `whisper` daemon** on `127.0.0.1:8099`, hosting both MLX models
  (`whisper-large-v3`, `whisper-large-v3-turbo`). The launcher keeps at most one
  model resident.
- **One-step cutover**: ship the daemon, switch PtM to it, delete the
  `mlx_whisper` subprocess lane in the same release. No transition flag.
- NB-Whisper keeps its existing `/v1/asr` route on the `audio` daemon for now;
  `converter.audio.transcribe_fleet()` hides the two wire shapes. Unifying the
  audio server onto the OpenAI contract is a follow-up, not part of this cutover.
- Plan file saved here.

## Why

`whisper` never appears in the flaate model list because the MLX lane is a
subprocess inside the consumer app (`converter/transcribe.py` shells out to
`mlx_whisper`), and the NB-Whisper lane hides inside the `audio` delegate
(`scripts/audio_server.py`, `/v1/asr`). Moving the MLX lane behind a
manifest-registered delegate makes ASR visible, startable/stoppable, and
memory-accounted like every other model, and removes the last serving
implementation (beyond the audio server) from PtM.

## Verified facts (2026-10-19)

- `mlx-audio` **0.5.9** is already installed as a uv tool at
  `~/.local/share/uv/tools/mlx-audio` with the `server`, `stt`, and `tts`
  extras (plus `torch`). The same tool already serves `higgs-tts` and
  `voice-design` in the fleet.
- `mlx_audio.server` exposes:
  - `POST /v1/audio/transcriptions` with `model`, `language`, `file`,
    `response_format` (`text|json|verbose_json|ndjson`), `word_timestamps`;
  - `GET /v1/models` (health probe + model list), `POST /v1/models` (load),
    `DELETE /v1/models/{id}` (unload);
  - vendored MLX Whisper that loads `mlx-community` whisper repos.
- `mlx_audio.server.ModelProvider` caches **every** loaded model forever (no
  eviction) → needs the launcher patch below.
- The OpenAI transcription form surface does **not** expose
  `condition_on_previous_text`; upstream defaults it to `True`. PtM currently
  passes `--condition-on-previous-text False` (ADR-0008: repetition-loop
  hallucination on long recordings) → needs the launcher patch below.
- The manifest already allows an `hf` source on a delegate daemon
  (`voice-design`), which is what makes the real `modelId` visible in flaate's
  list.
- The live manifest is `~/Library/Application Support/flaate/models.json`
  (seeded once, never overwritten by deploys). flaate loads it at startup →
  restart required after edits. The committed bootstrap copy is
  `~/Projects/macos-dev-config/models.json`.
- Port `8099` is free in the manifest (8080–8098 are taken).

## Target state

```
flaate manifest
├─ whisper  :8099  delegate serve-whisper.sh → mlx_audio.server (+ launcher patches)
│    ├─ whisper-large-v3        hf mlx-community/whisper-large-v3-mlx
│    └─ whisper-large-v3-turbo  hf mlx-community/whisper-large-v3-turbo
└─ audio    :8089  delegate serve-audio.sh → PtM audio_server.py
     └─ nb-whisper-large (+ pyannote / DeepFilterNet / WPE / SepFormer)

PtM
├─ converter/audio.py    transcribe_fleet(model, path, language, words)
│                         ├─ whisper-* → OpenAI multipart /v1/audio/transcriptions
│                         └─ nb-whisper-large → existing JSON /v1/asr
├─ converter/transcribe.py  model resolution via fleet; no mlx_whisper subprocess
└─ TUI settings             model = fleet model names; old HF ids aliased
```

## Phase 1 — Fleet: whisper daemon online

Land this first; verify before touching PtM.

### 1.1 `macos-dev-config/tools/mlx-whisper-server.py` (new)

Model it on `tools/mlx-higgs-server.py` (launcher patch precedent). Two
documented, env-gated patches over `mlx_audio.server`:

```python
"""Launch mlx_audio.server with PtM-compatible whisper defaults.

Patches over upstream mlx-audio 0.5.9:
1. Model.generate defaults condition_on_previous_text=False (PtM ADR-0008);
   WHISPER_CONDITION_ON_PREVIOUS_TEXT=1 restores upstream.
2. ModelProvider keeps at most WHISPER_MAX_LOADED models (default 1); the
   upstream provider never evicts.
"""
import functools
import os
import mlx_audio.server as server
from mlx_audio.stt.models.whisper.whisper import Model

_orig_generate = Model.generate

@functools.wraps(_orig_generate)   # keeps inspect.signature() intact
def _generate(self, *args, **kwargs):
    enabled = os.environ.get("WHISPER_CONDITION_ON_PREVIOUS_TEXT", "0").lower() in {
        "1", "true", "yes", "on",
    }
    kwargs.setdefault("condition_on_previous_text", enabled)
    return _orig_generate(self, *args, **kwargs)

Model.generate = _generate

_orig_load = server.model_provider.load_model

def _load(model_name):
    max_loaded = int(os.environ.get("WHISPER_MAX_LOADED", "1") or "1")
    models = server.model_provider.models
    if max_loaded > 0 and model_name not in models:
        while len(models) >= max_loaded:
            models.pop(next(iter(models)))   # FIFO; single-user local server
    return _orig_load(model_name)

server.model_provider.load_model = _load

from mlx_audio.server import main  # noqa: E402
if __name__ == "__main__":
    main()
```

Notes:
- **`functools.wraps` is load-bearing**: the STT adapter filters request
  kwargs against `inspect.signature(stt_model.generate)`. A bare
  `*args, **kwargs` wrapper would hide `language`/`word_timestamps`/etc. and
  silently drop them. `wraps` exposes `__wrapped__`, which `inspect.signature`
  follows.
- Verify the patch points against the installed 0.5.9 before committing; fail
  loudly if `Model.generate`/`model_provider.load_model` move (assert attributes).
- The eviction is best-effort and single-process; the inference broker
  serializes model loads in practice.

### 1.2 `macos-dev-config/tools/serve-whisper.sh` (new)

Mirror `serve-higgs.sh`:

- `SERVER_BIN="mlx_audio.server"`, `LAUNCHER="$ROOT/tools/mlx-whisper-server.py"`.
- Resolve the uv-tool python from the console script's shebang.
- Verbs: `serve`, `start`, `stop`, `status`, `warm`, `log`.
- `_up` = `curl -sf http://$WHISPER_HOST:$WHISPER_PORT/v1/models`.
- Honor daemon `HOST`/`PORT` first, then `WHISPER_HOST`/`WHISPER_PORT`, then
  `127.0.0.1:8099`.
- Export `HF_HOME` from the SSD models root when present (copy the serve-higgs
  block).
- Log to `$ROOT/var/serve-whisper.log`.
- `warm`: `curl -X POST "http://$HOST:$PORT/v1/models?model_name=$WHISPER_MODEL"`
  (loads without inference), default `mlx-community/whisper-large-v3-mlx`.
- Install hint (do **not** reinstall with fewer extras; keep TTS working):
  `uv tool install --force "mlx-audio[server,stt,tts]" --with torch --with "safetensors<0.9" --python 3.12 --prerelease=allow`.
  Verify first that the existing 0.5.9 venv imports the whisper model class.

### 1.3 Manifest entries (both copies)

Add to the `daemons` array:

```json
{ "name": "whisper", "runner": "delegate", "delegate": "serve-whisper.sh", "host": "127.0.0.1", "port": 8099 }
```

Add to the `models` array:

```json
{
  "name": "whisper-large-v3",
  "daemon": "whisper",
  "source": { "kind": "hf", "repo": "mlx-community/whisper-large-v3-mlx" },
  "capabilities": { "contextLength": 448, "thinkingMode": false, "supportsSystemPrompt": false },
  "defaults": { "temperature": 0.0 },
  "modeTags": []
},
{
  "name": "whisper-large-v3-turbo",
  "daemon": "whisper",
  "source": { "kind": "hf", "repo": "mlx-community/whisper-large-v3-turbo" },
  "capabilities": { "contextLength": 448, "thinkingMode": false, "supportsSystemPrompt": false },
  "defaults": { "temperature": 0.0 },
  "modeTags": []
}
```

Edit both:
- `~/Projects/macos-dev-config/models.json` (committed bootstrap),
- `~/Library/Application Support/flaate/models.json` (live).

Then restart the control daemon:
`launchctl kickstart -k gui/$(id -u)/com.flaate.engine`
(or re-run `flaate/tools/deploy.sh`, which reloads the agent but does not
overwrite the live manifest).

### 1.4 Phase 1 verification

```bash
curl -s http://127.0.0.1:9300/list | jq '.models[] | select(.name|startswith("whisper"))'
curl -s -X POST http://127.0.0.1:9300/start/whisper-large-v3
curl -s http://127.0.0.1:8099/v1/models

# smoke (first call loads the model; allow minutes)
curl -s http://127.0.0.1:8099/v1/audio/transcriptions \
  -F model=mlx-community/whisper-large-v3-mlx \
  -F language=en -F response_format=verbose_json -F word_timestamps=true \
  -F file=@/path/to/clip.flac | jq '{language, n: (.segments|length), first: .segments[0]}'
```

Expected: `flaate` list shows both models with non-empty `modelId`; status
transitions up; the smoke returns segments with `start`/`end` and word timings.

No flaate code change is expected. Optionally add one line to
`SERVING-MANIFEST.md` documenting the launcher patches and `WHISPER_MAX_LOADED`.

## Phase 2 — PtM: one-step cutover (delete the subprocess lane)

This phase flips the default and removes the `mlx_whisper` CLI path in the same
change. Keep commits reviewable (client → routing → delete → tests/docs), but
ship as one release.

### 2.1 `converter/audio.py`

Add a model registry and one public ASR entry point:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class AsrModelSpec:
    repo: str
    base_url: str
    protocol: str          # "openai" | "audio_json"

ASR_MODELS = {
    "whisper-large-v3": AsrModelSpec(
        repo="mlx-community/whisper-large-v3-mlx",
        base_url="http://127.0.0.1:8099/v1",
        protocol="openai",
    ),
    "whisper-large-v3-turbo": AsrModelSpec(
        repo="mlx-community/whisper-large-v3-turbo",
        base_url="http://127.0.0.1:8099/v1",
        protocol="openai",
    ),
    "nb-whisper-large": AsrModelSpec(
        repo="NbAiLab/nb-whisper-large",
        base_url="http://127.0.0.1:8089/v1",
        protocol="audio_json",
    ),
}
AUDIO_MODEL_ALIASES = {
    "mlx-community/whisper-large-v3-mlx": "whisper-large-v3",
    "mlx-community/whisper-large-v3-turbo": "whisper-large-v3-turbo",
}
```

- `normalize_audio_model(value)` → canonical fleet name (alias map; unknown
  values pass through).
- `_asr_base_url(spec, model_name)` → protocol-scoped env override
  (`WHISPER_BASE_URL` for `openai`, existing `AUDIO_DIARIZE_BASE_URL` for
  `audio_json`) → `fleet.base_url(model_name)` → static `spec.base_url`.
- `transcribe_fleet(audio_path, *, model, language=None, word_timestamps=False,
  timeout=_ASR_TIMEOUT)`:
  - `protocol == "openai"`: stream a multipart POST to
    `<base>/audio/transcriptions` with `model=repo`, `language`,
    `response_format=verbose_json`, `word_timestamps`; parse:
    - words: `segments[*].words[*]` → `{start, end, text: word}`
    - segments: `segments[*]` → `{start, end, text}`
    - return words when requested, else segments (same contract as today's
      `asr(return_words=...)`).
  - `protocol == "audio_json"`: call the existing `asr()` unchanged (precision
    re-decode keeps `num_beams`).
- Multipart: use `http.client` and write the file in chunks (do not load a
  multi-hundred-MB FLAC into memory). Include the `Authorization` header when
  `WHISPER_API_KEY`/`AUDIO_DIARIZE_API_KEY` is set. Raise on non-2xx with the
  body's `error`/`detail`.
- Keep `asr()` for the audio daemon; it is no longer the default path.

### 2.2 `converter/transcribe.py`

- `AUDIO_MODEL = normalize_audio_model(os.environ.get("AUDIO_MODEL", "nb-whisper-large"))`
  (import `normalize_audio_model`/`transcribe_fleet` from `converter.audio`).
- Delete `AUDIO_ASR_SERVER_SENTINEL`, `AUDIO_MLX_WHISPER_BIN`, the `mlx_whisper`
  `cmd` block, the JSON-glob fallback handling, and the `use_server_asr` branch.
- `transcribe_audio()`: after ffmpeg/dereverb/enhance/isolate, always call
  `transcribe_fleet(..., model=AUDIO_MODEL, word_timestamps=return_words)`.
- `want_words = diarize_requested()` (both routes can return words now).
- `_redecode_windows()`: keep beam re-decode for `audio_json` (NB); for whisper
  models, log one `[WARN]` line and skip (the OpenAI form surface exposes no
  beam width; coverage is opt-in). Note this in ADR-0062.
- Keep the `AUDIO_MODEL` module global mutation contract used by
  `engine._apply_audio_options`.

### 2.3 `engine.py` + settings migration

- No new endpoints; `_apply_audio_options` already sets the model name.
- `GET /api/config` / settings reads must return canonical names via
  `normalize_audio_model`, so persisted old HF ids display correctly.
- `tui/settings.go`: update `audioModels` to
  `{"nb-whisper-large", "whisper-large-v3", "whisper-large-v3-turbo"}` and any
  human labels; keep the cycle order and default.
- `converter/config.py`: no `SERVERS` entry needed (audio resolves in
  `converter/audio.py`); only touch it if settings normalization lives there.

### 2.4 Tests

- `tests/test_transcribe.py`:
  - delete `test_transcribe_audio_routes_to_server_asr` and
    `test_transcribe_audio_server_asr_falls_back_to_mlx`;
  - add: `transcribe_audio` routes whisper models to the OpenAI client; nb to
    `asr`; a failed ASR call raises (no subprocess fallback);
  - add: `want_words` is true whenever diarization is requested.
- New `scripts/stub_whisper_server.py` (or an extended stub fixture) serving
  `/v1/models` and `/v1/audio/transcriptions` (`verbose_json` with
  `segments[*].words`), for hermetic tests and manual TUI smoke.
- `tests/test_transcribe_integration.py`: replace the `mlx_whisper` gate with
  "whisper daemon reachable + ffmpeg" (keep `PTM_RUN_AUDIO_INTEGRATION=1`).
- `tests/test_config.py`: alias normalization and old-setting migration.
- Existing `tests/test_audio_server.py` stays (NB route unchanged).

### 2.5 Docs and decisions

- New `docs/adr/0062-asr-on-the-fleet-whisper-daemon.md`:
  - decision, daemon/model layout, one-step cutover, no subprocess fallback;
  - consequences: transcription now requires the flaate daemon + `whisper`
    daemon; first request loads the model (~tens of seconds); one resident
    model; beam re-decode is NB-only.
- Amend:
  - `0005` — mlx-whisper subprocess lane superseded by 0062;
  - `0043` — defaults/model names now fleet names;
  - `0044` — fallback semantics (no mlx fallback; warn + fail the job);
  - `0045` — engine-hosted job unchanged, but progress is phase-level only.
- Update `docs/adr/README.md` index.
- Update `AGENTS.md`: audio bullet (`whisper` daemon, no `mlx_whisper`),
  commands/gotchas (`start whisper-large-v3`; no `AUDIO_MLX_WHISPER_BIN`).
- Update `docs/ai-audio.md`:
  - models table: whisper models served by the fleet daemon;
  - remove the `uv tool install mlx-whisper` step; keep ffmpeg;
  - config table: drop `AUDIO_MLX_WHISPER_BIN`, document `AUDIO_MODEL` names,
    add `WHISPER_BASE_URL`/`WHISPER_API_KEY` (audio daemon keeps
    `AUDIO_DIARIZE_BASE_URL`);
  - pipeline diagram unchanged except the final `→ ASR (fleet)`.
- Update `docs/runbook.md`: prerequisites, health checks (`start
  whisper-large-v3`), troubleshooting rows, integration-test instructions.
- `macos-dev-config/SERVING-MANIFEST.md`: whisper daemon + models + launcher
  patch note.

### 2.6 Verification

```bash
# daemon still healthy
curl -s http://127.0.0.1:9300/status/whisper-large-v3

# unit suite
./.venv/bin/python -m pytest

# end-to-end through the engine/TUI (no CLI spikes)
ptm-tui
# settings: model=whisper-large-v3, language=en, diarize=on, speakers=2
# run a 2-speaker English clip; expect SPEAKER_00/SPEAKER_01 + word-quality turns
```

Check:
- transcript header names the fleet model;
- `.srt` and Markdown timestamps match the cleaned FLAC;
- with `whisper` stopped, the job fails with a clear warning (no silent
  subprocess fallback);
- `flaate` list shows `whisper` up when the server runs; `GET /v1/models` shows
  the loaded repo, and the manifest `modelId` is the HF repo.

## Rollback

- PtM: revert the cutover commit; the deleted lane returns with the code.
  `mlx-whisper` remains installed on the machine, so no reinstall is needed.
- Fleet: remove the daemon/model entries from both manifests and restart
  flaate; `serve-whisper.sh`/launcher can stay (inert) or be removed.
- If the launcher patches break on an `mlx-audio` upgrade, bypass them by
  starting `mlx_audio.server` directly (temporarily accepting upstream
  `condition_on_previous_text=True` and no eviction).

## Risks / mitigations

| Risk | Mitigation |
|---|---|
| mlx-audio upgrade moves the patch points | assert attributes in the launcher; pin the tool version in `serve-whisper.sh`'s install hint; document bypass |
| First request loads ~3 GB model | engine heartbeat already streams; `serve-whisper.sh warm`; turbo for speed |
| Both models resident | launcher FIFO eviction (`WHISPER_MAX_LOADED=1`) |
| Long recordings exceed the 1 h HTTP timeout | same `AUDIO_TIMEOUT=3600` as today; recommend turbo for long files; raise `AUDIO_TIMEOUT` if needed |
| Multi-hundred-MB multipart upload | stream from disk via `http.client`; loopback only |
| No beam re-decode on whisper | coverage is opt-in; ADR-0062 records the limitation; follow-up can add a path/beam endpoint |
| NB-Whisper still PtM-owned | Plan B (extract audio-serving) remains the follow-up; Plan C shrinks PtM's serving surface to the audio server |
| `transcriber` (vision) name confusion persists | document in ADRs/runbook; renaming is out of scope (breaks consumers) |

## Non-goals

- Extracting `scripts/audio_server.py` into its own project (Plan B).
- Moving NB-Whisper onto the whisper daemon (PyTorch vs MLX) or unifying its
  contract in this change.
- Fleet-derived dynamic TUI model lists (manifest schema/modeTags change).
- Realtime streaming STT (`/v1/realtime`) in the TUI.

## Checklist

- [ ] Phase 1.1 launcher + docs
- [ ] Phase 1.2 `serve-whisper.sh`
- [ ] Phase 1.3 manifest (committed + live) + flaate restart
- [ ] Phase 1.4 smoke test + list shows both models
- [ ] Phase 2.1 `converter/audio.py` registry + OpenAI multipart client
- [ ] Phase 2.2 `converter/transcribe.py` routing; delete subprocess lane
- [ ] Phase 2.3 settings migration + TUI cycle
- [ ] Phase 2.4 tests (unit, stub, integration gate)
- [ ] Phase 2.5 ADR-0062 + amendments + docs
- [ ] Phase 2.6 full pytest + TUI 2-speaker English run
- [ ] Update this file's status to done
