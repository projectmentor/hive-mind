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

# Did the invoking shell already have ~/.local/bin on PATH? Capture NOW, before any
# PATH munging below — this decides whether the summary needs a `source ~/.bashrc` hint.
# (A child process can't reload the parent shell; the hint only matters when it's absent.)
case ":$PATH:" in *":$HOME/.local/bin:"*) BIN_ON_PATH=1 ;; *) BIN_ON_PATH="" ;; esac

# Cross-platform supervisor seam: systemd units on Linux/WSL, launchd on macOS.
# Sources _units.sh (the systemd writer) and, on macOS, _launchd.sh — and exposes
# service_install / service_restart / service_is_active + the launchd_* backend.
_SVCLIB="$HIVE_DIR/scripts/installer/_service.sh"
if [ -f "$_SVCLIB" ]; then . "$_SVCLIB"; fi

# Safe username. Termux does NOT export $USER, and this script runs `set -u`, so a
# bare $USER (principal default, the SSH summary line) aborts with "unbound variable".
# Resolve it once, with fallbacks that exist on Termux too.
THIS_USER="${USER:-$(id -un 2>/dev/null || whoami 2>/dev/null || echo user)}"

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

# HiveMind runs on Linux (native or WSL2), macOS, and Android (Termux). WSL2 is a
# Linux kernel, so native Linux works the same; macOS uses launchd; Android uses
# termux-services/runit. Termux reports `uname -s`=Linux, so it is matched FIRST
# (via `uname -o`=Android / $TERMUX_VERSION / the Termux data dir) before the WSL
# probe. We branch on the Windows-interop steps (IS_WSL) and the supervisor.
IS_WSL=""; IS_MAC=""; IS_ANDROID=""
case "$(uname -s)" in
  Linux)
    if [ "$(uname -o 2>/dev/null)" = "Android" ] || [ -n "${TERMUX_VERSION:-}" ] \
       || [ -d /data/data/com.termux ]; then
      IS_ANDROID=1
    elif grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null; then
      IS_WSL=1
    fi ;;
  Darwin) IS_MAC=1 ;;
  *)      die "HiveMind installs on Linux (native or WSL2), macOS, or Android (Termux). This system ($(uname -s)) is none." ;;
esac

# ── detect supervisor / init system ─────────────────────────────────────────
# Android FIRST: Termux has no systemctl / systemd PID 1 / loginctl, so the probes
# below would error or mis-fire there.
INIT_SYSTEM="none"
if [ -n "$IS_ANDROID" ]; then
  INIT_SYSTEM="runit"
elif [ -n "$IS_MAC" ]; then
  INIT_SYSTEM="launchd"
elif command -v systemctl &>/dev/null; then
  INIT_SYSTEM="systemctl"
  loginctl enable-linger "$THIS_USER" 2>/dev/null || true
elif [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]')" == "systemd" ]]; then
  INIT_SYSTEM="systemd"
  loginctl enable-linger "$THIS_USER" 2>/dev/null || true
elif [ -d /etc/init.d ]; then
  INIT_SYSTEM="initd"
fi

case "$INIT_SYSTEM" in
  launchd)   ok "Init system: launchd (macOS)" ;;
  runit)     ok "Init system: termux-services / runit (Android)" ;;
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

ok "Pre-flight OK ($([ -n "$IS_MAC" ] && echo 'macOS' || { [ -n "$IS_ANDROID" ] && echo 'Android (Termux)' || { [ -n "$IS_WSL" ] && echo 'WSL2' || echo 'native Linux'; }; }))"

# ── Android: ensure termux-services (runit) is present + nudge Termux:Boot ───
# Termux has no systemd; termux-services provides `sv`/runsvdir. Termux:Boot (a
# separate F-Droid app) gives start-on-reboot — the CLI can only prompt for it.
if [ -n "$IS_ANDROID" ]; then
  if ! command -v sv &>/dev/null; then
    info "Installing termux-services (runit supervisor)..."
    pkg install -y termux-services 2>/dev/null || warn "pkg install termux-services failed — will fall back to a nohup keep-alive."
  fi
  # Bring the supervisor tree up for THIS session if a login shell hasn't already.
  if ! pgrep -x runsvdir >/dev/null 2>&1; then
    . "$PREFIX/etc/profile.d/start-services.sh" 2>/dev/null || true
  fi
  warn "For start-on-reboot: install the Termux:Boot app from F-Droid, open it once,"
  warn "then disable battery optimisation for Termux + Termux:Boot. (Keep-alive while"
  warn "running works without it.)"
fi

# ── Already installed AND running? Offer the lighter path before redoing the work. ──────────
# Re-running install is safe and idempotent — it KEEPS this device's identity (STEP 5 reuses an
# existing .device-key). But if the device is already set up and the sync daemon is live, the
# user most likely wants `hive-mind update`, not all 13 steps again — so surface that and let
# them bail. Non-blocking on a non-interactive shell (automation re-runs proceed).
_existing_device=""
if [ -x "$HIVE_DIR/hv" ]; then
  _existing_device="$(cd "$HIVE_DIR" && ./hv config identity show 2>/dev/null | awk '/^device_id:/{print $2}')"
fi
_daemon_live=""
if command -v service_is_active >/dev/null 2>&1 && service_is_active "$SERVICE_NAME" 2>/dev/null; then
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
# STEP 2 — Tailscale
# ════════════════════════════════════════════════════════════════════════════
step "2/$TOTAL  Tailscale"

if [ -n "$IS_ANDROID" ]; then
  # Android: Tailscale is the VPN APP (userspace), not a Termux CLI — the installer
  # cannot run `tailscale up`. The 100.x address lives on an app-owned tun that may
  # not even be visible inside Termux, so discovery is best-effort. TS_IP is only
  # used for display + the `self` label; joining points at the PEER and sync is
  # outbound, so a missing IP is non-fatal.
  TS_IP="$( { command -v ip >/dev/null 2>&1 && ip -4 addr 2>/dev/null; \
              command -v ifconfig >/dev/null 2>&1 && ifconfig 2>/dev/null; } \
            | grep -oE '100\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 )"
  if [ -n "$TS_IP" ]; then
    ok "Tailscale IP (from VPN tun): $TS_IP"
  else
    warn "Can't read the Tailscale IP from Termux (the VPN tun is owned by the app)."
    warn "Open the Tailscale app and confirm it shows Connected."
    if [ -t 0 ]; then
      ask "Paste this device's Tailscale 100.x IP (optional, Enter to skip): "
      read -r TS_IP
    fi
    TS_IP="${TS_IP:-}"
  fi

elif [ -n "$IS_MAC" ]; then
  # macOS: Tailscale ships as a GUI app (App Store / standalone) OR the brew CLI.
  # Either way the app/`brew services` owns tailscaled — never `sudo systemctl` it.
  TS_BIN="$(_resolve_tailscale || true)"
  if [ -z "$TS_BIN" ] && command -v brew &>/dev/null; then
    info "Installing Tailscale via Homebrew..."
    brew install tailscale && TS_BIN="$(_resolve_tailscale || true)"
  fi
  if [ -z "$TS_BIN" ]; then
    warn "Tailscale not found. Install the app from https://tailscale.com/download/mac"
    warn "(or: brew install tailscale), authenticate, then re-run 'hive-mind install'."
    TS_IP=""
  else
    ok "Tailscale present: $TS_BIN"
    "$TS_BIN" up --ssh --accept-routes 2>/dev/null \
      || warn "Run '$TS_BIN up --ssh' and authenticate if the daemon is not yet up."
    TS_IP=$("$TS_BIN" ip 2>/dev/null | grep '^100\.' | head -1 | tr -d '[:space:]')
    [ -n "$TS_IP" ] && ok "Tailscale IP: $TS_IP" || warn "No 100.x IP yet — authenticate Tailscale, then re-run."
  fi

else
# ── Linux / WSL ──────────────────────────────────────────────────────────────
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
fi  # end Linux/WSL Tailscale branch

# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — python3 + uv + requests
# ════════════════════════════════════════════════════════════════════════════
step "3/$TOTAL  Python dependencies"

if [ -n "$IS_ANDROID" ]; then
  # Termux: `pkg` not apt, no sudo, no root. The core only needs `requests`, and
  # `uv` has no reliable Termux/aarch64 wheel — so install python/git via pkg and
  # requests via plain pip, skipping uv entirely.
  command -v python3 &>/dev/null || { info "Installing python (pkg)..."; pkg install -y python; }
  command -v git     &>/dev/null || { info "Installing git (pkg)...";    pkg install -y git; }
  ok "python3 $(python3 --version)"
  python3 -c "import requests" 2>/dev/null || {
    info "Installing requests (pip)..."
    pip install requests
  }
  ok "requests available (Termux, no uv)"
else
  command -v python3 &>/dev/null || {
    info "Installing python3..."
    if [ -n "$IS_MAC" ]; then
      command -v brew &>/dev/null && brew install python \
        || die "Python 3 not found. Install it (brew install python) and re-run."
    else
      sudo apt-get update -qq && sudo apt-get install -y -qq python3
    fi
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
fi

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
# Human label for this node. A GENERIC hostname (linux/localhost/blank — common on containers and
# fresh WSL2) is not unique, so two such boxes would both register as "linux" in the hive. Suffix
# it with a short stable machine id. (hv's NODE_LABEL applies the same guard at runtime, preferring
# the device fingerprint once a key is minted — this early value is for the installer's display.)
THIS_NODE=$(hostname 2>/dev/null || echo node)
case "$(printf '%s' "$THIS_NODE" | tr '[:upper:]' '[:lower:]')" in
  ""|linux|localhost|localhost.localdomain|ubuntu|debian|kali|raspberrypi|archlinux)
    _mid=""
    for _f in /etc/machine-id /var/lib/dbus/machine-id; do
      [ -r "$_f" ] && _mid="$(cut -c1-6 < "$_f" 2>/dev/null)" && [ -n "$_mid" ] && break
    done
    [ -n "$_mid" ] && THIS_NODE="${THIS_NODE:-node}-$_mid"
    ;;
esac

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
if ./hv config identity show 2>/dev/null | grep -q '^device_id:'; then
  THIS_DEVICE=$(./hv config identity show 2>/dev/null | awk '/^device_id:/{print $2}')
  ok "Device key present: $THIS_DEVICE"
elif ./hv config identity init >/dev/null 2>&1; then
  THIS_DEVICE=$(./hv config identity show 2>/dev/null | awk '/^device_id:/{print $2}')
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

  CHOICE="n"; SEED_HOST=""; SEED_PORT="9876"; JOIN_ATTEMPTED=""
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
    # Nothing auto-discovered. On Android this is EXPECTED and permanent: `hv discover`
    # enumerates the tailnet via the `tailscale` CLI, which Termux doesn't have (Tailscale
    # is the phone's VPN app). Rather than silently start a brand-new hive (the wrong
    # default for someone who meant to JOIN), offer to join by pasting an address — you
    # only need ONE node of an existing hive to get in.
    if [ -n "$IS_ANDROID" ]; then
      info "No hive auto-discovered. (On Android, discovery can't scan the tailnet — Tailscale is the app, with no CLI in Termux.)"
    else
      info "No hive auto-discovered on the tailnet."
    fi
    if [ -t 0 ]; then
      echo "  To JOIN your existing hive, paste its address below."
      echo -e "  Get it by running  ${BLD}hive-mind invite${RST}  on a device that's already in the hive."
      echo "  (Or just press Enter to start a NEW hive on this device.)"
      _tries=0
      while [ "$_tries" -lt 3 ]; do
        ask "Hive address to join (paste, or Enter for a new hive): "
        read -r _ADDR
        [ -z "$_ADDR" ] && break        # explicit Enter → bootstrap a new hive
        JOIN_ATTEMPTED=1
        # Forgiving parse: accept IP, ip:port, name@ip:port, http://ip:port/..., or a host.
        _PARSED=$(python3 -c "
import sys,re
s=sys.argv[1].strip()
s=re.sub(r'^[a-zA-Z][a-zA-Z0-9+.-]*://','',s)   # strip scheme
if '@' in s: s=s.split('@',1)[1]                # strip user@
s=s.split('/',1)[0]                             # strip path
host,port=s,'9876'
if ':' in s: host,port=s.rsplit(':',1)
host=host.strip(); port=(port.strip() or '9876')
print(f'{host} {port}' if host and re.match(r'^[A-Za-z0-9._-]+\$',host) and port.isdigit() else '')
" "$_ADDR" 2>/dev/null)
        SEED_HOST=$(echo "$_PARSED" | awk '{print $1}'); SEED_PORT=$(echo "$_PARSED" | awk '{print $2}')
        SEED_PORT="${SEED_PORT:-9876}"
        if [ -z "$SEED_HOST" ]; then
          warn "Couldn't read an address from that. Paste just the line that 'hive-mind invite' prints."
          _tries=$((_tries + 1)); continue
        fi
        info "Checking for a hive at $SEED_HOST:$SEED_PORT ..."
        if curl -sf -m 8 "http://$SEED_HOST:${SEED_PORT}/hive/info" >/dev/null 2>&1; then
          ok "Found a hive at $SEED_HOST:$SEED_PORT."
          break
        fi
        warn "No hive answered at $SEED_HOST:$SEED_PORT — that device may be off, or it's a typo. Try again."
        _tries=$((_tries + 1))   # keep SEED_HOST as the last candidate in case it's just temporarily down
      done
    fi
  fi

  # Resolve the JOIN target: a pasted seed address wins; else a numbered discover choice.
  PEER_IP=""; PEER_PORT="9876"
  if [ -n "$SEED_HOST" ]; then
    PEER_IP="$SEED_HOST"; PEER_PORT="$SEED_PORT"
  elif [ "$CHOICE" != "n" ] && [ "$CHOICE" != "N" ] && [ -n "$CHOICE" ]; then
    PEER=$(printf '%s' "$HIVES_JSON" | python3 -c "import sys,json;hs=json.loads(sys.stdin.read());i=int('$CHOICE')-1;h=hs[i];print(h['ip'],h['port'])" 2>/dev/null)
    PEER_IP=$(echo "$PEER" | awk '{print $1}'); PEER_PORT=$(echo "$PEER" | awk '{print $2}')
    [ -n "$PEER_IP" ] || die "Invalid choice '$CHOICE'."
  fi

  # A failed join attempt must NOT silently fall through to creating a new hive — that's
  # exactly the trap that put a phone on its own island. Only an explicit Enter bootstraps.
  if [ -z "$PEER_IP" ] && [ -n "$JOIN_ATTEMPTED" ]; then
    die "Couldn't read a hive address to join. Run 'hive-mind invite' on a device already in your hive to get the exact line to paste, then re-run 'hive-mind install'. (Not starting a new hive, since you meant to join one.)"
  fi

  if [ -z "$PEER_IP" ]; then
    # ── Bootstrap: become the owner (queen bee) ──
    ask "Your name (principal tag for your devices): "
    read -r PRINCIPAL; PRINCIPAL="${PRINCIPAL:-$THIS_USER}"
    python3 -c "import json,sys;json.dump({'self':sys.argv[1],'bind':'0.0.0.0','port':9876,'peers':[]},open(sys.argv[2],'w'),indent=2)" "$THIS_DEVICE" "$PEERS_FILE"
    ./hv owner init >/dev/null && ok "New hive created — you are the queen bee!"
    ./hv group admit "$THIS_DEVICE" --principal "$PRINCIPAL" >/dev/null && ok "Admitted this device (principal: $PRINCIPAL)."
    ok "hive_id: $(./hv owner show 2>/dev/null | awk '/hive_id:/{print $2}')"
    # Surface the invite right where a hive is born, so the owner knows exactly what to
    # paste on their other devices to add them — no "how do I get the address?" round-trip.
    if [ -f "$HIVE_DIR/scripts/installer/_invite.sh" ]; then
      echo ""
      HIVE_DIR="$HIVE_DIR" bash "$HIVE_DIR/scripts/installer/_invite.sh" 2>/dev/null || true
    fi
  else
    # ── Join: configure the chosen/pasted hive node as a peer, sync, request admission ──
    python3 -c "import json,sys;json.dump({'self':sys.argv[1],'bind':'0.0.0.0','port':9876,'peers':[{'id':sys.argv[2].replace('.','-'),'url':'http://'+sys.argv[2]+':'+sys.argv[3]}]},open(sys.argv[4],'w'),indent=2)" "$THIS_DEVICE" "$PEER_IP" "$PEER_PORT" "$PEERS_FILE"
    ok "Peer configured: $PEER_IP:$PEER_PORT"
    info "Syncing the hive (pulling its journal + owner declaration)..."
    ./hv sync now >/dev/null 2>&1 || true
    ask "Your name (the principal you'd like to be admitted as): "
    read -r PRINCIPAL; PRINCIPAL="${PRINCIPAL:-$THIS_USER}"
    ./hv join --principal "$PRINCIPAL" 2>/dev/null || true
    warn "You're syncing the hive but NOT yet admitted — your writes won't count until the owner admits you."
    echo "  Ask the hive's owner to run:  hv group admit $THIS_DEVICE --principal $PRINCIPAL"
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
step "8/$TOTAL  Daemon supervision"

if [[ "$INIT_SYSTEM" == "launchd" ]]; then
  # macOS launchd agents: hive-sync (KeepAlive) + hive-doctor (15-min self-heal).
  launchd_install "$HIVE_DIR" "$SERVICE_NAME" \
    && ok "launchd agents installed: hive-sync (KeepAlive) + hive-doctor (15-min self-heal)" \
    || warn "launchd install reported a problem — check 'launchctl print gui/\$(id -u)/com.projectmentor.hive-sync'."

elif [[ "$INIT_SYSTEM" == "runit" ]]; then
  # Android/Termux: runit services hive-sync (runsvdir respawn) + hive-doctor
  # (15-min loop), plus a Termux:Boot entrypoint. See scripts/platform/termux/_runit.sh.
  if command -v runit_install >/dev/null 2>&1 && command -v sv >/dev/null 2>&1; then
    runit_install "$HIVE_DIR" "$SERVICE_NAME" \
      && ok "runit services installed: hive-sync (keep-alive) + hive-doctor (15-min loop)" \
      || warn "runit install reported a problem — check 'sv status hive-sync'."
  else
    # No termux-services available — keep-alive the daemon by hand so the node
    # still syncs; Termux:Boot (if installed) re-launches it on reboot.
    warn "termux-services (sv) unavailable — falling back to a nohup keep-alive (no auto-respawn)."
    mkdir -p "$HOME/.hive-mind/logs"
    pkill -f hive_sync_daemon.py 2>/dev/null || true
    nohup python3 "$HIVE_DIR/hive_sync_daemon.py" >> "$HOME/.hive-mind/logs/hive-sync.log" 2>&1 &
  fi

elif [[ "$INIT_SYSTEM" == "systemctl" || "$INIT_SYSTEM" == "systemd" ]]; then
  # Hardened daemon unit + self-heal timer (see scripts/platform/linux/_units.sh).
  write_systemd_units "$HIVE_DIR" "$SERVICE_NAME"
  ok "systemd units installed: hive-sync.service (hardened) + hive-doctor.timer"

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
  start) nohup /usr/bin/python3 $HIVE_DIR/hive_sync_daemon.py >> /tmp/hive-sync.log 2>&1 & ;;
  stop)  pkill -f hive_sync_daemon.py || true ;;
  restart) \$0 stop; \$0 start ;;
esac
INITD
  sudo chmod +x "$INITD_SCRIPT"
  sudo update-rc.d "$SERVICE_NAME" defaults 2>/dev/null || true
  ok "init.d service installed: $INITD_SCRIPT"

else
  CRON_LINE="@reboot HIVE_HOME=$HIVE_DIR /usr/bin/python3 $HIVE_DIR/hive_sync_daemon.py >> /tmp/hive-sync.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v "sync_daemon"; echo "$CRON_LINE" ) | crontab -
  ok "@reboot cron entry installed (fallback)"
fi

# ════════════════════════════════════════════════════════════════════════════
# STEP 9 — Start sync daemon
# ════════════════════════════════════════════════════════════════════════════
step "9/$TOTAL  Start sync daemon"

if [[ "$INIT_SYSTEM" == "launchd" ]]; then
  launchd_restart "$SERVICE_NAME"
elif [[ "$INIT_SYSTEM" == "runit" ]]; then
  if command -v sv >/dev/null 2>&1; then
    runit_restart "$SERVICE_NAME"
  fi   # else: the step-8 nohup fallback already started it
elif [[ "$INIT_SYSTEM" == "systemctl" || "$INIT_SYSTEM" == "systemd" ]]; then
  systemctl --user restart "$SERVICE_NAME"
elif [[ "$INIT_SYSTEM" == "initd" ]]; then
  sudo /etc/init.d/$SERVICE_NAME restart
else
  pkill -f hive_sync_daemon.py 2>/dev/null || true
  nohup python3 "$HIVE_DIR/hive_sync_daemon.py" >> /tmp/hive-sync.log 2>&1 &
fi
sleep 2

HELLO=$(curl -sf "http://127.0.0.1:9876/sync/hello" 2>/dev/null) && {
  ok "Daemon responding: $(echo "$HELLO" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("node_id","?"), "—", d.get("journal_entries","?"), "entries")')"
} || {
  if [[ "$INIT_SYSTEM" == "launchd" ]]; then
    warn "Daemon not yet responding on :9876. Check: ~/Library/Logs/hive-mind/hive-sync.log"
  elif [[ "$INIT_SYSTEM" == "runit" ]]; then
    warn "Daemon not yet responding on :9876. Check: ~/.hive-mind/logs/hive-sync.log  (and: sv status hive-sync)"
  else
    warn "Daemon not yet responding on :9876. Check: journalctl --user -u $SERVICE_NAME -n 20"
  fi
}

# ════════════════════════════════════════════════════════════════════════════
# STEP 10 — Hermes memory plugin (best-effort)
# ════════════════════════════════════════════════════════════════════════════
step "10/$TOTAL  Hermes integration"

if command -v hermes &>/dev/null; then
  HERMES_PLUGINS="${HERMES_HOME:-$HOME/.hermes}/plugins"
  mkdir -p "$HERMES_PLUGINS"
  rm -rf "$HERMES_PLUGINS/hive-mind"
  ln -s "$HIVE_DIR/integrations/hermes" "$HERMES_PLUGINS/hive-mind"
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
# macOS defaults to zsh — also persist PATH there so `hv`/`hive-mind` resolve in new shells.
if [ -n "$IS_MAC" ] && ! grep -q 'local/bin' "$HOME/.zshrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
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
if [[ "$INIT_SYSTEM" == "launchd" ]]; then
  echo "  Daemon:  launchctl print gui/\$(id -u)/com.projectmentor.hive-sync"
  echo "  Logs:    ~/Library/Logs/hive-mind/hive-sync.log"
elif [[ "$INIT_SYSTEM" == "runit" ]]; then
  echo "  Daemon:  sv status hive-sync"
  echo "  Logs:    ~/.hive-mind/logs/hive-sync.log"
else
  echo "  Daemon:  systemctl --user status $SERVICE_NAME"
  echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
fi
echo "  Try:     hv stats   (health check: hv doctor)"
[ -z "$BIN_ON_PATH" ] && \
  echo "           ↳ new terminals work automatically; for THIS shell once: source ~/.bashrc"
echo ""
if [ -n "$IS_ANDROID" ]; then
  echo "  Add another device to this hive:"
  echo "    hive-mind invite      (prints the address to paste during its install)"
else
  echo "  SSH to this device from any peer:"
  echo "    tailscale ssh ${THIS_USER}@${THIS_IP}"
  echo "  Add another device to this hive:  hive-mind invite"
fi
echo ""

if [ -f "$PEERS_FILE" ]; then
  PEER_COUNT=$(python3 -c "import json; d=json.load(open('$PEERS_FILE')); print(len(d.get('peers',[])))")
  if [ "$PEER_COUNT" -gt 0 ]; then
    echo "  Peers configured: $PEER_COUNT"
    echo "  Test sync:  cd $HIVE_DIR && ./hv sync now"
    echo ""
  fi
fi
