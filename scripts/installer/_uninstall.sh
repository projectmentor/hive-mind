#!/usr/bin/env bash
# =============================================================================
# hive-mind uninstall  —  scripts/installer/_uninstall.sh
#
# Removes HiveMind from this device: the sync daemon, the `hive-mind` and `hv`
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
DOCTOR_TIMER="${DOCTOR_TIMER:-$HOME/.config/systemd/user/hive-doctor.timer}"
DOCTOR_SVC="${DOCTOR_SVC:-$HOME/.config/systemd/user/hive-doctor.service}"
SETTINGS="${SETTINGS:-$HOME/.claude/settings.json}"

# Reusable identity-preservation primitive (lives in the repo — source before we delete it).
_IDLIB="$HIVE_DIR/scripts/installer/_identity.sh"
[ -f "$_IDLIB" ] && . "$_IDLIB"

KEEP_HIVE=""; KEEP_IDENTITY=""; ASSUME_YES=""
for a in "$@"; do
  case "$a" in
    --keep-hive)     KEEP_HIVE=1 ;;
    --keep-identity) KEEP_IDENTITY=1 ;;
    -y|--yes)        ASSUME_YES=1 ;;
    -h|--help)       echo "Usage: hive-mind uninstall [--keep-hive] [--keep-identity] [--yes]"; exit 0 ;;
    *) warn "Ignoring unknown option: $a" ;;
  esac
done

echo ""
echo -e "${BLD}hive-mind uninstall${RST}"
echo "────────────────────────────────────────────"
echo "  Removes: the sync daemon, the hive-mind + hv commands, the Claude Code hooks."
if [ -n "$KEEP_HIVE" ]; then
  echo "  Your Hive (journal + keys) will be PRESERVED to a backup folder,"
  echo "  and this device's identity kept so a reinstall resumes it (no re-admit)."
elif [ -n "$KEEP_IDENTITY" ]; then
  echo "  Your Hive DATA will be DELETED, but this device's IDENTITY is kept"
  echo "  so a reinstall resumes the same device_id (no re-admit)."
else
  echo -e "  Your Hive DATA (journal, keys, store.db) will be ${BLD}DELETED${RST}."
fi
echo ""
if [ -z "$ASSUME_YES" ]; then
  printf "Continue? [y/N] "; read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# ── 1. sync daemon + self-heal timer ─────────────────────────────────────────
info "Stopping and removing the sync daemon..."
systemctl --user disable --now hive-doctor.timer 2>/dev/null || true
systemctl --user stop hive-doctor.service        2>/dev/null || true
rm -f "$DOCTOR_TIMER" "$DOCTOR_SVC"
systemctl --user stop "$SERVICE_NAME"        2>/dev/null || true
systemctl --user disable "$SERVICE_NAME"     2>/dev/null || true
rm -f "$UNIT_FILE"
systemctl --user daemon-reload               2>/dev/null || true
systemctl --user reset-failed "$SERVICE_NAME" 2>/dev/null || true
systemctl --user reset-failed hive-doctor.service 2>/dev/null || true
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

# ── 4. Identity + Hive data ──────────────────────────────────────────────────
# Preserve the device IDENTITY (to the stable stash) whenever the user asked to keep anything, so
# a reinstall can resume the same device_id with no re-admit. --keep-hive ALSO snapshots the full
# journal to a timestamped folder.
if [ -n "$KEEP_HIVE" ] || [ -n "$KEEP_IDENTITY" ]; then
  if command -v keep_identity_save >/dev/null 2>&1 && keep_identity_save "$HIVE_DIR"; then
    ok "Device identity preserved → ${HIVE_IDENTITY_STASH}"
    echo "  A reinstall will offer to resume it (same device_id; your admission still applies)."
  else
    warn "Could not preserve device identity (no key found, or identity primitive unavailable)."
  fi
fi
if [ -n "$KEEP_HIVE" ]; then
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  KEEP_DIR="$HOME/hive-mind-keep-$STAMP"
  mkdir -p "$KEEP_DIR"
  for f in journal .device-key .device-id .owner-key .peers.json; do
    [ -e "$HIVE_DIR/$f" ] && cp -a "$HIVE_DIR/$f" "$KEEP_DIR/" 2>/dev/null || true
  done
  echo ""
  ok "App removed. Full Hive backup (journal + keys) at:"
  echo "    $KEEP_DIR"
elif [ -n "$KEEP_IDENTITY" ]; then
  echo ""
  ok "App + Hive data removed; device identity kept for a future reinstall."
else
  echo ""
  ok "App + Hive data removed."
fi

echo ""
echo "  (Tailscale and your ~/.bashrc PATH line were left untouched.)"
echo "  Reload your shell to drop the removed commands: source ~/.bashrc"
echo ""

# Last: remove the repo dir itself — which contains THIS running script. `exec` hands
# off to a standalone rm so the deletion can't depend on bash still reading its own
# (now-deleted) script file. cd to a safe place first so we're not inside the target.
cd "$HOME"
exec rm -rf "$HIVE_DIR"
