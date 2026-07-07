#!/bin/bash
# Install blueprint and blueprint-runner as bare commands via ~/.local/bin symlinks.
# Run once after cloning; re-running is idempotent.
#
# Usage: bash scripts/install-cli.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$HOME/.local/bin"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[install-cli]${NC} $1"; }
ok()    { echo -e "${GREEN}[install-cli]${NC} $1"; }
warn()  { echo -e "${YELLOW}[install-cli]${NC} $1"; }

# Create ~/.local/bin if it doesn't exist.
if [ ! -d "$BIN_DIR" ]; then
  mkdir -p "$BIN_DIR"
  info "Created $BIN_DIR"
fi

link_cmd() {
  local name="$1"
  local target="$REPO_ROOT/$name"
  local link="$BIN_DIR/$name"

  if [ ! -f "$target" ]; then
    echo -e "${RED}[install-cli]${NC} $target not found — is REPO_ROOT correct? ($REPO_ROOT)"
    exit 1
  fi

  if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
    ok "$name already linked → $target"
    return
  fi

  # Remove stale symlink or file at the destination before relinking.
  { [ -e "$link" ] || [ -L "$link" ]; } && rm "$link"
  ln -s "$target" "$link"
  ok "Linked: $link → $target"
}

link_cmd blueprint
link_cmd blueprint-runner

# Warn if ~/.local/bin is not on PATH so the operator knows what to do.
case ":$PATH:" in
  *":$BIN_DIR:"*)
    ok "~/.local/bin is already on PATH — 'blueprint' and 'blueprint-runner' are ready."
    info "To run the runner durably: 'blueprint-runner install --token <token>' (systemd service; add --system on shared boxes)."
    ;;
  *)
    warn "~/.local/bin is NOT on your PATH."
    warn "Add this line to your shell rc file (~/.bashrc, ~/.zshrc, etc.) and restart your shell:"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    warn "After that, 'blueprint' and 'blueprint-runner' will work from any directory."
    ;;
esac
