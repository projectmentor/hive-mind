#!/usr/bin/env bash
# =============================================================================
# hive-mind installer  —  scripts/installer/install.sh
#
# Bootstrap (run once via curl):
#   curl -fsSL https://raw.githubusercontent.com/projectmentor/hive-mind/main/scripts/installer/install.sh | bash
#
# Or after cloning:
#   bash ~/projects/hive-mind/scripts/installer/install.sh
#
# What this does:
#   1. Installs the `hive-mind` command to ~/.local/bin
#   2. Runs `hive-mind install` which sets up the full node environment
#
# Assumptions:
#   - Running inside WSL2 (Ubuntu) on Windows 11
#   - Tailscale installed on Windows host (for your own connectivity)
#   - Tailscale will be installed inside WSL by the installer if missing
#     (WSL gets its own 100.x tailnet IP — no portproxy or mirrored networking needed)
#   - Internet access available for git clone + Tailscale install
#   - systemd enabled in WSL (/etc/wsl.conf [boot] systemd=true)
#
# Subcommands installed:
#   hive-mind install   — full node setup (clone, daemon, .wslconfig, peers)
#   hive-mind update    — pull latest + restart daemon  [future]
#   hive-mind status    — show node health              [future]
#   hive-mind peers     — manage peer list              [future]
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/projectmentor/hive-mind.git"
HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
BIN_DIR="$HOME/.local/bin"
CMD="$BIN_DIR/hive-mind"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }
die()  { echo -e "${RED}[!!]${RST}  $*" >&2; exit 1; }

echo ""
echo -e "${BLD}hive-mind installer${RST}"
echo "────────────────────────────────────────────"
echo ""

# ── guard: must be WSL ─────────────────────────────────────────────────────
if ! grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null; then
  die "This installer is for WSL2 on Windows 11. Not detected."
fi

# ── 1. ensure ~/.local/bin exists and is on PATH ───────────────────────────
mkdir -p "$BIN_DIR"
PATH_ADDED=""
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  export PATH="$BIN_DIR:$PATH"      # this subshell only — can't touch the caller's shell
  # Persist to ~/.bashrc for future shells (idempotent — don't stack duplicate lines on re-run).
  if ! grep -qsF 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.bashrc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  fi
  PATH_ADDED=1
  warn "$BIN_DIR added to PATH in ~/.bashrc (new shells will pick it up)."
fi

# ── 2. clone or update the repo to get the latest installer ───────────────
if [ -d "$HIVE_DIR/.git" ]; then
  info "Repo already present at $HIVE_DIR — pulling latest..."
  git -C "$HIVE_DIR" pull --ff-only --quiet
  ok "Repo updated"
else
  info "Cloning hive-mind repo to $HIVE_DIR ..."
  mkdir -p "$(dirname "$HIVE_DIR")"
  git clone --quiet "$REPO_URL" "$HIVE_DIR"
  ok "Repo cloned"
fi

# ── 3. write the hive-mind dispatcher to ~/.local/bin ────────────────────
info "Installing hive-mind command to $CMD ..."
cat > "$CMD" << 'DISPATCHER'
#!/usr/bin/env bash
# hive-mind command dispatcher
HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
INSTALLER="$HIVE_DIR/scripts/installer"
CMD="${1:-help}"
shift 2>/dev/null || true

case "$CMD" in
  install) exec bash "$INSTALLER/_install_node.sh" "$@" ;;
  update)  exec bash "$INSTALLER/_update.sh"       "$@" ;;
  status)  exec bash "$INSTALLER/_status.sh"       "$@" ;;
  *)
    echo "Usage: hive-mind <subcommand>"
    echo ""
    echo "Subcommands:"
    echo "  install    Set up this node from scratch"
    echo "  update     Pull latest + restart daemon"
    echo "  status     Show node health and peer sync state"
    echo ""
    ;;
esac
DISPATCHER
chmod +x "$CMD"
ok "hive-mind command installed at $CMD"

echo ""
echo -e "${BLD}hive-mind command installed.${RST}"
echo ""
echo "  Set up your Hive:"
echo -e "    ${BLD}hive-mind install${RST}"
echo ""
if [ -n "$PATH_ADDED" ]; then
  echo "  (first reload your shell so 'hive-mind' resolves:  source ~/.bashrc"
  echo -e "   — or run it by full path:  ${BLD}$CMD install${RST})"
  echo ""
fi
