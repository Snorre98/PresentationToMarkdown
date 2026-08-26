# AI audio pass (optional)

A local **audio-to-text transcription** that turns a lecture recording into a
timestamped, speaker-labelled transcript and attaches it to a Markdown file (or
writes a standalone `<stem>.transcript.md` when none exists yet). It runs as its
own `ptm-transcribe` command, decoupled from conversion (ADR-0009). It is
strictly a *companion*: the deterministic text extraction always runs first, and
the transcript is appended as a `# Transcript` section (plus a `.srt` sidecar).
Along the way the audio is **cleaned** (denoise + dereverb + loudness) and the
cleaned audio is **persisted** as a `.clean.flac`.

> Transcription runs **locally** — no audio leaves the machine. This document
> covers how to serve the models and enable the pass. See the
> [ADR index](adr/README.md) (0004–0009) for the rationale.

## Models and runtime

| Role | Model | Runtime | Notes |
| --- | --- | --- | --- |
| ASR (default) | `mlx-community/whisper-large-v3-turbo` | `mlx-whisper` (MLX) | 809M params, ~4–5× realtime on Apple Silicon |
| ASR (max quality) | `mlx-community/whisper-large-v3-mlx` | `mlx-whisper` (MLX) | 1.55B params, ~1× realtime |
| Enhancement | DeepFilterNet (denoise + dereverb) | PyTorch server (`:8083`) | optional, ~8 MB, no gating |
| Diarization | `pyannote/speaker-diarization-3.1` | PyTorch server (`:8083`) | optional, gated HF model |

### 1. Install the ASR toolchain

`mlx-whisper` (via `uv`, Python 3.12) and `ffmpeg`:

```bash
brew install uv ffmpeg
uv tool install mlx-whisper --python 3.12
```

`mlx_whisper` downloads weights on first run (to `HF_HOME`, see
`macos-dev-config/inference-readme.md`). The converter never downloads models
itself — it invokes `mlx_whisper` as a subprocess.

### 2. (Optional) Serve the audio-model server

Speaker labels and deep enhancement run in a single PyTorch service (its own
venv, on `:8083`), because `pyannote-audio` and `deepfilternet` are deliberately
kept out of `converter` (ADR-0006, ADR-0008):

```bash
scripts/audio_serve.sh install       # one-time: ~/tools/audio-env (py3.11) + deps
scripts/audio_serve.sh start         # background; reads HF_TOKEN from env or ./.env
scripts/audio_serve.sh status
scripts/audio_serve.sh stop
scripts/audio_serve.sh stub-start    # no-PyTorch stand-in for testing
```

`install` uses `uv` (falls back to `python3.11 -m venv`/`pip`). For reboot
persistence, `scripts/audio_serve.sh launchd-install` installs a `RunAtLoad` +
`KeepAlive` LaunchAgent. See [docs/runbook.md](runbook.md) for the full
operational runbook.

> Only **diarization** needs Hugging Face; enhancement (DeepFilterNet) does not.
> For the exact Hugging Face steps — accept the licenses on
> `pyannote/speaker-diarization-3.1` **and** `pyannote/segmentation-3.0`, then
> create a **Read** token — see
> [the runbook's "Hugging Face setup" section](runbook.md#21-hugging-face-setup-only-for---diarize--speaker-labels).

The service exposes two endpoints:

- `POST /v1/diarize` — `{"path": "<audio>", "min_speakers": n, "max_speakers": n}`
  → `[{"start": f, "end": f, "speaker": "SPEAKER_00"}, …]`.
- `POST /v1/enhance` — `{"path": "<in>", "output": "<out>"}` → `{"ok": true}`
  (DeepFilterNet denoise+dereverb, 48 kHz enhance → 16 kHz save).

Reference servers ship in `scripts/` — `audio_server.py` (real) and
`stub_audio_server.py` (no-PyTorch stand-in for testing). See
[docs/runbook.md](runbook.md) for the full operational runbook.

If the server is down, transcription still succeeds — enhancement degrades to
the deterministic ffmpeg chain and speaker labels are dropped.

`ptm-transcribe` streams live progress to stderr (ffmpeg/mlx-whisper output plus
short phase lines), with a `still working … (elapsed …)` heartbeat during quiet
phases such as the first-run model download. Only one `ptm-transcribe` runs at a
time: it holds an exclusive `flock` on
`<PTM_STATE_DIR or ~/.local/state/ptm>/transcribe.lock`, and a second invocation
exits fast with code `3`. Output files are written atomically, and Ctrl-C
terminates the child, cleans temp files, and releases the lock.

### 3. Enable the pass

Transcription is decoupled from conversion (ADR-0009) and runs as its own
`ptm-transcribe` command — with or without an existing Markdown file:

```bash
# attach to existing Markdown (discover same-stem audio beside it)
ptm-transcribe deck.md
ptm-transcribe --diarize deck.md

# no Markdown yet — transcribe straight to a transcript file
ptm-transcribe week-2.mp3
ptm-transcribe --audio-file lecture.mp3 deck.md   # explicit audio for deck.md
ptm-transcribe lecture.mp3 --to deck.md           # attach lecture.mp3 to deck.md
```

## Configuration

| Var | Default | Purpose |
| --- | --- | --- |
| `AUDIO_ENABLED` | *(unset = off)* | Master switch — `1`/`true`/`yes`/`on` |
| `AUDIO_MODEL` | `mlx-community/whisper-large-v3-turbo` | ASR model id (override to `…-large-v3-mlx` for max quality) |
| `AUDIO_MLX_WHISPER_BIN` | `mlx_whisper` | mlx-whisper CLI |
| `AUDIO_FFMPEG_BIN` | `ffmpeg` | ffmpeg binary |
| `AUDIO_LANGUAGE` | *(unset = auto-detect)* | Whisper language hint (e.g. `no`, `en`) |
| `AUDIO_HEARTBEAT_SECONDS` | `20` | Quiet-interval before a `still working …` heartbeat line |
| `AUDIO_PREPROCESS` | `1` | Deterministic ffmpeg enhancement chain (hum/hiss/noise/level) |
| `AUDIO_ENHANCE_ENABLED` | `1` | DeepFilterNet denoise+dereverb via the audio server |
| `AUDIO_ENHANCE_BASE_URL` | `AUDIO_DIARIZE_BASE_URL` | Enhancement endpoint |
| `AUDIO_DIARIZE_ENABLED` | *(unset = off)* | Enable speaker labelling via the diarization server |
| `AUDIO_DIARIZE_BASE_URL` | `http://127.0.0.1:8083/v1` | Audio server base URL |
| `AUDIO_DIARIZE_API_KEY` | *(unset)* | Optional bearer token |

## How the audio is found

1. An **explicit** `--to MARKDOWN.md` pairs an audio file to a specific lecture.
2. An **explicit** `--audio-file PATH` wins (paired by stem, or the sole target).
3. Otherwise, by **convention**: a file in the same folder as the Markdown with the
   same stem and a known audio extension — `deck.md` pairs with `deck.mp3`
   (`.m4a`, `.wav`, `.flac`, `.ogg`, `.aac`, `.m4b`, `.mp4`, `.mov`, `.webm`,
   `.aiff`).
4. If the pairing is still ambiguous, `ptm-transcribe` prompts to pick the
   lecture (`[0]` = standalone).
5. No audio found for a `.md` → `[WARN]`; an audio file with no Markdown →
   standalone `<stem>.transcript.md`.

## Output

Given `deck.md` + `deck.mp3`:

```text
deck.md                    # slides + appended "# Transcript" section
deck.transcript.srt        # SubRip sidecar (timestamps + speaker cues)
deck.clean.flac            # persisted cleaned audio (denoised/dereverbed, 16 kHz)
```

Transcript Markdown (speaker omitted when diarization is off):

```markdown
# Transcript

<details>
<summary>Auto-generated transcript (whisper-large-v3-turbo)</summary>

[00:00:04] **Speaker A:** Welcome to today's lecture.
[00:00:21] **Speaker A:** We'll cover three topics.

</details>
```

The cleaned `.clean.flac` is the exact audio Whisper transcribed, so its
timestamps match the transcript. The source recording is never modified.

Every segment is also recorded to `ptm.sqlite` (`transcript_segments` table) so
the transcript is searchable and inspectable:

```sql
SELECT start, end, speaker, text
FROM transcript_segments WHERE source = ? ORDER BY start;
```

## A/B checking enhancement

Enhancement is on by default but can occasionally hurt already-clean audio. To
compare, run the same file twice and diff the transcripts:

```bash
ptm-transcribe deck.md                                         # enhancement on
AUDIO_PREPROCESS=0 AUDIO_ENHANCE_ENABLED=0 ptm-transcribe deck.md   # raw audio
```

## Limitations

- **Segment-level timestamps only** — word-level precision needs wav2vec2 forced
  alignment, deferred to v2.
- **No per-slide alignment yet** — the transcript is timestamped, not placed
  under matching slides (ADR-0007).
- Whisper can hallucinate on long silence/music; keep recordings clean, and
  consider `AUDIO_LANGUAGE` to avoid mis-detection on short clips.
- DeepFilterNet runs at 48 kHz and is resampled to 16 kHz for Whisper — a slight
  quality loss vs. transcribing at 48 kHz, traded for a smaller artifact.
