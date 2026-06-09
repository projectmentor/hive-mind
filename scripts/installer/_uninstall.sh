#!/usr/bin/env bash
# =============================================================================
# hive-mind uninstall  —  scripts/installer/_uninstall.sh
#
# Removes HiveMind from this node: the sync daemon, the `hive-mind` and `hv`
# commands, and the Claude Code hooks. By default it also DELETES the Hive
# (journal, keys, store.db). Pass --keep-hive to preserve your journal + keys
# to a backup folder so a reinstall can resume the same identity.
#
# Invoked by: hive-mind uninstall [--keep-hive] [--yes]
# Or directly: bash scripts/installer/_uninstall.sh [--keep-hive] [--yes]
#
# Does NOT touch Tailscale or your ~/.bashrc PATH line (both harmless to leave).
# Idempotent; safe to run more than once.
# =============================================================================
set -uo pipefail
cd "$HOME"   # never run from inside the dir we may delete

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }

# Paths are overridable (non-standard installs + tests); defaults match the installer.
HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE_NAME="${SERVICE_NAME:-hive-sync}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
UNIT_FILE="${UNIT_FILE:-$HOME/.config/systemd/user/$SERVICE_NAME.service}"
SETTINGS="${SETTINGS:-$HOME/.claude/settings.json}"

KEEP_HIVE=""; ASSUME_YES=""
for a in "$@"; do
  case "$a" in
    --keep-hive) KEEP_HIVE=1 ;;
    -y|--yes)    ASSUME_YES=1 ;;
    -h|--help)   echo "Usage: hive-mind uninstall [--keep-hive] [--yes]"; exit 0 ;;
    *) warn "Ignoring unknown option: $a" ;;
  esac
done

echo ""
echo -e "${BLD}hive-mind uninstall${RST}"
echo "────────────────────────────────────────────"
echo "  Removes: the sync daemon, the hive-mind + hv commands, the Claude Code hooks."
if [ -n "$KEEP_HIVE" ]; then
  echo "  Your Hive (journal + keys) will be PRESERVED to a backup folder."
else
  echo -e "  Your Hive DATA (journal, keys, store.db) will be ${BLD}DELETED${RST}."
fi
echo ""
if [ -z "$ASSUME_YES" ]; then
  printf "Continue? [y/N] "; read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# ── 1. sync daemon ──────────────────────────────────────────────────────────
info "Stopping and removing the sync daemon..."
systemctl --user stop "$SERVICE_NAME"        2>/dev/null || true
systemctl --user disable "$SERVICE_NAME"     2>/dev/null || true
rm -f "$UNIT_FILE"
systemctl --user daemon-reload               2>/dev/null || true
systemctl --user reset-failed "$SERVICE_NAME" 2>/dev/null || true
if [ -z "${HIVE_UNINSTALL_TEST:-}" ]; then
  pkill -f "sync_daemon.py" 2>/dev/null || true    # any stray/non-systemd daemon
  if crontab -l 2>/dev/null | grep -q "sync_daemon.py"; then
    crontab -l 2>/dev/null | grep -v "sync_daemon.py" | crontab - 2>/dev/null || true
  fi
fi
ok "Daemon stopped and removed."

# ── 2. CLI commands ─────────────────────────────────────────────────────────
rm -f "$BIN_DIR/hive-mind" "$BIN_DIR/hv"
ok "Removed hive-mind and hv from $BIN_DIR."

# ── 3. Claude Code integration (surgical — strip only HiveMind hooks) ───────
if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak.uninstall" 2>/dev/null || true
  python3 - "$SETTINGS" "$HIVE_DIR" << 'PY' && \
    ok "Removed HiveMind hooks from ~/.claude/settings.json (backup: settings.json.bak.uninstall)."
import json, sys
path, hive = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path))
except Exception:
    sys.exit(0)
def is_hive(h):
    s = json.dumps(h)
    return any(m in s for m in (hive, "hive-mind", "_hook.sh", "hv nudge", "hv telemetry"))
hooks = d.get("hooks", {})
for ev in list(hooks):
    kept = [h for h in (hooks[ev] or []) if not is_hive(h)]
    if kept:
        hooks[ev] = kept
    else:
        del hooks[ev]
if not hooks:
    d.pop("hooks", None)
json.dump(d, open(path, "w"), indent=2)
PY
fi
rm -rf "$HOME/.claude/skills/hive-memory" 2>/dev/null || true
rm -f /tmp/hive-sync.log 2>/dev/null || true

# ── 4. Hive data ────────────────────────────────────────────────────────────
if [ -n "$KEEP_HIVE" ]; then
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  KEEP_DIR="$HOME/hive-mind-keep-$STAMP"
  mkdir -p "$KEEP_DIR"
  for f in journal .device-key .device-id .owner-key .peers.json; do
    [ -e "$HIVE_DIR/$f" ] && cp -a "$HIVE_DIR/$f" "$KEEP_DIR/" 2>/dev/null || true
  done
  echo ""
  ok "App removed. Your Hive (journal + keys) is preserved at:"
  echo "    $KEEP_DIR"
  echo "  To resume after a reinstall: run hive-mind install, then copy those files into"
  echo "  the new $HIVE_DIR and run: hv rebuild"
else
  echo ""
  ok "App + Hive data removed."
fi

echo ""
echo "  (Tailscale and your ~/.bashrc PATH line were left untouched.)"
echo "  Reload your shell to drop the removed commands: source ~/.bashrc"
echo ""

rm -rf "$HIVE_DIR"   # last — removes this script too (open fd keeps it running)
