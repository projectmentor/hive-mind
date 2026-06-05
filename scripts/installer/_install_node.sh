#!/usr/bin/env bash
# =============================================================================
# hive-mind install  —  scripts/installer/_install_node.sh
#
# Full node setup. Invoked by: hive-mind install
#
# Steps:
#   1.  Pre-flight checks (WSL2, systemd, Tailscale reachable)
#   2.  python3 + uv + requests
#   3.  Clone / update repo
#   4.  .wslconfig — networkingMode=mirrored  (makes Tailscale 100.x visible in WSL)
#       -> prints manual wsl --shutdown instruction if needed, waits for confirm
#   5.  Detect this node's Tailscale IP
#   6.  Collect peer Tailscale IPs (single prompt)
#   7.  Write .peers.json
#   8.  Init store.db  (hv rebuild)
#   9.  Install systemd service  (hive-sync.service)
#   10. Start daemon + smoke-test /sync/hello
#   11. Wire Hermes memory plugin (best-effort)
#   12. Final summary
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/projectmentor/hive-mind.git"
HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE_NAME="hive-sync"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'
ok()      { echo -e "${GRN}[ok]${RST}  $*"; }
info()    { echo -e "${BLD}[..]${RST}  $*"; }
warn()    { echo -e "${YLW}[!!]${RST}  $*"; }
step()    { echo -e "\n${CYN}──── $* ${RST}"; }
die()     { echo -e "\n${RED}[FATAL]${RST} $*\n" >&2; exit 1; }
ask()     { echo -e "${BLD}[?>]${RST}  $*"; }

echo ""
echo -e "${BLD}╔══════════════════════════════════════╗${RST}"
echo -e "${BLD}║       hive-mind node installer       ║${RST}"
echo -e "${BLD}╚══════════════════════════════════════╝${RST}"
echo ""

# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Pre-flight
# ════════════════════════════════════════════════════════════════════════════
step "1/12  Pre-flight checks"

grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null \
  || die "Must run inside WSL2 on Windows 11."

[ -d /run/systemd/system ] \
  || die "systemd not running in this WSL. Enable it first:\n  sudo bash -c 'echo -e \"[boot]\\nsystemd=true\" >> /etc/wsl.conf'\n  Then from Windows: wsl --shutdown\n  Reopen WSL and re-run hive-mind install"

# Tailscale: check that the Windows host has a 100.x address visible via
# /mnt/c (mirrored mode makes it our address too, but we can query it via PS)
TS_IP=$(powershell.exe -NoProfile -Command \
  "try { (& tailscale ip 2>null).Trim() } catch { '' }" 2>/dev/null \
  | tr -d '\r' | head -1) || true

if [[ -z "$TS_IP" || ! "$TS_IP" =~ ^100\. ]]; then
  die "Could not detect a Tailscale IP on the Windows host.\nMake sure Tailscale is installed, signed in, and connected."
fi
ok "Tailscale detected: $TS_IP"

# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — python3 + uv + requests
# ════════════════════════════════════════════════════════════════════════════
step "2/12  Python dependencies"

command -v python3 &>/dev/null || {
  info "Installing python3..."
  sudo apt-get update -qq && sudo apt-get install -y -qq python3
}
ok "python3 $(python3 --version)"

if ! command -v uv &>/dev/null; then
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  source "$HOME/.local/bin/env" 2>/dev/null \
    || source "$HOME/.cargo/env" 2>/dev/null \
    || export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version)"

# requests for sync_client.py
python3 -c "import requests" 2>/dev/null || {
  info "Installing requests..."
  uv pip install --system requests
}
ok "requests available"

# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — Clone / update repo
# ════════════════════════════════════════════════════════════════════════════
step "3/12  Repository"

if [ -d "$HIVE_DIR/.git" ]; then
  info "Repo exists at $HIVE_DIR — pulling latest..."
  git -C "$HIVE_DIR" pull --ff-only --quiet
  ok "Repo up to date"
else
  info "Cloning to $HIVE_DIR ..."
  mkdir -p "$(dirname "$HIVE_DIR")"
  git clone --quiet "$REPO_URL" "$HIVE_DIR"
  ok "Repo cloned"
fi

chmod +x "$HIVE_DIR/hv"
cd "$HIVE_DIR"

# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — .wslconfig  networkingMode=mirrored
# ════════════════════════════════════════════════════════════════════════════
step "4/12  WSL mirrored networking"

WIN_USER=$(powershell.exe -NoProfile -Command \
  "[System.Environment]::UserName" 2>/dev/null | tr -d '\r')
WSLCONFIG="/mnt/c/Users/${WIN_USER}/.wslconfig"

NEEDS_SHUTDOWN=false

if [ -f "$WSLCONFIG" ] && grep -q 'networkingMode=mirrored' "$WSLCONFIG" 2>/dev/null; then
  ok ".wslconfig already has networkingMode=mirrored"
else
  info "Writing networkingMode=mirrored to $WSLCONFIG ..."

  # Append [wsl2] section if not present, else add under existing [wsl2]
  if grep -q '^\[wsl2\]' "$WSLCONFIG" 2>/dev/null; then
    # Already has [wsl2] — insert networkingMode after it
    python3 - "$WSLCONFIG" <<'PYEOF'
import sys, re
path = sys.argv[1]
txt = open(path).read()
txt = re.sub(r'(\[wsl2\])', r'\1\nnetworkingMode=mirrored', txt, count=1)
open(path, 'w').write(txt)
PYEOF
  else
    printf '\n[wsl2]\nnetworkingMode=mirrored\n' >> "$WSLCONFIG"
  fi
  ok ".wslconfig updated"
  NEEDS_SHUTDOWN=true
fi

# Check if WSL already has the Tailscale IP (mirrored already active)
if ip addr show 2>/dev/null | grep -q "$TS_IP"; then
  ok "Tailscale IP $TS_IP already visible inside WSL (mirrored active)"
  NEEDS_SHUTDOWN=false
fi

if $NEEDS_SHUTDOWN; then
  echo ""
  warn "═══════════════════════════════════════════════════════"
  warn "  ACTION REQUIRED: WSL must be restarted to apply"
  warn "  networkingMode=mirrored."
  warn ""
  warn "  1. Open a WINDOWS terminal (PowerShell or cmd) and run:"
  warn "       wsl --shutdown"
  warn "  2. Reopen WSL (just open a new Ubuntu terminal)"
  warn "  3. Run: hive-mind install   (resumes from here)"
  warn "═══════════════════════════════════════════════════════"
  echo ""
  ask "Press Enter once WSL has been restarted and you're back..."
  read -r

  # Recheck
  ip addr show 2>/dev/null | grep -q "$TS_IP" \
    || die "Tailscale IP $TS_IP still not visible inside WSL.\nCheck that networkingMode=mirrored is in $WSLCONFIG and run wsl --shutdown again."
  ok "Tailscale IP $TS_IP now visible inside WSL"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 5 — Detect this node's Tailscale IP
# ════════════════════════════════════════════════════════════════════════════
step "5/12  Node identity"

# With mirrored networking, TS_IP is now our own WSL IP too
THIS_IP="$TS_IP"
THIS_NODE=$(hostname)
ok "This node: $THIS_NODE @ $THIS_IP"

# ════════════════════════════════════════════════════════════════════════════
# STEP 6 — Collect peer IPs (the ONLY interactive prompt)
# ════════════════════════════════════════════════════════════════════════════
step "6/12  Peer configuration"

PEERS_FILE="$HIVE_DIR/.peers.json"

if [ -f "$PEERS_FILE" ]; then
  ok ".peers.json already exists — skipping peer prompt"
  echo "  (delete $PEERS_FILE and re-run to reconfigure peers)"
else
  echo ""
  echo "  Enter the Tailscale IPs of your peer nodes."
  echo "  Comma-separated, e.g.: 100.114.200.119"
  echo "  Leave blank if this is a single-node setup."
  echo ""
  ask "Peer Tailscale IPs: "
  read -r PEER_INPUT

  # Build peers JSON
  PEERS_JSON="[]"
  if [ -n "$PEER_INPUT" ]; then
    PEERS_JSON=$(python3 - "$PEER_INPUT" << 'PYEOF'
import sys, json
ips = [x.strip() for x in sys.argv[1].split(',') if x.strip()]
peers = [{"id": ip.replace('.', '-'), "url": f"http://{ip}:9876"} for ip in ips]
print(json.dumps(peers, indent=2))
PYEOF
)
  fi

  python3 - "$THIS_NODE" "$THIS_IP" "$PEERS_JSON" "$PEERS_FILE" << 'PYEOF'
import sys, json
node, ip, peers_raw, outfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
config = {
  "self": node.lower(),
  "bind": "0.0.0.0",
  "port": 9876,
  "peers": json.loads(peers_raw)
}
json.dump(config, open(outfile, 'w'), indent=2)
print(f"wrote {outfile}")
PYEOF
  ok ".peers.json written"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 7 — Init store.db
# ════════════════════════════════════════════════════════════════════════════
step "7/12  Database initialisation"

cd "$HIVE_DIR"
if [ -f store.db ]; then
  ok "store.db already exists"
else
  info "Running hv rebuild to initialise database..."
  ./hv rebuild
  ok "store.db created"
fi
./hv stats

# ════════════════════════════════════════════════════════════════════════════
# STEP 8 — systemd service
# ════════════════════════════════════════════════════════════════════════════
step "8/12  Systemd service"

UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_FILE" << UNIT
[Unit]
Description=Hive Mind sync daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=$HIVE_DIR
ExecStart=/usr/bin/python3 $HIVE_DIR/sync_daemon.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=HIVE_HOME=$HIVE_DIR

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME" --quiet
ok "systemd unit installed: $UNIT_FILE"

# ════════════════════════════════════════════════════════════════════════════
# STEP 9 — Start daemon + smoke-test
# ════════════════════════════════════════════════════════════════════════════
step "9/12  Start sync daemon"

systemctl --user restart "$SERVICE_NAME"
sleep 2

# smoke-test /sync/hello
HELLO=$(curl -sf "http://127.0.0.1:9876/sync/hello" 2>/dev/null) && {
  ok "Daemon responding: $(echo "$HELLO" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("node_id","?"), "—", d.get("journal_entries","?"), "entries")')"
} || {
  warn "Daemon not yet responding on :9876. Check: journalctl --user -u $SERVICE_NAME -n 20"
}

# ════════════════════════════════════════════════════════════════════════════
# STEP 10 — Hermes memory plugin (best-effort)
# ════════════════════════════════════════════════════════════════════════════
step "10/12  Hermes integration"

if command -v hermes &>/dev/null; then
  HERMES_PLUGINS="${HERMES_HOME:-$HOME/.hermes}/plugins"
  mkdir -p "$HERMES_PLUGINS"
  rm -rf "$HERMES_PLUGINS/hive-mind"
  ln -s "$HIVE_DIR/hermes_plugin" "$HERMES_PLUGINS/hive-mind"
  hermes config set memory.provider hive-mind 2>/dev/null && \
    ok "Hermes memory plugin linked and activated" || \
    warn "Hermes plugin linked but config set failed — run manually: hermes config set memory.provider hive-mind"
else
  warn "Hermes not installed — skipping plugin wiring"
  warn "  When Hermes is installed, run: hermes config set memory.provider hive-mind"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 11 — PATH persistence
# ════════════════════════════════════════════════════════════════════════════
step "11/12  PATH"

BIN_DIR="$HOME/.local/bin"
if ! grep -q 'local/bin' "$HOME/.bashrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi
ok "~/.local/bin in PATH"

# ════════════════════════════════════════════════════════════════════════════
# STEP 12 — Summary
# ════════════════════════════════════════════════════════════════════════════
step "12/12  Done"

echo ""
echo -e "${GRN}${BLD}╔══════════════════════════════════════╗${RST}"
echo -e "${GRN}${BLD}║     hive-mind node setup complete    ║${RST}"
echo -e "${GRN}${BLD}╚══════════════════════════════════════╝${RST}"
echo ""
echo "  Node:    $THIS_NODE"
echo "  IP:      $THIS_IP"
echo "  Data:    $HIVE_DIR"
echo "  Daemon:  systemctl --user status $SERVICE_NAME"
echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
echo ""

# Show peers
if [ -f "$PEERS_FILE" ]; then
  PEER_COUNT=$(python3 -c "import json; d=json.load(open('$PEERS_FILE')); print(len(d.get('peers',[])))")
  if [ "$PEER_COUNT" -gt 0 ]; then
    echo "  Peers configured: $PEER_COUNT"
    echo "  Test sync:  cd $HIVE_DIR && ./hv sync now"
    echo ""
  fi
fi
