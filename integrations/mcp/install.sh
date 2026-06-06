#!/usr/bin/env bash
# =============================================================================
# Hive Mind MCP — installer  (integrations/mcp/install.sh)
#
# Wires Claude Desktop (Windows) into this WSL node's local Hive Mind corpus
# via a local stdio MCP server. Run inside WSL:
#
#   bash ~/projects/hive-mind/integrations/mcp/install.sh
#
# What it does:
#   1. Verifies `uv` is available and warms the `mcp` SDK dependency.
#   2. Smoke-tests the stdio server end to end.
#   3. Detects your Windows user under /mnt/c/Users.
#   4. Prints — and offers to merge — the claude_desktop_config.json entry.
#
# After running: RESTART Claude Desktop (it loads MCP servers on launch only).
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}[ok]${RST}  $*"; }
info() { echo -e "${BLD}[..]${RST}  $*"; }
warn() { echo -e "${YLW}[!!]${RST}  $*"; }
err()  { echo -e "${RED}[xx]${RST}  $*"; }
step() { echo -e "\n${CYN}──── $* ${RST}"; }

SERVER="$HOME/projects/hive-mind/integrations/mcp/hive_mcp.py"
# The command Claude Desktop runs. ~ expands inside the WSL login shell.
BRIDGE_CMD="uv run --with mcp python ~/projects/hive-mind/integrations/mcp/hive_mcp.py"

echo -e "${BLD}Hive Mind MCP — Claude Desktop on-ramp${RST}"

# ── 1. uv + warm the dep ─────────────────────────────────────────────────────
step "1/4  Dependencies"
if ! command -v uv >/dev/null 2>&1; then
  err "uv not found on PATH. Install it (curl -LsSf https://astral.sh/uv/install.sh | sh) and re-run."
  exit 1
fi
ok "uv: $(command -v uv)"
[ -f "$SERVER" ] || { err "server not found at $SERVER"; exit 1; }
info "warming the mcp SDK (uv caches it)…"
uv run --with mcp python -c "import mcp" >/dev/null 2>&1 && ok "mcp SDK ready" \
  || { err "failed to import mcp under uv"; exit 1; }

# ── 2. Smoke-test the server over stdio ──────────────────────────────────────
step "2/4  Server smoke-test"
SMOKE=$(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"install","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | timeout 90 bash -lc "$BRIDGE_CMD" 2>/dev/null || true)
if echo "$SMOKE" | grep -q '"name":"hive_search"'; then
  ok "server responds over stdio (tools listed)"
else
  err "server smoke-test failed — fix this before wiring Claude Desktop."
  exit 1
fi

# ── 3. Detect the Windows user ───────────────────────────────────────────────
step "3/4  Windows user"
WIN_USER="${HIVE_WIN_USER:-}"
if [ -z "$WIN_USER" ] && [ -d /mnt/c/Users ]; then
  # Real users have an AppData\Roaming\Claude-capable profile; skip system dirs.
  mapfile -t CANDIDATES < <(
    for d in /mnt/c/Users/*/; do
      u=$(basename "$d")
      case "$u" in Public|Default|"Default User"|"All Users"|defaultuser0|desktop.ini) continue;; esac
      [ -d "${d}AppData/Roaming" ] && echo "$u"
    done
  )
  if [ "${#CANDIDATES[@]}" -eq 1 ]; then
    WIN_USER="${CANDIDATES[0]}"
  elif [ "${#CANDIDATES[@]}" -gt 1 ]; then
    warn "multiple Windows users found: ${CANDIDATES[*]}"
    warn "re-run with HIVE_WIN_USER=<name> to pick one; using '${CANDIDATES[0]}' for the printed path."
    WIN_USER="${CANDIDATES[0]}"
  fi
fi
if [ -n "$WIN_USER" ]; then
  ok "Windows user: $WIN_USER"
  CFG_DIR="/mnt/c/Users/$WIN_USER/AppData/Roaming/Claude"
  CFG="$CFG_DIR/claude_desktop_config.json"
else
  warn "could not auto-detect the Windows user; you'll merge the snippet by hand."
  CFG=""
fi

# ── 4. Print + offer to merge the config ─────────────────────────────────────
step "4/4  Claude Desktop config"
SNIPPET=$(cat <<'JSON'
{
  "mcpServers": {
    "hive-memory": {
      "command": "wsl.exe",
      "args": ["-e", "bash", "-lc", "uv run --with mcp python ~/projects/hive-mind/integrations/mcp/hive_mcp.py"]
    }
  }
}
JSON
)
echo "Add this to your Claude Desktop config (Windows):"
echo "  %APPDATA%\\Claude\\claude_desktop_config.json"
echo "$SNIPPET"

if [ -n "$CFG" ]; then
  echo
  read -r -p "Merge the hive-memory entry into $CFG now? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    mkdir -p "$CFG_DIR"
    [ -f "$CFG" ] && cp "$CFG" "$CFG.bak.$$" && info "backed up existing config -> $CFG.bak.$$"
    # Merge with python3 so we don't clobber other servers/keys.
    python3 - "$CFG" <<'PY'
import json, sys, os
path = sys.argv[1]
entry = {"command": "wsl.exe",
         "args": ["-e", "bash", "-lc",
                  "uv run --with mcp python ~/projects/hive-mind/integrations/mcp/hive_mcp.py"]}
cfg = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            cfg = json.load(f) or {}
    except json.JSONDecodeError:
        print("  existing config is not valid JSON — refusing to overwrite. Merge by hand.")
        sys.exit(2)
cfg.setdefault("mcpServers", {})["hive-memory"] = entry
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("  merged hive-memory into mcpServers")
PY
    ok "config updated"
  else
    info "skipped — paste the snippet yourself when ready."
  fi
fi

echo
echo -e "${BLD}Next:${RST} fully RESTART Claude Desktop, then ask it to run hive_stats."
echo "      (MCP servers load on launch only — no live reload.)"
