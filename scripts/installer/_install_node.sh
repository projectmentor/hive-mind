#!/usr/bin/env bash
# =============================================================================
# hive-mind install  —  scripts/installer/_install_node.sh
#
# Full node setup. Invoked by: hive-mind install
#
# Steps:
#   1.  Pre-flight checks (WSL2, systemd)
#   2.  Tailscale in WSL (install, start, auth, enable SSH)
#   3.  python3 + uv + requests
#   4.  Clone / update repo
#   5.  Node identity
#   6.  Peer configuration
#   7.  Init store.db
#   8.  Install systemd service
#   9.  Start daemon + smoke-test
#   10. Hermes memory plugin (best-effort)
#   11. PATH
#   12. Summary
#
# Architecture note:
#   Tailscale in WSL gets its own 100.x IP (separate machine on the tailnet).
#   No portproxy, no mirrored networking, no Win OpenSSH needed.
#   Sync daemon binds 0.0.0.0:9876 — reachable at the WSL Tailscale IP.
#   SSH between nodes: tailscale ssh user@<wsl-100.x>
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/projectmentor/hive-mind.git"
HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
SERVICE_NAME="hive-sync"
TOTAL=12

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }
step() { echo -e "\n${CYN}──── $* ${RST}"; }
die()  { echo -e "\n${RED}[FATAL]${RST} $*\n" >&2; exit 1; }
ask()  { echo -e "${BLD}[?>]${RST}  $*"; }

echo ""
echo -e "${BLD}╔══════════════════════════════════════╗${RST}"
echo -e "${BLD}║       hive-mind node installer       ║${RST}"
echo -e "${BLD}╚══════════════════════════════════════╝${RST}"
echo ""

# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Pre-flight
# ════════════════════════════════════════════════════════════════════════════
step "1/$TOTAL  Pre-flight checks"

grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null \
  || die "Must run inside WSL2 on Windows 11."

# ── detect init system ──────────────────────────────────────────────────────
INIT_SYSTEM="none"
if command -v systemctl &>/dev/null; then
  INIT_SYSTEM="systemctl"
  loginctl enable-linger "$USER" 2>/dev/null || true
elif [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]')" == "systemd" ]]; then
  INIT_SYSTEM="systemd"
  loginctl enable-linger "$USER" 2>/dev/null || true
elif [ -d /etc/init.d ]; then
  INIT_SYSTEM="initd"
fi

case "$INIT_SYSTEM" in
  systemctl) ok "Init system: systemctl" ;;
  systemd)   ok "Init system: systemd (no systemctl)" ;;
  initd)     ok "Init system: init.d" ;;
  none)
    warn "No known init system detected — daemon will run via @reboot cron as fallback."
    warn "For proper persistence, enable systemd in WSL:"
    warn "  sudo bash -c 'echo -e \"[boot]\nsystemd=true\" >> /etc/wsl.conf'"
    warn "  Then from Windows: wsl --shutdown, reopen WSL and re-run hive-mind install"
    ;;
esac

ok "WSL2 + systemd confirmed"

# ── check for stale portproxy on 9876 ───────────────────────────────────────
PORTPROXY=$(powershell.exe -NoProfile -Command \
  "netsh interface portproxy show all" 2>/dev/null | tr -d '\r' | grep 9876 || true)

if [[ -n "$PORTPROXY" ]]; then
  warn "Stale Windows portproxy rule found on port 9876 — this will block the sync daemon."
  warn "Staging removal script to C:\\Users\\Public\\hive-remove-portproxy.bat ..."

  WIN_USER=$(powershell.exe -NoProfile -Command \
    "[System.Environment]::UserName" 2>/dev/null | tr -d '\r')
  BAT="/mnt/c/Users/Public/hive-remove-portproxy.bat"

  cat > "$BAT" << 'WBAT'
@echo off
echo Removing stale HiveMind portproxy rules...
netsh interface portproxy delete v4tov4 listenport=9876 listenaddress=0.0.0.0 >nul 2>&1
netsh advfirewall firewall delete rule name="HiveMind Sync 9876" >nul 2>&1
echo Done. Verify:
netsh interface portproxy show all
pause
WBAT

  echo ""
  warn "══════════════════════════════════════════════════════"
  warn "  ACTION REQUIRED: run this as Administrator on Windows"
  warn ""
  warn "  Right-click Start -> Terminal (Admin), then run:"
  warn "    C:\\Users\\Public\\hive-remove-portproxy.bat"
  warn "══════════════════════════════════════════════════════"
  echo ""
  ask "Press Enter once the portproxy has been removed..."
  read -r

  # Verify it's gone
  PORTPROXY_AFTER=$(powershell.exe -NoProfile -Command \
    "netsh interface portproxy show all" 2>/dev/null | tr -d '\r' | grep 9876 || true)
  if [[ -n "$PORTPROXY_AFTER" ]]; then
    die "Portproxy rule still present on port 9876. Run the bat file as Administrator and try again."
  fi
  ok "Portproxy rule removed"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — Tailscale in WSL
# ════════════════════════════════════════════════════════════════════════════
step "2/$TOTAL  Tailscale (WSL)"

# Install if not present
if ! command -v tailscale &>/dev/null; then
  info "Installing Tailscale inside WSL..."
  curl -fsSL https://tailscale.com/install.sh | sh
  ok "Tailscale installed"
else
  ok "Tailscale already installed: $(tailscale version 2>/dev/null | head -1)"
fi

# Ensure tailscaled is running
if ! sudo systemctl is-active --quiet tailscaled 2>/dev/null; then
  info "Starting tailscaled..."
  sudo systemctl enable --quiet tailscaled
  sudo systemctl start tailscaled
  sleep 2
fi
ok "tailscaled running"

# Check auth state
TS_BACKEND=$(sudo tailscale status --json 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('BackendState','NeedsLogin'))" \
  2>/dev/null || echo "NeedsLogin")

if [[ "$TS_BACKEND" == "Running" ]]; then
  TS_IP=$(tailscale ip 2>/dev/null | grep '^100\.' | head -1 | tr -d '[:space:]')
  ok "Already authenticated: $TS_IP"
  # Ensure SSH is enabled (idempotent)
  sudo tailscale up --ssh --accept-routes 2>/dev/null || true
  ok "Tailscale SSH enabled"
else
  echo ""
  warn "Tailscale needs authentication."
  warn "A URL will appear below — open it in a browser to authenticate this node."
  warn "(Takes ~30 seconds)"
  echo ""
  sudo tailscale up --ssh --accept-routes
  echo ""
  TS_IP=$(tailscale ip 2>/dev/null | grep '^100\.' | head -1 | tr -d '[:space:]')
  [[ -n "$TS_IP" ]] || die "Auth completed but no 100.x IP found. Run: tailscale ip"
  ok "Authenticated: $TS_IP"
  ok "Tailscale SSH enabled"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — python3 + uv + requests
# ════════════════════════════════════════════════════════════════════════════
step "3/$TOTAL  Python dependencies"

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

python3 -c "import requests" 2>/dev/null || {
  info "Installing requests..."
  uv pip install --system requests
}
ok "requests available"

# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — Clone / update repo
# ════════════════════════════════════════════════════════════════════════════
step "4/$TOTAL  Repository"

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
# STEP 5 — Node identity
# ════════════════════════════════════════════════════════════════════════════
step "5/$TOTAL  Node identity"

THIS_IP="$TS_IP"
THIS_NODE=$(hostname)
ok "This node: $THIS_NODE @ $THIS_IP"

# ════════════════════════════════════════════════════════════════════════════
# STEP 6 — Peer configuration
# ════════════════════════════════════════════════════════════════════════════
step "6/$TOTAL  Peer configuration"

PEERS_FILE="$HIVE_DIR/.peers.json"

if [ -f "$PEERS_FILE" ]; then
  ok ".peers.json already exists — skipping peer prompt"
  echo "  (delete $PEERS_FILE and re-run to reconfigure peers)"
else
  echo ""
  echo "  Enter the Tailscale IPs of your peer nodes."
  echo "  Use the WSL Tailscale IP for each peer (run: tailscale ip on the peer)."
  echo "  Comma-separated for multiple peers, e.g.: 100.114.200.119"
  echo ""
  echo "  First node / no peers yet? Just press Enter — you can add peers later"
  echo "  by editing $HIVE_DIR/.peers.json and running: hive-mind install"
  echo ""
  ask "Peer WSL Tailscale IPs (or Enter to skip): "
  read -r PEER_INPUT

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
step "7/$TOTAL  Database initialisation"

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
# STEP 8 — Service / daemon persistence
# ════════════════════════════════════════════════════════════════════════════
step "8/$TOTAL  Systemd service"

if [[ "$INIT_SYSTEM" == "systemctl" || "$INIT_SYSTEM" == "systemd" ]]; then
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

elif [[ "$INIT_SYSTEM" == "initd" ]]; then
  INITD_SCRIPT="/etc/init.d/$SERVICE_NAME"
  sudo tee "$INITD_SCRIPT" > /dev/null << INITD
#!/bin/sh
### BEGIN INIT INFO
# Provides:          $SERVICE_NAME
# Required-Start:    \$network
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Description:       Hive Mind sync daemon
### END INIT INFO
HIVE_HOME=$HIVE_DIR
case "\$1" in
  start) nohup /usr/bin/python3 $HIVE_DIR/sync_daemon.py >> /tmp/hive-sync.log 2>&1 & ;;
  stop)  pkill -f sync_daemon.py || true ;;
  restart) \$0 stop; \$0 start ;;
esac
INITD
  sudo chmod +x "$INITD_SCRIPT"
  sudo update-rc.d "$SERVICE_NAME" defaults 2>/dev/null || true
  ok "init.d service installed: $INITD_SCRIPT"

else
  CRON_LINE="@reboot HIVE_HOME=$HIVE_DIR /usr/bin/python3 $HIVE_DIR/sync_daemon.py >> /tmp/hive-sync.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v "sync_daemon"; echo "$CRON_LINE" ) | crontab -
  ok "@reboot cron entry installed (fallback)"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 9 — Start sync daemon
# ════════════════════════════════════════════════════════════════════════════
step "9/$TOTAL  Start sync daemon"

if [[ "$INIT_SYSTEM" == "systemctl" || "$INIT_SYSTEM" == "systemd" ]]; then
  systemctl --user restart "$SERVICE_NAME"
elif [[ "$INIT_SYSTEM" == "initd" ]]; then
  sudo /etc/init.d/$SERVICE_NAME restart
else
  pkill -f sync_daemon.py 2>/dev/null || true
  nohup python3 "$HIVE_DIR/sync_daemon.py" >> /tmp/hive-sync.log 2>&1 &
fi
sleep 2

HELLO=$(curl -sf "http://127.0.0.1:9876/sync/hello" 2>/dev/null) && {
  ok "Daemon responding: $(echo "$HELLO" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("node_id","?"), "—", d.get("journal_entries","?"), "entries")')"
} || {
  warn "Daemon not yet responding on :9876. Check: journalctl --user -u $SERVICE_NAME -n 20"
}

# ════════════════════════════════════════════════════════════════════════════
# STEP 10 — Hermes memory plugin (best-effort)
# ════════════════════════════════════════════════════════════════════════════
step "10/$TOTAL  Hermes integration"

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
step "11/$TOTAL  PATH"

BIN_DIR="$HOME/.local/bin"
if ! grep -q 'local/bin' "$HOME/.bashrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi
ok "~/.local/bin in PATH"

# ════════════════════════════════════════════════════════════════════════════
# STEP 12 — Summary
# ════════════════════════════════════════════════════════════════════════════
step "12/$TOTAL  Done"

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
echo "  SSH to this node from any peer:"
echo "    tailscale ssh ${USER}@${THIS_IP}"
echo ""

if [ -f "$PEERS_FILE" ]; then
  PEER_COUNT=$(python3 -c "import json; d=json.load(open('$PEERS_FILE')); print(len(d.get('peers',[])))")
  if [ "$PEER_COUNT" -gt 0 ]; then
    echo "  Peers configured: $PEER_COUNT"
    echo "  Test sync:  cd $HIVE_DIR && ./hv sync now"
    echo ""
  fi
fi
