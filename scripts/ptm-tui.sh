#!/usr/bin/env bash
# ptm-tui.sh — build and run the native Go TUI against the repo's ptm-engine.
#
# Resolves the repo venv (bootstrap if missing), builds bin/ptm-tui from tui/,
# and execs the binary with the engine command and state location set:
#
#   scripts/ptm-tui.sh
#   scripts/ptm-tui.sh --rebuild
#
# The TUI discovers files in the current directory; conversion runs through the
# ptm-engine sidecar (ADR-0025/0040), which the TUI spawns and stops itself.
#
# Env overrides:
#   PTM_VENV         venv path (default: <repo>/.venv)
#   PTM_ENGINE_PORT  engine port (default: 9091)
#   PTM_STATE_DIR    state dir (default: ~/.local/state/ptm)
#   VISION_LOG_DB    settings/log sqlite (default: $PTM_STATE_DIR/ptm.sqlite)
#   PTM_TRANSCRIBE_CMD  ptm-transcribe binary (default: $VENV/bin/ptm-transcribe)
#
# No `set -e`/`set -u`: stock macOS bash 3.2 footguns (see audio_serve.sh).
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PTM_VENV:-$ROOT/.venv}"

_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { _c '0;36'; printf '• %s\n' "$*"; _c '0'; }
ok()   { _c '0;32'; printf '✓ %s\n' "$*"; _c '0'; }
warn() { _c '0;33'; printf '⚠ %s\n' "$*" >&2; _c '0'; }
die()  { _c '0;31'; printf '✗ %s\n' "$*" >&2; _c '0'; exit 1; }

_venv_python() { printf '%s/bin/python' "$VENV"; }

_bootstrap() {
  local py
  if [ ! -x "$(_venv_python)" ]; then
    log "creating venv: $VENV"
    command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"
    python3 -m venv "$VENV" || die "python3 -m venv failed"
  fi
  py="$(_venv_python)"
  log "installing requirements.txt"
  "$py" -m pip install -r "$ROOT/requirements.txt" || die "pip install -r failed"
  log "installing package (editable) — adds ptm-engine to $VENV/bin"
  "$py" -m pip install -e "$ROOT" || die "pip install -e . failed"
}

# Ensure the ptm-engine console script exists (the TUI spawns it).
if [ ! -x "$VENV/bin/ptm-engine" ]; then
  warn "ptm-engine not found in $VENV/bin — bootstrapping (pip install -e .)"
  _bootstrap
fi
[ -x "$VENV/bin/ptm-engine" ] || die "ptm-engine still missing after bootstrap: $VENV/bin/ptm-engine"

# Build the TUI binary when missing, stale, or asked to.
_build() {
  command -v go >/dev/null 2>&1 || die "go not found on PATH (needed to build ptm-tui)"
  mkdir -p "$ROOT/bin"
  local stale=""
  if [ ! -x "$ROOT/bin/ptm-tui" ]; then
    stale=1
  elif [ -n "$(find "$ROOT/tui" -name '*.go' -newer "$ROOT/bin/ptm-tui" 2>/dev/null)" ]; then
    stale=1
  fi
  if [ -n "$stale" ]; then
    log "building ptm-tui (go build)"
    (cd "$ROOT/tui" && go build -o "$ROOT/bin/ptm-tui" .) || die "go build failed"
  fi
}

case "${1:-}" in
  --rebuild) _build_force=1; shift ;;
esac
if [ -n "${_build_force:-}" ]; then
  log "rebuilding ptm-tui"
  (cd "$ROOT/tui" && go build -o "$ROOT/bin/ptm-tui" .) || die "go build failed"
else
  _build
fi

# Stable state location so settings/logs are global, not per-cwd.
export PTM_STATE_DIR="${PTM_STATE_DIR:-$HOME/.local/state/ptm}"
export VISION_LOG_DB="${VISION_LOG_DB:-$PTM_STATE_DIR/ptm.sqlite}"
export PTM_ENGINE_CMD="$VENV/bin/ptm-engine"
export PTM_TRANSCRIBE_CMD="$VENV/bin/ptm-transcribe"

exec "$ROOT/bin/ptm-tui" "$@"
