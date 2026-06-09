#!/usr/bin/env bash
# =============================================================================
# hive-mind install  —  scripts/installer/_install_node.sh
#
# Full device setup. Invoked by: hive-mind install
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
TOTAL=13

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
echo -e "${BLD}║         hive-mind installer          ║${RST}"
echo -e "${BLD}╚══════════════════════════════════════╝${RST}"
echo ""

# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Pre-flight
# ════════════════════════════════════════════════════════════════════════════
step "1/$TOTAL  Pre-flight checks"

# HiveMind runs on Linux. WSL2 is a Linux kernel, so native Linux works the same;
# we only branch on the handful of Windows-interop steps below (gated on IS_WSL).
if [ "$(uname -s)" != "Linux" ]; then
  die "HiveMind installs on Linux (native, or WSL2 on Windows 11). This system is not Linux."
fi
IS_WSL=""
grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null && IS_WSL=1

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
    if [ -n "$IS_WSL" ]; then
      warn "For proper persistence, enable systemd in WSL:"
      warn "  sudo bash -c 'echo -e \"[boot]\nsystemd=true\" >> /etc/wsl.conf'"
      warn "  Then from Windows: wsl --shutdown, reopen WSL and re-run hive-mind install"
    fi
    ;;
esac

ok "Pre-flight OK ($([ -n "$IS_WSL" ] && echo 'WSL2' || echo 'native Linux'))"

# ── Already installed AND running? Offer the lighter path before redoing the work. ──────────
# Re-running install is safe and idempotent — it KEEPS this device's identity (STEP 5 reuses an
# existing .device-key). But if the device is already set up and the sync daemon is live, the
# user most likely wants `hive-mind update`, not all 13 steps again — so surface that and let
# them bail. Non-blocking on a non-interactive shell (automation re-runs proceed).
_existing_device=""
if [ -x "$HIVE_DIR/hv" ]; then
  _existing_device="$(cd "$HIVE_DIR" && ./hv key show 2>/dev/null | awk '/^device_id:/{print $2}')"
fi
_daemon_live=""
if [ "$INIT_SYSTEM" = "systemctl" ] && systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  _daemon_live=1
fi
if [ -n "$_existing_device" ] && [ -n "$_daemon_live" ]; then
  _existing_hive="$(cd "$HIVE_DIR" && ./hv owner show 2>/dev/null | awk '/hive_id:/{print $2}')"
  warn "hive-mind is already installed and running on this device:"
  echo  "        device: $_existing_device${_existing_hive:+   hive: $_existing_hive}"
  echo  "        • Just want the latest code + a daemon restart?  →  hive-mind update"
  echo  "        • Want a completely fresh start (NEW identity)?  →  hive-mind uninstall, then install"
  echo  "        (Re-running install is safe and keeps this device's identity.)"
  if [ -t 0 ]; then
    printf "        Continue with a full re-install anyway? [y/N] "
    read -r _reinstall_ans
    case "$_reinstall_ans" in
      y|Y|yes|YES) ok "Continuing with full re-install." ;;
      *) info "Aborted. Tip: run \`hive-mind update\` to just pull + restart."; exit 0 ;;
    esac
  else
    warn "Non-interactive shell — continuing with the (idempotent) re-install."
  fi
fi

# ── (WSL only) stale Windows-host portproxy on 9876 ─────────────────────────
# Legacy host-Tailscale + portproxy setups can squat :9876; clean them via
# Windows interop. Irrelevant on native Linux (no powershell.exe), so skip.
if [ -n "$IS_WSL" ]; then
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
fi  # end IS_WSL portproxy check

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
  warn "A URL will appear below — open it in a browser to authenticate this device."
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

# Authenticity: confirm the cloned code is the official, untampered HiveMind.
VERIFY_OUT="$("$HIVE_DIR/hv" verify 2>/dev/null || true)"
if printf '%s' "$VERIFY_OUT" | grep -q '⚠'; then
  warn "Authenticity check flagged a problem with this repo:"
  printf '%s\n' "$VERIFY_OUT" | sed 's/^/    /'
  warn "If you did not intend a fork or mirror, STOP and reinstall from $REPO_URL"
else
  ok "$(printf '%s' "$VERIFY_OUT" | head -1)"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 5 — Node identity
# ════════════════════════════════════════════════════════════════════════════
step "5/$TOTAL  Node identity"

cd "$HIVE_DIR"
THIS_IP="$TS_IP"
THIS_NODE=$(hostname)

# Resume a preserved identity (from a prior `uninstall --keep-identity` / `--keep-hive`) BEFORE
# minting, so a reinstall keeps the same device_id — and the owner's prior admission (keyed by
# device_id in the synced journal) still applies, with no re-admit.
_IDLIB="$HIVE_DIR/scripts/installer/_identity.sh"
[ -f "$_IDLIB" ] && . "$_IDLIB"
if command -v keep_identity_can_restore >/dev/null 2>&1 && keep_identity_can_restore "$HIVE_DIR"; then
  _stashed="$(keep_identity_stashed_id)"
  info "Found a preserved device identity${_stashed:+ ($_stashed)} from a previous install."
  _resume="y"
  if [ -t 0 ]; then printf "  Resume it (keeps your admission — no re-admit)? [Y/n] "; read -r _resume; fi
  case "${_resume:-y}" in
    n|N|no|NO) info "Starting fresh — a new device identity will be minted." ;;
    *) keep_identity_restore "$HIVE_DIR" && ok "Resumed device identity${_stashed:+ $_stashed}." ;;
  esac
fi

# Mint this device's Ed25519 device key. On a fresh install the journal is still empty, so this
# succeeds; the device then identifies by an unforgeable key fingerprint, not a hostname.
if ./hv key show 2>/dev/null | grep -q '^device_id:'; then
  THIS_DEVICE=$(./hv key show 2>/dev/null | awk '/^device_id:/{print $2}')
  ok "Device key present: $THIS_DEVICE"
elif ./hv key init >/dev/null 2>&1; then
  THIS_DEVICE=$(./hv key show 2>/dev/null | awk '/^device_id:/{print $2}')
  ok "Device key minted: $THIS_DEVICE"
else
  warn "Could not mint a device key (existing journal?). Continuing with hostname identity."
  THIS_DEVICE="$THIS_NODE"
fi
ok "This device: $THIS_NODE ($THIS_DEVICE) @ $THIS_IP"

# ════════════════════════════════════════════════════════════════════════════
# STEP 6 — Bootstrap or join a hive
# ════════════════════════════════════════════════════════════════════════════
step "6/$TOTAL  Bootstrap or join a hive"

PEERS_FILE="$HIVE_DIR/.peers.json"

if [ -f "$PEERS_FILE" ] && ./hv owner show 2>/dev/null | grep -q '^owner:'; then
  ok "Already part of a hive ($(./hv owner show 2>/dev/null | awk '/hive_id:/{print $2}')) — skipping."
  echo "  (delete $PEERS_FILE and re-run to reconfigure)"
else
  info "Scanning the tailnet for existing hives..."
  HIVES_TMP=$(mktemp)
  ./hv discover --format json >"$HIVES_TMP" 2>/dev/null &
  SCAN_PID=$!
  SP='|/-\'; k=0
  while kill -0 "$SCAN_PID" 2>/dev/null; do
    k=$(((k + 1) % 4)); printf "\r  scanning %s" "${SP:$k:1}"; sleep 0.2
  done
  wait "$SCAN_PID" 2>/dev/null; printf "\r            \r"
  HIVES_JSON=$(cat "$HIVES_TMP" 2>/dev/null); rm -f "$HIVES_TMP"
  [ -n "$HIVES_JSON" ] || HIVES_JSON='[]'
  HIVE_COUNT=$(printf '%s' "$HIVES_JSON" | python3 -c "import sys,json;print(len(json.loads(sys.stdin.read() or '[]')))" 2>/dev/null || echo 0)

  CHOICE="n"
  if [ "${HIVE_COUNT:-0}" -gt 0 ]; then
    echo ""
    echo "  Found $HIVE_COUNT hive(s) on your tailnet:"
    printf '%s' "$HIVES_JSON" | python3 -c "import sys,json
for i,h in enumerate(json.loads(sys.stdin.read())):
    print(f\"    [{i+1}] {h['label'] or h['hive_id']}  (owner {h['owner_id']}, {h['node_count']} node(s), at {h['ip']}:{h['port']})\")"
    echo "    [n] Start your OWN new hive (you become the owner)"
    echo ""
    ask "Join which hive? (number, or 'n' for a new hive): "
    read -r CHOICE
  else
    ok "No existing hive found on the tailnet."
  fi

  if [ "$CHOICE" = "n" ] || [ "$CHOICE" = "N" ] || [ -z "$CHOICE" ]; then
    # ── Bootstrap: become the owner (queen bee) ──
    ask "Your name (principal tag for your devices): "
    read -r PRINCIPAL; PRINCIPAL="${PRINCIPAL:-$USER}"
    python3 -c "import json,sys;json.dump({'self':sys.argv[1],'bind':'0.0.0.0','port':9876,'peers':[]},open(sys.argv[2],'w'),indent=2)" "$THIS_DEVICE" "$PEERS_FILE"
    ./hv owner init >/dev/null && ok "New hive created — you are the queen bee!"
    ./hv admit "$THIS_DEVICE" --principal "$PRINCIPAL" >/dev/null && ok "Admitted this device (principal: $PRINCIPAL)."
    ok "hive_id: $(./hv owner show 2>/dev/null | awk '/hive_id:/{print $2}')"
  else
    # ── Join: configure the chosen hive's node as a peer, sync, request admission ──
    PEER=$(printf '%s' "$HIVES_JSON" | python3 -c "import sys,json;hs=json.loads(sys.stdin.read());i=int('$CHOICE')-1;h=hs[i];print(h['ip'],h['port'])" 2>/dev/null)
    PEER_IP=$(echo "$PEER" | awk '{print $1}'); PEER_PORT=$(echo "$PEER" | awk '{print $2}')
    [ -n "$PEER_IP" ] || die "Invalid choice '$CHOICE'."
    python3 -c "import json,sys;json.dump({'self':sys.argv[1],'bind':'0.0.0.0','port':9876,'peers':[{'id':sys.argv[2].replace('.','-'),'url':'http://'+sys.argv[2]+':'+sys.argv[3]}]},open(sys.argv[4],'w'),indent=2)" "$THIS_DEVICE" "$PEER_IP" "$PEER_PORT" "$PEERS_FILE"
    ok "Peer configured: $PEER_IP:$PEER_PORT"
    info "Syncing the hive (pulling its journal + owner declaration)..."
    ./hv sync now >/dev/null 2>&1 || true
    ask "Your name (the principal you'd like to be admitted as): "
    read -r PRINCIPAL; PRINCIPAL="${PRINCIPAL:-$USER}"
    ./hv join --principal "$PRINCIPAL" 2>/dev/null || true
    warn "You're syncing the hive but NOT yet admitted — your writes won't count until the owner admits you."
    echo "  Ask the hive's owner to run:  hv admit $THIS_DEVICE --principal $PRINCIPAL"
  fi
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
  ok "Hermes auto-reorients on boot (system_prompt_block). For the full capture-and-audit loop,"
  echo "     have Hermes read $HIVE_DIR/docs/AGENT_INTEGRATION.md and self-integrate (it self-updates via 'hv version')."
else
  warn "Hermes not installed — skipping plugin wiring"
  warn "  When Hermes is installed, run: hermes config set memory.provider hive-mind"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 11 — Claude Code integration (skill + telemetry/nudge hooks, best-effort)
# ════════════════════════════════════════════════════════════════════════════
step "11/$TOTAL  Claude Code integration"

if [ -d "$HOME/.claude" ] || command -v claude &>/dev/null; then
  if bash "$HIVE_DIR/integrations/claude-code/install.sh" >/dev/null 2>&1; then
    ok "Claude Code skill + telemetry/nudge hooks wired (~/.claude/settings.json)"
  else
    warn "Claude Code integration failed — run manually: bash $HIVE_DIR/integrations/claude-code/install.sh"
  fi
  echo "     Integration spec (self-updating): $HIVE_DIR/docs/AGENT_INTEGRATION.md"
else
  warn "Claude Code (~/.claude) not found — skipping (later: bash $HIVE_DIR/integrations/claude-code/install.sh)"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 12 — PATH persistence
# ════════════════════════════════════════════════════════════════════════════
step "12/$TOTAL  PATH"

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
if ! grep -q 'local/bin' "$HOME/.bashrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi
# Put `hv` on PATH so the CLI works from anywhere. hv resolves its own real directory
# (Path(__file__).resolve()) for imports, so a symlink is fine.
ln -sf "$HIVE_DIR/hv" "$BIN_DIR/hv"
ok "~/.local/bin in PATH; hv linked ($BIN_DIR/hv)"

# ── Optional: security alerts + project update emails ───────────────────────
echo ""
echo "  Leave your email to get critical security alerts. Unsubscribe anytime."
ask "Email for critical security alerts and updates (optional, press Enter to skip): "
read -r SEC_EMAIL
case "$SEC_EMAIL" in
  "") : ;;
  *@*.*)
    SUB_BODY=$(python3 -c "import json,sys;print(json.dumps({'email':sys.argv[1].strip().lower(),'source':'installer-security'}))" "$SEC_EMAIL")
    if curl -fsS -m 10 -X POST "https://hivemind.projectmentor.org/api/subscribe" \
         -H "Content-Type: application/json" -d "$SUB_BODY" >/dev/null 2>&1; then
      ok "You're on the list. We'll email critical security alerts and updates. Unsubscribe anytime at hivemind.projectmentor.org."
    else
      warn "Could not reach the update service — skipped. You can subscribe later at hivemind.projectmentor.org."
    fi ;;
  *) warn "That doesn't look like an email — skipped." ;;
esac

# ════════════════════════════════════════════════════════════════════════════
# STEP 13 — Summary
# ════════════════════════════════════════════════════════════════════════════
step "13/$TOTAL  Done"

echo ""
echo -e "${GRN}${BLD}╔══════════════════════════════════════╗${RST}"
echo -e "${GRN}${BLD}║         Hive setup complete          ║${RST}"
echo -e "${GRN}${BLD}╚══════════════════════════════════════╝${RST}"
echo ""
echo "  Device:  $THIS_NODE"
echo "  IP:      $THIS_IP"
echo "  Data:    $HIVE_DIR"
echo "  Daemon:  systemctl --user status $SERVICE_NAME"
echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
echo "  Try:     hv stats   (health check: hv doctor)   — reload your shell first: source ~/.bashrc"
echo ""
echo "  SSH to this device from any peer:"
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
