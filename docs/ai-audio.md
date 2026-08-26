# AI audio pass (optional)

A local **audio-to-text transcription** post-pass that turns a lecture recording
into a timestamped, speaker-labelled transcript and attaches it to the Markdown
converted from a PDF. It is strictly a *companion*: the deterministic text
extraction always runs first, and the transcript is appended as a `# Transcript`
section (plus a `.srt` sidecar).

> Transcription runs **locally** — no audio leaves the machine. This document
> covers how to serve the models and enable the pass. See the
> [ADR index](adr/README.md) (0004–0007) for the rationale.

## Models and runtime

| Role | Model | Runtime | Notes |
| --- | --- | --- | --- |
| ASR (default) | `mlx-community/whisper-large-v3-turbo` | `mlx-whisper` (MLX) | 809M params, ~4–5× realtime on Apple Silicon |
| ASR (max quality) | `mlx-community/whisper-large-v3-mlx` | `mlx-whisper` (MLX) | 1.55B params, ~1× realtime |
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

### 2. (Optional) Serve the diarizer

Speaker labels need a PyTorch service (its own venv, on `:8083`), because
`pyannote-audio` is gated and PyTorch-based and is deliberately kept out of
`converter` (ADR-0006):

```bash
# one-time setup: accept the gated model terms, then
#   https://huggingface.co/settings/tokens  -> a read token
# (see macos-dev-config for a reusable serve.sh wrapper)
python -m pip install pyannote.audio torch torchaudio
```

The service exposes a single endpoint, `POST /v1/diarize`, accepting
`{"path": "<audio>", "min_speakers": n, "max_speakers": n}` and returning
`[{"start": f, "end": f, "speaker": "SPEAKER_00"}, …]`.

Reference servers ship in `scripts/` — `diarize_server.py` (real pyannote) and
`stub_diarize_server.py` (no-PyTorch stand-in for testing). See
[docs/runbook.md](runbook.md) for the full operational runbook.

If the server is down, transcription still succeeds — just without speaker
labels.

### 3. Enable the pass

```bash
# GUI
ptm-start --audio               # transcript only
ptm-start --audio --diarize     # transcript + speaker labels

# headless
ptm --audio deck.pdf
ptm --audio --diarize --audio-file lecture.mp3 deck.pdf
```

## Configuration

| Var | Default | Purpose |
| --- | --- | --- |
| `AUDIO_ENABLED` | *(unset = off)* | Master switch — `1`/`true`/`yes`/`on` |
| `AUDIO_MODEL` | `mlx-community/whisper-large-v3-turbo` | ASR model id (override to `…-large-v3-mlx` for max quality) |
| `AUDIO_MLX_WHISPER_BIN` | `mlx_whisper` | mlx-whisper CLI |
| `AUDIO_FFMPEG_BIN` | `ffmpeg` | ffmpeg binary |
| `AUDIO_LANGUAGE` | *(unset = auto-detect)* | Whisper language hint (e.g. `no`, `en`) |
| `AUDIO_DIARIZE_ENABLED` | *(unset = off)* | Enable speaker labelling via the diarization server |
| `AUDIO_DIARIZE_BASE_URL` | `http://127.0.0.1:8083/v1` | Diarization service base URL |
| `AUDIO_DIARIZE_API_KEY` | *(unset)* | Optional bearer token |

## How the audio is found

1. An **explicit** `--audio-file PATH` (CLI) or `audio_path` argument wins.
2. Otherwise, by **convention**: a file in the same folder as the source with the
   same stem and a known audio extension — `lecture.pdf` pairs with
   `lecture.mp3` (`.m4a`, `.wav`, `.flac`, `.ogg`, `.aac`, `.m4b`, `.mp4`,
   `.mov`, `.webm`, `.aiff`).
3. No audio found → the pass is a no-op (no warning; the feature is opportunistic).

## Output

Given `deck.pdf` + `deck.mp3`:

```text
deck.md                    # slides + appended "# Transcript" section
deck.transcript.srt        # SubRip sidecar (timestamps + speaker cues)
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

Every segment is also recorded to `ptm.sqlite` (`transcript_segments` table) so
the transcript is searchable and inspectable:

```sql
SELECT start, end, speaker, text
FROM transcript_segments WHERE source = ? ORDER BY start;
```

## Limitations

- **Segment-level timestamps only** — word-level precision needs wav2vec2 forced
  alignment, deferred to v2.
- **No per-slide alignment yet** — the transcript is timestamped, not placed
  under matching slides (ADR-0007).
- Whisper can hallucinate on long silence/music; keep recordings clean, and
  consider `AUDIO_LANGUAGE` to avoid mis-detection on short clips.
