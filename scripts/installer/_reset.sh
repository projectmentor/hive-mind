#!/usr/bin/env bash
# =============================================================================
# hive-mind reset  —  scripts/installer/_reset.sh
#
# Recover a WEDGED install to a clean, official state — without touching your Hive.
# Force-aligns the code to origin/<branch> (even after a history rewrite, even with
# local edits), rebuilds the DB from the journal, refreshes the supervisor units +
# Claude Code hooks, restarts the sync daemon, and verifies authenticity.
#
# PRESERVES your journal, keys, and device identity (all gitignored) — this is NOT
# `hive-mind uninstall`. Reach for it when `hv doctor`/`hv verify` is unhappy after a
# breaking change, or when you just want the long post-rewrite incantation as one word.
#
# Invoked by: hive-mind reset [-y]
# =============================================================================
set -uo pipefail

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE="hive-sync"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

# Supervisor seam (service_install / service_restart) — systemd / launchd / runit.
_SVCLIB="$HIVE_DIR/scripts/installer/_service.sh"
[ -f "$_SVCLIB" ] && . "$_SVCLIB"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }

ASSUME_YES=""
for a in "$@"; do
  case "$a" in
    -y|--yes)  ASSUME_YES=1 ;;
    -h|--help) echo "Usage: hive-mind reset [-y]"; echo "  Force-align code to origin + rebuild + restart + verify. Your Hive data is preserved."; exit 0 ;;
  esac
done

if [ -z "${HIVE_RESET_REEXEC:-}" ]; then
  echo ""
  echo -e "${BLD}hive-mind reset${RST}"
  echo "────────────────────────────────────"
  echo "  Force-resets this device's CODE to the official origin and restarts the daemon."
  echo "  Rebuilds the local DB, refreshes supervision + hooks, and verifies authenticity."
  echo -e "  Your Hive (journal, keys, device identity) is ${BLD}PRESERVED${RST} — this is not uninstall."
  echo ""
  if [ -z "$ASSUME_YES" ] && [ -t 0 ]; then
    printf "Continue? [y/N] "; read -r _ans
    case "$_ans" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
  fi
fi

# ── 1. force-align the code to origin/<branch> (handles rewrites + a dirty tree) ──
info "Force-aligning code to origin..."
git -C "$HIVE_DIR" fetch --prune --tags origin 2>&1 | tail -1 || true
_BR="$(git -C "$HIVE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ -z "$_BR" ] || [ "$_BR" = "HEAD" ] && _BR=main
git -C "$HIVE_DIR" reset --hard "origin/$_BR" 2>&1 | tail -1
ok "Code at origin/$_BR ($(git -C "$HIVE_DIR" rev-parse --short HEAD 2>/dev/null))"

# Re-exec the freshly-reset copy once so the steps below run the NEW logic (mirrors _update.sh).
# Guarded so an old checkout that lacks this file degrades gracefully (runs inline).
if [ -z "${HIVE_RESET_REEXEC:-}" ] && [ -f "$HIVE_DIR/scripts/installer/_reset.sh" ]; then
  export HIVE_RESET_REEXEC=1
  exec bash "$HIVE_DIR/scripts/installer/_reset.sh" -y
fi

# ── 2. relink the commands (new subcommands land without a reinstall) ──
mkdir -p "$BIN_DIR"
ln -sf "$HIVE_DIR/scripts/installer/dispatcher.sh" "$BIN_DIR/hive-mind"
ln -sf "$HIVE_DIR/hv" "$BIN_DIR/hv"
ok "Commands relinked (hive-mind, hv)"

# ── 3. rebuild store.db from the (preserved) journal ──
info "Rebuilding database from the journal..."
( cd "$HIVE_DIR" && ./hv doctor rebuild >/dev/null 2>&1 ) && ok "Database rebuilt" || warn "DB rebuild reported a problem — run 'hv doctor' to inspect."

# ── 4. refresh supervisor units + Claude Code hooks ──
if command -v service_install >/dev/null 2>&1; then
  service_install "$HIVE_DIR" "$SERVICE" && ok "Supervisor units refreshed"
fi
if [ -d "$HOME/.claude" ] || command -v claude >/dev/null 2>&1; then
  "$HIVE_DIR/hv" wire claude >/dev/null 2>&1 && ok "Claude Code hooks re-wired" || true
fi

# ── 5. restart the sync daemon ──
info "Restarting the sync daemon..."
if command -v service_restart >/dev/null 2>&1; then
  service_restart "$SERVICE"
else
  systemctl --user restart "$SERVICE" 2>/dev/null || true
fi
sleep 2
curl -sf http://127.0.0.1:9876/sync/hello >/dev/null 2>&1 && ok "Daemon responding on :9876" \
  || warn "Daemon not yet responding — give it a moment, then check 'hive-mind status'."

# ── 6. verify authenticity (the thing that was failing pre-reset) ──
info "Verifying authenticity..."
( cd "$HIVE_DIR" && ./hv verify 2>&1 | head -2 )

echo ""
ok "Reset complete — your Hive data was preserved. Try: hv stats  /  hv peers"
echo ""
