#!/usr/bin/env bash
# hive-mind WSL node setup — run from inside WSL after 01-windows-setup.bat
# Idempotent — safe to run multiple times
# Does: clone repo, install deps, set up SSH key, start sync daemon

set -euo pipefail

HIVE_DIR="${HIVE_DIR:-$HOME/projects/hive-mind}"
REPO_URL="${REPO_URL:-https://github.com/projectmentor/hive-mind.git}"
PEERS_FILE="$HIVE_DIR/.peers.json"

echo "=== hive-mind WSL node setup ==="
echo

# ---- 1. Clone or update repo ----
if [ -d "$HIVE_DIR/.git" ]; then
    echo "[OK] Repo exists, pulling latest..."
    git -C "$HIVE_DIR" pull --ff-only
else
    echo "Cloning hive-mind repo..."
    mkdir -p "$(dirname "$HIVE_DIR")"
    git clone "$REPO_URL" "$HIVE_DIR"
fi

# ---- 2. Ensure python3 and uv ----
if ! command -v python3 &>/dev/null; then
    echo "Installing python3..."
    sudo apt-get install -y python3
fi
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source "$HOME/.cargo/env" 2>/dev/null || true
fi
echo "[OK] python3 $(python3 --version)"

# ---- 3. Generate SSH key if missing ----
if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
    echo "Generating SSH key..."
    ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519" -N "" -C "$(whoami)@$(hostname)"
fi
PUBKEY=$(cat "$HOME/.ssh/id_ed25519.pub")
echo "[OK] SSH public key: $PUBKEY"

# ---- 4. Install pubkey for Windows OpenSSH ----
# Admin-group users do NOT use ~/.ssh/authorized_keys — Windows OpenSSH reads
# C:\ProgramData\ssh\administrators_authorized_keys (corpus fact #16), and the
# perms must be SYSTEM:(F)+Administrators:(F) with inheritance removed. Writing
# there / running icacls usually needs an elevated shell, so this is best-effort
# with a precise fallback command.
WIN_SSH_DIR="/mnt/c/ProgramData/ssh"
ADMIN_KEYS="$WIN_SSH_DIR/administrators_authorized_keys"
WIN_KEYS_PATH='C:\ProgramData\ssh\administrators_authorized_keys'
if [ -d "$WIN_SSH_DIR" ]; then
    if grep -qsF "$PUBKEY" "$ADMIN_KEYS" 2>/dev/null; then
        echo "[OK] Pubkey already in administrators_authorized_keys"
    elif echo "$PUBKEY" >> "$ADMIN_KEYS" 2>/dev/null; then
        echo "[OK] Appended pubkey to $ADMIN_KEYS"
    else
        echo "[WARN] Could not write $ADMIN_KEYS (needs elevation). In an ELEVATED PowerShell:"
        echo "       Add-Content $WIN_KEYS_PATH '$PUBKEY'"
    fi
    if icacls.exe "$WIN_KEYS_PATH" /inheritance:r /grant 'SYSTEM:(F)' /grant 'BUILTIN\Administrators:(F)' >/dev/null 2>&1; then
        echo "[OK] Set perms on administrators_authorized_keys"
    else
        echo "[WARN] Could not set perms (needs elevation). In an ELEVATED shell:"
        echo "       icacls $WIN_KEYS_PATH /inheritance:r /grant SYSTEM:(F) /grant BUILTIN\\Administrators:(F)"
    fi
else
    echo "[WARN] $WIN_SSH_DIR not found — run scripts/install/01-windows-setup.bat (installs OpenSSH) first."
fi

# ---- 5. Initialize hive-mind DB ----
cd "$HIVE_DIR"
if [ ! -f store.db ]; then
    echo "Initializing database..."
    ./hv rebuild
fi
echo "[OK] store.db exists"
./hv stats

# ---- 6. Configure .peers.json ----
if [ ! -f "$PEERS_FILE" ]; then
    echo
    echo "WARNING: $PEERS_FILE not found."
    echo "Copy .peers.json.example and add peer Tailscale IPs:"
    echo "  cp $HIVE_DIR/.peers.json.example $PEERS_FILE"
    echo "  then edit with peer addresses."
else
    echo "[OK] .peers.json exists"
fi

# ---- 7. Wire Hermes memory plugin ----
# Symlink the hive-mind plugin into Hermes plugins dir and activate it
if command -v hermes &>/dev/null; then
    HERMES_PLUGINS="${HERMES_HOME:-$HOME/.hermes}/plugins"
    mkdir -p "$HERMES_PLUGINS"
    # Remove old copy if present, create symlink to repo
    rm -rf "$HERMES_PLUGINS/hive-mind"
    ln -s "$HIVE_DIR/hermes_plugin" "$HERMES_PLUGINS/hive-mind"
    hermes config set memory.provider hive-mind 2>/dev/null || true
    echo "[OK] Hermes hive-mind memory plugin linked and activated"
else
    echo "[SKIP] Hermes not installed — plugin not wired (run after installing Hermes)"
fi

# ---- 8. Install the sync daemon as a persistent systemd service ----
# Survives WSL restarts (corpus fact #19). Requires systemd (WSL [boot]
# systemd=true). 03-daemon-service.sh generates the unit, enables + starts it,
# and verifies /sync/hello.
if [ -d /run/systemd/system ]; then
    HIVE_DIR="$HIVE_DIR" "$HIVE_DIR/scripts/install/03-daemon-service.sh"
else
    echo "[WARN] systemd not running in this WSL — enable it (/etc/wsl.conf [boot] systemd=true,"
    echo "       then 'wsl --shutdown' from Windows), then run scripts/install/03-daemon-service.sh."
    echo "       Manual fallback: setsid python3 $HIVE_DIR/sync_daemon.py </dev/null >/tmp/hive.log 2>&1 & disown"
fi

echo
echo "=== WSL node setup complete ==="
echo
echo "Next steps:"
echo "  1. Edit $PEERS_FILE with peer Tailscale IPs (if not already done)"
echo "  2. Daemon runs as systemd service 'hive-sync' (journalctl -u hive-sync -f)"
echo "  3. Test from peer: curl http://<this-tailscale-ip>:9876/sync/hello"
