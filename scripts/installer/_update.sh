#!/usr/bin/env bash
# hive-mind update  —  scripts/installer/_update.sh
set -euo pipefail

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE="hive-sync"

GRN='\033[0;32m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }

echo ""
echo -e "${BLD}hive-mind update${RST}"
echo "────────────────────────────────────"

info "Pulling latest from GitHub..."
git -C "$HIVE_DIR" pull --ff-only
ok "Repo updated"

info "Rebuilding database..."
cd "$HIVE_DIR" && ./hv rebuild
ok "DB rebuilt"

info "Restarting sync daemon..."
systemctl --user restart "$SERVICE"
sleep 2
curl -sf http://127.0.0.1:9876/sync/hello >/dev/null && ok "Daemon responding" \
  || echo "  Daemon may still be starting — check: journalctl --user -u $SERVICE -f"

echo ""
ok "Update complete"
echo ""
