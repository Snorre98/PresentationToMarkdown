# AI audio pass (optional)

A local **audio-to-text transcription** that turns a lecture recording into a
timestamped, speaker-labelled transcript and attaches it to a Markdown file (or
writes a standalone `<stem>.transcript.md` when none exists yet). It runs as an
**engine-hosted job** (ADR-0045), decoupled from conversion (ADR-0009) and
driven from the TUI. It is strictly a *companion*: the deterministic text
extraction always runs first, and the transcript is appended as a
`# Transcript` section (plus a `.srt` sidecar). Along the way the audio is
**cleaned** (denoise + dereverb) and the cleaned audio is **persisted** as a
`.clean.flac`.

> Transcription runs **locally** — no audio leaves the machine. This document
> covers how to serve the models and enable the pass. See the
> [ADR index](adr/README.md) (0004–0009) for the rationale.

## Models and runtime

| Role | Model | Runtime | Notes |
| --- | --- | --- | --- |
| ASR (Norwegian, default) | `NbAiLab/nb-whisper-large` (fleet `nb-whisper-large`, via `/v1/asr`) | audio server (PyTorch) | ~2× fewer Norwegian errors than stock Whisper (6.6%/2.2% vs 10.4%/6.8% WER); Apache-2.0 (ADR-0044) |
| ASR (max quality) | `mlx-community/whisper-large-v3-asr-fp16` (fleet `whisper-large-v3`) | `whisper` daemon (`:8099`, MLX) | 1.55B params; `verbose_json` + word timestamps (ADR-0062) |
| ASR (speed) | `mlx-community/whisper-large-v3-turbo-asr-fp16` (fleet `whisper-large-v3-turbo`) | `whisper` daemon (`:8099`, MLX) | 809M params, near-equal quality; one resident model at a time |
| Enhancement | DeepFilterNet (denoise + dereverb) | PyTorch server (`:8089`, manifest `audio` daemon) | optional, ~8 MB, no gating |
| Diarization | `pyannote/speaker-diarization-3.1` | PyTorch server (`:8089`, manifest `audio` daemon) | optional, gated HF model |

### 1. Prerequisites

`ffmpeg` plus the fleet's `whisper` ASR daemon:

```bash
brew install ffmpeg
# start the ASR daemon through the flaate control plane (not by hand):
curl -s -X POST http://127.0.0.1:9300/start/whisper-large-v3
```

The daemon is part of the machine's fleet manifest (`macos-dev-config/models.json`,
`whisper` on `:8099`) and is installed with the shared `mlx-audio` uv tool — see
`macos-dev-config/SERVING-MANIFEST.md`. Do **not** install `mlx-whisper`: the
subprocess lane is gone (ADR-0062). Models download on first load to `HF_HOME`
(the SSD; see `macos-dev-config/inference-readme.md`) and the launcher keeps at
most one resident; `serve-whisper.sh warm [model]` preloads without inference.

### 2. (Optional) Serve the audio-model server

Speaker labels and deep enhancement run in a single PyTorch service (its own
venv, on `:8089`), because `pyannote-audio` and `deepfilternet` are deliberately
kept out of `converter` (ADR-0006, ADR-0008):

```bash
scripts/audio_serve.sh install       # one-time: ~/tools/audio-env (py3.11) + deps
scripts/audio_serve.sh start         # background; reads HF_TOKEN from env or ./.env
scripts/audio_serve.sh status
scripts/audio_serve.sh stop
scripts/audio_serve.sh stub-start    # no-PyTorch stand-in for testing
```

`install` uses `uv` (falls back to `python3.11 -m venv`/`pip`). The server reads
`AUDIO_ENHANCE_ATTEN_DB` (DeepFilterNet noise-attenuation limit in dB; default
`12.0`, `0` = full suppression) — set it where the server starts, e.g. in `./.env`.
For reboot persistence, `scripts/audio_serve.sh launchd-install` installs a `RunAtLoad` +
`KeepAlive` LaunchAgent. See [docs/runbook.md](runbook.md) for the full
operational runbook.

> Only **diarization** needs Hugging Face; enhancement (DeepFilterNet) does not.
> For the exact Hugging Face steps — accept the licenses on
> `pyannote/speaker-diarization-3.1` **and** `pyannote/segmentation-3.0`, then
> create a **Read** token — see
> [the runbook's "Hugging Face setup" section](runbook.md#21-hugging-face-setup-only-for-speaker-labels).

The service exposes four endpoints:

- `POST /v1/diarize` — `{"path": "<audio>", "min_speakers": n, "max_speakers": n}`
  → `[{"start": f, "end": f, "speaker": "SPEAKER_00"}, …]`.
- `POST /v1/enhance` — `{"path": "<in>", "output": "<out>"}` → `{"ok": true}`
  (DeepFilterNet denoise, 48 kHz enhance → 16 kHz save).
- `POST /v1/dereverb` — `{"path": "<in>", "output": "<out>"}` → `{"ok": true}`
  (WPE dereverberation, pure NumPy, 16 kHz in/out).
- `POST /v1/isolate` — `{"path": "<in>", "output": "<out>"}` → `{"ok": true}`
  (SepFormer voice isolation, 8 kHz in → 16 kHz out).
- `POST /v1/asr` — `{"path": "<audio>", "language": "no", "return_words": bool}`
  → segments (or per-word timestamps); the NB-Whisper route (ADR-0044).

The MLX whisper models are a **separate fleet daemon** (`whisper`, `:8099`),
not part of this server: it exposes the OpenAI-compatible
`POST /v1/audio/transcriptions` (multipart; `response_format=verbose_json`,
`word_timestamps`) and `GET /v1/models` (ADR-0062). `converter.audio.transcribe_fleet()`
hides the two wire shapes.

Reference servers ship in `scripts/` — `audio_server.py` (real),
`stub_audio_server.py` and `stub_whisper_server.py` (no-model stand-ins for
testing). See [docs/runbook.md](runbook.md) for the full operational runbook.

If the audio server is down, preprocessing still succeeds — dereverberation and
enhancement degrade to the deterministic ffmpeg chain, isolation is skipped,
speaker labels are dropped, and NB-Whisper is unavailable. A down **ASR daemon**
fails the job with a warning; there is no mlx-whisper fallback (ADR-0062).

The engine streams live progress over its WebSocket (ffmpeg output plus short
phase frames), with a heartbeat during the local subprocess phases. ASR itself
is one HTTP call to a fleet daemon: progress is **phase-level only** there
(`transcribing with <model> (fleet ASR) …`, then silence until the reply; the
first request after a start also pays the model load). Only one transcription
runs at a time: the engine holds an exclusive `flock` on
`<PTM_STATE_DIR or ~/.local/state/ptm>/transcribe.lock`, and a second job is
rejected while it runs. Output files are written atomically.

### 3. Enable the pass

Transcription is decoupled from conversion (ADR-0009) and runs as an engine
job, driven from the TUI (ADR-0045). Audio files appear in the launch view with
a `♫` marker; select them and press Enter. The engine pairs the recording with
a same-stem Markdown file or writes a standalone transcript.

The options are persisted **settings** (edited on the TUI's settings screen) with
environment overrides applied at engine launch:

```bash
# diarization + a pinned speaker count (e.g. a two-person interview)
AUDIO_DIARIZE_ENABLED=1 AUDIO_DIARIZE_SPEAKERS=2 scripts/ptm-tui.sh

# voice isolation / language hint / ASR model override
AUDIO_ISOLATE_ENABLED=1 AUDIO_LANGUAGE=en scripts/ptm-tui.sh
AUDIO_MODEL=whisper-large-v3-turbo scripts/ptm-tui.sh
```

The audio pipeline is `ffmpeg clean → WPE dereverb → DeepFilterNet enhance →
[SepFormer isolate] → fleet ASR`. WPE dereverberation and enhancement both run
whenever the server is up (default on); voice isolation is opt-in
(`AUDIO_ISOLATE_ENABLED`) and, when enabled, writes a `<stem>.isolated.<N>.flac`
that ASR transcribes instead of the cleaned file.

### Speaker count

Speaker diarization is opt-in and the **number of speakers can be
injected** (TUI settings row or env):

- `AUDIO_DIARIZE_SPEAKERS=N` pins an **exact** count — pyannote is told
  `num_speakers = N`. Use `2` for a two-person interview.
- `AUDIO_DIARIZE_MIN_SPEAKERS` / `AUDIO_DIARIZE_MAX_SPEAKERS` give pyannote a
  range to search.
- All three imply diarization (no separate switch needed); the exact count
  overrides the min/max range.

Consecutive same-speaker ASR segments are merged into continuous utterances in
the Markdown and `.srt` output, so an interview reads as alternating
`**SPEAKER_00:**` / `**SPEAKER_01:**` blocks rather than many fragmented ~5 s
lines. Speaker labels remain generic (`SPEAKER_00`, `SPEAKER_01`, …); there is
no name mapping. Labels are assigned by each segment's midpoint overlapping a
diarized turn, so heavy crosstalk/overlapping speech can still be mislabelled.

### Defaults and TUI control

The default is `nb-whisper-large` — a Norwegian fine-tuned Whisper served by the
audio server (`/v1/asr`, ADR-0044) that roughly halves Norwegian ASR errors.
The MLX whisper models are served by the fleet's `whisper` daemon (`:8099`) over
the OpenAI transcription endpoint with word timestamps (ADR-0062). A failed ASR
call fails the job with a warning; there is no subprocess fallback. Model,
language, diarization, and the speaker count are persisted **settings** the TUI
controls:

- In `ptm-tui`, the settings screen (`s`) shows `model`, `language`, `diarize`
  and `speakers` rows. Model cycles `nb-whisper (no)` → `large-v3 (max)` →
  `turbo (fast)`; language is a freeform edit where `auto` means auto-detect.
  Changes round-trip through the engine's `/api/config` (stored in
  `converter.settings`) and are applied to the next transcription job — no
  restart needed (ADR-0043/0045/0062). Old persisted HF ids are aliased to the
  fleet names on read and write.
- Defaults: model `nb-whisper-large`, language `no`, diarize **on**, speakers
  `2` (interview-first). The `AUDIO_*` environment variables override them at
  engine launch.
- NB-Whisper is Norwegian/English focused — pick `whisper-large-v3` (quality)
  or `whisper-large-v3-turbo` (speed) for other languages. Start the daemon
  first: `curl -X POST http://127.0.0.1:9300/start/whisper-large-v3`.

## Configuration

| Var | Default | Purpose |
| --- | --- | --- |
| `AUDIO_ENABLED` | *(unset = off)* | Master switch — `1`/`true`/`yes`/`on` |
| `AUDIO_MODEL` | `nb-whisper-large` | Fleet ASR model name: `nb-whisper-large` (audio server `/v1/asr`), `whisper-large-v3` (max quality) or `whisper-large-v3-turbo` (speed), the latter two on the fleet `whisper` daemon (ADR-0062). Old HF ids are aliased |
| `AUDIO_FFMPEG_BIN` | `ffmpeg` | ffmpeg binary |
| `AUDIO_LANGUAGE` | `no` | Whisper language hint (set to `auto` or empty for auto-detection) |
| `AUDIO_HEARTBEAT_SECONDS` | `20` | Quiet-interval before a `still working …` heartbeat line (local subprocess phases) |
| `WHISPER_BASE_URL` | manifest `whisper` daemon (`:8099/v1`) | Whisper daemon base URL override |
| `WHISPER_API_KEY` | *(unset)* | Optional bearer token for the whisper daemon |
| `AUDIO_PREPROCESS` | `1` | Gentle ffmpeg chain (DC/rumble removal + band-limit) — denoise/dereverb live in the server |
| `AUDIO_DEREVERB_ENABLED` | `1` | WPE dereverberation via the audio server |
| `AUDIO_ENHANCE_ENABLED` | `1` | DeepFilterNet denoise via the audio server |
| `AUDIO_ISOLATE_ENABLED` | *(unset = off)* | Voice isolation (SepFormer) via the audio server |
| `AUDIO_ENHANCE_BASE_URL` | `AUDIO_DIARIZE_BASE_URL` | Enhancement endpoint |
| `AUDIO_DIARIZE_ENABLED` | *(unset = off)* | Enable speaker labelling via the diarization server |
| `AUDIO_DIARIZE_SPEAKERS` | *(unset = auto)* | Exact speaker count for pyannote; implies diarization, overrides min/max |
| `AUDIO_DIARIZE_MIN_SPEAKERS` | *(unset = auto)* | Lower bound of the speaker-count range; implies diarization |
| `AUDIO_DIARIZE_MAX_SPEAKERS` | *(unset = auto)* | Upper bound of the speaker-count range; implies diarization |
| `AUDIO_DIARIZE_BASE_URL` | `http://127.0.0.1:8089/v1` | Audio server base URL (diarization + NB-Whisper) |
| `AUDIO_DIARIZE_API_KEY` | *(unset)* | Optional bearer token for the audio server |

`AUDIO_MLX_WHISPER_BIN` and `AUDIO_CONDITION_ON_PREVIOUS_TEXT` are gone with the
subprocess lane (ADR-0062). The equivalent prompting knob now lives on the
daemon: `WHISPER_CONDITION_ON_PREVIOUS_TEXT` (default off; see
`macos-dev-config/SERVING-MANIFEST.md`).

## How the audio is found

1. By **convention**: a file in the same folder as the Markdown with the same
   stem and a known audio extension — `deck.md` pairs with `deck.mp3`
   (`.m4a`, `.wav`, `.flac`, `.ogg`, `.aac`, `.m4b`, `.mp4`, `.mov`, `.webm`,
   `.aiff`). Selecting the `.md` attaches its same-stem audio.
2. No audio found for a `.md` → `[WARN]`.
3. An audio file with no Markdown → standalone `<stem>.transcript.md`.

Standalone transcripts are **append-only**: re-running the same audio writes
`<stem>.transcript.1.md`, `.2.md`, … (with a matching `.srt` **and**
`.clean.<N>.flac`) so past transcripts are preserved for A/B comparison.
Attaching to a `.md` is idempotent — the `# Transcript` section is replaced,
not duplicated.

## Output

Given `deck.md` + `deck.mp3`:

```text
deck.md                    # slides + appended "# Transcript" section
deck.transcript.srt        # SubRip sidecar (timestamps + speaker cues)
deck.clean.flac            # persisted cleaned audio (dereverbed + denoised, 16 kHz)
deck.isolated.flac         # voice-isolated stream (only with voice isolation enabled)
```

Transcript Markdown (speaker omitted when diarization is off):

```markdown
# Transcript

<details>
<summary>Auto-generated transcript (whisper-large-v3)</summary>

[00:00:04] **Speaker A:** Welcome to today's lecture.
[00:00:21] **Speaker A:** We'll cover three topics.

</details>
```

The cleaned `.clean.flac` is the exact audio Whisper transcribed, so its
timestamps match the transcript (with isolation enabled, that is
`.isolated.flac` instead). The source recording is never modified.

Every segment is also recorded to `ptm.sqlite` (`transcript_segments` table) so
the transcript is searchable and inspectable:

```sql
SELECT start, end, speaker, text
FROM transcript_segments WHERE source = ? ORDER BY start;
```

## A/B checking enhancement

Enhancement is on by default but can occasionally hurt already-clean audio. To
compare, toggle *Enhancement* / *Dereverberation* in the TUI settings (or set
`AUDIO_PREPROCESS=0 AUDIO_ENHANCE_ENABLED=0` at engine launch) and transcribe
the audio again — standalone transcripts are append-only, so both outputs exist
side by side and can be diffed.

## Limitations

- **Overlapping speech is not separated.** Whisper (MLX or NB-Whisper) transcribes
  a single stream, so when two people talk at once the overlapping text is
  garbled and speaker attribution at those moments is unreliable (midpoint
  assignment, ADR-0042). Dual-stream isolation is a documented, deferred idea;
  for occasional overlap the NB-Whisper + diarization combo is the practical
  mitigation.
- **The rendered transcript is segment/utterance-level.** Per-word timestamps
  are requested from both ASR routes when diarizing (ADR-0045/0062), used to
  assign speakers and split at pauses, but not rendered word by word.
- **Beam re-decode is NB-Whisper only** — the OpenAI transcription form has no
  beam-width field, so whisper models warn and skip the coverage re-decode pass
  (ADR-0062).
- **No per-slide alignment yet** — the transcript is timestamped, not placed
  under matching slides (ADR-0007).
- Whisper can hallucinate on long silence/music; keep recordings clean, and
  consider `AUDIO_LANGUAGE` to avoid mis-detection on short clips.
- DeepFilterNet runs at 48 kHz and is resampled to 16 kHz for Whisper — a slight
  quality loss vs. transcribing at 48 kHz, traded for a smaller artifact.
- WPE dereverberation is single-channel, so its effect on very reverberant mono
  recordings is real but bounded; the multi-channel case improves much more.
- Voice isolation (`AUDIO_ISOLATE_ENABLED`) is **best-effort**: SepFormer splits into two
  unordered streams and we keep the higher-energy one, so a dominant second
  speaker or very similar voices may be picked incorrectly — it degrades to the
  unisolated audio rather than failing.
