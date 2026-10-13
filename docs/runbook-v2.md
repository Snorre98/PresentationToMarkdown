# Audio Pipeline Testing Runbook (v2)

Test each stage of the audio pipeline in isolation — from the most fully-featured
run down to the simplest one. This is the *testing* companion to
[`runbook.md`](runbook.md), which covers the operational setup (install, server
lifecycle, Hugging Face auth, troubleshooting). Use this when you want to isolate
which stage (dereverb, enhance, isolate, diarize, ASR) changes the transcript.

Transcription is an engine-hosted job (ADR-0045): each level below launches the
TUI with the stage switches set in the environment (the engine inherits them),
then you select `week-2.mp3` in the picker and press Enter. Diarization and the
speaker count are *per-job* settings — the TUI settings screen sends them with
each request — so toggle those there rather than via the environment. Standalone
transcripts are append-only, so the levels stack up as diffable versions.

**Pipeline:** `ffmpeg clean → WPE dereverb → DeepFilterNet enhance → [SepFormer isolate] → mlx-whisper` (+ optional diarization).

---

## 1. Prerequisites

- `ffmpeg` and `mlx_whisper` on `PATH` (see [runbook §1](runbook.md#1-prerequisites-one-time)).
- The audio server, with the **two new dependencies installed** (it is the
  manifest's `audio` daemon on `:8089`, managed by the control daemon, ADR-0035):

  ```bash
  scripts/audio_serve.sh install    # re-run: now also installs nara_wpe + speechbrain
  scripts/audio_serve.sh status     # daemon state — running on :8089?
  scripts/audio_serve.sh start      # if not (blocks on the daemon's 60s health bound)
  ```

  > ⚠️ **Unverified pin:** `requirements-audio.txt` now pins `speechbrain==1.0.2`
  > and `nara_wpe==0.0.11`. The isolated `~/tools/audio-env` was not built against
  > `torch==2.5.1`/`huggingface-hub==0.36.2` yet — run `install` **before** relying
  > on isolation. If `speechbrain 1.0.2` conflicts, try another `1.x` pin.

- The TUI + engine: `scripts/ptm-tui.sh` builds/launches both. Every level below
  runs from the folder that contains `week-2.mp3`; after the TUI opens, type `@`,
  filter to the file, and press Enter.

---

## 2. Pipeline map

| # | Stage | Where | Switch | Default | Set via |
| --- | --- | --- | --- | --- | --- |
| 1 | ffmpeg clean (transcode + filter chain) | local `ffmpeg` | `AUDIO_PREPROCESS` | `1` | env at launch |
| 2 | WPE dereverb | server `/v1/dereverb` | `AUDIO_DEREVERB_ENABLED` | `1` | env at launch |
| 3 | DeepFilterNet enhance | server `/v1/enhance` | `AUDIO_ENHANCE_ENABLED` | `1` | env at launch |
| 4 | SepFormer voice isolate | server `/v1/isolate` | `AUDIO_ISOLATE_ENABLED` | off | env at launch |
| 5 | ASR (mlx-whisper / nb-whisper) | local subprocess / server | `AUDIO_MODEL`, `AUDIO_LANGUAGE` | nb-whisper, `no` | env or TUI settings |
| 6 | pyannote diarize | server `/v1/diarize` | `AUDIO_DIARIZE_ENABLED`, `AUDIO_DIARIZE_SPEAKERS` | off (TUI default: on) | TUI settings |

Every server stage degrades to a warning (never fails the run) when the server is
down or the model is missing.

**Artifacts** are append-only and share one version number `N` per run:

```
week-2.transcript.<N>.md  +  week-2.transcript.<N>.srt   ← what Whisper produced
week-2.clean.<N>.flac                                      ← after stages 1–3 (always written)
week-2.isolated.<N>.flac                                   ← after stage 4 (only with isolation; then this is what Whisper transcribes)
```

The base run uses no suffix (`week-2.transcript.md`); subsequent runs get `.1`, `.2`, ….

---

## 3. The ladder (server running)

Each level is one launch + one Enter. Run them in order and you'll get a stack of
versioned transcripts you can diff (see §6).

### L0 — fully fledged (all six stages)

```bash
AUDIO_ISOLATE_ENABLED=1 scripts/ptm-tui.sh
```

with **diarize on** (and e.g. speakers `2`) in the TUI settings screen.

- Active: ffmpeg → dereverb → enhance → isolate → ASR → diarize.
- Expect: `week-2.transcript.md` with `**SPEAKER_XX:**` labels; **both**
  `week-2.clean.flac` and `week-2.isolated.flac` (Whisper transcribes the isolated one).

### L1 — everything except speaker labels

```bash
AUDIO_ISOLATE_ENABLED=1 scripts/ptm-tui.sh
```

with **diarize off** in the TUI settings.

- Same as L0 minus diarization. Verifies isolation doesn't depend on diarization.

### L2 — default (dereverb + enhance)

```bash
scripts/ptm-tui.sh
```

- Active: ffmpeg → dereverb → enhance → ASR (plus diarization if it is on in the
  settings screen — the TUI default is on).
- Expect: `week-2.transcript.md` + `week-2.clean.flac`; **no** `week-2.isolated.*`.
- Verify: `ffprobe` `clean.flac` duration ≈ source (~40 min), not 3× (see §6).

### L3 — no dereverberation (enhance only)

```bash
AUDIO_DEREVERB_ENABLED=0 scripts/ptm-tui.sh
```

- Skips WPE; DeepFilterNet still runs. Useful on already-dry audio.

### L4 — no enhancement (dereverb only)

```bash
AUDIO_ENHANCE_ENABLED=0 scripts/ptm-tui.sh
```

- Skips DeepFilterNet; WPE still runs. Isolates the dereverb contribution.

### L5 — no server processing (ffmpeg filter chain only)

```bash
AUDIO_DEREVERB_ENABLED=0 AUDIO_ENHANCE_ENABLED=0 scripts/ptm-tui.sh
```

- Only the deterministic ffmpeg chain + ASR. No server calls at all (no warning
  from dereverb/enhance even though the server is up).

### L6 — raw transcode (no filters at all)

```bash
AUDIO_PREPROCESS=0 AUDIO_DEREVERB_ENABLED=0 AUDIO_ENHANCE_ENABLED=0 scripts/ptm-tui.sh
```

- ffmpeg only transcodes to 16 kHz mono, then ASR. The closest you get to "raw
  Whisper on the original file" while still going through the pipeline.

---

## 4. Server stopped (the degradation floor)

The simplest way to run the pipeline is with **no server** — every server stage
degrades to a warning and you still get a transcript:

```bash
scripts/audio_serve.sh stop
scripts/ptm-tui.sh                 # then select week-2.mp3
```

Expected run-pane output (warnings are per file; the run ends `1 of 1`):

```
week-2.mp3: Audio dereverberation failed: …; using reverberant audio
week-2.mp3: Audio enhancement failed: …; using preprocessed audio
```

(If the server is *down*, diarization/isolation also warn and are skipped.)

> L5 produces the *same* clean audio deliberately, but keeps the server up so you
> don't have to stop it. L6 goes one step further and drops the ffmpeg filter chain.

---

## 5. Server endpoint smoke tests (curl)

Exercise each endpoint directly, without going through the engine. This is the
fastest way to pin down *which* server stage is broken.

```bash
# 1. a 2-second probe tone
ffmpeg -f lavfi -i "sine=frequency=440:duration=2" -ar 16000 -ac 1 /tmp/ptm-probe.wav

# 2. each endpoint (diarize needs the gated pyannote model -> HF_TOKEN)
curl -s http://127.0.0.1:8089/v1/dereverb -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/ptm-probe.wav","output":"/tmp/ptm-probe.dereverb.flac"}'
curl -s http://127.0.0.1:8089/v1/enhance -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/ptm-probe.wav","output":"/tmp/ptm-probe.enhanced.flac"}'
curl -s http://127.0.0.1:8089/v1/isolate -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/ptm-probe.wav","output":"/tmp/ptm-probe.isolated.flac"}'
curl -s http://127.0.0.1:8089/v1/diarize -H 'Content-Type: application/json' \
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

- **`AUDIO_DEREVERB_ENABLED` is on by default.** Every transcription run now
  calls `/v1/dereverb` when the server is up — and warns when it isn't. To test
  "no server" cleanly you must *explicitly* disable it (L5/L6), unlike earlier
  versions where dereverb didn't exist.
- **`AUDIO_PREPROCESS` ≠ `AUDIO_ENHANCE_ENABLED`.** The former is the local
  ffmpeg filter chain (always-on deterministic), the latter is the DeepFilterNet
  *server* pass. L6 disables the former; L4 disables the latter.
- **Isolation is best-effort.** The "voice" is picked as the higher-RMS SepFormer
  stream — a dominant second speaker or loud music can win. Treat `.isolated.flac`
  as a candidate, not ground truth.
- **First use downloads weights.** Isolation (SepFormer, ~100–200 MB, ungated) and
  diarization (pyannote, gated — see runbook §2.1) both download on their first
  request; the run is slow once, fast afterwards.
- **One instance at a time.** A second transcription job while one is running is
  rejected with “Another transcription is already running.” in the run pane.
