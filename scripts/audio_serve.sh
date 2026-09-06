#!/usr/bin/env bash
# audio_serve.sh — lifecycle manager for the audio-model server.
#
# The audio server is a manifest daemon (`audio`, :8089) owned by the
# macos-dev-config control daemon (ADR-0035): start/stop/status/log here proxy
# the daemon's verbs, exactly like the AI passes do (ADR-0029). What remains
# local is the venv bootstrap (`install`) and the no-PyTorch test stub
# (`stub-start`/`stub-stop`/`stub-status`/`stub-log`), which is not a manifest
# service.
#
# Usage:
#   audio_serve.sh install            # create ~/tools/audio-env (py3.11) + install deps
#   audio_serve.sh start              # start the audio daemon via the control daemon
#   audio_serve.sh stop               # stop the audio daemon
#   audio_serve.sh status             # daemon state (up/down/starting/...)
#   audio_serve.sh log                # tail -f the daemon-side server log
#   audio_serve.sh stub-start [--port N]   # start the stub (no PyTorch/HF)
#   audio_serve.sh stub-stop
#   audio_serve.sh stub-status
#   audio_serve.sh stub-log
#
# Env overrides:
#   DAEMON_URL        control daemon base URL (default: http://127.0.0.1:9300)
#   MACOS_DEV_CONFIG_ROOT  macos-dev-config repo (default: sibling dir)
#   AUDIO_VENV        venv path (default: ~/tools/audio-env)
#   PTM_AUDIO_PORT    stub port (default: 8089)
#   PTM_AUDIO_HOST    stub bind address (default: 127.0.0.1)
#   PTM_STATE_DIR     pid/log dir for the stub (default: ~/.local/state/ptm)
#   AUDIO_ENV_FILE    token file (default: <repo>/.env); HF_TOKEN env wins
#
# The HF token is read from HF_TOKEN or a git-ignored .env — never hard-coded,
# never committed. See docs/runbook.md §2.
#
# No `set -e`/`set -u`: stock macOS bash 3.2 footguns (see serve.sh).
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_SERVER="$ROOT/scripts/audio_server.py"
STUB_SERVER="$ROOT/scripts/stub_audio_server.py"
REQUIREMENTS="$ROOT/requirements-audio.txt"

VENV="${AUDIO_VENV:-$HOME/tools/audio-env}"
STATE_DIR="${PTM_STATE_DIR:-$HOME/.local/state/ptm}"
DEFAULT_PORT="${PTM_AUDIO_PORT:-8089}"
HOST="${PTM_AUDIO_HOST:-127.0.0.1}"
ENV_FILE="${AUDIO_ENV_FILE:-$ROOT/.env}"
DAEMON_URL="${DAEMON_URL:-http://127.0.0.1:9300}"
DEV_CONFIG_ROOT="${MACOS_DEV_CONFIG_ROOT:-$(dirname "$ROOT")/macos-dev-config}"
DEV_AUDIO_LOG="$DEV_CONFIG_ROOT/var/serve-audio.log"

# ── Logging (shared serve.sh convention) ───────────────────────────────────────
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { _c '0;36'; printf '• %s\n' "$*"; _c '0'; }
ok()   { _c '0;32'; printf '✓ %s\n' "$*"; _c '0'; }
warn() { _c '0;33'; printf '⚠ %s\n' "$*" >&2; _c '0'; }
die()  { _c '0;31'; printf '✗ %s\n' "$*" >&2; _c '0'; exit 1; }

# ── Helpers ────────────────────────────────────────────────────────────────────
_pidfile() { printf '%s/%s.pid' "$STATE_DIR" "$1"; }    # $1 = audio | stub
_logfile() { printf '%s/%s.log' "$STATE_DIR" "$1"; }
_portfile() { printf '%s/%s.port' "$STATE_DIR" "$1"; }

# The distinguishing token in the server process's command line. Keeps stop/status
# from ever matching an unrelated process that happens to share the port/PID.
_server_marker() {   # $1 = audio | stub
  case "$1" in
    audio) printf '%s' 'scripts/audio_server.py' ;;
    stub)  printf '%s' 'scripts/stub_audio_server.py' ;;
  esac
}

_venv_python() { printf '%s/bin/python' "$VENV"; }

_stub_python() {   # stub needs only the stdlib — any python3 works
  if [ -n "${AUDIO_STUB_PYTHON:-}" ]; then
    printf '%s' "$AUDIO_STUB_PYTHON"
  elif [ -x "$ROOT/.venv/bin/python" ]; then
    printf '%s' "$ROOT/.venv/bin/python"
  else
    printf '%s' "$(command -v python3)"
  fi
}

_port_listening() {   # $1 = port -> 0 if something is LISTENing on it
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

_pid_alive() {   # $1 = pid -> 0 if alive
  [ -n "$1" ] && kill -0 "$1" 2>/dev/null
}

# Return the token: HF_TOKEN env wins, then the .env file, else empty.
_resolve_token() {
  if [ -n "${HF_TOKEN:-}" ]; then
    printf '%s' "$HF_TOKEN"
    return
  fi
  if [ -f "$ENV_FILE" ]; then
    local v
    v="$(sed -n 's/^[[:space:]]*HF_TOKEN[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -n1)"
    [ -n "$v" ] && printf '%s' "$v"
  fi
}

_parse_port() {   # $1.. -> prints the port after consuming --port / -p
  local port="$DEFAULT_PORT" prev=""
  for arg in "$@"; do
    case "$prev" in
      --port|-p) port="$arg"; break ;;
    esac
    prev="$arg"
  done
  printf '%s' "$port"
}

# The port the running server was actually started on (from state), else default.
_actual_port() {   # $1 = kind
  local pfile="$(_portfile "$1")"
  if [ -f "$pfile" ]; then cat "$pfile"; else _parse_port; fi
}

# ── install ────────────────────────────────────────────────────────────────────
_install() {
  local py
  mkdir -p "$(dirname "$VENV")"

  if [ -x "$(_venv_python)" ]; then
    ok "venv already present: $VENV"
  else
    if command -v uv >/dev/null 2>&1; then
      log "creating venv with uv: $VENV (Python 3.11)"
      uv venv "$VENV" --python 3.11 || die "uv venv failed"
    elif command -v python3.11 >/dev/null 2>&1; then
      log "uv not found — falling back to python3.11 -m venv"
      python3.11 -m venv "$VENV" || die "python3.11 -m venv failed"
    else
      warn "no uv and no python3.11 — trying $(command -v python3)"
      python3 -m venv "$VENV" || die "python3 -m venv failed"
    fi
  fi

  py="$(_venv_python)"
  [ -x "$py" ] || die "venv interpreter missing: $py"

  if command -v uv >/dev/null 2>&1; then
    log "installing requirements-audio.txt (uv)"
    uv pip install --python "$py" -r "$REQUIREMENTS" || die "uv pip install failed"
  else
    log "uv not found — falling back to pip"
    "$py" -m pip install -r "$REQUIREMENTS" || die "pip install failed"
  fi
  ok "audio server deps installed into $VENV"
}

# ── start / stop / status / log (shared by real + stub) ────────────────────────
_start() {   # $1 = audio | stub, $2.. = extra args
  local kind="$1" marker server py pidfile logfile port
  shift
  marker="$(_server_marker "$kind")"
  pidfile="$(_pidfile "$kind")"
  logfile="$(_logfile "$kind")"
  port="$(_parse_port "$@")"
  mkdir -p "$STATE_DIR"

  if [ "$kind" = audio ]; then
    server="$REAL_SERVER"
    py="$(_venv_python)"
    [ -x "$py" ] || die "no venv at $VENV — run: audio_serve.sh install"
  else
    server="$STUB_SERVER"
    py="$(_stub_python)"
    [ -n "$py" ] && [ -x "$py" ] || die "no python3 found for the stub"
  fi

  if [ -f "$pidfile" ] && _pid_alive "$(cat "$pidfile" 2>/dev/null)"; then
    warn "$kind: already running (pid $(cat "$pidfile")) — use 'stop' first, or 'status'."
    return 0
  fi
  if _port_listening "$port"; then
    die "$kind: port $port already in use on $HOST — stop whatever holds it, or pass --port N"
  fi

  if [ "$kind" = audio ]; then
    local token
    token="$(_resolve_token)"
    if [ -z "$token" ]; then
      warn "HF_TOKEN not set (and no $ENV_FILE) — /v1/diarize will fail; /v1/enhance still works."
      nohup "$py" "$server" --port "$port" --host "$HOST" >> "$logfile" 2>&1 &
    else
      HF_TOKEN="$token" nohup "$py" "$server" --port "$port" --host "$HOST" >> "$logfile" 2>&1 &
    fi
  else
    nohup "$py" "$server" --port "$port" --host "$HOST" >> "$logfile" 2>&1 &
  fi
  local pid=$!
  echo "$pid" > "$pidfile"
  echo "$port" > "$(_portfile "$kind")"
  log "$kind: starting (pid $pid, port $port) — log: $logfile"

  local i
  for i in $(seq 1 30); do
    if _port_listening "$port"; then
      ok "$kind: ready — http://$HOST:$port"
      return 0
    fi
    _pid_alive "$pid" || break
    sleep 0.2
  done
  warn "$kind: not responding yet — run: audio_serve.sh $([ "$kind" = audio ] && echo log || echo stub-log)"
}

_stop() {   # $1 = audio | stub
  local kind="$1" marker pidfile pid port
  marker="$(_server_marker "$kind")"
  pidfile="$(_pidfile "$kind")"
  pid="$(cat "$pidfile" 2>/dev/null || true)"

  if [ -n "$pid" ] && _pid_alive "$pid" \
     && ps -p "$pid" -o command= 2>/dev/null | grep -qF "$marker"; then
    if _kill_pid "$pid"; then
      rm -f "$pidfile" "$(_portfile "$kind")"
      ok "$kind: stopped (pid $pid)"
      return 0
    fi
  fi
  rm -f "$pidfile"

  # Fallback: find whoever holds the port and only kill it if it's ours.
  port="$(_actual_port "$kind")"
  local holder
  holder="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n1)"
  if [ -n "$holder" ] && ps -p "$holder" -o command= 2>/dev/null | grep -qF "$marker"; then
    if _kill_pid "$holder"; then
      rm -f "$(_portfile "$kind")"
      ok "$kind: stopped (pid $holder, via port $port)"
      return 0
    fi
  fi
  warn "$kind: nothing running"
}

_kill_pid() {   # $1 = pid -> SIGTERM, wait, then SIGKILL
  local pid="$1" i
  kill "$pid" 2>/dev/null
  for i in $(seq 1 50); do
    _pid_alive "$pid" || return 0
    sleep 0.1
  done
  kill -9 "$pid" 2>/dev/null
  return 0
}

_status() {   # $1 = audio | stub
  local kind="$1" marker pidfile pid port state
  marker="$(_server_marker "$kind")"
  pidfile="$(_pidfile "$kind")"
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  port="$(_actual_port "$kind")"

  if [ -n "$pid" ] && _pid_alive "$pid" \
     && ps -p "$pid" -o command= 2>/dev/null | grep -qF "$marker"; then
    state="up"
  else
    # Confirm via the port (handles a lost pid file).
    if _port_listening "$port" \
       && lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null \
          | xargs ps -p -o command= 2>/dev/null | grep -qF "$marker"; then
      state="up"
    else
      state="down"
    fi
  fi

  if [ "$state" = up ]; then
    ok "$kind server: running on http://$HOST:$port"
  else
    warn "$kind server: not running (port $port)"
  fi
  [ "$state" = up ]
}

_log() {   # $1 = audio | stub
  local logfile
  logfile="$(_logfile "$1")"
  [ -f "$logfile" ] && tail -f "$logfile" || die "no log yet — start it first"
}

# ── Control-daemon lifecycle (the real server; ADR-0035) ──────────────────────
# The daemon is the sole lifecycle authority for the `audio` manifest daemon
# (start/stop/status verbs, blocking start with a 60s health bound). These
# helpers proxy it; the server binds the manifest's :8089.
_daemon_get() {   # $1 = path -> parsed JSON or empty on failure
  curl -sf --max-time 5 "$DAEMON_URL$1" 2>/dev/null
}

_daemon_post() {  # $1 = path -> parsed JSON or empty on failure
  curl -sf --max-time 90 -X POST "$DAEMON_URL$1" 2>/dev/null
}

_daemon_start() {
  local rsp
  rsp="$(_daemon_post /start/audio)"
  if [ -n "$rsp" ]; then
    ok "audio daemon: $(printf '%s' "$rsp" | sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p') — http://127.0.0.1:8089/v1"
    return 0
  fi
  warn "audio daemon: start failed — is the control daemon up on $DAEMON_URL?"
  warn "start it: launchctl start com.macosdev.fleetdaemon (or run macos-dev-config/bin/fleetdaemon)"
  return 1
}

_daemon_stop() {
  local rsp
  rsp="$(_daemon_post /stop/audio)"
  if [ -n "$rsp" ]; then
    ok "audio daemon: stopped"
    return 0
  fi
  warn "audio daemon: stop failed — is the control daemon up on $DAEMON_URL?"
  return 1
}

_daemon_status() {
  local rsp state
  rsp="$(_daemon_get /status/audio)"
  state="$(printf '%s' "$rsp" | sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  if [ -n "$state" ]; then
    case "$state" in
      up)   ok "audio daemon: running — http://127.0.0.1:8089/v1" ;;
      *)    warn "audio daemon: state '$state' — start it: audio_serve.sh start" ;;
    esac
    [ "$state" = up ]
    return
  fi
  warn "audio daemon: unknown — is the control daemon up on $DAEMON_URL?"
  return 1
}

# ── launchd (superseded) ───────────────────────────────────────────────────────
# The real server is daemon-managed now (ADR-0035); the com.ptm.audio LaunchAgent
# was retired. launchd-install/uninstall are gone — the fleet daemon's agent is
# the always-on authority.

# ── Dispatch ───────────────────────────────────────────────────────────────────
case "${1:-help}" in
  install)          _install ;;
  start)            _daemon_start ;;
  stop)             _daemon_stop ;;
  status)           _daemon_status ;;
  log)              [ -f "$DEV_AUDIO_LOG" ] && tail -f "$DEV_AUDIO_LOG" || die "no daemon log yet — start it first" ;;
  stub-start)       shift; _start stub "$@" ;;
  stub-stop)        _stop stub ;;
  stub-status)      _status stub ;;
  stub-log)         _log stub ;;
  -h|--help|help)   sed -n '9,21p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command: ${1:-} (install|start|stop|status|log|stub-start|stub-stop|stub-status|stub-log)" ;;
esac
