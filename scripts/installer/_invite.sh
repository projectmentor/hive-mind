#!/usr/bin/env bash
# =============================================================================
# hive-mind invite  —  scripts/installer/_invite.sh
#
# Prints the ONE line a new device needs to join this hive: this node's Tailscale
# address. The whole point is to make "how do I get the address?" a single command
# with copy-paste output — no networking knowledge required. The new device pastes
# it at the `hive-mind install` join prompt.
#
# Resolving our own tailnet IP is cross-platform: the `tailscale` CLI when present
# (Linux/macOS), else the interface (Android/Termux, where Tailscale is the app and
# has no CLI — same path STEP 2 of the installer uses), else `hostname -I`.
#
# Invoked by: hive-mind invite   |   bash scripts/installer/_invite.sh
# =============================================================================
set -uo pipefail

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
PORT="${HIVE_SYNC_PORT:-9876}"

GRN='\033[0;32m'; YLW='\033[1;33m'; BLD='\033[1m'; RST='\033[0m'
warn() { echo -e "${YLW}[!!]${RST}  $*"; }

# macOS app path for the tailscale binary (defines _resolve_tailscale) — best-effort.
_SVCLIB="$HIVE_DIR/scripts/installer/_service.sh"
[ -f "$_SVCLIB" ] && . "$_SVCLIB" 2>/dev/null || true

# Resolve THIS device's 100.x tailnet IP, trying the most authoritative source first.
_tailnet_ip() {
  local ip=""
  # 1. tailscale CLI (Linux / macOS with brew CLI)
  if command -v tailscale >/dev/null 2>&1; then
    ip="$(tailscale ip -4 2>/dev/null | grep '^100\.' | head -1 | tr -d '[:space:]')"
    [ -n "$ip" ] && { echo "$ip"; return; }
  fi
  # 2. macOS GUI-app tailscale binary
  if command -v _resolve_tailscale >/dev/null 2>&1; then
    local tsbin; tsbin="$(_resolve_tailscale 2>/dev/null || true)"
    if [ -n "$tsbin" ]; then
      ip="$("$tsbin" ip -4 2>/dev/null | grep '^100\.' | head -1 | tr -d '[:space:]')"
      [ -n "$ip" ] && { echo "$ip"; return; }
    fi
  fi
  # 3. the interface (Android/Termux app-owned tun, or any node without the CLI)
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 addr 2>/dev/null | grep -oE '100\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
    [ -n "$ip" ] && { echo "$ip"; return; }
  fi
  if command -v ifconfig >/dev/null 2>&1; then
    ip="$(ifconfig 2>/dev/null | grep -oE '100\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
    [ -n "$ip" ] && { echo "$ip"; return; }
  fi
  # 4. last resort
  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep '^100\.' | head -1)"
    [ -n "$ip" ] && { echo "$ip"; return; }
  fi
  echo ""
}

TS_IP="$(_tailnet_ip)"
HIVE_ID=""
[ -x "$HIVE_DIR/hv" ] && HIVE_ID="$("$HIVE_DIR/hv" owner show 2>/dev/null | awk '/hive_id:/{print $2}')"
# The genesis FINGERPRINT (<hive_id>/<owner_id>/<hash8>), so the new device can pin this hive BEFORE
# it trusts any journal (hive-mind-private #14). Carried as a PATH after the address: every older
# installer strips the path when parsing, so an old device still reads just the address.
GENESIS_FP=""
[ -x "$HIVE_DIR/hv" ] && GENESIS_FP="$("$HIVE_DIR/hv" owner show 2>/dev/null \
  | sed -n 's/.*fingerprint \([^ ]*\).*/\1/p' | head -1)"
PASTE="$TS_IP"
[ -n "$GENESIS_FP" ] && PASTE="$TS_IP/$GENESIS_FP"

echo ""
echo -e "${BLD}hive-mind invite${RST}${HIVE_ID:+   (hive $HIVE_ID)}"
echo "────────────────────────────────────"
if [ -z "$TS_IP" ]; then
  warn "Couldn't find this device's Tailscale (100.x) address."
  warn "Make sure Tailscale is connected, then run 'hive-mind invite' again."
  exit 0
fi
echo "  Add a device to this hive. On the NEW device, run:"
echo -e "      ${BLD}hive-mind install${RST}"
echo "  and when it asks for a hive address, paste this:"
echo ""
echo -e "      ${GRN}${BLD}${PASTE}${RST}"
echo ""
if [ -n "$GENESIS_FP" ]; then
  echo "  (This device's Tailscale address, then this hive's genesis fingerprint. The new device"
  echo "   pins that fingerprint before it trusts anything, so another hive advertising this"
  echo "   hive's id can't capture it. Paste the whole line.)"
else
  echo "  (That's this device's Tailscale address — the new device only needs one node to join.)"
  warn "This device has not pinned its genesis, so the invite carries no fingerprint."
  warn "Run 'hv owner pin --set' here, then 'hive-mind invite' again."
fi
echo ""
