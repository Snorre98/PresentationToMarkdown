#!/usr/bin/env bash
# install-global.sh — install a `ptm-tui` launcher into ~/.local/bin.
#
# Idempotent: writes (or refreshes) ~/.local/bin/ptm-tui to exec the repo's
# scripts/ptm-tui.sh with the absolute path (symlink-safe). ~/.local/bin is
# already on PATH for the common shells; add it to $PATH if not.
#
#   scripts/install-global.sh
#
# No `set -e`/`set -u`: stock macOS bash 3.2 footguns (see audio_serve.sh).
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/ptm-tui"

mkdir -p "$BIN_DIR" || { echo "could not create $BIN_DIR" >&2; exit 1; }

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$ROOT/scripts/ptm-tui.sh" "\$@"
EOF
chmod +x "$LAUNCHER"

echo "installed: $LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) echo "$BIN_DIR is on PATH" ;;
  *) echo "note: add $BIN_DIR to your PATH (e.g. export PATH=\"\$HOME/.local/bin:\$PATH\")" ;;
esac
