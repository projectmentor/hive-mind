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

# ---- 4. Add pubkey to Windows Admin authorized_keys ----
# Discover Windows username from /mnt/c/Users/
WIN_USER=$(ls /mnt/c/Users/ | grep -v -E '^(Public|Default|All Users|Default User)$' | head -1)
WIN_AUTH_KEYS="/mnt/c/Users/$WIN_USER/.ssh/authorized_keys"
mkdir -p "/mnt/c/Users/$WIN_USER/.ssh"
if [ -f "$WIN_AUTH_KEYS" ]; then
    if ! grep -qF "$PUBKEY" "$WIN_AUTH_KEYS"; then
        echo "$PUBKEY" >> "$WIN_AUTH_KEYS"
        echo "[OK] Added pubkey to $WIN_AUTH_KEYS"
    else
        echo "[OK] Pubkey already in $WIN_AUTH_KEYS"
    fi
else
    echo "$PUBKEY" > "$WIN_AUTH_KEYS"
    echo "[OK] Created $WIN_AUTH_KEYS with pubkey"
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

# ---- 8. Test sync daemon starts ----

echo
echo "Testing sync daemon (5 second check)..."
pkill -f sync_daemon.py 2>/dev/null || true
sleep 1
python3 "$HIVE_DIR/sync_daemon.py" &
DAEMON_PID=$!
sleep 2
if kill -0 $DAEMON_PID 2>/dev/null; then
    RESPONSE=$(curl -s --max-time 3 http://127.0.0.1:9876/sync/hello || echo "FAIL")
    kill $DAEMON_PID 2>/dev/null
    wait $DAEMON_PID 2>/dev/null || true
    if echo "$RESPONSE" | grep -q node_id; then
        echo "[OK] Sync daemon responds correctly"
    else
        echo "[WARN] Sync daemon started but /sync/hello failed: $RESPONSE"
    fi
else
    echo "[FAIL] Sync daemon did not start — check sync_daemon.py"
    exit 1
fi

echo
echo "=== WSL node setup complete ==="
echo
echo "Next steps:"
echo "  1. Edit $PEERS_FILE with peer Tailscale IPs"
echo "  2. Start daemon: cd $HIVE_DIR && ./hv sync daemon"
echo "  3. Test from peer: curl http://<this-tailscale-ip>:9876/sync/hello"
