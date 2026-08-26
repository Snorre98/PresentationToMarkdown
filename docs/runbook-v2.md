# Audio Pipeline Testing Runbook (v2)

Test each stage of the audio pipeline in isolation — from the most fully-featured
run down to the simplest one. This is the *testing* companion to
[`runbook.md`](runbook.md), which covers the operational setup (install, server
lifecycle, Hugging Face auth, troubleshooting). Use this when you want to isolate
which stage (dereverb, enhance, isolate, diarize, ASR) changes the transcript.

**Pipeline:** `ffmpeg clean → WPE dereverb → DeepFilterNet enhance → [SepFormer isolate] → mlx-whisper` (+ optional `--diarize`).

---

## 1. Prerequisites

- `ffmpeg` and `mlx_whisper` on `PATH` (see [runbook §1](runbook.md#1-prerequisites-one-time)).
- The audio server, with the **two new dependencies installed**:

  ```bash
  scripts/audio_serve.sh install    # re-run: now also installs nara_wpe + speechbrain
  scripts/audio_serve.sh status     # running on :8083?
  scripts/audio_serve.sh start      # if not
  ```

  > ⚠️ **Unverified pin:** `requirements-audio.txt` now pins `speechbrain==1.0.2`
  > and `nara_wpe==0.0.11`. The isolated `~/tools/audio-env` was not built against
  > `torch==2.5.1`/`huggingface-hub==0.36.2` yet — run `install` **before** relying
  > on `--isolate`. If `speechbrain 1.0.2` conflicts, try another `1.x` pin.

- The `ptm-transcribe` entry point. Every command below uses its **full path**, so
  they copy-paste cleanly into any terminal (no shell variable to set first):

  ```bash
  /Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh
  ```

  This wrapper resolves `.venv/bin/ptm-transcribe` (and bootstraps it with
  `pip install -e .` if missing), so it works from any folder with no venv
  activation. All commands use `week-2.mp3` as the example and assume you run
  them from the folder that contains it.

---

## 2. Pipeline map

| # | Stage | Where | Switch | Default | CLI |
| --- | --- | --- | --- | --- | --- |
| 1 | ffmpeg clean (transcode + filter chain) | local `ffmpeg` | `AUDIO_PREPROCESS` | `1` | — |
| 2 | WPE dereverb | server `/v1/dereverb` | `AUDIO_DEREVERB_ENABLED` | `1` | — |
| 3 | DeepFilterNet enhance | server `/v1/enhance` | `AUDIO_ENHANCE_ENABLED` | `1` | — |
| 4 | SepFormer voice isolate | server `/v1/isolate` | `AUDIO_ISOLATE_ENABLED` | off | `--isolate` |
| 5 | ASR (mlx-whisper) | local subprocess | (always) | — | `--language` |
| 6 | pyannote diarize | server `/v1/diarize` | `AUDIO_DIARIZE_ENABLED` | off | `--diarize` |

Every server stage degrades to a `[WARN]` (never fails the run) when the server is
down or the model is missing.

**Artifacts** are append-only and share one version number `N` per run:

```
week-2.transcript.<N>.md  +  week-2.transcript.<N>.srt   ← what Whisper produced
week-2.clean.<N>.flac                                      ← after stages 1–3 (always written)
week-2.isolated.<N>.flac                                   ← after stage 4 (only with --isolate; then this is what Whisper transcribes)
```

The base run uses no suffix (`week-2.transcript.md`); subsequent runs get `.1`, `.2`, ….
`--overwrite` forces the base (un-numbered) name.

---

## 3. The ladder (server running)

Each level is one command. Run them in order and you'll get a stack of versioned
transcripts you can diff (see §6).

### L0 — fully fledged (all six stages)

```bash
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh --diarize --isolate week-2.mp3
```

- Active: ffmpeg → dereverb → enhance → isolate → ASR → diarize.
- Expect: `week-2.transcript.md` with `**SPEAKER_XX:**` labels; **both**
  `week-2.clean.flac` and `week-2.isolated.flac` (Whisper transcribes the isolated one).
- Add `--language no` if auto-detection misbehaves.

### L1 — everything except speaker labels

```bash
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh --isolate week-2.mp3
```

- Same as L0 minus diarize. Verifies isolation doesn't depend on diarization.

### L2 — default (dereverb + enhance)

```bash
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh week-2.mp3
```

- Active: ffmpeg → dereverb → enhance → ASR. This is the normal run.
- Expect: `week-2.transcript.md` + `week-2.clean.flac`; **no** `week-2.isolated.*`.
- Verify: `ffprobe` `clean.flac` duration ≈ source (~40 min), not 3× (see §6).

### L3 — no dereverberation (enhance only)

```bash
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh --env AUDIO_DEREVERB_ENABLED=0 week-2.mp3
```

- Skips WPE; DeepFilterNet still runs. Useful on already-dry audio.

### L4 — no enhancement (dereverb only)

```bash
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh --env AUDIO_ENHANCE_ENABLED=0 week-2.mp3
```

- Skips DeepFilterNet; WPE still runs. Isolates the dereverb contribution.

### L5 — no server processing (ffmpeg filter chain only)

```bash
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh --env AUDIO_DEREVERB_ENABLED=0 AUDIO_ENHANCE_ENABLED=0 week-2.mp3
```

- Only the deterministic ffmpeg chain + ASR. No server calls at all (no `[WARN]`
  from dereverb/enhance even though the server is up).

### L6 — raw transcode (no filters at all)

```bash
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh --env AUDIO_PREPROCESS=0 AUDIO_DEREVERB_ENABLED=0 AUDIO_ENHANCE_ENABLED=0 week-2.mp3
```

- ffmpeg only transcodes to 16 kHz mono, then ASR. The closest you get to "raw
  Whisper on the original file" while still going through the pipeline.

---

## 4. Server stopped (the degradation floor)

The simplest way to run the pipeline is with **no server and no flags** — every
server stage degrades to a warning and you still get a transcript:

```bash
scripts/audio_serve.sh stop
/Users/snorresaether/Documents/Liv/Projects/PresentationToMarkdown/scripts/ptm-transcribe.sh week-2.mp3
```

Expected output:

```
[WARN] week-2.mp3: Audio dereverberation failed: …; using reverberant audio
[WARN] week-2.mp3: Audio enhancement failed: …; using preprocessed audio
[OK]  week-2.mp3 -> week-2.transcript.md
Done: 1 of 1 transcribed.
```

(If the server is *down*, `--diarize`/`--isolate` also warn and are skipped.)

> L5 produces the *same* clean audio deliberately, but keeps the server up so you
> don't have to stop it. L6 goes one step further and drops the ffmpeg filter chain.

---

## 5. Server endpoint smoke tests (curl)

Exercise each endpoint directly, without going through `ptm-transcribe`. This is
the fastest way to pin down *which* server stage is broken.

```bash
# 1. a 2-second probe tone
ffmpeg -f lavfi -i "sine=frequency=440:duration=2" -ar 16000 -ac 1 /tmp/ptm-probe.wav

# 2. each endpoint (diarize needs the gated pyannote model -> HF_TOKEN)
curl -s http://127.0.0.1:8083/v1/dereverb -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/ptm-probe.wav","output":"/tmp/ptm-probe.dereverb.flac"}'
curl -s http://127.0.0.1:8083/v1/enhance -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/ptm-probe.wav","output":"/tmp/ptm-probe.enhanced.flac"}'
curl -s http://127.0.0.1:8083/v1/isolate -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/ptm-probe.wav","output":"/tmp/ptm-probe.isolated.flac"}'
curl -s http://127.0.0.1:8083/v1/diarize -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/ptm-probe.wav","min_speakers":1,"max_speakers":2}'

# 3. each should return {"ok": true} (or a speaker-turn list), and the written
#    file's duration must stay ~2s (a 3x duration means a resample bug):
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/ptm-probe.dereverb.flac
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/ptm-probe.isolated.flac
```

No models installed yet? `scripts/audio_serve.sh stub-start` serves the same four
endpoints (diarize fabricates turns; enhance/dereverb/isolate copy the input), so
you can verify the *wiring* end-to-end with no PyTorch/Hugging Face.

---

## 6. Comparing runs

Because outputs are append-only, run L0, L2, L5 in sequence and compare:

```bash
ls week-2.*                                     # which artifacts each level produced

diff week-2.transcript.md week-2.transcript.1.md   # e.g. L2 vs L0

# every clean/isolated file must be the source's duration (~40 min), never 3x:
for f in week-2.clean*.flac week-2.isolated*.flac; do
  printf '%s  ' "$f"; ffprobe -v error -show_entries format=duration -of csv=p=0 "$f"
done

afplay week-2.isolated.flac                     # A/B isolation by ear vs week-2.clean.flac

sqlite3 ptm.sqlite \
  "SELECT source,start,speaker,substr(text,1,60) FROM transcript_segments ORDER BY source,start;"
```

---

## 7. Gotchas

- **`AUDIO_DEREVERB_ENABLED` is on by default.** Every `ptm-transcribe` run now
  calls `/v1/dereverb` when the server is up — and warns when it isn't. To test
  "no server" cleanly you must *explicitly* disable it (L5/L6), unlike earlier
  versions where dereverb didn't exist.
- **`AUDIO_PREPROCESS` ≠ `AUDIO_ENHANCE_ENABLED`.** The former is the local
  ffmpeg filter chain (always-on deterministic), the latter is the DeepFilterNet
  *server* pass. L6 disables the former; L4 disables the latter.
- **Isolation is best-effort.** The "voice" is picked as the higher-RMS SepFormer
  stream — a dominant second speaker or loud music can win. Treat `.isolated.flac`
  as a candidate, not ground truth.
- **First use downloads weights.** `--isolate` (SepFormer, ~100–200 MB, ungated) and
  `--diarize` (pyannote, gated — see runbook §2.1) both download on their first
  request; the run is slow once, fast afterwards.
- **One instance at a time.** A second `ptm-transcribe` while one is running exits
  `3` with `another instance is already running (PID …)`.
