# Audio Transcription Runbook

Local audio→text via **engine-hosted transcription** (ADR-0045), driven from the
TUI (`ptm-tui`): select an audio row and press Enter. It transcribes a lecture
recording, attaches a timestamped, speaker-labelled `# Transcript` to an
existing Markdown file (or writes a standalone `<stem>.transcript.md`), and saves
a **cleaned** copy of the audio (`.clean.flac`). Transcription is decoupled from
conversion (ADR-0009): `convert_files` never transcribes.

**Hardware:** Apple M4, 32 GB · **ASR:** fleet `whisper` daemon (MLX) + NB-Whisper · **Enhancement:** DeepFilterNet · **Diarization:** pyannote

## 1. Prerequisites (one-time)

| Tool | Check | Install |
| --- | --- | --- |
| `ffmpeg` | `which ffmpeg` | `brew install ffmpeg` (already present) |
| fleet whisper daemon | `curl -sf http://127.0.0.1:8099/v1/models` (after a start) | managed by flaate from `macos-dev-config/models.json`; see `SERVING-MANIFEST.md` |
| audio server (`audio` daemon) | `curl -sf http://127.0.0.1:8089/v1/models` | `scripts/audio_serve.sh install` + start (below) |

Verify: `ffmpeg -version` and
`curl -s -X POST http://127.0.0.1:9300/start/whisper-large-v3-asr-fp16` both succeed.

> The whisper models download on their first load (to `HF_HOME`, your SSD) from
> inside the daemon; the converter never downloads models itself. The stored
> repos are the mlx-audio `-asr-fp16` conversions
> (`mlx-community/whisper-large-v3-asr-fp16`, `…-turbo-asr-fp16`) because the
> older `whisper-large-v3-mlx` repos carry no HF processor (ADR-0062).
> `serve-whisper.sh warm [model]` preloads without inference.

## 2. The audio-model server (diarization + enhancement)

Both speaker labels and deep denoise/dereverb run in one PyTorch service on
`:8089` — a manifest daemon (`audio`) owned by the `macos-dev-config` control
daemon (ADR-0035), like every other ML service (ADR-0029). Four endpoints:

```json
// POST /v1/diarize — who spoke when
{"path": "/abs/path/lecture.flac", "min_speakers": 1, "max_speakers": 4}
-> [{"start": 0.0, "end": 9.2, "speaker": "SPEAKER_00"}, ...]

// POST /v1/enhance — denoise (DeepFilterNet), write a cleaned file
{"path": "/abs/path/lecture.flac", "output": "/abs/path/lecture.clean.flac"}
-> {"ok": true}

// POST /v1/dereverb — dereverberate (WPE), write a de-echoed file
{"path": "/abs/path/lecture.clean.flac", "output": "/abs/path/lecture.clean.flac"}
-> {"ok": true}

// POST /v1/isolate — isolate the dominant voice (SepFormer)
{"path": "/abs/path/lecture.clean.flac", "output": "/abs/path/lecture.isolated.flac"}
-> {"ok": true}

// POST /v1/asr — NB-Whisper (return_words = per-word timestamps)
{"path": "/abs/path/lecture.clean.flac", "language": "no", "return_words": false}
-> [{"start": 0.0, "end": 9.2, "text": "..."}, ...]
```

The MLX whisper models are served by a **separate fleet daemon**
(`whisper`, `:8099`) through the OpenAI transcription contract:

```bash
curl -s http://127.0.0.1:8099/v1/audio/transcriptions \
  -F model=mlx-community/whisper-large-v3-asr-fp16 \
  -F language=en -F response_format=verbose_json -F word_timestamps=true \
  -F file=@lecture.clean.flac
```

The real servers are managed by the control daemon (`com.flaate.engine`);
`scripts/audio_serve.sh` proxies the audio daemon's verbs (start/stop/status/log)
and keeps the one-time venv install + the no-PyTorch test stub.

### Option A — Stub (for testing, no PyTorch, no Hugging Face)

```bash
scripts/audio_serve.sh stub-start          # start in the background
scripts/audio_serve.sh stub-status
scripts/audio_serve.sh stub-stop
```

Fakes speaker turns and copies audio, so the whole pipeline runs end-to-end
without installing PyTorch or touching Hugging Face. (Under the hood it runs
`.venv/bin/python scripts/stub_audio_server.py`; pass `--port N` to override the
default `8089`.)

### Option B — Real server (isolated venv, daemon-managed)

```bash
scripts/audio_serve.sh install             # one-time: create ~/tools/audio-env (py3.11) + deps
scripts/audio_serve.sh start               # blocks on the daemon's start (60s health bound)
scripts/audio_serve.sh status              # daemon state (up/down/starting/…)
scripts/audio_serve.sh log                 # tail -f the daemon-side server log
scripts/audio_serve.sh stop
```

`install` prefers `uv` (`uv venv … --python 3.11` + `uv pip install …`) and
falls back to `python3.11 -m venv` / `pip` when `uv` is absent. It is idempotent.

> The pinned versions in `requirements-audio.txt` (Python 3.11, torch 2.5.1,
> torchaudio 2.5.1, pyannote 3.4.0, deepfilternet 0.5.6, huggingface-hub <1.0,
> nara_wpe 0.0.11, speechbrain 1.0.2) are interdependent and are what the server
> was tested against. See the header comment in that file for the rationale.

Only the **diarization** model is gated behind Hugging Face; **enhancement
(DeepFilterNet), dereverberation (WPE) and isolation (SepFormer) need no HF
account at all.** So:

- If you only want enhancement/dereverb (no speaker labels), just `start` the
  server — no Hugging Face setup.
- If you want speaker labels too, complete **§2.1 (Hugging Face setup)** first,
  then `start` it.

> SepFormer (voice isolation) downloads its weights on the first `/v1/isolate`
> request (~100–200 MB, ungated). WPE is pure NumPy — no model, no download.

`start` reads `HF_TOKEN` from the environment, or from a git-ignored `.env` in
the repo root (see §2.1, Step 4). The server binds the manifest's `:8089`; the
stub's state lives in `~/.local/state/ptm` (`stub.pid` / `stub.log`; override
with `PTM_STATE_DIR`, the stub port with `PTM_AUDIO_PORT` or `--port N`), and
DeepFilterNet's noise-attenuation limit with `AUDIO_ENHANCE_ATTEN_DB`
(server-side; default `12.0` dB, `0` = full suppression).

**Always-on (survive reboot):** the `com.flaate.engine` LaunchAgent is the
always-on authority for every manifest daemon, the audio server and the
`whisper` ASR daemon included (ADR-0035/0062, flaate ADR-0001). There is no
separate audio LaunchAgent anymore; `launchd-install` was removed.

### 2.1 Hugging Face setup (only for speaker labels)

The `pyannote/speaker-diarization-3.1` model is **gated**: Hugging Face will
refuse to serve it unless you (a) have an account, (b) accepted its license
terms, and (c) authenticate with a token. This is a one-time, ~2-minute task.

**Step 1 — Create an account (skip if you have one).**

Go to <https://huggingface.co/join>, sign up (free), and stay signed in.

**Step 2 — Accept the license on TWO model pages.**

Open each of these URLs, signed in, and click **"Agree and access repository"**
(you won't see the button unless you're logged in). Do this for **both**:

1. <https://huggingface.co/pyannote/speaker-diarization-3.1>
2. <https://huggingface.co/pyannote/segmentation-3.0>

> `speaker-diarization-3.1` pulls `segmentation-3.0` in as a dependency, so both
> gates must be accepted or the download will fail with a 401/403.

**Step 3 — Create a read token.**

1. Go to <https://huggingface.co/settings/tokens>.
2. Click **"New token"** (or "Create new token").
3. **Name** — anything, e.g. `ptm-diarize`.
4. **Token type** — choose **"Read"** (not "Write", not "Fine-grained").
5. Click **"Create token"**.
6. **Copy** the token — it looks like `hf_XXXXXXXXXXXXXXXXXXXX`. This is your
   only chance to copy it; Hugging Face shows it once.

**Step 4 — Give it to the server.**

The management script (`scripts/audio_serve.sh start`) reads `HF_TOKEN` from the
environment, or from a git-ignored `.env` in the repo root. Any of these work:

```bash
# inline (one command)
HF_TOKEN=hf_XXXXXXXXXXXXXXXXXXXX scripts/audio_serve.sh start

# export for the session
export HF_TOKEN=hf_XXXXXXXXXXXXXXXXXXXX
scripts/audio_serve.sh start

# git-ignored .env in the repo root (never committed)
echo 'HF_TOKEN=hf_XXXXXXXXXXXXXXXXXXXX' >> .env
scripts/audio_serve.sh start

# permanent (adds it to your shell profile)
echo 'export HF_TOKEN=hf_XXXXXXXXXXXXXXXXXXXX' >> ~/.zshrc && source ~/.zshrc
scripts/audio_serve.sh start
```

`launchd-install` embeds the token in the LaunchAgent plist at install time
(machine-local, under `~/Library/LaunchAgents`) — re-run it after changing the
token.

The token is only used to download the weights on the first run (cached in
`~/.cache/huggingface/` after that); no audio is ever sent to Hugging Face.

**How to check it worked:** after starting the server, its first request should
download the model and return speaker turns. If you instead see a 401/403
"gated repo" error, you're missing Step 2 (accept the license) or Step 3 (token).

> If no server is running, enabling diarization logs `[WARN] Diarization failed: …`
> and still produces an **unlabelled** transcript; enhancement degrades to the
> built-in ffmpeg chain. Never fatal.

## 3. Running transcription

In the TUI, audio files in the current directory are marked `♫`: select one (or
several) and press Enter. The engine runs the transcription and streams progress
into the run pane. The settings screen (`s`) covers diarization, speaker count,
model, and language; the same options apply to the engine from the environment:

```bash
# enable transcription (the engine sets AUDIO_ENABLED itself per job)
AUDIO_ENABLED=1 scripts/ptm-tui.sh

# diarization + pinned speaker count (e.g. a two-person interview)
AUDIO_DIARIZE_ENABLED=1 AUDIO_DIARIZE_SPEAKERS=2 scripts/ptm-tui.sh

# isolate the dominant voice / language hint
AUDIO_ISOLATE_ENABLED=1 AUDIO_LANGUAGE=no scripts/ptm-tui.sh
```

**Audio pairing rules (in order):**

1. Convention: same folder, same stem — `deck.md` + `deck.mp3`
   (priority `.wav > .m4a > .mp3 > …`) — the transcript is attached to the
   Markdown as a `# Transcript` section.
2. Selecting the `.md` directly attaches its same-stem audio.
3. Nothing found → a standalone `<stem>.transcript.md` (plus `.srt` and
   `.clean.flac`).

Standalone transcripts are **append-only**: re-running the same audio writes
`<stem>.transcript.1.md`, `.2.md`, … (with a matching `.srt` **and** `.clean.<N>.flac`)
so past transcripts are kept for A/B comparison. Attaching to a `.md` is
idempotent — the `# Transcript` section is replaced, not duplicated.

The audio pipeline is `ffmpeg clean → WPE dereverb → DeepFilterNet enhance →
[SepFormer isolate] → fleet ASR`. WPE dereverberation and enhancement run
whenever the server is up (both default on, degrade to a `[WARN]` if it is
down); voice isolation runs only with `--isolate` and writes a
`<stem>.isolated.<N>.flac` that ASR transcribes instead of the cleaned file.
ASR is an HTTP call to the audio server (`nb-whisper-large`) or the fleet
`whisper` daemon (`whisper-large-v3-asr-fp16` / `whisper-large-v3-turbo-asr-fp16`); a dead daemon fails the job
with a warning and there is no mlx-whisper fallback (ADR-0062).

### Progress output & the single-instance guard

While a file transcribing, the engine streams live progress over its WebSocket:
ffmpeg output appears in real time with short phase frames (`ffmpeg` /
`dereverberating` / `enhancing` / `isolating` / `transcribing` / `diarizing`).
The ASR phase itself is **phase-level only** — one HTTP call to a daemon, with
no incremental transcript — so expect silence there (and a longer first request
while the model loads). Local subprocess phases emit a heartbeat line every
`AUDIO_HEARTBEAT_SECONDS` seconds (default `20`).

Only one transcription may run at a time. The engine holds an exclusive `flock`
on `<PTM_STATE_DIR or ~/.local/state/ptm>/transcribe.lock` (writing its PID
into it) for the job's duration; a second job is rejected with “Another
transcription is already running.” in the run pane, so two runs never clobber
each other's `.clean.flac` / `.md` / `.srt`. The lock is released when the job
ends, and the OS drops it automatically if the process is killed. Output files
are written atomically (temp file + `os.replace`), so an interrupt never leaves
a truncated `.md` / `.srt` / `.clean.flac`.

## 4. Verify the output

```bash
ls out/                          # deck.md, deck.transcript.srt, deck.clean.flac
tail -40 out/deck.md             # "# Transcript" section at the end
cat out/deck.transcript.srt      # SubRip timestamps + speaker cues
afinfo out/deck.clean.flac       # the cleaned audio (or: afplay to listen)

sqlite3 ptm.sqlite \
  "SELECT start,end,speaker,text FROM transcript_segments WHERE source LIKE '%deck.md' ORDER BY start;"
```

`deck.clean.flac` is the exact cleaned audio Whisper transcribed, so its
timestamps match the transcript. With `--isolate`, Whisper transcribes
`deck.isolated.flac` instead (the voice-isolated stream). The source recording
is never modified.

Expected Markdown:

```markdown
# Transcript
<details>
<summary>Auto-generated transcript (whisper-large-v3-asr-fp16)</summary>

[00:00:04] **Speaker A:** Welcome to today's lecture.
[00:00:21] **Speaker A:** We'll cover three topics.

</details>
```

## 5. Testing

```bash
# fast unit tests (no model/binary needed)
./.venv/bin/python -m pytest tests/test_transcribe.py -v

# client vs the stub server (no model/binary needed)
./.venv/bin/python -m pytest tests/test_transcribe_integration.py -v

# full suite
./.venv/bin/python -m pytest -q

# real ffmpeg + fleet whisper daemon end-to-end (opt-in; start the daemon first)
PTM_RUN_AUDIO_INTEGRATION=1 ./.venv/bin/python -m pytest tests/test_transcribe_integration.py -v
```

## 6. Model tuning

| Goal | Setting |
| --- | --- |
| Max ASR quality | `AUDIO_MODEL=whisper-large-v3-asr-fp16` |
| Speed (non-Norwegian) | `AUDIO_MODEL=whisper-large-v3-turbo-asr-fp16` |
| Norwegian (avoid mis-detect on short clips) | `AUDIO_MODEL=nb-whisper-large` + `AUDIO_LANGUAGE=no` |
| Skip enhancement (A/B check) | `AUDIO_PREPROCESS=0 AUDIO_ENHANCE_ENABLED=0` |
| Skip dereverberation (very dry audio) | `AUDIO_DEREVERB_ENABLED=0` |
| Isolate voice from music/chatter | `AUDIO_ISOLATE_ENABLED=1` (TUI setting) |
| Enhancement only, no speaker labels | run the server, leave diarization off |
| Default | `nb-whisper-large`, language `no`, diarize on, 2 speakers |

Start/stop the whisper daemon through flaate (model name, not daemon name):

```bash
curl -s -X POST http://127.0.0.1:9300/start/whisper-large-v3-asr-fp16
curl -s -X POST http://127.0.0.1:9300/stop/whisper-large-v3-asr-fp16
/Users/snorresaether/Projects/macos-dev-config/tools/serve-whisper.sh warm whisper-large-v3-asr-fp16
```

## 7. Troubleshooting

| Symptom | Cause → Fix |
| --- | --- |
| `[WARN] Audio transcription failed: transcription returned HTTP …` / connection refused | Whisper daemon down → `curl -X POST http://127.0.0.1:9300/start/whisper-large-v3-asr-fp16` (no fallback lane exists) |
| `[WARN] … unknown ASR model` | `AUDIO_MODEL` is not a fleet name → use `nb-whisper-large`, `whisper-large-v3-asr-fp16` or `whisper-large-v3-turbo-asr-fp16` |
| `[WARN] … ffmpeg not found` | `brew install ffmpeg` |
| `[WARN] Audio enhancement failed: …` | Server down → `scripts/audio_serve.sh start` (daemon-managed `audio`, :8089), or set `AUDIO_ENHANCE_ENABLED=0` |
| `[WARN] Audio dereverberation failed: …` | Server down → start it, or set `AUDIO_DEREVERB_ENABLED=0` |
| `[WARN] Voice isolation failed: …` | Server down or SepFormer not installed → start it / install `speechbrain`, or disable `AUDIO_ISOLATE_ENABLED` |
| `[WARN] Diarization failed: …` | Server down → start it, or disable diarization |
| Server logs a 401/403 "gated repo" on startup | Re-do §2.1: accept both model licenses + create a **Read** token |
| No `# Transcript` appears | No same-stem audio found → select the Markdown whose audio you want attached |
| Wrong language / gibberish | Set `AUDIO_LANGUAGE`, or use `whisper-large-v3-asr-fp16` instead of NB-Whisper |
| Long silence / music transcribed | Whisper hallucination → trim the recording, or set `WHISPER_CONDITION_ON_PREVIOUS_TEXT=1` where the whisper daemon starts |
| `[WARN] Fallback re-decode is NB-Whisper only …` | Expected for whisper models: the OpenAI transcription form has no beam width (ADR-0062) |
| First request is slow / times out | The daemon is loading ~3 GB → `serve-whisper.sh warm`, or use turbo / raise `AUDIO_TIMEOUT` |
| “Another transcription is already running.” in the run pane | Another transcription is active — wait for it, or stop the engine (the `flock` releases on process death) |
