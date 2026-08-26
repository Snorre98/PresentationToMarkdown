# Audio Transcription Runbook

Local audio→text via the standalone **`ptm-transcribe`** command. Transcribes a
lecture recording, attaches a timestamped, speaker-labelled `# Transcript` to an
existing Markdown file (or writes a standalone `<stem>.transcript.md`), and saves
a **cleaned** copy of the audio (`.clean.flac`). Transcription is decoupled from
conversion (ADR-0009): `ptm`/`ptm-start` never transcribe.

**Hardware:** Apple M4, 32 GB · **ASR:** `mlx-whisper` · **Enhancement:** DeepFilterNet · **Diarization:** pyannote

## 1. Prerequisites (one-time)

| Tool | Check | Install |
| --- | --- | --- |
| `ffmpeg` | `which ffmpeg` | `brew install ffmpeg` (already present) |
| `mlx-whisper` | `which mlx_whisper` | `uv tool install mlx-whisper --python 3.12` |
| `uv` | `which uv` | `brew install uv` (already present) |

Verify: `mlx_whisper --help` and `ffmpeg -version` both return.

> First transcription downloads `mlx-community/whisper-large-v3-turbo`
> (~1.6 GB) to `HF_HOME` (your SSD). The converter never downloads models
> itself.

## 2. The audio-model server (diarization + enhancement)

Both speaker labels and deep denoise/dereverb run in one PyTorch service on
`:8083`. Two endpoints:

```json
// POST /v1/diarize — who spoke when
{"path": "/abs/path/lecture.flac", "min_speakers": 1, "max_speakers": 4}
-> [{"start": 0.0, "end": 9.2, "speaker": "SPEAKER_00"}, ...]

// POST /v1/enhance — denoise + dereverb, write a cleaned file
{"path": "/abs/path/lecture.flac", "output": "/abs/path/lecture.clean.flac"}
-> {"ok": true}
```

Both are managed by one script, `scripts/audio_serve.sh` (start/stop/status +
install + optional launchd always-on — the same lifecycle the vision models get
from `macos-dev-config/tools/serve.sh`).

### Option A — Stub (for testing, no PyTorch, no Hugging Face)

```bash
scripts/audio_serve.sh stub-start          # start in the background
scripts/audio_serve.sh stub-status
scripts/audio_serve.sh stub-stop
```

Fakes speaker turns and copies audio, so the whole pipeline runs end-to-end
without installing PyTorch or touching Hugging Face. (Under the hood it runs
`.venv/bin/python scripts/stub_audio_server.py`; pass `--port N` to override the
default `8083`.)

### Option B — Real server (isolated venv)

```bash
scripts/audio_serve.sh install             # one-time: create ~/tools/audio-env (py3.11) + deps
scripts/audio_serve.sh start               # start in the background
scripts/audio_serve.sh status              # running? on which port?
scripts/audio_serve.sh log                 # tail -f the server log
scripts/audio_serve.sh stop
```

`install` prefers `uv` (`uv venv … --python 3.11` + `uv pip install …`) and
falls back to `python3.11 -m venv` / `pip` when `uv` is absent. It is idempotent.

> The pinned versions in `requirements-audio.txt` (Python 3.11, torch 2.5.1,
> torchaudio 2.5.1, pyannote 3.4.0, deepfilternet 0.5.6, huggingface-hub <1.0)
> are interdependent and are what the server was tested against. See the header
> comment in that file for the rationale.

Only the **diarization** model is gated behind Hugging Face; **enhancement
(DeepFilterNet) needs no HF account at all.** So:

- If you only want enhancement (no speaker labels), just `start` the server — no
  Hugging Face setup.
- If you want speaker labels too, complete **§2.1 (Hugging Face setup)** first,
  then `start` it.

`start` reads `HF_TOKEN` from the environment, or from a git-ignored `.env` in
the repo root (see §2.1, Step 4). State lives in `~/.local/state/ptm`
(`audio.pid` / `audio.log`); override with `PTM_STATE_DIR`, the port with
`PTM_AUDIO_PORT` or `--port N`.

**Always-on (survive reboot):**

```bash
scripts/audio_serve.sh launchd-install     # LaunchAgent, RunAtLoad + KeepAlive
scripts/audio_serve.sh launchd-uninstall   # remove it
```

### 2.1 Hugging Face setup (only for `--diarize` / speaker labels)

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

> If no server is running, `--diarize` logs `[WARN] Diarization failed: …` and
> still produces an **unlabelled** transcript; enhancement degrades to the
> built-in ffmpeg chain. Never fatal.

## 3. Running transcription

```bash
# with existing Markdown: discover same-stem audio beside the .md and attach
ptm-transcribe deck.md                          # deck.mp3/m4a/wav beside deck.md

# without Markdown: transcribe straight to a transcript file
ptm-transcribe week-2.mp3                       # -> week-2.transcript.md

# re-running a standalone file is append-only (never overwrites):
ptm-transcribe week-2.mp3                       # -> week-2.transcript.1.md, .2.md, …
ptm-transcribe week-2.mp3 --overwrite           # force-replace the base transcript.md

# explicit pairing / picking a lecture
ptm-transcribe week-2.mp3 --to deck.md          # attach week-2's audio to deck.md
ptm-transcribe --audio-file lecture.m4a deck.md # explicit audio for deck.md
ptm-transcribe --diarize deck.md                # + speaker labels
ptm-transcribe --language no week-2.mp3         # language hint

# folders are scanned recursively for .md and audio files
ptm-transcribe lectures/                        # pair by stem; prompt on ambiguity
```

`ptm-transcribe` sets `AUDIO_ENABLED=1` (and `--diarize`/`--language` as given)
itself, so no `--audio` flag is needed — and `ptm`/`ptm-start` no longer accept
one. The raw-env equivalent:

```bash
AUDIO_ENABLED=1 ./.venv/bin/python -m cli_transcribe deck.md
```

No venv activated? Run the wrapper from the repo root — it bootstraps the venv
(`pip install -e .`) on first use:

```bash
scripts/ptm-transcribe.sh /path/to/week-2.mp3
```

**Audio pairing rules (in order):**

1. Explicit `--to MARKDOWN.md` — attach to that file.
2. Explicit `--audio-file PATH` — paired by stem to a `.md`, or the sole target.
3. Convention: same folder, same stem — `deck.md` + `deck.mp3`
   (priority `.wav > .m4a > .mp3 > …`).
4. Interactive prompt — when an audio file matches no `.md` and candidates exist,
   `ptm-transcribe` asks which lecture it belongs to (`[0]` = standalone).
5. Nothing found → a `[WARN]`, or a standalone `<stem>.transcript.md`.

Standalone transcripts are **append-only**: re-running the same audio writes
`<stem>.transcript.1.md`, `.2.md`, … (with a matching `.srt` **and** `.clean.<N>.flac`)
so past transcripts
are kept for A/B comparison. Use `--overwrite` to replace the base file instead.

### Progress output & the single-instance guard

While a file is transcribing, `ptm-transcribe` streams live progress to stderr —
ffmpeg and mlx-whisper output (including the first-run ~1.6 GB model download)
appears in real time, with short phase lines (`ffmpeg` / `enhancing` /
`transcribing` / `diarizing`). If a phase goes quiet (e.g. the model download),
a `still working … (elapsed …)` heartbeat is printed every
`AUDIO_HEARTBEAT_SECONDS` seconds (default `20`). When stderr is piped
(CI/scripts), carriage-return progress bars are suppressed and only the start /
heartbeat / phase / result lines are emitted.

Only one `ptm-transcribe` may run at a time. It holds an exclusive `flock` on
`<PTM_STATE_DIR or ~/.local/state/ptm>/transcribe.lock` (writing its PID into
it); a second invocation fails fast with exit code `3` and
`ptm-transcribe: another instance is already running (PID …)`, so two runs never
clobber each other's `.clean.flac` / `.md` / `.srt`. The lock is released on
normal exit and on `SIGINT`/`SIGTERM`, and the OS drops it automatically if the
process is killed. Ctrl-C mid-transcription terminates the child subprocess,
cleans up the per-run temp dir, removes partial temp output, and releases the
lock (exit `130`). Output files are written atomically (temp file + `os.replace`),
so an interrupt never leaves a truncated `.md` / `.srt` / `.clean.flac`.

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
timestamps match the transcript. The source recording is never modified.

Expected Markdown:

```markdown
# Transcript
<details>
<summary>Auto-generated transcript (mlx-community/whisper-large-v3-turbo)</summary>

[00:00:04] **Speaker A:** Welcome to today's lecture.
[00:00:21] **Speaker A:** We'll cover three topics.

</details>
```

## 5. Testing

```bash
# fast unit tests (no model/binary needed)
./.venv/bin/python -m pytest tests/test_transcribe.py -v

# the ptm-transcribe CLI (parser, pairing, end-to-end with faked subprocess)
./.venv/bin/python -m pytest tests/test_cli_transcribe.py -v

# client vs the stub server (no model/binary needed)
./.venv/bin/python -m pytest tests/test_transcribe_integration.py -v

# full suite
./.venv/bin/python -m pytest -q

# real ffmpeg + mlx-whisper end-to-end (opt-in; downloads the model on first run)
PTM_RUN_AUDIO_INTEGRATION=1 ./.venv/bin/python -m pytest tests/test_transcribe_integration.py -v
```

## 6. Model tuning

| Goal | Setting |
| --- | --- |
| Max ASR quality | `AUDIO_MODEL=mlx-community/whisper-large-v3-mlx` (or `--env AUDIO_MODEL=…`) |
| Norwegian (avoid mis-detect on short clips) | `AUDIO_LANGUAGE=no` |
| Skip enhancement (A/B check) | `AUDIO_PREPROCESS=0 AUDIO_ENHANCE_ENABLED=0` |
| Enhancement only, no speaker labels | run the server, leave `--diarize` off |
| Default | `mlx-community/whisper-large-v3-turbo` |

## 7. Troubleshooting

| Symptom | Cause → Fix |
| --- | --- |
| `[WARN] Audio transcription failed: mlx_whisper not found` | `uv tool install mlx-whisper`, or set `AUDIO_MLX_WHISPER_BIN` |
| `[WARN] … ffmpeg not found` | `brew install ffmpeg` |
| `[WARN] Audio enhancement failed: …` | Server down → start `scripts/audio_server.py`, or set `AUDIO_ENHANCE_ENABLED=0` |
| `[WARN] Diarization failed: …` | Server down → start it, or drop `--diarize` |
| Server logs a 401/403 "gated repo" on startup | Re-do §2.1: accept both model licenses + create a **Read** token |
| No `# Transcript` appears | No same-stem audio found → pass `--audio-file` or `--to` |
| Wrong language / gibberish | Set `AUDIO_LANGUAGE`, or upgrade to `large-v3-mlx` |
| Long silence / music transcribed | Whisper hallucination → trim the recording, or try `AUDIO_CONDITION_ON_PREVIOUS_TEXT=1` |
| `ptm-transcribe: another instance is already running (PID …)` (exit 3) | Another transcription is active — wait for it, or stop that PID (the `flock` releases on process death) |
