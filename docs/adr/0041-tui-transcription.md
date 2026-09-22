# 0041. Audio transcription in the native Go TUI

- Status: Accepted
- Date: 2026-09-22
- Relates to: [0009](0009-decouple-transcription-from-conversion.md), [0036](0036-terminal-tui.md), [0040](0040-native-go-tui.md)

## Context

ADR-0040's MVP excluded audio transcription and named it as follow-on work
("spawn `ptm-transcribe`"). ADR-0036 §6 (superseded, but its product goals were
kept by ADR-0040) specified the mechanism: the TUI spawns `ptm-transcribe` per
batch and streams its progress lines into the log pane, preserving the
single-instance `flock` (`lock.py`), the atomic artifact naming, and the
`[WARN]`-degradation floor without re-implementing the pipeline.

Two gaps block this in the as-built TUI:

1. **Discovery.** The TUI's file list comes entirely from the engine's
   `/api/fs/glob`, which yields conversion inputs via `collect_inputs` (`.pptx`/
   `.pdf`/`.tex`/LaTeX projects) — audio files are invisible. ADR-0040 forbids
   re-implementing discovery in Go, so audio must surface through the engine.
2. **Dispatch.** ADR-0036 wanted Enter to dispatch *by extension* (convertibles
   convert, audio transcribes). The current picker knows only one kind.

## Decision

Add transcription to `ptm-tui` as a spawned `ptm-transcribe` subprocess, with
audio discovery delegated to a new engine endpoint.

### 1. Engine — audio discovery

`GET /api/fs/glob` gains an optional `kinds=audio` query param. Default
(`convert`, the only value until now) is unchanged, so the web dashboard's
"Add Folder" proxy keeps listing only conversion inputs. `kinds=audio` returns
`{path, files}` via a new `_fs_glob_audio`, mirroring `_fs_glob` but matching
`converter.transcribe.AUDIO_EXTENSIONS` (imported lazily inside the handler).
The TUI calls the endpoint twice on launch and tags each file `convert` or
`audio`.

### 2. TUI — spawn `ptm-transcribe`

- `scripts/ptm-tui.sh` exports `PTM_TRANSCRIBE_CMD="$VENV/bin/ptm-transcribe"`
  (same editable install as `ptm-engine`, so the existing bootstrap guarantee
  covers it). The Go binary reads it via `TranscribeBin()`, defaulting to
  `ptm-transcribe` on PATH for venv-less manual builds.
- Selection is partitioned by kind. Enter dispatches **both, sequentially** in
  one run screen: convertibles stream through the engine's `/ws` job first, then
  the selected audio runs as a single `ptm-transcribe` invocation whose
  stdout/stderr lines stream into the log pane; the phase title flips
  `converting` → `transcribing`.
- The child's stdin is the null device, so `ptm-transcribe`'s interactive
  lecture picker (`sys.stdin.isatty()`) degrades to a standalone transcript
  instead of blocking the TUI.
- For each selected audio with a sibling `<stem>.md`, that Markdown is passed
  too, so `ptm-transcribe` attaches the transcript (`deck.md` + `deck.mp3` →
  `# Transcript` section) rather than emitting a standalone file.

### 3. Scope

MVP is plain transcription: no `--diarize`/`--isolate`/`--language`. Those are
import-time env vars (ADR-0002), not runtime `config` toggles, so they do not
fit the engine-backed settings screen without a restart; they remain available
from the `ptm-transcribe` CLI. Transcript history is untouched (ADR-0040's
other follow-on).

## Consequences

- The TUI subsumes the transcription workflow: select `♫`-marked audio and press
  Enter; the decoupled pipeline (ADR-0009), the `transcribe.lock` single-flight
  rule, and the atomic `<stem>.transcript.<N>.md` / `.clean.<N>.flac` naming are
  preserved because `ptm-transcribe` still runs.
- Progress is line-streamed (coarser than the conversion stage line, exactly as
  ADR-0036 predicted); heartbeats from the subprocess keep long runs alive.
- The engine's glob contract is extended without breaking the web surface: the
  default response is byte-identical to before.
- Go gains no transcription logic beyond argv construction and subprocess
  plumbing; everything else stays in Python.

## Alternatives considered

- **Engine-hosted transcription** (a `/ws` transcribe message reusing the job
  machinery) — would mix transcription into the engine's single-job model and
  bypass `lock.py`'s flock, allowing concurrent `mlx_whisper` across surfaces.
  Rejected: the flock is per-invocation inside `ptm-transcribe`.
- **Audio in the default `/api/fs/glob` response** — one round trip, but the web
  "Add Folder" would suddenly list audio as convertible inputs. Rejected: the
  `kinds` param keeps the default contract stable.
- **Go-side audio discovery** (recursive walk in the TUI) — re-implements
  discovery, which ADR-0040 explicitly ruled out. Rejected.
- **Per-file `ptm-transcribe` invocations** — cleaner per-file `curFile`
  tracking, but re-loads the model and re-acquires the lock per file. Rejected:
  one batch invocation shares the model load and the lock.