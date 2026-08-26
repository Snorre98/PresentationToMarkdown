# Audio Transcription Runbook

Local audio→text for the Presentation-to-Markdown converter. Transcribes a
lecture recording and attaches a timestamped, speaker-labelled `# Transcript`
to the PDF's Markdown.

**Hardware:** Apple M4, 32 GB · **ASR:** `mlx-whisper` · **Diarization:** pyannote (isolated server)

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

## 2. Serving the diarizer (only for `--diarize`)

Two options, both exposing `POST /v1/diarize` on `:8083`:

```json
// request
{"path": "/abs/path/lecture.mp3", "min_speakers": 1, "max_speakers": 4}
// response
[{"start": 0.0, "end": 9.2, "speaker": "SPEAKER_00"}, ...]
```

**Option A — Stub (for testing, no PyTorch):**

```bash
./.venv/bin/python scripts/stub_diarize_server.py --port 8083
```

Fabricates alternating speaker turns spanning the audio duration (via
`ffprobe`), so the `--diarize` → labels path works end-to-end.

**Option B — Real pyannote (isolated venv):**

```bash
python3.12 -m venv ~/tools/diarize-env
~/tools/diarize-env/bin/pip install pyannote.audio torch torchaudio
# accept the gated terms, then export a read token:
#   https://hf.co/pyannote/speaker-diarization-3.1  +  pyannote/segmentation-3.0
#   https://huggingface.co/settings/tokens
HF_TOKEN=hf_... ~/tools/diarize-env/bin/python scripts/diarize_server.py --port 8083
```

> If no server is running, `--diarize` logs `[WARN] Diarization failed: …` and
> still produces an **unlabelled** transcript. Never fatal.

## 3. Running transcription

```bash
# headless (pip install -e . exposes ptm)
ptm --audio lecture.pdf                        # discover lecture.mp3/m4a/wav beside the PDF
ptm --audio --audio-file lecture.m4a deck.pdf  # explicit pairing
ptm --audio --diarize deck.pdf                 # + speaker labels
ptm --all --audio --diarize deck.pdf           # every slide pass + audio

# GUI
ptm-start --audio --diarize

# raw env (equivalent)
AUDIO_ENABLED=1 AUDIO_DIARIZE_ENABLED=1 ./.venv/bin/python main.py
```

**Audio pairing rules (in order):**

1. Explicit `--audio-file PATH` (paired by stem to an input, or the sole input).
2. Convention: same folder, same stem — `lecture.pdf` + `lecture.mp3`
   (priority `.wav > .m4a > .mp3 > …`).
3. Nothing found → silent no-op (the feature is opportunistic).

## 4. Verify the output

```bash
ls out/                          # deck.md, deck.transcript.srt
tail -40 out/deck.md             # "# Transcript" section at the end
cat out/deck.transcript.srt      # SubRip timestamps + speaker cues

sqlite3 ptm.sqlite \
  "SELECT start,end,speaker,text FROM transcript_segments WHERE source LIKE '%deck.pdf' ORDER BY start;"
```

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

# diarization client vs the stub server (no model/binary needed)
./.venv/bin/python -m pytest tests/test_transcribe_integration.py -v

# full suite
./.venv/bin/python -m pytest -q

# real ffmpeg + mlx-whisper end-to-end (opt-in; downloads the model on first run)
PTM_RUN_AUDIO_INTEGRATION=1 ./.venv/bin/python -m pytest tests/test_transcribe_integration.py -v
```

## 6. Model tuning

| Goal | Setting |
| --- | --- |
| Max quality | `AUDIO_MODEL=mlx-community/whisper-large-v3-mlx` (or `--env AUDIO_MODEL=…`) |
| Norwegian (avoid mis-detect on short clips) | `AUDIO_LANGUAGE=no` |
| Default | `mlx-community/whisper-large-v3-turbo` |

## 7. Troubleshooting

| Symptom | Cause → Fix |
| --- | --- |
| `[WARN] Audio transcription failed: mlx_whisper not found` | `uv tool install mlx-whisper`, or set `AUDIO_MLX_WHISPER_BIN` |
| `[WARN] … ffmpeg not found` | `brew install ffmpeg` |
| `[WARN] Diarization failed: …` | Server down → start it, or drop `--diarize` |
| No `# Transcript` appears | `AUDIO_ENABLED` off, or no same-stem audio file found → pass `--audio-file` |
| Wrong language / gibberish | Set `AUDIO_LANGUAGE`, or upgrade to `large-v3-mlx` |
| Long silence / music transcribed | Whisper hallucination → trim the recording |
