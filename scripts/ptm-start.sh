#!/usr/bin/env bash
# ptm-start.sh — run the `ptm-start` CLI from the repo's venv without activating it.
#
# `pip install -e .` puts `ptm-start` (and `ptm`) inside `.venv/bin/`, which is
# only on PATH while the venv is activated. This wrapper resolves that binary and
# execs it with all arguments, so you can run e.g.:
#
#   scripts/ptm-start.sh --audio --diarize
#   scripts/ptm-start.sh --vision --summary
#
# If the venv binary is missing, it bootstraps it: creates `.venv` if needed,
# installs `requirements.txt`, then `pip install -e .`.
#
# Env overrides:
#   PTM_VENV   venv path (default: <repo>/.venv)
#   PTM_CMD    which CLI to run (default: ptm-start; also accepts ptm)
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

CMD="${PTM_CMD:-ptm-start}"
case "$CMD" in
  ptm|ptm-start) ;;
  *) die "unknown PTM_CMD: $CMD (ptm|ptm-start)" ;;
esac

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
  log "installing package (editable) — adds ptm/ptm-start to $VENV/bin"
  "$py" -m pip install -e "$ROOT" || die "pip install -e . failed"
}

bin="$VENV/bin/$CMD"
if [ ! -x "$bin" ]; then
  warn "$CMD not found in $VENV/bin — bootstrapping (pip install -e .)"
  _bootstrap
fi
[ -x "$bin" ] || die "$CMD still missing after bootstrap: $bin"

exec "$bin" "$@"
