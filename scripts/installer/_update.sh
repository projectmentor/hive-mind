#!/usr/bin/env bash
# hive-mind update  —  scripts/installer/_update.sh
set -euo pipefail

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE="hive-sync"

# Shared systemd-unit writer — lets `update` re-apply the hardened unit + the
# self-heal timer to already-installed nodes (not just fresh installs).
_UNITLIB="$HIVE_DIR/scripts/installer/_units.sh"
[ -f "$_UNITLIB" ] && . "$_UNITLIB"

GRN='\033[0;32m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }

echo ""
echo -e "${BLD}hive-mind update${RST}"
echo "────────────────────────────────────"

info "Pulling latest from GitHub..."
git -C "$HIVE_DIR" pull --ff-only
ok "Repo updated"

# Refresh the command symlinks so new subcommands land without a reinstall.
# (Older installs had a static dispatcher copy that never picked up new commands.)
info "Refreshing commands..."
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
ln -sf "$HIVE_DIR/scripts/installer/dispatcher.sh" "$BIN_DIR/hive-mind"
ln -sf "$HIVE_DIR/hv" "$BIN_DIR/hv"
ok "Commands refreshed (hive-mind, hv)"

info "Rebuilding database..."
cd "$HIVE_DIR" && ./hv rebuild
ok "DB rebuilt"

info "Refreshing systemd units..."
if command -v write_systemd_units >/dev/null 2>&1; then
  write_systemd_units "$HIVE_DIR" "$SERVICE"
  ok "Units refreshed (hive-sync hardened + hive-doctor self-heal timer)"
fi

info "Restarting sync daemon..."
systemctl --user restart "$SERVICE" 2>/dev/null || true
sleep 2
curl -sf http://127.0.0.1:9876/sync/hello >/dev/null && ok "Daemon responding" \
  || echo "  Daemon may still be starting — check: journalctl --user -u $SERVICE -f"

echo ""
ok "Update complete"
echo ""
