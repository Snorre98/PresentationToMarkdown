#!/usr/bin/env bash
# ptm-transcribe.sh — run the `ptm-transcribe` CLI from the repo's venv without activating it.
#
# Thin wrapper over scripts/ptm-start.sh: it pins PTM_CMD to `ptm-transcribe` and
# reuses the same bootstrap (creates .venv, installs requirements.txt, then
# `pip install -e .` when the binary is missing), so you can run e.g.:
#
#   scripts/ptm-transcribe.sh week-2.mp3
#   scripts/ptm-transcribe.sh --to deck.md week-2.mp3
#   scripts/ptm-transcribe.sh --diarize deck.md
#
# Env overrides: PTM_VENV (venv path; default: <repo>/.venv).
#
# No `set -e`/`set -u`: stock macOS bash 3.2 footguns (see audio_serve.sh).
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PTM_CMD=ptm-transcribe exec "$ROOT/scripts/ptm-start.sh" "$@"
