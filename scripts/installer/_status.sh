#!/usr/bin/env bash
# hive-mind status  —  scripts/installer/_status.sh
set -euo pipefail

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE="hive-sync"

# Cross-platform supervisor seam (service_is_active, _resolve_tailscale on macOS).
_SVCLIB="$HIVE_DIR/scripts/installer/_service.sh"
if [ -f "$_SVCLIB" ]; then . "$_SVCLIB"; fi

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }
bad()  { echo -e "${RED}[!!]${RST}  $*"; }

echo ""
echo -e "${BLD}hive-mind status${RST}"
echo "────────────────────────────────────"

# Daemon
if command -v service_is_active >/dev/null 2>&1 && service_is_active "$SERVICE" 2>/dev/null; then
  ok "Daemon: running"
else
  bad "Daemon: not running"
fi

# /sync/hello
HELLO=$(curl -sf http://127.0.0.1:9876/sync/hello 2>/dev/null) && {
  NODE=$(echo "$HELLO" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("node_id","?"))')
  ENT=$(echo "$HELLO" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("journal_entries","?"))')
  ok "HTTP: /sync/hello responding  (node=$NODE entries=$ENT)"
} || warn "HTTP: daemon not responding on :9876"

# Stats
if [ -x "$HIVE_DIR/hv" ]; then
  echo ""
  "$HIVE_DIR/hv" stats
fi

# Tailscale
case "$(uname -s)" in
  Darwin)
    TSBIN="$(_resolve_tailscale 2>/dev/null || true)"
    TS_IP=$([ -n "$TSBIN" ] && "$TSBIN" ip 2>/dev/null | grep '^100\.' | head -1 | tr -d '[:space:]') || true
    [[ -n "$TS_IP" ]] && ok "Tailscale: $TS_IP" || warn "Tailscale: no 100.x IP (is Tailscale running + authenticated?)"
    ;;
  *)
    # WSL: query the Windows host's tailscale via interop; native Linux has no powershell.exe (skips).
    TS_IP=$(powershell.exe -NoProfile -Command "try { (& tailscale ip 2>null).Trim() } catch { '' }" 2>/dev/null | tr -d '\r' | head -1) || true
    if [[ -n "$TS_IP" ]]; then
      ok "Tailscale: $TS_IP"
      ip addr show 2>/dev/null | grep -q "$TS_IP" && ok "Mirrored networking: active" \
        || warn "Mirrored networking: Tailscale IP not visible in WSL (run wsl --shutdown)"
    fi
    ;;
esac

# Peers
PEERS_FILE="$HIVE_DIR/.peers.json"
if [ -f "$PEERS_FILE" ]; then
  python3 - "$PEERS_FILE" << 'PYEOF'
import json, sys, urllib.request, urllib.error
data = json.load(open(sys.argv[1]))
peers = data.get("peers", [])
if not peers:
    print("  No peers configured")
else:
    for p in peers:
        url = p["url"] + "/sync/hello"
        try:
            r = urllib.request.urlopen(url, timeout=5)
            d = json.loads(r.read())
            print(f"  \033[0;32m[ok]\033[0m  peer {p['id']}: {d.get('journal_entries','?')} entries")
        except Exception as e:
            print(f"  \033[1;33m[!!]\033[0m  peer {p['id']}: unreachable ({e})")
PYEOF
fi

echo ""
