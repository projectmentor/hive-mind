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

# The pull above may have replaced THIS script on disk, but bash is still running
# the old copy from memory — so any update logic added in the new version (e.g.
# rewriting systemd units) would silently not run until a SECOND update. Re-exec
# the freshly-pulled copy once so the new logic applies on the FIRST update. The
# env-var guard prevents an infinite re-exec loop.
if [ -z "${HIVE_UPDATE_REEXEC:-}" ]; then
  export HIVE_UPDATE_REEXEC=1
  exec bash "$HIVE_DIR/scripts/installer/_update.sh" "$@"
fi

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

# Re-assert the Claude Code integration so an update from before a hook existed picks it up here,
# not only on the 15-min self-heal timer. Idempotent; silent + harmless on a node without Claude Code.
if [ -d "$HOME/.claude" ] || command -v claude >/dev/null 2>&1; then
  info "Re-asserting Claude Code hooks..."
  "$HIVE_DIR/hv" doctor wire-agent >/dev/null 2>&1 && ok "Claude Code hooks wired" || true
fi

info "Restarting sync daemon..."
systemctl --user restart "$SERVICE" 2>/dev/null || true
sleep 2
curl -sf http://127.0.0.1:9876/sync/hello >/dev/null && ok "Daemon responding" \
  || echo "  Daemon may still be starting — check: journalctl --user -u $SERVICE -f"

echo ""
ok "Update complete"
echo ""
